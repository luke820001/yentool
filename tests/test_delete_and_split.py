"""
Two defects the owner reported on 2026-09-22, each pinned by what goes wrong.

1. A record registered and then cancelled could not be removed. Voiding the
   only buy left the position at status "open" with zero shares and zero
   executions: it stayed on 持倉 claiming 持有中, nagged from 待處理 every day,
   and the only exits were 封存 or 撤銷, each of which just moves the ghost to
   a different permanent table.

2. The column self-check warnings were pointing at a real defect, not a tight
   bound. 6949 stepped 1,490.00 -> 67.10 across a seven-session gap with volume
   up a hundred-fold, which is an un-adjusted split. The row published MA20
   511.15 and MA60 791.80 against a close of 50.80, told the owner the stock
   was down 95.3% in a month, and still carried Integrity_OK = True.

    python -m unittest tests.test_delete_and_split -v
"""
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "mobile" / "app.js"


def app():
    return APP.read_text(encoding="utf-8")


def between(src, start, end):
    i = src.find(start)
    assert i > 0, "anchor not found: %s" % start
    j = src.find(end, i + 1)
    return src[i:j if j > 0 else len(src)]


class ARecordCanBeRemoved(unittest.TestCase):
    """Reported twice: it cannot be removed from 已平倉／封存, and it should
    also be removable straight from 持倉."""

    def test_the_action_exists(self):
        src = app()
        self.assertIn('act === "pos-delete"', src)
        self.assertIn("async function deletePosition(", src)

    def test_the_button_is_on_both_screens(self):
        src = app()
        cards = between(src, "function positionCard(", "function renderPerf()")
        self.assertIn('btn("pos-delete"', cards,
                      "no way to remove a record from 持倉")
        perf = between(src, "function renderPerf()", "function openCycleDetail")
        self.assertIn('btn("pos-delete"', perf,
                      "no way to remove a record from 績效與歷史")

    def test_it_removes_everything_keyed_to_the_position(self):
        """Leftovers become orphans that portfolioSummary and latestMark keep
        reading."""
        body = between(app(), "async function deletePosition(",
                       "function deleteCost(")
        for store in ("positions", "executions", "marks", "meta"):
            self.assertIn('"%s"' % store, body,
                          "deletePosition leaves %s behind" % store)
        self.assertIn("cycle:", body, "the frozen result is not removed")

    def test_it_is_one_transaction(self):
        body = between(app(), "async function deletePosition(",
                       "function deleteCost(")
        self.assertEqual(body.count("db.transaction("), 1,
                         "a partial delete would leave the record in pieces")

    def test_the_confirm_says_what_is_destroyed(self):
        """From here nothing is recoverable, so the owner has to be told what
        goes, and pointed at the non-destructive alternative."""
        body = between(app(), 'act === "pos-delete"', 'act === "void-pos"')
        self.assertIn("deleteCost(", body)
        self.assertIn("cost.live", body, "the fill count is not named")
        self.assertIn("cost.realized", body, "the realised P&L is not named")
        self.assertIn("無法復原", body)
        self.assertIn("封存", body,
                      "the confirm must point at 封存 as the way to keep it")

    def test_an_empty_record_gets_the_shorter_warning(self):
        body = between(app(), 'act === "pos-delete"', 'act === "void-pos"')
        self.assertIn("cost.empty", body,
                      "a record with nothing in it should not be warned about "
                      "destroying P&L it never had")


class ASplitIsADataErrorNotAPriceMove(unittest.TestCase):
    """Taiwan limits a day to +-10%, and even a no-limit first listing cannot
    triple. A step beyond that means the series carries two price bases."""

    def series(self, closes):
        dates = ["2026-08-%02d" % (20 + i) if i < 5 else "2026-09-%02d" % (i - 4)
                 for i in range(len(closes))]
        return pd.DataFrame([
            {"date": d, "open": c, "high": c, "low": c, "close": c,
             "Volume_Lot": 100.0} for d, c in zip(dates, closes)])

    def test_the_6949_shape_is_caught_and_distrusted(self):
        from scanner.data_integrity import audit_series
        ig = audit_series(self.series([1235.0, 1355.0, 1490.0, 67.1, 60.4, 54.9]))
        self.assertEqual(ig["splits"], 1)
        self.assertIn("split:1", ig["flags"])
        self.assertFalse(ig["trustworthy"],
                         "a series in two different units was published as "
                         "trustworthy")

    def test_a_violent_but_legal_series_is_still_trusted(self):
        from scanner.data_integrity import audit_series
        ig = audit_series(self.series([100.0, 109.5, 120.0, 108.0, 118.0]))
        self.assertEqual(ig["splits"], 0)
        self.assertTrue(ig["trustworthy"])

    def test_the_boundary_is_where_the_new_unit_starts(self):
        from scanner.data_integrity import split_start
        self.assertEqual(
            split_start(pd.DataFrame({"close": [1235.0, 1355.0, 1490.0, 67.1, 60.4]})), 3)
        self.assertEqual(split_start(pd.DataFrame({"close": [10, 11, 10.5, 11.2]})), 0)

    def test_the_scan_stops_averaging_across_the_boundary(self):
        src = (ROOT / "scanner" / "chip_verifier.py").read_text(encoding="utf-8")
        i = src.find("merged.reset_index(drop=True, inplace=True)")
        j = src.find("Max_Price_20", i)
        self.assertGreater(j, i)
        window = src[i:j]
        self.assertIn("split_start", window,
                      "the truncation must happen BEFORE any rolling figure "
                      "is computed, or MA20 spans two price bases")

    def test_it_is_registered_as_a_hard_flag(self):
        from scanner.result_checks import _HARD_FLAGS
        self.assertIn("split:", _HARD_FLAGS,
                      "a split flag on a row claiming Integrity_OK True would "
                      "pass the payload check unnoticed")


if __name__ == "__main__":
    unittest.main()
