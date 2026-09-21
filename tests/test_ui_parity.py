"""
The desktop and the backend must not disagree about whether to exit.

Found 2026-09-21 by an audit: gui/app.py recomputed the holding status live
but knew only about the MARKET half of the time-exit extension. A row the
backend shipped as "delay" (the stock is still above its own 5-bar mean, keep
riding) was re-derived on the desktop as "exit today" -- one screen telling
you to sell what the other said to hold.

    python -m unittest tests.test_ui_parity -v
"""
import unittest

from scanner.holding_tracker import _still_strong


def gui_still_riding(row):
    """Import the desktop predicate without importing tkinter."""
    import importlib.util
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    src = (root / "gui" / "app.py").read_text(encoding="utf-8")
    # Pull out just the two functions we need, so this test does not require
    # a display or the whole GUI module to import.
    import math
    ns = {"math": math}
    exec(compile(_extract(src, "def _num("), "gui_num", "exec"), ns)
    exec(compile(_extract(src, "def _still_riding("), "gui_ride", "exec"), ns)
    return ns["_still_riding"](row)


def _extract(src, header):
    start = src.index(header)
    lines = src[start:].split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if line and not line[0].isspace():
            break
        out.append(line)
    return "\n".join(out)


class RidePredicateParity(unittest.TestCase):
    CASES = [
        {"Close_Price": 101.0, "MA5": 100.0},      # above -> ride
        {"Close_Price": 100.0, "MA5": 100.0},      # exactly at -> do NOT ride
        {"Close_Price": 99.0, "MA5": 100.0},       # below -> exit
        {"Close_Price": None, "MA5": 100.0},       # unknown -> exit
        {"Close_Price": 101.0, "MA5": None},       # unknown -> exit
        {},                                        # nothing -> exit
        {"Close_Price": "101.0", "MA5": "100.0"},  # strings from JSON
    ]

    def test_desktop_matches_the_backend_exactly(self):
        for row in self.CASES:
            self.assertEqual(
                bool(_still_strong(row)), bool(gui_still_riding(row)),
                "desktop and backend disagree on %r" % (row,))

    def test_the_comparison_is_strict(self):
        """Equal is NOT riding: a close sitting exactly on its 5-bar mean is
        not strength, and both sides must agree on that."""
        row = {"Close_Price": 100.0, "MA5": 100.0}
        self.assertFalse(_still_strong(row))
        self.assertFalse(gui_still_riding(row))


if __name__ == "__main__":
    unittest.main()
