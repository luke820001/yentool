"""
eval_winrate_round2b.py

Finalist matrix for the round-2 trailing-lock finding (see
eval_winrate_round2.py): sensitivity plateau around arm/lock, interaction with
hold length and take-profit, quarterly stability and the streak==1 real-entry
view for the chosen configuration.

Run:  python eval_winrate_round2b.py
ASCII only.
"""
import json

import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import regime_map, winrate
from eval_winrate_search import FULL_WARMUP, FULL_START
from eval_winrate_round2 import sim_trail, line
from eval_winrate_final import quarterly


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
    base = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)]
    core = base[(base["dist52"] <= 0.05) & (base["ret5"] <= 0.05)]

    print("=== arm/lock plateau (hold10, stop15) ===")
    for arm in (0.06, 0.08, 0.10):
        for lock in (0.01, 0.02, 0.03):
            line("arm%d lock+%d" % (arm * 100, lock * 100),
                 sim_trail(core, fwd, stop=0.15, arm=arm, lock=lock))

    print("\n=== hold x tp interaction (stop15, trail arm8 lock+2) ===")
    for h in (10, 12, 15):
        line("hold%d trail only" % h,
             sim_trail(core, fwd, hold=h, stop=0.15, arm=0.08, lock=0.02))
        line("hold%d trail + tp20" % h,
             sim_trail(core, fwd, hold=h, stop=0.15, tp=0.20, arm=0.08, lock=0.02))
        line("hold%d trail + tp25" % h,
             sim_trail(core, fwd, hold=h, stop=0.15, tp=0.25, arm=0.08, lock=0.02))

    print("\n=== quarterly stability of the finalist ===")
    quarterly("hold10 stop15 trail(8,+2)",
              sim_trail(core, fwd, stop=0.15, arm=0.08, lock=0.02))
    quarterly("hold10 stop15 tp20 trail(8,+2)",
              sim_trail(core, fwd, stop=0.15, tp=0.20, arm=0.08, lock=0.02))

    print("\n=== real-entry view (streak==1) ===")
    s1 = core[core["streak"] == 1]
    line("streak1 plain hold10", sim_trail(s1, fwd, stop=None))
    line("streak1 stop15 trail(8,+2)",
         sim_trail(s1, fwd, stop=0.15, arm=0.08, lock=0.02))
    line("streak1 stop15 tp20 trail(8,+2)",
         sim_trail(s1, fwd, stop=0.15, tp=0.20, arm=0.08, lock=0.02))


if __name__ == "__main__":
    main()
