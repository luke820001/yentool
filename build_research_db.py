"""
Build / maintain a research (backtest) price database: the FULL listed universe
(~1900 stocks, not just the momentum-selected scan subset) with multi-year
history (spanning bull AND bear regimes). Stored SEPARATELY from the live scan
db so it never interferes with scanning.

Self-healing & incremental: each run only fills the GAPS --
  - new stocks (or too-short history)  -> full backfill (LOOKBACK_DAYS)
  - stocks behind the latest trading day -> small incremental top-up
  - already up-to-date stocks            -> skipped
Derived columns are recomputed over the full merged series after every upsert,
so an incremental fetch never leaves partial-window values behind. ASCII only.

Usage:  python build_research_db.py [limit]
"""
import sys
import time
import sqlite3
from datetime import datetime, timedelta

import pandas as pd

from scanner.market_filter import fetch_full_market
from ingestion.price_volume_multi import (
    fetch_yfinance_batch, _add_derived, YF_BATCH_SIZE,
)
from storage.data_store import batch_latest_date_strings, batch_row_counts
from config.settings import DATA_DIR

RESEARCH_DB      = DATA_DIR / "research_prices.db"
LOOKBACK_DAYS    = 1500   # ~5-6 years; spans multiple regimes incl. a real bear
INCREMENTAL_DAYS = 40     # top-up window for stocks already holding history
MIN_FULL_BARS    = 200    # below this many stored bars -> force a full backfill

_BASE = ["date", "stock_id", "open", "high", "low", "close", "volume_share"]
_EOD_HOUR = 14


def _latest_trading_day() -> str:
    """Most recent trading session date 'YYYY-MM-DD' (pre-close/weekends rolled
    back; holidays self-correct -- a stale fetch just finds nothing new)."""
    now = datetime.now()
    d = now.date()
    if now.hour < _EOD_HOUR:
        d = d - timedelta(days=1)
    wd = d.weekday()
    if wd == 5:
        d = d - timedelta(days=1)
    elif wd == 6:
        d = d - timedelta(days=2)
    return d.strftime("%Y-%m-%d")


def _upsert_chunk(frames, conn):
    """Merge each stock's new bars with existing history (no trimming) and
    recompute derived columns over the full series."""
    for sid, new_df in frames.items():
        if new_df is None or new_df.empty:
            continue
        try:
            existing = pd.read_sql_query(
                "SELECT date,stock_id,open,high,low,close,volume_share "
                "FROM data WHERE stock_id = ?", conn, params=(str(sid),))
        except Exception:
            existing = pd.DataFrame(columns=_BASE)

        if existing.empty:
            merged = new_df[_BASE].copy()
        else:
            merged = pd.concat([existing, new_df[_BASE]], ignore_index=True)
            merged = (merged.drop_duplicates(subset=["date", "stock_id"], keep="last")
                            .sort_values("date").reset_index(drop=True))
            conn.execute("DELETE FROM data WHERE stock_id = ?", (str(sid),))

        _add_derived(merged).to_sql("data", conn, if_exists="append", index=False)


def _fetch_group(ids, id_to_market, lookback, conn, label):
    got = 0
    for i in range(0, len(ids), YF_BATCH_SIZE):
        chunk = ids[i:i + YF_BATCH_SIZE]
        tmap = {s: id_to_market.get(s, "TSE") for s in chunk}
        res = fetch_yfinance_batch(tmap, lookback_days=lookback)
        _upsert_chunk(res, conn)
        got += len(res)
        print("  [{}] {}/{} got {} (cum {})".format(
            label, min(i + YF_BATCH_SIZE, len(ids)), len(ids), len(res), got))
    return got


def build(limit=None):
    t0 = time.time()
    print("Fetching full-market universe ...")
    full = fetch_full_market()
    if full.empty:
        print("ERROR: could not fetch market list.")
        return
    if limit:
        full = full.head(int(limit))
    ids = [str(s) for s in full["stock_id"]]
    id_to_market = {str(r["stock_id"]): str(r.get("market", "TSE"))
                    for _, r in full.iterrows()}

    # Decide per stock what (if anything) to fetch -- fill only the gaps.
    dates  = batch_latest_date_strings(RESEARCH_DB, ids)
    counts = batch_row_counts(RESEARCH_DB, ids)
    target = _latest_trading_day()

    full_ids = [s for s in ids if counts.get(s, 0) < MIN_FULL_BARS]
    fullset = set(full_ids)
    incr_ids = [s for s in ids
                if s not in fullset and (dates.get(s) is None or dates.get(s) < target)]
    fresh = len(ids) - len(full_ids) - len(incr_ids)

    print("universe {} | full-backfill {} | incremental {} | up-to-date {} | db {}".format(
        len(ids), len(full_ids), len(incr_ids), fresh, RESEARCH_DB))

    RESEARCH_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(RESEARCH_DB, timeout=60) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        if full_ids:
            _fetch_group(full_ids, id_to_market, LOOKBACK_DAYS, conn, "full")
        if incr_ids:
            _fetch_group(incr_ids, id_to_market, INCREMENTAL_DAYS, conn, "incr")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_research ON data(stock_id, date)")

    print("\nDONE in {:.0f}s  (full {}, incr {}, skipped {}) -> {}".format(
        time.time() - t0, len(full_ids), len(incr_ids), fresh, RESEARCH_DB))


if __name__ == "__main__":
    build(limit=sys.argv[1] if len(sys.argv) > 1 else None)
