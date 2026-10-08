"""tools/rec_set_status.py: ending a published recommendation by hand.

Temp copies only; the real data/recommendations.json is never touched."""
import json
import os
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from tools import rec_set_status as tool

REC = {
    "cycle_seq": 1, "first_qualified_session": "2026-09-17",
    "gate_snapshot": "{\"Buy_Ready\": true}", "horizon_days": 10,
    "initial_buy_price": "18275.00", "initial_stop_price": "15530.00",
    "initial_target_price": "21930.00", "market": "OTC",
    "recommendation_id": "rec-5274-mode_prelaunch-1",
    "recommended_at": "2026-09-17 20:00:01", "status": "active",
    "stock_id": "5274", "stock_name": "\u4fe1\u9a4a", "strategy": "mode_prelaunch",
    "strategy_version": "prelaunch-2026-09-09", "trail_arm_price": "18730.00",
    "trail_lock_price": "18640.00", "valid_until_session": None,
}
OTHER = dict(REC, recommendation_id="rec-1815-mode_prelaunch-1", stock_id="1815",
             stock_name="\u5bcc\u55ac", initial_buy_price="126.00")


def _write(path, crlf=False):
    doc = {"count": 2, "format_version": 1, "recommendations": [OTHER, REC],
           "table": "recommendations"}
    text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    if crlf:
        text = text.replace("\n", "\r\n")
    Path(path).write_bytes(text.encode("utf-8"))


class TestRecSetStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "recommendations.json")
        _write(self.path)

    def _items(self):
        with open(self.path, encoding="utf-8") as f:
            return {r["recommendation_id"]: r for r in json.load(f)["recommendations"]}

    def test_sets_the_terminal_fields_only(self):
        before = Path(self.path).read_bytes()
        got = tool.set_status(self.path, REC["recommendation_id"], "cancelled",
                              "degraded_run", "2026-09-17")
        self.assertEqual(got["to"], "cancelled")
        self.assertIsNone(got["ledger"])
        items = self._items()
        rec = items[REC["recommendation_id"]]
        self.assertEqual(rec["status"], "cancelled")
        self.assertEqual(rec["status_reason"], "degraded_run")
        self.assertEqual(rec["status_session"], "2026-09-17")
        self.assertIsNone(rec["outcome"])
        for k, v in REC.items():
            if k != "status":
                self.assertEqual(rec[k], v, k)
        self.assertEqual(items[OTHER["recommendation_id"]], OTHER)
        # the rest of the file is byte-identical: only the new keys and the
        # status line differ
        after = Path(self.path).read_bytes()
        removed = Counter(before.splitlines()) - Counter(after.splitlines())
        self.assertEqual(dict(removed), {b'   "status": "active",': 1})

    def test_keeps_crlf_line_endings(self):
        _write(self.path, crlf=True)
        tool.set_status(self.path, REC["recommendation_id"], "cancelled",
                        "degraded_run", "2026-09-17")
        raw = Path(self.path).read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertFalse(raw.endswith(b"\n"))

    def test_refuses_a_second_time_and_writes_nothing(self):
        tool.set_status(self.path, REC["recommendation_id"], "cancelled",
                        "degraded_run", "2026-09-17")
        before = Path(self.path).read_bytes()
        with self.assertRaises(tool.Refused):
            tool.set_status(self.path, REC["recommendation_id"], "expired",
                            "no_fill", "2026-09-18")
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_refuses_bad_requests(self):
        before = Path(self.path).read_bytes()
        bad = [("rec-nope-1", "cancelled", "x", "2026-09-17"),
               (REC["recommendation_id"], "active", "x", "2026-09-17"),
               (REC["recommendation_id"], "converted", "x", "2026-09-17"),
               (REC["recommendation_id"], "cancelled", "Bad Reason", "2026-09-17"),
               (REC["recommendation_id"], "cancelled", "x", "17/09/2026")]
        for args in bad:
            with self.assertRaises(tool.Refused, msg=str(args)):
                tool.set_status(self.path, *args)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_outcome_is_stored_as_json_text(self):
        tool.set_status(self.path, REC["recommendation_id"], "closed", "lock",
                        "2026-09-29", outcome={"reason": "lock", "bars": 5})
        rec = self._items()[REC["recommendation_id"]]
        self.assertEqual(json.loads(rec["outcome"]), {"bars": 5, "reason": "lock"})

    def test_ledger_option_transitions_the_local_row(self):
        from portfolio.publish import seed_from_export
        ledger = os.path.join(self.tmp.name, "portfolio_ledger.db")
        seed_from_export(ledger, self.path)
        got = tool.set_status(self.path, REC["recommendation_id"], "cancelled",
                              "degraded_run", "2026-09-17", ledger_path=ledger)
        self.assertTrue(got["ledger"])
        conn = sqlite3.connect(ledger)
        try:
            row = conn.execute(
                "SELECT status, status_reason, status_session FROM recommendations "
                "WHERE recommendation_id = ?", (REC["recommendation_id"],)).fetchone()
            ev = conn.execute(
                "SELECT event_type, reason_code FROM recommendation_events "
                "WHERE recommendation_id = ?", (REC["recommendation_id"],)).fetchall()
            other = conn.execute(
                "SELECT status FROM recommendations WHERE recommendation_id = ?",
                (OTHER["recommendation_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("cancelled", "degraded_run", "2026-09-17"))
        self.assertEqual(ev, [("cancelled", "degraded_run")])
        self.assertEqual(other, ("active",))

    def test_cli_exit_codes(self):
        rid = REC["recommendation_id"]
        args = ["--id", rid, "--status", "cancelled", "--reason", "degraded_run",
                "--session", "2026-09-17", "--json", self.path]
        self.assertEqual(tool.main(args), 0)
        self.assertEqual(tool.main(args), 1)
        with self.assertRaises(SystemExit) as cm, redirect_stderr(StringIO()):
            tool.main(["--id", rid])
        self.assertEqual(cm.exception.code, 2)

    def test_tool_is_ascii(self):
        raw = Path(tool.__file__).read_bytes()
        self.assertTrue(all(b < 128 for b in raw))


if __name__ == "__main__":
    unittest.main()
