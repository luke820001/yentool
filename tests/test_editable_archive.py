"""
The archived profit records must be editable, and an edit must actually change
the numbers.

Owner, 2026-09-22: 「獲利資料的封存應該要可以編輯」.

The phone's logic lives in mobile/app.js and runs in a browser, so these are
source-level invariants -- the same idiom tests/test_ui_parity.py and
tests/test_mobile_assets.py use. Each test is named for the failure it prevents.

    python -m unittest tests.test_editable_archive -v
"""
import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parent.parent / "mobile" / "app.js")


def source():
    return APP.read_text(encoding="utf-8")


class RestatedResultIsActuallyRebuilt(unittest.TestCase):
    """restateCycle() sets needs_rebuild on the frozen 10-day result when a fill
    is corrected or voided. Until 2026-09-22 NOTHING read that flag:
    freezeDueCycles() skipped any cycle that already existed, so the history
    page grew a 「已更正」 label while the figures stayed the ones computed from
    the fill the owner had just corrected. Editing an archived record is
    pointless if the profit number does not follow."""

    def test_the_flag_is_read_and_not_only_written(self):
        src = source()
        self.assertIn("needs_rebuild", src)
        # a read is a use in a condition or a property access on a loaded record
        reads = re.findall(r"\.needs_rebuild\b", src)
        self.assertTrue(
            reads,
            "needs_rebuild is written but never read: a corrected fill would "
            "restate the label and leave the number alone")

    def test_an_already_frozen_cycle_is_not_rebuilt_without_the_flag(self):
        """Report 5.3: at D10 the number STOPS moving. Later prices must never
        rewrite it -- only a correction to the fills behind it may."""
        src = source()
        self.assertIn("if (existing && !existing.needs_rebuild) continue;", src)

    def test_the_restatement_trail_survives_a_rebuild(self):
        """"Yesterday's number changed" has to stay answerable, so a SUCCESSFUL
        rebuild keeps revisions, the original frozen_at and the restatement
        reason.

        Anchored on `rebuilt_at`, which only the success branch writes. The
        earlier draft anchored on the first `needs_rebuild: false` in the file
        and broke the moment a refusal branch was added above it -- an
        assertion about source order, not about the audit trail.
        """
        src = source()
        i = src.find("rebuilt_at: nowStamp()")
        self.assertGreater(i, 0, "no successful rebuild branch found")
        window = src[max(0, i - 600):i + 300]
        for field in ("revisions", "frozen_at", "restated_at", "restated_reason",
                      "needs_rebuild: false"):
            self.assertIn(field, window,
                          "a rebuild drops %s, losing the audit trail" % field)

    def test_every_rebuild_path_clears_the_flag(self):
        """A path that leaves needs_rebuild set hangs a 已更正 label over a
        number nothing will ever recompute."""
        src = source()
        i = src.find("async function freezeDueCycles")
        j = src.find("function cycleFrom(pos, mark, horizon)", i)
        body = src[i:j]
        self.assertGreater(body.count("needs_rebuild: false"), 1,
                           "only one path clears the flag; the refusal paths "
                           "must clear it too")
        for reason in ("rebuild_failed", "rebuild_checked_at"):
            self.assertIn(reason, body,
                          "a refusal to rebuild must say why")

    def test_a_rebuild_never_invents_a_valuation(self):
        """The quote feed keeps 30 sessions. Rebuilding an older archived trade
        off the last available mark would rewrite its valuation day to ~30
        sessions ago and compute a return from the close of a stock the owner
        no longer holds."""
        src = source()
        i = src.find("async function freezeDueCycles")
        j = src.find("function cycleFrom(pos, mark, horizon)", i)
        body = src[i:j]
        self.assertIn("sameSession", body)
        self.assertIn("valuation_stale", body)
        self.assertIn("pos.open_shares > 0", body,
                      "a still-held position must not have a frozen figure "
                      "overwritten with today's unrealised one")
        self.assertIn("session_date: existing.session_date", body,
                      "a flat position keeps its frozen valuation day")


class StaleIndexIsNotAClosedMarket(unittest.TestCase):
    """The TAIEX arrives from a different feed than the per-stock bars and can
    be a session behind them. On 2026-09-21 it was: the index had closed above
    both its 20 and 60-day averages, the feed had not published that bar yet,
    and the buy gate vetoed all 46 rows with the reason "regime" -- which both
    screens render as 「大盤未站上20/60MA」, a statement about the market that
    was false. The veto is right; the sentence was not."""

    def test_the_two_reasons_are_separate_codes(self):
        from scanner.result_checks import BUY_BLOCKS
        self.assertIn("regime", BUY_BLOCKS)
        self.assertIn("regime_stale", BUY_BLOCKS)

    def test_both_screens_can_render_the_new_reason(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("mobile/app.js", "gui/app.py"):
            self.assertIn(
                "regime_stale", (root / rel).read_text(encoding="utf-8"),
                "%s would show the raw code to the owner" % rel)

    def test_the_scan_timer_retries_a_stale_index(self):
        """data_date is today, nothing is degraded and no check fails, so none
        of the timer's other conditions notice -- and the session stays
        unbuyable for a reason that usually fixes itself within the hour."""
        root = Path(__file__).resolve().parent.parent
        sh = (root / ".github" / "scripts" / "scan_timer.sh").read_text(
            encoding="utf-8")
        self.assertIn("meta.regime.is_current", sh)
        self.assertIn('"$regime_current" != "false"', sh)

    def test_the_regime_block_is_published_with_its_freshness(self):
        """The timer reads meta.regime.is_current straight off the payload."""
        root = Path(__file__).resolve().parent.parent
        src = (root / "scanner" / "result_export.py").read_text(encoding="utf-8")
        self.assertIn('"is_current": reg.get("is_current")', src)


if __name__ == "__main__":
    unittest.main()
