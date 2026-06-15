import pandas as pd

_INF = float("inf")


def _safe_num(df, col, fill):
    """Return numeric Series for col; fill missing values with fill."""
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").fillna(fill)
    return pd.Series([fill] * len(df), index=df.index, dtype=float)


def _safe_bool(df, col):
    """Return boolean Series for col; default False if missing."""
    if col in df.columns:
        return df[col].astype(bool)
    return pd.Series([False] * len(df), index=df.index)


def apply_scan_mode(df, selected_mode):
    """
    Post-verification filter: keep only rows matching the selected scan mode.

    Parameters
    ----------
    df : pd.DataFrame
        Output of verify_candidates (result DataFrame with all signal columns).
    selected_mode : str
        One of 'mode_squeeze', 'mode_breakout', 'mode_bottom', 'mode_short_explosion'.
        Unknown values return the full DataFrame unchanged.

    Returns
    -------
    pd.DataFrame
        Filtered and reset-index copy.
    """
    if df is None or df.empty:
        return df

    close    = _safe_num(df, "Close_Price",      0.0)
    ma60     = _safe_num(df, "MA60",             _INF)
    vol_ma20 = _safe_num(df, "Vol_MA20",         0.0)
    vol_ma5  = _safe_num(df, "Vol_MA5",          _INF)
    vol_now  = _safe_num(df, "Vol_Today",         0.0)
    max20p   = _safe_num(df, "Max_Price_20_Prev", _INF)

    cond_a    = _safe_bool(df, "Cond_A")
    cond_a_5d = _safe_bool(df, "Cond_A_5D")
    cond_b    = _safe_bool(df, "Cond_B")
    hist_turn = _safe_bool(df, "MACD_Hist_Turn")

    if selected_mode == "mode_squeeze":
        # Classic Squeeze: low-price stock above 60MA with box contraction
        # base:    close < 150,  20d avg volume > 500 lots
        # trend:   close > MA60
        # trigger: Cond_A today OR Cond_A triggered within last 5 days
        mask = (
            (close < 150) &
            (vol_ma20 > 500) &
            (close > ma60) &
            (cond_a | cond_a_5d)
        )
        return df[mask].reset_index(drop=True)

    if selected_mode == "mode_breakout":
        # Momentum Breakout: high liquidity, price breaks 20d high with volume surge
        # base:    20d avg volume > 1000 lots
        # trigger: today close > prev-20d-high  AND  today volume > 5d avg * 2
        mask = (
            (vol_ma20 > 1000) &
            (close > max20p) &
            (vol_now > vol_ma5 * 2)
        )
        return df[mask].reset_index(drop=True)

    if selected_mode == "mode_bottom":
        # Bottom Accumulation: price below 60MA, MACD hist turned positive, big-holder buying
        # base:    close < MA60
        # trigger: MACD histogram negative-to-positive in last 3 days  AND  Cond_B
        mask = (
            (close < ma60) &
            hist_turn &
            cond_b
        )
        return df[mask].reset_index(drop=True)

    if selected_mode == "mode_short_explosion":
        # Short-Term Explosion: extreme intraday strength + volume fire, 5-day trend aligned
        # Requires High_Today, Low_Today, Close_Prev; any None => row is excluded via NaN propagation.
        high       = _safe_num(df, "High_Today",  _INF)  # _INF makes ratio checks fail safely
        low        = _safe_num(df, "Low_Today",   0.0)   # 0 makes amplitude ratio = _INF -> ok
        close_prev = _safe_num(df, "Close_Prev",  0.0)   # 0 makes pct-change ratio = _INF -> ok
        ma5        = _safe_num(df, "MA5",         _INF)
        ma10       = _safe_num(df, "MA10",        _INF)

        # NaN guard: if any required price column is missing, exclude the row
        have_prices = (
            df["High_Today"].notna()  if "High_Today"  in df.columns else pd.Series(False, index=df.index)
        ) & (
            df["Low_Today"].notna()   if "Low_Today"   in df.columns else pd.Series(False, index=df.index)
        ) & (
            df["Close_Prev"].notna()  if "Close_Prev"  in df.columns else pd.Series(False, index=df.index)
        ) & (
            df["MA5"].notna()         if "MA5"         in df.columns else pd.Series(False, index=df.index)
        ) & (
            df["MA10"].notna()        if "MA10"        in df.columns else pd.Series(False, index=df.index)
        )

        # 1. base liquidity: 20d avg volume > 1000 lots
        liq = vol_ma20 > 1000

        # 2. intraday amplitude >= 5%
        amplitude = (high - low) / low.replace(0, float("nan"))
        wide_bar = amplitude >= 0.05

        # 3a. daily gain >= 4% vs previous close
        gain = (close - close_prev) / close_prev.replace(0, float("nan"))
        strong_up = gain >= 0.04

        # 3b. close within 1.5% of today's high (closed near ceiling)
        near_high = (high - close) / close.replace(0, float("nan")) <= 0.015

        # 4. volume surge: today vol > 5d avg * 2.5
        vol_surge = vol_now > vol_ma5 * 2.5

        # 5. fast trend: close > MA5 > MA10
        fast_trend = (close > ma5) & (ma5 > ma10)

        mask = have_prices & liq & wide_bar & strong_up & near_high & vol_surge & fast_trend
        return df[mask].reset_index(drop=True)

    # Unknown mode: return unfiltered
    return df
