"""
Forward-performance ledger for the scanner. ASCII only.

The scanner used to be open-loop: it emitted a shortlist and forgot it (the CSV
export overwrites itself every run). This module makes every scan accumulate.

Two tables in a single append-only SQLite file (config.SIGNAL_LEDGER_FILE):

  picks     one row per (scan_session, scan_mode, stock_id). Captures what was
            recommended, the full feature/score snapshot at that moment, and the
            buy DECISION that went with it (core_plus / buy_ready / buy_block /
            gate_detail / rule_version) so a past row can be re-judged instead
            of merely re-read. Re-running the same mode on the same day REPLACES
            that day's rows (idempotent), so a double-scan does not double-count
            -- the stored row is the last scan of that day, the one whose export
            the user actually saw.

  outcomes  one row per (scan_session, scan_mode, stock_id, horizon_days). The
            realized forward result, backfilled once enough future bars exist in
            price_volume.db. Anchored on each pick's own bar_date so a stale
            quote does not contaminate the return.

            TWO measurements per row, because they answer different questions:
              fwd_return_pct  raw signal quality: buy the signal-day close, no
                              stop, no target, hold the full horizon.
              rule_return_pct what the TRADING RULE would have returned: enter
                              at the next bar's OPEN, disaster stop -20%, lock
                              +2% once +6% trades, take profit +20%, else exit
                              on the horizon's close.
            Only the second one describes the strategy. Keeping the first is
            still worth it -- the gap between them is the cost/benefit of the
            exit stack. Over 2026-07-01..08-06 the raw view read 20.6% win /
            -10.13% mean while the rule returned 40.6% / -6.11%, so a ledger
            carrying only the raw number was reporting a strategy nobody
            trades and understating this one by 20 points. NOTE: that 40.6% /
            -6.11% pair was produced by the pre-2026-09-09 _simulate_rule, whose
            same-bar event order was wrong (F09). It is quoted here as the
            reason the second measurement exists, not as a current figure --
            rerun reset_rule_outcomes() + backfill_outcomes() to get one.

Nothing here changes selection logic; it only observes. Failures are swallowed
so a ledger problem can never break a live scan.
"""
import json
import sqlite3
from datetime import datetime, date

import pandas as pd

from config.settings import SIGNAL_LEDGER_FILE, PRICE_VOLUME_FILE
from scanner.scan_mode import (
    PRELAUNCH_STOP_PCT, PRELAUNCH_TP_PCT,
    PRELAUNCH_TRAIL_ARM, PRELAUNCH_TRAIL_LOCK,
)
from storage.data_store import load_sheet
from scanner.exit_rules import simulate_exit

# Forward windows measured for every pick (trading bars).
HORIZONS = (5, 10, 20)

# Modes whose exit stack is validated well enough to simulate in the ledger.
RULE_MODES = ("mode_prelaunch",)

# Fallback buy-rule revision stamped on stored picks when scan_mode does not
# publish one; see _rule_version(). Two incomparable rules must never share one
# label in the ledger, which is the whole point of storing it.
BUY_RULE_VERSION_FALLBACK = "buy_rule-2026-08-06"


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
            core_plus       INTEGER,
            buy_ready       INTEGER,
            buy_block       TEXT,
            gate_detail     TEXT,
            rule_version    TEXT,
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
    # F17: the ledger recorded WHAT was shortlisted but never WHY it was or was
    # not buyable, so a past row cannot be re-judged -- you could not tell a
    # name the rule refused (regime shut, stale bar, already held) from one it
    # bought. These five carry the decision itself plus the rule revision that
    # produced it. Same lightweight forward migration as `market` above.
    for name, decl in (("core_plus", "INTEGER"), ("buy_ready", "INTEGER"),
                       ("buy_block", "TEXT"), ("gate_detail", "TEXT"),
                       ("rule_version", "TEXT")):
        if name not in cols:
            conn.execute("ALTER TABLE picks ADD COLUMN {} {}".format(
                name, decl))
    ocols = {row[1] for row in conn.execute("PRAGMA table_info(outcomes)")}
    for name, decl in (("rule_entry", "REAL"), ("rule_return_pct", "REAL"),
                       ("rule_exit", "TEXT")):
        if name not in ocols:
            conn.execute("ALTER TABLE outcomes ADD COLUMN {} {}".format(
                name, decl))


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


def _b(row, col):
    """Tri-state 1/0/None accessor for a boolean-ish column.

    None is not False here: a column the scan never produced ("we did not
    evaluate this") must stay distinguishable from an evaluated False. NaN maps
    to None for the same reason -- bool(NaN) is True, which is how a missing
    Core_Plus once read as a pass (report section 15).
    """
    if col not in row:
        return None
    v = row[col]
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return 1 if bool(v) else 0


def _s(row, col):
    """Stripped-string-or-None accessor that tolerates missing columns / NaN."""
    if col not in row:
        return None
    v = row[col]
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s or None


def _rule_version():
    """Revision string stamped onto every pick.

    Read from scan_mode.BUY_RULE_VERSION so the stamp cannot drift away from the
    rule it names -- the rule lives there, so the version has to as well. The
    local literal only covers a scan_mode old enough not to publish one, and
    names the rule as it stood at the 2026-08-06 audit (market gate + OTC +
    rank < N_ENTER + Core_Plus + fresh signal).
    """
    try:
        import scanner.scan_mode as _sm
        published = str(getattr(_sm, "BUY_RULE_VERSION", "") or "")
        return published or BUY_RULE_VERSION_FALLBACK
    except Exception:
        return BUY_RULE_VERSION_FALLBACK


def record_picks(df, scan_mode, scan_session=None):
    """
    Append today's shortlist for `scan_mode` to the ledger. Idempotent: the
    same (session, mode) is replaced wholesale, so re-scanning a day is safe.

    `scan_session` defaults to today's wall-clock date (the run that produced
    the list). The per-stock forward anchor is each row's own Data_Date.

    Also stores the buy DECISION, not just the shortlist (F17): core_plus,
    buy_ready, buy_block, a gate_detail JSON of the remaining gate inputs and
    the rule revision that judged them. Those come from mark_buy_ready, which
    the scan runs before this; when it has not run (a mode with no buy rule, or
    an older caller ordering) the columns land as NULL, which reads as "not
    evaluated" rather than "refused".
    """
    if df is None or df.empty:
        return 0

    now = datetime.now()
    session = scan_session or now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    rule_version = _rule_version()

    # The market gate is a property of the RUN, not of a row, and it is the most
    # common single reason a name is not buyable -- capture it once so a stored
    # buy_block == "regime" can still be explained months later. Best effort:
    # the ledger must never be the thing that breaks a scan.
    regime = None
    try:
        from scanner.market_regime import get_market_regime
        r = get_market_regime() or {}
        regime = {k: r.get(k) for k in
                  ("ok", "enter_ok", "is_current", "as_of_date", "ref_date")}
    except Exception:
        regime = None

    rows = []
    for rank, (_, r) in enumerate(df.iterrows()):
        sid = str(r.get("Stock_ID", "")).strip()
        if not sid:
            continue
        bar_date = str(r.get("Data_Date") or "")[:10] or session
        # Everything the gate looked at that has no column of its own. Kept as
        # JSON because the gate's inputs grow over time (Integrity_OK and the
        # per-row freshness check were both added after the table was designed)
        # and a schema change per input would be worse than a blob here.
        gate = {
            "hold_status": _s(r, "Hold_Status"),
            "integrity_ok": _b(r, "Integrity_OK"),
            "regime": regime,
        }
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
            _b(r, "Core_Plus"),
            _b(r, "Buy_Ready"),
            _s(r, "Buy_Block"),
            json.dumps(gate, ensure_ascii=False, sort_keys=True),
            rule_version,
        ))

    if not rows:
        return 0

    try:
        with _connect() as conn:
            _ensure_schema(conn)
            # Delete-then-insert keeps a same-day rescan idempotent (a double
            # scan must not double-count). The decision columns are part of that
            # replacement, so the row always describes the LAST scan of the day
            # -- which is the one whose CSV/JSON the user actually saw.
            conn.execute(
                "DELETE FROM picks WHERE scan_session = ? AND scan_mode = ?",
                (session, scan_mode),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO picks "
                "(scan_session, scan_ts, scan_mode, stock_id, stock_name, "
                " market, rank, bar_date, close, suggested_buy, stop_loss, "
                " risk_pct, launch_score, surge_score, explosion_score, "
                " core_plus, buy_ready, buy_block, gate_detail, rule_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        dates = closes = highs = lows = opens = []
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
            opens = pd.to_numeric(
                series.get("open", series["close"]), errors="coerce").tolist()

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
            fwd_open = opens[start:] if start is not None else []
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
                # What the rule (next-open entry + exit stack) actually made.
                # 'na' rather than NULL for the modes/rows it cannot apply to,
                # so the done-check below can tell "computed, nothing to say"
                # from "never computed" and stops re-simulating them forever.
                r_entry, r_ret, r_why = None, None, "na"
                if mode in RULE_MODES:
                    r_entry, r_ret, r_why = _simulate_rule(
                        fwd_open, fwd_high, fwd_low, fwd_close, h)
                new_rows.append((
                    session, mode, sid, h, bar_date, round(entry_used, 2),
                    fwd_date[h - 1], h,
                    round(last_c, 2),
                    round((last_c / entry_used - 1.0) * 100, 2),
                    round((max(win_h) / entry_used - 1.0) * 100, 2),
                    round((min(win_l) / entry_used - 1.0) * 100, 2),
                    round(r_entry, 2) if r_entry is not None else None,
                    round(r_ret, 2) if r_ret is not None else None,
                    r_why,
                ))
    return new_rows, stragglers


def _simulate_rule(opens, highs, lows, closes, hold):
    """Replay the adopted prelaunch exit stack over `hold` forward bars.

    Returns (entry, return_pct, reason) or (None, None, "na") when the window is
    short or the open is unusable. Same levels as
    archive/research/eval_winrate_round2.sim_trail, the simulator every adopted
    threshold was chosen on -- but NOT the same event order any more; see the
    F09 note below, and read the two together before comparing a ledger number
    with a backtest number:
      entry  = the first forward bar's OPEN (the live rule is a market order at
               the next open; no limit is posted)
      stop   = entry * (1 - PRELAUNCH_STOP_PCT), disaster insurance only
      lock   = once a bar CLOSES at or above +PRELAUNCH_TRAIL_ARM, the stop
               rises to entry * (1 + PRELAUNCH_TRAIL_LOCK) from the NEXT bar
      target = entry * (1 + PRELAUNCH_TP_PCT), taken intraday

    EVENT ORDER WITHIN ONE BAR (F09, report sections 5.4 / 15). A daily bar is
    four numbers; it cannot tell you the true intraday sequence, so the order
    below is a stated assumption, not a measurement:

      1. THE OPEN, which is the one price whose timing IS known -- it is the
         first trade of the day. A bar that opens at or through a level filled
         AT THE OPEN, not at the level. So an open above the target is a take
         profit at the open (a gap up to 125 with a 120 target books +25%, not
         the +2% lock that the old low-first ordering booked), and an open at or
         below the live stop is a stop at the open.
      2. THE REST OF THE BAR, where high-vs-low ordering is unknowable, so the
         LOWEST exit level the bar actually touched is booked -- deliberately
         pessimistic. Only the stop CARRIED IN to the bar counts: a bar with
         high 125 and low 80 on a 100 entry could have gone down first (-20%)
         or up through the target (+20%), and -20% is the one we cannot rule
         out. A fill inside the bar is at the level itself (a gap through it
         was already handled in step 1).
      3. THE CLOSE, where arming is observed (2026-09-21). A close at or above
         arm_px raises the stop to the lock FOR THE NEXT SESSION. It used to
         arm on the intraday high and let that same bar be stopped on the lock
         it had just armed -- a protection nobody could have placed, since the
         scan runs after the close and the order goes in the next morning. On
         the 564 CORE+ trades that ordering reported 69.7% wins where the
         executable version scores 63.0%; see scanner/exit_rules.py.

    This function has therefore returned three different sets of numbers: the
    pre-2026-09-09 order, the F09 same-bar-arming order, and the 2026-09-21
    executable order. Any stored rule_return_pct / rule_exit written before
    today was produced by a rule the app no longer follows, so it must be
    recomputed -- reset_rule_outcomes() then backfill_outcomes() -- before
    being compared with anything new. Group by picks.rule_version when
    comparing across the change.

    The old research simulator (archive/research/eval_winrate_round2.sim_trail)
    still carries the pre-F09 ordering and is NOT the reference any more; the
    2026-09-21 parameters were selected on archive/research/sandbox_lock_delay.py,
    which was verified bar-for-bar against exit_rules.replay_exit over all 564
    CORE+ trades with zero disagreements.
    """
    # ONE implementation, shared with any parameter search, so the rule
    # that chooses the numbers and the rule that measures them can never
    # drift apart again -- which is exactly how F09 got baked into both
    # this function and eval_winrate_round2.sim_trail independently.
    return simulate_exit(
        opens, highs, lows, closes, hold_bars=hold,
        stop_pct=PRELAUNCH_STOP_PCT, tp_pct=PRELAUNCH_TP_PCT,
        arm_pct=PRELAUNCH_TRAIL_ARM, lock_pct=PRELAUNCH_TRAIL_LOCK)


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
            # A row counts as done only once the rule columns exist. Rows
            # written before those columns were added come back through here
            # once, which is how the historical ledger acquires its rule-based
            # measurement instead of only the raw close-to-close one.
            done = set(conn.execute(
                "SELECT scan_session, scan_mode, stock_id, horizon_days "
                "FROM outcomes WHERE rule_exit IS NOT NULL"
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
                # Columns spelled out: the rule_* columns arrive via ALTER
                # TABLE on an existing ledger, so positional VALUES would bind
                # to whatever order the migration happened to produce.
                conn.executemany(
                    "INSERT OR REPLACE INTO outcomes "
                    "(scan_session, scan_mode, stock_id, horizon_days, "
                    " bar_date, entry_close, asof_date, bars, fwd_close, "
                    " fwd_return_pct, mfe_pct, mae_pct, "
                    " rule_entry, rule_return_pct, rule_exit) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    new_rows,
                )
                written = len(new_rows)
    except Exception as e:
        print("  [ledger] backfill failed: {}".format(e))
    return written


def reset_rule_outcomes():
    """
    Clear the simulated rule columns so the next backfill recomputes them.

    DELIBERATE step, called by nothing in the scan path. backfill_outcomes
    treats a row with a non-NULL rule_exit as done, so the F09 event-order fix
    (see _simulate_rule) only reaches rows written AFTER it -- every historical
    row keeps the old, wrong same-bar ordering until this is run. Clearing the
    three rule_* columns is safe: they are pure simulation, recomputed from the
    price series, and the measured columns (fwd_return_pct / mfe_pct / mae_pct)
    are not touched.

    Returns the number of rows cleared.
        python -c "from scanner.signal_ledger import reset_rule_outcomes as r; print(r())"
        python tools/backfill_ledger.py
    """
    if not SIGNAL_LEDGER_FILE.exists():
        return 0
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            cur = conn.execute(
                "UPDATE outcomes SET rule_entry = NULL, rule_return_pct = NULL, "
                "rule_exit = NULL WHERE rule_exit IS NOT NULL")
            return cur.rowcount
    except Exception as e:
        print("  [ledger] rule reset failed: {}".format(e))
        return 0


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
