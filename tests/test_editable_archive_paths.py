"""
The editable archive, one test per failure it prevents.

Owner, 2026-09-22: 「獲利資料的封存應該要可以編輯」. Making the archive editable
turned out to need five data fixes underneath it, because the edit paths an
archived record would newly reach were already wrong for live positions too.

The phone's logic runs in a browser, so these are source-level invariants, the
idiom tests/test_ui_parity.py and tests/test_mobile_assets.py already use.

    python -m unittest tests.test_editable_archive_paths -v
"""
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "mobile" / "app.js"


def source():
    return APP.read_text(encoding="utf-8")


def between(src, start, end):
    """The slice of `src` from `start` up to the next `end`."""
    i = src.find(start)
    assert i > 0, "anchor not found: %s" % start
    j = src.find(end, i + 1)
    return src[i:j if j > 0 else len(src)]


class ArchivedRecordHasAReachableEditPath(unittest.TestCase):
    """An archived record renders no position card, so the 補登／更正成交
    button never appears and the record is permanently uneditable. The
    已平倉／封存 table was its only representation, and it was read-only."""

    def test_the_history_table_offers_an_edit_button(self):
        body = between(source(), "function renderPerf()", "function openCycleDetail")
        self.assertIn('btn("execs", "更正"', body)

    def test_it_reuses_the_existing_action_and_sheet(self):
        src = source()
        self.assertIn('if (act === "execs")', src,
                      "the dispatcher branch it routes to must still exist")
        sheet = between(src, "async function openExecutionList",
                        "async function openCycleDetail")
        self.assertNotIn("pos.status", sheet,
                         "openExecutionList must stay status-agnostic, or an "
                         "archived record is refused at the sheet")


class SellCorrectionIsBoundedByTheFold(unittest.TestCase):
    """pos.open_shares is the state AFTER the whole history, and it is 0 for
    every archived and closed record. Correcting the sell that closed the trade
    was rejected with 只持有 0 股, against the state that sell itself produced.
    The same bug blocked amending a staged exit on a live position."""

    def test_the_summary_field_is_no_longer_the_bound(self):
        self.assertNotIn("}, pos, pos ? pos.open_shares : 0);", source())

    def test_the_fold_helpers_exist_and_are_used(self):
        src = source()
        for fn in ("plannedExecutions", "heldBefore", "firstOversell"):
            self.assertIn("function %s(" % fn, src, "%s is not defined" % fn)
            self.assertGreaterEqual(src.count("%s(" % fn), 2,
                                    "%s is defined but never used" % fn)

    def test_the_form_hint_uses_the_same_fold(self):
        """Reading the summary field for the hint would print 目前持有 0 股
        next to a field that now accepts 1,000."""
        body = between(source(), "async function openExecutionForm",
                       "function readForm")
        self.assertNotIn("const held = pos ? pos.open_shares : 0", body,
                         "the hint still reads the post-history summary")
        self.assertIn("heldBefore(", body)


class ACorrectionCannotSilentlyDropALaterSell(unittest.TestCase):
    """Bounding only the edited row says nothing about the fills after it. A
    corrected sell can swallow the shares a later sell needs; replay() then
    skips that sell and its whole proceeds vanish from realised P and L with no
    error and no label. The mirror case, amending an earlier buy down, lands
    open_shares negative."""

    def _writer(self, name):
        body = between(source(), name, "await txDone")
        return body

    def test_both_ledger_writers_refuse_before_the_transaction(self):
        for fn in ("async function addExecution", "async function voidExecution"):
            body = self._writer(fn)
            self.assertIn("firstOversell(", body,
                          "%s does not check the whole fold" % fn)
            guard = body.find("firstOversell(")
            opened = body.find("db.transaction(")
            self.assertTrue(opened < 0 or guard < opened,
                            "%s opens the transaction before checking" % fn)

    def test_the_refusal_names_the_conflicting_fill(self):
        src = source()
        self.assertIn("function oversellMessage(", src)
        self.assertIn("bad.exe.session_date", src,
                      "the refusal must name WHICH sell conflicts")

    def test_the_form_checks_the_whole_history_too(self):
        body = between(source(), "async function saveExecution",
                       "async function unarchivedNote")
        self.assertIn("firstOversell(", body)

    def test_the_defensive_comment_is_now_backed_by_a_refusal(self):
        """replay() calls its oversell branch 'defensive; addExecution refuses
        this', which was not true until firstOversell existed."""
        body = between(source(), "async function addExecution",
                       "async function voidExecution")
        self.assertIn("LedgerError", body)


class AnEditNeverLeavesAnInvisibleLiveHolding(unittest.TestCase):
    """Voiding the closing sell, or backfilling a buy, would leave the status
    archived with shares above zero: hidden from 持倉, skipped by 待處理,
    unfetched by the universe, yet valued by rebuildMarks and summed by
    portfolioSummary every day."""

    def _derived(self):
        return between(source(), "function applyDerived(",
                       "// Validate and record one fill")

    def test_a_record_that_regains_shares_leaves_the_archive(self):
        body = self._derived()
        self.assertIn('pos.status === "archived" && d.shares !== 0', body)
        self.assertIn('pos.status = "open"', body)
        self.assertIn("unarchived_at", body)

    def test_the_latch_itself_is_untouched(self):
        """Dropping archived from the condition is the smaller-looking change
        and the destructive one: the next unrelated correction would flip it
        back to closed, so archiving would be undone by a price typo fix."""
        self.assertIn('if (pos.status !== "archived" && pos.status !== "void")',
                      self._derived())

    def test_the_filing_timestamp_is_never_erased(self):
        src = source()
        self.assertNotIn("archived_at = null", src)
        self.assertNotIn("archived_at: null", src)

    def test_the_move_is_announced(self):
        src = source()
        self.assertIn("async function unarchivedNote(", src)
        self.assertGreaterEqual(
            src.count("unarchivedNote("), 3,
            "both the save path and the void path must announce it")


class ABackfilledFillRestatesTheFrozenResult(unittest.TestCase):
    """A backfill moved realized_net while the frozen 十日成果 kept its old
    figure and grew no 已更正 label at all, which is strictly worse than the
    corrected path: nothing on the page said the two disagreed."""

    def test_every_write_restates_not_only_a_correction(self):
        src = source()
        self.assertNotIn("if (o.supersedes) await restateCycle", src)
        body = between(src, "async function addExecution",
                       "async function voidExecution")
        self.assertIn("成交更正", body)
        self.assertIn("補登成交", body)


class ARebuildNeverBlanksAColumn(unittest.TestCase):
    """replay() zeroes avg_cost the moment shares hit zero, so any rebuild of a
    closed or archived record turned 相對實際成本 into a dash. Editing a trade
    would lose a column."""

    def test_the_ratio_comes_from_the_mark(self):
        body = between(source(), "function cycleFrom(",
                       "async function restateCycle")
        self.assertIn("mark.open_shares", body)
        self.assertIn("mark.gross_cost", body)

    def test_a_rebuild_carries_a_non_null_field_forward(self):
        body = between(source(), "async function freezeDueCycles",
                       "function cycleFrom(pos, mark, horizon)")
        self.assertIn("existing.return_vs_cost", body)
        self.assertIn("existing.day_index", body)


class ShortenedHistoryLeavesNoOrphanMarks(unittest.TestCase):
    """Nothing deleted a mark, so latestMark returned a valuation day the
    corrected fills no longer reach, and 帳面總損益 kept a profit that
    已實現淨損益 had already given up."""

    def _rebuild(self):
        return between(source(), "async function rebuildMarks",
                       "async function freezeDueCycles")

    def test_the_sweep_exists_and_is_range_guarded(self):
        body = self._rebuild()
        self.assertIn(".delete(", body, "orphan marks are never removed")
        self.assertIn("sessions[sessions.length - 1]", body,
                      "an unbounded sweep would wipe every older mark, and "
                      "marks are not in the backup")
        self.assertLess(body.find("if (!sessions.length) return;"),
                        body.find(".delete("),
                        "on a feed-outage day the keep-set is empty and an "
                        "unguarded sweep would wipe the whole window")

    def test_the_delete_and_the_put_share_one_transaction(self):
        body = self._rebuild()
        i = body.find('db.transaction("marks", "readwrite")')
        self.assertGreater(i, 0)
        tail = body[i:]
        self.assertIn(".delete(", tail)
        self.assertIn(".put(", tail)


class VoidedPositionsDoNotShowAFrozenProfit(unittest.TestCase):
    """loadCycles keys by position_id with no status filter, so the frozen
    table could show a profit for a position the same page declares
    不計入損益."""

    def test_the_frozen_table_filters_voided_positions(self):
        body = between(source(), "function renderPerf()", "function openCycleDetail")
        self.assertIn('status !== "void"', body)


class TheDetailModalNeverPrintsDNull(unittest.TestCase):
    """renderPerf guards day_index; openCycleDetail printed it unguarded."""

    def test_the_day_index_is_guarded(self):
        body = between(source(), "async function openCycleDetail",
                       "function openDetail")
        self.assertIn("cycle.day_index === null", body)


class TheInTableButtonFitsAPhone(unittest.TestCase):
    """.btn defaults to a 96px minimum width, which blows a four-column table
    off a 375px screen. The touch target must still be thumb-sized."""

    def test_the_table_button_is_sized_down_but_not_shortened(self):
        css = (APP.parent / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".tbl .btn", css)
        rule = between(css, ".tbl .btn", "}")
        self.assertIn("min-width: 0", rule)
        self.assertNotIn("min-height", rule,
                         "min-height must stay at --tap, inherited from .btn")


if __name__ == "__main__":
    unittest.main()
