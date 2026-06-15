import time
from datetime import date
import pandas as pd
from ingestion.price_volume import PriceVolumeFetcher
from ingestion.price_volume_multi import multi_fetch_and_save
from ingestion.large_holder import LargeHolderFetcher
from ingestion.market_index import MarketIndexFetcher
from analyzer.signal_evaluator import _load_stock_data, _evaluate_conditions
from analyzer.support_resistance import calc_all
from analyzer.trend_analysis import calc_trend_analysis
from storage.data_store import load_sheet
from config.settings import LARGE_HOLDER_FILE, PRICE_VOLUME_FILE

INTER_STOCK_SLEEP = 0

# re-fetch price only when the latest cached row is older than this many days
PRICE_CACHE_DAYS = 1   # 1 = re-fetch only if we don't have today's (or yesterday's) bar
CHIP_CACHE_DAYS  = 8   # chip is weekly; 8 days covers one full cycle


def _cache_age_days(file_path, stock_id):
    """
    Return how many calendar days have elapsed since the most recent cached row.
    Returns None when there is no cached data at all.
    """
    df = load_sheet(file_path, stock_id)
    if df.empty or "date" not in df.columns:
        return None
    latest = pd.to_datetime(df["date"], errors="coerce").max()
    if pd.isna(latest):
        return None
    return (date.today() - latest.date()).days


def _is_cache_fresh(file_path, stock_id, max_age_days):
    age = _cache_age_days(file_path, stock_id)
    return age is not None and age <= max_age_days


# session-level flag: once FinMind returns 402 for chip data, skip all
# remaining chip fetches in this scan run (data is weekly; one miss is fine)
_chip_quota_exhausted = False


def _fetch_chip_safe(lh_fetcher, stock_id):
    """Fetch chip data; set session flag on first 402 and skip all subsequent calls."""
    global _chip_quota_exhausted
    if _chip_quota_exhausted:
        return
    try:
        lh_fetcher.fetch_and_save(stock_id)
    except Exception as e:
        msg = str(e)
        print("  [{}] chip error: {}".format(stock_id, msg))
        if "402" in msg:
            _chip_quota_exhausted = True
            print("  [WARN] chip quota exhausted — skipping chip fetch for rest of scan")


def _get_volume_stats(merged: pd.DataFrame) -> dict:
    """Extract scan-mode filter columns from the price-volume DataFrame."""
    defaults = {
        "Vol_MA20":          None,
        "Vol_MA5":           None,
        "Vol_Today":         None,
        "Max_Price_20_Prev": None,
        "High_Today":        None,
        "Low_Today":         None,
        "Close_Prev":        None,
    }
    if merged.empty or "Volume_Lot" not in merged.columns:
        return defaults

    vol = pd.to_numeric(merged["Volume_Lot"], errors="coerce")
    vol_ma20 = vol.rolling(20, min_periods=20).mean()
    result = dict(defaults)

    last_ma20 = vol_ma20.iloc[-1]
    result["Vol_MA20"] = round(float(last_ma20), 0) if pd.notna(last_ma20) else None

    if "MA5_Volume" in merged.columns:
        ma5v = pd.to_numeric(merged["MA5_Volume"], errors="coerce").iloc[-1]
        result["Vol_MA5"] = round(float(ma5v), 0) if pd.notna(ma5v) else None

    last_vol = vol.iloc[-1]
    result["Vol_Today"] = int(last_vol) if pd.notna(last_vol) else None

    if "Max_Price_20" in merged.columns and len(merged) >= 2:
        mp20_prev = pd.to_numeric(merged["Max_Price_20"], errors="coerce").iloc[-2]
        result["Max_Price_20_Prev"] = round(float(mp20_prev), 2) if pd.notna(mp20_prev) else None

    if "high" in merged.columns:
        h = pd.to_numeric(merged["high"], errors="coerce").iloc[-1]
        result["High_Today"] = round(float(h), 2) if pd.notna(h) else None

    if "low" in merged.columns:
        l = pd.to_numeric(merged["low"], errors="coerce").iloc[-1]
        result["Low_Today"] = round(float(l), 2) if pd.notna(l) else None

    if "close" in merged.columns and len(merged) >= 2:
        prev_c = pd.to_numeric(merged["close"], errors="coerce").iloc[-2]
        result["Close_Prev"] = round(float(prev_c), 2) if pd.notna(prev_c) else None

    return result


def _get_chip_data(stock_id: str) -> dict:
    """讀取最新一週集保資料，回傳 Cond_B 相關欄位。"""
    df = load_sheet(LARGE_HOLDER_FILE, stock_id)
    defaults = {
        "Cond_B":           False,
        "Cond_B_Available": False,
        "Large_Holder_Pct": None,
        "Large_Pct_Change": None,
        "Retail_Pct":       None,
        "Retail_Pct_Change": None,
    }
    if df.empty or "Cond_B" not in df.columns:
        return defaults

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    # 需要至少兩週才能算出週差值
    if len(df) < 2:
        return defaults

    latest = df.iloc[-1]

    def _safe_float(key):
        v = latest.get(key)
        return round(float(v), 4) if pd.notna(v) else None

    return {
        "Cond_B":            bool(str(latest.get("Cond_B", "False")).strip()
                                  in ("True", "1", "true")),
        "Cond_B_Available":  True,
        "Large_Holder_Pct":  _safe_float("Large_Holder_Pct"),
        "Large_Pct_Change":  _safe_float("Large_Pct_Change"),
        "Retail_Pct":        _safe_float("Retail_Pct"),
        "Retail_Pct_Change": _safe_float("Retail_Pct_Change"),
    }


def verify_candidates(
    candidates: pd.DataFrame,
    progress_callback=None,
) -> pd.DataFrame:
    global _chip_quota_exhausted
    _chip_quota_exhausted = False   # reset at the start of every scan run
    lh_fetcher = LargeHolderFetcher()

    # TAIEX 資料在迴圈外只拉一次
    taiex_df = MarketIndexFetcher().get()
    if taiex_df.empty:
        print("  [WARN] TAIEX data unavailable — RS calculation skipped.")

    total   = len(candidates)
    results = []

    for rank, (_, row) in enumerate(candidates.iterrows(), start=1):
        stock_id   = str(row["stock_id"])
        stock_name = str(row.get("stock_name", ""))
        market     = str(row.get("market", "TSE"))

        if progress_callback:
            progress_callback(rank, total, "Scanning {} ({}/{})".format(stock_id, rank, total))

        # 1. price-volume
        pv_age = _cache_age_days(PRICE_VOLUME_FILE, stock_id)
        if pv_age is None:
            # no history at all: fetch full rolling window
            try:
                multi_fetch_and_save(stock_id, market=market, incremental=False)
            except Exception as e:
                print("  [{}] pv error: {}".format(stock_id, e))
        elif pv_age > PRICE_CACHE_DAYS:
            # have history but missing recent days: top-up only
            try:
                multi_fetch_and_save(stock_id, market=market, incremental=True)
            except Exception as e:
                print("  [{}] pv error: {}".format(stock_id, e))
        else:
            print("  [{}] pv cache hit".format(stock_id))

        # 2. chip: skip API when local cache is fresh or quota is exhausted
        if not _is_cache_fresh(LARGE_HOLDER_FILE, stock_id, CHIP_CACHE_DAYS):
            _fetch_chip_safe(lh_fetcher, stock_id)
        else:
            print("  [{}] chip cache hit".format(stock_id))

        merged = _load_stock_data(stock_id)
        if merged.empty:
            if rank < total:
                time.sleep(INTER_STOCK_SLEEP)
            continue

        evaluated = _evaluate_conditions(merged)
        if evaluated.empty:
            if rank < total:
                time.sleep(INTER_STOCK_SLEEP)
            continue

        evaluated = evaluated.sort_values("date").reset_index(drop=True)
        latest    = evaluated.iloc[-1]
        recent    = evaluated.tail(5)

        # signals are now informational columns only — no longer used as a gate
        raw_golden  = bool(recent["Is_Golden_Signal"].any())
        is_breakout = bool(recent["Is_Breakout_Signal"].any())

        chip   = _get_chip_data(stock_id)
        cond_b = chip["Cond_B"]

        if chip["Cond_B_Available"]:
            is_golden = raw_golden and cond_b
        else:
            is_golden = raw_golden

        # compute full metrics for every candidate; apply_scan_mode is the sole filter
        sr = calc_all(merged)
        ta = calc_trend_analysis(merged, taiex_df=taiex_df if not taiex_df.empty else None)
        vs = _get_volume_stats(merged)

        def _lr(key):
            v = latest.get(key)
            return round(float(v), 4) if pd.notna(v) else None

        results.append({
                # ── 主列表核心欄位 ──
                "Stock_ID":          stock_id,
                "Stock_Name":        stock_name,
                "Close_Price":       round(float(latest.get("close", 0)), 2),
                "Explosion_Score":   round(float(latest.get("Explosion_Score", 0)), 1)
                                     if pd.notna(latest.get("Explosion_Score")) else 0.0,
                "RS_Score":          ta.get("RS_Score"),
                "Dist_52W_High_Pct": ta.get("Dist_52W_High_Pct"),
                "Sup_Gap_Pct":       sr.get("Sup_Gap_Pct"),
                "Res_Gap_Pct":       sr.get("Res_Gap_Pct"),
                # ── 訊號條件 ──
                "Cond_A":            bool(latest.get("Cond_A", False)),
                "Cond_C":            bool(latest.get("Cond_C", False)),
                "Cond_B":            cond_b,
                "Squeeze":           bool(sr.get("Squeeze", False)),
                "Is_Golden_Signal":  is_golden,
                "Is_Breakout_Signal": is_breakout,
                # ── 線型評估（主列表） ──
                "MA_Bull_Align":     bool(ta.get("MA_Bull_Align", False)),
                "Donchian_Break":    bool(ta.get("Donchian_Break", False)),
                "MACD_Cross":        bool(ta.get("MACD_Cross", False)),
                # ── 詳細面板：線型輔助 ──
                "MA_Squeeze":        bool(ta.get("MA_Squeeze", False)),
                "Trend_Breakout":    bool(ta.get("Trend_Breakout", False)),
                "MACD_Hist_Turn":    bool(ta.get("MACD_Hist_Turn", False)),
                "Near_52W_High":     bool(ta.get("Near_52W_High", False)),
                "RS_Strong":         bool(ta.get("RS_Strong", False)),
                # ── 詳細面板：集保籌碼 ──
                "Large_Holder_Pct":  chip["Large_Holder_Pct"],
                "Large_Pct_Change":  chip["Large_Pct_Change"],
                "Retail_Pct":        chip["Retail_Pct"],
                "Retail_Pct_Change": chip["Retail_Pct_Change"],
                # ── 詳細面板：均線與支撐 ──
                "MA5":               ta.get("MA5"),
                "MA10":              ta.get("MA10"),
                "MA20":              sr.get("MA20"),
                "MA60":              sr.get("MA60"),
                "Resist_60H":        sr.get("Resist_60H"),
                "Support_60L":       sr.get("Support_60L"),
                "VP_Zone1":          sr.get("VP_Zone1"),
                "VP_Zone2":          sr.get("VP_Zone2"),
                "VP_Zone3":          sr.get("VP_Zone3"),
                "Gap_Up_Sup":        sr.get("Gap_Up_Sup"),
                "Gap_Dn_Res":        sr.get("Gap_Dn_Res"),
                "Round_Level":       sr.get("Round_Level"),
                # ── 詳細面板：原始指標 ──
                "Range_Tightness":   _lr("Range_Tightness"),
                "Volume_Dryup":      _lr("Volume_Dryup_Ratio"),
                "Volume_Bias":       _lr("Volume_Bias"),
                # scan mode filter columns
                "Vol_MA20":          vs["Vol_MA20"],
                "Vol_MA5":           vs["Vol_MA5"],
                "Vol_Today":         vs["Vol_Today"],
                "Max_Price_20_Prev": vs["Max_Price_20_Prev"],
                "High_Today":        vs["High_Today"],
                "Low_Today":         vs["Low_Today"],
                "Close_Prev":        vs["Close_Prev"],
                "Cond_A_5D":         bool(recent["Cond_A"].any()),
            })

        if rank < total:
            time.sleep(INTER_STOCK_SLEEP)

    if not results:
        return pd.DataFrame()

    out = pd.DataFrame(results)
    out = out.sort_values("Explosion_Score", ascending=False).reset_index(drop=True)
    return out
