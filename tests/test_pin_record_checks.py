"""Pins for the RECORD and the SELF-CHECK: the live scorecard (live_record)
and the publish-time checker (result_checks). ASCII only, stdlib unittest,
synthetic stores only.

Why. The live scorecard reports the rule's real record (62.5% on the eight
signals since 2026-06-25), and the checker is the last thing standing between a
drifted constant and a published list. Both read the SAME settled numbers as
the producers, so a drifted number is self-consistent: the checker agrees with
the drifted scan, the scorecard replays with the drifted rule. A mutation audit
(2026-10-09) found the costs, the window sizes, the rank cut, the start date,
the regime legs and eleven checker rules could all move with no test failing.

Here the cost schedule, windows, rank cut, start date and regime means are
literals, and every checker rule is driven just over and just under its
tolerance.

    python -m unittest tests.test_pin_record_checks -v
"""
import inspect
import sqlite3
import sys
import unittest
from unittest import mock

import pandas as pd

from scanner import live_record as LR
from scanner.result_checks import _trade_params, check_payload
from tests.pin_support import collect, taiex_db
from tests.test_live_record import D1, Fixture
from tests.test_result_checks import clean_payload, clean_quotes, clean_row, codes


# -------------------------------------------------------------------- live record
class _RankFixture(Fixture):
    """Two OTC CORE+ buy-ready first-day names, ranked 19 and 20 (0-based
    rank 19 is the 20th name on the list)."""

    def _ledger(self):
        picks = [
            ("s1", "t1", "mode_prelaunch", "1111", "A", 19, D1, "OTC", 1, 1),
            ("s1", "t1", "mode_prelaunch", "4444", "D", 20, D1, "OTC", 1, 1),
        ]
        with sqlite3.connect(self.ledger) as c:
            c.execute("CREATE TABLE picks (scan_session TEXT, scan_ts TEXT, "
                      "scan_mode TEXT, stock_id TEXT, stock_name TEXT, rank INTEGER, "
                      "bar_date TEXT, market TEXT, core_plus INTEGER, buy_ready INTEGER)")
            c.executemany("INSERT INTO picks VALUES (?,?,?,?,?,?,?,?,?,?)", picks)


class LiveRecordCosts(unittest.TestCase):
    def test_the_round_trip_is_the_taiwan_schedule(self):
        # buy 0.1425% ; sell 0.1425% + 0.3% transaction tax
        self.assertAlmostEqual(LR.BUY_COST, 0.001425, places=9)
        self.assertAlmostEqual(LR.SELL_COST, 0.004425, places=9)
        self.assertAlmostEqual(LR.net_pct(0.0), -0.58416, places=4)         # 0.585% round trip
        self.assertAlmostEqual(LR.net_pct(20.0), 19.2992, places=3)

    def test_net_pct_is_the_cost_formula_written_out_from_the_literals(self):
        for gross in (-20.0, -5.0, 0.0, 1.0, 10.0, 20.0):
            want = ((1 + gross / 100.0) * (1 - 0.004425) / (1 + 0.001425) - 1) * 100.0
            self.assertAlmostEqual(LR.net_pct(gross), want, places=9, msg=gross)
        # the buy cost is charged on the way IN (divides), not credited
        self.assertAlmostEqual(LR.net_pct(10.0), 9.3574, places=3)
        self.assertAlmostEqual(LR.net_pct(-20.0), -20.4673, places=3)


class LiveRecordRule(unittest.TestCase):
    def test_the_published_sizes_and_defaults(self):
        self.assertEqual((LR.MIN_BARS_FOR_CORE, LR.FULL_52W_BARS, LR.TRADES_KEPT,
                          LR.BY_SID_KEPT, LR.BENCH_POINTS), (120, 240, 40, 5, 120))
        self.assertEqual(inspect.signature(LR.classify_signals).parameters["since"].default,
                         "2026-06-25")
        self.assertEqual(inspect.signature(LR.build_live_record).parameters["since"].default,
                         "2026-06-25")

    def test_the_20th_name_is_a_signal_the_21st_is_not(self):
        fx = _RankFixture()
        try:
            rows, counters = LR.classify_signals(since=D1, ledger_file=fx.ledger,
                                                 price_file=fx.price, taiex_file=fx.taiex)
            self.assertEqual([r["sid"] for r in rows], ["1111"])
            self.assertEqual(counters["candidates"], 1)
        finally:
            collect()
            fx.tmp.cleanup()

    def test_the_live_regime_is_above_both_means_strictly(self):
        cases = {
            "equal to the 20-day mean": ([90.0] * 40 + [100.0] * 20, False),
            "one step over the 20-day mean": ([90.0] * 40 + [100.0] * 19 + [100.5], True),
            "above the 20 but under the 60": ([200.0] * 40 + [100.0] * 19 + [130.0], False),
            "above the 60 but under the 20": ([100.0] * 40 + [130.0] * 19 + [120.0], False),
            "above both": ([100.0] * 59 + [101.0], True),
            "equal to the 60-day mean": ([95.0] * 38 + [100.0] * 2 + [110.0] * 19 + [100.0], False),
        }
        for label, (closes, want) in cases.items():
            with taiex_db(closes) as (path, dates):
                got = LR._regime_by_date(path)
                self.assertEqual(got[dates[-1]], want, label)
            collect()

    def test_core_recompute_needs_120_bars_and_flags_a_full_window_at_240(self):
        def hist(n, last=100.0):
            close = [100.0] * (n - 1) + [last]
            return pd.DataFrame({"close": close, "high": [c * 1.05 for c in close],
                                 "low": [c * 0.95 for c in close]})
        self.assertEqual(LR._core_from_bars(hist(119)), (None, False))
        self.assertEqual(LR._core_from_bars(hist(120))[0], True)
        self.assertEqual(LR._core_from_bars(hist(239))[1], False)
        self.assertEqual(LR._core_from_bars(hist(240))[1], True)

    def test_the_recomputed_core_gate_has_the_same_inclusive_edges(self):
        # ATR: a mean range of exactly 4.5% qualifies, 4.4% does not
        close = [100.0] * 130
        at = pd.DataFrame({"close": close, "high": [102.25] * 130, "low": [97.75] * 130})
        self.assertEqual(LR._core_from_bars(at)[0], True)
        under = pd.DataFrame({"close": close, "high": [102.2] * 130, "low": [97.8] * 130})
        self.assertEqual(LR._core_from_bars(under)[0], False)
        # distance: exactly 5% under the 52-week high qualifies, 5.5% does not
        c = [100.0] * 149 + [95.0]
        near = pd.DataFrame({"close": c, "high": [x * 1.05 for x in c], "low": [x * 0.95 for x in c]})
        self.assertEqual(LR._core_from_bars(near)[0], True)
        c = [100.0] * 149 + [94.0]
        far = pd.DataFrame({"close": c, "high": [x * 1.05 for x in c], "low": [x * 0.95 for x in c]})
        self.assertEqual(LR._core_from_bars(far)[0], False)
        # 5-day return: +4.9% qualifies, +5.5% does not (exactly +5.0 is unreachable in
        # floats: 105 / 100 - 1 is 0.05000000000000004)
        c = [100.0] * 144 + [100.0, 100.0, 100.0, 100.0, 100.0, 104.9]
        run = pd.DataFrame({"close": c, "high": [x * 1.05 for x in c], "low": [x * 0.95 for x in c]})
        self.assertEqual(LR._core_from_bars(run)[0], True)
        c = [100.0] * 144 + [100.0, 100.0, 100.0, 100.0, 100.0, 105.5]
        run = pd.DataFrame({"close": c, "high": [x * 1.05 for x in c], "low": [x * 0.95 for x in c]})
        self.assertEqual(LR._core_from_bars(run)[0], False)


# ------------------------------------------------------------------- the checker
class CheckerRulesFire(unittest.TestCase):
    """clean_row is a 312.00 close: stop 249.50, target 374.50, arm 320.00,
    lock 318.00, add 280.50, scale-out 359.00, risk 20.0."""

    def report(self, **changes):
        row = clean_row()
        row.update(changes)
        p = clean_payload([row])
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def fires(self, code, **changes):
        return code in codes(self.report(**changes))

    def test_each_price_identity_fires_on_a_clear_drift(self):
        for code, change in (
            ("stop_pct_mismatch", {"Strict_Stop_Loss": 249.0}),
            ("target_pct_mismatch", {"Target_Price": 400.0}),
            ("entry_ref_not_close", {"Suggested_Buy_Price": 313.0}),
            ("add_pct_mismatch", {"Add_Price": 250.0}),
            ("scale_out_pct_mismatch", {"Scale_Out_Price": 330.0}),
            ("risk_pct_mismatch", {"Risk_Pct": 5.0}),
            ("risk_pct_mismatch", {"Risk_Pct": 21.5}),
            ("trail_lock_not_below_arm", {"Trail_Lock_Price": 320.0, "Trail_Arm_Price": 320.0}),
        ):
            self.assertTrue(self.fires(code, **change), "%s %s" % (code, change))

    def test_a_stop_at_the_entry_or_a_target_at_the_entry_is_an_error(self):
        # a stop must sit strictly below the entry and a target strictly above
        self.assertTrue(self.fires("stop_not_below_entry", Strict_Stop_Loss=312.0))
        self.assertFalse(self.fires("stop_not_below_entry", Strict_Stop_Loss=311.99))
        self.assertTrue(self.fires("target_not_above_entry", Target_Price=312.0))
        self.assertFalse(self.fires("target_not_above_entry", Target_Price=312.01))
        self.assertFalse(self.fires("trail_lock_not_below_arm", Trail_Lock_Price=319.99, Trail_Arm_Price=320.0))

    def test_the_price_tolerance_is_a_cent(self):
        # two-decimal rounding on both sides: 0.01 off passes, 0.02 off does not
        self.assertFalse(self.fires("stop_pct_mismatch", Strict_Stop_Loss=249.51))
        self.assertTrue(self.fires("stop_pct_mismatch", Strict_Stop_Loss=249.52))
        self.assertTrue(self.fires("stop_pct_mismatch", Strict_Stop_Loss=249.1))
        self.assertFalse(self.fires("target_pct_mismatch", Target_Price=374.51))
        self.assertTrue(self.fires("target_pct_mismatch", Target_Price=374.52))
        self.assertTrue(self.fires("entry_ref_not_close", Suggested_Buy_Price=312.02))
        self.assertFalse(self.fires("entry_ref_not_close", Suggested_Buy_Price=312.01))

    def test_the_risk_tolerance_is_a_tenth_of_a_percent(self):
        # (312 - 249.5) / 312 = 20.03%
        self.assertFalse(self.fires("risk_pct_mismatch", Risk_Pct=20.1))
        self.assertTrue(self.fires("risk_pct_mismatch", Risk_Pct=20.2))
        self.assertTrue(self.fires("risk_pct_mismatch", Risk_Pct=19.8))

    def test_the_gap_tolerance_is_hundredths_of_a_percent(self):
        r = clean_row()
        self.assertFalse(self.fires("sup_gap_mismatch", Sup_Gap_Pct=r["Sup_Gap_Pct"] + 0.02))
        self.assertTrue(self.fires("sup_gap_mismatch", Sup_Gap_Pct=r["Sup_Gap_Pct"] + 0.2))
        self.assertTrue(self.fires("res_gap_mismatch", Res_Gap_Pct=r["Res_Gap_Pct"] + 0.2))

    def test_a_close_move_over_ten_and_a_half_percent_needs_a_jump_flag(self):
        self.assertTrue(self.fires("daily_move_over_limit", Close_Prev=278.0))       # +12.2%
        self.assertFalse(self.fires("daily_move_over_limit", Close_Prev=284.0))      # +9.9%
        self.assertFalse(self.fires("daily_move_over_limit", Close_Prev=282.5))      # +10.44%
        self.assertTrue(self.fires("daily_move_over_limit", Close_Prev=282.0))       # +10.64%
        self.assertFalse(self.fires("daily_move_over_limit", Close_Prev=278.0, Recent_Jump=True))

    def test_squeeze_needs_a_support_gap_under_five_and_a_resistance_gap_under_two(self):
        close = 312.0

        def squeeze(sup_gap, res_gap, flag):
            sup = round(close / (1 + sup_gap / 100.0), 2)
            res = round(close * (1 + res_gap / 100.0), 2)
            return self.fires("squeeze_mismatch", Support_Used=sup,
                              Sup_Gap_Pct=round((close - sup) / sup * 100, 2),
                              Resist_60H=res, Res_Gap_Pct=round((res - close) / close * 100, 2),
                              Squeeze=flag)
        self.assertTrue(squeeze(5.5, 1.0, True))        # support gap too wide to be a squeeze
        self.assertFalse(squeeze(5.5, 1.0, False))
        self.assertFalse(squeeze(4.5, 1.0, True))
        self.assertTrue(squeeze(4.5, 1.0, False))
        self.assertTrue(squeeze(4.5, 2.5, True))        # resistance gap too wide
        self.assertFalse(squeeze(4.5, 1.9, True))

    def test_the_fallback_rule_when_scan_mode_cannot_be_imported(self):
        with mock.patch.dict(sys.modules, {"scanner.scan_mode": None}):
            self.assertEqual(_trade_params(), (0.20, 0.20, 0.025, 0.02, 0.10, 0.15, 20))

    def test_the_rule_the_checker_judges_by_is_the_settled_one(self):
        self.assertEqual(_trade_params(), (0.20, 0.20, 0.025, 0.02, 0.10, 0.15, 20))

    def test_the_checker_agrees_with_the_settled_core_gate_not_with_the_producer(self):
        # a CORE+ flag that disagrees with the three columns is an error
        self.assertTrue(self.fires("core_plus_mismatch", Dist_52W_High_Pct=5.1, Core_Plus=True))
        self.assertFalse(self.fires("core_plus_mismatch", Dist_52W_High_Pct=5.0, Core_Plus=True))
        self.assertTrue(self.fires("core_plus_mismatch", Dist_52W_High_Pct=5.0, Core_Plus=False))
        self.assertTrue(self.fires("core_plus_mismatch", ATR_Pct=4.4, Core_Plus=True))
        self.assertFalse(self.fires("core_plus_mismatch", ATR_Pct=4.5, Core_Plus=True))
        self.assertTrue(self.fires("core_plus_mismatch", Ret_5D_Pct=5.1, Core_Plus=True))
        self.assertFalse(self.fires("core_plus_mismatch", Ret_5D_Pct=5.0, Core_Plus=True))


class CheckerBuyGate(unittest.TestCase):
    """buy_ready_violates_gate: Buy_Ready on a row that fails any one of the
    OTC / CORE+ / fresh-signal / integrity / rank<20 gates is an error."""

    def check(self, rows):
        p = clean_payload(rows)
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def row(self, sid="6426", **changes):
        r = clean_row(sid=sid)
        r.update(Buy_Ready=True, Buy_Block="")
        r.update(changes)
        return r

    def violates(self, **changes):
        return "buy_ready_violates_gate" in codes(self.check([self.row(**changes)]), "error")

    def test_a_clean_buyable_row_passes(self):
        self.assertFalse(self.violates())

    def test_each_gate_alone_makes_a_buy_signal_an_error(self):
        self.assertTrue(self.violates(Market="TSE"))
        self.assertTrue(self.violates(Core_Plus=False))
        self.assertTrue(self.violates(Integrity_OK=False))
        self.assertTrue(self.violates(Integrity_OK=None))
        self.assertTrue(self.violates(Hold_Status="holding", First_Day=False))
        self.assertTrue(self.violates(Hold_Status="unknown", First_Day=False))

    def test_a_re_entry_first_day_counts_as_fresh(self):
        # First_Day makes the name fresh even though its streak says "holding";
        # the card is then an open trade, which is its own error
        rep = self.check([self.row(Hold_Status="holding", First_Day=True)])
        self.assertNotIn("buy_ready_violates_gate", codes(rep, "error"))
        self.assertIn("buy_ready_while_open", codes(rep))

    def test_only_the_first_twenty_rows_can_be_buy_ready(self):
        def ready_at(i):
            rows = [clean_row(sid="%04d" % (6000 + k)) for k in range(21)]
            for r in rows:
                r["Buy_Ready"] = False
                r["Buy_Block"] = "rank"
            rows[i].update(Buy_Ready=True, Buy_Block="")
            return "buy_ready_violates_gate" in codes(self.check(rows), "error")
        self.assertFalse(ready_at(19))
        self.assertTrue(ready_at(20))

    def test_a_buy_signal_on_an_open_or_closed_trade_is_reported(self):
        for status in ("holding", "delay"):
            rep = self.check([self.row(Hold_Status=status, First_Day=True)])
            self.assertIn("buy_ready_while_open", codes(rep), status)     # reported, not an error
        for status in ("exited", "exit_today", "overdue"):
            rep = self.check([self.row(Hold_Status=status, First_Day=True)])
            self.assertIn("buy_ready_on_closed_segment", codes(rep, "error"), status)

    def test_a_buy_signal_and_a_block_reason_cannot_coexist(self):
        rep = self.check([self.row(Buy_Block="rank")])
        self.assertIn("buy_ready_with_block", codes(rep, "error"))
        r = clean_row()
        r.update(Buy_Ready=False, Buy_Block="")
        self.assertIn("blocked_without_reason", codes(self.check([r]), "error"))


class CheckerMirrorsAgreeWithTheirSources(unittest.TestCase):
    def test_the_mirrored_constants(self):
        import portfolio.sync as sy
        import scanner.result_checks as rc
        import scanner.signal_ledger as sl
        import scanner.trade_restrictions as trs
        from portfolio.ledger import record_recommendation
        self.assertEqual(sy.REC_HORIZON_SESSIONS, 25)
        self.assertEqual(rc.REC_HORIZON_SESSIONS, 25)
        self.assertEqual(sy.CLOSED_GRACE_SESSIONS, 5)
        self.assertEqual(rc.BENCH_MAX_POINTS, LR.BENCH_POINTS)
        self.assertEqual(rc.BY_SID_MAX, LR.BY_SID_KEPT)
        self.assertEqual(rc.DEFAULT_BLOCKING_RESTRICTIONS, ("suspended",))
        self.assertEqual(tuple(trs.BLOCKING_RESTRICTIONS), ("suspended",))
        self.assertEqual(rc.RESTRICTION_KINDS, tuple(trs.RESTRICTION_KINDS))
        self.assertIn("Plan_Stop", rc.ORDER_LEVEL_COLUMNS)
        self.assertEqual(sl.HORIZONS, (5, 10, 20))
        self.assertEqual(inspect.signature(record_recommendation).parameters["horizon_days"].default, 10)
        self.assertEqual(rc.EXIT_SIGNALS, ("", "stop", "lock", "tp", "late", "time"))
        self.assertEqual(rc._HARD_FLAGS, ("nan:", "nonpos:", "ohlc:", "dup:", "split:"))

    def test_every_published_order_level_column_must_sit_on_the_quote_ladder(self):
        import scanner.result_checks as rc
        self.assertEqual(rc.ORDER_LEVEL_COLUMNS, (
            "Strict_Stop_Loss", "Target_Price", "Trail_Arm_Price", "Trail_Lock_Price",
            "Add_Price", "Scale_Out_Price",
            "Fill_Stop_Loss", "Fill_Trail_Arm_Price", "Fill_Trail_Lock_Price",
            "Fill_Target_Price", "Fill_Scale_Out_Price",
            "Plan_Stop", "Plan_Add_Price", "Initial_Stop_Price", "Initial_Target_Price"))

    def test_only_a_suspended_row_may_be_declared_blocking_by_default(self):
        # a payload that declares nothing is judged by the settled blocking set
        p = clean_payload([clean_row()])
        p["meta"]["quality"].pop("restrictions", None)
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertNotIn("buy_ready_on_blocked_row", codes(rep))


if __name__ == "__main__":
    unittest.main()
