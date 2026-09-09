"""
Find the fingerprint of EXTREME short-term movers on the full-universe research
db, recent ~2 years. +10%/20d is noise in this regime (base ~24%); the moves
that actually matter are +30% / +50% in 20 days. Question: do the extreme
movers share cleaner common traits, and which features separate them?
ASCII only.
"""
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from config.settings import DATA_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
FWD = 20
RECENT_DAYS = 730   # ~2 years


def build():
    con = sqlite3.connect(RESEARCH_DB)
    df = pd.read_sql_query("SELECT stock_id,date,high,low,close,Volume_Lot FROM data", con)
    con.close()
    cutoff = (pd.to_datetime(df["date"].str[:10]).max() - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        n = len(g)
        if n < 90 + FWD:
            continue
        c = pd.to_numeric(g["close"], errors="coerce")
        h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce")
        v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        ma5 = c.rolling(5).mean(); ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean(); ma60 = c.rolling(60).mean()
        ret60 = c / c.shift(63) - 1
        ret20 = c / c.shift(20) - 1
        dist_ma60 = (c - ma60) / ma60
        prev = c.shift(1)
        upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = upv.rolling(10).sum() / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan)
        h52 = c.rolling(252, min_periods=63).max()
        dist52 = (h52 - c) / h52
        near60 = c / h.rolling(60).max()                     # 1.0 = at 60d high
        vol_surge = v.rolling(5).mean() / v.rolling(20).mean().shift(5)   # recent vol vs prior
        atr = ((h - l) / c).rolling(20).mean()               # volatility
        fwd_max = (c[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1) / c - 1)
        d = g["date"].astype(str).str[:10].values
        sub = pd.DataFrame({
            "date": d, "price": c.values, "ret60": ret60.values, "ret20": ret20.values,
            "dist_ma60": dist_ma60.values, "bias": bias.values, "dist52": dist52.values,
            "near60": near60.values, "vol_surge": vol_surge.values, "atr": atr.values,
            "above60": (c > ma60).astype(float).values,
            "align": ((ma5 > ma10) & (ma10 > ma20)).astype(float).values,
            "fwdmax": fwd_max.values,
        })
        sub = sub[(sub["date"] >= cutoff)].iloc[:].dropna(subset=["fwdmax", "ret60", "bias"])
        rows.append(sub)
    return pd.concat(rows, ignore_index=True), cutoff


def main():
    T, cutoff = build()
    print("recent-2y full-market stock-bars: {}  (since {})\n".format(len(T), cutoff))

    print("=== how common is each move (base rate) ===")
    for thr in [0.10, 0.20, 0.30, 0.50]:
        print("  fwd20 >= {:>3.0%}:  {:>6.2%} of bars".format(thr, (T["fwdmax"] >= thr).mean()))

    feats = ["price", "ret60", "ret20", "dist_ma60", "bias", "dist52",
             "near60", "vol_surge", "atr", "above60", "align"]
    print("\n=== fingerprint: median feature by mover tier ===")
    print("{:<11}{:>10}{:>10}{:>10}{:>10}".format("feature", "field", ">=10%", ">=30%", ">=50%"))
    field = T
    g10 = T[T["fwdmax"] >= 0.10]; g30 = T[T["fwdmax"] >= 0.30]; g50 = T[T["fwdmax"] >= 0.50]
    for f in feats:
        print("{:<11}{:>10.3f}{:>10.3f}{:>10.3f}{:>10.3f}".format(
            f, field[f].median(), g10[f].median(), g30[f].median(), g50[f].median()))

    # which feature best separates >=30% movers (lift of top quintile)
    print("\n=== single-feature lift for predicting fwd20 >= 30% (top quintile) ===")
    base30 = (T["fwdmax"] >= 0.30).mean()
    for f in feats:
        q = T[f].rank(pct=True)
        for hi in [True]:
            prec = (T.loc[q >= 0.8, "fwdmax"] >= 0.30).mean()
            print("  {:<11} top-quintile P(>=30%)={:.2%}  lift={:.2f}".format(f, prec, prec/base30))


if __name__ == "__main__":
    main()
