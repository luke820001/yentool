"""
A research harness must measure the rule the project actually ships.

Found 2026-09-22. archive/research/sandbox_entry_gate.py built its replay plan
by hand -- stop, tp, arm, lock -- and left out the late profit-take, which is
the fifth and sixth legs of scanner/exit_rules.DEFAULT_RULE. Every number that
file produced was therefore for a rule nobody trades.

It went unnoticed because of an unlucky coincidence: on the 556-trade sample
the late threshold changes the RECENT window (69.08% with no late leg, 70.81%
at the shipped +1%, 71.68% at the +0% that section H of docs/BACKTEST_LOG.md
records as REJECTED) and leaves the OLD window identical at 69.52% for both
thresholds. The documents, the desktop and the phone all carried 71.7%, which
is the rejected variant.

These are source-level assertions because running the harness needs a 427MB
research database and a warm cache. What they pin is the thing that actually
went wrong: a hand-written parameter list drifting from the shipped rule.

    python -m unittest tests.test_research_measures_shipped_rule -v
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "archive" / "research" / "sandbox_entry_gate.py"


def source():
    return GATE.read_text(encoding="utf-8")


class TheHarnessCarriesEveryLegOfTheRule(unittest.TestCase):
    def test_every_default_rule_key_is_taken_from_default_rule(self):
        """A leg read from DEFAULT_RULE cannot silently fall behind it."""
        from scanner.exit_rules import DEFAULT_RULE
        src = source()
        for key in ("stop_pct", "tp_pct", "arm_pct", "lock_pct",
                    "late_from", "late_gain"):
            self.assertIn('DEFAULT_RULE["%s"]' % key, src,
                          "%s is not taken from DEFAULT_RULE, so it can drift "
                          "from the shipped rule" % key)

    def test_the_replay_plan_includes_the_late_leg(self):
        src = source()
        m = re.search(r"plan = dict\((.*?)\)\n", src, re.S)
        self.assertIsNotNone(m, "the replay plan could not be found")
        plan = m.group(1)
        for leg in ("stop=", "tp=", "arm=", "lock=", "late_profit="):
            self.assertIn(leg, plan,
                          "the replay plan omits %s, so it measures a rule "
                          "the project does not ship" % leg)

    def test_no_exit_number_is_hand_written_in_the_plan(self):
        """A literal here is how the drift started."""
        src = source()
        m = re.search(r"plan = dict\((.*?)\)\n", src, re.S)
        plan = m.group(1)
        self.assertNotRegex(
            plan, r"=\s*0\.\d",
            "a hand-written threshold in the replay plan: take it from "
            "DEFAULT_RULE instead")


class TheDocumentsQuoteTheShippedRule(unittest.TestCase):
    """The headline the owner reads must belong to the rule that runs. 71.7%
    is the +0% threshold, which the log records as rejected."""

    FILES = ("README.md", "docs/TASKS.md", "docs/STRATEGY.md", "mobile/app.js")

    def test_the_rejected_variant_is_never_quoted_as_the_headline(self):
        for rel in self.FILES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            for line in text.splitlines():
                if "71.7%" not in line:
                    continue
                # Only a line CLAIMING the current rule's performance counts.
                # A comparison table may legitimately carry 71.7% as some
                # variant's own win rate.
                claims = any(w in line for w in
                             ("近 3 年", "每筆", "回測（",
                              "recent", "win rate"))
                if not claims:
                    continue
                # A claiming line may still mention it while explaining the
                # correction.
                explains = any(w in line for w in
                               ("否決", "更正", "0%", "REJECTED",
                                "used to quote"))
                self.assertTrue(
                    explains,
                    "%s quotes 71.7%% as a live figure: that is the +0%% "
                    "threshold the backtest log records as rejected.\n  %s"
                    % (rel, line.strip()[:120]))


if __name__ == "__main__":
    unittest.main()
