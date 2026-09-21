"""
Full data for stocks you may still HOLD but that dropped off today's list.
ASCII only.

The gap this closes (owner, 2026-09-21): "1815 was recommended a few days ago,
I bought it, and now the app tells me it is not on the list -- so I have no
other data to refer to. If I already hold something, it should still give me
advice on it."

That was exactly right. Hysteresis drops a name once it leaves the top 80, and
from that moment the payload carried only its PRICE (quote_feed tops those up
so the valuation survives). Every other column -- institutional flow, the
moving averages the ride rule needs, the exit plan, the indicator panel --
simply stopped existing for it, at precisely the point in the trade where the
holder needs them most.

The scanner cannot read the owner's holdings: they live in the phone's own
database and never leave it. What the scanner CAN do is cover the superset --
every stock it recommended recently is a stock the owner might be holding --
and publish full rows for all of them. The phone then matches its private
holdings against that set. Nothing about the position leaves the device.

The window is the exit cap plus a margin, so a name stays covered for as long
as any live position in it could still be open.
"""
from datetime import datetime

TRACKED_SESSIONS = 30      # >= the 20-bar exit cap, with room for weekends
MAX_TRACKED = 60           # a sane ceiling on the extra work per scan


def _sessions(conn, limit):
    rows = conn.execute(
        "SELECT date, COUNT(*) FROM data GROUP BY date ORDER BY date DESC "
        "LIMIT ?", (limit * 3,)).fetchall()
    try:
        from scanner.data_integrity import nonsession_dates
        skip = set(nonsession_dates(rows))
    except Exception:
        skip = set()
    dates = sorted({str(d)[:10] for d, _ in rows} - skip)
    return dates[-limit:]


def recent_pick_ids(price_db, ledger_db, scan_mode, sessions=TRACKED_SESSIONS,
                    with_dates=False):
    """Stock ids this mode picked within the last `sessions` trading days.

    These are the names a live position could still be open in, so they are
    the ones worth carrying full data for.

    With `with_dates`, returns {stock_id: last pick date} instead of a set, so
    a caller that has to drop some of them can drop the OLDEST.
    """
    import os
    import sqlite3
    empty = {} if with_dates else set()
    if not os.path.exists(str(price_db)) or not os.path.exists(str(ledger_db)):
        return empty
    # `with sqlite3.connect(...)` manages the TRANSACTION, not the connection:
    # it leaves the handle open, which on Windows keeps the file locked. Close
    # it explicitly.
    conn = None
    try:
        conn = sqlite3.connect(str(price_db), timeout=30)
        window = _sessions(conn, sessions)
    except Exception:
        return empty
    finally:
        if conn is not None:
            conn.close()
    if not window:
        return empty
    conn = None
    try:
        conn = sqlite3.connect(str(ledger_db), timeout=30)
        rows = conn.execute(
            "SELECT stock_id, MAX(bar_date) FROM picks WHERE scan_mode = ? "
            "AND bar_date >= ? GROUP BY stock_id", (scan_mode, window[0])
        ).fetchall()
    except Exception:
        return empty
    finally:
        if conn is not None:
            conn.close()
    if with_dates:
        return {str(r[0]): str(r[1] or "")[:10] for r in rows if r and r[0]}
    return {str(r[0]) for r in rows if r and r[0]}


def split_tracked(verified, published, tracked_ids, limit=MAX_TRACKED,
                  picked_on=None):
    """Rows for names we verified this scan that are NOT on the published list
    but were picked recently. Returns a DataFrame (possibly empty).

    `verified` is the full verify_candidates output, before the mode filter and
    hysteresis removed anything.

    `picked_on` is {stock_id: last pick date}; with it, the cap keeps the most
    recently picked names. Without it the cap keeps whatever order `verified`
    arrived in, which is turnover rank -- the comment used to claim "the most
    recently relevant ones" while doing exactly that, so on a heavy-pick day a
    name the owner actually holds could be dropped for a busier one.
    """
    if verified is None or verified.empty or not tracked_ids:
        return verified.iloc[0:0] if verified is not None else None
    shown = set()
    if published is not None and not published.empty and "Stock_ID" in published:
        shown = {str(s) for s in published["Stock_ID"]}
    want = {str(s) for s in tracked_ids} - shown
    if not want:
        return verified.iloc[0:0]
    out = verified[verified["Stock_ID"].astype(str).isin(want)].copy()
    if len(out) > limit:
        # The cap only ever bites if the ledger has an unusual burst of picks.
        if picked_on:
            out["_picked"] = [str(picked_on.get(str(s), "")) for s in out["Stock_ID"]]
            out = out.sort_values("_picked", ascending=False).drop(columns="_picked")
        out = out.head(limit)
    return out.reset_index(drop=True)


def annotate_tracked(df, scan_mode):
    """Give a tracked row the same treatment a listed row gets, so the phone
    can render it with the same code path: trade levels, holding day, exit
    plan, chip readout. Buy_Ready is forced false -- a name that is no longer
    on the list is not a new buy, whatever its indicators say."""
    if df is None or df.empty:
        return df
    from scanner.scan_mode import add_trade_columns
    from scanner.holding_tracker import annotate_holding
    out = add_trade_columns(df, scan_mode)
    try:
        out = annotate_holding(out, scan_mode)
    except Exception as e:
        print("  [tracked] holding annotation skipped: {}".format(e))
    try:
        from scanner.chip_signal import annotate_chip_action
        out = annotate_chip_action(out, scan_mode)
    except Exception as e:
        print("  [tracked] chip annotation skipped: {}".format(e))
    out["Buy_Ready"] = False
    out["Buy_Block"] = "dropped"
    return out


def summary(df):
    if df is None or df.empty:
        return {"count": 0, "ids": [], "as_of": None}
    return {
        "count": int(len(df)),
        "ids": [str(s) for s in df["Stock_ID"]],
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
