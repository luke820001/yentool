"""
Trade restrictions per stock: disposition, altered trading, suspension,
signal-day limit lock and the attention list. ASCII only.

Why this exists (plan P0-1, 2026-10-08). Nothing in the scanner knew that a
name was in a disposition period. 2026-10-07 shipped 8227 (TPEX disposition
10-02..10-08: manual matching about every 2 minutes, every order prepaid) as
Buy_Ready with no word about it, and 6533 / the tracked 3055, 6672 and 3441
were in disposition with nothing on their cards. The order a holder or a
buyer places in such a name does not behave like a normal order.

What it does by default: DISPLAY + ORDER GUIDANCE + CHECKS. Every row gets
six columns (RESTRICTION_COLUMNS); the phone renders the label, the period,
the matching interval and the prepayment terms from them. Only the kinds in
BLOCKING_RESTRICTIONS make scan_mode.mark_buy_ready refuse a buy, and that set
is ("suspended",) -- a name the exchange is not trading cannot be bought at
the next open anyway. Disposition is deliberately NOT blocking: the
preliminary research (plan P2-1, DECISIONS correction 3) found the signals
that entered DURING a TPEX disposition period were the best bucket (82.5% /
+5.92 against 68.2% / +1.07), so a block would cut about 40% of the return
sum on the strength of a hunch. The formal research verdict goes to
docs/BACKTEST_LOG.md section M; flipping the constant is the whole change
(and then bump scan_mode.STRATEGY_VERSION, it changes the buy rule).

A failed feed is NOT a block either: it yields Trade_Restriction "unknown"
on that board's rows and a meta warning (result_checks
restrictions_feed_failed). Restrictions are information by default, and
"we could not read the list" is information too. The altered / halt feeds
follow the same rule (2026-10-09): when one is unreadable, a name it does not
list reads 'unknown' (never 'none'), because the one blocking kind,
'suspended', comes from exactly those feeds; result_checks raises the warning
altered_feed_failed.

Sources (all live-verified 2026-10-08, TLS on, plain GET):
  disposition  TPEX openapi tpex_disposal_information, fallback the TPEX web
               bulletin (period-overlap filter); TWSE openapi
               announcement/punish, fallback the TWSE rwd punish query.
  attention    TPEX openapi tpex_trading_warning_information (last 2 dates);
               TWSE rwd announcement/notice over the last 7 calendar days
               (the openapi /announcement/notice is same-day and a
               placeholder at night -- not used).
  altered      TPEX openapi tpex_cmode (AlteredTrading / ManagedStock /
               PeriodicTrading / MatchingFrequency = altered,
               SuspensionOfTrading = suspended); TWSE openapi TWT85U
               (altered); TWSE openapi TWTAWU (trading halts = suspended).

Publication timing (Last-Modified): the TPEX lists refresh about 23:30
Taipei on day D, the TWSE ones about 05:20-05:30 on D+1. A 15:00 scan on D
therefore sees announcements up to the evening of D-1; a disposition
announced after the close of D that starts on D+1 is invisible until the
evening refresh (owned by the list-freeze stage, tighten-only).

Matching is by exact Stock_ID: CB / warrant codes (65331, 705235) are kept
and never touch the parent stock's row.

This module must not import scanner.scan_mode (scan_mode imports it).
"""
import json
import re
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from scanner.market_filter import REQUEST_TIMEOUT, _HEADERS
from scanner.tick import round_to_tick

# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------
# Most severe first. Trade_Restriction is the most severe flag a row carries,
# and it is never empty: "none" is spelled out because result_checks treats
# "" as null, i.e. "never checked".
RESTRICTION_KINDS = ("suspended", "disposition", "altered", "limit_lock",
                     "unknown", "attention", "none")
SEVERITY = {k: i for i, k in enumerate(RESTRICTION_KINDS)}

# The kinds that make mark_buy_ready refuse a buy (Buy_Block "restricted",
# lowest priority in its mask chain, so it binds only when every other gate
# passes). ("suspended",) by decision of 2026-10-08: disposition entries were
# the BEST bucket in the preliminary P2-1 numbers, see the module docstring
# and docs/BACKTEST_LOG.md section M. Read at call time by mark_buy_ready and
# the checker, so a flip here (or a test patch) is the whole change. Adding
# "unknown" makes an unreadable feed -- and a frame never annotated -- block.
BLOCKING_RESTRICTIONS = ("suspended",)

# flags shown on the card but never a Trade_Restriction kind
DISPLAY_FLAGS = ("limit_down",)
PREPAY_KINDS = ("all", "threshold")

RESTRICTION_COLUMNS = (
    "Trade_Restriction",        # RESTRICTION_KINDS, never empty
    "Restriction_Flags",        # every flag, sorted, comma-joined, or None
    "Restriction_Since",        # earliest start of the covering dispositions
    "Restriction_Until",        # latest end of the covering dispositions
    "Restriction_Match_Min",    # matching interval in minutes (strictest)
    "Restriction_Prepay",       # 'all' | 'threshold' | None
)

BOARDS = ("OTC", "TSE")

TPEX_DISPOSAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"
TPEX_DISPOSAL_WEB_URL = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
TWSE_PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TWSE_PUNISH_WEB_URL = "https://www.twse.com.tw/rwd/zh/announcement/punish"
TPEX_WARNING_URL = ("https://www.tpex.org.tw/openapi/v1/"
                    "tpex_trading_warning_information")
TWSE_NOTICE_WEB_URL = "https://www.twse.com.tw/rwd/zh/announcement/notice"
TPEX_CMODE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_cmode"
TWSE_ALTERED_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT85U"
TWSE_HALT_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWTAWU"

TPEX_WEB_DAYS_AHEAD = 14      # web bulletin window: session .. session + 14
TWSE_WEB_DAYS_BACK = 30       # rwd punish window: session - 30 .. session
TWSE_NOTICE_DAYS_BACK = 7     # rwd notice window: session - 7 .. session

# Whole-fetch budget in seconds. The two primary disposition lists always get
# their try; fallbacks and the best-effort lists are skipped once it is spent
# so a hanging exchange cannot hold the 15:0x publish hostage.
FETCH_BUDGET_S = 75.0
FETCH_TIMEOUT = min(REQUEST_TIMEOUT, 15)

# Chinese keywords as escapes so this file stays ASCII.
_KW_CODE = u"\u8b49\u5238\u4ee3\u865f"                 # zheng quan dai hao
_KW_PERIOD = u"\u8655\u7f6e\u8d77"                     # chu zhi qi (shi jian)
_KW_ANNOUNCED = u"\u516c\u5e03\u65e5\u671f"            # gong bu ri qi
_KW_CONTENT = u"\u8655\u7f6e\u5167\u5bb9"              # chu zhi nei rong
_KW_DATE = u"\u65e5\u671f"                             # ri qi
_KW_THRESHOLD = u"\u55ae\u7b46\u9054"                  # dan bi da
_KW_PREPAY_ALL = u"\u5168\u90e8\u4e4b\u8cb7\u9032\u50f9\u91d1"   # all buy money
_KW_NO_DATA = u"\u6c92\u6709"                          # mei you (no data)
_FW_Y = u"\uff39"                                      # fullwidth Y
_FW_TILDE = u"\uff5e"                                  # fullwidth tilde

_CN_DIGITS = {u"\u96f6": 0, u"\u3007": 0, u"\u4e00": 1, u"\u4e8c": 2,
              u"\u5169": 2, u"\u4e09": 3, u"\u56db": 4, u"\u4e94": 5,
              u"\u516d": 6, u"\u4e03": 7, u"\u516b": 8, u"\u4e5d": 9}
_CN_TEN = u"\u5341"
# yue mei <n> fen zhong ("about every n minutes"); n in ASCII, fullwidth or
# Chinese numerals (TWSE writes er = 2, TPEX writes 2)
_MATCH_RE = re.compile(
    u"\u7d04\u6bcf\\s*([0-9\uff10-\uff19" + u"".join(_CN_DIGITS) + _CN_TEN
    + u"]+)\\s*\u5206\u9418")
_CODE_RE = re.compile(r"^[0-9A-Z]{4,6}$")


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------
def _roc_iso(s):
    """'115/10/02' | '115.10.07' | '115-10-07' | '1151007' (ROC) |
    '20261007' (Gregorian) | '2026-10-07' -> '2026-10-07'; None when it is
    not a real date."""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    try:
        if "/" in t or "." in t or "-" in t:
            parts = re.split(r"[/.\-]", t)
            if len(parts) != 3:
                return None
            y, m, d = (int(p) for p in parts)
        elif t.isdigit() and len(t) == 8:
            y, m, d = int(t[:4]), int(t[4:6]), int(t[6:])
        elif t.isdigit() and len(t) in (6, 7):
            y, m, d = int(t[:-4]), int(t[-4:-2]), int(t[-2:])
        else:
            return None
        if y < 1911:
            y += 1911
        return date(y, m, d).isoformat()
    except (TypeError, ValueError):
        return None


def _period(s):
    """'1151002~1151008' | '115/10/06<FULLWIDTH TILDE>115/10/13' ->
    (start, end) ISO, or None."""
    if s is None:
        return None
    t = str(s).replace(_FW_TILDE, "~").strip()
    if "~" not in t:
        return None
    a, b = t.split("~", 1)
    start, end = _roc_iso(a), _roc_iso(b)
    if not start or not end or end < start:
        return None
    return start, end


def _cn_int(s):
    """'2' | fullwidth 2 | 'er' | 'shi' | 'er shi wu' -> int, else None."""
    t = str(s or "").strip()
    if not t:
        return None
    if t.isdigit():
        try:
            return int(t)          # int() reads fullwidth digits too
        except ValueError:
            return None
    if any(ch not in _CN_DIGITS and ch != _CN_TEN for ch in t):
        return None
    if _CN_TEN in t:
        head, _, tail = t.partition(_CN_TEN)
        tens = _CN_DIGITS.get(head, None) if head else 1
        ones = _CN_DIGITS.get(tail, None) if tail else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(t) == 1:
        return _CN_DIGITS.get(t)
    return None


def match_minutes(text):
    """The matching interval of a disposition, in minutes, from its condition
    text ('... yue mei 2 fen zhong ...'), or None. The strictest (largest)
    interval when the text names more than one."""
    found = [_cn_int(m) for m in _MATCH_RE.findall(str(text or ""))]
    found = [n for n in found if n]
    return max(found) if found else None


def prepay_terms(text):
    """'threshold' when only orders of 10+ lots (or 30+ cumulative) must be
    prepaid, 'all' when every order is; None without any text."""
    t = str(text or "")
    if not t.strip():
        return None
    if _KW_THRESHOLD in t:
        return "threshold"
    # Every disposition measure includes prepayment; without the threshold
    # clause it applies to every order (second dispositions, most TPEX ones).
    return "all"


def _code(v):
    t = str(v if v is not None else "").strip()
    return t if _CODE_RE.match(t) else ""


def _flag_y(v):
    t = str(v if v is not None else "").strip().upper()
    return t in ("Y", _FW_Y)


def _empty_stats():
    return {"raw": 0, "rows": 0, "placeholder": 0}


def _keyed(r, keys):
    """A dict row that carries at least one of the expected keys. A row
    without any of them is neither data nor a placeholder: a renamed field
    must surface as a shape error (fail visible), not as an empty list that
    marks every name 'none'."""
    return isinstance(r, dict) and any(k in r for k in keys)


def _add_period(out, sid, period):
    out.setdefault(sid, []).append(period)


def _disposal_row(board, code, period_text, announced, condition):
    """One parsed disposition, or 'placeholder' / None (unparseable)."""
    sid = _code(code)
    if not sid or not str(period_text or "").strip():
        return "placeholder"
    p = _period(period_text)
    if p is None:
        return None
    return sid, {"board": board, "start": p[0], "end": p[1],
                 "announced": _roc_iso(announced),
                 "match_min": match_minutes(condition),
                 "prepay": prepay_terms(condition)}


def _tally(out, stats, parsed):
    if parsed == "placeholder":
        stats["placeholder"] += 1
    elif parsed is not None:
        _add_period(out, parsed[0], parsed[1])
        stats["rows"] += 1


# --------------------------------------------------------------------------
# parsers: pure functions on the decoded JSON
# --------------------------------------------------------------------------
# Each returns (result, stats) with stats {raw, rows, placeholder}, or
# (None, stats) when the document does not have the expected shape at all.
def parse_tpex_disposal(data):
    """TPEX openapi tpex_disposal_information -> ({sid: [period]}, stats).
    Fields: SecuritiesCompanyCode, DispositionPeriod ('1151002~1151008'),
    Date (announcement, '1151001'), DisposalCondition (the measures)."""
    stats = _empty_stats()
    if not isinstance(data, list):
        return None, stats
    out = {}
    for r in data:
        stats["raw"] += 1
        if not _keyed(r, ("SecuritiesCompanyCode", "DispositionPeriod")):
            continue
        _tally(out, stats, _disposal_row(
            "OTC", r.get("SecuritiesCompanyCode"), r.get("DispositionPeriod"),
            r.get("Date"), r.get("DisposalCondition")))
    return out, stats


def parse_twse_punish(data):
    """TWSE openapi announcement/punish -> ({sid: [period]}, stats).
    Fields: Code, DispositionPeriod ('115/10/06<U+FF5E>115/10/13'), Date,
    Detail (measures; the interval in Chinese numerals)."""
    stats = _empty_stats()
    if not isinstance(data, list):
        return None, stats
    out = {}
    for r in data:
        stats["raw"] += 1
        if not _keyed(r, ("Code", "DispositionPeriod")):
            continue
        _tally(out, stats, _disposal_row(
            "TSE", r.get("Code"), r.get("DispositionPeriod"), r.get("Date"),
            r.get("Detail")))
    return out, stats


def _field_index(fields, keyword, exact=False):
    for i, f in enumerate(fields or []):
        f = str(f or "").strip()
        if (f == keyword) if exact else (keyword in f):
            return i
    return None


def _cell(row, i):
    if i is None or not isinstance(row, (list, tuple)) or i >= len(row):
        return None
    return row[i]


def _web_table(fields, data, board):
    stats = _empty_stats()
    i_code = _field_index(fields, _KW_CODE)
    i_per = _field_index(fields, _KW_PERIOD)
    if i_code is None or i_per is None or not isinstance(data, list):
        stats["raw"] = len(data) if isinstance(data, list) else 0
        return None, stats
    i_ann = _field_index(fields, _KW_ANNOUNCED)
    i_txt = _field_index(fields, _KW_CONTENT)
    out = {}
    for r in data:
        stats["raw"] += 1
        # TPEX web names carry a link suffix "(../../...)"; only the code
        # column is read, never the name.
        _tally(out, stats, _disposal_row(
            board, _cell(r, i_code), _cell(r, i_per), _cell(r, i_ann),
            _cell(r, i_txt)))
    return out, stats


def _no_data(stat):
    return _KW_NO_DATA in str(stat or "")


def parse_tpex_disposal_web(data):
    """TPEX web bulletin/disposal?response=json -> ({sid: [period]}, stats).
    tables[0].fields / data; the period column reads '115/10/02~115/10/08'."""
    stats = _empty_stats()
    if not isinstance(data, dict):
        return None, stats
    tables = data.get("tables")
    if not isinstance(tables, list) or not tables:
        if str(data.get("stat") or "").lower() == "ok" or _no_data(data.get("stat")):
            return {}, stats
        return None, stats
    t = tables[0] if isinstance(tables[0], dict) else {}
    return _web_table(t.get("fields"), t.get("data") or [], "OTC")


def parse_twse_punish_web(data):
    """TWSE rwd announcement/punish?response=json -> ({sid: [period]}, stats).
    fields / data; a no-data answer is a stat text, not an error."""
    stats = _empty_stats()
    if not isinstance(data, dict):
        return None, stats
    if str(data.get("stat") or "").upper() != "OK":
        return ({}, stats) if _no_data(data.get("stat")) else (None, stats)
    return _web_table(data.get("fields"), data.get("data") or [], "TSE")


def parse_tpex_warning(data):
    """TPEX openapi tpex_trading_warning_information -> ({sid: last date},
    stats). It carries the last two attention dates."""
    stats = _empty_stats()
    if not isinstance(data, list):
        return None, stats
    out = {}
    for r in data:
        stats["raw"] += 1
        if not _keyed(r, ("SecuritiesCompanyCode",)):
            continue
        sid, d = _code(r.get("SecuritiesCompanyCode")), _roc_iso(r.get("Date"))
        if not sid or not d:
            stats["placeholder"] += 1
            continue
        stats["rows"] += 1
        if d > out.get(sid, ""):
            out[sid] = d
    return out, stats


def parse_twse_notice(data):
    """TWSE rwd announcement/notice?response=json -> ({sid: last date},
    stats). Fields include the code and the date ('115.10.07')."""
    stats = _empty_stats()
    if not isinstance(data, dict):
        return None, stats
    if str(data.get("stat") or "").upper() != "OK":
        return ({}, stats) if _no_data(data.get("stat")) else (None, stats)
    fields = data.get("fields") or []
    rows = data.get("data") or []
    i_code = _field_index(fields, _KW_CODE)
    i_date = _field_index(fields, _KW_DATE, exact=True)
    if i_code is None or i_date is None or not isinstance(rows, list):
        return None, stats
    out = {}
    for r in rows:
        stats["raw"] += 1
        sid, d = _code(_cell(r, i_code)), _roc_iso(_cell(r, i_date))
        if not sid or not d:
            stats["placeholder"] += 1
            continue
        stats["rows"] += 1
        if d > out.get(sid, ""):
            out[sid] = d
    return out, stats


def parse_tpex_cmode(data):
    """TPEX openapi tpex_cmode -> ({sid: 'altered' | 'suspended'}, stats).
    The flags are a fullwidth Y; the key ' FinancialAnnouncements' has a
    leading space (not used)."""
    stats = _empty_stats()
    if not isinstance(data, list):
        return None, stats
    out = {}
    for r in data:
        stats["raw"] += 1
        if not _keyed(r, ("SecuritiesCompanyCode",)):
            continue
        sid = _code(r.get("SecuritiesCompanyCode"))
        if not sid:
            stats["placeholder"] += 1
            continue
        stats["rows"] += 1
        if _flag_y(r.get("SuspensionOfTrading")):
            out[sid] = "suspended"
        elif any(_flag_y(r.get(k)) for k in ("AlteredTrading", "ManagedStock",
                                             "PeriodicTrading",
                                             "MatchingFrequency")):
            out.setdefault(sid, "altered")
    return out, stats


def parse_twse_altered(data):
    """TWSE openapi exchangeReport/TWT85U -> ({sid: 'altered'}, stats).
    Membership is the fact (altered trading method / full delivery)."""
    stats = _empty_stats()
    if not isinstance(data, list):
        return None, stats
    out = {}
    for r in data:
        stats["raw"] += 1
        if not _keyed(r, ("Code",)):
            continue
        sid = _code(r.get("Code"))
        if not sid:
            stats["placeholder"] += 1
            continue
        stats["rows"] += 1
        out[sid] = "altered"
    return out, stats


def parse_twse_halts(data, on_day=None):
    """TWSE openapi exchangeReport/TWTAWU (trading halts) -> ({sid:
    'suspended'}, stats): halted on `on_day` (the next session), i.e. halt
    date <= on_day and no resumption on or before it."""
    stats = _empty_stats()
    if not isinstance(data, list):
        return None, stats
    out = {}
    for r in data:
        stats["raw"] += 1
        if not _keyed(r, ("Code", "TradingHaltDate")):
            continue
        sid = _code(r.get("Code"))
        halt = _roc_iso(r.get("TradingHaltDate"))
        if not sid or not halt:
            stats["placeholder"] += 1
            continue
        stats["rows"] += 1
        resume = _roc_iso(r.get("TradingResumptionDate"))
        if on_day and halt <= on_day and (not resume or resume > on_day):
            out[sid] = "suspended"
    return out, stats


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def _get_json(url, params=None, timeout=FETCH_TIMEOUT, tries=2, backoff=3.0):
    """GET and decode JSON. (data, last_modified) on success, (None, None)
    when every try failed -- HTTP error, timeout, an HTML throttle page that
    does not decode. Never market_filter._fetch_json: its [] on failure is
    indistinguishable from an empty list, which here would mark every name
    'none' (fail-open)."""
    for attempt in range(max(1, int(tries))):
        try:
            r = requests.get(url, params=params, headers=_HEADERS,
                             timeout=timeout)
            if r.status_code == 200:
                return r.json(), r.headers.get("Last-Modified")
        except Exception:
            pass
        if attempt + 1 < tries:
            time.sleep(backoff * (attempt + 1))
    return None, None


def _day(value):
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def next_session_after(session):
    """The session after `session` from the committed TWSE holiday schedule
    (scanner.market_calendar, no network), else the next weekday; None when
    `session` is empty or not a date (never raises)."""
    if not session:
        return None
    try:
        from scanner.market_calendar import next_session
        return next_session(session, fetch=False)[0]
    except Exception:
        try:
            d = _day(session) + timedelta(days=1)
        except (TypeError, ValueError):
            return None
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.isoformat()


def _board_entry():
    return {"ok": False, "source": None, "rows": 0, "last_modified": None,
            "error": None}


def empty_info(session=None):
    """The shape fetch_restrictions returns, with nothing fetched."""
    return {
        "ok": False,
        "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "session": session or None,
        "next_session": next_session_after(session) if session else None,
        "boards": {b: _board_entry() for b in BOARDS},
        "disposition": {},
        "attention": {},
        "attention_latest": {b: None for b in BOARDS},
        "attention_ok": {b: False for b in BOARDS},
        "altered": {},
        "altered_ok": {b: False for b in BOARDS},
    }


def _date10(v):
    """'YYYY-MM-DD' from a date-like cell, '' for None / NaN / blank."""
    if v is None:
        return ""
    try:
        if v != v:                  # NaN / NaT
            return ""
    except Exception:
        pass
    return str(v).strip()[:10]


def _parse_checked(parser, data):
    """(result, error) where error is None, 'fetch' or 'shape'. A non-empty
    list that parses to nothing and has no placeholder row is a shape change
    (renamed fields), not an empty list."""
    if data is None:
        return None, "fetch"
    try:
        got, st = parser(data)
    except Exception:
        return None, "shape"
    if got is None:
        return None, "shape"
    if st["raw"] > 0 and st["rows"] == 0 and st["placeholder"] == 0:
        return None, "shape"
    return got, None


def fetch_restrictions(session_date=None, budget_s=FETCH_BUDGET_S):
    """Fetch and parse every restriction list for the scan of `session_date`
    (default: today). Never raises.

    Returns {ok, fetched_at, session, next_session,
             boards: {OTC|TSE: {ok, source ('openapi'|'web'|None), rows,
                                last_modified, error}},
             disposition: {sid: [{board, start, end, announced, match_min,
                                  prepay}]},
             attention: {sid: last date}, attention_latest: {board: date},
             attention_ok: {board: bool},
             altered: {sid: 'altered'|'suspended'}, altered_ok: {board: bool}}

    `ok` is the two disposition lists (primary or fallback); attention and
    altered are best effort and never make a board unknown.

    Budget: both primary disposition lists always get their full try (two
    tries of FETCH_TIMEOUT each), and they run FIRST. Every other call -- a
    fallback, the attention / altered / halt lists -- is skipped once
    `budget_s` is spent, and its timeout is cut to what is left, so the
    whole fetch stays within about max(budget_s, 2 primaries) seconds.

    An EMPTY primary disposition list is cross-checked against the web
    bulletin when the budget allows: TPEX has served empty lists during
    outages, and an empty list here would mark every name 'none'.
    """
    session = str(session_date or "")[:10] or date.today().isoformat()
    info = empty_info(session)
    info["ex_today"] = load_ex_today(session)
    t0 = time.monotonic()

    def left():
        return budget_s - (time.monotonic() - t0)

    def get(url, params=None, tries=2):
        """A non-primary call: never started past the budget, and never
        allowed to run (much) past it."""
        rem = left()
        if rem <= 0:
            return None, None
        if rem < 2 * FETCH_TIMEOUT:
            tries = 1
        return _get_json(url, params, timeout=max(1.0, min(FETCH_TIMEOUT, rem)),
                         tries=tries)

    try:
        nxt = info["next_session"]
        s_day = _day(session)
        boards = (
            ("OTC", (TPEX_DISPOSAL_URL, None, parse_tpex_disposal),
             (TPEX_DISPOSAL_WEB_URL,
              {"startDate": s_day.strftime("%Y/%m/%d"),
               "endDate": (s_day + timedelta(days=TPEX_WEB_DAYS_AHEAD)).strftime("%Y/%m/%d"),
               "response": "json"},
              parse_tpex_disposal_web)),
            ("TSE", (TWSE_PUNISH_URL, None, parse_twse_punish),
             (TWSE_PUNISH_WEB_URL,
              {"response": "json",
               "startDate": (s_day - timedelta(days=TWSE_WEB_DAYS_BACK)).strftime("%Y%m%d"),
               "endDate": s_day.strftime("%Y%m%d")},
              parse_twse_punish_web)),
        )
        # 1) both primary lists first, each with its full try
        primaries = {}
        for board, primary, _fallback in boards:
            data, lm = _get_json(primary[0], primary[1])
            got, err = _parse_checked(primary[2], data)
            primaries[board] = (got, err, lm)
        # 2) the fallback of a board whose primary failed or came back empty
        for board, _primary, fallback in boards:
            entry = info["boards"][board]
            got, err, lm = primaries[board]
            source = "openapi"
            if not got:                 # None (failed) or {} (empty)
                if got is None:
                    entry["primary_error"] = err
                if left() > 0:
                    data2, lm2 = get(fallback[0], fallback[1])
                    got2, err2 = _parse_checked(fallback[2], data2)
                    if got2 is not None and (got is None or got2):
                        if got is not None:
                            entry["primary_error"] = "empty"
                        got, err, lm, source = got2, None, lm2, "web"
                    elif got is None:
                        err = err2
                elif got is None:
                    err = "budget"
            if got is None:
                entry.update(ok=False, source=None, error=err)
                continue
            entry.update(ok=True, source=source, error=None,
                         last_modified=lm,
                         rows=sum(len(v) for v in got.values()))
            for sid, periods in got.items():
                info["disposition"].setdefault(sid, []).extend(periods)

        # attention (display only)
        latest = info["attention_latest"]
        for board, url, params, parser in (
                ("OTC", TPEX_WARNING_URL, None, parse_tpex_warning),
                ("TSE", TWSE_NOTICE_WEB_URL,
                 {"response": "json",
                  "startDate": (s_day - timedelta(days=TWSE_NOTICE_DAYS_BACK)).strftime("%Y%m%d"),
                  "endDate": s_day.strftime("%Y%m%d")},
                 parse_twse_notice)):
            if left() <= 0:
                continue
            got, err = _parse_checked(parser, get(url, params, tries=1)[0])
            if got is None:
                continue
            info["attention_ok"][board] = True
            latest[board] = max(got.values()) if got else None
            for sid, d in got.items():
                if d > info["attention"].get(sid, ""):
                    info["attention"][sid] = d

        # altered trading / suspension (best effort)
        alt = info["altered"]
        if left() > 0:
            got, err = _parse_checked(parse_tpex_cmode,
                                      get(TPEX_CMODE_URL, tries=1)[0])
            if got is not None:
                info["altered_ok"]["OTC"] = True
                alt.update(got)
        tse_ok = True
        for url, parser in ((TWSE_ALTERED_URL, parse_twse_altered),
                            (TWSE_HALT_URL,
                             lambda d: parse_twse_halts(d, on_day=nxt))):
            if left() <= 0:
                tse_ok = False
                continue
            got, err = _parse_checked(parser, get(url, tries=1)[0])
            if got is None:
                tse_ok = False
                continue
            for sid, kind in got.items():
                if kind == "suspended" or sid not in alt:
                    alt[sid] = kind
        info["altered_ok"]["TSE"] = tse_ok

        info["ok"] = all(info["boards"][b]["ok"] for b in BOARDS)
    except Exception as e:          # never take the scan down
        info["ok"] = False
        info["error"] = "{}: {}".format(type(e).__name__, str(e)[:120])
    info["elapsed_s"] = round(time.monotonic() - t0, 1)
    return info


# --------------------------------------------------------------------------
# annotate
# --------------------------------------------------------------------------
def session_of(df):
    """The newest non-empty Data_Date in the frame ('' when none) -- the same
    fallback mark_buy_ready uses when it is not told the session."""
    if df is None or "Data_Date" not in getattr(df, "columns", ()):
        return ""
    dates = [str(d)[:10] for d in df["Data_Date"]
             if d is not None and d == d and str(d).strip()]
    return max(dates) if dates else ""


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _truthy(v):
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    try:
        if v != v:
            return False
    except Exception:
        return False
    return bool(v)


def load_ex_today(session, path=None):
    """{sid: {date, kind, cash}}: the ex-dividend / ex-rights entries of the
    committed company-events cache (ingestion/company_events, data/
    company_events.json) dated `session`. Never raises ({} when the file is
    missing or unreadable). The cache is the one the PREVIOUS run committed:
    ex-dates are announced days ahead, so it already holds today's. Several
    entries for one id on the day (a cash and a rights line) read as 'both'.
    Read with plain json: this module must stay free of ingestion imports."""
    s = _date10(session)
    out = {}
    if not s:
        return out
    try:
        if path is None:
            from config.settings import COMPANY_EVENTS_FILE as path
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        for blk in ((got.get("exdiv") or {}) if isinstance(got, dict)
                    else {}).values():
            for sid, entries in (blk.items() if isinstance(blk, dict) else ()):
                for e in entries if isinstance(entries, list) else ():
                    if not isinstance(e, dict) or _date10(e.get("date")) != s:
                        continue
                    sid = str(sid).strip()
                    if sid in out:
                        out[sid]["kind"] = "both"
                        continue
                    out[sid] = {"date": s, "kind": e.get("kind"),
                                "cash": _f(e.get("cash"))}
    except Exception:
        return {}
    return out


def reference_price(prev, cash):
    """The exchange's reference price on a cash ex-dividend day: the previous
    close minus the dividend, floored to the cent (Decimal: 62.6 - 1.0 must
    be 61.60, not 61.59999). None when it is not a positive price."""
    try:
        from decimal import Decimal, ROUND_FLOOR
        base = (Decimal(str(prev)) - Decimal(str(cash))).quantize(
            Decimal("0.01"), rounding=ROUND_FLOOR)
    except Exception:
        return None
    return float(base) if base > 0 else None


def limit_flags(row, sid=None, ex=None):
    """(closed at limit-up, closed at limit-down) for the row's bar.

    limit-up = base x 1.10 rounded DOWN onto the quote ladder, limit-down =
    base x 0.90 rounded UP; a close exactly there is a lock (High_Today
    cannot exceed it). Skipped when Recent_Jump is set or a price is
    missing.

    `base` is Close_Prev, except on the name's ex-date, where the exchange
    uses the REDUCED reference price. `ex` is that day's company-events
    entry ({kind, cash}) or None:
      * kind 'div' with a cash amount: base = reference_price(prev, cash)
        (checked against TWSE TWT49U 2026: 1083 of 1090 cash-only rows hit
        the official limit-up, 1089 the limit-down; the misses are all
        01xxxT trust codes);
      * any other ex-date (rights, rights + dividend, no cash amount, a
        trust code): the reference is not derivable from the cache, so the
        answer is 'unknown' = (False, False) rather than a level computed
        from the raw close (which sat above every reachable close and, on
        the down side, named a level the stock never trades at).
    Without `ex` the raw previous close is used, as before."""
    if _truthy(row.get("Recent_Jump")):
        return False, False
    prev, close = _f(row.get("Close_Prev")), _f(row.get("Close_Price"))
    if prev is None or close is None or prev <= 0:
        return False, False
    sid = sid if sid is not None else str(row.get("Stock_ID") or "").strip()
    base = prev
    if ex is not None:
        cash = _f(ex.get("cash")) if isinstance(ex, dict) else None
        if (not isinstance(ex, dict) or ex.get("kind") != "div"
                or cash is None or cash <= 0 or sid.upper().endswith("T")):
            return False, False
        base = reference_price(prev, cash)
        if base is None:
            return False, False
    up = round_to_tick(base * 1.10, "down", sid)
    down = round_to_tick(base * 0.90, "up", sid)
    return (up is not None and abs(close - up) < 1e-6,
            down is not None and abs(close - down) < 1e-6)


def kind_of(flags):
    """The Trade_Restriction for a set of flags: the most severe kind, display
    flags ignored, 'none' when nothing applies."""
    kinds = [f for f in flags if f in SEVERITY and f != "none"]
    return min(kinds, key=SEVERITY.get) if kinds else "none"


def _unknown_row():
    return {"Trade_Restriction": "unknown", "Restriction_Flags": "unknown",
            "Restriction_Since": None, "Restriction_Until": None,
            "Restriction_Match_Min": None, "Restriction_Prepay": None}


def _covering(periods, session, nxt):
    """The dispositions in force at the next session (start <= next <= end);
    without a next session, those whose end is after the scanned one."""
    out = []
    for p in periods or []:
        start, end = p.get("start"), p.get("end")
        if not start or not end:
            continue
        if nxt:
            if start <= nxt <= end:
                out.append(p)
        elif session and end > session:
            out.append(p)
    return out


def restriction_of(row, info, session, nxt, ex_map=None):
    """The six RESTRICTION_COLUMNS for one row (a dict). `ex_map` is
    load_ex_today's result (default: info['ex_today'], which
    fetch_restrictions fills); None leaves the limit flags on the raw
    previous close."""
    sid = str(row.get("Stock_ID") if row.get("Stock_ID") is not None else "").strip()
    if not sid:
        return _unknown_row()
    board = "OTC" if str(row.get("Market") or "").strip() == "OTC" else "TSE"
    flags = set()
    detail = {"Restriction_Since": None, "Restriction_Until": None,
              "Restriction_Match_Min": None, "Restriction_Prepay": None}
    if not isinstance(info, dict):
        flags.add("unknown")
    else:
        b = (info.get("boards") or {}).get(board) or {}
        if not b.get("ok"):
            flags.add("unknown")
        else:
            cov = _covering((info.get("disposition") or {}).get(sid), session, nxt)
            if cov:
                flags.add("disposition")
                mins = [p.get("match_min") for p in cov if p.get("match_min")]
                prepay = [p.get("prepay") for p in cov if p.get("prepay")]
                detail.update(
                    Restriction_Since=min(p["start"] for p in cov),
                    Restriction_Until=max(p["end"] for p in cov),
                    # the strictest terms in force: the longest interval, and
                    # 'all' over 'threshold' (a re-disposition tightens both)
                    Restriction_Match_Min=int(max(mins)) if mins else None,
                    Restriction_Prepay=("all" if "all" in prepay else
                                        "threshold" if prepay else None))
        alt = (info.get("altered") or {}).get(sid)
        if alt in ("altered", "suspended"):
            flags.add(alt)
        elif (info.get("altered_ok") or {}).get(board) is False:
            # The altered / halt feeds were unreadable: "not listed" proves
            # nothing, and 'suspended' is the one kind that blocks a buy. The
            # row says so ('unknown' is display only, it never blocks).
            flags.add("unknown")
        seen = (info.get("attention") or {}).get(sid)
        latest = (info.get("attention_latest") or {}).get(board)
        if seen and latest and seen >= latest:
            flags.add("attention")
    # The limit flags describe the session's own bar: a row whose bar is
    # older (a halted / lagging name) did not close at the limit today.
    own = _date10(row.get("Data_Date"))
    if not session or not own or own == session:
        if ex_map is None and isinstance(info, dict):
            ex_map = info.get("ex_today")
        ex = (ex_map or {}).get(sid)
        # the ex-date has to be the bar's own day: an older ex-date is
        # already inside Close_Prev
        if ex is not None and (own or session) != ex.get("date"):
            ex = None
        up, down = limit_flags(row, sid, ex)
        if up:
            flags.add("limit_lock")
        if down:
            flags.add("limit_down")
    out = {"Trade_Restriction": kind_of(flags),
           "Restriction_Flags": ",".join(sorted(flags)) or None}
    out.update(detail)
    return out


def annotate_restrictions(df, info, session_date=None):
    """Add RESTRICTION_COLUMNS to a copy of `df`. Never raises: on an
    internal error every row is 'unknown'.

    `session_date` defaults to the newest Data_Date in the frame (the desktop
    scan passes none), then to the session the lists were fetched for -- a
    frame without dates must not read every disposition as over. `info` is
    fetch_restrictions' result; None (the fetch crashed) makes every row
    'unknown' except for what the row's own bar says (a limit lock is still
    a limit lock). The limit flags are read only off a row whose Data_Date
    is the session (or unknown)."""
    if df is None:
        return df
    out = df.copy()
    n = len(out)
    try:
        session = str(session_date or "")[:10] or session_of(out)
        if not session and isinstance(info, dict):
            session = str(info.get("session") or "")[:10]
        nxt = None
        if isinstance(info, dict) and info.get("session") == session:
            nxt = info.get("next_session")
        if not nxt and session:
            nxt = next_session_after(session)
        ex_map = info.get("ex_today") if isinstance(info, dict) else None
        if ex_map is None:
            ex_map = load_ex_today(session)
        vals = [restriction_of(r, info, session, nxt, ex_map)
                for r in out.to_dict("records")]
    except Exception:
        vals = [_unknown_row() for _ in range(n)]
    for col in RESTRICTION_COLUMNS:
        out[col] = pd.Series([v[col] for v in vals], index=out.index,
                             dtype=object)
    return out


def _flags(v):
    if v is None or (isinstance(v, float) and v != v):
        return set()
    return {f for f in str(v).split(",") if f.strip()}


def summarize(info, df=None, tracked=None):
    """meta.quality.restrictions: the feeds' state plus counts over the
    published rows. JSON-ready."""
    boards = {}
    src = (info or {}).get("boards") if isinstance(info, dict) else None
    for b in BOARDS:
        e = dict((src or {}).get(b) or _board_entry())
        if not isinstance(info, dict):
            e["error"] = "fetch_crashed"
        boards[b] = {k: e.get(k) for k in ("ok", "source", "rows",
                                           "last_modified", "error")}
        if e.get("primary_error"):
            boards[b]["primary_error"] = e.get("primary_error")
    out = {
        "ok": bool(isinstance(info, dict) and info.get("ok")),
        "fetched_at": info.get("fetched_at") if isinstance(info, dict) else None,
        "session": info.get("session") if isinstance(info, dict) else None,
        "next_session": info.get("next_session") if isinstance(info, dict) else None,
        "boards": boards,
        "attention_ok": dict((info or {}).get("attention_ok") or {b: False for b in BOARDS})
        if isinstance(info, dict) else {b: False for b in BOARDS},
        "altered_ok": dict((info or {}).get("altered_ok") or {b: False for b in BOARDS})
        if isinstance(info, dict) else {b: False for b in BOARDS},
        "blocking": list(BLOCKING_RESTRICTIONS),
        "active": 0, "blocked": 0, "unknown": 0, "counts": {},
    }
    ex = info.get("ex_today") if isinstance(info, dict) else None
    if isinstance(ex, dict):
        out["ex_today"] = {"date": info.get("session"), "ids": sorted(ex)}
    if isinstance(info, dict) and info.get("error"):
        out["error"] = info.get("error")

    def count(frame):
        c = {}
        if frame is None or getattr(frame, "empty", True) \
                or "Trade_Restriction" not in frame.columns:
            return c
        for k, v in frame["Trade_Restriction"].value_counts().items():
            if str(k) and str(k) != "none":
                c[str(k)] = int(v)
        return c

    if df is not None and not getattr(df, "empty", True):
        out["counts"] = count(df)
        if "Restriction_Flags" in df.columns:
            out["active"] = int(sum(1 for v in df["Restriction_Flags"]
                                    if "disposition" in _flags(v)))
        if "Buy_Block" in df.columns:
            out["blocked"] = int((df["Buy_Block"].astype(str) == "restricted").sum())
        out["unknown"] = int(out["counts"].get("unknown", 0))
    if tracked is not None and not getattr(tracked, "empty", True):
        out["tracked_counts"] = count(tracked)
    return out


def describe(summary):
    """One log line: '[restrict] OTC ok (openapi, 28) TSE ok (openapi, 12)
    attention 2/2 altered 2/2 -> 2 disposition, 0 blocked'."""
    s = summary or {}
    parts = []
    for b in BOARDS:
        e = (s.get("boards") or {}).get(b) or {}
        if e.get("ok"):
            parts.append("{} ok ({}, {})".format(b, e.get("source"), e.get("rows")))
        else:
            parts.append("{} FAILED ({})".format(b, e.get("error")))
    att = sum(1 for v in (s.get("attention_ok") or {}).values() if v)
    alt = sum(1 for v in (s.get("altered_ok") or {}).values() if v)
    return "[restrict] {} attention {}/2 altered {}/2 -> {} disposition, " \
           "{} blocked, {} unknown".format(" ".join(parts), att, alt,
                                           s.get("active", 0),
                                           s.get("blocked", 0),
                                           s.get("unknown", 0))


# --------------------------------------------------------------------------
# recording (fixtures) / manual probe
# --------------------------------------------------------------------------
def _record(session, out_dir):
    """Fetch every endpoint once for `session` and write the raw answers as
    tests/fixtures-style wrappers {url, params, status, last_modified,
    fetched_at, body}. Used to (re)record the test fixtures."""
    import os
    s_day = _day(session)
    eps = {
        "tpex_disposal_openapi": (TPEX_DISPOSAL_URL, None),
        "twse_punish_openapi": (TWSE_PUNISH_URL, None),
        "tpex_disposal_web": (TPEX_DISPOSAL_WEB_URL, {
            "startDate": s_day.strftime("%Y/%m/%d"),
            "endDate": (s_day + timedelta(days=TPEX_WEB_DAYS_AHEAD)).strftime("%Y/%m/%d"),
            "response": "json"}),
        "twse_punish_web": (TWSE_PUNISH_WEB_URL, {
            "response": "json",
            "startDate": (s_day - timedelta(days=TWSE_WEB_DAYS_BACK)).strftime("%Y%m%d"),
            "endDate": s_day.strftime("%Y%m%d")}),
        "tpex_warning_openapi": (TPEX_WARNING_URL, None),
        "twse_notice_web": (TWSE_NOTICE_WEB_URL, {
            "response": "json",
            "startDate": (s_day - timedelta(days=TWSE_NOTICE_DAYS_BACK)).strftime("%Y%m%d"),
            "endDate": s_day.strftime("%Y%m%d")}),
        "tpex_cmode_openapi": (TPEX_CMODE_URL, None),
        "twse_twt85u_openapi": (TWSE_ALTERED_URL, None),
        "twse_twtawu_openapi": (TWSE_HALT_URL, None),
    }
    os.makedirs(out_dir, exist_ok=True)
    for name, (url, params) in eps.items():
        try:
            r = requests.get(url, params=params, headers=_HEADERS,
                             timeout=FETCH_TIMEOUT)
            try:
                body = r.json()
            except Exception:
                body = None
            rec = {"url": url, "params": params, "status": r.status_code,
                   "last_modified": r.headers.get("Last-Modified"),
                   "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                   "body": body}
        except Exception as e:
            rec = {"url": url, "params": params, "status": None,
                   "error": str(e)[:200], "body": None}
        with open(os.path.join(out_dir, name + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
        print("  {} -> {} ({})".format(name, rec.get("status"),
                                       rec.get("last_modified")))
        time.sleep(1.5)


def main(argv=None):
    """python -m scanner.trade_restrictions [--session YYYY-MM-DD]
    [--record DIR]: print the parsed state (or record raw fixtures)."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m scanner.trade_restrictions")
    ap.add_argument("--session", default=date.today().isoformat())
    ap.add_argument("--record", default=None,
                    help="write the raw endpoint answers into this directory")
    a = ap.parse_args(argv)
    if a.record:
        _record(a.session, a.record)
        return 0
    info = fetch_restrictions(a.session)
    print(describe(summarize(info)))
    for sid in sorted(info["disposition"]):
        for p in info["disposition"][sid]:
            print("  {} {} {}..{} every {} min prepay {}".format(
                sid, p["board"], p["start"], p["end"], p["match_min"],
                p["prepay"]))
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
