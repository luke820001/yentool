"""
Store the WHOLE market's daily bar, not just the names the scan analysed.
ASCII only.

The gap this closes (owner, 2026-09-21): "the scanner should really be
scanning all of the market's data ... even for a stock the system never
recommended, help me with advice on what I bought."

Until now `price_volume.db` only grew for the ~300 candidates each scan looked
at. Every other stock kept whatever bar it had from the last time it happened
to be a candidate, so a holding outside the turnover pool slowly went stale and
nothing -- not its moving averages, not its support levels -- could be computed
for it.

It turns out the daily bar for EVERY listed stock is already one request per
exchange. `market_filter.fetch_full_market` calls both endpoints on every scan
and then throws away the open, high and low:

    TWSE  STOCK_DAY_ALL          OpeningPrice / HighestPrice / LowestPrice
    TPEX  mainboard_daily_close  Open / High / Low

So this module re-reads those same two payloads, keeps the full bar, and
upserts it. One extra pair of requests per scan, and the whole market stays
current instead of only the shortlist.

What is deliberately NOT stored: warrants and other six-digit instruments, and
any row without a positive price. ETFs ARE stored -- somebody can hold 0050,
and the scanner's own selection filters them out separately.
"""
import sqlite3

import pandas as pd

TSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
OTC_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

# Minimum rows a snapshot must have before it is allowed to write anything, so
# a half-empty response can never overwrite good data (same reasoning as
# market_filter.FEED_MIN_ROWS).
MIN_ROWS = {"TSE": 500, "OTC": 500}


def _num(x):
    try:
        v = float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _roc_to_iso(value):
    """'1150918' -> '2026-09-18'. Returns None for anything else."""
    s = str(value or "").strip()
    if len(s) == 7 and s.isdigit():
        return "{:04d}-{}-{}".format(int(s[:3]) + 1911, s[3:5], s[5:7])
    if len(s) == 8 and s.isdigit():
        return "{}-{}-{}".format(s[:4], s[4:6], s[6:8])
    if len(s) == 10 and s[4] == "-":
        return s
    return None


def _keep_code(code):
    """A tradeable instrument somebody could hold, not a warrant.

    4-digit ordinary shares, 5-digit (emerging/innovation board), and the
    00-prefixed ETF codes -- which run from FOUR characters (0050, 0056)
    through five (00878) to six with a trailing letter (00400A). Six-digit
    numeric codes are warrants and are dropped.
    """
    c = str(code or "").strip()
    if not c:
        return False
    if c.startswith("00"):
        return 4 <= len(c) <= 7
    return c.isdigit() and len(c) in (4, 5)


def parse_tse(raw):
    """TWSE STOCK_DAY_ALL -> DataFrame[date, stock_id, open, high, low, close,
    volume_share, Volume_Lot]."""
    rows = []
    for r in raw or []:
        code = r.get("Code")
        if not _keep_code(code):
            continue
        iso = _roc_to_iso(r.get("Date"))
        o, h, l, c = (_num(r.get(k)) for k in
                      ("OpeningPrice", "HighestPrice", "LowestPrice", "ClosingPrice"))
        v = _num(r.get("TradeVolume"))
        if not iso or not c or c <= 0 or v is None:
            continue
        # A suspended name reports a close with no trades; keep it out rather
        # than write a bar the market did not make.
        if v <= 0:
            continue
        o = o if o and o > 0 else c
        h = h if h and h > 0 else max(o, c)
        l = l if l and l > 0 else min(o, c)
        rows.append({"date": iso, "stock_id": str(code).strip(),
                     "open": o, "high": h, "low": l, "close": c,
                     "volume_share": v, "Volume_Lot": round(v / 1000.0, 3)})
    return pd.DataFrame(rows)


def parse_otc(raw):
    """TPEX mainboard daily close quotes -> the same frame."""
    rows = []
    for r in raw or []:
        code = r.get("SecuritiesCompanyCode")
        if not _keep_code(code):
            continue
        iso = _roc_to_iso(r.get("Date"))
        o, h, l, c = (_num(r.get(k)) for k in ("Open", "High", "Low", "Close"))
        v = _num(r.get("TradingShares"))
        if not iso or not c or c <= 0 or v is None or v <= 0:
            continue
        o = o if o and o > 0 else c
        h = h if h and h > 0 else max(o, c)
        l = l if l and l > 0 else min(o, c)
        rows.append({"date": iso, "stock_id": str(code).strip(),
                     "open": o, "high": h, "low": l, "close": c,
                     "volume_share": v, "Volume_Lot": round(v / 1000.0, 3)})
    return pd.DataFrame(rows)


def fetch_snapshot(log=print):
    """Both exchanges' latest daily bar for every instrument. Returns
    (DataFrame, health) where health says what each board contributed."""
    from scanner.market_filter import _fetch_json
    tse = parse_tse(_fetch_json(TSE_URL))
    otc = parse_otc(_fetch_json(OTC_URL))
    health = {"TSE": len(tse), "OTC": len(otc), "ok": True, "skipped": []}
    frames = []
    for name, df in (("TSE", tse), ("OTC", otc)):
        if len(df) < MIN_ROWS[name]:
            health["ok"] = False
            health["skipped"].append(name)
            log("  [snapshot] {} returned {} rows, below the {} floor -- not "
                "storing that board".format(name, len(df), MIN_ROWS[name]))
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame(), health
    return pd.concat(frames, ignore_index=True), health


def store_snapshot(price_db, df, log=print):
    """Upsert the snapshot into price_volume.db. Returns rows written.

    Existing rows for the same (stock_id, date) are replaced, so re-running a
    scan is idempotent and a corrected exchange figure wins.
    """
    if df is None or df.empty:
        return 0
    # Never write a date the market as a whole did not trade (the 2026-09-20
    # placeholder-bar lesson). A snapshot carries one date per board, and a
    # board that published is by definition a session for that board.
    conn = sqlite3.connect(str(price_db), timeout=60)
    try:
        cols = ["date", "stock_id", "open", "high", "low", "close",
                "volume_share", "Volume_Lot"]
        recs = [tuple(str(r[c]) if c in ("date", "stock_id") else float(r[c])
                      for c in cols)
                for _, r in df[cols].iterrows()]
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_data_sid_date "
                     "ON data(stock_id, date)")
        conn.executemany(
            "INSERT OR REPLACE INTO data (date, stock_id, open, high, low, "
            "close, volume_share, Volume_Lot) VALUES (?,?,?,?,?,?,?,?)", recs)
        conn.commit()
        return len(recs)
    except sqlite3.IntegrityError as e:
        # A pre-existing duplicate (stock_id, date) blocks the unique index.
        # Say so and fall back to a plain insert of only the missing rows,
        # rather than failing the scan over a housekeeping problem.
        log("  [snapshot] unique index unavailable ({}); writing new rows only"
            .format(str(e)[:70]))
        conn.rollback()
        return _store_missing_only(conn, df, log)
    finally:
        conn.close()


def _store_missing_only(conn, df, log=print):
    have = set()
    dates = sorted({str(d) for d in df["date"]})
    for d in dates:
        have |= {(str(s), d) for (s,) in conn.execute(
            "SELECT stock_id FROM data WHERE date = ?", (d,))}
    new = [r for _, r in df.iterrows()
           if (str(r["stock_id"]), str(r["date"])) not in have]
    if not new:
        return 0
    conn.executemany(
        "INSERT INTO data (date, stock_id, open, high, low, close, "
        "volume_share, Volume_Lot) VALUES (?,?,?,?,?,?,?,?)",
        [(str(r["date"]), str(r["stock_id"]), float(r["open"]), float(r["high"]),
          float(r["low"]), float(r["close"]), float(r["volume_share"]),
          float(r["Volume_Lot"])) for r in new])
    conn.commit()
    return len(new)


def refresh_market(price_db, log=print):
    """Fetch and store in one call. Returns (rows_written, health)."""
    df, health = fetch_snapshot(log=log)
    if df.empty:
        return 0, health
    n = store_snapshot(price_db, df, log=log)
    log("  [snapshot] stored {} whole-market bars (TSE {} / OTC {})".format(
        n, health["TSE"], health["OTC"]))
    return n, health
