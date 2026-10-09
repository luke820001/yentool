"""
Fail-closed pins from the 2026-10-09 conformance audit (D6A-01).

An events answer the parser cannot read is not "no events". An empty or
renamed-key ex-date answer used to be filed as healthy and wipe the cached
block (so an ex-day name lost its reduced reference price in limit_flags);
a renamed announcement key silently stopped the news conference dates.
ASCII only.

    python -m unittest tests.test_failclosed_events_20261009 -v
"""
import json
import unittest

import ingestion.company_events as ce
from tests.test_company_events import CacheCase, FakeFetch, TODAY, _json


def _renamed(rows, old, new):
    out = []
    for r in rows:
        r = dict(r)
        r[new] = r.pop(old)
        out.append(r)
    return out


class ExDateAnswersAreChecked(CacheCase):
    FIRST, NEXT = "2026-10-05", "2026-10-06"       # the fixture's last ex-date is 10-07

    def _first(self):
        ev, _ = self.refresh(today=self.FIRST)
        self.assertTrue(ev["exdiv"]["OTC"])               # control: a real block
        return ev

    def test_empty_answer_does_not_wipe_upcoming_ex_dates(self):
        first = self._first()
        before = json.dumps(first["exdiv"]["OTC"], sort_keys=True)
        # the next day: an upcoming date is still in the block
        self.assertTrue(any(e["date"] >= self.NEXT
                            for v in first["exdiv"]["OTC"].values() for e in v))
        fetch = FakeFetch(over={ce.EXDIV_URLS["OTC"]: []})
        ev, _ = self.refresh(fetch, today=self.NEXT)
        self.assertEqual(json.dumps(ev["exdiv"]["OTC"], sort_keys=True), before)
        h = ev["sources"]["exdiv_otc"]
        self.assertFalse(h["ok"])
        self.assertIn("upcoming", h["error"])

    def test_renamed_key_is_a_failed_source_and_keeps_the_block(self):
        first = self._first()
        before = json.dumps(first["exdiv"]["OTC"], sort_keys=True)
        rows = _renamed(_json("tpex_exdiv.json"),
                        "ExRrightsExDividendDate", "ExRightsDate")
        ev, _ = self.refresh(FakeFetch(over={ce.EXDIV_URLS["OTC"]: rows}),
                             today=self.NEXT)
        self.assertEqual(json.dumps(ev["exdiv"]["OTC"], sort_keys=True), before)
        h = ev["sources"]["exdiv_otc"]
        self.assertFalse(h["ok"])
        self.assertIn("keys", h["error"])

    def test_a_quiet_window_is_still_accepted(self):
        # nothing cached ahead of today: an empty answer is a real quiet day
        ev, _ = self.refresh(FakeFetch(over={ce.EXDIV_URLS["OTC"]: []}))
        self.assertTrue(ev["sources"]["exdiv_otc"]["ok"])
        self.assertEqual(ev["exdiv"]["OTC"], {})

    def test_rows_with_only_unrecognised_kinds_are_accepted(self):
        rows = _json("tpex_exdiv.json")
        for r in rows:
            r["ExRrightsExDividend"] = "other"
        ev, _ = self.refresh(FakeFetch(over={ce.EXDIV_URLS["OTC"]: rows}))
        self.assertTrue(ev["sources"]["exdiv_otc"]["ok"])    # keys present

    def test_the_dead_row_floor_constant_is_gone(self):
        self.assertFalse(hasattr(ce, "EXDIV_MIN_ROWS"))


class NewsAnswersAreChecked(CacheCase):
    def test_renamed_clause_key_fails_the_source(self):
        rows = _renamed(_json("tpex_news.json"), ce.K_CLAUSE, "clause")
        ev, _ = self.refresh(FakeFetch(over={ce.NEWS_URLS["OTC"]: rows}))
        h = ev["sources"]["news_otc"]
        self.assertFalse(h["ok"])
        self.assertIn("keys", h["error"])

    def test_control_the_fixture_is_healthy(self):
        ev, _ = self.refresh()
        self.assertTrue(ev["sources"]["news_otc"]["ok"])

    def test_an_empty_news_list_is_accepted(self):
        ev, _ = self.refresh(FakeFetch(over={ce.NEWS_URLS["OTC"]: []}))
        self.assertTrue(ev["sources"]["news_otc"]["ok"])


if __name__ == "__main__":
    unittest.main()
