"""
Tests for the exit-plan replay (scanner/exit_rules.replay_exit) and the
holding tracker's Plan_Stop / Exit_Signal columns built on it.

replay_exit must agree with simulate_exit bar for bar (simulate_exit now
delegates to it), and the live annotation must say the same thing the ledger
would score. Stdlib unittest, no network; the tracker test uses a temporary
price database.

    python -m unittest tests.test_exit_plan -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from scanner.exit_rules import replay_exit, simulate_exit, DEFAULT_RULE
import scanner.holding_tracker as tracker


# Levels the rule puts on a 100.00 entry, so the fixtures follow the constants
# instead of restating them (the 2026-09-20 stop change broke every literal).
STOP = round(100.0 * (1 - DEFAULT_RULE["stop_pct"]), 2)
ARM = round(100.0 * (1 + DEFAULT_RULE["arm_pct"]), 2)
LOCK = round(100.0 * (1 + DEFAULT_RULE["lock_pct"]), 2)
TARGET = round(100.0 * (1 + DEFAULT_RULE["tp_pct"]), 2)
ADD = round(100.0 * (1 - tracker.ADD_PCT), 2)


def bars(*rows):
    """rows of (open, high, low, close) -> four lists."""
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


class Replay(unittest.TestCase):
    def test_open_position_reports_live_stop(self):
        # both closes stay below the arm price, so nothing is armed
        o, h, l, c = bars((100, 103, 99, ARM - 0.5), (102, 104, 100, ARM - 0.5))
        p = replay_exit(o, h, l, c, dates=["d1", "d2"], hold_bars=None)
        self.assertEqual(p["reason"], "")
        self.assertFalse(p["exited"])
        self.assertFalse(p["armed"])
        self.assertAlmostEqual(p["stop"], STOP)
        self.assertAlmostEqual(p["arm_px"], ARM)
        self.assertAlmostEqual(p["target"], TARGET)

    def test_stop_hit_books_the_stop_level(self):
        o, h, l, c = bars((100, 101, 98, 99), (99, 100, STOP - 1, STOP))
        p = replay_exit(o, h, l, c, dates=["d1", "d2"], hold_bars=None)
        self.assertEqual(p["reason"], "stop")
        self.assertEqual(p["bar"], 1)
        self.assertEqual(p["date"], "d2")
        self.assertAlmostEqual(p["exit_price"], STOP)
        self.assertAlmostEqual(p["ret_pct"], -DEFAULT_RULE["stop_pct"] * 100)

    def test_gap_through_stop_books_the_open(self):
        o, h, l, c = bars((100, 101, 98, 99), (80, 82, 78, 81))
        p = replay_exit(o, h, l, c, hold_bars=None)
        self.assertEqual(p["reason"], "stop")
        self.assertAlmostEqual(p["exit_price"], 80.0)

    def test_arming_raises_the_stop_to_the_lock(self):
        # a close at or above the arm price arms; the stop reported is the
        # level to place for the NEXT session
        o, h, l, c = bars((100, 101, 99, 100), (103, ARM + 2, 102.5, ARM))
        p = replay_exit(o, h, l, c, hold_bars=None)
        self.assertEqual(p["reason"], "")
        self.assertTrue(p["armed"])
        self.assertAlmostEqual(p["stop"], LOCK)

    def test_arming_is_read_from_the_close_not_the_high(self):
        """A bar that spikes through the arm price but closes below it has not
        armed: the evening payload and the phone only ever see the close."""
        o, h, l, c = bars((100, 101, 99, 100), (100, ARM + 5, 99, ARM - 0.5))
        p = replay_exit(o, h, l, c, hold_bars=None)
        self.assertFalse(p["armed"])
        self.assertAlmostEqual(p["stop"], STOP)

    def test_the_arming_bar_itself_cannot_be_stopped_on_the_lock(self):
        """The raised stop is an order placed for the NEXT session, so a bar
        that arms at its close after dipping below the lock earlier the same
        day is not an exit. This is the defect the 2026-09-21 change removed:
        the old engine booked a lock exit nobody could have placed."""
        o, h, l, c = bars((100, 101, 99, 100), (103, ARM + 2, LOCK - 1, ARM))
        p = replay_exit(o, h, l, c, dates=["d1", "d2"], hold_bars=None)
        self.assertEqual(p["reason"], "")
        self.assertFalse(p["exited"])
        self.assertTrue(p["armed"])

    def test_armed_then_lock_hit_is_a_lock_exit(self):
        o, h, l, c = bars((100, 101, 99, 100), (103, ARM + 2, 102.5, ARM),
                          (ARM, ARM, LOCK - 1, LOCK - 0.5))
        p = replay_exit(o, h, l, c, dates=["d1", "d2", "d3"], hold_bars=None)
        self.assertEqual(p["reason"], "lock")
        self.assertEqual(p["date"], "d3")
        self.assertAlmostEqual(p["exit_price"], LOCK)

    def test_target_hit(self):
        o, h, l, c = bars((100, 101, 99, 100), (110, 121, 108, 119))
        p = replay_exit(o, h, l, c, hold_bars=None)
        self.assertEqual(p["reason"], "tp")
        self.assertAlmostEqual(p["exit_price"], 120.0)

    def test_bad_entry_is_na(self):
        self.assertEqual(replay_exit([None], [1], [1], [1], hold_bars=None)["reason"], "na")
        self.assertEqual(replay_exit([], [], [], [], hold_bars=None)["reason"], "na")

    def test_simulate_exit_agrees_with_replay(self):
        cases = [
            bars(*[(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(12)]),
            bars((100, 101, 98, 99), (99, 100, STOP - 1, STOP),
                 *[(STOP, STOP + 1, STOP - 1, STOP)] * 10),
            bars((100, 101, 99, 100), (103, ARM + 2, 102.5, ARM),
                 (ARM, ARM, LOCK - 1, LOCK - 0.5), *[(101, 102, 100, 101)] * 9),
            bars((100, 101, 99, 100), (101, ARM + 2, 100, ARM),
                 *[(101, 102, 100, 101)] * 10),
            bars((100, 101, 99, 100), (110, TARGET + 1, 108, TARGET - 1),
                 *[(119, 120, 118, 119)] * 10),
        ]
        for o, h, l, c in cases:
            entry, ret, reason = simulate_exit(o, h, l, c)
            p = replay_exit(o, h, l, c, hold_bars=DEFAULT_RULE["hold_bars"])
            self.assertEqual(reason, p["reason"])
            self.assertAlmostEqual(entry, p["entry"])
            self.assertAlmostEqual(ret, p["ret_pct"])

    def test_simulate_exit_time_exit_unchanged(self):
        # Drifting DOWN: never arms, never in profit late, so the hold runs to
        # the time exit. (A fixture that drifts up now books a "late" exit --
        # see LateProfit below, which is the rule working, not a regression.)
        o, h, l, c = bars(*[(100, 101, 99, 100 - i * 0.1) for i in range(10)])
        entry, ret, reason = simulate_exit(o, h, l, c)
        self.assertEqual(reason, "time")
        self.assertAlmostEqual(ret, (c[9] / 100.0 - 1) * 100)


class LateProfit(unittest.TestCase):
    """From DEFAULT_RULE['late_from'] onward, a close at or above the fill
    sells at the NEXT open instead of carrying the profit into the last day.
    Adopted 2026-09-21: the time exit was closing 28% of trades at -6.9%."""

    LATE = DEFAULT_RULE["late_from"]
    # a close that counts as a real profit after costs
    IN_PROFIT = round(100.0 * (1 + DEFAULT_RULE["late_gain"]) + 0.5, 2)

    def test_in_profit_late_sells_at_the_next_open(self):
        # flat at the fill until day 8 closes a shade above it
        rows = [(100, 101, 99, 99.5)] * (self.LATE - 1) +                [(100, 102, 99, self.IN_PROFIT), (101, 102, 100, 101)]
        o, h, l, c = bars(*rows)
        p = replay_exit(o, h, l, c, hold_bars=DEFAULT_RULE["hold_bars"])
        self.assertEqual(p["reason"], "late")
        self.assertEqual(p["bar"], self.LATE)          # the NEXT bar
        self.assertAlmostEqual(p["exit_price"], 101.0)  # its OPEN

    def test_a_losing_position_is_left_alone(self):
        rows = [(100, 101, 99, 99.0)] * 10
        o, h, l, c = bars(*rows)
        p = replay_exit(o, h, l, c, hold_bars=DEFAULT_RULE["hold_bars"])
        self.assertEqual(p["reason"], "time")

    def test_an_early_profit_does_not_trigger_it(self):
        rows = [(100, 102, 99, self.IN_PROFIT)] * 3 + [(100, 101, 99, 99.0)] * 7
        o, h, l, c = bars(*rows)
        p = replay_exit(o, h, l, c, hold_bars=DEFAULT_RULE["hold_bars"])
        self.assertNotEqual(p["reason"], "late")

    def test_an_open_position_reports_that_it_is_due(self):
        rows = [(100, 101, 99, 99.5)] * (self.LATE - 1) +                [(100, 102, 99, self.IN_PROFIT)]
        o, h, l, c = bars(*rows)
        p = replay_exit(o, h, l, c, hold_bars=None)
        self.assertFalse(p["exited"])
        self.assertTrue(p["late_due"])

    def test_the_stop_still_wins_on_the_same_bar(self):
        """The overnight late order is a market sell at the open, but a bar
        that GAPS below the stop is a stop at the open, not a late exit -- the
        stop is the price you would actually get."""
        rows = [(100, 101, 99, 99.5)] * (self.LATE - 1) +                [(100, 102, 99, self.IN_PROFIT),
                (STOP - 2, STOP - 1, STOP - 3, STOP - 2)]
        o, h, l, c = bars(*rows)
        p = replay_exit(o, h, l, c, hold_bars=DEFAULT_RULE["hold_bars"])
        self.assertEqual(p["reason"], "late")
        self.assertAlmostEqual(p["exit_price"], STOP - 2)


class _TrackerHarness:
    """Drive annotate_holding against a tiny price store.

    `series` maps a stock id to bars (open, high, low, close) on CAL in
    order; a None entry leaves that session without a bar for the stock. The
    ledger's buy_ready flags ({sid: {date: 1|0|None}}) and the market leg's
    disturbed dates are injected, so nothing reads the real ledger or index.
    """

    CAL = ["2026-09-%02d" % d for d in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14)]

    def _store(self, tmp, series):
        db = Path(tmp) / "pv.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, open REAL, "
                         "high REAL, low REAL, close REAL)")
            for sid, rows in series.items():
                for d, row in zip(self.CAL, rows):
                    if row is None:
                        continue
                    o, h, l, c = row
                    conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?)",
                                 (sid, d, o, h, l, c))
            conn.commit()
        finally:
            conn.close()
        return db

    def _run(self, series, ledger_dates, today="2026-09-14", flags=None,
             rec_anchors=None, add_own_bar=True, disturbed=()):
        def close_on(rows):
            row = rows[self.CAL.index(today)] if len(rows) > self.CAL.index(today) else None
            return row[3] if row is not None else 100.0
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store(tmp, series)
            df = pd.DataFrame([{"Stock_ID": sid, "Data_Date": today,
                                "Close_Price": close_on(rows),
                                "Strict_Stop_Loss": round(
                                    close_on(rows) * (1 - tracker.STOP_PCT), 2),
                                "Add_Price": round(
                                    close_on(rows) * (1 - tracker.ADD_PCT), 2)}
                               for sid, rows in series.items()])
            marks = set(disturbed)

            def make_fn():
                return lambda i, d: d in marks
            with mock.patch.object(tracker, "PRICE_VOLUME_FILE", db), \
                    mock.patch.object(tracker, "_ledger_bar_dates",
                                      lambda mode: ledger_dates), \
                    mock.patch.object(tracker, "_ledger_buy_flags",
                                      lambda mode: flags or {}), \
                    mock.patch.object(tracker, "_disturbed_fn", make_fn):
                return tracker.annotate_holding(df, "mode_prelaunch",
                                                rec_anchors=rec_anchors,
                                                add_own_bar=add_own_bar)


class TrackerColumns(_TrackerHarness, unittest.TestCase):

    def test_stop_hit_is_reported_with_date_and_price(self):
        flat = [(100, 101, 99, 100)] * 10
        crash = list(flat)
        crash[5] = (100, 100, STOP - 5, STOP - 3)   # 09-08: low through the stop
        crash[6:] = [(STOP - 3, STOP - 2, STOP - 4, STOP - 3)] * 4
        out = self._run({"1111": flat, "2222": crash},
                        {"1111": ["2026-09-01"], "2222": ["2026-09-01"]})
        by = out.set_index("Stock_ID")
        # A booked price exit ENDS the hold. Until 2026-09-21 the calendar
        # kept counting alongside it, so the same row said "held 6/10, exit in
        # 4 trading day(s)" next to "stop exit booked 2026-09-08" -- 18 of the
        # 101 published rows that day carried the contradiction.
        self.assertEqual(by.loc["2222", "Hold_Status"], "exited")
        self.assertEqual(by.loc["2222", "Hold_Remaining"], 0)
        self.assertIn("closed by the stop", by.loc["2222", "Hold_Note"])
        self.assertEqual(by.loc["2222", "Exit_Signal"], "stop")
        self.assertEqual(by.loc["2222", "Exit_Signal_Date"], "2026-09-08")
        self.assertAlmostEqual(by.loc["2222", "Exit_Signal_Price"], STOP)
        self.assertAlmostEqual(by.loc["2222", "Plan_Stop"], STOP)
        self.assertIn("stop exit booked", by.loc["2222", "Exit_Note"])
        # the untouched name is simply holding with its stop shown
        self.assertEqual(by.loc["1111", "Exit_Signal"], "")
        self.assertAlmostEqual(by.loc["1111", "Plan_Stop"], STOP)
        self.assertFalse(by.loc["1111", "Plan_Armed"])
        self.assertIn("sell if it trades below %s" % STOP, by.loc["1111", "Exit_Note"])

    def test_armed_row_shows_raised_stop(self):
        # anchored late in the calendar so the position is only a few days old:
        # past day 8 an armed position is in profit, and the late profit-take
        # (correctly) sells it at the next open instead.
        # Anchored at CAL[5] so the fill is CAL[6] and today is only day 4:
        # past day 8 an armed position is in profit and the late profit-take
        # (correctly) sells it at the next open instead of just showing a stop.
        rows = [(100, 101, 99, 100)] * 6 +                [(100, ARM + 2, 99.5, ARM)] + [(ARM, ARM + 1, LOCK + 0.5, ARM)] * 3
        out = self._run({"3333": rows}, {"3333": [self.CAL[5]]})
        r = out.iloc[0]
        self.assertTrue(r["Plan_Armed"])
        self.assertAlmostEqual(r["Plan_Stop"], LOCK)
        self.assertEqual(r["Exit_Signal"], "")
        self.assertIn("lock armed", r["Exit_Note"])

    def test_pending_row_carries_reference_stop(self):
        rows = [(100, 101, 99, 100)] * 10
        out = self._run({"4444": rows}, {"4444": []})
        r = out.iloc[0]
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertAlmostEqual(r["Plan_Stop"], STOP)
        self.assertAlmostEqual(r["Plan_Add_Price"], ADD)
        self.assertEqual(r["Add_Hit_Date"], "")
        self.assertIsNone(r["Exit_Signal_Price"])

    def test_staged_add_level_and_first_touch(self):
        """The add level is fill x (1 - ADD_PCT) and Add_Hit_Date is the FIRST
        session it traded there while the position was open."""
        touch = list([(100, 101, 99, 100)] * 10)
        touch[4] = (100, 100, ADD - 1, ADD)          # 09-07
        touch[7] = (100, 100, ADD - 2, ADD)          # later touch, must not win
        out = self._run({"6666": touch, "7777": [(100, 101, 99, 100)] * 10},
                        {"6666": ["2026-09-01"], "7777": ["2026-09-01"]})
        by = out.set_index("Stock_ID")
        self.assertAlmostEqual(by.loc["6666", "Plan_Add_Price"], ADD)
        self.assertEqual(by.loc["6666", "Add_Hit_Date"], "2026-09-07")
        # never traded there -> no date, and the level is still published
        self.assertAlmostEqual(by.loc["7777", "Plan_Add_Price"], ADD)
        self.assertEqual(by.loc["7777", "Add_Hit_Date"], "")

    def test_add_after_the_stop_is_not_counted(self):
        """A bar that stops the trade out also passes the add level on its way
        down, but the position is gone: only a stop bar may book an add, and
        anything after the exit must not."""
        rows = list([(100, 101, 99, 100)] * 10)
        rows[3] = (100, 100, STOP - 5, STOP - 4)     # stop bar, passes the add
        rows[4:] = [(STOP - 4, ADD + 1, STOP - 6, ADD)] * 6
        out = self._run({"8888": rows}, {"8888": ["2026-09-01"]})
        r = out.iloc[0]
        self.assertEqual(r["Exit_Signal"], "stop")
        self.assertEqual(r["Add_Hit_Date"], "2026-09-04")

    def test_time_exit_when_the_hold_is_over(self):
        rows = [(100, 101, 99, 100)] * 10
        cal = self.CAL
        # signal 09-01, entry 09-02 (idx 1), base exit idx 10 -> beyond; use a
        # short calendar by anchoring earlier: signal on cal[0], hold 10 means
        # exit at idx 10 which is past the end, so shrink via cap/hold patch.
        # A continuously listed name has a ledger row for every session, which
        # is what keeps the streak anchored to its first day.
        with mock.patch.dict(tracker.HOLD_BARS_BY_MODE, {"mode_prelaunch": 5}), \
                mock.patch.dict(tracker.EXIT_DELAY_CAP_BY_MODE, {"mode_prelaunch": 5}):
            out = self._run({"5555": rows}, {"5555": list(cal)})
        r = out.iloc[0]
        # 2026-10-08: the canonical replay booked the time exit on cal[5],
        # before today, so the trade is closed -- "overdue" now means past
        # the cap with no exit in the store (missing bars), not this.
        self.assertEqual(r["Hold_Status"], "exited")
        self.assertEqual(r["Hold_Day"], 5)
        self.assertEqual(r["Hold_Remaining"], 0)
        self.assertEqual(r["Exit_Signal"], "time")
        self.assertEqual(r["Exit_Signal_Date"], cal[5])
        self.assertAlmostEqual(r["Exit_Signal_Price"], 100.0)


if __name__ == "__main__":
    unittest.main()


class RidePastTheTimeExit(unittest.TestCase):
    """DEFAULT_RULE["ride_cap"] (added to the engine 2026-09-23). At the time
    exit's close and every close after it, the position is kept while the
    close is above its own 5-bar mean, at most until the cap. Verified
    bar-for-bar against archive/research/sandbox_daily_plan.run_plan on all
    556 CORE+ trades (0 disagreements) the day it was added."""

    HOLD = DEFAULT_RULE["hold_bars"]

    @staticmethod
    def rising(n, step=0.05, start=100.0):
        # closes creep up but stay below the arm price and the late-profit
        # line, so neither the lock nor the late take-profit can fire
        rows = []
        for i in range(n):
            c = round(start + step * (i + 1), 2)
            rows.append((c - 0.02, c + 0.03, c - 0.05, c))
        return bars(*rows)

    def test_the_ride_extends_the_hold_to_the_cap(self):
        cap = self.HOLD + 2
        o, h, l, c = self.rising(cap + 3)
        p = replay_exit(o, h, l, c, hold_bars=self.HOLD, ride_cap=cap)
        self.assertEqual(p["reason"], "time")
        self.assertEqual(p["bar"], cap - 1)
        self.assertAlmostEqual(p["exit_price"], c[cap - 1])

    def test_a_close_under_its_5_bar_mean_ends_the_ride(self):
        o, h, l, c = self.rising(self.HOLD + 5)
        # the time exit's close falls below the mean of the last five closes
        c[self.HOLD - 1] = round(c[self.HOLD - 5] - 0.1, 2)
        p = replay_exit(o, h, l, c, hold_bars=self.HOLD, ride_cap=20)
        self.assertEqual(p["reason"], "time")
        self.assertEqual(p["bar"], self.HOLD - 1)
        self.assertAlmostEqual(p["exit_price"], c[self.HOLD - 1])

    def test_ride_cap_none_is_the_plain_time_exit(self):
        o, h, l, c = self.rising(self.HOLD + 5)
        p = replay_exit(o, h, l, c, hold_bars=self.HOLD, ride_cap=None)
        self.assertEqual((p["reason"], p["bar"]), ("time", self.HOLD - 1))
        self.assertFalse(p["riding"])

    def test_a_window_that_ends_mid_ride_is_open_not_a_result(self):
        """Booking the last available bar as a time exit would publish a
        number for a rule nobody trades; the trade is still on."""
        o, h, l, c = self.rising(self.HOLD + 1)
        p = replay_exit(o, h, l, c, hold_bars=self.HOLD, ride_cap=20)
        self.assertEqual(p["reason"], "")
        self.assertTrue(p["riding"])
        self.assertFalse(p["exited"])
        self.assertEqual(simulate_exit(o, h, l, c, hold_bars=self.HOLD,
                                       ride_cap=20)[2], "na")

    def test_the_late_profit_take_still_fires_during_the_ride(self):
        o, h, l, c = self.rising(self.HOLD + 6)
        late_px = 100.0 * (1 + DEFAULT_RULE["late_gain"])
        c[self.HOLD] = round(late_px + 0.5, 2)       # day 11 closes in profit
        h[self.HOLD] = c[self.HOLD] + 0.03
        p = replay_exit(o, h, l, c, hold_bars=self.HOLD, ride_cap=20)
        self.assertEqual(p["reason"], "late")
        self.assertEqual(p["bar"], self.HOLD + 1)
        self.assertAlmostEqual(p["exit_price"], o[self.HOLD + 1])

    def test_the_ledger_window_rows_do_not_ride(self):
        """signal_ledger's h-bar rows keep their meaning: the rule inside the
        window. The ride belongs to scanner/live_record.py."""
        src = (Path(__file__).resolve().parent.parent / "scanner"
               / "signal_ledger.py").read_text(encoding="utf-8")
        i = src.find("def _simulate_rule(")
        self.assertIn("ride_cap=None", src[i:i + 4000])


class FirstDayColumn(TrackerColumns):
    """First_Day is 'not on the previous LEDGER session', independent of the
    streak's gap tolerance (2026-09-23)."""

    def test_absent_yesterday_is_a_first_day_even_inside_a_streak(self):
        flat = [(100, 101, 99, 100)] * 10
        # A: on the list 09-10 and 09-11 (the previous ledger session);
        # B: last seen 09-09, so absent on 09-11 -- a re-entry the streak
        # (gap tolerance 10) still counts as one appearance block
        led = {"A": ["2026-09-10", "2026-09-11"], "B": ["2026-09-09"]}
        out = self._run({"A": flat, "B": flat}, led)
        by = dict(zip(out["Stock_ID"], out["First_Day"]))
        self.assertFalse(by["A"])
        self.assertTrue(by["B"])
        # and the streak still anchors B on 09-09: the holder's calendar is kept
        self.assertEqual(out.set_index("Stock_ID").loc["B", "Entry_Date"], "2026-09-10")

    def test_no_history_at_all_is_a_first_day(self):
        flat = [(100, 101, 99, 100)] * 10
        out = self._run({"A": flat}, {})
        self.assertTrue(bool(out["First_Day"].iloc[0]))


# --------------------------------------------------------------------------
# 2026-10-08: canonical replay, segments and Prev_* (DECISIONS "Canonical rule
# replay" / "Segments / anchors"). The calendar is long enough for a full
# ride; a filler name listed on every session before today stands in for the
# rest of the list, so the ledger has sessions on which the name was absent.
# --------------------------------------------------------------------------
def _weekdays(start, n):
    from datetime import date, timedelta
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


LONG = _weekdays("2026-08-03", 40)
FLAT = (100, 101, 99, 100)
FILLER = "9999"
# signal d0, entry d1 @100, flat; the day-10 close (d10) of 97 is under its
# 5-bar mean (99.4), so the rule's time exit books d10 @97.
TIME_EXIT = [FLAT] * 10 + [(100, 100.5, 96.5, 97)] + [(97, 98, 96, 97)] * 29


class _LongHarness(_TrackerHarness):
    CAL = LONG

    def led(self, own, today_i, extra=None):
        out = {"A": [self.CAL[i] for i in own],
               FILLER: list(self.CAL[:today_i])}
        out.update(extra or {})
        return out

    def run_a(self, rows, own, today_i, **kw):
        out = self._run({"A": rows[:today_i + 1]}, self.led(own, today_i),
                        today=self.CAL[today_i], **kw)
        return out.set_index("Stock_ID").loc["A"]


class PathDependentExit(_LongHarness, unittest.TestCase):
    """The rule decides the ride at the day-10 close, not at today's (the
    3498 shape: 09-15 close under its 5-bar mean, later closes above it)."""

    def test_a_later_strong_close_does_not_undo_the_time_exit(self):
        rows = TIME_EXIT[:11] + [(97, 99, 96.5, 98.5), (98.5, 100.5, 98, 100.4)]
        r = self.run_a(rows, range(12), 12)
        self.assertEqual(r["Hold_Status"], "exited")
        self.assertEqual(r["Exit_Signal"], "time")
        self.assertEqual(r["Exit_Signal_Date"], LONG[10])
        self.assertAlmostEqual(r["Exit_Signal_Price"], 97.0)
        self.assertEqual(r["Hold_Day"], 10)
        self.assertEqual(r["Hold_Remaining"], 0)

    def test_exit_on_todays_bar_is_exit_today(self):
        r = self.run_a(TIME_EXIT, range(10), 10)
        self.assertEqual(r["Hold_Status"], "exit_today")
        self.assertEqual(r["Exit_Signal"], "time")
        self.assertEqual(r["Exit_Signal_Date"], LONG[10])
        self.assertEqual(r["Hold_Remaining"], 0)

    def test_the_market_leg_rides_on_a_disturbed_day(self):
        r = self.run_a(TIME_EXIT, range(10), 10, disturbed={LONG[10]})
        self.assertEqual(r["Hold_Status"], "delay")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertIn("TAIEX", r["Hold_Note"])
        # the next close is not disturbed and still under its mean: out
        rows = TIME_EXIT[:11] + [(97, 98, 96, 96.5), (96.5, 97, 96, 96.5)]
        r = self.run_a(rows, range(12), 12, disturbed={LONG[10]})
        self.assertEqual(r["Hold_Status"], "exited")
        self.assertEqual(r["Exit_Signal"], "time")
        self.assertEqual(r["Exit_Signal_Date"], LONG[11])
        self.assertAlmostEqual(r["Exit_Signal_Price"], 96.5)

    def test_the_stock_leg_rides_while_the_close_beats_its_mean(self):
        rows = ([FLAT, (100, 100.5, 96.5, 97)] + [(97, 97.5, 96.5, 97)] * 4
                + [(97, 97, 95.5, 96)] * 2 + [(96, 97.5, 95.5, 97),
                                               (97, 98.5, 96.5, 98),
                                               (98, 99.5, 97.5, 99),
                                               (99, 100.5, 98.5, 100)])
        r = self.run_a(rows, range(11), 11)
        self.assertEqual(r["Hold_Status"], "delay")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertIn("5-bar mean", r["Hold_Note"])
        self.assertEqual(r["Hold_Day"], 11)
        self.assertEqual(r["Hold_Day"] + r["Hold_Remaining"], 10)

    def test_past_the_cap_without_bars_is_overdue(self):
        a = [FLAT] * 4 + [None] * 18
        series = {"A": a, "F": [FLAT] * 22}
        out = self._run(series, self.led(range(21), 21), today=LONG[21])
        r = out.set_index("Stock_ID").loc["A"]
        self.assertEqual(r["Hold_Status"], "overdue")
        self.assertEqual(r["Exit_Signal"], "")

    def test_a_name_with_no_bars_since_entry_is_judged_by_the_calendar(self):
        """T5: calendar_status (holding / exit_today / overdue) had no test.
        The name has one bar (the signal day) and nothing after it, so the
        entry has no fill: the status can only come from the trading
        calendar, which the filler name keeps moving."""
        want = {5: ("holding", "held 5/10, exit in 5 trading day(s)"),
                9: ("holding", "held 9/10, exit in 1 trading day(s)"),
                10: ("exit_today", "exit at close"),
                11: ("overdue", "no result in the price store"),
                14: ("overdue", "no result in the price store")}
        for today_i, (status, note) in want.items():
            with self.subTest(today=LONG[today_i]):
                series = {"A": [FLAT], FILLER: [FLAT] * (today_i + 1)}
                out = self._run(series, self.led(range(today_i + 1), today_i),
                                today=LONG[today_i])
                r = out.set_index("Stock_ID").loc["A"]
                self.assertEqual(r["Hold_Status"], status)
                self.assertIn(note, r["Hold_Note"])
                self.assertEqual(r["Entry_Date"], LONG[1])
                self.assertEqual(r["Exit_Signal"], "")
                self.assertEqual(r["Exit_Note"], "no bars since entry yet")
                self.assertTrue(pd.isna(r["Entry_Open"]))
                self.assertEqual(r["Hold_Day"], today_i)
                self.assertEqual(r["Hold_Remaining"], 10 - today_i)

    def test_the_tracker_books_what_live_record_books(self):
        """Parity: the card and the live record replay one rule."""
        import random
        from scanner.live_record import replay_trade
        marks = {LONG[11], LONG[12], LONG[15]}
        for seed in range(40):
            rng = random.Random(seed)
            rows, px = [], 100.0
            for _ in range(26):
                op = round(px * (1 + rng.uniform(-0.03, 0.03)), 2)
                cl = round(op * (1 + rng.uniform(-0.05, 0.05)), 2)
                hi = round(max(op, cl) * (1 + rng.uniform(0, 0.03)), 2)
                lo = round(min(op, cl) * (1 - rng.uniform(0, 0.03)), 2)
                rows.append((op, hi, lo, cl))
                px = cl
            with self.subTest(seed=seed):
                r = self.run_a(rows, range(25), 25, disturbed=marks)
                fwd = pd.DataFrame([dict(date=d, open=o, high=h, low=l, close=c)
                                    for d, (o, h, l, c) in zip(LONG[1:26], rows[1:26])])
                t = replay_trade(fwd, extend_if=lambda i, d: d in marks)
                if t["exited"]:
                    self.assertEqual(r["Exit_Signal"], t["reason"])
                    self.assertEqual(r["Exit_Signal_Date"], t["exit_date"])
                    self.assertAlmostEqual(r["Exit_Signal_Price"],
                                           round(t["exit_price"], 2))
                    self.assertIn(r["Hold_Status"], ("exited", "exit_today"))
                else:
                    self.assertEqual(r["Exit_Signal"], "")


class ReentrySegment(_LongHarness, unittest.TestCase):
    """A fresh signal after the old trade closed is a NEW segment: the card
    shows the new trade and Prev_* the old one (8227 / 3498, 2026-10-07)."""

    def test_reentry_after_a_closed_trade_is_pending(self):
        from scanner.live_record import net_pct, replay_trade
        r = self.run_a(TIME_EXIT, list(range(11)), 14,
                       flags={"A": {LONG[0]: 1}})
        self.assertTrue(bool(r["First_Day"]))
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Entry_Date"], "")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertEqual(r["Hold_Anchor"], LONG[14])
        self.assertEqual(r["Hold_Anchor_Kind"], "reentry")
        self.assertEqual(r["Prev_Signal_Date"], LONG[0])
        self.assertEqual(r["Prev_Was_Signal"], True)
        self.assertEqual(r["Prev_Entry_Date"], LONG[1])
        self.assertAlmostEqual(r["Prev_Entry_Open"], 100.0)
        self.assertEqual(r["Prev_Exit_Signal"], "time")
        self.assertEqual(r["Prev_Exit_Signal_Date"], LONG[10])
        self.assertAlmostEqual(r["Prev_Exit_Signal_Price"], 97.0)
        self.assertEqual(r["Sessions_Since_Prev_Exit"], 4)
        # (g) the net return is live_record's, for the same bars
        self.assertAlmostEqual(r["Prev_Exit_Ret_Pct"], round(net_pct(-3.0), 2))
        fwd = pd.DataFrame([dict(date=d, open=o, high=h, low=l, close=c)
                            for d, (o, h, l, c) in zip(LONG[1:15], TIME_EXIT[1:15])])
        self.assertAlmostEqual(r["Prev_Exit_Ret_Pct"],
                               round(replay_trade(fwd)["ret_net_pct"], 2))

    def test_the_session_after_is_day_one_of_the_new_trade(self):
        r = self.run_a(TIME_EXIT, list(range(11)) + [14], 15,
                       flags={"A": {LONG[0]: 1, LONG[14]: 1}})
        self.assertEqual(r["Hold_Status"], "holding")
        self.assertEqual(r["Hold_Day"], 1)
        self.assertEqual(r["Entry_Date"], LONG[15])
        self.assertAlmostEqual(r["Entry_Open"], 97.0)
        self.assertAlmostEqual(r["Plan_Stop"],
                               tracker._lvl(97.0, -tracker.STOP_PCT, "down", "A"))
        self.assertEqual(r["Exit_Signal"], "")
        self.assertEqual(r["Prev_Signal_Date"], LONG[0])
        self.assertEqual(r["Prev_Exit_Signal"], "time")

    def test_a_resignal_inside_an_open_trade_keeps_the_calendar(self):
        for flag in (None, 1):
            with self.subTest(flag=flag):
                r = self.run_a([FLAT] * 9, range(6), 8,
                               flags={"A": {LONG[0]: flag}})
                self.assertTrue(bool(r["First_Day"]))
                self.assertEqual(r["Entry_Date"], LONG[1])
                self.assertEqual(r["Hold_Status"], "holding")
                self.assertEqual(r["Hold_Anchor_Kind"], "first")
                for col in tracker.PREV_COLUMNS:
                    self.assertIsNone(r[col], col)

    def test_a_non_signal_anchor_reanchors_on_reentry(self):
        """8227: the 09-16 anchor was not a signal (buy_ready 0); its
        hypothetical trade hit the target, and 10-07 is a fresh signal."""
        rows = [FLAT] * 3 + [(100, 121, 99, 118)] + [(118, 119, 117, 118)] * 10
        r = self.run_a(rows, range(6), 9, flags={"A": {LONG[0]: 0}})
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Hold_Anchor_Kind"], "reentry")
        self.assertEqual(r["Prev_Was_Signal"], False)
        self.assertEqual(r["Prev_Exit_Signal"], "tp")
        self.assertEqual(r["Prev_Exit_Signal_Date"], LONG[3])
        self.assertAlmostEqual(r["Prev_Exit_Signal_Price"], 120.0)
        self.assertEqual(r["Sessions_Since_Prev_Exit"], 6)
        # still open but not a signal: re-anchors all the same
        r = self.run_a([FLAT] * 10, range(6), 9, flags={"A": {LONG[0]: 0}})
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Prev_Was_Signal"], False)
        self.assertEqual(r["Prev_Exit_Signal"], "")
        self.assertIsNone(r["Prev_Exit_Signal_Date"])
        self.assertIsNone(r["Prev_Exit_Ret_Pct"])
        self.assertIsNone(r["Sessions_Since_Prev_Exit"])

    def test_a_tracked_row_never_reanchors_on_its_own_bar(self):
        rows = [FLAT, FLAT, (100, 100, 70, 75)] + [(75, 76, 74, 75)] * 5
        r = self.run_a(rows, [0], 5)
        self.assertEqual(r["Hold_Status"], "pending")
        r = self.run_a(rows, [0], 5, add_own_bar=False)
        self.assertEqual(r["Hold_Status"], "exited")
        self.assertEqual(r["Entry_Date"], LONG[1])
        self.assertEqual(r["Exit_Signal"], "stop")
        self.assertIsNone(r["Prev_Signal_Date"])

    def test_a_long_gap_still_splits_without_prev(self):
        r = self.run_a([FLAT] * 13, [0], 12)
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Hold_Anchor_Kind"], "gap")
        for col in tracker.PREV_COLUMNS:
            self.assertIsNone(r[col], col)

    def test_buy_flags_read_the_latest_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "ledger.db"
            conn = sqlite3.connect(led)
            try:
                conn.execute("CREATE TABLE picks (scan_ts TEXT, scan_mode TEXT, "
                             "stock_id TEXT, bar_date TEXT, buy_ready INTEGER)")
                conn.executemany("INSERT INTO picks VALUES (?,?,?,?,?)", [
                    ("2026-10-07 15:05", "mode_prelaunch", "A", "2026-10-07", 0),
                    ("2026-10-07 18:30", "mode_prelaunch", "A", "2026-10-07", 1),
                    ("2026-09-01 15:00", "mode_prelaunch", "A", "2026-09-01", None),
                    ("2026-10-07 15:05", "mode_other", "B", "2026-10-07", 1)])
                conn.commit()
            finally:
                conn.close()
            with mock.patch.object(tracker, "SIGNAL_LEDGER_FILE", led):
                flags = tracker._ledger_buy_flags("mode_prelaunch")
            with mock.patch.object(tracker, "SIGNAL_LEDGER_FILE", Path(tmp) / "no.db"):
                self.assertEqual(tracker._ledger_buy_flags("mode_prelaunch"), {})
        self.assertEqual(flags, {"A": {"2026-10-07": 1, "2026-09-01": None}})


    def test_a_replayed_past_session_does_not_see_later_sessions(self):
        # the store already holds the sessions after `today` (a replay of an
        # old scan): the signal-day card must read exactly as it did live --
        # no entry date, no fill priced from tomorrow's open
        rows = [FLAT] * 20
        out = self._run({"A": rows}, self.led([14], 14), today=LONG[14])
        r = out.set_index("Stock_ID").loc["A"]
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Entry_Date"], "")
        self.assertTrue(r["Entry_Open"] is None or r["Entry_Open"] != r["Entry_Open"])
        self.assertEqual(r["Exit_Date"], "")

class RecAnchors(_LongHarness, unittest.TestCase):
    """rec_anchors {sid: {anchor, stop, target, rec_id}} override the natural
    anchor (portfolio.sync builds them from live recommendations)."""

    ROWS = [(round(100 + 0.1 * i, 2), 101, 99, 100) for i in range(20)]

    def test_rec_anchor_overrides_the_streak(self):
        r = self.run_a(self.ROWS, range(9), 9,
                       rec_anchors={"A": {"anchor": LONG[6], "stop": 80.0,
                                          "target": 120.0, "rec_id": "r1"}})
        self.assertEqual(r["Hold_Anchor_Kind"], "rec")
        self.assertEqual(r["Entry_Date"], LONG[7])
        self.assertAlmostEqual(r["Entry_Open"], 100.7)
        self.assertEqual(r["Hold_Day"], 3)
        self.assertEqual(r["Prev_Signal_Date"], LONG[0])
        self.assertEqual(r["Prev_Entry_Date"], LONG[1])
        self.assertAlmostEqual(r["Prev_Entry_Open"], 100.1)
        # the streak's own trade was still open at the recommendation
        self.assertEqual(r["Prev_Exit_Signal"], "")
        self.assertIsNone(r["Prev_Exit_Signal_Date"])

    def test_rec_anchor_today_is_pending_with_rec_stop(self):
        r = self.run_a(self.ROWS, range(9), 9,
                       rec_anchors={"A": {"anchor": LONG[9], "stop": 95.5,
                                          "target": 120.0, "rec_id": "r1"}})
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertAlmostEqual(r["Plan_Stop"], 95.5)
        self.assertEqual(r["Exit_Signal"], "")
        self.assertEqual(r["Prev_Signal_Date"], LONG[0])

    def test_an_anchor_outside_the_calendar_falls_back(self):
        r = self.run_a(self.ROWS, range(9), 9,
                       rec_anchors={"A": {"anchor": "2030-01-02", "stop": 95.5}})
        self.assertEqual(r["Hold_Anchor_Kind"], "first")
        self.assertEqual(r["Entry_Date"], LONG[1])
        self.assertIsNone(r["Prev_Signal_Date"])

    def test_a_closed_rec_gives_way_to_a_newer_signal(self):
        rows = [FLAT] * 3 + [(100, 121, 99, 118)] + [(118, 119, 117, 118)] * 10
        r = self.run_a(rows, range(6), 9, flags={"A": {LONG[0]: 1}},
                       rec_anchors={"A": {"anchor": LONG[0], "stop": 80.0}})
        self.assertEqual(r["Hold_Anchor_Kind"], "reentry")
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Prev_Exit_Signal"], "tp")

    def test_no_rec_anchor_is_unchanged(self):
        a = self.run_a(self.ROWS, range(9), 9)
        b = self.run_a(self.ROWS, range(9), 9, rec_anchors={})
        self.assertEqual(list(a.index), list(b.index))
        self.assertEqual(a.astype(str).tolist(), b.astype(str).tolist())


class FrameRobustness(_LongHarness, unittest.TestCase):
    """Verifier fixes, 2026-10-08: "today" is the frame's newest bar date,
    not row 0's, and one row the tracker cannot work out is blanked on its
    own (unknown status blocks the buy) instead of taking the frame with it."""

    def _frame(self, series, dates, today_i, **patches):
        led = self.led(range(today_i), today_i,
                       extra={sid: list(self.CAL[:today_i]) for sid in series})
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store(tmp, series)
            df = pd.DataFrame([{"Stock_ID": sid, "Data_Date": dates[sid],
                                "Close_Price": 100.0, "Strict_Stop_Loss": 80.0,
                                "Add_Price": 90.0} for sid in series])
            with mock.patch.object(tracker, "PRICE_VOLUME_FILE", db), \
                    mock.patch.object(tracker, "_ledger_bar_dates", lambda m: led), \
                    mock.patch.object(tracker, "_ledger_buy_flags", lambda m: {}), \
                    mock.patch.object(tracker, "_disturbed_fn", lambda: None):
                ctx = [mock.patch.object(tracker, k, v) for k, v in patches.items()]
                for c in ctx:
                    c.start()
                try:
                    out = tracker.annotate_holding(df, "mode_prelaunch")
                finally:
                    for c in ctx:
                        c.stop()
        return out.set_index("Stock_ID")

    def test_a_stale_first_row_does_not_wind_back_today(self):
        series = {"S": [FLAT] * 6, "A": TIME_EXIT[:13]}
        out = self._frame(series, {"S": LONG[5], "A": LONG[12]}, 12)
        a = out.loc["A"]
        # as of LONG[12] the time exit of LONG[10] is history; read as of
        # row 0's LONG[5] it would still be "holding" on day 4
        self.assertEqual(a["Hold_Status"], "exited")
        self.assertEqual(a["Exit_Signal_Date"], LONG[10])
        self.assertEqual(tracker._frame_today(pd.DataFrame(
            {"Data_Date": [LONG[5], None, "bad", LONG[12], float("nan")]})), LONG[12])
        self.assertEqual(tracker._frame_today(pd.DataFrame({"X": [1]})), "")

    def test_a_failing_exit_plan_blanks_only_its_row(self):
        series = {"A": TIME_EXIT[:13], "B": TIME_EXIT[:13]}
        real = tracker._plan_row

        def flaky(sid, *a, **k):
            if sid == "B":
                raise ValueError("boom")
            return real(sid, *a, **k)
        out = self._frame(series, {"A": LONG[12], "B": LONG[12]}, 12,
                          _plan_row=flaky)
        self.assertEqual(out.loc["A", "Hold_Status"], "exited")
        self.assertEqual(out.loc["B", "Hold_Status"], "")
        self.assertEqual(out.loc["B", "Exit_Signal"], "")
        self.assertEqual(out.loc["B", "Entry_Date"], out.loc["A", "Entry_Date"])

    def test_a_failing_segment_walk_blanks_only_its_row(self):
        series = {"A": TIME_EXIT[:13], "B": TIME_EXIT[:13]}
        real = tracker._segment_walk

        def flaky(known, *a, **k):
            flaky.calls += 1           # one walk per row, in frame order: B
            if flaky.calls == 2:
                raise ValueError("boom")
            return real(known, *a, **k)
        flaky.calls = 0
        out = self._frame(series, {"A": LONG[12], "B": LONG[12]}, 12,
                          _segment_walk=flaky)
        self.assertEqual(out.loc["A", "Hold_Status"], "exited")
        self.assertEqual(out.loc["B", "Hold_Status"], "")
        self.assertEqual(out.loc["B", "Hold_Anchor"], "")
        self.assertEqual(out.loc["B", "Hold_Anchor_Kind"], "")
        for c in tracker.PREV_COLUMNS:
            self.assertTrue(pd.isna(out.loc["B", c]), c)

    def test_a_failing_previous_trade_blanks_the_whole_group(self):
        led_flags = {"A": {LONG[0]: 1}}
        with mock.patch.object(tracker, "_prev_columns",
                               side_effect=ValueError("boom")):
            out = self._run({"A": TIME_EXIT[:15]},
                            self.led(list(range(11)), 14), today=LONG[14],
                            flags=led_flags)
        r = out.set_index("Stock_ID").loc["A"]
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Hold_Anchor_Kind"], "reentry")
        for c in tracker.PREV_COLUMNS:
            self.assertTrue(pd.isna(r[c]), c)
