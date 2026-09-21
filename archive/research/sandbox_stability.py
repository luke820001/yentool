"""
sandbox_stability.py -- how STABLE is the adopted rule, and can volatility
scaling make it steadier?  ASCII only.

Owner's question 2026-09-21: "back-test from every angle, cross-check, find a
higher STABLE win rate." Two things follow from the word stable:

  1. A number is not a result until you have seen its spread. This prints the
     adopted stack year by year, quarter by quarter, by market regime strength,
     by holding outcome and by entry characteristics, so the worst cell is as
     visible as the average.

  2. A fixed -20% stop means something different on a stock that moves 4% a day
     and one that moves 9% a day. Scaling the exit levels by the stock's own
     ATR is the one untested mechanism that could plausibly make the win rate
     steadier ACROSS stocks rather than just higher on average (TASKS W09/W12).

Both run on the ungated-universe cache built by sandbox_entry_gate.py build,
through the shipped exit stack plus the adopted ride rule.

Commands:
  python archive/research/sandbox_stability.py report   # spread of the adopted rule
  python archive/research/sandbox_stability.py atrscale # ATR-scaled levels
  python archive/research/sandbox_stability.py regime   # tailwind strength tiers
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join("archive", "research"))

from sandbox_entry_gate import (  # noqa: E402
    BASE_GATE, HOLD, LOCK, ARM, STOP, TP, RECENT_FROM, load, passes, stats,
    boot_lo, split,
)
from sandbox_daily_plan import run_plan  # noqa: E402

SEED = 7


def run(t, plan=None, hold=HOLD, cap=20):
    plan = plan or dict(stop=STOP, tp=TP, arm=ARM, lock=LOCK, ride="ma5", cap=cap)
    pnl, capital, events = run_plan(t, plan, hold=hold)
    return (events[-1][1] if events else "na"), pnl / capital * 100


def frame(trades, gate=None, plan_of=None):
    gate = BASE_GATE if gate is None else gate
    rows = []
    for t in trades:
        if not passes(t, gate):
            continue
        plan = plan_of(t) if plan_of else None
        reason, r = run(t, plan)
        rows.append({"sig": t["sig"], "ret": r, "exit": reason,
                     "atr": t.get("atr"), "ls": t.get("ls"),
                     "str20": t.get("str20"), "dist52": t.get("dist52"),
                     "ret5": t.get("ret5"), "sid": t["sid"]})
    return pd.DataFrame(rows)


def cell(df):
    if not len(df):
        return "  n=   0"
    s = stats(df)
    return "n=%4d win %5.1f%% mean %+5.2f" % (s["n"], s["win"], s["mean"])


def cmd_report():
    trades = load()
    df = frame(trades)
    print("=== the adopted rule, and the spread behind its average ===")
    rec, old = split(df)
    for name, sub in (("RECENT", rec), ("OLD", old), ("ALL", df)):
        s = stats(sub)
        print("  %-7s n=%4d win %5.1f%% (bootLo %4.1f) mean %+5.2f p10 %+6.2f"
              % (name, s["n"], s["win"], boot_lo(sub), s["mean"], s["p10"]))

    print()
    print("-- by calendar year")
    df["year"] = df["sig"].str.slice(0, 4)
    for y, g in df.groupby("year"):
        s = stats(g)
        print("   %s  %s  worst %+6.1f%%  exits %s"
              % (y, cell(g), g["ret"].min(),
                 g["exit"].value_counts(normalize=True).round(2).to_dict()))

    print()
    print("-- by quarter (win rate only; the spread is the point)")
    df["q"] = df["sig"].str.slice(0, 4) + "Q" + \
        ((df["sig"].str.slice(5, 7).astype(int) - 1) // 3 + 1).astype(str)
    qs = df.groupby("q").apply(
        lambda g: pd.Series({"n": len(g), "win": 100 * (g["ret"] > 0).mean()}),
        include_groups=False)
    qs = qs[qs["n"] >= 8]
    print("   quarters with n>=8: %d | win min %.1f%% / median %.1f%% / max %.1f%%"
          % (len(qs), qs["win"].min(), qs["win"].median(), qs["win"].max()))
    print("   below 50%%: %d | below 60%%: %d | at or above 70%%: %d"
          % (int((qs["win"] < 50).sum()), int((qs["win"] < 60).sum()),
             int((qs["win"] >= 70).sum())))
    worst = qs.sort_values("win").head(5)
    print("   worst quarters: %s"
          % ", ".join("%s %.0f%% (n=%d)" % (i, r["win"], r["n"])
                      for i, r in worst.iterrows()))

    print()
    print("-- concentration: how much does one stock or one day matter")
    per_sid = df.groupby("sid")["ret"].agg(["size", "mean"])
    print("   distinct stocks %d | most-traded name appears %d times"
          % (len(per_sid), int(per_sid["size"].max())))
    per_day = df.groupby("sig")["ret"].size()
    print("   signal days %d | busiest day %d picks | median %d"
          % (len(per_day), int(per_day.max()), int(per_day.median())))

    print()
    print("-- what actually ends the trade")
    for name, sub in (("RECENT", rec), ("OLD", old)):
        mix = sub["exit"].value_counts(normalize=True).round(3)
        wins = sub.groupby("exit")["ret"].agg(["size", "mean"])
        print("   %s: %s" % (name, ", ".join(
            "%s %.0f%% (mean %+.1f)" % (k, 100 * mix[k], wins.loc[k, "mean"])
            for k in mix.index)))


def _atr_plan(t, k_stop, k_arm, k_lock, floor_stop=0.08, cap_stop=0.30):
    """Levels as multiples of the stock's own 20-day mean range."""
    atr = t.get("atr")
    if atr is None or atr != atr or atr <= 0:
        return None
    a = atr / 100.0
    stop = min(max(k_stop * a, floor_stop), cap_stop)
    arm = max(k_arm * a, 0.01)
    lock = max(k_lock * a, 0.005)
    if lock >= arm:
        lock = arm * 0.8
    return dict(stop=stop, tp=TP, arm=arm, lock=lock, ride="ma5", cap=20)


def cmd_atrscale():
    """Does sizing the stop and the lock to the stock's own volatility make the
    win rate steadier than one fixed percentage for every name?"""
    trades = load()
    base = frame(trades)
    brec, bold = split(base)
    print("=== fixed levels (adopted) ===")
    print("  REC %s bootLo %4.1f | OLD %s bootLo %4.1f"
          % (cell(brec), boot_lo(brec), cell(bold), boot_lo(bold)))
    # spread ACROSS volatility buckets is the stability question
    print("  by ATR bucket (this is what scaling is supposed to flatten):")
    for lo, hi in ((0, 5), (5, 6), (6, 7.5), (7.5, 99)):
        sub = base[(base["atr"] >= lo) & (base["atr"] < hi)]
        r, o = split(sub)
        print("    ATR [%4s,%4s) %s | REC %s | OLD %s"
              % (lo, hi, cell(sub), cell(r), cell(o)))

    print()
    print("=== ATR-scaled levels ===")
    for k_stop, k_arm, k_lock in ((3.0, 0.5, 0.4), (3.5, 0.5, 0.4),
                                  (4.0, 0.5, 0.4), (3.5, 0.4, 0.3),
                                  (3.5, 0.6, 0.5), (3.0, 0.4, 0.3)):
        cand = frame(trades, plan_of=lambda t: _atr_plan(t, k_stop, k_arm, k_lock))
        rec, old = split(cand)
        spread = []
        for lo, hi in ((0, 5), (5, 6), (6, 7.5), (7.5, 99)):
            sub = cand[(cand["atr"] >= lo) & (cand["atr"] < hi)]
            spread.append(stats(sub)["win"] if len(sub) >= 15 else np.nan)
        rng = np.nanmax(spread) - np.nanmin(spread)
        base_spread = []
        for lo, hi in ((0, 5), (5, 6), (6, 7.5), (7.5, 99)):
            sub = base[(base["atr"] >= lo) & (base["atr"] < hi)]
            base_spread.append(stats(sub)["win"] if len(sub) >= 15 else np.nan)
        base_rng = np.nanmax(base_spread) - np.nanmin(base_spread)
        print("  stop %.1fxATR arm %.1fx lock %.1fx | REC %s | OLD %s | "
              "ATR-bucket spread %.1fpp (fixed %.1fpp)"
              % (k_stop, k_arm, k_lock, cell(rec), cell(old), rng, base_rng))


def cmd_regime():
    """The tailwind is already a gate; is its STRENGTH worth grading?"""
    trades = load()
    df = frame(trades)
    print("=== by how far the index sits above its own 20MA on the signal day ===")
    for lo, hi in ((-9, 0.0), (0.0, 0.01), (0.01, 0.022), (0.022, 0.04), (0.04, 9)):
        sub = df[(df["str20"] >= lo) & (df["str20"] < hi)]
        r, o = split(sub)
        print("  str20 [%6.3f,%6.3f) %s | REC %s | OLD %s"
              % (lo, hi, cell(sub), cell(r), cell(o)))
    print()
    print("=== cumulative: only take signals at or above a strength floor ===")
    for floor in (None, 0.0, 0.005, 0.01, 0.015, 0.022, 0.03):
        sub = df if floor is None else df[df["str20"] >= floor]
        r, o = split(sub)
        print("  floor %-6s | REC %s bootLo %4.1f | OLD %s"
              % (floor, cell(r), boot_lo(r), cell(o)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"report": cmd_report, "atrscale": cmd_atrscale, "regime": cmd_regime}[cmd]()
