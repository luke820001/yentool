"""
Tests for the final-once list freeze (scanner/list_freeze.py, 2026-10-08,
plan P0-5 / P1-9): the finality predicate, meta.list_status, the ledger's
list_sessions marker, the gates in scan_headless, the session-aware
hysteresis state, the data-session picks key, the stable tie-break, the
evening restriction amend, and the workflow / timer wiring.

No network: Pages and the restriction feeds are stubbed. Every file lives in
a temp dir.

    python -m unittest tests.test_list_freeze -v
"""
import copy
import gc
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

import scanner.list_freeze as lf
import scanner.signal_ledger as sl
import scanner.scan_state as ss
import scanner.trade_restrictions as tr
from scanner.result_checks import check_files, check_payload
from tests.test_result_checks import (DATE, clean_payload, clean_quotes,
                                      clean_row, codes, rec_item, rec_row)

ROOT = Path(__file__).resolve().parent.parent
MODE = "mode_prelaunch"
NEXT = "2026-09-15"
NO_PAGES_ENV = {"PAGES_URL": "", "GITHUB_REPOSITORY": "",
                "YENTOOL_FORCE_RESCAN": ""}


def good_meta(**changes):
    m = {"session_date": DATE, "data_date": DATE, "degraded": None,
         "checks": {"status": "ok"}, "regime": {"ok": True, "is_current": True},
         "count": 3, "empty_ok": False, "scan_time": DATE + " 15:05:00",
         "strategy_version": "prelaunch-test", "mode": MODE}
    m.update(changes)
    return m


def final_block(session=DATE, published=DATE + " 15:05:00", revision=1,
                **changes):
    b = lf.list_status_block(good_meta(session_date=session, data_date=session,
                                       scan_time=published))
    b["revision"] = revision
    b.update(changes)
    return b


def ledger_row(session=DATE, revision=1, published=DATE + " 15:05:00"):
    return {"scan_session": session, "scan_mode": MODE, "state": "final",
            "published_at": published, "first_published_at": published,
            "revision": revision, "revised_reason": None, "revised_at": None,
            "reasons": [], "buy_ready_ids": []}


def pages_payload(block):
    return {"meta": {"session_date": block["session"], "list_status": block},
            "rows": []}


# --------------------------------------------------------------------------
# finality / list_status_block
# --------------------------------------------------------------------------
class TestFinality(unittest.TestCase):
    def test_a_good_meta_is_final(self):
        self.assertEqual(lf.finality(good_meta()), ("final", []))

    def test_a_warn_is_still_final(self):
        self.assertEqual(lf.finality(good_meta(checks={"status": "warn"}))[0],
                         "final")

    def test_each_reason(self):
        cases = {
            "data_lag": good_meta(data_date="2026-09-11"),
            "degraded": good_meta(degraded="feed degraded: OTC"),
            "unchecked": good_meta(checks=None),
            "checks_fail": good_meta(checks={"status": "fail"}),
            "regime_stale": good_meta(regime={"ok": True, "is_current": False}),
            "regime_unknown": good_meta(regime={"ok": False, "is_current": True}),
            "empty_unflagged": good_meta(count=0, empty_ok=False),
            "too_early": good_meta(scan_time=DATE + " 14:58:00"),
        }
        for reason, meta in cases.items():
            state, reasons = lf.finality(meta)
            self.assertEqual(state, "provisional", reason)
            self.assertEqual(reasons, [reason])
            self.assertIn(reason, lf.REASON_CODES)

    def test_an_empty_flagged_day_and_the_boundary_minute_are_final(self):
        self.assertEqual(lf.finality(good_meta(count=0, empty_ok=True))[0], "final")
        self.assertEqual(lf.finality(good_meta(scan_time=DATE + " 15:00:00"))[0],
                         "final")
        # the next calendar day still counts (a late run of the same data)
        self.assertEqual(lf.finality(good_meta(scan_time="2026-09-15 01:10:00"))[0],
                         "final")

    def test_no_session_is_never_final(self):
        state, reasons = lf.finality(good_meta(session_date="", data_date=""))
        self.assertEqual(state, "provisional")
        self.assertIn("data_lag", reasons)


class TestListStatusBlock(unittest.TestCase):
    def test_seed_is_provisional_unchecked(self):
        b = lf.seed_block(good_meta())
        self.assertEqual(b["state"], "provisional")
        self.assertEqual(b["reasons"], ["unchecked"])
        self.assertEqual(b["revision"], 1)
        self.assertEqual(b["policy"], lf.POLICY)
        self.assertEqual(b["first_published_at"], DATE + " 15:05:00")

    def test_fields(self):
        b = lf.list_status_block(good_meta())
        self.assertEqual(set(b), {"policy", "state", "reasons", "session",
                                  "published_at", "first_published_at",
                                  "revision", "strategy_version",
                                  "revised_reason", "revised_at"})
        self.assertEqual((b["state"], b["session"], b["revision"]),
                         ("final", DATE, 1))
        self.assertEqual(b["strategy_version"], "prelaunch-test")

    def test_same_publish_carries(self):
        prev = final_block(revision=2, revised_reason="force_rescan",
                           revised_at=DATE + " 18:00:00",
                           first_published_at=DATE + " 15:05:00",
                           published_at=DATE + " 18:00:00")
        b = lf.list_status_block(good_meta(scan_time=DATE + " 18:00:00"), prev=prev)
        self.assertEqual(b["revision"], 2)
        self.assertEqual(b["revised_reason"], "force_rescan")
        self.assertEqual(b["first_published_at"], DATE + " 15:05:00")

    def test_revised_reason_bumps(self):
        prev = final_block()
        b = lf.list_status_block(good_meta(scan_time=DATE + " 19:10:00"),
                                 prev=prev, revised_reason="force_rescan",
                                 now=datetime(2026, 9, 14, 19, 11))
        self.assertEqual(b["revision"], 2)
        self.assertEqual(b["revised_reason"], "force_rescan")
        self.assertEqual(b["revised_at"], "2026-09-14 19:11:00")
        self.assertEqual(b["first_published_at"], DATE + " 15:05:00")
        self.assertEqual(b["published_at"], DATE + " 19:10:00")

    def test_amend_of_the_same_publish_still_bumps(self):
        """restriction_info keeps scan_time; the revision must move anyway."""
        prev = final_block()
        b = lf.list_status_block(good_meta(), prev=prev,
                                 revised_reason="restriction_info",
                                 now="2026-09-14 23:40:00")
        self.assertEqual((b["revision"], b["revised_reason"], b["revised_at"]),
                         (2, "restriction_info", "2026-09-14 23:40:00"))

    def test_new_session_resets(self):
        prev = final_block(session="2026-09-11", revision=3,
                           published="2026-09-11 15:05:00")
        b = lf.list_status_block(good_meta(), prev=prev)
        self.assertEqual(b["revision"], 1)
        self.assertIsNone(b["revised_reason"])
        self.assertEqual(b["first_published_at"], DATE + " 15:05:00")

    def test_a_provisional_prev_of_another_publish_resets(self):
        prev = lf.list_status_block(good_meta(scan_time=DATE + " 14:50:00"))
        self.assertEqual(prev["state"], "provisional")
        b = lf.list_status_block(good_meta(scan_time=DATE + " 16:15:00"), prev=prev)
        self.assertEqual(b["first_published_at"], DATE + " 16:15:00")
        self.assertEqual(b["revision"], 1)

    def test_pick_prev_takes_the_highest_revision(self):
        a = final_block(revision=1)
        b = final_block(revision=3, state="provisional")
        other = final_block(session="2026-09-11", revision=9)
        self.assertEqual(lf.pick_prev(DATE, a, b, other, None)["revision"], 3)
        self.assertIsNone(lf.pick_prev(DATE, other))


# --------------------------------------------------------------------------
# ledger: list_sessions / picks
# --------------------------------------------------------------------------
class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "signal_ledger.db"

    def tearDown(self):
        gc.collect()        # record_picks leaves its connection to the GC
        self.tmp.cleanup()

    def files(self):
        return sorted(p.name for p in Path(self.tmp.name).iterdir())

    def picks(self, session=DATE, sids=("6426", "8069")):
        rows = [{"Stock_ID": s, "Stock_Name": "T", "Market": "OTC",
                 "Data_Date": session, "Close_Price": 100.0,
                 "Buy_Ready": i == 0, "Buy_Block": None if i == 0 else "rank",
                 "Trade_Restriction": "none", "Restriction_Until": None}
                for i, s in enumerate(sids)]
        with mock.patch.object(sl, "SIGNAL_LEDGER_FILE", self.db), \
                mock.patch("scanner.market_regime.get_market_regime",
                           lambda *a, **k: {"ok": True}):
            return sl.record_picks(pd.DataFrame(rows), MODE, scan_session=session)


class TestListSessions(LedgerCase):
    def test_final_is_never_downgraded(self):
        self.assertEqual(sl.record_list_session(MODE, final_block(), rows=3,
                                                path=self.db), "inserted")
        prov = lf.list_status_block(good_meta(checks={"status": "fail"},
                                              scan_time=DATE + " 19:00:00"))
        self.assertEqual(sl.record_list_session(MODE, prov, path=self.db), "refused")
        row = sl.final_list_session(MODE, DATE, path=self.db)
        self.assertEqual((row["state"], row["published_at"]),
                         ("final", DATE + " 15:05:00"))

    def test_overwriting_final_needs_a_reason(self):
        sl.record_list_session(MODE, final_block(), path=self.db)
        later = final_block(published=DATE + " 19:10:00")
        self.assertEqual(sl.record_list_session(MODE, later, path=self.db), "refused")
        self.assertEqual(sl.final_list_session(MODE, DATE, path=self.db)["revision"], 1)
        later = final_block(published=DATE + " 19:10:00", revision=2,
                            revised_reason="force_rescan",
                            first_published_at=DATE + " 19:10:00")
        self.assertEqual(sl.record_list_session(MODE, later, path=self.db), "revised")
        row = sl.final_list_session(MODE, DATE, path=self.db)
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["revised_reason"], "force_rescan")
        self.assertEqual(row["first_published_at"], DATE + " 15:05:00")

    def test_same_publish_is_a_no_op(self):
        sl.record_list_session(MODE, final_block(), path=self.db)
        self.assertEqual(sl.record_list_session(MODE, final_block(), path=self.db),
                         "same")

    def test_missing_file_or_table_reads_none_and_creates_nothing(self):
        self.assertIsNone(sl.final_list_session(MODE, path=self.db))
        self.assertEqual(self.files(), [])
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE picks (x TEXT)")
        conn.commit()
        conn.close()
        self.assertIsNone(sl.final_list_session(MODE, path=self.db))
        self.assertEqual(self.files(), ["signal_ledger.db"])

    def test_reads_leave_no_wal_files(self):
        sl.record_list_session(MODE, final_block(), path=self.db)
        before = self.files()
        self.assertIsNotNone(sl.final_list_session(MODE, path=self.db))
        self.assertEqual(self.files(), before)
        self.assertEqual(before, ["signal_ledger.db"])

    def test_newest_final(self):
        sl.record_list_session(MODE, final_block("2026-09-11",
                                                 "2026-09-11 15:05:00"), path=self.db)
        sl.record_list_session(MODE, final_block(), path=self.db)
        prov = lf.list_status_block(good_meta(session_date="2026-09-15",
                                              data_date="2026-09-15",
                                              scan_time="2026-09-15 14:50:00"))
        sl.record_list_session(MODE, prov, path=self.db)
        self.assertEqual(sl.final_list_session(MODE, path=self.db)["scan_session"], DATE)
        self.assertEqual(sl.final_list_session(MODE, "2026-09-11",
                                               path=self.db)["scan_session"],
                         "2026-09-11")
        self.assertIsNone(sl.final_list_session(MODE, "2026-09-15", path=self.db))
        self.assertIsNone(sl.final_list_session("mode_other", path=self.db))

    def test_picks_ts_and_ids(self):
        self.picks()
        sl.record_list_session(MODE, final_block(), rows=2, buy_ready_ids=["6426"],
                               run_id="77", path=self.db)
        row = sl.final_list_session(MODE, DATE, path=self.db)
        self.assertTrue(row["picks_ts"])
        self.assertEqual(row["buy_ready_ids"], ["6426"])
        self.assertEqual((row["rows"], row["run_id"]), (2, "77"))

    def test_amend_picks_keeps_scan_ts(self):
        self.picks()
        conn = sqlite3.connect(str(self.db))
        before = conn.execute("SELECT stock_id, scan_ts FROM picks "
                              "ORDER BY stock_id").fetchall()
        conn.close()
        n = sl.amend_picks(DATE, MODE, {
            "6426": {"restriction": "disposition", "restriction_until": "2026-09-28",
                     "buy_ready": 0, "buy_block": "restricted"},
            "9999": {"restriction": "attention"}}, path=self.db)
        self.assertEqual(n, 1)
        conn = sqlite3.connect(str(self.db))
        after = conn.execute("SELECT stock_id, scan_ts FROM picks "
                             "ORDER BY stock_id").fetchall()
        row = conn.execute("SELECT gate_detail, buy_ready, buy_block FROM picks "
                           "WHERE stock_id = '6426'").fetchone()
        conn.close()
        self.assertEqual(before, after)
        gate = json.loads(row[0])
        self.assertEqual((gate["restriction"], gate["restriction_until"]),
                         ("disposition", "2026-09-28"))
        self.assertEqual((row[1], row[2]), (0, "restricted"))


class TestRecordPicksDataSession(LedgerCase):
    def test_keyed_on_the_data_session_after_midnight(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 15, 1, 0, 0)
        with mock.patch.object(sl, "datetime", Clock):
            self.assertEqual(self.picks(), 2)
            self.assertEqual(self.picks(), 2)          # replaced, not doubled
        conn = sqlite3.connect(str(self.db))
        got = conn.execute("SELECT DISTINCT scan_session, scan_ts FROM picks").fetchall()
        n = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        conn.close()
        self.assertEqual(got, [(DATE, "2026-09-15 01:00:00")])
        self.assertEqual(n, 2)

    def test_scan_headless_passes_the_data_session(self):
        import inspect
        import scan_headless as sh
        src = inspect.getsource(sh.run_scan)
        self.assertIn("record_picks(result_df, scan_mode,\n"
                      "                             scan_session=session_date or None)",
                      src)


# --------------------------------------------------------------------------
# check_files stamps list_status
# --------------------------------------------------------------------------
class TestCheckFilesStamp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload):
        scan = self.dir / "scan_result.json"
        quotes = self.dir / "quotes.json"
        recs = self.dir / "recommendations.json"
        scan.write_text(json.dumps(payload), encoding="utf-8")
        quotes.write_text(json.dumps(clean_quotes(payload["rows"])), encoding="utf-8")
        recs.write_text(json.dumps({"count": 0, "recommendations": []}),
                        encoding="utf-8")
        return scan, quotes, recs

    def stamp(self, payload, **kw):
        scan, quotes, recs = self.write(payload)
        rep = check_files(scan, quotes_path=quotes, recs_path=recs, **kw)
        meta = json.loads(scan.read_text(encoding="utf-8"))["meta"]
        return rep, meta

    def test_clean_file_is_final(self):
        rep, meta = self.stamp(clean_payload())
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(meta["list_status"]["state"], "final")
        self.assertEqual(meta["list_status"]["session"], DATE)
        self.assertEqual(meta["checks"]["status"], "ok")

    def test_failed_checks_are_provisional(self):
        p = clean_payload()
        p["meta"]["count"] = 5                   # count_mismatch is an error
        rep, meta = self.stamp(p)
        self.assertEqual(rep["status"], "fail")
        self.assertEqual(meta["list_status"]["state"], "provisional")
        self.assertIn("checks_fail", meta["list_status"]["reasons"])

    def test_restamp_of_the_same_file_carries_the_revision(self):
        p = clean_payload()
        prev = final_block(published=DATE + " 12:00:00")
        _, meta = self.stamp(p, revised_reason="force_rescan", list_prev=prev)
        self.assertEqual(meta["list_status"]["revision"], 2)
        p["meta"] = meta                          # the workflow's self-check
        _, again = self.stamp(p)
        self.assertEqual(again["list_status"]["revision"], 2)
        self.assertEqual(again["list_status"]["revised_reason"], "force_rescan")

    def test_no_write_no_stamp(self):
        scan, quotes, recs = self.write(clean_payload())
        check_files(scan, quotes_path=quotes, recs_path=recs, write=False)
        self.assertNotIn("list_status",
                         json.loads(scan.read_text(encoding="utf-8"))["meta"])

    def test_session_mismatch_is_an_error(self):
        p = clean_payload()
        p["meta"]["list_status"] = final_block(session="2026-09-11",
                                               published="2026-09-11 15:05:00")
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("list_status_session_mismatch", codes(rep, "error"))
        p["meta"]["list_status"] = final_block(state="frozen")
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("list_status_bad_state", codes(rep, "error"))
        p["meta"]["list_status"] = final_block()
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertFalse({"list_status_session_mismatch",
                          "list_status_bad_state"} & codes(rep))

    def test_export_seeds_a_provisional_block(self):
        import scanner.result_export as rx
        out = self.dir / "scan_result.json"
        with mock.patch.object(rx, "MOBILE_DIR", self.dir), \
                mock.patch.object(rx, "MOBILE_DATA_FILE", out), \
                mock.patch.object(rx, "_publish_quotes", lambda df, names=None: {}), \
                mock.patch.object(rx, "_regime", lambda: {"ok": True}), \
                mock.patch.object(rx, "_calendar_tail", lambda: []):
            rx.export_scan_result_json(pd.DataFrame([clean_row()]), MODE,
                                       scan_time=DATE + " 15:05:00",
                                       session_date=DATE)
        ls = json.loads(out.read_text(encoding="utf-8"))["meta"]["list_status"]
        self.assertEqual((ls["state"], ls["reasons"], ls["session"]),
                         ("provisional", ["unchecked"], DATE))


# --------------------------------------------------------------------------
# scan_state
# --------------------------------------------------------------------------
class TestScanState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(ss, "_STATE_DIR", Path(self.tmp.name))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def raw(self):
        return json.loads(ss._path(MODE).read_text(encoding="utf-8"))

    def test_old_format(self):
        ss._path(MODE).write_text('{"held_ids": [1, 2]}', encoding="utf-8")
        st = ss.load_state(MODE)
        self.assertIsNone(st["session"])
        self.assertIsNone(st["prior_ids"])
        self.assertEqual(ss.prior_for(st, DATE), ["1", "2"])

    def test_same_session_keeps_its_prior(self):
        ss.save_state(MODE, DATE, ["A"], prior_ids=["P"])
        ss.save_state(MODE, DATE, ["B"], prior_ids=["A"])
        st = ss.load_state(MODE)
        self.assertEqual((st["held_ids"], st["prior_ids"]), (["B"], ["P"]))
        self.assertEqual(ss.prior_for(st, DATE), ["P"])
        self.assertEqual(ss.prior_for(st, NEXT), ["B"])

    def test_new_session_rolls(self):
        ss.save_state(MODE, DATE, ["A"], prior_ids=["P"])
        ss.save_state(MODE, DATE, ["B"])
        ss.save_state(MODE, NEXT, ["C"])
        st = ss.load_state(MODE)
        self.assertEqual((st["held_ids"], st["session"]), (["C"], NEXT))
        self.assertEqual((st["prior_ids"], st["prior_session"]), (["B"], DATE))

    def test_an_older_session_never_overwrites(self):
        ss.save_state(MODE, NEXT, ["C"], prior_ids=["B"])
        self.assertFalse(ss.save_state(MODE, DATE, ["X"]))
        self.assertEqual(ss.load_state(MODE)["held_ids"], ["C"])

    def test_save_held_ids_keeps_the_other_keys(self):
        ss.save_state(MODE, DATE, ["A"], prior_ids=["P"])
        ss.save_held_ids(MODE, ["G"])
        raw = self.raw()
        self.assertEqual(raw["held_ids"], ["G"])
        self.assertEqual((raw["session"], raw["prior_ids"]), (DATE, ["P"]))
        self.assertEqual(ss.load_held_ids(MODE), ["G"])


# --------------------------------------------------------------------------
# stable tie-break
# --------------------------------------------------------------------------
class TestTieBreak(unittest.TestCase):
    def test_equal_scores_sort_by_stock_id(self):
        from scanner.scan_mode import sort_for_mode
        rows = [{"Stock_ID": "6805", "Launch_Score": 71.3},
                {"Stock_ID": "9999", "Launch_Score": 80.0},
                {"Stock_ID": "3498", "Launch_Score": 71.3},
                {"Stock_ID": "1101", "Launch_Score": None}]
        want = ["9999", "3498", "6805", "1101"]
        for order in (rows, rows[::-1], rows[1:] + rows[:1]):
            got = sort_for_mode(pd.DataFrame(order), MODE)["Stock_ID"].tolist()
            self.assertEqual(got, want)
        self.assertNotIn("_sort_sid", sort_for_mode(pd.DataFrame(rows), MODE).columns)


# --------------------------------------------------------------------------
# the gate decision and the Pages helpers
# --------------------------------------------------------------------------
class TestDecide(unittest.TestCase):
    def pg(self, block):
        return lf.pages_status(pages_payload(block))

    def test_ledger_and_pages_final(self):
        d = lf.decide(DATE, ledger_row(), self.pg(final_block()))
        self.assertEqual((d["action"], d["reason"]), ("frozen", "final"))

    def test_ledger_final_pages_unreachable(self):
        d = lf.decide(DATE, ledger_row(), None)
        self.assertEqual((d["action"], d["reason"]), ("frozen", "final_ledger"))

    def test_pages_behind_rescans(self):
        older = final_block(session="2026-09-11", published="2026-09-11 15:05:00")
        d = lf.decide(DATE, ledger_row(), self.pg(older))
        self.assertEqual((d["action"], d["revised_reason"]), ("scan", "pages_behind"))
        self.assertEqual(d["prev"]["revision"], 1)
        self.assertTrue(d["warning"])
        prov = final_block(state="provisional")
        d = lf.decide(DATE, ledger_row(), self.pg(prov))
        self.assertEqual(d["revised_reason"], "pages_behind")
        # a payload from before the freeze has no list status: not final
        d = lf.decide(DATE, ledger_row(), lf.pages_status(
            {"meta": {"session_date": DATE}, "rows": []}))
        self.assertEqual(d["revised_reason"], "pages_behind")

    def test_pages_at_an_older_revision_rescans(self):
        """A force_rescan whose deploy never landed: Pages is still final
        for the session, but at the revision before it. Verifier fix: this
        used to stay frozen on the superseded list for good."""
        led = dict(ledger_row(revision=3), revised_reason="force_rescan")
        d = lf.decide(DATE, led, self.pg(final_block(revision=2)))
        self.assertEqual((d["action"], d["reason"], d["revised_reason"]),
                         ("scan", "pages_behind", "pages_behind"))
        self.assertEqual(d["prev"]["revision"], 3)    # the next one is 4
        self.assertIn("revision 2", d["warning"])
        led = dict(ledger_row(revision=2), revised_reason="pages_behind")
        self.assertEqual(lf.decide(DATE, led, self.pg(final_block()))["action"],
                         "scan")

    def test_an_unlanded_restriction_amend_is_left_to_the_amend(self):
        led = dict(ledger_row(revision=2), revised_reason="restriction_info")
        d = lf.decide(DATE, led, self.pg(final_block(revision=1)))
        self.assertEqual((d["action"], d["reason"]), ("frozen", "final"))

    def test_same_or_newer_pages_revision_stays_frozen(self):
        led = dict(ledger_row(revision=2), revised_reason="force_rescan")
        for rev in (2, 3):
            d = lf.decide(DATE, led, self.pg(final_block(revision=rev)))
            self.assertEqual((d["action"], d["reason"]), ("frozen", "final"), rev)

    def test_pages_final_ledger_missing(self):
        d = lf.decide(DATE, None, self.pg(final_block()))
        self.assertEqual((d["action"], d["reason"]), ("frozen", "final_pages"))
        self.assertIn("ledger missing final marker for " + DATE, d["warning"])

    def test_nothing_final(self):
        d = lf.decide(DATE, None, None)
        self.assertEqual((d["action"], d["revised_reason"]), ("scan", None))
        older = final_block(session="2026-09-11", published="2026-09-11 15:05:00")
        self.assertEqual(lf.decide(DATE, None, self.pg(older))["action"], "scan")

    def test_pages_ahead(self):
        d = lf.decide("2026-09-11", None, self.pg(final_block()))
        self.assertEqual((d["action"], d["reason"]), ("frozen", "pages_ahead"))

    def test_force(self):
        d = lf.decide(DATE, ledger_row(revision=2), self.pg(final_block()),
                      forced=True)
        self.assertEqual((d["action"], d["revised_reason"]), ("scan", "force_rescan"))
        self.assertEqual(d["prev"]["revision"], 2)
        d = lf.decide(DATE, None, None, forced=True)
        self.assertEqual((d["action"], d["revised_reason"]), ("scan", None))

    def test_force_env(self):
        self.assertTrue(lf.force_requested({"YENTOOL_FORCE_RESCAN": "1"}))
        self.assertTrue(lf.force_requested({"YENTOOL_FORCE_RESCAN": "true"}))
        self.assertFalse(lf.force_requested({"YENTOOL_FORCE_RESCAN": ""}))
        self.assertFalse(lf.force_requested({}))


class TestPages(unittest.TestCase):
    def test_pages_url(self):
        self.assertEqual(lf.pages_url({"PAGES_URL": "https://o.github.io/yentool/"}),
                         "https://o.github.io/yentool")
        # an empty github.event.repository on a scheduled event
        self.assertEqual(lf.pages_url({"PAGES_URL": "https://o.github.io/",
                                       "GITHUB_REPOSITORY": "Owner/yentool"}),
                         "https://owner.github.io/yentool")
        self.assertIsNone(lf.pages_url({}))
        self.assertIsNone(lf.pages_url({"PAGES_URL": "https://o.github.io/"}))

    def test_fetch(self):
        seen = {}

        class R(object):
            def __init__(self, code, body):
                self.status_code, self.content = code, body

        def ok(url, params=None, timeout=None, headers=None):
            seen.update(url=url, params=params, timeout=timeout)
            return R(200, json.dumps({"meta": {"session_date": DATE}}).encode())
        doc = lf.fetch_pages("https://o.github.io/yentool", get=ok)
        self.assertEqual(doc["meta"]["session_date"], DATE)
        self.assertEqual(seen["url"], "https://o.github.io/yentool/scan_result.json")
        self.assertIn("t", seen["params"])
        self.assertEqual(seen["timeout"], lf.PAGES_TIMEOUT)

        def missing(url, **kw):
            return R(404, b"")

        def boom(url, **kw):
            raise IOError("down")
        self.assertIsNone(lf.fetch_pages("https://x", get=missing))
        self.assertIsNone(lf.fetch_pages("https://x", get=boom))
        self.assertIsNone(lf.fetch_pages(None, get=ok))

    def test_every_gitignored_mobile_data_file_is_republished(self):
        """The amend uploads mobile/ as checked out plus what it downloads;
        anything gitignored there and missing from PAGES_DATA_FILES would
        vanish from the site."""
        ignored = set()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
            m = re.match(r"^mobile/([^/*]+\.json)\s*$", line.strip())
            if m:
                ignored.add(m.group(1))
        self.assertTrue(ignored)
        self.assertLessEqual(ignored, set(lf.PAGES_DATA_FILES))


# --------------------------------------------------------------------------
# scan_headless: gates, exit codes, source order
# --------------------------------------------------------------------------
def scanned_frame():
    return pd.DataFrame([{"Stock_ID": "6426", "Stock_Name": "T", "Market": "OTC",
                          "Data_Date": DATE, "Launch_Score": 80.0,
                          "Close_Price": 100.0},
                         {"Stock_ID": "8069", "Stock_Name": "U", "Market": "OTC",
                          "Data_Date": DATE, "Launch_Score": 70.0,
                          "Close_Price": 50.0}])


class GateRun(unittest.TestCase):
    """run_scan with the market data layer and every writer stubbed."""

    def run_scan(self, final_rows, env=None, pages=None, clock=DATE,
                 applied=None, verified=None, select=None):
        """`applied` replaces apply_scan_mode (e.g. nothing qualifies),
        `verified` the frame verify_candidates returns, `select` the
        hysteresis selection (df, prior) -> (df, held_ids)."""
        import scan_headless as sh
        self.m = {}
        frame = scanned_frame()
        checked = frame if verified is None else verified

        def final(mode, session=None, path=None):
            if session is None:
                rows = sorted(final_rows.values(), key=lambda r: r["scan_session"])
                return rows[-1] if rows else None
            return final_rows.get(session)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with ExitStack() as st:
            def patch(target, name, value=None, **kw):
                mk = value if value is not None else mock.MagicMock(**kw)
                st.enter_context(mock.patch.object(target, name, mk))
                self.m[name] = mk
                return mk
            import scanner.market_snapshot as msnap
            import scanner.tracked_rows as trk
            import scanner.chip_verifier as cv
            import scanner.chip_signal as chip
            import scanner.live_record as lr
            import scanner.quote_feed as qf
            import scanner.data_integrity as di
            import scanner.universe_export as ux
            import scanner.result_checks as rc
            import config.settings as cfg
            e = dict(NO_PAGES_ENV)
            e.update(env or {})
            st.enter_context(mock.patch.dict(os.environ, e))
            patch(lf, "fetch_pages", mock.MagicMock(return_value=pages))
            patch(cv, "_latest_trading_day", mock.MagicMock(return_value=clock))
            patch(sh, "final_list_session", final)
            patch(msnap, "refresh_market", return_value=(0, {"frame": None}))
            patch(msnap, "backfill_history")
            # display-only fetchers (2026-10-08): no network, no real cache
            import ingestion.otc_index as otc
            import ingestion.company_events as cev
            patch(otc, "refresh", return_value={})
            patch(cev, "refresh", return_value=None)
            patch(trk, "recent_pick_ids", return_value={})
            # like market_filter.apply_prefilter: no bar dates yet (only the
            # verified frame carries Data_Date)
            patch(sh, "get_candidate_list",
                  return_value=frame.drop(columns=["Data_Date"]).copy())
            patch(sh, "get_feed_health", return_value={"ok": True})
            patch(sh, "verify_candidates", return_value=checked.copy())
            patch(sh, "apply_scan_mode",
                  side_effect=applied or (lambda df, mode: df))
            patch(sh, "load_state", return_value={
                "held_ids": ["1111"], "session": "2026-09-11",
                "prior_ids": ["2222"], "prior_session": "2026-09-10"})
            patch(sh, "save_state")
            patch(sh, "select_with_hysteresis",
                  side_effect=select or (lambda df, prior: (df, ["6426"])))
            patch(sh, "add_trade_columns", side_effect=lambda df, mode: df)
            patch(sh, "_prepare_recommendations", return_value=({}, None))
            patch(sh, "annotate_holding", side_effect=lambda df, *a, **k: df)
            patch(sh, "fetch_restrictions", return_value=None)
            patch(sh, "annotate_restrictions", side_effect=lambda df, *a, **k: df)
            patch(sh, "mark_buy_ready",
                  side_effect=lambda df, *a, **k: df.assign(Buy_Ready=[True, False],
                                                            Buy_Block=[None, "rank"]))
            patch(sh, "_create_recommendations",
                  side_effect=lambda df, *a: (df, {}, {"created": 0}))
            patch(sh, "attach_recommendations")
            patch(chip, "annotate_chip_action", side_effect=lambda df, mode: df)
            patch(trk, "split_tracked", return_value=None)
            patch(sh, "_finish_recommendations", side_effect=lambda df, *a: df)
            patch(sh, "record_picks", return_value=2)
            patch(sh, "backfill_outcomes", return_value=0)
            patch(lr, "build_live_record", return_value={})
            patch(qf, "refresh_tracked_prices", return_value={})
            patch(di, "purge_nonsession_bars", return_value={})
            patch(cfg, "STOCK_NAMES_FILE", Path(tmp.name) / "none.json")
            patch(ux, "export", return_value=0)
            patch(sh, "export_scan_result", return_value="x")
            patch(sh, "_data_health", return_value={})
            patch(sh, "build_market_reports", return_value={})
            patch(rc, "check_files", return_value={"status": "ok"})
            patch(rc, "format_report", return_value="")
            patch(sh, "_record_list_session")
            patch(lf, "amend_restrictions",
                  return_value=(lf.UNCHANGED, "stub"))
            return sh.run_scan(MODE)

    def assert_nothing_written(self):
        for name in ("save_state", "record_picks", "attach_recommendations",
                     "export_scan_result", "_prepare_recommendations",
                     "_create_recommendations", "check_files",
                     "_record_list_session"):
            self.m[name].assert_not_called()


class TestGateRuns(GateRun):
    def test_pre_gate_freezes_before_any_fetch(self):
        import scan_headless as sh
        self.assertEqual(self.run_scan({DATE: ledger_row()}), sh.FROZEN)
        self.m["refresh_market"].assert_not_called()
        self.m["get_candidate_list"].assert_not_called()
        self.assert_nothing_written()
        self.m["amend_restrictions"].assert_not_called()   # no Pages: nothing to amend

    def test_post_gate_freezes_on_the_data_session(self):
        """The clock expects the next session (one it does not know is a
        holiday) but the bars are DATE's, and DATE is final."""
        import scan_headless as sh
        out = self.run_scan({DATE: ledger_row()}, clock=NEXT)
        self.assertEqual(out, sh.FROZEN)
        self.m["refresh_market"].assert_called_once()
        self.m["verify_candidates"].assert_called_once()
        self.m["select_with_hysteresis"].assert_not_called()
        self.assert_nothing_written()

    def test_force_rescan_bypasses_the_freeze(self):
        import scan_headless as sh
        pages = pages_payload(final_block())
        out = self.run_scan({DATE: ledger_row()},
                            env={"YENTOOL_FORCE_RESCAN": "1",
                                 "PAGES_URL": "https://o.github.io/yentool"},
                            pages=pages)
        self.assertIsInstance(out, pd.DataFrame)
        self.m["refresh_market"].assert_called_once()
        self.m["save_state"].assert_called_once()
        args = self.m["save_state"].call_args[0]
        self.assertEqual(args[1], DATE)
        # a same-session state would start from its prior; here the stored
        # session is older, so the held set is the starting point
        self.assertEqual(args[3], ["1111"])
        self.m["record_picks"].assert_called_once()
        self.assertEqual(self.m["record_picks"].call_args[1]["scan_session"], DATE)
        kw = self.m["check_files"].call_args[1]
        self.assertEqual(kw["revised_reason"], "force_rescan")
        self.assertEqual(kw["list_prev"]["revision"], 1)
        self.m["_record_list_session"].assert_called_once()
        self.m["amend_restrictions"].assert_not_called()

    def test_a_leading_bar_on_a_dropped_name_does_not_move_the_gate(self):
        """E2E-1: the gate's session came from every ranked row, the
        published list from the selection. A leading (partial) bar on a name
        the selection drops made the forced rebuild look up the freeze marker
        under a session nothing is published for: no force_rescan stamp, and
        the hysteresis state saved under the wrong session."""
        extra = pd.DataFrame([{"Stock_ID": "9999", "Stock_Name": "X",
                               "Market": "OTC", "Data_Date": NEXT,
                               "Launch_Score": 10.0, "Close_Price": 20.0}])

        def applied(df, mode):
            return pd.concat([df, extra], ignore_index=True)

        def select(df, prior):
            return df[df["Data_Date"] == DATE].copy(), ["6426"]
        out = self.run_scan({DATE: ledger_row()},
                            env={"YENTOOL_FORCE_RESCAN": "1",
                                 "PAGES_URL": "https://o.github.io/yentool"},
                            pages=pages_payload(final_block()),
                            applied=applied, select=select)
        self.assertIsInstance(out, pd.DataFrame)
        self.assertEqual(set(out["Stock_ID"]), {"6426", "8069"})
        self.assertEqual(self.m["save_state"].call_args[0][1], DATE)
        self.assertEqual(self.m["record_picks"].call_args[1]["scan_session"], DATE)
        kw = self.m["check_files"].call_args[1]
        self.assertEqual(kw["revised_reason"], "force_rescan")
        self.assertEqual(kw["list_prev"]["revision"], 1)
        # judged on the leading date first, then on the published one
        self.assertEqual(self.m["select_with_hysteresis"].call_count, 2)

    def test_a_leading_bar_on_a_dropped_name_still_freezes_a_final_session(self):
        """The same shape without the force flag: the final list of DATE is
        what is published, so the rerun is frozen (the old code, looking up
        NEXT, found no marker and rebuilt the list)."""
        import scan_headless as sh
        extra = pd.DataFrame([{"Stock_ID": "9999", "Stock_Name": "X",
                               "Market": "OTC", "Data_Date": NEXT,
                               "Launch_Score": 10.0, "Close_Price": 20.0}])
        out = self.run_scan(
            {DATE: ledger_row()}, clock=NEXT,
            applied=lambda df, mode: pd.concat([df, extra], ignore_index=True),
            select=lambda df, prior: (df[df["Data_Date"] == DATE].copy(), ["6426"]))
        self.assertEqual(out, sh.FROZEN)
        self.assert_nothing_written()

    def test_not_final_scans_normally(self):
        out = self.run_scan({})
        self.assertIsInstance(out, pd.DataFrame)
        kw = self.m["check_files"].call_args[1]
        self.assertIsNone(kw["revised_reason"])
        self.assertIsNone(kw["list_prev"])
        # candidates include yesterday's held set AND the set it started from
        inc = self.m["get_candidate_list"].call_args[1]["include_ids"]
        self.assertLessEqual({"1111", "2222"}, inc)

    def test_pages_behind_rescans_with_a_revision(self):
        older = final_block(session="2026-09-11", published="2026-09-11 15:05:00")
        out = self.run_scan({DATE: ledger_row()},
                            env={"PAGES_URL": "https://o.github.io/yentool"},
                            pages=pages_payload(older))
        self.assertIsInstance(out, pd.DataFrame)
        self.assertEqual(self.m["check_files"].call_args[1]["revised_reason"],
                         "pages_behind")

    def test_frozen_with_pages_runs_the_amend(self):
        import scan_headless as sh
        pages = pages_payload(final_block())
        self.run_scan({DATE: ledger_row()},
                      env={"PAGES_URL": "https://o.github.io/yentool"}, pages=pages)
        self.m["amend_restrictions"].assert_called_once()
        self.assert_nothing_written()
        self.m["fetch_pages"].assert_called_once()     # one fetch for both

    def test_amended_return(self):
        import scan_headless as sh
        pages = pages_payload(final_block())
        with mock.patch.object(lf, "amend_restrictions",
                               return_value=(lf.AMENDED, "x")):
            # run_scan re-patches amend_restrictions; call _frozen_exit directly
            ctx = sh._FreezeContext(MODE)
            ctx._fetched, ctx._pages = True, pages
            with mock.patch.object(sh, "final_list_session", lambda *a, **k: None):
                self.assertEqual(sh._frozen_exit(MODE, ctx), sh.AMENDED)


class TestEmptyScanGate(GateRun):
    """Verifier fix: the post-gate took its session from the RESULT rows
    only, so a scan where nothing qualified had session '' and was never
    frozen -- on a holiday the clock does not know it republished an empty
    provisional list over the final one, and save_state('') wiped held_ids."""

    @staticmethod
    def nothing(df, mode):
        return df.iloc[0:0]

    def test_nothing_qualified_on_a_final_session_is_frozen(self):
        import scan_headless as sh
        out = self.run_scan({DATE: ledger_row()}, clock=NEXT, applied=self.nothing)
        self.assertEqual(out, sh.FROZEN)
        self.m["select_with_hysteresis"].assert_not_called()
        self.assert_nothing_written()

    def test_no_bar_date_at_all_is_judged_as_the_newest_final(self):
        import scan_headless as sh
        empty = scanned_frame().iloc[0:0]
        out = self.run_scan({"2026-09-11": ledger_row(session="2026-09-11"),
                             DATE: ledger_row()},
                            clock=NEXT, applied=self.nothing, verified=empty)
        self.assertEqual(out, sh.FROZEN)
        self.assert_nothing_written()
        # no ledger marker, but Pages shows a final list: also frozen
        out = self.run_scan({}, clock=NEXT, applied=self.nothing, verified=empty,
                            env={"PAGES_URL": "https://o.github.io/yentool"},
                            pages=pages_payload(final_block()))
        self.assertEqual(out, sh.FROZEN)
        self.assert_nothing_written()

    def test_nothing_qualified_on_a_new_session_scans_on_that_session(self):
        out = self.run_scan({}, applied=self.nothing)
        self.assertIsInstance(out, pd.DataFrame)
        self.m["select_with_hysteresis"].assert_called_once()
        self.assertTrue(self.m["select_with_hysteresis"].call_args[0][0].empty)
        # the verified bars name the session; the old code saved under ''
        self.assertEqual(self.m["save_state"].call_args[0][1], DATE)

    def test_no_bar_date_never_rebuilds_a_final_list(self):
        """T4: a scan with no bar date has nothing to rebuild a final list
        FROM. Whatever decide() says (Pages behind, Pages provisional, an
        older Pages session), the run is frozen and writes nothing."""
        import scan_headless as sh
        empty = scanned_frame().iloc[0:0]
        env = {"PAGES_URL": "https://o.github.io/yentool"}
        provisional = final_block()
        provisional.update(state="provisional", reasons=["checks_fail"])
        cases = {
            "pages older": pages_payload(final_block(
                session="2026-09-11", published="2026-09-11 15:05:00")),
            "pages provisional": pages_payload(provisional),
            "pages final": pages_payload(final_block()),
            "pages unreachable": None,
        }
        for name, pages in cases.items():
            out = self.run_scan({DATE: ledger_row()}, clock=NEXT,
                                applied=self.nothing, verified=empty,
                                env=env, pages=pages)
            self.assertEqual(out, sh.FROZEN, name)
            self.assert_nothing_written()
            self.m["select_with_hysteresis"].assert_not_called()

    def test_the_inferred_session_decision_says_why(self):
        import scan_headless as sh
        ctx = mock.MagicMock(forced=False)
        ctx.status.return_value = {"state": "final", "session": "2026-09-11",
                                   "revision": 1, "block": final_block(
                                       session="2026-09-11")}
        with mock.patch.object(sh, "final_list_session",
                               lambda mode, session=None, path=None:
                               ledger_row() if session in (None, DATE) else None):
            dec = sh._post_gate(MODE, "", ctx)
        self.assertEqual(dec["action"], lf.ACTION_FROZEN)
        self.assertEqual(dec["reason"], "no_bar_date")
        self.assertEqual(dec["session"], DATE)
        self.assertIsNone(dec["revised_reason"])
        # forced runs skip the inference and the freeze
        ctx.forced = True
        with mock.patch.object(sh, "final_list_session", lambda *a, **k: None):
            dec = sh._post_gate(MODE, "", ctx)
        self.assertEqual(dec["action"], lf.ACTION_SCAN)

    def test_no_bar_date_and_nothing_final_still_scans(self):
        empty = scanned_frame().iloc[0:0]
        out = self.run_scan({}, applied=self.nothing, verified=empty)
        self.assertIsInstance(out, pd.DataFrame)
        self.m["export_scan_result"].assert_called()

    def test_forced_with_no_bar_date_scans(self):
        empty = scanned_frame().iloc[0:0]
        out = self.run_scan({DATE: ledger_row()}, applied=self.nothing,
                            verified=empty, env={"YENTOOL_FORCE_RESCAN": "1"})
        self.assertIsInstance(out, pd.DataFrame)


class TestGuiWorkerSession(unittest.TestCase):
    """gui/scan_worker.py: the same session-aware hysteresis state and
    DATA-session picks as scan_headless (verifier fix; it still used
    load_held_ids / save_held_ids and wall-clock picks). The desktop has no
    freeze gate -- nothing it writes is deployed or committed."""

    def run_worker(self, state, frame):
        import gui.scan_worker as sw
        import scanner.tracked_rows as trk
        import scanner.live_record as lr
        import scanner.chip_signal as chip
        m = {}
        errors = []
        with ExitStack() as st:
            def patch(target, name, value=None, **kw):
                mk = value if value is not None else mock.MagicMock(**kw)
                st.enter_context(mock.patch.object(target, name, mk))
                m[name] = mk
                return mk
            patch(sw, "load_state", return_value=state)
            patch(sw, "save_state")
            patch(sw, "get_candidate_list", return_value=frame.copy())
            patch(sw, "get_feed_health", return_value={"ok": True})
            patch(sw, "verify_candidates", return_value=frame.copy())
            patch(sw, "apply_scan_mode", side_effect=lambda df, mode: df)
            patch(sw, "select_with_hysteresis",
                  side_effect=lambda df, prior: (df, ["6426"]))
            patch(sw, "add_trade_columns", side_effect=lambda df, mode: df)
            patch(sw, "record_picks", return_value=2)
            patch(sw, "backfill_outcomes", return_value=0)
            patch(sw, "annotate_holding", side_effect=lambda df, *a, **k: df)
            patch(sw, "mark_buy_ready", side_effect=lambda df, *a, **k: df)
            patch(sw, "export_scan_result", return_value=None)
            patch(trk, "recent_pick_ids", return_value={})
            patch(trk, "split_tracked", return_value=None)
            patch(lr, "build_live_record", return_value={})
            patch(chip, "annotate_chip_action", side_effect=lambda df, mode: df)
            patch(tr, "fetch_restrictions", return_value=None)
            # display-only fetchers (2026-10-08): no network, no real cache
            import ingestion.otc_index as otc
            import ingestion.company_events as cev
            patch(otc, "refresh", return_value={})
            patch(cev, "refresh", return_value=None)
            w = sw.ScanWorker(lambda *a: None, lambda df: None, errors.append,
                              lambda: None, scan_mode=MODE)
            w._run()
        self.assertEqual(errors, [])
        return m

    def test_same_session_rerun_starts_from_that_sessions_prior(self):
        m = self.run_worker({"held_ids": ["1111"], "session": DATE,
                             "prior_ids": ["2222"], "prior_session": "2026-09-11"},
                            scanned_frame())
        inc = m["get_candidate_list"].call_args[1]["include_ids"]
        self.assertLessEqual({"1111", "2222"}, inc)
        self.assertEqual(m["select_with_hysteresis"].call_args[0][1], ["2222"])
        self.assertEqual(m["save_state"].call_args[0],
                         (MODE, DATE, ["6426"], ["2222"]))
        self.assertEqual(m["record_picks"].call_args[1]["scan_session"], DATE)

    def test_new_session_starts_from_the_held_set(self):
        m = self.run_worker({"held_ids": ["1111"], "session": "2026-09-11",
                             "prior_ids": ["2222"], "prior_session": "2026-09-10"},
                            scanned_frame())
        self.assertEqual(m["select_with_hysteresis"].call_args[0][1], ["1111"])
        self.assertEqual(m["save_state"].call_args[0][1], DATE)

    def test_source_has_no_wall_clock_writers(self):
        src = (Path(__file__).resolve().parent.parent / "gui"
               / "scan_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("save_held_ids(", src)
        self.assertNotIn("record_picks(result_df, self._scan_mode)", src)


class TestMainAndSource(unittest.TestCase):
    def exit_code(self, value):
        import scan_headless as sh
        with mock.patch.object(sh, "run_scan", return_value=value), \
                mock.patch("sys.argv", ["scan_headless.py"]):
            with self.assertRaises(SystemExit) as cm:
                sh.main()
        return cm.exception.code

    def test_exit_codes(self):
        import scan_headless as sh
        self.assertEqual(self.exit_code(sh.FROZEN), 3)
        self.assertEqual(self.exit_code(sh.AMENDED), 4)
        self.assertEqual(self.exit_code(None), 1)
        self.assertEqual(self.exit_code(pd.DataFrame()), 0)
        self.assertEqual((sh.EXIT_FROZEN, sh.EXIT_AMENDED), (3, 4))

    def test_gate_order(self):
        import inspect
        import scan_headless as sh
        src = inspect.getsource(sh.run_scan)
        pre, refresh = src.find("_pre_gate("), src.find("refresh_market(")
        post = src.find("_post_gate(")
        self.assertTrue(0 <= pre < refresh, (pre, refresh))
        for later in ("select_with_hysteresis(", "save_state(",
                      "_prepare_recommendations(", "record_picks(",
                      "export_scan_result("):
            self.assertLess(post, src.find(later), later)
        self.assertGreater(post, src.find("sort_for_mode("))
        self.assertNotIn("save_held_ids(", src)
        self.assertLess(src.find("check_files("), src.find("_record_list_session("))


class TestRecordListSessionHook(LedgerCase):
    def run_hook(self, block, degraded=None):
        import scan_headless as sh
        import config.settings as cfg
        scan = Path(self.tmp.name) / "scan_result.json"
        scan.write_text(json.dumps({"meta": {"list_status": block}}), encoding="utf-8")
        df = pd.DataFrame([{"Stock_ID": "6426", "Buy_Ready": True},
                           {"Stock_ID": "8069", "Buy_Ready": False}])
        with mock.patch.object(cfg, "MOBILE_DATA_FILE", scan), \
                mock.patch.object(sl, "SIGNAL_LEDGER_FILE", self.db):
            return sh._record_list_session(MODE, df, degraded)

    def test_final_is_recorded(self):
        block = final_block(revision=2, revised_reason="force_rescan")
        self.assertEqual(self.run_hook(block), "inserted")
        row = sl.final_list_session(MODE, DATE, path=self.db)
        self.assertEqual((row["revision"], row["revised_reason"], row["rows"]),
                         (2, "force_rescan", 2))
        self.assertEqual(row["buy_ready_ids"], ["6426"])

    def test_provisional_or_degraded_is_not(self):
        self.assertIsNone(self.run_hook(final_block(state="provisional")))
        self.assertIsNone(self.run_hook(final_block(), degraded="feed degraded"))
        self.assertFalse(self.db.exists())


# --------------------------------------------------------------------------
# workflow / timer wiring
# --------------------------------------------------------------------------
class TestWorkflowStrings(unittest.TestCase):
    def setUp(self):
        self.yml = (ROOT / ".github" / "workflows" / "scan.yml").read_text(
            encoding="utf-8").replace("\r\n", "\n")
        self.sh = (ROOT / ".github" / "scripts" / "scan_timer.sh").read_text(
            encoding="utf-8").replace("\r\n", "\n")

    def test_force_rescan_input_and_env(self):
        wd = self.yml.split("workflow_dispatch:", 1)[1].split("permissions:", 1)[0]
        self.assertIn("force_rescan:", wd)
        self.assertIn("type: boolean", wd)
        self.assertIn("YENTOOL_FORCE_RESCAN: ${{ inputs.force_rescan && '1' || '' }}",
                      self.yml)
        self.assertIn("PAGES_URL: https://${{ github.repository_owner }}.github.io/"
                      "${{ github.event.repository.name }}", self.yml)

    def test_exit_codes_mapped(self):
        case = self.yml.split('case "$code" in', 1)[1].split("esac", 1)[0]
        three = case.split("3)", 1)[1].split(";;", 1)[0]
        self.assertIn("published=false", three)
        self.assertIn("frozen=true", three)
        four = case.split("4)", 1)[1].split(";;", 1)[0]
        self.assertIn("published=true", four)

    def test_commit_step_is_unconditional(self):
        step = self.yml.split("- name: Commit durable state", 1)[1].split("run: |", 1)[0]
        self.assertNotIn("if:", step)
        self.assertNotIn("published", step)

    def test_the_push_retry_loop_survives_a_failed_fetch(self):
        """CW-2: `run:` is bash -e, so an unguarded `git fetch` inside the
        retry loop turns the scan job red and the deploy job (needs: scan)
        is skipped although the Pages artifact was uploaded."""
        run = self.yml.split("- name: Commit durable state", 1)[1]
        run = run.split("run: |", 1)[1].split("\n  deploy:", 1)[0]
        fetches = [ln for ln in run.splitlines()
                   if re.match(r"\s*git fetch\b", ln)]
        self.assertTrue(fetches)
        for ln in fetches:
            self.assertIn("||", ln, ln)
            self.assertIn("continue", ln, ln)
        self.assertNotIn("Pages already deployed", run)

    def test_timer_reads_the_list_state(self):
        self.assertIn(".meta.list_status.state", self.sh)
        self.assertIn('"$list_state" == "final"', self.sh)
        m = re.search(r'^FIRST_ATTEMPT="([0-9:]+)"', self.sh, re.M)
        self.assertEqual(m.group(1), lf.FINAL_NOT_BEFORE)
        # the pre-freeze fallback keeps the original predicate
        self.assertIn('"$regime_current" != "false"', self.sh)

    def test_docs_explain_the_freeze(self):
        doc = (ROOT / "docs" / "\u6392\u7a0b\u8207\u5373\u6642\u884c\u60c5.md"
               ).read_text(encoding="utf-8")
        for s in ("force_rescan", "exit 3", "exit 4", "final-once-v1",
                  "list_status"):
            self.assertIn(s, doc)


# --------------------------------------------------------------------------
# evening restriction amend
# --------------------------------------------------------------------------
def info_with(disposition=None, altered=None, ok=True, attention_ok=True):
    info = tr.empty_info(DATE)
    board = {"ok": ok, "source": "openapi", "rows": 12,
             "last_modified": "Mon, 14 Sep 2026 15:30:00 GMT", "error": None}
    info.update(ok=ok, boards={"OTC": dict(board), "TSE": dict(board)},
                attention_ok={"OTC": attention_ok, "TSE": attention_ok},
                altered_ok={"OTC": True, "TSE": True},
                disposition=disposition or {}, altered=altered or {})
    return info


DISPO = {"6426": [{"board": "OTC", "start": NEXT, "end": "2026-09-28",
                   "announced": DATE, "match_min": 5, "prepay": "all"}]}


class AmendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.paths = {"scan_result.json": self.dir / "mobile" / "scan_result.json",
                      "quotes.json": self.dir / "mobile" / "quotes.json",
                      "universe.json": self.dir / "mobile" / "universe.json",
                      "recs": self.dir / "recommendations.json",
                      "history": self.dir / "scan_checks.json",
                      "signal_ledger": self.dir / "signal_ledger.db",
                      "portfolio_ledger": self.dir / "portfolio_ledger.db"}
        (self.dir / "mobile").mkdir()

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def publish(self, rows, recs=None, **meta):
        """The 15:05 publish: a checked payload as Pages serves it."""
        p = clean_payload(rows)
        p["meta"].update(meta)
        self.quotes = clean_quotes(rows)
        self.universe = {"as_of": DATE, "stocks": {}}
        self.recs = recs or {"count": 0, "recommendations": []}
        work = self.dir / "work"
        work.mkdir(exist_ok=True)
        (work / "s.json").write_text(json.dumps(p), encoding="utf-8")
        (work / "q.json").write_text(json.dumps(self.quotes), encoding="utf-8")
        (work / "r.json").write_text(json.dumps(self.recs), encoding="utf-8")
        check_files(work / "s.json", quotes_path=work / "q.json",
                    recs_path=work / "r.json")
        self.pages = json.loads((work / "s.json").read_text(encoding="utf-8"))
        self.assertEqual(self.pages["meta"]["list_status"]["state"], "final",
                         self.pages["meta"]["checks"])
        self.paths["recs"].write_text(json.dumps(self.recs, indent=1, sort_keys=True),
                                      encoding="utf-8")
        return self.pages

    def get(self, url, params=None, timeout=None, headers=None):
        name = url.rsplit("/", 1)[-1]
        doc = {"quotes.json": self.quotes, "universe.json": self.universe}.get(name)

        class R(object):
            pass
        r = R()
        r.status_code = 200 if doc is not None else 404
        r.content = json.dumps(doc).encode() if doc is not None else b""
        return r

    def amend(self, info, ledger_row=None, get=None, blocking=None):
        return lf.amend_restrictions(
            self.pages, MODE, base_url="https://o.github.io/yentool",
            ledger_row=ledger_row, fetch=lambda s: info, get=get or self.get,
            paths=self.paths, expected_session=DATE,
            now=datetime(2026, 9, 14, 23, 40), blocking=blocking)

    def written(self):
        return json.loads(self.paths["scan_result.json"].read_text(encoding="utf-8"))


class TestAmend(AmendCase):
    def test_disposition_amends_only_the_restriction_columns(self):
        rows = [clean_row(), clean_row(sid="8069")]
        before = self.publish(rows)
        out, detail = self.amend(info_with(DISPO))
        self.assertEqual(out, lf.AMENDED, detail)
        after = self.written()
        self.assertEqual([r["Stock_ID"] for r in after["rows"]], ["6426", "8069"])
        r0, b0 = after["rows"][0], before["rows"][0]
        self.assertEqual(r0["Trade_Restriction"], "disposition")
        self.assertEqual((r0["Restriction_Since"], r0["Restriction_Until"],
                          r0["Restriction_Match_Min"], r0["Restriction_Prepay"]),
                         (NEXT, "2026-09-28", 5, "all"))
        changed = {k for k in set(r0) | set(b0) if r0.get(k) != b0.get(k)}
        self.assertLessEqual(changed, set(tr.RESTRICTION_COLUMNS))
        self.assertEqual(after["rows"][1], before["rows"][1])
        mchanged = {k for k in after["meta"]
                    if after["meta"][k] != before["meta"][k]}
        self.assertLessEqual(mchanged, {"quality", "list_status", "checks"})
        q = {k for k in after["meta"]["quality"]
             if after["meta"]["quality"][k] != before["meta"]["quality"][k]}
        self.assertEqual(q, {"restrictions"})
        self.assertEqual(after["meta"]["quality"]["restrictions"]["active"], 1)
        ls = after["meta"]["list_status"]
        self.assertEqual((ls["state"], ls["revision"], ls["revised_reason"]),
                         ("final", 2, "restriction_info"))
        self.assertEqual(ls["published_at"], before["meta"]["scan_time"])
        self.assertEqual(ls["revised_at"], "2026-09-14 23:40:00")
        # the companion files the deploy needs came from Pages
        self.assertEqual(json.loads(self.paths["quotes.json"].read_text(
            encoding="utf-8")), self.quotes)
        self.assertTrue(self.paths["universe.json"].exists())

    def test_same_lists_change_nothing(self):
        self.publish([clean_row()])
        out, detail = self.amend(info_with())
        self.assertEqual(out, lf.UNCHANGED, detail)
        self.assertFalse(self.paths["scan_result.json"].exists())
        self.assertFalse(self.paths["quotes.json"].exists())

    def test_pages_files_unreachable_writes_nothing(self):
        self.publish([clean_row()])

        def down(url, **kw):
            raise IOError("pages down")
        out, detail = self.amend(info_with(DISPO), get=down)
        self.assertEqual(out, lf.SKIPPED)
        self.assertIn("unreachable", detail)
        self.assertEqual(sorted(p.name for p in (self.dir / "mobile").iterdir()), [])

    def test_an_incomplete_fetch_never_downgrades(self):
        self.publish([clean_row()])
        out, detail = self.amend(info_with(DISPO, ok=False))
        self.assertEqual(out, lf.SKIPPED)
        out, detail = self.amend(info_with(DISPO, attention_ok=False))
        self.assertEqual(out, lf.SKIPPED)
        self.assertIn("attention_ok", detail)
        self.assertFalse(self.paths["scan_result.json"].exists())

    def test_not_final_is_skipped(self):
        self.publish([clean_row()])
        self.pages["meta"]["list_status"]["state"] = "provisional"
        self.assertEqual(self.amend(info_with(DISPO))[0], lf.SKIPPED)

    def test_ledger_is_amended_when_it_has_the_marker(self):
        rows = [clean_row()]
        self.publish(rows)
        db = self.paths["signal_ledger"]
        with mock.patch.object(sl, "SIGNAL_LEDGER_FILE", db), \
                mock.patch("scanner.market_regime.get_market_regime",
                           lambda *a, **k: {"ok": True}):
            sl.record_picks(pd.DataFrame(rows), MODE, scan_session=DATE)
        sl.record_list_session(MODE, self.pages["meta"]["list_status"], rows=1,
                               path=db)
        conn = sqlite3.connect(str(db))
        ts = conn.execute("SELECT scan_ts FROM picks").fetchall()
        conn.close()
        out, detail = self.amend(info_with(DISPO),
                                 ledger_row=sl.final_list_session(MODE, DATE, path=db))
        self.assertEqual(out, lf.AMENDED, detail)
        row = sl.final_list_session(MODE, DATE, path=db)
        self.assertEqual((row["revision"], row["revised_reason"]),
                         (2, "restriction_info"))
        conn = sqlite3.connect(str(db))
        gate = json.loads(conn.execute("SELECT gate_detail FROM picks").fetchone()[0])
        self.assertEqual(conn.execute("SELECT scan_ts FROM picks").fetchall(), ts)
        conn.close()
        self.assertEqual(gate["restriction"], "disposition")

    def test_a_restricted_block_is_never_lifted(self):
        row = clean_row()
        row.update(Trade_Restriction="suspended", Restriction_Flags="suspended",
                   Buy_Block="restricted")
        self.publish([row])
        out, detail = self.amend(info_with())       # no longer suspended
        self.assertEqual(out, lf.UNCHANGED, detail)
        self.assertIn("restricted kept", detail)

    def test_blocking_kind_withdraws_the_buy_and_the_recommendation(self):
        row = rec_row(Buy_Ready=True, Buy_Block=None)
        recs = {"count": 1, "recommendations": [rec_item(row)]}
        self.publish([row], recs=recs,
                     regime=dict(clean_payload()["meta"]["regime"], enter_ok=True))
        # the checks read the same module-level set, so widen it there
        with mock.patch.object(tr, "BLOCKING_RESTRICTIONS",
                               ("suspended", "disposition")):
            out, detail = self.amend(info_with(DISPO))
        self.assertEqual(out, lf.AMENDED, detail)
        r = self.written()["rows"][0]
        self.assertEqual((r["Buy_Ready"], r["Buy_Block"]), (False, "restricted"))
        self.assertIsNone(r["Recommendation_ID"])
        self.assertIsNone(r["Rec_Status"])
        doc = json.loads(self.paths["recs"].read_text(encoding="utf-8"))
        rec = doc["recommendations"][0]
        self.assertEqual((rec["status"], rec["status_reason"], rec["status_session"]),
                         ("superseded", "retracted:restricted", DATE))
        q = self.written()["meta"]["quality"]
        self.assertEqual(q["buy_ready"], 0)
        self.assertEqual(q["restrictions"]["blocked"], 1)

    def test_a_non_blocking_kind_never_touches_the_buy(self):
        row = rec_row(Buy_Ready=True, Buy_Block=None)
        recs = {"count": 1, "recommendations": [rec_item(row)]}
        self.publish([row], recs=recs,
                     regime=dict(clean_payload()["meta"]["regime"], enter_ok=True))
        out, detail = self.amend(info_with(DISPO))     # default blocking set
        self.assertEqual(out, lf.AMENDED, detail)
        r = self.written()["rows"][0]
        self.assertEqual((r["Buy_Ready"], r["Recommendation_ID"]),
                         (True, row["Recommendation_ID"]))
        doc = json.loads(self.paths["recs"].read_text(encoding="utf-8"))
        self.assertEqual(doc["recommendations"][0]["status"], "active")

    def test_tracked_rows_are_amended_too(self):
        rows = [clean_row(sid="8069")]
        self.publish(rows)
        self.pages["tracked"] = [clean_row()]
        out, detail = self.amend(info_with(DISPO))
        self.assertEqual(out, lf.AMENDED, detail)
        after = self.written()
        self.assertEqual(after["tracked"][0]["Trade_Restriction"], "disposition")
        self.assertEqual(after["rows"][0]["Trade_Restriction"], "none")


class TestFrozenRunPagesUnreachable(GateRun):
    def test_exit_3_and_no_write(self):
        import scan_headless as sh
        # the ledger says final, Pages cannot be read: frozen on the ledger,
        # and there is nothing to amend from
        out = self.run_scan({DATE: ledger_row()},
                            env={"PAGES_URL": "https://o.github.io/yentool"},
                            pages=None)
        self.assertEqual(out, sh.FROZEN)
        self.m["amend_restrictions"].assert_not_called()
        self.assert_nothing_written()


if __name__ == "__main__":
    unittest.main()
