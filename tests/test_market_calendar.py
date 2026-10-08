"""scanner.market_calendar: future sessions from the TWSE holiday schedule.

Never touches the network: _fetch_year is patched everywhere and the
committed list is redirected to a temp file or the recorded fixture."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scanner import market_calendar as mc

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "twse_holiday_schedule_2026.json"


def _payload():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


class _Case(unittest.TestCase):
    def setUp(self):
        mc.clear_cache()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(mc.clear_cache)
        self.file = Path(self.tmp) / "twse_holidays.json"
        p = mock.patch.object(mc, "HOLIDAY_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

        def fetch(year):
            self.calls.append(year)
            return _payload()
        p = mock.patch.object(mc, "_fetch_year", side_effect=fetch)
        self.fetch = p.start()
        self.addCleanup(p.stop)


class TestParse(_Case):
    def test_fixture_parses_to_the_2026_closures(self):
        closed = mc.parse_schedule(_payload(), 2026)
        for d in ("2026-01-01", "2026-02-12", "2026-02-13", "2026-02-16",
                  "2026-04-03", "2026-05-01", "2026-06-19", "2026-09-25",
                  "2026-09-28", "2026-10-09", "2026-10-26", "2026-12-25"):
            self.assertIn(d, closed)
        # first / last trading days are listed but are sessions
        for d in ("2026-01-02", "2026-02-11", "2026-02-23"):
            self.assertNotIn(d, closed)

    def test_a_reply_for_another_year_is_unknown(self):
        # TWSE answers an unpublished year with the current year's table
        self.assertIsNone(mc.parse_schedule(_payload(), 2027))

    def test_bad_replies_are_unknown(self):
        self.assertIsNone(mc.parse_schedule(None, 2026))
        self.assertIsNone(mc.parse_schedule({"stat": "error"}, 2026))
        self.assertIsNone(mc.parse_schedule({"stat": "ok", "data": "x"}, 2026))


class TestIsSession(_Case):
    """T3: is_session had no test; both its callers (the revenue deadline roll
    and the OTC month-end) swallow every exception, so replacing it with an
    always-open stub left the whole suite green."""

    def test_a_listed_holiday_is_closed(self):
        self.assertEqual(mc.is_session("2026-10-09"), (False, "twse"))
        self.assertEqual(mc.is_session("2026-09-28"), (False, "twse"))

    def test_a_normal_weekday_is_a_session(self):
        self.assertEqual(mc.is_session("2026-10-12"), (True, "twse"))
        self.assertEqual(mc.is_session("2026-10-08"), (True, "twse"))

    def test_a_weekend_is_closed_without_asking_the_calendar(self):
        self.assertEqual(mc.is_session("2026-10-10"), (False, "weekday"))
        self.assertEqual(mc.is_session("2026-10-11"), (False, "weekday"))
        self.assertEqual(self.calls, [])

    def test_an_unknown_year_is_none(self):
        self.assertEqual(mc.is_session("2027-03-02", fetch=False),
                         (None, "weekday"))
        self.fetch.side_effect = OSError("offline")
        self.assertEqual(mc.is_session("2027-03-02"), (None, "weekday"))
        self.fetch.assert_called_with(2027)

    def test_a_no_fetch_miss_does_not_pin_the_year_as_unknown(self):
        self.assertIsNone(mc.closed_dates(2027, fetch=False))
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(mc.closed_dates(2026, fetch=True))   # asks
        self.assertEqual(self.calls, [2026])
        self.assertEqual(mc.is_session("2026-10-09", fetch=False), (False, "twse"))

    def test_a_date_object_is_accepted(self):
        from datetime import date
        self.assertEqual(mc.is_session(date(2026, 10, 9)), (False, "twse"))
        self.assertEqual(mc.is_session(date(2026, 10, 12)), (True, "twse"))

    def test_the_committed_list_decides_without_the_network(self):
        self.file.write_text(json.dumps(
            {"years": {"2026": {"closed": ["2026-11-10"]}}}), encoding="utf-8")
        self.assertEqual(mc.is_session("2026-11-10", fetch=False), (False, "twse"))
        self.assertEqual(mc.is_session("2026-11-11", fetch=False), (True, "twse"))
        self.assertEqual(self.calls, [])


class TestNextSession(_Case):
    def test_holiday_aware(self):
        self.assertEqual(mc.next_session("2026-10-07"), ("2026-10-08", "twse"))
        self.assertEqual(mc.next_session("2026-10-08")[0], "2026-10-12")
        self.assertEqual(mc.next_session("2026-10-23")[0], "2026-10-27")
        self.assertEqual(mc.next_session("2026-09-24")[0], "2026-09-29")
        self.assertEqual(mc.next_session("2026-02-10")[0], "2026-02-11")
        self.assertEqual(mc.next_session("2026-02-11")[0], "2026-02-23")

    def test_network_reply_is_cached(self):
        mc.next_session("2026-10-07")
        mc.next_session("2026-10-08")
        self.assertEqual(self.calls, [2026])

    def test_committed_file_wins_and_skips_the_network(self):
        self.file.write_text(json.dumps(
            {"years": {"2026": {"closed": ["2026-10-09"]}}}), encoding="utf-8")
        self.assertEqual(mc.next_session("2026-10-08"), ("2026-10-12", "twse"))
        self.assertEqual(self.calls, [])

    def test_weekday_fallback_when_nothing_knows_the_year(self):
        self.fetch.side_effect = OSError("offline")
        self.assertEqual(mc.next_session("2026-10-08"), ("2026-10-09", "weekday"))
        mc.clear_cache()
        self.assertEqual(mc.next_session("2026-10-09"), ("2026-10-12", "weekday"))

    def test_unpublished_next_year_falls_back(self):
        # 2026-12-31 (Thu) -> 2027-01-01 is a weekday of an unknown year
        self.assertEqual(mc.next_session("2026-12-31"), ("2027-01-01", "weekday"))

    def test_no_fetch_mode_never_calls_out(self):
        self.assertEqual(mc.next_session("2026-10-08", fetch=False),
                         ("2026-10-09", "weekday"))
        self.assertEqual(self.calls, [])


class TestEntrySessionAfter(_Case):
    CAL = ["2026-09-23", "2026-09-24", "2026-09-29", "2026-09-30"]

    def test_history_uses_the_price_calendar(self):
        self.assertEqual(mc.entry_session_after("2026-09-24", self.CAL), "2026-09-29")
        self.assertEqual(mc.entry_session_after("2026-09-23", self.CAL), "2026-09-24")
        self.assertEqual(self.calls, [])

    def test_future_uses_the_schedule(self):
        self.assertEqual(mc.entry_session_after("2026-09-30", self.CAL), "2026-10-01")
        self.assertEqual(mc.entry_session_after("2026-10-08", self.CAL), "2026-10-12")

    def test_empty_day(self):
        self.assertIsNone(mc.entry_session_after("", self.CAL))


class TestRefresh(_Case):
    def test_refresh_writes_only_years_the_reply_covers(self):
        got = mc.refresh([2026, 2027], path=self.file)
        self.assertEqual(got[2027], None)
        self.assertEqual(got[2026], 24)
        doc = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(sorted(doc["years"]), ["2026"])
        self.assertIn("2026-10-09", doc["years"]["2026"]["closed"])


class TestCommittedList(unittest.TestCase):
    def test_committed_list_matches_the_fixture(self):
        with open(mc.HOLIDAY_FILE, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(sorted(doc["years"]["2026"]["closed"]),
                         sorted(mc.parse_schedule(_payload(), 2026)))


if __name__ == "__main__":
    unittest.main()
