"""
Tests for the dropped-out-but-still-held rows (scanner/tracked_rows.py).

The gap: a stock recommended four days ago and bought can leave the list, and
until 2026-09-21 the payload then carried only its price -- no chips, no
moving averages, no exit plan -- at the point in the trade where a holder
needs them most.

    python -m unittest tests.test_tracked_rows -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scanner import tracked_rows as tr


def price_db(path, dates, ids):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, close REAL)")
        for d in dates:
            for sid in ids:
                conn.execute("INSERT INTO data VALUES (?,?,?)", (sid, d, 10.0))
        conn.commit()
    finally:
        conn.close()


def ledger_db(path, picks):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE picks (scan_mode TEXT, stock_id TEXT, "
                     "bar_date TEXT)")
        conn.executemany("INSERT INTO picks VALUES (?,?,?)", picks)
        conn.commit()
    finally:
        conn.close()


DATES = ["2026-09-%02d" % d for d in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14)]


class RecentPicks(unittest.TestCase):
    def test_finds_names_picked_inside_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, l = Path(tmp) / "pv.db", Path(tmp) / "led.db"
            price_db(p, DATES, ["1111", "2222", "3333"])
            ledger_db(l, [("mode_prelaunch", "1111", "2026-09-02"),
                          ("mode_prelaunch", "2222", "2026-09-11"),
                          ("mode_other", "3333", "2026-09-11")])
            got = tr.recent_pick_ids(p, l, "mode_prelaunch", sessions=10)
        self.assertEqual(got, {"1111", "2222"})

    def test_a_name_outside_the_window_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, l = Path(tmp) / "pv.db", Path(tmp) / "led.db"
            price_db(p, DATES, ["1111"])
            ledger_db(l, [("mode_prelaunch", "1111", "2026-08-01")])
            got = tr.recent_pick_ids(p, l, "mode_prelaunch", sessions=5)
        self.assertEqual(got, set())

    def test_missing_files_are_not_an_error(self):
        self.assertEqual(tr.recent_pick_ids("no.db", "no.db", "m"), set())


class Split(unittest.TestCase):
    def frame(self, ids):
        return pd.DataFrame([{"Stock_ID": s, "Close_Price": 10.0,
                              "Data_Date": "2026-09-14"} for s in ids])

    def test_only_recent_picks_that_left_the_list(self):
        verified = self.frame(["1111", "2222", "3333", "4444"])
        published = self.frame(["1111", "3333"])
        out = tr.split_tracked(verified, published, {"1111", "2222", "9999"})
        self.assertEqual(list(out["Stock_ID"]), ["2222"])

    def test_nothing_to_track_returns_an_empty_frame(self):
        verified = self.frame(["1111"])
        out = tr.split_tracked(verified, self.frame(["1111"]), {"1111"})
        self.assertEqual(len(out), 0)
        out = tr.split_tracked(verified, self.frame(["1111"]), set())
        self.assertEqual(len(out), 0)

    def test_the_cap_bounds_the_extra_work(self):
        ids = ["%04d" % i for i in range(1000, 1100)]
        out = tr.split_tracked(self.frame(ids), self.frame([]), set(ids), limit=5)
        self.assertEqual(len(out), 5)


class Annotate(unittest.TestCase):
    def test_a_tracked_row_is_never_buyable(self):
        df = pd.DataFrame([{
            "Stock_ID": "1815", "Stock_Name": "x", "Market": "OTC",
            "Data_Date": "2026-09-14", "Close_Price": 100.0, "MA10": 98.0,
            "MA20": 95.0, "Min_Price_3": 97.0,
        }])
        out = tr.annotate_tracked(df, "mode_prelaunch")
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "dropped")
        # and it got the trade levels a listed row gets
        self.assertIn("Strict_Stop_Loss", out.columns)
        self.assertIn("Target_Price", out.columns)

    def test_empty_input_is_returned_unchanged(self):
        self.assertIsNone(tr.annotate_tracked(None, "mode_prelaunch"))


if __name__ == "__main__":
    unittest.main()
