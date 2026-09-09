"""
sandbox_pattern_mine.py  (SANDBOX_PLAN.md hypothesis H2)

Loser autopsy on the 6-year research replay: computes signal-day features
that are NOT on the playbook's settled/rejected list, buckets each into
train-window quantiles, and reports the full-stack win rate per bucket.

Discipline (SANDBOX_PLAN section 3):
  * features use bars <= signal date only (no lookahead);
  * bucket edges are fit on the TRAIN window (<= 2024-12-31) and frozen;
  * the VALID window (2025+) is only ever evaluated, never re-fit;
  * a candidate is promoted only if the train-window pattern repeats in
    the valid window with n >= 100 and the playbook guard suite passes.

Run:  python sandbox_pattern_mine.py
ASCII only.
"""
import json

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_winrate_round2 import sim_trail
from sandbox_research_replay import (RESEARCH_DB, WARMUP, EVAL_FROM,
                                     TRAIN_END, research_regime)

STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)
N_BUCKETS = 4


def extra_features(df):
    """Per (sid, date) signal-day features beyond er.build_features.
    All windows end at the signal bar -- nothing forward-looking."""
    out = []
    for sid, g in df.rename(columns={"stock_id": "sid"}).groupby("sid"):
        g = g.reset_index(drop=True)
        if len(g) < 70:
            continue
        c, h, l, v = g["close"], g["high"], g["low"], g["Volume_Lot"]
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()],
                       axis=1).max(axis=1)
        h52 = c.rolling(252, min_periods=63).max()
        near = ((h52 - c) / h52) <= 0.05
        above60 = c > c.rolling(60).mean()
        out.append(pd.DataFrame({
            "date": g["date"], "sid": str(sid),
            "atr_pct": tr.rolling(14).mean() / c,
            "dryup": v.rolling(5).mean() / v.rolling(20).mean(),
            "base_len": near.rolling(60, min_periods=20).sum(),
            "days_above60": above60.groupby(
                (~above60).cumsum()).cumcount() + 1,
            "candle_pos": (c - l) / (h - l).replace(0, np.nan),
            "ret20": c / c.shift(21) - 1,
        }))
    return pd.concat(out, ignore_index=True)


def taiex_strength():
    tx = pd.read_csv("data/research_taiex.csv")
    tx["str20"] = tx["close"] / tx["close"].rolling(20).mean() - 1
    return tx.set_index("date")["str20"].to_dict()


def bucket_table(seg_tr, seg_va, feat, edges):
    print("  %-14s %-22s | train           | valid" % (feat, "bucket"))
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        mt = seg_tr[(seg_tr[feat] > lo) & (seg_tr[feat] <= hi)]
        mv = seg_va[(seg_va[feat] > lo) & (seg_va[feat] <= hi)]
        def s(m):
            if len(m) < 20:
                return "n=%3d   --  " % len(m)
            return "n=%3d win=%4.1f%%" % (len(m), 100 * (m["ret"] > 0).mean())
        print("  %-14s (%8.3f,%8.3f] | %s | %s"
              % ("", lo, hi, s(mt), s(mv)))


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
    P = P.merge(T[["date", "sid", "ret5", "dist52", "turn20"]],
                on=["date", "sid"], how="left")
    core = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)
             & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)].copy()

    print("computing signal-day features...")
    X = extra_features(df)
    core = core.merge(X, on=["date", "sid"], how="left")
    core["taiex_str"] = core["date"].map(lambda d: tstr.get(d, np.nan))
    core["dow"] = pd.to_datetime(core["date"]).dt.dayofweek

    sim = sim_trail(core, fwd, **STACK)
    core = core.merge(sim.rename(columns={"ret": "ret"}),
                      on=["date", "sid"], how="inner")

    tr = core[core["date"] <= TRAIN_END]
    va = core[core["date"] > TRAIN_END]
    print("\ntrades: train n=%d win=%.1f%% | valid n=%d win=%.1f%%"
          % (len(tr), 100 * (tr["ret"] > 0).mean(),
             len(va), 100 * (va["ret"] > 0).mean()))

    feats = ["atr_pct", "dryup", "base_len", "days_above60",
             "candle_pos", "ret20", "taiex_str", "turn20", "ls", "rank"]
    print("\n=== per-feature bucket win rates (edges fit on TRAIN only) ===")
    for f in feats:
        s = tr[f].dropna()
        if s.empty:
            continue
        qs = np.unique(s.quantile(np.linspace(0, 1, N_BUCKETS + 1)).values)
        if len(qs) < 3:
            continue
        qs[0] -= 1e-9
        bucket_table(tr, va, f, list(qs))

    print("\n=== day-of-week (observation only) ===")
    for d in range(5):
        mt, mv = tr[tr["dow"] == d], va[va["dow"] == d]
        print("  dow=%d | train n=%3d win=%4.1f%% | valid n=%3d win=%4.1f%%"
              % (d, len(mt), 100 * (mt["ret"] > 0).mean() if len(mt) else 0,
                 len(mv), 100 * (mv["ret"] > 0).mean() if len(mv) else 0))


if __name__ == "__main__":
    main()
