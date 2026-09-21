"""
Backfill per-date institutional flow for one board into a research store.
ASCII only.

    python tools/backfill_inst_history.py TSE 2017-03-01 2026-09-18
    python tools/backfill_inst_history.py OTC 2017-03-01 2026-09-18 --retry

Trading days come from data/research_prices.db (a date the tape actually
traded: at least 300 stocks with a bar), so weekends and holidays are never
requested. Each board writes its own file (data/research_inst_<board>.db) so
two boards can run side by side without fighting over one SQLite lock. The
run is resumable; --retry re-asks the days that failed last time.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingestion.inst_history import backfill  # noqa: E402

RESEARCH_DB = os.path.join("data", "research_prices.db")


def trading_days(start, end, min_stocks=300, db=RESEARCH_DB):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT date, COUNT(*) FROM data WHERE date >= ? AND date <= ? "
            "AND Volume_Lot > 0 GROUP BY date", (start, end)).fetchall()
    finally:
        conn.close()
    return sorted(str(d)[:10] for d, n in rows if n >= min_stocks)


def main():
    board = sys.argv[1].upper()
    start, end = sys.argv[2], sys.argv[3]
    retry = "--retry" in sys.argv
    out = os.path.join("data", "research_inst_{}.db".format(board.lower()))
    days = trading_days(start, end)
    print("{} trading days {}..{} -> {}".format(len(days), start, end, out))
    n = backfill(out, board, days, sleep=(0.5 if board == "OTC" else 1.0), retry_failed=retry)
    print("fetched {} day(s)".format(n))


if __name__ == "__main__":
    main()
