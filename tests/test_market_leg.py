"""
Tests for the market leg of the ride (scanner/market_leg.py) and the
`extend_if` hook it plugs into (scanner/exit_rules.replay_exit), 2026-10-08.

The hook touches the shared exit engine every caller scores with, so the
default path is pinned bit for bit: a fingerprint of 1,200 replays over
synthetic series, taken before the hook existed, must not move.

Stdlib unittest, no network; the index history is a temporary sqlite file.

    python -m unittest tests.test_market_leg -v
"""
import hashlib
import json
import os
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scanner import market_leg
from scanner.exit_rules import replay_exit

# replay_exit's output over synthetic_series(seed, 3 + seed % 24) for seeds
# 0..239 and five (hold_bars, ride_cap) settings, taken on the engine BEFORE
# extend_if existed (HEAD 87c788c). Any change to the default path moves it.
GOLDEN = ("71f88877faf7dba31e5876dfbf0ad86f317516be562293c007baffe0eac74d02", 1200)


def synthetic_series(seed, n):
    rng = random.Random(seed)
    o, h, l, c = [], [], [], []
    px = 100.0
    for _ in range(n):
        op = round(px * (1 + rng.uniform(-0.03, 0.03)), 2)
        cl = round(op * (1 + rng.uniform(-0.06, 0.06)), 2)
        hi = round(max(op, cl) * (1 + rng.uniform(0, 0.04)), 2)
        lo = round(min(op, cl) * (1 - rng.uniform(0, 0.04)), 2)
        if rng.random() < 0.03:
            hi = float("nan")
        o.append(op)
        h.append(hi)
        l.append(lo)
        c.append(cl)
        px = cl
    return o, h, l, c


def fingerprint(**extra):
    out = []
    for seed in range(240):
        n = 3 + seed % 24
        o, h, l, c = synthetic_series(seed, n)
        dates = ["d%02d" % i for i in range(n)]
        for hold, cap in ((None, Ellipsis), (10, Ellipsis), (10, None),
                          (5, 12), (10, 20)):
            p = replay_exit(o, h, l, c, dates=dates, hold_bars=hold,
                            ride_cap=cap, **extra)
            out.append({k: (round(v, 6) if isinstance(v, float) else v)
                        for k, v in sorted(p.items())})
    blob = json.dumps(out, sort_keys=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest(), len(out)


# A trade whose day-10 close (bar 9) sits under its own 5-bar mean: the stock
# leg alone books the time exit there. Bars 10-11 recover above the mean.
FLAT = (100, 101, 99, 100)
WEAK = [FLAT] * 9 + [(100, 100.5, 96.5, 97)]
RECOVER = WEAK + [(97, 98, 96, 96.5), (96.5, 101, 96, 100.5), (100.5, 100.8, 99, 100.6)]


def cols(rows):
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


class DefaultPathUnchanged(unittest.TestCase):
    def test_fingerprint_without_the_hook(self):
        self.assertEqual(fingerprint(), GOLDEN)

    def test_a_hook_that_never_extends_changes_nothing(self):
        self.assertEqual(fingerprint(extend_if=lambda i, d: False), GOLDEN)

    def test_a_failing_hook_reads_as_no(self):
        def boom(i, d):
            raise RuntimeError("index feed down")
        self.assertEqual(fingerprint(extend_if=boom), GOLDEN)


class ExtendIf(unittest.TestCase):
    def test_the_stock_leg_alone_exits_at_the_time_bar(self):
        p = replay_exit(*cols(RECOVER), dates=list(range(13)), hold_bars=10,
                        ride_cap=20)
        self.assertEqual((p["reason"], p["bar"]), ("time", 9))

    def test_a_disturbed_day_keeps_the_position(self):
        # disturbed on bar 9 only: ride; bar 10 is weak and not disturbed: out
        p = replay_exit(*cols(RECOVER), dates=list(range(13)), hold_bars=10,
                        ride_cap=20, extend_if=lambda i, d: d == 9)
        self.assertEqual((p["reason"], p["bar"]), ("time", 10))
        self.assertAlmostEqual(p["exit_price"], 96.5)

    def test_then_the_stock_leg_takes_over_until_the_window_ends(self):
        # disturbed on bars 9 and 10, then bars 11-12 close above their mean:
        # the window runs out mid-ride, which is an OPEN trade
        p = replay_exit(*cols(RECOVER), dates=list(range(13)), hold_bars=10,
                        ride_cap=20, extend_if=lambda i, d: d in (9, 10))
        self.assertFalse(p["exited"])
        self.assertTrue(p["riding"])
        self.assertEqual(p["reason"], "")

    def test_the_cap_still_binds(self):
        rows = WEAK + [(97, 97.5, 96.5, 97)] * 15
        p = replay_exit(*cols(rows), dates=list(range(len(rows))), hold_bars=10,
                        ride_cap=20, extend_if=lambda i, d: True)
        self.assertEqual((p["reason"], p["bar"]), ("time", 19))

    def test_asked_only_where_the_time_exit_would_fire(self):
        seen = []

        def spy(i, d):
            seen.append((i, d))
            return False
        dates = ["x%02d" % i for i in range(13)]
        replay_exit(*cols(RECOVER), dates=dates, hold_bars=10, ride_cap=20,
                    extend_if=spy)
        self.assertEqual(seen, [(9, "x09")])

    def test_ignored_without_a_ride(self):
        for cap in (None, 10):
            p = replay_exit(*cols(RECOVER), dates=list(range(13)), hold_bars=10,
                            ride_cap=cap, extend_if=lambda i, d: True)
            self.assertEqual((p["reason"], p["bar"]), ("time", 9))


def taiex_db(path, closes, start="2026-01-01"):
    from datetime import date, timedelta
    d = date.fromisoformat(start)
    rows = []
    for c in closes:
        rows.append((d.isoformat(), c))
        d += timedelta(days=1)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS "TAIEX" ("date" TEXT, "close" REAL)')
        conn.executemany("INSERT INTO TAIEX VALUES (?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return [r[0] for r in rows]


class MarketLeg(unittest.TestCase):
    def setUp(self):
        market_leg.clear_cache()
        self.addCleanup(market_leg.clear_cache)

    def _series(self):
        # 70 days of a steady uptrend, then a 3-day pullback that stays above
        # the 60-day mean, then a fall through it
        up = [100.0 + i for i in range(70)]
        return up + [155.0, 153.0, 152.0] + [100.0, 95.0]

    def test_pullback_inside_an_uptrend_is_disturbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "taiex.db"
            dates = taiex_db(db, self._series())
            table = market_leg.disturbed_by_date(db)
            self.assertFalse(table[dates[69]])          # at the high
            for d in dates[70:73]:                       # below 20MA, above 60MA
                self.assertTrue(table[d], d)
            self.assertFalse(table[dates[74]])          # broke the 60MA: a bear
            self.assertTrue(market_leg.is_disturbed(dates[71], db))
            fn = market_leg.make_disturbed_fn(db)
            self.assertTrue(fn(9, dates[71]))
            self.assertFalse(fn(9, None))
            self.assertIs(fn.table, table)

    def test_short_history_and_missing_dates_are_not_disturbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "taiex.db"
            dates = taiex_db(db, [100.0 + i for i in range(40)] + [90.0])
            table = market_leg.disturbed_by_date(db)
            self.assertFalse(any(table.values()))       # never 60 bars
            self.assertFalse(market_leg.is_disturbed("2030-01-01", db))
            self.assertFalse(market_leg.is_disturbed(dates[-1], db))

    def test_a_missing_file_is_never_disturbed(self):
        missing = Path(tempfile.gettempdir()) / "no_such_taiex_20261008.db"
        self.assertEqual(market_leg.disturbed_by_date(missing), {})
        self.assertFalse(market_leg.make_disturbed_fn(missing)(0, "2026-10-07"))

    def test_the_cache_follows_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "taiex.db"
            series = self._series()
            dates = taiex_db(db, series[:70])
            first = market_leg.disturbed_by_date(db)
            self.assertIs(market_leg.disturbed_by_date(db), first)   # cached
            self.assertNotIn("2026-03-12", first)
            later = time.time() + 5
            taiex_db(db, series[70:73], start="2026-03-12")
            os.utime(db, (later, later))
            second = market_leg.disturbed_by_date(db)
            self.assertIsNot(second, first)
            self.assertTrue(second["2026-03-12"])

    def test_the_default_file_is_the_configured_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "taiex.db"
            dates = taiex_db(db, self._series())
            import config.settings as settings
            with mock.patch.object(settings, "TAIEX_FILE", db):
                self.assertTrue(market_leg.is_disturbed(dates[71]))
