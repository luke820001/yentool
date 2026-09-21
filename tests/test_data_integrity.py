"""
Tests for the placeholder-bar guard (scanner/data_integrity.py).

2026-09-20: yfinance returned a bar dated on a Sunday for 22 of ~1930 stocks,
copying Friday's close with a small non-zero volume and, on some names, a
high/low that was the next session's limit band. It became a "session": hold
days advanced, one row got a Sunday Entry_Date, every quote read as a gap, and
a fabricated low can book a stop that never happened.

The rule under test is "either the market traded or it did not": a recent date
only a small fraction of the store has bars for is not a session. Old sparse
dates (a few names backfilled long ago) must survive.

    python -m unittest tests.test_data_integrity -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scanner.data_integrity import (
    drop_nonsession_rows, nonsession_dates, purge_nonsession_bars,
)

SESSIONS = ["2026-09-%02d" % d for d in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14,
                                         15, 16, 17, 18)]
FAKE = "2026-09-20"


def counts(full=1900, thin=None):
    out = [(d, full) for d in SESSIONS]
    if thin is not None:
        out.append((FAKE, thin))
    return out


class NonSessionDates(unittest.TestCase):
    def test_thin_recent_date_is_not_a_session(self):
        self.assertEqual(nonsession_dates(counts(thin=22)), [FAKE])

    def test_a_full_session_is_kept(self):
        self.assertEqual(nonsession_dates(counts(thin=1880)), [])

    def test_a_quiet_but_real_session_is_kept(self):
        # half the market reporting is a bad feed day, not a fake session
        self.assertEqual(nonsession_dates(counts(thin=950)), [])

    def test_old_sparse_dates_are_left_alone(self):
        # the store starts with a handful of backfilled names; those dates are
        # real sessions with thin coverage and must not be deleted
        old = [("2025-05-%02d" % d, 2) for d in range(12, 30)]
        rows = old + counts(thin=22)
        self.assertEqual(nonsession_dates(rows, window=len(SESSIONS) + 1), [FAKE])

    def test_too_little_history_decides_nothing(self):
        self.assertEqual(nonsession_dates([("2026-09-18", 1900)]), [])


class DropRows(unittest.TestCase):
    def frames(self, fake_ids):
        out = {}
        for i in range(20):
            sid = "%04d" % (1000 + i)
            dates = list(SESSIONS) + ([FAKE] if sid in fake_ids else [])
            out[sid] = pd.DataFrame({"date": dates, "close": [10.0] * len(dates)})
        return out

    def test_drops_only_the_fake_date(self):
        frames, dropped = drop_nonsession_rows(self.frames({"1000", "1001"}),
                                               window=len(SESSIONS) + 1)
        self.assertEqual(dropped, [FAKE])
        for sid, f in frames.items():
            self.assertNotIn(FAKE, list(f["date"]))
            self.assertEqual(len(f), len(SESSIONS))

    def test_clean_store_is_untouched(self):
        original = self.frames(set())
        frames, dropped = drop_nonsession_rows(original)
        self.assertEqual(dropped, [])
        self.assertIs(frames, original)


class PurgeStore(unittest.TestCase):
    def test_purge_removes_the_fake_session_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pv.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE data (date TEXT, stock_id TEXT, close REAL)")
                for d in SESSIONS:
                    for i in range(100):
                        conn.execute("INSERT INTO data VALUES (?,?,?)",
                                     (d, "%04d" % (1000 + i), 10.0))
                for i in range(3):
                    conn.execute("INSERT INTO data VALUES (?,?,?)",
                                 (FAKE, "%04d" % (1000 + i), 10.0))
                conn.commit()
            finally:
                conn.close()

            out = purge_nonsession_bars(db, window=len(SESSIONS) + 1)
            self.assertEqual(out["dates"], [FAKE])
            self.assertEqual(out["rows"], 3)

            conn = sqlite3.connect(db)
            try:
                left = conn.execute(
                    "SELECT COUNT(*) FROM data WHERE date = ?", (FAKE,)).fetchone()[0]
                kept = conn.execute("SELECT COUNT(*) FROM data").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(left, 0)
            self.assertEqual(kept, len(SESSIONS) * 100)

    def test_missing_store_is_reported_not_raised(self):
        out = purge_nonsession_bars(Path(tempfile.gettempdir()) / "no_such_pv.db")
        self.assertEqual(out["rows"], 0)


class ReaderGuards(unittest.TestCase):
    """A store written before the purge shipped must still not fool the two
    readers that go straight to it."""

    def _poisoned_store(self, tmp):
        db = Path(tmp) / "pv.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE data (date TEXT, stock_id TEXT, open REAL,"
                         " high REAL, low REAL, close REAL, Volume_Lot INTEGER)")
            for d in SESSIONS:
                for i in range(40):
                    conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?,?)",
                                 (d, "%04d" % (1000 + i), 10.0, 10.5, 9.5, 10.0, 500))
            # the placeholder: one stock, copying the last close, small volume
            conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?,?)",
                         (FAKE, "1000", 10.0, 11.0, 9.0, 10.0, 7))
            conn.commit()
        finally:
            conn.close()
        return db

    def test_trading_calendar_skips_it(self):
        from unittest import mock
        import scanner.holding_tracker as tracker
        with tempfile.TemporaryDirectory() as tmp:
            db = self._poisoned_store(tmp)
            with mock.patch.object(tracker, "PRICE_VOLUME_FILE", db):
                cal = tracker._trading_calendar()
        self.assertEqual(cal, SESSIONS)
        self.assertNotIn(FAKE, cal)

    def test_quote_feed_sessions_skip_it(self):
        from scanner.quote_feed import build_quote_feed
        with tempfile.TemporaryDirectory() as tmp:
            db = self._poisoned_store(tmp)
            payload = build_quote_feed(db, ["1000", "1001"], sessions=5)
        self.assertEqual(payload["as_of"], SESSIONS[-1])
        self.assertNotIn(FAKE, payload["sessions"])


if __name__ == "__main__":
    unittest.main()


class CoverageStepChange(unittest.TestCase):
    """From 2026-09-21 the scan stores the WHOLE market each day (~2,300
    names) while older dates hold only the daily shortlist. Judging every date
    against one window median would delete those genuine older sessions from
    the trading calendar -- the same damage the placeholder bars caused."""

    def test_a_thin_but_real_session_survives_a_coverage_jump(self):
        old_days = [("2026-08-%02d" % d, 300) for d in range(1, 26)]
        new_days = [("2026-09-%02d" % d, 2300) for d in range(1, 16)]
        self.assertEqual(nonsession_dates(old_days + new_days), [])

    def test_a_placeholder_is_still_caught_after_the_jump(self):
        days = [("2026-09-%02d" % d, 2300) for d in range(1, 16)]
        days.append(("2026-09-20", 22))          # the real 2026-09-20 shape
        self.assertEqual(nonsession_dates(days), ["2026-09-20"])

    def test_a_placeholder_among_thin_days_is_still_caught(self):
        days = [("2026-08-%02d" % d, 300) for d in range(1, 26)]
        days.append(("2026-08-26", 9))
        self.assertEqual(nonsession_dates(days), ["2026-08-26"])

    def test_the_boundary_date_itself_is_not_flagged(self):
        days = ([("2026-08-%02d" % d, 400) for d in range(1, 21)] +
                [("2026-09-%02d" % d, 2300) for d in range(1, 21)])
        flagged = nonsession_dates(days)
        self.assertEqual(flagged, [], "coverage step wrongly read as a holiday")


class StepChangeBoundary(unittest.TestCase):
    """The date where coverage changes level has one neighbour set from each
    regime. Comparing against the quieter side keeps it out of trouble."""

    def test_the_last_thin_day_before_the_jump_survives(self):
        days = ([("2026-09-%02d" % d, 84) for d in range(1, 11)] +
                [("2026-09-%02d" % d, 1930) for d in range(11, 21)])
        self.assertEqual(nonsession_dates(days), [])

    def test_the_first_full_day_after_a_thin_stretch_survives(self):
        days = ([("2026-09-%02d" % d, 1930) for d in range(1, 11)] +
                [("2026-09-%02d" % d, 84) for d in range(11, 21)])
        self.assertEqual(nonsession_dates(days), [])

    def test_a_placeholder_as_the_newest_date_is_still_caught(self):
        days = [("2026-09-%02d" % d, 1930) for d in range(1, 19)]
        days.append(("2026-09-20", 22))
        self.assertEqual(nonsession_dates(days), ["2026-09-20"])

    def test_a_placeholder_in_the_middle_is_still_caught(self):
        days = [("2026-09-%02d" % d, 1930) for d in range(1, 21)]
        days[10] = ("2026-09-11", 20)
        self.assertEqual(nonsession_dates(days), ["2026-09-11"])
