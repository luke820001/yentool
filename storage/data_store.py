import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, date as date_cls
from config.settings import ROLLING_DAYS

# Files where sheet_name argument = stock_id (one logical "sheet" per stock).
# All other files treat sheet_name as the SQLite table name.
_STOCK_KEYED_STEMS = {"price_volume", "large_holder", "broker_branch"}

# Schema revision of a stock-keyed store, kept in PRAGMA user_version (the
# header field SQLite reserves for exactly this -- no extra table, and the
# stamp is written inside the same transaction as the change it describes).
#   0  pre-versioning. Table created by pandas to_sql: no primary key, no
#      uniqueness, only the non-unique idx_data_sid_date lookup index. Every
#      store written before 2026-09-09 is this.
#   2  UNIQUE(stock_id, date) enforced by ux_data_sid_date.
# Version 1 was never shipped; the number is skipped so a "1" found in the wild
# can only mean a hand-edited file. New stores are born at SCHEMA_VERSION;
# existing ones are upgraded only by migrate_stock_store(), never by a scan.
SCHEMA_VERSION = 2

# pandas dtype kind -> SQLite declared type. Same mapping to_sql used, so a
# table created here is indistinguishable from one it created.
_SQL_TYPE = {"i": "INTEGER", "u": "INTEGER", "b": "INTEGER", "f": "REAL"}


def _is_stock_keyed(file_path: Path) -> bool:
    return file_path.stem in _STOCK_KEYED_STEMS


def _get_cutoff_date() -> str:
    cutoff = datetime.today() - timedelta(days=ROLLING_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute("PRAGMA table_info([{}])".format(table))]


def _sql_decl(dtype) -> str:
    return _SQL_TYPE.get(getattr(dtype, "kind", "O"), "TEXT")


def _create_table_like(conn: sqlite3.Connection, table: str,
                       df: pd.DataFrame) -> None:
    """CREATE TABLE with the frame's columns -- what to_sql did implicitly."""
    cols = ", ".join("[{}] {}".format(c, _sql_decl(df[c].dtype))
                     for c in df.columns)
    conn.execute("CREATE TABLE IF NOT EXISTS [{}] ({})".format(table, cols))


def _add_missing_columns(conn: sqlite3.Connection, table: str,
                         df: pd.DataFrame, known: list) -> bool:
    """Widen `table` for columns the frame has and it does not. Returns True if
    anything was added.

    to_sql(append) raises on an unknown column, which would fail a whole 200
    stock batch because one new indicator appeared upstream. Widening keeps the
    data instead: same lightweight forward migration signal_ledger._ensure_schema
    uses. A column is only ever ADDED, so nothing already stored can be lost.
    """
    added = False
    have = set(known)
    for c in df.columns:
        if c not in have:
            conn.execute("ALTER TABLE [{}] ADD COLUMN [{}] {}".format(
                table, c, _sql_decl(df[c].dtype)))
            added = True
    return added


def _bindable(v):
    """Coerce one pandas cell into something sqlite3 will bind.

    NaN/NaT must become real NULLs (a stored float nan compares equal to
    nothing and breaks MAX(date)), and numpy scalars are not bindable at all --
    sqlite3 rejects numpy.int64. to_sql did this internally; doing our own
    INSERTs means doing it here.
    """
    if v is None or v != v:          # None, NaN, NaT (never equal to itself)
        return None
    if isinstance(v, (int, float, str, bytes)):   # bool is an int subclass
        return v
    item = getattr(v, "item", None)               # numpy scalar -> python
    if item is not None:
        try:
            return item()
        except Exception:
            pass
    return str(v)


def _rows_for_insert(df: pd.DataFrame, cols: list) -> list:
    """Frame -> list of bindable tuples, column-wise (tolist() converts numpy
    scalars to Python ones in one C-level pass instead of per cell)."""
    columns = [df[c].tolist() for c in cols]
    return [tuple(_bindable(col[i]) for col in columns)
            for i in range(len(df))]


def _ensure_data_schema(conn: sqlite3.Connection, fresh: bool = False) -> None:
    """Index + schema stamp for a stock-keyed `data` table.

    A brand-new store gets the real uniqueness constraint on the natural key
    (stock_id, date) and the version stamp. An EXISTING store gets only the
    plain lookup index it already had, deliberately: building a UNIQUE index on
    the live 87MB price file fails the moment it holds a single duplicate bar,
    and turning a routine price save into a surprise migration is exactly what
    must not happen to data the owner cannot refetch quickly. Upgrading an
    existing store is a deliberate step -- see migrate_stock_store().
    """
    if fresh:
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_data_sid_date "
                         "ON data(stock_id, date)")
            conn.execute("PRAGMA user_version = {}".format(int(SCHEMA_VERSION)))
            return
        except Exception:
            pass    # fall through to the plain index; never fail a write
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_data_sid_date ON data(stock_id, date)"
        )
    except Exception:
        pass


# ── public read API ───────────────────────────────────────────────────────────

def load_sheet(file_path: Path, sheet_name: str) -> pd.DataFrame:
    if not file_path.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(file_path) as conn:
            if _is_stock_keyed(file_path):
                return pd.read_sql_query(
                    "SELECT * FROM data WHERE stock_id = ?",
                    conn, params=(sheet_name,),
                )
            else:
                return pd.read_sql_query(
                    "SELECT * FROM [{}]".format(sheet_name), conn
                )
    except Exception:
        return pd.DataFrame()


def get_latest_date(file_path: Path, stock_id: str):
    """
    Return the most recent date string for stock_id (index scan only — fast).
    Returns None when no data exists.
    """
    if not file_path.exists():
        return None
    try:
        with sqlite3.connect(file_path) as conn:
            cur = conn.execute(
                "SELECT MAX(date) FROM data WHERE stock_id = ?", (stock_id,)
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        return None


def max_stored_date(file_path: Path, table: str = "data"):
    """
    Newest date held in a store, as 'YYYY-MM-DD' (None when there is none).

    Not per stock: this is "how fresh is this database as a whole". market_regime
    uses it as the reference point for "is the TAIEX cache at least as fresh as
    the stock data we are about to scan" (F15).
    """
    if not Path(file_path).exists():
        return None
    try:
        with sqlite3.connect(file_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM [{}]".format(table)).fetchone()
        if row and row[0]:
            return str(row[0])[:10]
    except Exception:
        return None
    return None


# ── public write API ──────────────────────────────────────────────────────────

def _replace_stock_rows(conn: sqlite3.Connection, table: str, sid: str,
                        df: pd.DataFrame, table_cols: list) -> int:
    """DELETE + INSERT one stock's rows inside the CALLER's transaction.

    An empty frame still deletes: that is how the rolling-window trim drops a
    stock whose whole history aged out. Refuses a frame with no stock_id rather
    than deleting the real rows and inserting NULL-keyed ones in their place.
    """
    conn.execute("DELETE FROM [{}] WHERE stock_id = ?".format(table), (sid,))
    if df is None or df.empty:
        return 0
    if "stock_id" not in df.columns:
        raise ValueError(
            "stock-keyed write for {} has no stock_id column".format(sid))
    known = set(table_cols)
    cols = [c for c in df.columns if c in known]
    conn.executemany(
        "INSERT INTO [{}] ({}) VALUES ({})".format(
            table,
            ",".join("[{}]".format(c) for c in cols),
            ",".join("?" for _ in cols),
        ),
        _rows_for_insert(df, cols),
    )
    return len(df)


def _write_stock_frames(file_path: Path, prepared: list) -> int:
    """Replace each (stock_id, frame) of `prepared` in ONE transaction.

    Returns the rows written. Raises after a real ROLLBACK if any stock fails,
    so the batch is all-or-nothing and a partial write cannot be mistaken for a
    complete one.

    F25 (2026-09-09 audit): the old path was `conn.execute(DELETE)` +
    `frame.to_sql(append)` per stock inside a `with sqlite3.connect(...)`. That
    block cannot roll the batch back -- pandas' SQLiteDatabase.run_transaction
    COMMITS inside every to_sql call, and pandas documents that on a plain
    sqlite3 connection its inserts do not roll back as callers expect
    (https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.to_sql.html).
    A failure on stock 180 of 200 therefore left 179 stocks committed while the
    caller was told the write had failed. Explicit parameterised INSERTs inside
    one BEGIN IMMEDIATE make the promise true.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(file_path, timeout=30)
    written = 0
    try:
        # Manual transaction control. sqlite3's legacy mode opens and closes
        # transactions on its own around DML, which is not good enough when the
        # unit of work is "all of these stocks, or none of them".
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")   # cannot run inside a txn
        fresh = not _table_exists(conn, "data")
        conn.execute("BEGIN IMMEDIATE")
        try:
            have_table = not fresh
            table_cols = _table_columns(conn, "data") if have_table else []
            for sid, frame in prepared:
                empty = frame is None or frame.empty
                if not have_table:
                    if empty:
                        continue      # nothing to derive a schema from yet
                    _create_table_like(conn, "data", frame)
                    have_table = True
                    table_cols = _table_columns(conn, "data")
                elif not empty and _add_missing_columns(
                        conn, "data", frame, table_cols):
                    table_cols = _table_columns(conn, "data")
                written += _replace_stock_rows(
                    conn, "data", sid, frame, table_cols)
            if have_table:
                _ensure_data_schema(conn, fresh=fresh)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()
    return written


def save_sheet(df: pd.DataFrame, file_path: Path, sheet_name: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_stock_keyed(file_path):
        # sheet_name IS the stock_id for these files; one stock, one txn.
        _write_stock_frames(file_path, [(sheet_name, df)])
        return
    with sqlite3.connect(file_path, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # allows concurrent readers during write
        df.to_sql(sheet_name, conn, if_exists="replace", index=False)


def apply_rolling_window(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    if df.empty or date_col not in df.columns:
        return df
    cutoff = _get_cutoff_date()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df[df[date_col] >= cutoff].copy()
    df[date_col] = df[date_col].dt.strftime("%Y-%m-%d")
    return df


def upsert_and_trim(
    file_path: Path,
    sheet_name: str,
    new_df: pd.DataFrame,
    date_col: str,
    key_cols: list,
) -> pd.DataFrame:
    existing = load_sheet(file_path, sheet_name)
    if existing.empty:
        combined = new_df.copy()
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)
    # Deduplicate on the natural key unconditionally. The first-write branch
    # used to skip it, so a source that repeated a bar stored it twice and
    # nothing downstream could tell -- and it is now also what keeps a fresh
    # store (which carries UNIQUE(stock_id, date), see SCHEMA_VERSION) writable.
    combined = combined.drop_duplicates(subset=key_cols, keep="last")
    combined = apply_rolling_window(combined, date_col)
    combined = combined.sort_values(by=key_cols).reset_index(drop=True)
    save_sheet(combined, file_path, sheet_name)
    return combined


# ── batch helpers (replace per-stock queries with single round-trips) ────────

def batch_latest_dates(file_path: Path, stock_ids: list) -> dict:
    """
    One query: return {stock_id: age_days} for every stock_id that has data.
    Stocks with no rows are absent from the result (caller treats as None).
    """
    if not file_path.exists() or not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    today = date_cls.today()
    result = {}
    try:
        with sqlite3.connect(file_path) as conn:
            rows = conn.execute(
                "SELECT stock_id, MAX(date) FROM data "
                "WHERE stock_id IN ({}) GROUP BY stock_id".format(placeholders),
                stock_ids,
            ).fetchall()
        for sid, max_date in rows:
            if max_date:
                try:
                    d = datetime.strptime(max_date[:10], "%Y-%m-%d").date()
                    result[sid] = (today - d).days
                except Exception:
                    pass
    except Exception:
        pass
    return result


def batch_latest_date_strings(file_path: Path, stock_ids: list) -> dict:
    """
    One query: return {stock_id: 'YYYY-MM-DD'} (the latest stored date) for every
    stock that has data. Used to detect staleness by comparing against the latest
    trading day, rather than a fixed age threshold.
    """
    if not file_path.exists() or not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    result = {}
    try:
        with sqlite3.connect(file_path) as conn:
            rows = conn.execute(
                "SELECT stock_id, MAX(date) FROM data "
                "WHERE stock_id IN ({}) GROUP BY stock_id".format(placeholders),
                stock_ids,
            ).fetchall()
        for sid, max_date in rows:
            if max_date:
                result[sid] = str(max_date)[:10]
    except Exception:
        pass
    return result


def batch_row_counts(file_path: Path, stock_ids: list) -> dict:
    """
    One query: return {stock_id: bar_count} for every stock_id that has data.
    Stocks with no rows are absent (caller treats as 0). Used to detect
    stocks whose stored history is too short for long-window indicators.
    """
    if not file_path.exists() or not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    result = {}
    try:
        with sqlite3.connect(file_path) as conn:
            rows = conn.execute(
                "SELECT stock_id, COUNT(*) FROM data "
                "WHERE stock_id IN ({}) GROUP BY stock_id".format(placeholders),
                stock_ids,
            ).fetchall()
        for sid, cnt in rows:
            result[sid] = int(cnt)
    except Exception:
        pass
    return result


def bulk_upsert_stocks(
    file_path: Path,
    frames: dict,
    date_col: str = "date",
    key_cols: list = None,
) -> int:
    """
    Upsert many stock frames for a stock-keyed file as ONE atomic transaction.

    frames : {stock_id: new_df}
    Returns the number of rows written; raises (after a rollback that really
    rolls back -- see _write_stock_frames) if any stock fails, leaving the file
    exactly as it was. Replaces the per-stock upsert_and_trim loop (which opened
    2 connections per stock) so a 200-stock fetch costs a single read query and
    a single write txn.
    """
    if not frames:
        return 0
    if key_cols is None:
        key_cols = ["date", "stock_id"]

    sids = list(frames.keys())
    existing_map = bulk_load_stocks(file_path, sids)   # one query for all
    cutoff = _get_cutoff_date()

    # Reshape EVERY frame before the write transaction opens. A pandas error on
    # stock 180 must not be able to abort a half-applied batch: doing all the
    # fallible work first keeps the transaction to pure SQL.
    prepared = []
    for sid, new_df in frames.items():
        if new_df is None or new_df.empty:
            continue
        existing = existing_map.get(sid, pd.DataFrame())
        if existing.empty:
            combined = new_df.copy()
        else:
            combined = pd.concat([existing, new_df], ignore_index=True)
        # Unconditional (the first-write branch above used to skip it, which is
        # how duplicate bars got in); (stock_id, date) is the natural key.
        combined = combined.drop_duplicates(subset=key_cols, keep="last")

        combined[date_col] = pd.to_datetime(combined[date_col], errors="coerce")
        combined = combined[combined[date_col] >= cutoff].copy()
        combined[date_col] = combined[date_col].dt.strftime("%Y-%m-%d")
        combined = combined.sort_values(by=key_cols).reset_index(drop=True)
        prepared.append((sid, combined))

    if not prepared:
        return 0
    return _write_stock_frames(file_path, prepared)


# ── schema version / migration (deliberate, never run by a scan) ─────────────

def get_schema_version(file_path: Path) -> int:
    """PRAGMA user_version of a store. 0 = pre-versioning (see SCHEMA_VERSION).

    Read-only and safe against a live file.
    """
    if not Path(file_path).exists():
        return 0
    try:
        with sqlite3.connect(file_path) as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def migrate_stock_store(file_path: Path, dedupe: bool = False,
                        backup: bool = True) -> dict:
    """
    Bring an existing stock-keyed store up to SCHEMA_VERSION. DELIBERATE step:
    nothing in the scan path calls this, because a routine price save must never
    turn into a migration of data the owner cannot refetch quickly.

    Returns a report dict -- never a bare bool -- so the operator can see what
    happened before and after:
      file, version_before, version_after, rows, duplicate_keys,
      duplicate_rows, unique_index, deduped, backup, note

    Order of operations, chosen so the live price database cannot be damaged:
      1. Count the duplicate (stock_id, date) keys. Read-only.
      2. If there are any and dedupe is False, STOP and report. Which of two
         copies of a bar is right is a data question, and this function will not
         answer it by deleting price history behind the owner's back.
      3. With dedupe=True, copy the whole file to <name>.pre-v{N}.bak first --
         via sqlite3's own backup API, so the copy is consistent even though the
         store runs in WAL mode and a plain file copy would miss the -wal.
      4. Then, in ONE transaction: drop the losing rows (highest rowid wins,
         i.e. the most recently written copy of that bar), create the UNIQUE
         index, drop the now-redundant non-unique one, stamp user_version.
    """
    file_path = Path(file_path)
    report = {"file": str(file_path), "version_before": None,
              "version_after": None, "rows": 0, "duplicate_keys": 0,
              "duplicate_rows": 0, "unique_index": False, "deduped": False,
              "backup": None, "note": ""}
    if not file_path.exists():
        report["note"] = "file does not exist"
        return report

    conn = sqlite3.connect(file_path, timeout=60)
    try:
        conn.isolation_level = None
        before = conn.execute("PRAGMA user_version").fetchone()[0]
        report["version_before"] = int(before)
        report["version_after"] = int(before)
        if not _table_exists(conn, "data"):
            report["note"] = "no data table: not a stock-keyed store"
            return report

        report["rows"] = int(
            conn.execute("SELECT COUNT(*) FROM data").fetchone()[0])
        dup_keys, dup_rows = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(n), 0) FROM ("
            "  SELECT COUNT(*) AS n FROM data GROUP BY stock_id, date"
            "  HAVING n > 1)"
        ).fetchone()
        report["duplicate_keys"] = int(dup_keys or 0)
        # Rows that would have to go: every copy of a duplicated key but one.
        report["duplicate_rows"] = int(dup_rows or 0) - int(dup_keys or 0)

        if report["duplicate_keys"] and not dedupe:
            report["note"] = (
                "{} duplicate (stock_id, date) keys hold the UNIQUE index back; "
                "inspect them, then re-run with dedupe=True to keep the newest "
                "row of each".format(report["duplicate_keys"]))
            return report

        if report["duplicate_keys"] and backup:
            bak = Path(str(file_path) + ".pre-v{}.bak".format(SCHEMA_VERSION))
            dst = sqlite3.connect(bak)
            try:
                conn.backup(dst)
            finally:
                dst.close()
            report["backup"] = str(bak)

        conn.execute("BEGIN IMMEDIATE")
        try:
            if report["duplicate_keys"]:
                conn.execute(
                    "DELETE FROM data WHERE rowid NOT IN "
                    "(SELECT MAX(rowid) FROM data GROUP BY stock_id, date)")
                report["deduped"] = True
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_data_sid_date "
                         "ON data(stock_id, date)")
            # Redundant once the unique index covers the same two columns, in
            # the same order. Dropping an index destroys no data and it can be
            # recreated from _ensure_data_schema at any time.
            conn.execute("DROP INDEX IF EXISTS idx_data_sid_date")
            conn.execute("PRAGMA user_version = {}".format(int(SCHEMA_VERSION)))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        report["unique_index"] = True
        report["version_after"] = int(
            conn.execute("PRAGMA user_version").fetchone()[0])
        report["note"] = "migrated"
    finally:
        conn.close()
    return report


def bulk_load_stocks(file_path: Path, stock_ids: list) -> dict:
    """
    One query: return {stock_id: DataFrame} for every stock_id.
    """
    if not file_path.exists() or not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    try:
        with sqlite3.connect(file_path) as conn:
            df = pd.read_sql_query(
                "SELECT * FROM data WHERE stock_id IN ({})".format(placeholders),
                conn,
                params=stock_ids,
            )
        if df.empty:
            return {}
        return {sid: grp.reset_index(drop=True) for sid, grp in df.groupby("stock_id")}
    except Exception:
        return {}


# ── backward-compat stubs (no-ops) ───────────────────────────────────────────

def warm_workbook_cache(file_path: Path) -> None:
    pass


def invalidate_workbook_cache(file_path: Path) -> None:
    pass
