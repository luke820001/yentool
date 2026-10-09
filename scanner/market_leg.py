"""
The MARKET leg of the ride past the time exit, per date. ASCII only.

The shipped exit rule (docs/STRATEGY.md 3.5) holds 10 bars and, at the day-10
close, rides on while EITHER the stock closes above its own 5-bar mean OR the
market is in a pullback inside an uptrend -- TAIEX below its 20-day mean but
still above its 60-day mean -- on THAT day, capped at 20 bars.

Until 2026-10-08 the market half lived only in holding_tracker and was read
off TODAY's regime: a trade whose day-10 bar fell weeks ago was judged by the
market of the day the scan ran. That is a different rule from the one the
backtest validated, and it made the tracker disagree with live_record (which
had no market leg at all) on the same trade. This module answers the question
per date, from the TAIEX history in TAIEX_FILE, so every replay of the rule
-- tracker, recommendation lifecycle, live_record -- passes the same
callback to scanner.exit_rules.replay_exit(extend_if=...).

Disturbed on date d <=> close(d) < MA20(d) and close(d) > MA60(d), both
means over the TAIEX closes up to and including d. A date with no TAIEX bar,
or without 60 bars of history behind it, is NOT disturbed: an unknown market
never extends a trade (the cloud's 2026-10-07 run had no TAIEX bar at all).

One loader, cached per process and keyed on the file's size and mtime, so a
scan that refreshes the index mid-run is re-read rather than served stale.
Failures return an empty table (= never disturbed); this only annotates.
"""
import os

import pandas as pd

import config.settings as _settings
from scanner.index_clean import clean_closes

_CACHE = {}


def _taiex_file(taiex_file=None):
    return taiex_file if taiex_file is not None else _settings.TAIEX_FILE


def _key(path):
    try:
        st = os.stat(str(path))
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _load(path):
    """date/close of the TAIEX sheet. The handle is closed explicitly: the
    sqlite3 context manager only commits, and an open handle keeps the file
    locked on Windows."""
    import sqlite3
    if not os.path.exists(str(path)):
        return pd.DataFrame()
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT date, close FROM TAIEX").fetchall()
    finally:
        conn.close()
    return pd.DataFrame(rows, columns=["date", "close"])


def disturbed_by_date(taiex_file=None):
    """{'YYYY-MM-DD': bool} for every stored TAIEX date (see the docstring).
    Dates absent from the map are not disturbed."""
    path = _taiex_file(taiex_file)
    key = _key(path)
    hit = _CACHE.get(key[0])
    if hit is not None and hit[0] == key:
        return hit[1]
    table = {}
    try:
        t = _load(path)
        if not t.empty:
            # real sessions with plausible closes only (scanner/index_clean):
            # a weekend or corrupt bar would shift both averages
            t, _dropped = clean_closes(t)
            c = t["close"]
            ma20 = c.rolling(20).mean()
            ma60 = c.rolling(60).mean()
            # NaN means are compared False, so short history is "not disturbed"
            flag = (c < ma20) & (c > ma60)
            table = {d: bool(v) for d, v in zip(t["date"], flag)}
    except Exception:
        table = {}
    _CACHE[key[0]] = (key, table)
    return table


def is_disturbed(date, taiex_file=None):
    """True when TAIEX on `date` was below its 20MA and above its 60MA."""
    if not date:
        return False
    return bool(disturbed_by_date(taiex_file).get(str(date)[:10], False))


def make_disturbed_fn(taiex_file=None):
    """The extend_if callback for scanner.exit_rules.replay_exit:
    fn(bar_index, date) -> bool. The table is loaded once, here."""
    table = disturbed_by_date(taiex_file)

    def disturbed(i, date):
        if not date:
            return False
        return bool(table.get(str(date)[:10], False))

    disturbed.table = table
    return disturbed


def clear_cache():
    _CACHE.clear()
