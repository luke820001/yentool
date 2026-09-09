"""
eval_winrate_final.py

Focused validation of the winning candidate from eval_winrate_search.py:

  CORE+ = adopted base (OTC + risk_on + rank<20 + hold10)
          + dist52 <= 0.05 (within 5pct of 52w high)
          + ret5  <= 0.05 (not already up >5pct in 5 days)

Checks the search pass could not: quarterly stability (not just halves), a FAIR
benchmark (same dist52/ret5 filter applied to the universe, so the lift is not
just "that slice of the tape did better"), picks/day usability, stop/tp
interaction on the final rule, and the streak==1 real-entry view.

Run:  python eval_winrate_final.py
ASCII only.
"""
import json

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import regime_map, winrate
from eval_winrate_search import simulate2, show, FULL_WARMUP, FULL_START


def quarterly(label, out):
    if out.empty:
        return
    q = out.copy()
    q["q"] = q["date"].str[:7].map(
        lambda m: m[:5] + {"01": "Q1", "02": "Q1", "03": "Q1",
                           "04": "Q2", "05": "Q2", "06": "Q2",
                           "07": "Q3", "08": "Q3", "09": "Q3",
                           "10": "Q4", "11": "Q4", "12": "Q4"}[m[5:7]])
    print("  %s by quarter:" % label)
    for qq, g in q.groupby("q"):
        r = g["ret"].to_numpy()
        print("    %s n=%4d win=%4.1f%% mean=%+5.2f"
              % (qq, len(r), 100 * (r > 0).mean(), r.mean()))


def main():
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = regime_map()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    er.WARMUP_START = FULL_WARMUP
    P = er.replay_selection(T)
    P = P[P["date"] >= FULL_START].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")

    uni = T[T["date"].isin(P["date"].unique())][
        ["date", "sid", "c", "ret5", "dist52"]].rename(columns={"c": "sig_close"}).copy()
    uni["mkt"] = uni["sid"].map(market).fillna("?")
    uni["ro"] = uni["date"].map(lambda d: reg.get(d, False))

    base = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)]
    q = (base["dist52"] <= 0.05) & (base["ret5"] <= 0.05)
    core = base[q]

    ubase = uni[(uni["mkt"] == "OTC") & uni["ro"]]
    uq = ubase[(ubase["dist52"] <= 0.05) & (ubase["ret5"] <= 0.05)]

    print("=== picks/day usability ===")
    for lbl, s in (("base", base), ("CORE+", core)):
        byday = s.groupby("date")["sid"].nunique()
        print("  %-6s days=%3d avg=%4.1f/day  zero-days=%d"
              % (lbl, byday.shape[0], byday.mean(),
                 (len(base["date"].unique()) - byday.shape[0])))

    print("\n=== CORE+ vs benchmarks (hold10 plain) ===")
    out_core = simulate2(core, fwd)
    show("BASE (adopted)", simulate2(base, fwd), simulate2(ubase, fwd))
    show("CORE+ vs raw universe", out_core, simulate2(ubase, fwd))
    show("CORE+ vs FAIR (filtered) universe", out_core, simulate2(uq, fwd))
    show("  fair universe itself", simulate2(uq, fwd))

    print("\n=== quarterly stability ===")
    quarterly("BASE", simulate2(base, fwd))
    quarterly("CORE+", out_core)

    print("\n=== exit interaction on CORE+ ===")
    for lbl, kw in (("plain hold10", {}),
                    ("stop10 (live)", {"stop": 0.10}),
                    ("stop12", {"stop": 0.12}),
                    ("stop15", {"stop": 0.15}),
                    ("tp20", {"tp": 0.20}),
                    ("tp25", {"tp": 0.25}),
                    ("stop15+tp20", {"stop": 0.15, "tp": 0.20}),
                    ("stop15+tp25", {"stop": 0.15, "tp": 0.25})):
        out = simulate2(core, fwd, **kw)
        s = winrate(out)
        if s:
            print("%-16s n=%4d win=%4.1f%% bootLo=%4.1f h1=%4.1f h2=%4.1f "
                  "mean=%+5.2f worst=%+6.1f"
                  % (lbl, s["n"], s["win"], s["bootLo"], s["h1"], s["h2"],
                     s["mean"], out["ret"].min()))

    print("\n=== real-entry view (streak==1 only, what a trader actually takes) ===")
    show("BASE streak==1", simulate2(base[base["streak"] == 1], fwd))
    show("CORE+ streak==1", simulate2(core[core["streak"] == 1], fwd))
    show("CORE+ streak==1 stop15", simulate2(core[core["streak"] == 1], fwd, stop=0.15))

    print("\n=== sensitivity: neighbours of the chosen thresholds ===")
    for d5, r5 in ((0.04, 0.05), (0.06, 0.05), (0.05, 0.04), (0.05, 0.06),
                   (0.07, 0.07), (0.03, 0.03)):
        m = (base["dist52"] <= d5) & (base["ret5"] <= r5)
        show("dist52<=%.2f ret5<=%.2f" % (d5, r5), simulate2(base[m], fwd))


if __name__ == "__main__":
    main()
