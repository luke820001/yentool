"""
sandbox_modes.py -- several complete trading MODES, scored on both axes.
ASCII only.

Owner's request 2026-09-21: "build a few more modes and test them -- see
whether the win rate can go higher, and what the highest return is."

Those are two different objectives and they pull against each other. A rule
that books every small profit wins often and earns little; a rule that lets
everything run earns more per trade and loses more often. So this file does
not report one number. Every mode is scored on:

  win        percentage of trades that end positive, after costs
  mean       average return per trade, on the capital that trade used
  bootLo     2.5th percentile of the win rate, resampling SIGNAL DAYS
  p10/worst  the bad tail, which is what a high win rate can hide
  pf         profit factor: gross gains / gross losses
  per-day    mean / average trading days held -- capital efficiency
  n/yr       how often the mode actually fires

Everything runs on the same cached universe (sandbox_entry_gate.py build) and
the same evening-order engine (sandbox_daily_plan.run_plan), so the modes are
directly comparable and every one of them is executable: orders are decided
from a close and filled on a later bar.

Commands:
  python archive/research/sandbox_modes.py compare   # the mode table
  python archive/research/sandbox_modes.py gap       # entry gap cap (new axis)
  python archive/research/sandbox_modes.py frontier  # win vs return trade-off
"""
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join("archive", "research"))

from scanner.exit_rules import DEFAULT_RULE  # noqa: E402
from sandbox_entry_gate import (  # noqa: E402
    BASE_GATE, HOLD, RECENT_FROM, load, passes, split, stats, boot_lo,
)
from sandbox_daily_plan import run_plan  # noqa: E402

RESEARCH_DB = os.path.join("data", "research_prices.db")
STOP, TP = DEFAULT_RULE["stop_pct"], DEFAULT_RULE["tp_pct"]
ARM, LOCK = DEFAULT_RULE["arm_pct"], DEFAULT_RULE["lock_pct"]
LATE = (DEFAULT_RULE["late_from"], DEFAULT_RULE["late_gain"])

# A mode is a complete, tradeable rule: what it does on the way down, on the
# way up, and when it runs out of time.
MODES = {
    "A adopted (win-first)": dict(
        stop=STOP, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE,
        ride="ma5", cap=20),
    "B adopted, no late-take": dict(
        stop=STOP, tp=TP, arm=ARM, lock=LOCK, ride="ma5", cap=20),
    "C let it run (profit-first)": dict(
        stop=STOP, tp=None, arm=None, ride="ma5", cap=20),
    "D no lock, keep +20% target": dict(
        stop=STOP, tp=TP, arm=None, ride="ma5", cap=20),
    "E lock only, no target": dict(
        stop=STOP, tp=None, arm=ARM, lock=LOCK, late_profit=LATE,
        ride="ma5", cap=20),
    "F wide target +30%": dict(
        stop=STOP, tp=0.30, arm=ARM, lock=LOCK, late_profit=LATE,
        ride="ma5", cap=20),
    "G early target +10%": dict(
        stop=STOP, tp=0.10, arm=ARM, lock=LOCK, late_profit=LATE,
        ride="ma5", cap=20),
    "H scale out half at +15%": dict(
        stop=STOP, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE,
        out=[(0.15, 0.5)], ride="ma5", cap=20),
    "I add half at -10%": dict(
        stop=STOP, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE,
        add=(0.10, 0.5), ride="ma5", cap=20),
    "J add + scale out": dict(
        stop=STOP, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE,
        add=(0.10, 0.5), out=[(0.15, 0.5)], ride="ma5", cap=20),
    "K half in, add half at -10%": dict(
        init=0.5, stop=STOP, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE,
        add=(0.10, 0.5), ride="ma5", cap=20),
    "L no ride (fixed 10 bars)": dict(
        stop=STOP, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE, ride="off"),
    "M take profit from day 5": dict(
        stop=STOP, tp=TP, arm=ARM, lock=LOCK, late_profit=(5, 0.01),
        ride="ma5", cap=20),
    "N tight stop -10%": dict(
        stop=0.10, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE,
        ride="ma5", cap=20),
    # --- built for the owner's +25% definition of a win (2026-09-21) ---
    # A +20% take-profit makes +25% structurally impossible, and a +2% lock
    # ends most runs before they start. These give the right tail room.
    "P1 target +25%, no lock": dict(
        stop=STOP, tp=0.25, arm=None, ride="ma5", cap=20),
    "P2 target +25%, hold 20": dict(
        stop=STOP, tp=0.25, arm=None, ride="ma5", cap=25),
    "P3 target +30%, no lock": dict(
        stop=STOP, tp=0.30, arm=None, ride="ma5", cap=20),
    "P4 +25% target, lock at +10%": dict(
        stop=STOP, tp=0.25, arm=0.10, lock=0.06, ride="ma5", cap=20),
    "P5 no target, no lock, hold 20": dict(
        stop=STOP, tp=None, arm=None, ride="ma5", cap=25),
    "O no stop at all": dict(
        stop=None, tp=TP, arm=ARM, lock=LOCK, late_profit=LATE,
        ride="ma5", cap=20),
}


def sig_closes(trades):
    """The signal-day close for each trade, so an entry GAP can be measured."""
    ids = sorted({t["sid"] for t in trades})
    conn = sqlite3.connect(RESEARCH_DB)
    try:
        rows = conn.execute(
            "SELECT stock_id, date, close FROM data WHERE stock_id IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchall()
    finally:
        conn.close()
    return {(str(s), str(d)[:10]): c for s, d, c in rows}


def evaluate(trades, plan, gate=None, extra_filter=None):
    gate = BASE_GATE if gate is None else gate
    rows = []
    for t in trades:
        if not passes(t, gate):
            continue
        if extra_filter and not extra_filter(t):
            continue
        pnl, capital, events = run_plan(t, plan, hold=HOLD)
        bars = (events[-1][0] + 1) if events else HOLD
        rows.append({"sig": t["sig"], "ret": pnl / capital * 100,
                     "exit": events[-1][1] if events else "na",
                     "bars": bars, "capital": capital, "abs": pnl})
    return pd.DataFrame(rows)


def score(df, years):
    if not len(df):
        return None
    r = df["ret"].to_numpy(float)
    gains, losses = r[r > 0].sum(), -r[r <= 0].sum()
    return {
        "n": len(r), "win": 100 * (r > 0).mean(), "mean": r.mean(),
        # The owner's definition (2026-09-21): a "real win" is +25% or better.
        # Reported alongside the ordinary win rate because they are different
        # questions -- one asks "was I right", the other "did it pay big".
        "p25": 100 * (r >= 25).mean(), "p10up": 100 * (r >= 10).mean(),
        "abs": df["abs"].mean() * 100,
        "p10": np.percentile(r, 10), "worst": r.min(),
        "pf": (gains / losses) if losses else float("inf"),
        "perday": r.mean() / df["bars"].mean(),
        "bars": df["bars"].mean(),
        "nyr": len(r) / years,
        "lo": boot_lo(df),
    }


def show(label, df, years_rec=3.0, years_old=3.7):
    rec, old = split(df)
    a, b = score(rec, years_rec), score(old, years_old)
    if a is None or b is None:
        print("  %-30s (too few trades)" % label)
        return
    print("  %-30s | %5.1f%% %5.1f%% %5.1f%% %+6.2f pf %4.2f p10 %+6.1f d%+5.3f"
          " | %5.1f%% %5.1f%% %+6.2f"
          % (label, a["win"], a["p10up"], a["p25"], a["mean"], a["pf"], a["p10"],
             a["perday"], b["win"], b["p25"], b["mean"]))


def cmd_compare():
    trades = load()
    print("=== complete modes, same picks, same engine ===")
    print("  %-30s | %s | %s"
          % ("mode", "RECENT: win>0  win>=10 win>=25  mean   pf   p10   per-day",
             "OLD: win>0 win>=25 mean"))
    for label, plan in MODES.items():
        show(label, evaluate(trades, plan))
    print()
    print("  win  = % of trades ending positive after costs")
    print("  mean = average return per trade on the capital it used")
    print("  pf   = gross gains / gross losses; p10 = the bad tail")
    print("  per-day = mean / average trading days held (capital efficiency)")


def cmd_gap():
    """An entry gap cap is executable: you SEE the open before you buy, so
    'if it opens more than X% above the signal close, do not buy' is a real
    order, unlike most entry filters."""
    trades = load()
    closes = sig_closes(trades)
    plan = MODES["A adopted (win-first)"]
    base = evaluate(trades, plan)
    print("=== entry gap: skip when the open is far above the signal close ===")
    show("no cap (adopted)", base)
    for cap in (0.01, 0.02, 0.03, 0.05, 0.08):
        def f(t, cap=cap):
            c = closes.get((t["sid"], t["sig"]))
            if not c:
                return True
            return (float(t["o"][0]) / float(c) - 1) <= cap
        show("skip gap > %+.0f%%" % (cap * 100), evaluate(trades, plan, extra_filter=f))
    print()
    print("-- and the trades that WOULD have been skipped, on their own")
    for lo, hi in ((-9, -0.02), (-0.02, 0.0), (0.0, 0.01), (0.01, 0.02),
                   (0.02, 0.03), (0.03, 0.05), (0.05, 9)):
        def f(t, lo=lo, hi=hi):
            c = closes.get((t["sid"], t["sig"]))
            if not c:
                return False
            g = float(t["o"][0]) / float(c) - 1
            return lo <= g < hi
        show("gap in [%+.0f%%,%+.0f%%)" % (lo * 100, hi * 100),
             evaluate(trades, plan, extra_filter=f))


def cmd_frontier():
    """The trade-off itself: which modes are not dominated on BOTH axes."""
    trades = load()
    pts = []
    for label, plan in MODES.items():
        df = evaluate(trades, plan)
        rec, old = split(df)
        a, b = score(rec, 3.0), score(old, 3.7)
        if a is None or b is None:
            continue
        pts.append((label, a["win"], a["mean"], b["win"], b["mean"], a["pf"],
                    a["p10"], a["perday"]))
    print("=== which modes are on the frontier (nothing beats them on BOTH axes) ===")
    front = []
    for p in pts:
        dominated = any(q[1] >= p[1] and q[2] >= p[2] and (q[1] > p[1] or q[2] > p[2])
                        for q in pts)
        if not dominated:
            front.append(p)
    for p in sorted(pts, key=lambda x: -x[1]):
        mark = "  <= frontier" if p in front else ""
        print("  %-30s REC %5.1f%% %+5.2f | OLD %5.1f%% %+5.2f | pf %4.2f p10 %+6.1f%s"
              % (p[0], p[1], p[2], p[3], p[4], p[5], p[6], mark))
    print()
    print("A mode on the frontier is one you can only leave by giving up win")
    print("rate or giving up return. Everything else is strictly worse.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compare"
    {"compare": cmd_compare, "gap": cmd_gap, "frontier": cmd_frontier}[cmd]()
