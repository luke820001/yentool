"""
sandbox_chip_manage.py -- does the DAY's institutional flow tell a holder
what to do TOMORROW (sell, add, or nothing)?  ASCII only.

User question 2026-09-21: "the recommendation should look at the day's chips
and say whether to sell or add the next day". Everything here is replayed on
the SAME trades as the stop/ladder study (archive/research/sandbox_scale_ladder.py
build -> %TEMP%/yentool_ladder/picks.pkl): CORE+ first-day entries, next-open
fill, and the adopted exit stack (stop 20 / tp 20 / arm 6 / lock 2 / hold 10),
so every overlay is PAIRED against the rule as shipped.

Institutional flow comes from the per-date backfill
(tools/backfill_inst_history.py -> data/research_inst_otc.db, 2018-03+). A
day's flow is known after the close, so a decision taken on it executes at
the NEXT open -- that is the only timing tested.

Flow is normalised as a percentage of the stock's previous 20-session
average volume (inst_pct = net lots / avg lots * 100), so "-2" means the
three institutions net sold 2% of a normal day's volume.

Commands (run from the repo root):
  python archive/research/sandbox_chip_manage.py manage   # sell / add / extend overlays
  python archive/research/sandbox_chip_manage.py entry    # signal-day flow as entry filter
  python archive/research/sandbox_chip_manage.py pop      # whole-OTC next-day predictiveness
"""
import json
import os
import pickle
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.getcwd())
from scanner.exit_rules import replay_exit  # noqa: E402

CACHE_DIR = os.environ.get("LADDER_CACHE", os.path.join(
    os.environ.get("TEMP", "."), "yentool_ladder"))
PICKS_PKL = os.path.join(CACHE_DIR, "picks.pkl")
RESEARCH_DB = os.path.join("data", "research_prices.db")
INST_DB = os.path.join("data", "research_inst_otc.db")
NAMES = os.path.join("data", "stock_names.json")

RECENT_FROM = "2023-09-18"
INST_FROM = "2018-04-02"          # first full month of TPEX history
HOLD = 10
EXT_HOLD = 20
BUY_COST = 0.001425
SELL_COST = 0.001425 + 0.003
# The ADOPTED stack, read from the shipped rule so this study can never be
# run against parameters the app no longer uses (2026-09-21).
from scanner.exit_rules import DEFAULT_RULE as _RULE  # noqa: E402
STACK = dict(stop_pct=_RULE["stop_pct"], tp_pct=_RULE["tp_pct"],
             arm_pct=_RULE["arm_pct"], lock_pct=_RULE["lock_pct"])
SEED = 7


# ------------------------------------------------------------------ data
def load_trades():
    trades = pickle.load(open(PICKS_PKL, "rb"))
    return [t for t in trades if t["sig"] >= INST_FROM and len(t["o"]) >= HOLD]


def load_inst(sids):
    conn = sqlite3.connect(INST_DB)
    q = ("SELECT stock_id, date, Foreign_Net, Trust_Net, Dealer_Net, Inst_Net "
         "FROM data WHERE board='OTC' AND stock_id IN (%s)" % ",".join("?" * len(sids)))
    d = pd.read_sql_query(q, conn, params=list(sids))
    days = pd.read_sql_query("SELECT date, rows FROM fetched WHERE board='OTC'", conn)
    conn.close()
    have = set(days[days["rows"] > 0]["date"])
    key = list(zip(d["stock_id"].astype(str), d["date"].astype(str)))
    vals = d[["Foreign_Net", "Trust_Net", "Dealer_Net", "Inst_Net"]].to_numpy(float)
    return dict(zip(key, vals)), have


def load_bars(sids):
    """Per sid: DataFrame(date, close, vol, vol20_prev) from the research db."""
    conn = sqlite3.connect(RESEARCH_DB)
    q = ("SELECT stock_id, date, close, Volume_Lot FROM data WHERE stock_id IN (%s) "
         "ORDER BY stock_id, date" % ",".join("?" * len(sids)))
    d = pd.read_sql_query(q, conn, params=list(sids))
    conn.close()
    d = d[d["Volume_Lot"] > 0]
    out = {}
    for sid, g in d.groupby("stock_id"):
        g = g.reset_index(drop=True)
        v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        g["vol20_prev"] = v.rolling(20).mean().shift(1)
        out[str(sid)] = g.set_index("date")
    return out


def build_features(trades):
    """Attach per-bar flow arrays to each trade (index k+1 for bar k, k=-1
    being the signal day). NaN where the board has no table that day."""
    sids = sorted({t["sid"] for t in trades})
    inst, have = load_inst(sids)
    bars = load_bars(sids)
    kept = []
    for t in trades:
        g = bars.get(t["sid"])
        if g is None or t["sig"] not in g.index:
            continue
        dates = [t["sig"]] + list(t["dates"][:EXT_HOLD])
        n = len(dates)
        f = {k: np.full(n, np.nan) for k in ("inst", "fn", "tn", "dn", "ret", "vol20")}
        closes = []
        for i, d in enumerate(dates):
            v20 = float(g["vol20_prev"].get(d, np.nan)) if d in g.index else np.nan
            f["vol20"][i] = v20
            if d in have and v20 and v20 > 0:
                row = inst.get((t["sid"], d))
                fn, tn, dn, tot = (row if row is not None else (0.0, 0.0, 0.0, 0.0))
                f["inst"][i] = tot / v20 * 100
                f["fn"][i] = fn / v20 * 100
                f["tn"][i] = tn / v20 * 100
                f["dn"][i] = dn / v20 * 100
            closes.append(float(g["close"].get(d, np.nan)) if d in g.index else np.nan)
        closes = np.array(closes)
        with np.errstate(invalid="ignore", divide="ignore"):
            f["ret"][1:] = closes[1:] / closes[:-1] - 1
        t = dict(t)
        t["F"] = f
        t["sig_close"] = closes[0]
        kept.append(t)
    return kept


# ------------------------------------------------------------------ engine
ENGINE = os.environ.get("CHIP_ENGINE", "live")   # live | nextbar


def replay_nextbar(o, h, l, c, hold_bars, stop_pct, tp_pct, arm_pct, lock_pct):
    """Same stack, but the lock ARMS at the close: an end-of-day trader who
    sees +6% on the arming day places the +2% stop for the NEXT session, so
    the arming bar itself can never be stopped on the lock. Everything else
    (open-through, lowest-touched, time exit) as in exit_rules.replay_exit."""
    E = float(o[0])
    stop = E * (1 - stop_pct)
    target = E * (1 + tp_pct)
    arm_px, lock_px = E * (1 + arm_pct), E * (1 + lock_pct)
    armed = False
    n = min(hold_bars, len(o))
    for i in range(n):
        op, hi, lo = float(o[i]), float(h[i]), float(l[i])
        if op >= target:
            return dict(bar=i, exit_price=op, reason="tp", ret_pct=(op / E - 1) * 100)
        if op <= stop:
            return dict(bar=i, exit_price=op, reason="lock" if armed else "stop",
                        ret_pct=(op / E - 1) * 100)
        if lo <= stop:
            return dict(bar=i, exit_price=stop, reason="lock" if armed else "stop",
                        ret_pct=(stop / E - 1) * 100)
        if hi >= target:
            return dict(bar=i, exit_price=target, reason="tp", ret_pct=(target / E - 1) * 100)
        if not armed and hi >= arm_px:
            armed = True
            stop = max(stop, lock_px)
    final = float(c[n - 1])
    return dict(bar=n - 1, exit_price=final, reason="time", ret_pct=(final / E - 1) * 100)


def _same_bar_lock(p, o, h, arm_px):
    """True when the live replay booked a lock on the very bar that armed it
    (the bar's high first reached +6% and its low then touched +2%)."""
    if p.get("reason") != "lock" or p.get("bar") is None:
        return False
    b = p["bar"]
    return all(float(h[i]) < arm_px for i in range(b)) and float(h[b]) >= arm_px


def base_plan(t, hold=HOLD):
    o, h, l, c = (t[x][:hold] for x in ("o", "h", "l", "c"))
    if ENGINE == "nextbar":
        return replay_nextbar(o, h, l, c, hold, **STACK)
    p = replay_exit(o, h, l, c, hold_bars=hold, **STACK)
    if ENGINE == "eod" and _same_bar_lock(p, o, h, float(o[0]) * (1 + STACK["arm_pct"])):
        # What the phone user actually does with "lock exit booked" on the
        # arming day: they read it after the close and sell at the NEXT open.
        b = p["bar"]
        if b + 1 < len(t["o"]):
            px = float(t["o"][b + 1])
            p = dict(p, bar=b + 1, exit_price=px, ret_pct=(px / float(o[0]) - 1) * 100,
                     reason="lock")
    return p


def net_ret(entry, exit_px):
    return exit_px * (1 - SELL_COST) / (entry * (1 + BUY_COST)) - 1


def cycle_pnl(entry, exit_px, adds):
    """Budget-normalised cycle: 1.0 at entry plus (weight, price) adds, all
    sold at exit_px. Returns (pnl, capital)."""
    shares = 1.0 / entry
    cost = 1.0 * (1 + BUY_COST)
    cap = 1.0
    for w, px in adds:
        shares += w / px
        cost += w * (1 + BUY_COST)
        cap += w
    return shares * exit_px * (1 - SELL_COST) - cost, cap


def fk(t, name, k):
    """Feature `name` at bar k (k=-1 signal day)."""
    return t["F"][name][k + 1]


def pnl_at(t, k):
    return t["c"][k] / t["o"][0] - 1


def streak(t, k, sign):
    n = 0
    for j in range(k, -2, -1):
        v = fk(t, "inst", j)
        if v == v and v * sign > 0:
            n += 1
        else:
            break
    return n


# Conditions: (label, fn(t, k) -> bool). k is the bar whose CLOSE we stand at.
def _ok(v):
    return v == v   # not NaN


SELL_RULES = [
    ("S1  inst net sold today (any)",          lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) < 0),
    ("S2  inst sold >=1% vol",                 lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -1),
    ("S3  inst sold >=2% vol",                 lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -2),
    ("S4  inst sold >=3% vol",                 lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -3),
    ("S5  inst sold >=5% vol",                 lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -5),
    ("S6  inst sold 2 days in a row",          lambda t, k: streak(t, k, -1) >= 2),
    ("S7  inst sold 3 days in a row",          lambda t, k: streak(t, k, -1) >= 3),
    ("S8  foreign sold >=2% vol",              lambda t, k: _ok(fk(t, "fn", k)) and fk(t, "fn", k) <= -2),
    ("S9  trust sold (any)",                   lambda t, k: _ok(fk(t, "tn", k)) and fk(t, "tn", k) < 0),
    ("S10 cum inst since entry <=-3%",         lambda t, k: _ok(fk(t, "inst", k)) and np.nansum(t["F"]["inst"][1:k + 2]) <= -3),
    ("S11 sold>=2% on a DOWN day",             lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -2 and fk(t, "ret", k) < 0),
    ("S12 sold>=2% on an UP day",              lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -2 and fk(t, "ret", k) > 0),
    ("S13 sold>=2% while under water",         lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -2 and pnl_at(t, k) < 0),
    ("S14 sold>=2% while in profit",           lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -2 and pnl_at(t, k) > 0),
    ("S15 sold>=1% AND under water",           lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) <= -1 and pnl_at(t, k) < 0),
]

ADD_RULES = [
    ("A1  inst net bought today (any)",        lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) > 0),
    ("A2  inst bought >=1% vol",               lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 1),
    ("A3  inst bought >=2% vol",               lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 2),
    ("A4  inst bought >=3% vol",               lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 3),
    ("A5  inst bought >=5% vol",               lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 5),
    ("A6  inst bought 2 days in a row",        lambda t, k: streak(t, k, 1) >= 2),
    ("A7  inst bought 3 days in a row",        lambda t, k: streak(t, k, 1) >= 3),
    ("A8  foreign bought >=2% vol",            lambda t, k: _ok(fk(t, "fn", k)) and fk(t, "fn", k) >= 2),
    ("A9  trust bought (any)",                 lambda t, k: _ok(fk(t, "tn", k)) and fk(t, "tn", k) > 0),
    ("A10 foreign AND trust bought",           lambda t, k: _ok(fk(t, "fn", k)) and fk(t, "fn", k) > 0 and fk(t, "tn", k) > 0),
    ("A11 bought>=2% on a DOWN day",           lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 2 and fk(t, "ret", k) < 0),
    ("A12 bought>=2% on an UP day",            lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 2 and fk(t, "ret", k) > 0),
    ("A13 bought>=2% while under water",       lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 2 and pnl_at(t, k) < 0),
    ("A14 bought>=2% while in profit",         lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 2 and pnl_at(t, k) > 0),
    ("A15 bought>=1% AND under water",         lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 1 and pnl_at(t, k) < 0),
]


def run_sell(t, cond, last_k=HOLD - 2, only_if=None):
    """Exit at next open the first time cond fires before the base exit."""
    p = base_plan(t)
    eb, ep = p["bar"], p["exit_price"]
    E = t["o"][0]
    for k in range(0, min(last_k, eb - 1) + 1):
        if cond(t, k) and (only_if is None or only_if(t, k)):
            return net_ret(E, t["o"][k + 1]), True, k
    return net_ret(E, ep), False, None


def run_add(t, cond, weight=0.5, last_k=HOLD - 2, first_frac=1.0):
    """Add `weight` (budget units) at next open the first time cond fires
    before the base exit; the whole position leaves at the base exit."""
    p = base_plan(t)
    eb, ep = p["bar"], p["exit_price"]
    E = t["o"][0]
    adds = []
    fired_k = None
    for k in range(0, min(last_k, eb - 1) + 1):
        if cond(t, k):
            adds.append((weight, t["o"][k + 1]))
            fired_k = k
            break
    shares = first_frac / E
    cost = first_frac * (1 + BUY_COST)
    cap = first_frac
    for w, px in adds:
        shares += w / px
        cost += w * (1 + BUY_COST)
        cap += w
    pnl = shares * ep * (1 - SELL_COST) - cost
    add_only = (net_ret(adds[0][1], ep) if adds else None)
    return pnl, cap, fired_k, add_only


def run_price_add(t, first_frac=0.5, weight=0.5, level=0.90):
    """The shipped optional plan: half at the open, rest at fill x 0.90."""
    p = base_plan(t)
    eb, ep = p["bar"], p["exit_price"]
    E = t["o"][0]
    lim = E * level
    adds = []
    for k in range(0, eb + 1):
        o, l = t["o"][k], t["l"][k]
        if k == 0:
            continue
        if o <= lim:
            adds.append((weight, o)); break
        if l <= lim and not (k == eb and p["reason"] != "stop"):
            adds.append((weight, lim)); break
    shares = first_frac / E
    cost = first_frac * (1 + BUY_COST)
    cap = first_frac
    for w, px in adds:
        shares += w / px
        cost += w * (1 + BUY_COST)
        cap += w
    return shares * ep * (1 - SELL_COST) - cost, cap


def run_extend(t, cond):
    """At the time-exit close (bar 9), keep holding to bar 19 under the same
    stack when cond holds; otherwise the base result."""
    p = base_plan(t)
    E = t["o"][0]
    if p["reason"] != "time" or len(t["o"]) < EXT_HOLD:
        return net_ret(E, p["exit_price"]), False
    if not cond(t, HOLD - 1):
        return net_ret(E, p["exit_price"]), False
    q = base_plan(t, hold=EXT_HOLD)
    return net_ret(E, q["exit_price"]), True


# ------------------------------------------------------------------ stats
def wl(x):
    x = np.asarray(x, float)
    return 100 * (x > 0).mean(), x.mean()


def boot_ci(d, n=2000):
    d = np.asarray(d, float)
    if len(d) == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(SEED)
    m = [rng.choice(d, len(d), replace=True).mean() for _ in range(n)]
    return np.percentile(m, 2.5), np.percentile(m, 97.5)


def windows(trades):
    sigs = np.array([t["sig"] for t in trades])
    rec = sigs >= RECENT_FROM
    out = {"RECENT": rec, "OLD": ~rec, "ALL": np.ones(len(trades), bool)}
    return out


def report_overlay(label, base, over, fired, sigs, kind="ret"):
    """base/over: per-trade returns (ret) or (pnl, cap) pairs (cycle)."""
    lines = []
    for wname, mask in (("RECENT", sigs >= RECENT_FROM), ("OLD", sigs < RECENT_FROM)):
        b = np.asarray(base)[mask]
        o = np.asarray(over)[mask]
        f = np.asarray(fired)[mask]
        if kind == "cycle":
            bw, bm = 100 * (b[:, 0] > 0).mean(), (b[:, 0] / b[:, 1]).mean() * 100
            ow, om = 100 * (o[:, 0] > 0).mean(), (o[:, 0] / o[:, 1]).mean() * 100
            d = o[:, 0] / o[:, 1] * 100 - b[:, 0] / b[:, 1] * 100
        else:
            bw, bm = wl(b * 100)
            ow, om = wl(o * 100)
            d = (o - b) * 100
        # halves inside the window (by signal date order)
        idx = np.where(mask)[0]
        half = len(idx) // 2
        h = []
        for part in (idx[:half], idx[half:]):
            if kind == "cycle":
                h.append((100 * (np.asarray(over)[part][:, 0] > 0).mean(),
                          100 * (np.asarray(base)[part][:, 0] > 0).mean()))
            else:
                h.append((100 * (np.asarray(over)[part] > 0).mean(),
                          100 * (np.asarray(base)[part] > 0).mean()))
        lo, hi = boot_ci(d[f]) if f.any() else (np.nan, np.nan)
        lines.append(
            "  %-7s n=%3d fired=%3d | base %5.1f%% %+5.2f | rule %5.1f%% %+5.2f "
            "| dwin %+4.1f dmean %+5.2f (fired-only CI %+.2f..%+.2f) | h1 %4.1f/%4.1f h2 %4.1f/%4.1f"
            % (wname, len(b), int(f.sum()), bw, bm, ow, om, ow - bw, om - bm, lo, hi,
               h[0][0], h[0][1], h[1][0], h[1][1]))
    print(label)
    print("\n".join(lines))


# ------------------------------------------------------------------ commands
def cmd_manage():
    trades = build_features(load_trades())
    sigs = np.array([t["sig"] for t in trades])
    cov = np.mean([np.isfinite(t["F"]["inst"][1:HOLD + 1]).mean() for t in trades])
    print("trades %d (%s..%s), inst coverage during hold %.0f%%" % (
        len(trades), min(sigs), max(sigs), cov * 100))
    base = np.array([net_ret(t["o"][0], base_plan(t)["exit_price"]) for t in trades])
    print("base: RECENT %5.1f%% %+5.2f (n=%d) | OLD %5.1f%% %+5.2f (n=%d)" % (
        *wl(base[sigs >= RECENT_FROM] * 100), (sigs >= RECENT_FROM).sum(),
        *wl(base[sigs < RECENT_FROM] * 100), (sigs < RECENT_FROM).sum()))

    print("\n=== SELL overlays: exit at NEXT OPEN the first day the condition holds ===")
    for label, cond in SELL_RULES:
        res = [run_sell(t, cond) for t in trades]
        report_overlay(label, base, [r[0] for r in res], [r[1] for r in res], sigs)
    print("\n--- same, but only in the first 5 hold days (k<=4) ---")
    for label, cond in SELL_RULES[:6]:
        res = [run_sell(t, cond, last_k=4) for t in trades]
        report_overlay(label, base, [r[0] for r in res], [r[1] for r in res], sigs)

    print("\n=== ADD overlays: full position at entry, +50% at NEXT OPEN when the condition holds ===")
    print("(rule mean is return per unit of capital deployed; 'add-only' is the added tranche's own return)")
    base_c = np.array([(net_ret(t["o"][0], base_plan(t)["exit_price"]), 1.0) for t in trades])
    for label, cond in ADD_RULES:
        res = [run_add(t, cond) for t in trades]
        over = np.array([(r[0], r[1]) for r in res])
        fired = np.array([r[2] is not None for r in res])
        report_overlay(label, base_c, over, fired, sigs, kind="cycle")
        ao = np.array([r[3] for r in res if r[3] is not None]) * 100
        if len(ao):
            for wname, m in (("RECENT", sigs >= RECENT_FROM), ("OLD", sigs < RECENT_FROM)):
                sel = np.array([r[3] for r, mm in zip(res, m) if mm and r[3] is not None]) * 100
                if len(sel):
                    print("          add-only %-6s n=%3d win %5.1f%% mean %+5.2f" % (wname, len(sel), *wl(sel)))
    # controls: unconditional add at bar 1 open, and the shipped price ladder
    res = [run_add(t, lambda t, k: k == 0) for t in trades]
    report_overlay("CTRL unconditional +50% at bar-1 open", base_c,
                   np.array([(r[0], r[1]) for r in res]), np.ones(len(trades), bool), sigs, kind="cycle")

    print("\n=== STAGED entry: half at the open, second half by chips vs by price (fill x 0.90) ===")
    half = np.array([(0.5 * net_ret(t["o"][0], base_plan(t)["exit_price"]), 0.5) for t in trades])
    price = np.array([run_price_add(t) for t in trades])
    report_overlay("shipped: half + rest at fill x 0.90", half, price,
                   np.array([p[1] > 0.5 for p in price]), sigs, kind="cycle")
    for label, cond in ADD_RULES:
        res = [run_add(t, cond, weight=0.5, first_frac=0.5) for t in trades]
        report_overlay("half + rest when " + label, half,
                       np.array([(r[0], r[1]) for r in res]),
                       np.array([r[2] is not None for r in res]), sigs, kind="cycle")

    print("\n=== EXTEND: at the day-10 time exit keep holding (cap 20) when ... ===")
    ext_rules = [
        ("inst net bought that day",       lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) > 0),
        ("inst bought >=2% that day",      lambda t, k: _ok(fk(t, "inst", k)) and fk(t, "inst", k) >= 2),
        ("inst 5-day sum > 0",             lambda t, k: np.nansum(t["F"]["inst"][k - 3:k + 2]) > 0),
        ("inst bought 3 days in a row",    lambda t, k: streak(t, k, 1) >= 3),
        ("CTRL always extend to 20",       lambda t, k: True),
    ]
    for label, cond in ext_rules:
        res = [run_extend(t, cond) for t in trades]
        report_overlay(label, base, [r[0] for r in res], [r[1] for r in res], sigs)


def cmd_entry():
    trades = build_features(load_trades())
    sigs = np.array([t["sig"] for t in trades])
    base = np.array([net_ret(t["o"][0], base_plan(t)["exit_price"]) for t in trades]) * 100
    rules = [
        ("signal-day inst net > 0",        lambda t: _ok(fk(t, "inst", -1)) and fk(t, "inst", -1) > 0),
        ("signal-day inst net <= 0",       lambda t: _ok(fk(t, "inst", -1)) and fk(t, "inst", -1) <= 0),
        ("signal-day inst >= 2% vol",      lambda t: _ok(fk(t, "inst", -1)) and fk(t, "inst", -1) >= 2),
        ("signal-day inst <= -2% vol",     lambda t: _ok(fk(t, "inst", -1)) and fk(t, "inst", -1) <= -2),
        ("signal-day foreign > 0",         lambda t: _ok(fk(t, "fn", -1)) and fk(t, "fn", -1) > 0),
        ("signal-day trust > 0",           lambda t: _ok(fk(t, "tn", -1)) and fk(t, "tn", -1) > 0),
        ("signal-day foreign AND trust > 0", lambda t: _ok(fk(t, "fn", -1)) and fk(t, "fn", -1) > 0 and fk(t, "tn", -1) > 0),
        ("no flow data",                   lambda t: not _ok(fk(t, "inst", -1))),
    ]
    print("trades %d; base RECENT %5.1f%% %+5.2f | OLD %5.1f%% %+5.2f" % (
        len(trades), *wl(base[sigs >= RECENT_FROM]), *wl(base[sigs < RECENT_FROM])))
    for label, cond in rules:
        m = np.array([bool(cond(t)) for t in trades])
        parts = []
        for wname, w in (("RECENT", sigs >= RECENT_FROM), ("OLD", sigs < RECENT_FROM)):
            sel = base[m & w]
            parts.append("%s n=%3d win %5.1f%% mean %+5.2f" % ((wname,) + ((len(sel),) + wl(sel) if len(sel) else (0, np.nan, np.nan))))
        print("  %-36s | %s | %s" % (label, parts[0], parts[1]))


def cmd_pop():
    """Whole-OTC cross-sectional test: rank-correlation between today's flow
    and tomorrow's return, per day, averaged. If this is ~0 there is nothing
    for a holder to act on; if it is positive the overlays above are the
    place it should show up."""
    names = json.load(open(NAMES, encoding="utf-8"))
    otc = [k for k, v in names.items() if isinstance(v, list) and len(v) > 1 and v[1] == "OTC"]
    conn = sqlite3.connect(RESEARCH_DB)
    px = pd.read_sql_query(
        "SELECT stock_id, date, open, close, Volume_Lot FROM data WHERE date >= ? "
        "AND stock_id IN (%s)" % ",".join("?" * len(otc)), conn, params=[INST_FROM] + otc)
    conn.close()
    px = px[px["Volume_Lot"] > 0].sort_values(["stock_id", "date"])
    g = px.groupby("stock_id")
    px["vol20"] = g["Volume_Lot"].transform(lambda s: s.rolling(20).mean().shift(1))
    px["turn20"] = px["vol20"] * px["close"] * 1000
    px["ret1"] = g["close"].shift(-1) / px["close"] - 1
    px["oc1"] = g["close"].shift(-1) / g["open"].shift(-1) - 1
    px["ret5"] = g["close"].shift(-5) / px["close"] - 1
    px["h52"] = g["close"].transform(lambda s: s.rolling(252, min_periods=120).max())
    px["dist52"] = (px["h52"] - px["close"]) / px["h52"]
    conn = sqlite3.connect(INST_DB)
    f = pd.read_sql_query("SELECT stock_id, date, Foreign_Net, Trust_Net, Inst_Net FROM data "
                          "WHERE board='OTC'", conn)
    conn.close()
    d = px.merge(f, on=["stock_id", "date"], how="inner")
    d = d[(d["vol20"] > 0) & (d["turn20"] >= 5e7)]
    for c in ("Foreign_Net", "Trust_Net", "Inst_Net"):
        d[c + "_pct"] = d[c] / d["vol20"] * 100

    def ic(sub, x, y):
        vals = []
        for _, day in sub.groupby("date"):
            if len(day) < 30:
                continue
            vals.append(day[x].rank().corr(day[y].rank()))
        # A day whose ranks are constant gives NaN; one such day used to turn
        # the whole window's mean into NaN.
        vals = np.array(vals, dtype=float)
        vals = vals[np.isfinite(vals)]
        if len(vals) < 2:
            return float("nan"), float("nan"), len(vals)
        return vals.mean(), vals.mean() / (vals.std() / np.sqrt(len(vals))), len(vals)

    def bucket(sub, x, y):
        q = sub[x]
        rows = []
        for label, m in (("<=-3", q <= -3), ("-3..-1", (q > -3) & (q <= -1)), ("-1..0", (q > -1) & (q < 0)),
                         ("0", q == 0), ("0..1", (q > 0) & (q < 1)), ("1..3", (q >= 1) & (q < 3)), (">=3", q >= 3)):
            s = sub[m][y]
            rows.append("%6s n=%6d win %5.1f%% mean %+5.3f" % (label, len(s), 100 * (s > 0).mean(), s.mean() * 100))
        return "\n".join(rows)

    for wname, m in (("RECENT", d["date"] >= RECENT_FROM), ("OLD", d["date"] < RECENT_FROM)):
        sub = d[m]
        print("=== %s: OTC universe (turnover >= 50M), %d stock-days ===" % (wname, len(sub)))
        for x in ("Inst_Net_pct", "Foreign_Net_pct", "Trust_Net_pct"):
            for y in ("ret1", "oc1", "ret5"):
                mu, tt, n = ic(sub, x, y)
                print("  IC %-16s -> %-4s  mean %+.4f  t=%+5.1f  days=%d" % (x, y, mu, tt, n))
        print("  next-day close-to-close by today's inst %% of volume:")
        print(bucket(sub, "Inst_Net_pct", "ret1"))
        near = sub[sub["dist52"] <= 0.10]
        print("  -- near 52w high (<=10%%) only, %d stock-days, next-day open-to-close:" % len(near))
        print(bucket(near, "Inst_Net_pct", "oc1"))


def cmd_engines():
    """How much of the adopted numbers is the unknowable same-bar order?
    live = exit_rules.replay_exit (the arming bar can be stopped on the lock
    it just armed); nextbar = the lock only guards from the next session."""
    global ENGINE
    trades = load_trades()
    sigs = np.array([t["sig"] for t in trades])
    for eng in ("live", "eod", "nextbar"):
        ENGINE = eng
        plans = [base_plan(t) for t in trades]
        rets = np.array([net_ret(t["o"][0], p["exit_price"]) for t, p in zip(trades, plans)]) * 100
        reasons = pd.Series([p["reason"] for p in plans])
        for wname, m in (("RECENT", sigs >= RECENT_FROM), ("OLD", sigs < RECENT_FROM)):
            w, mu = wl(rets[m])
            mix = reasons[m].value_counts(normalize=True).round(2).to_dict()
            print("  %-8s %-7s n=%3d win %5.1f%% mean %+5.2f  exits %s" % (eng, wname, m.sum(), w, mu, mix))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "manage"
    {"manage": cmd_manage, "entry": cmd_entry, "pop": cmd_pop,
     "engines": cmd_engines}[cmd]()
