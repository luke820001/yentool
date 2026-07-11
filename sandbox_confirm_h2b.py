"""
sandbox_confirm_h2b.py  (SANDBOX_PLAN.md -- H2 finalist: C2+C3)

Round 1 (sandbox_confirm_h2.py) verdicts:
  * C1 ret20 REJECTED: streak==1 valid window 61.8% vs base 64.3% (worse on
    the view that matters) and 2023 flips negative. Recorded, not adopted.
  * C2 taiex_str>=0.022 and C3 atr_pct>=0.044 each lift train AND valid;
    C2+C3 lifts both windows ~+2.8pp with picks/day nearly intact.

This script finishes the guard suite for C2+C3: yearly-vs-base table,
streak==1 train/valid, fair-universe alpha (C3 applied to the universe
too; C2 is a day condition so it filters universe days identically), and
threshold neighbours on the combo. Also caches the 6-year feature build
to the session scratchpad so iterations stop costing minutes.

Run:  python sandbox_confirm_h2b.py
ASCII only.
"""
import json
import os
import pickle

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import winrate
from eval_winrate_round2 import sim_trail
from sandbox_research_replay import (RESEARCH_DB, WARMUP, EVAL_FROM,
                                     TRAIN_END, research_regime)
from sandbox_pattern_mine import extra_features, taiex_strength

STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)
CACHE = os.path.join(os.environ.get("TEMP", "."),
                     "yentool_sandbox_features.pkl")


def build_all():
    if os.path.exists(CACHE):
        print("loading cached features: %s" % CACHE)
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    er.DB = RESEARCH_DB
    er.WARMUP_START = WARMUP
    print("building features (6y)...")
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    P = er.replay_selection(T)
    X = extra_features(df)
    with open(CACHE, "wb") as f:
        pickle.dump((df, T, P, X), f, protocol=4)
    print("cached -> %s" % CACHE)
    return df, T, P, X


def seg(label, out):
    if out is None or out.empty:
        print("%-40s n=0" % label)
        return
    tr = out[out["date"] <= TRAIN_END]
    va = out[out["date"] > TRAIN_END]
    s = winrate(out)
    def w(m):
        return "n=%4d win=%4.1f%%" % (len(m), 100 * (m["ret"] > 0).mean()) \
            if len(m) else "n=   0          "
    print("%-40s | %s | %s | pool=%4.1f bootLo=%4.1f"
          % (label, w(tr), w(va), s["win"], s["bootLo"]))


def main():
    df, T, P, X = build_all()
    fwd = er.make_fwd(df)
    reg = research_regime()
    tstr = taiex_strength()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = P[P["date"] >= EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    P = P.merge(T[["date", "sid", "ret5", "dist52"]], on=["date", "sid"],
                how="left")
    core = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)
             & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)].copy()
    core = core.merge(X[["date", "sid", "atr_pct"]], on=["date", "sid"],
                      how="left")
    core["taiex_str"] = core["date"].map(lambda d: tstr.get(d, np.nan))

    c2 = core["taiex_str"] >= 0.022
    c3 = core["atr_pct"] >= 0.044
    base_out = sim_trail(core, fwd, **STACK)
    comb_out = sim_trail(core[c2 & c3], fwd, **STACK)

    print("\n=== guard: combo threshold neighbours ===")
    hdr = "%-40s | %-22s | %-22s |" % ("", "train <=2024", "valid 2025+")
    print(hdr)
    seg("BASE", base_out)
    for t2, t3 in ((0.018, 0.044), (0.022, 0.040), (0.022, 0.044),
                   (0.022, 0.048), (0.026, 0.044)):
        m = (core["taiex_str"] >= t2) & (core["atr_pct"] >= t3)
        seg("C2>=%.3f C3>=%.3f" % (t2, t3), sim_trail(core[m], fwd, **STACK))

    print("\n=== guard: yearly, combo vs base (win / mean) ===")
    for y in sorted(base_out["date"].str[:4].unique()):
        b = base_out[base_out["date"].str[:4] == y]["ret"].to_numpy()
        c = comb_out[comb_out["date"].str[:4] == y]["ret"].to_numpy()
        print("  %s base n=%4d %4.1f%% %+5.2f | combo n=%4d %4.1f%% %+5.2f"
              % (y, len(b), 100 * (b > 0).mean(), b.mean(),
                 len(c), (100 * (c > 0).mean()) if len(c) else 0,
                 c.mean() if len(c) else 0))

    print("\n=== guard: streak==1 real-entry view ===")
    print(hdr)
    s1 = core["streak"] == 1
    seg("BASE streak==1", sim_trail(core[s1], fwd, **STACK))
    seg("C2 streak==1", sim_trail(core[s1 & c2], fwd, **STACK))
    seg("C3 streak==1", sim_trail(core[s1 & c3], fwd, **STACK))
    seg("C2+C3 streak==1", sim_trail(core[s1 & c2 & c3], fwd, **STACK))

    print("\n=== guard: fair-universe alpha (same filters on universe) ===")
    uni = T[T["date"].isin(core["date"].unique())][
        ["date", "sid", "ret5", "dist52"]].copy()
    uni["mkt"] = uni["sid"].map(market).fillna("?")
    uni["ro"] = uni["date"].map(lambda d: reg.get(d, False))
    uni = uni[(uni["mkt"] == "OTC") & uni["ro"]
              & (uni["dist52"] <= 0.05) & (uni["ret5"] <= 0.05)]
    uni = uni.merge(X[["date", "sid", "atr_pct"]], on=["date", "sid"],
                    how="left")
    uni["taiex_str"] = uni["date"].map(lambda d: tstr.get(d, np.nan))
    u = uni[(uni["taiex_str"] >= 0.022) & (uni["atr_pct"] >= 0.044)]
    uout = sim_trail(u, fwd, **STACK)
    seg("universe, same C2+C3 filters", uout)
    if not comb_out.empty and not uout.empty:
        print("fair alpha: %+.2f pp (win) %+.2f pp (mean)"
              % (100 * (comb_out["ret"] > 0).mean()
                 - 100 * (uout["ret"] > 0).mean(),
                 comb_out["ret"].mean() - uout["ret"].mean()))

    print("\npicks/day: base %.2f -> combo %.2f"
          % (len(base_out) / base_out["date"].nunique(),
             len(comb_out) / comb_out["date"].nunique()))


if __name__ == "__main__":
    main()
