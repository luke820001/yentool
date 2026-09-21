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


class TrackerColumns(unittest.TestCase):
    """Drive annotate_holding against a tiny price store."""

    CAL = ["2026-09-%02d" % d for d in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14)]

    def _store(self, tmp, series):
        db = Path(tmp) / "pv.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, open REAL, "
                         "high REAL, low REAL, close REAL)")
            for sid, rows in series.items():
                for d, (o, h, l, c) in zip(self.CAL, rows):
                    conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?)",
                                 (sid, d, o, h, l, c))
            conn.commit()
        finally:
            conn.close()
        return db

    def _run(self, series, ledger_dates, today="2026-09-14"):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store(tmp, series)
            df = pd.DataFrame([{"Stock_ID": sid, "Data_Date": today,
                                "Close_Price": rows[self.CAL.index(today)][3],
                                "Strict_Stop_Loss": round(
                                    rows[self.CAL.index(today)][3] * (1 - tracker.STOP_PCT), 2),
                                "Add_Price": round(
                                    rows[self.CAL.index(today)][3] * (1 - tracker.ADD_PCT), 2)}
                               for sid, rows in series.items()])
            with mock.patch.object(tracker, "PRICE_VOLUME_FILE", db), \
                    mock.patch.object(tracker, "_ledger_bar_dates",
                                      lambda mode: ledger_dates), \
                    mock.patch("scanner.market_regime.get_market_regime",
                               lambda: {"ok": True, "above20": True, "risk_on": True}):
                return tracker.annotate_holding(df, "mode_prelaunch")

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
        self.assertEqual(r["Hold_Status"], "overdue")
        self.assertEqual(r["Exit_Signal"], "time")
        self.assertEqual(r["Exit_Signal_Date"], cal[5])
        self.assertAlmostEqual(r["Exit_Signal_Price"], 100.0)


if __name__ == "__main__":
    unittest.main()
