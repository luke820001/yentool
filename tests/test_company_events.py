"""
ingestion/company_events.py: the display-only company events (monthly
revenue, ex-rights / ex-dividend dates, investor conferences) and the proof
that they never reach a score or a gate.

Every exchange answer is a recorded fixture under tests/fixtures/
company_events/; nothing here touches the network or the real cache.

    python -m unittest tests.test_company_events -v
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import ingestion.company_events as ce
import scanner.market_regime as market_regime
from scanner.scan_mode import (
    add_trade_columns, apply_scan_mode, mark_buy_ready, select_with_hysteresis,
    sort_for_mode,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "company_events"
ROOT = Path(__file__).resolve().parent.parent
TODAY = "2026-10-08"
SESSION = "2026-10-07"
MODE = "mode_prelaunch"


def _json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _text(name):
    return (FIX / name).read_text(encoding="utf-8")


def _cp950(name):
    # the MOPS revenue page is served in cp950 and recorded as such
    return (FIX / name).read_bytes().decode("cp950", errors="replace")


class FakeFetch:
    """Routes each endpoint to its fixture. `fail` holds URL fragments that
    raise; `over` maps a URL to a replacement payload."""

    def __init__(self, fail=(), over=None):
        self.fail = tuple(fail)
        self.over = dict(over or {})
        self.calls = []

    def count(self, frag):
        return sum(1 for u in self.calls if frag in u)

    def __call__(self, url, method="GET", data=None, encoding=None):
        self.calls.append(url)
        if any(f in url for f in self.fail):
            raise RuntimeError("HTTP 503")
        if url in self.over:
            return self.over[url]
        if url == ce.REVENUE_URLS["TSE"]:
            return _json("twse_rev.json")
        if url == ce.REVENUE_URLS["OTC"]:
            return _json("tpex_rev.json")
        if url == ce.EXDIV_URLS["TSE"]:
            return _json("twse_exdiv.json")
        if url == ce.EXDIV_URLS["OTC"]:
            return _json("tpex_exdiv.json")
        if url == ce.NEWS_URLS["TSE"]:
            return _json("twse_news.json")
        if url == ce.NEWS_URLS["OTC"]:
            return _json("tpex_news.json")
        if url == ce.CONF_URL:
            self.calls[-1] = url + "#" + data["TYPEK"] + "/" + data["month"]
            if data["month"] != "10":
                return "<html><table></table></html>"
            return _text("mops_conf_sii.html" if data["TYPEK"] == "sii"
                         else "mops_conf_otc.html")
        if "t21sc03" in url and "/otc/" in url:
            return _cp950("t21_otc_115_9.html")
        raise RuntimeError("HTTP 404")


SMALL_FLOORS = (
    mock.patch.object(ce, "REV_MIN_ROWS", {"TSE": 3, "OTC": 3}),
    mock.patch.object(ce, "MOPS_REV_MIN_ROWS", 2),
)


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "company_events.json"
        for p in SMALL_FLOORS:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self, fetch=None, today=TODAY, **kw):
        logs = []
        ev = ce.refresh(cache_path=self.path, today=today,
                        fetch=fetch or FakeFetch(), log=logs.append, **kw)
        return ev, logs


# --------------------------------------------------------------- parsers
class Parsers(unittest.TestCase):
    def test_roc_dates_and_months(self):
        self.assertEqual(ce._roc_iso("1151007"), "2026-10-07")
        self.assertEqual(ce._roc_iso("115/10/07"), "2026-10-07")
        self.assertEqual(ce._roc_iso("99/1/5"), "2010-01-05")
        for bad in ("", None, "1151332", "115/02/30", "2026-10-07", "abc"):
            self.assertIsNone(ce._roc_iso(bad), bad)
        self.assertEqual(ce._roc_ym("11508"), "2026-08")
        for bad in ("11513", "11500", "1158", "", None, "2026-08"):
            self.assertIsNone(ce._roc_ym(bad), bad)

    def test_revenue_openapi(self):
        tse = ce.parse_revenue(_json("twse_rev.json"))
        otc = ce.parse_revenue(_json("tpex_rev.json"))
        self.assertEqual(len(tse), 6)
        self.assertEqual(sorted(otc), ["1240", "3498", "8227", "8440"])
        r = otc["8227"]
        self.assertEqual((r["month"], r["as_of"]), ("2026-08", "2026-09-17"))
        self.assertEqual(r["amount_k"], 149751.0)
        self.assertEqual((r["yoy"], r["mom"], r["cum_yoy"]), (42.2, 14.2, 73.2))
        self.assertEqual(tse["1101"]["mom"], -1.7)
        self.assertIsNone(tse["1101"]["note"])
        self.assertTrue(tse["2330"]["note"])
        # garbage in -> nothing out, never an exception
        self.assertEqual(ce.parse_revenue(None), {})
        self.assertEqual(ce.parse_revenue([{"x": 1}, "row", None]), {})

    def test_revenue_row_edges(self):
        def row(sid, ym, **kw):
            r = {ce.K_ID: sid, ce.K_YM: ym, ce.K_ASOF: "1150917",
                 ce.K_AMT: "1,000", ce.K_YOY: "", ce.K_MOM: "-3.04999",
                 ce.K_CUM: "nan", ce.K_NOTE: "A &amp; B"}
            r.update(kw)
            return r
        got = ce.parse_revenue([row("8227", "11508"), row("00981A", "11508"),
                                row("1111", "11513"), row("", "11508"),
                                row("12", "11508")])
        self.assertEqual(sorted(got), ["00981A", "8227"])   # 6-char id kept
        r = got["8227"]
        self.assertEqual(r["amount_k"], 1000.0)
        self.assertIsNone(r["yoy"])                         # blank pct
        self.assertIsNone(r["cum_yoy"])
        self.assertEqual(r["mom"], -3.0)
        self.assertEqual(r["note"], "A & B")                # entities unescaped

    def test_exdiv_kind_text(self):
        div, right = ce.CH_DIV, ce.CH_RIGHT
        rm = "\u9664"                                       # the 'ex-' prefix
        for text, kind in ((div, "div"), (right, "right"), (right + div, "both"),
                           (rm + div, "div"), (rm + right, "right"),
                           ("", None), ("cash", None)):
            self.assertEqual(ce._ex_kind(text), kind, text)
        got = ce.parse_exdiv_tse([{"Date": "1151019", "Code": "2330",
                                   "Exdividend": "?", "CashDividend": "5"}])
        self.assertEqual(got, {})                           # unknown kind dropped

    def test_exdiv_both_boards(self):
        tse = ce.parse_exdiv_tse(_json("twse_exdiv.json"))
        self.assertEqual(tse["2890"], [{"date": "2026-10-07", "kind": "right",
                                        "cash": None}])
        self.assertEqual(tse["2614"][0]["kind"], "both")
        self.assertEqual(tse["2614"][0]["cash"], 0.4)
        self.assertEqual(tse["00400A"][0], {"date": "2026-10-08", "kind": "div",
                                            "cash": 0.12})
        self.assertIsNone(tse["00401A"][0]["cash"])           # blank cash
        otc = ce.parse_exdiv_otc(_json("tpex_exdiv.json"))
        self.assertEqual(otc["6129"][0]["kind"], "right")
        self.assertEqual(otc["1784"][0]["cash"], 0.881)
        self.assertEqual(otc["8440"][0]["date"], "2026-09-30")
        for kinds in (tse, otc):
            for lst in kinds.values():
                for e in lst:
                    self.assertIn(e["kind"], ce.EX_KINDS)

    def test_other_clauses_are_not_conferences(self):
        rows = [{ce.K_ID: "6414", ce.K_CLAUSE: " " + ce.CONF_CLAUSE + " ",
                 ce.K_FACT: "1151016"},
                {ce.K_ID: "6415", ce.K_CLAUSE: ce.CONF_CLAUSE.replace("12", "10"),
                 ce.K_FACT: "1151016"},
                {ce.K_ID: "6416", ce.K_CLAUSE: ce.CONF_CLAUSE, ce.K_FACT: "bad"}]
        self.assertEqual(ce.parse_conf_news(rows, ce.K_ID),
                         {"6414": ["2026-10-16"]})

    def test_conference_announcements_keep_clause_12_only(self):
        tse = ce.parse_conf_news(_json("twse_news.json"), ce.K_ID)
        self.assertEqual(tse, {"2328": ["2026-10-13"], "6414": ["2026-10-16"]})
        otc = ce.parse_conf_news(_json("tpex_news.json"), "SecuritiesCompanyCode")
        self.assertEqual(otc, {"8421": ["2026-10-07"], "3624": ["2026-10-07"],
                               "6517": ["2026-10-07"], "6190": ["2026-10-14"]})

    def test_mops_conference_pages(self):
        otc = ce.parse_mops_conf_html(_text("mops_conf_otc.html"))
        self.assertEqual(otc["2221"], [("2026-10-08", "2026-10-13")])
        self.assertEqual(otc["3105"], [("2026-10-23", "2026-10-23")])
        sii = ce.parse_mops_conf_html(_text("mops_conf_sii.html"))
        self.assertEqual(sorted(sii), ["1338", "2408", "2603", "3008"])
        self.assertEqual(sii["2408"], [("2026-10-12", "2026-10-12")])
        self.assertEqual(ce.parse_mops_conf_html(""), {})

    def test_mops_revenue_page(self):
        got = ce.parse_mops_revenue_html(_cp950("t21_otc_115_9.html"), "2026-09")
        self.assertEqual(sorted(got), ["1780", "1796", "4207", "8227"])
        r = got["8227"]
        self.assertEqual((r["month"], r["as_of"]), ("2026-09", "2026-10-08"))
        self.assertEqual(r["amount_k"], 156008.0)
        self.assertEqual((r["yoy"], r["mom"], r["cum_yoy"]), (-23.8, 4.2, 51.9))


# --------------------------------------------------------------- refresh
class Refresh(CacheCase):
    def test_first_run_fills_every_block_and_writes_ascii(self):
        ev, logs = self.refresh()
        self.assertEqual(len(ev["revenue"]["TSE"]), 6)
        self.assertEqual(len(ev["revenue"]["OTC"]), 4)
        self.assertEqual(ev["revenue"]["MOPS_OTC"]["8227"]["month"], "2026-09")
        self.assertNotIn("MOPS_TSE", ev["revenue"])
        for blk in ev["revenue"].values():
            for r in blk.values():
                self.assertNotIn("note", r)            # free text is not cached
        # 8440's 09-30 ex-date is older than PRUNE_DAYS: dropped
        self.assertNotIn("8440", ev["exdiv"]["OTC"])
        self.assertIn("6129", ev["exdiv"]["OTC"])
        self.assertEqual(ev["conf"]["2221"], [["2026-10-08", "2026-10-13", "mops"]])
        self.assertEqual(ev["conf"]["2328"], [["2026-10-13", "2026-10-13", "news"]])
        raw = self.path.read_bytes()
        self.assertTrue(all(b < 128 for b in raw))
        self.assertTrue(re.search(rb'(?m)^\s+"8227": \{', raw))
        self.assertEqual(json.loads(raw.decode("ascii"))["revenue"]["OTC"]["8227"],
                         ev["revenue"]["OTC"]["8227"])
        self.assertEqual(ce.load_cache(self.path)["conf"], ev["conf"])
        self.assertTrue(logs)

    def test_health_per_source(self):
        ev, _ = self.refresh()
        src = ev["sources"]
        self.assertEqual(set(src), set(ce.SOURCES))
        for name in ("revenue_tse", "revenue_otc", "exdiv_tse", "exdiv_otc",
                     "news_tse", "news_otc", "revenue_mops_otc",
                     "conf_mops_tse", "conf_mops_otc"):
            self.assertTrue(src[name]["ok"], name)
            self.assertFalse(src[name]["stale"], name)
        self.assertEqual(src["revenue_otc"]["rows"], 4)
        self.assertEqual(src["revenue_otc"]["as_of"], "2026-09-17")
        self.assertEqual(src["news_otc"]["as_of"], "2026-10-06")
        # the MOPS TSE page is not in the fixtures: an optional source fails
        h = src["revenue_mops_tse"]
        self.assertFalse(h["ok"])
        self.assertTrue(h["optional"])
        self.assertIn("HTTP 404", h["error"])
        self.assertFalse(src["revenue_tse"]["optional"])

    def test_once_per_day_except_the_announcements(self):
        f1 = FakeFetch()
        self.refresh(f1)
        f2 = FakeFetch()
        ev, _ = self.refresh(f2)
        self.assertEqual(f2.count(ce.REVENUE_URLS["TSE"]), 0)
        self.assertEqual(f2.count(ce.EXDIV_URLS["OTC"]), 0)
        self.assertEqual(f2.count(ce.CONF_URL), 0)
        self.assertEqual(f2.count("/otc/t21sc03"), 0)
        self.assertEqual(f2.count(ce.NEWS_URLS["TSE"]), 1)
        self.assertEqual(f2.count(ce.NEWS_URLS["OTC"]), 1)
        # the failed optional page is asked again
        self.assertEqual(f2.count("/sii/t21sc03"), 1)
        self.assertEqual(len(ev["revenue"]["OTC"]), 4)

    def test_conferences_accumulate_across_runs(self):
        self.refresh()
        other = [{ce.K_ID: "2330", ce.K_CLAUSE: ce.CONF_CLAUSE,
                  ce.K_FACT: "1151020"}]
        ev, _ = self.refresh(FakeFetch(over={ce.NEWS_URLS["TSE"]: other,
                                             ce.NEWS_URLS["OTC"]: []}),
                             today="2026-10-09")
        self.assertIn("2330", ev["conf"])
        self.assertIn("2328", ev["conf"])            # yesterday's file
        self.assertIn("8421", ev["conf"])

    def test_a_failure_keeps_the_last_good_block(self):
        self.refresh()
        fail = (ce.REVENUE_URLS["OTC"], ce.EXDIV_URLS["TSE"])
        ev, _ = self.refresh(FakeFetch(fail=fail), today="2026-10-09")
        self.assertEqual(len(ev["revenue"]["OTC"]), 4)
        self.assertIn("2890", ev["exdiv"]["TSE"])
        h = ev["sources"]["revenue_otc"]
        self.assertFalse(h["ok"])
        self.assertIn("503", h["error"])
        self.assertFalse(h["stale"])                 # one day old
        self.assertTrue(str(h["fetched_at"]).startswith(TODAY))
        # four days without a good answer: the ex-dates are stale
        ev, _ = self.refresh(FakeFetch(fail=fail), today="2026-10-12")
        self.assertTrue(ev["sources"]["exdiv_tse"]["stale"])
        self.assertFalse(ev["sources"]["revenue_otc"]["stale"])

    def test_the_row_floor_rejects_a_short_answer(self):
        with mock.patch.object(ce, "REV_MIN_ROWS", {"TSE": 800, "OTC": 3}):
            ev, _ = self.refresh()
        self.assertNotIn("TSE", ev["revenue"])
        self.assertFalse(ev["sources"]["revenue_tse"]["ok"])
        self.assertIn("floor", ev["sources"]["revenue_tse"]["error"])
        self.assertTrue(ev["sources"]["revenue_otc"]["ok"])

    def test_an_unreachable_mopsov_is_optional(self):
        ev, _ = self.refresh(FakeFetch(fail=("mopsov.twse.com.tw",)))
        for name in ("conf_mops_tse", "conf_mops_otc", "revenue_mops_otc"):
            self.assertFalse(ev["sources"][name]["ok"])
            self.assertTrue(ev["sources"][name]["optional"])
        meta = ce.meta_block(ev, SESSION)
        self.assertTrue(meta["ok"])                   # required sources fine

    def test_a_dead_network_never_raises_and_keeps_the_cache(self):
        self.refresh()

        def dead(*a, **k):
            raise OSError("network down")
        ev, _ = self.refresh(dead, today="2026-10-09")
        self.assertEqual(len(ev["revenue"]["TSE"]), 6)
        self.assertFalse(any(h["ok"] for n, h in ev["sources"].items()
                             if not h.get("skipped")))
        # a broken cache file reads as empty, never raises
        self.path.write_text("{not json", encoding="ascii")
        self.assertEqual(ce.load_cache(self.path)["revenue"], {})
        ev, _ = self.refresh(dead)
        self.assertEqual(ev["revenue"], {})

    def test_write_false_leaves_no_file(self):
        ev, _ = self.refresh(write=False)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(ev["revenue"]["OTC"]), 4)

    def test_past_conferences_are_pruned(self):
        seed = ce._empty_cache()
        seed["conf"] = {"1111": [["2026-09-10", "2026-09-20", "news"]],
                        "2222": [["2026-09-28", "2026-10-02", "mops"]]}
        ce.save_cache(seed, self.path)
        ev, _ = self.refresh()
        self.assertNotIn("1111", ev["conf"])
        self.assertEqual(ev["conf"]["2222"], [["2026-09-28", "2026-10-02", "mops"]])

    def test_mops_revenue_is_skipped_while_the_openapi_is_current(self):
        seed = ce._empty_cache()
        seed["revenue"]["MOPS_OTC"] = {"8227": {"month": "2026-07"}}
        ce.save_cache(seed, self.path)
        f = FakeFetch()
        ev, _ = self.refresh(f, today="2026-09-20")
        self.assertEqual(f.count("t21sc03"), 0)
        self.assertNotIn("MOPS_OTC", ev["revenue"])
        h = ev["sources"]["revenue_mops_otc"]
        self.assertTrue(h["ok"])
        self.assertEqual(h["skipped"], "openapi current")
        self.assertFalse(h["stale"])


    def test_required_sources_run_before_the_optional_pages(self):
        # verifier 2026-10-08: the shared budget runs out on the first
        # mopsov request (a hanging host); every required source must
        # already be done by then
        class Hang(FakeFetch):
            def spent(self):
                return any("mopsov" in u for u in self.calls)

        f = Hang()
        ev, _ = self.refresh(f)
        first = min(i for i, u in enumerate(f.calls) if "mopsov" in u)
        for url in (list(ce.REVENUE_URLS.values())
                    + list(ce.EXDIV_URLS.values())
                    + list(ce.NEWS_URLS.values())):
            self.assertLess(f.calls.index(url), first, url)
        for name, (kind, optional) in ce.SOURCES.items():
            h = ev["sources"][name]
            if optional:
                continue
            self.assertTrue(h["ok"], name)
        spent = [n for n, h in ev["sources"].items()
                 if h.get("error") == "time budget spent"]
        self.assertTrue(spent)
        self.assertTrue(all(ce.SOURCES[n][1] for n in spent), spent)
        self.assertTrue(ce.meta_block(ev, SESSION)["ok"])


class FetcherRetries(unittest.TestCase):
    """_Fetcher's dead-host memo (verifier 2026-10-08): one dropped
    connection must not end the run for a host that serves several
    required sources; a refused TLS handshake or a host that failed every
    try does."""

    URL = "https://www.tpex.org.tw/openapi/v1/a"
    OTHER = "https://www.tpex.org.tw/openapi/v1/b"

    def setUp(self):
        p = mock.patch.object(ce, "BACKOFF_S", 0)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def ok(payload):
        r = mock.Mock()
        r.status_code = 200
        r.json.return_value = payload
        return r

    def test_one_reset_then_success_keeps_the_host(self):
        import requests
        f = ce._Fetcher(budget_s=60)
        seq = [requests.exceptions.ConnectionError("reset"), self.ok([1])]
        with mock.patch("requests.get", side_effect=seq) as g:
            self.assertEqual(f(self.URL), [1])
            self.assertEqual(g.call_count, 2)
        self.assertNotIn("www.tpex.org.tw", f.dead)
        with mock.patch("requests.get", return_value=self.ok([2])):
            self.assertEqual(f(self.OTHER), [2])

    def test_a_reset_on_one_url_does_not_block_the_next(self):
        import requests
        f = ce._Fetcher(budget_s=60)
        seq = [requests.exceptions.ConnectionError("reset"),
               mock.Mock(status_code=503), self.ok([3])]
        with mock.patch("requests.get", side_effect=seq):
            with self.assertRaises(RuntimeError):
                f(self.URL)                  # reset, then HTTP 503
            self.assertNotIn("www.tpex.org.tw", f.dead)
            self.assertEqual(f(self.OTHER), [3])

    def test_every_try_unreachable_marks_the_host_dead(self):
        import requests
        f = ce._Fetcher(budget_s=60)
        with mock.patch("requests.get",
                        side_effect=requests.exceptions.Timeout("t")) as g:
            with self.assertRaises(requests.exceptions.Timeout):
                f(self.URL)
            self.assertEqual(g.call_count, ce.TRIES)
            self.assertIn("www.tpex.org.tw", f.dead)
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                f(self.OTHER)
            self.assertEqual(g.call_count, ce.TRIES)   # not asked again

    def test_a_refused_handshake_is_not_retried(self):
        import requests
        f = ce._Fetcher(budget_s=60)
        with mock.patch("requests.post",
                        side_effect=requests.exceptions.SSLError("tls")) as p:
            with self.assertRaises(requests.exceptions.SSLError):
                f(ce.CONF_URL, method="POST", data={}, encoding="cp950")
            self.assertEqual(p.call_count, 1)
        self.assertIn("mopsov.twse.com.tw", f.dead)
        self.assertNotIn("www.tpex.org.tw", f.dead)

    def test_a_spent_budget_asks_nothing(self):
        f = ce._Fetcher(budget_s=-1)
        with mock.patch("requests.get") as g:
            with self.assertRaisesRegex(RuntimeError, "budget"):
                f(self.URL)
            g.assert_not_called()


# -------------------------------------------------------------- annotate
class Annotate(CacheCase):
    def setUp(self):
        super().setUp()
        self.ev, _ = self.refresh(write=False)

    def frame(self, *sids):
        return pd.DataFrame([{"Stock_ID": s, "Data_Date": SESSION,
                              "Launch_Score": 50.0} for s in sids])

    def test_no_events_means_nine_null_columns(self):
        for events in (None, {}, ce._empty_cache()):
            out = ce.annotate_events(self.frame("8227", "2890"), events, SESSION)
            for c in ce.EVENT_COLUMNS:
                self.assertIn(c, out.columns)
                self.assertTrue(out[c].isna().all(), c)
        empty = ce.annotate_events(pd.DataFrame(), self.ev, SESSION)
        self.assertEqual(set(ce.EVENT_COLUMNS) - set(empty.columns), set())
        self.assertIsNone(ce.annotate_events(None, self.ev))

    def test_input_is_not_modified_and_order_kept(self):
        df = self.frame("8227", "9999", "2890")
        before = df.copy()
        out = ce.annotate_events(df, self.ev, SESSION)
        pd.testing.assert_frame_equal(df, before)
        pd.testing.assert_frame_equal(out[list(before.columns)], before)

    def test_newest_published_month_before_the_session(self):
        out = ce.annotate_events(self.frame("8227", "1240"), self.ev, SESSION)
        r = out.iloc[0]
        # MOPS already has September for 8227; the openapi still says August
        self.assertEqual(r["Rev_Month"], "2026-09")
        self.assertEqual(r["Rev_Amount_K"], 156008.0)
        self.assertEqual(r["Rev_YoY_Pct"], -23.8)
        self.assertEqual(out.iloc[1]["Rev_Month"], "2026-08")
        # a month is never shown on or before its own session month
        out = ce.annotate_events(self.frame("8227"), self.ev, "2026-09-30")
        self.assertEqual(out.iloc[0]["Rev_Month"], "2026-08")
        self.assertEqual(out.iloc[0]["Rev_Amount_K"], 149751.0)
        out = ce.annotate_events(self.frame("8227"), self.ev, "2026-08-31")
        self.assertIsNone(out.iloc[0]["Rev_Month"])

    def test_ex_date_is_strictly_after_the_session(self):
        out = ce.annotate_events(self.frame("2890", "00400A", "6129", "2614"),
                                 self.ev, SESSION)
        self.assertIsNone(out.iloc[0]["Ex_Date"])          # ex on the session
        self.assertEqual(out.iloc[1][["Ex_Date", "Ex_Kind", "Ex_Cash_Div"]].tolist(),
                         ["2026-10-08", "div", 0.12])
        self.assertIsNone(out.iloc[2]["Ex_Date"])
        self.assertIsNone(out.iloc[3]["Ex_Date"])
        out = ce.annotate_events(self.frame("6129", "2890"), self.ev, "2026-10-06")
        self.assertEqual(out.iloc[0][["Ex_Date", "Ex_Kind"]].tolist(),
                         ["2026-10-07", "right"])
        self.assertIsNone(out.iloc[0]["Ex_Cash_Div"])
        self.assertEqual(out.iloc[1]["Ex_Kind"], "right")

    def test_conference_date_is_the_start_of_a_running_span(self):
        out = ce.annotate_events(self.frame("2221", "3265", "8421", "2328"),
                                 self.ev, SESSION)
        self.assertEqual(out["Conf_Date"].tolist(),
                         ["2026-10-08", None, "2026-10-07", "2026-10-13"])
        out = ce.annotate_events(self.frame("2221"), self.ev, "2026-10-12")
        self.assertEqual(out.iloc[0]["Conf_Date"], "2026-10-08")
        out = ce.annotate_events(self.frame("2221"), self.ev, "2026-10-14")
        self.assertIsNone(out.iloc[0]["Conf_Date"])

    def test_unknown_names_are_null(self):
        out = ce.annotate_events(self.frame("9999"), self.ev, SESSION)
        for c in ce.EVENT_COLUMNS:
            self.assertIsNone(out.iloc[0][c], c)

    def test_session_defaults_to_the_newest_bar(self):
        df = self.frame("8227")
        df.loc[0, "Data_Date"] = "2026-09-30"
        out = ce.annotate_events(df, self.ev)
        self.assertEqual(out.iloc[0]["Rev_Month"], "2026-08")


class Meta(CacheCase):
    def test_block_shape_and_coverage(self):
        ev, _ = self.refresh(write=False)
        df = ce.annotate_events(pd.DataFrame([{"Stock_ID": "8227"},
                                              {"Stock_ID": "00400A"},
                                              {"Stock_ID": "9999"}]), ev, SESSION)
        tracked = ce.annotate_events(pd.DataFrame([{"Stock_ID": "2221"}]), ev,
                                     SESSION)
        m = ce.meta_block(ev, SESSION, df, tracked)
        self.assertTrue(m["ok"])
        self.assertEqual(m["session_date"], SESSION)
        self.assertEqual(m["revenue_month_latest"], "2026-09")
        self.assertEqual(m["next_revenue_deadline"], "2026-10-12")
        self.assertEqual(m["next_report_deadline"],
                         {"date": "2026-11-14", "what": "Q3", "approximate": True})
        self.assertEqual(m["coverage"], {"rows": 4, "revenue": 1, "exdiv": 1,
                                         "conf": 1})
        self.assertEqual(set(m["sources"]), set(ce.SOURCES))
        self.assertIn("never scored", m["note"])
        json.dumps(m)
        # a required source down: the block says so
        ev["sources"]["revenue_otc"]["ok"] = False
        self.assertFalse(ce.meta_block(ev, SESSION)["ok"])
        self.assertIsNone(ce.meta_block(None, SESSION))

    def test_point_in_time_revenue_rule(self):
        # BACKTEST_LOG M.6: month M is known only AFTER the 10th of M+1,
        # rolled to the next session (2026-10-10 is a Saturday holiday)
        self.assertEqual(ce.revenue_deadline("2026-09"), "2026-10-12")
        self.assertFalse(ce.revenue_month_visible("2026-09", "2026-10-12"))
        self.assertTrue(ce.revenue_month_visible("2026-09", "2026-10-13"))
        self.assertEqual(ce.revenue_deadline("2026-08"), "2026-09-10")
        self.assertFalse(ce.revenue_month_visible("2026-08", "2026-09-10"))
        self.assertTrue(ce.revenue_month_visible("2026-08", "2026-09-11"))
        self.assertEqual(ce.revenue_deadline("2026-12")[:7], "2027-01")
        self.assertFalse(ce.revenue_month_visible("bad", "2026-10-13"))

    def test_deadlines(self):
        self.assertEqual(ce.next_revenue_deadline("2026-10-13"), "2026-11-10")
        self.assertEqual(ce.next_revenue_deadline("2026-10-12"), "2026-10-12")
        self.assertEqual(ce.next_report_deadline("2026-12-01")["date"],
                         "2027-03-31")
        self.assertEqual(ce.next_report_deadline("2026-04-01")["what"], "Q1")

    def test_a_holiday_on_the_10th_rolls_the_deadline(self):
        """T3: is_session was never exercised, and _roll_to_session swallows
        every exception, so an always-open stub left the suite green. 2026-11
        has no such holiday, so inject one on a Tuesday 10th."""
        import scanner.market_calendar as mc

        def closed(year, fetch=True):
            return frozenset({"2026-11-10"})
        with mock.patch.object(mc, "closed_dates", closed):
            self.assertEqual(ce.next_revenue_deadline("2026-11-02"), "2026-11-11")
            self.assertEqual(ce.revenue_deadline("2026-10"), "2026-11-11")
            self.assertTrue(ce.revenue_month_visible("2026-10", "2026-11-12"))
            self.assertFalse(ce.revenue_month_visible("2026-10", "2026-11-11"))
        # a year the calendar does not know: the weekday rule alone
        with mock.patch.object(mc, "closed_dates", lambda year, fetch=True: None):
            self.assertEqual(ce.next_revenue_deadline("2026-11-02"), "2026-11-10")
        # and an always-open calendar is what the real one is for 2026-11
        with mock.patch.object(mc, "closed_dates",
                               lambda year, fetch=True: frozenset()):
            self.assertEqual(ce.revenue_deadline("2026-10"), "2026-11-10")


# ------------------------------------------------- never scored, never gated
def scan_frame():
    """30 candidates: TSE and OTC, CORE+ and not, so the pipeline produces
    buyable rows and every kind of block."""
    rows = []
    for i in range(30):
        rows.append({
            "Stock_ID": str(8000 + i), "Stock_Name": "N%d" % i,
            "Market": "OTC" if i % 3 else "TSE", "Data_Date": SESSION,
            "Close_Price": 50.0 + i, "MA20": 48.0 + i,
            "Launch_Score": 70.0 if i == 7 else 90.0 - i * 1.5,
            "Integrity_OK": True, "Hold_Status": "pending",
            "Dist_52W_High_Pct": 3.0 if i % 2 == 0 else 9.0,
            "Ret_5D_Pct": 2.0, "ATR_Pct": 5.0})
    return pd.DataFrame(rows)


def loud_events(sids):
    """Events that would move any score that read them: every name has a
    huge revenue jump, an ex-date tomorrow and a conference today."""
    ev = ce._empty_cache()
    ev["revenue"]["OTC"] = {s: {"month": "2026-09", "as_of": "2026-10-05",
                                "amount_k": 1e9, "yoy": 9999.0, "mom": -99.0,
                                "cum_yoy": 500.0} for s in sids}
    ev["exdiv"]["OTC"] = {s: [{"date": TODAY, "kind": "both", "cash": 9.9}]
                          for s in sids}
    ev["conf"] = {s: [[SESSION, SESSION, "news"]] for s in sids}
    return ev


class EventsNeverFeedScoringOrGates(unittest.TestCase):
    def setUp(self):
        self._real = market_regime.get_market_regime
        market_regime.get_market_regime = lambda: {
            "ok": True, "enter_ok": True, "risk_on": True, "is_current": True}

    def tearDown(self):
        market_regime.get_market_regime = self._real

    def pipeline(self, df):
        a = apply_scan_mode(df, MODE)
        a = sort_for_mode(a, MODE)
        sel, ids = select_with_hysteresis(a, ["8025"])
        sel = add_trade_columns(sel, MODE)
        return mark_buy_ready(sel, MODE, session_date=SESSION), ids

    def test_scores_and_buy_ready_identical_with_and_without_events(self):
        base = scan_frame()
        plain, ids_plain = self.pipeline(base)
        ev = loud_events(base["Stock_ID"].tolist())
        annotated_in = ce.annotate_events(base, ev, SESSION)
        self.assertTrue(annotated_in["Rev_YoY_Pct"].notna().all())
        self.assertTrue(annotated_in["Ex_Date"].notna().all())
        self.assertTrue(annotated_in["Conf_Date"].notna().all())
        with_ev, ids_ev = self.pipeline(annotated_in)
        self.assertEqual(ids_plain, ids_ev)
        # the run is meaningful: some rows buy, some are blocked
        self.assertGreater(int(plain["Buy_Ready"].sum()), 0)
        self.assertGreater(int((~plain["Buy_Ready"].astype(bool)).sum()), 0)
        cols = [c for c in plain.columns]
        self.assertTrue(set(cols) <= set(with_ev.columns))
        pd.testing.assert_frame_equal(plain[cols].reset_index(drop=True),
                                      with_ev[cols].reset_index(drop=True))
        for c in ("Stock_ID", "Launch_Score", "Core_Plus", "Buy_Ready",
                  "Buy_Block"):
            self.assertEqual(plain[c].tolist(), with_ev[c].tolist(), c)

    def test_annotating_after_the_gates_changes_nothing_else(self):
        out, _ = self.pipeline(scan_frame())
        ann = ce.annotate_events(out, loud_events(out["Stock_ID"].tolist()),
                                 SESSION)
        pd.testing.assert_frame_equal(ann[list(out.columns)], out)
        self.assertEqual(set(ann.columns) - set(out.columns),
                         set(ce.EVENT_COLUMNS))

    def test_no_scoring_module_reads_an_event_column(self):
        cols = (r"Rev_(Month|Amount_K|YoY_Pct|MoM_Pct|Cum_YoY_Pct)"
                r"|Ex_(Date|Kind|Cash_Div)|Conf_Date")
        pat = re.compile(cols + r"|company_events")
        # One sanctioned reader (final review, DECISIONS addendum item 6):
        # trade_restrictions.load_ex_today reads today's ex-date entries from
        # the cache file to find the exchange's reference price for the limit
        # flags. It never touches an event COLUMN, and the flags it improves
        # (limit_lock / limit_down) are display-only unless
        # BLOCKING_RESTRICTIONS is flipped (pinned by test_trade_restrictions).
        pat_restr = re.compile(cols)
        files = [ROOT / "scanner" / n for n in (
            "scan_mode.py", "chip_verifier.py", "exit_rules.py",
            "holding_tracker.py", "market_filter.py", "signal_ledger.py",
            "market_regime.py", "market_leg.py", "live_record.py",
            "tracked_rows.py", "list_freeze.py", "trade_restrictions.py")]
        files += [ROOT / "portfolio" / n for n in ("sync.py", "ledger.py")]
        files += sorted((ROOT / "analyzer").glob("*.py"))
        for p in files:
            if not p.exists():
                continue
            use = pat_restr if p.name == "trade_restrictions.py" else pat
            hit = use.search(p.read_text(encoding="utf-8"))
            self.assertIsNone(hit, "{} mentions {}".format(
                p.name, hit.group(0) if hit else ""))

    def test_the_cloud_scan_annotates_after_every_gate_and_write(self):
        src = (ROOT / "scan_headless.py").read_text(encoding="utf-8")
        run = src[src.find("def run_scan("):]
        at = run.find("annotate_events(result_df")
        self.assertGreater(at, 0)
        for call in ("mark_buy_ready(result_df", "_finish_recommendations(",
                     "record_picks(result_df", "build_live_record("):
            i = run.find(call)
            self.assertGreater(i, 0, call)
            self.assertLess(i, at, call)
        # the refresh starts only after the list-freeze gate
        self.assertLess(run.find("gate.get(\"revised_reason\")"),
                        run.find("_start_events_refresh()"))

    def test_the_desktop_scan_annotates_after_every_gate_and_write(self):
        src = (ROOT / "gui" / "scan_worker.py").read_text(encoding="utf-8")
        at = src.find("annotate_events(result_df")
        self.assertGreater(at, 0)
        for call in ("mark_buy_ready(result_df", "record_picks(result_df"):
            i = src.find(call)
            self.assertGreater(i, 0, call)
            self.assertLess(i, at, call)
        self.assertIn("refresh(write=False)", src)


if __name__ == "__main__":
    unittest.main()
