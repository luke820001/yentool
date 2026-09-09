"""
Portfolio ledger schema. sqlite3 + Decimal-as-TEXT, no third-party imports.

Why a new database rather than more columns on signal_ledger.db: the existing
ledger answers "what did the scanner say, and what happened next". This one
answers "what did the user actually own, and what is it worth today". Report
section 5.2 is the whole reason they cannot be one table -- a recommendation
and a position have different lifecycles, and collapsing them is exactly the
bug that let Entry_Open masquerade as a real fill (F03).

Design rules taken from the report:

  * Amounts are TEXT holding a Decimal (section 9.1). REAL would round-trip
    through float and undo portfolio/money.py.
  * Every timestamp is Asia/Taipei and we keep FOUR distinct times: the session
    the data belongs to, when we obtained it, when it takes effect, and when the
    row was written (section 9.1).
  * Nothing is destructively updated. A correction writes a new revision and
    demotes the old one (is_current), so "why did yesterday's number change" is
    always answerable (section 9.1, 11.6).
  * A first-day recommendation is immutable once written (section 5.1, F01).
    The schema enforces it with a uniqueness constraint, not a convention.

Migrations are explicit and versioned. Report section 11.2: "add schema_version,
migrate version by version, and do not let an arbitrary read function quietly
create or alter a table." So ensure_schema() only ever runs numbered steps.
"""
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# One statement per entry so a migration can replay a subset.
_V1 = [
    # -- provenance -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # Official market calendar. Report F14: the phone was inferring trading days
    # from Mon-Fri and treating "no data" as a holiday. A closure is a fact that
    # has to be recorded, not guessed, and it must support FUTURE dates so an
    # exit date can be planned.
    """
    CREATE TABLE IF NOT EXISTS trading_sessions (
        market         TEXT NOT NULL,
        session_date   TEXT NOT NULL,
        is_open        INTEGER NOT NULL,
        closure_reason TEXT,
        source         TEXT,
        confirmed_at   TEXT,
        PRIMARY KEY (market, session_date)
    )
    """,
    # -- the advice side of the world ----------------------------------------
    # One row per (stock, strategy, cycle). Written the FIRST time a name passes
    # the complete buy gate and never rewritten (F01). Re-running the same scan
    # ten times a day must return the same recommendation_id with the same
    # price -- that is the acceptance case in report section 12.
    """
    CREATE TABLE IF NOT EXISTS recommendations (
        recommendation_id       TEXT PRIMARY KEY,
        stock_id                TEXT NOT NULL,
        stock_name              TEXT,
        market                  TEXT,
        strategy                TEXT NOT NULL,
        strategy_version        TEXT NOT NULL,
        cycle_seq               INTEGER NOT NULL DEFAULT 1,
        first_qualified_session TEXT NOT NULL,
        recommended_at          TEXT NOT NULL,
        initial_buy_price       TEXT NOT NULL,
        initial_stop_price      TEXT,
        initial_target_price    TEXT,
        trail_arm_price         TEXT,
        trail_lock_price        TEXT,
        valid_until_session     TEXT,
        horizon_days            INTEGER NOT NULL DEFAULT 10,
        gate_snapshot           TEXT,
        status                  TEXT NOT NULL DEFAULT 'active',
        UNIQUE (stock_id, strategy, cycle_seq)
    )
    """,
    # Append-only. Expiry, correction, withdrawal, a new cycle, a changed exit
    # plan -- all events, never an UPDATE on the row above (section 5.2: "an
    # exit condition that was met and later cancelled must still be preserved").
    """
    CREATE TABLE IF NOT EXISTS recommendation_events (
        event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        recommendation_id TEXT NOT NULL,
        event_type        TEXT NOT NULL,
        effective_session TEXT,
        recorded_at       TEXT NOT NULL,
        reason_code       TEXT,
        payload           TEXT,
        FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
    )
    """,
    # -- the ownership side of the world -------------------------------------
    # A position is a CONTAINER. Its numbers are derived from executions, never
    # typed in directly -- that is what stops a scan from inventing a fill.
    """
    CREATE TABLE IF NOT EXISTS positions (
        position_id       TEXT PRIMARY KEY,
        account_id        TEXT NOT NULL DEFAULT 'default',
        recommendation_id TEXT,
        stock_id          TEXT NOT NULL,
        stock_name        TEXT,
        market            TEXT,
        strategy          TEXT,
        strategy_version  TEXT,
        fee_schedule      TEXT NOT NULL,
        horizon_days      INTEGER NOT NULL DEFAULT 10,
        opened_session    TEXT,
        closed_session    TEXT,
        status            TEXT NOT NULL DEFAULT 'open',
        origin            TEXT NOT NULL DEFAULT 'recommended',
        open_shares       INTEGER NOT NULL DEFAULT 0,
        avg_cost          TEXT NOT NULL DEFAULT '0',
        cost_basis        TEXT NOT NULL DEFAULT '0',
        gross_cost        TEXT NOT NULL DEFAULT '0',
        realized_net      TEXT NOT NULL DEFAULT '0',
        realized_gross    TEXT NOT NULL DEFAULT '0',
        dividends         TEXT NOT NULL DEFAULT '0',
        initial_stop      TEXT,
        target_price      TEXT,
        active_stop       TEXT,
        trail_armed_at    TEXT,
        highest_close     TEXT,
        note              TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id)
    )
    """,
    # The single source of truth for what was really traded (F05, F12).
    # idempotency_key is UNIQUE so the phone re-sending a fill after a flaky
    # save cannot create a second trade (report section 12, "重送與並發").
    """
    CREATE TABLE IF NOT EXISTS executions (
        execution_id    TEXT PRIMARY KEY,
        position_id     TEXT NOT NULL,
        side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        session_date    TEXT NOT NULL,
        executed_at     TEXT,
        shares          INTEGER NOT NULL CHECK (shares > 0),
        price           TEXT NOT NULL,
        fee             TEXT NOT NULL DEFAULT '0',
        tax             TEXT NOT NULL DEFAULT '0',
        fee_schedule    TEXT,
        note            TEXT,
        revision_of     TEXT,
        superseded_by   TEXT,
        is_current      INTEGER NOT NULL DEFAULT 1,
        idempotency_key TEXT UNIQUE,
        recorded_at     TEXT NOT NULL,
        FOREIGN KEY (position_id) REFERENCES positions(position_id)
    )
    """,
    # One valuation per position per session. Report F19: outcomes only existed
    # at 5/10/20 days, so "what was my P&L on day 4" had no answer at all.
    # revision + is_current means a late price correction restates the day
    # instead of erasing it.
    """
    CREATE TABLE IF NOT EXISTS position_daily_marks (
        position_id      TEXT NOT NULL,
        session_date     TEXT NOT NULL,
        revision         INTEGER NOT NULL DEFAULT 1,
        day_index        INTEGER,
        close_price      TEXT,
        price_source     TEXT,
        price_basis      TEXT NOT NULL DEFAULT 'raw',
        open_shares      INTEGER NOT NULL,
        cost_basis       TEXT NOT NULL,
        market_value     TEXT NOT NULL,
        unrealized_book  TEXT NOT NULL,
        unrealized_gross TEXT NOT NULL,
        realized_net     TEXT NOT NULL,
        total_book       TEXT NOT NULL,
        total_gross      TEXT NOT NULL,
        day_pnl_gross    TEXT,
        day_pnl_book     TEXT,
        net_if_liquidated TEXT,
        data_status      TEXT NOT NULL DEFAULT 'current',
        is_current       INTEGER NOT NULL DEFAULT 1,
        recorded_at      TEXT NOT NULL,
        PRIMARY KEY (position_id, session_date, revision),
        FOREIGN KEY (position_id) REFERENCES positions(position_id)
    )
    """,
    # Stop / trail / time-exit state as EVENTS, so "the stop moved and here is
    # why and from when" survives (report section 5.4). A protective stop only
    # ratchets up; the ledger keeps previous_stop so that is auditable.
    """
    CREATE TABLE IF NOT EXISTS position_rule_events (
        event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id    TEXT NOT NULL,
        session_date   TEXT NOT NULL,
        rule_version   TEXT,
        event_type     TEXT NOT NULL,
        previous_stop  TEXT,
        active_stop    TEXT,
        reason_code    TEXT,
        effective_from TEXT,
        recorded_at    TEXT NOT NULL,
        FOREIGN KEY (position_id) REFERENCES positions(position_id)
    )
    """,
    # The frozen D10 result. Report section 5.3: "day 10's outcome is fixed and
    # must not drift"; D11 prices keep updating the POSITION but can never
    # rewrite this row.
    """
    CREATE TABLE IF NOT EXISTS cycle_results (
        cycle_id          TEXT PRIMARY KEY,
        position_id       TEXT,
        recommendation_id TEXT,
        horizon_days      INTEGER NOT NULL,
        basis             TEXT NOT NULL,
        session_date      TEXT NOT NULL,
        day_index         INTEGER NOT NULL,
        close_price       TEXT,
        open_shares       INTEGER,
        cost_basis        TEXT,
        total_gross       TEXT,
        total_book        TEXT,
        net_if_liquidated TEXT,
        return_vs_initial TEXT,
        return_vs_cost    TEXT,
        still_open        INTEGER NOT NULL DEFAULT 0,
        frozen_at         TEXT NOT NULL
    )
    """,
    # -- indexes --------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS idx_rec_stock ON recommendations(stock_id, strategy, status)",
    "CREATE INDEX IF NOT EXISTS idx_rec_session ON recommendations(first_qualified_session)",
    "CREATE INDEX IF NOT EXISTS idx_pos_stock ON positions(stock_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status, account_id)",
    "CREATE INDEX IF NOT EXISTS idx_exec_pos ON executions(position_id, session_date)",
    "CREATE INDEX IF NOT EXISTS idx_marks_session ON position_daily_marks(session_date, is_current)",
    "CREATE INDEX IF NOT EXISTS idx_marks_pos ON position_daily_marks(position_id, session_date, is_current)",
    "CREATE INDEX IF NOT EXISTS idx_rule_pos ON position_rule_events(position_id, session_date)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cycle_unique ON cycle_results(position_id, horizon_days, basis)",
]

MIGRATIONS = {1: _V1}


def connect(path):
    """Open the ledger with the pragmas this schema assumes.

    foreign_keys is OFF by default in sqlite3 and must be turned on per
    connection -- without it the FK declarations above are decoration.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30,
                           detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def current_version(conn) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if row is None:
        return 0
    got = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(got["value"]) if got else 0


def ensure_schema(conn):
    """Bring the database up to SCHEMA_VERSION, one numbered step at a time.

    Runs inside a single transaction so a half-applied migration cannot leave
    the ledger in a shape no version describes.
    """
    have = current_version(conn)
    if have >= SCHEMA_VERSION:
        return have
    with conn:
        for version in range(have + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return SCHEMA_VERSION


def open_ledger(path):
    """connect() + ensure_schema(). The only entry point callers should use."""
    conn = connect(path)
    ensure_schema(conn)
    return conn
