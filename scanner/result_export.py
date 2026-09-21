"""
Export the latest scan result for offline review and for the mobile PWA.

Only the most recent version is kept (the files are overwritten each scan), so
the folder never accumulates clutter. UTF-8 BOM is used for the CSV so Excel
opens the Chinese stock names correctly. All Python strings ASCII.

2026-09-09 audit, F18. Both exporters used to begin with

    if df is None or df.empty:
        return None

which meant a day with zero picks published NOTHING -- and the phone, finding
no new file, went on showing yesterday's shortlist under yesterday's data date
as if it were today's answer. A healthy market day with no qualifying setups is
a real, informative result ("nothing passed the gate today"), and it is not the
same event as "the scan crashed". They now produce visibly different output:
a zero-row publish with count=0 and a fresh timestamp, versus no publish at all
plus a non-zero exit code from scan_headless.
"""
import json
from datetime import datetime

import pandas as pd

from config.settings import (
    SCAN_RESULTS_DIR, SCAN_RESULT_FILE, MOBILE_DIR, MOBILE_DATA_FILE,
    MOBILE_QUOTES_FILE, PRICE_VOLUME_FILE, PORTFOLIO_LEDGER_FILE,
)


def export_scan_result(df, scan_mode="", reports=None, degraded=None,
                       session_date=None, strategy_version="",
                       quality=None, quotes_meta=None, tracked=None):
    """
    Write the full result DataFrame (all computed columns) to SCAN_RESULT_FILE,
    overwriting any previous version. Two context columns (mode + timestamp) are
    prepended so a saved file is self-describing. A JSON twin is also written
    for the mobile PWA, plus the quote feed (see export_scan_result_json).

    `df` may legitimately be EMPTY -- that publishes a zero-pick day rather
    than leaving stale files in place. Only `None` means "no result to publish".

    Returns the written CSV path.
    """
    if df is None:
        return None

    SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = df.copy()
    out.insert(0, "Scan_Mode", scan_mode)
    out.insert(1, "Scan_Time", scan_time)

    # utf-8-sig => Excel detects UTF-8 and renders Chinese names correctly.
    # An empty frame still writes its header row, so the file's mtime and the
    # Scan_Time column both say the scan really ran today.
    out.to_csv(SCAN_RESULT_FILE, index=False, encoding="utf-8-sig")

    try:
        export_scan_result_json(df, scan_mode, scan_time, reports=reports,
                                degraded=degraded, session_date=session_date,
                                strategy_version=strategy_version,
                                quality=quality, quotes_meta=quotes_meta,
                                tracked=tracked)
    except Exception as e:
        # A mobile-feed hiccup must never break the primary CSV export.
        print("  [export] mobile json failed: {}".format(e))

    return str(SCAN_RESULT_FILE)


def _regime():
    """Market regime for the phone's entry gate, with its own freshness.

    Failing closed matters here: the phone draws the "new positions allowed"
    banner from this, and an unreadable regime is not a tailwind (F15).
    """
    try:
        from scanner.market_regime import get_market_regime
        return get_market_regime() or {}
    except Exception:
        return {}


def _calendar_tail(n=40):
    """Real trading dates so the phone can place a holding on the calendar.

    Extrapolating weekdays is what made the phone run a day ahead through every
    typhoon closure (F14); the authoritative tail is the antidote.
    """
    try:
        from scanner.holding_tracker import _trading_calendar
        return _trading_calendar()[-n:]
    except Exception:
        return []


def _publish_quotes(df, names=None):
    """Write quotes.json covering the WHOLE price universe, not a chosen subset.

    F04 needs a holding to stay priced after it drops off the shortlist. The
    obvious way to do that is to publish the scan UNION whatever the ledger
    tracks -- and that is what this did first. It leaks.

    quotes.json is served from a public GitHub Pages site that several people
    read. A stock that is NOT in today's shortlist but IS in the feed can only
    be there because somebody holds it, so the file quietly announces the
    holdings list to every viewer. The prices never left the device; the
    position ITSELF did. A `tracked_count` field spelled out the number too.

    Publishing every stock removes the inference entirely: presence in the feed
    says nothing about anyone, because everything is present. Measured cost is
    1,953 stocks x 30 sessions = 334KB raw, 102KB gzipped over the wire. That is
    a small price for a privacy property that holds by construction rather than
    by remembering to be careful.

    It is also strictly better for F04: a position in a stock the scanner has
    NEVER picked is now priced too, which the union approach could only manage
    if the server-side ledger happened to know about it.
    """
    from scanner.quote_feed import build_quote_feed, write_quote_feed
    payload = build_quote_feed(PRICE_VOLUME_FILE, None, names=names)
    write_quote_feed(MOBILE_QUOTES_FILE, payload)
    return payload


def export_scan_result_json(df, scan_mode="", scan_time="", reports=None,
                            degraded=None, session_date=None,
                            strategy_version="", quality=None,
                            quotes_meta=None, tracked=None):
    """
    Write the scan result as JSON for the mobile PWA. Structure:
        {"meta": {...}, "rows": [...]}

    `reports` is an optional {market: text} map (ALL/OTC/TSE) of AI reports.
    `degraded` is a short ASCII reason string when an exchange feed failed its
    sanity floor this run (the phone shows a data-fault banner and the missing
    market must NOT be read as "no candidates today"). None = healthy.
    `quotes_meta` is an optional dict merged into meta.quotes -- e.g.
    {"source_ended": {stock_id: last_bar_date}} from the pre-export top-up, so
    the column check can tell a halted name from an un-refreshed one.
    `tracked` is an optional DataFrame of FULL rows for names that dropped off
    the list but were picked recently, published under "tracked" so a holder
    of one keeps its chips, moving averages and exit plan (owner, 2026-09-21:
    "if I already hold something, it should still give me advice on it"). The
    scanner never learns what is actually held -- it publishes the superset and
    the phone matches its own private holdings against it.
    NaN/inf are coerced to null so the JSON is valid. Returns the written path.
    """
    MOBILE_DIR.mkdir(parents=True, exist_ok=True)

    if df is None or df.empty:
        rows = []
        names = {}
    else:
        clean = df.replace([float("inf"), float("-inf")], pd.NA)
        rows = json.loads(clean.to_json(orient="records", force_ascii=False))
        names = {}
        if "Stock_ID" in df.columns and "Stock_Name" in df.columns:
            names = {str(a).strip(): str(b) for a, b
                     in zip(df["Stock_ID"], df["Stock_Name"])}

    reg = _regime()
    quotes = {}
    try:
        quotes = _publish_quotes(df, names=names)
    except Exception as e:
        print("  [quotes] failed: {}".format(str(e)[:100]))

    # Data date actually represented by the rows, distinct from the wall-clock
    # time the scan ran (report section 8: Data_Date and Scan_Time are not the
    # same fact and both belong on screen).
    data_date = ""
    if rows:
        dates = [str(r.get("Data_Date") or "")[:10] for r in rows]
        dates = [d for d in dates if d]
        data_date = max(dates) if dates else ""

    payload = {
        "meta": {
            "mode": scan_mode,
            "strategy_version": strategy_version,
            "scan_time": scan_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "session_date": str(session_date or "")[:10] or data_date,
            "data_date": data_date,
            "count": len(rows),
            # A zero-pick day is a RESULT. The phone renders "0 setups today"
            # instead of silently keeping yesterday's list on screen.
            "empty_ok": len(rows) == 0 and not degraded,
            "regime": {
                "ok": bool(reg.get("ok", False)),
                "risk_on": bool(reg.get("risk_on", False)),
                "enter_ok": bool(reg.get("enter_ok", False)),
                "above20": bool(reg.get("above20", True)),
                "str20": reg.get("str20"),
                "strong": bool(reg.get("strong", False)),
                "text": reg.get("text", ""),
                # Added by F15 so the phone can tell a real tailwind from a
                # stale cache that merely looks like one.
                "as_of_date": reg.get("as_of_date"),
                "is_current": reg.get("is_current"),
            },
            "calendar_tail": _calendar_tail(),
            "degraded": degraded,
            "quality": quality or {},
            "quotes": {
                "file": "quotes.json",
                "as_of": quotes.get("as_of", ""),
                "count": quotes.get("count", 0),
                "sessions": len(quotes.get("sessions", [])),
                "missing": quotes.get("missing", []),
            },
            "reports": reports or {},
        },
        "rows": rows,
    }
    if tracked is not None and not tracked.empty:
        t = tracked.replace([float("inf"), float("-inf")], pd.NA)
        payload["tracked"] = json.loads(
            t.to_json(orient="records", force_ascii=False))
        payload["meta"]["tracked"] = {
            "count": int(len(tracked)),
            "built_for": str(session_date or "")[:10],
            "note": "recently recommended names no longer on the list; "
                    "full data so a holder keeps its chips and exit plan",
        }
    else:
        # A caller that does not build the tracked block must not DELETE one.
        # gui/scan_worker.py runs the same export without it, so opening the
        # desktop app after a cloud scan silently stripped the dropped-out
        # holdings -- the rows a holder depends on -- from the payload the
        # phone reads. Carry the previous block forward, and say which session
        # it was built for so nothing can pass it off as today's.
        try:
            with open(MOBILE_DATA_FILE, encoding="utf-8") as f:
                prev = json.load(f)
            old = prev.get("tracked")
            if isinstance(old, list) and old:
                payload["tracked"] = old
                meta_old = dict((prev.get("meta") or {}).get("tracked") or {})
                meta_old["count"] = len(old)
                meta_old["carried_forward"] = True
                meta_old.setdefault("built_for", str(
                    (prev.get("meta") or {}).get("session_date") or "")[:10])
                payload["meta"]["tracked"] = meta_old
        except Exception:
            pass
    if quotes_meta:
        payload["meta"]["quotes"].update(quotes_meta)
    with open(MOBILE_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return str(MOBILE_DATA_FILE)
