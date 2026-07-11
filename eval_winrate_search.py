"""
eval_winrate_search.py

Systematic search for overlays that raise the ADOPTED combo's win rate
(OTC + risk_on + rank<20 + hold 10) with the project's anti-curve-fit guards:
every candidate is judged on the full 214-day replay by win, bootLo,
h1 AND h2 (both halves must improve), and alpha vs the matched universe.

Adds to eval_realtrade features: signal-day volume ratio, signal-day change,
and entry-gap awareness (entry open vs signal close) so anti-chasing filters
and take-profit exits can be tested realistically.

Run:  python eval_winrate_search.py
ASCII only.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import regime_map, winrate

FULL_WARMUP = "2025-06-02"
FULL_START = "2025-08-01"
HOLD = 10


def show(label, out, bench=None):
    s = winrate(out)
    if s is None or s["n"] == 0:
        print("%-40s n=0" % label)
        return None
    a = ""
    if bench is not None and not bench.empty:
        b = winrate(bench)
        if b:
            a = "  alpha=%+.1fpp" % (s["win"] - b["win"])
    print("%-40s n=%4d win=%4.1f%% bootLo=%4.1f h1=%4.1f h2=%4.1f mean=%+5.2f%s"
          % (label, s["n"], s["win"], s["bootLo"], s["h1"], s["h2"], s["mean"], a))
    return s


def simulate2(rows, fwd, hold=HOLD, stop=None, tp=None, max_gap=None):
    """Entry next-day open. Options:
       stop    intraday disaster stop, fraction below entry (e.g. 0.10)
       tp      intraday take-profit, fraction above entry (e.g. 0.20)
       max_gap skip the trade when entry open gaps above signal close by more
               than this fraction (decidable at the open in live trading)
    """
    rets = []
    for r in rows.itertuples(index=False):
        fb = fwd(r.sid, r.date, hold)
        if fb is None or len(fb) < hold:
            continue
        e = float(fb.iloc[0]["open"])
        if not np.isfinite(e) or e <= 0:
            continue
        if max_gap is not None:
            sc = getattr(r, "sig_close", None)
            if sc and np.isfinite(sc) and sc > 0 and e / sc - 1 > max_gap:
                continue
        ret = None
        for i in range(hold):
            b = fb.iloc[i]
            if stop is not None and float(b["low"]) <= e * (1 - stop):
                px = min(float(b["open"]), e * (1 - stop)) if i > 0 else e * (1 - stop)
                ret = px / e - 1
                break
            if tp is not None and float(b["high"]) >= e * (1 + tp):
                px = max(float(b["open"]), e * (1 + tp)) if i > 0 else e * (1 + tp)
                ret = px / e - 1
                break
        if ret is None:
            ret = float(fb.iloc[hold - 1]["close"]) / e - 1
        rets.append((r.date, r.sid, ret * 100))
    return pd.DataFrame(rets, columns=["date", "sid", "ret"])


def main():
    df, T = er.build_features()
    # extra signal-day features for entry filters
    grp = df.sort_values(["stock_id", "date"]).groupby("stock_id")
    df2 = df.sort_values(["stock_id", "date"]).copy()
    df2["vr"] = df2["Volume_Lot"] / grp["Volume_Lot"].transform(
        lambda s: s.rolling(20).mean())
    T = T.merge(df2.rename(columns={"stock_id": "sid"})[["date", "sid", "vr"]],
                on=["date", "sid"], how="left")
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
    # attach signal-day features to each pick
    feat = T[["date", "sid", "c", "ret5", "rt", "bias", "dist52",
              "day_chg", "vr", "turn20"]].rename(columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")

    uni = T[T["date"].isin(P["date"].unique())][["date", "sid", "c"]].rename(
        columns={"c": "sig_close"}).copy()
    uni["mkt"] = uni["sid"].map(market).fillna("?")
    uni["ro"] = uni["date"].map(lambda d: reg.get(d, False))

    base = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)]
    ubase = uni[(uni["mkt"] == "OTC") & uni["ro"]]
    bench = simulate2(ubase, fwd)

    print("=== base: OTC + risk_on + rank<20 + hold10 (ADOPTED) ===")
    show("BASE", simulate2(base, fwd), bench)
    print()

    print("=== exit variants on BASE ===")
    show("stop10 (live rule)", simulate2(base, fwd, stop=0.10), bench)
    show("stop12", simulate2(base, fwd, stop=0.12), bench)
    show("stop15", simulate2(base, fwd, stop=0.15), bench)
    show("tp15", simulate2(base, fwd, tp=0.15), bench)
    show("tp20", simulate2(base, fwd, tp=0.20), bench)
    show("tp25", simulate2(base, fwd, tp=0.25), bench)
    show("stop10+tp20", simulate2(base, fwd, stop=0.10, tp=0.20), bench)
    print()

    print("=== entry-gap filter on BASE (skip if open gaps > x over signal close) ===")
    for g in (0.02, 0.03, 0.05):
        show("max_gap %.0f%%" % (g * 100), simulate2(base, fwd, max_gap=g), bench)
    print()

    print("=== single entry filters on BASE ===")
    filters = [
        ("ls>=40",        base["ls"] >= 40),
        ("ls>=50",        base["ls"] >= 50),
        ("bias>=0.55",    base["bias"] >= 0.55),
        ("bias>=0.60",    base["bias"] >= 0.60),
        ("rt<=0.15",      base["rt"] <= 0.15),
        ("rt<=0.20",      base["rt"] <= 0.20),
        ("dist52<=0.10",  base["dist52"] <= 0.10),
        ("dist52<=0.05",  base["dist52"] <= 0.05),
        ("ret5<=0.05",    base["ret5"] <= 0.05),
        ("ret5<=0.03",    base["ret5"] <= 0.03),
        ("day_chg<=0.04", base["day_chg"] <= 0.04),
        ("day_chg<=0.02", base["day_chg"] <= 0.02),
        ("vr>=1.0",       base["vr"] >= 1.0),
        ("vr<=1.5",       base["vr"] <= 1.5),
        ("turn20>=3e8",   base["turn20"] >= 3e8),
        ("streak==1 (info only; refuted b4)", base["streak"] == 1),
        ("streak>=3",     base["streak"] >= 3),
        ("rank<10",       base["rank"] < 10),
    ]
    results = {}
    for label, mask in filters:
        results[label] = show(label, simulate2(base[mask], fwd), bench)
    print()

    print("=== promising pairs (auto: filters whose h1 AND h2 beat base) ===")
    b = winrate(simulate2(base, fwd))
    good = [(l, m) for (l, m) in filters
            if results.get(l) and results[l]["h1"] > b["h1"]
            and results[l]["h2"] > b["h2"] and results[l]["n"] >= 150]
    print("qualifying singles: %s" % [l for l, _ in good])
    for i in range(len(good)):
        for j in range(i + 1, len(good)):
            l = good[i][0] + " & " + good[j][0]
            show(l, simulate2(base[good[i][1] & good[j][1]], fwd), bench)

    print()
    print("ADOPT ONLY IF: win, bootLo, h1 AND h2 all clear BASE and alpha > 0.")


if __name__ == "__main__":
    main()
