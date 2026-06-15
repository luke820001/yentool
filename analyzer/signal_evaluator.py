import pandas as pd
from storage.data_store import load_sheet, save_sheet
from analyzer.base_signal import BaseSignal, SignalResult
from config.settings import (
    PRICE_VOLUME_FILE,
    SIGNAL_LOG_FILE,
    BREAKOUT_VOLUME_MULTIPLIER,
    WATCHLIST,
)

RECENT_DAYS = 5
DAILY_SIGNALS_SHEET = "signals"
VOLUME_BIAS_WINDOW = 10
CONSOLIDATION_WINDOW = 20
MA20_VOLUME_WINDOW = 20


def _load_stock_data(stock_id: str) -> pd.DataFrame:
    pv = load_sheet(PRICE_VOLUME_FILE, stock_id)
    if pv.empty:
        return pd.DataFrame()
    pv["date"] = pd.to_datetime(pv["date"], errors="coerce")
    pv = pv.sort_values("date").reset_index(drop=True)
    return pv


def _calc_volume_bias(df: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(df["close"], errors="coerce")
    vol = pd.to_numeric(df["Volume_Lot"], errors="coerce")
    prev_close = close.shift(1)
    up_vol = vol.where(close > prev_close, 0.0)
    dn_vol = vol.where(close < prev_close, 0.0)
    roll_up = up_vol.rolling(window=VOLUME_BIAS_WINDOW, min_periods=VOLUME_BIAS_WINDOW).sum()
    roll_dn = dn_vol.rolling(window=VOLUME_BIAS_WINDOW, min_periods=VOLUME_BIAS_WINDOW).sum()
    total = roll_up + roll_dn
    return (roll_up / total.replace(0, float("nan"))).round(4)


def _calc_range_tightness(df: pd.DataFrame) -> pd.Series:
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    roll_high = high.rolling(window=CONSOLIDATION_WINDOW, min_periods=CONSOLIDATION_WINDOW).max()
    roll_low = low.rolling(window=CONSOLIDATION_WINDOW, min_periods=CONSOLIDATION_WINDOW).min()
    tightness = (roll_high - roll_low) / roll_low.replace(0, float("nan"))
    return tightness.round(4)


def _calc_volume_dryup(df: pd.DataFrame) -> pd.Series:
    vol = pd.to_numeric(df["Volume_Lot"], errors="coerce")
    ma20 = vol.rolling(window=MA20_VOLUME_WINDOW, min_periods=MA20_VOLUME_WINDOW).mean()
    ratio = vol / ma20.replace(0, float("nan"))
    return ratio.round(4)


def _calc_explosion_score(df: pd.DataFrame) -> pd.Series:
    bias = _calc_volume_bias(df)
    tightness = _calc_range_tightness(df)
    dryup = _calc_volume_dryup(df)

    # Range tightness score (0-35): tighter box = higher score
    # tightness < 0.03 -> 35, tightness > 0.20 -> 0
    tightness_score = (0.20 - tightness.clip(upper=0.20)) / 0.20 * 35

    # Volume dry-up score (0-35): dryup < 0.3 -> 35, dryup > 1.0 -> 0
    dryup_score = (1.0 - dryup.clip(upper=1.0)) * 35

    # Volume bias score (0-30): bias > 0.6 -> scales to 30
    bias_score = (bias.clip(lower=0.5, upper=1.0) - 0.5) / 0.5 * 30

    total = (tightness_score + dryup_score + bias_score).round(1)
    return total.clip(lower=0, upper=100)


def _evaluate_conditions(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in ["close", "high", "low", "Min_Price_20", "Max_Price_20",
                "Volume_Lot", "Min_Volume_20", "MA5_Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Volume_Bias"] = _calc_volume_bias(df)
    df["Range_Tightness"] = _calc_range_tightness(df)
    df["Volume_Dryup_Ratio"] = _calc_volume_dryup(df)
    df["Explosion_Score"] = _calc_explosion_score(df)

    # Cond_A: tight box consolidation + volume drying up
    has_cols = all(c in df.columns for c in ["Range_Tightness", "Volume_Dryup_Ratio"])
    if has_cols:
        cond_a = (
            (df["Range_Tightness"] < 0.08) &
            (df["Volume_Dryup_Ratio"] < 0.60)
        )
    else:
        cond_a = pd.Series([False] * len(df), index=df.index)

    # Cond_B: bypassed (paid data required)
    cond_b = pd.Series([False] * len(df), index=df.index)

    # Cond_C: accumulation bias - up-day volume dominates
    cond_c = df["Volume_Bias"] >= 0.60

    df["Cond_A"] = cond_a
    df["Cond_B"] = cond_b
    df["Cond_C"] = cond_c
    df["Is_Golden_Signal"] = cond_a & cond_c

    # Cond_D: breakout with volume surge
    has_breakout_cols = all(c in df.columns for c in
                            ["close", "Max_Price_20", "Volume_Lot", "MA5_Volume"])
    if has_breakout_cols:
        prev_max = df["Max_Price_20"].shift(1)
        df["Is_Breakout_Signal"] = (
            (df["close"] > prev_max) &
            (df["Volume_Lot"] > df["MA5_Volume"] * BREAKOUT_VOLUME_MULTIPLIER)
        )
    else:
        df["Is_Breakout_Signal"] = False

    return df


class SignalEvaluator(BaseSignal):

    def evaluate(self, stock_id: str) -> SignalResult:
        df = _load_stock_data(stock_id)
        if df.empty:
            return SignalResult(
                stock_id=stock_id,
                triggered_golden=False,
                triggered_breakout=False,
            )
        df = _evaluate_conditions(df)
        df["stock_id"] = stock_id

        cutoff = df["date"].max() - pd.Timedelta(days=RECENT_DAYS)
        recent = df[df["date"] >= cutoff]

        triggered_golden = bool(recent["Is_Golden_Signal"].any())
        triggered_breakout = bool(recent["Is_Breakout_Signal"].any())

        signal_rows = recent[
            recent["Is_Golden_Signal"] | recent["Is_Breakout_Signal"]
        ].copy()

        return SignalResult(
            stock_id=stock_id,
            triggered_golden=triggered_golden,
            triggered_breakout=triggered_breakout,
            signal_rows=signal_rows,
        )


def run_all_evaluations(watchlist: list = None) -> pd.DataFrame:
    if watchlist is None:
        watchlist = WATCHLIST

    evaluator = SignalEvaluator()
    all_signals = []

    for stock_id in watchlist:
        result = evaluator.evaluate(stock_id)
        if not result.signal_rows.empty:
            all_signals.append(result.signal_rows)

    if not all_signals:
        return pd.DataFrame()

    combined = pd.concat(all_signals, ignore_index=True)
    combined["date"] = combined["date"].dt.strftime("%Y-%m-%d")

    output_cols = [
        "date", "stock_id", "close", "Volume_Lot", "MA5_Volume",
        "Range_Tightness", "Volume_Dryup_Ratio", "Volume_Bias",
        "Explosion_Score", "Cond_A", "Cond_C",
        "Is_Golden_Signal", "Is_Breakout_Signal",
    ]
    output_cols = [c for c in output_cols if c in combined.columns]
    combined = combined[output_cols].sort_values(
        ["Explosion_Score", "date"], ascending=[False, False]
    ).reset_index(drop=True)

    save_sheet(combined, SIGNAL_LOG_FILE, DAILY_SIGNALS_SHEET)
    return combined
