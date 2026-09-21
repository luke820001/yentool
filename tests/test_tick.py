"""
Tests for the Taiwan price ladder (scanner/tick.py).

    python -m unittest tests.test_tick -v
"""
import unittest

from scanner import tick


class TickSize(unittest.TestCase):
    def test_bands(self):
        for price, want in ((5.0, 0.01), (9.99, 0.01), (10.0, 0.05),
                            (49.95, 0.05), (50.0, 0.1), (99.9, 0.1),
                            (100.0, 0.5), (499.5, 0.5), (500.0, 1.0),
                            (999.0, 1.0), (1000.0, 5.0), (2460.0, 5.0)):
            self.assertEqual(tick.tick_size(price), want, price)

    def test_unusable_prices(self):
        for bad in (0, -1, None, "x", float("nan")):
            self.assertIsNone(tick.tick_size(bad))
            self.assertIsNone(tick.round_to_tick(bad, "down"))


class Rounding(unittest.TestCase):
    def test_the_cases_the_owner_reported(self):
        # 191.50 x 0.80 = 153.20, which is not on the 0.50 ladder
        self.assertEqual(tick.stop_price(153.20), 153.0)
        # 191.50 x 1.20 = 229.80 -> a target rounds UP
        self.assertEqual(tick.target_price(229.80), 230.0)
        # 191.50 x 0.90 = 172.35 -> a buy limit rounds DOWN
        self.assertEqual(tick.buy_price(172.35), 172.0)

    def test_a_price_already_on_the_ladder_does_not_move(self):
        for price in (9.99, 10.05, 50.1, 153.0, 230.0, 500.0, 999.0, 1005.0):
            for mode in ("down", "up", "nearest"):
                self.assertEqual(tick.round_to_tick(price, mode), price,
                                 "%s %s" % (price, mode))

    def test_direction(self):
        self.assertEqual(tick.round_to_tick(153.4, "down"), 153.0)
        self.assertEqual(tick.round_to_tick(153.4, "up"), 153.5)
        self.assertEqual(tick.round_to_tick(153.4, "nearest"), 153.5)
        self.assertEqual(tick.round_to_tick(153.2, "nearest"), 153.0)

    def test_moves_by_at_most_one_tick(self):
        for price in (7.777, 23.456, 88.88, 234.56, 654.3, 1234.5):
            size = tick.tick_size(price)
            for mode in ("down", "up", "nearest"):
                got = tick.round_to_tick(price, mode)
                self.assertLessEqual(abs(got - price), size + 1e-9,
                                     "%s %s" % (price, mode))

    def test_band_boundary_is_a_valid_order(self):
        # 499.8 rounds up to 500.0, where the step becomes 1.00 -- 500.0 is on
        # both ladders, so it stays
        self.assertEqual(tick.round_to_tick(499.8, "up"), 500.0)
        self.assertEqual(tick.round_to_tick(500.2, "down"), 500.0)
        self.assertEqual(tick.round_to_tick(1001.0, "down"), 1000.0)
        self.assertEqual(tick.round_to_tick(1001.0, "up"), 1005.0)

    def test_is_on_tick(self):
        self.assertTrue(tick.is_on_tick(153.0))
        self.assertTrue(tick.is_on_tick(10.05))
        self.assertFalse(tick.is_on_tick(153.2))
        self.assertFalse(tick.is_on_tick(229.8))
        self.assertFalse(tick.is_on_tick(172.35))
        self.assertFalse(tick.is_on_tick(0))

    def test_every_rounded_price_is_on_tick(self):
        p = 3.0
        while p < 3000:
            for mode in ("down", "up", "nearest"):
                got = tick.round_to_tick(p, mode)
                self.assertTrue(tick.is_on_tick(got), "%s %s -> %s" % (p, mode, got))
            p *= 1.013


if __name__ == "__main__":
    unittest.main()
