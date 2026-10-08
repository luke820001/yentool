"""
The PyInstaller recipe must carry every file the app reads at run time.

2026-10-08: config/report_text.json and config/twse_holidays.json joined
config/scan_modes.json, and TaiwanScanner.spec plus the packaging notes still
listed only the first. The frozen GUI would have run, quietly on the English
fallback report text and with no holiday list, which nothing else would flag.

    python -m unittest tests.test_packaging -v
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _packaging_notes():
    for p in sorted((ROOT / "docs").glob("*.txt")):
        text = p.read_text(encoding="utf-8")
        if "PyInstaller" in text and "--add-data" in text:
            return text
    return ""


class TestBundledConfig(unittest.TestCase):
    def setUp(self):
        self.files = sorted(p.name for p in (ROOT / "config").glob("*.json"))

    def test_there_are_config_files_to_bundle(self):
        self.assertIn("scan_modes.json", self.files)
        self.assertIn("report_text.json", self.files)
        self.assertIn("twse_holidays.json", self.files)

    def test_the_spec_bundles_every_config_json(self):
        spec = (ROOT / "TaiwanScanner.spec").read_text(encoding="utf-8")
        for name in self.files:
            with self.subTest(file=name):
                self.assertIn("('config/%s', 'config')" % name, spec)

    def test_the_packaging_notes_pass_every_config_json(self):
        notes = _packaging_notes()
        self.assertTrue(notes, "packaging notes not found under docs/")
        lines = [l for l in notes.splitlines() if "PyInstaller" in l
                 and "--add-data" in l]
        self.assertGreaterEqual(len(lines), 2)       # onedir and onefile
        for line in lines:
            for name in self.files:
                with self.subTest(file=name):
                    self.assertIn('--add-data "config/%s;config"' % name, line)


if __name__ == "__main__":
    unittest.main()
