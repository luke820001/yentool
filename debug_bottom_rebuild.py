"""Validate a rebuilt 'bottom' mode = STRONG PULLBACK (buy an uptrend stock that
dipped to its 20MA) vs the old falling-knife logic, on the research db recent 2y.
Target = fwd20 max >= 20%. ASCII only."""
import sqlite3
from datetime import timedelta
import numpy as np
import pandas as pd
from config.settings import DATA_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
FWD = 20


def main():
    con = sqlite3.connect(RESEARCH_DB)
    maxd = pd.read_sql_query("SELECT MAX(date) m FROM data", con).iloc[0]["m"]
    cut = (pd.to_datetime(str(maxd)[:10]) - timedelta(days=850)).strftime("%Y-%m-%d")
    df = pd.read_sql_query("SELECT stock_id,date,high,low,close,Volume_Lot FROM data WHERE date>=?",
                           con, params=(cut,))
    con.close()
    rcut = (pd.to_datetime(str(maxd)[:10]) - timedelta(days=730)).strftime("%Y-%m-%d")
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 90 + FWD:
            continue
        c = pd.to_numeric(g["close"], errors="coerce"); h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce"); v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        ma10 = c.rolling(10).mean(); ma20 = c.rolling(20).mean(); ma60 = c.rolling(60).mean()
        ret60 = c / c.shift(63) - 1
        e12 = c.ewm(span=12, adjust=False).mean(); e26 = c.ewm(span=26, adjust=False).mean()
        hist = (e12 - e26) - (e12 - e26).ewm(span=9, adjust=False).mean()
        hturn = (hist > 0) & (hist.shift(1) <= 0)
        prev = c.shift(1); upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = upv.rolling(10).sum() / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan)
        vma20 = v.rolling(20).mean()
        # OLD bottom: below 60ma + macd hist turn + warm volume
        old = (c < ma60) & hturn.rolling(3).max().fillna(0).astype(bool) & (v > v.rolling(5).mean() * 1.5)
        # NEW bottom: strong pullback -> uptrend stock dipped to ~20MA, holding
        new = ((c > ma60) & (ret60 >= 0.10) & (c <= ma20 * 1.04) & (c >= ma20 * 0.96)
               & (c > ma60) & (bias >= 0.45) & (vma20 > 300))
        fwd_max = (c[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1) / c - 1)
        sub = pd.DataFrame({"date": g["date"].astype(str).str[:10].values,
                            "old": old.fillna(False).values, "new": new.fillna(False).values,
                            "fwdmax": fwd_max.values})
        rows.append(sub[sub["date"] >= rcut].dropna(subset=["fwdmax"]))
    T = pd.concat(rows, ignore_index=True)
    base = (T["fwdmax"] >= 0.20).mean()
    print("recent-2y bars {}  base P(>=20%)={:.2%}\n".format(len(T), base))
    for col, lab in [("old", "OLD bottom (falling knife)"), ("new", "NEW bottom (strong pullback)")]:
        m = T[col]
        n = int(m.sum())
        if n < 50:
            print("  {:<30} fires={} (too few)".format(lab, n)); continue
        p = (T.loc[m, "fwdmax"] >= 0.20).mean()
        print("  {:<30} fires={:>6} ({:.1f}%)  P(>=20%)={:.2%}  lift={:.2f}".format(
            lab, n, 100*n/len(T), p, p/base))


if __name__ == "__main__":
    main()
