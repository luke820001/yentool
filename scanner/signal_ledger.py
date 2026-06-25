"""
Forward-performance ledger for the scanner. ASCII only.

The scanner used to be open-loop: it emitted a shortlist and forgot it (the CSV
export overwrites itself every run). This module makes every scan accumulate.

Two tables in a single append-only SQLite file (config.SIGNAL_LEDGER_FILE):

  picks     one row per (scan_session, scan_mode, stock_id). Captures what was
            recommended and the full feature/score snapshot at that moment.
            Re-running the same mode on the same day REPLACES that day's rows
            (idempotent), so a double-scan does not double-count.

  outcomes  one row per (scan_session, scan_mode, stock_id, horizon_days). The
            realized forward result, backfilled once enough future bars exist in
            price_volume.db. Anchored on each pick's own bar_date so a stale
            quote does not contaminate the return.

Nothing here changes selection logic; it only observes. Failures are swallowed
so a ledger problem can never break a live scan.
"""
import sqlite3
from datetime import datetime, date

import pandas as pd

from config.settings import SIGNAL_LEDGER_FILE, PRICE_VOLUME_FILE
from storage.data_store import load_sheet

# Forward windows measured for every pick (trading bars).
HORIZONS = (5, 10, 20)


def _connect():
    SIGNAL_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SIGNAL_LEDGER_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS picks (
            scan_session    TEXT,
            scan_ts         TEXT,
            scan_mode       TEXT,
            stock_id        TEXT,
            stock_name      TEXT,
            market          TEXT,
            rank            INTEGER,
            bar_date        TEXT,
            close           REAL,
            suggested_buy   REAL,
            stop_loss       REAL,
            risk_pct        REAL,
            launch_score    REAL,
            surge_score     REAL,
            explosion_score REAL,
            PRIMARY KEY (scan_session, scan_mode, stock_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            scan_session   TEXT,
            scan_mode      TEXT,
            stock_id       TEXT,
            horizon_days   INTEGER,
            bar_date       TEXT,
            entry_close    REAL,
            asof_date      TEXT,
            bars           INTEGER,
            fwd_close      REAL,
            fwd_return_pct REAL,
            mfe_pct        REAL,
            mae_pct        REAL,
            PRIMARY KEY (scan_session, scan_mode, stock_id, horizon_days)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_picks_sid ON picks(stock_id, bar_date)"
    )
    # Lightweight forward migration: add columns introduced after a table was
    # first created (CREATE TABLE IF NOT EXISTS never alters an existing table).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(picks)")}
    if "market" not in cols:
        conn.execute("ALTER TABLE picks ADD COLUMN market TEXT")


def _f(row, col):
    """Float-or-None accessor that tolerates missing columns / NaN."""
    if col not in row:
        return None
    v = row[col]
    try:
        if pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def record_picks(df, scan_mode, scan_session=None):
    """
    Append today's shortlist for `scan_mode` to the ledger. Idempotent: the
    same (session, mode) is replaced wholesale, so re-scanning a day is safe.

    `scan_session` defaults to today's wall-clock date (the run that produced
    the list). The per-stock forward anchor is each row's own Data_Date.
    """
    if df is None or df.empty:
        return 0

    now = datetime.now()
    session = scan_session or now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for rank, (_, r) in enumerate(df.iterrows()):
        sid = str(r.get("Stock_ID", "")).strip()
        if not sid:
            continue
        bar_date = str(r.get("Data_Date") or "")[:10] or session
        rows.append((
            session, ts, scan_mode, sid,
            str(r.get("Stock_Name", "")),
            str(r.get("Market", "TSE")),
            rank,
            bar_date,
            _f(r, "Close_Price"),
            _f(r, "Suggested_Buy_Price"),
            _f(r, "Strict_Stop_Loss"),
            _f(r, "Risk_Pct"),
            _f(r, "Launch_Score"),
            _f(r, "Surge_Score"),
            _f(r, "Explosion_Score"),
        ))

    if not rows:
        return 0

    try:
        with _connect() as conn:
            _ensure_schema(conn)
            conn.execute(
                "DELETE FROM picks WHERE scan_session = ? AND scan_mode = ?",
                (session, scan_mode),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO picks "
                "(scan_session, scan_ts, scan_mode, stock_id, stock_name, "
                " market, rank, bar_date, close, suggested_buy, stop_loss, "
                " risk_pct, launch_score, surge_score, explosion_score) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)
    except Exception as e:
        print("  [ledger] record failed: {}".format(e))
        return 0


def _calendar_mature(bar_date, horizon):
    """
    True when enough wall-clock time has passed since bar_date that `horizon`
    trading bars SHOULD already exist. Trading->calendar is ~5/7, so horizon
    bars span ~horizon*1.4 calendar days; a 5-day buffer covers holidays. Used
    only to decide whether a missing-bar pick is worth a network re-fetch (vs
    simply too recent to have matured yet).
    """
    try:
        d = datetime.strptime(str(bar_date)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return (date.today() - d).days >= int(horizon * 1.4) + 5


def _fill_from_db(by_stock):
    """
    Compute outcome rows from whatever price_volume.db currently holds.

    Returns (new_rows, stragglers) where `stragglers` is the set of stock_ids
    that have a pick whose window SHOULD be mature by the calendar but whose
    forward bars are missing from the db -- i.e. names that fell out of the
    scan universe and stopped getting price updates. The caller re-fetches them.
    """
    new_rows = []
    stragglers = set()
    for sid, jobs in by_stock.items():
        series = load_sheet(PRICE_VOLUME_FILE, sid)
        have = not series.empty and "date" in series.columns
        dates = closes = highs = lows = []
        if have:
            series = series.copy()
            series["date"] = pd.to_datetime(
                series["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            series = series.dropna(subset=["date"]).sort_values("date")
            dates = series["date"].tolist()
            closes = pd.to_numeric(series["close"], errors="coerce").tolist()
            highs = pd.to_numeric(
                series.get("high", series["close"]), errors="coerce").tolist()
            lows = pd.to_numeric(
                series.get("low", series["close"]), errors="coerce").tolist()

        for session, mode, bar_date, entry, need in jobs:
            if entry <= 0:
                continue
            start = None
            if have:
                start = next((i for i, d in enumerate(dates) if d > bar_date),
                             None)
            fwd_close = closes[start:] if start is not None else []
            fwd_high = highs[start:] if start is not None else []
            fwd_low = lows[start:] if start is not None else []
            fwd_date = dates[start:] if start is not None else []

            # Anchor the entry on the SAME series the forward bars come from, so
            # both share one adjustment basis. yfinance auto_adjust rescales all
            # history on every ex-dividend; the snapshot close stored at scan
            # time is on the old basis, so using it would skew any return that
            # straddles an ex-date by the dividend. Fall back to the snapshot
            # close only when the entry bar is not in the series.
            entry_used = entry
            if start is not None and start > 0 and dates[start - 1] == bar_date:
                c0 = closes[start - 1]
                if pd.notna(c0) and float(c0) > 0:
                    entry_used = float(c0)

            for h in need:
                if len(fwd_close) < h:
                    # not enough forward bars: re-fetch only if it should exist
                    if _calendar_mature(bar_date, h):
                        stragglers.add(sid)
                    continue
                win_h = fwd_high[:h]
                win_l = fwd_low[:h]
                last_c = fwd_close[h - 1]
                new_rows.append((
                    session, mode, sid, h, bar_date, round(entry_used, 2),
                    fwd_date[h - 1], h,
                    round(last_c, 2),
                    round((last_c / entry_used - 1.0) * 100, 2),
                    round((max(win_h) / entry_used - 1.0) * 100, 2),
                    round((min(win_l) / entry_used - 1.0) * 100, 2),
                ))
    return new_rows, stragglers


def _refetch(stock_ids, market_of):
    """Top up price_volume.db for stragglers. Returns the set actually fetched."""
    if not stock_ids:
        return set()
    try:
        from ingestion.price_volume_multi import multi_fetch_and_save_batch
    except Exception:
        return set()
    ids = list(stock_ids)
    tmap = {sid: (market_of.get(sid) or "TSE") for sid in ids}
    try:
        print("  [ledger] fetching {} matured stragglers to complete outcomes".format(
            len(ids)))
        return multi_fetch_and_save_batch(ids, tmap)
    except Exception as e:
        print("  [ledger] straggler refetch failed: {}".format(e))
        return set()


def backfill_outcomes(horizons=HORIZONS, allow_fetch=True):
    """
    Fill realized forward returns (close-to-close, plus max favorable / adverse
    excursion) for every pick whose window has matured. A horizon is written
    only once its FULL window exists, so partial-window noise is never stored.

    Self-completing: a pick that left the scan universe stops getting price
    updates, so its forward bars can be missing. When `allow_fetch` is True (the
    default, i.e. a normal scan) such matured-but-missing names are re-fetched
    and filled in the same pass -- pressing Scan is enough, no manual step.

    Returns the number of (pick, horizon) outcome rows written.
    """
    written = 0
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            picks = conn.execute(
                "SELECT scan_session, scan_mode, stock_id, market, bar_date, close "
                "FROM picks"
            ).fetchall()
            done = set(conn.execute(
                "SELECT scan_session, scan_mode, stock_id, horizon_days "
                "FROM outcomes"
            ).fetchall())

            # Group pending picks by stock so each price series loads once.
            by_stock = {}
            market_of = {}
            for session, mode, sid, market, bar_date, close in picks:
                need = [h for h in horizons
                        if (session, mode, sid, h) not in done]
                if need and bar_date and close:
                    by_stock.setdefault(sid, []).append(
                        (session, mode, bar_date, float(close), need))
                    market_of[sid] = market

            # Pass 1: fill from the data already on disk.
            new_rows, stragglers = _fill_from_db(by_stock)

            # Pass 2: top up names that should have matured but stopped updating,
            # then fill just those. Skipped when allow_fetch is False (offline).
            if allow_fetch and stragglers:
                fetched = _refetch(stragglers, market_of)
                if fetched:
                    sub = {sid: by_stock[sid] for sid in fetched
                           if sid in by_stock}
                    more, _ = _fill_from_db(sub)
                    new_rows.extend(more)

            if new_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO outcomes VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    new_rows,
                )
                written = len(new_rows)
    except Exception as e:
        print("  [ledger] backfill failed: {}".format(e))
    return written


def load_picks():
    """Whole picks table as a DataFrame (empty if the ledger does not exist)."""
    if not SIGNAL_LEDGER_FILE.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(SIGNAL_LEDGER_FILE) as conn:
            return pd.read_sql_query("SELECT * FROM picks", conn)
    except Exception:
        return pd.DataFrame()


def load_outcomes():
    """Whole outcomes table as a DataFrame (empty if none yet)."""
    if not SIGNAL_LEDGER_FILE.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(SIGNAL_LEDGER_FILE) as conn:
            return pd.read_sql_query("SELECT * FROM outcomes", conn)
    except Exception:
        return pd.DataFrame()
