"""
Tests for the trade-restriction lists (scanner/trade_restrictions.py,
DECISIONS "Trade restrictions", plan P0-1).

Every endpoint answer comes from tests/fixtures/restrictions/, recorded on
2026-10-08 for the 2026-10-07 session (`python -m scanner.trade_restrictions
--session 2026-10-07 --record tests/fixtures/restrictions`). No test touches
the network: requests.get is patched to fail for the whole module.

The acceptance case: the 2026-10-07 published payload (8227 OTC and 6533 TSE
on the list, five dropped-out names) annotated with the recorded lists --
8227 and 6533 in disposition with their period, matching interval and
prepayment terms; Buy_Ready unchanged under the default blocking set; 8227
blocked ('restricted') the moment the set names 'disposition'.

    python -m unittest tests.test_trade_restrictions -v
"""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import scanner.market_regime as market_regime
import scanner.trade_restrictions as tr
from scanner.scan_mode import mark_buy_ready

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "restrictions"
MODE = "mode_prelaunch"
SESSION = "2026-10-07"

DISPO_COLS = list(tr.RESTRICTION_COLUMNS)

# The real reader of data/company_events.json. Every test runs with it stubbed
# to "no ex-dates" (the cache rolls daily, and a test must not depend on which
# names happened to go ex on 2026-10-07); ExDayLimits uses this one on temp
# files.
REAL_LOAD_EX_TODAY = tr.load_ex_today


def no_ex_today(session=None, path=None):
    return {}


def _fixture(name):
    return json.loads((FIX / (name + ".json")).read_text(encoding="utf-8"))


def _body(name):
    return _fixture(name)["body"]


def _recorded():
    out = {}
    for fn in sorted(os.listdir(FIX)):
        if fn.endswith(".json") and not fn.startswith("payload"):
            rec = json.loads((FIX / fn).read_text(encoding="utf-8"))
            out[rec["url"]] = rec
    return out


RECORDED = _recorded()


def fake_get(drop=(), replace=None):
    """A _get_json that serves the recorded answers by URL. `drop` URLs fail
    (None, None); `replace` maps a URL to a substitute body."""
    replace = replace or {}

    def get(url, params=None, **kw):
        if url in drop:
            return None, None
        if url in replace:
            return replace[url], None
        rec = RECORDED.get(url)
        if rec is None:
            return None, None
        return copy.deepcopy(rec["body"]), rec.get("last_modified")
    return get


def fetch_recorded(session=SESSION, **kw):
    budget = kw.pop("budget_s", tr.FETCH_BUDGET_S)
    with mock.patch.object(tr, "_get_json", fake_get(**kw)):
        return tr.fetch_restrictions(session, budget_s=budget)


def payload():
    return _fixture("payload_20261007_rows")


def by_sid(df):
    return {str(r["Stock_ID"]): r for r in df.to_dict("records")}


def row(sid="9999", market="OTC", prev=100.0, close=101.0, **kw):
    r = {"Stock_ID": sid, "Market": market, "Data_Date": SESSION,
         "Close_Prev": prev, "Close_Price": close, "Recent_Jump": False}
    r.update(kw)
    return r


class NoNetwork(unittest.TestCase):
    """Every test in this module runs with requests.get patched to fail."""

    def setUp(self):
        def refuse(*a, **kw):
            raise AssertionError("network call in a test: {}".format(a[:1]))
        self._net = mock.patch.object(tr.requests, "get", refuse)
        self._net.start()
        self._ex = mock.patch.object(tr, "load_ex_today", no_ex_today)
        self._ex.start()

    def tearDown(self):
        self._ex.stop()
        self._net.stop()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
class Helpers(NoNetwork):
    def test_roc_and_gregorian_dates(self):
        for raw in ("115/10/02", "115.10.02", "115-10-02", "1151002",
                    "20261002", "2026-10-02"):
            self.assertEqual(tr._roc_iso(raw), "2026-10-02", raw)
        for raw in (None, "", "abc", "115/02/30", "115/10", "12345"):
            self.assertIsNone(tr._roc_iso(raw), raw)

    def test_periods_with_either_tilde(self):
        self.assertEqual(tr._period("1151002~1151008"),
                         ("2026-10-02", "2026-10-08"))
        self.assertEqual(tr._period(u"115/10/06\uff5e115/10/13"),
                         ("2026-10-06", "2026-10-13"))
        self.assertIsNone(tr._period("1151008~1151002"))     # reversed
        self.assertIsNone(tr._period("1151002"))
        self.assertIsNone(tr._period(None))

    def test_match_minutes_any_numerals(self):
        yue_mei, fen_zhong = u"\u7d04\u6bcf", u"\u5206\u9418"
        self.assertEqual(tr.match_minutes(yue_mei + u"\u4e8c" + fen_zhong), 2)
        self.assertEqual(tr.match_minutes(yue_mei + "5" + fen_zhong), 5)
        self.assertEqual(tr.match_minutes(yue_mei + u"\uff15" + fen_zhong), 5)
        self.assertEqual(tr.match_minutes(yue_mei + u"\u5341" + fen_zhong), 10)
        self.assertEqual(
            tr.match_minutes(yue_mei + u"\u4e8c\u5341\u4e94" + fen_zhong), 25)
        # two intervals named: the strictest
        self.assertEqual(tr.match_minutes(
            yue_mei + "10" + fen_zhong + " ... " + yue_mei + "20" + fen_zhong), 20)
        self.assertIsNone(tr.match_minutes(""))
        self.assertIsNone(tr.match_minutes(None))

    def test_prepay_terms(self):
        threshold = u"\u55ae\u7b46\u9054\u5341\u4ea4\u6613\u55ae\u4f4d"
        self.assertEqual(tr.prepay_terms("x " + threshold + " y"), "threshold")
        self.assertEqual(tr.prepay_terms("every order prepaid"), "all")
        self.assertIsNone(tr.prepay_terms(""))
        self.assertIsNone(tr.prepay_terms(None))

    def test_codes(self):
        self.assertEqual(tr._code(" 8227 "), "8227")
        self.assertEqual(tr._code("00631L"), "00631L")
        self.assertEqual(tr._code(""), "")
        self.assertEqual(tr._code("8227(../link)"), "")

    def test_severity_picks_the_worst_kind(self):
        self.assertEqual(tr.kind_of({"attention", "disposition", "limit_lock"}),
                         "disposition")
        self.assertEqual(tr.kind_of({"disposition", "suspended"}), "suspended")
        self.assertEqual(tr.kind_of({"unknown", "attention"}), "unknown")
        self.assertEqual(tr.kind_of({"limit_lock", "unknown"}), "limit_lock")
        self.assertEqual(tr.kind_of({"limit_down"}), "none")
        self.assertEqual(tr.kind_of(set()), "none")
        self.assertEqual(tr.RESTRICTION_KINDS[0], "suspended")
        self.assertEqual(tr.RESTRICTION_KINDS[-1], "none")

    def test_default_blocking_set_is_suspended_only(self):
        self.assertEqual(tr.BLOCKING_RESTRICTIONS, ("suspended",))


# --------------------------------------------------------------------------
# parsers on the recorded answers
# --------------------------------------------------------------------------
class Parsers(NoNetwork):
    def test_tpex_disposal_openapi(self):
        got, st = tr.parse_tpex_disposal(_body("tpex_disposal_openapi"))
        self.assertEqual((st["raw"], st["rows"], st["placeholder"]), (28, 28, 0))
        self.assertEqual(got["8227"], [{
            "board": "OTC", "start": "2026-10-02", "end": "2026-10-08",
            "announced": "2026-10-01", "match_min": 2, "prepay": "all"}])
        self.assertEqual(got["3441"][0]["start"], "2026-10-08")
        # TPEX first dispositions carry the 10-lot threshold too
        self.assertEqual(got["4760"][0]["prepay"], "threshold")
        # a re-disposition stacked on the first: both periods kept
        self.assertEqual(sorted(p["match_min"] for p in got["8084"]), [10, 25])

    def test_twse_punish_openapi(self):
        got, st = tr.parse_twse_punish(_body("twse_punish_openapi"))
        self.assertEqual(st["rows"], 12)
        self.assertEqual(got["6533"], [{
            "board": "TSE", "start": "2026-10-06", "end": "2026-10-13",
            "announced": "2026-10-05", "match_min": 2, "prepay": "threshold"}])
        self.assertEqual(got["3055"][0]["prepay"], "all")

    def test_web_fallbacks_agree_with_the_openapi(self):
        web, st = tr.parse_tpex_disposal_web(_body("tpex_disposal_web"))
        api, _ = tr.parse_tpex_disposal(_body("tpex_disposal_openapi"))
        self.assertGreater(st["rows"], 0)
        self.assertEqual(web["8227"], api["8227"])
        self.assertEqual(web["3441"], api["3441"])
        web, st = tr.parse_twse_punish_web(_body("twse_punish_web"))
        api, _ = tr.parse_twse_punish(_body("twse_punish_openapi"))
        self.assertGreater(st["rows"], 0)
        self.assertEqual(web["6533"], api["6533"])
        self.assertEqual(web["6672"], api["6672"])

    def test_attention_lists(self):
        got, st = tr.parse_tpex_warning(_body("tpex_warning_openapi"))
        self.assertEqual(st["rows"], 50)
        self.assertEqual(got["8111"], SESSION)
        self.assertEqual(got["3441"], SESSION)
        got, st = tr.parse_twse_notice(_body("twse_notice_web"))
        self.assertGreater(st["rows"], 100)
        self.assertEqual(got["3605"], SESSION)
        self.assertEqual(got["6533"], "2026-10-05")     # not on the latest list

    def test_altered_and_suspended(self):
        got, _ = tr.parse_tpex_cmode(_body("tpex_cmode_openapi"))
        self.assertEqual(got["2067"], "suspended")
        self.assertEqual(got["3064"], "altered")
        self.assertEqual(got["8084"], "altered")
        got, st = tr.parse_twse_altered(_body("twse_twt85u_openapi"))
        self.assertEqual(st["rows"], 10)
        self.assertEqual(got["1213"], "altered")

    def test_halts_on_the_next_session_only(self):
        body = _body("twse_twtawu_openapi")         # 1218 halted 08-13 -> 08-14
        self.assertEqual(tr.parse_twse_halts(body, "2026-08-13")[0],
                         {"1218": "suspended"})
        self.assertEqual(tr.parse_twse_halts(body, "2026-08-14")[0], {})
        self.assertEqual(tr.parse_twse_halts(body, "2026-10-08")[0], {})
        self.assertEqual(tr.parse_twse_halts(body, "2026-08-12")[0], {})

    def test_empty_placeholder_and_shape(self):
        # an empty list and a blank placeholder row are "nobody", not errors
        self.assertEqual(tr._parse_checked(tr.parse_tpex_disposal, []), ({}, None))
        blank = [{"SecuritiesCompanyCode": "", "DispositionPeriod": ""}]
        self.assertEqual(tr._parse_checked(tr.parse_tpex_disposal, blank), ({}, None))
        # renamed fields, an HTML page, nothing at all: visible failures
        self.assertEqual(tr._parse_checked(tr.parse_tpex_disposal, [{"Foo": 1}]),
                         (None, "shape"))
        self.assertEqual(tr._parse_checked(tr.parse_twse_punish, [{"Foo": 1}]),
                         (None, "shape"))
        self.assertEqual(tr._parse_checked(tr.parse_tpex_cmode, [{"Foo": 1}]),
                         (None, "shape"))
        self.assertEqual(tr._parse_checked(tr.parse_tpex_disposal, "<html>"),
                         (None, "shape"))
        self.assertEqual(tr._parse_checked(tr.parse_tpex_disposal, None),
                         (None, "fetch"))
        # every row unparseable (a new period format): a shape change
        bad = [{"SecuritiesCompanyCode": "8227", "DispositionPeriod": "soon"}]
        self.assertEqual(tr._parse_checked(tr.parse_tpex_disposal, bad),
                         (None, "shape"))

    def test_web_no_data_answers(self):
        no_data = u"\u5f88\u62b1\u6b49\uff0c\u6c92\u6709\u7b26\u5408\u689d\u4ef6\u7684\u8cc7\u6599!"
        self.assertEqual(tr.parse_twse_punish_web({"stat": no_data})[0], {})
        self.assertEqual(tr.parse_twse_notice({"stat": no_data})[0], {})
        self.assertIsNone(tr.parse_twse_punish_web({"stat": "error"})[0])
        self.assertEqual(tr.parse_tpex_disposal_web({"stat": "ok", "tables": []})[0], {})
        self.assertIsNone(tr.parse_tpex_disposal_web(
            {"stat": "ok", "tables": [{"fields": ["a", "b"], "data": [[1, 2]]}]})[0])


# --------------------------------------------------------------------------
# fetch with fallbacks (recorded answers, no network)
# --------------------------------------------------------------------------
class Fetch(NoNetwork):
    def test_everything_from_the_openapi(self):
        info = fetch_recorded()
        self.assertTrue(info["ok"])
        self.assertEqual(info["session"], SESSION)
        self.assertEqual(info["next_session"], "2026-10-08")
        self.assertEqual(info["boards"]["OTC"]["source"], "openapi")
        self.assertEqual(info["boards"]["OTC"]["rows"], 28)
        self.assertEqual(info["boards"]["TSE"]["rows"], 12)
        self.assertEqual(info["boards"]["OTC"]["last_modified"],
                         "Wed, 07 Oct 2026 15:30:10 GMT")
        self.assertEqual(info["attention_ok"], {"OTC": True, "TSE": True})
        self.assertEqual(info["altered_ok"], {"OTC": True, "TSE": True})
        self.assertEqual(info["attention_latest"], {"OTC": SESSION, "TSE": SESSION})
        self.assertEqual(info["altered"]["2067"], "suspended")
        self.assertEqual(info["altered"]["1213"], "altered")
        json.dumps(info)

    def test_primary_down_reads_the_web_bulletin(self):
        info = fetch_recorded(drop=(tr.TPEX_DISPOSAL_URL, tr.TWSE_PUNISH_URL))
        self.assertTrue(info["ok"])
        for b in ("OTC", "TSE"):
            self.assertEqual(info["boards"][b]["source"], "web", b)
            self.assertEqual(info["boards"][b]["primary_error"], "fetch", b)
        self.assertEqual(info["disposition"]["8227"][0]["end"], "2026-10-08")
        self.assertEqual(info["disposition"]["6533"][0]["prepay"], "threshold")

    def test_both_down_fails_the_board_only(self):
        info = fetch_recorded(drop=(tr.TPEX_DISPOSAL_URL, tr.TPEX_DISPOSAL_WEB_URL))
        self.assertFalse(info["ok"])
        self.assertFalse(info["boards"]["OTC"]["ok"])
        self.assertEqual(info["boards"]["OTC"]["error"], "fetch")
        self.assertTrue(info["boards"]["TSE"]["ok"])
        self.assertNotIn("8227", info["disposition"])

    def test_a_shape_change_is_not_an_empty_list(self):
        info = fetch_recorded(drop=(tr.TWSE_PUNISH_WEB_URL,),
                              replace={tr.TWSE_PUNISH_URL: [{"Foo": "1"}]})
        self.assertFalse(info["boards"]["TSE"]["ok"])
        self.assertEqual(info["boards"]["TSE"]["primary_error"], "shape")

    def test_best_effort_lists_never_fail_a_board(self):
        info = fetch_recorded(drop=(tr.TPEX_WARNING_URL, tr.TPEX_CMODE_URL,
                                    tr.TWSE_HALT_URL))
        self.assertTrue(info["ok"])
        self.assertEqual(info["attention_ok"], {"OTC": False, "TSE": True})
        self.assertEqual(info["altered_ok"], {"OTC": False, "TSE": False})
        self.assertNotIn("2067", info["altered"])

    def test_budget_spent_skips_the_extras(self):
        info = fetch_recorded(budget_s=0)
        self.assertTrue(info["ok"])                 # the primaries still ran
        self.assertEqual(info["attention_ok"], {"OTC": False, "TSE": False})
        self.assertFalse(info["altered_ok"]["TSE"])

    def test_never_raises(self):
        def boom(*a, **kw):
            raise RuntimeError("socket on fire")
        with mock.patch.object(tr, "_get_json", boom):
            info = tr.fetch_restrictions(SESSION)
        self.assertFalse(info["ok"])
        self.assertIn("RuntimeError", info["error"])

    def test_a_bad_session_never_raises(self):
        # verifier fix: empty_info -> next_session_after raised ValueError
        with mock.patch.object(tr, "_get_json", fake_get()):
            info = tr.fetch_restrictions("garbage")
        self.assertFalse(info["ok"])
        self.assertIn("error", info)
        self.assertIsNone(tr.next_session_after("garbage"))

    def test_an_empty_primary_is_cross_checked(self):
        # TPEX has served empty lists in outages: an empty openapi answer is
        # checked against the web bulletin, which wins when it has rows
        info = fetch_recorded(replace={tr.TPEX_DISPOSAL_URL: []})
        otc = info["boards"]["OTC"]
        self.assertTrue(otc["ok"])
        self.assertEqual((otc["source"], otc["primary_error"]), ("web", "empty"))
        self.assertEqual(info["disposition"]["8227"][0]["end"], "2026-10-08")
        # both empty: a real empty list, from the primary
        info = fetch_recorded(replace={tr.TPEX_DISPOSAL_URL: [],
                                       tr.TPEX_DISPOSAL_WEB_URL: {"stat": "ok", "tables": []}})
        otc = info["boards"]["OTC"]
        self.assertEqual((otc["ok"], otc["source"], otc["rows"]), (True, "openapi", 0))
        self.assertNotIn("primary_error", otc)
        # the web bulletin down: the empty primary stands
        info = fetch_recorded(replace={tr.TPEX_DISPOSAL_URL: []},
                              drop=(tr.TPEX_DISPOSAL_WEB_URL,))
        self.assertEqual((info["boards"]["OTC"]["ok"],
                          info["boards"]["OTC"]["source"]), (True, "openapi"))

    def test_primaries_first_and_the_rest_within_the_budget(self):
        calls = []
        real = fake_get(drop=(tr.TPEX_DISPOSAL_URL,))

        def spy(url, params=None, **kw):
            calls.append((url, kw))
            return real(url, params, **kw)
        with mock.patch.object(tr, "_get_json", spy):
            info = tr.fetch_restrictions(SESSION, budget_s=5)
        urls = [u for u, _ in calls]
        self.assertEqual(urls[:2], [tr.TPEX_DISPOSAL_URL, tr.TWSE_PUNISH_URL])
        self.assertEqual(info["boards"]["OTC"]["source"], "web")
        for url, kw in calls[2:]:
            # every non-primary call is cut to the budget, one try
            self.assertLessEqual(kw.get("timeout", 99), 5, url)
            self.assertEqual(kw.get("tries"), 1, url)
        for url, kw in calls[:2]:
            self.assertNotIn("timeout", kw, url)       # the full default try

    def test_get_json_is_never_the_fail_open_helper(self):
        """market_filter._fetch_json returns [] on failure -- an empty list
        here would mark every name 'none'. _get_json returns (None, None)."""
        def refuse(*a, **kw):
            raise IOError("down")
        with mock.patch.object(tr.requests, "get", refuse), \
                mock.patch.object(tr.time, "sleep", lambda s: None):
            self.assertEqual(tr._get_json("http://x.invalid/", tries=2), (None, None))


# --------------------------------------------------------------------------
# annotate
# --------------------------------------------------------------------------
class Annotate(NoNetwork):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(tr, "_get_json", fake_get()), \
                mock.patch.object(tr, "load_ex_today", no_ex_today):
            cls.info = tr.fetch_restrictions(SESSION)

    def annotate(self, rows, info="default", session=SESSION):
        info = self.info if info == "default" else info
        return tr.annotate_restrictions(pd.DataFrame(rows), info, session)

    def test_payload_rows_on_1007(self):
        out = by_sid(self.annotate(payload()["rows"]))
        r = out["8227"]
        self.assertEqual(r["Trade_Restriction"], "disposition")
        self.assertEqual(r["Restriction_Flags"], "disposition")
        self.assertEqual((r["Restriction_Since"], r["Restriction_Until"]),
                         ("2026-10-02", "2026-10-08"))
        self.assertEqual((r["Restriction_Match_Min"], r["Restriction_Prepay"]),
                         (2, "all"))
        r = out["6533"]
        self.assertEqual(r["Trade_Restriction"], "disposition")
        self.assertEqual((r["Restriction_Since"], r["Restriction_Until"]),
                         ("2026-10-06", "2026-10-13"))
        self.assertEqual((r["Restriction_Match_Min"], r["Restriction_Prepay"]),
                         (2, "threshold"))

    def test_tracked_rows_on_1007(self):
        out = by_sid(self.annotate(payload()["tracked"]))
        self.assertEqual(out["3055"]["Trade_Restriction"], "disposition")
        self.assertEqual(out["3055"]["Restriction_Until"], "2026-10-12")
        self.assertEqual(out["6672"]["Trade_Restriction"], "disposition")
        self.assertEqual(out["6672"]["Restriction_Flags"],
                         "attention,disposition,limit_lock")
        # announced on the 7th, in force from the next session
        self.assertEqual(out["3441"]["Trade_Restriction"], "disposition")
        self.assertEqual(out["3441"]["Restriction_Since"], "2026-10-08")
        self.assertEqual(out["3441"]["Restriction_Flags"], "attention,disposition")
        self.assertEqual(out["1301"]["Trade_Restriction"], "limit_lock")
        self.assertEqual(out["6538"]["Trade_Restriction"], "limit_lock")
        self.assertIsNone(out["6538"]["Restriction_Until"])

    def test_a_period_ending_on_the_scan_day_is_over_by_the_next(self):
        # the 10-08 scan trades on 10-09; 8227's period ends 10-08
        r = by_sid(self.annotate([row("8227", Data_Date="2026-10-08")],
                                 session="2026-10-08"))["8227"]
        self.assertEqual(r["Trade_Restriction"], "none")
        self.assertIsNone(r["Restriction_Until"])

    def test_the_strictest_terms_when_periods_stack(self):
        r = by_sid(self.annotate([row("8084")]))["8084"]
        self.assertEqual(r["Trade_Restriction"], "disposition")
        self.assertEqual(r["Restriction_Match_Min"], 25)
        self.assertEqual((r["Restriction_Since"], r["Restriction_Until"]),
                         ("2026-10-02", "2026-10-14"))
        # altered too, but disposition is the more severe
        self.assertIn("altered", r["Restriction_Flags"].split(","))

    def test_suspended_and_altered(self):
        out = by_sid(self.annotate([row("2067"), row("3064"),
                                    row("1213", market="TSE")]))
        self.assertEqual(out["2067"]["Trade_Restriction"], "suspended")
        self.assertEqual(out["3064"]["Trade_Restriction"], "altered")
        self.assertEqual(out["1213"]["Trade_Restriction"], "altered")

    def test_limit_lock_on_the_quote_ladder(self):
        out = by_sid(self.annotate([
            row("1001", prev=337.0, close=370.5),     # 370.7 -> 370.5
            row("1002", prev=68.6, close=75.4),       # 75.46 -> 75.4
            row("1003", prev=302.0, close=332.0),     # 332.2 -> 332.0
            row("1004", prev=68.6, close=75.3),       # one tick short
            row("1005", prev=337.0, close=370.5, Recent_Jump=True),
            row("1006", prev=100.0, close=90.0),      # limit-down: display only
            row("1007", prev=None, close=90.0),
        ]))
        for sid in ("1001", "1002", "1003"):
            self.assertEqual(out[sid]["Trade_Restriction"], "limit_lock", sid)
        for sid in ("1004", "1005", "1007"):
            self.assertEqual(out[sid]["Trade_Restriction"], "none", sid)
        self.assertEqual(out["1006"]["Trade_Restriction"], "none")
        self.assertEqual(out["1006"]["Restriction_Flags"], "limit_down")

    def test_a_failed_board_is_unknown_for_that_board_only(self):
        info = copy.deepcopy(self.info)
        info["boards"]["OTC"]["ok"] = False
        out = by_sid(self.annotate(payload()["rows"], info=info))
        self.assertEqual(out["8227"]["Trade_Restriction"], "unknown")
        self.assertEqual(out["8227"]["Restriction_Flags"], "unknown")
        self.assertIsNone(out["8227"]["Restriction_Until"])
        self.assertEqual(out["6533"]["Trade_Restriction"], "disposition")

    def test_no_info_is_unknown_but_the_bar_still_speaks(self):
        out = by_sid(self.annotate(payload()["tracked"], info=None))
        self.assertEqual(out["3055"]["Trade_Restriction"], "unknown")
        self.assertEqual(out["6538"]["Trade_Restriction"], "limit_lock")
        self.assertEqual(out["6538"]["Restriction_Flags"], "limit_lock,unknown")

    def test_an_internal_error_is_unknown_not_a_crash(self):
        out = self.annotate(payload()["rows"], info={"boards": "garbage"})
        self.assertEqual(set(out["Trade_Restriction"]), {"unknown"})

    def test_never_empty(self):
        odd = [row("8227"), {"Stock_ID": None, "Market": None},
               {"Stock_ID": "", "Close_Price": float("nan")},
               row("5555", market="EMG", prev="x", close="y")]
        out = self.annotate(odd)
        for v in out["Trade_Restriction"]:
            self.assertIn(v, tr.RESTRICTION_KINDS)
        self.assertEqual(self.annotate([]).columns.tolist()[-6:], DISPO_COLS)
        self.assertIsNone(tr.annotate_restrictions(None, self.info))

    def test_a_frame_without_dates_uses_the_lists_session(self):
        # verifier fix: no Data_Date column read every period as over ('none')
        rows = [{"Stock_ID": "8227", "Market": "OTC"},
                {"Stock_ID": "6533", "Market": "TSE"}]
        out = by_sid(tr.annotate_restrictions(pd.DataFrame(rows), self.info))
        self.assertEqual(out["8227"]["Trade_Restriction"], "disposition")
        self.assertEqual(out["8227"]["Restriction_Until"], "2026-10-08")
        self.assertEqual(out["6533"]["Trade_Restriction"], "disposition")

    def test_limit_flags_only_on_the_sessions_own_bar(self):
        # a lagging row's last bar closed at the limit some earlier day: that
        # is not "closed locked today"
        out = by_sid(self.annotate([
            row("1001", prev=337.0, close=370.5, Data_Date="2026-10-05"),
            row("1002", prev=337.0, close=370.5),
            row("1003", prev=100.0, close=90.0, Data_Date="2026-10-05"),
            row("1004", prev=337.0, close=370.5, Data_Date=None),
        ]))
        self.assertEqual(out["1001"]["Trade_Restriction"], "none")
        self.assertIsNone(out["1001"]["Restriction_Flags"])
        self.assertEqual(out["1002"]["Trade_Restriction"], "limit_lock")
        self.assertIsNone(out["1003"]["Restriction_Flags"])
        self.assertEqual(out["1004"]["Trade_Restriction"], "limit_lock")
        # and the checker agrees (no limit_lock_mismatch on the lagging row)
        from scanner.result_checks import check_payload
        recs = json.loads(self.annotate([
            row("1001", prev=337.0, close=370.5, Data_Date="2026-10-05")
        ]).to_json(orient="records"))
        rep = check_payload({"meta": {"data_date": SESSION, "session_date": SESSION},
                             "rows": recs})
        self.assertNotIn("limit_lock_mismatch", {i["code"] for i in rep["items"]})

    def test_input_frame_untouched_and_session_fallback(self):
        df = pd.DataFrame(payload()["rows"])
        before = df.columns.tolist()
        a = tr.annotate_restrictions(df, self.info)        # newest Data_Date
        b = tr.annotate_restrictions(df, self.info, SESSION)
        self.assertEqual(df.columns.tolist(), before)
        pd.testing.assert_frame_equal(a, b)
        self.assertEqual(tr.session_of(df), SESSION)


# --------------------------------------------------------------------------
# the buy gate
# --------------------------------------------------------------------------
def frame(**overrides):
    r = {"Stock_ID": "8069", "Stock_Name": "TestCo", "Market": "OTC",
         "Data_Date": SESSION, "Close_Price": 100.0, "Core_Plus": True,
         "Integrity_OK": True, "Hold_Status": "pending", "Launch_Score": 80.0}
    r.update(overrides)
    return pd.DataFrame([r])


class BuyGate(NoNetwork):
    def setUp(self):
        super().setUp()
        self._real = market_regime.get_market_regime
        market_regime.get_market_regime = lambda: {
            "ok": True, "enter_ok": True, "risk_on": True, "is_current": True}

    def tearDown(self):
        market_regime.get_market_regime = self._real
        super().tearDown()

    def gate(self, df):
        out = mark_buy_ready(df, MODE, session_date=SESSION)
        return bool(out["Buy_Ready"].iloc[0]), out["Buy_Block"].iloc[0]

    def test_suspended_is_restricted(self):
        self.assertEqual(self.gate(frame(Trade_Restriction="suspended")),
                         (False, "restricted"))

    def test_display_kinds_do_not_block_by_default(self):
        for kind in ("disposition", "altered", "limit_lock", "attention",
                     "unknown", "none"):
            self.assertEqual(self.gate(frame(Trade_Restriction=kind)), (True, ""), kind)
        self.assertEqual(self.gate(frame()), (True, ""))            # no column
        self.assertEqual(self.gate(frame(Trade_Restriction=None)), (True, ""))

    def test_lowest_priority(self):
        s = "suspended"
        self.assertEqual(self.gate(frame(Trade_Restriction=s, Market="TSE")),
                         (False, "market"))
        self.assertEqual(self.gate(frame(Trade_Restriction=s, Integrity_OK=False)),
                         (False, "integrity"))
        self.assertEqual(self.gate(frame(Trade_Restriction=s, Core_Plus=False)),
                         (False, "quality"))
        self.assertEqual(self.gate(frame(Trade_Restriction=s, Hold_Status="holding")),
                         (False, "held"))
        market_regime.get_market_regime = lambda: {
            "ok": True, "enter_ok": False, "risk_on": False, "is_current": True}
        self.assertEqual(self.gate(frame(Trade_Restriction=s)), (False, "regime"))

    def test_flipping_the_constant_is_the_whole_change(self):
        with mock.patch.object(tr, "BLOCKING_RESTRICTIONS",
                               ("suspended", "disposition")):
            self.assertEqual(self.gate(frame(Trade_Restriction="disposition")),
                             (False, "restricted"))
            self.assertEqual(self.gate(frame(Trade_Restriction="attention")),
                             (True, ""))
        with mock.patch.object(tr, "BLOCKING_RESTRICTIONS", ("suspended", "unknown")):
            # not annotated at all reads as unknown
            self.assertEqual(self.gate(frame()), (False, "restricted"))
            self.assertEqual(self.gate(frame(Trade_Restriction="frozen")),
                             (False, "restricted"))

    def test_flipping_to_limit_lock_blocks_it(self):
        with mock.patch.object(tr, "BLOCKING_RESTRICTIONS",
                               ("suspended", "limit_lock")):
            self.assertEqual(self.gate(frame(Trade_Restriction="limit_lock")),
                             (False, "restricted"))
            self.assertEqual(self.gate(frame(Trade_Restriction="disposition")),
                             (True, ""))


class Acceptance(NoNetwork):
    """The 2026-10-07 published payload with the recorded 10-07 lists."""

    def setUp(self):
        super().setUp()
        self.p = payload()
        self._real = market_regime.get_market_regime
        reg = dict(self.p["regime"])
        market_regime.get_market_regime = lambda: dict(reg)
        self.info = fetch_recorded()
        self.rows = tr.annotate_restrictions(pd.DataFrame(self.p["rows"]),
                                             self.info, SESSION)

    def tearDown(self):
        market_regime.get_market_regime = self._real
        super().tearDown()

    def test_tagged_with_period_interval_and_prepay(self):
        out = by_sid(self.rows)
        self.assertEqual(
            {k: out["8227"][k] for k in DISPO_COLS},
            {"Trade_Restriction": "disposition", "Restriction_Flags": "disposition",
             "Restriction_Since": "2026-10-02", "Restriction_Until": "2026-10-08",
             "Restriction_Match_Min": 2, "Restriction_Prepay": "all"})
        self.assertEqual(
            {k: out["6533"][k] for k in DISPO_COLS},
            {"Trade_Restriction": "disposition", "Restriction_Flags": "disposition",
             "Restriction_Since": "2026-10-06", "Restriction_Until": "2026-10-13",
             "Restriction_Match_Min": 2, "Restriction_Prepay": "threshold"})

    def test_buy_ready_unchanged_under_the_default(self):
        out = mark_buy_ready(self.rows, MODE, session_date=SESSION)
        self.assertEqual(out["Buy_Ready"].tolist(),
                         [r["Buy_Ready"] for r in self.p["rows"]])
        self.assertEqual(out["Buy_Block"].tolist(),
                         [r["Buy_Block"] for r in self.p["rows"]])
        self.assertTrue(bool(by_sid(out)["8227"]["Buy_Ready"]))

    def test_flipping_to_disposition_blocks_8227(self):
        with mock.patch.object(tr, "BLOCKING_RESTRICTIONS",
                               ("suspended", "disposition")):
            out = by_sid(mark_buy_ready(self.rows, MODE, session_date=SESSION))
        self.assertFalse(bool(out["8227"]["Buy_Ready"]))
        self.assertEqual(out["8227"]["Buy_Block"], "restricted")
        # 6533 keeps its own, higher-priority reason
        self.assertEqual(out["6533"]["Buy_Block"], "market")

    def test_summary_and_checks_on_it(self):
        from scanner.result_checks import check_payload
        rows = mark_buy_ready(self.rows, MODE, session_date=SESSION)
        tracked = tr.annotate_restrictions(pd.DataFrame(self.p["tracked"]),
                                           self.info, SESSION)
        s = tr.summarize(self.info, rows, tracked)
        json.dumps(s)
        self.assertTrue(s["ok"])
        self.assertEqual(s["blocking"], ["suspended"])
        self.assertEqual((s["active"], s["blocked"], s["unknown"]), (2, 0, 0))
        self.assertEqual(s["counts"], {"disposition": 2})
        self.assertEqual(s["tracked_counts"],
                         {"disposition": 3, "limit_lock": 2})
        line = tr.describe(s)
        self.assertIn("OTC ok (openapi, 28)", line)
        self.assertIn("TSE ok (openapi, 12)", line)
        self.assertIn("2 disposition", line)
        # the 10-07 lists are fresh for a 10-07 scan
        rep = check_payload({"meta": {"data_date": SESSION, "session_date": SESSION,
                                      "calendar_tail": ["2026-10-05", "2026-10-06", SESSION],
                                      "quality": {"restrictions": s}},
                             "rows": []})
        codes = {i["code"] for i in rep["items"]}
        for c in ("restrictions_unchecked", "restrictions_feed_failed",
                  "restrictions_stale", "restrictions_fallback"):
            self.assertNotIn(c, codes)

    def test_summary_of_a_crashed_fetch(self):
        s = tr.summarize(None, self.rows)
        self.assertFalse(s["ok"])
        self.assertEqual(s["boards"]["OTC"]["error"], "fetch_crashed")
        self.assertIn("FAILED", tr.describe(s))


# --------------------------------------------------------------------------
# wiring: scan_headless, GUI worker, ledgers
# --------------------------------------------------------------------------
class Wiring(NoNetwork):
    def test_scan_headless_order(self):
        import inspect
        import scan_headless as sh
        src = inspect.getsource(sh.run_scan)
        steps = ["fetch_restrictions(session_date)",
                 "annotate_restrictions(result_df, restr_info, session_date)",
                 "mark_buy_ready(result_df", "record_picks(result_df",
                 "_data_health(result_df, session_date, degraded,"]
        at = [src.find(s) for s in steps]
        self.assertTrue(all(i >= 0 for i in at), dict(zip(steps, at)))
        self.assertEqual(at, sorted(at), dict(zip(steps, at)))
        i_tr = src.find("annotate_restrictions(tracked_df")
        self.assertGreater(i_tr, src.find("annotate_tracked(tracked_df"))
        self.assertLess(i_tr, at[-1])
        # read-only display: fetched on a degraded run too
        lead = src[:at[0]].rsplit("\n\n", 1)[-1]
        self.assertNotIn("degraded is None", lead)

    def test_data_health_carries_the_summary(self):
        import scan_headless as sh
        info = fetch_recorded()
        df = tr.annotate_restrictions(pd.DataFrame(payload()["rows"]), info, SESSION)
        h = sh._data_health(df, SESSION, None, restr_info=info)
        self.assertTrue(h["restrictions"]["ok"])
        self.assertEqual(h["restrictions"]["active"], 2)
        h = sh._data_health(df, SESSION, None, restr_info=None)
        self.assertFalse(h["restrictions"]["ok"])
        self.assertNotIn("restrictions", sh._data_health(df, SESSION, None))

    def test_gui_worker_mirrors_it(self):
        import re
        src = (ROOT / "gui" / "scan_worker.py").read_text(encoding="utf-8")
        flat = re.sub(r"\s+", " ", src)
        i_ann = flat.find("annotate_restrictions(result_df, restr_info, restr_session)")
        i_buy = flat.find("mark_buy_ready(result_df, self._scan_mode)")
        self.assertTrue(0 <= i_ann < i_buy, (i_ann, i_buy))
        # the tracked rows are judged against the LISTED rows' session, as
        # the headless scan does (verifier fix: they used their own dates)
        self.assertIn("annotate_restrictions(tracked_df, restr_info, restr_session)",
                      flat)
        self.assertIn("restr_session = session_of(result_df) or None", flat)
        self.assertIn("quality=quality", flat)

    def test_gate_snapshot_fields(self):
        from portfolio.sync import GATE_FIELDS
        self.assertIn("Trade_Restriction", GATE_FIELDS)
        self.assertIn("Restriction_Until", GATE_FIELDS)

    def test_signal_ledger_records_the_restriction(self):
        import scanner.signal_ledger as sl
        df = tr.annotate_restrictions(pd.DataFrame(payload()["rows"]),
                                      fetch_recorded(), SESSION)
        # record_picks leaves its connection to the garbage collector; on
        # Windows the file stays locked until it runs
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "signal_ledger.db"
            real = market_regime.get_market_regime
            market_regime.get_market_regime = lambda: {"ok": True}
            try:
                with mock.patch.object(sl, "SIGNAL_LEDGER_FILE", db):
                    self.assertEqual(sl.record_picks(df, MODE, scan_session=SESSION), 2)
            finally:
                market_regime.get_market_regime = real
            conn = sqlite3.connect(db)
            try:
                got = dict(conn.execute(
                    "SELECT stock_id, gate_detail FROM picks").fetchall())
            finally:
                conn.close()
            import gc
            gc.collect()
        g = json.loads(got["8227"])
        self.assertEqual(g["restriction"], "disposition")
        self.assertEqual(g["restriction_until"], "2026-10-08")

    def test_touched_backend_files_are_ascii(self):
        for rel in ("scanner/trade_restrictions.py", "scanner/scan_mode.py",
                    "scanner/result_checks.py", "scanner/live_record.py",
                    "scanner/signal_ledger.py", "portfolio/sync.py",
                    "scan_headless.py", "gui/scan_worker.py",
                    "tests/test_trade_restrictions.py"):
            raw = (ROOT / rel).read_bytes()
            self.assertFalse(any(b > 127 for b in raw), rel)


# --------------------------------------------------------------------------
# RE-1: the limit flags on an ex-dividend / ex-rights day
# --------------------------------------------------------------------------
EX_DIV = {"date": SESSION, "kind": "div", "cash": 1.0}


class ExDayLimits(NoNetwork):
    """On a name's ex-date the exchange computes the limits from the REDUCED
    reference price (previous close minus the cash dividend, floored to the
    cent), not from Close_Prev. Checked against TWSE TWT49U 2026 (1205 rows):
    the raw formula matched the official limit-up in 38, the reference-price
    formula in 1083 of the 1090 cash-only rows."""

    def flags(self, close, ex=EX_DIV, prev=62.6, sid="2947"):
        r = row(sid, prev=prev, close=close)
        return tr.limit_flags(r, sid, ex)

    def test_reference_price_is_floored_to_the_cent(self):
        self.assertEqual(tr.reference_price(62.6, 1.0), 61.6)      # not 61.59
        self.assertEqual(tr.reference_price(62.6, 0.881), 61.71)
        self.assertEqual(tr.reference_price(10.0, 10.0), None)
        self.assertEqual(tr.reference_price(10.0, 11.0), None)
        self.assertIsNone(tr.reference_price("x", 1.0))

    def test_2947_on_its_ex_date(self):
        # official TWT49U levels: up 67.70, down 55.50 (raw: 68.80 / 56.40)
        self.assertEqual(self.flags(67.7), (True, False))
        self.assertEqual(self.flags(55.5), (False, True))
        self.assertEqual(self.flags(60.5), (False, False))
        # the raw levels are no longer read as locks: they cannot be reached
        self.assertEqual(self.flags(68.8), (False, False))
        self.assertEqual(self.flags(56.4), (False, False))

    def test_without_the_entry_the_raw_close_is_used_as_before(self):
        self.assertEqual(self.flags(68.8, ex=None), (True, False))
        self.assertEqual(self.flags(56.4, ex=None), (False, True))
        self.assertEqual(self.flags(67.7, ex=None), (False, False))

    def test_an_ex_date_whose_reference_is_unknown_is_indeterminate(self):
        for ex in ({"kind": "right", "cash": None},
                   {"kind": "both", "cash": 1.0},
                   {"kind": "div", "cash": None},
                   {"kind": "div", "cash": 0},
                   {"kind": None, "cash": 1.0}, "junk"):
            for close in (67.7, 68.8, 55.5, 56.4):
                self.assertEqual(self.flags(close, ex=ex), (False, False),
                                 (ex, close))
        # a trust code (01xxxT) is off the cash-only rule
        self.assertEqual(self.flags(67.7, sid="01001T"), (False, False))

    def test_recent_jump_and_missing_prices_still_skip(self):
        r = row("2947", prev=62.6, close=67.7, Recent_Jump=True)
        self.assertEqual(tr.limit_flags(r, "2947", EX_DIV), (False, False))
        r = row("2947", prev=None, close=67.7)
        self.assertEqual(tr.limit_flags(r, "2947", EX_DIV), (False, False))

    def test_restriction_of_uses_the_map_for_the_bars_own_day_only(self):
        r = row("2947", prev=62.6, close=67.7)
        info = fetch_recorded()
        mk = lambda ex: tr.restriction_of(r, info, SESSION, None, ex)
        self.assertEqual(mk({"2947": EX_DIV})["Trade_Restriction"], "limit_lock")
        # no map / another id: raw close, 67.7 is below the raw limit-up
        self.assertEqual(mk({})["Trade_Restriction"], "none")
        self.assertEqual(mk({"1101": EX_DIV})["Trade_Restriction"], "none")
        # an older ex-date is already inside Close_Prev
        old = dict(EX_DIV, date="2026-10-05")
        self.assertEqual(mk({"2947": old})["Trade_Restriction"], "none")
        lock = row("2947", prev=62.6, close=68.8)
        self.assertEqual(tr.restriction_of(lock, info, SESSION, None,
                                           {})["Trade_Restriction"], "limit_lock")
        self.assertEqual(tr.restriction_of(lock, info, SESSION, None,
                                           {"2947": EX_DIV})["Trade_Restriction"],
                         "none")

    def test_restriction_of_reads_the_map_off_the_fetch_result(self):
        info = fetch_recorded()
        info["ex_today"] = {"2947": EX_DIV}
        out = tr.restriction_of(row("2947", prev=62.6, close=67.7), info,
                                SESSION, "2026-10-08")
        self.assertEqual(out["Trade_Restriction"], "limit_lock")

    def test_fetch_carries_the_ex_dates(self):
        with mock.patch.object(tr, "load_ex_today",
                               lambda s, path=None: {"2947": EX_DIV}):
            info = fetch_recorded()
        self.assertEqual(info["ex_today"], {"2947": EX_DIV})
        summ = tr.summarize(info)
        self.assertEqual(summ["ex_today"], {"date": SESSION, "ids": ["2947"]})
        # a fetch that never ran carries none
        self.assertNotIn("ex_today", tr.summarize(None))

    def test_a_crashed_fetch_still_reads_the_cache(self):
        df = pd.DataFrame([row("2947", prev=62.6, close=67.7)])
        with mock.patch.object(tr, "load_ex_today",
                               lambda s, path=None: {"2947": EX_DIV}):
            out = tr.annotate_restrictions(df, None, SESSION)
        self.assertEqual(out["Trade_Restriction"].tolist(), ["limit_lock"])
        out = tr.annotate_restrictions(df, None, SESSION)       # stubbed: {}
        self.assertEqual(out["Trade_Restriction"].tolist(), ["unknown"])

    def test_the_cache_reader(self):
        cache = {"exdiv": {
            "OTC": {"2947": [{"date": SESSION, "kind": "div", "cash": 1.0},
                             {"date": "2026-10-20", "kind": "div", "cash": 2.0}],
                    "6129": [{"date": SESSION, "kind": "right", "cash": None}]},
            "TSE": {"2614": [{"date": SESSION, "kind": "div", "cash": 0.4},
                             {"date": SESSION, "kind": "right", "cash": None}],
                    "1101": [{"date": "2026-10-08", "kind": "div", "cash": 1.0}]}}}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "company_events.json"
            p.write_text(json.dumps(cache), encoding="utf-8")
            got = REAL_LOAD_EX_TODAY(SESSION, path=str(p))
            self.assertEqual(sorted(got), ["2614", "2947", "6129"])
            self.assertEqual(got["2947"], EX_DIV)
            self.assertEqual(got["6129"]["kind"], "right")
            self.assertEqual(got["2614"]["kind"], "both")     # two lines, one day
            self.assertEqual(REAL_LOAD_EX_TODAY("2026-10-08", path=str(p)),
                             {"1101": {"date": "2026-10-08", "kind": "div",
                                       "cash": 1.0}})
            self.assertEqual(REAL_LOAD_EX_TODAY("", path=str(p)), {})
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(REAL_LOAD_EX_TODAY(SESSION, path=str(p)), {})
            self.assertEqual(REAL_LOAD_EX_TODAY(
                SESSION, path=str(Path(tmp) / "missing.json")), {})

    def test_the_checker_does_not_second_guess_an_ex_date_name(self):
        """result_checks recomputes the lock from the raw close; on an
        ex-date that disagrees by construction, so the payload declares the
        ex-date ids (meta.quality.restrictions.ex_today) and they are skipped."""
        from scanner.result_checks import check_payload
        info = fetch_recorded()
        info["ex_today"] = {"2947": EX_DIV}
        df = tr.annotate_restrictions(
            pd.DataFrame([row("2947", prev=62.6, close=67.7)]), info, SESSION)
        self.assertEqual(df["Trade_Restriction"].tolist(), ["limit_lock"])
        recs = json.loads(df.to_json(orient="records"))

        def mismatch(restr):
            meta = {"data_date": SESSION, "session_date": SESSION,
                    "quality": {"restrictions": restr}}
            rep = check_payload({"meta": meta, "rows": recs})
            return "limit_lock_mismatch" in {i["code"] for i in rep["items"]}
        self.assertFalse(mismatch(tr.summarize(info, df)))
        # an older payload without the declaration is judged by the raw close
        bare = tr.summarize(info, df)
        bare.pop("ex_today")
        self.assertTrue(mismatch(bare))
        # another session's declaration does not apply
        other = tr.summarize(info, df)
        other["ex_today"]["date"] = "2026-10-06"
        self.assertTrue(mismatch(other))


class ExDayNeverBlocksByDefault(NoNetwork):
    def test_the_blocking_set_is_still_suspended_only(self):
        # the reference-price fix only sharpens the display flags; it must
        # not change what blocks a buy
        self.assertEqual(tuple(tr.BLOCKING_RESTRICTIONS), ("suspended",))


if __name__ == "__main__":
    unittest.main()
