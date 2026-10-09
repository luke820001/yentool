"""
Small conformance pins from the 2026-10-09 audit that belong to no larger file.

    python -m unittest tests.test_conformance_20261009 -v

ASCII only.
"""
import unittest

import pandas as pd

from scanner import live_record as LR
from scanner.exit_rules import replay_exit


def _hist(last, n=130, atr_ratio=0.05):
    close = [100.0] * (n - 1) + [last]
    return pd.DataFrame({"close": close,
                         "high": [c * (1 + atr_ratio) for c in close],
                         "low": [c * (1 - atr_ratio) for c in close]})


class LiveCoreRecomputeRoundsLikeTheScan(unittest.TestCase):
    """The scan gates on Ret_5D_Pct / Dist_52W_High_Pct rounded to 1 decimal and
    ATR_Pct to 2 (analyzer/trend_analysis.py); the live record's recompute of
    CORE+ for an old signal must read the same stored precision."""

    def test_plus_5_03_percent_is_stored_as_5_0_and_passes(self):
        core, _full = LR._core_from_bars(_hist(105.03))        # 5.03 -> 5.0 <= 5
        self.assertTrue(core)

    def test_plus_5_05_percent_is_stored_as_5_1_and_fails(self):
        core, _full = LR._core_from_bars(_hist(105.06))        # 5.06 -> 5.1 > 5
        self.assertFalse(core)

    def test_atr_4_4965_is_stored_as_4_5_and_passes(self):
        # range ratio 0.0224825 each side -> mean (high-low)/close = 4.4965 -> 4.50
        core, _full = LR._core_from_bars(_hist(100.0, atr_ratio=0.0224825))
        self.assertTrue(core)

    def test_atr_just_under_4_495_fails(self):
        core, _full = LR._core_from_bars(_hist(100.0, atr_ratio=0.0224))
        self.assertFalse(core)


class AnExactTouchOfALevelBooksTheExit(unittest.TestCase):
    """D8-06: a level is a product of floats (5.60 * 0.8 reads back as
    4.4799999999999995), so a low of exactly 4.48 used to miss the stop below
    about NT$12. 'The bar touched the level' includes equality."""

    @staticmethod
    def _cents(x):
        return abs(x * 100 - round(x * 100)) < 1e-6        # a level quoted in whole cents

    def test_the_documented_case(self):
        r = replay_exit([5.60, 5.5], [5.7, 5.6], [5.5, 4.48], [5.6, 4.6])
        self.assertTrue(r["exited"])
        self.assertEqual(r["reason"], "stop")
        self.assertAlmostEqual(r["exit_price"], 4.48, places=9)

    def test_every_cent_level_from_one_to_twelve_dollars(self):
        checked = 0
        for k in range(100, 1200):                          # entries 1.00 .. 11.99
            e = k / 100.0
            stop, tp = e * 0.8, e * 1.2
            if self._cents(stop):
                r = replay_exit([e, e], [e * 1.01, e], [e * 0.99, round(stop, 2)], [e, e])
                self.assertEqual((r["exited"], r["reason"]), (True, "stop"), e)
                checked += 1
            if self._cents(tp):
                r = replay_exit([e, e], [e * 1.01, round(tp, 2)], [e * 0.99, e], [e, e])
                self.assertEqual((r["exited"], r["reason"]), (True, "tp"), e)
                checked += 1
        self.assertGreater(checked, 400)

    def test_a_cent_inside_the_level_still_does_not_exit(self):
        r = replay_exit([5.60, 5.5], [5.7, 5.6], [5.5, 4.49], [5.6, 4.6])
        self.assertFalse(r["exited"])
        r = replay_exit([5.60, 5.7], [5.7, 6.71], [5.5, 5.6], [5.6, 6.5])
        self.assertFalse(r["exited"])


if __name__ == "__main__":
    unittest.main()
