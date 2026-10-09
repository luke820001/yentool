"""
One place that decides which stored index bars are real.

The TAIEX / TPEX tables in taiex.db are written from yfinance and FinMind with
no session check. The 2026-10-09 conformance audit found a Sunday bar
(2026-09-20) that fed the regime moving averages and was published in the
phone's benchmark chart, and showed that a bar with a corrupt close (zero, or
a 10x print) was accepted by every reader. Four readers each loaded the table
their own way (market_regime, market_leg, live_record, regime_report), so the
fix lives here and each of them calls it.

What is dropped, in this order:
  * a close that is not a finite positive number;
  * a date that cannot be parsed, or that is not a trading session (weekend or
    a TWSE closure the committed calendar knows about; a year the calendar does
    not know is judged by weekday alone, never by a network call);
  * a bar that sits more than MAX_DAILY_MOVE away from EVERY neighbouring
    session that can judge it (previous and next). The exchange limit is 10
    percent, so a larger one-session step is a bad print, not a market. Judging
    against both sides means a bad bar cannot take the good bars after it down
    with it, and the first and last bar (one neighbour) are still checked.
    Across a gap of more than a few calendar days a step is not judged (an
    outage can hide a real multi-day move).

A bar this cannot recognise as bad (a 3 percent mis-print) still gets through;
only a second source could catch that. Pure functions, no I/O. ASCII only.
"""
import datetime as _dt
import math

import pandas as pd

MAX_DAILY_MOVE = 0.11          # exchange limit is 10 percent; margin for rounding
ADJACENT_DAYS = 5              # calendar days: a step across more is not judged


def _is_session(day):
    """True/False for a date string; None when the calendar cannot say."""
    try:
        from scanner.market_calendar import is_session
        ok, _src = is_session(day, fetch=False)
        return ok
    except Exception:
        d = _dt.date.fromisoformat(day)
        return d.weekday() < 5


def clean_closes(table, date_col="date", close_col="close"):
    """Return (clean, dropped).

    `clean` is a DataFrame with `date_col` (YYYY-MM-DD strings) and `close_col`
    (float), ascending, one row per date. `dropped` is {reason: count}.
    Never raises: anything unreadable yields an empty frame.
    """
    dropped = {"bad_close": 0, "bad_date": 0, "non_session": 0, "jump": 0}
    empty = pd.DataFrame({date_col: pd.Series(dtype=str),
                          close_col: pd.Series(dtype=float)})
    try:
        if table is None or len(table) == 0:
            return empty, dropped
        t = table[[date_col, close_col]].copy()
        t[date_col] = t[date_col].astype(str).str.slice(0, 10)
        t[close_col] = pd.to_numeric(t[close_col], errors="coerce")
        t = t.sort_values(date_col, kind="mergesort")
        t = t.drop_duplicates(date_col, keep="last")
        rows = []                       # (date string, date, close) that pass the basics
        for d, c in zip(t[date_col], t[close_col]):
            if c is None or not math.isfinite(float(c)) or float(c) <= 0:
                dropped["bad_close"] += 1
                continue
            try:
                day = _dt.date.fromisoformat(d)
            except ValueError:
                dropped["bad_date"] += 1
                continue
            if _is_session(d) is False:
                dropped["non_session"] += 1
                continue
            rows.append((d, day, float(c)))

        def _steps(i):
            """Relative steps from the neighbours of rows[i] that are
            adjacent sessions (previous and next)."""
            out_steps = []
            for j in (i - 1, i + 1):
                if 0 <= j < len(rows):
                    gap = abs((rows[i][1] - rows[j][1]).days)
                    if gap <= ADJACENT_DAYS:
                        out_steps.append(abs(rows[i][2] / rows[j][2] - 1))
            return out_steps

        keep_dates, keep_closes = [], []
        for i, (d, _day, c) in enumerate(rows):
            steps = _steps(i)
            # a spike: every neighbour that can judge it says the step is
            # beyond the exchange limit. One neighbour (the first or the last
            # bar) is enough; a bar whose neighbours disagree is kept.
            if steps and all(x > MAX_DAILY_MOVE for x in steps):
                dropped["jump"] += 1
                continue
            keep_dates.append(d)
            keep_closes.append(c)
        out = pd.DataFrame({date_col: keep_dates, close_col: keep_closes})
        return out.reset_index(drop=True), dropped
    except Exception:
        return empty, dropped
