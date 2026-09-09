"""Design + validate an Explosive-Potential ('surge') score on the research db,
recent 2y, target = fwd20 max >= 30%. Compare candidates vs the current strength
score and pick the best top-quintile lift + top-group forward return."""
import sqlite3
from datetime import timedelta
import numpy as np
import pandas as pd
from config.settings import DATA_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
FWD = 20


def build():
    con = sqlite3.connect(RESEARCH_DB)
    maxd = pd.read_sql_query("SELECT MAX(date) m FROM data", con).iloc[0]["m"]
    cut = (pd.to_datetime(str(maxd)[:10]) - timedelta(days=730 + 120)).strftime("%Y-%m-%d")
    df = pd.read_sql_query("SELECT stock_id,date,high,low,close,Volume_Lot FROM data WHERE date>=?",
                           con, params=(cut,))
    con.close()
    rcut = (pd.to_datetime(str(maxd)[:10]) - timedelta(days=730)).strftime("%Y-%m-%d")
    out = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 90 + FWD:
            continue
        c = pd.to_numeric(g["close"], errors="coerce"); h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce"); v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        ma5 = c.rolling(5).mean(); ma10 = c.rolling(10).mean(); ma20 = c.rolling(20).mean(); ma60 = c.rolling(60).mean()
        ret60 = c / c.shift(63) - 1; ret20 = c / c.shift(20) - 1
        dist60 = (c - ma60) / ma60
        prev = c.shift(1); upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = upv.rolling(10).sum() / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan)
        vol_surge = v.rolling(5).mean() / v.rolling(20).mean().shift(5)
        atr = ((h - l) / c).rolling(20).mean()
        above60 = (c > ma60).astype(float); align = ((ma5 > ma10) & (ma10 > ma20)).astype(float)
        h52 = c.rolling(252, min_periods=63).max(); dist52 = (h52 - c) / h52

        # current strength (baseline)
        strn = ((ret60.clip(lower=0)/0.6).clip(upper=1)*30 + (ret20.clip(lower=0)/0.3).clip(upper=1)*20
                + above60*15 + align*15 + ((bias-0.5).clip(lower=0)/0.5).clip(upper=1)*10
                + (dist52 <= 0.15).astype(float)*10)
        # momentum sub (direction, 0-1)
        mom = ((ret60.clip(lower=0)/0.5).clip(upper=1)*0.5 + (dist60.clip(lower=0)/0.25).clip(upper=1)*0.3
               + (ret20.clip(lower=0)/0.25).clip(upper=1)*0.2)
        vola = (atr/0.06).clip(upper=1)                     # volatility 0-1
        volu = (((bias-0.5)/0.4).clip(lower=0, upper=1)*0.6 + ((vol_surge-0.8)/0.8).clip(lower=0, upper=1)*0.4)

        S1 = (mom*45 + vola*35 + volu*20) * above60          # additive, gated by uptrend
        S2 = mom * (0.4 + 0.6*vola) * 100 * above60          # momentum scaled by volatility
        S3 = (mom*0.55 + vola*0.45) * 100 * above60          # momentum+vol balanced

        fwd_max = (c[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)/c - 1)
        sub = pd.DataFrame({"date": g["date"].astype(str).str[:10].values,
                            "strn": strn.values, "S1": S1.values, "S2": S2.values,
                            "S3": S3.values, "atr": atr.values, "fwdmax": fwd_max.values})
        sub = sub[sub["date"] >= rcut].dropna(subset=["fwdmax", "S1"])
        out.append(sub)
    return pd.concat(out, ignore_index=True)


def main():
    T = build()
    base30 = (T["fwdmax"] >= 0.30).mean()
    base20 = (T["fwdmax"] >= 0.20).mean()
    print("recent-2y bars: {}  base P(>=30%)={:.2%}  P(>=20%)={:.2%}\n".format(len(T), base30, base20))
    print("{:<8}{:>12}{:>10}{:>12}{:>10}".format("score", "topQ>=30%", "lift30", "topDec>=30%", "lift30D"))
    for col in ["strn", "atr", "S1", "S2", "S3"]:
        q = T[col].rank(pct=True)
        pq = (T.loc[q >= 0.8, "fwdmax"] >= 0.30).mean()
        pd_ = (T.loc[q >= 0.9, "fwdmax"] >= 0.30).mean()
        fwdmed = T.loc[q >= 0.9, "fwdmax"].median()
        print("{:<8}{:>11.2%}{:>10.2f}{:>11.2%}{:>10.2f}   topDec fwd_med={:+.1%}".format(
            col, pq, pq/base30, pd_, pd_/base30, fwdmed))


if __name__ == "__main__":
    main()
