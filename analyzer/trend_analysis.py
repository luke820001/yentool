import pandas as pd


# ── 均線排列 ──────────────────────────────────────────────────────────────────

def calc_ma_alignment(df: pd.DataFrame) -> dict:
    close = pd.to_numeric(df["close"], errors="coerce")
    ma5   = close.rolling(5,  min_periods=5).mean()
    ma10  = close.rolling(10, min_periods=10).mean()
    ma20  = close.rolling(20, min_periods=20).mean()
    ma60  = close.rolling(60, min_periods=60).mean()

    m5, m10, m20, m60 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    cur = close.iloc[-1]

    # 均線糾結：5/10/20MA 彼此差距 < 3%，且收盤站上三線
    vals = [v for v in [m5, m10, m20] if pd.notna(v)]
    ma_squeeze = False
    if len(vals) == 3:
        spread = (max(vals) - min(vals)) / min(vals)
        ma_squeeze = bool(spread < 0.03 and pd.notna(cur) and float(cur) > min(vals))

    # 多頭排列：5MA > 10MA > 20MA > 60MA
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


# ── Donchian 通道突破（取代 W 底型態偵測）────────────────────────────────────

def calc_donchian_break(df: pd.DataFrame,
                        window: int = 40,
                        max_range_pct: float = 0.15) -> dict:
    """
    近 window 日：
      1. 高低點振幅 < max_range_pct（壓縮整理）
      2. 收盤價 ≥ window 日最高價 × 97%（逼近突破點）
    兩者同時成立 → 視為 Donchian 突破前夕。
    比 W 底型態偵測穩健，不受 K 棒形狀影響。
    """
    close = pd.to_numeric(df["close"], errors="coerce")
    high  = pd.to_numeric(df["high"],  errors="coerce")
    low   = pd.to_numeric(df["low"],   errors="coerce")

    if len(close) < window:
        return {"Donchian_Break": False}

    h_win = high.rolling(window).max().iloc[-1]
    l_win = low.rolling(window).min().iloc[-1]
    cur   = float(close.iloc[-1]) if pd.notna(close.iloc[-1]) else 0.0

    if pd.isna(h_win) or pd.isna(l_win) or float(l_win) <= 0:
        return {"Donchian_Break": False}

    h, l = float(h_win), float(l_win)
    compressed = (h - l) / l < max_range_pct   # 振幅 < 15%
    near_top   = cur >= h * 0.97               # 在最高點 97% 以上

    return {"Donchian_Break": bool(compressed and near_top)}


# ── 52 週高點位置 ─────────────────────────────────────────────────────────────

def calc_52w_position(df: pd.DataFrame, window: int = 252) -> dict:
    """
    計算收盤距 52 週最高點的百分比距離。
    距高點 ≤ 15%（Near_52W_High = True）是大波段飆股的先決條件。
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


# ── 相對強弱（RS vs 加權指數）────────────────────────────────────────────────

def calc_rs(stock_df: pd.DataFrame,
            taiex_df: pd.DataFrame = None,
            window: int = 63) -> dict:
    """
    個股近 3 個月報酬率 − 加權指數近 3 個月報酬率（單位：百分點）。
    RS_Score > 0 = 跑贏大盤；RS_Strong = 超額報酬 > 10%。
    """
    result = {"RS_Score": None, "RS_Strong": False}
    if taiex_df is None or taiex_df.empty or stock_df.empty:
        return result

    try:
        sc = pd.to_numeric(stock_df["close"], errors="coerce").dropna()
        tc = pd.to_numeric(taiex_df["close"], errors="coerce").dropna()

        if len(sc) < window or len(tc) < window:
            return result

        s_ret = float(sc.iloc[-1]) / float(sc.iloc[-window]) - 1
        t_ret = float(tc.iloc[-1]) / float(tc.iloc[-window]) - 1
        rs    = round((s_ret - t_ret) * 100, 2)

        result["RS_Score"]  = rs
        result["RS_Strong"] = rs > 10.0
    except Exception:
        pass

    return result


# ── 下降趨勢線突破（保留作詳細面板輔助）─────────────────────────────────────

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


# ── MACD ─────────────────────────────────────────────────────────────────────

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


# ── 統一入口 ──────────────────────────────────────────────────────────────────

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
