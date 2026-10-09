"""Pins for the phone's copy of the settled rule (mobile/app.js). ASCII only.

The numbers on the order card, the quote ladder, the buy verdict, the sizing
and the auto-refresh clock are re-implemented in JavaScript. tests/pin_phone_probe.js
boots the whole app in a node vm context and compares them to the settled
values as literals; this module runs it. Skipped when node is not installed,
so a Python-only environment still runs the rest of the suite. No network.

The static half (no node needed) checks that the phone and the backend quote
the same strategy numbers in text the owner reads.

    python -m unittest tests.test_pin_phone -v
"""
import re
import shutil
import subprocess
import unittest

from tests.pin_support import ROOT, read_text

PROBE = ROOT / "tests" / "pin_phone_probe.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class PhoneProbe(unittest.TestCase):
    def test_the_phone_copy_of_the_rule_matches_the_settled_numbers(self):
        proc = subprocess.run([NODE, str(PROBE)], cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(proc.returncode, 0, out[-3000:])
        self.assertRegex(out, r"PASS \d+")
        self.assertGreaterEqual(int(re.search(r"PASS (\d+)", out).group(1)), 45, out[-500:])


class PhoneMirrorsTheBackendStatically(unittest.TestCase):
    """Text-level mirror checks that need no node."""

    def setUp(self):
        self.js = read_text("mobile/app.js").replace("\r\n", "\n")

    def block(self):
        return self.js.split("const STRATEGY = {", 1)[1].split("\n};", 1)[0]

    def num(self, key):
        m = re.search(r"(?m)^\s*%s:\s*(-?[0-9.]+)\s*," % key, self.block())
        self.assertIsNotNone(m, key)
        return float(m.group(1))

    def test_the_strategy_block_equals_the_backend_rule(self):
        from scanner.exit_rules import DEFAULT_RULE as R
        self.assertEqual(self.num("stopPct"), -R["stop_pct"] * 100)
        self.assertAlmostEqual(self.num("armPct"), R["arm_pct"] * 100, places=9)
        self.assertAlmostEqual(self.num("lockPct"), R["lock_pct"] * 100, places=9)
        self.assertAlmostEqual(self.num("targetPct"), R["tp_pct"] * 100, places=9)
        self.assertEqual(self.num("lateFrom"), R["late_from"])
        self.assertAlmostEqual(self.num("lateGainPct"), R["late_gain"] * 100, places=9)
        self.assertEqual(self.num("horizon"), R["hold_bars"])
        self.assertEqual(self.num("cap"), R["ride_cap"])

    def test_the_staged_entry_numbers_equal_the_backend(self):
        from scanner import scan_mode as sm
        self.assertAlmostEqual(self.num("addPct"), -sm.PRELAUNCH_ADD_PCT * 100, places=9)
        self.assertAlmostEqual(self.num("addFirstPct"), sm.PRELAUNCH_ADD_FIRST * 100, places=9)
        self.assertAlmostEqual(self.num("scaleOutPct"), sm.PRELAUNCH_SCALE_OUT_PCT * 100, places=9)
        m = re.search(r'(?m)^\s*version:\s*"([^"]+)"', self.block())
        self.assertEqual(m.group(1), sm.STRATEGY_VERSION)

    def test_the_rank_cut_and_the_clock_equal_the_backend(self):
        from scanner import list_freeze, scan_mode as sm
        self.assertEqual(int(re.search(r"const N_ENTER_UI = (\d+);", self.js).group(1)), sm.N_ENTER)
        m = re.search(r"const EOD_READY_MIN = (\d+) \* 60;", self.js)
        self.assertEqual("%02d:00" % int(m.group(1)), list_freeze.FINAL_NOT_BEFORE)

    def test_the_sizing_defaults_and_fee_schedule_names(self):
        self.assertIn("const SIZING_DEFAULTS = { slots: 8, risk_pct: 1 };", self.js)
        self.assertIn('const DEFAULT_SCHEDULE = "tw-equity-v1";', self.js)
        from portfolio.money import FeeSchedule
        self.assertEqual(FeeSchedule.default().version, "tw-equity-v1")
        self.assertEqual(FeeSchedule.exact().version, "tw-equity-exact")


if __name__ == "__main__":
    unittest.main()
