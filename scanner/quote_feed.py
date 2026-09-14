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

The feed covers the WHOLE universe rather than a chosen subset, because a
chosen subset is readable: this file is public, and a name in it that is not in
today's shortlist could only be there because somebody holds it. Publishing
everything makes presence carry no information. Measured 2026-09-09: 1,953
stocks x 30 sessions = 334KB raw, 102KB gzipped over the wire -- cheap enough
that the privacy property can hold by construction instead of by vigilance.
"""
import json
import os
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

    `stock_ids=None` means EVERY stock with a bar in the window. That is the
    mode the published feed uses, and it is a privacy property, not an
    optimisation: a feed containing a chosen subset lets a reader infer why
    each name is there, and for a name that is not in today's shortlist the
    only available reason is that somebody holds it. Publishing everything
    makes presence in the feed carry no information about anyone.

    Closes are positional against `sessions`, with null where a stock has no
    bar for that date. A null is meaningful and must survive to the client: it
    is the difference between "did not trade" and "we never looked", and the
    report is emphatic that filling gaps with the previous price is a
    falsification (section 4.3).
    """
    everything = stock_ids is None
    wanted = set() if everything else {
        str(s).strip() for s in stock_ids if str(s).strip()}
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
    if not wanted and not everything:
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
            if not everything and sid not in wanted:
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
    # In universe mode there is no request list to fall short of, and listing
    # what the market does not have would be a second inference channel, so it
    # stays empty by construction.
    payload["missing"] = ([] if everything
                          else sorted(wanted - set(payload["closes"])))
    payload["scope"] = "universe" if everything else "requested"
    payload["count"] = len(payload["closes"])
    payload["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return payload


def write_quote_feed(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return str(path)


# ---------------------------------------------------------------------------
# Keeping dropped-out names priced (F04, 2026-09-14 column audit)
# ---------------------------------------------------------------------------
def stale_tracked_ids(price_db, ledger_db, scan_mode, as_of,
                      sessions=FEED_SESSIONS):
    """{stock_id: market} for stocks the ledger picked inside the feed window
    whose newest bar in price_db is older than `as_of`.

    Only today's candidates get fetched by the scan, so a name that fell off
    the list stops updating; its trailing closes then publish as null and a
    holder of it loses the valuation. This finds exactly those names."""
    as_of = str(as_of or "")[:10]
    if not as_of or not os.path.exists(str(price_db))             or not os.path.exists(str(ledger_db)):
        return {}
    try:
        with sqlite3.connect(str(price_db), timeout=30) as conn:
            window = _recent_sessions(conn, sessions)
    except Exception:
        return {}
    if not window:
        return {}
    try:
        with sqlite3.connect(str(ledger_db), timeout=30) as conn:
            rows = conn.execute(
                "SELECT stock_id, MAX(market) FROM picks "
                "WHERE scan_mode = ? AND bar_date >= ? GROUP BY stock_id",
                (scan_mode, window[0])).fetchall()
    except Exception:
        return {}
    ids = {str(s).strip(): (str(m) if m in ("TSE", "OTC") else "TSE")
           for s, m in rows if s and str(s).strip()}
    if not ids:
        return {}
    try:
        with sqlite3.connect(str(price_db), timeout=30) as conn:
            latest = {str(s).strip(): str(d)[:10] for s, d in conn.execute(
                "SELECT stock_id, MAX(date) FROM data WHERE stock_id IN (%s) "
                "GROUP BY stock_id" % ",".join("?" * len(ids)), list(ids))}
    except Exception:
        return {}
    return {sid: mkt for sid, mkt in ids.items() if latest.get(sid, "") < as_of}


def refresh_tracked_prices(scan_mode, as_of, price_db=None, ledger_db=None):
    """Fetch the stale names from stale_tracked_ids(). Returns how many were
    fetched. Never raises; a failed top-up just leaves the gap visible (the
    column check reports it as quotes_gap_tracked)."""
    from config.settings import PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE
    price_db = price_db or PRICE_VOLUME_FILE
    ledger_db = ledger_db or SIGNAL_LEDGER_FILE
    stale = stale_tracked_ids(price_db, ledger_db, scan_mode, as_of)
    if not stale:
        return 0
    try:
        from ingestion.price_volume_multi import multi_fetch_and_save_batch
        fetched = multi_fetch_and_save_batch(sorted(stale), stale)
        return len(fetched or ())
    except Exception as e:
        print("  [quotes] top-up of {} dropped-out name(s) failed: {}".format(
            len(stale), str(e)[:100]))
        return 0
