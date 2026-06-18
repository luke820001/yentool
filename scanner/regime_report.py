"""
Generate a market-regime / explosion-fingerprint report from the research db
(full universe, recent ~2 years). One click -> a compact, readable file that
captures how 'explosive' moves look in the CURRENT regime, so the calculation
can be recalibrated as the market evolves (e.g. the AI-leadership regime).
Latest version only (overwritten). Report text is English/ASCII so it stays
portable and easy to read.
"""
import sqlite3
from datetime import timedelta

import numpy as np
import pandas as pd

from config.settings import DATA_DIR, TAIEX_FILE, SCAN_RESULTS_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
REPORT_FILE = SCAN_RESULTS_DIR / "regime_report.md"
FWD = 20
RECENT_DAYS = 730


def _scored_recent():
    con = sqlite3.connect(RESEARCH_DB)
    maxd = pd.read_sql_query("SELECT MAX(date) m FROM data", con).iloc[0]["m"]
    cutoff = (pd.to_datetime(str(maxd)[:10]) - timedelta(days=RECENT_DAYS + 120)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(
        "SELECT stock_id,date,high,low,close,Volume_Lot FROM data WHERE date >= ?",
        con, params=(cutoff,))
    con.close()
    recent_cut = (pd.to_datetime(str(maxd)[:10]) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 90 + FWD:
            continue
        c = pd.to_numeric(g["close"], errors="coerce"); h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce"); v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        ma5 = c.rolling(5).mean(); ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean(); ma60 = c.rolling(60).mean()
        ret60 = c / c.shift(63) - 1; ret20 = c / c.shift(20) - 1
        dist_ma60 = (c - ma60) / ma60
        prev = c.shift(1); upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = upv.rolling(10).sum() / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan)
        h52 = c.rolling(252, min_periods=63).max(); dist52 = (h52 - c) / h52
        near60 = c / h.rolling(60).max()
        vol_surge = v.rolling(5).mean() / v.rolling(20).mean().shift(5)
        atr = ((h - l) / c).rolling(20).mean()
        above60 = (c > ma60).astype(float)
        align = ((ma5 > ma10) & (ma10 > ma20)).astype(float)
        strn = ((ret60.clip(lower=0) / 0.6).clip(upper=1) * 30
                + (ret20.clip(lower=0) / 0.3).clip(upper=1) * 20
                + above60 * 15 + align * 15
                + ((bias - 0.5).clip(lower=0) / 0.5).clip(upper=1) * 10
                + (dist52 <= 0.15).astype(float) * 10)
        rt = (h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()
        dry = v / v.rolling(20).mean()
        expl = (((0.20 - rt.clip(upper=0.20)) / 0.20 * 35).clip(lower=0)
                + (1.0 - dry.clip(upper=1.0)) * 35
                + (bias.clip(lower=0.5, upper=1.0) - 0.5) / 0.5 * 30)
        fwd_max = (c[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1) / c - 1)
        sub = pd.DataFrame({
            "date": g["date"].astype(str).str[:10].values, "price": c.values,
            "ret60": ret60.values, "ret20": ret20.values, "dist_ma60": dist_ma60.values,
            "bias": bias.values, "dist52": dist52.values, "near60": near60.values,
            "vol_surge": vol_surge.values, "atr": atr.values, "above60": above60.values,
            "align": align.values, "strn": strn.values, "expl": expl.values,
            "fwdmax": fwd_max.values,
        })
        sub = sub[sub["date"] >= recent_cut].dropna(subset=["fwdmax", "ret60", "bias"])
        rows.append(sub)
    return pd.concat(rows, ignore_index=True), recent_cut, str(maxd)[:10]


def _market_line():
    try:
        con = sqlite3.connect(TAIEX_FILE)
        try:
            t = pd.read_sql_query("SELECT * FROM TAIEX", con)
        except Exception:
            t = pd.read_sql_query("SELECT * FROM data", con)
        con.close()
        c = pd.to_numeric(t.sort_values("date")["close"], errors="coerce").dropna()
        c2 = c.tail(504)
        ret = c2.iloc[-1] / c2.iloc[0] - 1
        dd = (c2 / c2.cummax() - 1).min()
        trend = "ABOVE" if c.iloc[-1] > c.tail(60).mean() else "BELOW"
        return "TAIEX recent-2y return {:+.0%}, max drawdown {:.0%}, now {} its 60-day MA".format(ret, dd, trend)
    except Exception:
        return "TAIEX data unavailable"


def generate_regime_report(refresh=True, progress=None):
    def say(m):
        if progress:
            progress(m)

    if refresh:
        say("Updating research database (filling gaps)...")
        try:
            from build_research_db import build
            build()
        except Exception as e:
            say("refresh skipped: {}".format(str(e)[:60]))

    say("Analyzing recent-2y regime...")
    T, recent_cut, asof = _scored_recent()
    base = {thr: (T["fwdmax"] >= thr).mean() for thr in [0.10, 0.20, 0.30, 0.50]}

    feats = ["price", "ret60", "ret20", "dist_ma60", "bias", "dist52",
             "near60", "vol_surge", "atr", "above60", "align"]
    field = T
    g30 = T[T["fwdmax"] >= 0.30]; g50 = T[T["fwdmax"] >= 0.50]
    base30 = base[0.30]

    def topq(col):
        s = T[col]
        if s.dropna().nunique() <= 2:          # binary feature -> use the "1" group
            mask = s >= s.max()
        else:
            mask = s.rank(pct=True) >= 0.8
        return (T.loc[mask, "fwdmax"] >= 0.30).mean()

    score_topq = topq

    lines = []
    lines.append("# Market-Regime / Explosion-Fingerprint Report")
    lines.append("")
    lines.append("- as-of: {}   window: {} .. {}   stock-bars: {}".format(
        asof, recent_cut, asof, len(T)))
    lines.append("- market: {}".format(_market_line()))
    lines.append("- definition: 'explosion' = max forward {}-day return reaching the threshold".format(FWD))
    lines.append("")
    lines.append("## 1. How rare is each move now (base rate)")
    for thr in [0.10, 0.20, 0.30, 0.50]:
        lines.append("- fwd{}d >= {:>3.0%}: {:.2%} of bars".format(FWD, thr, base[thr]))
    lines.append("")
    lines.append("## 2. Fingerprint -- median feature by mover tier")
    lines.append("| feature | field | >=30% | >=50% |")
    lines.append("|---|---|---|---|")
    for f in feats:
        lines.append("| {} | {:.3f} | {:.3f} | {:.3f} |".format(
            f, field[f].median(), g30[f].median(), g50[f].median()))
    lines.append("")
    lines.append("## 3. Single-feature power to predict a >=30% move (top quintile)")
    lines.append("| feature | P(>=30%) | lift |")
    lines.append("|---|---|---|")
    rankrows = []
    for f in feats:
        p = topq(f)
        rankrows.append((f, p, p / base30 if base30 else 0))
    for f, p, lift in sorted(rankrows, key=lambda x: -x[2]):
        lines.append("| {} | {:.2%} | {:.2f} |".format(f, p, lift))
    lines.append("")
    lines.append("## 4. Strength score vs Explosion score (top quintile, target >=30%)")
    for col, lab in [("strn", "Strength"), ("expl", "Explosion")]:
        p = score_topq(col)
        lines.append("- {}: top-quintile P(>=30%) = {:.2%}  (lift {:.2f})".format(
            lab, p, p / base30 if base30 else 0))
    lines.append("")
    lines.append("## Reading guide")
    lines.append("- Features with lift >> 1 are what current explosive movers share -- weight these.")
    lines.append("- Features with lift ~1 carry no signal now -- drop them.")
    lines.append("- If Explosion lift < 1 it is inverted (anti-predictive); rank by Strength instead.")

    SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    say("Report written: {}".format(REPORT_FILE))
    return str(REPORT_FILE)


if __name__ == "__main__":
    print(generate_regime_report(refresh=False, progress=print))
