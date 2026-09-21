"""
sandbox_entry_gate.py -- re-select the ENTRY gate on the corrected exit engine.
ASCII only.

Why this exists. The CORE+ gate (within 5% of the 52-week high, 5-day return
<= 5%, ATR >= 4.5%), the rank-20 cut and the 10-bar hold were all chosen on the
simulator that armed the trailing lock intraday and let the same bar be stopped
on it. That simulator has now been shown to overstate the win rate by ~6.7
points and to understate the mean (scanner/exit_rules.py, 2026-09-21). Every
threshold selected against it is therefore "not wrongly chosen, but chosen
against the wrong scoreboard" -- exactly the status the lock parameters had
before they were re-selected and gained 4 points.

This file caches the pick universe WITHOUT the quality gate so the thresholds
can be swept, and evaluates every candidate gate through the shipped exit stack
(scanner.exit_rules.replay_exit) plus the adopted ride rule.

Discipline, unchanged from the rest of the project:
  * two windows (recent 3 years primary, older window as a direction check),
  * win / bootLo / both halves must all beat the base,
  * quarterly stability,
  * a plateau rather than a peak,
  * and the slippage stress that caught the fake optimum in the lock study.

Commands:
  python archive/research/sandbox_entry_gate.py build   (slow, once)
  python archive/research/sandbox_entry_gate.py base
  python archive/research/sandbox_entry_gate.py sweep   (one axis at a time)
  python archive/research/sandbox_entry_gate.py joint   (the adoption gate)
"""
import json
import os
import pickle
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join("archive", "research"))
from scanner.exit_rules import DEFAULT_RULE, replay_exit  # noqa: E402

RESEARCH_DB = os.path.join("data", "research_prices.db")
CACHE_DIR = os.environ.get("GATE_CACHE", os.path.join(
    os.environ.get("TEMP", "."), "yentool_gate"))
PICKS_PKL = os.path.join(CACHE_DIR, "universe.pkl")

WARMUP = "2017-03-01"
EVAL_FROM = "2017-06-01"
RECENT_FROM = "2023-09-18"
FWD_BARS = 26
RANK_POOL = 60            # cache this deep so the rank cut itself can be swept

HOLD = DEFAULT_RULE["hold_bars"]
STOP = DEFAULT_RULE["stop_pct"]
TP = DEFAULT_RULE["tp_pct"]
ARM = DEFAULT_RULE["arm_pct"]
LOCK = DEFAULT_RULE["lock_pct"]
BUY_COST = 0.001425
SELL_COST = 0.001425 + 0.003
SEED = 7

# The gate as shipped today.
BASE_GATE = {"rank": 20, "dist52": 0.05, "ret5": 0.05, "atr": 4.5}


# ------------------------------------------------------------------- build
def build():
    import eval_realtrade as er
    os.makedirs(CACHE_DIR, exist_ok=True)
    er.DB = RESEARCH_DB
    er.WARMUP_START = WARMUP
    print("building features (slow)...")
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)

    live = df[df["Volume_Lot"] > 0]
    cnt = live.groupby("date")["stock_id"].size()
    cal = set(cnt[cnt >= 300].index)

    tw = pd.read_csv(os.path.join("data", "research_taiex.csv"))
    tw = tw[tw["date"].isin(cal)].reset_index(drop=True)
    c = pd.to_numeric(tw["close"], errors="coerce")
    tw["risk_on"] = (c > c.rolling(20).mean()) & (c > c.rolling(60).mean())
    tw["str20"] = c / c.rolling(20).mean() - 1
    risk_on = dict(zip(tw["date"], tw["risk_on"]))
    str20 = dict(zip(tw["date"], tw["str20"]))

    d = df.sort_values(["stock_id", "date"])
    g = d.groupby("stock_id")
    atr = ((d["high"] - d["low"]) / d["close"]).groupby(d["stock_id"]).transform(
        lambda s: s.rolling(20).mean()) * 100
    ma20 = g["close"].transform(lambda s: s.rolling(20).mean())
    ma60 = g["close"].transform(lambda s: s.rolling(60).mean())
    vol20 = g["Volume_Lot"].transform(lambda s: s.rolling(20).mean())
    key = list(zip(d["stock_id"].astype(str), d["date"]))
    amap = dict(zip(key, atr))
    m20map = dict(zip(key, d["close"] / ma20 - 1))
    m60map = dict(zip(key, d["close"] / ma60 - 1))
    turnmap = dict(zip(key, vol20 * d["close"] * 1000))

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    print("replaying selection...")
    P = er.replay_selection(T)
    P = P[P["date"] >= EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P = P.merge(T[["date", "sid", "c", "ret5", "dist52", "rt", "bias", "ret60"]],
                on=["date", "sid"], how="left")
    P["atr"] = [amap.get((str(s), x)) for s, x in zip(P["sid"], P["date"])]
    P["ma20d"] = [m20map.get((str(s), x)) for s, x in zip(P["sid"], P["date"])]
    P["ma60d"] = [m60map.get((str(s), x)) for s, x in zip(P["sid"], P["date"])]
    P["turn20"] = [turnmap.get((str(s), x)) for s, x in zip(P["sid"], P["date"])]
    P["ro"] = P["date"].map(lambda x: bool(risk_on.get(x, False)))
    P["str20"] = P["date"].map(lambda x: str20.get(x, np.nan))

    # Everything the shipped rule fixes stays fixed: OTC, market tailwind,
    # first day on the list. Only the QUALITY thresholds and the rank cut are
    # left open, so a sweep cannot quietly re-open a settled question.
    keep = ((P["mkt"] == "OTC") & P["ro"] & (P["streak"] == 1)
            & (P["rank"] < RANK_POOL))
    G = P[keep].copy()
    print("universe (OTC, risk_on, first day, rank<%d): %d" % (RANK_POOL, len(G)))

    bars = {sid: b[b["date"].isin(cal) & (b["Volume_Lot"] > 0)].reset_index(drop=True)
            for sid, b in live.groupby("stock_id")}
    trades = []
    for r in G.itertuples(index=False):
        b = bars.get(str(r.sid))
        if b is None:
            continue
        idx = b.index[b["date"] == r.date]
        if len(idx) == 0:
            continue
        i = int(idx[0])
        fb = b.iloc[i + 1: i + 1 + FWD_BARS]
        if len(fb) < HOLD:
            continue
        trades.append({
            "sig": r.date, "sid": str(r.sid), "rank": int(r.rank),
            "dist52": r.dist52, "ret5": r.ret5, "atr": r.atr, "ls": r.ls,
            "rt": r.rt, "bias": r.bias, "ret60": r.ret60,
            "ma20d": r.ma20d, "ma60d": r.ma60d, "turn20": r.turn20,
            "str20": r.str20,
            "dates": fb["date"].tolist(),
            "o": fb["open"].to_numpy(float), "h": fb["high"].to_numpy(float),
            "l": fb["low"].to_numpy(float), "c": fb["close"].to_numpy(float),
        })
    pickle.dump(trades, open(PICKS_PKL, "wb"))
    print("cached %d trades -> %s" % (len(trades), PICKS_PKL))


# ------------------------------------------------------------------- engine
def load():
    return pickle.load(open(PICKS_PKL, "rb"))


def net(entry, px, slip=0.0):
    return (px * (1 - slip) * (1 - SELL_COST) / (entry * (1 + BUY_COST)) - 1) * 100


def run(t, hold=HOLD, ride=True, cap=20, slip=0.0):
    """The shipped exit stack plus the adopted ride rule.

    This delegates to sandbox_daily_plan.run_plan rather than re-deriving the
    walk. A first attempt here replayed the whole 20-bar window for price exits
    and only THEN consulted the ride rule, which let a stop or target on bar 15
    be booked for a trade the calendar had already closed on bar 10 -- it
    inflated the recent-window win rate by about 6 points. One engine, verified
    bar-for-bar against replay_exit, is the whole point.
    """
    from sandbox_daily_plan import run_plan
    plan = dict(stop=STOP, tp=TP, arm=ARM, lock=LOCK,
                ride="ma5" if ride else "off", cap=cap if ride else hold)
    pnl, capital, events = run_plan(t, plan, hold=hold)
    reason = events[-1][1] if events else "na"
    r = pnl / capital * 100
    if slip and reason in ("stop", "lock"):
        # re-price that exit `slip` worse, in return space
        E = float(t["o"][0])
        px = (r / 100 + 1) * E * (1 + BUY_COST) / (1 - SELL_COST)
        r = net(E, px * (1 - slip))
    return reason, r


# -------------------------------------------------------------------- stats
def evaluate(trades, gate, **kw):
    sel = [t for t in trades if passes(t, gate)]
    rows = []
    for t in sel:
        reason, r = run(t, **kw)
        rows.append({"sig": t["sig"], "ret": r, "exit": reason})
    return pd.DataFrame(rows)


def passes(t, gate):
    for k, v in gate.items():
        if v is None:
            continue
        x = t.get(k)
        if x is None or x != x:
            return False
        if k in ("rank",):
            if not x < v:
                return False
        elif k in ("dist52", "ret5"):
            if not x <= v:
                return False
        elif k in ("atr", "ls", "turn20", "str20"):
            if not x >= v:
                return False
        elif k in ("rt", "ma20d", "ma60d", "ret60"):
            if not x <= v:
                return False
    return True


def stats(df):
    if not len(df):
        return {"n": 0, "win": float("nan"), "mean": float("nan")}
    r = df["ret"].to_numpy(float)
    return {"n": len(r), "win": 100 * (r > 0).mean(), "mean": r.mean(),
            "p10": np.percentile(r, 10)}


def boot_lo(df, iters=1200, seed=SEED):
    if len(df) < 20:
        return float("nan")
    rng = np.random.default_rng(seed)
    groups = [g["ret"].to_numpy(float) for _, g in df.groupby("sig")]
    w = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        w.append(100 * (np.concatenate([groups[i] for i in pick]) > 0).mean())
    return np.percentile(w, 2.5)


def split(df):
    rec = df[df["sig"] >= RECENT_FROM]
    old = df[df["sig"] < RECENT_FROM]
    return rec, old


def halves(df):
    d = df.sort_values("sig")
    h = len(d) // 2
    return d.iloc[:h], d.iloc[h:]


def line(label, df, base=None):
    rec, old = split(df)
    parts = []
    for name, sub in (("REC", rec), ("OLD", old)):
        s = stats(sub)
        h1, h2 = halves(sub)
        parts.append("%s n=%4d win %5.1f%% %+5.2f lo %4.1f h %4.1f/%4.1f"
                     % (name, s["n"], s["win"], s["mean"], boot_lo(sub),
                        stats(h1)["win"], stats(h2)["win"]))
    print("  %-34s | %s | %s" % (label, parts[0], parts[1]))


def quarters(a, b):
    """Fraction of quarters where candidate a is at least as good as base b."""
    def q(df):
        key = df["sig"].str.slice(0, 4) + "Q" +             ((df["sig"].str.slice(5, 7).astype(int) - 1) // 3 + 1).astype(str)
        return pd.DataFrame({"q": key, "w": df["ret"] > 0}).groupby("q")["w"].mean()
    qa, qb = q(a), q(b)
    idx = qb.index.intersection(qa.index)
    return int((qa[idx] >= qb[idx]).sum()), len(idx)


def cmd_joint():
    """Map the ATR axis finely and put each point through the full gate:
    win / bootLo / both halves in BOTH windows, quarterly stability, and the
    slippage stress that caught the fake optimum in the lock study."""
    trades = load()
    base = evaluate(trades, BASE_GATE)
    brec, bold = split(base)
    print("=== ATR axis, full adoption gate (base = ATR >= %.1f) ===" % BASE_GATE["atr"])
    print("base: REC %5.1f%% %+5.2f lo %4.1f | OLD %5.1f%% %+5.2f lo %4.1f | n=%d"
          % (stats(brec)["win"], stats(brec)["mean"], boot_lo(brec),
             stats(bold)["win"], stats(bold)["mean"], boot_lo(bold), len(base)))
    for atr in (3.25, 3.5, 3.75, 4.0, 4.25, 4.5, 4.75, 5.0):
        g = dict(BASE_GATE, atr=atr)
        cand = evaluate(trades, g)
        slipped = evaluate(trades, g, slip=0.005)
        rec, old = split(cand)
        srec, sold = split(slipped)
        ok = True
        cells = []
        for (c, b) in ((rec, brec), (old, bold)):
            sc, sb = stats(c), stats(b)
            lc, lb = boot_lo(c), boot_lo(b)
            h1c, h2c = halves(c)
            h1b, h2b = halves(b)
            passed = (sc["win"] >= sb["win"] and lc >= lb
                      and stats(h1c)["win"] >= stats(h1b)["win"]
                      and stats(h2c)["win"] >= stats(h2b)["win"])
            ok = ok and passed
            cells.append("%5.1f%% %+5.2f lo %4.1f h %4.1f/%4.1f %s"
                         % (sc["win"], sc["mean"], lc, stats(h1c)["win"],
                            stats(h2c)["win"], "ok" if passed else "FAIL"))
        nq, tq = quarters(cand, base)
        print("  atr %.2f n=%4d | REC %s | OLD %s | q %2d/%2d | slip0.5%% REC %5.1f%% OLD %5.1f%% | %s"
              % (atr, len(cand), cells[0], cells[1], nq, tq,
                 stats(srec)["win"], stats(sold)["win"],
                 "ADOPTABLE" if ok and nq >= tq - 1 else ""))


def cmd_holdctl():
    """Does holding longer win more BECAUSE OF THE SELECTION, or because
    everything drifts up over a longer window?

    The control is the same replay with the quality gate removed: if the
    ungated universe gains just as much from a longer hold, the gain is the
    tape, not the picks. This is the beta control the playbook demands,
    applied to the holding period instead of to the returns.
    """
    trades = load()
    print("%-6s | %-34s | %-34s | selection edge" % ("hold", "CORE+ gate", "ungated (control)"))
    for hold in (5, 8, 10, 12, 15, 18, 20):
        cells, wins = [], []
        for gate in (BASE_GATE, {}):
            df = evaluate(trades, gate, hold=hold, cap=max(20, hold + 5))
            rec, old = split(df)
            wins.append((stats(rec)["win"], stats(old)["win"]))
            cells.append("REC %5.1f%% %+5.2f | OLD %5.1f%% %+5.2f"
                         % (stats(rec)["win"], stats(rec)["mean"],
                            stats(old)["win"], stats(old)["mean"]))
        edge = (wins[0][0] - wins[1][0], wins[0][1] - wins[1][1])
        print("%-6d | %s | %s | REC %+4.1fpp OLD %+4.1fpp"
              % (hold, cells[0], cells[1], edge[0], edge[1]))
    print()
    print("If the control column rises as fast as the gated one, a longer hold")
    print("is buying market drift, not better selection.")
    print()
    print("=== capital efficiency: return per trading day held ===")
    for hold in (10, 12, 15, 18, 20):
        df = evaluate(trades, BASE_GATE, hold=hold, cap=max(20, hold + 5))
        rec, old = split(df)
        print("  hold %2d | REC mean %+5.2f  per-day %+5.3f | OLD mean %+5.2f  per-day %+5.3f"
              % (hold, stats(rec)["mean"], stats(rec)["mean"] / hold,
                 stats(old)["mean"], stats(old)["mean"] / hold))


def cmd_buckets():
    """Is a threshold real, or is it just dropping a handful of trades?

    A gate that moves from 4.5 to 4.75 does exactly one thing: it stops taking
    the trades whose ATR sits in [4.5, 4.75). If that slice is small, its win
    rate is noise and the "improvement" is noise with it. Bucketing the axis
    answers that directly, which sweeping thresholds cannot.
    """
    trades = load()
    for key, edges in (("atr", (0, 3.0, 3.5, 4.0, 4.5, 4.75, 5.0, 5.5, 6.5, 99)),
                       ("dist52", (-1, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 9)),
                       ("ret5", (-9, -0.02, 0.0, 0.02, 0.05, 0.08, 9)),
                       ("ls", (0, 60, 65, 70, 75, 80, 200)),
                       ("rank", (0, 3, 6, 10, 14, 20))):
        print("-- %s buckets (other gates as shipped)" % key)
        g = dict(BASE_GATE)
        g.pop(key, None)
        sel = [t for t in trades if passes(t, g)]
        for lo, hi in zip(edges[:-1], edges[1:]):
            sub = [t for t in sel if t.get(key) is not None and t[key] == t[key]
                   and lo <= t[key] < hi]
            if not sub:
                continue
            rows = []
            for t in sub:
                reason, r = run(t)
                rows.append({"sig": t["sig"], "ret": r})
            df = pd.DataFrame(rows)
            rec, old = split(df)
            print("   [%6s,%6s) n=%4d | REC n=%4d win %5.1f%% %+5.2f | OLD n=%4d win %5.1f%% %+5.2f"
                  % (lo, hi, len(df), len(rec), stats(rec)["win"], stats(rec)["mean"],
                     len(old), stats(old)["win"], stats(old)["mean"]))
        print()


def cmd_hold():
    """The hold length was also chosen on the old simulator."""
    trades = load()
    base = evaluate(trades, BASE_GATE)
    line("BASE hold %d + ride" % HOLD, base)
    print()
    for hold in (5, 8, 10, 12, 15):
        for ride in (True, False):
            line("hold %2d %s" % (hold, "+ride" if ride else "fixed"),
                 evaluate(trades, BASE_GATE, hold=hold, ride=ride))
    print()
    print("-- ride cap")
    for cap in (12, 15, 20, 25):
        line("hold 10 + ride cap %d" % cap,
             evaluate(trades, BASE_GATE, cap=cap))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "base"
    if cmd == "build":
        build()
    else:
        trades = load()
        if cmd == "base":
            print("cached trades: %d (%s..%s)" % (
                len(trades), min(t["sig"] for t in trades),
                max(t["sig"] for t in trades)))
            print("=== the shipped gate, on the corrected engine ===")
            line("ungated (OTC/risk_on/first day)", evaluate(trades, {}))
            line("shipped CORE+ gate", evaluate(trades, BASE_GATE))
            line("  without the ride rule",
                 evaluate(trades, BASE_GATE, ride=False))
        elif cmd == "joint":
            cmd_joint()
        elif cmd == "holdctl":
            cmd_holdctl()
        elif cmd == "buckets":
            cmd_buckets()
        elif cmd == "hold":
            cmd_hold()
        elif cmd == "sweep":
            base = evaluate(trades, BASE_GATE)
            line("BASE", base)
            print()
            for key, values in (("rank", (10, 15, 20, 25, 30, 40, 60)),
                                ("dist52", (0.02, 0.03, 0.05, 0.08, 0.12, 0.20, None)),
                                ("ret5", (0.00, 0.02, 0.05, 0.08, 0.12, None)),
                                ("atr", (3.0, 4.0, 4.5, 5.5, 6.5, 8.0, None))):
                print("-- %s" % key)
                for v in values:
                    g = dict(BASE_GATE)
                    g[key] = v
                    line("%s = %s" % (key, v), evaluate(trades, g))
                print()
