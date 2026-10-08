"""
Stage F1 (2026-10-08): the picks page's three groups and the investor views.

Source-level guards on mobile/app.js:
  * every backend code the phone can receive has a label (Buy_Block,
    Trade_Restriction and its display flags, Hold_Status, Exit_Signal,
    Rec_Status and Rec_Status_Reason, the list-freeze reason codes);
  * only the BLOCKING restriction kinds may read as an error or as "cannot
    buy" -- disposition entries were the best bucket (BACKTEST_LOG M.1);
  * the restriction sentences are the same templates the AI report uses
    (config/report_text.json), with the matching interval a PARSED value;
  * every column the phone reads is registered in scanner/result_checks, and
    the ones the new views read are phone=True (the company-event columns
    stay phone=False on purpose: display only, an outage must not fail);
  * the shipped wording stays inside the research findings (M.4 slots, M.6
    revenue, M.7 odd lots) and never quotes the rejected or inflated figures.

The rendering itself is exercised by tests/mobile_probe.js
(tests/test_mobile_probe.py).

    python -m unittest tests.test_mobile_picks_ui -v
"""
import json
import re
import unittest
from pathlib import Path

from scanner import list_freeze
from scanner import result_checks as rc
from scanner import trade_restrictions as tr

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "mobile" / "app.js").read_text(encoding="utf-8")
REPORT_TEXT = json.loads((ROOT / "config" / "report_text.json").read_text(encoding="utf-8"))

# columns stage F1 made phone=True (the picks page now reads them)
F1_PHONE = ("Gain_1M_Pct", "Vol_MA20", "Vol_Today", "Core_Plus", "Entry_Date",
            "Entry_Open", "Hold_Status", "Fill_Target_Price",
            "Initial_Stop_Price", "Initial_Target_Price")
# display-only company events: rendered, deliberately phone=False
EVENT_COLS = ("Rev_Month", "Rev_Amount_K", "Rev_YoY_Pct", "Rev_MoM_Pct",
              "Rev_Cum_YoY_Pct", "Ex_Date", "Ex_Kind", "Ex_Cash_Div", "Conf_Date")
# Rec_Status_Reason values written by portfolio/sync.py (exit reasons,
# no_fill, horizon_elapsed, rule_version, retracted:<code>) and
# tools/rec_set_status.py (degraded_run)
REC_REASONS = ("time", "stop", "lock", "tp", "late", "no_fill",
               "horizon_elapsed", "rule_version", "degraded_run", "off_list")


def _block(start):
    """Source text of the {...} that follows `start`, string-aware."""
    i = APP.index(start)
    j = APP.index("{", i + len(start) - 1)
    depth, quote, esc = 0, None, False
    for k in range(j, len(APP)):
        ch = APP[k]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return APP[j:k + 1]
    raise AssertionError("unbalanced: " + start)


def js_object(name):
    """{key: value-source} of a flat `const NAME = {...}` literal. String
    values are decoded; anything else is returned as its source text."""
    body = _block("const %s = {" % name)[1:-1]
    strings = []

    def mask(m):
        strings.append(m.group(0))
        return "\x00%d\x00" % (len(strings) - 1)

    masked = re.sub(r'"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`', mask, body)
    masked = re.sub(r"//[^\n]*", "", masked)
    parts, depth, cur = [], 0, ""
    for ch in masked:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)

    def unmask(s):
        return re.sub(r"\x00(\d+)\x00", lambda m: strings[int(m.group(1))], s)

    out = {}
    for part in parts:
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, val = part.split(":", 1)
        key, val = unmask(key.strip()), unmask(val.strip())
        if key.startswith('"'):
            key = json.loads(key)
        out[key] = json.loads(val) if val.startswith('"') else val
    return out


def fn_body(name):
    return _block("function %s(" % name)


class EveryBackendCodeHasALabel(unittest.TestCase):
    def test_buy_blocks(self):
        labels = js_object("BLOCK_TEXT")
        for code in rc.BUY_BLOCKS:
            if code:
                self.assertIn(code, labels, "Buy_Block %r has no phone label" % code)
                self.assertTrue(labels[code])

    def test_new_blocks_match_the_report_wording(self):
        labels = js_object("BLOCK_TEXT")
        for code in ("dropped", "restricted", "held", "regime", "regime_stale",
                     "quality", "market", "rank"):
            self.assertEqual(labels[code], REPORT_TEXT["block_label"][code], code)

    def test_restriction_kinds_and_display_flags(self):
        text, tone = js_object("RESTRICT_TEXT"), js_object("RESTRICT_TONE")
        for kind in tuple(tr.RESTRICTION_KINDS) + tuple(tr.DISPLAY_FLAGS):
            if kind == "none":
                continue
            self.assertIn(kind, text, kind)
            self.assertIn(kind, tone, kind)
            if kind in REPORT_TEXT["restriction_label"]:
                self.assertEqual(text[kind], REPORT_TEXT["restriction_label"][kind], kind)

    def test_hold_statuses_and_exit_signals(self):
        hold = js_object("HOLD_TEXT")
        for st in rc.HOLD_STATUSES:
            if st:
                self.assertIn(st, hold, st)
        reasons = js_object("EXIT_REASON_TEXT")
        for sig in rc.EXIT_SIGNALS:
            self.assertIn(sig, reasons, repr(sig))

    def test_list_freeze_codes(self):
        reasons = js_object("LIST_REASON_TEXT")
        for code in list_freeze.REASON_CODES:
            self.assertIn(code, reasons, code)
        revised = js_object("LIST_REVISED_TEXT")
        for code in list_freeze.REVISED_REASONS:
            self.assertIn(code, revised, code)

    def test_recommendation_statuses_and_reasons(self):
        st = js_object("REC_STATUS_TEXT")
        for s in rc.REC_STATUSES:
            self.assertIn(s, st, s)
        rr = js_object("REC_REASON_TEXT")
        for s in REC_REASONS:
            self.assertIn(s, rr, s)
        # "retracted:<Buy_Block>" is decoded through BLOCK_TEXT
        self.assertIn('s.startsWith("retracted:")', fn_body("recReasonText"))


class RestrictionsGuideTheOrderNotTheDecision(unittest.TestCase):
    def test_only_blocking_kinds_read_as_errors(self):
        tone = js_object("RESTRICT_TONE")
        for kind, t in tone.items():
            if kind in tr.BLOCKING_RESTRICTIONS:
                self.assertEqual(t, "err", kind)
            else:
                self.assertNotEqual(t, "err", "%s must not look like a refusal" % kind)

    def test_non_blocking_sentences_never_refuse(self):
        notes = js_object("RESTRICT_NOTE")
        for kind in ("disposition", "altered", "limit_lock", "attention", "unknown", "limit_down"):
            for word in ("\u4e0d\u53ef\u8cb7", "\u7121\u6cd5\u4e0b\u55ae", "\u898f\u5247\u4e0d\u8cb7"):
                self.assertNotIn(word, notes[kind], kind)

    def test_sentences_match_the_report_templates(self):
        notes = js_object("RESTRICT_NOTE")
        for key, val in REPORT_TEXT["restriction_note"].items():
            self.assertEqual(notes.get(key), val, key)

    def test_minutes_are_parsed_never_written(self):
        notes = js_object("RESTRICT_NOTE")
        self.assertIn("{match}", notes["disposition"])
        self.assertIn("{min}", notes["match"])
        for name in ("RESTRICT_NOTE", "ORDER_RULES"):
            src = _block("const %s = {" % name)
            self.assertIsNone(re.search(r"\d+\s*\u5206\u9418", src), name)
        self.assertIn("Restriction_Match_Min", fn_body("restrictionInfo"))
        self.assertIn("Restriction_Prepay", fn_body("restrictionInfo"))

    def test_only_suspended_blocks(self):
        self.assertEqual(tuple(tr.BLOCKING_RESTRICTIONS), ("suspended",))


class ThePhoneReadsRegisteredColumns(unittest.TestCase):
    def test_f1_columns_are_phone_columns(self):
        for col in F1_PHONE:
            self.assertTrue(rc.COLUMNS[col]["phone"], col)

    def test_event_columns_stay_display_only(self):
        for col in EVENT_COLS:
            self.assertFalse(rc.COLUMNS[col]["phone"], col)

    def test_every_column_read_is_registered(self):
        cols = set(re.findall(r"\b(?:r|row|x|t)\.([A-Z][A-Za-z0-9_]+)", APP))
        self.assertTrue(cols)
        self.assertEqual(sorted(c for c in cols if c not in rc.COLUMNS), [])

    def test_the_new_views_read_phone_columns(self):
        i = APP.index("10b. Investor views")
        j = APP.index("// --- 11.2 ")
        cols = set(re.findall(r"\b(?:r|row)\.([A-Z][A-Za-z0-9_]+)", APP[i:j]))
        self.assertIn("Prev_Signal_Date", cols)
        off = sorted(c for c in cols if c not in EVENT_COLS and not rc.COLUMNS[c]["phone"])
        self.assertEqual(off, [], "read by the picks views but phone=False")


class TheWordingStaysInsideTheResearch(unittest.TestCase):
    def test_slot_guidance_m4(self):
        src = _block("const SIZING_DEFAULTS = {")
        self.assertIn("slots: 8", src)
        i = APP.index("const SLOT_GUIDANCE")
        text = APP[i:APP.index(";", i)]
        for want in ("5\uff5e8", "\u907f\u514d 1\uff5e3 \u683c", "NT$70k", "NT$112k", "12%", "17%", "2026"):
            self.assertIn(want, text, want)

    def test_never_the_inflated_figures(self):
        # 71.7% is the rejected +0% late threshold; 35.8% is K.1's 3-slot
        # CAGR, inflated by same-day slot reuse (ADDENDUM 5). Code comments
        # may explain them; nothing the owner reads may quote them.
        for line in APP.splitlines():
            code = line.strip()
            if code.startswith(("//", "*", "/*")):
                continue
            for bad in ("71.7%", "35.8%"):
                self.assertNotIn(bad, code)

    def test_slot_text_cites_m4_and_no_loose_slot_cagr(self):
        # M.8 item 8 / ADDENDUM 5: every slot figure the owner reads is the
        # strict M.4 band. The old strategy card quoted K.1/L's loose
        # "10 slots +18% / 5 slots +25% / -26%" (same-day slot reuse).
        i = APP.index("const SLOT_GUIDANCE")
        self.assertIn("\uff08BACKTEST_LOG M.4\uff09", APP[i:APP.index(";", i)])
        for line in APP.splitlines():
            code = line.strip()
            if code.startswith(("//", "*", "/*")):
                continue
            for bad in ("+25% / \u221226%", "\u516d\u5e74\u534a\u5e74\u5316",
                        "10 \u683c\u3001\u6709\u8a0a\u865f\u5c31\u8cb7"):
                self.assertNotIn(bad, code)
        j = APP.index("mode_prelaunch: {")
        card = APP[j:APP.index("mode_momentum_leader", j)]
        self.assertIn("BACKTEST_LOG M.4 \u56b4\u683c\u683c\u6578", card)
        self.assertIn("5\uff5e8 \u500b\u7b49\u6b0a\u683c", card)

    def test_odd_lot_figures_m7(self):
        rules = js_object("ORDER_RULES")
        for want in ("+0.2%\uff5e+0.3%", "\u4e2d\u4f4d\u6578 0", "-1.7%\uff5e+2.9%"):
            self.assertIn(want, rules["odd_cost"], want)
        self.assertIn("Entry_Open", rules["anchor_note"])
        self.assertRegex(rules["as_of"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(rules["odd_first"], "09:10")

    def test_revenue_is_labelled_and_not_coloured_m6(self):
        body = fn_body("eventsLine")
        self.assertIn("\u6708\u71df\u6536", body)
        for colour in ("signClass", '"pos"', '"neg"', "class="):
            self.assertNotIn(colour, body)

    def test_sizing_is_advice_not_an_order(self):
        self.assertIn("\u975e\u6295\u8cc7\u5efa\u8b70", APP[APP.index("const SIZING_CAPTION"):][:80])
        # the buy form only HINTS the size; it never pre-fills shares
        self.assertIn("buyQtyHint(row)", fn_body("openExecutionForm"))
        self.assertIn('const defShares = editing ? String(editing.shares) : "";', APP)


class PicksPageStructure(unittest.TestCase):
    def test_three_groups_in_order(self):
        body = fn_body("renderPicks")
        a = body.index('data-group="buy"')
        b = body.index('data-group="hold"')
        c = body.index('<details class="grp" data-group="ref"')
        self.assertLess(a, b)
        self.assertLess(b, c)
        self.assertIn("\u53c3\u8003\uff08\u4e0a\u5e02\u3001\u4e0d\u53ef\u8cb7\uff0c", body)
        self.assertIn("exitSignalSummary(holdLive)", body)

    def test_group_rule_reuses_the_backend_verdict(self):
        body = fn_body("pickGroup")
        self.assertTrue(body.lstrip("{").strip().startswith('if (buyVerdict(r).ok) return "buy";'))

    def test_toggle_state_survives_rerender(self):
        i = APP.index('document.addEventListener("toggle"')
        seg = APP[i:i + 700]
        self.assertIn("}, true);", seg)
        self.assertIn("STATE.refOpen", seg)
        self.assertIn("data-forced", seg)

    def test_buy_paths_read_list_rows_not_the_universe(self):
        self.assertNotIn("STATE.rows.find((r) => String(r.Stock_ID) === d.id)", APP)
        self.assertIn("listRowFor(data.row)", fn_body("saveExecution"))
        body = fn_body("listRowFor")
        self.assertNotIn("universe", body)

    def test_sizing_is_in_the_backup(self):
        # mobile.md P1-3: meta "sizing" = {capital_cents, max_slots,
        # risk_pct} beside "settings", exported with the backup (exportBackup
        # exports every non-private meta row) and validated on import.
        self.assertIn('const SIZING_META = "sizing";', APP)
        self.assertIn("capital_cents: v.capital * 100, max_slots: v.slots, risk_pct: v.risk_pct",
                      fn_body("sizingToMeta"))
        self.assertIn("metaSet(SIZING_META, sizingToMeta(value))", fn_body("saveSizing"))
        boot = fn_body("boot")
        self.assertIn("sizingFromMeta(await metaGet(SIZING_META, null))", boot)
        imp = fn_body("runImport")
        self.assertIn("sizingFromMeta(sizingRow.value)", imp)
        self.assertLess(imp.index("importedSizing) {"), imp.index("dbPutMany"))
        exp = fn_body("exportBackup")
        self.assertIn("PRIVATE_META.has(m.key)", exp)
        self.assertNotIn("sizing", exp)

    def test_sizing_storage_is_wrapped(self):
        for name in ("loadSizing", "saveSizing"):
            body = fn_body(name)
            self.assertIn("try {", body, name)
            self.assertIn("catch", body, name)

    def test_asset_version_bumped(self):
        sw = (ROOT / "mobile" / "sw.js").read_text(encoding="utf-8")
        m = re.search(r'const VERSION = "v(\d+)"', sw)
        self.assertGreaterEqual(int(m.group(1)), 31)


if __name__ == "__main__":
    unittest.main()
