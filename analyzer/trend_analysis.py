import pandas as pd


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
    min_p = max(60, window // 4)

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
