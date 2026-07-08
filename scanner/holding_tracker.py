"""
Holding-day / exit-date tracker. ASCII only.

The trade plan is "enter next-day open after the signal, hold N trading days,
exit on the N-th bar's close". A user who does not open the scanner every day
cannot tell which day of the hold a given pick is on. This module answers that
per stock, anchored to reality rather than to how often the app is opened:

  * entry is anchored to the FIRST day the pick appeared in the current streak
    (from the signal ledger), not to today.
  * all day math is in TRADING days off price_volume.db's calendar, so skipped
    weekends/holidays/unopened days never miscount.

It adds these columns to the result DataFrame:
  Entry_Date     the open you would have bought (next trading day after signal)
  Exit_Date      the N-th trading bar's date (blank if still in the future)
  Hold_Day       which trading day of the hold today is (0 = not entered yet)
  Hold_Remaining trading days until the time exit (0 = today, <0 = overdue)
  Hold_Status    machine code for the UI: pending | holding | exit_today |
                 overdue  (UIs render their own localized text from this)
  Hold_Note      ASCII plain-language action for CSV review

Nothing here changes selection; it only annotates. Failures are swallowed so a
tracker problem can never break a scan.
"""
import sqlite3

import pandas as pd

from config.settings import PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE

# Time-exit horizon per mode (trading bars). Modes without a validated time
# exit are left out and simply get no holding annotation.
# prelaunch moved 5 -> 10 on 2026-07-07: the 214-day overlay sweep
# (eval_prelaunch_overlays.py) showed OTC + risk_on + top-20 + hold 10 lifts the
# win rate 53 -> 56pct in BOTH window halves and raises alpha to +5.5pp, and
# hold 10 also matches the hysteresis hold band (names stay listed ~6-11 days).
HOLD_BARS_BY_MODE = {
    "mode_prelaunch": 10,
}


def _trading_calendar():
    """Sorted distinct trading dates ('YYYY-MM-DD') from price_volume.db."""
    try:
        with sqlite3.connect(PRICE_VOLUME_FILE) as conn:
            rows = conn.execute("SELECT DISTINCT date FROM data").fetchall()
    except Exception:
        return []
    dates = sorted({str(r[0])[:10] for r in rows if r and r[0]})
    return dates


def _ledger_bar_dates(scan_mode):
    """{stock_id: sorted list of distinct signal bar_dates} for this mode."""
    if not SIGNAL_LEDGER_FILE.exists():
        return {}
    try:
        with sqlite3.connect(SIGNAL_LEDGER_FILE) as conn:
            rows = conn.execute(
                "SELECT stock_id, bar_date FROM picks WHERE scan_mode = ?",
                (scan_mode,),
            ).fetchall()
    except Exception:
        return {}
    out = {}
    for sid, bd in rows:
        out.setdefault(str(sid), set()).add(str(bd)[:10])
    return {sid: sorted(s) for sid, s in out.items()}


def _streak_start(bar_dates, idx_of, gap_tol):
    """Earliest bar_date of the current contiguous appearance streak. A gap of
    more than `gap_tol` trading days means the name left and came back, so only
    the latest block counts."""
    known = [d for d in bar_dates if d in idx_of]
    if not known:
        return None
    start = known[-1]
    for earlier in reversed(known[:-1]):
        if idx_of[start] - idx_of[earlier] <= gap_tol:
            start = earlier
        else:
            break
    return start


def annotate_holding(df, scan_mode):
    """Return df with Entry_Date/Exit_Date/Hold_Day/Hold_Note added (best effort).
    Modes without a validated time exit are returned unchanged."""
    if df is None or df.empty or "Stock_ID" not in df.columns:
        return df
    hold = HOLD_BARS_BY_MODE.get(scan_mode)
    if not hold:
        return df

    cal = _trading_calendar()
    if not cal:
        return df
    idx_of = {d: i for i, d in enumerate(cal)}
    last_idx = len(cal) - 1

    led = _ledger_bar_dates(scan_mode)
    df = df.copy()
    today = str(df["Data_Date"].iloc[0])[:10] if "Data_Date" in df.columns else cal[-1]

    entry_dates, exit_dates, hold_days = [], [], []
    remainings, statuses, notes = [], [], []
    for _, r in df.iterrows():
        sid = str(r.get("Stock_ID", "")).strip()
        # Union the ledger history with this pick's own signal day so a just-
        # written (or just-missed) row still anchors correctly.
        bar_dates = set(led.get(sid, []))
        bar_dates.add(str(r.get("Data_Date") or today)[:10])
        anchor = _streak_start(sorted(bar_dates), idx_of, gap_tol=hold)

        if anchor is None or anchor not in idx_of:
            entry_dates.append(""); exit_dates.append("")
            hold_days.append(None); remainings.append(None)
            statuses.append(""); notes.append("")
            continue

        entry_idx = idx_of[anchor] + 1          # buy the open AFTER the signal
        today_idx = idx_of.get(today, last_idx)
        exit_idx = entry_idx + hold - 1         # close of the N-th bar

        entry_dates.append(cal[entry_idx] if entry_idx <= last_idx else "")
        exit_dates.append(cal[exit_idx] if exit_idx <= last_idx else "")

        day_no = today_idx - entry_idx + 1      # trading days held incl. today
        remaining = exit_idx - today_idx        # 0 = exit today, <0 = overdue
        hold_days.append(max(day_no, 0))
        remainings.append(remaining)

        if day_no <= 0:
            statuses.append("pending")
            notes.append("next-open entry (signal {})".format(anchor))
        elif remaining > 0:
            statuses.append("holding")
            notes.append("held {}/{}, exit in {} trading day(s)".format(
                day_no, hold, remaining))
        elif remaining == 0:
            statuses.append("exit_today")
            notes.append("day {}, exit at today close".format(hold))
        else:
            statuses.append("overdue")
            notes.append("overdue: day {}, should be sold".format(day_no))

    df["Entry_Date"] = entry_dates
    df["Exit_Date"] = exit_dates
    df["Hold_Day"] = hold_days
    df["Hold_Remaining"] = remainings
    df["Hold_Total"] = hold          # N in "day X of N" (UIs render the label)
    df["Hold_Status"] = statuses
    df["Hold_Note"] = notes
    return df
