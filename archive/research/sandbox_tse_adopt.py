"""
sandbox_tse_adopt.py

Full adoption-gate validation for the TSE broadening (appendix C option B):
  base  = adopted OTC-CORE+  (OTC + risk_on + rank<20 + dist52<=5 + ret5<=5 + ATR>=4.5)
  cand  = OTC+TSE-CORE+      (drop the OTC restriction; everything else identical)

Adoption bar (project discipline): win / bootLo / h1 / h2 all >= base, fair
alpha vs the SAME-filtered universe positive, quarterly stability, and the
capital-utilization angle (names/day, since the whole point is deploying more
capital). Reports 6y OOS and the 2y AI window. Also isolates the ADDED TSE
subset so we can see directly whether it dilutes.

Run:  python sandbox_tse_adopt.py
ASCII only.
"""
import json

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import winrate
from eval_winrate_round2 import sim_trail
from sandbox_research_replay import RESEARCH_DB, WARMUP, EVAL_FROM
from sandbox_regime_support import regime_states

STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)
ATR_MIN = 4.5


def row(label, out):
    s = winrate(out)
    if not s:
        print("  %-26s n=   0" % label)
        return None
    print("  %-26s n=%4d win=%5.1f%% bootLo=%4.1f h1=%4.1f h2=%4.1f mean=%+5.2f worst=%+5.1f"
          % (label, s["n"], s["win"], s["bootLo"], s["h1"], s["h2"], s["mean"],
             out["ret"].min()))
    return s


def yearly(label, out):
    if out is None or out.empty:
        return
    print("  %s by year:" % label)
    for y, g in out.groupby(out["date"].str[:4]):
        r = g["ret"].to_numpy()
        print("    %s n=%4d win=%5.1f%% mean=%+5.2f" % (y, len(r), (r > 0).mean()*100, r.mean()))


def atr_map(df):
    d = df.rename(columns={"stock_id": "sid"}).sort_values(["sid", "date"])
    atr = ((d["high"] - d["low"]) / d["close"]).groupby(d["sid"]).transform(
        lambda s: s.rolling(20).mean()) * 100
    return dict(zip(zip(d["sid"].astype(str), d["date"]), atr))


def main():
    er.DB = RESEARCH_DB
    er.WARMUP_START = WARMUP
    print("building features from %s (be patient)..." % RESEARCH_DB)
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = regime_states()
    amap = atr_map(df)

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = er.replay_selection(T)
    P = P[P["date"] >= EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, ("?", False))[0] == "risk_on")
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")
    P["atr"] = [amap.get((str(s), d)) for s, d in zip(P["sid"], P["date"])]

    gate = (P["ro"] & (P["rank"] < 20) & (P["dist52"] <= 0.05)
            & (P["ret5"] <= 0.05) & (P["atr"] >= ATR_MIN))
    base = P[gate & (P["mkt"] == "OTC")].copy()      # adopted
    cand = P[gate].copy()                            # OTC+TSE (drop mkt filter)
    added = P[gate & (P["mkt"] == "TSE")].copy()     # the newly-included subset

    # fair universe: same gate on the whole tape (not just the shortlist)
    uni = T[["date", "sid", "c", "ret5", "dist52"]].rename(columns={"c": "sig_close"}).copy()
    uni["mkt"] = uni["sid"].map(market).fillna("?")
    uni["ro"] = uni["date"].map(lambda d: reg.get(d, ("?", False))[0] == "risk_on")
    uni["atr"] = [amap.get((str(s), d)) for s, d in zip(uni["sid"], uni["date"])]
    uni = uni[uni["date"].isin(P["date"].unique())]
    ug = (uni["ro"] & (uni["dist52"] <= 0.05) & (uni["ret5"] <= 0.05) & (uni["atr"] >= ATR_MIN))
    uni_otc = uni[ug & (uni["mkt"] == "OTC")]
    uni_all = uni[ug & (uni["mkt"].isin(["OTC", "TSE"]))]

    def windows(sub_base, sub_cand, sub_add, ubase, uall, tag):
        print("\n=== %s ===" % tag)
        ob = sim_trail(sub_base, fwd, **STACK)
        oc = sim_trail(sub_cand, fwd, **STACK)
        oa = sim_trail(sub_add, fwd, **STACK)
        sb = row("base OTC-CORE+ (adopted)", ob)
        sc = row("cand OTC+TSE-CORE+", oc)
        sa = row("  ...added TSE subset", oa)
        ub = sim_trail(ubase, fwd, **STACK)
        ua = sim_trail(uall, fwd, **STACK)
        if sb and sc:
            print("  fair alpha: base %+.2fpp | cand %+.2fpp (pick mean - same-filter universe mean)"
                  % (ob["ret"].mean() - ub["ret"].mean(),
                     oc["ret"].mean() - ua["ret"].mean()))
            db = sub_base.groupby("date").size().mean()
            dc = sub_cand.groupby("date").size().mean()
            print("  names/day: base %.1f  cand %.1f  (capital utilisation x%.1f)"
                  % (db, dc, dc / db if db else 0))
        return ob, oc

    # 6y
    ob, oc = windows(base, cand, added, uni_otc, uni_all, "6-year OOS")
    yearly("base OTC-CORE+", ob)
    yearly("cand OTC+TSE-CORE+", oc)

    # 2y AI window
    def win2(x):
        return x[x["date"] >= "2024-07-01"]
    windows(win2(base), win2(cand), win2(added),
            win2(uni_otc), win2(uni_all), "2-year AI window (2024-07+)")


if __name__ == "__main__":
    main()
