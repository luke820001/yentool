"""Diagnose Surge_Score distribution / saturation and test a recalibration that
spreads the score without losing the >=30% lift. Research db, recent 2y."""
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
    cut = (pd.to_datetime(str(maxd)[:10]) - timedelta(days=850)).strftime("%Y-%m-%d")
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
        vsurge = v.rolling(5).mean() / v.rolling(20).mean().shift(5)
        atr = ((h - l) / c).rolling(20).mean()
        above60 = (c > ma60).astype(float)

        def cl(x): return x.clip(lower=0, upper=1)
        # CURRENT (saturating) calibration
        momA = cl(ret60/0.5)*0.5 + cl(dist60/0.25)*0.3 + cl(ret20/0.25)*0.2
        volaA = cl(atr/0.06); voluA = cl((bias-0.5)/0.4)*0.6 + cl((vsurge-0.8)/0.8)*0.4
        cur = (momA*45 + volaA*35 + voluA*20) * above60
        # RECAL (wider denominators -> spreads at the top)
        momB = cl(ret60/1.0)*0.5 + cl(dist60/0.45)*0.3 + cl(ret20/0.45)*0.2
        volaB = cl(atr/0.11); voluB = cl((bias-0.5)/0.45)*0.6 + cl((vsurge-0.8)/1.4)*0.4
        rec = (momB*45 + volaB*35 + voluB*20) * above60

        fwd_max = (c[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)/c - 1)
        mlq = (above60.astype(bool)) & (ret60 >= 0.20) & (ret20 >= 0.05) & (bias >= 0.50)
        sub = pd.DataFrame({"date": g["date"].astype(str).str[:10].values,
                            "cur": cur.values, "rec": rec.values,
                            "qual": mlq.fillna(False).values, "fwdmax": fwd_max.values})
        out.append(sub[sub["date"] >= rcut].dropna(subset=["fwdmax", "cur"]))
    return pd.concat(out, ignore_index=True)


def pct(s):
    return "p50={:.0f} p75={:.0f} p90={:.0f} p99={:.0f} max={:.0f}".format(
        s.quantile(.5), s.quantile(.75), s.quantile(.9), s.quantile(.99), s.max())


def lift(T, col):
    base = (T["fwdmax"] >= 0.30).mean()
    q = T[col].rank(pct=True)
    return (T.loc[q >= 0.9, "fwdmax"] >= 0.30).mean() / base


def main():
    T = build()
    Q = T[T["qual"]]
    print("all bars: {}   qualifying(momentum_leader-like): {}\n".format(len(T), len(Q)))
    print("CURRENT score   all: {}".format(pct(T["cur"])))
    print("CURRENT score  qual: {}   (<- shown in scans; saturated?)".format(pct(Q["cur"])))
    print("  qual fraction >=90: {:.0%}   >=80: {:.0%}".format((Q["cur"] >= 90).mean(), (Q["cur"] >= 80).mean()))
    print()
    print("RECAL   score   all: {}".format(pct(T["rec"])))
    print("RECAL   score  qual: {}".format(pct(Q["rec"])))
    print("  qual fraction >=90: {:.0%}   >=80: {:.0%}".format((Q["rec"] >= 90).mean(), (Q["rec"] >= 80).mean()))
    print()
    print("top-decile lift for >=30%:  CURRENT {:.2f}   RECAL {:.2f}".format(lift(T, "cur"), lift(T, "rec")))


if __name__ == "__main__":
    main()
