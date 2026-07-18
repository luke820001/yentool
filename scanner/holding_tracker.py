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

# Market-shock exit delay (validated 2026-07-08, eval_exit_delay.py): if the
# TAIEX is below its 20MA on the scheduled exit day, keep holding until it climbs
# back above 20MA, capped at this many bars. On the 214-day OTC replay this beat
# the fixed 10-bar exit on win AND mean AND both window halves (mean +3.98 ->
# +4.44) by not dumping a position into a brief market shock.
#
# 2026-07-18 CONDITIONAL refinement (sandbox_redteam2.py, 6y OOS): the delay
# must ALSO require TAIEX > 60MA (a pullback WITHIN an uptrend), not just
# < 20MA. On the full 6y window the unconditional delay is ~noise overall
# (50.3 vs 49.7 fixed), but on the subset whose exit lands while TAIEX < 60MA
# (a confirmed bear, n=196) it is actively WORSE than fixed (24.0 vs 26.5,
# mean -4.23 vs -4.16) -- holding high-beta OTC longer into a below-60MA
# breakdown. The conditional delay reverts to a plain 10-bar exit in that case
# (26.5, identical to fixed) and is unchanged in normal pullbacks, so it is
# strictly >= the old rule. See annotate_holding()'s `disturbed` gate.
EXIT_DELAY_CAP_BY_MODE = {
    "mode_prelaunch": 20,
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
    cap = EXIT_DELAY_CAP_BY_MODE.get(scan_mode, hold)   # >= hold; == hold disables

    # Is the market a PULLBACK-WITHIN-UPTREND today (TAIEX below 20MA but still
    # above 60MA)? Only then does the exit delay engage -- see
    # EXIT_DELAY_CAP_BY_MODE for why the 60MA condition matters (delaying into a
    # confirmed below-60MA bear is worse than a plain 10-bar exit). Best effort;
    # default not-disturbed. reg["risk_on"] == (TAIEX > 60MA).
    disturbed = False
    try:
        from scanner.market_regime import get_market_regime
        reg = get_market_regime()
        disturbed = (bool(reg.get("ok"))
                     and not reg.get("above20", True)
                     and bool(reg.get("risk_on", False)))
    except Exception:
        disturbed = False

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
        exit_idx = entry_idx + hold - 1         # close of the base N-th bar
        cap_idx = entry_idx + cap - 1           # hard latest exit bar

        entry_dates.append(cal[entry_idx] if entry_idx <= last_idx else "")
        exit_dates.append(cal[exit_idx] if exit_idx <= last_idx else "")

        day_no = today_idx - entry_idx + 1      # trading days held incl. today
        remaining = exit_idx - today_idx        # to base exit; 0 = today, <0 past
        hold_days.append(max(day_no, 0))
        remainings.append(remaining)

        if day_no <= 0:
            statuses.append("pending")
            notes.append("next-open entry (signal {})".format(anchor))
        elif remaining > 0:
            statuses.append("holding")
            notes.append("held {}/{}, exit in {} trading day(s)".format(
                day_no, hold, remaining))
        elif today_idx >= cap_idx:
            # reached the delay cap -> must exit regardless of the market
            statuses.append("exit_today" if today_idx == cap_idx else "overdue")
            notes.append("day {} (delay cap {}), exit now".format(day_no, cap))
        elif disturbed and cap > hold:
            # at/past base exit but the market is disturbed -> hold and watch
            statuses.append("delay")
            notes.append("day {}: TAIEX below 20MA, hold until it recovers "
                         "(cap day {})".format(day_no, cap))
        else:
            statuses.append("exit_today" if remaining == 0 else "overdue")
            notes.append("day {}, exit at close".format(day_no))

    df["Entry_Date"] = entry_dates
    df["Exit_Date"] = exit_dates
    df["Hold_Day"] = hold_days
    df["Hold_Remaining"] = remainings
    df["Hold_Total"] = hold          # base N in "day X of N"
    df["Hold_Cap"] = cap             # latest exit bar when delayed
    df["Hold_Status"] = statuses
    df["Hold_Note"] = notes
    return df
