"""
Fail-closed pins, stores and files (2026-10-09 audit D11-02/03/05/09).

A published file that cannot be read is history, not "nothing": it is kept,
reported, and never overwritten with whatever an empty rebuild produced.
ASCII only.

    python -m unittest tests.test_failclosed_stores_20261009 -v
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scan_headless
from portfolio import publish
from portfolio.schema import open_ledger


def _ledger_with(path, ids):
    conn = open_ledger(str(path))
    try:
        with conn:
            for rid in ids:
                conn.execute(
                    "INSERT INTO recommendations (recommendation_id, stock_id, "
                    "stock_name, market, strategy, strategy_version, cycle_seq, "
                    "first_qualified_session, recommended_at, initial_buy_price, "
                    "initial_stop_price, initial_target_price, status) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, rid[4:8], "N", "OTC", "mode_prelaunch", "v", 1,
                     "2026-09-01", "2026-09-01 15:00:00", 100.0, 80.0, 120.0,
                     "active"))
    finally:
        conn.close()


class RecommendationExportIsNeverOverwrittenBlind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        self.led = self.t / "pl.db"
        self.out = self.t / "recommendations.json"

    def test_a_normal_export_is_atomic_and_complete(self):
        _ledger_with(self.led, ["rec-1111-m-1", "rec-2222-m-1"])
        self.assertEqual(publish.export_recommendations(self.led, self.out), 2)
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["count"], 2)
        self.assertFalse((self.t / "recommendations.json.tmp").exists())

    def test_a_corrupt_file_is_kept_and_the_export_refused(self):
        _ledger_with(self.led, ["rec-1111-m-1"])
        self.out.write_text("{truncated", encoding="utf-8")
        with self.assertRaises(publish.ExportRefused):
            publish.export_recommendations(self.led, self.out)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "{truncated")
        self.assertTrue((self.t / "recommendations.json.bad").exists())

    def test_wrong_shape_is_refused(self):
        self.out.write_text(json.dumps({"recommendations": "nope"}), encoding="utf-8")
        with self.assertRaises(publish.ExportRefused):
            publish.export_recommendations(self.led, self.out)

    def test_an_empty_ledger_cannot_erase_published_history(self):
        _ledger_with(self.led, ["rec-1111-m-1", "rec-2222-m-1"])
        publish.export_recommendations(self.led, self.out)
        before = self.out.read_text(encoding="utf-8")
        empty = self.t / "empty.db"
        open_ledger(str(empty)).close()
        with self.assertRaises(publish.ExportRefused):
            publish.export_recommendations(empty, self.out)
        self.assertEqual(self.out.read_text(encoding="utf-8"), before)

    def test_growth_is_allowed(self):
        _ledger_with(self.led, ["rec-1111-m-1"])
        publish.export_recommendations(self.led, self.out)
        _ledger_with(self.t / "more.db", ["rec-1111-m-1", "rec-3333-m-1"])
        self.assertEqual(publish.export_recommendations(self.t / "more.db",
                                                        self.out), 2)

    def test_first_run_without_a_file_exports(self):
        _ledger_with(self.led, ["rec-1111-m-1"])
        self.assertEqual(publish.export_recommendations(self.led, self.out), 1)


class RunReadsTheExportAsReadOnlyWhenBroken(unittest.TestCase):
    def test_flag_is_true_only_for_a_present_unusable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self.assertFalse(scan_headless._recs_export_unreadable(t / "no.json"))
            (t / "bad.json").write_text("garbage", encoding="utf-8")
            self.assertTrue(scan_headless._recs_export_unreadable(t / "bad.json"))
            (t / "ok.json").write_text(json.dumps({"recommendations": []}),
                                       encoding="utf-8")
            self.assertFalse(scan_headless._recs_export_unreadable(t / "ok.json"))

    def test_prepare_does_not_seed_or_advance_on_a_broken_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            bad = t / "recommendations.json"
            bad.write_text("garbage", encoding="utf-8")
            with mock.patch.object(scan_headless, "RECOMMENDATIONS_EXPORT_FILE", bad), \
                    mock.patch.object(scan_headless, "PORTFOLIO_LEDGER_FILE",
                                      t / "pl.db"), \
                    mock.patch.object(scan_headless, "seed_from_export") as seed, \
                    mock.patch.object(scan_headless, "advance_recommendations") as adv:
                anchors, stats = scan_headless._prepare_recommendations(
                    "mode_prelaunch", "2026-09-09", None)
            seed.assert_not_called()
            adv.assert_not_called()
            self.assertIsNone(stats)
            self.assertEqual(anchors, {})


class SessionDatesAreValidated(unittest.TestCase):
    """D11-04: one NaN, junk or future Data_Date redated the scan and poisoned
    scan_state.session, after which every save was refused."""

    def test_valid_day(self):
        from scanner.scan_state import valid_day
        for good in ("2026-09-23", "2026-09-23 15:00:00", "2000-01-01"):
            self.assertTrue(valid_day(good), good)
        for bad in ("nan", "None", "", None, float("nan"), "2099-01-01",
                    "1999-12-31", "2026-13-01", "26-09-23", "2026/09/23"):
            self.assertFalse(valid_day(bad), repr(bad))

    def test_session_date_ignores_junk_and_future_values(self):
        import pandas as pd
        df = pd.DataFrame({"Data_Date": ["2026-09-23", float("nan"), "2099-01-01",
                                         "nan", "2026-09-22"]})
        self.assertEqual(scan_headless._session_date(df), "2026-09-23")
        self.assertEqual(scan_headless._session_date(
            pd.DataFrame({"Data_Date": [float("nan"), "2099-01-01"]})), "")

    def test_a_poisoned_stored_session_heals(self):
        from scanner import scan_state
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scan_state, "_STATE_DIR", Path(tmp)):
                (Path(tmp) / "m.json").write_text(json.dumps(
                    {"held_ids": ["1111"], "session": "nan", "prior_ids": None,
                     "prior_session": None}), encoding="utf-8")
                self.assertIsNone(scan_state.load_state("m")["session"])
                self.assertTrue(scan_state.save_state("m", "2026-09-23", ["2222"]))
                self.assertEqual(scan_state.load_state("m")["session"], "2026-09-23")
                self.assertEqual(scan_state.load_state("m")["prior_ids"], ["1111"])

    def test_junk_and_future_sessions_are_never_saved(self):
        from scanner import scan_state
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scan_state, "_STATE_DIR", Path(tmp)):
                scan_state.save_state("m", "2026-09-23", ["1111"])
                for bad in ("nan", "2099-01-01", "junk"):
                    self.assertFalse(scan_state.save_state("m", bad, ["9999"]), bad)
                st = scan_state.load_state("m")
        self.assertEqual((st["session"], st["held_ids"]), ("2026-09-23", ["1111"]))


class LostScanStateIsRebuiltFromTheLedger(unittest.TestCase):
    """D11-05: a missing or corrupt state file turned the N_ENTER/N_HOLD
    hysteresis off and the list changed shape on a FINAL day."""

    def _ledger(self, tmp):
        path = Path(tmp) / "signal_ledger.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE picks (scan_session TEXT, scan_mode TEXT, "
                     "stock_id TEXT, rank INTEGER)")
        conn.executemany("INSERT INTO picks VALUES (?,?,?,?)", [
            ("2026-09-21", "m", "1111", 1), ("2026-09-22", "m", "3333", 2),
            ("2026-09-22", "m", "2222", 1), ("2026-09-23", "m", "4444", 1),
            ("2026-09-22", "other", "9999", 1)])
        conn.commit()
        conn.close()
        return path

    def test_previous_session_ids_reads_the_newest_older_list_in_rank_order(self):
        from scanner import signal_ledger
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(signal_ledger, "SIGNAL_LEDGER_FILE",
                                   self._ledger(tmp)):
                self.assertEqual(signal_ledger.previous_session_ids("m", "2026-09-23"),
                                 ["2222", "3333"])
                self.assertIsNone(signal_ledger.previous_session_ids("m", "2026-09-21"))
                self.assertIsNone(signal_ledger.previous_session_ids("none", "2026-09-23"))
            with mock.patch.object(signal_ledger, "SIGNAL_LEDGER_FILE",
                                   Path(tmp) / "gone.db"):
                self.assertIsNone(signal_ledger.previous_session_ids("m", "2026-09-23"))

    def test_prior_prefers_a_usable_state_and_falls_back_to_the_ledger(self):
        from scanner import signal_ledger
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(signal_ledger, "SIGNAL_LEDGER_FILE",
                                   self._ledger(tmp)):
                good = {"held_ids": ["7777"], "session": "2026-09-22",
                        "prior_ids": None, "prior_session": None}
                self.assertEqual(scan_headless._hysteresis_prior(
                    good, "m", "2026-09-23"), ["7777"])
                lost = {"held_ids": [], "session": None, "prior_ids": None,
                        "prior_session": None}
                self.assertEqual(scan_headless._hysteresis_prior(
                    lost, "m", "2026-09-23"), ["2222", "3333"])
                # a session with an honestly empty list is not "lost"
                empty = dict(good, held_ids=[])
                self.assertEqual(scan_headless._hysteresis_prior(
                    empty, "m", "2026-09-23"), [])
                # no ledger either: plain top-N, as before
            with mock.patch.object(signal_ledger, "SIGNAL_LEDGER_FILE",
                                   Path(tmp) / "gone.db"):
                self.assertEqual(scan_headless._hysteresis_prior(
                    lost, "m", "2026-09-23"), [])


class AFailedWriteCannotLeaveAFinalList(unittest.TestCase):
    """D11-02 / D11-09: a run that could not write its picks, or could not
    publish its payload, must not read as a final, frozen list."""

    def _payload(self, t):
        p = t / "scan_result.json"
        p.write_text(json.dumps({"meta": {"mode": "mode_prelaunch",
                                          "session_date": "2026-09-09",
                                          "data_date": "2026-09-09"},
                                 "rows": [{"Stock_ID": "8069"}]}),
                     encoding="utf-8")
        return p

    def test_run_faults_become_check_errors(self):
        from scanner import result_checks
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            rep = result_checks.check_files(
                self._payload(t), write=False,
                run_faults=[("ledger_write_failed", "0 of 1 picks reached the "
                             "signal ledger")])
        errs = {i["code"] for i in rep["items"] if i["level"] == "error"}
        self.assertIn("ledger_write_failed", errs)
        self.assertEqual(rep["status"], "fail")

    def test_a_stamped_payload_with_a_run_fault_is_provisional(self):
        from scanner import result_checks
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            p = self._payload(t)
            result_checks.check_files(
                p, run_faults=[("ledger_write_failed", "x")], write=True)
            meta = json.loads(p.read_text(encoding="utf-8"))["meta"]
        self.assertEqual(meta["list_status"]["state"], "provisional")
        self.assertIn("checks_fail", meta["list_status"]["reasons"])

    def test_a_failed_mobile_json_raises_after_the_csv_is_written(self):
        import pandas as pd
        import scanner.result_export as rx
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            csv = t / "scan_result.csv"
            with mock.patch.object(rx, "SCAN_RESULTS_DIR", t),                     mock.patch.object(rx, "SCAN_RESULT_FILE", csv),                     mock.patch.object(rx, "export_scan_result_json",
                                      side_effect=ValueError("disk full")):
                with self.assertRaises(RuntimeError):
                    rx.export_scan_result(pd.DataFrame([{"Stock_ID": "1"}]), "m")
            self.assertTrue(csv.exists())

    def test_the_marker_is_only_for_this_runs_session(self):
        import pandas as pd
        import config.settings as cfg
        from scanner import signal_ledger as sl
        from tests.test_list_freeze import final_block
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            scan = t / "scan_result.json"
            block = final_block()
            scan.write_text(json.dumps({"meta": {"list_status": block}}),
                            encoding="utf-8")
            df = pd.DataFrame([{"Stock_ID": "6426", "Buy_Ready": True}])
            db = t / "led.db"
            with mock.patch.object(cfg, "MOBILE_DATA_FILE", scan),                     mock.patch.object(sl, "SIGNAL_LEDGER_FILE", db):
                other = scan_headless._record_list_session(
                    "mode_prelaunch", df, None, session="2099-01-01")
                same = scan_headless._record_list_session(
                    "mode_prelaunch", df, None, session=block["session"])
        self.assertIsNone(other)
        self.assertEqual(same, "inserted")


if __name__ == "__main__":
    unittest.main()
