"""
scanner/index_clean.py decides which stored index bars are real, and
market_regime reads the result AS OF the session being scanned
(2026-10-09 audit D1-03 / D11-06 / M-12). ASCII only.
"""
import datetime as dt
import unittest
from unittest import mock

import pandas as pd

from scanner import index_clean, market_regime
from scanner.index_clean import clean_closes


def _sessions(n, start=dt.date(2026, 1, 5)):
    """n real trading-session date strings (weekday and not a known closure)."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and index_clean._is_session(d.isoformat()) is not False:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _table(dates, closes):
    return pd.DataFrame({"date": dates, "close": closes})


class CleanClosesTest(unittest.TestCase):
    def test_clean_series_is_untouched(self):
        days = _sessions(30)
        closes = [100 + i * 0.5 for i in range(30)]
        out, dropped = clean_closes(_table(days, closes))
        self.assertEqual(list(out["date"]), days)
        self.assertEqual(sum(dropped.values()), 0)

    def test_weekend_bar_is_dropped(self):
        days = _sessions(10)
        sunday = "2026-09-20"
        self.assertEqual(dt.date.fromisoformat(sunday).weekday(), 6)
        t = _table(days + [sunday], [100.0] * 10 + [101.0])
        out, dropped = clean_closes(t)
        self.assertNotIn(sunday, list(out["date"]))
        self.assertEqual(dropped["non_session"], 1)

    def test_bad_closes_are_dropped(self):
        days = _sessions(6)
        t = _table(days, [100.0, 0.0, float("nan"), -5.0, 101.0, 102.0])
        out, dropped = clean_closes(t)
        self.assertEqual(dropped["bad_close"], 3)
        self.assertEqual(list(out["date"]), [days[0], days[4], days[5]])

    def test_unparseable_date_is_dropped_and_counted(self):
        days = _sessions(3)
        t = _table(days + ["not-a-date"], [100.0, 101.0, 102.0, 103.0])
        out, dropped = clean_closes(t)
        self.assertEqual(list(out["date"]), days)
        self.assertEqual(dropped["bad_date"], 1)

    def test_single_spike_goes_but_neighbours_stay(self):
        days = _sessions(10)
        closes = [100.0] * 10
        closes[5] = 1000.0                       # a 10x print
        out, dropped = clean_closes(_table(days, closes))
        self.assertEqual(dropped["jump"], 1)
        self.assertEqual(list(out["date"]),
                         [d for i, d in enumerate(days) if i != 5])

    def test_bad_first_bar_does_not_take_good_bars_with_it(self):
        days = _sessions(10)
        closes = [1000.0] + [100.0] * 9
        out, dropped = clean_closes(_table(days, closes))
        self.assertEqual(dropped["jump"], 1)
        self.assertEqual(list(out["date"]), days[1:])

    def test_bad_last_bar_is_judged_by_its_single_neighbour(self):
        days = _sessions(10)
        closes = [100.0] * 9 + [0.5]
        out, dropped = clean_closes(_table(days, closes))
        self.assertEqual(dropped["jump"], 1)
        self.assertEqual(list(out["date"]), days[:9])

    def test_real_limit_move_is_kept(self):
        days = _sessions(6)
        closes = [100.0, 100.0, 90.5, 90.5, 90.5, 90.5]   # about -9.5 pct
        out, dropped = clean_closes(_table(days, closes))
        self.assertEqual(sum(dropped.values()), 0)
        self.assertEqual(len(out), 6)

    def test_duplicate_dates_keep_last(self):
        days = _sessions(3)
        t = _table([days[0], days[1], days[1], days[2]],
                   [100.0, 100.0, 101.0, 101.0])
        out, _dropped = clean_closes(t)
        self.assertEqual(list(out["date"]), days)
        self.assertEqual(float(out["close"].iloc[1]), 101.0)

    def test_garbage_input_never_raises(self):
        for bad in (None, pd.DataFrame(), pd.DataFrame({"x": [1]}),
                    pd.DataFrame({"date": [None], "close": [None]})):
            out, dropped = clean_closes(bad)
            self.assertEqual(len(out), 0)
            self.assertEqual(set(dropped), {"bad_close", "bad_date",
                                            "non_session", "jump"})

    def test_a_gap_hides_a_step_from_the_jump_test(self):
        # An outage of more than ADJACENT_DAYS can hide a real multi-day move,
        # so a big step across it is not called a bad print.
        a = _sessions(3)
        b = "2026-04-27"
        c = "2026-04-28"
        t = _table(a + [b, c], [100.0, 100.0, 100.0, 140.0, 140.0])
        out, dropped = clean_closes(t)
        self.assertEqual(dropped["jump"], 0)


class RegimeAsOfSessionTest(unittest.TestCase):
    """get_market_regime(data_date) must use only bars up to that session."""

    def _run(self, table, ref):
        with mock.patch.object(market_regime, "load_sheet", lambda *a, **k: table), \
                mock.patch.object(market_regime, "max_stored_date",
                                  lambda *a, **k: ref):
            return market_regime.get_market_regime(ref)

    def test_bars_after_the_session_are_ignored(self):
        days = _sessions(90)
        rising = [100.0 + i * 0.3 for i in range(90)]
        t = _table(days, rising)
        ref = days[79]
        reg = self._run(t, ref)
        self.assertTrue(reg["ok"])
        self.assertEqual(reg["as_of_date"], ref)
        self.assertTrue(reg["is_current"])

    def test_future_crash_does_not_leak_into_an_earlier_session(self):
        days = _sessions(90)
        closes = [100.0 + i * 0.3 for i in range(80)] + [100.0 + 79 * 0.3] * 10
        closes[85] = closes[85] * 0.9          # dips after the session under test
        ref = days[79]
        reg = self._run(_table(days, closes), ref)
        self.assertTrue(reg["enter_ok"])

    def test_weekend_bar_does_not_become_the_latest_bar(self):
        days = _sessions(80)
        t = _table(days + ["2026-09-20"], [100.0 + i * 0.3 for i in range(80)] + [95.0])
        reg = self._run(t, days[-1])
        self.assertEqual(reg["as_of_date"], days[-1])
        self.assertGreaterEqual(reg.get("bars_dropped", 0), 1)

    def test_unreadable_index_fails_closed(self):
        reg = self._run(pd.DataFrame(), "2026-09-21")
        self.assertFalse(reg["ok"])
        self.assertFalse(reg["enter_ok"])
        self.assertFalse(reg["risk_on"])

    def test_banner_text_comes_from_the_json_file(self):
        reg = self._run(pd.DataFrame(), "2026-09-21")
        self.assertTrue(reg["text"])


if __name__ == "__main__":
    unittest.main()
