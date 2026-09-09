"""
Publish a small close-price feed alongside the scan result. ASCII only.

This is the fix for F04. The phone looked every price up in the CURRENT scan
rows (`ALL_ROWS.find(r => r.Stock_ID === h.id)`), so the moment a holding fell
out of the top 80 its price, its P&L and its exit levels all rendered as "-".
The position did not stop existing; only our willingness to price it did.

Report section 9.3 step 2 states the requirement directly: the daily update
must cover "candidates, open positions, unfinished ten-day tracking and history
still to be backfilled" -- not just whatever the scan happened to select.

So the feed carries a WINDOW of recent closes, not a single quote:

  * a single quote only answers "what is it worth now"; a window also answers
    "what was my P&L on each of the last ten days", which is the whole of
    report section 6 and cannot be reconstructed from one number.
  * it lets the phone rebuild a position's daily marks entirely offline, so no
    private holding has to be sent anywhere to be valued. That is what makes
    the static GitHub Pages deployment viable at all.

Size: ~600 stocks x 30 sessions of one float is a few hundred KB before gzip,
which is an acceptable price for the phone never going blind again.
"""
import json
import sqlite3
from datetime import datetime

# Sessions of history published per stock. 30 comfortably covers the 10-bar
# hold plus the 20-bar delay cap, with room to show a position that was opened
# before the user last opened the app.
FEED_SESSIONS = 30


def _recent_sessions(conn, limit):
    rows = conn.execute(
        "SELECT DISTINCT date FROM data ORDER BY date DESC LIMIT ?",
        (int(limit),)).fetchall()
    return sorted(str(r[0])[:10] for r in rows)


def build_quote_feed(price_db, stock_ids, sessions=FEED_SESSIONS,
                     names=None):
    """Return the quote-feed payload for `stock_ids`.

    Closes are positional against `sessions`, with null where a stock has no
    bar for that date. A null is meaningful and must survive to the client: it
    is the difference between "did not trade" and "we never looked", and the
    report is emphatic that filling gaps with the previous price is a
    falsification (section 4.3).
    """
    wanted = {str(s).strip() for s in stock_ids if str(s).strip()}
    payload = {
        "as_of": "",
        "sessions": [],
        "closes": {},
        "names": {},
        # price_volume.db currently mixes yfinance adjusted closes with the
        # exchanges' unadjusted ones and keeps no source column (F08). Until
        # that is separated we must not claim these are reconcilable raw
        # prices, so the basis is declared unverified rather than "raw".
        "price_basis": "unverified",
        "note": "close only; see F08 before reconciling against a broker statement",
    }
    if not wanted:
        return payload

    conn = None
    try:
        conn = sqlite3.connect(str(price_db), timeout=30)
        session_list = _recent_sessions(conn, sessions)
        if not session_list:
            return payload
        payload["sessions"] = session_list
        payload["as_of"] = session_list[-1]

        index = {d: i for i, d in enumerate(session_list)}
        # One scan of the recent window beats one query per stock: at ~600
        # names the per-stock form was the slowest step in the export.
        cursor = conn.execute(
            "SELECT stock_id, date, close FROM data WHERE date >= ?",
            (session_list[0],))
        for stock_id, date, close in cursor:
            sid = str(stock_id).strip()
            if sid not in wanted:
                continue
            slot = index.get(str(date)[:10])
            if slot is None or close is None:
                continue
            series = payload["closes"].get(sid)
            if series is None:
                series = [None] * len(session_list)
                payload["closes"][sid] = series
            try:
                series[slot] = round(float(close), 2)
            except (TypeError, ValueError):
                continue
    except Exception as e:
        payload["note"] = "quote feed incomplete: {}".format(str(e)[:100])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if names:
        payload["names"] = {sid: names[sid] for sid in payload["closes"]
                            if sid in names}
    # Names we were asked for but found no bar for at all. The phone shows
    # these as "price unavailable" rather than silently omitting the position.
    payload["missing"] = sorted(wanted - set(payload["closes"]))
    payload["count"] = len(payload["closes"])
    payload["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return payload


def write_quote_feed(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return str(path)
