"""
Careful full-database audit of price_volume.db (+ taiex.db).

Sections:
  1. INVENTORY & DATA QUALITY  - coverage, gaps, dupes, bad bars, extreme jumps
  2. UNIVERSE BIAS             - how selected/skewed the stock set is
  3. PERIOD / MARKET           - how bullish the window was (drawdown, up-days)
  4. STRENGTH-SCORE ROBUSTNESS - re-test the key finding on INDEPENDENT samples
                                 (non-overlapping) and CROSS-SECTIONALLY (rank
                                 stocks on each date), since earlier tests used
                                 overlapping bars (serially correlated, inflated N).
All strings ASCII.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE, TAIEX_FILE

FWD = 20


def load_pv():
    con = sqlite3.connect(PRICE_VOLUME_FILE)
    df = pd.read_sql_query("SELECT * FROM data", con)
    con.close()
    for c in ["open", "high", "low", "close", "Volume_Lot"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["d"] = pd.to_datetime(df["date"].astype(str).str[:10], errors="coerce")
    return df


# ---------- 1. INVENTORY & QUALITY ----------
def section_quality(df):
    print("=" * 70)
    print("1. INVENTORY & DATA QUALITY")
    print("=" * 70)
    print("rows: {}   stocks: {}   date span: {} -> {}".format(
        len(df), df["stock_id"].nunique(), df["d"].min().date(), df["d"].max().date()))
    dup = df.duplicated(subset=["stock_id", "date"]).sum()
    print("duplicate (stock_id,date) rows:", int(dup))

    bars = df.groupby("stock_id").size()
    print("bars/stock: min={} p10={:.0f} median={:.0f} max={}".format(
        bars.min(), bars.quantile(0.1), bars.median(), bars.max()))
    print("stocks with <120 bars (short history):", int((bars < 120).sum()))

    # staleness spread (latest bar per stock)
    last = df.groupby("stock_id")["d"].max()
    print("latest-bar dates: most-recent={}  oldest-latest={}".format(
        last.max().date(), last.min().date()))
    stale = (last < last.max()).sum()
    print("stocks whose latest bar < global latest:", int(stale))

    bad = df[(df["close"] <= 0) | (df["high"] < df["low"]) |
             (df["close"].isna()) | (df["Volume_Lot"].isna())]
    print("bad bars (close<=0 / high<low / NaN):", len(bad))
    zerovol = int((df["Volume_Lot"] == 0).sum())
    print("zero-volume bars:", zerovol)

    # calendar gaps and extreme jumps, per stock
    big_gap_stocks = 0
    extreme_bars = 0
    extreme_stocks = set()
    for sid, g in df.sort_values("d").groupby("stock_id"):
        gg = g.dropna(subset=["d", "close"])
        if len(gg) < 5:
            continue
        gap = gg["d"].diff().dt.days
        if (gap > 7).sum() > 0:
            big_gap_stocks += 1
        ret = gg["close"].pct_change().abs()
        ex = (ret > 0.11).sum()   # beyond TW +/-10% limit -> adjustment/data issue
        if ex > 0:
            extreme_bars += int(ex); extreme_stocks.add(sid)
    print("stocks with a >7-day calendar gap (suspension/missing):", big_gap_stocks)
    print("bars with >11% close-to-close move (limit is 10% -> adj/anomaly): {} across {} stocks".format(
        extreme_bars, len(extreme_stocks)))


# ---------- 2. UNIVERSE BIAS ----------
def section_universe(df):
    print("\n" + "=" * 70)
    print("2. UNIVERSE BIAS (is the stock set cherry-picked / skewed?)")
    print("=" * 70)
    rets = {}
    for sid, g in df.sort_values("d").groupby("stock_id"):
        c = g["close"].dropna()
        if len(c) >= 120:
            rets[sid] = c.iloc[-1] / c.iloc[0] - 1
    r = pd.Series(rets)
    print("full-period return per stock (n={}):".format(len(r)))
    print("  median={:+.1%}  mean={:+.1%}  p10={:+.1%}  p90={:+.1%}".format(
        r.median(), r.mean(), r.quantile(0.1), r.quantile(0.9)))
    print("  stocks UP over period: {:.0%}   doubled(>+100%): {:.0%}   halved(<-50%): {:.0%}".format(
        (r > 0).mean(), (r > 1.0).mean(), (r < -0.5).mean()))
    last_close = df.sort_values("d").groupby("stock_id")["close"].last()
    print("latest close: median={:.1f}  <50: {:.0%}  >500: {:.0%}".format(
        last_close.median(), (last_close < 50).mean(), (last_close > 500).mean()))


# ---------- 3. PERIOD / MARKET ----------
def section_period():
    print("\n" + "=" * 70)
    print("3. PERIOD / MARKET REGIME (how bullish was the window?)")
    print("=" * 70)
    con = sqlite3.connect(TAIEX_FILE)
    try:
        t = pd.read_sql_query("SELECT * FROM TAIEX", con)
    except Exception:
        t = pd.read_sql_query("SELECT * FROM data", con)
    con.close()
    t["close"] = pd.to_numeric(t["close"], errors="coerce")
    t = t.dropna().sort_values("date").reset_index(drop=True)
    c = t["close"]
    total = c.iloc[-1] / c.iloc[0] - 1
    updays = (c.pct_change() > 0).mean()
    dd = (c / c.cummax() - 1).min()
    print("TAIEX bars: {}  total return: {:+.1%}  up-days: {:.0%}  max drawdown: {:.1%}".format(
        len(t), total, updays, dd))
    print("  -> A window with shallow drawdown and mostly-up days CANNOT test")
    print("     bear-market behaviour; downside/regime conclusions are untestable.")


# ---------- 4. STRENGTH-SCORE ROBUSTNESS ----------
def build_scored(df):
    rows = []
    for sid, g in df.sort_values("d").groupby("stock_id"):
        g = g.reset_index(drop=True)
        n = len(g)
        if n < 64 + FWD + 2:
            continue
        c = g["close"]; h = g["high"]; l = g["low"]; v = g["Volume_Lot"]
        cN = c.to_numpy(float)
        ma5 = c.rolling(5).mean().to_numpy(float); ma10 = c.rolling(10).mean().to_numpy(float)
        ma20 = c.rolling(20).mean().to_numpy(float); ma60 = c.rolling(60).mean().to_numpy(float)
        rt = ((h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()).to_numpy(float)
        dry = (v / v.rolling(20).mean()).to_numpy(float)
        prev = c.shift(1); upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = (upv.rolling(10).sum() / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan)).to_numpy(float)
        h52 = c.rolling(252, min_periods=63).max().to_numpy(float)
        dates = g["date"].astype(str).str[:10].to_numpy()
        for i in range(64, n - FWD):
            if cN[i] <= 0 or np.isnan(ma60[i]) or np.isnan(cN[i-63]) or np.isnan(bias[i]):
                continue
            ret60 = cN[i]/cN[i-63]-1; ret20 = cN[i]/cN[i-20]-1
            dist52 = (h52[i]-cN[i])/h52[i] if h52[i] > 0 else 1.0
            expl = (max(0.0, (0.20-min(rt[i], 0.20))/0.20*35) + (1.0-min(dry[i], 1.0))*35
                    + (min(max(bias[i], 0.5), 1.0)-0.5)/0.5*30)
            strn = (min(max(ret60, 0)/0.6, 1)*30 + min(max(ret20, 0)/0.3, 1)*20
                    + (15 if cN[i] > ma60[i] else 0) + (15 if ma5[i] > ma10[i] > ma20[i] else 0)
                    + min(max(bias[i]-0.5, 0)/0.5, 1)*10 + (10 if dist52 <= 0.15 else 0))
            up10 = (np.nanmax(cN[i+1:i+1+FWD])/cN[i]-1) >= 0.10
            fwd = cN[min(i+FWD, n-1)]/cN[i]-1
            rows.append((sid, dates[i], i, expl, strn, up10, fwd))
    return pd.DataFrame(rows, columns=["sid", "date", "bar", "expl", "strn", "up10", "fwd"])


def section_strength(df):
    print("\n" + "=" * 70)
    print("4. STRENGTH-SCORE ROBUSTNESS (independent + cross-sectional)")
    print("=" * 70)
    T = build_scored(df)
    base = T["up10"].mean()
    print("all bars: n={} (OVERLAPPING -> not independent)  base up10={:.1%}".format(len(T), base))

    # (a) non-overlapping: one bar every FWD per stock -> quasi-independent
    ind = T[T["bar"] % FWD == 0]
    print("\n(a) INDEPENDENT sample (1 bar / {} per stock): n={}".format(FWD, len(ind)))
    for col, lab in [("expl", "Explosion"), ("strn", "Strength")]:
        ind2 = ind.copy()
        ind2["dec"] = pd.qcut(ind2[col], 5, labels=False, duplicates="drop")
        gp = ind2.groupby("dec")["up10"].mean()
        print("  {:<10} quintile up10: {}   top/bot lift: {:.2f} / {:.2f}".format(
            lab, "  ".join("{:.0%}".format(x) for x in gp.values),
            gp.iloc[-1]/base, gp.iloc[0]/base))

    # (b) cross-sectional: each DATE, rank stocks; does top beat bottom that day?
    print("\n(b) CROSS-SECTIONAL (rank stocks on each date, need >=20/day):")
    for col, lab in [("expl", "Explosion"), ("strn", "Strength")]:
        tops, bots, corrs, ndays = [], [], [], 0
        for dt, gd in T.groupby("date"):
            if len(gd) < 20:
                continue
            ndays += 1
            q = gd[col].rank(pct=True)
            tops.append(gd.loc[q >= 0.8, "up10"].mean())
            bots.append(gd.loc[q <= 0.2, "up10"].mean())
            corrs.append(gd[col].rank().corr(gd["fwd"].rank()))  # Pearson-on-ranks = Spearman
        print("  {:<10} days={}  top-quintile up10={:.1%}  bottom={:.1%}  spread={:+.1%}  mean rankcorr={:+.3f}".format(
            lab, ndays, np.nanmean(tops), np.nanmean(bots),
            np.nanmean(tops)-np.nanmean(bots), np.nanmean(corrs)))


def main():
    df = load_pv()
    section_quality(df)
    section_universe(df)
    section_period()
    section_strength(df)


if __name__ == "__main__":
    main()
