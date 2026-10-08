"""
ingestion/otc_index.py: the TPEX (OTC) index stored next to TAIEX in
taiex.db table 'TPEX', for the live record's benchmark (display only).

The exchange answers are recorded fixtures under tests/fixtures/otc_index/;
nothing here touches the network or the real taiex.db.

    python -m unittest tests.test_otc_index -v
"""
import gc
import json
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import pandas as pd

import ingestion.otc_index as oi
import storage.data_store as ds

FIX = Path(__file__).resolve().parent / "fixtures" / "otc_index"
TODAY = "2026-10-08"


def _json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeGet:
    """Legacy months 2026-06 and 2026-10 from fixtures, every other month an
    empty (future-like) answer; the openapi from its fixture."""

    def __init__(self, fail=False, fail_openapi=False, full=False):
        self.fail = fail
        self.fail_openapi = fail_openapi
        self.full = full            # every other month: its last session
        self.months = []
        self.openapi = 0

    def __call__(self, url, params=None):
        if self.fail:
            raise RuntimeError("HTTP 503")
        if url == oi.OPENAPI_URL:
            self.openapi += 1
            if self.fail_openapi:
                raise RuntimeError("HTTP 503")
            return _json("tpex_index_openapi.json")
        self.assertLegacy(url, params)
        d = params["d"]
        self.months.append(d)
        if d == "115/06":
            return _json("tpex_inx_11506.json")
        if d == "115/10":
            return _json("tpex_inx_11510.json")
        if self.full:
            y, m = int(d[:3]) + 1911, int(d[4:])
            last = oi._last_session_of_month(y, m).replace("-", "/")
            return {"stat": "ok", "tables": [{"date": d, "data": [
                [last, "400", "401", "399", "400.5", "0"]]}]}
        return _json("tpex_inx_11512.json")

    @staticmethod
    def assertLegacy(url, params):
        assert url == oi.LEGACY_URL, url
        assert params["o"] == "json" and params["l"] == "zh-tw", params


class Parsers(unittest.TestCase):
    def test_legacy_month(self):
        f = oi.parse_legacy(_json("tpex_inx_11506.json"))
        self.assertEqual(list(f.columns), oi.FRAME_COLS)
        self.assertEqual(len(f), 21)
        self.assertEqual(f["date"].iloc[0], "2026-06-01")
        self.assertEqual(f["close"].iloc[0], 446.02)
        self.assertEqual(f["date"].iloc[-1], "2026-06-30")
        self.assertEqual(f["close"].iloc[-1], 426.97)
        self.assertTrue(f["date"].is_monotonic_increasing)

    def test_future_month_bad_status_and_garbage_are_empty(self):
        self.assertTrue(oi.parse_legacy(_json("tpex_inx_11512.json")).empty)
        bad = _json("tpex_inx_11506.json")
        bad["stat"] = "error"
        self.assertTrue(oi.parse_legacy(bad).empty)
        for junk in (None, [], {"stat": "ok"}, {"tables": ["x"]}, "text"):
            self.assertTrue(oi.parse_legacy(junk).empty, junk)
            self.assertTrue(oi.parse_openapi(junk).empty, junk)

    def test_thousands_separators_and_bad_rows(self):
        p = {"stat": "ok", "tables": [{"data": [
            ["2026/06/01", "1,234.00", "1,240.50", "1,200.00", "1,234.56", "1"],
            ["2026/06/31", "1", "1", "1", "1", "0"],             # no such day
            ["2026/06/02", "1", "1", "1", "--", "0"],            # no close
            ["short"]]}]}
        f = oi.parse_legacy(p)
        self.assertEqual(f["date"].tolist(), ["2026-06-01"])
        self.assertEqual(f["close"].iloc[0], 1234.56)

    def test_openapi(self):
        f = oi.parse_openapi(_json("tpex_index_openapi.json"))
        self.assertEqual(f["date"].tolist(), ["2026-10-01", "2026-10-02",
                                              "2026-10-05", "2026-10-06",
                                              "2026-10-07"])
        self.assertEqual(f["close"].iloc[-1], 430.46)


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "taiex.db"
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE TAIEX (date TEXT, close REAL)")
            c.executemany("INSERT INTO TAIEX VALUES (?,?)",
                          [("2026-10-06", 22000.0), ("2026-10-07", 22100.0)])
        c.close()
        # the rolling trim reads the real clock; pin it so the fixtures'
        # 2026 dates stay inside the window whenever the suite runs
        p = mock.patch.object(ds, "_get_cutoff_date", lambda: "2025-09-03")
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        # storage.data_store.load_sheet leaves its connection to the garbage
        # collector; on Windows the file stays locked until it runs
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        self.tmp.cleanup()

    def run_refresh(self, get, today=TODAY):
        logs = []
        h = oi.refresh(taiex_file=self.db, today=today, get=get,
                       log=logs.append)
        self.assertTrue(logs)
        return h

    def table(self, name):
        conn = sqlite3.connect(self.db)
        try:
            return pd.read_sql_query("SELECT * FROM [{}]".format(name), conn)
        finally:
            conn.close()

    def test_first_run_backfills_and_leaves_taiex_alone(self):
        g = FakeGet()
        h = self.run_refresh(g, today="2026-10-07")
        self.assertTrue(h["ok"], h)
        self.assertIsNone(h["error"])
        # 12 whole past months of the window; the current month from the
        # openapi, which already has today's bar
        self.assertEqual(len(g.months), 12)
        self.assertEqual(g.months[0], "114/10")
        self.assertEqual(g.openapi, 1)
        self.assertNotIn("115/10", g.months)
        tpex = self.table("TPEX")
        self.assertEqual(list(tpex.columns), ["date", "close"])
        self.assertEqual(len(tpex), 21 + 5)
        self.assertEqual(h["rows"], 26)
        self.assertEqual(h["added"], 26)
        self.assertEqual(h["last_date"], "2026-10-07")
        self.assertEqual(tpex["date"].iloc[0], "2026-06-01")
        taiex = self.table("TAIEX")
        self.assertEqual(taiex["close"].tolist(), [22000.0, 22100.0])
        loaded = oi.load(self.db)
        self.assertEqual(loaded["close"].iloc[-1], 430.46)
        # a str path works as well as a Path (load_sheet calls .exists())
        self.assertEqual(oi.load(str(self.db))["close"].iloc[-1], 430.46)
        h2 = oi.refresh(taiex_file=str(self.db), today="2026-10-07",
                        get=FakeGet(), log=lambda *_: None)
        self.assertEqual(h2["rows"], 26, h2)

    def test_the_legacy_month_to_date_tops_up_a_lagging_openapi(self):
        # 15:00 on 10-08: the openapi stops at 10-07, so the legacy
        # month-to-date is asked too (it has no 10-08 either: no error)
        g = FakeGet()
        h = self.run_refresh(g, today=TODAY)
        self.assertEqual(g.months[-1], "115/10")
        self.assertEqual(len(g.months), 13)
        self.assertTrue(h["ok"], h)
        self.assertEqual(h["last_date"], "2026-10-07")

    def test_a_second_run_asks_only_for_what_is_missing(self):
        first = FakeGet(full=True)
        self.run_refresh(first, today="2026-10-07")
        self.assertEqual(len(first.months), 12)
        g = FakeGet(full=True)
        h = self.run_refresh(g, today="2026-10-07")
        self.assertEqual(g.months, [])               # nothing missing
        self.assertEqual(g.openapi, 1)
        self.assertEqual(h["added"], 0)
        self.assertTrue(h["ok"])
        # a month that answered empty is asked again; one with a row is not
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DROP TABLE TPEX")
            conn.commit()
        finally:
            conn.close()
        g = FakeGet()
        self.run_refresh(FakeGet(), today="2026-10-07")
        self.run_refresh(g, today="2026-10-07")
        self.assertNotIn("115/06", g.months)
        self.assertIn("115/07", g.months)

    def test_a_missing_month_end_is_fetched_again(self):
        self.run_refresh(FakeGet(full=True), today="2026-10-07")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM TPEX WHERE date = '2026-09-30'")
            conn.execute("INSERT INTO TPEX VALUES ('2026-09-15', 401.0)")
            conn.commit()
        finally:
            conn.close()
        g = FakeGet(full=True)
        self.run_refresh(g, today="2026-10-07")
        self.assertEqual(g.months, ["115/09"])

    def test_legacy_current_month_when_openapi_lags(self):
        g = FakeGet(fail_openapi=True)
        h = self.run_refresh(g)
        self.assertIn("115/10", g.months)
        self.assertEqual(h["last_date"], "2026-10-07")
        self.assertIn("openapi", h["error"])
        self.assertFalse(h["ok"])

    def test_total_failure_never_raises(self):
        h = self.run_refresh(FakeGet(fail=True))
        self.assertFalse(h["ok"])
        self.assertEqual(h["rows"], 0)
        self.assertTrue(h["error"])
        self.assertTrue(self.table("TAIEX").shape[0] == 2)

        def boom(*a, **k):
            raise ValueError("bad")
        h = oi.refresh(taiex_file=self.db, today=TODAY, get=boom,
                       log=lambda *_: None)
        self.assertFalse(h["ok"])
        h = oi.refresh(taiex_file=self.db, today="not a date", get=boom,
                       log=lambda *_: None)
        self.assertFalse(h["ok"])

    def test_a_dead_legacy_host_costs_one_request_not_one_per_month(self):
        # verifier 2026-10-08: nothing gets stored while the legacy URL
        # fails, so without the stop every run would retry all 12 months
        class LegacyDown(FakeGet):
            def __call__(self, url, params=None):
                if url == oi.LEGACY_URL:
                    self.months.append(params["d"])
                    raise RuntimeError("HTTP 405")
                return FakeGet.__call__(self, url, params)

        g = LegacyDown()
        h = self.run_refresh(g)              # 10-08: openapi lags a day
        self.assertEqual(g.months, ["114/10"])
        self.assertEqual(g.openapi, 1)       # the month-to-date still lands
        self.assertFalse(h["ok"])
        self.assertIn("11 more month(s) left", h["error"])
        self.assertNotIn("legacy current", h["error"])
        self.assertEqual(h["last_date"], "2026-10-07")
        self.assertEqual(h["rows"], 5)
        # the next run asks the first missing month again, nothing else
        g = LegacyDown()
        self.run_refresh(g)
        self.assertEqual(g.months, ["114/10"])

    def test_the_taiex_readers_ignore_the_new_table(self):
        from scanner.live_record import _regime_by_date
        before = _regime_by_date(self.db)
        self.run_refresh(FakeGet())
        self.assertEqual(_regime_by_date(self.db), before)
        self.assertEqual(ds.load_sheet(self.db, "TAIEX")["close"].tolist(),
                         [22000.0, 22100.0])

    def test_month_window(self):
        from datetime import date
        self.assertEqual(oi._months(date(2025, 11, 15), date(2026, 2, 1)),
                         [(2025, 11), (2025, 12), (2026, 1), (2026, 2)])
        self.assertEqual(oi._roc_month(2026, 6), "115/06")
        # 2026-10-10 is a Saturday (and a holiday): September's last session
        self.assertEqual(oi._last_session_of_month(2026, 9), "2026-09-30")


if __name__ == "__main__":
    unittest.main()
