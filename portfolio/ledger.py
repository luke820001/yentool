"""
The trade ledger: what was recommended, what was actually bought, what it is
worth each day. sqlite3 + Decimal only, no third-party imports, ASCII only.

The one idea this module exists to enforce, from report section 16:
"put recording it correctly ahead of recommending more". Everything else here
follows from keeping four things that used to be one thing apart:

    initial_buy_price   fixed the first day the full gate passed. Never moves.
    latest reference    today's recomputed number. Lives in the scan, not here.
    execution price     what the user actually paid. Only a fill can set it.
    active stop/target  derived from the execution, updated by rule EVENTS.

Nothing in here guesses a fill. annotate_holding's Entry_Open was the market's
open on an INFERRED date, and the old UI rendered it as "held" for a user who
had never bought anything (F03). A position in this ledger exists only because
an execution was recorded.
"""
import json
import uuid
from datetime import datetime
from decimal import Decimal

from portfolio.money import D, money, as_text, pct, get_schedule, ZERO
from portfolio.schema import open_ledger  # re-exported for callers

__all__ = [
    "open_ledger", "now_ts", "record_recommendation", "expire_recommendations",
    "open_position", "add_execution", "record_dividend", "mark_day",
    "freeze_cycle", "position_view", "portfolio_summary", "LedgerError",
]


class LedgerError(Exception):
    """Refused write. The message is shown to the user, so it says what to do."""


def now_ts():
    # Asia/Taipei is pinned by the scheduler (TZ env in the workflow) and by the
    # desktop's own locale; storing local wall time keeps every timestamp in the
    # ledger comparable with session_date, which is a Taiwan trading day.
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sid(value):
    return str(value or "").strip()


def _session(value):
    return str(value or "")[:10]


# --- Recommendations --------------------------------------------------------

def _open_cycle(conn, stock_id, strategy):
    """The recommendation cycle currently in flight for this name, if any.

    Report section 5.3: while a strategy already holds an unclosed position in a
    name, a later scan does NOT open a second automatic buy cycle. Otherwise a
    name that stays on the list for three weeks manufactures a fresh "first day"
    every morning, which is precisely the repeat-counting that inflated the old
    win rate (W02).
    """
    return conn.execute(
        """
        SELECT r.* FROM recommendations r
        LEFT JOIN positions p ON p.recommendation_id = r.recommendation_id
        WHERE r.stock_id = ? AND r.strategy = ?
          AND (r.status = 'active' OR p.status = 'open')
        ORDER BY r.cycle_seq DESC LIMIT 1
        """,
        (stock_id, strategy),
    ).fetchone()


def record_recommendation(conn, stock_id, strategy, strategy_version,
                          session_date, buy_price, stock_name="", market="",
                          stop_price=None, target_price=None,
                          trail_arm_price=None, trail_lock_price=None,
                          valid_until_session=None, horizon_days=10,
                          gate_snapshot=None, recommended_at=None):
    """Create the immutable first-day recommendation, or return the existing one.

    This is F01's fix and it is deliberately boring: if a cycle is already open
    for (stock, strategy) we return that row untouched. Re-running the 14:30,
    17:00 and 18:00 scans therefore yields ONE recommendation_id with ONE price,
    which is the acceptance case in report section 12 ("D0 recommends 100, D1
    closes 110, the recommendation is still 100").

    `gate_snapshot` is the full set of gate inputs that made this qualify. It is
    stored so a later audit can ask "would this still qualify under the rules we
    have now" without re-deriving the market state from scratch.
    """
    stock_id = _sid(stock_id)
    if not stock_id:
        raise LedgerError("stock_id is required")
    price = D(buy_price)
    if price <= 0:
        raise LedgerError("initial buy price must be positive, got {}".format(buy_price))
    session_date = _session(session_date)

    existing = _open_cycle(conn, stock_id, strategy)
    if existing is not None:
        return existing["recommendation_id"], False

    seq_row = conn.execute(
        "SELECT COALESCE(MAX(cycle_seq), 0) AS n FROM recommendations "
        "WHERE stock_id = ? AND strategy = ?", (stock_id, strategy)).fetchone()
    cycle_seq = int(seq_row["n"]) + 1
    rec_id = "rec-{}-{}-{}".format(stock_id, strategy, cycle_seq)

    with conn:
        conn.execute(
            """
            INSERT INTO recommendations (
                recommendation_id, stock_id, stock_name, market, strategy,
                strategy_version, cycle_seq, first_qualified_session,
                recommended_at, initial_buy_price, initial_stop_price,
                initial_target_price, trail_arm_price, trail_lock_price,
                valid_until_session, horizon_days, gate_snapshot, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active')
            """,
            (rec_id, stock_id, stock_name, market, strategy, strategy_version,
             cycle_seq, session_date, recommended_at or now_ts(),
             as_text(price),
             as_text(stop_price) if stop_price is not None else None,
             as_text(target_price) if target_price is not None else None,
             as_text(trail_arm_price) if trail_arm_price is not None else None,
             as_text(trail_lock_price) if trail_lock_price is not None else None,
             _session(valid_until_session) or None, int(horizon_days),
             json.dumps(gate_snapshot, ensure_ascii=False, sort_keys=True)
             if gate_snapshot else None),
        )
        conn.execute(
            "INSERT INTO recommendation_events "
            "(recommendation_id, event_type, effective_session, recorded_at, reason_code) "
            "VALUES (?,?,?,?,?)",
            (rec_id, "created", session_date, now_ts(), "first_qualified"),
        )
    return rec_id, True


def expire_recommendations(conn, session_date, reason="window_elapsed"):
    """Mark active recommendations whose entry window has passed.

    Expiry is a state change plus an event, never a delete: an expired
    recommendation is still evidence of what the system said that day.
    """
    session_date = _session(session_date)
    rows = conn.execute(
        "SELECT recommendation_id FROM recommendations "
        "WHERE status = 'active' AND valid_until_session IS NOT NULL "
        "AND valid_until_session < ?", (session_date,)).fetchall()
    if not rows:
        return 0
    with conn:
        for row in rows:
            rid = row["recommendation_id"]
            conn.execute("UPDATE recommendations SET status='expired' "
                         "WHERE recommendation_id = ?", (rid,))
            conn.execute(
                "INSERT INTO recommendation_events "
                "(recommendation_id, event_type, effective_session, recorded_at, reason_code) "
                "VALUES (?,?,?,?,?)",
                (rid, "expired", session_date, now_ts(), reason))
    return len(rows)


# --- Positions and executions ----------------------------------------------

def open_position(conn, stock_id, stock_name="", market="", strategy="",
                  strategy_version="", recommendation_id=None,
                  fee_schedule="tw-equity-v1", horizon_days=10,
                  account_id="default", origin="recommended", note=""):
    """Create an empty position container. It owns nothing until a BUY lands.

    `origin` separates "the system suggested this" from "the user bought it on
    their own" (report section 5.2 allows both, but they must not be reported as
    the same thing when measuring the strategy).
    """
    stock_id = _sid(stock_id)
    if not stock_id:
        raise LedgerError("stock_id is required")
    get_schedule(fee_schedule)  # validate now, not at valuation time
    pos_id = "pos-{}".format(uuid.uuid4().hex[:12])
    ts = now_ts()
    with conn:
        conn.execute(
            """
            INSERT INTO positions (
                position_id, account_id, recommendation_id, stock_id, stock_name,
                market, strategy, strategy_version, fee_schedule, horizon_days,
                status, origin, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)
            """,
            (pos_id, account_id, recommendation_id, stock_id, stock_name, market,
             strategy, strategy_version, fee_schedule, int(horizon_days),
             origin, note, ts, ts),
        )
        if recommendation_id:
            # The recommendation has done its job: it is now a real holding,
            # not an outstanding suggestion. Report section 5.2's lifecycle
            # ends the advice branch here ("已建立實際持倉"), which also stops
            # expire_recommendations from later marking it as one that lapsed
            # unacted-on -- it did not lapse, it was taken.
            conn.execute(
                "UPDATE recommendations SET status = 'converted' "
                "WHERE recommendation_id = ? AND status = 'active'",
                (recommendation_id,))
            conn.execute(
                "INSERT INTO recommendation_events "
                "(recommendation_id, event_type, effective_session, recorded_at, reason_code, payload) "
                "VALUES (?,?,?,?,?,?)",
                (recommendation_id, "position_opened", None, ts, "user_execution",
                 json.dumps({"position_id": pos_id})))
    return pos_id


def add_execution(conn, position_id, side, session_date, shares, price,
                  fee=None, tax=None, executed_at=None, note="",
                  idempotency_key=None, fee_schedule=None):
    """Record one real fill and rebuild the position from its full trade history.

    Validation is strict on purpose (F11): the phone used to do
    `num(raw) || ref`, so typing "abc" or 0 silently stored the REFERENCE price
    as if the user had traded at it. A price we are not sure about is not a
    price; refuse it and make the user retype.

    Fees default to the position's fee schedule but can be overridden with the
    broker's actual figures, because the schedule is a model and the statement
    is the truth.
    """
    pos = conn.execute("SELECT * FROM positions WHERE position_id = ?",
                       (position_id,)).fetchone()
    if pos is None:
        raise LedgerError("unknown position: {}".format(position_id))

    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise LedgerError("side must be BUY or SELL, got {}".format(side))

    # int(1.5) is 1, not an error -- truncating here would book a trade the
    # user did not make. Shares are whole; odd lots are still whole shares.
    if isinstance(shares, bool):
        raise LedgerError("shares must be a whole number, got {!r}".format(shares))
    try:
        share_d = D(shares)
    except Exception:
        raise LedgerError("shares must be a whole number, got {!r}".format(shares))
    if share_d != share_d.to_integral_value():
        raise LedgerError(
            "shares must be a whole number of shares, got {!r} -- enter lots "
            "multiplied by the lot size instead".format(shares))
    shares = int(share_d)
    if shares <= 0:
        raise LedgerError("shares must be greater than 0, got {}".format(shares))

    try:
        price = D(price)
    except Exception:
        raise LedgerError("price is not a number: {!r}".format(price))
    if price <= 0:
        raise LedgerError("price must be greater than 0, got {}".format(price))

    session_date = _session(session_date)
    if not session_date:
        raise LedgerError("session_date is required -- an execution without a "
                          "trade date cannot be placed on the P&L timeline")

    if idempotency_key:
        dup = conn.execute(
            "SELECT execution_id FROM executions WHERE idempotency_key = ?",
            (idempotency_key,)).fetchone()
        if dup is not None:
            return dup["execution_id"], False

    sched = get_schedule(fee_schedule or pos["fee_schedule"])
    if side == "BUY":
        fee = sched.buy_fee(price, shares) if fee is None else money(fee)
        tax = ZERO if tax is None else money(tax)
    else:
        held = int(pos["open_shares"])
        if shares > held:
            raise LedgerError(
                "cannot sell {} shares, only {} are held".format(shares, held))
        fee = sched.sell_fee(price, shares) if fee is None else money(fee)
        tax = sched.sell_tax(price, shares) if tax is None else money(tax)

    exe_id = "exe-{}".format(uuid.uuid4().hex[:12])
    with conn:
        conn.execute(
            """
            INSERT INTO executions (
                execution_id, position_id, side, session_date, executed_at,
                shares, price, fee, tax, fee_schedule, note, idempotency_key,
                recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (exe_id, position_id, side, session_date, executed_at, shares,
             as_text(price), as_text(fee), as_text(tax), sched.version, note,
             idempotency_key, now_ts()),
        )
        _recompute_position(conn, position_id)
    return exe_id, True


def _recompute_position(conn, position_id):
    """Derive every position number from its executions, in trade order.

    Rebuilding from scratch (rather than incrementally adjusting) is what makes
    corrections safe: amend a fill from three days ago and the position, not
    just today, comes out right. Report section 11.6 requires exactly this --
    "after the user amends a fill, recompute the snapshots from the affected
    date onward".

    Cost method is moving weighted average (report section 6.3 picks one method
    for the account and requires any difference from the broker statement to be
    shown rather than hidden).
    """
    rows = conn.execute(
        "SELECT * FROM executions WHERE position_id = ? AND is_current = 1 "
        "ORDER BY session_date, COALESCE(executed_at,''), recorded_at, rowid",
        (position_id,)).fetchall()

    shares = 0
    cost_basis = ZERO      # includes buy fees -- the book cost
    gross_cost = ZERO      # excludes fees -- comparable to a quoted price
    realized_net = ZERO
    realized_gross = ZERO
    opened_session = None
    closed_session = None

    for e in rows:
        e_shares = int(e["shares"])
        e_price = D(e["price"])
        e_fee = D(e["fee"])
        e_tax = D(e["tax"])
        consideration = e_price * e_shares

        if e["side"] == "BUY":
            if opened_session is None:
                opened_session = e["session_date"]
            shares += e_shares
            cost_basis += consideration + e_fee
            gross_cost += consideration
            closed_session = None
        else:
            if shares <= 0:
                continue  # defensive: add_execution already refuses this
            removed_book = cost_basis * D(e_shares) / D(shares)
            removed_gross = gross_cost * D(e_shares) / D(shares)
            realized_net += (consideration - e_fee - e_tax) - removed_book
            realized_gross += consideration - removed_gross
            cost_basis -= removed_book
            gross_cost -= removed_gross
            shares -= e_shares
            if shares == 0:
                closed_session = e["session_date"]

    if shares == 0:
        # Zero out residual cents left by the division above; carrying a
        # 0.0001 cost basis on an empty position makes "closed" look wrong.
        cost_basis = ZERO
        gross_cost = ZERO

    avg_cost = (gross_cost / D(shares)) if shares else ZERO
    status = "open" if shares > 0 else ("closed" if rows else "open")

    conn.execute(
        """
        UPDATE positions SET
            open_shares = ?, avg_cost = ?, cost_basis = ?, gross_cost = ?,
            realized_net = ?, realized_gross = ?, opened_session = ?,
            closed_session = ?, status = ?, updated_at = ?
        WHERE position_id = ?
        """,
        (shares, as_text(avg_cost), as_text(cost_basis), as_text(gross_cost),
         as_text(realized_net), as_text(realized_gross), opened_session,
         closed_session if shares == 0 else None, status, now_ts(), position_id),
    )


def record_dividend(conn, position_id, session_date, amount, note=""):
    """Cash dividend actually received. Report section 6.3 counts it in the
    total but keeps it a separate term, so a dividend never looks like a price
    gain."""
    pos = conn.execute("SELECT dividends FROM positions WHERE position_id = ?",
                       (position_id,)).fetchone()
    if pos is None:
        raise LedgerError("unknown position: {}".format(position_id))
    total = D(pos["dividends"]) + D(amount)
    with conn:
        conn.execute("UPDATE positions SET dividends = ?, updated_at = ? "
                     "WHERE position_id = ?",
                     (as_text(total), now_ts(), position_id))
        conn.execute(
            "INSERT INTO position_rule_events "
            "(position_id, session_date, event_type, reason_code, recorded_at) "
            "VALUES (?,?,?,?,?)",
            (position_id, _session(session_date), "dividend",
             note or "cash_dividend", now_ts()))
    return total


# --- Daily valuation --------------------------------------------------------

def _day_index(calendar, opened_session, session_date):
    """Trading days held, inclusive of the entry day. None when unknowable.

    Returning None rather than a guess is the point (report section 4.3): a
    missing bar is not a holiday, and a holiday is not a missing bar. The phone
    used to extrapolate plain weekdays and ran a day ahead through every typhoon
    closure (F14).
    """
    if not calendar or not opened_session:
        return None
    try:
        return calendar.index(session_date) - calendar.index(opened_session) + 1
    except ValueError:
        return None


def mark_day(conn, position_id, session_date, close_price, calendar=None,
             price_source="", price_basis="raw", data_status="current"):
    """Write (or restate) one session's valuation for one position.

    Every figure the UI can show is computed here, ONCE, so desktop and phone
    cannot disagree (F02, F21). The four totals are kept apart deliberately --
    report section 6.3 forbids them sharing a "total profit" label:

        total_gross        price difference only, no costs
        total_book         realized net + unrealized book + dividends
        net_if_liquidated  total_book minus the cost of selling out today
        (and the two returns, vs the fixed recommendation and vs real cost)

    Restating a day writes revision+1 and demotes the previous row rather than
    updating it, so "yesterday's number changed" stays answerable.
    """
    pos = conn.execute("SELECT * FROM positions WHERE position_id = ?",
                       (position_id,)).fetchone()
    if pos is None:
        raise LedgerError("unknown position: {}".format(position_id))
    session_date = _session(session_date)
    sched = get_schedule(pos["fee_schedule"])

    shares = int(pos["open_shares"])
    cost_basis = D(pos["cost_basis"])
    gross_cost = D(pos["gross_cost"])
    realized_net = D(pos["realized_net"])
    realized_gross = D(pos["realized_gross"])
    dividends = D(pos["dividends"])

    if close_price is None:
        # No price is a state, not a zero -- and not a fabricated bar either.
        # Report section 7.2's own overview mock carries the last known close
        # and SAYS SO ("still valued at the 9/8 close"), which is honest in a
        # way that both zeroing the position and inventing today's price are
        # not. With no prior mark at all there is nothing to carry, so the day
        # is recorded as missing rather than guessed.
        carried = conn.execute(
            "SELECT session_date, close_price FROM position_daily_marks "
            "WHERE position_id = ? AND session_date < ? AND is_current = 1 "
            "AND close_price IS NOT NULL ORDER BY session_date DESC LIMIT 1",
            (position_id, session_date)).fetchone()
        if carried is not None:
            close_d = D(carried["close_price"])
            price_source = "carried:{}".format(carried["session_date"])
            data_status = "stale"
        else:
            close_d = None
            data_status = "missing"
    else:
        close_d = D(close_price)

    if close_d is None:
        market_value = ZERO
        unrealized_book = ZERO
        unrealized_gross = ZERO
        net_if_liq = None
    else:
        market_value = close_d * D(shares)
        unrealized_book = market_value - cost_basis
        unrealized_gross = market_value - gross_cost
        net_if_liq = None

    total_book = realized_net + unrealized_book + dividends
    total_gross = realized_gross + unrealized_gross
    if close_d is not None:
        net_if_liq = total_book - (sched.exit_cost(close_d, shares)
                                   if shares else ZERO)

    prev = conn.execute(
        "SELECT total_gross, total_book FROM position_daily_marks "
        "WHERE position_id = ? AND session_date < ? AND is_current = 1 "
        "ORDER BY session_date DESC LIMIT 1",
        (position_id, session_date)).fetchone()
    prev_gross = D(prev["total_gross"]) if prev else ZERO
    prev_book = D(prev["total_book"]) if prev else ZERO

    revision = int(conn.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 AS n FROM position_daily_marks "
        "WHERE position_id = ? AND session_date = ?",
        (position_id, session_date)).fetchone()["n"])

    with conn:
        conn.execute(
            "UPDATE position_daily_marks SET is_current = 0 "
            "WHERE position_id = ? AND session_date = ?",
            (position_id, session_date))
        conn.execute(
            """
            INSERT INTO position_daily_marks (
                position_id, session_date, revision, day_index, close_price,
                price_source, price_basis, open_shares, cost_basis, market_value,
                unrealized_book, unrealized_gross, realized_net, total_book,
                total_gross, day_pnl_gross, day_pnl_book, net_if_liquidated,
                data_status, is_current, recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
            """,
            (position_id, session_date, revision,
             _day_index(calendar, pos["opened_session"], session_date),
             as_text(close_d) if close_d is not None else None,
             price_source, price_basis, shares, as_text(cost_basis),
             as_text(market_value), as_text(unrealized_book),
             as_text(unrealized_gross), as_text(realized_net),
             as_text(total_book), as_text(total_gross),
             as_text(total_gross - prev_gross), as_text(total_book - prev_book),
             as_text(net_if_liq) if net_if_liq is not None else None,
             data_status, now_ts()),
        )
        # A protective stop ratchets up only (report section 5.4): a falling
        # market must never recompute a LOWER stop and quietly widen the risk.
        if close_d is not None and shares > 0:
            high = D(pos["highest_close"]) if pos["highest_close"] else ZERO
            if close_d > high:
                conn.execute("UPDATE positions SET highest_close = ?, updated_at = ? "
                             "WHERE position_id = ?",
                             (as_text(close_d), now_ts(), position_id))
    return revision


def freeze_cycle(conn, position_id, horizon_days=None, basis="position"):
    """Freeze the horizon-day result so later prices can never rewrite it.

    Report section 5.3: at D10 the ten-day outcome is fixed. If the user still
    holds, the POSITION keeps being valued (D11, D12, ...) and stays visible as
    something to deal with -- but the ten-day number stops moving. The unique
    index on (position_id, horizon, basis) is what enforces "fixed".
    """
    pos = conn.execute("SELECT * FROM positions WHERE position_id = ?",
                       (position_id,)).fetchone()
    if pos is None:
        raise LedgerError("unknown position: {}".format(position_id))
    horizon = int(horizon_days or pos["horizon_days"] or 10)

    existing = conn.execute(
        "SELECT cycle_id FROM cycle_results WHERE position_id = ? "
        "AND horizon_days = ? AND basis = ?",
        (position_id, horizon, basis)).fetchone()
    if existing is not None:
        return existing["cycle_id"], False

    mark = conn.execute(
        "SELECT * FROM position_daily_marks WHERE position_id = ? "
        "AND day_index = ? AND is_current = 1", (position_id, horizon)).fetchone()
    if mark is None:
        # Either the position closed early or day 10 has not happened. Both are
        # legitimate; a closed position freezes on its final mark instead, which
        # is the report's "sold on D4 -> D10 review shows the realised result".
        if pos["status"] != "closed":
            return None, False
        mark = conn.execute(
            "SELECT * FROM position_daily_marks WHERE position_id = ? "
            "AND is_current = 1 ORDER BY session_date DESC LIMIT 1",
            (position_id,)).fetchone()
        if mark is None:
            return None, False

    rec = None
    if pos["recommendation_id"]:
        rec = conn.execute(
            "SELECT initial_buy_price FROM recommendations WHERE recommendation_id = ?",
            (pos["recommendation_id"],)).fetchone()

    close_d = D(mark["close_price"]) if mark["close_price"] else None
    ret_initial = (pct(close_d - D(rec["initial_buy_price"]),
                       D(rec["initial_buy_price"]))
                   if rec and close_d is not None else None)
    avg = D(pos["avg_cost"])
    ret_cost = pct(close_d - avg, avg) if close_d is not None and avg > 0 else None

    cycle_id = "cyc-{}-{}-{}".format(position_id, horizon, basis)
    with conn:
        conn.execute(
            """
            INSERT INTO cycle_results (
                cycle_id, position_id, recommendation_id, horizon_days, basis,
                session_date, day_index, close_price, open_shares, cost_basis,
                total_gross, total_book, net_if_liquidated, return_vs_initial,
                return_vs_cost, still_open, frozen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (cycle_id, position_id, pos["recommendation_id"], horizon, basis,
             mark["session_date"], mark["day_index"], mark["close_price"],
             mark["open_shares"], mark["cost_basis"], mark["total_gross"],
             mark["total_book"], mark["net_if_liquidated"],
             str(ret_initial) if ret_initial is not None else None,
             str(ret_cost) if ret_cost is not None else None,
             1 if pos["status"] == "open" else 0, now_ts()),
        )
    return cycle_id, True


# --- Read models ------------------------------------------------------------

def position_view(conn, position_id):
    """Everything one position card needs, already decided by the backend.

    The phone must not re-derive any of this (F07). Its job is to render what
    the ledger says and, at most, mark it stale.
    """
    pos = conn.execute("SELECT * FROM positions WHERE position_id = ?",
                       (position_id,)).fetchone()
    if pos is None:
        return None
    mark = conn.execute(
        "SELECT * FROM position_daily_marks WHERE position_id = ? "
        "AND is_current = 1 ORDER BY session_date DESC LIMIT 1",
        (position_id,)).fetchone()
    rec = None
    if pos["recommendation_id"]:
        rec = conn.execute(
            "SELECT * FROM recommendations WHERE recommendation_id = ?",
            (pos["recommendation_id"],)).fetchone()
    cycle = conn.execute(
        "SELECT * FROM cycle_results WHERE position_id = ? ORDER BY horizon_days "
        "LIMIT 1", (position_id,)).fetchone()

    avg = D(pos["avg_cost"])
    close_d = D(mark["close_price"]) if mark and mark["close_price"] else None
    return {
        "position_id": pos["position_id"],
        "stock_id": pos["stock_id"],
        "stock_name": pos["stock_name"],
        "market": pos["market"],
        "status": pos["status"],
        "origin": pos["origin"],
        "opened_session": pos["opened_session"],
        "closed_session": pos["closed_session"],
        "open_shares": int(pos["open_shares"]),
        "avg_cost": str(avg),
        "cost_basis": pos["cost_basis"],
        "realized_net": pos["realized_net"],
        "dividends": pos["dividends"],
        "initial_buy_price": rec["initial_buy_price"] if rec else None,
        "recommendation_id": pos["recommendation_id"],
        "active_stop": pos["active_stop"],
        "target_price": pos["target_price"],
        "trail_armed_at": pos["trail_armed_at"],
        "highest_close": pos["highest_close"],
        "session_date": mark["session_date"] if mark else None,
        "day_index": mark["day_index"] if mark else None,
        "horizon_days": int(pos["horizon_days"] or 10),
        "close_price": mark["close_price"] if mark else None,
        "data_status": mark["data_status"] if mark else "missing",
        "total_gross": mark["total_gross"] if mark else None,
        "total_book": mark["total_book"] if mark else None,
        "day_pnl_gross": mark["day_pnl_gross"] if mark else None,
        "net_if_liquidated": mark["net_if_liquidated"] if mark else None,
        "return_vs_cost": str(pct(close_d - avg, avg))
                          if close_d is not None and avg > 0 else None,
        "return_vs_initial": (str(pct(close_d - D(rec["initial_buy_price"]),
                                      D(rec["initial_buy_price"])))
                              if rec and close_d is not None else None),
        "cycle_frozen": bool(cycle),
        "cycle_session": cycle["session_date"] if cycle else None,
    }


def portfolio_summary(conn, account_id="default", session_date=None):
    """Account totals for the overview page.

    Report section 6.4 is the whole spec here: amounts add up, PERCENTAGES DO
    NOT. So this returns money plus the cost base it came from and leaves the
    caller to divide -- and it reports valuation_complete/stale_positions,
    because a total built from mixed valuation dates has to say so.
    """
    positions = conn.execute(
        "SELECT * FROM positions WHERE account_id = ?", (account_id,)).fetchall()

    total_book = ZERO
    total_gross = ZERO
    realized = ZERO
    day_pnl = ZERO
    invested = ZERO
    open_n = 0
    stale = []
    dates = set()

    for pos in positions:
        realized += D(pos["realized_net"])
        if pos["status"] == "open":
            open_n += 1
            invested += D(pos["cost_basis"])
        mark = conn.execute(
            "SELECT * FROM position_daily_marks WHERE position_id = ? "
            "AND is_current = 1 ORDER BY session_date DESC LIMIT 1",
            (pos["position_id"],)).fetchone()
        if mark is None:
            if pos["status"] == "open":
                stale.append({"position_id": pos["position_id"],
                              "stock_id": pos["stock_id"], "as_of": None})
            continue
        total_book += D(mark["total_book"])
        total_gross += D(mark["total_gross"])
        day_pnl += D(mark["day_pnl_gross"] or 0)
        dates.add(mark["session_date"])
        if session_date and mark["session_date"] < _session(session_date) \
                and pos["status"] == "open":
            stale.append({"position_id": pos["position_id"],
                          "stock_id": pos["stock_id"],
                          "as_of": mark["session_date"]})

    return {
        "account_id": account_id,
        "open_positions": open_n,
        "total_positions": len(positions),
        "invested_cost": as_text(invested),
        "total_book": as_text(total_book),
        "total_gross": as_text(total_gross),
        "realized_net": as_text(realized),
        "day_pnl_gross": as_text(day_pnl),
        # Static-cohort return only. An account with deposits/withdrawals needs
        # a time-weighted return or XIRR instead (report section 6.4).
        "return_on_cost_pct": str(pct(total_book, invested))
                              if invested > 0 else None,
        "valuation_complete": not stale,
        "stale_positions": stale,
        "valuation_dates": sorted(dates),
    }
