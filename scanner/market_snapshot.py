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

    An existing (stock_id, date) row has its SIX quote columns updated and
    everything else left alone; a new one is inserted.

    Two things this deliberately does NOT do, both found 2026-09-21 by audit:

    1. It does not `INSERT OR REPLACE`. The `data` table has twelve columns
       and the snapshot supplies eight, and OR REPLACE deletes the old row
       before inserting, so MA5_Volume, Min_Volume_20, Max_Price_20 and
       Min_Price_20 were being blanked on every row it touched -- 990 of
       1,292 rows on 2026-09-21. chip_verifier recomputes those four before
       it uses them, which is the only reason nothing broke, but
       analyzer/signal_evaluator and the research tools read them straight
       from the store.

    2. It does not create the unique index any more. That statement was
       character-for-character the one in storage/data_store.migrate_stock_store,
       a function whose docstring says nothing in the scan path may call it,
       because it backs the file up first and stamps user_version. A routine
       price save had quietly become a migration -- with no backup, and
       leaving get_schema_version() reporting 0 on a half-migrated store. The
       update-then-insert below needs no index at all.
    """
    if df is None or df.empty:
        return 0
    # Never write a date the market as a whole did not trade (the 2026-09-20
    # placeholder-bar lesson). A snapshot carries one date per board, and a
    # board that published is by definition a session for that board.
    cols = ["date", "stock_id", "open", "high", "low", "close",
            "volume_share", "Volume_Lot"]
    conn = sqlite3.connect(str(price_db), timeout=60)
    try:
        written = 0
        for _, r in df[cols].iterrows():
            date, sid = str(r["date"]), str(r["stock_id"])
            vals = tuple(float(r[c]) for c in cols[2:])
            cur = conn.execute(
                "UPDATE data SET open = ?, high = ?, low = ?, close = ?, "
                "volume_share = ?, Volume_Lot = ? WHERE stock_id = ? AND date = ?",
                vals + (sid, date))
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO data (date, stock_id, open, high, low, close, "
                    "volume_share, Volume_Lot) VALUES (?,?,?,?,?,?,?,?)",
                    (date, sid) + vals)
            written += 1
        conn.commit()
        return written
    except sqlite3.Error as e:
        log("  [snapshot] not stored ({}); the scan continues on the history "
            "it already has".format(str(e)[:80]))
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()


def refill_rolling_columns(price_db, log=print):
    """Recompute the four stored rolling columns wherever they are NULL.

    `data` carries MA5_Volume, Min_Volume_20, Max_Price_20 and Min_Price_20
    alongside the raw bar. chip_verifier recomputes all four from close and
    Volume_Lot before it uses them (they drift when prices are re-fetched), so
    the scan itself never noticed that the snapshot had been blanking them --
    but analyzer/signal_evaluator and the research tools read the STORED
    values. store_snapshot no longer destroys them; this heals what the old
    INSERT OR REPLACE already blanked, and fills them in for the whole market
    rather than only for names that once passed through the candidate path.

    Same definition as chip_verifier: rolling over the stock's own bars in
    date order, min_periods=1. Returns the number of rows updated.
    """
    conn = sqlite3.connect(str(price_db), timeout=120)
    try:
        need = conn.execute(
            "SELECT COUNT(*) FROM data WHERE MA5_Volume IS NULL "
            "OR Min_Volume_20 IS NULL OR Max_Price_20 IS NULL "
            "OR Min_Price_20 IS NULL").fetchone()[0]
        if not need:
            return 0
        df = pd.read_sql_query(
            "SELECT rowid AS rid, stock_id, date, close, Volume_Lot, "
            "MA5_Volume, Min_Volume_20, Max_Price_20, Min_Price_20 FROM data",
            conn)
        if df.empty:
            return 0
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["Volume_Lot"] = pd.to_numeric(df["Volume_Lot"], errors="coerce")
        df = df.sort_values(["stock_id", "date"])
        g = df.groupby("stock_id", sort=False)
        df["mx"] = g["close"].transform(lambda x: x.rolling(20, min_periods=1).max())
        df["mn"] = g["close"].transform(lambda x: x.rolling(20, min_periods=1).min())
        df["v5"] = g["Volume_Lot"].transform(lambda x: x.rolling(5, min_periods=1).mean())
        df["v20"] = g["Volume_Lot"].transform(lambda x: x.rolling(20, min_periods=1).min())
        # Only the rows that are actually missing a value get rewritten, and
        # only when the replacement is a real number. A row whose close or
        # volume is itself null can never be filled, so it must not keep the
        # whole table rewriting itself on every future scan.
        stored = ["MA5_Volume", "Min_Volume_20", "Max_Price_20", "Min_Price_20"]
        for col in stored:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        gap = df[stored].isna().any(axis=1)
        sub = df[gap]
        recs = []
        for r in sub.itertuples():
            vals = (r.v5, r.v20, r.mx, r.mn)
            if any(v != v for v in vals):
                continue
            recs.append((float(r.v5), float(r.v20), float(r.mx),
                         float(r.mn), int(r.rid)))
        if not recs:
            return 0
        conn.executemany(
            "UPDATE data SET MA5_Volume = ?, Min_Volume_20 = ?, "
            "Max_Price_20 = ?, Min_Price_20 = ? WHERE rowid = ?", recs)
        conn.commit()
        log("  [snapshot] refilled rolling columns on {} row(s) of {} missing"
            .format(len(recs), int(need)))
        return len(recs)
    except Exception as e:
        log("  [snapshot] rolling refill skipped: {}".format(str(e)[:80]))
        return 0
    finally:
        conn.close()


# A price disagreement this large is not rounding: it is two different bases
# (or two different days) for the same bar.
BASIS_TOLERANCE_PCT = 0.5


def compare_to_store(price_db, df, log=print, tol=BASIS_TOLERANCE_PCT):
    """Report stored closes that disagree with the exchange's own figure.

    price_volume.db is written by TWO sources: this snapshot, which stores the
    exchange's raw published bar, and ingestion/price_volume_multi, which asks
    yfinance with auto_adjust=True and therefore stores a DIVIDEND-ADJUSTED
    series. Both write the same table, and the candidate path runs last, so a
    stock that goes ex-dividend keeps back-adjusted history and accumulates
    raw bars after it -- a step in the series the size of the dividend, which
    in Taiwan is routinely 4-6%. Nothing would have shown that.

    This does not pick a basis; choosing one is a strategy decision, because
    every validated backtest was measured on the stored series. It makes the
    disagreement VISIBLE, per scan, with the names attached. Measured on
    2026-09-21: 19 of 1,340 rows, so this is a monitor, not an alarm.

    Returns {"checked": n, "off": [...], "worst": pct}.
    """
    out = {"checked": 0, "off": [], "worst": 0.0, "off_count": 0}
    if df is None or df.empty:
        return out
    try:
        # KEYED ON (stock, date). The two boards do not always publish the
        # same session -- on 2026-09-21 the TSE endpoint was still serving
        # 09-18 while the OTC one had 09-21 -- so comparing a whole frame
        # against one date lines stocks up against the wrong day and invents
        # differences that are simply the market having moved.
        feed = {(str(r.stock_id), str(r.date)[:10]): float(r.close)
                for r in df.itertuples() if r.close and float(r.close) > 0}
        dates = sorted({d for _, d in feed})
        conn = sqlite3.connect(str(price_db), timeout=60)
        try:
            rows = conn.execute(
                "SELECT stock_id, date, close FROM data WHERE date IN (%s)"
                % ",".join("?" * len(dates)), dates).fetchall()
        finally:
            conn.close()
        off = []
        for sid, d, stored in rows:
            f = feed.get((str(sid), str(d)[:10]))
            try:
                st = float(stored)
            except (TypeError, ValueError):
                continue
            if not f or not st:
                continue
            out["checked"] += 1
            pct = abs(st - f) / f * 100.0
            if pct > tol:
                off.append((round(pct, 2), str(sid), round(st, 2), round(f, 2)))
        off.sort(reverse=True)
        date = ", ".join(dates)
        out["off_count"] = len(off)
        out["off"] = off[:20]
        out["worst"] = off[0][0] if off else 0.0
        if off:
            log("  [snapshot] {} of {} stored close(s) differ from the "
                "exchange figure for {} by more than {}% (worst {}: {}% "
                "stored {} vs feed {})".format(
                    len(off), out["checked"], date, tol, off[0][1], off[0][0],
                    off[0][2], off[0][3]))
    except Exception as e:
        log("  [snapshot] basis comparison skipped: {}".format(str(e)[:80]))
    return out


def refresh_market(price_db, log=print):
    """Fetch and store in one call. Returns (rows_written, health).

    `health["frame"]` carries the fetched bars so the caller can RE-APPLY them
    after the candidate fetcher has run. See reassert_exchange_prices.
    """
    df, health = fetch_snapshot(log=log)
    health["frame"] = df
    if df.empty:
        return 0, health
    n = store_snapshot(price_db, df, log=log)
    log("  [snapshot] stored {} whole-market bars (TSE {} / OTC {})".format(
        n, health["TSE"], health["OTC"]))
    refill_rolling_columns(price_db, log=log)
    health["basis"] = compare_to_store(price_db, df, log=log)
    return n, health


def reassert_exchange_prices(price_db, df, log=print):
    """Put the exchange's own published bar back, after the candidate and
    backfill fetchers have overwritten it.

    Two writers share `data`. This module stores the price the exchange
    published. ingestion/price_volume_multi asks yfinance with
    auto_adjust=True and therefore stores a DIVIDEND-ADJUSTED series, and it
    runs later in the scan, so for every name it touches the adjusted value
    wins for dates the exchange has also published. Measured 2026-09-21 right
    after a 150-name backfill: 61 of 1,393 stored closes differed from the
    exchange figure by more than 0.5%, the worst by 9.03% (6538, stored 388.00
    against a published 426.50 -- an ex-dividend adjustment).

    A stock whose history is adjusted and whose newer bars are raw has a STEP
    in its series the size of the dividend, and a step is what moving averages,
    Ret_5D_Pct and the ride rule read as a move. Neither basis is wrong on its
    own; mixing them is. For a date the exchange has published, the exchange's
    number is the one the owner could have traded, so that is the one kept.

    This does not touch dates the exchange snapshot does not cover, so history
    older than whole-market storage stays as it is. The residual is reported
    by compare_to_store on the next scan.
    """
    if df is None or df.empty:
        return 0
    before = compare_to_store(price_db, df, log=lambda *a: None)
    n = store_snapshot(price_db, df, log=log)
    after = compare_to_store(price_db, df, log=lambda *a: None)
    fixed = int(before.get("off_count") or 0) - int(after.get("off_count") or 0)
    if fixed > 0:
        log("  [snapshot] restored the exchange's own close on {} bar(s) a "
            "later fetch had replaced with an adjusted price".format(fixed))
    refill_rolling_columns(price_db, log=log)
    return n


# ---------------------------------------------------------------- backfill
# Storing the whole market gives every instrument TODAY's bar, and nothing
# else. A holding card needs averages, so a stock with one bar is a stock the
# owner still gets no advice on -- and waiting for three months of snapshots to
# accumulate is not an answer.
#
# So each scan also fetches history for a slice of the names that lack it,
# liquid ones first, because those are the ones somebody might actually hold.
# At this size the whole market is covered within a few days without ever
# asking the feed for thousands of stocks at once.
BACKFILL_PER_SCAN = 150
BACKFILL_MIN_BARS = 60


# Some instruments simply cannot be filled -- bond ETFs and A-suffix codes the
# batch feed does not carry. Without a memory of that, every scan would spend
# its whole backfill budget retrying the same names forever, and the ones that
# CAN be filled would never come up. Three failures buys a month off.
BACKFILL_MAX_TRIES = 3
BACKFILL_RETRY_DAYS = 30


def _ensure_attempts(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS backfill_attempts ("
                 "stock_id TEXT PRIMARY KEY, tries INTEGER, last TEXT)")


def _give_up_ids(conn):
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=BACKFILL_RETRY_DAYS)).isoformat()
    _ensure_attempts(conn)
    return {str(r[0]) for r in conn.execute(
        "SELECT stock_id FROM backfill_attempts WHERE tries >= ? AND last >= ?",
        (BACKFILL_MAX_TRIES, cutoff))}


def _record_attempts(price_db, asked, filled):
    from datetime import date
    today = date.today().isoformat()
    conn = sqlite3.connect(str(price_db), timeout=60)
    try:
        _ensure_attempts(conn)
        for sid in asked:
            if sid in filled:
                conn.execute("DELETE FROM backfill_attempts WHERE stock_id = ?",
                             (sid,))
            else:
                conn.execute(
                    "INSERT INTO backfill_attempts (stock_id, tries, last) "
                    "VALUES (?, 1, ?) ON CONFLICT(stock_id) DO UPDATE SET "
                    "tries = tries + 1, last = excluded.last", (sid, today))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def stocks_needing_history(price_db, min_bars=BACKFILL_MIN_BARS,
                           limit=BACKFILL_PER_SCAN):
    """{stock_id: market} for the most liquid names whose recent window is
    incomplete, skipping the ones repeated attempts have shown cannot be
    filled.

    "Incomplete" means MISSING SESSIONS, not merely few rows. The first
    version asked `COUNT(*) < 60` over all time, which cannot see a hole: on
    2026-09-21 stock 2330 held 63 bars covering 69 sessions -- a 6-session
    gap straight through the middle -- so it was never queued, and every
    average that would cross that gap stayed null forever. The window is the
    last `min_bars` sessions of the market's own calendar, and a stock is
    queued when it has a row on fewer than all of them.
    """
    conn = sqlite3.connect(str(price_db), timeout=60)
    try:
        skip = _give_up_ids(conn)
        sessions = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT date FROM data ORDER BY date DESC LIMIT ?",
            (int(min_bars),)).fetchall()]
        if not sessions:
            return {}
        first = sessions[-1]
        want_n = len(sessions)
        # Two ways to be under-covered, and both matter: too few bars overall
        # (a newly listed or newly stored instrument), or bars that skip
        # sessions inside the window (a name that was a candidate for a while
        # and then was not). The second is the one the row count cannot see.
        #
        # Rank by the busiest of the stock's OWN recent bars, not by the very
        # latest session: a name missing from today's feed is exactly the one
        # that needs filling, and keying the sort on today's turnover sent
        # every such name to the back of the queue (2330 among them).
        rows = conn.execute(
            "SELECT stock_id, COUNT(DISTINCT date) AS total, "
            "  COUNT(DISTINCT CASE WHEN date >= ? THEN date END) AS inwin, "
            "  MAX(close * Volume_Lot) AS turn "
            "FROM data GROUP BY stock_id "
            "HAVING total < ? OR inwin < ? "
            "ORDER BY turn IS NULL, turn DESC LIMIT ?",
            (first, int(min_bars), want_n, int(limit) * 4)).fetchall()
    except sqlite3.OperationalError as e:
        # Any SQLite too old for this shape falls back to the plain count.
        rows = conn.execute(
            "SELECT stock_id, COUNT(*) AS n FROM data GROUP BY stock_id "
            "HAVING n < ? LIMIT ?", (int(min_bars), int(limit) * 4)).fetchall()
        rows = [(r[0], r[1], None) for r in rows]
    finally:
        conn.close()
    out = {}
    for r in rows:
        sid = str(r[0])
        if sid in skip:
            continue
        # 00-prefixed instruments list on the TSE side of the fetchers.
        out[sid] = "TSE" if sid.startswith("00") else None
        if len(out) >= limit:
            break
    return out


def backfill_history(price_db, limit=BACKFILL_PER_SCAN, log=print):
    """Fetch history for a slice of the under-covered names. Returns how many
    were filled. Never raises: this is a completeness improvement, not a
    precondition for the scan."""
    try:
        want = stocks_needing_history(price_db, limit=limit)
        if not want:
            return 0
        from ingestion.price_volume_multi import (multi_fetch_and_save_batch,
                                                  resolve_market)
        id_to_market = {sid: (mkt or resolve_market(sid) or "TSE")
                        for sid, mkt in want.items()}
        got = multi_fetch_and_save_batch(list(want), id_to_market)
        _record_attempts(price_db, list(want), set(got))
        log("  [backfill] filled history for {} of {} under-covered name(s)"
            .format(len(got), len(want)))
        return len(got)
    except Exception as e:
        log("  [backfill] skipped: {}".format(str(e)[:100]))
        return 0
