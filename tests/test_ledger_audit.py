"""
tools/ledger_audit.py: the owner's real fills against the rule, on the same
synthetic stores tests/test_live_record.py uses (its Fixture), plus a phone
backup written the way mobile/app.js exportBackup() writes it.

    python -m unittest tests.test_ledger_audit -v
"""
import gc
import json
import os
import tempfile
import unittest

from scanner.exit_rules import DEFAULT_RULE
from scanner.live_record import net_pct
from tests.test_live_record import Fixture, SESSIONS, SIG, D1
from tools import ledger_audit as la

DAY1 = SESSIONS[SIG + 1]        # the session after the signal: the fill day
DAY2 = SESSIONS[SIG + 2]
DAY3 = SESSIONS[SIG + 3]


def exe(pid, side, date, shares, price, **kw):
    e = {"execution_id": "exe_%s_%s_%s" % (pid, side, date), "position_id": pid,
         "side": side, "session_date": date, "shares": shares,
         "price_cents": int(round(price * 100)), "created_at": date + "T10:00"}
    e.update(kw)
    return e


class TheAuditReadsARealBackup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        positions = [
            {"position_id": "A", "stock_id": "1111", "stock_name": "A", "status": "closed"},
            {"position_id": "B", "stock_id": "2222", "stock_name": "B", "status": "closed"},
            {"position_id": "C", "stock_id": "4444", "stock_name": "D", "status": "open"},
            {"position_id": "F", "stock_id": "3333", "stock_name": "C", "status": "open"},
            {"position_id": "E", "stock_id": "2222", "stock_name": "B", "status": "void"},
        ]
        executions = [
            # A: a real signal, bought at the open the day after, sold at the
            # target the same day the rule took it -> follows the rule
            exe("A", "BUY", DAY1, 1000, 100.0),
            exe("A", "SELL", DAY1, 1000, 120.0),
            # B: the list showed it but the badge refused; the rule's stop is
            # the open of DAY2 (70); the owner sold a session later at 65
            exe("B", "BUY", DAY1, 1000, 100.0),
            exe("B", "SELL", DAY3, 1000, 65.0),
            # a voided fill must not count
            exe("B", "BUY", DAY1, 5000, 50.0, voided_at="x"),
            # ... nor a superseded revision (the phone marks it is_current 0)
            exe("B", "BUY", DAY2, 7000, 40.0, is_current=0, superseded_by="y"),
            # C: still held, flat bars
            exe("C", "BUY", DAY1, 2000, 100.0),
            # F: a fill the market never offered that day (bars are 99-101)
            exe("F", "BUY", DAY1, 1000, 90.0),
            # E: a voided position is skipped entirely
            exe("E", "BUY", DAY1, 1000, 100.0),
        ]
        cls.backup = os.path.join(cls.fx.tmp.name, "backup.json")
        with open(cls.backup, "w", encoding="utf-8") as f:
            json.dump({"schema": "yentool-mobile-ledger", "version": 1,
                       "positions": positions, "executions": executions}, f)
        cls.rows, cls.sm = la.audit(
            cls.backup, since=SESSIONS[0], ledger_file=cls.fx.ledger,
            price_file=cls.fx.price, taiex_file=cls.fx.taiex, research_file=None)
        cls.by = {r["position_id"]: r for r in cls.rows}

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        cls.fx.tmp.cleanup()

    def test_a_voided_position_and_a_voided_fill_are_ignored(self):
        self.assertNotIn("E", self.by)
        self.assertEqual(self.by["B"]["buy_shares"], 1000)
        self.assertEqual(self.by["B"]["avg_cost"], 100.0)

    def test_the_rule_follower_is_clean(self):
        a = self.by["A"]
        self.assertEqual(a["bucket"], "tradable")
        self.assertEqual(a["signal_date"], D1)
        self.assertEqual(a["rule_exit"], "tp")
        self.assertEqual(a["verdict"], "ok")
        self.assertAlmostEqual(a["actual_ret_net"], round(net_pct(20.0), 2), places=2)
        self.assertAlmostEqual(a["leak_pct"], 0.0, places=2)

    def test_a_stop_not_taken_is_named_and_priced(self):
        b = self.by["B"]
        self.assertEqual(b["bucket"], "not_core")
        self.assertEqual(b["rule_exit"], "stop")
        self.assertEqual(b["rule_exit_date"], DAY2)
        self.assertEqual(b["rule_exit_price"], 70.0)
        self.assertIn("no_signal", b["flags"])
        self.assertIn("stop_not_taken", b["flags"])
        # the leak is what ignoring the stop cost, net of costs on both legs
        self.assertAlmostEqual(b["leak_pct"], round(net_pct(-35.0) - net_pct(-30.0), 2), places=1)

    def test_an_open_position_is_marked_not_judged(self):
        c = self.by["C"]
        self.assertIsNone(c["actual_ret_net"])
        self.assertIsNotNone(c["mark_ret_net"])
        self.assertEqual(c["rule_exit"], "time")
        self.assertEqual(c["verdict"], "ok")
        self.assertLessEqual(c["held_sessions"], DEFAULT_RULE["ride_cap"])

    def test_a_fill_outside_the_days_range_is_a_ledger_error(self):
        f = self.by["F"]
        self.assertIn("fill_outside_bar", f["flags"])
        self.assertEqual(f["bar_range"], "99.00-101.00")

    def test_the_summary_adds_up(self):
        self.assertEqual(self.sm["positions"], 4)
        self.assertEqual(self.sm["closed"], 2)
        self.assertEqual(self.sm["flags"]["stop_not_taken"], 1)
        self.assertEqual(self.sm["flags"]["no_signal"], 2)   # B and F
        self.assertAlmostEqual(self.sm["leak_sum_pct"],
                               round(self.by["B"]["leak_pct"], 1), places=1)

    def test_a_foreign_file_is_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump({"schema": "something-else"}, f)
        try:
            with self.assertRaises(ValueError):
                la.load_backup(f.name)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
