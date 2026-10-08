"""
Tests for the scan-side fixes: the buy gate (F06), the frozen first-day
recommendation (F01), the quote feed (F04) and zero-pick publishing (F18).

Stdlib unittest; pandas is already a hard dependency of the scanner.

    python -m unittest discover -s tests -t . -v
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import scanner.market_regime as market_regime
from scanner.scan_mode import mark_buy_ready, _safe_bool, STRATEGY_VERSION
from scanner.quote_feed import build_quote_feed
from portfolio.sync import attach_recommendations

MODE = "mode_prelaunch"
TODAY = "2026-09-09"


def frame(**overrides):
    """One row that passes every gate, so each test can break exactly one."""
    row = {
        "Stock_ID": "8069", "Stock_Name": "TestCo", "Market": "OTC",
        "Data_Date": TODAY, "Close_Price": 100.0,
        "Suggested_Buy_Price": 100.0, "Strict_Stop_Loss": 85.0,
        "Target_Price": 120.0, "Trail_Arm_Price": 106.0,
        "Trail_Lock_Price": 102.0, "Core_Plus": True, "Integrity_OK": True,
        "Hold_Status": "pending", "Launch_Score": 80.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


class GateCase(unittest.TestCase):
    """Patches the market regime, which mark_buy_ready imports at call time."""

    def setUp(self):
        self._real = market_regime.get_market_regime
        market_regime.get_market_regime = lambda: {
            "ok": True, "enter_ok": True, "risk_on": True, "is_current": True}

    def tearDown(self):
        market_regime.get_market_regime = self._real

    def block_of(self, df):
        out = mark_buy_ready(df, MODE, session_date=TODAY)
        return bool(out["Buy_Ready"].iloc[0]), out["Buy_Block"].iloc[0]


class TestBuyGate(GateCase):
    """F06: data faults must block a buy, each with its own reason."""

    def test_clean_row_is_buyable(self):
        ready, block = self.block_of(frame())
        self.assertTrue(ready)
        self.assertEqual(block, "")

    def test_stale_bar_blocks(self):
        ready, block = self.block_of(frame(Data_Date="2026-09-04"))
        self.assertFalse(ready)
        self.assertEqual(block, "stale")

    def test_failed_integrity_blocks(self):
        ready, block = self.block_of(frame(Integrity_OK=False))
        self.assertFalse(ready)
        self.assertEqual(block, "integrity")

    def test_unknown_hold_status_blocks(self):
        """The old rule read `status.isin(("", "pending"))`, so a row whose
        hold state could not be computed counted as a fresh signal."""
        ready, block = self.block_of(frame(Hold_Status=""))
        self.assertFalse(ready)
        self.assertEqual(block, "unknown")

    def test_already_held_blocks(self):
        ready, block = self.block_of(frame(Hold_Status="holding"))
        self.assertFalse(ready)
        self.assertEqual(block, "held")

    def test_nan_core_plus_blocks_instead_of_passing(self):
        """Report section 15's minimum repro: Core_Plus = NaN reached
        Buy_Ready = True, because float('nan') is truthy."""
        ready, block = self.block_of(frame(Core_Plus=float("nan")))
        self.assertFalse(ready)
        self.assertEqual(block, "quality")

    def test_safe_bool_treats_nan_as_false(self):
        df = pd.DataFrame({"flag": [True, False, float("nan")]})
        self.assertEqual(list(_safe_bool(df, "flag")), [True, False, False])
        self.assertEqual(list(_safe_bool(df, "absent")), [False, False, False])

    def test_missing_market_column_does_not_raise(self):
        df = frame()
        del df["Market"]
        ready, block = self.block_of(df)
        self.assertFalse(ready)
        self.assertEqual(block, "market")

    def test_unreadable_regime_blocks_everything(self):
        market_regime.get_market_regime = lambda: {"ok": False, "risk_on": True}
        ready, block = self.block_of(frame())
        self.assertFalse(ready)
        self.assertEqual(block, "regime")

    def test_stale_regime_blocks_even_when_it_reads_green(self):
        """F15: a tailwind computed from a months-old TAIEX cache is not one.

        The VETO is the point and it is unchanged. What changed on 2026-09-22
        is the reason code: "regime" asserts the index is below its 20/60-day
        averages, and on 2026-09-21 that assertion was false -- the index had
        closed above both, the feed simply had not published the bar yet, and
        all 46 rows were blocked while both screens told the owner the market
        had not reclaimed its averages. A stale feed says "regime_stale".
        """
        market_regime.get_market_regime = lambda: {
            "ok": True, "enter_ok": True, "risk_on": True, "is_current": False}
        ready, block = self.block_of(frame())
        self.assertFalse(ready)
        self.assertEqual(block, "regime_stale")

    def test_a_genuinely_closed_market_still_says_regime(self):
        market_regime.get_market_regime = lambda: {
            "ok": True, "enter_ok": False, "risk_on": False, "is_current": True}
        ready, block = self.block_of(frame())
        self.assertFalse(ready)
        self.assertEqual(block, "regime")

    def test_both_reasons_are_registered_and_rendered(self):
        """A code the screens cannot render shows as a raw identifier."""
        from pathlib import Path
        from scanner.result_checks import BUY_BLOCKS
        root = Path(__file__).resolve().parent.parent
        self.assertIn("regime_stale", BUY_BLOCKS)
        for rel in ("mobile/app.js", "gui/app.py"):
            self.assertIn("regime_stale",
                          (root / rel).read_text(encoding="utf-8"),
                          "%s cannot render the new reason" % rel)

    def test_regime_without_freshness_keys_still_works(self):
        """Older callers return no is_current; absent must not mean stale."""
        market_regime.get_market_regime = lambda: {"ok": True, "enter_ok": True}
        ready, _ = self.block_of(frame())
        self.assertTrue(ready)

    def test_unsupported_mode_is_never_buyable(self):
        out = mark_buy_ready(frame(), "mode_squeeze", session_date=TODAY)
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "no_rule")


class TestRecommendationFreeze(GateCase):
    """F01 end to end, through the scan's own entry point."""

    def setUp(self):
        super(TestRecommendationFreeze, self).setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "portfolio_ledger.db"

    def tearDown(self):
        super(TestRecommendationFreeze, self).tearDown()
        self.tmp.cleanup()

    def scan(self, df, session):
        df = mark_buy_ready(df, MODE, session_date=session)
        return attach_recommendations(df, MODE, STRATEGY_VERSION, self.ledger,
                                      session_date=session)

    def test_first_qualifying_day_fixes_the_price(self):
        out, stats = self.scan(frame(), TODAY)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(out["Initial_Buy_Price"].iloc[0], 100.0)
        self.assertEqual(out["Recommended_On"].iloc[0], TODAY)

        # Day two: the stock has run to 118, so add_trade_columns would now
        # suggest 118. The recommendation must still say 100.
        later = frame(Close_Price=118.0, Suggested_Buy_Price=118.0,
                      Strict_Stop_Loss=100.3, Data_Date="2026-09-10",
                      Hold_Status="holding")
        out2, stats2 = self.scan(later, "2026-09-10")
        self.assertEqual(stats2["created"], 0)
        self.assertEqual(out2["Initial_Buy_Price"].iloc[0], 100.0)
        self.assertEqual(out2["Suggested_Buy_Price"].iloc[0], 118.0)
        self.assertEqual(out2["Recommendation_ID"].iloc[0],
                         out["Recommendation_ID"].iloc[0])

    def test_three_scans_in_one_day_create_one_recommendation(self):
        for _ in range(3):     # 14:30, 17:00, 18:00
            _, stats = self.scan(frame(), TODAY)
        conn = sqlite3.connect(self.ledger)
        n = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_a_blocked_row_creates_nothing(self):
        _, stats = self.scan(frame(Integrity_OK=False), TODAY)
        self.assertEqual(stats["created"], 0)
        conn = sqlite3.connect(self.ledger)
        n = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_gate_snapshot_is_kept(self):
        self.scan(frame(), TODAY)
        conn = sqlite3.connect(self.ledger)
        raw = conn.execute("SELECT gate_snapshot FROM recommendations").fetchone()[0]
        conn.close()
        snap = json.loads(raw)
        self.assertEqual(snap["Core_Plus"], True)
        self.assertEqual(snap["Data_Date"], TODAY)
        self.assertTrue(snap["Buy_Ready"])

    def test_ledger_failure_never_breaks_the_scan(self):
        # A path whose PARENT is a regular file: mkdir cannot create it on any
        # platform. (A bare "/nonexistent/..." is not unopenable on Windows --
        # it is just a relative path on the current drive, and gets created.)
        blocker = Path(self.tmp.name) / "not-a-directory"
        blocker.write_text("x", encoding="ascii")

        df = mark_buy_ready(frame(), MODE, session_date=TODAY)
        out, stats = attach_recommendations(
            df, MODE, STRATEGY_VERSION, blocker / "sub" / "ledger.db",
            session_date=TODAY)
        self.assertIsNotNone(stats["error"])
        self.assertEqual(len(out), 1, "the frame must survive intact")
        self.assertEqual(out["Stock_ID"].iloc[0], "8069")


class TestQuoteFeed(unittest.TestCase):
    """F04: a holding is priced because the user holds it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "price_volume.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, close REAL)")
        rows = []
        for i, day in enumerate(["2026-09-0{}".format(d) for d in (1, 2, 3, 4, 7)]):
            rows.append(("8069", day, 100.0 + i))
            if i < 3:      # this one stopped trading -- a gap, not a zero
                rows.append(("1234", day, 50.0 + i))
        conn.executemany("INSERT INTO data VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_of_closes_is_published(self):
        feed = build_quote_feed(self.db, ["8069"], sessions=10)
        self.assertEqual(feed["as_of"], "2026-09-07")
        self.assertEqual(len(feed["sessions"]), 5)
        self.assertEqual(feed["closes"]["8069"], [100.0, 101.0, 102.0, 103.0, 104.0])

    def test_gaps_stay_null_rather_than_carrying_a_price(self):
        feed = build_quote_feed(self.db, ["1234"], sessions=10)
        self.assertEqual(feed["closes"]["1234"], [50.0, 51.0, 52.0, None, None])

    def test_a_stock_absent_from_the_scan_is_still_priced(self):
        """The whole point: 1234 is not in any shortlist, but the user holds it."""
        feed = build_quote_feed(self.db, ["8069", "1234"], sessions=10)
        self.assertEqual(sorted(feed["closes"]), ["1234", "8069"])
        self.assertEqual(feed["count"], 2)

    def test_unknown_ids_are_reported_not_silently_dropped(self):
        feed = build_quote_feed(self.db, ["8069", "9999"], sessions=10)
        self.assertEqual(feed["missing"], ["9999"])

    def test_basis_is_declared_unverified(self):
        """F08 is unfixed: the price table mixes adjusted and unadjusted
        closes, so the feed must not claim these reconcile to a statement."""
        feed = build_quote_feed(self.db, ["8069"], sessions=10)
        self.assertEqual(feed["price_basis"], "unverified")

    # -- privacy -----------------------------------------------------------
    # quotes.json is served from a PUBLIC Pages site that several people read.
    # If the feed carried a chosen subset, a name in the feed but not in
    # today's shortlist could only be there because somebody holds it -- the
    # file would announce the holdings list. These tests pin the property that
    # makes that inference impossible.

    def test_universe_mode_publishes_every_stock(self):
        feed = build_quote_feed(self.db, None, sessions=10)
        self.assertEqual(feed["scope"], "universe")
        self.assertEqual(sorted(feed["closes"]), ["1234", "8069"])

    def test_universe_mode_reveals_no_request_list(self):
        """`missing` would be a second inference channel: a short list of names
        the publisher asked for and did not get is still a list of names the
        publisher cared about."""
        feed = build_quote_feed(self.db, None, sessions=10)
        self.assertEqual(feed["missing"], [])

    def test_feed_is_identical_whatever_is_held(self):
        """The published bytes must not depend on anyone's positions at all."""
        a = build_quote_feed(self.db, None, sessions=10)
        b = build_quote_feed(self.db, None, sessions=10)
        for f in (a, b):
            f.pop("generated_at", None)
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_export_publishes_the_universe_not_a_selection(self):
        """The real publish path must not narrow the feed to the scan rows --
        that is what made a dropped-off holding visible in the first place."""
        from scanner import result_export
        originals = {n: getattr(result_export, n) for n in
                     ("MOBILE_DIR", "MOBILE_DATA_FILE", "MOBILE_QUOTES_FILE",
                      "PRICE_VOLUME_FILE", "PORTFOLIO_LEDGER_FILE")}
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result_export.MOBILE_DIR = Path(tmp)
                result_export.MOBILE_DATA_FILE = Path(tmp) / "scan_result.json"
                result_export.MOBILE_QUOTES_FILE = Path(tmp) / "quotes.json"
                result_export.PRICE_VOLUME_FILE = self.db
                result_export.PORTFOLIO_LEDGER_FILE = Path(tmp) / "ledger.db"
                # The scan only selected 8069; 1234 is not in it.
                result_export.export_scan_result_json(
                    pd.DataFrame([{"Stock_ID": "8069", "Stock_Name": "T",
                                   "Data_Date": "2026-09-07"}]),
                    "mode_prelaunch", "2026-09-07 18:00:00")
                feed = json.loads(
                    (Path(tmp) / "quotes.json").read_text(encoding="utf-8"))
            finally:
                for name, value in originals.items():
                    setattr(result_export, name, value)
        self.assertEqual(feed["scope"], "universe")
        self.assertIn("1234", feed["closes"],
                      "a stock absent from the scan must still be priced")
        self.assertNotIn("tracked_count", feed,
                         "the feed must not disclose how many positions exist")


class TestEmptyPublish(unittest.TestCase):
    """F18: a zero-pick day is a result, not a failure."""

    def test_empty_frame_still_produces_a_payload(self):
        """Writes to a temp dir and reads the file back.

        (An earlier version of this test monkeypatched result_export.json.dump
        and redirected only MOBILE_DATA_FILE. json is a shared module object, so
        the patch also silenced the quote feed's writer, and MOBILE_QUOTES_FILE
        still pointed at the repo -- the test left a 0-byte mobile/quotes.json
        behind. Redirect every path the function writes to, and do not patch
        stdlib modules other code is using.)
        """
        from scanner import result_export

        # Every path the function touches, including the ones reached only via
        # the quote feed -- otherwise the test creates a real data/ ledger.
        originals = {name: getattr(result_export, name) for name in
                     ("MOBILE_DIR", "MOBILE_DATA_FILE", "MOBILE_QUOTES_FILE",
                      "PRICE_VOLUME_FILE", "PORTFOLIO_LEDGER_FILE")}
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result_export.MOBILE_DIR = Path(tmp)
                result_export.MOBILE_DATA_FILE = Path(tmp) / "scan_result.json"
                result_export.MOBILE_QUOTES_FILE = Path(tmp) / "quotes.json"
                result_export.PRICE_VOLUME_FILE = Path(tmp) / "price_volume.db"
                result_export.PORTFOLIO_LEDGER_FILE = Path(tmp) / "ledger.db"
                out = result_export.export_scan_result_json(
                    pd.DataFrame(), "mode_prelaunch", "2026-09-09 18:00:00",
                    session_date=TODAY)
            finally:
                for name, value in originals.items():
                    setattr(result_export, name, value)

            payload = json.loads(Path(out).read_text(encoding="utf-8"))

        self.assertEqual(payload["meta"]["count"], 0)
        self.assertTrue(payload["meta"]["empty_ok"],
                        "a healthy zero-pick day must be flagged as such")
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["meta"]["session_date"], TODAY)

    def test_none_is_not_publishable(self):
        from scanner.result_export import export_scan_result
        self.assertIsNone(export_scan_result(None, "mode_prelaunch"))


class TestLedgerPublishGuard(unittest.TestCase):
    """The repo is public and git history is forever (report 9.2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "portfolio_ledger.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_recommendations_only_is_publishable(self):
        from portfolio.ledger import open_ledger, record_recommendation
        from tools.check_ledger_public import check
        conn = open_ledger(self.db)
        record_recommendation(conn, "8069", MODE, STRATEGY_VERSION, TODAY, "100")
        conn.close()
        self.assertEqual(check(self.db), 0)

    def test_a_real_fill_blocks_publication(self):
        from portfolio.ledger import open_ledger, open_position, add_execution
        from tools.check_ledger_public import check
        conn = open_ledger(self.db)
        pid = open_position(conn, "8069", strategy=MODE)
        add_execution(conn, pid, "BUY", TODAY, 1000, "102")
        conn.close()
        self.assertEqual(check(self.db), 1,
                         "a ledger holding real trades must not be published")

    def test_missing_ledger_is_not_an_error(self):
        from tools.check_ledger_public import check
        self.assertEqual(check(self.db), 0)

    def test_deleting_the_positions_does_not_make_it_publishable(self):
        """The obvious workaround -- clear the tables, then commit -- must not
        pass. open_position leaves a 'position_opened' event and flips the
        recommendation to 'converted', both of which say which stock became a
        real holding; and SQLite keeps deleted row content in freelist pages,
        so a zero count is not evidence the bytes are gone."""
        import sqlite3
        from portfolio.ledger import (open_ledger, record_recommendation,
                                      open_position, add_execution)
        from tools.check_ledger_public import check
        conn = open_ledger(self.db)
        rid, _ = record_recommendation(conn, "1815", MODE, STRATEGY_VERSION,
                                       TODAY, "126")
        pid = open_position(conn, "1815", recommendation_id=rid, strategy=MODE)
        add_execution(conn, pid, "BUY", TODAY, 1000, "128")
        conn.close()

        raw = sqlite3.connect(self.db)
        raw.execute("DELETE FROM executions")
        raw.execute("DELETE FROM positions")
        raw.commit()
        raw.close()

        self.assertEqual(check(self.db), 1,
                         "clearing the tables must not defeat the guard")

    def test_a_converted_recommendation_alone_blocks_publication(self):
        """Even with no execution rows at all, 'this suggestion became a
        holding' is ownership information."""
        import sqlite3
        from portfolio.ledger import open_ledger, record_recommendation
        from tools.check_ledger_public import check
        conn = open_ledger(self.db)
        rid, _ = record_recommendation(conn, "1815", MODE, STRATEGY_VERSION,
                                       TODAY, "126")
        with conn:
            conn.execute("UPDATE recommendations SET status='converted' "
                         "WHERE recommendation_id=?", (rid,))
        conn.close()
        self.assertEqual(check(self.db), 1)


class TestRecommendationExport(unittest.TestCase):
    """The published artifact is a recommendations-only JSON, not the database.

    The database can hold real fills; this file cannot, because publish.py reads
    one table and names every column. That is the property, not the guard.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "portfolio_ledger.db"
        self.out = Path(self.tmp.name) / "recommendations.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_omits_positions_even_when_they_exist(self):
        from portfolio.ledger import (open_ledger, record_recommendation,
                                      open_position, add_execution)
        from portfolio.publish import export_recommendations
        conn = open_ledger(self.db)
        rid, _ = record_recommendation(conn, "1815", MODE, STRATEGY_VERSION,
                                       TODAY, "126", stock_name="Test")
        pid = open_position(conn, "1815", recommendation_id=rid, strategy=MODE)
        add_execution(conn, pid, "BUY", TODAY, 1000, "128.5")
        conn.close()

        self.assertEqual(export_recommendations(self.db, self.out), 1)
        text = self.out.read_text(encoding="utf-8")
        payload = json.loads(text)
        self.assertEqual(payload["table"], "recommendations")
        self.assertEqual(payload["recommendations"][0]["initial_buy_price"],
                         "126.00")
        # The fill price, the share count and the position id must be nowhere
        # in the bytes -- not merely absent from a field we happened to check.
        for secret in ("128.5", "128.50", "1000", pid):
            self.assertNotIn(secret, text,
                             "{!r} leaked into the published export".format(secret))

    def test_seed_restores_the_fixed_price_on_a_fresh_machine(self):
        """CI has no ledger; without this the first-day price resets daily."""
        from portfolio.ledger import open_ledger, record_recommendation
        from portfolio.publish import export_recommendations, seed_from_export
        conn = open_ledger(self.db)
        record_recommendation(conn, "1815", MODE, STRATEGY_VERSION, TODAY,
                              "126", stock_name="Test")
        conn.close()
        export_recommendations(self.db, self.out)

        fresh = Path(self.tmp.name) / "fresh.db"
        self.assertEqual(seed_from_export(fresh, self.out), 1)
        conn = open_ledger(fresh)
        row = conn.execute("SELECT initial_buy_price, first_qualified_session "
                           "FROM recommendations").fetchone()
        conn.close()
        self.assertEqual(row["initial_buy_price"], "126.00")
        self.assertEqual(row["first_qualified_session"], TODAY)

    def test_seed_never_overwrites_an_existing_recommendation(self):
        from portfolio.ledger import open_ledger, record_recommendation
        from portfolio.publish import seed_from_export
        conn = open_ledger(self.db)
        record_recommendation(conn, "1815", MODE, STRATEGY_VERSION, TODAY,
                              "126")
        conn.close()
        self.out.write_text(json.dumps({
            "format_version": 1, "table": "recommendations", "count": 1,
            "recommendations": [{
                "recommendation_id": "rec-1815-{}-1".format(MODE),
                "stock_id": "1815", "strategy": MODE,
                "strategy_version": STRATEGY_VERSION, "cycle_seq": 1,
                "first_qualified_session": TODAY, "recommended_at": TODAY,
                "initial_buy_price": "999.00", "status": "active"}]},
            ensure_ascii=False), encoding="utf-8")

        self.assertEqual(seed_from_export(self.db, self.out), 0)
        conn = open_ledger(self.db)
        price = conn.execute(
            "SELECT initial_buy_price FROM recommendations").fetchone()[0]
        conn.close()
        self.assertEqual(price, "126.00", "a sync must never rewrite a fixed price")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestFirstDayIsAFreshSignal(GateCase):
    """2026-09-23 (BACKTEST_LOG section L). The rule was validated on
    'absent the previous session'; reading freshness off Hold_Status alone
    required a name to be gone for more than ten sessions, which dropped 177
    of 479 signals over 2020-01..2026-09 with no gain in quality."""

    def test_a_re_entry_the_streak_calls_held_is_buyable(self):
        ready, block = self.block_of(frame(Hold_Status="holding", First_Day=True))
        self.assertTrue(ready)
        self.assertEqual(block, "")

    def test_a_name_on_yesterdays_list_is_still_held(self):
        ready, block = self.block_of(frame(Hold_Status="holding", First_Day=False))
        self.assertFalse(ready)
        self.assertEqual(block, "held")

    def test_pending_without_the_column_still_buys(self):
        ready, block = self.block_of(frame(Hold_Status="pending"))
        self.assertTrue(ready)

    def test_an_unknown_status_blocks_even_on_a_first_day(self):
        ready, block = self.block_of(frame(Hold_Status="", First_Day=True))
        self.assertFalse(ready)
        self.assertEqual(block, "unknown")

    def test_the_column_check_accepts_the_re_entry(self):
        from scanner.result_checks import check_payload
        out = mark_buy_ready(frame(Hold_Status="holding", First_Day=True), MODE,
                             session_date=TODAY)
        row = out.iloc[0].to_dict()
        row.update({"Rank": 0})
        items = check_payload({"meta": {"mode": MODE, "data_date": TODAY,
                                        "session_date": TODAY},
                               "rows": [row]})["items"]
        self.assertFalse(any(i["code"] == "buy_ready_violates_gate" for i in items),
                         [i for i in items if i["code"] == "buy_ready_violates_gate"])


class TestReentryCardIsTheNewTrade(GateCase):
    """2026-10-08 (the 8227 shape of 2026-10-07). A name whose previous
    segment was not a signal and whose hypothetical trade already hit the
    target comes back as a first-day signal. The Buy_Ready set is unchanged
    (it is a valid signal under the validated rule); what changes is that its
    card is the NEW trade -- pending, no exit -- not the closed old one."""

    def _annotate(self):
        from unittest import mock
        import scanner.holding_tracker as tracker
        from datetime import date, timedelta
        cal, d = [], date(2026, 8, 24)
        while len(cal) < 13:
            if d.weekday() < 5:
                cal.append(d.isoformat())
            d += timedelta(days=1)
        self.assertEqual(cal[-1], TODAY)
        bars = ([(100, 101, 99, 100)] * 3 + [(100, 121, 99, 118)]
                + [(118, 119, 117, 118)] * 9)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pv.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, "
                             "open REAL, high REAL, low REAL, close REAL)")
                for day, (o, h, l, c) in zip(cal, bars):
                    conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?)",
                                 ("8069", day, o, h, l, c))
                conn.commit()
            finally:
                conn.close()
            led = {"8069": cal[:6], "9999": cal[:-1]}
            flags = {"8069": {cal[0]: 0}}
            df = frame(Close_Price=118.0, Suggested_Buy_Price=118.0,
                       Strict_Stop_Loss=94.4, Target_Price=142.0)
            df = df.drop(columns=["Hold_Status"])
            with mock.patch.object(tracker, "PRICE_VOLUME_FILE", db), \
                    mock.patch.object(tracker, "_ledger_bar_dates", lambda m: led), \
                    mock.patch.object(tracker, "_ledger_buy_flags", lambda m: flags), \
                    mock.patch.object(tracker, "_disturbed_fn", lambda: None):
                out = tracker.annotate_holding(df, MODE)
        return cal, mark_buy_ready(out, MODE, session_date=TODAY)

    def test_the_buy_ready_row_shows_the_new_pending_trade(self):
        cal, out = self._annotate()
        r = out.iloc[0]
        self.assertTrue(bool(r["Buy_Ready"]))
        self.assertTrue(bool(r["First_Day"]))
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertAlmostEqual(r["Plan_Stop"], 94.4)
        self.assertEqual(r["Prev_Signal_Date"], cal[0])
        self.assertEqual(r["Prev_Exit_Signal"], "tp")
        self.assertEqual(r["Sessions_Since_Prev_Exit"], 9)

    def test_the_payload_check_accepts_it(self):
        from scanner.result_checks import check_payload
        _, out = self._annotate()
        row = out.iloc[0].to_dict()
        items = check_payload({"meta": {"mode": MODE, "data_date": TODAY,
                                        "session_date": TODAY},
                               "rows": [row]})["items"]
        codes = {i["code"] for i in items}
        self.assertNotIn("buy_ready_on_closed_segment", codes)
        self.assertNotIn("buy_ready_violates_gate", codes)
        # the minimal fixture lacks most scan columns (column_missing etc.)
        # and meta.quality (restrictions_unchecked); what matters is that no
        # holding / plan / Prev_* identity fails
        row_errors = [i for i in items if i["level"] == "error"
                      and i["code"] not in ("column_missing", "core_plus_mismatch",
                                            "count_mismatch",
                                            "restrictions_unchecked")]
        self.assertFalse(row_errors, row_errors)

    def test_a_buy_ready_row_on_a_closed_trade_is_an_error(self):
        """What the 2026-10-07 payload carried (8227: exited + Buy_Ready),
        which meta.checks did not catch."""
        from scanner.result_checks import check_payload
        out = mark_buy_ready(frame(Hold_Status="exited", First_Day=True), MODE,
                             session_date=TODAY)
        self.assertTrue(bool(out["Buy_Ready"].iloc[0]))   # the set is unchanged
        items = check_payload({"meta": {"mode": MODE, "data_date": TODAY,
                                        "session_date": TODAY},
                               "rows": [out.iloc[0].to_dict()]})["items"]
        hit = [i for i in items if i["code"] == "buy_ready_on_closed_segment"]
        self.assertTrue(hit)
        self.assertEqual(hit[0]["level"], "error")


# --------------------------------------------------------------------------
# 2026-10-08: the recommendation lifecycle inside the scan (P0-3 / P0-4)
# --------------------------------------------------------------------------
class TestDegradedRunWritesNoRecommendation(GateCase):
    """The 2026-09-17 20:00 run was degraded (OTC feed down) and still wrote
    rec 5274 -- a name the recovered feed never listed. A degraded run now
    attaches what exists and creates, retracts and exports nothing."""

    def setUp(self):
        super(TestDegradedRunWritesNoRecommendation, self).setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "portfolio_ledger.db"
        self.export = Path(self.tmp.name) / "recommendations.json"

    def tearDown(self):
        super(TestDegradedRunWritesNoRecommendation, self).tearDown()
        self.tmp.cleanup()

    def count(self):
        if not self.ledger.exists():
            return 0
        conn = sqlite3.connect(self.ledger)
        try:
            return conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        finally:
            conn.close()

    def test_degraded_run_creates_nothing(self):
        from portfolio.sync import REC_COLUMNS
        df = mark_buy_ready(frame(), MODE, session_date=TODAY)
        self.assertTrue(bool(df["Buy_Ready"].iloc[0]))
        out, stats = attach_recommendations(df, MODE, STRATEGY_VERSION, self.ledger,
                                            session_date=TODAY, allow_writes=False)
        self.assertEqual((stats["created"], stats["writes"]), (0, False))
        self.assertEqual(self.count(), 0)
        self.assertEqual(len(REC_COLUMNS), 8)
        for col in REC_COLUMNS:
            self.assertIn(col, out.columns)
            self.assertIsNone(out[col].iloc[0], col)

    def test_degraded_run_still_attaches_existing(self):
        df = mark_buy_ready(frame(), MODE, session_date=TODAY)
        out, _ = attach_recommendations(df, MODE, STRATEGY_VERSION, self.ledger,
                                        session_date=TODAY)
        later = frame(Data_Date="2026-09-10", Hold_Status="holding")
        out2, stats = attach_recommendations(later, MODE, STRATEGY_VERSION,
                                             self.ledger, session_date="2026-09-10",
                                             allow_writes=False)
        self.assertEqual(stats["attached"], 1)
        self.assertEqual(out2["Recommendation_ID"].iloc[0],
                         out["Recommendation_ID"].iloc[0])
        self.assertEqual(self.count(), 1)

    def test_next_session_is_stored(self):
        df = mark_buy_ready(frame(), MODE, session_date=TODAY)
        out, _ = attach_recommendations(df, MODE, STRATEGY_VERSION, self.ledger,
                                        session_date=TODAY,
                                        next_session="2026-09-10")
        self.assertEqual(out["Rec_Valid_Until"].iloc[0], "2026-09-10")
        conn = sqlite3.connect(self.ledger)
        try:
            vu = conn.execute(
                "SELECT valid_until_session FROM recommendations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(vu, "2026-09-10")

    def _helpers(self, degraded):
        """scan_headless's three recommendation steps on temp files, with the
        lifecycle and the card drawing stubbed out."""
        from unittest import mock
        import scan_headless as sh
        import scanner.market_calendar as mc
        adv = mock.Mock(return_value={"backfilled": 0, "moved": 0, "closed": 0,
                                      "expired": 0, "superseded": 0,
                                      "error": None, "transitions": []})
        drawn = []

        def annotate(df, mode, rec_anchors=None, **kw):
            drawn.append(dict(rec_anchors or {}))
            return df
        with mock.patch.object(sh, "PORTFOLIO_LEDGER_FILE", self.ledger), \
                mock.patch.object(sh, "RECOMMENDATIONS_EXPORT_FILE", self.export), \
                mock.patch.object(sh, "advance_recommendations", adv), \
                mock.patch.object(sh, "annotate_holding", annotate), \
                mock.patch.object(mc, "entry_session_after",
                                  lambda day, **kw: "2026-09-10"):
            anchors, adv_stats = sh._prepare_recommendations(MODE, TODAY, degraded)
            df = mark_buy_ready(frame(), MODE, session_date=TODAY)
            df, anchors, stats = sh._create_recommendations(
                df, MODE, TODAY, degraded, anchors)
            sh._finish_recommendations(None, MODE, TODAY, degraded)
        return adv, adv_stats, df, stats, drawn

    def test_degraded_helpers_write_nothing(self):
        self.export.write_bytes(b'{"count": 0, "recommendations": []}')
        before = self.export.read_bytes()
        adv, adv_stats, df, stats, drawn = self._helpers("feed degraded: OTC")
        adv.assert_not_called()
        self.assertIsNone(adv_stats)
        self.assertEqual(stats["created"], 0)
        self.assertFalse(stats["writes"])
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.export.read_bytes(), before)
        self.assertEqual(drawn, [])                  # nothing to re-anchor
        self.assertIsNone(df["Recommendation_ID"].iloc[0])

    def test_clean_run_creates_reanchors_and_exports(self):
        adv, adv_stats, df, stats, drawn = self._helpers(None)
        adv.assert_called_once()
        self.assertEqual(stats["created"], 1)
        self.assertEqual(df["Rec_Valid_Until"].iloc[0], "2026-09-10")
        self.assertEqual(len(drawn), 1)
        self.assertEqual(drawn[0]["8069"]["anchor"], TODAY)
        doc = json.loads(self.export.read_text(encoding="utf-8"))
        self.assertEqual([r["stock_id"] for r in doc["recommendations"]], ["8069"])
        self.assertEqual(doc["recommendations"][0]["valid_until_session"],
                         "2026-09-10")

    def test_run_scan_order(self):
        """recs.md: anchors before the cards, the buy gate, then the freeze
        and the re-anchor, the chip verdict on the final card, the tracked
        rows, the export, and record_picks only on a clean run."""
        import inspect
        import scan_headless as sh
        src = inspect.getsource(sh.run_scan)
        steps = ["_prepare_recommendations(", "annotate_holding(result_df",
                 "mark_buy_ready(result_df", "_create_recommendations(",
                 "annotate_chip_action(result_df", "split_tracked(",
                 "_finish_recommendations(", "record_picks(result_df"]
        at = [src.find(s) for s in steps]
        self.assertTrue(all(i >= 0 for i in at), dict(zip(steps, at)))
        self.assertEqual(at, sorted(at), dict(zip(steps, at)))
        guard = src[:at[-1]].rsplit("if ", 1)[-1]
        self.assertTrue(guard.startswith("degraded is None"), guard)
        self.assertEqual(src.count("rec_stats=rec_meta(rec_attach, rec_advance)"), 2)


class TestNewRecommendationCard(GateCase):
    """A recommendation created in this run is drawn as ITS trade: pending,
    anchored on today, with the recommendation's own stop."""

    def test_new_recommendation_card_is_pending(self):
        from contextlib import ExitStack
        from datetime import date, timedelta
        from unittest import mock
        import scan_headless as sh
        import scanner.holding_tracker as tracker
        import scanner.market_calendar as mc
        cal, d = [], date(2026, 8, 24)
        while len(cal) < 13:
            if d.weekday() < 5:
                cal.append(d.isoformat())
            d += timedelta(days=1)
        self.assertEqual(cal[-1], TODAY)
        bars = ([(100, 101, 99, 100)] * 3 + [(100, 121, 99, 118)]
                + [(118, 119, 117, 118)] * 9)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            db = Path(tmp) / "pv.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, "
                             "open REAL, high REAL, low REAL, close REAL)")
                for day, (o, h, l, c) in zip(cal, bars):
                    conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?)",
                                 ("8069", day, o, h, l, c))
                conn.commit()
            finally:
                conn.close()
            led = {"8069": cal[:6], "9999": cal[:-1]}
            flags = {"8069": {cal[0]: 0}}
            ledger = Path(tmp) / "portfolio_ledger.db"
            for target, name, value in (
                    (tracker, "PRICE_VOLUME_FILE", db),
                    (tracker, "_ledger_bar_dates", lambda m: led),
                    (tracker, "_ledger_buy_flags", lambda m: flags),
                    (tracker, "_disturbed_fn", lambda: None),
                    (sh, "PORTFOLIO_LEDGER_FILE", ledger),
                    (mc, "entry_session_after", lambda day, **kw: "2026-09-10")):
                stack.enter_context(mock.patch.object(target, name, value))
            df = frame(Close_Price=118.0, Suggested_Buy_Price=118.0,
                       Strict_Stop_Loss=94.4, Target_Price=142.0)
            df = df.drop(columns=["Hold_Status"])
            df = tracker.annotate_holding(df, MODE, rec_anchors={})
            df = mark_buy_ready(df, MODE, session_date=TODAY)
            self.assertTrue(bool(df["Buy_Ready"].iloc[0]))
            out, anchors, stats = sh._create_recommendations(
                df, MODE, TODAY, None, {})
        self.assertEqual(stats["created"], 1)
        r = out.iloc[0]
        self.assertEqual(r["Hold_Anchor_Kind"], "rec")
        self.assertEqual(r["Hold_Anchor"], TODAY)
        self.assertEqual(r["Hold_Status"], "pending")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertEqual(r["Recommended_On"], TODAY)
        self.assertAlmostEqual(r["Plan_Stop"], r["Initial_Stop_Price"])
        self.assertAlmostEqual(r["Plan_Stop"], 94.4)
        self.assertTrue(bool(r["Buy_Ready"]))         # the verdict is untouched
        self.assertEqual(anchors["8069"]["rec_id"], r["Recommendation_ID"])
