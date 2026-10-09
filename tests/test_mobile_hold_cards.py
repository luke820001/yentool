"""
What the phone prints on a simulated hold card, pinned to what the BACKEND writes.

Audit D5-04 / D5-05 (2026-10-09): the desktop says "day N of 10, R trading days
left, exit date ..." and the phone did not; and the late profit-take (a close
at or above the fill +1% from day 8 sells at the next open) reaches the payload
only as the free text of Exit_Note, which neither screen read. The phone now
shows both (mobile/app.js holdClock / lateDueNow). Reading a sentence is only
safe while a test holds the phone's pattern to the sentence the backend really
writes, which is what this does -- it drives the real tracker for a late-due
row and applies the app's own regular expression to the real Exit_Note.

    python -m unittest tests.test_mobile_hold_cards -v
"""
import re
import unittest
from pathlib import Path

from scanner import result_checks as rc
from tests.test_exit_plan import _TrackerHarness

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "mobile" / "app.js").read_text(encoding="utf-8")


def app_late_pattern():
    m = re.search(r"const LATE_DUE_NOTE = /(.+)/;", APP)
    assert m, "mobile/app.js has no LATE_DUE_NOTE"
    return re.compile(m.group(1))


class TheLateTakeSentence(_TrackerHarness, unittest.TestCase):
    def _row(self, rows, today):
        out = self._run({"7777": rows}, {"7777": [self.CAL[0]]}, today=today)
        return out.iloc[0]

    def test_the_phone_pattern_matches_the_real_late_due_note(self):
        # anchored on CAL[0] the fill is CAL[1]'s open (day 1); CAL[8] is day 8
        rows = [(100, 101, 99, 100)] + [(100, 101.6, 99.9, 101.5)] * 8
        r = self._row(rows, self.CAL[8])
        self.assertEqual(r["Hold_Day"], 8)
        self.assertEqual(r["Hold_Status"], "holding")
        self.assertEqual(r["Exit_Signal"], "")
        self.assertRegex(r["Exit_Note"], app_late_pattern())

    def test_it_does_not_match_the_other_notes(self):
        pat = app_late_pattern()
        # day 8 but under water: the plain stop note
        rows = [(100, 101, 99, 100)] + [(100, 100.4, 99.0, 99.5)] * 8
        r = self._row(rows, self.CAL[8])
        self.assertIsNone(pat.search(r["Exit_Note"]), r["Exit_Note"])
        # day 8 in profit but under +1%: still the plain stop note
        rows = [(100, 101, 99, 100)] + [(100, 100.9, 99.9, 100.8)] * 8
        r = self._row(rows, self.CAL[8])
        self.assertIsNone(pat.search(r["Exit_Note"]), r["Exit_Note"])
        # day 7 with a +1.5% close: the rung does not exist yet
        rows = [(100, 101, 99, 100)] + [(100, 101.6, 99.9, 101.5)] * 7
        r = self._row(rows, self.CAL[7])
        self.assertIsNone(pat.search(r["Exit_Note"]), r["Exit_Note"])
        # an armed lock whose close is under the late line
        rows = [(100, 101, 99, 100)] * 1 + [(100, 103, 99.9, 102.6)] + [(102.6, 102.9, 102.1, 102.6)] * 4
        r = self._row(rows, self.CAL[5])
        self.assertIn("lock armed", r["Exit_Note"])
        self.assertIsNone(pat.search(r["Exit_Note"]), r["Exit_Note"])

    def test_the_app_only_acts_on_it_for_a_live_hold(self):
        i = APP.index("function lateDueNow(r) {")
        body = APP[i:APP.index("\n}\n", i)]
        self.assertIn('"holding"', body)
        self.assertIn('"delay"', body)
        self.assertIn("Exit_Signal", body)
        self.assertNotIn("exit_today", body)


class TheHoldClock(unittest.TestCase):
    def test_the_phone_guard_is_the_registry_identity(self):
        i = APP.index("function holdClock(r) {")
        body = APP[i:APP.index("\n}\n", i)]
        self.assertIn("day + rem !== total", body)       # Hold_Day + Hold_Remaining == Hold_Total
        self.assertIn("Number.isInteger", body)
        # the same identity result_checks enforces on the payload
        self.assertIn("Hold_Day + Hold_Remaining == Hold_Total", Path(rc.__file__).read_text(encoding="utf-8"))

    def test_the_clock_columns_exist_and_are_display_only(self):
        for col in ("Hold_Day", "Hold_Remaining", "Exit_Date", "Exit_Note"):
            self.assertIn(col, rc.COLUMNS)
            self.assertFalse(rc.COLUMNS[col]["phone"], col)
        for col in ("Hold_Total", "Hold_Cap"):
            self.assertTrue(rc.COLUMNS[col]["phone"], col)


if __name__ == "__main__":
    unittest.main()
