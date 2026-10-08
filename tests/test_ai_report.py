"""
AI report: block legend, per-row reasons, the template, the retry policy and
meta.report_sources (2026-10-08, plan P1-7). No network: requests.post is
stubbed and the clock / sleep are fakes. ASCII only (Chinese as escapes).
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import requests

import gemini_hook.gemini_client as gc
import gemini_hook.prompt_builder as pb
from scanner.result_checks import BUY_BLOCKS, check_payload, append_history
from tests.test_result_checks import clean_payload, clean_quotes, codes

ROOT = Path(__file__).resolve().parent.parent
TEXT_FILE = ROOT / "config" / "report_text.json"
LISTED = "\u4e0a\u5e02"          # TWSE-listed
OTC_ZH = "\u4e0a\u6ac3"          # TPEx / OTC
MARKET_COND = "\u5e02\u5834\u689d\u4ef6"   # "market conditions"
TEMPLATE_ZH = "\u6a21\u677f"     # "template"
RAW_MARKET = "\uff08market\uff09"
HELD_PREV_ZH = "\u524d\u4e00\u4ea4\u6613\u65e5"      # "previous session"
HELD_ENTERED_ZH = "\u5df2\u9032\u5834"                 # "already entered"


def raw_text():
    with open(TEXT_FILE, encoding="utf-8") as f:
        return json.load(f)


def expected_codes():
    return (set(BUY_BLOCKS) - {""}) | {"not_evaluated"}


def two_rows():
    return pd.DataFrame([
        {"Stock_ID": "6426", "Stock_Name": "OtcCo", "Market": "OTC",
         "Buy_Ready": True, "Buy_Block": "", "Close_Price": 100.0,
         "Trade_Restriction": "none"},
        {"Stock_ID": "2330", "Stock_Name": "TseCo", "Market": "TSE",
         "Buy_Ready": False, "Buy_Block": "market", "Close_Price": 900.0,
         "Trade_Restriction": "none"},
    ])


def line_of(prompt, sid):
    return [ln for ln in prompt.splitlines() if " {} ".format(sid) in ln][0]


class Ascii(unittest.TestCase):
    def test_report_modules_are_pure_ascii(self):
        for rel in ("gemini_hook/prompt_builder.py",
                    "gemini_hook/gemini_client.py",
                    "tests/test_ai_report.py"):
            data = (ROOT / rel).read_bytes()
            bad = [i for i, b in enumerate(data) if b > 127]
            self.assertEqual(bad, [], rel)


class Legend(unittest.TestCase):
    def test_every_block_code_is_explained_everywhere(self):
        txt = raw_text()
        for code in sorted(expected_codes()):
            self.assertIn(code, pb.BLOCK_LEGEND, code)
            self.assertIn(code, pb.BLOCK_ORDER, code)
            self.assertIn("block={}:".format(code), pb.SYSTEM_INSTRUCTION, code)
            self.assertIn("block={}:".format(code), pb.system_instruction(), code)
            self.assertTrue(str(txt["block_reason"].get(code) or "").strip(), code)
            self.assertTrue(str(txt["block_label"].get(code) or "").strip(), code)

    def test_every_code_the_scanner_can_ship_is_registered(self):
        """A new mask in mark_buy_ready (or tracked_rows) must reach
        BUY_BLOCKS and therefore this legend; otherwise CI fails here."""
        src = (ROOT / "scanner" / "scan_mode.py").read_text(encoding="utf-8")
        shipped = set(re.findall(r'block\.mask\([^\n]*?,\s*"(\w+)"\)', src))
        shipped |= {"regime", "regime_stale", "no_rule"}
        trk = (ROOT / "scanner" / "tracked_rows.py").read_text(encoding="utf-8")
        shipped |= set(re.findall(r'\["Buy_Block"\]\s*=\s*"(\w+)"', trk))
        self.assertIn("restricted", shipped)
        self.assertIn("dropped", shipped)
        self.assertLessEqual(shipped, set(BUY_BLOCKS))
        self.assertLessEqual(shipped, set(pb.BLOCK_LEGEND))

    def test_market_is_not_about_market_conditions(self):
        self.assertNotIn("quality/market/rank", pb.SYSTEM_INSTRUCTION)
        self.assertIn("OTC", pb.BLOCK_LEGEND["market"])
        self.assertIn("NOT", pb.BLOCK_LEGEND["market"])
        reason = raw_text()["block_reason"]["market"]
        self.assertIn(LISTED, reason)
        self.assertIn(OTC_ZH, reason)
        self.assertNotIn(MARKET_COND, reason)
        for code in ("quality", "rank", "held", "restricted"):
            self.assertNotIn(MARKET_COND, raw_text()["block_reason"][code], code)

    def test_held_does_not_claim_the_rule_bought_earlier(self):
        """held = on the previous session's list and not pending
        (scan_mode.mark_buy_ready: ~fresh & known). In the live ledger
        (09-09..10-07) 13 of the 21 held rows began their streak on a REFUSED
        day (7 regime, 6 quality) and only 1 on a real buy, so "the rule
        already entered this name" was false for most of them."""
        legend = pb.BLOCK_LEGEND["held"]
        self.assertIn("previous session", legend)
        self.assertIn("first day", legend)
        self.assertNotIn("already entered", legend)
        reason = raw_text()["block_reason"]["held"]
        self.assertIn(HELD_PREV_ZH, reason)
        self.assertNotIn(HELD_ENTERED_ZH, reason)
        self.assertNotIn(HELD_ENTERED_ZH, raw_text()["block_label"]["held"])

    def test_numbers_in_the_text_match_the_rule(self):
        from scanner import scan_mode as sm
        self.assertEqual(sm.N_ENTER, 20)
        self.assertEqual((sm.CORE_PLUS_DIST52_MAX, sm.CORE_PLUS_RET5_MAX,
                          sm.CORE_PLUS_ATR_MIN), (5.0, 5.0, 4.5))
        self.assertIn("20", pb.BLOCK_LEGEND["rank"])
        self.assertIn("20", raw_text()["block_reason"]["rank"])
        for s in (pb.BLOCK_LEGEND["quality"], raw_text()["block_reason"]["quality"]):
            self.assertIn("4.5%", s)
            self.assertIn("5%", s)

    def test_restricted_legend_follows_the_blocking_set(self):
        import scanner.trade_restrictions as tr
        self.assertIn("kinds: suspended", pb.system_instruction())
        self.assertIn("disposition", pb.block_legend()["restricted"])
        with mock.patch.object(tr, "BLOCKING_RESTRICTIONS",
                               ("suspended", "disposition")):
            line = pb.block_legend()["restricted"]
            self.assertIn("kinds: suspended, disposition", line)
            self.assertNotIn("(disposition,", line)
            self.assertIn("attention", line)


class Prompt(unittest.TestCase):
    def test_refused_row_quotes_its_reason_and_the_buy_row_does_not(self):
        prompt = pb.build_prompt(two_rows())
        reason = raw_text()["block_reason"]["market"]
        self.assertIn('why="{}"'.format(reason), line_of(prompt, "2330"))
        self.assertIn("buy=NO block=market", line_of(prompt, "2330"))
        self.assertNotIn("why=", line_of(prompt, "6426"))
        self.assertIn("buy=YES", line_of(prompt, "6426"))

    def test_market_report_rows_carry_their_full_list_position(self):
        df = two_rows()
        df["List_Pos"] = [23, 4]
        prompt = pb.build_prompt(df)
        self.assertTrue(line_of(prompt, "6426").startswith("rank=1 list#=23 6426"))
        self.assertTrue(line_of(prompt, "2330").startswith("rank=2 list#=4 2330"))
        self.assertIn("list#:", pb.SYSTEM_INSTRUCTION)
        self.assertNotIn("list#", line_of(pb.build_prompt(two_rows()), "6426"))

    def test_list_position_is_omitted_where_it_equals_the_rank(self):
        df = two_rows()
        df["List_Pos"] = [1, 7]
        prompt = pb.build_prompt(df)
        self.assertTrue(line_of(prompt, "6426").startswith("rank=1 6426"))
        self.assertTrue(line_of(prompt, "2330").startswith("rank=2 list#=7 2330"))

    def test_text_buy_ready_false_is_not_a_yes(self):
        df = two_rows()
        df["Buy_Ready"] = ["False", "0"]
        df["Buy_Block"] = ["held", "market"]
        prompt = pb.build_prompt(df)
        self.assertIn("buy=NO block=held", line_of(prompt, "6426"))
        self.assertIn("buy=NO block=market", line_of(prompt, "2330"))
        self.assertIn("0 of these 2 are buy=YES", prompt)
        df["Buy_Ready"] = ["True", "1"]
        self.assertIn("buy=YES", line_of(pb.build_prompt(df), "6426"))

    def test_system_instruction_is_not_sent_twice(self):
        prompt = pb.build_prompt(two_rows())
        self.assertNotIn("You are a senior", prompt)
        self.assertIn("=== Screened Stocks (top 2 of 2) ===", prompt)

    def test_unevaluated_frame_is_not_buyable(self):
        df = two_rows().drop(columns=["Buy_Ready", "Buy_Block"])
        line = line_of(pb.build_prompt(df), "6426")
        self.assertIn("buy=UNKNOWN block=not_evaluated", line)
        self.assertIn('why="{}"'.format(raw_text()["block_reason"]["not_evaluated"]), line)

    def test_nan_buy_ready_is_not_a_yes(self):
        df = two_rows()
        df["Buy_Ready"] = [float("nan"), False]
        self.assertIn("buy=NO", line_of(pb.build_prompt(df), "6426"))

    def test_disposition_note_uses_the_parsed_terms(self):
        df = two_rows()
        df.loc[0, "Trade_Restriction"] = "disposition"
        df["Restriction_Until"] = ["2026-10-08", None]
        df["Restriction_Match_Min"] = [2.0, float("nan")]
        df["Restriction_Prepay"] = ["all", None]
        line = line_of(pb.build_prompt(df), "6426")
        notes = raw_text()["restriction_note"]
        self.assertIn('exec="', line)
        self.assertIn("2026-10-08", line)
        self.assertIn(notes["match"].format(min=2), line)
        self.assertIn(notes["prepay_all"], line)
        self.assertIn("buy=YES", line)
        self.assertNotIn("why=", line)
        # no exec= for a name without a measure
        self.assertNotIn("exec=", line_of(pb.build_prompt(df), "2330"))

    def test_no_interval_is_invented(self):
        row = {"Trade_Restriction": "disposition", "Restriction_Match_Min": None,
               "Restriction_Prepay": None, "Restriction_Until": None}
        note = pb.restriction_note(row)
        frag = raw_text()["restriction_note"]["match"].split("{min}")[0]
        self.assertTrue(note)
        self.assertNotIn(frag, note)
        self.assertNotIn("{", note)

    def test_a_widened_blocking_set_changes_the_note(self):
        import scanner.trade_restrictions as tr
        row = {"Trade_Restriction": "disposition", "Restriction_Match_Min": 5,
               "Restriction_Prepay": "threshold", "Restriction_Until": "2026-10-20"}
        normal = pb.restriction_note(row)
        with mock.patch.object(tr, "BLOCKING_RESTRICTIONS",
                               ("suspended", "disposition")):
            blocked = pb.restriction_note(row)
        self.assertNotEqual(normal, blocked)
        label = raw_text()["restriction_label"]["disposition"]
        self.assertTrue(blocked.startswith(label))
        self.assertIn("2026-10-20", blocked)

    def test_restricted_reason_names_the_kind(self):
        row = {"Trade_Restriction": "suspended"}
        got = pb.block_reason("restricted", row)
        self.assertIn(raw_text()["restriction_label"]["suspended"], got)
        self.assertNotIn("{", got)


class Template(unittest.TestCase):
    def test_header_says_template_and_reasons_are_words(self):
        out = pb.build_local_report(two_rows())
        txt = raw_text()
        self.assertEqual(out.split("\n")[0], txt["template_header"])
        self.assertIn(TEMPLATE_ZH, out.split("\n")[0])
        self.assertIn(txt["block_reason"]["market"], out)
        self.assertNotIn(RAW_MARKET, out)
        self.assertIn(txt["verdict_ok"], out)

    def test_market_template_shows_the_full_list_position(self):
        """An OTC template row #6 that is list #30 must not read '#6 ... not
        in the top 20' (live payload 2026-10-07, 1569)."""
        df = two_rows()
        df["List_Pos"] = [1, 30]
        out = pb.build_local_report(df)
        pos_txt = raw_text()["row_pos"].format(pos=30)
        self.assertIn("#2" + pos_txt + " 2330", out)
        self.assertIn("#1 6426", out)
        self.assertNotIn("{", out)
        with mock.patch.object(pb, "REPORT_TEXT_FILE", ROOT / "config" / "nope.json"):
            out = pb.build_local_report(df)
        self.assertIn("#2 (full list #30) 2330", out)
        self.assertNotIn("{", out)

    def test_empty_frame(self):
        self.assertEqual(pb.build_local_report(pd.DataFrame()), raw_text()["empty"])
        self.assertEqual(pb.build_local_report(None), raw_text()["empty"])

    def test_missing_text_file_falls_back_to_ascii(self):
        with mock.patch.object(pb, "REPORT_TEXT_FILE", ROOT / "config" / "nope.json"):
            out = pb.build_local_report(two_rows())
            prompt = pb.build_prompt(two_rows())
        self.assertTrue(out.strip())
        self.assertEqual(out.split("\n")[0], pb._FALLBACK_TEXT["template_header"])
        self.assertIn("2330", out)
        self.assertIn('why="', line_of(prompt, "2330"))

    def test_broken_text_file_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "report_text.json"
            bad.write_text("{not json", encoding="utf-8")
            with mock.patch.object(pb, "REPORT_TEXT_FILE", bad):
                self.assertTrue(pb.build_local_report(two_rows()).strip())
            odd = Path(d) / "odd.json"
            odd.write_text(json.dumps({"row": "#{rank} {nope[3]}", "flags": "x",
                                       "block_reason": {"market": 5}}),
                           encoding="utf-8")
            with mock.patch.object(pb, "REPORT_TEXT_FILE", odd):
                out = pb.build_local_report(two_rows())
                self.assertTrue(out.strip())
                # a non-string reason keeps the fallback
                self.assertEqual(pb.report_text()["block_reason"]["market"],
                                 pb.BLOCK_LEGEND["market"])


class FakeResp(object):
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def gemini_ok(text="AI says hi"):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def groq_ok(text="Groq says hi"):
    return FakeResp(200, {"choices": [{"message": {"content": text}}]})


class Clock(object):
    """Fake monotonic clock; every post costs `post_cost` seconds."""

    def __init__(self, post_cost=0.0):
        self.t = 1000.0
        self.sleeps = []
        self.post_cost = post_cost

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class Client(unittest.TestCase):
    def setUp(self):
        self.clk = Clock()
        self.patches = [
            mock.patch.object(gc, "GEMINI_API_KEY", "gem-key-123456"),
            mock.patch.object(gc, "GROQ_API_KEY", ""),
            mock.patch.object(gc, "_save_report"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def run_with(self, responses, budget=None, df=None):
        seq = list(responses)
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append({"url": url, "timeout": timeout, "json": json})
            self.clk.t += self.clk.post_cost
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        if budget is None:
            budget = gc.RetryBudget(clock=self.clk.clock, sleep=self.clk.sleep)
        with mock.patch.object(gc.requests, "post", side_effect=fake_post):
            res = gc.generate_report_meta(two_rows() if df is None else df,
                                          budget=budget)
        return res, calls

    def test_503_then_ok_retries_once(self):
        res, calls = self.run_with([FakeResp(503, text="overloaded"), gemini_ok()])
        self.assertEqual(res["source"], "gemini")
        self.assertEqual(res["text"], "AI says hi")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.clk.sleeps, [gc.TRANSIENT_WAIT])
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(res["error"], "")
        self.assertEqual(res["model"], gc.GEMINI_MODEL)

    def test_503_twice_is_a_template_and_groq_is_skipped(self):
        res, calls = self.run_with([FakeResp(503, text="a"), FakeResp(503, text="b")])
        self.assertEqual(res["source"], "template")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("googleapis" in c["url"] for c in calls))
        self.assertEqual(res["text"].split("\n")[0], raw_text()["template_header"])
        self.assertIn("HTTP 503", res["error"])
        self.assertIn("Groq: not configured", res["error"])
        self.assertEqual(res["model"], "")

    def test_timeout_and_connection_error_are_retried(self):
        res, _ = self.run_with([requests.Timeout("slow"), gemini_ok()])
        self.assertEqual(res["source"], "gemini")
        self.clk.sleeps = []
        res, _ = self.run_with([requests.ConnectionError("reset"), gemini_ok()])
        self.assertEqual(res["source"], "gemini")
        self.assertEqual(self.clk.sleeps, [gc.TRANSIENT_WAIT])

    def test_client_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            self.clk.sleeps = []
            res, calls = self.run_with([FakeResp(status, text="no")])
            self.assertEqual(len(calls), 1, status)
            self.assertEqual(self.clk.sleeps, [], status)
            self.assertEqual(res["source"], "template", status)

    def test_non_json_and_empty_answers_are_not_retried(self):
        res, calls = self.run_with([FakeResp(200, None, text="<html>")])
        self.assertEqual((res["source"], len(calls)), ("template", 1))
        res, calls = self.run_with([gemini_ok("   ")])
        self.assertEqual((res["source"], len(calls)), ("template", 1))

    def test_429_keeps_its_old_schedule(self):
        res, calls = self.run_with([FakeResp(429, text="rate")] * 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.clk.sleeps, [10, 20])
        self.assertEqual(res["source"], "template")

    def test_retry_after_is_honoured_up_to_the_cap(self):
        res, _ = self.run_with([FakeResp(503, text="x", headers={"Retry-After": "120"}),
                                gemini_ok()])
        self.assertEqual(self.clk.sleeps, [gc.TRANSIENT_WAIT_CAP])
        self.clk.sleeps = []
        self.run_with([FakeResp(503, text="x", headers={"Retry-After": "3"}), gemini_ok()])
        self.assertEqual(self.clk.sleeps, [3.0])

    def test_no_retry_when_the_budget_cannot_cover_it(self):
        budget = gc.RetryBudget(seconds=gc.TRANSIENT_WAIT + gc.RETRY_MIN_CALL_S - 1,
                                clock=self.clk.clock, sleep=self.clk.sleep)
        res, calls = self.run_with([FakeResp(503, text="x")], budget=budget)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.clk.sleeps, [])
        self.assertEqual(res["source"], "template")

    def test_one_budget_bounds_the_whole_run(self):
        """Three reports, every call fails after 20 s: retries stop once the
        shared budget is spent, and the retried calls' timeouts shrink to fit."""
        self.clk.post_cost = 20.0
        budget = gc.RetryBudget(clock=self.clk.clock, sleep=self.clk.sleep)
        all_calls = []
        for _ in range(3):
            _, calls = self.run_with([FakeResp(503, text="x")] * 2, budget=budget)
            all_calls.append(calls)
        self.assertEqual([len(c) for c in all_calls], [2, 2, 1])
        self.assertLessEqual(budget.spent, gc.RETRY_BUDGET_S)
        self.assertEqual(budget.retries, 2)
        self.assertEqual(all_calls[0][0]["timeout"], gc.REQUEST_TIMEOUT)
        self.assertEqual(all_calls[0][1]["timeout"], gc.REQUEST_TIMEOUT)
        # 90 - 35 spent = 55 left; minus the 15 s wait = 40 s for the call
        self.assertEqual(all_calls[1][1]["timeout"], 40.0)
        self.assertEqual(budget.spent, 70.0)

    def test_gemini_without_a_key_makes_no_request(self):
        with mock.patch.object(gc, "GEMINI_API_KEY", ""):
            res, calls = self.run_with([])
        self.assertEqual(calls, [])
        self.assertEqual(res["source"], "template")
        self.assertIn("not configured", res["error"])
        self.assertEqual(res["attempts"], 0)

    def test_groq_is_the_second_provider(self):
        with mock.patch.object(gc, "GROQ_API_KEY", "groq-key-123456"):
            res, calls = self.run_with([FakeResp(400, text="bad"), groq_ok()])
        self.assertEqual(res["source"], "groq")
        self.assertEqual(res["model"], gc.GROQ_MODEL)
        self.assertEqual(res["attempts"], 2)
        self.assertIn("HTTP 400", res["error"])
        sysmsg = calls[1]["json"]["messages"][0]
        self.assertEqual(sysmsg["role"], "system")
        self.assertEqual(sysmsg["content"], pb.system_instruction())

    def test_groq_quota_exhausted_is_not_retried(self):
        with mock.patch.object(gc, "GROQ_API_KEY", "groq-key-123456"):
            res, calls = self.run_with([FakeResp(400, text="bad"),
                                        FakeResp(429, text="daily quota")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.clk.sleeps, [])
        self.assertEqual(res["source"], "template")

    def test_gemini_gets_the_system_instruction_once(self):
        _, calls = self.run_with([gemini_ok()])
        body = calls[0]["json"]
        self.assertEqual(body["system_instruction"]["parts"][0]["text"],
                         pb.system_instruction())
        self.assertNotIn("You are a senior", body["contents"][0]["parts"][0]["text"])

    def test_error_is_short_ascii_and_masks_the_key(self):
        res, _ = self.run_with([FakeResp(400, text="bad key gem-key-123456 \u00e9 " + "x" * 400)])
        err = res["error"]
        self.assertLessEqual(len(err), gc.ERROR_MAX)
        self.assertTrue(all(ord(c) < 128 for c in err))
        self.assertNotIn("gem-key-123456", err)

    def test_generate_report_returns_the_text(self):
        with mock.patch.object(gc.requests, "post",
                               side_effect=[gemini_ok("one"), gemini_ok("one")]):
            text = gc.generate_report(two_rows())
            meta = gc.generate_report_meta(two_rows())
        self.assertIsInstance(text, str)
        self.assertEqual(text, meta["text"])

    def test_an_unexpected_provider_error_still_gives_the_template(self):
        res, calls = self.run_with([RuntimeError("bug in a provider path")])
        self.assertEqual(res["source"], "template")
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["text"].split("\n")[0], raw_text()["template_header"])
        self.assertIn("bug in a provider path", res["error"])

    def test_a_prompt_that_cannot_be_built_gives_the_template(self):
        with mock.patch.object(gc, "build_prompt", side_effect=ValueError("bad frame")):
            res, calls = self.run_with([])
        self.assertEqual(calls, [])
        self.assertEqual(res["source"], "template")
        self.assertIn("bad frame", res["error"])

    def test_nothing_to_summarise_raises(self):
        with self.assertRaises(gc.AIReportError):
            gc.generate_report_meta(pd.DataFrame())
        with self.assertRaises(gc.AIReportError):
            gc.generate_report_meta(None)
        self.assertIs(gc.GeminiError, gc.AIReportError)

    def test_sources_registry_matches_the_checker(self):
        from scanner import result_checks as rc
        self.assertEqual(tuple(gc.REPORT_SOURCES), tuple(rc.REPORT_SOURCES))


class MarketReports(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([
            {"Stock_ID": "2330", "Market": "TSE"},
            {"Stock_ID": "6426", "Market": "OTC"},
        ])

    def test_otc_first_one_budget_and_sources(self):
        import scan_headless as sh
        seen, pos = [], []

        def fake(df, budget=None, save=True):
            seen.append((sorted(df["Market"].unique()), budget))
            pos.append(list(df["List_Pos"]))
            return {"text": "t{}".format(len(seen)), "source": "template",
                    "model": "", "attempts": 2, "seconds": 1.0, "error": "e"}

        sources = {}
        with mock.patch.object(gc, "generate_report_meta", side_effect=fake):
            reports = sh.build_market_reports(self.frame(), sources=sources)
        self.assertEqual([m for m, _ in seen], [["OTC"], ["OTC", "TSE"], ["TSE"]])
        # the full-list position survives the split by market
        self.assertEqual(pos, [[2], [1, 2], [1]])
        self.assertEqual(len({id(b) for _, b in seen}), 1)
        self.assertIsInstance(seen[0][1], gc.RetryBudget)
        # generated OTC first, published in the usual ALL / OTC / TSE order
        self.assertEqual(list(reports), ["ALL", "OTC", "TSE"])
        self.assertEqual(set(sources), set(reports))
        self.assertTrue(all(isinstance(v, str) for v in reports.values()))
        self.assertEqual(sources["OTC"], {"source": "template", "model": "",
                                          "attempts": 2, "seconds": 1.0,
                                          "error": "e"})

    def test_a_failing_view_is_skipped_and_sources_is_optional(self):
        import scan_headless as sh

        def fake(df, budget=None, save=True):
            if set(df["Market"]) == {"TSE"}:
                raise RuntimeError("boom")
            return {"text": "ok", "source": "gemini", "model": "m",
                    "attempts": 1, "seconds": 0.5, "error": ""}

        with mock.patch.object(gc, "generate_report_meta", side_effect=fake):
            reports = sh.build_market_reports(self.frame())
        self.assertEqual(set(reports), {"ALL", "OTC"})


    def test_no_unreachable_code_after_the_return(self):
        import inspect
        import scan_headless as sh
        src = inspect.getsource(sh.build_market_reports)
        self.assertNotIn("generate_report(", src)
        last = src.rstrip().splitlines()[-1].strip()
        self.assertTrue(last.startswith("return"), last)


class Payload(unittest.TestCase):
    def test_export_carries_report_sources_next_to_reports(self):
        import scanner.result_export as ex
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(ex, "MOBILE_DIR", root), \
                 mock.patch.object(ex, "MOBILE_DATA_FILE", root / "data.json"), \
                 mock.patch.object(ex, "MOBILE_QUOTES_FILE", root / "quotes.json"):
                df = pd.DataFrame([{"Stock_ID": "1111", "Market": "OTC",
                                    "Close_Price": 100.0, "Data_Date": "2026-10-07"}])
                ex.export_scan_result_json(
                    df, "mode_prelaunch", "2026-10-07 15:00:00",
                    reports={"ALL": "x"},
                    report_sources={"ALL": {"source": "template", "attempts": 2},
                                    "OTC": {"source": "gemini"}})
                got = json.loads((root / "data.json").read_text(encoding="utf-8"))
                self.assertEqual(got["meta"]["reports"], {"ALL": "x"})
                self.assertEqual(got["meta"]["report_sources"],
                                 {"ALL": {"source": "template", "attempts": 2}})
                ex.export_scan_result_json(df, "mode_prelaunch", "2026-10-07 15:01:00")
                got = json.loads((root / "data.json").read_text(encoding="utf-8"))
                self.assertEqual(got["meta"]["report_sources"], {})
                self.assertEqual(got["meta"]["reports"], {})


class Checks(unittest.TestCase):
    def check(self, sources, reports=None):
        p = clean_payload()
        if reports is not None:
            p["meta"]["reports"] = reports
        if sources is not None:
            p["meta"]["report_sources"] = sources
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def test_template_report_is_info(self):
        rep = self.check({"ALL": {"source": "template"}, "OTC": {"source": "gemini"}})
        self.assertIn("report_template", codes(rep, "info"))
        self.assertNotIn("report_template", codes(rep, "warn"))
        self.assertNotIn("report_sources_shape", codes(rep))
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["warnings"], 0)

    def test_old_payload_without_sources_is_not_judged(self):
        rep = self.check(None)
        self.assertNotIn("report_template", codes(rep))
        self.assertNotIn("report_sources_shape", codes(rep))

    def test_bad_sources_warn(self):
        rep = self.check({"ALL": {"source": "bogus"}, "OTC": {"source": "gemini"}})
        self.assertIn("report_sources_shape", codes(rep, "warn"))
        rep = self.check({"ALL": {"source": "gemini"}})       # OTC missing
        self.assertIn("report_sources_shape", codes(rep, "warn"))
        rep = self.check(["gemini"])
        self.assertIn("report_sources_shape", codes(rep, "warn"))
        self.assertNotEqual(rep["status"], "fail")

    def test_history_counts_the_sources(self):
        p = clean_payload()
        p["meta"]["report_sources"] = {"ALL": {"source": "gemini"},
                                       "OTC": {"source": "template"}}
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        with tempfile.TemporaryDirectory() as d:
            hist = os.path.join(d, "scan_checks.json")
            append_history(hist, rep, p["meta"])
            with open(hist, encoding="utf-8") as f:
                run = json.load(f)["runs"][-1]
        self.assertEqual(run["report_sources"], {"ALL": "gemini", "OTC": "template"})
        self.assertNotIn("report_template", run["codes"])


if __name__ == "__main__":
    unittest.main()
