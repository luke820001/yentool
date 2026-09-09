"""
sandbox_research_replay.py  (SANDBOX_PLAN.md hypothesis H1)

Replays the ADOPTED prelaunch rule stack on research_prices.db -- 6 years
(2020-09 .. 2026-06, 1957 stocks, includes the 2022 bear) instead of the
14-month price_volume.db window the 71% was tuned on. This is an
out-of-sample validity check, not a parameter search: if the stack holds
year by year, the number is real; if a bear year breaks it, the next work
item is regime conditioning, not more filters.

Reuses the exact shipped-formula machinery from eval_realtrade /
eval_winrate_round2 (launch_score, hysteresis replay, sim_trail).
TAIEX history beyond taiex.db's window is fetched once from yfinance and
cached at data/research_taiex.csv.

Run:  python sandbox_research_replay.py
ASCII only.
"""
import json
import os
import warnings

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import winrate
from eval_winrate_round2 import sim_trail, line

RESEARCH_DB = "data/research_prices.db"
TAIEX_CACHE = "data/research_taiex.csv"
WARMUP = "2020-07-01"     # features need ~63 bars from 2020-03-25 db start
EVAL_FROM = "2020-09-01"
TRAIN_END = "2024-12-31"  # SANDBOX_PLAN section 3: mine <= here, validate after


def research_regime():
    """date -> risk_on (TAIEX above both 20MA and 60MA), 6-year span."""
    if os.path.exists(TAIEX_CACHE):
        tx = pd.read_csv(TAIEX_CACHE)
    else:
        import yfinance as yf
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = yf.download("^TWII", start="2019-10-01",
                              auto_adjust=True, progress=False)
        raw = raw.reset_index()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [str(c[0]).lower() for c in raw.columns]
        else:
            raw.columns = [str(c).lower() for c in raw.columns]
        tx = pd.DataFrame({
            "date": pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d"),
            "close": raw["close"],
        }).dropna()
        tx = tx[tx["close"] > 0]          # drop synthetic/holiday zero rows
        tx.to_csv(TAIEX_CACHE, index=False)
        print("cached %d TAIEX bars -> %s" % (len(tx), TAIEX_CACHE))
    tx["risk_on"] = ((tx["close"] > tx["close"].rolling(20).mean())
                     & (tx["close"] > tx["close"].rolling(60).mean()))
    return tx.set_index("date")["risk_on"].to_dict()


def yearly(label, out):
    if out is None or out.empty:
        return
    print("  %s by year:" % label)
    for y, g in out.groupby(out["date"].str[:4]):
        r = g["ret"].to_numpy()
        print("    %s n=%4d win=%4.1f%% mean=%+5.2f worst=%+6.1f"
              % (y, len(r), 100 * (r > 0).mean(), r.mean(), r.min()))


def main():
    er.DB = RESEARCH_DB
    er.WARMUP_START = WARMUP

    print("building features from %s (6y, be patient)..." % RESEARCH_DB)
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = research_regime()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = er.replay_selection(T)
    P = P[P["date"] >= EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(
        columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")

    base = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)]
    core = base[(base["dist52"] <= 0.05) & (base["ret5"] <= 0.05)]
    s1 = core[core["streak"] == 1]

    # fair benchmark: same filters applied to the whole universe
    uni = T[T["date"].isin(P["date"].unique())][
        ["date", "sid", "ret5", "dist52"]].copy()
    uni["mkt"] = uni["sid"].map(market).fillna("?")
    uni["ro"] = uni["date"].map(lambda d: reg.get(d, False))
    uq = uni[(uni["mkt"] == "OTC") & uni["ro"]
             & (uni["dist52"] <= 0.05) & (uni["ret5"] <= 0.05)]

    print("\n=== H1: adopted stack, 6-year out-of-sample (%s .. ) ==="
          % EVAL_FROM)
    print("picks/day: base %.1f  core+ %.1f  (days %d)"
          % (base.groupby("date")["sid"].nunique().mean() if len(base) else 0,
             core.groupby("date")["sid"].nunique().mean() if len(core) else 0,
             base["date"].nunique()))

    stack = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)
    out_core = sim_trail(core, fwd, **stack)
    out_s1 = sim_trail(s1, fwd, **stack)
    out_uni = sim_trail(uq, fwd, **stack)
    line("CORE+ full stack (pooled)", out_core)
    line("CORE+ full stack (streak==1)", out_s1)
    line("fair universe, same stack", out_uni)
    if not out_core.empty and not out_uni.empty:
        print("fair alpha (pooled - universe): %+.2f pp"
              % (out_core["ret"].mean() - out_uni["ret"].mean()))
    line("CORE+ plain hold10 (no exits)", sim_trail(core, fwd, stop=None))

    print("\n=== yearly stability (the actual test) ===")
    yearly("CORE+ full stack (pooled)", out_core)
    yearly("CORE+ full stack (streak==1)", out_s1)
    yearly("fair universe", out_uni)

    print("\n=== train/validate split sanity (plan section 3) ===")
    for lbl, lo, hi in (("train  <=2024", "0000", TRAIN_END),
                        ("valid  2025+", TRAIN_END, "9999")):
        seg = out_s1[(out_s1["date"] > lo) & (out_s1["date"] <= hi)] \
            if lbl.startswith("train") else out_s1[out_s1["date"] > TRAIN_END]
        s = winrate(seg)
        if s:
            print("  %s n=%4d win=%4.1f%% bootLo=%4.1f mean=%+5.2f"
                  % (lbl, s["n"], s["win"], s["bootLo"], s["mean"]))


if __name__ == "__main__":
    main()
