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
    high20p  = _safe_num(df, "High_20_Prev",      _INF)

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
        # trigger: today close > prior-20d HIGH (excl. today)  AND  today volume > 5d avg * 2
        mask = (
            (vol_ma20 > 1000) &
            (close > high20p) &
            (vol_now > vol_ma5 * 2)
        )
        return df[mask].reset_index(drop=True)

    if selected_mode == "mode_bottom":
        # Strong Pullback: an established uptrend leader that has dipped back to
        # its 20-day line -- buy-the-dip in strength. Replaces the old falling-
        # knife logic (buy below 60MA on a MACD turn), which had NO edge on the
        # full-universe research data (lift 0.83). Backtest of this rule lifts
        # P(>=20%/20d) to ~1.55x base.
        ma20   = _safe_num(df, "MA20",        0.0)
        gain60 = _safe_num(df, "Gain_3M_Pct", float("-inf"))
        bias   = _safe_num(df, "Volume_Bias", 0.0)
        mask = (
            (close > ma60) &                              # established uptrend
            (gain60 >= 10.0) &                            # 3-month momentum
            (close <= ma20 * 1.04) & (close >= ma20 * 0.96) &  # dipped to ~20MA
            (bias >= 0.45) &                              # still accumulating
            (vol_ma20 > 300)
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

    if selected_mode == "mode_prelaunch":
        # Pre-Launch (forward-looking, EARLY): flag the setup BEFORE the run.
        # The eligible pool is simply "above 60MA + liquid" (encoded as
        # Launch_Score > 0); ranking by Launch_Score plus the hysteresis top-N
        # in the worker does the real selection. Validated lift ~2.26 for a
        # >=25%/20d move with the median name flagged at only +0.3% trailing-5d
        # (i.e. before the climax) -- see analyzer.trend_analysis.calc_launch_score.
        launch = _safe_num(df, "Launch_Score", 0.0)
        mask = launch > 0
        return df[mask].reset_index(drop=True)

    if selected_mode == "mode_momentum_leader":
        # Pre-Launch Momentum (forward-looking, NOT a past-winners list).
        #
        # Derived empirically from price_volume.db: for 665 cases where a stock
        # rose >= 30% within 20 trading days, the pre-launch fingerprint was
        # *momentum continuation*, not quiet consolidation. The features that
        # actually separated launch days from ordinary days (and validated with
        # lift ~1.7 / hit-rate 27% vs 16% base) were:
        #   above 60MA, MA bull stack (5>10>20), 3-month gain already positive,
        #   1-month still rising, and up-day volume bias. (Box tightness / volume
        #   dry-up showed NO predictive edge here, so they are intentionally out.)
        ma5    = _safe_num(df, "MA5",          0.0)
        ma10   = _safe_num(df, "MA10",         0.0)
        ma20   = _safe_num(df, "MA20",         0.0)
        bias   = _safe_num(df, "Volume_Bias",  0.0)
        gain60 = _safe_num(df, "Gain_3M_Pct",  float("-inf"))
        gain20 = _safe_num(df, "Gain_1M_Pct",  float("-inf"))
        mask = (
            (close > ma60) &              # established uptrend
            (ma5 > ma10) & (ma10 > ma20) &  # short-term bull stack
            (gain60 >= 20.0) &            # 3-month momentum established
            (gain20 >= 5.0) &             # still rising near-term (not stalled)
            (bias >= 0.50) &              # up-day volume dominates (accumulation)
            (vol_ma20 > 300)              # liquidity floor
        )
        return df[mask].reset_index(drop=True)

    # Unknown mode: return unfiltered
    return df


# Per-mode ranking key. Momentum modes must NOT be ranked by Explosion_Score:
# that score rewards tight consolidation + volume dry-up, which is the inverse
# of a breakout/runner, so the strongest candidates would sink to the bottom.
# Surge_Score (explosive-potential) is the validated ranking for momentum modes
# (top-decile lift ~3.3 for a >=30% move). Squeeze keeps Explosion_Score, which
# is its native low-volatility coiling metric.
_SORT_KEYS = {
    "mode_squeeze":         "Explosion_Score",
    "mode_bottom":          "Surge_Score",
    "mode_breakout":        "Surge_Score",
    "mode_short_explosion": "Surge_Score",
    "mode_momentum_leader": "Surge_Score",
    "mode_prelaunch":       "Launch_Score",
}


def sort_for_mode(df, selected_mode):
    """Rank rows by a key appropriate to the mode (descending)."""
    if df is None or df.empty:
        return df
    key = _SORT_KEYS.get(selected_mode, "Explosion_Score")
    if key not in df.columns:
        return df
    score = pd.to_numeric(df[key], errors="coerce").fillna(float("-inf"))
    return (
        df.assign(_sort_key=score)
          .sort_values("_sort_key", ascending=False)
          .drop(columns="_sort_key")
          .reset_index(drop=True)
    )


# Hysteresis top-N selection. The single biggest driver of the "list changes
# completely every day / feels like hindsight" problem was hard pass/fail gates
# at the candidate boundary plus same-day event triggers. Ranking + hysteresis
# fixes it: a name ENTERS the shortlist only in the strict top N_ENTER, but is
# HELD as long as it stays within the looser top N_HOLD. On the research db this
# lifted day-to-day list overlap from ~0.60 to ~0.89 and median time-on-list
# from 2 to ~11 days, with no loss of forward lift (debug_early_design.py).
# enter 20 / hold 80 chosen on the live-flow replay (debug_prelaunch_live_sim.py):
# it lifts day-to-day list overlap to ~0.79 and median time-on-list to ~6 days
# (vs 0.05-0.13 and 1 day for the old same-day-event modes) while keeping the
# forward lift at ~2.5. Widening the hold band further only trades lift for size.
N_ENTER = 20   # fresh-entry cutoff (strict)
N_HOLD  = 80   # retention cutoff (loose) -- a held name survives down to here


def select_with_hysteresis(df, prior_ids, n_enter=N_ENTER, n_hold=N_HOLD):
    """
    Stabilize the displayed shortlist. `df` must already be ranked best-first
    (see sort_for_mode). A row is kept when it ranks within `n_enter` (a fresh
    pick) OR it was selected last run (`prior_ids`) and still ranks within
    `n_hold` (held through noise). Order is preserved.

    Returns (selected_df, new_ids) where new_ids is the Stock_ID list to persist
    for the next run. With an empty prior set this is just plain top-n_enter.
    """
    if df is None or df.empty:
        return df, []
    ids = df["Stock_ID"].astype(str).tolist() if "Stock_ID" in df.columns else []
    prior = set(str(x) for x in (prior_ids or []))

    keep_mask = []
    for rank, sid in enumerate(ids):
        fresh = rank < n_enter
        held  = (sid in prior) and (rank < n_hold)
        keep_mask.append(fresh or held)

    selected = df[pd.Series(keep_mask, index=df.index)].reset_index(drop=True)
    new_ids = selected["Stock_ID"].astype(str).tolist() if "Stock_ID" in selected.columns else []
    return selected, new_ids


# Entry/stop parameters, tuned on price_volume.db (debug_stop_backtest.py).
# Backtest finding: a structural stop (MA10/3-day low) often lands 1-2% below
# entry on extended names and gets shaken out 30%+ of the time on noise (the
# "bought then immediately stopped out" problem). Stops of ~8-13% cut premature
# exits to ~10-20% and raise expectancy. So the structural stop is kept but
# CLAMPED into a sane risk band, and entry is a shallow dip (not a deep MA5 dip
# that never fills and underperforms).
ENTRY_BUFFER = 0.02   # suggest entry ~2% below close (realistic limit fill)
MIN_STOP_PCT = 0.06   # never tighter than 6% below entry (avoid instant shakeout)
MAX_STOP_PCT = 0.13   # never risk more than 13% per trade

# mode_prelaunch override, validated on the signal ledger (2026-07-06; see
# docs/EVAL_PLAYBOOK.md and eval_realtrade.py). Two findings forced this:
#   1. The limit-below entry is adverse selection: picks that filled made
#      +0.54% (5d) while picks that never pulled back to the limit made +9.67%
#      -- the limit systematically skips the winners. Entry is therefore
#      next-day OPEN at market; Suggested_Buy_Price becomes a reference price
#      (= signal-day close), not a limit to post.
#   2. Any stop inside the daily noise band destroys the edge (-6% intraday
#      turned a +3.03% mean into +0.16%). Only a wide disaster stop survives,
#      so the stop is a flat -10% off the reference. The live stop must be
#      recomputed off the actual fill: fill * (1 - PRELAUNCH_STOP_PCT).
# Exit is time-based (5th bar close); that rule lives in the UI banner and the
# playbook, not in these columns. Other modes keep the old plan: their rules
# have no as-directed ledger evidence yet, changing them would be a blind edit.
PRELAUNCH_STOP_PCT = 0.10


def add_trade_columns(df, scan_mode: str) -> "pd.DataFrame":
    """
    Append Suggested_Buy_Price, Strict_Stop_Loss and Risk_Pct.

    Suggested_Buy_Price
      mode_prelaunch:
          Close_Price -- reference for a next-day OPEN market entry (no limit;
          see PRELAUNCH_STOP_PCT block comment for the ledger evidence).
      momentum (breakout / short_explosion / momentum_leader):
          close * (1 - ENTRY_BUFFER) -- buy a shallow dip near price.
      consolidation (squeeze / bottom):
          min(Close_Price, MA20)     -- enter in the value zone.

    Strict_Stop_Loss
      mode_prelaunch:
          reference * (1 - PRELAUNCH_STOP_PCT) -- disaster stop only.
      other modes:
          structural support = max(Min_Price_3, MA10), CLAMPED so the distance
          below the entry stays within [MIN_STOP_PCT, MAX_STOP_PCT]. This
          guarantees stop < buy with breathing room. Risk_Pct exposes the
          resulting distance for sizing.
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    close = _safe_num(df, "Close_Price", 0.0)
    ma10  = _safe_num(df, "MA10",        0.0)
    ma20  = _safe_num(df, "MA20",        0.0)
    min3  = _safe_num(df, "Min_Price_3", 0.0)

    if scan_mode == "mode_prelaunch":
        buy  = close
        stop = close * (1 - PRELAUNCH_STOP_PCT)
        df["Suggested_Buy_Price"] = buy.round(2)
        df["Strict_Stop_Loss"]    = stop.round(2)
        df["Risk_Pct"]            = round(PRELAUNCH_STOP_PCT * 100, 1)
        return df

    if scan_mode in ("mode_breakout", "mode_short_explosion",
                     "mode_momentum_leader"):
        buy = close * (1 - ENTRY_BUFFER)
    else:
        # take the lower of close and MA20; fall back to close when MA20 is 0
        stacked = pd.concat(
            [close, ma20.replace(0, float("nan"))], axis=1
        )
        stacked.columns = ["c", "m"]
        buy = stacked.min(axis=1).fillna(close)

    buy = buy.where(buy > 0, close)

    # structural support = max(Min_Price_3, MA10); NaN when both are missing
    struct = pd.concat(
        [min3.replace(0, float("nan")), ma10.replace(0, float("nan"))], axis=1
    ).max(axis=1)

    # clamp the stop into [buy*(1-MAX), buy*(1-MIN)] so it is always safely below
    # entry and never tighter than MIN_STOP_PCT
    lo = buy * (1 - MAX_STOP_PCT)
    hi = buy * (1 - MIN_STOP_PCT)
    stop = struct.clip(lower=lo, upper=hi)
    stop = stop.where(stop.notna(), hi)   # no structure -> use the MIN_STOP band

    df["Suggested_Buy_Price"] = buy.round(2)
    df["Strict_Stop_Loss"]    = stop.round(2)
    df["Risk_Pct"]            = ((buy - stop) / buy * 100).round(1)
    return df
