"""
sandbox_atr_gap.py

Answers two execution-risk critiques of the prelaunch stack on the 6y research
db (and the 2y AI-era slice):

  Q3a  Does high ATR + the +6% trail-arm / +2% lock cause whipsaw? Split the
       adopted CORE+ (risk_on) picks by signal-day ATR and compare the FULL
       stack win rate. If Gemini is right, the highest-ATR names win LESS.

  Q3b  Next-open market entry gap risk: distribution of (next_open/sig_close-1)
       for the CORE+ entry set. How often does the open gap DOWN toward the
       -15% stop, or gap UP enough to matter? Also the win rate with a
       max-gap-up entry skip (the rejected filter), for reference.

Run:  python sandbox_atr_gap.py [eval_from] [warmup]
ASCII only.
"""
import json
import sys

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_winrate_round2 import sim_trail
from eval_prelaunch_overlays import winrate
from sandbox_research_replay import RESEARCH_DB, WARMUP, EVAL_FROM


def atr_map(df):
    """(sid,date) -> ATR_Pct = mean_20 (high-low)/close * 100 at signal day."""
    d = df.rename(columns={"stock_id": "sid"}).sort_values(["sid", "date"])
    g = d.groupby("sid")
    atr = ((d["high"] - d["low"]) / d["close"]).groupby(d["sid"]).transform(
        lambda s: s.rolling(20).mean())
    return dict(zip(zip(d["sid"].astype(str), d["date"]), atr * 100))


def line(label, out):
    s = winrate(out)
    if not s or s["n"] == 0:
        print("  %-30s n=   0" % label)
        return
    print("  %-30s n=%4d win=%5.1f%% h1=%4.1f h2=%4.1f mean=%+6.2f worst=%+6.1f"
          % (label, s["n"], s["win"], s["h1"], s["h2"], s["mean"], out["ret"].min()))


def main():
    eval_from = sys.argv[1] if len(sys.argv) > 1 else EVAL_FROM
    warmup = sys.argv[2] if len(sys.argv) > 2 else WARMUP
    er.DB = RESEARCH_DB
    er.WARMUP_START = warmup
    print("window: eval_from=%s warmup=%s" % (eval_from, warmup))

    print("building features (be patient)...")
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)

    from sandbox_regime_support import regime_states
    states = regime_states()
    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = er.replay_selection(T)
    P = P[P["date"] >= eval_from].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(
        columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")
    P["state"] = P["date"].map(lambda d: states.get(d, ("?", False))[0])

    amap = atr_map(df)
    P["atr"] = [amap.get((str(s), d)) for s, d in zip(P["sid"], P["date"])]

    core = P[(P["mkt"] == "OTC") & (P["state"] == "risk_on") & (P["rank"] < 20)
             & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)
             & (P["atr"] >= 4.5)].copy()
    stack = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)

    print("\n=== Q3a: does higher ATR whipsaw the +6%/+2% trail? (CORE+, full stack) ===")
    print("  (all CORE+ already have ATR>=4.5; split the survivors)")
    q1, q2 = core["atr"].quantile([0.5, 0.8])
    buckets = [
        ("ATR 4.5-%.1f (lower half)" % q1, core[core["atr"] < q1]),
        ("ATR %.1f-%.1f" % (q1, q2), core[(core["atr"] >= q1) & (core["atr"] < q2)]),
        ("ATR >=%.1f (top fifth)" % q2, core[core["atr"] >= q2]),
    ]
    line("ALL CORE+ full stack", sim_trail(core, fwd, **stack))
    for lbl, sub in buckets:
        line(lbl, sim_trail(sub, fwd, **stack))
    print("  -- compare NO-trail (stop15+tp20 only) to isolate the trail's effect:")
    for lbl, sub in buckets:
        line(lbl + " [no trail]", sim_trail(sub, fwd, stop=0.15, tp=0.20))

    print("\n=== Q3b: next-open gap risk (CORE+ entry set) ===")
    gaps = []
    for r in core.itertuples(index=False):
        fb = fwd(r.sid, r.date, 1)
        if fb is None or len(fb) < 1:
            continue
        op = float(fb.iloc[0]["open"])
        sc = getattr(r, "sig_close", None)
        if op > 0 and sc and np.isfinite(sc) and sc > 0:
            gaps.append(op / sc - 1)
    g = np.array(gaps) * 100
    if len(g):
        print("  n=%d  mean gap=%+.2f%%  median=%+.2f%%" % (len(g), g.mean(), np.median(g)))
        print("  gap distribution (open vs signal close):")
        for lo, hi, lbl in [(-100, -8, "<= -8% (near disaster)"),
                            (-8, -4, "-8..-4%"), (-4, -2, "-4..-2%"),
                            (-2, 2, "-2..+2% (flat)"), (2, 4, "+2..+4%"),
                            (4, 8, "+4..+8%"), (8, 100, ">= +8% (chase risk)")]:
            pct = 100.0 * ((g >= lo) & (g < hi)).mean()
            print("    %-22s %5.1f%%" % (lbl, pct))
        print("  gap-up >5%% share: %.1f%%   gap-dn >5%% share: %.1f%%"
              % (100 * (g > 5).mean(), 100 * (g < -5).mean()))
    # the rejected max-gap-up entry filter, for the record
    print("  win rate if we SKIP entries gapping up > 3%% (rejected filter):")
    keep = core.copy()
    keep["_op"] = [ (fwd(r.sid, r.date, 1).iloc[0]["open"]
                     if (fwd(r.sid, r.date, 1) is not None
                         and len(fwd(r.sid, r.date, 1))) else np.nan)
                    for r in core.itertuples(index=False) ]
    keep = keep[np.isfinite(keep["_op"]) & (keep["_op"] / keep["sig_close"] - 1 <= 0.03)]
    line("CORE+ minus gap-ups >3%", sim_trail(keep, fwd, **stack))


if __name__ == "__main__":
    main()
