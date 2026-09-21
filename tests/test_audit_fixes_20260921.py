"""
Regressions for the 2026-09-21 code audit. One test per defect it found, each
named for what actually goes wrong if it comes back.

    python -m unittest tests.test_audit_fixes_20260921 -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scanner import tick
from scanner.exit_rules import replay_exit, simulate_exit

MOBILE = Path(__file__).resolve().parent.parent / "mobile"
ROOT = Path(__file__).resolve().parent.parent


class EtfTickLadder(unittest.TestCase):
    """ETFs and ETNs quote 0.01 below 50 and 0.05 above -- NOT the ordinary
    share ladder, whose step in the 100-500 band is 0.50. Snapping an ETF
    level with the equity table moved a trailing lock the WRONG way (0050 at
    106.75: 108.85 -> 108.50, a stop wider than the rule)."""

    def test_an_etf_uses_the_etf_ladder(self):
        self.assertEqual(tick.tick_size(106.75, "0050"), 0.05)
        self.assertEqual(tick.tick_size(30.0, "00878"), 0.01)
        self.assertEqual(tick.tick_size(106.75, "2330"), 0.5)

    def test_leveraged_and_suffixed_codes_count_as_etfs(self):
        for sid in ("0050", "0056", "00878", "00663L", "00400A"):
            self.assertTrue(tick.is_etf(sid), sid)
        for sid in ("2330", "1815", "6488", "", None):
            self.assertFalse(tick.is_etf(sid), sid)

    def test_the_lock_on_an_etf_is_not_widened(self):
        fill = 106.75
        self.assertEqual(tick.stop_price(fill * 1.02, "0050"), 108.85)
        # the equity ladder is what got it wrong, in the direction that costs
        self.assertEqual(tick.stop_price(fill * 1.02, "2330"), 108.5)

    def test_every_snapped_price_is_on_its_own_ladder(self):
        for sid in ("0050", "00878", "2330"):
            for p in (9.93, 47.62, 63.31, 178.44, 534.51, 1234.5):
                for mode in ("up", "down", "nearest"):
                    got = tick.round_to_tick(p, mode, sid)
                    self.assertTrue(tick.is_on_tick(got, sid),
                                    "%s %s %s -> %s" % (sid, p, mode, got))

    def test_the_phone_uses_the_same_two_ladders(self):
        js = (MOBILE / "app.js").read_text(encoding="utf-8")
        self.assertIn("ETF_TICKS", js)
        self.assertIn("function tickSize(priceCents, stockId)", js)
        self.assertIn("function tickRound(priceCents, dir, stockId)", js)


class LateProfitTakeDay(unittest.TestCase):
    """The late profit-take fires on trading day 8, counted 1-based on both
    sides. The phone used lateFrom - 1 and put a rung marked MUST on the card
    a day early, so the owner would sell at day 8's open while the backend was
    still waiting for day 9's."""

    def _bars(self, n, close):
        return ([100.0] * n, [101.0] * n, [99.5] * n, [close] * n)

    def test_day_seven_does_not_arm_it(self):
        o, h, l, c = self._bars(7, 101.5)
        self.assertFalse(replay_exit(o, h, l, c, hold_bars=None)["late_due"])

    def test_day_eight_arms_it(self):
        o, h, l, c = self._bars(8, 101.5)
        self.assertTrue(replay_exit(o, h, l, c, hold_bars=None)["late_due"])

    def test_the_phone_counts_from_the_same_day(self):
        js = (MOBILE / "app.js").read_text(encoding="utf-8")
        self.assertIn("dayIdx >= STRATEGY.lateFrom", js)
        self.assertNotIn("dayIdx >= STRATEGY.lateFrom - 1", js)


class SimulateExitForwardsEveryLeg(unittest.TestCase):
    """A parameter search must be able to switch the late leg off. It could
    not, so a sweep pricing "no take profit, no lock" was silently measuring a
    rule that still took late profits."""

    def test_the_late_leg_can_be_disabled(self):
        o, h, l, c = [100.0] * 12, [101.0] * 12, [99.5] * 12, [101.5] * 12
        self.assertEqual(simulate_exit(o, h, l, c)[2], "late")
        self.assertEqual(simulate_exit(o, h, l, c, late_from=None)[2], "time")

    def test_disabling_everything_leaves_only_the_time_exit(self):
        o, h, l, c = [100.0] * 12, [140.0] * 12, [99.5] * 12, [130.0] * 12
        self.assertEqual(
            simulate_exit(o, h, l, c, tp_pct=None, arm_pct=None,
                          late_from=None)[2], "time")


COLS = ("date TEXT, stock_id TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume_share REAL, Volume_Lot REAL, MA5_Volume REAL, "
        "Min_Volume_20 REAL, Max_Price_20 REAL, Min_Price_20 REAL")


class SnapshotPreservesRollingColumns(unittest.TestCase):
    """`data` has twelve columns and the snapshot supplies eight. INSERT OR
    REPLACE deleted the row first, blanking MA5_Volume, Min_Volume_20,
    Max_Price_20 and Min_Price_20 on 990 of 1,292 rows."""

    def _db(self, tmp):
        db = Path(tmp) / "pv.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE data (%s)" % COLS)
            conn.execute(
                "INSERT INTO data VALUES ('2026-09-21','1111',10,11,9,10.5,"
                "5000,5.0,4.4,3.0,12.0,8.0)")
            conn.commit()
        finally:
            conn.close()
        return db

    def _frame(self):
        return pd.DataFrame([{
            "date": "2026-09-21", "stock_id": "1111", "open": 10.2,
            "high": 11.5, "low": 9.8, "close": 11.0, "volume_share": 6000.0,
            "Volume_Lot": 6.0}])

    def test_an_existing_row_keeps_its_rolling_columns(self):
        from scanner.market_snapshot import store_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            store_snapshot(db, self._frame(), log=lambda *a: None)
            conn = sqlite3.connect(db)
            try:
                row = conn.execute(
                    "SELECT close, MA5_Volume, Min_Volume_20, Max_Price_20, "
                    "Min_Price_20 FROM data").fetchone()
                n = conn.execute("SELECT COUNT(*) FROM data").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(n, 1, "the upsert duplicated the bar")
        self.assertAlmostEqual(row[0], 11.0, msg="the quote was not updated")
        self.assertEqual(row[1:], (4.4, 3.0, 12.0, 8.0))

    def test_the_scan_path_does_not_create_the_migration_index(self):
        src = (ROOT / "scanner" / "market_snapshot.py").read_text(
            encoding="utf-8")
        self.assertNotIn("CREATE UNIQUE INDEX", src,
                         "a routine price save must not migrate the store; "
                         "storage/data_store.migrate_stock_store owns that")

    def test_a_missing_row_is_inserted(self):
        from scanner.market_snapshot import store_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            df = self._frame()
            df.loc[0, "stock_id"] = "2222"
            store_snapshot(db, df, log=lambda *a: None)
            conn = sqlite3.connect(db)
            try:
                n = conn.execute("SELECT COUNT(*) FROM data").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(n, 2)

    def test_refill_restores_what_was_already_blanked(self):
        from scanner.market_snapshot import refill_rolling_columns
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pv.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE data (%s)" % COLS)
                for i, d in enumerate(("2026-09-17", "2026-09-18",
                                       "2026-09-21")):
                    conn.execute(
                        "INSERT INTO data VALUES (?,'1111',10,11,9,?,?,?,"
                        "NULL,NULL,NULL,NULL)",
                        (d, 10.0 + i, 5000.0, 5.0 + i))
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(refill_rolling_columns(db, log=lambda *a: None), 3)
            conn = sqlite3.connect(db)
            try:
                got = conn.execute(
                    "SELECT MA5_Volume, Max_Price_20, Min_Price_20 FROM data "
                    "WHERE date = '2026-09-21'").fetchone()
                left = conn.execute(
                    "SELECT COUNT(*) FROM data WHERE MA5_Volume IS NULL"
                ).fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(left, 0)
        self.assertAlmostEqual(got[0], 6.0)      # mean of 5, 6, 7
        self.assertAlmostEqual(got[1], 12.0)     # max close
        self.assertAlmostEqual(got[2], 10.0)     # min close

    def test_a_second_run_rewrites_nothing(self):
        from scanner.market_snapshot import refill_rolling_columns
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            self.assertEqual(refill_rolling_columns(db, log=lambda *a: None), 0)


class BackfillQueueSeesGaps(unittest.TestCase):
    """COUNT(*) < 60 cannot see a hole. 2330 held 63 bars covering 69 sessions
    -- an 8-session gap through the middle -- so it was never queued and every
    average that crossed the gap stayed null forever."""

    def _db(self, tmp, rows):
        db = str(Path(tmp) / "pv.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE data (date TEXT, stock_id TEXT, "
                         "close REAL, Volume_Lot REAL)")
            for sid, dates, turn in rows:
                for d in dates:
                    conn.execute("INSERT INTO data VALUES (?,?,?,1.0)",
                                 (d, sid, turn))
            conn.commit()
        finally:
            conn.close()
        return db

    def _sessions(self, n):
        import datetime
        end = datetime.date(2026, 9, 21)
        return [(end - datetime.timedelta(days=n - 1 - i)).isoformat()
                for i in range(n)]

    def test_a_name_with_enough_bars_but_a_hole_is_queued(self):
        from scanner.market_snapshot import stocks_needing_history
        ses = self._sessions(70)
        holed = ses[:30] + ses[38:]          # 62 bars, 8 sessions missing
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [("2330", holed, 900.0), ("1111", ses, 800.0)])
            got = stocks_needing_history(db, min_bars=60, limit=5)
        self.assertIn("2330", got)
        self.assertNotIn("1111", got, "a complete window must not be queued")

    def test_a_name_missing_from_the_newest_session_still_ranks_first(self):
        from scanner.market_snapshot import stocks_needing_history
        ses = self._sessions(70)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [("2330", ses[:20], 900.0),
                                ("1111", ses[:19] + [ses[-1]], 10.0)])
            got = stocks_needing_history(db, min_bars=60, limit=1)
        self.assertEqual(list(got), ["2330"])


class UniverseAveragesDoNotCrossGaps(unittest.TestCase):
    """A 5-ROW mean over 15 calendar days is not a 5-day mean, and the phone
    renders it straight into "above / below the 5-day mean"."""

    def _db(self, tmp, series):
        db = Path(tmp) / "pv.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE data (date TEXT, stock_id TEXT, "
                         "open REAL, high REAL, low REAL, close REAL, "
                         "Volume_Lot REAL)")
            for sid, dates in series.items():
                for d in dates:
                    conn.execute("INSERT INTO data VALUES (?,?,10,11,9,10,5)",
                                 (d, sid))
            conn.commit()
        finally:
            conn.close()
        return db

    def _dates(self, n):
        import datetime
        end = datetime.date(2026, 9, 21)
        return [(end - datetime.timedelta(days=n - 1 - i)).isoformat()
                for i in range(n)]

    def test_a_gapped_series_publishes_no_five_day_mean(self):
        from scanner.universe_export import build
        ses = self._dates(12)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, {"1111": ses, "2222": ses[:6] + ses[9:]})
            out = build(db)
        self.assertIsNotNone(out["1111"]["MA5"])
        self.assertEqual(out["1111"]["Gap_Sessions"], 0)
        self.assertIsNone(out["2222"]["MA5"],
                          "a mean was published across a 3-session hole")
        self.assertEqual(out["2222"]["Gap_Sessions"], 3)
        self.assertIsNone(out["2222"].get("Ret_5D_Pct"))

    def test_the_record_still_carries_the_price(self):
        from scanner.universe_export import build
        ses = self._dates(12)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, {"2222": ses[:6] + ses[9:]})
            out = build(db)
        self.assertAlmostEqual(out["2222"]["Close_Price"], 10.0)
        self.assertEqual(out["2222"]["Data_Date"], ses[-1])


class TrackedBlockIsAudited(unittest.TestCase):
    """The tracked block is what a HOLDER reads, and nothing checked it: the
    column registry, the tick ladder and every fill-level identity ran on
    `rows` only."""

    def _payload(self, tracked):
        return {"meta": {"mode": "mode_prelaunch", "data_date": "2026-09-21",
                         "session_date": "2026-09-21"},
                "rows": [], "tracked": tracked}

    def test_an_off_ladder_level_in_tracked_is_reported(self):
        from scanner.result_checks import check_payload
        rep = check_payload(self._payload([
            {"Stock_ID": "1111", "Close_Price": 191.5,
             "Strict_Stop_Loss": 153.2}]))
        codes = {i["code"] for i in rep["items"]}
        self.assertIn("tracked_price_off_tick", codes)

    def test_a_clean_tracked_block_adds_no_error(self):
        from scanner.result_checks import check_payload
        rep = check_payload(self._payload([
            {"Stock_ID": "1111", "Close_Price": 191.5,
             "Strict_Stop_Loss": 153.0}]))
        codes = {i["code"] for i in rep["items"]}
        self.assertNotIn("tracked_price_off_tick", codes)

    def test_tracked_problems_never_fail_the_scan(self):
        from scanner.result_checks import check_payload
        rep = check_payload(self._payload([
            {"Stock_ID": "1111", "Close_Price": 191.5,
             "Strict_Stop_Loss": 153.2}]))
        levels = {i["code"]: i["level"] for i in rep["items"]}
        self.assertEqual(levels.get("tracked_price_off_tick"), "warn",
                         "a tracked-row problem must be visible, not block a "
                         "scan whose actual list is sound")


class TrackedBlockIsNotDeleted(unittest.TestCase):
    """gui/scan_worker.py runs the same export without building the tracked
    block, so opening the desktop app after a cloud scan stripped the
    dropped-out holdings from the payload the phone reads."""

    def test_a_publisher_without_tracked_carries_the_old_one_forward(self):
        import json
        import scanner.result_export as rx
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan_result.json"
            path.write_text(json.dumps({
                "meta": {"session_date": "2026-09-18",
                         "tracked": {"count": 1}},
                "rows": [], "tracked": [{"Stock_ID": "1815"}]}),
                encoding="utf-8")
            old = rx.MOBILE_DATA_FILE
            rx.MOBILE_DATA_FILE = path
            try:
                rx.export_scan_result_json(
                    pd.DataFrame([{"Stock_ID": "2330", "Close_Price": 100.0}]),
                    scan_mode="mode_prelaunch", session_date="2026-09-21")
                got = json.loads(path.read_text(encoding="utf-8"))
            finally:
                rx.MOBILE_DATA_FILE = old
        self.assertEqual([r["Stock_ID"] for r in got["tracked"]], ["1815"])
        self.assertTrue(got["meta"]["tracked"]["carried_forward"])
        self.assertEqual(got["meta"]["tracked"]["built_for"], "2026-09-18")

    def test_the_phone_says_when_it_is_carried_forward(self):
        js = (MOBILE / "app.js").read_text(encoding="utf-8")
        self.assertIn("function trackedStale()", js)
        self.assertIn("carried_forward", js)


class SessionFloor(unittest.TestCase):
    """purge_nonsession_bars is the only code here that DELETEs price rows,
    and coverage now swings four-fold. A real shortlist-only day between two
    whole-market days sat 60 names above the threshold."""

    def test_a_shortlist_day_between_full_days_survives(self):
        from scanner.data_integrity import nonsession_dates
        days = [("2026-09-%02d" % d, 2300) for d in range(1, 11)]
        days.append(("2026-09-11", 320))
        days += [("2026-09-%02d" % d, 2300) for d in range(14, 22)]
        self.assertEqual(nonsession_dates(days), [])

    def test_a_placeholder_is_still_caught(self):
        from scanner.data_integrity import nonsession_dates
        days = [("2026-09-%02d" % d, 1930) for d in range(1, 19)]
        days.append(("2026-09-20", 22))
        self.assertEqual(nonsession_dates(days), ["2026-09-20"])


class ExitEndsTheHold(unittest.TestCase):
    """18 of 101 published rows said "day 12, keep riding" next to "the lock
    closed this trade on day 6"."""

    def test_the_status_is_terminal_once_the_rule_books_an_exit(self):
        from scanner.result_checks import HOLD_STATUSES
        self.assertIn("exited", HOLD_STATUSES)

    def test_the_desktop_does_not_recompute_a_closed_hold(self):
        from gui.app import _live_hold
        self.assertIsNone(_live_hold({"Entry_Date": "2026-09-01",
                                      "Exit_Signal": "lock"}))


class InstitutionalWindowIsNotClamped(unittest.TestCase):
    """A stock absent from a date's exchange table had no institutional trade
    that day: that is a 0, not a missing session. Clamping the window to the
    stock's own last row made the phone report the flow as stale while the
    feed was current."""

    def test_an_absent_newest_day_counts_as_zero(self):
        from ingestion.inst_trades import get_inst_features
        df = pd.DataFrame([
            {"stock_id": "1111", "date": "2026-09-18", "board": "TSE",
             "Foreign_Net": 10.0, "Trust_Net": 0.0, "Dealer_Net": 0.0,
             "Inst_Net": 10.0},
            {"stock_id": "2222", "date": "2026-09-21", "board": "TSE",
             "Foreign_Net": 5.0, "Trust_Net": 0.0, "Dealer_Net": 0.0,
             "Inst_Net": 5.0},
        ])
        out = get_inst_features(["1111"], days=5, existing=df)
        self.assertEqual(out["1111"]["Inst_Date"], "2026-09-21")
        self.assertAlmostEqual(out["1111"]["Inst_Net"], 0.0)
        self.assertAlmostEqual(out["1111"]["Inst_Net_5D"], 10.0)


class UniverseCarriesInstitutionalFlow(unittest.TestCase):
    """inst=None was passed with get_inst_features imported and never called,
    so a holding the scanner never picked was told forever that the scan had
    no institutional data for it."""

    def test_the_scan_passes_real_flow_to_the_export(self):
        src = (ROOT / "scan_headless.py").read_text(encoding="utf-8")
        self.assertNotIn("inst=None", src)
        self.assertIn("get_inst_features(", src)
        self.assertIn("inst=_inst", src)


if __name__ == "__main__":
    unittest.main()
