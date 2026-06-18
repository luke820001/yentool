# YenTool Changelog

---

## 2026-06-18

### scoring: recalibrate Surge_Score to spread (it was saturating)

**File:** `analyzer/trend_analysis.py`

Among the filtered candidates shown in a scan the score sat at 80-100 (42% >=80),
because the normalization caps were too low (ATR maxed at 6%, momentum at 50%).
Widened the denominators (ATR/0.11, ret60/1.0, ret20/0.45, dist60/0.45) so the
candidate median is ~50 and p90 ~73, while the >=30% top-decile lift is unchanged
(3.34 -> 3.31). See `debug_surge_dist.py`.

### chip: free whole-market shareholding (TDCC) replaces paid FinMind

**Files:** `ingestion/tdcc_holders.py` (new), `scanner/chip_verifier.py`,
`gui/app.py`

`大戶/散戶持股%` was blank ("-") because the FinMind chip path is paid/disabled.
New `ingestion/tdcc_holders.py` pulls the TDCC open-data shareholding-distribution
(one request, whole market ~3990 stocks, weekly, free) and derives large-holder
(>=400 lots) / retail (<=50 lots) percentages, with week-over-week change building
over time. Wired into the scan as a single weekly-cached request.

### chip: daily institutional net buy/sell (TWSE T86 + TPEX), free

**Files:** `ingestion/inst_trades.py` (new), `scanner/chip_verifier.py`,
`gui/app.py`

New `ingestion/inst_trades.py` fetches the three-institution daily net buy/sell
(foreign / trust / dealer) for the whole market -- TWSE T86 (dated, supports
backfill) + TPEX openapi -- in lots, storing daily snapshots. Adds `Foreign_Net`,
`Trust_Net`, `Foreign_Net_5D`, `Inst_Buy_Days` columns and a detail-panel section.
TPEX dates are ROC (`1150618`) and are converted to Gregorian.

### chip: validated -- holdings LEVEL is not predictive; flow is a confirmation

**Files:** `gui/app.py`, validation scripts (`debug_chip_vs_return.py`,
`debug_inst_backtest.py`)

- The static large-holder / retail percentage does NOT predict moves: cross-
  sectionally large% correlates -0.06 with trailing return (very-high large% =
  locked/dead float), retail% +0.07. So holdings level was NOT added to the score.
- Backtested foreign flow on 76 backfilled days: foreign 5-day net as a FILTER on
  high-surge candidates raises precision (23.5% -> 24.9% for a >=30% move), but
  BLENDING it into the score additively HURTS (down to ~22-23%). So Surge_Score
  stays pure price/volume; `Foreign_Net_5D` is surfaced as a main-table
  confirmation column (prefer foreign-buying among high-surge names).

---

## 2026-06-17

### scoring: replace Explosion_Score with Surge_Score as the headline metric

**Files:** `analyzer/trend_analysis.py` (new `calc_surge_score`),
`scanner/chip_verifier.py`, `scanner/scan_mode.py`, `gui/app.py`

**Problem:** Backtests on the full-universe research db (6 years, 1946 stocks)
showed `Explosion_Score` (box-tightness + volume dry-up + bias) is *inverted* —
its top decile had a lift of **0.47** for a >=30%/20-day move (anti-predictive).
Ranking by it picked the worst stocks. Validated across overlapping, independent
(non-overlapping), and cross-sectional tests; the inversion held every year.

**Fix:** New `Surge_Score` (0-100) = momentum x volatility x volume, gated by
trend (price > 60MA). Components by validated power: ATR(volatility) lift 2.54,
3-/1-month momentum ~2.3, up-volume bias minor; distance-to-52w-high and box
tightness carry NO signal and are excluded. Top-decile lift **3.34** for a
>=30% move. Added `ATR_Pct` column. GUI headline column `爆發分` -> `噴發分`,
plus a `波動%` column. `Explosion_Score` kept as `蓄勢分` for the squeeze mode.
Momentum modes now rank by `Surge_Score` (`_SORT_KEYS`).

### scan_mode: forward-looking momentum_leader + honest mode relabels

**Files:** `scanner/scan_mode.py`, `config/scan_modes.json`,
`scanner/market_filter.py`, `scanner/chip_verifier.py`

- **New `mode_momentum_leader`** ("起漲前動能"): empirically-derived pre-launch
  momentum screen (above 60MA, MA stack, 3M gain >=20%, 1M >=5%, up-volume bias).
  Added `Gain_3M_Pct` / `Gain_1M_Pct`.
- **`mode_bottom` rebuilt** from falling-knife (lift 0.83) to **Strong Pullback**
  ("強勢回檔買點": uptrend leader dipped to ~20MA) — backtested lift **1.55**.
- **`mode_squeeze` relabeled** "經典爆發蓄勢" -> "低波蓄勢(高勝率穩健)" (lift 0.30
  for explosions; it is a low-volatility steady mode, not an explosion screen).
- **Mode-aware ranking** (`sort_for_mode`): momentum modes were being sorted by
  `Explosion_Score`, which is inverse-correlated; now ranked by `Surge_Score`.

### data fetch: batch yfinance + bulk write, freshness, decouple chip

**Files:** `ingestion/price_volume_multi.py`, `storage/data_store.py`,
`scanner/chip_verifier.py`, `config/settings.py`

- **Batch yfinance** (`fetch_yfinance_batch`, `multi_fetch_and_save_batch`) + a
  single-transaction `bulk_upsert_stocks`: full-market fetch ~70s -> ~15s.
- **Freshness fix:** staleness is now judged against the latest trading day
  (`_latest_trading_day`, 14:00 EOD cutoff + weekend rollback) instead of a fixed
  2-day window, so a scan picks up TODAY's bar instead of lagging up to 2 days.
- **Chip (Cond_B) decoupled** from scans (`CHIP_FETCH_IN_SCAN`, default off):
  the FinMind 1.5s-throttled serial loop no longer hangs the scan; cache-only.
- **yfinance log noise silenced** (404/delisted on the .TW-vs-.TWO probe).

### calc fixes: stale rolling columns, support, dryup; full audit

**Files:** `scanner/chip_verifier.py`, `analyzer/support_resistance.py`,
`analyzer/signal_evaluator.py`

- **Max_Price_20 drift fix:** stored rolling-derived columns could drift out of
  sync with re-fetched / auto-adjusted close, feeding a stale prior-high into the
  breakout signal. Now recomputed from raw close/volume in the analysis loop.
- **Adaptive support:** added `Support_20L` and `Support_Used` = nearest level
  below price among MA10/MA20/20-day low. The old 60-day low sat 30-50% under
  price for runners (8042: 距支撐 283% -> ~18%) and was not actionable.
- **Volume_Dryup smoothed** to a 3-day average volume (was single-day), so
  Explosion/Cond_A no longer whipsaw on one spike (3236 case).
- **Full column audit** (`debug_audit_all.py`): all other columns verified
  correct to rounding (MAs, S/R, gaps, gains, MACD, Donchian, booleans).

### trade plan: stop-below-buy invariant + risk-banded stop

**File:** `scanner/scan_mode.py`

`add_trade_columns` previously could produce `stop >= buy` (e.g. 8042) and stops
1-2% from entry that get shaken out. Backtest: tight stops on volatile momentum
names get stopped prematurely ~30%. Fix: structural stop clamped into a
[6%, 13%] band below entry (always below buy, never too tight); added `Risk_Pct`.

### GUI: manual lookup, sortable columns, regime banner, trend report

**Files:** `gui/app.py`, `gui/scan_worker.py`, `scanner/market_regime.py` (new),
`scanner/regime_report.py` (new), `scanner/result_export.py` (new),
`scanner/market_filter.py`

- **Manual single-stock lookup** button: resolves market + Chinese name from the
  live snapshot (with a persistent `stock_names.json` cache — first lookup caches
  ~11500 names, later lookups are instant), runs the full pipeline, shows the row.
- **Clickable column-header sorting** by underlying value (numbers/bools/blanks),
  toggle direction, arrow indicator.
- **Market-regime banner:** TAIEX above/below its MAs -> momentum-edge tailwind
  vs headwind (the 2022 bear collapsed the edge to lift ~1.07).
- **"趨勢報告" button:** refreshes the research db (incremental) and writes a
  recent-2y explosion-fingerprint report (`data/scan_results/regime_report.md`).
- **Scan result CSV export** after every full scan (`scan_result_latest.csv`,
  latest only, utf-8-sig for Excel) with all 57+ computed columns.

### research infrastructure: full-universe multi-year backtest db

**Files:** `build_research_db.py` (new), `scanner/regime_report.py` (new),
plus validation scripts (`debug_*.py`)

- **`build_research_db.py`:** builds/maintains `data/research_prices.db` — the
  full listed universe (~1946 stocks) with ~6 years history (spans the 2022
  bear), stored separately from the live scan db. Self-healing & incremental:
  each run fills only the gaps (new/short -> full backfill; stale -> top-up;
  fresh -> skip), recomputing derived columns over the full merged series.
- Used to re-validate every conclusion on **unbiased, multi-regime** data after
  the audit found the live db was a momentum-survivor sample (median +99.5%
  return, 49% of stocks doubled, 0% halved — and no bear market).

**Key empirical findings (drive the above):** momentum-continuation (not quiet
consolidation) precedes explosions; ATR is the single best predictor of big
moves; the edge is regime-robust in trending years (~1.4x) but collapses in the
2022 bear; +10%/20d is noise now (base 24%), the meaningful move is >=30% (5%).

---

## 2026-06-14

### price/volume: multi-source fetcher with yfinance + TWSE/TPEX official API

**Files:** `ingestion/price_volume_multi.py` (new), `scanner/chip_verifier.py`,
`requirements.txt`

**Problem:**
FinMind free tier returns HTTP 402 after ~50–80 API calls per day.
Scanning 200 candidates generates 400 calls (price + chip per stock),
exhausting the quota mid-scan and leaving the remaining stocks without data.

**Fix:**
New module `ingestion/price_volume_multi.py` provides `multi_fetch_and_save`
which tries three sources in priority order:

| Priority | Source | Notes |
|----------|--------|-------|
| 1 | **yfinance** | Free, fast. TSE: `2330.TW`, OTC: `3008.TWO`. No token. |
| 2 | **TWSE API** (TSE) / **TPEX API** (OTC) | Official exchange endpoints. Free, no token. Fetches month-by-month, with 0.4 s politeness delay between months. |
| 3 | **FinMind** | Original source, kept as last resort. |

`chip_verifier.py` now calls `multi_fetch_and_save(stock_id, market=market)`
instead of `pv_fetcher.fetch_and_save(stock_id)`. The `market` value
(`"TSE"` / `"OTC"`) is read from the candidates DataFrame row so the correct
exchange API is chosen.

Combined with the existing cache check (`PRICE_CACHE_DAYS = 3`), FinMind is
only reached when both yfinance and the official exchange API fail **and** the
local cache is stale — effectively avoiding 402 errors in normal operation.

---

### chip_verifier: local cache to avoid FinMind 402 rate-limit errors

**File:** `scanner/chip_verifier.py`

**Problem:**
Scanning 200 candidates generates ~400 FinMind API calls per run
(price + chip per stock). The free-tier quota is exhausted mid-scan,
returning HTTP 402 for the remaining stocks. Those stocks then have no
local data, so they are silently dropped from results.

**Fix:**
Added `_is_cache_fresh(file_path, stock_id, max_age_days)` which reads the
local Excel sheet for a stock and checks whether the most recent date row
is within `max_age_days` of today.

Before each `fetch_and_save` call, the cache is checked:

| Data type | Cache threshold | Constant |
|-----------|----------------|----------|
| Price / volume | 3 calendar days | `PRICE_CACHE_DAYS = 3` |
| Chip (shareholding) | 8 calendar days | `CHIP_CACHE_DAYS = 8` |

If the cache is fresh the API call is skipped entirely and a `cache hit`
line is printed instead. On the first scan of the day, all 200 stocks are
fetched as before. On every subsequent scan in the same session (or same
day), zero API calls are made for stocks already cached.

---

### chip_verifier: remove is_golden / is_breakout gate

**File:** `scanner/chip_verifier.py`

**Problem:**
`verify_candidates` only added a stock to the result set when it satisfied
`is_golden OR is_breakout` in the past 5 days.  This created a double-filter
architecture:

1. `chip_verifier` — gated by `is_golden | is_breakout`
2. `apply_scan_mode` — mode-specific Pandas masks

The gate made most scan modes find zero targets:

- `mode_bottom` targets stocks *below* MA60 in accumulation phase.
  Those stocks never trigger `is_golden` (no tight-box breakout) or
  `is_breakout` (no 20-day high breach), so they were invisible to every scan
  mode before even reaching `apply_scan_mode`.
- `mode_squeeze` required `Cond_A AND Cond_C` to pass the gate; stocks with
  `Cond_A` only (volume not yet biased upward) were silently dropped.
- `mode_short_explosion` / `mode_breakout` could occasionally survive, but
  the gate's `BREAKOUT_VOLUME_MULTIPLIER = 2.5` was the same threshold as
  scan mode's 2× check, making survival near-impossible.

**Fix:**
- Removed the `if is_golden or is_breakout:` conditional block.
- `is_golden` and `is_breakout` are now **informational columns** in the
  result DataFrame, not selection gates.
- `Cond_B` is no longer a hard requirement for `is_golden`; it remains a
  column so `mode_bottom` can use it as a filter condition.
- `best_row` (the most recent signal row) is replaced by `latest`
  (the most recent calendar row) for all per-row metric reads.
- `calc_all`, `calc_trend_analysis`, and `_get_volume_stats` are now called
  **for every candidate**, not only those that passed the old gate.
- `apply_scan_mode` is now the **sole** filter layer.

**Impact:**
- Each scan now returns up to 200 rows before scan-mode filtering.
- Scan time is roughly the same (expensive I/O already ran for all 200
  candidates; only the conditional `calc_all` / `calc_trend_analysis` calls
  move outside the `if` block).

---

### scan_mode: add mode_short_explosion (Short-Term Explosion)

**Files:** `scanner/scan_mode.py`, `config/scan_modes.json`,
`scanner/chip_verifier.py`

**New mode key:** `mode_short_explosion`

**Filter conditions (all must be True):**

| # | Condition | Column(s) used |
|---|-----------|----------------|
| 1 | 20d avg volume > 1000 lots | `Vol_MA20` |
| 2 | Intraday amplitude ≥ 5% | `High_Today`, `Low_Today` |
| 3a | Daily gain ≥ 4% vs previous close | `Close_Price`, `Close_Prev` |
| 3b | Close within 1.5% of day high | `High_Today`, `Close_Price` |
| 4 | Volume > 5d avg × 2.5 | `Vol_Today`, `Vol_MA5` |
| 5 | Close > MA5 > MA10 | `MA5`, `MA10` |

New columns added to `_get_volume_stats` (and result dict):
`High_Today`, `Low_Today`, `Close_Prev`

NaN guard: if any required price column is None for a stock, that row is
excluded (safe Pandas NaN propagation, no explicit isnull checks needed).

---

### scan_mode: add modes 1–3 (prior session)

**Files:** `scanner/scan_mode.py`, `config/scan_modes.json`

| Mode key | Label | Key conditions |
|----------|-------|----------------|
| `mode_squeeze` | Classic Squeeze - 經典爆發蓄勢 | price<150, Vol_MA20>500, close>MA60, Cond_A |
| `mode_breakout` | Momentum Breakout - 動能突破發動 | Vol_MA20>1000, close>20d-high, vol>MA5×2 |
| `mode_bottom` | Bottom Accumulation - 跌深大戶建倉 | close<MA60, MACD_Hist_Turn, Cond_B |

---

### market_filter: expand pre-filter pool

**File:** `scanner/market_filter.py`, `config/settings.py`

- `PREFILTER_TOP_N = 200` (was `VOLUME_TOP_N = 50`)
- Price cap removed from pre-filter for all modes except `mode_squeeze`
  (`mode_squeeze` pre-applies `close < 150` to avoid scanning irrelevant
  high-price stocks before the mode filter stage).

---

### large_holder: fix KeyError on missing 'percent' column

**File:** `ingestion/large_holder.py`

Added early return in `_transform` when the API response does not include a
`percent` column (occurs for certain stock categories on FinMind).
Previously caused a `KeyError: 'percent'` logged for every such stock.
