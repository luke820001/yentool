"""
Fail-closed pins from the 2026-10-09 conformance audit (D6B-02).

A recommendation step that failed used to be invisible: the scan printed
"skipped", the list could freeze final, and a buy had no frozen entry / stop /
target record behind it. Now a failed step leaves meta.rec.error and the
checker turns that, or a Buy_Ready row without its record, into an ERROR (a
degraded run, which writes nothing by design, only warns). ASCII only.

    python -m unittest tests.test_failclosed_recs_20261009 -v
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scan_headless as sh
from portfolio.sync import rec_meta
from scanner.result_checks import check_payload
from tests.test_result_checks import clean_payload, clean_quotes, clean_row, codes


def _payload(rec, buy_ready=True, rid=None, degraded=None):
    row = clean_row()
    row["Buy_Ready"] = buy_ready
    row["Recommendation_ID"] = rid
    p = clean_payload([row])
    p["meta"]["rec"] = rec
    p["meta"]["degraded"] = degraded
    return p


def _check(p):
    return check_payload(p, quotes=clean_quotes(p["rows"]))


def _rec(**kw):
    out = {"created": 0, "attached": 0, "closed": 0, "expired": 0,
           "superseded": 0, "backfilled": 0, "moved": 0, "deferred": 0,
           "writes": True, "advanced": True}
    out.update(kw)
    return out


class TheCheckerSeesARecFailure(unittest.TestCase):
    def test_a_buy_without_its_record_is_an_error(self):
        rep = _check(_payload(_rec()))
        self.assertIn("buy_ready_without_recommendation", codes(rep, "error"))

    def test_a_buy_with_its_record_is_clean(self):
        rep = _check(_payload(_rec(created=1), rid="R-1"))
        self.assertNotIn("buy_ready_without_recommendation", codes(rep))

    def test_no_buy_no_complaint(self):
        rep = _check(_payload(_rec(), buy_ready=False))
        self.assertNotIn("buy_ready_without_recommendation", codes(rep))

    def test_a_read_only_run_is_not_blamed(self):
        # writes False: the export was unreadable (its own error says so)
        rep = _check(_payload(_rec(writes=False)))
        self.assertNotIn("buy_ready_without_recommendation", codes(rep))

    def test_meta_rec_error_is_an_error(self):
        rep = _check(_payload(_rec(error="attach: database is locked"), rid="R-1"))
        self.assertIn("rec_failed", codes(rep, "error"))

    def test_a_degraded_run_only_warns(self):
        p = _payload(_rec(error="attach: database is locked", writes=False),
                     degraded="feed degraded: OTC")
        rep = _check(p)
        self.assertIn("rec_failed", codes(rep, "warn"))
        self.assertNotIn("rec_failed", codes(rep, "error"))
        self.assertNotIn("buy_ready_without_recommendation", codes(rep))

    def test_a_payload_from_before_the_lifecycle_is_left_alone(self):
        p = _payload(_rec())
        del p["meta"]["rec"]
        self.assertNotIn("buy_ready_without_recommendation", codes(_check(p)))


class RecMetaCarriesTheFailure(unittest.TestCase):
    def test_an_advance_error_is_reported_and_not_called_advanced(self):
        m = rec_meta({"created": 0, "writes": True}, {"error": "advance: boom"})
        self.assertIn("boom", m["error"])
        self.assertFalse(m["advanced"])

    def test_a_clean_advance_is_advanced(self):
        m = rec_meta({"created": 0, "writes": True}, {"closed": 1})
        self.assertTrue(m["advanced"])
        self.assertNotIn("error", m)


class TheScanRecordsTheFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.ledger, self.export = t / "l.db", t / "recs.json"
        del sh._RUN_FAULTS[:]
        self.addCleanup(lambda: sh._RUN_FAULTS.clear())

    def _patched(self):
        return (mock.patch.object(sh, "PORTFOLIO_LEDGER_FILE", self.ledger),
                mock.patch.object(sh, "RECOMMENDATIONS_EXPORT_FILE", self.export))

    def test_attach_failure_lands_in_stats_error(self):
        import pandas as pd
        a, b = self._patched()
        boom = mock.Mock(side_effect=RuntimeError("database is locked"))
        with a, b, mock.patch.object(sh, "attach_recommendations", boom):
            df, anchors, stats = sh._create_recommendations(
                pd.DataFrame([{"Stock_ID": "8069"}]), "mode_prelaunch",
                "2026-09-09", None, {})
        self.assertIn("database is locked", stats["error"])
        self.assertEqual(rec_meta(stats, None)["error"], stats["error"])

    def test_advance_failure_is_returned_as_an_error_stat(self):
        a, b = self._patched()
        boom = mock.Mock(side_effect=RuntimeError("database is locked"))
        with a, b, mock.patch.object(sh, "advance_recommendations", boom):
            anchors, adv = sh._prepare_recommendations(
                "mode_prelaunch", "2026-09-09", None)
        self.assertIn("locked", adv["error"])

    def test_export_failure_is_a_run_fault(self):
        a, b = self._patched()
        boom = mock.Mock(side_effect=RuntimeError("disk full"))
        with a, b, mock.patch.object(sh, "export_recommendations", boom):
            sh._finish_recommendations(None, "mode_prelaunch", "2026-09-09", None)
        self.assertEqual([c for c, _ in sh._RUN_FAULTS], ["rec_export_failed"])

    def test_a_degraded_run_exports_nothing_and_faults_nothing(self):
        a, b = self._patched()
        boom = mock.Mock(side_effect=RuntimeError("disk full"))
        with a, b, mock.patch.object(sh, "export_recommendations", boom):
            sh._finish_recommendations(None, "mode_prelaunch", "2026-09-09",
                                       "feed degraded: OTC")
        self.assertEqual(sh._RUN_FAULTS, [])


class TheRunHandsTheFaultsToTheChecker(unittest.TestCase):
    def test_run_scan_clears_then_forwards_the_faults(self):
        import inspect
        src = inspect.getsource(sh.run_scan)
        self.assertIn("del _RUN_FAULTS[:]", src)
        self.assertIn("run_faults = list(_RUN_FAULTS)", src)
        self.assertLess(src.index("del _RUN_FAULTS[:]"),
                        src.index("_prepare_recommendations("))
        self.assertLess(src.index("_finish_recommendations("),
                        src.index("run_faults = list(_RUN_FAULTS)"))


if __name__ == "__main__":
    unittest.main()
