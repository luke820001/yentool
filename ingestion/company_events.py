"""
Company events for the phone card: latest monthly revenue, the next
ex-rights / ex-dividend date, the next investor conference. ASCII only;
Chinese keys are \\uXXXX escapes.

DISPLAY ONLY. Nothing here may feed scoring, gates, exits or Buy_Ready:
scan_mode, chip_verifier, mark_buy_ready and the exit stack never read the
columns this module writes (tests/test_company_events.py asserts identical
scores and Buy_Ready with and without them, and greps the scoring modules).
Research round 4 (docs/BACKTEST_LOG.md M.6) rejected revenue YoY as a filter;
the decision (DECISIONS.md addendum 2026-10-08, item 3) is to show the
latest month actually published at scan time WITH its month label, with no
good/bad colouring, and never to use FinMind create_time as a release date.

Sources (all verified 2026-10-08; plan P1-6, brief data.md):
  revenue     TWSE openapi t187ap05_L, TPEX openapi mopsfin_t187ap05_O.
              Same keys on both. The openapi publishes around the 17th of the
              following month, so from the 1st to ~17th it holds month M-2.
  revenue+    OPTIONAL fresher source, the MOPS static monthly pages
              mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{rocY}_{m}_0.html
              (Big5 HTML; companies report M-1 by the 10th). Fetched only
              while the openapi has not reached M-1.
  ex-dates    TWSE openapi TWT48U_ALL, TPEX openapi tpex_exright_prepost
              (a window of a few past days to ~4 weeks ahead).
  conference  TWSE t187ap04_L / TPEX mopsfin_t187ap04_O, the daily material
              announcements; clause 12 marks an investor conference and the
              fact date is its date. Each file holds ONE announcement day,
              so the conferences are ACCUMULATED in the cache.
  conference+ OPTIONAL monthly list, POST mopsov ajax_t100sb02_1 (date
              ranges "YYY/MM/DD <to> YYY/MM/DD").
  The mopsov host refused TLS from the development PC on 2026-10-08, so both
  mopsov sources are optional: their failure is reported at info level and
  the openapi data alone is complete enough to publish.

Cache: data/company_events.json (COMPANY_EVENTS_FILE), committed by
.github/workflows/scan.yml so the accumulated conferences survive between
cloud runs. refresh() fetches each daily source at most once per calendar
day (news every run), keeps the last good block when a fetch fails or comes
back short, records per-source health, prunes past events, and writes the
file atomically with one stock per line so the daily git diff stays small.
refresh() never raises.
"""
import html as _html
import json
import os
import re
import time
from datetime import date, datetime, timedelta

import pandas as pd

from config.settings import COMPANY_EVENTS_FILE

CACHE_VERSION = 1

# ---------------------------------------------------------------- endpoints
REVENUE_URLS = {
    "TSE": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "OTC": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
}
EXDIV_URLS = {
    "TSE": "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL",
    "OTC": "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost",
}
NEWS_URLS = {
    "TSE": "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
    "OTC": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O",
}
CONF_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1"
MOPS_REV_URL = "https://mopsov.twse.com.tw/nas/t21/{board}/t21sc03_{rocy}_{m}_0.html"
MOPS_BOARD = {"TSE": "sii", "OTC": "otc"}

# A revenue answer shorter than this is a partial publish, not the market.
REV_MIN_ROWS = {"TSE": 800, "OTC": 600}
MOPS_REV_MIN_ROWS = 50          # early in the month only a few have reported
TIMEOUT = 15
TRIES = 2
BACKOFF_S = 3.0
BUDGET_S = 60
MAX_AGE_DAYS = {"revenue": 45, "exdiv": 3, "news": 7, "conf": 7}
PRUNE_DAYS = 7                  # past events kept this long, then dropped
HEADERS = {"User-Agent": "Mozilla/5.0",
           "Accept": "application/json,text/html,*/*"}

# source name -> (kind, optional)
SOURCES = {
    "revenue_tse": ("revenue", False),
    "revenue_otc": ("revenue", False),
    "revenue_mops_tse": ("revenue", True),
    "revenue_mops_otc": ("revenue", True),
    "exdiv_tse": ("exdiv", False),
    "exdiv_otc": ("exdiv", False),
    "news_tse": ("news", False),
    "news_otc": ("news", False),
    "conf_mops_tse": ("conf", True),
    "conf_mops_otc": ("conf", True),
}

# The nine display-only columns annotate_events always adds.
EVENT_COLUMNS = ("Rev_Month", "Rev_Amount_K", "Rev_YoY_Pct", "Rev_MoM_Pct",
                 "Rev_Cum_YoY_Pct", "Ex_Date", "Ex_Kind", "Ex_Cash_Div",
                 "Conf_Date")
EX_KINDS = ("div", "right", "both")

# Statutory filing deadlines (month, day, what). Approximate: financial and
# insurance companies file Q2 by 08-31, and a holiday moves a deadline.
REPORT_DEADLINES = ((3, 31, "annual"), (5, 15, "Q1"), (8, 14, "Q2"),
                    (11, 14, "Q3"))
REVENUE_DEADLINE_DAY = 10

# ------------------------------------------------------------ Chinese keys
K_ID = "\u516c\u53f8\u4ee3\u865f"                       # company code
K_YM = "\u8cc7\u6599\u5e74\u6708"                       # data year-month
K_ASOF = "\u51fa\u8868\u65e5\u671f"                     # report date
K_AMT = "\u71df\u696d\u6536\u5165-\u7576\u6708\u71df\u6536"
K_MOM = ("\u71df\u696d\u6536\u5165-\u4e0a\u6708\u6bd4\u8f03"
         "\u589e\u6e1b(%)")
K_YOY = ("\u71df\u696d\u6536\u5165-\u53bb\u5e74\u540c\u6708"
         "\u589e\u6e1b(%)")
K_CUM = ("\u7d2f\u8a08\u71df\u696d\u6536\u5165-\u524d\u671f"
         "\u6bd4\u8f03\u589e\u6e1b(%)")
K_NOTE = "\u5099\u8a3b"                                 # remarks
K_CLAUSE = "\u7b26\u5408\u689d\u6b3e"                   # matching clause
K_FACT = "\u4e8b\u5be6\u767c\u751f\u65e5"               # fact date
CONF_CLAUSE = "\u7b2c12\u6b3e"                          # clause 12
CH_DIV = "\u606f"                                       # dividend
CH_RIGHT = "\u6b0a"                                     # rights

_ROC7 = re.compile(r"^(\d{3})(\d{2})(\d{2})$")
_ROC_SLASH = re.compile(r"(?<!\d)(\d{2,3})/(\d{1,2})/(\d{1,2})(?!\d)")
_TR = re.compile(r"(?is)<tr\b[^>]*>(.*?)(?=<tr\b|</table>|$)")
_CELL = re.compile(r"(?is)<t([dh])\b[^>]*>(.*?)</t[dh]\s*>")
_TAG = re.compile(r"(?s)<[^>]+>")
_SID = re.compile(r"^[0-9]{4,6}[A-Z]?$")


# ---------------------------------------------------------------- parsers
def _num(x):
    """'1,234.5' -> 1234.5; '', NaN, garbage -> None."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _r1(x):
    v = _num(x)
    return None if v is None else round(v, 1)


def _valid_iso(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except (TypeError, ValueError):
        return None


def _roc_iso(s):
    """'1151007' or '115/10/07' -> '2026-10-07'; anything else -> None."""
    s = str(s or "").strip()
    m = _ROC7.match(s)
    if m:
        return _valid_iso(int(m.group(1)) + 1911, m.group(2), m.group(3))
    m = _ROC_SLASH.fullmatch(s)
    if m:
        return _valid_iso(int(m.group(1)) + 1911, m.group(2), m.group(3))
    return None


def _roc_ym(s):
    """'11508' -> '2026-08'; a month outside 1..12 -> None."""
    s = str(s or "").strip()
    if len(s) != 5 or not s.isdigit():
        return None
    mm = int(s[3:])
    if not 1 <= mm <= 12:
        return None
    return "{:04d}-{:02d}".format(int(s[:3]) + 1911, mm)


def _sid(x):
    s = str(x or "").strip()
    return s if _SID.match(s) else ""


def parse_revenue(raw):
    """openapi monthly revenue (either board) ->
    {sid: {month, as_of, amount_k, yoy, mom, cum_yoy, note}}.
    Percentages rounded to 1 dp; rows without an id or a valid month are
    dropped."""
    out = {}
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        sid = _sid(r.get(K_ID))
        ym = _roc_ym(r.get(K_YM))
        if not sid or not ym:
            continue
        note = _html.unescape(str(r.get(K_NOTE) or "")).strip()
        out[sid] = {"month": ym, "as_of": _roc_iso(r.get(K_ASOF)),
                    "amount_k": _num(r.get(K_AMT)), "yoy": _r1(r.get(K_YOY)),
                    "mom": _r1(r.get(K_MOM)), "cum_yoy": _r1(r.get(K_CUM)),
                    "note": note if note not in ("", "-") else None}
    return out


def _ex_kind(text):
    s = str(text or "")
    d, r = CH_DIV in s, CH_RIGHT in s
    return "both" if d and r else "right" if r else "div" if d else None


# The keys each ex-date answer must carry (date, id, kind). A feed that
# renames them parses to nothing, and an answer that parses to nothing is
# otherwise indistinguishable from a quiet window.
EXDIV_KEYS = {"TSE": ("Date", "Code", "Exdividend"),
              "OTC": ("ExRrightsExDividendDate", "SecuritiesCompanyCode",
                      "ExRrightsExDividend")}


def _has_keys(raw, keys):
    """True when at least one row of `raw` carries every key in `keys`."""
    return any(isinstance(r, dict) and all(k in r for k in keys) for r in raw)


def _upcoming_exdiv(block, today):
    """How many ex-dates in a cached {sid: [{date, ...}]} block are on or
    after `today`."""
    n = 0
    for v in (block or {}).values():
        for e in v if isinstance(v, list) else []:
            if isinstance(e, dict) and str(e.get("date") or "") >= today.isoformat():
                n += 1
    return n


def _parse_exdiv(raw, date_key, id_key, kind_key):
    out = {}
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        sid = _sid(r.get(id_key))
        d = _roc_iso(r.get(date_key))
        kind = _ex_kind(r.get(kind_key))
        if not sid or not d or kind is None:
            continue
        cash = _num(r.get("CashDividend"))
        out.setdefault(sid, []).append(
            {"date": d, "kind": kind,
             "cash": round(cash, 4) if cash else None})
    for v in out.values():
        v.sort(key=lambda e: e["date"])
    return out


def parse_exdiv_tse(raw):
    """TWT48U_ALL -> {sid: [{date, kind, cash}]} sorted by date. kind is
    'div' | 'right' | 'both'; unrecognised kinds are dropped."""
    return _parse_exdiv(raw, "Date", "Code", "Exdividend")


def parse_exdiv_otc(raw):
    """tpex_exright_prepost -> {sid: [{date, kind, cash}]} (the API spells
    its own keys ExRrights...)."""
    return _parse_exdiv(raw, "ExRrightsExDividendDate",
                        "SecuritiesCompanyCode", "ExRrightsExDividend")


def parse_conf_news(raw, id_key):
    """Daily material announcements -> {sid: [conference dates]} for the
    clause-12 (investor conference) rows; the fact date is the date."""
    out = {}
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        clause = re.sub(r"\s+", "", str(r.get(K_CLAUSE) or ""))
        if clause != CONF_CLAUSE:
            continue
        sid = _sid(r.get(id_key))
        d = _roc_iso(r.get(K_FACT))
        if sid and d:
            out.setdefault(sid, set()).add(d)
    return {k: sorted(v) for k, v in out.items()}


def _cells(row_html):
    return [(kind.lower(), _TAG.sub("", body).replace("&nbsp;", " ").strip())
            for kind, body in _CELL.findall(row_html)]


def parse_mops_conf_html(text):
    """mopsov ajax_t100sb02_1 HTML -> {sid: [(start, end)]}. The date cell
    holds one ROC date or a range joined by the 'to' character; the ROC
    dates are found by pattern, so the page's encoding does not matter."""
    out = {}
    for row in _TR.findall(str(text or "")):
        cells = _cells(row)
        if len(cells) < 3 or cells[0][0] != "d":
            continue
        sid = _sid(cells[0][1])
        if not sid:
            continue
        found = [_roc_iso("{}/{}/{}".format(*m))
                 for m in _ROC_SLASH.findall(cells[2][1])]
        found = [d for d in found if d]
        if not found:
            continue
        span = (min(found), max(found))
        if span not in out.setdefault(sid, []):
            out[sid].append(span)
    for v in out.values():
        v.sort()
    return out


def parse_mops_revenue_html(text, month):
    """MOPS t21sc03 HTML (decoded) -> the parse_revenue shape for `month`
    ('YYYY-MM', the month the page was requested for). Cells: id, name,
    this month, last month, last year's month, MoM %, YoY %, cumulative,
    last year's cumulative, cumulative %, remarks. Total rows (th) are
    skipped. as_of is the page's report date when it can be read."""
    text = str(text or "")
    as_of = None
    i = text.find(K_ASOF)
    if i >= 0:
        m = _ROC_SLASH.search(text, i, i + 80)
        if m:
            as_of = _roc_iso("{}/{}/{}".format(*m.groups()))
    out = {}
    for row in _TR.findall(text):
        cells = _cells(row)
        if len(cells) < 10 or cells[0][0] != "d":
            continue
        sid = _sid(cells[0][1])
        if not sid:
            continue
        note = _html.unescape(cells[10][1]).strip() if len(cells) > 10 else ""
        out[sid] = {"month": month, "as_of": as_of,
                    "amount_k": _num(cells[2][1]), "yoy": _r1(cells[6][1]),
                    "mom": _r1(cells[5][1]), "cum_yoy": _r1(cells[9][1]),
                    "note": note if note not in ("", "-") else None}
    return out


# ------------------------------------------------------------------ fetch
class _Fetcher:
    """requests with TLS verification, TRIES attempts, a shared time budget
    and a dead-host memo: a host that refused TLS, or that failed every try
    of one request with a connection error or a timeout, is not asked again
    in the same run (mopsov from some networks). One dropped connection is
    NOT enough -- the same host serves several required sources (TPEX
    revenue, ex-dates and announcements), and a run that gave up on all of
    them after one reset would lose that day's announcement file for good
    (verifier, 2026-10-08)."""

    def __init__(self, budget_s=BUDGET_S):
        self.t0 = time.time()
        self.budget_s = budget_s
        self.dead = set()

    def spent(self):
        return time.time() - self.t0 > self.budget_s

    def __call__(self, url, method="GET", data=None, encoding=None):
        import requests
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        if host in self.dead:
            raise RuntimeError("host unreachable earlier this run")
        last = None
        unreachable = False
        for attempt in range(TRIES):
            if self.spent():
                raise RuntimeError("time budget spent")
            try:
                if method == "POST":
                    r = requests.post(url, data=data, headers=HEADERS,
                                      timeout=TIMEOUT)
                else:
                    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code == 200:
                    if encoding:
                        return r.content.decode(encoding, errors="replace")
                    return r.json()
                last = RuntimeError("HTTP {}".format(r.status_code))
                unreachable = False
            except requests.exceptions.SSLError as e:
                # a refused TLS handshake does not change on a retry
                last = e
                self.dead.add(host)
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last = e
                unreachable = True
            except Exception as e:      # not JSON
                last = e
                unreachable = False
            if attempt + 1 < TRIES:
                time.sleep(BACKOFF_S)
        if unreachable:
            self.dead.add(host)
        raise last if last is not None else RuntimeError("no response")


# ------------------------------------------------------------------ cache
def _empty_cache():
    return {"version": CACHE_VERSION, "updated_at": None, "sources": {},
            "revenue": {}, "exdiv": {}, "conf": {}}


def load_cache(cache_path=None):
    """The cached events dict, or an empty one (never raises)."""
    path = cache_path or COMPANY_EVENTS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return _empty_cache()
    if not isinstance(got, dict):
        return _empty_cache()
    base = _empty_cache()
    for k in ("sources", "revenue", "exdiv", "conf"):
        if isinstance(got.get(k), dict):
            base[k] = got[k]
    base["updated_at"] = got.get("updated_at")
    return base


def _dumps(obj, depth=0):
    """JSON with one stock per line: containers of containers are expanded
    down to depth 2, everything deeper is written compact."""
    pad = "  " * depth
    if isinstance(obj, dict) and depth < 3 and obj and any(
            isinstance(v, (dict, list)) for v in obj.values()):
        parts = []
        for k in sorted(obj):
            v = obj[k]
            if isinstance(v, dict) and depth < 2:
                body = _dumps(v, depth + 1)
            else:
                body = json.dumps(v, sort_keys=True, ensure_ascii=True,
                                  separators=(",", ":"))
            parts.append("{}  {}: {}".format(pad, json.dumps(str(k)), body))
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    return json.dumps(obj, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def save_cache(events, cache_path=None):
    """Atomic write (tmp + os.replace)."""
    path = str(cache_path or COMPANY_EVENTS_FILE)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(_dumps(events) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- refresh
def _as_date(today):
    if today is None:
        return date.today()
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    return datetime.strptime(str(today)[:10], "%Y-%m-%d").date()


def _prev_month(d):
    first = d.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _next_month_first(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _health(events, name):
    h = events["sources"].get(name)
    if not isinstance(h, dict):
        h = {}
    kind, optional = SOURCES[name]
    h.setdefault("ok", False)
    h.setdefault("rows", 0)
    h.setdefault("as_of", None)
    h.setdefault("fetched_at", None)
    h.setdefault("error", None)
    h["optional"] = optional
    events["sources"][name] = h
    return h


def _fresh_today(h, today):
    return bool(h.get("fetched_at")) and str(h["fetched_at"])[:10] == today.isoformat()


def _mark(h, ok, rows=None, as_of=None, error=None, now_s=None, skipped=None):
    h["ok"] = bool(ok)
    h["attempted_at"] = now_s
    if ok:
        h["fetched_at"] = now_s
        h["error"] = None
        if rows is not None:
            h["rows"] = int(rows)
        if as_of is not None:
            h["as_of"] = as_of
    else:
        h["error"] = str(error or "failed")[:160]
    if skipped:
        h["skipped"] = skipped
    else:
        h.pop("skipped", None)


def _merge_conf(conf, sid, start, end, src):
    lst = conf.setdefault(sid, [])
    for e in lst:
        if e[0] == start:
            if end > e[1]:
                e[1] = end
            if src == "mops":
                e[2] = "mops"
            return
    lst.append([start, end, src])
    lst.sort()


def refresh(cache_path=None, today=None, fetch=None, log=print, write=True,
            budget_s=BUDGET_S):
    """Update the events cache from the exchanges. Never raises.

    `fetch(url, method="GET", data=None, encoding=None)` returns decoded
    JSON, or text when `encoding` is given, and raises on failure (tests pass
    a fake). `write=False` updates in memory only (gui/scan_worker: the
    cache file is the cloud's, committed by scan.yml).
    Returns the events dict (load_cache shape)."""
    events = load_cache(cache_path)
    try:
        today = _as_date(today)
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
        if today != date.today():
            now_s = today.isoformat() + " 00:00"
        fetcher = fetch or _Fetcher(budget_s)
        spent = getattr(fetcher, "spent", lambda: False)

        def attempt(name, fn):
            h = _health(events, name)
            if spent():
                _mark(h, False, error="time budget spent", now_s=now_s)
                return
            try:
                fn(h)
            except Exception as e:
                _mark(h, False, error="{}: {}".format(type(e).__name__,
                                                      str(e)[:120]),
                      now_s=now_s)

        # revenue (openapi): at most once per day per board
        for board in ("TSE", "OTC"):
            name = "revenue_" + board.lower()
            h = _health(events, name)
            if _fresh_today(h, today) and h.get("ok") \
                    and events["revenue"].get(board):
                continue

            def rev(h, board=board):
                got = parse_revenue(fetcher(REVENUE_URLS[board]))
                if len(got) < REV_MIN_ROWS[board]:
                    raise RuntimeError("{} rows < floor {}".format(
                        len(got), REV_MIN_ROWS[board]))
                for v in got.values():
                    v.pop("note", None)
                events["revenue"][board] = got
                as_of = max((v["as_of"] or "" for v in got.values()), default="")
                _mark(h, True, rows=len(got), as_of=as_of or None, now_s=now_s)
            attempt(name, rev)

        # The REQUIRED sources (openapi revenue above, ex-dates and the
        # announcement files below) run first; the optional mopsov pages
        # only get what is left of the shared budget. mopsov hanging instead
        # of refusing (unverified from the cloud) must not starve the
        # announcement file, which holds one day and is lost if missed.

        # ex-rights / ex-dividend: at most once per day per board
        parsers = {"TSE": parse_exdiv_tse, "OTC": parse_exdiv_otc}
        for board in ("TSE", "OTC"):
            name = "exdiv_" + board.lower()
            h = _health(events, name)
            if _fresh_today(h, today) and h.get("ok"):
                continue

            def exdiv(h, board=board):
                raw = fetcher(EXDIV_URLS[board])
                if not isinstance(raw, list):
                    raise RuntimeError("not a list")
                got = parsers[board](raw)
                # An answer the parser cannot read is not "no events": a
                # renamed key parses to nothing, and an empty list during an
                # outage would erase ex-dates that are still ahead (and the
                # reduced reference price limit_flags builds from them).
                if raw and not _has_keys(raw, EXDIV_KEYS[board]):
                    raise RuntimeError(
                        "{} rows without the ex-date keys".format(len(raw)))
                ahead = _upcoming_exdiv(events["exdiv"].get(board), today)
                if not got and ahead:
                    raise RuntimeError(
                        "empty answer would drop {} upcoming ex-dates".format(ahead))
                events["exdiv"][board] = got
                _mark(h, True, rows=sum(len(v) for v in got.values()),
                      as_of=today.isoformat(), now_s=now_s)
            attempt(name, exdiv)

        # conferences from the daily announcements: every run, accumulated
        id_keys = {"TSE": K_ID, "OTC": "SecuritiesCompanyCode"}
        for board in ("TSE", "OTC"):
            name = "news_" + board.lower()

            def news(h, board=board):
                raw = fetcher(NEWS_URLS[board])
                if not isinstance(raw, list):
                    raise RuntimeError("not a list")
                if raw and not _has_keys(raw, (K_CLAUSE, K_FACT, id_keys[board])):
                    raise RuntimeError(
                        "{} rows without the announcement keys".format(len(raw)))
                got = parse_conf_news(raw, id_keys[board])
                for sid, ds in got.items():
                    for d in ds:
                        _merge_conf(events["conf"], sid, d, d, "news")
                spoken = max((_roc_iso(r.get("\u767c\u8a00\u65e5\u671f")) or ""
                              for r in raw if isinstance(r, dict)), default="")
                _mark(h, True, rows=sum(len(v) for v in got.values()),
                      as_of=spoken or None, now_s=now_s)
            attempt(name, news)

        # revenue (optional MOPS pages), only while the openapi lags M-1
        target = _prev_month(today)
        for board in ("TSE", "OTC"):
            name = "revenue_mops_" + board.lower()
            key = "MOPS_" + board
            h = _health(events, name)
            api_month = max((v.get("month") or "" for v in
                             (events["revenue"].get(board) or {}).values()),
                            default="")
            if api_month >= target:
                events["revenue"].pop(key, None)
                _mark(h, True, rows=0, now_s=now_s, skipped="openapi current")
                continue
            if _fresh_today(h, today) and h.get("ok"):
                continue

            def mops_rev(h, board=board, key=key):
                y, m = int(target[:4]), int(target[5:7])
                url = MOPS_REV_URL.format(board=MOPS_BOARD[board],
                                          rocy=y - 1911, m=m)
                got = parse_mops_revenue_html(fetcher(url, encoding="cp950"),
                                              target)
                if len(got) < MOPS_REV_MIN_ROWS:
                    raise RuntimeError("{} rows < floor {}".format(
                        len(got), MOPS_REV_MIN_ROWS))
                for v in got.values():
                    v.pop("note", None)
                events["revenue"][key] = got
                as_of = max((v["as_of"] or "" for v in got.values()), default="")
                _mark(h, True, rows=len(got), as_of=as_of or None, now_s=now_s)
            attempt(name, mops_rev)

        # conferences from the optional MOPS monthly list: once per day,
        # this month and next
        months = [today, _next_month_first(today)]
        for board in ("TSE", "OTC"):
            name = "conf_mops_" + board.lower()
            h = _health(events, name)
            if _fresh_today(h, today) and h.get("ok"):
                continue

            def mops_conf(h, board=board):
                n = 0
                for d in months:
                    body = {"encodeURIComponent": "1", "step": "1",
                            "firstin": "1", "off": "1",
                            "TYPEK": MOPS_BOARD[board],
                            "year": str(d.year - 1911), "month": str(d.month),
                            "co_id": ""}
                    got = parse_mops_conf_html(
                        fetcher(CONF_URL, method="POST", data=body,
                                encoding="cp950"))
                    for sid, spans in got.items():
                        for (s, e) in spans:
                            _merge_conf(events["conf"], sid, s, e, "mops")
                            n += 1
                _mark(h, True, rows=n, as_of=today.isoformat(), now_s=now_s)
            attempt(name, mops_conf)

        # prune past events, then stamp staleness
        cut = (today - timedelta(days=PRUNE_DAYS)).isoformat()
        for board, blk in list(events["exdiv"].items()):
            if not isinstance(blk, dict):
                events["exdiv"].pop(board, None)
                continue
            for sid in list(blk):
                keep = [e for e in blk[sid] if str(e.get("date") or "") >= cut]
                if keep:
                    blk[sid] = keep
                else:
                    blk.pop(sid)
        for sid in list(events["conf"]):
            keep = [e for e in events["conf"][sid]
                    if isinstance(e, list) and len(e) == 3 and str(e[1]) >= cut]
            if keep:
                events["conf"][sid] = keep
            else:
                events["conf"].pop(sid)
        for name in SOURCES:
            h = _health(events, name)
            kind = SOURCES[name][0]
            last = str(h.get("fetched_at") or "")[:10]
            try:
                age = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
            except ValueError:
                age = None
            h["stale"] = age is None or age > MAX_AGE_DAYS[kind]
            if h.get("skipped") == "openapi current":
                h["stale"] = False
        events["updated_at"] = now_s
        events["version"] = CACHE_VERSION
        ok = {n: h.get("ok") for n, h in events["sources"].items()}
        log("  [events] revenue {} / exdiv {} / conf {} stock(s); failed: {}"
            .format(sum(len(b) for b in events["revenue"].values()),
                    sum(len(b) for b in events["exdiv"].values()),
                    len(events["conf"]),
                    ", ".join(sorted(n for n, v in ok.items() if not v))
                    or "none"))
        if write:
            save_cache(events, cache_path)
    except Exception as e:
        try:
            log("  [events] refresh failed, cache kept: {}: {}".format(
                type(e).__name__, str(e)[:120]))
        except Exception:
            pass
    return events


# --------------------------------------------------------------- annotate
def _session_of(df, session_date):
    if session_date:
        return str(session_date)[:10]
    try:
        if df is not None and "Data_Date" in df.columns and len(df):
            v = max(str(x)[:10] for x in df["Data_Date"].dropna())
            if v:
                return v
    except Exception:
        pass
    return date.today().isoformat()


def _revenue_for(events, sid, session_month):
    best = None
    for blk in (events.get("revenue") or {}).values():
        r = blk.get(sid) if isinstance(blk, dict) else None
        if not isinstance(r, dict):
            continue
        m = str(r.get("month") or "")
        # a month is reported only after it ends: never one at or after the
        # session's own month
        if len(m) != 7 or m >= session_month:
            continue
        key = (m, str(r.get("as_of") or ""))
        if best is None or key > best[0]:
            best = (key, r)
    return best[1] if best else None


def _exdiv_for(events, sid, session):
    nxt = []
    for blk in (events.get("exdiv") or {}).values():
        for e in (blk.get(sid) or []) if isinstance(blk, dict) else []:
            if isinstance(e, dict) and str(e.get("date") or "") > session \
                    and e.get("kind") in EX_KINDS:
                nxt.append(e)
    return min(nxt, key=lambda e: e["date"]) if nxt else None


def _conf_for(events, sid, session):
    """Start date of the first conference still running on or after the
    session (a multi-day range may start before it)."""
    spans = [e for e in ((events.get("conf") or {}).get(sid) or [])
             if isinstance(e, list) and len(e) >= 2 and str(e[1]) >= session]
    return min(str(e[0]) for e in spans) if spans else None


def annotate_events(df, events, session_date=None):
    """Add the nine display-only EVENT_COLUMNS to `df` (a copy). They are
    always present -- all null when `events` is None or empty -- so a column
    can never go missing from the payload. Never raises for a DataFrame."""
    if df is None:
        return df
    out = df.copy()
    n = len(out)
    cols = {c: [None] * n for c in EVENT_COLUMNS}
    try:
        if events and n and "Stock_ID" in out.columns:
            session = _session_of(out, session_date)
            smonth = session[:7]
            for i, sid in enumerate(out["Stock_ID"].tolist()):
                sid = str(sid or "").strip()
                rv = _revenue_for(events, sid, smonth)
                if rv:
                    cols["Rev_Month"][i] = rv.get("month")
                    cols["Rev_Amount_K"][i] = _num(rv.get("amount_k"))
                    cols["Rev_YoY_Pct"][i] = _num(rv.get("yoy"))
                    cols["Rev_MoM_Pct"][i] = _num(rv.get("mom"))
                    cols["Rev_Cum_YoY_Pct"][i] = _num(rv.get("cum_yoy"))
                ex = _exdiv_for(events, sid, session)
                if ex:
                    cols["Ex_Date"][i] = ex["date"]
                    cols["Ex_Kind"][i] = ex["kind"]
                    cols["Ex_Cash_Div"][i] = _num(ex.get("cash"))
                cols["Conf_Date"][i] = _conf_for(events, sid, session)
    except Exception as e:
        print("  [events] annotate failed, columns left null: {}".format(e))
        cols = {c: [None] * n for c in EVENT_COLUMNS}
    for c in EVENT_COLUMNS:
        out[c] = pd.Series(cols[c], index=out.index, dtype=object)
    return out


# ------------------------------------------------------------------- meta
def _roll_to_session(d):
    try:
        from scanner.market_calendar import is_session
    except Exception:
        is_session = None
    for _ in range(15):
        if d.weekday() < 5:
            ok = None
            if is_session is not None:
                try:
                    ok = is_session(d, fetch=False)[0]
                except Exception:
                    ok = None
            if ok is None or ok:
                return d
        d += timedelta(days=1)
    return d


def next_revenue_deadline(session):
    """The 10th (rolled to a session) on or after `session`."""
    d = _as_date(session)
    dl = _roll_to_session(d.replace(day=REVENUE_DEADLINE_DAY))
    if dl < d:
        dl = _roll_to_session(_next_month_first(d).replace(
            day=REVENUE_DEADLINE_DAY))
    return dl.isoformat()


def revenue_deadline(month):
    """The statutory deadline for month M's revenue ('YYYY-MM'): the 10th
    of M+1, rolled forward to the next session when it is not one."""
    y, m = int(str(month)[:4]), int(str(month)[5:7])
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return _roll_to_session(date(y2, m2, REVENUE_DEADLINE_DAY)).isoformat()


def revenue_month_visible(month, day):
    """Point-in-time rule for grouping HISTORICAL signals by revenue
    (docs/BACKTEST_LOG.md M.6): month M counts as known on `day` only when
    `day` is after revenue_deadline(M). The live Rev_* columns do not use
    it -- they show what the exchange had published at scan time, labelled
    with its month. Bad input -> False."""
    try:
        return _as_date(day).isoformat() > revenue_deadline(month)
    except Exception:
        return False


def next_report_deadline(session):
    """{date, what, approximate} -- the next statutory financial-report
    deadline on or after `session`."""
    d = _as_date(session)
    for year in (d.year, d.year + 1):
        for (m, day, what) in REPORT_DEADLINES:
            dl = date(year, m, day)
            if dl >= d:
                return {"date": dl.isoformat(), "what": what,
                        "approximate": True}
    return None


def meta_block(events, session_date, df=None, tracked=None):
    """meta.events for the payload: per-source health, the latest revenue
    month, the next statutory deadlines (labelled deadlines, not report
    dates) and row coverage. Never raises; None when there is nothing."""
    try:
        if not isinstance(events, dict):
            return None
        session = str(session_date or date.today().isoformat())[:10]
        smonth = session[:7]
        months = [str(r.get("month") or "")
                  for blk in (events.get("revenue") or {}).values()
                  if isinstance(blk, dict)
                  for r in blk.values() if isinstance(r, dict)]
        months = [m for m in months if len(m) == 7 and m < smonth]
        cover = {"rows": 0, "revenue": 0, "exdiv": 0, "conf": 0}
        for frame in (df, tracked):
            if frame is None or getattr(frame, "empty", True):
                continue
            cover["rows"] += int(len(frame))
            for col, key in (("Rev_Month", "revenue"), ("Ex_Date", "exdiv"),
                             ("Conf_Date", "conf")):
                if col in frame.columns:
                    cover[key] += int(frame[col].notna().sum())
        sources = {}
        for name in SOURCES:
            h = (events.get("sources") or {}).get(name)
            if isinstance(h, dict):
                sources[name] = {k: h.get(k) for k in
                                 ("ok", "rows", "as_of", "fetched_at",
                                  "stale", "error", "optional", "skipped")
                                 if k in h}
        required_ok = all(sources.get(n, {}).get("ok")
                          for n, (_k, opt) in SOURCES.items() if not opt)
        return {
            "ok": bool(required_ok),
            "updated_at": events.get("updated_at"),
            "session_date": session,
            "sources": sources,
            "revenue_month_latest": max(months) if months else None,
            "next_revenue_deadline": next_revenue_deadline(session),
            "next_report_deadline": next_report_deadline(session),
            "coverage": cover,
            "note": "display only; never scored",
        }
    except Exception as e:
        print("  [events] meta skipped: {}".format(e))
        return None
