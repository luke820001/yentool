"""
Future Taiwan trading sessions, from the TWSE holiday schedule. ASCII only.

Why this exists (2026-10-08). A recommendation is entered at the NEXT
session's open, so its entry deadline (valid_until_session) is a future date
and the price store cannot supply it: the store only knows sessions that have
already traded. Weekday arithmetic gets it wrong around every holiday --
next_session('2026-10-08') is 2026-10-12, not Friday 10-09, which is the
National Day bridge holiday.

Source: GET HOLIDAY_URL (TWSE rwd holidaySchedule). Each row is
[date, name, description]. A listed date is CLOSED unless its name marks the
first or last trading day around a holiday ("start of trading" / "last
trading day", OPEN_MARKERS); the settlement-only days before Lunar New Year
(02-12 / 02-13 in 2026) are closed. Weekends are always closed. TWSE answers
a year it has not published yet with the CURRENT year's table, so a reply
whose rows are not in the requested year counts as "not available".

Lookup order per year: the in-process cache, the committed list
HOLIDAY_FILE (config/twse_holidays.json, refreshed with
`python -m scanner.market_calendar --refresh YEAR`), then the network. When
none of them knows the year, next_session falls back to the next weekday and
says so (source 'weekday'); the recommendation lifecycle repairs a wrong
guess once the price store shows the real next session
(portfolio.sync.advance_recommendations, 'market_closed'). Typhoon closures
are never in the schedule; they are handled the same way.

Nothing here reads the wall clock: every answer is relative to the session it
is asked about.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

HOLIDAY_URL = ("https://www.twse.com.tw/rwd/zh/holidaySchedule/"
               "holidaySchedule?response=json&queryYear={}")
# u"\u958b\u59cb\u4ea4\u6613" = start of trading, u"\u6700\u5f8c\u4ea4\u6613"
# = last trading day: listed, but sessions.
OPEN_MARKERS = (u"\u958b\u59cb\u4ea4\u6613", u"\u6700\u5f8c\u4ea4\u6613")
HOLIDAY_FILE = Path(__file__).resolve().parent.parent / "config" / "twse_holidays.json"
FETCH_TIMEOUT = 10

_CACHE = {}     # year -> frozenset of closed 'YYYY-MM-DD', or None (unknown)


def clear_cache():
    _CACHE.clear()


def _day(value):
    s = str(value or "")[:10]
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def parse_schedule(payload, year=None):
    """The closed dates in a TWSE holidaySchedule reply, as a set of
    'YYYY-MM-DD'; None when the reply is unusable or (with `year`) holds no
    row of that year."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("stat") or "").lower() != "ok":
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    prefix = "{}-".format(int(year)) if year is not None else ""
    closed, seen = set(), False
    for row in rows:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        d = str(row[0] or "")[:10]
        if len(d) != 10 or d[4] != "-" or d[7] != "-":
            continue
        if prefix and not d.startswith(prefix):
            continue
        seen = True
        name = str(row[1]) if len(row) > 1 and row[1] is not None else ""
        if any(m in name for m in OPEN_MARKERS):
            continue
        closed.add(d)
    if prefix and not seen:
        return None
    return closed


def _fetch_year(year):
    """The raw TWSE reply for `year` (network; TLS verified)."""
    import requests
    r = requests.get(HOLIDAY_URL.format(int(year)), timeout=FETCH_TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _committed(year):
    """Closed dates for `year` from HOLIDAY_FILE, or None."""
    try:
        with open(HOLIDAY_FILE, encoding="utf-8") as f:
            got = (json.load(f).get("years") or {}).get(str(int(year)))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(got, dict) or not isinstance(got.get("closed"), list):
        return None
    return {str(d)[:10] for d in got["closed"]}


def closed_dates(year, fetch=True):
    """frozenset of closed weekdays-or-not dates for `year` (holidays as
    listed by TWSE), or None when no source knows the year."""
    year = int(year)
    if year in _CACHE:
        return _CACHE[year]
    got = _committed(year)
    if got is None and fetch:
        try:
            got = parse_schedule(_fetch_year(year), year)
        except Exception:
            got = None
    if got is None and not fetch:
        # "not known without asking" is not "unknown": do not pin None, or a
        # later fetch=True call for the same year would never ask
        return None
    _CACHE[year] = frozenset(got) if got is not None else None
    return _CACHE[year]


def is_session(day, fetch=True):
    """(True|False|None, source) -- None when the year is unknown."""
    d = _day(day)
    if d.weekday() >= 5:
        return False, "weekday"
    closed = closed_dates(d.year, fetch=fetch)
    if closed is None:
        return None, "weekday"
    return d.isoformat() not in closed, "twse"


def next_session(day, fetch=True):
    """(the first trading session after `day`, source).

    source is 'twse' when the holiday schedule decided it and 'weekday' when
    the schedule for that year was unavailable and the next weekday was
    taken instead (a guess; see the module docstring)."""
    d = _day(day)
    for _ in range(60):
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        closed = closed_dates(d.year, fetch=fetch)
        if closed is None:
            return d.isoformat(), "weekday"
        if d.isoformat() in closed:
            continue
        return d.isoformat(), "twse"
    return d.isoformat(), "weekday"      # 60 days of closures: not a market


def entry_session_after(day, calendar=None, fetch=True):
    """The session a signal on `day` is entered on: the first PRICE-CALENDAR
    session after it when the store already has one (history), else
    next_session(day) (the future). `calendar` is the sorted list of traded
    sessions (holding_tracker._trading_calendar()); None reads it."""
    d = str(day or "")[:10]
    if not d:
        return None
    if calendar is None:
        try:
            from scanner.holding_tracker import _trading_calendar
            calendar = _trading_calendar()
        except Exception:
            calendar = []
    import bisect
    cal = list(calendar or [])
    i = bisect.bisect_right(cal, d)
    if i < len(cal):
        return cal[i]
    try:
        return next_session(d, fetch=fetch)[0]
    except Exception:
        return None


def refresh(years, path=None):
    """Fetch `years` from TWSE and store them in the committed list. Returns
    {year: count of closed dates | None}. Run by hand once TWSE publishes a
    new year (usually in the last quarter of the year before)."""
    path = Path(path) if path else HOLIDAY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("source", HOLIDAY_URL.format("YYYY"))
    store = doc.setdefault("years", {})
    out = {}
    for y in years:
        try:
            got = parse_schedule(_fetch_year(int(y)), int(y))
        except Exception:
            got = None
        out[int(y)] = None if got is None else len(got)
        if got is not None:
            store[str(int(y))] = {"fetched": date.today().isoformat(),
                                  "closed": sorted(got)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=True, indent=1, sort_keys=True)
        f.write("\n")
    clear_cache()
    return out


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--refresh":
        print(refresh(sys.argv[2:]))
    else:
        print("usage: python -m scanner.market_calendar --refresh YEAR [YEAR...]")
