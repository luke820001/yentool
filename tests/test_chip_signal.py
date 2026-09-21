"""
Tests for the chip verdict (scanner/chip_signal.py).

    python -m unittest tests.test_chip_signal -v
"""
import unittest
from unittest import mock

import pandas as pd

from scanner import chip_signal as cs


def frame(**over):
    base = dict(Stock_ID="1815", Data_Date="2026-09-21", Inst_Date="2026-09-21",
                Inst_Net=-300.0, Inst_Streak=-2, Inst_Sessions=5, Vol_MA20=10000.0,
                Hold_Status="holding", Hold_Day=3, Exit_Signal="")
    base.update(over)
    return pd.DataFrame([base])


ENABLED = {
    "sell": {"enabled": True, "inst_pct_max": -2.0, "streak_min": None},
    "add": {"enabled": True, "inst_pct_min": 2.0, "streak_min": None, "max_hold_day": 5},
}
DISABLED = {
    "sell": {"enabled": False, "inst_pct_max": -2.0, "streak_min": None},
    "add": {"enabled": False, "inst_pct_min": 2.0, "streak_min": None, "max_hold_day": 5},
}


class Readout(unittest.TestCase):
    def test_pct_is_share_of_average_volume(self):
        self.assertEqual(cs.inst_pct(-300.0, 10000.0), -3.0)
        self.assertIsNone(cs.inst_pct(-300.0, 0))
        self.assertIsNone(cs.inst_pct(None, 10000.0))

    def test_basis_flags_a_lagging_feed(self):
        self.assertEqual(cs.chip_basis("2026-09-21", "2026-09-21"), "current")
        self.assertEqual(cs.chip_basis("2026-09-18", "2026-09-21"), "lag")
        self.assertEqual(cs.chip_basis(None, "2026-09-21"), "")

    def test_columns_always_present(self):
        out = cs.annotate_chip_action(frame(), "mode_prelaunch")
        for c in ("Inst_Pct", "Chip_Basis", "Chip_Action", "Chip_Note"):
            self.assertIn(c, out.columns)
        self.assertEqual(out["Inst_Pct"].iloc[0], -3.0)
        self.assertEqual(out["Chip_Basis"].iloc[0], "current")


class Verdict(unittest.TestCase):
    def test_no_validated_rule_means_no_action(self):
        with mock.patch.object(cs, "CHIP_RULES", DISABLED):
            out = cs.annotate_chip_action(frame(), "mode_prelaunch")
        self.assertEqual(out["Chip_Action"].iloc[0], "")
        self.assertIn("confirmation only", out["Chip_Note"].iloc[0])

    def test_sell_when_institutions_dump(self):
        with mock.patch.object(cs, "CHIP_RULES", ENABLED):
            out = cs.annotate_chip_action(frame(), "mode_prelaunch")
        self.assertEqual(out["Chip_Action"].iloc[0], "sell")
        self.assertIn("next open", out["Chip_Note"].iloc[0])

    def test_add_only_inside_the_add_window(self):
        with mock.patch.object(cs, "CHIP_RULES", ENABLED):
            early = cs.annotate_chip_action(frame(Inst_Net=500.0, Inst_Streak=1, Hold_Day=2),
                                            "mode_prelaunch")
            late = cs.annotate_chip_action(frame(Inst_Net=500.0, Inst_Streak=1, Hold_Day=8),
                                           "mode_prelaunch")
        self.assertEqual(early["Chip_Action"].iloc[0], "add")
        self.assertEqual(late["Chip_Action"].iloc[0], "hold")

    def test_lagging_flow_gives_no_verdict(self):
        with mock.patch.object(cs, "CHIP_RULES", ENABLED):
            out = cs.annotate_chip_action(frame(Inst_Date="2026-09-18"), "mode_prelaunch")
        self.assertEqual(out["Chip_Action"].iloc[0], "")
        self.assertEqual(out["Chip_Basis"].iloc[0], "lag")
        self.assertIn("no verdict", out["Chip_Note"].iloc[0])

    def test_pending_and_exited_rows_get_no_action(self):
        with mock.patch.object(cs, "CHIP_RULES", ENABLED):
            pending = cs.annotate_chip_action(frame(Hold_Status="pending"), "mode_prelaunch")
            exited = cs.annotate_chip_action(frame(Exit_Signal="stop"), "mode_prelaunch")
        self.assertEqual(pending["Chip_Action"].iloc[0], "")
        self.assertEqual(exited["Chip_Action"].iloc[0], "")

    def test_other_modes_are_readout_only(self):
        with mock.patch.object(cs, "CHIP_RULES", ENABLED):
            out = cs.annotate_chip_action(frame(), "mode_bottom")
        self.assertEqual(out["Chip_Action"].iloc[0], "")

    def test_missing_columns_do_not_raise(self):
        df = pd.DataFrame([{"Stock_ID": "1815"}])
        out = cs.annotate_chip_action(df, "mode_prelaunch")
        self.assertEqual(out["Chip_Action"].iloc[0], "")
        self.assertEqual(out["Chip_Note"].iloc[0], "no institutional data")


if __name__ == "__main__":
    unittest.main()
