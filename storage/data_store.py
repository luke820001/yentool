import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, date as date_cls
from config.settings import ROLLING_DAYS

# Files where sheet_name argument = stock_id (one logical "sheet" per stock).
# All other files treat sheet_name as the SQLite table name.
_STOCK_KEYED_STEMS = {"price_volume", "large_holder", "broker_branch"}


def _is_stock_keyed(file_path: Path) -> bool:
    return file_path.stem in _STOCK_KEYED_STEMS


def _get_cutoff_date() -> str:
    cutoff = datetime.today() - timedelta(days=ROLLING_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def _ensure_index(conn: sqlite3.Connection) -> None:
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


# ── public write API ──────────────────────────────────────────────────────────

def save_sheet(df: pd.DataFrame, file_path: Path, sheet_name: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(file_path, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # allows concurrent readers during write
        if _is_stock_keyed(file_path):
            try:
                conn.execute("DELETE FROM data WHERE stock_id = ?", (sheet_name,))
            except Exception:
                pass
            df.to_sql("data", conn, if_exists="append", index=False)
            _ensure_index(conn)
        else:
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
