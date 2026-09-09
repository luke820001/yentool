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
        """F15: a tailwind computed from a months-old TAIEX cache is not one."""
        market_regime.get_market_regime = lambda: {
            "ok": True, "enter_ok": True, "risk_on": True, "is_current": False}
        ready, block = self.block_of(frame())
        self.assertFalse(ready)
        self.assertEqual(block, "regime")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
