"""
Pins from the 2026-10-09 conformance audit, M-26: the universe's names and
boards come from the two daily feeds, not from a cache of codes seen before.

    python -m unittest tests.test_failclosed_universe_20261009 -v

ASCII only (names in the fixtures are plain Latin).
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scanner import market_snapshot as ms
from scanner import universe_export as ue


def _price_db(path, sids, days=3):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, open REAL, "
                     "high REAL, low REAL, close REAL, Volume_Lot REAL)")
        for sid in sids:
            for i in range(days):
                conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?,?)",
                             (sid, "2026-10-0%d" % (5 + i), 10, 11, 9, 10, 100))
        conn.commit()
    finally:
        conn.close()


class FeedNames(unittest.TestCase):
    TSE = [{"Code": "2330", "Name": "TSMC"},
           {"Code": "00400A", "Name": "An ETF"},
           {"Code": "030001", "Name": "A warrant"},          # six digits: dropped
           {"Code": "1101", "Name": ""},                     # no name: skipped
           "not a dict"]
    OTC = [{"SecuritiesCompanyCode": "6488", "CompanyName": "GlobalWafers"},
           {"SecuritiesCompanyCode": "009821", "CompanyName": "Another ETF"},
           {"SecuritiesCompanyCode": "12345A", "CompanyName": "Junk"}]

    def test_every_listed_instrument_is_named_with_its_board(self):
        t = ms.parse_names(self.TSE, "TSE")
        o = ms.parse_names(self.OTC, "OTC")
        self.assertEqual(t, {"2330": ["TSMC", "TSE"], "00400A": ["An ETF", "TSE"]})
        self.assertEqual(o, {"6488": ["GlobalWafers", "OTC"],
                             "009821": ["Another ETF", "OTC"]})

    def test_a_missing_feed_names_nothing(self):
        self.assertEqual(ms.parse_names(None, "TSE"), {})
        self.assertEqual(ms.parse_names([], "OTC"), {})


class NameCacheRefresh(unittest.TestCase):
    def _path(self, tmp, content=None):
        p = Path(tmp) / "names.json"
        if content is not None:
            p.write_text(json.dumps(content), encoding="utf-8")
        return p

    def test_feed_wins_and_unlisted_codes_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._path(tmp, {"2330": ["Old", "TSE"], "9999": ["Delisted", "OTC"]})
            out = ms.refresh_name_cache(p, {"2330": ["New", "TSE"],
                                            "6488": ["GW", "OTC"]}, log=lambda *a: None)
            self.assertEqual(out["2330"], ["New", "TSE"])
            self.assertEqual(out["9999"], ["Delisted", "OTC"])      # never shrinks
            self.assertEqual(out["6488"], ["GW", "OTC"])
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), out)

    def test_a_code_that_changed_board_follows_the_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._path(tmp, {"2938": ["X", "OTC"]})
            out = ms.refresh_name_cache(p, {"2938": ["X", "TSE"]}, log=lambda *a: None)
            self.assertEqual(out["2938"][1], "TSE")

    def test_nothing_fresh_changes_nothing_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._path(tmp, {"2330": ["TSMC", "TSE"]})
            before = p.stat().st_mtime_ns
            out = ms.refresh_name_cache(p, None, log=lambda *a: None)
            self.assertEqual(out, {"2330": ["TSMC", "TSE"]})
            self.assertEqual(p.stat().st_mtime_ns, before)
            ms.refresh_name_cache(p, {"2330": ["TSMC", "TSE"]}, log=lambda *a: None)
            self.assertEqual(p.stat().st_mtime_ns, before)           # identical: no write

    def test_an_unreadable_cache_is_rebuilt_from_the_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "names.json"
            p.write_text("{not json", encoding="utf-8")
            out = ms.refresh_name_cache(p, {"2330": ["TSMC", "TSE"]}, log=lambda *a: None)
            self.assertEqual(out, {"2330": ["TSMC", "TSE"]})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), out)
            self.assertFalse(Path(str(p) + ".tmp").exists())

    def test_a_missing_cache_and_no_feed_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = ms.refresh_name_cache(Path(tmp) / "none.json", None, log=lambda *a: None)
            self.assertEqual(out, {})


class UniverseBoard(unittest.TestCase):
    def test_board_from_the_snapshot_fills_a_code_the_cache_does_not_know(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pv.db"
            _price_db(db, ["2330", "7812"])
            recs = ue.build(db, names={"2330": ["TSMC", "TSE"]},
                            boards={"2330": "TSE", "7812": "OTC"})
            self.assertEqual(recs["2330"]["Market"], "TSE")
            self.assertEqual(recs["7812"]["Market"], "OTC")           # was ""
            self.assertEqual(recs["7812"]["Stock_Name"], "7812")      # name still unknown

    def test_the_cached_board_wins_when_it_has_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pv.db"
            _price_db(db, ["2330"])
            recs = ue.build(db, names={"2330": ["TSMC", "TSE"]}, boards={"2330": "OTC"})
            self.assertEqual(recs["2330"]["Market"], "TSE")

    def test_export_counts_and_reports_the_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pv.db"
            _price_db(db, ["2330", "7812", "7825"])
            out = Path(tmp) / "universe.json"
            said = []
            n = ue.export(out, db, names={"2330": ["TSMC", "TSE"]},
                          boards={"7812": "OTC"}, log=said.append)
            self.assertEqual(n, 3)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["blank_market"], 1)              # 7825: no source at all
            self.assertEqual(payload["unnamed"], 2)                   # 7812 and 7825
            self.assertTrue(any("7825" in s for s in said), said)

    def test_a_clean_universe_says_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pv.db"
            _price_db(db, ["2330"])
            out = Path(tmp) / "universe.json"
            said = []
            ue.export(out, db, names={"2330": ["TSMC", "TSE"]}, log=said.append)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual((payload["blank_market"], payload["unnamed"]), (0, 0))
            self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
