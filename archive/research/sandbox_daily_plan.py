"""
sandbox_daily_plan.py -- what should tomorrow's orders be?  ASCII only.

Owner's request 2026-09-21: "at the close, give me tomorrow's advice too --
below which price should I cut or add, above which should I take some profit
or keep riding".

That is a different question from the ones this project has answered before,
and it needs a different engine. Every earlier ladder study (and the shipped
exit stack until today) walked the price path INSIDE a bar and fired levels
in an assumed intraday order. This file does not. Here:

    every order is decided from a CLOSE, and can only execute on a LATER bar.

That is exactly how the owner trades: the cloud scan runs after the close, the
phone shows the plan in the evening, the orders sit in the broker overnight.
An order is filled when the next bar opens through it (filled at the open) or
trades through it during the day (filled at the level). Nothing a bar does can
influence an order that the same bar's close had not yet placed.

Consequence worth stating plainly: the 2026-09-17 ladder verdicts (partial cut
on a break, sell-half-rebuy-lower, scale-out at +10/+20) were measured on the
old intraday engine, which also contained the same-bar lock defect. They are
re-run here rather than quoted.

Trades: the same 564 CORE+ first-day entries as every other study
(%TEMP%/yentool_ladder/picks.pkl, built by sandbox_scale_ladder.py build).

Commands:
  python archive/research/sandbox_daily_plan.py base    # engine agreement check
  python archive/research/sandbox_daily_plan.py ladder  # cut / add / scale-out
  python archive/research/sandbox_daily_plan.py trail    # riding rules
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.getcwd())
from scanner.exit_rules import DEFAULT_RULE  # noqa: E402

CACHE_DIR = os.environ.get("LADDER_CACHE", os.path.join(
    os.environ.get("TEMP", "."), "yentool_ladder"))
PICKS_PKL = os.path.join(CACHE_DIR, "picks.pkl")

RECENT_FROM = "2023-09-18"
HOLD = DEFAULT_RULE["hold_bars"]
STOP = DEFAULT_RULE["stop_pct"]
TP = DEFAULT_RULE["tp_pct"]
ARM = DEFAULT_RULE["arm_pct"]
LOCK = DEFAULT_RULE["lock_pct"]
BUY_COST = 0.001425
SELL_COST = 0.001425 + 0.003
SEED = 7


def load_trades():
    t = pickle.load(open(PICKS_PKL, "rb"))
    return [x for x in t if len(x["o"]) >= HOLD]


def taiex_flags():
    """{date: (above20, above60)} from the cached index series. The shipped
    market-shock delay (holding_tracker.EXIT_DELAY_CAP_BY_MODE) holds a
    position past its exit date when the index is below its 20MA but still
    above its 60MA -- a pullback inside an uptrend. It was validated on the
    old intraday engine, so any interaction with the stock-strength ride has
    to be re-measured, not assumed."""
    d = pd.read_csv(os.path.join("data", "research_taiex.csv"))
    c = pd.to_numeric(d["close"], errors="coerce")
    a20 = c > c.rolling(20).mean()
    a60 = c > c.rolling(60).mean()
    return {str(x)[:10]: (bool(p), bool(q))
            for x, p, q in zip(d["date"], a20, a60)}


# --------------------------------------------------------------------- engine
class Position(object):
    """One trade, in units of the planned budget (1.0 = the full position)."""

    def __init__(self, entry, size=1.0):
        self.entry = entry
        self.shares = size / entry
        self.cost = size * (1 + BUY_COST)
        self.capital = size          # peak budget committed
        self.cash = 0.0
        self.events = []

    @property
    def open(self):
        return self.shares > 1e-12

    def buy(self, budget, price, tag, bar):
        self.shares += budget / price
        self.cost += budget * (1 + BUY_COST)
        self.capital += budget
        self.events.append((bar, tag, price))

    def sell(self, fraction_of_initial, price, tag, bar, initial_shares):
        qty = min(self.shares, initial_shares * fraction_of_initial)
        if qty <= 1e-12:
            return
        self.shares -= qty
        self.cash += qty * price * (1 - SELL_COST)
        if self.shares <= 1e-12:
            self.shares = 0.0
        self.events.append((bar, tag, price))

    def close_at(self, price, tag, bar):
        if self.open:
            self.cash += self.shares * price * (1 - SELL_COST)
            self.shares = 0.0
            self.events.append((bar, tag, price))

    def result(self, last_close):
        """(pnl, money committed). The denominator includes the buy fee so a
        single-buy trade returns exactly what scanner.exit_rules would report,
        and a staged plan is still measured on the capital it really used."""
        value = self.cash + self.shares * last_close * (1 - SELL_COST)
        pnl = value - self.cost
        return pnl, self.capital * (1 + BUY_COST)


def run_plan(t, plan, hold=HOLD):
    """Replay one trade under a plan of evening-placed orders.

    plan keys (all fractions of the FIRST fill, all optional):
      stop        disaster stop
      tp          take profit, whole remaining position
      arm, lock   close at or above arm -> stop rises to lock (next session)
      cut         (level, fraction) sell part when it trades down through level
      add         (level, fraction) buy more when it trades down through level
      out         list of (level, fraction) scale-out steps on the way up
      ride        'off' | 'ma5'  -- extend past the time exit while the close
                  stays above its own 5-bar mean (capped at `cap`)
      cap         maximum bars when riding
    """
    o, h, l, c = t["o"], t["h"], t["l"], t["c"]
    E = float(o[0])
    p = Position(E)
    initial_shares = p.shares
    stop_px = E * (1 - plan["stop"]) if plan.get("stop") else None
    tp_px = E * (1 + plan["tp"]) if plan.get("tp") else None
    arm_px = E * (1 + plan.get("arm", ARM)) if plan.get("arm") is not None else None
    lock_px = E * (1 + plan.get("lock", LOCK))
    armed = False
    cut_px = E * (1 - plan["cut"][0]) if plan.get("cut") else None
    add_px = E * (1 - plan["add"][0]) if plan.get("add") else None
    outs = [(E * (1 + lv), fr) for lv, fr in (plan.get("out") or [])]
    cap = plan.get("cap", hold)
    ride = plan.get("ride", "off")
    # Extra lock rungs: each (gain, lock) raises the stop again once the close
    # clears `gain`. The stop only ever ratchets UP.
    steps = sorted(plan.get("steps") or [])

    n = min(len(o), max(hold, cap))
    for i in range(n):
        if not p.open:
            break
        op, hi, lo, cl = float(o[i]), float(h[i]), float(l[i]), float(c[i])
        if op != op or hi != hi or lo != lo:
            continue

        # --- fills from orders resting since the previous close ---------
        # The open is the one moment whose timing is known.
        if tp_px is not None and op >= tp_px:
            p.close_at(op, "tp", i)
            break
        if stop_px is not None and op <= stop_px:
            p.close_at(op, "lock" if armed else "stop", i)
            break
        # Within the bar the order of high and low is unknowable, so the
        # DOWNSIDE is served first -- the pessimistic convention this project
        # has used since F09.
        if stop_px is not None and lo <= stop_px:
            p.close_at(stop_px, "lock" if armed else "stop", i)
            break
        if cut_px is not None and lo <= cut_px:
            p.sell(plan["cut"][1], min(op, cut_px), "cut", i, initial_shares)
            cut_px = None
            if not p.open:
                break
        if add_px is not None and lo <= add_px:
            p.buy(plan["add"][1], min(op, add_px), "add", i)
            add_px = None
        for k, (lv, fr) in enumerate(list(outs)):
            if hi >= lv:
                p.sell(fr, max(op, lv), "out", i, initial_shares)
                outs = [x for x in outs if x[0] != lv]
        if tp_px is not None and hi >= tp_px and p.open:
            p.close_at(tp_px, "tp", i)
            break
        if not p.open:
            break

        # --- the close: what we learn, and what we place for tomorrow ----
        if arm_px is not None and not armed and cl == cl and cl >= arm_px:
            armed = True
            stop_px = lock_px if stop_px is None else max(stop_px, lock_px)
        if cl == cl:
            for gain, lv in steps:
                if cl >= E * (1 + gain):
                    want = E * (1 + lv)
                    stop_px = want if stop_px is None else max(stop_px, want)
                    armed = True

        last = i
        if i >= hold - 1:
            keep = False
            if i + 1 < n and ride in ("market", "either"):
                # the shipped rule: a pullback inside an uptrend
                flags = plan.get("flags") or {}
                day = t["dates"][i] if i < len(t["dates"]) else None
                a20, a60 = flags.get(day, (True, True))
                keep = (not a20) and a60 and i < cap - 1
            if not keep and ride in ("ma5", "either") and i + 1 < n:
                window = [float(x) for x in c[max(0, i - 4):i + 1]]
                keep = cl > (sum(window) / len(window)) and i < cap - 1
            if not keep:
                p.close_at(cl, "time", i)
                break
    else:
        last = n - 1
        p.close_at(float(c[last]), "time", last)

    if p.open:
        p.close_at(float(c[last]), "time", last)
    pnl, capital = p.result(float(c[last]))
    return pnl, capital, p.events


# ---------------------------------------------------------------------- stats
def evaluate(trades, plan):
    rows = []
    for t in trades:
        pnl, capital, ev = run_plan(t, plan)
        rows.append({"sig": t["sig"], "ret": pnl / capital * 100,
                     "capital": capital,
                     "exit": ev[-1][1] if ev else "na"})
    return pd.DataFrame(rows)


def stats(df):
    r = df["ret"].to_numpy(float)
    return {"n": len(r), "win": 100 * (r > 0).mean(), "mean": r.mean(),
            "p10": np.percentile(r, 10), "worst": r.min()}


def boot_lo(df, iters=1500, seed=SEED):
    rng = np.random.default_rng(seed)
    groups = [g["ret"].to_numpy(float) for _, g in df.groupby("sig")]
    wins = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        wins.append(100 * (np.concatenate([groups[i] for i in pick]) > 0).mean())
    return np.percentile(wins, 2.5)


def paired(base, cand, iters=1500, seed=SEED):
    rng = np.random.default_rng(seed)
    d = cand["ret"].to_numpy(float) - base["ret"].to_numpy(float)
    sig = base["sig"].to_numpy()
    groups = [d[sig == s] for s in pd.unique(sig)]
    out = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        out.append(np.concatenate([groups[i] for i in pick]).mean())
    return np.percentile(out, 2.5), np.percentile(out, 97.5)


def report(label, base, cand):
    parts = []
    verdict = True
    for wname in ("RECENT", "OLD"):
        m = (base["sig"] >= RECENT_FROM) if wname == "RECENT" else (base["sig"] < RECENT_FROM)
        b, k = base[m], cand[m]
        sb, sk = stats(b), stats(k)
        lo_b, lo_k = boot_lo(b), boot_lo(k)
        idx = np.argsort(b["sig"].to_numpy())
        half = len(idx) // 2
        h = []
        for part in (idx[:half], idx[half:]):
            h.append((100 * (k["ret"].to_numpy()[part] > 0).mean(),
                      100 * (b["ret"].to_numpy()[part] > 0).mean()))
        ok = (sk["win"] >= sb["win"] and lo_k >= lo_b
              and h[0][0] >= h[0][1] and h[1][0] >= h[1][1])
        verdict = verdict and ok
        parts.append("%s %5.1f%%/%+5.2f (base %5.1f/%+5.2f) lo %4.1f/%4.1f "
                     "h %4.1f/%4.1f %4.1f/%4.1f %s"
                     % (wname[:3], sk["win"], sk["mean"], sb["win"], sb["mean"],
                        lo_k, lo_b, h[0][0], h[0][1], h[1][0], h[1][1],
                        "ok" if ok else "FAIL"))
    ci = paired(base, cand)
    print("  %-40s | %s | %s | dmean %+5.2f (CI %+.2f..%+.2f) p10 %+6.2f %s"
          % (label, parts[0], parts[1],
             cand["ret"].mean() - base["ret"].mean(), ci[0], ci[1],
             np.percentile(cand["ret"], 10),
             "ADOPTABLE" if verdict else ""))


BASE_PLAN = dict(stop=STOP, tp=TP, arm=ARM, lock=LOCK)


def cmd_base():
    trades = load_trades()
    base = evaluate(trades, BASE_PLAN)
    print("=== evening-order engine vs the shipped replay (must agree) ===")
    from scanner.exit_rules import replay_exit
    diff = 0
    for t, (_, row) in zip(trades, base.iterrows()):
        p = replay_exit(t["o"][:HOLD], t["h"][:HOLD], t["l"][:HOLD], t["c"][:HOLD],
                        hold_bars=HOLD)
        net = (p["exit_price"] * (1 - SELL_COST)
               / (float(t["o"][0]) * (1 + BUY_COST)) - 1) * 100
        if abs(net - row["ret"]) > 1e-6:
            diff += 1
    print("  trades %d, disagreements %d" % (len(trades), diff))
    for wname in ("RECENT", "OLD"):
        m = (base["sig"] >= RECENT_FROM) if wname == "RECENT" else (base["sig"] < RECENT_FROM)
        s = stats(base[m])
        print("  %-7s n=%3d win %5.1f%% mean %+5.2f p10 %+6.2f  %s"
              % (wname, s["n"], s["win"], s["mean"], s["p10"],
                 base[m]["exit"].value_counts(normalize=True).round(2).to_dict()))


def cmd_ladder():
    trades = load_trades()
    base = evaluate(trades, BASE_PLAN)
    print("=== tomorrow's ladder, re-measured on the evening-order engine ===")
    print("(base = adopted rule: stop -%.0f%%, lock +%.1f%% armed at +%.1f%%, tp +%.0f%%, hold %d)"
          % (STOP * 100, LOCK * 100, ARM * 100, TP * 100, HOLD))
    print("\n-- cut part of the position when it breaks a level")
    for lv in (0.05, 0.08, 0.10, 0.12):
        for fr in (0.3, 0.5):
            plan = dict(BASE_PLAN, cut=(lv, fr))
            report("cut %.0f%% at -%.0f%%" % (fr * 100, lv * 100), base,
                   evaluate(trades, plan))
    print("\n-- add to the position when it dips to a level (staged entry)")
    for lv in (0.05, 0.08, 0.10, 0.12):
        for fr in (0.5,):
            plan = dict(BASE_PLAN, add=(lv, fr))
            report("add %.0f%% at -%.0f%%" % (fr * 100, lv * 100), base,
                   evaluate(trades, plan))
    print("\n-- scale out on the way up")
    for outs, label in (
            ([(0.10, 0.5)], "sell half at +10%"),
            ([(0.15, 0.5)], "sell half at +15%"),
            ([(0.10, 1 / 3.), (0.20, 1 / 3.)], "sell 1/3 at +10%, 1/3 at +20%"),
            ([(0.08, 0.5)], "sell half at +8%"),
            ([(0.12, 0.3)], "sell 30% at +12%")):
        plan = dict(BASE_PLAN, out=outs)
        report(label, base, evaluate(trades, plan))


def cmd_trail():
    trades = load_trades()
    base = evaluate(trades, BASE_PLAN)
    print("=== keep riding instead of selling on day %d ===" % HOLD)
    for cap in (15, 20, 30):
        plan = dict(BASE_PLAN, ride="ma5", cap=cap)
        report("ride while close > 5-bar mean, cap %d" % cap, base,
               evaluate(trades, plan))
    print()
    print("=== further lock rungs on the way up (the stop ratchets, never falls) ===")
    for steps in ([(0.06, 0.03)], [(0.08, 0.05)], [(0.10, 0.06)], [(0.12, 0.08)],
                  [(0.06, 0.03), (0.10, 0.06)],
                  [(0.05, 0.02), (0.08, 0.05), (0.12, 0.08)],
                  [(0.06, 0.04), (0.12, 0.09)]):
        plan = dict(BASE_PLAN, steps=steps)
        label = " then ".join("+%.0f%%->stop+%.0f%%" % (g * 100, lv * 100)
                              for g, lv in steps)
        report(label, base, evaluate(trades, plan))
    print()
    print("=== extending past day %d: market rule vs stock rule vs both ===" % HOLD)
    flags = taiex_flags()
    for ride, label in (("market", "shipped: TAIEX below 20MA but above 60MA"),
                        ("ma5", "stock: close above its own 5-bar mean"),
                        ("either", "either of the two")):
        report(label, base, evaluate(trades, dict(BASE_PLAN, ride=ride, cap=20,
                                                  flags=flags)))
    print()
    print("=== the whole ladder together ===")
    for extra, label in (
            (dict(add=(0.08, 0.5)), "add@-8%"),
            (dict(ride="ma5", cap=20), "ride>MA5"),
            (dict(steps=[(0.06, 0.03)]), "rung +6->+3"),
            (dict(add=(0.08, 0.5), ride="ma5", cap=20), "add@-8% + ride>MA5"),
            (dict(add=(0.08, 0.5), ride="ma5", cap=20, steps=[(0.06, 0.03)]),
             "add@-8% + ride>MA5 + rung +6->+3"),
            (dict(add=(0.08, 0.5), ride="ma5", cap=20, out=[(0.15, 0.5)]),
             "add@-8% + ride>MA5 + sell half @+15%")):
        report(label, base, evaluate(trades, dict(BASE_PLAN, **extra)))


def cmd_honest():
    """The add-on-dip result, checked the ways it could be an artefact.

    Returns here are per unit of capital COMMITTED, so a plan that commits
    1.5 units on the losers can show a better percentage while losing more
    money. This prints the absolute result too (same budget per SIGNAL, which
    is what the owner's account actually experiences), plus the worst trade,
    the capital actually used and quarterly stability.
    """
    trades = load_trades()
    base = evaluate(trades, BASE_PLAN)
    plans = [("base", BASE_PLAN),
             ("add 50% at -5%", dict(BASE_PLAN, add=(0.05, 0.5))),
             ("add 50% at -8%", dict(BASE_PLAN, add=(0.08, 0.5))),
             ("add 50% at -10%", dict(BASE_PLAN, add=(0.10, 0.5))),
             ("half then add half at -10% (shipped option)",
              None),
             ("add 50% at -8% + sell half at +15%",
              dict(BASE_PLAN, add=(0.08, 0.5), out=[(0.15, 0.5)])),
             ("add 50% at -8% + sell half at +10%",
              dict(BASE_PLAN, add=(0.08, 0.5), out=[(0.10, 0.5)])),
             ("sell half at +15%", dict(BASE_PLAN, out=[(0.15, 0.5)])),
             ("sell half at +8%", dict(BASE_PLAN, out=[(0.08, 0.5)]))]
    print("=== absolute view: every SIGNAL gets the same budget (1.0) ===")
    print("%-44s %-28s %-28s %s" % ("plan", "RECENT win/mean%/abs", "OLD win/mean%/abs", "worst abs | cap used"))
    for label, plan in plans:
        if plan is None:
            rows = []
            for t in trades:
                pnl, cap, ev = run_plan(t, dict(BASE_PLAN, add=(0.10, 0.5)))
                # half at the open, the rest on the dip: scale the whole book
                pnl2, cap2, _ = run_plan(t, dict(BASE_PLAN, add=(0.10, 1.0)))
                rows.append({"sig": t["sig"], "ret": pnl2 / cap2 * 100 if cap2 else 0,
                             "abs": pnl2 * 0.5, "cap": cap2 * 0.5})
            df = pd.DataFrame(rows)
        else:
            rows = []
            for t in trades:
                pnl, cap, ev = run_plan(t, plan)
                rows.append({"sig": t["sig"], "ret": pnl / cap * 100,
                             "abs": pnl, "cap": cap})
            df = pd.DataFrame(rows)
        cells = []
        for wname in ("RECENT", "OLD"):
            m = (df["sig"] >= RECENT_FROM) if wname == "RECENT" else (df["sig"] < RECENT_FROM)
            d = df[m]
            cells.append("%5.1f%% %+5.2f %+6.3f" % (
                100 * (d["abs"] > 0).mean(), d["ret"].mean(), d["abs"].mean() * 100))
        print("%-44s %-28s %-28s %+6.2f | %.2f" % (
            label, cells[0], cells[1], df["abs"].min() * 100, df["cap"].mean()))
    print("(win% here counts a WINNING TRADE in money; mean% is per capital used;")
    print(" abs is mean profit per signal in % of one full position's budget)")

    print()
    print("=== quarterly stability of the add (win in money, vs base) ===")
    add = []
    for t in trades:
        pnl, cap, ev = run_plan(t, dict(BASE_PLAN, add=(0.08, 0.5)))
        add.append({"sig": t["sig"], "abs": pnl})
    add = pd.DataFrame(add)
    b = []
    for t in trades:
        pnl, cap, ev = run_plan(t, BASE_PLAN)
        b.append({"sig": t["sig"], "abs": pnl})
    b = pd.DataFrame(b)
    q = add["sig"].str.slice(0, 4) + "Q" +         ((add["sig"].str.slice(5, 7).astype(int) - 1) // 3 + 1).astype(str)
    agg = pd.DataFrame({"q": q, "add": add["abs"] > 0, "base": b["abs"] > 0}).groupby("q").mean()
    print("  quarters add >= base: %d / %d" % (int((agg["add"] >= agg["base"]).sum()), len(agg)))
    worse = agg[agg["add"] < agg["base"]]
    if len(worse):
        print("  worse quarters: %s" % ", ".join("%s %.0f%% vs %.0f%%" % (i, r["add"] * 100, r["base"] * 100)
                                                 for i, r in worse.iterrows()))
    fired = sum(1 for t in trades if any(e[1] == "add" for e in run_plan(t, dict(BASE_PLAN, add=(0.08, 0.5)))[2]))
    print("  the add fired on %d of %d trades (%.0f%%)" % (fired, len(trades), 100 * fired / len(trades)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "base"
    {"base": cmd_base, "ladder": cmd_ladder, "trail": cmd_trail,
     "honest": cmd_honest}[cmd]()
