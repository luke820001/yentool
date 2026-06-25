from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from ingestion.price_volume_multi import multi_fetch_and_save, multi_fetch_and_save_batch
from ingestion.large_holder import LargeHolderFetcher
from ingestion.tdcc_holders import update_tdcc_holdings
from ingestion.inst_trades import update_inst_trades, get_inst_features
from ingestion.market_index import MarketIndexFetcher
from analyzer.signal_evaluator import _evaluate_conditions
from analyzer.support_resistance import calc_all
from scanner.data_integrity import audit_series
from analyzer.trend_analysis import calc_trend_analysis, calc_surge_score, calc_launch_score
from storage.data_store import (
    load_sheet, batch_latest_dates, batch_latest_date_strings,
    batch_row_counts, bulk_load_stocks,
)
from config.settings import (
    LARGE_HOLDER_FILE, PRICE_VOLUME_FILE, FINMIND_TOKEN, CHIP_FETCH_IN_SCAN,
)

CHIP_CACHE_DAYS  = 8

# Taiwan session closes 13:30; the consolidated end-of-day quote is published by
# ~14:00. Before then "today" has no official close yet, so the latest usable
# trading session is the previous trading day. A stock is stale (needs a fetch)
# whenever its newest stored bar predates this date -- this is what makes a scan
# pick up TODAY's bar instead of lagging a day.
_EOD_HOUR = 14


def _latest_trading_day() -> str:
    """Most recent trading session date as 'YYYY-MM-DD' (weekends/pre-close
    rolled back; holidays self-correct -- a stale fetch just finds nothing new)."""
    now = datetime.now()
    d = now.date()
    if now.hour < _EOD_HOUR:        # today's close not published yet
        d = d - timedelta(days=1)
    wd = d.weekday()                # Mon=0 .. Sun=6
    if wd == 5:                     # Saturday -> Friday
        d = d - timedelta(days=1)
    elif wd == 6:                   # Sunday -> Friday
        d = d - timedelta(days=2)
    return d.strftime("%Y-%m-%d")

# Below this many stored bars, a stock cannot support the 52-week-high (252) or
# RS (63) calculations, so we force a full backfill instead of a 7-day top-up.
MIN_HISTORY_BARS = 240

_PV_NUMERIC_COLS = (
    "close", "high", "low", "open", "Volume_Lot",
    "Min_Price_20", "Max_Price_20", "Min_Volume_20", "MA5_Volume",
)


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
    """Fetch chip data; on the FIRST failure of any kind, stop trying for the
    rest of this scan run. Chip data (Cond_B) is weekly, optional, and requires
    a paid FinMind plan, so a single failed probe is enough to conclude the
    source is unavailable. Without this, the 1.5s-throttled serial loop would
    grind through every candidate (100+ * 1.5s) and appear to hang."""
    global _chip_quota_exhausted
    if _chip_quota_exhausted:
        return
    try:
        lh_fetcher.fetch_and_save(stock_id)
    except Exception as e:
        _chip_quota_exhausted = True
        print("  [{}] chip unavailable ({}) -- skipping chip fetch for rest of scan"
              .format(stock_id, str(e)[:80]))


def _get_volume_stats(merged: pd.DataFrame) -> dict:
    """Extract scan-mode filter columns from the price-volume DataFrame."""
    defaults = {
        "Vol_MA20":          None,
        "Vol_MA5":           None,
        "Vol_Today":         None,
        "High_20_Prev":      None,
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

    # Prior 5-day average volume EXCLUDING today. The scan-mode "today vol > 5d
    # avg * N" surge test must compare against the baseline BEFORE today; the
    # stored MA5_Volume includes today, so today's own spike inflates the
    # denominator and silently makes the surge threshold harder to clear.
    if len(vol) >= 6:
        prior5 = vol.iloc[-6:-1].mean()
        result["Vol_MA5"] = round(float(prior5), 0) if pd.notna(prior5) else None
    elif "MA5_Volume" in merged.columns:
        ma5v = pd.to_numeric(merged["MA5_Volume"], errors="coerce").iloc[-1]
        result["Vol_MA5"] = round(float(ma5v), 0) if pd.notna(ma5v) else None

    last_vol = vol.iloc[-1]
    result["Vol_Today"] = int(last_vol) if pd.notna(last_vol) else None

    # Prior 20-day HIGH, excluding today (the breakout reference level). Built
    # from intraday highs -- not the max of closes -- so "close breaks the
    # 20-day high" means it actually exceeds the range's highs. Requires a full
    # 20-bar window.
    if "high" in merged.columns and len(merged) >= 21:
        h_series = pd.to_numeric(merged["high"], errors="coerce")
        high20_prev = h_series.rolling(20, min_periods=20).max().shift(1).iloc[-1]
        result["High_20_Prev"] = round(float(high20_prev), 2) if pd.notna(high20_prev) else None

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

    # One weekly snapshot is enough to show holdings; the change columns are
    # simply None until a second week is stored.
    if len(df) < 1:
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
        print("  [WARN] TAIEX data unavailable -- RS calculation skipped.")

    total        = len(candidates)
    candidate_ids = [str(r["stock_id"]) for _, r in candidates.iterrows()]
    id_to_market  = {str(r["stock_id"]): str(r.get("market", "TSE"))
                     for _, r in candidates.iterrows()}
    id_to_name    = {str(r["stock_id"]): str(r.get("stock_name", ""))
                     for _, r in candidates.iterrows()}

    # ── Phase 1: single query to find stale/missing stocks ───────────────────
    if progress_callback:
        progress_callback(0, total, "Checking cache ages...")
    pv_dates  = batch_latest_date_strings(PRICE_VOLUME_FILE, candidate_ids)
    pv_counts = batch_row_counts(PRICE_VOLUME_FILE, candidate_ids)
    chip_ages = batch_latest_dates(LARGE_HOLDER_FILE, candidate_ids)

    target_day = _latest_trading_day()

    # A stock needs a (re)fetch if its newest bar predates the latest trading day
    # (so we pick up today's close) OR its stored history is too short for the
    # long-window indicators (52-week high, RS).
    def _needs_full(sid):
        return pv_counts.get(sid, 0) < MIN_HISTORY_BARS

    def _is_stale(sid):
        d = pv_dates.get(sid)
        return d is None or d < target_day

    # ── Phase 2a: batch pv fetch (one threaded yfinance call per ~50 stocks) ──
    stale_pv = [
        sid for sid in candidate_ids
        if _is_stale(sid) or _needs_full(sid)
    ]
    fresh_pv = total - len(stale_pv)
    print("  pv cache hits: {}/{}  fetching: {}".format(fresh_pv, total, len(stale_pv)))

    if progress_callback:
        progress_callback(0, total,
                          "Fetching price data (batch {})...".format(len(stale_pv)))

    # Fast path: bulk yfinance + single-transaction write. Most stocks resolve here.
    fetched = multi_fetch_and_save_batch(stale_pv, id_to_market)
    missing = [sid for sid in stale_pv if sid not in fetched]
    print("  pv batch yfinance: {}/{}  fallback: {}".format(
        len(fetched), len(stale_pv), len(missing)))

    # Slow path: only the tickers yfinance could not return (TWSE/TPEX/FinMind).
    if missing:
        _done_count = [0]

        def _fetch_pv_fallback(stock_id):
            try:
                multi_fetch_and_save(
                    stock_id,
                    market=id_to_market[stock_id],
                    incremental=False,
                    skip_yfinance=True,
                )
            except Exception as e:
                print("  [{}] pv error: {}".format(stock_id, e))
            _done_count[0] += 1
            if progress_callback:
                progress_callback(
                    _done_count[0], len(missing),
                    "Fetching price data fallback ({}/{})...".format(
                        _done_count[0], len(missing)),
                )

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(_fetch_pv_fallback, missing))

    # ── Phase 2b: chip (集保) from TDCC free open data ───────────────────────
    # One whole-market weekly request (no quota, no per-stock loop) gives every
    # stock's large-holder / retail percentages. Replaces the paid, throttled
    # FinMind path that left this column blank.
    if progress_callback:
        progress_callback(total, total, "Updating shareholding (TDCC)...")
    try:
        tdcc_date = update_tdcc_holdings()
        print("  [chip] TDCC holdings up to {}".format(tdcc_date))
    except Exception as e:
        print("  [chip] TDCC update failed: {}".format(str(e)[:80]))

    # Daily three-institution net buy/sell (TWSE T86 + TPEX), whole-market, free.
    try:
        inst_date = update_inst_trades()
        print("  [inst] institutional net up to {}".format(inst_date))
    except Exception as e:
        print("  [inst] update failed: {}".format(str(e)[:80]))
    inst_feats = get_inst_features(candidate_ids)

    # ── Phase 3: bulk load — two queries for all stocks ───────────────────────
    if progress_callback:
        progress_callback(total, total, "Loading all price data...")
    pv_store   = bulk_load_stocks(PRICE_VOLUME_FILE, candidate_ids)
    chip_store = bulk_load_stocks(LARGE_HOLDER_FILE, candidate_ids)

    # Real trading calendar = union of every loaded stock's dates (whole-market),
    # so the per-stock data-integrity gap check is exact, not holiday-fooled.
    try:
        _cal_dates = pd.concat(
            [f["date"] for f in pv_store.values() if "date" in f.columns],
            ignore_index=True)
        _calendar = pd.DatetimeIndex(
            sorted(pd.to_datetime(_cal_dates, errors="coerce").dropna().unique()))
    except Exception:
        _calendar = None

    # ── Phase 4: analysis loop (no DB I/O) ───────────────────────────────────
    import time as _time
    _t_eval = _t_sr = _t_ta = _t_vs = 0.0
    results = []
    for rank, stock_id in enumerate(candidate_ids, start=1):
        stock_name = id_to_name[stock_id]

        if progress_callback:
            progress_callback(rank, total,
                              "Analyzing {} ({}/{})".format(stock_id, rank, total))

        raw = pv_store.get(stock_id, pd.DataFrame())
        if raw.empty:
            continue

        # Single copy + sort + numeric conversion; all sub-functions receive clean data
        merged = raw.copy()
        for _c in _PV_NUMERIC_COLS:
            if _c in merged.columns:
                merged[_c] = pd.to_numeric(merged[_c], errors="coerce")
        merged.sort_values("date", inplace=True)
        merged.reset_index(drop=True, inplace=True)

        # Recompute rolling-derived columns from the raw close/volume. The stored
        # ones are computed at fetch time and can drift out of sync with prices
        # that were later re-fetched or back-adjusted (auto_adjust), which would
        # otherwise feed a stale prior-high into the breakout signal.
        if "close" in merged.columns:
            merged["Max_Price_20"] = merged["close"].rolling(20, min_periods=1).max()
            merged["Min_Price_20"] = merged["close"].rolling(20, min_periods=1).min()
        if "Volume_Lot" in merged.columns:
            merged["MA5_Volume"]    = merged["Volume_Lot"].rolling(5,  min_periods=1).mean()
            merged["Min_Volume_20"] = merged["Volume_Lot"].rolling(20, min_periods=1).min()

        _t0 = _time.perf_counter()
        evaluated = _evaluate_conditions(merged)
        _t_eval += _time.perf_counter() - _t0
        if evaluated.empty:
            continue

        # already sorted (merged is sorted; _evaluate_conditions preserves order)
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
        ss = calc_surge_score(merged)
        ls = calc_launch_score(merged)
        _t_ta += _time.perf_counter() - _t0

        _t0 = _time.perf_counter()
        vs = _get_volume_stats(merged)
        _t_vs += _time.perf_counter() - _t0

        # Data-integrity audit (non-destructive): never alters scores, only
        # surfaces whether this name's series is clean. recent_jump means an
        # un-adjusted corporate action within ~60 bars is tainting its MAs.
        ig = audit_series(merged, calendar=_calendar)

        # 1-month (~20 bars) and 3-month (~63 bars) price change. Both drive the
        # pre-launch momentum mode and expose how much each name has already run.
        close_series = pd.to_numeric(merged["close"], errors="coerce").dropna().reset_index(drop=True)
        gain_3m = gain_1m = None
        cur_c = float(close_series.iloc[-1]) if len(close_series) else 0.0
        if len(close_series) >= 64:
            base_c = float(close_series.iloc[-64])
            if base_c > 0:
                gain_3m = round((cur_c / base_c - 1.0) * 100, 1)
        if len(close_series) >= 21:
            base_20 = float(close_series.iloc[-21])
            if base_20 > 0:
                gain_1m = round((cur_c / base_20 - 1.0) * 100, 1)

        def _lr(key):
            v = latest.get(key)
            return round(float(v), 4) if pd.notna(v) else None

        results.append({
            "Stock_ID":           stock_id,
            "Stock_Name":         stock_name,
            "Market":             id_to_market.get(stock_id, "TSE"),
            "Data_Date":          str(latest.get("date"))[:10],
            "Close_Price":        round(float(latest.get("close", 0)), 2),
            "Explosion_Score":    round(float(latest.get("Explosion_Score", 0)), 1)
                                  if pd.notna(latest.get("Explosion_Score")) else 0.0,
            "Surge_Score":        ss.get("Surge_Score"),
            "Launch_Score":       ls.get("Launch_Score"),
            "Ret_5D_Pct":         ls.get("Ret_5D_Pct"),
            "ATR_Pct":            ss.get("ATR_Pct"),
            "RS_Score":           ta.get("RS_Score"),
            "Gain_3M_Pct":        gain_3m,
            "Gain_1M_Pct":        gain_1m,
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
            "Foreign_Net":        inst_feats.get(stock_id, {}).get("Foreign_Net"),
            "Trust_Net":          inst_feats.get(stock_id, {}).get("Trust_Net"),
            "Foreign_Net_5D":     inst_feats.get(stock_id, {}).get("Foreign_Net_5D"),
            "Inst_Buy_Days":      inst_feats.get(stock_id, {}).get("Inst_Buy_Days"),
            "MA5":                ta.get("MA5"),
            "MA10":               ta.get("MA10"),
            "MA20":               sr.get("MA20"),
            "MA60":               sr.get("MA60"),
            "Resist_60H":         sr.get("Resist_60H"),
            "Support_60L":        sr.get("Support_60L"),
            "Support_20L":        sr.get("Support_20L"),
            "Support_Used":       sr.get("Support_Used"),
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
            "High_20_Prev":       vs["High_20_Prev"],
            "High_Today":         vs["High_Today"],
            "Low_Today":          vs["Low_Today"],
            "Close_Prev":         vs["Close_Prev"],
            "Min_Price_3":        vs["Min_Price_3"],
            "Cond_A_5D":          bool(recent["Cond_A"].any()),
            "Integrity_OK":       ig["trustworthy"],
            "Integrity_Flags":    ";".join(ig["flags"]),
            "Recent_Jump":        ig["recent_jump"],
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
