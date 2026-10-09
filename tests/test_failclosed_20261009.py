"""
Fail-closed pins from the 2026-10-09 conformance audit (fault injection, D11).

Each class feeds the pipeline a broken input and checks the rule still holds:
a store that cannot be read must never be read as "nothing there". ASCII only.

    python -m unittest tests.test_failclosed_20261009 -v
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

import scanner.holding_tracker as tracker
from scanner import result_checks
from scanner.scan_mode import mark_buy_ready
from tests.test_scan_pipeline import GateCase, MODE, TODAY, frame

CONFLICT_MARK = "<" * 7 + " HEAD"


def _calendar(n=13, start=date(2026, 8, 24)):
    cal, d = [], start
    while len(cal) < n:
        if d.weekday() < 5:
            cal.append(d.isoformat())
        d += timedelta(days=1)
    return cal


def _price_db(path, cal, sid="8069"):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, open REAL, "
                     "high REAL, low REAL, close REAL)")
        for day in cal:
            conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?)",
                         (sid, day, 100, 101, 99, 100))
        conn.commit()
    finally:
        conn.close()


def _ledger_db(path, picks):
    """picks = [(stock_id, bar_date, buy_ready)] for MODE."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE picks (scan_ts TEXT, scan_mode TEXT, "
                     "stock_id TEXT, bar_date TEXT, buy_ready INTEGER)")
        conn.executemany("INSERT INTO picks VALUES (?,?,?,?,?)",
                         [("t", MODE, s, d, b) for s, d, b in picks])
        conn.commit()
    finally:
        conn.close()


class UnreadableLedgerBlocksTheBuy(GateCase):
    """D11-01 / M-15. A ledger that cannot be read read as "no history", every
    held name looked like a first-day signal and became Buy_Ready."""

    def _run(self, ledger_setup):
        cal = _calendar()
        self.assertEqual(cal[-1], TODAY)
        with tempfile.TemporaryDirectory() as tmp:
            pv = Path(tmp) / "pv.db"
            _price_db(pv, cal)
            led = Path(tmp) / "ledger.db"
            ledger_setup(led, cal)
            df = frame().drop(columns=["Hold_Status"])
            with mock.patch.object(tracker, "PRICE_VOLUME_FILE", pv), \
                    mock.patch.object(tracker, "SIGNAL_LEDGER_FILE", led), \
                    mock.patch.object(tracker, "_disturbed_fn", lambda: None):
                out = tracker.annotate_holding(df, MODE)
        return mark_buy_ready(out, MODE, session_date=TODAY)

    def test_control_readable_ledger_lets_a_fresh_name_through(self):
        def setup(led, cal):                  # another name, yesterday
            _ledger_db(led, [("9999", cal[-2], 0)])
        out = self._run(setup)
        self.assertTrue(bool(out["Buy_Ready"].iloc[0]), out.iloc[0].to_dict())

    def test_missing_ledger_blocks_unknown(self):
        out = self._run(lambda led, cal: None)          # file never created
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "unknown")
        self.assertEqual(out["Hold_Status"].iloc[0], "")

    def test_garbage_ledger_blocks_unknown(self):
        out = self._run(lambda led, cal: led.write_bytes(b"not a database" * 50))
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "unknown")

    def test_ledger_without_a_picks_table_blocks_unknown(self):
        def setup(led, cal):
            conn = sqlite3.connect(led)
            conn.execute("CREATE TABLE other (x TEXT)")
            conn.commit()
            conn.close()
        out = self._run(setup)
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "unknown")

    def test_the_reader_says_none_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "l.db"
            with mock.patch.object(tracker, "SIGNAL_LEDGER_FILE", led):
                self.assertIsNone(tracker._ledger_bar_dates(MODE))   # missing
                led.write_bytes(b"junk" * 100)
                self.assertIsNone(tracker._ledger_bar_dates(MODE))   # garbage
            led2 = Path(tmp) / "ok.db"
            _ledger_db(led2, [("8069", "2026-09-08", 1)])
            with mock.patch.object(tracker, "SIGNAL_LEDGER_FILE", led2):
                self.assertEqual(tracker._ledger_bar_dates(MODE),
                                 {"8069": ["2026-09-08"]})
                self.assertEqual(tracker._ledger_bar_dates("mode_other"), {})


class ADepletedPreviousSessionIsNotYesterdaysList(UnreadableLedgerBlocksTheBuy):
    """D3-02: one row where 49 are usual made 49 of 50 names look new."""

    def _picks(self, cal, last_count):
        picks = []
        for day in cal[-6:-1]:
            picks += [("S%02d" % i, day, 0) for i in range(40)]
        picks += [("S%02d" % i, cal[-2], 0) for i in range(last_count)]
        return picks

    def test_thin_detector(self):
        led = {"S%02d" % i: ["d1", "d2", "d3", "d4"] for i in range(40)}
        led["S00"] = ["d1", "d2", "d3", "d4", "d5"]          # d5 holds 1 name
        self.assertEqual(tracker._thin_previous_session(led, "d6"), ("d5", 1, 40))
        led2 = {"S%02d" % i: ["d1", "d2", "d3", "d4", "d5"] for i in range(40)}
        self.assertIsNone(tracker._thin_previous_session(led2, "d6"))
        self.assertIsNone(tracker._thin_previous_session({"A": ["d1"]}, "d6"))

    def test_a_thin_previous_session_blocks_unknown(self):
        def setup(led, cal):
            conn = sqlite3.connect(led)
            conn.execute("CREATE TABLE picks (scan_ts TEXT, scan_mode TEXT, "
                         "stock_id TEXT, bar_date TEXT, buy_ready INTEGER)")
            rows = []
            for day in cal[-7:-2]:                         # usual: 40 names
                rows += [("t", MODE, "S%02d" % i, day, 0) for i in range(40)]
            rows += [("t", MODE, "S00", cal[-2], 0)]       # yesterday: 1 name
            conn.executemany("INSERT INTO picks VALUES (?,?,?,?,?)", rows)
            conn.commit()
            conn.close()
        out = self._run(setup)
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "unknown")

    def test_a_full_previous_session_buys(self):
        def setup(led, cal):
            conn = sqlite3.connect(led)
            conn.execute("CREATE TABLE picks (scan_ts TEXT, scan_mode TEXT, "
                         "stock_id TEXT, bar_date TEXT, buy_ready INTEGER)")
            rows = []
            for day in cal[-7:-1]:
                rows += [("t", MODE, "S%02d" % i, day, 0) for i in range(40)]
            conn.executemany("INSERT INTO picks VALUES (?,?,?,?,?)", rows)
            conn.commit()
            conn.close()
        out = self._run(setup)
        self.assertTrue(bool(out["Buy_Ready"].iloc[0]), out.iloc[0].to_dict())


class TheCheckerSeesTheStores(unittest.TestCase):
    """D11-01 / D11-03: check_files raises an ERROR (so the list stays
    provisional) when the ledger or the recommendations export is broken."""

    def test_ledger_problem_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self.assertEqual(result_checks.ledger_problem(t / "none.db"), "missing")
            (t / "junk.db").write_bytes(b"junk" * 100)
            self.assertTrue(
                result_checks.ledger_problem(t / "junk.db").startswith("unreadable"))
            conn = sqlite3.connect(t / "empty.db")
            conn.execute("CREATE TABLE other (x TEXT)")
            conn.commit()
            conn.close()
            self.assertTrue(
                result_checks.ledger_problem(t / "empty.db").startswith("unreadable"))
            _ledger_db(t / "ok.db", [("1111", "2026-09-08", 0)])
            self.assertIsNone(result_checks.ledger_problem(t / "ok.db"))

    def test_recs_problem_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self.assertIsNone(result_checks.recs_problem(t / "absent.json"))
            (t / "bad.json").write_text("{not json", encoding="utf-8")
            self.assertIsNotNone(result_checks.recs_problem(t / "bad.json"))
            (t / "shape.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
            self.assertIsNotNone(result_checks.recs_problem(t / "shape.json"))
            (t / "list.json").write_text("[]", encoding="utf-8")
            self.assertIsNotNone(result_checks.recs_problem(t / "list.json"))
            (t / "ok.json").write_text(
                json.dumps({"recommendations": []}), encoding="utf-8")
            self.assertIsNone(result_checks.recs_problem(t / "ok.json"))

    def _payload(self, t):
        p = t / "scan_result.json"
        p.write_text(json.dumps({"meta": {"mode": MODE, "session_date": TODAY,
                                          "data_date": TODAY},
                                 "rows": [{"Stock_ID": "8069"}]}),
                     encoding="utf-8")
        return p

    def test_check_files_errors_on_a_broken_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            p = self._payload(t)
            rep = result_checks.check_files(p, ledger_path=t / "gone.db",
                                            write=False)
        codes = {i["code"] for i in rep["items"] if i["level"] == "error"}
        self.assertIn("ledger_unreadable", codes)

    def test_check_files_errors_on_a_corrupt_recs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            p = self._payload(t)
            (t / "recs.json").write_text(CONFLICT_MARK, encoding="utf-8")
            rep = result_checks.check_files(p, recs_path=t / "recs.json",
                                            write=False)
        codes = {i["code"] for i in rep["items"] if i["level"] == "error"}
        self.assertIn("recs_unreadable", codes)

    def test_a_healthy_pair_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            p = self._payload(t)
            _ledger_db(t / "led.db", [("1111", "2026-09-08", 0)])
            (t / "recs.json").write_text(
                json.dumps({"recommendations": []}), encoding="utf-8")
            rep = result_checks.check_files(p, ledger_path=t / "led.db",
                                            recs_path=t / "recs.json", write=False)
        codes = {i["code"] for i in rep["items"]}
        self.assertNotIn("ledger_unreadable", codes)
        self.assertNotIn("recs_unreadable", codes)

    def test_no_rows_no_ledger_complaint(self):
        # a zero-pick day needs no history
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            p = t / "scan_result.json"
            p.write_text(json.dumps({"meta": {"mode": MODE}, "rows": []}),
                         encoding="utf-8")
            rep = result_checks.check_files(p, ledger_path=t / "gone.db",
                                            write=False)
        self.assertNotIn("ledger_unreadable", {i["code"] for i in rep["items"]})


if __name__ == "__main__":
    unittest.main()
