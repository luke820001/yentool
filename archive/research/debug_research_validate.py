"""
Validate on the FULL-universe, multi-regime research db, split BY SUB-PERIOD to
test the AI-regime hypothesis: is the strength-score edge regime-amplified
(much stronger in the recent AI-concentration years than in earlier rotation
years)? Also reports the REAL base rates on an unbiased universe. ASCII only.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import DATA_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
FWD = 20


def scored_table():
    con = sqlite3.connect(RESEARCH_DB)
    df = pd.read_sql_query("SELECT stock_id,date,open,high,low,close,Volume_Lot FROM data", con)
    con.close()
    out = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        n = len(g)
        if n < 64 + FWD + 5:
            continue
        c = pd.to_numeric(g["close"], errors="coerce")
        h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce")
        v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        ma5 = c.rolling(5).mean(); ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean(); ma60 = c.rolling(60).mean()
        ret60 = c / c.shift(63) - 1
        ret20 = c / c.shift(20) - 1
        rt = (h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()
        dry = v / v.rolling(20).mean()
        prev = c.shift(1)
        upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = upv.rolling(10).sum() / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan)
        h52 = c.rolling(252, min_periods=63).max()
        dist52 = (h52 - c) / h52

        expl = (((0.20 - rt.clip(upper=0.20)) / 0.20 * 35).clip(lower=0)
                + (1.0 - dry.clip(upper=1.0)) * 35
                + (bias.clip(lower=0.5, upper=1.0) - 0.5) / 0.5 * 30)
        strn = ((ret60.clip(lower=0) / 0.6).clip(upper=1) * 30
                + (ret20.clip(lower=0) / 0.3).clip(upper=1) * 20
                + (c > ma60).astype(float) * 15
                + ((ma5 > ma10) & (ma10 > ma20)).astype(float) * 15
                + ((bias - 0.5).clip(lower=0) / 0.5).clip(upper=1) * 10
                + (dist52 <= 0.15).astype(float) * 10)

        fwd_max = c[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)
        up10 = (fwd_max / c - 1) >= 0.10
        fwd = c.shift(-20) / c - 1

        sub = pd.DataFrame({
            "date": g["date"].astype(str).str[:10].values,
            "expl": expl.values, "strn": strn.values,
            "up10": up10.values, "fwd": fwd.values,
        })
        sub = sub.iloc[64:n - FWD]
        out.append(sub.dropna(subset=["expl", "strn", "fwd"]))
    T = pd.concat(out, ignore_index=True)
    T["year"] = T["date"].str[:4]
    return T


def period_report(T, label):
    base = T["up10"].mean()
    def topq(col):
        q = T[col].rank(pct=True)
        return T.loc[q >= 0.8, "up10"].mean()
    et, st = topq("expl"), topq("strn")
    print("  {:<12} n={:>7}  base_up10={:.1%}  fwd_med={:+.1%} | "
          "Expl_topQ={:.1%}(lift{:.2f})  Strn_topQ={:.1%}(lift{:.2f})".format(
              label, len(T), base, T["fwd"].median(),
              et, et / base, st, st / base))


def main():
    print("Building scored table from research db ...")
    T = scored_table()
    print("total scored stock-bars: {}  stocks: covered  span {}..{}\n".format(
        len(T), T["date"].min(), T["date"].max()))

    print("=== REAL base rate + strength edge, BY YEAR ===")
    for yr in sorted(T["year"].unique()):
        sub = T[T["year"] == yr]
        if len(sub) > 500:
            period_report(sub, yr)

    print("\n=== Recent ~2y (AI regime) vs earlier ===")
    yrs = sorted(T["year"].unique())
    recent = yrs[-2:]
    period_report(T[T["year"].isin(recent)], "recent2y")
    period_report(T[~T["year"].isin(recent)], "earlier")
    period_report(T, "ALL")


if __name__ == "__main__":
    main()
