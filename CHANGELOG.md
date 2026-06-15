# YenTool Changelog

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
