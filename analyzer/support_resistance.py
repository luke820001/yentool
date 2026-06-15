import pandas as pd


def _to_float(val) -> float | None:
    try:
        v = float(val)
        return round(v, 2) if not pd.isna(v) else None
    except Exception:
        return None


def calc_moving_averages(df: pd.DataFrame) -> dict:
    close = pd.to_numeric(df["close"], errors="coerce")
    result = {}
    for w in [20, 60]:
        ma = close.rolling(window=w, min_periods=1).mean()
        result["MA{}".format(w)] = _to_float(ma.iloc[-1]) if len(ma) > 0 else None
    return result


def calc_horizontal_sr(df: pd.DataFrame) -> dict:
    tail60 = df.tail(60)
    high = pd.to_numeric(
        tail60.get("high", pd.Series(dtype=float)), errors="coerce"
    )
    # Resistance = prior 59 bars only (exclude today).
    # When today's close > prior high → already broken out → Res_Gap_Pct < 0.
    prior_high = high.iloc[:-1] if len(high) > 1 else high

    # Support = 20-day low (more relevant to current price structure).
    low20 = pd.to_numeric(
        df.tail(20).get("low", pd.Series(dtype=float)), errors="coerce"
    )
    return {
        "Resist_60H":  _to_float(prior_high.max()),
        "Support_60L": _to_float(low20.min()),
    }


def calc_volume_profile(df: pd.DataFrame, top_n: int = 3) -> dict:
    # Use only the most recent 60 bars so VP reflects current price structure,
    # not a historical range that may be far from today's price.
    recent = df.tail(60)
    close = pd.to_numeric(recent["close"], errors="coerce")
    vol   = pd.to_numeric(recent["Volume_Lot"], errors="coerce")

    # Derive bucket_size from the recent price range (not all-time min).
    cur_close = close.iloc[-1] if len(close) > 0 else float("nan")
    if pd.isna(cur_close) or cur_close <= 0:
        return {"VP_Zone{}".format(i): None for i in range(1, top_n + 1)}

    # 1% of current price per bucket, floored at 0.5
    bucket_size = max(round(cur_close * 0.01, 1), 0.5)
    buckets = ((close / bucket_size).round() * bucket_size).round(2)

    vp = pd.DataFrame({"price": buckets, "vol": vol}).dropna()
    grouped = vp.groupby("price")["vol"].sum().sort_values(ascending=False)

    result = {}
    for i, price in enumerate(grouped.head(top_n).index, 1):
        result["VP_Zone{}".format(i)] = _to_float(price)
    for i in range(len(grouped.head(top_n)) + 1, top_n + 1):
        result["VP_Zone{}".format(i)] = None
    return result


def calc_gaps(df: pd.DataFrame) -> dict:
    df = df.copy().sort_values("date").reset_index(drop=True)
    for col in ["open", "close", "high", "low"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    up_gap_support = None
    dn_gap_resist = None

    for i in range(1, len(df)):
        prev_high = df.loc[i - 1, "high"]
        prev_low = df.loc[i - 1, "low"]
        curr_low = df.loc[i, "low"]
        curr_high = df.loc[i, "high"]

        if pd.isna(prev_high) or pd.isna(prev_low) or pd.isna(curr_low) or pd.isna(curr_high):
            continue

        if curr_low > prev_high:
            up_gap_support = round(float(prev_high), 2)

        if curr_high < prev_low:
            dn_gap_resist = round(float(prev_low), 2)

    return {
        "Gap_Up_Sup": up_gap_support,
        "Gap_Dn_Res": dn_gap_resist,
    }


def calc_round_level(close: float) -> float:
    if close < 10:
        step = 1.0
    elif close < 50:
        step = 5.0
    elif close < 100:
        step = 10.0
    elif close < 500:
        step = 50.0
    else:
        step = 100.0
    return round(round(close / step) * step, 2)


def calc_squeeze_score(close: float, support: float, resist: float) -> dict:
    result = {
        "Sup_Gap_Pct": None,
        "Res_Gap_Pct": None,
        "Squeeze": False,
    }
    if close <= 0 or support <= 0 or resist <= 0:
        return result
    if resist <= support:
        return result

    sup_gap = round((close - support) / support * 100, 2)
    res_gap = round((resist - close) / close * 100, 2)
    result["Sup_Gap_Pct"] = sup_gap
    result["Res_Gap_Pct"] = res_gap
    # Squeeze only makes sense when price is still BELOW resistance (res_gap > 0).
    # Negative res_gap means price has already broken above the reference high.
    result["Squeeze"] = (sup_gap < 5.0) and (0 < res_gap < 2.0)
    return result


def calc_all(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    result = {}
    result.update(calc_moving_averages(df))
    result.update(calc_horizontal_sr(df))
    result.update(calc_volume_profile(df, top_n=3))
    result.update(calc_gaps(df))
    close_val = pd.to_numeric(df["close"], errors="coerce").iloc[-1]
    if pd.notna(close_val):
        result["Round_Level"] = calc_round_level(float(close_val))
    else:
        result["Round_Level"] = None

    close = close_val if pd.notna(close_val) else 0.0
    support = result.get("Support_60L") or 0.0
    resist = result.get("Resist_60H") or 0.0
    result.update(calc_squeeze_score(float(close), float(support), float(resist)))
    return result
