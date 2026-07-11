"""
sandbox_confirm_h2.py  (SANDBOX_PLAN.md -- H2 candidate confirmation)

The mining pass (sandbox_pattern_mine.py) surfaced three candidates whose
direction repeats in BOTH the train (<=2024) and valid (2025+) windows:

  C1  ret20 high     strong 1-month momentum, paused last week
  C2  taiex_str high market well above its 20MA, not merely risk_on
  C3  atr_pct floor  exclude the too-quiet bottom quartile

This script applies the playbook guard suite to each candidate and to the
2-way combos: threshold plateau (3 neighbours), train AND valid windows
improving separately, picks/day usability, yearly breakdown for the
finalist. Everything is evaluated on the full-stack simulation.

Run:  python sandbox_confirm_h2.py
ASCII only.
"""
import json

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import winrate
from eval_winrate_round2 import sim_trail
from sandbox_research_replay import (RESEARCH_DB, WARMUP, EVAL_FROM,
                                     TRAIN_END, research_regime)
from sandbox_pattern_mine import extra_features, taiex_strength

STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)


def seg_line(label, out, dates):
    """Print train/valid/pooled win rates for one rule variant."""
    if out.empty:
        print("%-38s n=0" % label)
        return
    tr = out[out["date"] <= TRAIN_END]
    va = out[out["date"] > TRAIN_END]
    days = out["date"].nunique()
    def w(m):
        return "n=%4d win=%4.1f%%" % (len(m), 100 * (m["ret"] > 0).mean()) \
            if len(m) else "n=   0          "
    s = winrate(out)
    print("%-38s | %s | %s | pool win=%4.1f bootLo=%4.1f | %4.2f/d"
          % (label, w(tr), w(va), s["win"], s["bootLo"],
             len(out) / max(days, 1)))


def main():
    er.DB = RESEARCH_DB
    er.WARMUP_START = WARMUP

    print("building features (6y)...")
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = research_regime()
    tstr = taiex_strength()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = er.replay_selection(T)
    P = P[P["date"] >= EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    P = P.merge(T[["date", "sid", "ret5", "dist52", "ret60"]],
                on=["date", "sid"], how="left")
    core = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)
             & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)].copy()

    print("computing signal-day features...")
    X = extra_features(df)
    core = core.merge(X[["date", "sid", "atr_pct", "ret20"]],
                      on=["date", "sid"], how="left")
    core["taiex_str"] = core["date"].map(lambda d: tstr.get(d, np.nan))

    def run(label, mask):
        seg_line(label, sim_trail(core[mask], fwd, **STACK), None)

    base_mask = core["sid"].notna()
    print("\n%-38s | %-22s | %-22s |" % ("", "train <=2024", "valid 2025+"))
    run("BASE core+ full stack", base_mask)

    print("\n--- C1 ret20 plateau ---")
    for th in (0.25, 0.30, 0.35, 0.40):
        run("ret20 >= %.2f" % th, core["ret20"] >= th)

    print("\n--- C2 taiex_str plateau ---")
    for th in (0.015, 0.022, 0.030):
        run("taiex_str >= %.3f" % th, core["taiex_str"] >= th)

    print("\n--- C3 atr floor plateau ---")
    for th in (0.040, 0.044, 0.048):
        run("atr_pct >= %.3f" % th, core["atr_pct"] >= th)

    print("\n--- 2-way combos (mid thresholds) ---")
    c1 = core["ret20"] >= 0.30
    c2 = core["taiex_str"] >= 0.022
    c3 = core["atr_pct"] >= 0.044
    run("C1+C2", c1 & c2)
    run("C1+C3", c1 & c3)
    run("C2+C3", c2 & c3)
    run("C1+C2+C3", c1 & c2 & c3)

    print("\n--- yearly breakdown: best combo vs base ---")
    for lbl, mask in (("BASE", base_mask), ("C1+C3", c1 & c3)):
        out = sim_trail(core[mask], fwd, **STACK)
        print("  %s:" % lbl)
        for y, g in out.groupby(out["date"].str[:4]):
            r = g["ret"].to_numpy()
            print("    %s n=%4d win=%4.1f%% mean=%+5.2f"
                  % (y, len(r), 100 * (r > 0).mean(), r.mean()))

    print("\n--- streak==1 view on candidates ---")
    s1 = core["streak"] == 1
    run("BASE streak==1", s1)
    run("C1 streak==1", s1 & c1)
    run("C1+C3 streak==1", s1 & c1 & c3)


if __name__ == "__main__":
    main()
