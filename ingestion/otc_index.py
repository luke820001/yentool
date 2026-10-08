"""
TPEX (OTC) capitalisation-weighted index -> TAIEX_FILE table 'TPEX'.
ASCII only. Display only: nothing in scoring, gates or exits reads it.

Why (plan P1-5 phase B, 2026-10-08). The live record (scanner/live_record)
showed the rule's own signals with no market to compare against. Every
signal it books is an OTC name, so TAIEX alone flatters or punishes the
record depending on which board led: over 2026-06-25 -> 10-07 TAIEX rose
while the OTC index fell (439.84 -> 430.46, -2.13%). Both are published
(live_record out['bench']) so neither can be cherry-picked.

Sources (verified 2026-10-08):
  * history, one request per month, ROC year/month:
        GET https://www.tpex.org.tw/web/stock/iNdex_info/inxh/Inx_result.php
            ?l=zh-tw&d=115/06&o=json            (header User-Agent required)
    -> {"stat": "ok", "tables": [{"date": "115/06", "data": [
           ["2026/06/01", open, high, low, close, change], ...]}]}
    Gregorian YYYY/MM/DD dates; a future month answers with empty data;
    the current month answers month-to-date.
  * daily top-up, month-to-date only:
        GET https://www.tpex.org.tw/openapi/v1/tpex_index
    -> [{"Date": "20261001", "Open", "High", "Low", "Close", "Change"}, ...]

The table holds the same columns as the TAIEX table (date, close) so every
reader of taiex.db keeps working: they all read the 'TAIEX' table by name
(ingestion.market_index, scanner.market_regime, scanner.market_leg,
scanner.live_record), and storage.data_store.upsert_and_trim trims this one
to the same ROLLING_DAYS window.

refresh() never raises; it returns a health dict. It runs inside the cloud
scan job (scan_headless.run_scan, right after the whole-market snapshot); no
PC-side scheduler.
"""
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from config.settings import ROLLING_DAYS, TAIEX_FILE

SHEET = "TPEX"
LEGACY_URL = "https://www.tpex.org.tw/web/stock/iNdex_info/inxh/Inx_result.php"
OPENAPI_URL = "https://www.tpex.org.tw/openapi/v1/tpex_index"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
TIMEOUT = 15
TRIES = 2
BACKOFF_S = 3.0
BUDGET_S = 45
FRAME_COLS = ["date", "open", "high", "low", "close"]


def _num(x):
    """'1,234.56' -> 1234.56; blank / garbage -> None."""
    try:
        v = float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _iso(s):
    """'2026/06/01' | '2026-06-01' | '20260601' -> '2026-06-01', else None."""
    s = str(s or "").strip()
    digits = s.replace("/", "").replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _frame(rows):
    df = pd.DataFrame(rows, columns=FRAME_COLS)
    if df.empty:
        return df
    df = df.dropna(subset=["date", "close"])
    df = df.drop_duplicates(subset=["date"], keep="last")
    return df.sort_values("date").reset_index(drop=True)


def parse_legacy(payload):
    """Inx_result.php JSON -> DataFrame(date, open, high, low, close).
    Empty frame for anything unexpected (stat not ok, no table, no data)."""
    rows = []
    if not isinstance(payload, dict):
        return _frame(rows)
    stat = str(payload.get("stat") or "").lower()
    if stat and stat != "ok":
        return _frame(rows)
    tables = payload.get("tables") or []
    if not tables or not isinstance(tables[0], dict):
        return _frame(rows)
    for r in tables[0].get("data") or []:
        if not isinstance(r, (list, tuple)) or len(r) < 5:
            continue
        d = _iso(r[0])
        if d is None:
            continue
        rows.append([d, _num(r[1]), _num(r[2]), _num(r[3]), _num(r[4])])
    return _frame(rows)


def parse_openapi(payload):
    """openapi tpex_index JSON -> DataFrame(date, open, high, low, close)."""
    rows = []
    for r in payload if isinstance(payload, list) else []:
        if not isinstance(r, dict):
            continue
        d = _iso(r.get("Date"))
        if d is None:
            continue
        rows.append([d, _num(r.get("Open")), _num(r.get("High")),
                     _num(r.get("Low")), _num(r.get("Close"))])
    return _frame(rows)


def _roc_month(year, month):
    return "{}/{:02d}".format(int(year) - 1911, int(month))


def _get(url, params=None):
    """GET JSON with TRIES attempts (TLS verified). Returns the decoded JSON
    or raises the last error."""
    import requests
    last = None
    for attempt in range(TRIES):
        try:
            r = requests.get(url, params=params, headers=HEADERS,
                             timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError("HTTP {}".format(r.status_code))
        except Exception as e:      # timeout, connection, not JSON
            last = e
        if attempt + 1 < TRIES:
            time.sleep(BACKOFF_S)
    raise last if last is not None else RuntimeError("no response")


def _months(start, end):
    """[(year, month)] from start's month through end's month."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _last_session_of_month(year, month):
    """The month's last trading day per the committed TWSE holiday file
    (weekday fallback when the year is unknown)."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    try:
        from scanner.market_calendar import is_session
    except Exception:
        is_session = None
    for _ in range(31):
        if d.weekday() < 5:
            ok = None
            if is_session is not None:
                try:
                    ok = is_session(d, fetch=False)[0]
                except Exception:
                    ok = None
            if ok is None or ok:
                return d.isoformat()
        d -= timedelta(days=1)
    return d.isoformat()


def load(taiex_file=None):
    """The stored series: DataFrame(date, close) sorted, possibly empty."""
    from storage.data_store import load_sheet
    # load_sheet needs a Path (it calls .exists()); accept a str too
    t = load_sheet(Path(taiex_file or TAIEX_FILE), SHEET)
    if t.empty or "close" not in t.columns or "date" not in t.columns:
        return pd.DataFrame(columns=["date", "close"])
    t = t[["date", "close"]].copy()
    t["date"] = t["date"].astype(str).str.slice(0, 10)
    t["close"] = pd.to_numeric(t["close"], errors="coerce")
    return t.dropna().sort_values("date").reset_index(drop=True)


def _as_date(today):
    if today is None:
        return date.today()
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    return datetime.strptime(str(today)[:10], "%Y-%m-%d").date()


def refresh(taiex_file=None, today=None, get=None, log=print,
            budget_s=BUDGET_S):
    """Bring table 'TPEX' up to date. Never raises.

    Plan: every month of the rolling window with NO stored row is fetched
    from the legacy endpoint (the first cloud run backfills ~14 requests,
    later runs none); the previous month again while its last trading day is
    missing (a gap across a month end); the current month from openapi, plus
    the legacy month-to-date when openapi has not reached today yet. The
    first legacy month that fails ends the legacy requests of the run (the
    rest are asked next run), so a dead or retired legacy URL costs one
    request's retries, not one per missing month.

    `get(url, params)` returns decoded JSON or raises (tests pass a fake).
    Returns {ok, added, rows, last_date, sources, error}.
    """
    health = {"ok": False, "added": 0, "rows": 0, "last_date": None,
              "sources": [], "error": None}
    try:
        from storage.data_store import upsert_and_trim
        taiex_file = Path(taiex_file or TAIEX_FILE)
        get = get or _get
        today = _as_date(today)
        t0 = time.time()
        have = load(taiex_file)
        dates = set(have["date"])
        # whole months inside the rolling window only: a month cut by the
        # trim cutoff would read as missing and be refetched every run
        months = _months(today - timedelta(days=max(ROLLING_DAYS - 31, 31)),
                         today)
        frames, errors = [], []

        def month_has(y, m):
            p = "{:04d}-{:02d}-".format(y, m)
            return any(d.startswith(p) for d in dates)

        todo = [(y, m) for (y, m) in months[:-1] if not month_has(y, m)]
        if len(months) >= 2:
            py, pm = months[-2]
            if (py, pm) not in todo and \
                    _last_session_of_month(py, pm) not in dates:
                todo.append((py, pm))

        def legacy(y, m, tag):
            f = parse_legacy(get(LEGACY_URL, {"l": "zh-tw",
                                              "d": _roc_month(y, m),
                                              "o": "json"}))
            frames.append(f)
            health["sources"].append("legacy:{}:{}".format(tag, len(f)))
            return f

        # The first failing month ends the backfill for this run: a host that
        # is down, or a retired legacy URL (the brief saw the new path answer
        # 302/405), would otherwise cost every missing month its full retries
        # -- ~45 s in front of the scan on EVERY run, since nothing gets
        # stored. The rest is asked again next run (verifier, 2026-10-08).
        legacy_down = False
        for (y, m) in todo:
            if time.time() - t0 > budget_s:
                errors.append("budget spent before {}-{:02d}".format(y, m))
                break
            try:
                legacy(y, m, "{}-{:02d}".format(y, m))
            except Exception as e:
                errors.append("legacy {}-{:02d}: {}".format(y, m, str(e)[:80]))
                left = len(todo) - todo.index((y, m)) - 1
                if left:
                    errors.append("{} more month(s) left for the next "
                                  "run".format(left))
                legacy_down = True
                break

        cur = pd.DataFrame(columns=FRAME_COLS)
        if time.time() - t0 <= budget_s:
            try:
                cur = parse_openapi(get(OPENAPI_URL, None))
                frames.append(cur)
                health["sources"].append("openapi:{}".format(len(cur)))
            except Exception as e:
                errors.append("openapi: {}".format(str(e)[:80]))
        newest = max(list(dates) + list(cur["date"]) + [""])
        if newest < today.isoformat() and today.weekday() < 5 \
                and not legacy_down and time.time() - t0 <= budget_s:
            try:
                legacy(today.year, today.month, "current")
            except Exception as e:
                # openapi already answered for the month: only an error
                # when it did not
                if cur.empty:
                    errors.append("legacy current: {}".format(str(e)[:80]))

        live = [f for f in frames if f is not None and not f.empty]
        if live:
            new = pd.concat(live, ignore_index=True)
            new = new[new["date"] <= today.isoformat()]
            new = new.drop_duplicates(subset=["date"], keep="last")
            health["added"] = int((~new["date"].isin(dates)).sum())
            if not new.empty:
                upsert_and_trim(file_path=taiex_file, sheet_name=SHEET,
                                new_df=new[["date", "close"]].copy(),
                                date_col="date", key_cols=["date"])
        after = load(taiex_file)
        health["rows"] = int(len(after))
        health["last_date"] = (str(after["date"].iloc[-1])
                               if not after.empty else None)
        health["ok"] = bool(len(after)) and not errors
        health["error"] = "; ".join(errors) or None
        log("  [tpex-index] {} row(s), +{} new, last {}{}".format(
            health["rows"], health["added"], health["last_date"],
            " ({})".format(health["error"]) if health["error"] else ""))
    except Exception as e:
        health["error"] = "{}: {}".format(type(e).__name__, str(e)[:120])
        try:
            log("  [tpex-index] skipped: {}".format(health["error"]))
        except Exception:
            pass
    return health
