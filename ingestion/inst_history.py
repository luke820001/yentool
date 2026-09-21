"""
Per-date institutional net buy/sell for BOTH boards, back through history.
ASCII only.

Why this exists (F16, TASKS.md P1): `ingestion.inst_trades` can only ask
TPEX for "the latest day" (openapi), so a missed run left a permanent hole in
the OTC series, and the whole-market table only reached back to 2026-02-23.
Any question of the form "does today's institutional flow say anything about
tomorrow" was therefore unanswerable on more than seven months of one regime.

TPEX's revamped site serves the same table for ANY date:
    /www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=YYYY/MM/DD&response=json
verified 2026-09-21 for 2018-04-02, 2020-03-02, 2023-09-18 and 2026-09-18 with
an identical 24-column layout (code, name, then buy/sell/net for seven
institution groups, then the three-institution total). It starts around
2018-03; earlier dates return an empty table. TWSE T86 already took a date
(and reaches back to 2012). So both boards can now be fetched day by day, for
the research window and for the live table's missing days alike.

Values are stored in LOTS (shares / 1000), matching inst_trades.db.
Foreign_Net follows the existing convention: foreign and mainland investors
EXCLUDING foreign dealers (T86 column "wai lu zi ... (bu han wai zi zi ying
shang)"; TPEX column 4). Dealer_Net is the dealer total (proprietary +
hedging). Chinese field keywords are written as hex code points and decoded
by _K() so this file stays ASCII.
"""
import sqlite3
import time
from datetime import datetime

import pandas as pd
import requests

_H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
T86_URL = ("https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALL"
           "&response=json")
TPEX_DAILY_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"

COLUMNS = ["stock_id", "Foreign_Net", "Trust_Net", "Dealer_Net", "Inst_Net", "date"]

# TPEX dailyTrade column positions (0-based), stable 2018..2026.
_TPEX_FOREIGN_EX_DEALER_NET = 4
_TPEX_TRUST_NET = 13
_TPEX_DEALER_TOTAL_NET = 22
_TPEX_TOTAL_NET = 23


def _K(hex_points):
    """Decode space-separated hex code points into the keyword (keeps this
    source file ASCII)."""
    return "".join(chr(int(h, 16)) for h in hex_points.split())


# T86 field keywords. The names changed on 2017-12-18 ("wai zi" became
# "wai lu zi (bu han wai zi zi ying shang)"), so the foreign column is looked
# up with the newer name first and the older as a fallback.
_KW_FOREIGN_NEW = _K("5916 9678 8cc7")              # wai lu zi
_KW_FOREIGN_OLD = _K("5916 8cc7")                   # wai zi
_KW_NET = _K("8cb7 8ce3 8d85")                      # mai mai chao
_KW_TRUST = _K("6295 4fe1")                         # tou xin
_KW_DEALER_NET = _K("81ea 71df 5546 8cb7 8ce3 8d85")          # zi ying shang mai mai chao
_KW_TOTAL_NET = _K("4e09 5927 6cd5 4eba 8cb7 8ce3 8d85")      # san da fa ren mai mai chao


def _is_stock(sid):
    sid = str(sid)
    return sid.isdigit() and len(sid) in (4, 5)


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return 0.0


def _get_json(url, params=None, timeout=30, tries=3, backoff=8.0):
    """GET and parse JSON with retries. Returns None when every try failed
    (throttled, HTML error page, network) so the caller can record a gap
    instead of silently storing nothing."""
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=_H, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:      # not JSON, timeout, connection error, ...
            pass
        time.sleep(backoff * (attempt + 1))
    return None


def parse_twse_t86(j, iso):
    """Parse a T86 JSON document for `iso` -> DataFrame[COLUMNS] in lots.
    Empty frame when the exchange reports no data (a non-trading day)."""
    if not isinstance(j, dict) or j.get("stat") != "OK" or not j.get("data"):
        return pd.DataFrame(columns=COLUMNS)
    fields = [str(f) for f in (j.get("fields") or [])]

    def idx(*kw):
        for i, f in enumerate(fields):
            if all(k in f for k in kw):
                return i
        return None

    i_for = idx(_KW_FOREIGN_NEW, _KW_NET)
    if i_for is None:
        i_for = idx(_KW_FOREIGN_OLD, _KW_NET)
    i_tru = idx(_KW_TRUST, _KW_NET)
    # The dealer TOTAL is the field that STARTS with the dealer keyword; a
    # substring match would land on the FOREIGN-dealer column (index 7 since
    # 2017-12, "wai zi zi ying shang ...") first and read 0 for nearly every
    # stock.
    i_dlr = next((i for i, f in enumerate(fields) if f.startswith(_KW_DEALER_NET)), None)
    i_tot = idx(_KW_TOTAL_NET)

    def gv(rec, i):
        if i is None or i >= len(rec):
            return 0.0
        return _num(rec[i])

    rows = []
    for rec in j["data"]:
        sid = str(rec[0]).strip()
        if not _is_stock(sid):
            continue
        rows.append({
            "stock_id": sid,
            "Foreign_Net": round(gv(rec, i_for) / 1000, 1),
            "Trust_Net": round(gv(rec, i_tru) / 1000, 1),
            "Dealer_Net": round(gv(rec, i_dlr) / 1000, 1),
            "Inst_Net": round(gv(rec, i_tot) / 1000, 1),
            "date": iso,
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def parse_tpex_daily(j, iso):
    """Parse a TPEX dailyTrade JSON document -> DataFrame[COLUMNS] in lots."""
    if not isinstance(j, dict):
        return pd.DataFrame(columns=COLUMNS)
    tables = j.get("tables") or []
    if str(j.get("stat", "")).lower() != "ok" or not tables:
        return pd.DataFrame(columns=COLUMNS)
    data = tables[0].get("data") or []
    rows = []
    for rec in data:
        if len(rec) < _TPEX_TOTAL_NET + 1:
            continue
        sid = str(rec[0]).strip()
        if not _is_stock(sid):
            continue
        rows.append({
            "stock_id": sid,
            "Foreign_Net": round(_num(rec[_TPEX_FOREIGN_EX_DEALER_NET]) / 1000, 1),
            "Trust_Net": round(_num(rec[_TPEX_TRUST_NET]) / 1000, 1),
            "Dealer_Net": round(_num(rec[_TPEX_DEALER_TOTAL_NET]) / 1000, 1),
            "Inst_Net": round(_num(rec[_TPEX_TOTAL_NET]) / 1000, 1),
            "date": iso,
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def fetch_twse_inst_date(iso):
    """TWSE T86 for one ISO date. Empty frame on a non-trading day; None
    when the request failed."""
    j = _get_json(T86_URL.format(iso.replace("-", "")))
    return None if j is None else parse_twse_t86(j, iso)


def fetch_tpex_inst_date(iso):
    """TPEX three-institution table for one ISO date. Empty frame on a
    non-trading day (or before the site's history starts); None when the
    request failed."""
    d = datetime.strptime(iso, "%Y-%m-%d")
    j = _get_json(TPEX_DAILY_URL, params={
        "type": "Daily", "sect": "EW", "date": d.strftime("%Y/%m/%d"),
        "response": "json"})
    return None if j is None else parse_tpex_daily(j, iso)


FETCHERS = {"TSE": fetch_twse_inst_date, "OTC": fetch_tpex_inst_date}


# ------------------------------------------------------------------ store
def _open(db_path):
    # Several shards write one board file side by side; wait for the lock
    # instead of dying on it (each write is one short transaction).
    conn = sqlite3.connect(str(db_path), timeout=120)
    # WAL lets readers and one writer proceed without the whole-file locks
    # of the rollback journal, which serialised the shards to a crawl.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE TABLE IF NOT EXISTS data (stock_id TEXT, Foreign_Net REAL,"
                 " Trust_Net REAL, Dealer_Net REAL, Inst_Net REAL, date TEXT,"
                 " board TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist ON data(stock_id, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_date ON data(date)")
    # One row per (board, date) fetched; rows = -1 when the request failed so
    # it can be retried, 0 for a genuine non-trading day.
    conn.execute("CREATE TABLE IF NOT EXISTS fetched (board TEXT, date TEXT,"
                 " rows INTEGER, PRIMARY KEY (board, date))")
    return conn


def fetched_dates(db_path, board, include_failed=False):
    conn = _open(db_path)
    try:
        q = "SELECT date FROM fetched WHERE board = ?"
        if not include_failed:
            q += " AND rows >= 0"
        return {r[0] for r in conn.execute(q, (board,)).fetchall()}
    finally:
        conn.close()


def store_day(db_path, board, iso, df, conn=None):
    """Write one board-day. df None = failed (recorded as rows=-1).

    Pass an open `conn` for a long run: closing the connection after every
    day forces a WAL checkpoint (a multi-second fsync of the whole file)
    each time, which is where a per-day open/close spent 90% of its time."""
    own = conn is None
    conn = conn or _open(db_path)
    try:
        if df is None:
            conn.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?)",
                         (board, iso, -1))
            conn.commit()
            return -1
        conn.execute("DELETE FROM data WHERE board = ? AND date = ?", (board, iso))
        if len(df):
            recs = [(str(r.stock_id), float(r.Foreign_Net), float(r.Trust_Net),
                     float(r.Dealer_Net), float(r.Inst_Net), iso, board)
                    for r in df.itertuples(index=False)]
            conn.executemany("INSERT INTO data VALUES (?,?,?,?,?,?,?)", recs)
        conn.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?)",
                     (board, iso, len(df)))
        conn.commit()
        return len(df)
    finally:
        if own:
            conn.close()


def backfill(db_path, board, dates, sleep=0.8, retry_failed=False, log=print):
    """Fetch every date in `dates` not yet stored for `board`, oldest first.
    Resumable: a run that dies can simply be started again. Returns the number
    of days fetched in this call."""
    fetch = FETCHERS[board]
    have = fetched_dates(db_path, board, include_failed=not retry_failed)
    todo = sorted(d for d in dates if d not in have)
    log("[{}] {} day(s) to fetch ({} already stored)".format(
        board, len(todo), len(have)))
    done = 0
    t0 = time.time()
    conn = _open(db_path)
    # Research store: durability against a power cut is not worth an fsync
    # per day; a lost day is re-fetched by the next (resumable) run.
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        for i, iso in enumerate(todo, 1):
            df = fetch(iso)
            n = store_day(db_path, board, iso, df, conn=conn)
            done += 1
            if i % 25 == 0 or i == len(todo):
                log("[{}] {}/{} {} rows={} ({:.0f}s)".format(
                    board, i, len(todo), iso, n, time.time() - t0))
            time.sleep(sleep)
    finally:
        conn.close()
    return done


def load_history(db_path, boards=("TSE", "OTC"), start=None, end=None):
    """Long DataFrame[COLUMNS + board] for the requested boards/dates."""
    conn = _open(db_path)
    try:
        q = ("SELECT stock_id, Foreign_Net, Trust_Net, Dealer_Net, Inst_Net, date, board "
             "FROM data WHERE board IN (%s)" % ",".join("?" * len(boards)))
        args = list(boards)
        if start:
            q += " AND date >= ?"
            args.append(start)
        if end:
            q += " AND date <= ?"
            args.append(end)
        return pd.read_sql_query(q, conn, params=args)
    finally:
        conn.close()
