"""Pins for the EXIT side of the settled rule. ASCII only, stdlib unittest.

The settled exit (docs/STRATEGY.md section 3, BACKTEST_LOG.md): disaster stop
20% below the fill, take profit 20% above it, a CLOSE at or above +2.5% arms a
lock at +2% that guards from the NEXT bar, from trading day 8 a close at or
above +1% sells the next open, a time exit on the close of bar 10, and a ride
past bar 10 while the close is above its own 5-bar mean (or the market leg
says so), capped at bar 20.

A 2026-10-09 mutation audit flipped `<=` to `<`, `>=` to `>`, a window by one
bar, a rounding direction -- and the suite stayed green for most of them,
because the existing exit tests use clean numbers far from every edge. Here
each level is hit EXACTLY and then missed by one tick.

Levels are read off the engine for a 100.00 fill (rule_levels) rather than
typed as 102.5 / 80.0, because 100 * 1.025 is not exactly 102.5 in binary and
the test must compare the engine with itself at the edge, while a separate
test pins those levels to the settled literals within a millionth.

    python -m unittest tests.test_pin_exit_rule -v
"""
import datetime
import types
import unittest
from unittest import mock

import pandas as pd

import scanner.holding_tracker as tracker
from scanner.exit_rules import DEFAULT_RULE, replay_exit, simulate_exit
from tests.pin_support import taiex_db
from tests.test_exit_plan import _TrackerHarness

FLAT = (100.0, 100.5, 99.5, 100.0)        # an uneventful bar on a 100 fill


def cols(rows):
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


def play(rows, **kw):
    o, h, l, c = cols(rows)
    kw.setdefault("hold_bars", None)
    return replay_exit(o, h, l, c, **kw)


def rule_levels():
    """(stop, arm, target, lock, late) the engine derives from a 100 fill."""
    p = play([FLAT])
    return p["stop"], p["arm_px"], p["target"], p["lock_px"], p["late_px"]


STOP, ARM, TARGET, LOCK, LATE = rule_levels()
TICK = 0.01


class SettledLevels(unittest.TestCase):
    def test_the_levels_off_a_100_fill_are_the_settled_percentages(self):
        self.assertAlmostEqual(STOP, 80.0, places=6)
        self.assertAlmostEqual(ARM, 102.5, places=6)
        self.assertAlmostEqual(TARGET, 120.0, places=6)
        self.assertAlmostEqual(LOCK, 102.0, places=6)
        self.assertAlmostEqual(LATE, 101.0, places=6)

    def test_the_rule_dictionary_is_the_settled_one(self):
        self.assertEqual(DEFAULT_RULE, {
            "stop_pct": 0.20, "tp_pct": 0.20, "arm_pct": 0.025, "lock_pct": 0.02,
            "hold_bars": 10, "late_from": 8, "late_gain": 0.01, "ride_cap": 20})

    def test_the_tracker_and_the_scan_use_the_same_numbers_as_the_engine(self):
        import scanner.scan_mode as sm
        self.assertEqual((tracker.STOP_PCT, tracker.TP_PCT, tracker.TRAIL_ARM,
                          tracker.TRAIL_LOCK, tracker.ADD_PCT, tracker.SCALE_OUT_PCT),
                         (0.20, 0.20, 0.025, 0.02, 0.10, 0.15))
        self.assertEqual((tracker.LATE_FROM, tracker.LATE_GAIN), (8, 0.01))
        self.assertEqual((sm.PRELAUNCH_STOP_PCT, sm.PRELAUNCH_TP_PCT,
                          sm.PRELAUNCH_TRAIL_ARM, sm.PRELAUNCH_TRAIL_LOCK),
                         (DEFAULT_RULE["stop_pct"], DEFAULT_RULE["tp_pct"],
                          DEFAULT_RULE["arm_pct"], DEFAULT_RULE["lock_pct"]))

    def test_the_entry_is_the_first_open_and_the_return_is_off_it(self):
        p = play([(50.0, 51.0, 49.0, 50.0), (50.0, 51.0, 40.0, 41.0)])
        self.assertEqual(p["entry"], 50.0)
        self.assertEqual(p["reason"], "stop")
        self.assertAlmostEqual(p["exit_price"], 40.0, places=6)
        self.assertAlmostEqual(p["ret_pct"], -20.0, places=6)


class StopAndTarget(unittest.TestCase):
    def test_a_low_exactly_on_the_stop_stops_out_one_tick_above_does_not(self):
        p = play([FLAT, (99.0, 100.0, STOP, 95.0)])
        self.assertEqual((p["reason"], p["bar"]), ("stop", 1))
        self.assertAlmostEqual(p["exit_price"], STOP, places=6)
        p = play([FLAT, (99.0, 100.0, STOP + TICK, 95.0)])
        self.assertEqual(p["reason"], "")
        self.assertFalse(p["exited"])

    def test_a_high_exactly_on_the_target_takes_profit_one_tick_below_does_not(self):
        p = play([FLAT, (101.0, TARGET, 100.0, 110.0)])
        self.assertEqual((p["reason"], p["bar"]), ("tp", 1))
        self.assertAlmostEqual(p["exit_price"], TARGET, places=6)
        p = play([FLAT, (101.0, TARGET - TICK, 100.0, 110.0)])
        self.assertEqual(p["reason"], "")

    def test_the_entry_bar_itself_can_be_stopped(self):
        p = play([(100.0, 100.5, STOP, 90.0)])
        self.assertEqual((p["reason"], p["bar"]), ("stop", 0))

    def test_a_bar_that_touches_both_levels_is_a_stop_not_a_target(self):
        p = play([FLAT, (100.0, TARGET + 5, STOP - 5, 100.0)])
        self.assertEqual(p["reason"], "stop")
        self.assertAlmostEqual(p["exit_price"], STOP, places=6)

    def test_an_open_through_the_target_books_the_open_not_the_target(self):
        p = play([FLAT, (TARGET + 3, TARGET + 4, TARGET + 2, TARGET + 3)])
        self.assertEqual(p["reason"], "tp")
        self.assertAlmostEqual(p["exit_price"], TARGET + 3, places=6)

    def test_an_open_through_the_stop_books_the_open_not_the_stop(self):
        p = play([FLAT, (STOP - 7, STOP - 6, STOP - 9, STOP - 8)])
        self.assertEqual(p["reason"], "stop")
        self.assertAlmostEqual(p["exit_price"], STOP - 7, places=6)

    def test_an_open_exactly_on_a_level_fills_at_that_level(self):
        p = play([FLAT, (STOP, STOP + 1, STOP - 1, STOP)])
        self.assertEqual((p["reason"], p["exit_price"]), ("stop", STOP))
        p = play([FLAT, (TARGET, TARGET + 1, TARGET - 1, TARGET)])
        self.assertEqual((p["reason"], p["exit_price"]), ("tp", TARGET))

    def test_a_stop_after_arming_is_reported_as_a_lock(self):
        p = play([FLAT, (100.0, 103.0, 100.0, ARM), (103.0, 104.0, LOCK, 103.0)])
        self.assertEqual(p["reason"], "lock")
        self.assertTrue(p["armed"])


class TrailingLock(unittest.TestCase):
    def test_a_close_exactly_on_the_arm_price_arms_one_tick_below_does_not(self):
        p = play([(100.0, 103.0, 99.5, ARM)])
        self.assertTrue(p["armed"])
        self.assertAlmostEqual(p["stop"], LOCK, places=6)
        p = play([(100.0, 103.0, 99.5, ARM - TICK)])
        self.assertFalse(p["armed"])
        self.assertAlmostEqual(p["stop"], STOP, places=6)

    def test_arming_reads_the_close_not_the_high(self):
        p = play([(100.0, 110.0, 99.5, 101.0)])        # high far above +2.5%
        self.assertFalse(p["armed"])
        self.assertAlmostEqual(p["stop"], STOP, places=6)

    def test_the_lock_guards_from_the_next_bar_not_the_arming_bar(self):
        # the arming bar trades down to 100.5, under the lock (102) it is about
        # to arm: it must NOT be stopped on a stop that did not exist yet
        p = play([FLAT, (100.0, 103.0, 100.5, ARM)])
        self.assertFalse(p["exited"])
        self.assertTrue(p["armed"])
        # the next bar, trading to the lock, is stopped there
        p = play([FLAT, (100.0, 103.0, 100.5, ARM), (103.0, 104.0, LOCK, 103.0)])
        self.assertEqual((p["reason"], p["bar"]), ("lock", 2))
        self.assertAlmostEqual(p["exit_price"], LOCK, places=6)

    def test_a_low_one_tick_above_the_lock_survives(self):
        p = play([FLAT, (100.0, 103.0, 100.5, ARM), (103.0, 104.0, LOCK + TICK, 103.0)])
        self.assertFalse(p["exited"])

    def test_a_gap_down_through_the_lock_books_the_open(self):
        p = play([FLAT, (100.0, 103.0, 100.5, ARM), (101.0, 102.0, 100.0, 101.0)])
        self.assertEqual(p["reason"], "lock")
        self.assertAlmostEqual(p["exit_price"], 101.0, places=6)

    def test_the_stop_never_moves_down_once_raised(self):
        p = play([FLAT, (100.0, 103.0, 100.5, ARM), (103.0, 104.0, 102.5, 103.0),
                  (103.0, 104.0, 102.5, 100.0)])
        self.assertAlmostEqual(p["stop"], LOCK, places=6)


class LateProfitTake(unittest.TestCase):
    def tape(self, day_closes):
        """FLAT bars, then `day_closes` = {trading day: close}."""
        rows = [FLAT] * 12
        for day, close in day_closes.items():
            rows[day - 1] = (100.0, max(close, 100.5), 99.5, close)
        return rows

    def test_a_close_exactly_at_plus_one_percent_on_day_8_sells_the_next_open(self):
        rows = self.tape({8: LATE})
        rows[8] = (103.7, 104.0, 103.0, 103.5)               # day 9 opens at 103.7
        p = play(rows[:9])
        self.assertEqual((p["reason"], p["bar"]), ("late", 8))
        self.assertAlmostEqual(p["exit_price"], 103.7, places=6)

    def test_one_tick_under_plus_one_percent_does_not(self):
        p = play(self.tape({8: LATE - TICK}))
        self.assertFalse(p["late_due"])
        self.assertNotEqual(p["reason"], "late")

    def test_the_last_close_arms_the_flag_the_caller_reads(self):
        p = play(self.tape({8: LATE})[:8])
        self.assertTrue(p["late_due"])
        self.assertEqual(p["reason"], "")

    def test_day_7_is_too_early_day_8_is_the_first(self):
        p = play(self.tape({7: 103.0})[:8])                   # day 7 in profit, day 8 flat
        self.assertFalse(p["late_due"])
        self.assertNotEqual(p["reason"], "late")
        p = play(self.tape({8: 103.0})[:8])
        self.assertTrue(p["late_due"])

    def test_a_late_order_fills_at_the_open_even_when_the_open_gaps(self):
        rows = self.tape({8: 103.0})
        rows[8] = (95.0, 96.0, 94.0, 95.0)                    # opens 5% under the fill
        p = play(rows[:9])
        self.assertEqual(p["reason"], "late")
        self.assertAlmostEqual(p["exit_price"], 95.0, places=6)


class TimeExitAndRide(unittest.TestCase):
    """Bar 10 is index 9. The ride keeps the position only while the close is
    STRICTLY above the mean of the last five closes (this bar included)."""

    def run_rule(self, closes, **kw):
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        rows[0] = (100.0, 100.2, 99.8, closes[0])
        kw.setdefault("late_from", None)           # keep the late leg out of these cases
        kw.setdefault("hold_bars", 10)
        o, h, l, c = cols(rows)
        return replay_exit(o, h, l, c, **kw)

    def test_the_time_exit_is_the_close_of_bar_10(self):
        p = self.run_rule([100.0] * 12)
        self.assertEqual((p["reason"], p["bar"]), ("time", 9))
        self.assertAlmostEqual(p["exit_price"], 100.0, places=6)

    def test_nine_bars_is_not_a_result_yet(self):
        p = self.run_rule([100.0] * 9)
        self.assertIn(p["reason"], ("", "na"))
        self.assertFalse(p["exited"])

    def test_simulate_exit_defaults_to_ten_bars(self):
        flat = ([100.0] * 12, [100.2] * 12, [99.8] * 12, [100.0] * 12)
        self.assertEqual(simulate_exit(*[x[:9] for x in flat])[2], "na")
        self.assertEqual(simulate_exit(*[x[:10] for x in flat], late_from=None)[2], "time")
        entry, ret, why = simulate_exit(*flat, late_from=None)
        self.assertEqual((entry, why), (100.0, "time"))
        self.assertAlmostEqual(ret, 0.0, places=6)

    def test_a_close_above_its_5_bar_mean_keeps_the_position(self):
        closes = [100.0] * 9 + [101.0, 99.0, 99.0]
        # bar 10: mean of the last five = (100 + 100 + 100 + 100 + 101) / 5 = 100.2
        p = self.run_rule(closes)
        self.assertEqual((p["reason"], p["bar"]), ("time", 10))     # bar 11 closes 99 < its mean
        self.assertAlmostEqual(p["exit_price"], 99.0, places=6)

    def test_a_close_equal_to_its_5_bar_mean_does_not_ride(self):
        p = self.run_rule([100.0] * 9 + [100.0] + [100.0, 100.0])
        self.assertEqual(p["bar"], 9)

    def test_the_ride_is_capped_at_bar_20(self):
        closes = [100.0 + 0.05 * k for k in range(25)]               # climbs every bar, below the arm
        p = self.run_rule(closes)
        self.assertEqual((p["reason"], p["bar"]), ("time", 19))
        self.assertAlmostEqual(p["exit_price"], closes[19], places=6)

    def test_a_window_that_ends_mid_ride_leaves_the_trade_open(self):
        closes = [100.0 + 0.05 * k for k in range(15)]
        p = self.run_rule(closes)
        self.assertEqual(p["reason"], "")
        self.assertTrue(p["riding"])
        self.assertFalse(p["exited"])

    def test_the_mean_is_over_5_closes_not_4_or_6(self):
        # bar 10 closes 100.5. The 5-bar mean is 100.58 (it includes the 102.4
        # five bars back): 100.5 is NOT above it, the position is closed. A
        # 4-bar mean (100.125) or a 6-bar mean (100.483) would ride instead.
        closes = [100.0] * 5 + [102.4] + [100.0] * 3 + [100.5, 99.0]
        p = self.run_rule(closes)
        self.assertEqual((p["reason"], p["bar"]), ("time", 9))
        # with that 102.4 one bar older the same close is above its 5-bar mean
        # (100.1) and rides one more bar
        closes = [100.0] * 4 + [102.4] + [100.0] * 4 + [100.5, 99.0]
        p = self.run_rule(closes)
        self.assertEqual((p["reason"], p["bar"]), ("time", 10))

    def test_the_market_leg_is_asked_only_where_the_time_exit_would_fire(self):
        asked = []

        def leg(i, when):
            asked.append((i, when))
            return True
        rows = [(100.0, 100.2, 99.8, 100.0)] * 25
        o, h, l, c = cols(rows)
        dates = ["d%02d" % i for i in range(25)]
        p = replay_exit(o, h, l, c, dates=dates, hold_bars=10, late_from=None, extend_if=leg)
        self.assertEqual((p["reason"], p["bar"]), ("time", 19))
        self.assertEqual(asked[0], (9, "d09"))
        self.assertEqual([i for i, _ in asked], list(range(9, 19)))

    def test_the_stock_leg_wins_first_and_the_market_leg_is_not_consulted(self):
        asked = []
        closes = [100.0] * 9 + [101.0]
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes] + [(101.0, 101.2, 100.8, 100.0)] * 3
        o, h, l, c = cols(rows)
        replay_exit(o, h, l, c, dates=["d%02d" % i for i in range(13)], hold_bars=10,
                    late_from=None, extend_if=lambda i, d: asked.append(i) or False)
        self.assertNotIn(9, asked)
        self.assertIn(10, asked)

    def test_a_market_leg_that_raises_means_no_extension(self):
        def boom(i, when):
            raise RuntimeError("no TAIEX")
        rows = [(100.0, 100.2, 99.8, 100.0)] * 14
        o, h, l, c = cols(rows)
        p = replay_exit(o, h, l, c, hold_bars=10, late_from=None, extend_if=boom)
        self.assertEqual((p["reason"], p["bar"]), ("time", 9))

    def test_without_a_market_leg_the_default_is_byte_identical(self):
        rows = [(100.0, 100.2, 99.8, 100.0)] * 14
        o, h, l, c = cols(rows)
        a = replay_exit(o, h, l, c, hold_bars=10, late_from=None)
        b = replay_exit(o, h, l, c, hold_bars=10, late_from=None, extend_if=None)
        self.assertEqual(a, b)


# ------------------------------------------------------------------ market leg
class MarketLegBoundaries(unittest.TestCase):
    """Disturbed on a date <=> close < its 20-day mean AND close > its 60-day
    mean. Equality on either side is NOT disturbed; a short history is not."""

    def disturbed(self, closes):
        from scanner import market_leg
        market_leg.clear_cache()
        with taiex_db(closes) as (path, dates):
            table = market_leg.disturbed_by_date(path)
            return table[dates[-1]], table, dates

    def test_a_pullback_inside_an_uptrend_is_disturbed(self):
        self.assertTrue(self.disturbed([100.0 + i for i in range(100)] + [195, 192, 190, 188, 186])[0])

    def test_a_close_equal_to_its_20_day_mean_is_not_disturbed(self):
        # last 20 closes: 19 x 100 + ... build so mean20 == close exactly
        closes = [90.0] * 40 + [100.0] * 20
        self.assertFalse(self.disturbed(closes)[0])

    def test_a_close_one_step_under_its_20_day_mean_is_disturbed(self):
        closes = [90.0] * 40 + [100.0] * 19 + [99.5]
        self.assertTrue(self.disturbed(closes)[0])          # mean20 = 99.975 > 99.5, mean60 = 93.3

    def test_a_close_equal_to_its_60_day_mean_is_not_disturbed(self):
        closes = [95.0] * 38 + [100.0] * 2 + [110.0] * 19 + [100.0]
        self.assertEqual(sum(closes), 6000.0)               # mean60 == 100 == close
        self.assertFalse(self.disturbed(closes)[0])

    def test_a_close_one_step_over_its_60_day_mean_is_disturbed(self):
        closes = [95.0] * 38 + [100.0] * 2 + [110.0] * 19 + [100.5]
        self.assertTrue(self.disturbed(closes)[0])

    def test_the_20_day_window_is_20_bars_long(self):
        # the 200 spike is the 21st bar back: outside a 20-bar window (mean 99.1,
        # close 101 is ABOVE it) but inside a 21-bar one
        closes = [100.0] * 39 + [200.0] + [99.0] * 19 + [101.0]
        self.assertFalse(self.disturbed([100.0] * 20 + closes)[0])
        # the spike is the OLDEST bar of the 20-bar window: mean 104.15, close is under it
        closes = [90.0] * 41 + [200.0] + [99.0] * 18 + [101.0]
        self.assertTrue(self.disturbed(closes)[0])

    def test_the_60_day_window_is_60_bars_long(self):
        # a 300 spike 61 bars back is outside a 60-bar window (mean 100.01, close 100.5 above it)
        closes = [100.0] * 5 + [300.0] + [100.0] * 59 + [100.5]
        self.assertFalse(self.disturbed(closes)[0])
        # a 40 low on the OLDEST bar of the 60-bar window pulls the mean under the close
        closes = [100.0] * 20 + [40.0] + [103.0] * 39 + [108.0] * 19 + [104.5]
        self.assertTrue(self.disturbed(closes)[0])

    def test_fewer_than_60_bars_is_never_disturbed(self):
        flag, table, dates = self.disturbed([100.0 + i for i in range(40)] + [120, 119, 118, 117, 116])
        self.assertFalse(any(table.values()))

    def test_an_unknown_date_and_an_empty_table_never_extend_a_trade(self):
        from scanner import market_leg
        market_leg.clear_cache()
        closes = [100.0 + i for i in range(100)] + [195, 192, 190, 188, 186]
        with taiex_db(closes) as (path, dates):
            fn = market_leg.make_disturbed_fn(path)
            self.assertTrue(fn(0, dates[-1]))
            self.assertTrue(fn(0, dates[-1] + " 00:00:00"))
            self.assertFalse(fn(0, "1999-01-04"))
            self.assertFalse(fn(0, None))
            self.assertFalse(fn(0, ""))
            self.assertTrue(market_leg.is_disturbed(dates[-1], path))
            self.assertFalse(market_leg.is_disturbed("", path))
        market_leg.clear_cache()
        self.assertEqual(market_leg.disturbed_by_date("Z:/no/such/taiex.db"), {})
        self.assertFalse(market_leg.make_disturbed_fn("Z:/no/such/taiex.db")(0, "2026-10-08"))

    def test_the_tracker_builds_its_market_leg_from_the_taiex_file(self):
        from scanner import market_leg
        market_leg.clear_cache()
        closes = [100.0 + i for i in range(100)] + [195, 192, 190, 188, 186]
        with taiex_db(closes) as (path, dates):
            with mock.patch("config.settings.TAIEX_FILE", path):
                fn = tracker._disturbed_fn()
            self.assertIsNotNone(fn)
            self.assertTrue(fn(0, dates[-1]))
            self.assertFalse(fn(0, dates[-30]))
        market_leg.clear_cache()

    def test_a_ride_is_extended_by_the_market_leg_on_that_dates_reading(self):
        from scanner import market_leg
        market_leg.clear_cache()
        closes = [100.0 + i for i in range(100)] + [195, 192, 190, 188, 186]
        rows = [(100.0, 100.2, 99.8, 100.0)] * 14
        o, h, l, c = cols(rows)
        with taiex_db(closes) as (path, dates):
            fn = market_leg.make_disturbed_fn(path)
            tape = dates[-14:]
            # every bar of this window falls inside the pullback only at the end;
            # the trade is asked per bar date, so its answer is that date's
            p = replay_exit(o, h, l, c, dates=tape, hold_bars=10, late_from=None, extend_if=fn)
            on_bar_10 = fn(9, tape[9])
        market_leg.clear_cache()
        self.assertEqual(p["bar"] == 9, not on_bar_10)


# --------------------------------------------------------------------- tracker
class TrackerOffLadderFill(_TrackerHarness, unittest.TestCase):
    """Every older fixture filled at 100.00, whose levels all sit exactly on
    the quote ladder, so no rounding DIRECTION was ever exercised. 191.50 is
    on the 0.50 band while none of its levels is."""

    def test_every_fill_level_rounds_the_way_that_never_flatters(self):
        flat = [(191.5, 192.5, 191.0, 191.5)] * 10
        out = self._run({"6488": flat}, {"6488": ["2026-09-01"]})
        r = out.iloc[0]
        self.assertAlmostEqual(r["Entry_Open"], 191.5)
        self.assertAlmostEqual(r["Fill_Stop_Loss"], 153.0)         # 153.20 down
        self.assertAlmostEqual(r["Fill_Trail_Arm_Price"], 196.5)   # 196.29 up
        self.assertAlmostEqual(r["Fill_Trail_Lock_Price"], 195.0)  # 195.33 down
        self.assertAlmostEqual(r["Fill_Target_Price"], 230.0)      # 229.80 up
        self.assertAlmostEqual(r["Fill_Scale_Out_Price"], 220.5)   # 220.23 up
        self.assertAlmostEqual(r["Plan_Add_Price"], 172.0)         # 172.35 down
        self.assertAlmostEqual(r["Plan_Stop"], 153.0)

    def test_an_armed_stop_is_published_on_the_tick_below_the_lock(self):
        rows = [(100, 101, 99, 100)] * 6 + [(191.5, 200.0, 191.0, 197.0)] + \
               [(197.0, 198.0, 196.0, 197.0)] * 3
        out = self._run({"6488": rows}, {"6488": [self.CAL[5]]})
        r = out.iloc[0]
        self.assertTrue(r["Plan_Armed"])
        self.assertAlmostEqual(r["Plan_Stop"], 195.0)              # 195.33 down, not 195.5


class TrackerHoldBoundaries(unittest.TestCase):
    CAL = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-09-01", periods=30)]

    def plan(self, today_idx, sbars=None, fill=191.5):
        idx_of = {d: i for i, d in enumerate(self.CAL)}
        info = {"anchor": self.CAL[0], "ref_stop": 153.2, "ref_add": 172.35, "kind": "first"}
        if sbars is None:
            sbars = [(self.CAL[1 + i], 191.5, 192.0, 191.0, 191.5) for i in range(5)]
        return tracker._plan_row("6488", info, fill, sbars, self.CAL, idx_of,
                                 self.CAL[today_idx], today_idx, 10, 20, None)

    def test_a_fill_with_no_bar_yet_shows_the_stop_on_the_tick_below(self):
        out = self.plan(1, sbars=[])
        self.assertAlmostEqual(out["Plan_Stop"], 153.0)            # 153.20 down, not 153.5
        self.assertAlmostEqual(out["Plan_Add_Price"], 172.0)

    def test_the_cap_bar_with_no_exit_in_the_store_is_overdue(self):
        # entry idx 1, cap 20 -> the cap bar is idx 20
        self.assertEqual(self.plan(20)["Hold_Status"], "overdue")
        self.assertEqual(self.plan(19)["Hold_Status"], "holding")
        self.assertEqual(self.plan(21)["Hold_Status"], "overdue")

    def test_the_base_exit_date_is_the_close_of_bar_10(self):
        out = self.plan(1)
        self.assertEqual(out["Entry_Date"], self.CAL[1])
        self.assertEqual(out["Exit_Date"], self.CAL[10])
        self.assertEqual(out["Hold_Remaining"], 9)
        self.assertEqual(self.plan(10)["Hold_Remaining"], 0)

    def test_a_tenth_bar_without_a_usable_close_falls_back_to_the_calendar(self):
        # ten bars in the store, the tenth close is missing: no result, and the
        # row must say the exit is due today, not that it is still early
        bars = [(self.CAL[1 + i], 191.5, 192.0, 191.0, 191.5) for i in range(9)]
        bars.append((self.CAL[10], 191.5, 192.0, 191.0, float("nan")))
        out = self.plan(10, sbars=bars)
        self.assertEqual(out["Hold_Status"], "exit_today")


# ------------------------------------------------------------------ desktop GUI
class _FakeDT(datetime.datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


class GuiLiveHold(unittest.TestCase):
    """The desktop recomputes the hold day as of NOW; it has its own copy of
    the 10 / 20 / 09:00 / two-leg rule."""
    CAL = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-09-01", periods=40)]

    def hold(self, now, row=None, disturbed=False):
        import gui.app as gapp
        _FakeDT._now = now
        fake = types.SimpleNamespace(datetime=_FakeDT, date=datetime.date,
                                     timedelta=datetime.timedelta)
        row = dict({"Entry_Date": self.CAL[0]}, **(row or {}))
        with mock.patch.object(gapp, "_calendar", lambda: self.CAL), \
                mock.patch.object(gapp, "datetime", fake):
            return gapp._live_hold(row, disturbed=disturbed)

    def at(self, i, hour=14, minute=0):
        y, m, d = (int(x) for x in self.CAL[i].split("-"))
        return datetime.datetime(y, m, d, hour, minute)

    def test_defaults_are_ten_and_twenty(self):
        r = self.hold(self.at(3))
        self.assertEqual((r["total"], r["cap"]), (10, 20))

    def test_the_entry_day_is_a_plan_until_nine(self):
        self.assertEqual(self.hold(self.at(0, 8, 59))["status"], "pending")
        self.assertEqual(self.hold(self.at(0, 9, 0))["status"], "holding")

    def test_day_ten_rides_on_either_leg_and_otherwise_exits(self):
        d10 = self.at(9)
        self.assertEqual(self.hold(d10)["status"], "exit_today")
        self.assertEqual(self.hold(d10, disturbed=True)["status"], "delay")              # market leg
        self.assertEqual(self.hold(d10, {"Close_Price": 105.0, "MA5": 100.0})["status"], "delay")  # stock leg
        self.assertEqual(self.hold(d10, {"Close_Price": 100.0, "MA5": 100.0})["status"], "exit_today")

    def test_day_nine_is_still_holding(self):
        self.assertEqual(self.hold(self.at(8), disturbed=True)["status"], "holding")

    def test_the_cap_bar_exits_and_the_next_one_is_overdue(self):
        self.assertEqual(self.hold(self.at(19), disturbed=True)["status"], "exit_today")
        self.assertEqual(self.hold(self.at(18), disturbed=True)["status"], "delay")
        self.assertEqual(self.hold(self.at(20), disturbed=True)["status"], "overdue")


if __name__ == "__main__":
    unittest.main()
