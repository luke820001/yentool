"""
Backtest whether institutional flow (foreign net buy/sell) improves accuracy.
Joins the backfilled daily inst data (inst_trades.db, TSE) with forward returns
and the surge score (research_prices.db). Answers:
  (a) does foreign 5-day net predict a >=30%/20d move on its own?
  (b) does requiring "foreign not selling" raise the surge score's precision?
  (c) does the 3236 pattern (extended + foreign selling) underperform?
ASCII only.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import DATA_DIR

INST_DB = DATA_DIR / "inst_trades.db"
RESEARCH_DB = DATA_DIR / "research_prices.db"
FWD = 20


def price_features():
    con = sqlite3.connect(RESEARCH_DB)
    df = pd.read_sql_query("SELECT stock_id,date,high,low,close,Volume_Lot FROM data", con)
    con.close()
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
        mom = cl(ret60/1.0)*0.5 + cl(dist60/0.45)*0.3 + cl(ret20/0.45)*0.2
        vola = cl(atr/0.11); volu = cl((bias-0.5)/0.45)*0.6 + cl((vsurge-0.8)/1.4)*0.4
        surge = (mom*45 + vola*35 + volu*20) * above60
        vma20 = v.rolling(20).mean()
        fwd_max = (c[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)/c - 1)
        out.append(pd.DataFrame({"stock_id": str(sid), "date": g["date"].astype(str).str[:10].values,
                                 "surge": surge.values, "vma20": vma20.values,
                                 "gain3m": ret60.values, "fwdmax": fwd_max.values}))
    return pd.concat(out, ignore_index=True)


def main():
    con = sqlite3.connect(INST_DB)
    inst = pd.read_sql_query("SELECT stock_id,date,Foreign_Net FROM data", con)
    con.close()
    inst["stock_id"] = inst["stock_id"].astype(str)
    inst["date"] = inst["date"].astype(str).str[:10]
    inst["Foreign_Net"] = pd.to_numeric(inst["Foreign_Net"], errors="coerce").fillna(0.0)
    inst = inst.sort_values("date")
    inst["For5D"] = inst.groupby("stock_id")["Foreign_Net"].transform(
        lambda s: s.rolling(5, min_periods=1).sum())
    print("inst rows:", len(inst), " dates:", inst["date"].nunique(),
          inst["date"].min(), "->", inst["date"].max())

    pf = price_features()
    T = pf.merge(inst, on=["stock_id", "date"], how="inner").dropna(subset=["fwdmax", "surge"])
    print("merged stock-days:", len(T), "\n")
    base = (T["fwdmax"] >= 0.30).mean()
    print("base P(>=30%) = {:.2%}\n".format(base))

    def lift(mask, label):
        n = int(mask.sum())
        if n < 100:
            print("  {:<34} n={} (too few)".format(label, n)); return
        p = (T.loc[mask, "fwdmax"] >= 0.30).mean()
        print("  {:<34} n={:>6}  P(>=30%)={:.2%}  lift={:.2f}".format(label, n, p, p/base))

    print("(a) foreign-flow alone:")
    q = T["For5D"].rank(pct=True)
    lift(q >= 0.8, "foreign 5d-net top quintile")
    lift(T["For5D"] > 0, "foreign 5d-net > 0 (buying)")
    lift(T["For5D"] < 0, "foreign 5d-net < 0 (selling)")

    print("\n(b) does foreign flow improve the surge score?")
    sq = T["surge"].rank(pct=True)
    lift(sq >= 0.8, "surge top quintile (baseline)")
    lift((sq >= 0.8) & (T["For5D"] > 0), "surge topQ AND foreign buying")
    lift((sq >= 0.8) & (T["For5D"] <= 0), "surge topQ BUT foreign selling")

    print("\n(c) the 3236 pattern (already extended + foreign selling):")
    ext = T["gain3m"] >= 0.50
    lift(ext, "extended (3m gain>=50%)")
    lift(ext & (T["For5D"] > 0), "extended AND foreign buying")
    lift(ext & (T["For5D"] < 0), "extended AND foreign selling")

    print("\n(d) combined score = surge + bounded foreign-flow adjustment:")
    ratio = T["For5D"] / (5.0 * T["vma20"].replace(0, np.nan))
    for w in [6, 10, 14]:
        adj = (ratio / 0.10).clip(-1, 1).fillna(0) * w
        T["surge_adj"] = (T["surge"] + adj).clip(0, 100)
        sq2 = T["surge_adj"].rank(pct=True)
        p = (T.loc[sq2 >= 0.8, "fwdmax"] >= 0.30).mean()
        print("  surge + flow(+/-{:>2}pts) topQ:  P(>=30%)={:.2%}  lift={:.2f}".format(w, p, p/base))
    sq0 = T["surge"].rank(pct=True)
    p0 = (T.loc[sq0 >= 0.8, "fwdmax"] >= 0.30).mean()
    print("  surge alone topQ (reference):   P(>=30%)={:.2%}  lift={:.2f}".format(p0, p0/base))


if __name__ == "__main__":
    main()
