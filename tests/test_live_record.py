"""
scanner/live_record.py: the shipped rule's ACTUAL record on the signals the
live scanner published, built from the signal ledger and the price store.

Fixtures are three temporary SQLite stores shaped like the real ones. Every
population rule in the module docstring gets one stock that exercises it, so
a change that quietly widens or narrows "what the rule buys" fails here.

    python -m unittest tests.test_live_record -v
"""
import json
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
        # the record replays the market leg too, so its rule text says so
        self.assertIn("TAIEX is below its 20MA and above its 60MA", self.rec["rule"])

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


def fwd_frame(rows, start=SIG + 1):
    """Forward bars (o, h, l, c) dated from SESSIONS[start] on."""
    return pd.DataFrame([{"date": SESSIONS[start + k], "open": o, "high": h,
                          "low": l, "close": c}
                         for k, (o, h, l, c) in enumerate(rows)])


FLAT_BAR = (100.0, 101.0, 99.0, 100.0)
# day-10 close (bar 9) under its own 5-bar mean: the stock leg alone exits
WEAK10 = [FLAT_BAR] * 9 + [(100.0, 100.5, 96.5, 97.0), (97.0, 98.0, 96.0, 96.5),
                           (96.5, 97.0, 96.0, 96.6)]


class ReplayTradeIsTheCanonicalBooking(unittest.TestCase):
    """replay_trade (2026-10-08) is the one booking the tracker, the
    recommendation lifecycle and this record share."""

    KEYS = {"exited", "reason", "entry_date", "entry_price", "exit_date",
            "exit_price", "bars", "ret_gross_pct", "ret_net_pct"}

    def test_a_time_exit_is_fully_described(self):
        from scanner.live_record import replay_trade
        t = replay_trade(fwd_frame([FLAT_BAR] * 12))
        self.assertEqual(set(t), self.KEYS)
        self.assertTrue(t["exited"])
        self.assertEqual(t["reason"], "time")
        self.assertEqual(t["entry_date"], SESSIONS[SIG + 1])
        self.assertEqual(t["exit_date"], SESSIONS[SIG + 10])
        self.assertEqual(t["bars"], DEFAULT_RULE["hold_bars"])
        self.assertAlmostEqual(t["entry_price"], 100.0)
        self.assertAlmostEqual(t["exit_price"], 100.0)
        self.assertAlmostEqual(t["ret_gross_pct"], 0.0)
        self.assertAlmostEqual(t["ret_net_pct"], net_pct(0.0))

    def test_the_old_tuple_is_a_thin_wrapper(self):
        from scanner.live_record import _replay, replay_trade
        for rows in ([FLAT_BAR] * 12, [FLAT_BAR] * 4, WEAK10,
                     [FLAT_BAR, (100.0, 100.0, 70.0, 75.0)]):
            f = fwd_frame(rows)
            t = replay_trade(f)
            got = _replay(f)
            if t["exited"]:
                self.assertEqual(got, (t["ret_net_pct"], t["reason"], t["bars"]))
            else:
                self.assertEqual(got, (None, "", len(f)))

    def test_a_short_window_is_open_never_guessed(self):
        from scanner.live_record import replay_trade
        t = replay_trade(fwd_frame([FLAT_BAR] * 4))
        self.assertFalse(t["exited"])
        self.assertEqual(t["reason"], "")
        self.assertIsNone(t["exit_date"])
        self.assertIsNone(t["ret_net_pct"])
        self.assertEqual(t["bars"], 4)
        self.assertEqual(t["entry_date"], SESSIONS[SIG + 1])

    def test_no_priceable_entry_is_na(self):
        from scanner.live_record import replay_trade
        self.assertEqual(replay_trade(fwd_frame([]))["reason"], "na")
        t = replay_trade(fwd_frame([(float("nan"), 101.0, 99.0, 100.0)] + [FLAT_BAR] * 11))
        self.assertEqual(t["reason"], "na")
        self.assertIsNone(t["entry_price"])

    def test_the_market_leg_holds_through_a_disturbed_day(self):
        from scanner.live_record import replay_trade
        f = fwd_frame(WEAK10)
        alone = replay_trade(f)
        self.assertEqual((alone["reason"], alone["exit_date"]), ("time", SESSIONS[SIG + 10]))
        marks = {SESSIONS[SIG + 10]}
        held = replay_trade(f, extend_if=lambda i, d: d in marks)
        self.assertEqual((held["reason"], held["exit_date"]), ("time", SESSIONS[SIG + 11]))
        self.assertAlmostEqual(held["exit_price"], 96.5)
        self.assertEqual(held["bars"], DEFAULT_RULE["hold_bars"] + 1)


class TheRecordCarriesDatesAndTheMarketLeg(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        import gc
        gc.collect()
        self.fx.tmp.cleanup()

    def test_rows_carry_entry_and_exit_dates(self):
        rec = self.fx.build()
        by = {x["sid"]: x for x in rec["tradable"]["trades"]}
        self.assertEqual(by["4444"]["entry_date"], SESSIONS[SIG + 1])
        self.assertEqual(by["4444"]["exit_date"], SESSIONS[SIG + 10])
        self.assertEqual(by["1111"]["exit_date"], SESSIONS[SIG + 1])
        self.assertEqual(by["6666"]["entry_date"], SESSIONS[SIG + 1])
        self.assertIsNone(by["6666"]["exit_date"])

    def test_the_record_replays_with_the_market_leg(self):
        # 4444's day-10 close is flat (not above its mean); a disturbed market
        # on that day keeps it one more bar, as the tracker would
        from unittest import mock
        import scanner.market_leg as ml
        marks = {SESSIONS[SIG + 10]}

        def fake(taiex_file=None):
            return lambda i, d: d in marks
        with mock.patch.object(ml, "make_disturbed_fn", fake):
            rec = self.fx.build()
        by = {x["sid"]: x for x in rec["tradable"]["trades"]}
        self.assertEqual(by["4444"]["exit"], "time")
        self.assertEqual(by["4444"]["exit_date"], SESSIONS[SIG + 11])
        self.assertEqual(by["4444"]["bars"], DEFAULT_RULE["hold_bars"] + 1)



class RestrictionFixture(Fixture):
    """The same ledger with the 2026-10-08 gate_detail column: 1111 was in
    disposition on its signal day, 4444 (hypothetically) suspended, the rest
    predate the field."""

    def _ledger(self):
        super()._ledger()
        with sqlite3.connect(self.ledger) as c:
            c.execute("ALTER TABLE picks ADD COLUMN gate_detail TEXT")
            c.execute("UPDATE picks SET gate_detail = ? WHERE stock_id = '1111'",
                      (json.dumps({"hold_status": "pending",
                                   "restriction": "disposition",
                                   "restriction_until": D2}),))
            c.execute("UPDATE picks SET gate_detail = ? WHERE stock_id = '4444' "
                      "AND bar_date = ?",
                      (json.dumps({"restriction": "suspended"}), D1))
            c.execute("UPDATE picks SET gate_detail = 'not json' "
                      "WHERE stock_id = '2222'")


class TheRecordCountsRestrictions(unittest.TestCase):
    """live_record exposes the restriction on each signal and never drops one
    unless asked (DECISIONS 'Trade restrictions')."""

    @classmethod
    def setUpClass(cls):
        cls.fx = RestrictionFixture()

    @classmethod
    def tearDownClass(cls):
        import gc
        gc.collect()
        cls.fx.tmp.cleanup()

    def test_nothing_is_excluded_by_default(self):
        rec = self.fx.build()
        self.assertEqual(rec["candidates"], 5)
        self.assertEqual(rec["restricted_excluded"], 0)
        self.assertEqual(rec["restrictions"],
                         {"disposition": 1, "suspended": 1, "unrecorded": 3})
        t = rec["tradable"]
        self.assertEqual(sorted(x["sid"] for x in t["trades"]), ["1111", "4444", "6666"])
        by = {x["sid"]: x for x in t["trades"]}
        self.assertEqual(by["1111"]["restriction"], "disposition")
        self.assertIsNone(by["6666"]["restriction"])
        self.assertEqual(rec["by_restriction"]["disposition"]["closed"], 1)
        self.assertEqual(rec["by_restriction"]["disposition"]["win_pct"], 100.0)
        self.assertEqual(rec["by_restriction"]["suspended"]["closed"], 1)
        self.assertEqual(rec["by_restriction"]["unrecorded"]["open"], 1)
        json.dumps(rec)                       # JSON-ready

    def test_excluding_the_blocking_set_drops_and_counts(self):
        rec = self.fx.build(exclude_restrictions=("suspended",))
        self.assertEqual(rec["restricted_excluded"], 1)
        self.assertEqual(sorted(x["sid"] for x in rec["tradable"]["trades"]),
                         ["1111", "6666"])
        self.assertNotIn("suspended", rec["by_restriction"])
        # still counted among the candidates it was found in
        self.assertEqual(rec["restrictions"]["suspended"], 1)

    def test_an_old_ledger_without_gate_detail_still_reads(self):
        fx = Fixture()
        try:
            rec = fx.build()
            self.assertEqual(rec["restrictions"], {"unrecorded": 5})
            self.assertEqual(set(rec["by_restriction"]), {"unrecorded"})
        finally:
            import gc
            gc.collect()
            fx.tmp.cleanup()


# --------------------------------------------------------------------------
# per-name history and the index benchmark (2026-10-08, display only)
# --------------------------------------------------------------------------
OTC_CLOSES = [400.0 + 0.5 * i for i in range(len(SESSIONS))]


class BenchFixture(Fixture):
    """The same stores plus the OTC index ('TPEX') on every session. TAIEX
    still stops at D2 (the crash bar), so a trade that exits later has no
    TAIEX window -- a lagging index must read as None, not as flat."""

    def _taiex(self):
        super()._taiex()
        with sqlite3.connect(self.taiex) as c:
            c.execute("CREATE TABLE TPEX (date TEXT, close REAL)")
            c.executemany("INSERT INTO TPEX VALUES (?,?)",
                          list(zip(SESSIONS, OTC_CLOSES)))


class TwiceFixture(Fixture):
    """1111 is also on the list two sessions before D1 (not CORE+); a TSE
    pick makes SIG-1 a session it is absent from, so D1 is a fresh signal
    again and 1111 has two entries."""

    def _ledger(self):
        super()._ledger()
        with sqlite3.connect(self.ledger) as c:
            c.executemany("INSERT INTO picks VALUES (?,?,?,?,?,?,?,?,?,?)", [
                ("s0", "t0", "mode_prelaunch", "1111", "A", 7,
                 SESSIONS[SIG - 2], "OTC", 0, 0),
                ("s0b", "t0b", "mode_prelaunch", "5555", "E", 1,
                 SESSIONS[SIG - 1], "TSE", 1, 0),
            ])


def _cleanup(fx):
    import gc
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()
    fx.tmp.cleanup()


def _pct(a, b):
    return round((b / a - 1.0) * 100.0, 2)


class TheRecordHasABenchmark(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = BenchFixture()
        cls.rec = cls.fx.build()

    @classmethod
    def tearDownClass(cls):
        _cleanup(cls.fx)

    def test_both_indices_over_the_whole_span(self):
        b = self.rec["bench"]
        self.assertEqual((b["from"], b["to"]), (SESSIONS[0], D2))
        self.assertEqual((b["taiex_from"], b["taiex_to"]), (1000.0, 500.0))
        self.assertEqual(b["taiex_pct"], -50.0)
        self.assertEqual((b["otc_from_date"], b["otc_to_date"]), (SESSIONS[0], D2))
        self.assertEqual(b["otc_pct"], _pct(OTC_CLOSES[0], OTC_CLOSES[SIG + 1]))

    def test_each_closed_trade_carries_its_own_window(self):
        by = {x["sid"]: x for x in self.rec["tradable"]["trades"]}
        # 1111: signal-day close (D1) -> exit-day close (D2)
        self.assertEqual(by["1111"]["taiex_pct"], _pct(1000.0 + SIG, 500.0))
        self.assertEqual(by["1111"]["otc_pct"],
                         _pct(OTC_CLOSES[SIG], OTC_CLOSES[SIG + 1]))
        # 4444 exits on day 10; TAIEX has no bar there
        self.assertIsNone(by["4444"]["taiex_pct"])
        self.assertEqual(by["4444"]["otc_pct"],
                         _pct(OTC_CLOSES[SIG], OTC_CLOSES[SIG + 10]))
        # an open trade has no window at all
        self.assertNotIn("otc_pct", by["6666"])
        w = self.rec["bench"]["windows"]
        self.assertEqual((w["n"], w["taiex_n"], w["otc_n"]), (2, 1, 2))
        self.assertAlmostEqual(w["otc_mean_pct"],
                               round((by["1111"]["otc_pct"]
                                      + by["4444"]["otc_pct"]) / 2, 2), places=2)
        self.assertAlmostEqual(w["trade_mean_pct"],
                               round((by["1111"]["ret"] + by["4444"]["ret"]) / 2, 2),
                               places=2)

    def test_each_index_mean_has_its_own_trade_mean(self):
        # verifier 2026-10-08: 4444 has no TAIEX window, so taiex_mean_pct
        # covers 1111 only -- it must be read against 1111's return, not
        # against the mean over both trades
        by = {x["sid"]: x for x in self.rec["tradable"]["trades"]}
        w = self.rec["bench"]["windows"]
        self.assertAlmostEqual(w["taiex_trade_mean_pct"],
                               round(by["1111"]["ret"], 2), places=2)
        self.assertAlmostEqual(w["taiex_trade_sum_pct"],
                               round(by["1111"]["ret"], 2), places=2)
        self.assertAlmostEqual(w["otc_trade_mean_pct"], w["trade_mean_pct"],
                               places=2)
        self.assertNotAlmostEqual(w["taiex_trade_mean_pct"],
                                  w["trade_mean_pct"], places=2)

    def test_the_series_is_thinned_and_ordered(self):
        s = self.rec["bench"]["series"]
        from scanner.live_record import BENCH_POINTS
        self.assertLessEqual(len(s), BENCH_POINTS)
        self.assertEqual(s[0][0], SESSIONS[0])
        self.assertEqual(s[-1], [D2, 500.0, OTC_CLOSES[SIG + 1]])
        days = [p[0] for p in s]
        self.assertEqual(days, sorted(set(days)))
        json.dumps(self.rec)

    def test_without_the_otc_table_the_otc_side_is_null(self):
        fx = Fixture()
        try:
            rec = fx.build()
            b = rec["bench"]
            self.assertEqual(b["taiex_pct"], -50.0)
            for k in ("otc_from", "otc_to", "otc_pct", "otc_from_date"):
                self.assertIsNone(b[k], k)
            self.assertTrue(all(p[2] is None for p in b["series"]))
            self.assertEqual(b["windows"]["otc_n"], 0)
            self.assertIsNone(b["windows"]["otc_mean_pct"])
            self.assertIsNone(b["windows"]["otc_trade_mean_pct"])
        finally:
            _cleanup(fx)

    def test_no_index_at_all_is_no_bench(self):
        fx = Fixture()
        try:
            conn = sqlite3.connect(fx.taiex)
            try:
                conn.execute("DROP TABLE TAIEX")
                conn.commit()
            finally:
                conn.close()
            rec = fx.build()
            self.assertIsNone(rec["bench"])
        finally:
            _cleanup(fx)

    def test_an_empty_ledger_has_no_history_and_no_crash(self):
        rec = build_live_record(since="2099-01-01", ledger_file=self.fx.ledger,
                                price_file=self.fx.price, taiex_file=self.fx.taiex)
        self.assertEqual(rec["by_sid"], {})
        self.assertIn("bench", rec)


class TheRecordListsEachNamesHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = TwiceFixture()

    @classmethod
    def tearDownClass(cls):
        _cleanup(cls.fx)

    def test_every_bucket_is_listed_oldest_first(self):
        rec = self.fx.build()
        bs = rec["by_sid"]
        self.assertEqual(sorted(bs), ["1111", "2222", "3333", "4444", "6666"])
        self.assertEqual([(e["sig"], e["bucket"]) for e in bs["1111"]],
                         [(SESSIONS[SIG - 2], "not_core"), (D1, "tradable")])
        self.assertEqual(bs["1111"][1]["exit"], "tp")
        self.assertEqual(bs["1111"][0]["rank"], 7)
        self.assertEqual(bs["3333"][0]["bucket"], "regime_closed")
        self.assertEqual(bs["2222"][0]["bucket"], "not_core")
        self.assertIsNone(bs["6666"][0]["ret"])
        keys = {"sig", "bucket", "rank", "ret", "exit", "bars", "entry_date",
                "exit_date", "restriction"}
        for lst in bs.values():
            for e in lst:
                self.assertEqual(set(e), keys)
        json.dumps(rec)

    def test_limited_to_the_names_in_the_payload(self):
        rec = self.fx.build(sids={"1111", "3333", "9999"})
        self.assertEqual(sorted(rec["by_sid"]), ["1111", "3333"])
        self.assertEqual(self.fx.build(sids=[])["by_sid"], {})
        # the summary buckets are never limited
        self.assertEqual(rec["tradable"]["closed"],
                         self.fx.build()["tradable"]["closed"])

    def test_capped_to_the_newest(self):
        from unittest import mock
        import scanner.live_record as lr
        with mock.patch.object(lr, "BY_SID_KEPT", 1):
            rec = self.fx.build(sids={"1111"})
        self.assertEqual([e["sig"] for e in rec["by_sid"]["1111"]], [D1])


class TheExportCarriesTheEvents(unittest.TestCase):
    def test_meta_events_is_written_and_carried_forward(self):
        from unittest import mock
        import scanner.result_export as ex
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(ex, "MOBILE_DIR", root), \
                 mock.patch.object(ex, "MOBILE_DATA_FILE", root / "data.json"), \
                 mock.patch.object(ex, "MOBILE_QUOTES_FILE", root / "quotes.json"):
                df = pd.DataFrame([{"Stock_ID": "1111", "Market": "OTC",
                                    "Close_Price": 100.0, "Data_Date": "2026-10-07"}])
                ev = {"ok": True, "session_date": "2026-10-07",
                      "revenue_month_latest": "2026-09"}
                ex.export_scan_result_json(df, "mode_prelaunch",
                                           "2026-10-07 15:00:00", events_meta=ev)
                got = json.loads((root / "data.json").read_text(encoding="utf-8"))
                self.assertEqual(got["meta"]["events"]["revenue_month_latest"],
                                 "2026-09")
                self.assertNotIn("carried_forward", got["meta"]["events"])
                ex.export_scan_result_json(df, "mode_prelaunch",
                                           "2026-10-07 15:01:00")
                got = json.loads((root / "data.json").read_text(encoding="utf-8"))
                self.assertEqual(got["meta"]["events"]["revenue_month_latest"],
                                 "2026-09")
                self.assertTrue(got["meta"]["events"]["carried_forward"])


if __name__ == "__main__":
    unittest.main()
