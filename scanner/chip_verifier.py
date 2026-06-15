from datetime import date, datetime
import pandas as pd
from ingestion.price_volume_multi import multi_fetch_and_save
from ingestion.large_holder import LargeHolderFetcher
from ingestion.market_index import MarketIndexFetcher
from analyzer.signal_evaluator import _evaluate_conditions
from analyzer.support_resistance import calc_all
from analyzer.trend_analysis import calc_trend_analysis
from storage.data_store import load_sheet, batch_latest_dates, bulk_load_stocks
from config.settings import LARGE_HOLDER_FILE, PRICE_VOLUME_FILE

# re-fetch price only when the latest cached row is older than this many days
PRICE_CACHE_DAYS = 1   # 1 = re-fetch only if we don't have today's (or yesterday's) bar
CHIP_CACHE_DAYS  = 8   # chip is weekly; 8 days covers one full cycle


def _age_from_dict(ages: dict, stock_id: str):
    """Look up pre-fetched age dict. Returns None when stock has no cached data."""
    return ages.get(stock_id)  # None if absent


def _is_fresh(ages: dict, stock_id: str, max_age_days: int) -> bool:
    age = _age_from_dict(ages, stock_id)
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
        "Min_Price_3":       None,
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
        low_s = pd.to_numeric(merged["low"], errors="coerce")
        l = low_s.iloc[-1]
        result["Low_Today"] = round(float(l), 2) if pd.notna(l) else None
        if len(low_s) >= 3:
            min3 = low_s.iloc[-3:].min()
            result["Min_Price_3"] = round(float(min3), 2) if pd.notna(min3) else None

    if "close" in merged.columns and len(merged) >= 2:
        prev_c = pd.to_numeric(merged["close"], errors="coerce").iloc[-2]
        result["Close_Prev"] = round(float(prev_c), 2) if pd.notna(prev_c) else None

    return result


def _get_chip_data(df: pd.DataFrame) -> dict:
    """Parse pre-loaded chip DataFrame; return Cond_B related fields."""
    defaults = {
        "Cond_B":            False,
        "Cond_B_Available":  False,
        "Large_Holder_Pct":  None,
        "Large_Pct_Change":  None,
        "Retail_Pct":        None,
        "Retail_Pct_Change": None,
    }
    if df is None or df.empty or "Cond_B" not in df.columns:
        return defaults

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < 2:
        return defaults

    latest = df.iloc[-1]

    def _sf(key):
        v = latest.get(key)
        return round(float(v), 4) if pd.notna(v) else None

    return {
        "Cond_B":            bool(str(latest.get("Cond_B", "False")).strip()
                                  in ("True", "1", "true")),
        "Cond_B_Available":  True,
        "Large_Holder_Pct":  _sf("Large_Holder_Pct"),
        "Large_Pct_Change":  _sf("Large_Pct_Change"),
        "Retail_Pct":        _sf("Retail_Pct"),
        "Retail_Pct_Change": _sf("Retail_Pct_Change"),
    }


def verify_candidates(
    candidates: pd.DataFrame,
    progress_callback=None,
) -> pd.DataFrame:
    global _chip_quota_exhausted
    _chip_quota_exhausted = False

    lh_fetcher  = LargeHolderFetcher()
    taiex_df    = MarketIndexFetcher().get()
    if taiex_df.empty:
        print("  [WARN] TAIEX data unavailable — RS calculation skipped.")

    total        = len(candidates)
    candidate_ids = [str(r["stock_id"]) for _, r in candidates.iterrows()]
    id_to_market  = {str(r["stock_id"]): str(r.get("market", "TSE"))
                     for _, r in candidates.iterrows()}
    id_to_name    = {str(r["stock_id"]): str(r.get("stock_name", ""))
                     for _, r in candidates.iterrows()}

    # ── Phase 1: single query to find stale/missing stocks ───────────────────
    if progress_callback:
        progress_callback(0, total, "Checking cache ages...")
    pv_ages   = batch_latest_dates(PRICE_VOLUME_FILE, candidate_ids)
    chip_ages = batch_latest_dates(LARGE_HOLDER_FILE, candidate_ids)

    # ── Phase 2: fetch only what is stale or missing ─────────────────────────
    for rank, stock_id in enumerate(candidate_ids, start=1):
        market = id_to_market[stock_id]
        if progress_callback:
            progress_callback(rank, total,
                              "Fetching {} ({}/{})".format(stock_id, rank, total))

        pv_age = pv_ages.get(stock_id)
        if pv_age is None:
            try:
                multi_fetch_and_save(stock_id, market=market, incremental=False)
            except Exception as e:
                print("  [{}] pv error: {}".format(stock_id, e))
        elif pv_age > PRICE_CACHE_DAYS:
            try:
                multi_fetch_and_save(stock_id, market=market, incremental=True)
            except Exception as e:
                print("  [{}] pv error: {}".format(stock_id, e))
        else:
            print("  [{}] pv cache hit ({} days)".format(stock_id, pv_age))

        if not _is_fresh(chip_ages, stock_id, CHIP_CACHE_DAYS):
            _fetch_chip_safe(lh_fetcher, stock_id)
        else:
            print("  [{}] chip cache hit".format(stock_id))

    # ── Phase 3: bulk load — two queries for all stocks ───────────────────────
    if progress_callback:
        progress_callback(total, total, "Loading all price data...")
    pv_store   = bulk_load_stocks(PRICE_VOLUME_FILE, candidate_ids)
    chip_store = bulk_load_stocks(LARGE_HOLDER_FILE, candidate_ids)

    # ── Phase 4: analysis loop (no DB I/O) ───────────────────────────────────
    import time as _time
    _t_eval = _t_sr = _t_ta = _t_vs = 0.0
    results = []
    for rank, stock_id in enumerate(candidate_ids, start=1):
        stock_name = id_to_name[stock_id]

        if progress_callback:
            progress_callback(rank, total,
                              "Analyzing {} ({}/{})".format(stock_id, rank, total))

        merged = pv_store.get(stock_id, pd.DataFrame())
        if merged.empty:
            continue

        _t0 = _time.perf_counter()
        evaluated = _evaluate_conditions(merged)
        _t_eval += _time.perf_counter() - _t0
        if evaluated.empty:
            continue

        evaluated = evaluated.sort_values("date").reset_index(drop=True)
        latest    = evaluated.iloc[-1]
        recent    = evaluated.tail(5)

        raw_golden  = bool(recent["Is_Golden_Signal"].any())
        is_breakout = bool(recent["Is_Breakout_Signal"].any())

        chip   = _get_chip_data(chip_store.get(stock_id))
        cond_b = chip["Cond_B"]
        is_golden = (raw_golden and cond_b) if chip["Cond_B_Available"] else raw_golden

        _t0 = _time.perf_counter()
        sr = calc_all(merged)
        _t_sr += _time.perf_counter() - _t0

        _t0 = _time.perf_counter()
        ta = calc_trend_analysis(merged, taiex_df=taiex_df if not taiex_df.empty else None)
        _t_ta += _time.perf_counter() - _t0

        _t0 = _time.perf_counter()
        vs = _get_volume_stats(merged)
        _t_vs += _time.perf_counter() - _t0

        def _lr(key):
            v = latest.get(key)
            return round(float(v), 4) if pd.notna(v) else None

        results.append({
            "Stock_ID":           stock_id,
            "Stock_Name":         stock_name,
            "Close_Price":        round(float(latest.get("close", 0)), 2),
            "Explosion_Score":    round(float(latest.get("Explosion_Score", 0)), 1)
                                  if pd.notna(latest.get("Explosion_Score")) else 0.0,
            "RS_Score":           ta.get("RS_Score"),
            "Dist_52W_High_Pct":  ta.get("Dist_52W_High_Pct"),
            "Sup_Gap_Pct":        sr.get("Sup_Gap_Pct"),
            "Res_Gap_Pct":        sr.get("Res_Gap_Pct"),
            "Cond_A":             bool(latest.get("Cond_A", False)),
            "Cond_C":             bool(latest.get("Cond_C", False)),
            "Cond_B":             cond_b,
            "Squeeze":            bool(sr.get("Squeeze", False)),
            "Is_Golden_Signal":   is_golden,
            "Is_Breakout_Signal": is_breakout,
            "MA_Bull_Align":      bool(ta.get("MA_Bull_Align", False)),
            "Donchian_Break":     bool(ta.get("Donchian_Break", False)),
            "MACD_Cross":         bool(ta.get("MACD_Cross", False)),
            "MA_Squeeze":         bool(ta.get("MA_Squeeze", False)),
            "Trend_Breakout":     bool(ta.get("Trend_Breakout", False)),
            "MACD_Hist_Turn":     bool(ta.get("MACD_Hist_Turn", False)),
            "Near_52W_High":      bool(ta.get("Near_52W_High", False)),
            "RS_Strong":          bool(ta.get("RS_Strong", False)),
            "Large_Holder_Pct":   chip["Large_Holder_Pct"],
            "Large_Pct_Change":   chip["Large_Pct_Change"],
            "Retail_Pct":         chip["Retail_Pct"],
            "Retail_Pct_Change":  chip["Retail_Pct_Change"],
            "MA5":                ta.get("MA5"),
            "MA10":               ta.get("MA10"),
            "MA20":               sr.get("MA20"),
            "MA60":               sr.get("MA60"),
            "Resist_60H":         sr.get("Resist_60H"),
            "Support_60L":        sr.get("Support_60L"),
            "VP_Zone1":           sr.get("VP_Zone1"),
            "VP_Zone2":           sr.get("VP_Zone2"),
            "VP_Zone3":           sr.get("VP_Zone3"),
            "Gap_Up_Sup":         sr.get("Gap_Up_Sup"),
            "Gap_Dn_Res":         sr.get("Gap_Dn_Res"),
            "Round_Level":        sr.get("Round_Level"),
            "Range_Tightness":    _lr("Range_Tightness"),
            "Volume_Dryup":       _lr("Volume_Dryup_Ratio"),
            "Volume_Bias":        _lr("Volume_Bias"),
            "Vol_MA20":           vs["Vol_MA20"],
            "Vol_MA5":            vs["Vol_MA5"],
            "Vol_Today":          vs["Vol_Today"],
            "Max_Price_20_Prev":  vs["Max_Price_20_Prev"],
            "High_Today":         vs["High_Today"],
            "Low_Today":          vs["Low_Today"],
            "Close_Prev":         vs["Close_Prev"],
            "Min_Price_3":        vs["Min_Price_3"],
            "Cond_A_5D":          bool(recent["Cond_A"].any()),
        })

    n = max(len(results), 1)
    print("[TIMING] eval={:.0f}ms  sr={:.0f}ms  ta={:.0f}ms  vs={:.0f}ms  per_stock={:.0f}ms".format(
        _t_eval * 1000, _t_sr * 1000, _t_ta * 1000, _t_vs * 1000,
        (_t_eval + _t_sr + _t_ta + _t_vs) * 1000 / n,
    ))

    if not results:
        return pd.DataFrame()

    out = pd.DataFrame(results)
    out = out.sort_values("Explosion_Score", ascending=False).reset_index(drop=True)
    return out
