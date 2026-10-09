"""
The complete record of a closed trade (2026-10-09): signal day, fill day, the
session the exit was decided, the session it filled, the price basis, and the
day-by-day path. ASCII only.

    python -m unittest tests.test_trade_record_20261009 -v
"""
import json
from pathlib import Path
import unittest
from datetime import date, timedelta

import pandas as pd

from scanner.live_record import net_pct, replay_trade, trade_record


def _days(n, start=date(2026, 9, 7)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _fwd(bars, n_extra=0):
    """bars = [(o, h, l, c)]; flat bars are appended for n_extra more days."""
    bars = list(bars) + [(100.0, 100.5, 99.5, 100.0)] * n_extra
    ds = _days(len(bars))
    return pd.DataFrame([(d,) + tuple(b) for d, b in zip(ds, bars)],
                        columns=["date", "open", "high", "low", "close"])


def _rec(fwd):
    t = replay_trade(fwd)
    return t, trade_record(fwd, t)


FLAT = (100.0, 100.5, 99.5, 100.0)


class StopTpAndGaps(unittest.TestCase):
    def test_a_stop_inside_the_day_fills_at_the_level(self):
        fwd = _fwd([FLAT, (100, 100, 79.0, 80.5)])
        t, r = _rec(fwd)
        self.assertEqual(t["reason"], "stop")
        self.assertEqual(r["exit_basis"], "level")
        self.assertEqual(r["exit_price"], 80.0)
        self.assertEqual(r["exit_decision_date"], r["exit_fill_date"])
        self.assertEqual(r["exit_fill_date"], fwd["date"].iloc[1])
        self.assertEqual(r["entry_date"], fwd["date"].iloc[0])
        self.assertEqual(r["bars"], 2)
        self.assertEqual(len(r["path"]), 2)
        self.assertEqual(r["path"][-1]["status"], "exit_stop")
        self.assertIsNone(r["path"][-1]["stop"])

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        fwd = _fwd([FLAT, (78.0, 79.0, 77.0, 78.5)])
        t, r = _rec(fwd)
        self.assertEqual(r["exit_basis"], "open")
        self.assertEqual(r["exit_price"], 78.0)

    def test_take_profit_inside_the_day(self):
        fwd = _fwd([FLAT, (101, 121.0, 100, 119)])
        t, r = _rec(fwd)
        self.assertEqual(t["reason"], "tp")
        self.assertEqual((r["exit_basis"], r["exit_price"]), ("level", 120.0))
        self.assertEqual(r["mfe_pct"], 21.0)
        self.assertEqual(r["mae_pct"], -0.5)

    def test_cost_is_gross_minus_net(self):
        fwd = _fwd([FLAT, (101, 121.0, 100, 119)])
        t, r = _rec(fwd)
        self.assertAlmostEqual(r["cost_pct"], r["ret_gross_pct"] - r["ret_net_pct"],
                               places=2)
        self.assertAlmostEqual(r["ret_net_pct"], net_pct(20.0), places=2)


class ArmingAndTheLateTake(unittest.TestCase):
    def test_arming_shows_in_the_path_and_the_carried_stop(self):
        fwd = _fwd([FLAT, (100, 103, 99.8, 102.6), (102.4, 102.5, 101.0, 101.5)])
        t, r = _rec(fwd)
        self.assertEqual(t["reason"], "lock")
        self.assertEqual(r["path"][0]["status"], "holding")
        self.assertEqual(r["path"][1]["status"], "armed")
        self.assertEqual(r["path"][1]["stop"], 102.0)        # +2% lock
        self.assertEqual(r["path"][-1]["status"], "exit_lock")
        self.assertEqual(r["exit_basis"], "level")

    def test_a_late_take_is_decided_one_session_before_it_fills(self):
        bars = [FLAT] * 7 + [(100, 101.4, 99.8, 101.2),     # day 8, close +1.2%
                             (101.5, 102.0, 100.8, 101.0)]  # day 9 opens 101.5
        fwd = _fwd(bars)
        t, r = _rec(fwd)
        self.assertEqual(t["reason"], "late")
        self.assertEqual(r["exit_basis"], "open")
        self.assertEqual(r["exit_decision_date"], fwd["date"].iloc[7])
        self.assertEqual(r["exit_fill_date"], fwd["date"].iloc[8])
        self.assertEqual(r["exit_price"], 101.5)
        self.assertEqual(r["path"][7]["status"], "sell_next_open")
        self.assertEqual(r["path"][8]["status"], "exit_late")


class TimeExitAndTheRide(unittest.TestCase):
    def _falling(self, n):
        # a slow bleed: never +1%, never a 5-bar-mean close, stop untouched
        return [(100.0 - 0.4 * i, 100.2 - 0.4 * i, 99.5 - 0.4 * i, 99.8 - 0.4 * i)
                for i in range(n)]

    def test_time_exit_books_the_close_and_keeps_the_next_open(self):
        fwd = _fwd(self._falling(12))
        t, r = _rec(fwd)
        self.assertEqual(t["reason"], "time")
        self.assertEqual(r["bars"], 10)
        self.assertEqual(r["exit_basis"], "close")
        self.assertEqual(r["exit_price"], t["exit_price"] and round(t["exit_price"], 2))
        self.assertEqual(r["exit_decision_date"], r["exit_fill_date"])
        # the order placed that evening would fill at the next open
        self.assertEqual(r["live_fill_date"], fwd["date"].iloc[10])
        self.assertEqual(r["live_fill_price"], round(float(fwd["open"].iloc[10]), 2))

    def test_no_live_fill_without_a_next_bar(self):
        fwd = _fwd(self._falling(10))
        t, r = _rec(fwd)
        self.assertEqual(t["reason"], "time")
        self.assertIsNone(r["live_fill_date"])

    def test_the_ride_is_flagged_after_day_ten(self):
        # closes creep up but never reach +1% (no late take) and stay above
        # their own 5-bar mean at day 10 and 11, then fall back on day 12
        closes = [100.0, 99.5, 99.8, 99.6, 99.9, 100.0, 100.1, 100.3, 100.5,
                  100.8, 100.9, 100.0, 100.0]
        bars = [(c, c + 0.3, c - 0.4, c) for c in closes]
        fwd = _fwd(bars)
        t, r = _rec(fwd)
        self.assertEqual(t["reason"], "time")
        self.assertEqual(r["bars"], 12)
        rides = [p["day"] for p in r["path"] if p["ride"]]
        self.assertEqual(rides, [10, 11])  # day 10 tested and kept, day 11 too

class ShapeAndSafety(unittest.TestCase):
    def test_an_unusable_entry_gives_an_empty_record(self):
        fwd = _fwd([(0.0, 0.0, 0.0, 0.0)])
        self.assertEqual(trade_record(fwd, replay_trade(fwd)), {})
        self.assertEqual(trade_record(None, {}), {})

    def test_an_open_trade_has_a_path_and_no_exit(self):
        fwd = _fwd([FLAT, FLAT, FLAT])
        t = replay_trade(fwd)
        r = trade_record(fwd, t)
        self.assertFalse(t["exited"])
        self.assertEqual(len(r["path"]), 3)
        self.assertIsNone(r["exit_fill_date"])
        self.assertIsNone(r["ret_net_pct"])

    def test_the_record_is_plain_json(self):
        fwd = _fwd([FLAT, (101, 121.0, 100, 119)])
        _, r = _rec(fwd)
        self.assertEqual(json.loads(json.dumps(r, allow_nan=False)), r)

    def test_the_last_path_day_is_the_fill_day(self):
        for bars in ([FLAT, (100, 100, 79.0, 80.5)],
                     [FLAT, (101, 121.0, 100, 119)]):
            fwd = _fwd(bars)
            t, r = _rec(fwd)
            self.assertEqual(r["path"][-1]["date"], r["exit_fill_date"])
            self.assertEqual(r["bars"], len(r["path"]))


# --------------------------------------------------------------------------
# the stored recommendation carries it
# --------------------------------------------------------------------------
from tests.test_rec_lifecycle import CAL, Case, FLAT as LFLAT   # noqa: E402


class TheClosedRecommendationCarriesTheRecord(Case):
    def test_a_stop_is_recorded_end_to_end(self):
        rid = self.rec()
        self.series("8069", CAL[1], [LFLAT] * 3 + [(95, 96, 75, 78)] + [LFLAT] * 5)
        self.advance(CAL[8])
        out = json.loads(self.row(rid)["outcome"])
        # the long-standing summary is untouched
        self.assertEqual((out["entry_date"], out["exit_date"], out["exit_price"],
                          out["reason"], out["bars"]),
                         (CAL[1], CAL[4], 80.0, "stop", 4))
        # and the record sits next to it
        self.assertEqual(out["record_schema"], 1)
        self.assertEqual(out["signal_session"], CAL[0])
        self.assertEqual(out["planned_entry_session"], CAL[1])
        self.assertEqual(out["exit_decision_date"], CAL[4])
        self.assertEqual(out["exit_fill_date"], CAL[4])
        self.assertEqual(out["exit_basis"], "level")
        self.assertEqual([p["date"] for p in out["path"]], CAL[1:5])
        self.assertEqual(out["path"][-1]["status"], "exit_stop")
        self.assertEqual(out["mae_pct"], -25.0)

    def test_a_time_exit_names_the_next_open_it_would_really_fill_at(self):
        rid = self.rec()
        self.series("8069", CAL[1], [LFLAT] * 25)
        self.advance(CAL[20])
        out = json.loads(self.row(rid)["outcome"])
        self.assertEqual((out["reason"], out["exit_basis"]), ("time", "close"))
        self.assertEqual(out["exit_fill_date"], CAL[10])
        self.assertEqual(out["live_fill_date"], CAL[11])
        self.assertEqual(out["live_fill_price"], 100.0)
        self.assertEqual(len(out["path"]), out["bars"])

    def test_the_export_keeps_the_path(self):
        from portfolio.publish import export_recommendations
        rid = self.rec()
        self.series("8069", CAL[1], [LFLAT] * 3 + [(95, 96, 75, 78)] + [LFLAT] * 5)
        self.advance(CAL[8])
        dest = Path(self.tmp.name) / "recs.json"
        export_recommendations(self.ledger, dest)
        recs = json.loads(dest.read_text(encoding="utf-8"))["recommendations"]
        out = recs[0]["outcome"]
        if isinstance(out, str):
            out = json.loads(out)
        self.assertEqual(len(out["path"]), 4)


# --------------------------------------------------------------------------
# the checker replays the stored path independently
# --------------------------------------------------------------------------
import copy   # noqa: E402

from scanner.result_checks import check_payload   # noqa: E402
from scanner.scan_mode import STRATEGY_VERSION   # noqa: E402
from tests.test_result_checks import clean_payload, clean_quotes   # noqa: E402


class TheCheckerReplaysTheStoredPath(Case):
    def _exported(self, rows, advance_to):
        from portfolio.publish import export_recommendations
        rid = self.rec()
        self.series("8069", CAL[1], rows)
        self.advance(CAL[advance_to])
        dest = Path(self.tmp.name) / "recs.json"
        export_recommendations(self.ledger, dest)
        recs = json.loads(dest.read_text(encoding="utf-8"))
        for r in recs["recommendations"]:
            if isinstance(r.get("outcome"), str):
                r["outcome"] = json.loads(r["outcome"])
        return recs

    def _codes(self, recs, level=None):
        p = clean_payload()
        rep = check_payload(p, quotes=clean_quotes(p["rows"]), recs=recs)
        return {i["code"] for i in rep["items"] if level is None or i["level"] == level}

    def _stop(self):
        return self._exported([LFLAT] * 3 + [(95, 96, 75, 78)] + [LFLAT] * 5, 8)

    def test_a_clean_record_passes(self):
        c = self._codes(self._stop(), "error")
        self.assertNotIn("rec_record_mismatch", c)

    def test_a_time_exit_record_passes(self):
        recs = self._exported([LFLAT] * 25, 20)
        self.assertNotIn("rec_record_mismatch", self._codes(recs, "error"))

    def _tampered(self, edit):
        recs = copy.deepcopy(self._stop())
        edit(recs["recommendations"][0]["outcome"])
        return self._codes(recs, "error")

    def test_a_path_that_never_touched_the_stop_is_caught(self):
        def edit(o):
            o["path"][-1]["low"] = 90.0       # the stop was never reached
        self.assertIn("rec_record_mismatch", self._tampered(edit))

    def test_a_wrong_exit_reason_is_caught(self):
        def edit(o):
            o["reason"] = "tp"
        self.assertIn("rec_record_mismatch", self._tampered(edit))

    def test_a_shifted_fill_day_is_caught(self):
        def edit(o):
            o["exit_fill_date"] = CAL[5]
        self.assertIn("rec_record_mismatch", self._tampered(edit))

    def test_an_entry_on_another_day_than_planned_is_caught(self):
        recs = copy.deepcopy(self._stop())
        recs["recommendations"][0]["valid_until_session"] = CAL[2]
        self.assertIn("rec_record_mismatch", self._codes(recs, "error"))

    def test_a_return_that_does_not_follow_the_prices_is_caught(self):
        def edit(o):
            o["ret_gross_pct"] = 5.0
        self.assertIn("rec_record_mismatch", self._tampered(edit))

    def test_a_record_from_another_strategy_version_is_not_judged(self):
        recs = copy.deepcopy(self._stop())
        r = recs["recommendations"][0]
        r["strategy_version"] = "prelaunch-2000-01-01"
        r["outcome"]["reason"] = "tp"
        self.assertNotIn("rec_record_mismatch", self._codes(recs))

    def test_an_old_outcome_without_a_path_is_information(self):
        recs = copy.deepcopy(self._stop())
        o = recs["recommendations"][0]["outcome"]
        for k in ("path", "record_schema", "exit_decision_date"):
            o.pop(k, None)
        c = self._codes(recs)
        self.assertIn("rec_record_legacy", c)
        self.assertNotIn("rec_record_mismatch", c)


if __name__ == "__main__":
    unittest.main()
