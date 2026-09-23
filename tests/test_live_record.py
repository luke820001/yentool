"""
scanner/live_record.py: the shipped rule's ACTUAL record on the signals the
live scanner published, built from the signal ledger and the price store.

Fixtures are three temporary SQLite stores shaped like the real ones. Every
population rule in the module docstring gets one stock that exercises it, so
a change that quietly widens or narrows "what the rule buys" fails here.

    python -m unittest tests.test_live_record -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scanner.exit_rules import DEFAULT_RULE
from scanner.live_record import build_live_record, net_pct

SESSIONS = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=140)]
SIG = 125                       # index of the first signal day (D1)
D1, D2 = SESSIONS[SIG], SESSIONS[SIG + 1]


def price_rows(sid, closes, spread=0.01, opens=None):
    rows = []
    for i, c in enumerate(closes):
        o = opens[i] if opens and i < len(opens) and opens[i] is not None else c
        rows.append((SESSIONS[i], sid, o, round(c * (1 + spread), 2),
                     round(c * (1 - spread), 2), c, 100.0))
    return rows


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.price = root / "price_volume.db"      # stem is stock-keyed
        self.taiex = root / "taiex.db"
        self.ledger = root / "signal_ledger.db"
        self._prices()
        self._taiex()
        self._ledger()

    def _prices(self):
        flat = [100.0] * (SIG + 1)
        rows = []
        # 1111: tradable, gaps to the target on the first forward bar
        rows += price_rows("1111", flat + [100.0, 100.0, 100.0],
                           opens=[None] * (SIG + 1) + [100.0])
        with_target = [r for r in rows if r[1] == "1111"]
        i = SIG + 1
        r = list(with_target[i]); r[3] = 125.0; rows[i] = tuple(r)
        # 2222: on the list, fails CORE+, then crashes through the stop
        rows += price_rows("2222", flat + [100.0, 70.0, 70.0])
        # 3333: CORE+ but signalled on the day the market gate is shut
        rows += price_rows("3333", flat + [100.0] * 12)
        # 4444: CORE+ on D1 AND on D2 -- only the first day is a signal;
        # flat forward bars -> time exit
        rows += price_rows("4444", flat + [100.0] * 12)
        # 5555: TSE, never in the population
        rows += price_rows("5555", flat + [100.0, 130.0])
        # 6666: no core_plus in the ledger (pre-2026-09-09 row); recomputed
        # from the bars (5% daily range -> ATR passes), three forward bars
        # only -> still open
        rows += price_rows("6666", flat + [100.0, 100.0, 100.0], spread=0.025)
        with sqlite3.connect(self.price) as c:
            c.execute("CREATE TABLE data (date TEXT, stock_id TEXT, open REAL, "
                      "high REAL, low REAL, close REAL, Volume_Lot REAL)")
            c.executemany("INSERT INTO data VALUES (?,?,?,?,?,?,?)", rows)

    def _taiex(self):
        closes = [1000.0 + i for i in range(SIG + 1)]     # rising: gate open on D1
        closes.append(500.0)                               # D2: far below both means
        with sqlite3.connect(self.taiex) as c:
            c.execute("CREATE TABLE TAIEX (date TEXT, close REAL)")
            c.executemany("INSERT INTO TAIEX VALUES (?,?)",
                          list(zip(SESSIONS[:len(closes)], closes)))

    def _ledger(self):
        picks = [
            # scan_session, scan_ts, mode, sid, name, rank, bar_date, market, core, ready
            ("s1", "t1", "mode_prelaunch", "1111", "A", 1, D1, "OTC", 1, 1),
            ("s1", "t1", "mode_prelaunch", "2222", "B", 2, D1, "OTC", 0, 0),
            ("s2", "t2", "mode_prelaunch", "3333", "C", 3, D2, "OTC", 1, 0),
            ("s1", "t1", "mode_prelaunch", "4444", "D", 4, D1, "OTC", 1, 1),
            ("s2", "t2", "mode_prelaunch", "4444", "D", 2, D2, "OTC", 1, 0),
            ("s1", "t1", "mode_prelaunch", "5555", "E", 1, D1, "TSE", 1, 0),
            ("s1", "t1", "mode_prelaunch", "6666", "F", 5, D1, "OTC", None, None),
            # another mode never counts
            ("s1", "t1", "mode_bottom", "1111", "A", 1, D1, "OTC", 1, 1),
        ]
        with sqlite3.connect(self.ledger) as c:
            c.execute("CREATE TABLE picks (scan_session TEXT, scan_ts TEXT, "
                      "scan_mode TEXT, stock_id TEXT, stock_name TEXT, rank INTEGER, "
                      "bar_date TEXT, market TEXT, core_plus INTEGER, buy_ready INTEGER)")
            c.executemany("INSERT INTO picks VALUES (?,?,?,?,?,?,?,?,?,?)", picks)

    def build(self, **kw):
        return build_live_record(since=SESSIONS[0], ledger_file=self.ledger,
                                 price_file=self.price, taiex_file=self.taiex, **kw)


class ThePopulationIsTheBuyRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.rec = cls.fx.build()

    @classmethod
    def tearDownClass(cls):
        # storage.data_store.load_sheet leaves its connection to the garbage
        # collector; on Windows the file stays locked until it runs
        import gc
        gc.collect()
        cls.fx.tmp.cleanup()

    def test_candidates_are_first_day_otc_top20_only(self):
        # 1111, 2222, 3333, 4444 (D1 only), 6666 -- not 5555 (TSE), not the
        # D2 repeat of 4444, not the other mode's row
        self.assertEqual(self.rec["candidates"], 5)

    def test_three_buckets(self):
        t, nc, rc = self.rec["tradable"], self.rec["not_core"], self.rec["regime_closed"]
        self.assertEqual((t["closed"], t["open"]), (2, 1))       # 1111, 4444; 6666 open
        self.assertEqual((nc["closed"], nc["open"]), (1, 0))     # 2222
        self.assertEqual((rc["closed"], rc["open"]), (1, 0))     # 3333 on D2
        self.assertEqual(sorted(x["sid"] for x in t["trades"]), ["1111", "4444", "6666"])

    def test_returns_are_net_of_costs_and_use_the_whole_rule(self):
        by = {x["sid"]: x for x in self.rec["tradable"]["trades"]}
        self.assertEqual(by["1111"]["exit"], "tp")
        self.assertAlmostEqual(by["1111"]["ret"],
                               round(net_pct(DEFAULT_RULE["tp_pct"] * 100), 2), places=2)
        # flat forward bars: day-10 close is not above its 5-bar mean, so no
        # ride; the time exit costs exactly the round trip
        self.assertEqual(by["4444"]["exit"], "time")
        self.assertEqual(by["4444"]["bars"], DEFAULT_RULE["hold_bars"])
        self.assertAlmostEqual(by["4444"]["ret"], round(net_pct(0.0), 2), places=2)
        self.assertIsNone(by["6666"]["ret"])
        self.assertEqual(by["6666"]["exit"], "")

    def test_core_is_recomputed_only_when_the_ledger_has_none(self):
        self.assertEqual(self.rec["core_recomputed"], 1)      # 6666, < 240 bars
        self.assertEqual(self.rec["core_unknown"], 0)

    def test_summary_numbers(self):
        t = self.rec["tradable"]
        self.assertEqual(t["win_pct"], 50.0)
        self.assertAlmostEqual(
            t["mean_pct"],
            round((net_pct(DEFAULT_RULE["tp_pct"] * 100) + net_pct(0.0)) / 2, 2),
            places=1)
        self.assertEqual(self.rec["through"], D2)
        self.assertIn("ride to {}".format(DEFAULT_RULE["ride_cap"]), self.rec["rule"])

    def test_an_empty_ledger_is_an_empty_record_not_an_error(self):
        rec = build_live_record(since="2099-01-01", ledger_file=self.fx.ledger,
                                price_file=self.fx.price, taiex_file=self.fx.taiex)
        self.assertEqual(rec["candidates"], 0)
        self.assertEqual(rec["tradable"]["closed"], 0)


class TheRecordReachesThePhone(unittest.TestCase):
    def test_the_export_carries_it_and_keeps_the_previous_one(self):
        """The desktop export has no record; it must not strip the cloud's."""
        import json
        from unittest import mock
        import scanner.result_export as ex
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(ex, "MOBILE_DIR", root), \
                 mock.patch.object(ex, "MOBILE_DATA_FILE", root / "data.json"), \
                 mock.patch.object(ex, "MOBILE_QUOTES_FILE", root / "quotes.json"):
                df = pd.DataFrame([{"Stock_ID": "1111", "Market": "OTC",
                                    "Close_Price": 100.0, "Data_Date": "2026-09-22"}])
                rec = {"since": "2026-06-25", "tradable": {"closed": 1, "open": 0}}
                ex.export_scan_result_json(df, "mode_prelaunch", "2026-09-22 15:00:00",
                                           live_record=rec)
                got = json.loads((root / "data.json").read_text(encoding="utf-8"))
                self.assertEqual(got["meta"]["live_record"]["since"], "2026-06-25")
                ex.export_scan_result_json(df, "mode_prelaunch", "2026-09-22 15:01:00")
                got = json.loads((root / "data.json").read_text(encoding="utf-8"))
                self.assertEqual(got["meta"]["live_record"]["since"], "2026-06-25")
                self.assertTrue(got["meta"]["live_record"]["carried_forward"])

    def test_the_phone_renders_all_three_buckets(self):
        src = (Path(__file__).resolve().parent.parent / "mobile" / "app.js").read_text(encoding="utf-8")
        i = src.find("function liveRecordHtml(")
        self.assertGreater(i, 0)
        body = src[i:src.find("function strategyCardHtml(", i)]
        for key in ("rec.tradable", "rec.not_core", "rec.regime_closed", "carried_forward"):
            self.assertIn(key, body)
        self.assertIn("liveRecordHtml()", src[src.find("function strategyCardHtml("):][:600])


if __name__ == "__main__":
    unittest.main()
