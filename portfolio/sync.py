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

Deliberately tolerant: the ledger is bookkeeping, and a bookkeeping failure
must never take down the market feed. Every entry point swallows and reports.
"""
import json

from portfolio.ledger import (open_ledger, record_recommendation,
                              expire_recommendations, LedgerError)

# Columns that carry the frozen first-day view into the export and the UI.
REC_COLUMNS = ("Recommendation_ID", "Initial_Buy_Price", "Initial_Stop_Price",
               "Initial_Target_Price", "Recommended_On", "Rec_Status",
               "Rec_Valid_Until")

# The inputs that made a row qualify, kept with the recommendation so a later
# audit can ask "would this still pass under today's rules" without having to
# reconstruct the whole market state (report section 9.1, signal_snapshots).
GATE_FIELDS = ("Close_Price", "Launch_Score", "Surge_Score", "Explosion_Score",
               "Core_Plus", "Dist_52W_High_Pct", "Ret_5D_Pct", "ATR_Pct",
               "Market", "Data_Date", "Integrity_OK", "Hold_Status",
               "Buy_Ready", "Buy_Block")


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


def attach_recommendations(df, scan_mode, strategy_version, ledger_path,
                           session_date=None, next_session=None):
    """Record first-qualified recommendations and attach the frozen columns.

    Returns (df, stats). `stats` reports created/attached/expired counts and
    any error, so the caller can log it without needing to catch anything.

    Only rows with Buy_Ready == True create a recommendation. That is the whole
    point of report section 5.3's "first genuinely qualified" rule: the day a
    name first appears on a watchlist is NOT the day it first became a buy, and
    conflating them is what let the old ledger backdate entries to whenever a
    stock first showed up.
    """
    stats = {"created": 0, "attached": 0, "expired": 0, "error": None}
    if df is None or df.empty or "Stock_ID" not in df.columns:
        return df, stats

    conn = None
    try:
        conn = open_ledger(ledger_path)
        df = df.copy()

        if session_date:
            try:
                stats["expired"] = expire_recommendations(conn, session_date)
            except Exception as e:
                stats["error"] = "expire: {}".format(str(e)[:120])

        # 1. Create the immutable record for anything that qualifies today.
        for _, row in df.iterrows():
            if not bool(_clean(row.get("Buy_Ready"))):
                continue
            price = _num(row, "Suggested_Buy_Price") or _num(row, "Close_Price")
            if not price or price <= 0:
                continue
            bar = str(_clean(row.get("Data_Date")) or session_date or "")[:10]
            try:
                _, created = record_recommendation(
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
            except LedgerError as e:
                stats["error"] = "record: {}".format(str(e)[:120])

        # 2. Hang the frozen view off every row we have one for -- including
        #    names that are no longer buyable, because "what did you originally
        #    tell me" is exactly the question a held position asks.
        known = {}
        for rec in conn.execute(
                "SELECT * FROM recommendations WHERE strategy = ? "
                "AND status IN ('active','converted')", (scan_mode,)):
            known[rec["stock_id"]] = rec

        ids, buys, stops, targets, dates, statuses, valids = ([] for _ in range(7))
        for sid in df["Stock_ID"].astype(str):
            rec = known.get(sid.strip())
            if rec is None:
                for bucket in (ids, buys, stops, targets, dates, statuses, valids):
                    bucket.append(None)
                continue
            stats["attached"] += 1
            ids.append(rec["recommendation_id"])
            buys.append(float(rec["initial_buy_price"]))
            # The frozen levels are PUBLISHED as order levels, so they have to
            # be prices the exchange quotes. Records written before the tick
            # ladder shipped (2026-09-21) carry raw multiplications -- 1815's
            # Initial_Target_Price was one of them -- and an unplaceable price
            # is not a plan. The ledger row itself is untouched: this rounds
            # the value on its way to the screen, by at most one tick, and the
            # direction follows the same rule as everywhere else (a stop down,
            # a target up, never flattering the level).
            stops.append(_on_ladder(rec["initial_stop_price"], "down",
                                    sid.strip()))
            targets.append(_on_ladder(rec["initial_target_price"], "up",
                                      sid.strip()))
            dates.append(rec["first_qualified_session"])
            statuses.append(rec["status"])
            valids.append(rec["valid_until_session"])

        for col, values in zip(REC_COLUMNS,
                               (ids, buys, stops, targets, dates, statuses, valids)):
            df[col] = values
    except Exception as e:
        stats["error"] = str(e)[:160]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return df, stats


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
    if stats.get("error"):
        return "recommendations: {} created, {} attached, error: {}".format(
            stats.get("created", 0), stats.get("attached", 0), stats["error"])
    return "recommendations: {} created, {} attached, {} expired".format(
        stats.get("created", 0), stats.get("attached", 0),
        stats.get("expired", 0))
