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


def bars(*rows):
    """rows of (open, high, low, close) -> four lists."""
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


class Replay(unittest.TestCase):
    def test_open_position_reports_live_stop(self):
        o, h, l, c = bars((100, 103, 99, 102), (102, 104, 100, 103))
        p = replay_exit(o, h, l, c, dates=["d1", "d2"], hold_bars=None)
        self.assertEqual(p["reason"], "")
        self.assertFalse(p["exited"])
        self.assertFalse(p["armed"])
        self.assertAlmostEqual(p["stop"], 85.0)
        self.assertAlmostEqual(p["arm_px"], 106.0)
        self.assertAlmostEqual(p["target"], 120.0)

    def test_stop_hit_books_the_stop_level(self):
        o, h, l, c = bars((100, 101, 98, 99), (99, 100, 84, 90))
        p = replay_exit(o, h, l, c, dates=["d1", "d2"], hold_bars=None)
        self.assertEqual(p["reason"], "stop")
        self.assertEqual(p["bar"], 1)
        self.assertEqual(p["date"], "d2")
        self.assertAlmostEqual(p["exit_price"], 85.0)
        self.assertAlmostEqual(p["ret_pct"], -15.0)

    def test_gap_through_stop_books_the_open(self):
        o, h, l, c = bars((100, 101, 98, 99), (80, 82, 78, 81))
        p = replay_exit(o, h, l, c, hold_bars=None)
        self.assertEqual(p["reason"], "stop")
        self.assertAlmostEqual(p["exit_price"], 80.0)

    def test_arming_raises_the_stop_to_the_lock(self):
        # the arming bar's low stays above the lock (102), so nothing is booked
        o, h, l, c = bars((100, 101, 99, 100), (103, 107, 102.5, 106))
        p = replay_exit(o, h, l, c, hold_bars=None)
        self.assertEqual(p["reason"], "")
        self.assertTrue(p["armed"])
        self.assertAlmostEqual(p["stop"], 102.0)

    def test_armed_then_lock_hit_is_a_lock_exit(self):
        o, h, l, c = bars((100, 101, 99, 100), (103, 107, 102.5, 106),
                          (105, 105, 101, 101.5))
        p = replay_exit(o, h, l, c, dates=["d1", "d2", "d3"], hold_bars=None)
        self.assertEqual(p["reason"], "lock")
        self.assertEqual(p["date"], "d3")
        self.assertAlmostEqual(p["exit_price"], 102.0)

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
            bars((100, 101, 98, 99), (99, 100, 84, 90), *[(90, 91, 89, 90)] * 10),
            bars((100, 101, 99, 100), (103, 107, 102.5, 106), (105, 105, 101, 101.5),
                 *[(101, 102, 100, 101)] * 9),
            bars((100, 101, 99, 100), (101, 107, 100, 106), *[(101, 102, 100, 101)] * 10),
            bars((100, 101, 99, 100), (110, 121, 108, 119), *[(119, 120, 118, 119)] * 10),
        ]
        for o, h, l, c in cases:
            entry, ret, reason = simulate_exit(o, h, l, c)
            p = replay_exit(o, h, l, c, hold_bars=DEFAULT_RULE["hold_bars"])
            self.assertEqual(reason, p["reason"])
            self.assertAlmostEqual(entry, p["entry"])
            self.assertAlmostEqual(ret, p["ret_pct"])

    def test_simulate_exit_time_exit_unchanged(self):
        o, h, l, c = bars(*[(100, 101, 99, 100 + i * 0.1) for i in range(10)])
        entry, ret, reason = simulate_exit(o, h, l, c)
        self.assertEqual(reason, "time")
        self.assertAlmostEqual(ret, (c[9] / 100.0 - 1) * 100)


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
                                "Strict_Stop_Loss": round(rows[self.CAL.index(today)][3] * 0.85, 2)}
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
        crash[5] = (100, 100, 80, 82)          # 09-08: low through 85
        crash[6:] = [(82, 83, 81, 82)] * 4
        out = self._run({"1111": flat, "2222": crash},
                        {"1111": ["2026-09-01"], "2222": ["2026-09-01"]})
        by = out.set_index("Stock_ID")
        self.assertEqual(by.loc["2222", "Hold_Status"], "holding")
        self.assertEqual(by.loc["2222", "Exit_Signal"], "stop")
        self.assertEqual(by.loc["2222", "Exit_Signal_Date"], "2026-09-08")
        self.assertAlmostEqual(by.loc["2222", "Exit_Signal_Price"], 85.0)
        self.assertAlmostEqual(by.loc["2222", "Plan_Stop"], 85.0)
        self.assertIn("stop exit booked", by.loc["2222", "Exit_Note"])
        # the untouched name is simply holding with its stop shown
        self.assertEqual(by.loc["1111", "Exit_Signal"], "")
        self.assertAlmostEqual(by.loc["1111", "Plan_Stop"], 85.0)
        self.assertFalse(by.loc["1111", "Plan_Armed"])
        self.assertIn("sell if it trades below 85.00", by.loc["1111", "Exit_Note"])

    def test_armed_row_shows_raised_stop(self):
        rows = [(100, 101, 99, 100), (100, 101, 99, 100), (103, 107, 102.5, 106)] + \
               [(106, 107, 105, 106)] * 7
        out = self._run({"3333": rows}, {"3333": ["2026-09-01"]})
        r = out.iloc[0]
        self.assertTrue(r["Plan_Armed"])
        self.assertAlmostEqual(r["Plan_Stop"], 102.0)
        self.assertEqual(r["Exit_Signal"], "")
        self.assertIn("lock armed", r["Exit_Note"])

    def test_pending_row_carries_reference_stop(self):
        rows = [(100, 101, 99, 100)] * 10
        out = self._run({"4444": rows}, {"4444": []})
        r = out.iloc[0]
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertAlmostEqual(r["Plan_Stop"], 85.0)
        self.assertIsNone(r["Exit_Signal_Price"])

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
