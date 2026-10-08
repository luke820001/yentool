"""
Bridge between the daily scan and the trade ledger. ASCII only.

This is where F01 actually gets fixed. `add_trade_columns` recomputes
Suggested_Buy_Price from today's close on every single run, so a name that was
recommended at 100 on Monday advertises 118 on Friday and the user has no way
to see what the system originally said. Both numbers are legitimate; they are
just not the same number:

    Suggested_Buy_Price   today's reference, recomputed, still useful
    Initial_Buy_Price     what we said the first day it qualified, frozen

attach_recommendations() writes the second one the first time a row passes the
complete gate, then hangs it off every subsequent scan of that name. The scan
keeps recomputing whatever it likes; it can no longer overwrite history.

The recommendation lifecycle (2026-10-08, "Model T"). 'active' means the
recommended trade is live: inside its entry window (the session after the
qualifying one, `valid_until_session`) or in trade. advance_recommendations()
moves it to a terminal status exactly once -- closed when the canonical
DEFAULT_RULE replay (scanner.live_record.replay_trade, the same function the
holding card and the live record use) books the exit, expired when it was
never filled, superseded when a rule change or a same-session re-run withdrew
it before entry -- and a terminal recommendation frees the name for the next
genuine signal (cycle_seq + 1). Until 2026-10-08 nothing ever ended a
recommendation except an expiry that would have killed a live trade on its
second day, so the four published recommendations were all 'active', some of
them weeks after their trade had closed.

Degraded runs (an exchange snapshot failed) write nothing: attach runs
read-only (allow_writes=False) and the caller skips advance and the export.
Rec 5274 was created by such a run on 2026-09-17 and stayed live for three
weeks.

Deliberately tolerant: the ledger is bookkeeping, and a bookkeeping failure
must never take down the market feed. Every entry point swallows and reports.
"""
import bisect
import json

from portfolio.ledger import (open_ledger, record_recommendation,
                              transition_recommendation, set_valid_until,
                              LedgerError)
from scanner.exit_rules import DEFAULT_RULE

# Columns that carry the frozen first-day view into the export and the UI.
# Rec_Status_Reason (2026-10-08) says why a recommendation ended (the exit
# reason of a closed one, e.g. 'time'); null while it is active.
REC_COLUMNS = ("Recommendation_ID", "Initial_Buy_Price", "Initial_Stop_Price",
               "Initial_Target_Price", "Recommended_On", "Rec_Status",
               "Rec_Valid_Until", "Rec_Status_Reason")

# The inputs that made a row qualify, kept with the recommendation so a later
# audit can ask "would this still pass under today's rules" without having to
# reconstruct the whole market state (report section 9.1, signal_snapshots).
# Hold_Status here is the NATURAL-segment status at the moment of
# qualification (taken before the card is re-anchored to the new
# recommendation): 'exited' / 'overdue' with Buy_Ready true is the re-entry
# after an old trade, evidence rather than an error. It is never rewritten.
# Trade_Restriction / Restriction_Until (2026-10-08): the disposition or other
# restriction in force when the recommendation was made, so a later audit can
# split the record by it (scanner/trade_restrictions).
GATE_FIELDS = ("Close_Price", "Launch_Score", "Surge_Score", "Explosion_Score",
               "Core_Plus", "Dist_52W_High_Pct", "Ret_5D_Pct", "ATR_Pct",
               "Market", "Data_Date", "Integrity_OK", "Hold_Status",
               "Buy_Ready", "Buy_Block", "Trade_Restriction",
               "Restriction_Until")

# A non-degraded re-run of the SAME session that no longer marks the row
# Buy_Ready (for a reason other than 'held'), or no longer lists it at all,
# withdraws the recommendation it created earlier that session:
# superseded('retracted:<Buy_Block>' | 'retracted:off_list'). The owner can
# switch this off if the evening re-run ever flaps against the 15:05 run.
RETRACT_SAME_SESSION = True

# A closed recommendation stays attached to its row for this many price
# sessions after its exit, so the card keeps showing how the trade ended.
CLOSED_GRACE_SESSIONS = 5

# Safety net: an active recommendation more than this many market sessions
# past its entry session can only mean missing data (the rule's own ride cap
# ends every trade 20 bars after entry). expired('horizon_elapsed').
REC_HORIZON_SESSIONS = int(DEFAULT_RULE["ride_cap"]) + 5

# sentinel: "use the market leg" (scanner.market_leg via the tracker)
_MARKET_LEG = object()


def _clean(value):
    """JSON-safe scalar. NaN becomes None: absent is not zero."""
    if value is None:
        return None
    try:
        if value != value:      # NaN
            return None
    except Exception:
        pass
    if hasattr(value, "item"):  # numpy scalar
        try:
            value = value.item()
        except Exception:
            return str(value)
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _num(row, col):
    value = _clean(row.get(col))
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _on_ladder(value, direction, stock_id=None):
    """A stored level snapped onto the exchange's quote ladder, or None."""
    if value in (None, ""):
        return None
    try:
        px = float(value)
    except (TypeError, ValueError):
        return None
    if not px > 0:
        return None
    try:
        from scanner.tick import round_to_tick
    except Exception:
        return round(px, 2)
    got = round_to_tick(px, direction, stock_id)
    return got if got is not None else round(px, 2)


def _day(value):
    return str(value or "")[:10]


def _calendar(calendar):
    """The sorted price calendar (holding_tracker._trading_calendar), or []."""
    if calendar is not None:
        return sorted({_day(d) for d in calendar if d})
    try:
        from scanner.holding_tracker import _trading_calendar
        return list(_trading_calendar())
    except Exception:
        return []


def _grace_floor(session_date, cal):
    """The oldest status_session a closed recommendation may carry and still
    be attached: the first of the last CLOSED_GRACE_SESSIONS calendar
    sessions up to `session_date`. None = attach no closed one."""
    sd = _day(session_date)
    if not sd or not cal:
        return None
    tail = [d for d in cal if d <= sd][-CLOSED_GRACE_SESSIONS:]
    return tail[0] if tail else None


def _attachable(conn, scan_mode, session_date=None, calendar=None):
    """{stock_id: recommendation row} -- the one recommendation each name's
    card and row describe: an active (or local 'converted') one first, else
    the newest closed one inside the grace window."""
    rows = conn.execute(
        "SELECT * FROM recommendations WHERE strategy = ? "
        "AND status IN ('active','converted','closed') "
        "ORDER BY cycle_seq", (scan_mode,)).fetchall()
    floor = None
    if any(r["status"] == "closed" for r in rows):
        floor = _grace_floor(session_date, _calendar(calendar))
    rank = {"active": 0, "converted": 1, "closed": 2}
    best = {}
    for r in rows:
        status = r["status"]
        if status == "closed":
            ss = _day(r["status_session"])
            if not floor or not ss or ss < floor or ss > _day(session_date):
                continue
        sid = str(r["stock_id"])
        key = (rank[status], -int(r["cycle_seq"] or 0))
        if sid not in best or key < best[sid][0]:
            best[sid] = (key, r)
    return {sid: r for sid, (_, r) in best.items()}


def _retract_same_session(conn, df, scan_mode, session_date, stats):
    """Withdraw what an earlier run of THIS session recommended and this run
    no longer does (see RETRACT_SAME_SESSION). Needs the Buy_Ready verdict:
    without the column (the buy rule failed) nothing is retracted."""
    sd = _day(session_date)
    if not RETRACT_SAME_SESSION or not sd or "Buy_Ready" not in df.columns:
        return
    by_sid = {}
    for _, row in df.iterrows():
        by_sid[str(row.get("Stock_ID", "")).strip()] = row
    for rec in conn.execute(
            "SELECT recommendation_id, stock_id FROM recommendations "
            "WHERE strategy = ? AND status = 'active' "
            "AND first_qualified_session = ?", (scan_mode, sd)).fetchall():
        row = by_sid.get(str(rec["stock_id"]))
        if row is None:
            reason = "retracted:off_list"
        else:
            if bool(_clean(row.get("Buy_Ready"))):
                continue
            block = str(_clean(row.get("Buy_Block")) or "").strip()
            if block in ("", "held"):
                continue
            reason = "retracted:" + block
        if transition_recommendation(conn, rec["recommendation_id"],
                                     "superseded", reason, sd):
            stats["superseded"] += 1


def attach_recommendations(df, scan_mode, strategy_version, ledger_path,
                           session_date=None, next_session=None,
                           allow_writes=True, calendar=None):
    """Record first-qualified recommendations and attach the frozen columns.

    Returns (df, stats). `stats` reports created / attached / superseded
    counts, `writes` (whether this call was allowed to write), `created_ids`
    and any error, so the caller can log it without needing to catch anything.

    Only rows with Buy_Ready == True create a recommendation. That is the whole
    point of report section 5.3's "first genuinely qualified" rule: the day a
    name first appears on a watchlist is NOT the day it first became a buy, and
    conflating them is what let the old ledger backdate entries to whenever a
    stock first showed up.

    `next_session` is the entry session stored as valid_until_session
    (market_calendar.entry_session_after(session_date)). allow_writes=False
    (a degraded run, the tracked rows) skips the same-session retraction and
    the create loop and only attaches. Nothing here expires anything any more:
    advance_recommendations ends a recommendation once its trade is over.
    """
    stats = {"created": 0, "attached": 0, "expired": 0, "superseded": 0,
             "writes": bool(allow_writes), "created_ids": [], "error": None}
    if df is None or df.empty or "Stock_ID" not in df.columns:
        return df, stats

    conn = None
    try:
        conn = open_ledger(ledger_path)
        df = df.copy()

        if allow_writes:
            # 0. Withdraw this session's earlier recommendations it no longer
            #    makes, BEFORE creating (a retracted name frees its cycle).
            try:
                _retract_same_session(conn, df, scan_mode, session_date, stats)
            except Exception as e:
                stats["error"] = "retract: {}".format(str(e)[:120])

            # 1. Create the immutable record for anything that qualifies today.
            for _, row in df.iterrows():
                if not bool(_clean(row.get("Buy_Ready"))):
                    continue
                price = _num(row, "Suggested_Buy_Price") or _num(row, "Close_Price")
                if not price or price <= 0:
                    continue
                bar = str(_clean(row.get("Data_Date")) or session_date or "")[:10]
                try:
                    rec_id, created = record_recommendation(
                        conn, str(row.get("Stock_ID", "")).strip(), scan_mode,
                        strategy_version, bar, price,
                        stock_name=str(_clean(row.get("Stock_Name")) or ""),
                        market=str(_clean(row.get("Market")) or ""),
                        stop_price=_num(row, "Strict_Stop_Loss"),
                        target_price=_num(row, "Target_Price"),
                        trail_arm_price=_num(row, "Trail_Arm_Price"),
                        trail_lock_price=_num(row, "Trail_Lock_Price"),
                        valid_until_session=next_session,
                        gate_snapshot={f: _clean(row.get(f)) for f in GATE_FIELDS
                                       if f in df.columns},
                    )
                    if created:
                        stats["created"] += 1
                        stats["created_ids"].append(rec_id)
                except LedgerError as e:
                    stats["error"] = "record: {}".format(str(e)[:120])

        # 2. Hang the frozen view off every row we have one for -- including
        #    names that are no longer buyable, because "what did you originally
        #    tell me" is exactly the question a held position asks. A closed
        #    recommendation stays on for CLOSED_GRACE_SESSIONS.
        known = _attachable(conn, scan_mode, session_date, calendar)

        cols = {c: [] for c in REC_COLUMNS}
        for sid in df["Stock_ID"].astype(str):
            rec = known.get(sid.strip())
            if rec is None:
                for c in REC_COLUMNS:
                    cols[c].append(None)
                continue
            stats["attached"] += 1
            cols["Recommendation_ID"].append(rec["recommendation_id"])
            cols["Initial_Buy_Price"].append(float(rec["initial_buy_price"]))
            # The frozen levels are PUBLISHED as order levels, so they have to
            # be prices the exchange quotes. Records written before the tick
            # ladder shipped (2026-09-21) carry raw multiplications -- 1815's
            # Initial_Target_Price was one of them -- and an unplaceable price
            # is not a plan. The ledger row itself is untouched: this rounds
            # the value on its way to the screen, by at most one tick, and the
            # direction follows the same rule as everywhere else (a stop down,
            # a target up, never flattering the level).
            cols["Initial_Stop_Price"].append(
                _on_ladder(rec["initial_stop_price"], "down", sid.strip()))
            cols["Initial_Target_Price"].append(
                _on_ladder(rec["initial_target_price"], "up", sid.strip()))
            cols["Recommended_On"].append(rec["first_qualified_session"])
            cols["Rec_Status"].append(rec["status"])
            cols["Rec_Valid_Until"].append(rec["valid_until_session"])
            cols["Rec_Status_Reason"].append(rec["status_reason"] or None)

        for col in REC_COLUMNS:
            df[col] = cols[col]
    except Exception as e:
        stats["error"] = str(e)[:160]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return df, stats


def load_rec_anchors(ledger_path, scan_mode, session_date=None, calendar=None):
    """{stock_id: {"anchor", "stop", "target", "rec_id", "status",
    "valid_until"}} for holding_tracker.annotate_holding(rec_anchors=...).

    The card of a name with a live (or recently closed) recommendation
    describes THAT trade: anchored on the qualifying session, entered at the
    next open, and before entry showing the recommendation's own stop. The
    levels go through the quote ladder exactly as the attached
    Initial_Stop_Price / Initial_Target_Price do, so Plan_Stop and
    Initial_Stop_Price are the same number by construction. {} on any
    failure (the card then falls back to the natural segment)."""
    out = {}
    conn = None
    try:
        conn = open_ledger(ledger_path)
        for sid, rec in _attachable(conn, scan_mode, session_date,
                                    calendar).items():
            if rec["status"] not in ("active", "closed"):
                continue
            out[sid] = {
                "anchor": _day(rec["first_qualified_session"]),
                "stop": _on_ladder(rec["initial_stop_price"], "down", sid),
                "target": _on_ladder(rec["initial_target_price"], "up", sid),
                "rec_id": rec["recommendation_id"],
                "status": rec["status"],
                "valid_until": _day(rec["valid_until_session"]) or None,
            }
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _store_bars(pairs, upto, cal_set):
    """Default bars_for source: {sid: [(date, o, h, l, c), ...]} from the
    price store (the tracker's own reader), traded sessions only."""
    from scanner.holding_tracker import _bars_since
    raw = _bars_since(pairs, upto)
    return {sid: [b for b in rows if b[0] in cal_set]
            for sid, rows in raw.items()}


def _outcome(t):
    def r2(v):
        return None if v is None else round(float(v), 2)
    return {"entry_date": t.get("entry_date"),
            "entry_price": r2(t.get("entry_price")),
            "exit_date": t.get("exit_date"),
            "exit_price": r2(t.get("exit_price")),
            "reason": t.get("reason"),
            "bars": int(t.get("bars") or 0),
            "ret_gross_pct": r2(t.get("ret_gross_pct")),
            "ret_net_pct": r2(t.get("ret_net_pct"))}


def _leg_unknown(extend_if, exit_day, trade, cap):
    """True when the replay's TIME exit on `exit_day` may only be an artefact
    of a lagging TAIEX feed, so booking it now could be wrong for good.

    The market leg (scanner.market_leg) reads a date with no TAIEX bar as
    "not disturbed", which is right for history and wrong for the newest
    sessions while the index feed is behind the stock data (2026-09-21, and
    the cloud's 2026-10-07 run had no TAIEX bar at all). A time exit is the
    only outcome a missing bar can create -- a missing date can only take a
    ride away, never add one -- and the leg is consulted only on bars before
    the cap (scanner.exit_rules.replay_exit), so the exit is provisional
    exactly when it falls before the cap on a day after the table's last
    date. A callback without a `.table` (a test stub, None) or an empty
    table (the index unreadable: the leg never extends) is taken as final."""
    if extend_if is None or not exit_day:
        return False
    table = getattr(extend_if, "table", None)
    if not table:
        return False
    try:
        if cap is not None and int(trade.get("bars") or 0) >= int(cap):
            return False
        return str(exit_day)[:10] > max(str(k)[:10] for k in table)
    except Exception:
        return False


def advance_recommendations(ledger_path, scan_mode, session_date,
                            strategy_version, bars_for=None, calendar=None,
                            extend_if=_MARKET_LEG, next_session_fn=None):
    """Move every ACTIVE recommendation of `scan_mode` as far through its
    lifecycle as the data up to `session_date` allows. Idempotent: every
    write is guarded by status = 'active' / an unchanged value, so a second
    pass over the same data changes nothing.

    Per recommendation, in order:
      1. backfill   valid_until_session null -> the entry session after the
                    qualifying one (market_calendar.entry_session_after)
      2. version    still before its entry session and written under another
                    strategy_version -> superseded('rule_version')
      3. window     session_date < valid_until -> stays active
      4. entry bar  the stock's first bar after the qualifying session must
                    be ON valid_until: a calendar without that session (the
                    market shut, e.g. a typhoon) moves valid_until to the
                    next traded session ('market_closed'); a later bar but
                    none on valid_until (halted) -> expired('no_fill'); no
                    bar at all yet -> wait (data lag; step 6 catches it)
      5. replay     scanner.live_record.replay_trade over the bars from the
                    entry through session_date (first ride_cap + 1), with the
                    market leg: no priceable fill -> expired('no_fill'); an
                    exit -> closed(<reason>) on the exit bar's session, with
                    the replayed trade as `outcome`; else stays active
      5b. deferral  a TIME exit the market leg could have prevented (a bar
                    before the ride cap) on a session the TAIEX table does
                    not reach yet stays active -- see _leg_unknown -- until
                    the index catches up; past the horizon it is booked as
                    replayed
      6. horizon    still active more than REC_HORIZON_SESSIONS calendar
                    sessions after valid_until -> expired('horizon_elapsed')

    bars_for(pairs, upto) -> {sid: [(date, o, h, l, c), ...]} (default: the
    price store), calendar = the traded sessions (default: the store's),
    extend_if = the replay's market-leg callback (default: scanner.market_leg;
    None rides on the stock leg alone), next_session_fn(day, calendar) -> the
    entry session (default market_calendar.entry_session_after).

    Returns stats {backfilled, moved, closed, expired, superseded, deferred,
    error, transitions: [[rec_id, status, reason, session], ...]} (a
    deferral is listed as [rec_id, 'active', 'exit_deferred', exit day])."""
    stats = {"backfilled": 0, "moved": 0, "closed": 0, "expired": 0,
             "superseded": 0, "deferred": 0, "error": None,
             "transitions": []}
    sd = _day(session_date)
    if not sd:
        stats["error"] = "no session date"
        return stats
    conn = None
    try:
        cal = [d for d in _calendar(calendar) if d <= sd]
        cal_set = set(cal)
        hold = DEFAULT_RULE["hold_bars"]
        cap = DEFAULT_RULE["ride_cap"]
        try:
            from scanner.holding_tracker import (HOLD_BARS_BY_MODE,
                                                 EXIT_DELAY_CAP_BY_MODE)
            hold = HOLD_BARS_BY_MODE.get(scan_mode, hold)
            cap = EXIT_DELAY_CAP_BY_MODE.get(scan_mode, cap)
        except Exception:
            pass
        window = int(cap if cap is not None else hold) + 1
        if extend_if is _MARKET_LEG:
            try:
                from scanner.holding_tracker import _disturbed_fn
                extend_if = _disturbed_fn()
            except Exception:
                extend_if = None
        if next_session_fn is None:
            from scanner.market_calendar import entry_session_after
            next_session_fn = entry_session_after

        conn = open_ledger(ledger_path)
        recs = conn.execute(
            "SELECT * FROM recommendations WHERE strategy = ? "
            "AND status = 'active' ORDER BY stock_id, cycle_seq",
            (scan_mode,)).fetchall()
        if not recs:
            return stats

        pairs = [(str(r["stock_id"]), _day(r["first_qualified_session"]))
                 for r in recs]
        if bars_for is None:
            bars_by = _store_bars(pairs, sd, cal_set)
        else:
            bars_by = bars_for(pairs, sd) or {}

        def move(rid, status, reason, session, outcome=None):
            if transition_recommendation(conn, rid, status, reason, session,
                                         outcome=outcome):
                stats[status] += 1
                stats["transitions"].append([rid, status, reason, session])

        for rec in recs:
            rid = rec["recommendation_id"]
            sid = str(rec["stock_id"])
            fq = _day(rec["first_qualified_session"])
            vu = _day(rec["valid_until_session"])
            try:
                # 1. backfill the entry session
                if not vu:
                    nv = _day(next_session_fn(fq, cal))
                    if not nv:
                        continue
                    if set_valid_until(conn, rid, nv, "backfill"):
                        stats["backfilled"] += 1
                    vu = nv
                # 2. / 3. the entry window is still open
                if sd < vu:
                    if strategy_version and \
                            str(rec["strategy_version"] or "") != str(strategy_version):
                        move(rid, "superseded", "rule_version", sd)
                    continue
                if not cal or vu > cal[-1]:
                    continue            # the store has not reached the entry yet
                bars = [b for b in (bars_by.get(sid) or [])
                        if fq < _day(b[0]) <= sd and _day(b[0]) in cal_set]
                bars.sort(key=lambda b: _day(b[0]))
                # 4. the entry bar
                if vu not in cal_set:
                    # the market did not trade on the planned entry session
                    i = bisect.bisect_right(cal, fq)
                    nv = cal[i] if i < len(cal) else None
                    if nv and nv != vu and set_valid_until(conn, rid, nv,
                                                           "market_closed"):
                        stats["moved"] += 1
                        stats["transitions"].append(
                            [rid, "active", "window_moved", nv])
                        vu = nv
                    if not nv or sd < vu:
                        continue
                if not bars:
                    pass                # no data yet: only the horizon applies
                elif _day(bars[0][0]) != vu:
                    move(rid, "expired", "no_fill", vu)
                    continue
                else:
                    # 5. the canonical replay
                    import pandas as pd
                    from scanner.live_record import replay_trade
                    fwd = pd.DataFrame(
                        [(_day(b[0]), b[1], b[2], b[3], b[4])
                         for b in bars[:window]],
                        columns=["date", "open", "high", "low", "close"])
                    t = replay_trade(fwd, extend_if=extend_if, hold_bars=hold,
                                     ride_cap=cap)
                    if t["reason"] == "na":
                        move(rid, "expired", "no_fill", vu)
                        continue
                    if t["exited"] and t.get("exit_date"):
                        xd = _day(t["exit_date"])
                        past = len(cal) - bisect.bisect_right(cal, vu)
                        if (t["reason"] == "time"
                                and _leg_unknown(extend_if, xd, t, cap)
                                and past <= REC_HORIZON_SESSIONS):
                            # 5b. the market leg cannot be read yet
                            stats["deferred"] += 1
                            stats["transitions"].append(
                                [rid, "active", "exit_deferred", xd])
                            continue
                        move(rid, "closed", t["reason"], xd,
                             outcome=_outcome(t))
                        continue
                # 6. the safety net
                past = len(cal) - bisect.bisect_right(cal, vu)
                if past > REC_HORIZON_SESSIONS:
                    move(rid, "expired", "horizon_elapsed", sd)
            except Exception as e:      # one record must not stop the others
                stats["error"] = "{}: {}".format(rid, str(e)[:120])
    except Exception as e:
        stats["error"] = str(e)[:160]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return stats


def rec_meta(attach_stats=None, advance_stats=None):
    """meta.rec for scan_result.json: what the lifecycle did this run."""
    a = attach_stats or {}
    v = advance_stats or {}
    out = {"created": int(a.get("created", 0) or 0),
           "attached": int(a.get("attached", 0) or 0),
           "closed": int(v.get("closed", 0) or 0),
           "expired": int(v.get("expired", 0) or 0) + int(a.get("expired", 0) or 0),
           "superseded": int(v.get("superseded", 0) or 0)
           + int(a.get("superseded", 0) or 0),
           "backfilled": int(v.get("backfilled", 0) or 0),
           "moved": int(v.get("moved", 0) or 0),
           "deferred": int(v.get("deferred", 0) or 0),
           "writes": bool(a.get("writes", False)),
           "advanced": bool(advance_stats is not None)}
    errs = [s.get("error") for s in (a, v) if s.get("error")]
    if errs:
        out["error"] = "; ".join(str(e)[:120] for e in errs)
    return out


def open_position_ids(ledger_path):
    """Stock ids with an open position or a live recommendation.

    The quote feed must cover these no matter what today's scan selected --
    that is F04. A position stops being visible in the scan the moment it drops
    out of the top 80, and the phone then had no price for it at all.
    """
    ids = set()
    conn = None
    try:
        conn = open_ledger(ledger_path)
        for row in conn.execute(
                "SELECT DISTINCT stock_id FROM positions WHERE status = 'open'"):
            ids.add(str(row["stock_id"]))
        for row in conn.execute(
                "SELECT DISTINCT stock_id FROM recommendations "
                "WHERE status = 'active'"):
            ids.add(str(row["stock_id"]))
    except Exception:
        return ids
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return ids


def summarize(stats):
    """One-line ASCII log string for the scan output."""
    head = "recommendations: {} created, {} attached".format(
        stats.get("created", 0), stats.get("attached", 0))
    if stats.get("superseded"):
        head += ", {} retracted".format(stats["superseded"])
    if not stats.get("writes", True):
        head += " (read-only)"
    if stats.get("error"):
        return head + ", error: {}".format(stats["error"])
    return head + ", {} expired".format(stats.get("expired", 0))


def summarize_advance(stats):
    """One-line ASCII log string for advance_recommendations."""
    line = ("advance: {closed} closed, {expired} expired, {superseded} "
            "superseded, {backfilled} backfilled, {moved} moved, "
            "{deferred} deferred").format(
        **{k: stats.get(k, 0) for k in ("closed", "expired", "superseded",
                                        "backfilled", "moved", "deferred")})
    if stats.get("error"):
        line += ", error: {}".format(stats["error"])
    return line
