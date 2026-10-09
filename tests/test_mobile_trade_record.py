"""
The complete record of a closed recommendation, on the phone, pinned to the
BACKEND that writes it (2026-10-09; the owner asked that a trade be recorded in full up to its real
exit).

What this proves. mobile/app.js prints, for a closed recommendation, the signal
day, the rule's entry, the day the exit was decided, the day/price/basis it was
booked at, the costs, the excursions, a day-by-day table and -- for the owner's
own fills -- a reconciliation against the rule. Every one of those numbers is
produced HERE by the real backend (scanner.live_record.replay_trade +
trade_record, stored through portfolio.sync._outcome exactly as the ledger
stores it) on synthetic bars, formatted independently with Decimal, and handed
to tests/mobile_probe.js (--trade-grid), which renders them with the app's own
functions and compares. A stored record that the backend's own checker
(result_checks._record_problem) would reject is a bug in THIS file, so every
fixture is asserted clean first.

The wording the owner reads lives in the probe (Chinese); this file passes
codes and numbers only and stays ASCII.

Skipped when node is missing. No network. Nothing under data/ is read or
written.

    python -m unittest tests.test_mobile_trade_record -v
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

from portfolio.money import FeeSchedule
from portfolio.sync import _outcome
from scanner import result_checks as rc
from scanner.live_record import replay_trade, trade_record

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "mobile_probe.js"
APP = ROOT / "mobile" / "app.js"
NODE = shutil.which("node")

FLAT = (100.0, 100.5, 99.5, 100.0)
TWO = Decimal("0.01")


def _days(start, n):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def _fwd(bars, start="2026-09-08"):
    ds = _days(start, len(bars))
    return pd.DataFrame([(d,) + tuple(b) for d, b in zip(ds, bars)],
                        columns=["date", "open", "high", "low", "close"])


def _bleed(n):
    """a slow decline: never +1%, never above its own 5-bar mean, stop untouched"""
    return [(100.0 - 0.4 * i, 100.2 - 0.4 * i, 99.5 - 0.4 * i, 99.8 - 0.4 * i)
            for i in range(n)]


_RIDE_CLOSES = [100.0, 99.5, 99.8, 99.6, 99.9, 100.0, 100.1, 100.3, 100.5,
                100.8, 100.9, 100.0, 100.0]

# name -> (bars, expected reason, expected basis)
SCENARIOS = {
    "stop_level": ([(87.3, 88.0, 86.5, 87.0), (86.8, 87.5, 85.0, 86.0),
                    (85.0, 85.4, 69.0, 70.5)], "stop", "level"),
    "stop_gap": ([(87.3, 88.0, 86.5, 87.0), (66.0, 67.0, 64.0, 65.0)],
                 "stop", "open"),
    "tp_level": ([(52.4, 53.0, 52.0, 52.8), (53.0, 54.0, 52.5, 53.5),
                  (54.0, 63.5, 53.8, 62.0)], "tp", "level"),
    "tp_gap": ([(52.4, 53.0, 52.0, 52.8), (64.0, 65.0, 63.0, 64.5)],
               "tp", "open"),
    "lock_level": ([FLAT, (100.0, 103.0, 99.9, 102.6),
                    (102.4, 102.5, 101.0, 101.5)], "lock", "level"),
    "lock_gap": ([FLAT, (100.0, 103.0, 99.9, 102.6),
                  (101.0, 101.5, 100.5, 101.2)], "lock", "open"),
    # day 8 closes +1.2%: the sale is decided that evening, filled day 9
    "late": ([FLAT] * 7 + [(100.0, 101.4, 99.8, 101.2),
                           (101.5, 102.0, 100.8, 101.0)], "late", "open"),
    "time_live": (_bleed(12), "time", "close"),
    "time_nolive": (_bleed(10), "time", "close"),
    "ride": ([(c, c + 0.3, c - 0.4, c) for c in _RIDE_CLOSES], "time", "close"),
}


def _txt(v, signed=False):
    """the 2-decimal text of a stored number, formatted by Python"""
    return ("%+.2f" if signed else "%.2f") % v


def _q(x):
    return x.quantize(TWO, rounding=ROUND_HALF_UP)


def build(name, as_text=False, legacy=False):
    """One recommendations.json row for a scenario, with the backend's own
    record in `outcome`, and the independent expectations for the probe."""
    bars, want_reason, want_basis = SCENARIOS[name]
    fwd = _fwd(bars)
    t = replay_trade(fwd)
    rec = trade_record(fwd, t)
    assert t["exited"] and t["reason"] == want_reason, (name, t["reason"])
    assert rec["exit_basis"] == want_basis, (name, rec["exit_basis"])
    first = fwd["date"].iloc[0]
    signal = (pd.Timestamp(first) - pd.offsets.BDay(1)).strftime("%Y-%m-%d")
    sid = {"stop_level": "1101", "stop_gap": "1102", "tp_level": "2201",
           "tp_gap": "2202", "lock_level": "3301", "lock_gap": "3302",
           "late": "4401", "time_live": "5501", "time_nolive": "5502",
           "ride": "6601"}[name]
    out = _outcome(t, record=None if legacy else rec,
                   signal_session=signal, planned_session=first)
    row = {
        "recommendation_id": "rec-%s-mode_prelaunch-1" % sid,
        "stock_id": sid, "stock_name": "T" + sid, "market": "OTC",
        "status": "closed", "status_reason": t["reason"],
        "status_session": t["exit_date"],
        "first_qualified_session": signal, "valid_until_session": first,
        "strategy": "mode_prelaunch", "strategy_version": "prelaunch-2026-09-09",
        "outcome": json.dumps(out, ensure_ascii=True, sort_keys=True)
        if as_text else out,
    }
    return row, out, fwd, t


def expectations(out, row, legacy=False):
    path = out.get("path") or []
    e = {
        "reason": out["reason"], "basis": None if legacy else out["exit_basis"],
        "bars": out["bars"], "calendar_days": out.get("calendar_days"),
        "signal": row["first_qualified_session"],
        "entry_date": out["entry_date"], "entry_price": _txt(out["entry_price"]),
        "exit_date": out["exit_date"], "exit_price": _txt(out["exit_price"]),
        "gross": _txt(out["ret_gross_pct"], True),
        "net": _txt(out["ret_net_pct"], True),
        "has_path": bool(path), "path": [], "live": None, "ride_days": 0,
        "issues": [],
    }
    if legacy:
        # an older record keeps no record: the phone derives days and cost from
        # the eight summary keys, and has no decision day or excursions
        e["calendar_days"] = _days_between(out["entry_date"], out["exit_date"])
        e["cost"] = _txt(_q(Decimal(str(out["ret_gross_pct"])) -
                            Decimal(str(out["ret_net_pct"]))))
        e.update(decision=None, mfe=None, mae=None)
        return e
    e.update(decision=out["exit_decision_date"],
             cost=_txt(out["cost_pct"]),
             mfe=_txt(out["mfe_pct"], True), mae=_txt(out["mae_pct"], True))
    for p in path:
        e["path"].append({
            "date": p["date"], "day": p["day"], "status": p["status"],
            "ride": p["ride"], "open": _txt(p["open"]), "high": _txt(p["high"]),
            "low": _txt(p["low"]), "close": _txt(p["close"]),
            "ret": _txt(p["close_ret_pct"], True),
            "stop": None if p["stop"] is None else _txt(p["stop"]),
        })
    e["ride_days"] = sum(1 for p in path if p["ride"])
    if out["reason"] == "time" and out.get("live_fill_date"):
        gap = _q((Decimal(str(out["live_fill_price"])) / Decimal(str(out["exit_price"]))
                  - 1) * 100)
        e["live"] = {"date": out["live_fill_date"],
                     "price": _txt(out["live_fill_price"]),
                     "gap": "%+.2f" % gap}
    return e


# --- the owner's fills --------------------------------------------------------
SCHED = FeeSchedule.default()


def _cents(d):
    return int((Decimal(str(d)) * 100).to_integral_value())


def _fill(side, date, price, shares):
    p = Decimal(str(price))
    return {
        "side": side, "session_date": date, "price_cents": _cents(p),
        "shares": shares, "is_current": 1, "executed_at": "",
        "recorded_at": "2026-10-09 10:00:00",
        "fee_cents": _cents(SCHED.buy_fee(p, shares) if side == "BUY"
                            else SCHED.sell_fee(p, shares)),
        "tax_cents": 0 if side == "BUY" else _cents(SCHED.sell_tax(p, shares)),
    }


def _pct(num, den):
    return _q(Decimal(num) * 100 / Decimal(den))


def _days_between(a, b):
    """calendar days from a to b (b minus a), negative when b is earlier"""
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


def reconcile_case(name, buys, sells, calendar):
    """buys/sells: [(date, price, shares)]. Expected figures by Decimal."""
    row, out, fwd, t = build(name)
    cal = list(calendar)
    execs = [_fill("BUY", d, p, n) for d, p, n in buys] + \
            [_fill("SELL", d, p, n) for d, p, n in sells]
    rule_entry = _cents(out["entry_price"])
    rule_exit = _cents(out["exit_price"])
    exp = {"n_buys": len(buys), "n_sells": len(sells)}
    b0 = execs[0]
    exp["buy"] = {
        "date": b0["session_date"], "price_cents": b0["price_cents"],
        "gap": cal.index(b0["session_date"]) - cal.index(out["entry_date"])
        if b0["session_date"] in cal else None,
        "days": _days_between(out["entry_date"], b0["session_date"]),
        "diff": "%+.2f" % _pct(b0["price_cents"] - rule_entry, rule_entry),
        "avg_cents": int((sum(Decimal(e["price_cents"] * e["shares"])
                              for e in execs if e["side"] == "BUY")
                          / sum(e["shares"] for e in execs if e["side"] == "BUY"))
                         .to_integral_value(rounding=ROUND_HALF_UP)),
    }
    sold = [e for e in execs if e["side"] == "SELL"]
    held = sum(e["shares"] for e in execs if e["side"] == "BUY") - \
        sum(e["shares"] for e in sold)
    exp["open_shares"] = held
    if sold and held == 0:
        px = int((sum(Decimal(e["price_cents"] * e["shares"]) for e in sold)
                  / sum(e["shares"] for e in sold))
                 .to_integral_value(rounding=ROUND_HALF_UP))
        last = sold[-1]["session_date"]
        exp["sell"] = {
            "date": last, "price_cents": px,
            "gap": cal.index(last) - cal.index(out["exit_date"])
            if last in cal and out["exit_date"] in cal else None,
            "days": _days_between(out["exit_date"], last),
            "diff": "%+.2f" % _pct(px - rule_exit, rule_exit),
            "live": None,
        }
        if out["reason"] == "time" and out.get("live_fill_date"):
            lp = _cents(out["live_fill_price"])
            exp["sell"]["live"] = {
                "date": out["live_fill_date"],
                "gap": cal.index(last) - cal.index(out["live_fill_date"])
                if last in cal and out["live_fill_date"] in cal else None,
                "diff": "%+.2f" % _pct(px - lp, lp),
            }
        cost = sum(e["price_cents"] * e["shares"] + e["fee_cents"]
                   for e in execs if e["side"] == "BUY")
        proceeds = sum(e["price_cents"] * e["shares"] - e["fee_cents"] - e["tax_cents"]
                       for e in sold)
        net = proceeds - cost
        pct = _pct(net, cost)
        exp["result"] = {
            "net_cents": net, "net_pct": "%+.2f" % pct,
            "rule_net": "%+.2f" % Decimal(str(out["ret_net_pct"])),
            "delta": "%+.2f" % _q(pct - Decimal(str(out["ret_net_pct"]))),
        }
    else:
        exp["sell"] = None
        exp["result"] = None
    return {"name": "recon_" + name, "rec": row, "execs": execs,
            "calendars": [cal], "expect": exp}


def reconcile_cases():
    cases = []
    # time exit, both live-fill rows exist: 1 session late at a dearer price,
    # sold two sessions after the live fill at a lower price
    row, out, fwd, t = build("time_live")
    cal = _days("2026-09-01", 40)
    ent, fill = out["entry_date"], out["exit_date"]
    i, x = cal.index(ent), cal.index(fill)
    cases.append(reconcile_case(
        "time_live", [(cal[i + 1], 100.45, 1000)], [(cal[x + 2], 96.2, 1000)], cal))
    # early (the signal day), cheaper than the rule, sold on the rule's own
    # exit day in two pieces at different prices
    row, out, fwd, t = build("tp_level")
    ent, fill = out["entry_date"], out["exit_date"]
    i, x = cal.index(ent), cal.index(fill)
    cases.append(reconcile_case(
        "tp_level", [(cal[i - 1], 51.7, 2000)],
        [(cal[x], 62.0, 1200), (cal[x], 62.9, 800)], cal))
    # two buys (staged entry), one sell: date/price of the FIRST buy, average shown
    row, out, fwd, t = build("stop_level")
    ent, fill = out["entry_date"], out["exit_date"]
    i, x = cal.index(ent), cal.index(fill)
    cases.append(reconcile_case(
        "stop_level", [(cal[i], 87.3, 1000), (cal[i + 1], 80.1, 1000)],
        [(cal[x + 1], 68.2, 2000)], cal))
    # still holding: no sell, no result
    cases.append(reconcile_case("late", [("2026-09-09", 100.2, 3000)], [], cal))
    # a calendar that does not know the dates: calendar days, not sessions
    row, out, fwd, t = build("lock_level")
    cases.append(reconcile_case(
        "lock_level", [("2026-09-09", 100.0, 1000)], [("2026-09-14", 101.5, 1000)], []))
    return cases


def grid():
    cases = []
    for name in SCENARIOS:
        for as_text in (False, True):
            row, out, fwd, t = build(name, as_text=as_text)
            cases.append({"name": name + (".text" if as_text else ".obj"),
                          "rec": row, "expect": expectations(out, row)})
    # an older closed recommendation: the eight summary keys, no record
    for name in ("stop_level", "time_live"):
        row, out, fwd, t = build(name, legacy=True)
        cases.append({"name": name + ".legacy", "rec": row,
                      "expect": expectations(out, row, legacy=True)})
    return {"cases": cases, "recon": reconcile_cases()}


class TheFixturesAreRealBackendRecords(unittest.TestCase):
    def test_every_scenario_passes_the_backends_own_record_check(self):
        for name in SCENARIOS:
            row, out, fwd, t = build(name)
            self.assertIsNone(rc._record_problem(row, out), name)
            self.assertEqual(out["record_schema"], 1)
            self.assertEqual(out["signal_session"], row["first_qualified_session"])
            self.assertEqual(out["planned_entry_session"], out["entry_date"])

    def test_the_scenarios_cover_every_exit_and_basis(self):
        seen = set()
        for name in SCENARIOS:
            row, out, fwd, t = build(name)
            seen.add((out["reason"], out["exit_basis"]))
        for want in (("stop", "level"), ("stop", "open"), ("tp", "level"),
                     ("tp", "open"), ("lock", "level"), ("lock", "open"),
                     ("late", "open"), ("time", "close")):
            self.assertIn(want, seen)

    def test_the_late_take_is_decided_the_session_before_it_fills(self):
        row, out, fwd, t = build("late")
        self.assertEqual(out["exit_decision_date"], fwd["date"].iloc[7])
        self.assertEqual(out["exit_fill_date"], fwd["date"].iloc[8])

    def test_the_time_exit_keeps_its_live_fill_and_the_ride_is_flagged(self):
        row, out, fwd, t = build("time_live")
        self.assertEqual(out["live_fill_date"], fwd["date"].iloc[10])
        row, out, fwd, t = build("time_nolive")
        self.assertIsNone(out["live_fill_date"])
        row, out, fwd, t = build("ride")
        self.assertEqual([p["day"] for p in out["path"] if p["ride"]], [10, 11])

    def test_a_legacy_outcome_really_has_no_record(self):
        row, out, fwd, t = build("stop_level", legacy=True)
        self.assertNotIn("path", out)
        self.assertNotIn("record_schema", out)
        self.assertEqual(sorted(out), sorted(
            ["entry_date", "entry_price", "exit_date", "exit_price", "reason",
             "bars", "ret_gross_pct", "ret_net_pct"]))

    def test_the_text_form_is_the_ledgers_json_text(self):
        row, out, fwd, t = build("stop_level", as_text=True)
        self.assertIsInstance(row["outcome"], str)
        self.assertEqual(json.loads(row["outcome"]), out)


@unittest.skipIf(NODE is None, "node is not installed")
class ThePhonePrintsWhatTheBackendWrote(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = grid()
        fd, path = tempfile.mkstemp(suffix=".json", prefix="yt_trade_grid_")
        with os.fdopen(fd, "w") as fh:
            json.dump(cls.payload, fh)
        try:
            cls.run_ = subprocess.run(
                [NODE, str(PROBE), "--trade-grid", path], cwd=str(ROOT),
                capture_output=True, timeout=300)
        finally:
            os.remove(path)
        text = cls.run_.stdout.decode("utf-8", "replace")
        cls.err = cls.run_.stderr.decode("utf-8", "replace")
        try:
            cls.out = json.loads(text)
        except ValueError:
            cls.out = {"pass": 0, "fail": -1,
                       "failures": ["unparseable probe output: " + text[-2000:]]}

    def test_every_check_passes(self):
        self.assertEqual(
            self.out.get("failures"), [],
            "probe failures:\n%s\n%s" % ("\n".join(self.out.get("failures") or []),
                                        self.err[-2000:]))
        self.assertEqual(self.run_.returncode, 0, self.err[-2000:])

    def test_every_backend_record_and_reconciliation_was_checked(self):
        self.assertEqual(self.out.get("trade_cases"), len(self.payload["cases"]))
        self.assertEqual(self.out.get("recon_cases"), len(self.payload["recon"]))
        self.assertGreaterEqual(len(self.payload["cases"]), 20)
        self.assertGreaterEqual(len(self.payload["recon"]), 5)


class TheRecordCodeStaysPrivateAndInsideTheRules(unittest.TestCase):
    """Source-level guards on the section of mobile/app.js that prints the
    record and reconciles the owner's fills."""

    @classmethod
    def setUpClass(cls):
        src = APP.read_text(encoding="utf-8")
        i = src.index("10c. Closed-trade record")
        j = src.index("// --- research-page lines for the new meta blocks")
        cls.src = src
        cls.block = src[i:j]

    def test_the_section_is_there(self):
        for fn in ("function tradeRecordView(", "function tradeRecordHtml(",
                   "function reconcileTrade(", "function recordsSectionHtml(",
                   "function sessionGap("):
            self.assertIn(fn, self.block)

    def test_the_owners_fills_never_leave_the_phone(self):
        # reading is fine; nothing here may transmit, store or log
        code = "\n".join(l for l in self.block.splitlines()
                         if not l.strip().startswith(("//", "*", "/*")))
        reads = code.replace("fetchJson(RECS_URL)", "")
        for bad in ("console.", "fetch(", "sendBeacon", "XMLHttpRequest",
                    "localStorage", "sessionStorage", "metaSet(", "dbPut(",
                    "dbDelete", "navigator.", "postMessage", "WebSocket"):
            self.assertNotIn(bad, reads, bad)
        # the one network read is the public recommendations file, no query
        self.assertEqual(code.count("fetchJson("), 1)
        self.assertIn('const RECS_URL = "./recommendations.json";', self.src)
        self.assertNotIn("recommendation_id=", self.block)

    def test_the_section_reads_no_scan_column(self):
        # tests/test_mobile_picks_ui.py fences scan columns to the registry
        self.assertIsNone(re.search(r"\b(?:r|row|x|t)\.[A-Z]", self.block))

    def test_no_wording_tells_the_owner_to_trim_a_position(self):
        for word in ("\u6e1b\u78bc", "\u6e1b\u5009", "\u964d\u90e8\u4f4d", "\u90e8\u4f4d\u6e1b\u91cf", "\u63d0\u524d\u8ce3", "\u5148\u8ce3\u4e00\u534a"):
            self.assertNotIn(word, self.block)

    def test_the_service_worker_knows_the_new_file(self):
        sw = (ROOT / "mobile" / "sw.js").read_text(encoding="utf-8")
        self.assertIn('"./recommendations.json"', sw)


class TheNewShellIsVersioned(unittest.TestCase):
    def test_version_33_everywhere(self):
        sw = (ROOT / "mobile" / "sw.js").read_text(encoding="utf-8")
        html = (ROOT / "mobile" / "index.html").read_text(encoding="utf-8")
        self.assertIn('const VERSION = "v33";', sw)
        self.assertEqual(re.findall(r"\?v=(\d+)", html), ["33", "33"])

    def test_the_readme_documents_the_view_and_the_contract(self):
        text = (ROOT / "mobile" / "README.md").read_text(encoding="utf-8")
        for key in ("recommendations.json", "exit_basis", "live_fill_date",
                    "record_schema", "path", "outcome"):
            self.assertIn(key, text)

    def test_the_app_is_still_clean_text(self):
        # git may check the file out with CRLF (autocrlf on Windows); what must
        # never appear is a NUL or a lone carriage return
        data = APP.read_bytes()
        self.assertNotIn(b"\x00", data)
        self.assertNotIn(b"\r", data.replace(b"\r\n", b"\n"))


if __name__ == "__main__":
    unittest.main()
