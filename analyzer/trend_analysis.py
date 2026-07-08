import numpy as np
import pandas as pd

# A "52-week high" needs roughly a full year of bars. With fewer, a 3-month high
# would be mislabeled as a 52w high, overstating Near_52W_High / proximity for
# young listings. Matches MIN_HISTORY_BARS (=240) used elsewhere.
_MIN_52W_BARS = 240


def _clip01(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.0


# --- Explosive-potential ('surge') score -------------------------------------

def calc_surge_score(df: pd.DataFrame) -> dict:
    """
    Forward-looking explosive-potential score (0-100): momentum x volatility x
    volume, gated by trend. Derived empirically on the full-universe research db
    (recent 2y): top-decile lift ~3.3 for catching a >=30%/20-day move, vs the
    old Explosion_Score which is anti-predictive. Components by validated power:
    ATR(volatility) and 3-/1-month momentum dominate; up-volume bias is minor;
    distance-to-52w-high and box tightness carry NO signal and are excluded.
    """
    out = {"Surge_Score": None, "ATR_Pct": None}
    c = pd.to_numeric(df.get("close", pd.Series(dtype=float)), errors="coerce")
    h = pd.to_numeric(df.get("high", pd.Series(dtype=float)), errors="coerce")
    l = pd.to_numeric(df.get("low", pd.Series(dtype=float)), errors="coerce")
    if "Volume_Lot" not in df.columns or len(c) < 64:
        return out
    v = pd.to_numeric(df["Volume_Lot"], errors="coerce")

    cur = float(c.iloc[-1])
    if cur <= 0:
        return out
    ma60 = c.rolling(60, min_periods=60).mean().iloc[-1]
    atr = ((h - l) / c).rolling(20).mean().iloc[-1]
    out["ATR_Pct"] = round(float(atr) * 100, 2) if pd.notna(atr) else None

    ret60 = cur / float(c.iloc[-64]) - 1 if float(c.iloc[-64]) > 0 else 0.0
    ret20 = cur / float(c.iloc[-21]) - 1 if (len(c) >= 21 and float(c.iloc[-21]) > 0) else 0.0
    dist60 = (cur - float(ma60)) / float(ma60) if (pd.notna(ma60) and ma60 > 0) else 0.0
    above60 = 1.0 if (pd.notna(ma60) and cur > float(ma60)) else 0.0

    prev = c.shift(1)
    upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
    tot = upv.rolling(10).sum() + dnv.rolling(10).sum()
    bias = (upv.rolling(10).sum() / tot.replace(0, np.nan)).iloc[-1]
    vsurge = (v.rolling(5).mean() / v.rolling(20).mean().shift(5)).iloc[-1]

    # Normalization denominators are set so the score SPREADS across qualifying
    # momentum candidates instead of saturating near 100. With tighter caps the
    # whole filtered list scored 80-100 (uninformative); these wider caps put the
    # candidate median near ~50 and p90 near ~73, while keeping the same >=30%
    # top-decile lift (~3.3). See debug_surge_dist.py.
    mom = (_clip01(ret60 / 1.0) * 0.5 + _clip01(dist60 / 0.45) * 0.3
           + _clip01(ret20 / 0.45) * 0.2)
    vola = _clip01(atr / 0.11) if pd.notna(atr) else 0.0
    volu = 0.0
    if pd.notna(bias):
        volu += _clip01((bias - 0.5) / 0.45) * 0.6
    if pd.notna(vsurge):
        volu += _clip01((vsurge - 0.8) / 1.4) * 0.4

    surge = (mom * 45 + vola * 35 + volu * 20) * above60
    out["Surge_Score"] = round(float(surge), 1)
    return out


# --- Early-launch ('pre-launch') score ---------------------------------------

def calc_launch_score(df: pd.DataFrame) -> dict:
    """
    EARLY-launch score (0-100): flags a name BEFORE its explosive run, the
    opposite of Surge_Score (which peaks once a stock is already running).

    Validated on the full-universe research db (debug_early_design.py, 1431
    trading days, 2.6M bars): top ~5% by this score carries lift ~2.26 for a
    >=25%/20-day move, while the median trailing-5-day return AT selection is
    only +0.3% (vs +6.8% for the momentum-leader gate) -- i.e. we flag the
    setup, not the climax. Paired with day-to-day hysteresis it holds a name
    ~11 days (persistence 0.89), which is what kills the list churn.

    Components (weights): 3-month momentum 0.30, "not-yet-run" 5-day freshness
    0.25, proximity to 52w high 0.20, up-volume accumulation 0.15, base
    tightness 0.10 -- gated by (close > 60MA) and a liquidity floor. Adding
    pivot-proximity or volume-expansion terms raised lift marginally but pulled
    selection back toward the climax (ret5 -> 4%), so they are intentionally out.
    """
    out = {"Launch_Score": None, "Ret_5D_Pct": None}
    c = pd.to_numeric(df.get("close", pd.Series(dtype=float)), errors="coerce")
    if "Volume_Lot" not in df.columns or len(c) < 64:
        return out
    v = pd.to_numeric(df["Volume_Lot"], errors="coerce")

    cur = float(c.iloc[-1])
    if cur <= 0:
        return out
    ma60   = c.rolling(60, min_periods=60).mean().iloc[-1]
    vol20  = v.rolling(20, min_periods=20).mean().iloc[-1]
    ret60  = cur / float(c.iloc[-64]) - 1 if float(c.iloc[-64]) > 0 else 0.0
    ret5   = cur / float(c.iloc[-6]) - 1 if (len(c) >= 6 and float(c.iloc[-6]) > 0) else 0.0
    out["Ret_5D_Pct"] = round(ret5 * 100, 1)

    h52 = c.rolling(252, min_periods=_MIN_52W_BARS).max().iloc[-1]
    dist52 = (float(h52) - cur) / float(h52) if (pd.notna(h52) and h52 > 0) else 1.0

    prev = c.shift(1)
    upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
    tot = upv.rolling(10).sum() + dnv.rolling(10).sum()
    bias = (upv.rolling(10).sum() / tot.replace(0, np.nan)).iloc[-1]
    bias = float(bias) if pd.notna(bias) else 0.5

    h = pd.to_numeric(df.get("high", pd.Series(dtype=float)), errors="coerce")
    l = pd.to_numeric(df.get("low",  pd.Series(dtype=float)), errors="coerce")
    rh = h.rolling(20).max().iloc[-1]; rl = l.rolling(20).min().iloc[-1]
    rt = (float(rh) - float(rl)) / float(rl) if (pd.notna(rl) and rl > 0) else 1.0

    # Liquidity gate measured in turnover VALUE (20d avg lots * price * 1000
    # >= 1e8 TWD), not share count: a 300-lot floor structurally excluded
    # high-priced stocks (7769 traded ~700 lots but ~15e8 TWD/day and was
    # invisible through a 9x run). A/B replay on research_prices.db (2025-09..
    # 2026-06, ~7.5k trades) showed the value basis raises prelaunch win rate
    # in every cell -- see CHANGELOG 2026-07-06. Keep in sync with the
    # prefilter (scanner/market_filter.py) and the replay (eval_realtrade.py).
    turn20 = (float(vol20) * cur * 1000.0) if pd.notna(vol20) else 0.0
    gate = 1.0 if (pd.notna(ma60) and cur > float(ma60)
                   and turn20 >= 1.0e8) else 0.0

    mom   = _clip01(max(ret60, 0.0) / 0.5)
    young = 1.0 - _clip01(max(ret5, 0.0) / 0.12)
    near  = 1.0 - _clip01(max(dist52, 0.0) / 0.30)
    acc   = _clip01(max(bias - 0.5, 0.0) / 0.45)
    tight = 1.0 - _clip01(max(rt, 0.0) / 0.25)

    score = (mom * 0.30 + young * 0.25 + near * 0.20
             + acc * 0.15 + tight * 0.10) * gate * 100
    out["Launch_Score"] = round(float(score), 1)
    return out


# --- Moving-average alignment ------------------------------------------------

def calc_ma_alignment(df: pd.DataFrame) -> dict:
    close = pd.to_numeric(df["close"], errors="coerce")
    ma5   = close.rolling(5,  min_periods=5).mean()
    ma10  = close.rolling(10, min_periods=10).mean()
    ma20  = close.rolling(20, min_periods=20).mean()
    ma60  = close.rolling(60, min_periods=60).mean()

    m5, m10, m20, m60 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    cur = close.iloc[-1]

    # MA convergence: 5/10/20MA within 3% of each other, close above all three
    vals = [v for v in [m5, m10, m20] if pd.notna(v)]
    ma_squeeze = False
    if len(vals) == 3:
        spread = (max(vals) - min(vals)) / min(vals)
        ma_squeeze = bool(spread < 0.03 and pd.notna(cur) and float(cur) > min(vals))

    # Bullish alignment: 5MA > 10MA > 20MA > 60MA
    ma_bull = bool(
        pd.notna(m5) and pd.notna(m10) and pd.notna(m20) and pd.notna(m60)
        and float(m5) > float(m10) > float(m20) > float(m60)
    )

    return {
        "MA5":           round(float(m5),  2) if pd.notna(m5)  else None,
        "MA10":          round(float(m10), 2) if pd.notna(m10) else None,
        "MA_Squeeze":    ma_squeeze,
        "MA_Bull_Align": ma_bull,
    }


# --- Donchian channel breakout (replaces W-bottom pattern detection) ---------

def calc_donchian_break(df: pd.DataFrame,
                        window: int = 40,
                        max_range_pct: float = 0.15) -> dict:
    """
    Prior window-1 bars (excluding today):
      1. High-low range < max_range_pct  (compressed consolidation)
      2. Today's close >= that prior high * 97%  (approaching / breaking out)
    Excluding today prevents today's own high from trivially satisfying near_top.
    """
    close = pd.to_numeric(df["close"], errors="coerce")
    high  = pd.to_numeric(df["high"],  errors="coerce")
    low   = pd.to_numeric(df["low"],   errors="coerce")

    # Need at least window+1 bars so we can exclude today and still have 'window' bars
    if len(close) < window + 1:
        return {"Donchian_Break": False}

    # Exclude today (iloc[:-1]) before computing the rolling window
    prior_high = high.iloc[:-1]
    prior_low  = low.iloc[:-1]

    h_win = prior_high.rolling(window).max().iloc[-1]
    l_win = prior_low.rolling(window).min().iloc[-1]
    cur   = float(close.iloc[-1]) if pd.notna(close.iloc[-1]) else 0.0

    if pd.isna(h_win) or pd.isna(l_win) or float(l_win) <= 0:
        return {"Donchian_Break": False}

    h, l = float(h_win), float(l_win)
    compressed = (h - l) / l < max_range_pct   # range < 15%
    near_top   = cur >= h * 0.97               # close >= 97% of prior 40-day high

    return {"Donchian_Break": bool(compressed and near_top)}


# --- 52-week-high position ---------------------------------------------------

def calc_52w_position(df: pd.DataFrame, window: int = 252) -> dict:
    """
    Percentage distance of the close below the 52-week high.
    Within 15% (Near_52W_High = True) is a precondition for a major-run stock.
    """
    close = pd.to_numeric(df["close"], errors="coerce")
    min_p = min(_MIN_52W_BARS, window)

    h52 = close.rolling(window, min_periods=min_p).max().iloc[-1]
    cur = float(close.iloc[-1]) if pd.notna(close.iloc[-1]) else 0.0

    if pd.isna(h52) or float(h52) <= 0:
        return {"Dist_52W_High_Pct": None, "Near_52W_High": False}

    dist_pct = round((float(h52) - cur) / float(h52) * 100, 1)
    return {
        "Dist_52W_High_Pct": dist_pct,
        "Near_52W_High":     dist_pct <= 15.0,
    }


# --- Relative strength (RS vs. the TAIEX index) ------------------------------

def calc_rs(stock_df: pd.DataFrame,
            taiex_df: pd.DataFrame = None,
            window: int = 63) -> dict:
    """
    Stock ~3-month return minus TAIEX ~3-month return (in percentage points).
    RS_Score > 0 = outperforming the market; RS_Strong = excess return > 10%.

    Stock and index are aligned on common trading dates first; otherwise the
    63-bar lookback would land on different calendar days when the two series
    have different coverage, distorting the comparison.
    """
    result = {"RS_Score": None, "RS_Strong": False}
    if taiex_df is None or taiex_df.empty or stock_df.empty:
        return result

    try:
        s = stock_df[["date", "close"]].copy()
        t = taiex_df[["date", "close"]].copy()
        s["date"] = pd.to_datetime(s["date"], errors="coerce")
        t["date"] = pd.to_datetime(t["date"], errors="coerce")
        s["close"] = pd.to_numeric(s["close"], errors="coerce")
        t["close"] = pd.to_numeric(t["close"], errors="coerce")

        merged = (
            s.dropna()
            .merge(t.dropna(), on="date", suffixes=("_s", "_t"))
            .sort_values("date")
            .reset_index(drop=True)
        )
        # window bars back -> need window+1 aligned rows
        if len(merged) < window + 1:
            return result

        sc = merged["close_s"]
        tc = merged["close_t"]
        s_ret = float(sc.iloc[-1]) / float(sc.iloc[-window - 1]) - 1
        t_ret = float(tc.iloc[-1]) / float(tc.iloc[-window - 1]) - 1
        rs    = round((s_ret - t_ret) * 100, 2)

        result["RS_Score"]  = rs
        result["RS_Strong"] = rs > 10.0
    except Exception:
        pass

    return result


# --- Descending trendline breakout (kept as a detail-panel helper) -----------

def calc_trend_breakout(df: pd.DataFrame) -> dict:
    df = df.copy().sort_values("date").reset_index(drop=True)
    high  = pd.to_numeric(df["high"],        errors="coerce")
    close = pd.to_numeric(df["close"],       errors="coerce")
    vol   = pd.to_numeric(df["Volume_Lot"], errors="coerce")

    result = {"Trend_Breakout": False}
    n = min(60, len(df))
    if n < 20:
        return result

    t_h = high.iloc[-n:].reset_index(drop=True)
    t_c = close.iloc[-n:].reset_index(drop=True)
    t_v = vol.iloc[-n:].reset_index(drop=True)

    swings = [
        (i, float(t_h[i]))
        for i in range(2, len(t_h) - 2)
        if (pd.notna(t_h[i]) and
            float(t_h[i]) > float(t_h[i-1]) and float(t_h[i]) > float(t_h[i-2]) and
            float(t_h[i]) > float(t_h[i+1]) and float(t_h[i]) > float(t_h[i+2]))
    ]

    if len(swings) >= 2:
        (x1, y1), (x2, y2) = swings[-2], swings[-1]
        if x2 > x1 and y2 < y1:
            projected = y1 + (y2 - y1) / (x2 - x1) * (len(t_h) - 1 - x1)
            cur       = float(t_c.iloc[-1]) if pd.notna(t_c.iloc[-1]) else 0.0
            ma5v      = t_v.rolling(5).mean().iloc[-1]
            vol_now   = float(t_v.iloc[-1]) if pd.notna(t_v.iloc[-1]) else 0.0
            vol_surge = pd.notna(ma5v) and float(ma5v) > 0 and vol_now > float(ma5v) * 1.5
            result["Trend_Breakout"] = bool(cur > projected and vol_surge)

    return result


# --- MACD ---------------------------------------------------------------------

def calc_macd(df: pd.DataFrame) -> dict:
    close = pd.to_numeric(df["close"], errors="coerce").dropna().reset_index(drop=True)
    result = {"MACD_Cross": False, "MACD_Hist_Turn": False}
    if len(close) < 35:
        return result

    ema12  = close.ewm(span=12, adjust=False).mean()
    ema26  = close.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist   = macd - signal

    for i in range(-3, 0):
        if float(macd.iloc[i]) > float(signal.iloc[i]) and float(macd.iloc[i-1]) <= float(signal.iloc[i-1]):
            result["MACD_Cross"] = True
            break

    for i in range(-3, 0):
        if float(hist.iloc[i]) > 0 and float(hist.iloc[i-1]) <= 0:
            result["MACD_Hist_Turn"] = True
            break

    return result


# --- Unified entry point ------------------------------------------------------

def calc_trend_analysis(df: pd.DataFrame,
                        taiex_df: pd.DataFrame = None) -> dict:
    empty = {
        "MA5": None, "MA10": None,
        "MA_Squeeze": False, "MA_Bull_Align": False,
        "Donchian_Break": False,
        "Trend_Breakout": False,
        "MACD_Cross": False, "MACD_Hist_Turn": False,
        "RS_Score": None, "RS_Strong": False,
        "Dist_52W_High_Pct": None, "Near_52W_High": False,
    }
    if df.empty:
        return empty
    try:
        out = {}
        out.update(calc_ma_alignment(df))
        out.update(calc_donchian_break(df))
        out.update(calc_52w_position(df))
        out.update(calc_trend_breakout(df))
        out.update(calc_macd(df))
        out.update(calc_rs(df, taiex_df))
        return out
    except Exception:
        return empty
