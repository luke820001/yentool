"""
Run tests/fold_probe.js, which exercises the ledger's pure fold helpers for
real instead of asserting on the source text.

mobile/app.js touches window, navigator and document at load, so it cannot be
imported. The probe cuts the pure functions out by brace matching and runs
them, which is the only way to check that the editable archive's share bound
computes the right NUMBER rather than merely containing the right words.

Skipped when node is not installed, so a Python-only environment still runs
the rest of the suite.

    python -m unittest tests.test_fold_probe -v
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "fold_probe.js"
NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node is not installed")
class FoldHelpersComputeTheRightNumbers(unittest.TestCase):
    def test_every_fold_check_passes(self):
        out = subprocess.run([NODE, str(PROBE)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(
            out.returncode, 0,
            "fold checks failed:\n%s\n%s" % (out.stdout, out.stderr))
        self.assertIn("FOLD CHECKS PASS", out.stdout)

    def test_the_probe_still_finds_every_function_it_needs(self):
        """A rename in app.js must fail loudly here, not silently skip."""
        out = subprocess.run([NODE, str(PROBE)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        self.assertNotIn("not found:", out.stderr)


if __name__ == "__main__":
    unittest.main()
