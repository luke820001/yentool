"""
Second half of the 2026-09-21 audit regressions: the checks and the data
comparisons, including one defect introduced WHILE fixing the others.

    python -m unittest tests.test_audit_fixes_20260921b -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

COLS = ("date TEXT, stock_id TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume_share REAL, Volume_Lot REAL, MA5_Volume REAL, "
        "Min_Volume_20 REAL, Max_Price_20 REAL, Min_Price_20 REAL")


class BasisMonitorComparesTheSameDay(unittest.TestCase):
    """The two boards do not always publish the same session -- on 2026-09-21
    the TSE endpoint still served 09-18 while the OTC one had 09-21. Comparing
    a whole frame against one date lines stocks up against the WRONG day and
    reports the market having moved as a price-basis disagreement: it claimed
    63 of 1,396 bars were off by up to 9% when the true count was zero.
    """

    def _db(self, tmp):
        db = Path(tmp) / "pv.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE data (%s)" % COLS)
            for d, close in (("2026-09-18", 388.0), ("2026-09-21", 426.5)):
                conn.execute(
                    "INSERT INTO data VALUES (?,'6538',400,430,380,?,1,1,"
                    "NULL,NULL,NULL,NULL)", (d, close))
            conn.commit()
        finally:
            conn.close()
        return db

    def _frame(self):
        return pd.DataFrame([
            {"date": "2026-09-18", "stock_id": "1111", "open": 10.0,
             "high": 11.0, "low": 9.0, "close": 10.0, "volume_share": 1.0,
             "Volume_Lot": 1.0},
            {"date": "2026-09-21", "stock_id": "6538", "open": 412.0,
             "high": 426.5, "low": 400.0, "close": 426.5,
             "volume_share": 1.0, "Volume_Lot": 1.0},
        ])

    def test_a_board_on_a_different_session_is_not_a_disagreement(self):
        from scanner.market_snapshot import compare_to_store
        with tempfile.TemporaryDirectory() as tmp:
            out = compare_to_store(self._db(tmp), self._frame(),
                                   log=lambda *a: None)
        self.assertEqual(out["off_count"], 0, out["off"])
        self.assertEqual(out["checked"], 1)

    def test_a_real_disagreement_is_still_reported(self):
        from scanner.market_snapshot import compare_to_store
        df = self._frame()
        df.loc[1, "close"] = 388.0          # an adjusted price for that day
        with tempfile.TemporaryDirectory() as tmp:
            out = compare_to_store(self._db(tmp), df, log=lambda *a: None)
        self.assertEqual(out["off_count"], 1)
        self.assertGreater(out["worst"], 8.0)

    def test_reassertion_puts_the_exchange_close_back(self):
        from scanner.market_snapshot import reassert_exchange_prices
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE data SET close = 388.0 "
                             "WHERE date = '2026-09-21'")   # adjusted wins
                conn.commit()
            finally:
                conn.close()
            reassert_exchange_prices(db, self._frame(), log=lambda *a: None)
            conn = sqlite3.connect(db)
            try:
                got = conn.execute(
                    "SELECT close FROM data WHERE date = '2026-09-21'"
                ).fetchone()[0]
            finally:
                conn.close()
        self.assertAlmostEqual(float(got), 426.5)


class ClosedTradeCounters(unittest.TestCase):
    """Hold_Day + Hold_Remaining == Hold_Total describes an OPEN hold. A trade
    the rule already closed has zero remaining, and Hold_Day is the day it
    closed on."""

    def _payload(self, row):
        base = {"Stock_ID": "1111", "Close_Price": 100.0,
                "Hold_Total": 10, "Hold_Cap": 20}
        base.update(row)
        return {"meta": {"mode": "mode_prelaunch", "data_date": "2026-09-21",
                         "session_date": "2026-09-21"},
                "rows": [base]}

    def _codes(self, row):
        from scanner.result_checks import check_payload
        rep = check_payload(self._payload(row))
        return {i["code"] for i in rep["items"]}

    def test_a_closed_trade_is_not_asked_to_add_up(self):
        codes = self._codes({"Hold_Status": "exited", "Hold_Day": 6,
                             "Hold_Remaining": 0})
        self.assertNotIn("hold_day_arithmetic", codes)
        self.assertNotIn("exited_row_has_days_left", codes)

    def test_a_closed_trade_with_days_left_is_reported(self):
        codes = self._codes({"Hold_Status": "exited", "Hold_Day": 6,
                             "Hold_Remaining": 4})
        self.assertIn("exited_row_has_days_left", codes)

    def test_an_open_hold_still_has_to_add_up(self):
        codes = self._codes({"Hold_Status": "holding", "Hold_Day": 6,
                             "Hold_Remaining": 3})
        self.assertIn("hold_day_arithmetic", codes)


class HaltedNameIsReportedNotFailed(unittest.TestCase):
    """A listed row with no close IS a problem, but a name whose feed has
    ended cannot be fixed by running the scan again -- and a failing check
    makes the scan timer retry."""

    def _payload(self, ended):
        return {"meta": {"mode": "mode_prelaunch", "data_date": "2026-09-21",
                         "session_date": "2026-09-21",
                         "quotes": {"source_ended": ended}},
                "rows": [{"Stock_ID": "3017", "Close_Price": 100.0}]}

    def _quotes(self):
        return {"sessions": ["2026-09-18", "2026-09-21"],
                "closes": {"3017": [100.0, None]}, "names": {"3017": "x"}}

    def test_a_halted_name_warns(self):
        from scanner.result_checks import check_payload
        rep = check_payload(self._payload({"3017": "2026-09-18"}),
                            quotes=self._quotes())
        levels = {i["code"]: i["level"] for i in rep["items"]}
        self.assertEqual(levels.get("quotes_gap_row_ended"), "warn")
        self.assertNotIn("quotes_gap_row", levels)

    def test_an_unexplained_gap_still_fails(self):
        from scanner.result_checks import check_payload
        rep = check_payload(self._payload({}), quotes=self._quotes())
        levels = {i["code"]: i["level"] for i in rep["items"]}
        self.assertEqual(levels.get("quotes_gap_row"), "error")


class FrozenLevelsArePlaceable(unittest.TestCase):
    """Initial_Stop_Price and Initial_Target_Price are published as order
    levels. Records written before the tick ladder shipped carry raw
    multiplications -- the 1815 target was one -- and an unplaceable price is
    not a plan."""

    def test_a_legacy_level_is_snapped_on_the_way_to_the_screen(self):
        from portfolio.sync import _on_ladder
        self.assertEqual(_on_ladder("229.80", "up", "2330"), 230.0)
        self.assertEqual(_on_ladder("153.20", "down", "2330"), 153.0)
        self.assertEqual(_on_ladder("108.83", "down", "0050"), 108.8)
        self.assertIsNone(_on_ladder(None, "up"))
        self.assertIsNone(_on_ladder(0, "up"))


class TrackedCapKeepsTheNewest(unittest.TestCase):
    """The comment said it kept the most recently relevant names and the code
    kept the first 60 in turnover order, so on a heavy-pick day a name the
    owner actually holds could be dropped for a busier one."""

    def test_the_oldest_picks_are_the_ones_dropped(self):
        from scanner.tracked_rows import split_tracked
        verified = pd.DataFrame({"Stock_ID": ["1111", "2222", "3333"]})
        published = pd.DataFrame({"Stock_ID": []})
        picked = {"1111": "2026-08-01", "2222": "2026-09-20",
                  "3333": "2026-09-19"}
        out = split_tracked(verified, published, set(picked), limit=2,
                            picked_on=picked)
        self.assertEqual(sorted(out["Stock_ID"]), ["2222", "3333"])


if __name__ == "__main__":
    unittest.main()
