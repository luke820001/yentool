"""
debug_churn_persistence.py

Quantify the complaint: the daily recommendation list churns so hard it feels
like hindsight (event already happened by the time a name appears).

For each scan mode we replay its SELECTION RULE across the full-universe research
db, then report per trading day:

  pass/day  : avg number of names the mode selects that day
  persist   : Jaccard overlap of today's selected set vs yesterday's
              (1.0 = identical list day-to-day, ~0 = total churn)
  survival  : median run-length, in days, a selected name stays selected
  base25    : base rate P(forward 20d max gain >= 25%) over all stock-bars
  sel25     : same probability but only over SELECTED bars
  lift      : sel25 / base25 (predictive edge; >1 = better than random)
  ret5@sel  : median trailing 5-day return AT selection
              (HINDSIGHT indicator: high = name already ran before we flagged it)
  fwd20     : median forward 20-day return of selected bars

A good "find it early" scanner wants: high persist, high survival, lift > 1,
and LOW ret5@sel (we flag it before the move, not after).

ASCII only.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import DATA_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
FWD = 20          # forward window (trading days)
BIG = 0.25        # "explosive" threshold within the forward window
RECENT_BARS = 0   # 0 = use all history; else keep only the last N bars per stock


def _feature_frame():
    con = sqlite3.connect(RESEARCH_DB)
    df = pd.read_sql_query(
        "SELECT stock_id,date,open,high,low,close,Volume_Lot FROM data", con)
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

        ma5  = c.rolling(5).mean();  ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean(); ma60 = c.rolling(60).mean()
        prev_max20 = c.rolling(20).max().shift(1)

        vol_ma20  = v.rolling(20).mean()
        vol_ma5p  = v.rolling(5).mean().shift(1)   # prior 5d avg, excludes today
        vol_now   = v

        ret60 = c / c.shift(63) - 1
        ret20 = c / c.shift(20) - 1
        ret5  = c / c.shift(5) - 1                 # trailing 5d (hindsight gauge)

        rt = (h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()
        vol3 = v.rolling(3, min_periods=1).mean()
        dry  = vol3 / vol_ma20                      # matches Volume_Dryup_Ratio

        prev = c.shift(1)
        upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = upv.rolling(10).sum() / (upv.rolling(10).sum()
                                        + dnv.rolling(10).sum()).replace(0, np.nan)

        # intraday shape (short_explosion)
        amp   = (h - l) / l.replace(0, np.nan)
        gain  = (c - prev) / prev.replace(0, np.nan)
        nearh = (h - c) / c.replace(0, np.nan)

        cond_a = (rt < 0.08) & (dry < 0.60)

        # --- per-mode selection masks (mirror scanner/scan_mode.py) ---------
        m_squeeze = ((c < 150) & (vol_ma20 > 500) & (c > ma60)
                     & (cond_a | cond_a.rolling(5, min_periods=1).max().astype(bool)))
        m_breakout = ((vol_ma20 > 1000) & (c > prev_max20) & (vol_now > vol_ma5p * 2))
        m_bottom = ((c > ma60) & (ret60 >= 0.10)
                    & (c <= ma20 * 1.04) & (c >= ma20 * 0.96)
                    & (bias >= 0.45) & (vol_ma20 > 300))
        m_short = ((vol_ma20 > 1000) & (amp >= 0.05) & (gain >= 0.04)
                   & (nearh <= 0.015) & (vol_now > vol_ma5p * 2.5)
                   & (c > ma5) & (ma5 > ma10))
        m_leader = ((c > ma60) & (ma5 > ma10) & (ma10 > ma20)
                    & (ret60 >= 0.20) & (ret20 >= 0.05)
                    & (bias >= 0.50) & (vol_ma20 > 300))

        # forward outcome
        fwd_max = c[::-1].rolling(FWD, min_periods=1).max()[::-1].shift(-1)
        up_big  = (fwd_max / c - 1) >= BIG
        fwd20   = c.shift(-FWD) / c - 1

        sub = pd.DataFrame({
            "date": g["date"].astype(str).str[:10].values,
            "sid":  str(sid),
            "m_squeeze":  m_squeeze.values,
            "m_breakout": m_breakout.values,
            "m_bottom":   m_bottom.values,
            "m_short":    m_short.values,
            "m_leader":   m_leader.values,
            "ret5":  ret5.values,
            "fwd20": fwd20.values,
            "up_big": up_big.values,
        })
        sub = sub.iloc[64:n - FWD]
        out.append(sub)

    T = pd.concat(out, ignore_index=True)
    T = T.dropna(subset=["up_big"])
    if RECENT_BARS:
        keep = sorted(T["date"].unique())[-RECENT_BARS:]
        T = T[T["date"].isin(set(keep))]
    return T


def _persistence(T, col):
    """Avg Jaccard overlap of consecutive-day selected sets."""
    sel = T[T[col].fillna(False)]
    by_date = sel.groupby("date")["sid"].apply(set)
    dates = sorted(by_date.index)
    ov = []
    for a, b in zip(dates, dates[1:]):
        sa, sb = by_date[a], by_date[b]
        u = sa | sb
        ov.append(len(sa & sb) / len(u) if u else np.nan)
    return np.nanmean(ov) if ov else np.nan


def _survival(T, col):
    """Median run-length (consecutive selected days) across all names."""
    runs = []
    for sid, g in T.sort_values("date").groupby("sid"):
        s = g[col].fillna(False).values
        run = 0
        for x in s:
            if x:
                run += 1
            elif run:
                runs.append(run); run = 0
        if run:
            runs.append(run)
    return np.median(runs) if runs else np.nan


def _report(T):
    base = T["up_big"].mean()
    n_days = T["date"].nunique()
    print("universe stock-bars: {}  trading days: {}  base{:.0f}%={:.1%}\n".format(
        len(T), n_days, BIG * 100, base))
    hdr = ("mode            pass/day  persist  survival   sel25    lift   "
           "ret5@sel   fwd20")
    print(hdr)
    print("-" * len(hdr))
    modes = [
        ("squeeze",  "m_squeeze"),
        ("breakout", "m_breakout"),
        ("bottom",   "m_bottom"),
        ("short_exp","m_short"),
        ("leader",   "m_leader"),
    ]
    for label, col in modes:
        sel = T[T[col].fillna(False)]
        if sel.empty:
            print("{:<14}  (no selections)".format(label))
            continue
        pass_day = len(sel) / n_days
        persist  = _persistence(T, col)
        surv     = _survival(T, col)
        sel25    = sel["up_big"].mean()
        lift     = sel25 / base if base else np.nan
        ret5     = sel["ret5"].median()
        fwd      = sel["fwd20"].median()
        print("{:<14} {:>8.1f} {:>8.2f} {:>9.1f} {:>7.1%} {:>7.2f} {:>9.1%} {:>7.1%}".format(
            label, pass_day, persist, surv, sel25, lift, ret5, fwd))


def main():
    print("Building feature/selection table from research db ...")
    T = _feature_frame()
    _report(T)
    print("\nReading: high persist + high survival + low ret5@sel = early & stable.")
    print("         low persist + high ret5@sel = churny & after-the-fact.")


if __name__ == "__main__":
    main()
