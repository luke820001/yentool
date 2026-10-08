"""
The recommendation lifecycle (2026-10-08, "Model T"): portfolio.ledger's
transition primitives, schema v2, portfolio.sync.advance_recommendations,
the same-session retraction and the grace attach.

Temp ledgers, injected bars and calendars: no price store, no network.

    python -m unittest tests.test_rec_lifecycle -v
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

from portfolio import ledger as L
from portfolio import schema
from portfolio import sync
from portfolio.publish import export_recommendations, seed_from_export
from scanner.exit_rules import DEFAULT_RULE
from scanner.live_record import _replay, net_pct, replay_trade
from scanner.scan_mode import STRATEGY_VERSION

MODE = "mode_prelaunch"
SV = STRATEGY_VERSION
OLD_SV = "prelaunch-2026-09-09"


def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


CAL = weekdays("2026-08-03", 45)
FLAT = (100, 101, 99, 100)


def next_in(cal):
    def fn(day, calendar=None):
        later = [d for d in (calendar or cal) if d > day]
        return later[0] if later else None
    return fn


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = str(Path(self.tmp.name) / "portfolio_ledger.db")
        self.bars = {}

    # -- fixtures -------------------------------------------------------------
    def rec(self, sid="8069", fq=CAL[0], vu="next", price="100",
            version=SV, stop="80", target="120"):
        if vu == "next":
            vu = CAL[CAL.index(fq) + 1]
        conn = L.open_ledger(self.ledger)
        try:
            rid, created = L.record_recommendation(
                conn, sid, MODE, version, fq, price, stop_price=stop,
                target_price=target, valid_until_session=vu)
        finally:
            conn.close()
        self.assertTrue(created)
        return rid

    def series(self, sid, start, rows, skip=()):
        """Bars on consecutive calendar sessions from `start`, minus `skip`."""
        i = CAL.index(start)
        out = []
        for r in rows:
            while CAL[i] in skip:
                i += 1
            out.append((CAL[i],) + tuple(float(x) for x in r))
            i += 1
        self.bars[sid] = out
        return out

    def advance(self, session, **kw):
        kw.setdefault("calendar", CAL)
        kw.setdefault("extend_if", None)
        kw.setdefault("bars_for", lambda pairs, upto: self.bars)
        kw.setdefault("next_session_fn", next_in(CAL))
        return sync.advance_recommendations(self.ledger, MODE, session, SV, **kw)

    def row(self, rid):
        conn = L.open_ledger(self.ledger)
        try:
            return dict(conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (rid,)).fetchone())
        finally:
            conn.close()

    def events(self, rid):
        conn = L.open_ledger(self.ledger)
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT event_type, reason_code, effective_session FROM "
                "recommendation_events WHERE recommendation_id = ? "
                "ORDER BY event_id", (rid,))]
        finally:
            conn.close()

    @staticmethod
    def fwd(bars):
        return pd.DataFrame(bars[:DEFAULT_RULE["ride_cap"] + 1],
                            columns=["date", "open", "high", "low", "close"])


# --------------------------------------------------------------------------
# ledger primitives and schema v2
# --------------------------------------------------------------------------
class TestSchemaV2(unittest.TestCase):
    def test_fresh_ledger_has_the_lifecycle_columns(self):
        conn = L.open_ledger(":memory:")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)")}
        conn.close()
        self.assertTrue({"status_reason", "status_session", "outcome"} <= cols)
        self.assertEqual(schema.SCHEMA_VERSION, 2)

    def test_a_v1_ledger_upgrades_with_its_rows_intact(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "v1.db")
            conn = schema.connect(path)
            with conn:
                for stmt in schema.MIGRATIONS[1]:
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_meta(key, value) "
                             "VALUES('schema_version', '1')")
                conn.execute(
                    "INSERT INTO recommendations (recommendation_id, stock_id, "
                    "strategy, strategy_version, cycle_seq, "
                    "first_qualified_session, recommended_at, initial_buy_price, "
                    "status) VALUES ('rec-1815-mode_prelaunch-1', '1815', ?, ?, "
                    "1, '2026-09-09', 't', '126.00', 'active')", (MODE, OLD_SV))
            conn.close()
            conn = L.open_ledger(path)
            try:
                self.assertEqual(schema.current_version(conn), 2)
                row = dict(conn.execute("SELECT * FROM recommendations").fetchone())
            finally:
                conn.close()
        self.assertEqual(row["initial_buy_price"], "126.00")
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["status_reason"])
        self.assertIsNone(row["status_session"])
        self.assertIsNone(row["outcome"])


class TestTransition(Case):
    def test_close_sets_four_fields_and_one_event(self):
        rid = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            self.assertTrue(L.transition_recommendation(
                conn, rid, "closed", "time", CAL[10], outcome={"bars": 10}))
            self.assertFalse(L.transition_recommendation(
                conn, rid, "closed", "time", CAL[10]))
            with self.assertRaises(L.LedgerError):
                L.transition_recommendation(conn, rid, "active", "x", CAL[10])
        finally:
            conn.close()
        r = self.row(rid)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("closed", "time", CAL[10]))
        self.assertEqual(json.loads(r["outcome"]), {"bars": 10})
        self.assertEqual(self.events(rid)[1:], [("closed", "time", CAL[10])])
        self.assertEqual(r["initial_buy_price"], "100.00")

    def test_a_terminal_recommendation_frees_the_cycle(self):
        rid = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, rid, "closed", "tp", CAL[4])
            rid2, created = L.record_recommendation(conn, "8069", MODE, SV,
                                                    CAL[6], "130")
        finally:
            conn.close()
        self.assertTrue(created)
        self.assertTrue(rid2.endswith("-2"))

    def test_converted_is_never_touched(self):
        rid = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            L.open_position(conn, "8069", recommendation_id=rid, strategy=MODE)
            self.assertFalse(L.transition_recommendation(
                conn, rid, "expired", "no_fill", CAL[1]))
        finally:
            conn.close()
        self.assertEqual(self.row(rid)["status"], "converted")

    def test_set_valid_until(self):
        rid = self.rec(vu=None)
        conn = L.open_ledger(self.ledger)
        try:
            self.assertTrue(L.set_valid_until(conn, rid, CAL[1], "backfill"))
            self.assertFalse(L.set_valid_until(conn, rid, CAL[1], "backfill"))
            self.assertTrue(L.set_valid_until(conn, rid, CAL[2], "market_closed"))
            L.transition_recommendation(conn, rid, "expired", "no_fill", CAL[2])
            self.assertFalse(L.set_valid_until(conn, rid, CAL[3], "backfill"))
        finally:
            conn.close()
        self.assertEqual(self.row(rid)["valid_until_session"], CAL[2])
        kinds = [e[0] for e in self.events(rid)]
        self.assertEqual(kinds, ["created", "window_set", "window_moved", "expired"])


# --------------------------------------------------------------------------
# advance_recommendations
# --------------------------------------------------------------------------
class TestAdvance(Case):
    def test_a_flat_trade_closes_on_time(self):
        rid = self.rec()
        bars = self.series("8069", CAL[1], [FLAT] * 25)
        st = self.advance(CAL[20])
        self.assertEqual(st["closed"], 1, st)
        r = self.row(rid)
        self.assertEqual(r["status"], "closed")
        self.assertEqual(r["status_reason"], "time")
        self.assertEqual(r["status_session"], CAL[10])     # the 10th bar
        out = json.loads(r["outcome"])
        t = replay_trade(self.fwd(bars))
        self.assertEqual(out["ret_net_pct"], round(net_pct(t["ret_gross_pct"]), 2))
        self.assertEqual(out["entry_date"], CAL[1])
        self.assertEqual(out["bars"], 10)
        self.assertEqual(st["transitions"], [[rid, "closed", "time", CAL[10]]])

    def test_a_crash_stops_out(self):
        rid = self.rec()
        self.series("8069", CAL[1], [FLAT] * 3 + [(95, 96, 75, 78)] + [FLAT] * 5)
        self.advance(CAL[8])
        r = self.row(rid)
        self.assertEqual((r["status"], r["status_reason"]), ("closed", "stop"))
        self.assertEqual(r["status_session"], CAL[4])
        self.assertEqual(json.loads(r["outcome"])["exit_price"], 80.0)

    def test_an_open_trade_stays_active(self):
        rid = self.rec()
        self.series("8069", CAL[1], [FLAT] * 3)
        st = self.advance(CAL[3])
        self.assertEqual(self.row(rid)["status"], "active")
        self.assertEqual(st["closed"] + st["expired"], 0)

    def test_the_window_is_open_until_the_entry_session(self):
        rid = self.rec()
        self.advance(CAL[0])
        self.assertEqual(self.row(rid)["status"], "active")

    def test_no_bar_on_the_entry_session_is_no_fill(self):
        # the market traded CAL[1]; the stock did not (halted), later it did
        rid = self.rec()
        self.series("8069", CAL[2], [FLAT] * 5)
        self.advance(CAL[6])
        r = self.row(rid)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("expired", "no_fill", CAL[1]))

    def test_an_unpriceable_entry_bar_is_no_fill(self):
        rid = self.rec()
        self.series("8069", CAL[1], [(0, 101, 99, 100)] + [FLAT] * 4)
        self.advance(CAL[5])
        self.assertEqual(self.row(rid)["status_reason"], "no_fill")

    def test_no_data_yet_waits(self):
        rid = self.rec()
        self.advance(CAL[4])
        self.assertEqual(self.row(rid)["status"], "active")

    def test_a_closed_market_moves_the_entry_session(self):
        # the rec said CAL[1]; the market shut that day (a typhoon): the price
        # calendar never got the session
        cal = [d for d in CAL if d != CAL[1]]
        rid = self.rec(vu=CAL[1])
        self.series("8069", CAL[2], [FLAT] * 3)
        st = self.advance(CAL[2], calendar=cal)
        r = self.row(rid)
        self.assertEqual(r["status"], "active")
        self.assertEqual(r["valid_until_session"], CAL[2])
        self.assertEqual(st["moved"], 1)
        self.assertEqual(self.events(rid)[-1], ("window_moved", "market_closed", CAL[2]))
        # and the trade then runs from the real entry
        self.series("8069", CAL[2], [FLAT] * 12)
        self.advance(CAL[13], calendar=cal)
        r = self.row(rid)
        self.assertEqual((r["status"], r["status_reason"]), ("closed", "time"))
        self.assertEqual(json.loads(r["outcome"])["entry_date"], CAL[2])

    def test_a_missing_entry_session_is_backfilled(self):
        rid = self.rec(vu=None)
        st = self.advance(CAL[0], next_session_fn=None)    # the real helper
        self.assertEqual(st["backfilled"], 1)
        self.assertEqual(self.row(rid)["valid_until_session"], CAL[1])
        self.assertEqual(self.events(rid)[-1], ("window_set", "backfill", CAL[1]))

    def test_rule_change_inside_the_window_supersedes(self):
        old = self.rec(version=OLD_SV)
        same = self.rec(sid="8070")
        st = self.advance(CAL[0])
        self.assertEqual(st["superseded"], 1)
        r = self.row(old)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("superseded", "rule_version", CAL[0]))
        self.assertEqual(self.row(same)["status"], "active")

    def test_rule_change_after_entry_does_not_supersede(self):
        rid = self.rec(version=OLD_SV)
        self.series("8069", CAL[1], [FLAT] * 3)
        self.advance(CAL[3])
        self.assertEqual(self.row(rid)["status"], "active")

    def test_horizon_safety_net(self):
        rid = self.rec()
        horizon = sync.REC_HORIZON_SESSIONS
        self.assertEqual(horizon, DEFAULT_RULE["ride_cap"] + 5)
        self.advance(CAL[1 + horizon])          # exactly 25 sessions past
        self.assertEqual(self.row(rid)["status"], "active")
        self.advance(CAL[2 + horizon])
        r = self.row(rid)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("expired", "horizon_elapsed", CAL[2 + horizon]))

    def test_converted_is_untouched(self):
        rid = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            L.open_position(conn, "8069", recommendation_id=rid, strategy=MODE)
        finally:
            conn.close()
        self.series("8069", CAL[1], [FLAT] * 25)
        st = self.advance(CAL[30])
        self.assertEqual(self.row(rid)["status"], "converted")
        self.assertEqual(st["transitions"], [])

    def test_idempotent(self):
        a = self.rec()
        b = self.rec(sid="8070", vu=None)
        c = self.rec(sid="8071")
        self.series("8069", CAL[1], [FLAT] * 25)
        self.series("8071", CAL[3], [FLAT] * 5)
        first = self.advance(CAL[20])
        self.assertTrue(first["closed"] and first["backfilled"] and first["expired"])
        before = {r: self.events(r) for r in (a, b, c)}
        second = self.advance(CAL[20])
        for k in ("backfilled", "moved", "closed", "expired", "superseded"):
            self.assertEqual(second[k], 0, k)
        self.assertEqual({r: self.events(r) for r in (a, b, c)}, before)

    def test_a_record_error_does_not_stop_the_others(self):
        a = self.rec()
        b = self.rec(sid="8070")
        self.series("8069", CAL[1], [FLAT] * 25)
        self.series("8070", CAL[1], [FLAT] * 25)

        def flaky(pairs, upto):
            bars = dict(self.bars)
            bars["8069"] = [5]                       # not a bar at all
            return bars
        st = self.advance(CAL[20], bars_for=flaky)
        self.assertIsNotNone(st["error"])
        self.assertEqual(self.row(b)["status"], "closed")
        self.assertEqual(self.row(a)["status"], "active")


class TestParity(Case):
    """advance books exactly what live_record books for the same bars."""

    SERIES = {
        "time": [FLAT] * 25,
        "stop": [FLAT] * 3 + [(95, 96, 75, 78)] + [FLAT] * 20,
        "tp": [FLAT] * 2 + [(105, 110, 104, 109), (110, 125, 109, 124)]
        + [(124, 125, 123, 124)] * 20,
        "lock": [FLAT, (100, 104, 99, 103), (103, 104, 100, 101)]
        + [(101, 101, 99, 100)] * 20,
    }

    def test_same_trade_as_live_record(self):
        for i, (want, rows) in enumerate(sorted(self.SERIES.items())):
            sid = str(9000 + i)
            rid = self.rec(sid=sid)
            bars = self.series(sid, CAL[1], rows)
            self.advance(CAL[26])
            r = self.row(rid)
            ret, reason, nbars = _replay(self.fwd(bars))
            out = json.loads(r["outcome"])
            self.assertEqual((r["status_reason"], out["bars"], out["ret_net_pct"]),
                             (reason, nbars, round(ret, 2)), want)
            self.assertEqual(reason, want)

    def test_same_trade_with_the_market_leg(self):
        rid = self.rec()
        bars = self.series("8069", CAL[1], [FLAT] * 25)
        leg = lambda i, d: True                      # the market stays disturbed
        self.advance(CAL[26], extend_if=leg)
        t = replay_trade(self.fwd(bars), extend_if=leg)
        r = self.row(rid)
        self.assertEqual(r["status_session"], t["exit_date"])
        self.assertEqual(json.loads(r["outcome"])["bars"], t["bars"])
        self.assertEqual(t["bars"], DEFAULT_RULE["ride_cap"])


def leg_with_table(table):
    """A market-leg callback shaped like scanner.market_leg.make_disturbed_fn:
    fn(i, date) -> disturbed, with the TAIEX table it reads as `.table`."""
    def fn(i, d):
        return bool(table.get(str(d)[:10], False))
    fn.table = table
    return fn


class TestMarketLegLag(Case):
    """A time exit on a session the TAIEX table does not reach yet is not
    booked: a missing index bar reads "not disturbed", and closing on it
    would be permanent even when the market leg later keeps the trade."""

    def setUp(self):
        super().setUp()
        self.rid = self.rec()
        self.series("8069", CAL[1], [FLAT] * 25)  # a flat close: no stock leg

    def test_the_exit_waits_for_the_index(self):
        leg = leg_with_table({d: False for d in CAL[:10]})     # ends CAL[9]
        st = self.advance(CAL[10], extend_if=leg)
        self.assertEqual(self.row(self.rid)["status"], "active")
        self.assertEqual((st["deferred"], st["closed"]), (1, 0), st)
        self.assertEqual(st["transitions"],
                         [[self.rid, "active", "exit_deferred", CAL[10]]])
        self.assertEqual([e[0] for e in self.events(self.rid)], ["created"])
        self.assertIn("1 deferred", sync.summarize_advance(st))
        self.assertEqual(sync.rec_meta({}, st)["deferred"], 1)
        # the index catches up, still calm: the same time exit is booked
        leg = leg_with_table({d: False for d in CAL[:11]})
        st = self.advance(CAL[10], extend_if=leg)
        r = self.row(self.rid)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("closed", "time", CAL[10]))
        self.assertEqual(st["deferred"], 0)

    def test_a_late_disturbed_reading_keeps_the_trade(self):
        # the case the deferral exists for: the day-10 market WAS a pullback
        self.advance(CAL[10], extend_if=leg_with_table(
            {d: False for d in CAL[:10]}))
        table = {d: False for d in CAL[:12]}
        table[CAL[10]] = True                  # rides day 10, exits day 11
        self.advance(CAL[11], extend_if=leg_with_table(table))
        r = self.row(self.rid)
        t = replay_trade(self.fwd(self.bars["8069"]),
                         extend_if=leg_with_table(table))
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("closed", "time", CAL[11]))
        self.assertEqual(json.loads(r["outcome"])["bars"], t["bars"])
        self.assertEqual(t["bars"], 11)

    def test_an_unreadable_index_or_a_plain_callback_is_final(self):
        for n, leg in enumerate((leg_with_table({}), lambda i, d: False)):
            sid = str(8070 + n)
            rid = self.rec(sid=sid)
            self.series(sid, CAL[1], [FLAT] * 25)
            st = self.advance(CAL[10], extend_if=leg)
            r = self.row(rid)
            self.assertEqual((r["status"], r["status_session"]),
                             ("closed", CAL[10]))
            self.assertEqual(st["deferred"], 0)

    def test_the_cap_bar_never_asks_the_market(self):
        # disturbed through CAL[19]; the cap bar CAL[20] is past the table,
        # but replay_exit does not consult the leg there, so it is final
        leg = leg_with_table({d: True for d in CAL[:20]})
        st = self.advance(CAL[20], extend_if=leg)
        r = self.row(self.rid)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("closed", "time", CAL[20]))
        self.assertEqual(json.loads(r["outcome"])["bars"],
                         DEFAULT_RULE["ride_cap"])
        self.assertEqual(st["deferred"], 0)

    def test_a_price_exit_is_never_deferred(self):
        rid = self.rec(sid="8072")
        self.series("8072", CAL[1], [FLAT] * 3 + [(95, 96, 75, 78)] + [FLAT] * 5)
        st = self.advance(CAL[8], extend_if=leg_with_table(
            {d: False for d in CAL[:2]}))
        self.assertEqual(self.row(rid)["status_reason"], "stop")
        self.assertEqual(st["deferred"], 0)

    def test_the_horizon_ends_the_wait(self):
        leg = leg_with_table({d: False for d in CAL[:10]})
        horizon = CAL.index(CAL[1]) + 1 + sync.REC_HORIZON_SESSIONS
        self.advance(CAL[horizon - 1], extend_if=leg)
        self.assertEqual(self.row(self.rid)["status"], "active")
        self.advance(CAL[horizon], extend_if=leg)
        r = self.row(self.rid)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("closed", "time", CAL[10]))

    def test_idempotent_while_deferred(self):
        leg = leg_with_table({d: False for d in CAL[:10]})
        self.advance(CAL[10], extend_if=leg)
        before = self.row(self.rid)
        st = self.advance(CAL[10], extend_if=leg)
        self.assertEqual(self.row(self.rid), before)
        self.assertEqual(st["deferred"], 1)


# --------------------------------------------------------------------------
# attach: retraction, grace, anchors
# --------------------------------------------------------------------------
def rows(*specs):
    """DataFrame rows: (sid, buy_ready, block) with a 100 close."""
    out = []
    for sid, ready, block in specs:
        out.append({"Stock_ID": sid, "Stock_Name": "T", "Market": "OTC",
                    "Data_Date": CAL[0], "Close_Price": 100.0,
                    "Suggested_Buy_Price": 100.0, "Strict_Stop_Loss": 80.0,
                    "Target_Price": 120.0, "Buy_Ready": ready, "Buy_Block": block})
    return pd.DataFrame(out)


class TestAttach(Case):
    def attach(self, df, session=CAL[0], **kw):
        kw.setdefault("calendar", CAL)
        return sync.attach_recommendations(df, MODE, SV, self.ledger,
                                           session_date=session, **kw)

    def test_next_session_is_stored_and_ids_reported(self):
        out, st = self.attach(rows(("8069", True, "")), next_session=CAL[1])
        self.assertEqual(out["Rec_Valid_Until"].iloc[0], CAL[1])
        self.assertEqual(st["created_ids"], [out["Recommendation_ID"].iloc[0]])
        self.assertTrue(st["writes"])
        self.assertIsNone(out["Rec_Status_Reason"].iloc[0])
        self.assertEqual(list(sync.REC_COLUMNS)[-1], "Rec_Status_Reason")

    def test_same_session_rerun_retracts(self):
        out, _ = self.attach(rows(("8069", True, ""), ("8070", True, ""),
                                  ("8071", True, "")), next_session=CAL[1])
        ids = dict(zip(out["Stock_ID"], out["Recommendation_ID"]))
        out2, st = self.attach(rows(("8069", False, "quality"),
                                    ("8070", False, "held")))
        self.assertEqual(st["superseded"], 2)
        a, b, c = (self.row(ids[s]) for s in ("8069", "8070", "8071"))
        self.assertEqual((a["status"], a["status_reason"], a["status_session"]),
                         ("superseded", "retracted:quality", CAL[0]))
        self.assertEqual(b["status"], "active")                 # 'held' keeps it
        self.assertEqual((c["status"], c["status_reason"]),
                         ("superseded", "retracted:off_list"))
        got = dict(zip(out2["Stock_ID"], out2["Recommendation_ID"]))
        self.assertTrue(pd.isna(got["8069"]))      # pandas may say NaN
        self.assertEqual(got["8070"], ids["8070"])
        # a later run of the same session that qualifies it again opens cycle 2
        out3, st3 = self.attach(rows(("8069", True, ""), ("8070", True, ""),
                                     ("8071", True, "")), next_session=CAL[1])
        self.assertEqual(st3["created"], 2)
        self.assertTrue(dict(zip(out3["Stock_ID"],
                                 out3["Recommendation_ID"]))["8069"].endswith("-2"))

    def test_no_retraction_on_another_session_read_only_or_switched_off(self):
        out, _ = self.attach(rows(("8069", True, "")), next_session=CAL[1])
        rid = out["Recommendation_ID"].iloc[0]
        self.attach(rows(("8069", False, "quality")), session=CAL[1])
        self.attach(rows(("8069", False, "quality")), allow_writes=False)
        self.attach(rows(("8070", True, "")).drop(columns=["Buy_Ready"]))
        with mock.patch.object(sync, "RETRACT_SAME_SESSION", False):
            self.attach(rows(("8069", False, "quality")))
        self.assertEqual(self.row(rid)["status"], "active")

    def test_read_only_attach_creates_nothing(self):
        out, st = self.attach(rows(("8069", True, "")), next_session=CAL[1],
                              allow_writes=False)
        self.assertEqual(st["created"], 0)
        self.assertFalse(st["writes"])
        for col in sync.REC_COLUMNS:
            self.assertIn(col, out.columns)
            self.assertIsNone(out[col].iloc[0])
        conn = sqlite3.connect(self.ledger)
        n = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_closed_recommendation_attaches_for_the_grace_window(self):
        rid = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, rid, "closed", "time", CAL[10])
        finally:
            conn.close()
        df = rows(("8069", False, "held"))
        last = CAL[10 + sync.CLOSED_GRACE_SESSIONS - 1]
        out, _ = self.attach(df, session=last)
        self.assertEqual(out["Rec_Status"].iloc[0], "closed")
        self.assertEqual(out["Rec_Status_Reason"].iloc[0], "time")
        out, _ = self.attach(df, session=CAL[10 + sync.CLOSED_GRACE_SESSIONS])
        self.assertTrue(pd.isna(out["Recommendation_ID"].iloc[0]))

    def test_active_wins_over_a_closed_one(self):
        rid = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, rid, "closed", "tp", CAL[3])
        finally:
            conn.close()
        rid2 = self.rec(fq=CAL[4])
        out, _ = self.attach(rows(("8069", False, "held")), session=CAL[5])
        self.assertEqual(out["Recommendation_ID"].iloc[0], rid2)

    def test_the_newest_cycle_wins_among_closed_ones(self):
        """T5: _attachable ranks by (status, -cycle_seq); a name that closed
        twice inside the grace window must show the LATER cycle, not the
        first one the table returns."""
        first = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, first, "closed", "tp", CAL[3])
        finally:
            conn.close()
        second = self.rec(fq=CAL[4])
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, second, "closed", "time", CAL[6])
        finally:
            conn.close()
        self.assertTrue(second.endswith("-2"))
        # both are inside the grace window on CAL[7]
        self.assertLessEqual(7 - 3, sync.CLOSED_GRACE_SESSIONS)
        out, _ = self.attach(rows(("8069", False, "held")), session=CAL[7])
        self.assertEqual(out["Recommendation_ID"].iloc[0], second)
        self.assertEqual(out["Rec_Status_Reason"].iloc[0], "time")

    def test_expired_and_superseded_never_attach(self):
        rid = self.rec()
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, rid, "expired", "no_fill", CAL[1])
        finally:
            conn.close()
        out, _ = self.attach(rows(("8069", False, "held")), session=CAL[2])
        self.assertTrue(pd.isna(out["Recommendation_ID"].iloc[0]))

    def test_rec_anchors(self):
        a = self.rec(stop="80.03", target="120.01")
        b = self.rec(sid="8070")
        c = self.rec(sid="8071")
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, b, "closed", "tp", CAL[3])
            L.transition_recommendation(conn, c, "expired", "no_fill", CAL[1])
        finally:
            conn.close()
        got = sync.load_rec_anchors(self.ledger, MODE, CAL[4], calendar=CAL)
        self.assertEqual(sorted(got), ["8069", "8070"])
        self.assertEqual(got["8069"]["anchor"], CAL[0])
        self.assertEqual(got["8069"]["rec_id"], a)
        self.assertEqual(got["8069"]["stop"], 80.0)       # down the ladder
        self.assertEqual(got["8069"]["target"], 120.5)    # up the ladder
        self.assertEqual(got["8070"]["status"], "closed")
        self.assertEqual(sync.load_rec_anchors(
            str(Path(self.tmp.name) / "nope" / "x" / "y.db") + "\0", MODE), {})


# --------------------------------------------------------------------------
# the default bar source: the tracker's own reader over the price store
# --------------------------------------------------------------------------
class TestStoreBars(unittest.TestCase):
    """T5: every other test injects bars_for; the default path (_store_bars ->
    holding_tracker._bars_since) only ran on the real store."""

    def setUp(self):
        import scanner.holding_tracker as tracker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "pv.db"
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, open REAL, "
                         "high REAL, low REAL, close REAL)")
            for sid in ("8069", "8070"):
                for i, d in enumerate(CAL[:8]):
                    conn.execute("INSERT INTO data VALUES (?,?,?,?,?,?)",
                                 (sid, d, 100 + i, 101 + i, 99 + i, 100 + i))
            conn.commit()
        finally:
            conn.close()
        p = mock.patch.object(tracker, "PRICE_VOLUME_FILE", self.db)
        p.start()
        self.addCleanup(p.stop)

    def test_bars_come_from_the_entry_session_through_upto(self):
        got = sync._store_bars([("8069", CAL[2])], CAL[5], set(CAL))
        self.assertEqual([b[0] for b in got["8069"]], CAL[2:6])
        self.assertEqual(got["8069"][0], (CAL[2], 102.0, 103.0, 101.0, 102.0))
        self.assertNotIn("8070", got)

    def test_only_traded_sessions_are_kept(self):
        cal_set = set(CAL) - {CAL[3]}
        got = sync._store_bars([("8069", CAL[2]), ("8070", CAL[4])], CAL[6],
                               cal_set)
        self.assertEqual([b[0] for b in got["8069"]],
                         [CAL[2], CAL[4], CAL[5], CAL[6]])
        # one query for the whole list: every name starts at the earliest
        # requested date (advance_recommendations filters per record)
        self.assertEqual([b[0] for b in got["8070"]],
                         [CAL[2], CAL[4], CAL[5], CAL[6]])

    def test_a_missing_store_gives_no_bars_not_an_error(self):
        import scanner.holding_tracker as tracker
        with mock.patch.object(tracker, "PRICE_VOLUME_FILE",
                               Path(self.tmp.name) / "nope" / "x.db"):
            self.assertEqual(sync._store_bars([("8069", CAL[0])], CAL[5],
                                              set(CAL)), {})

    def test_advance_uses_the_store_when_no_source_is_injected(self):
        """The whole default path: a rec whose entry bar is in the file."""
        conn = L.open_ledger(str(Path(self.tmp.name) / "l.db"))
        try:
            rid, _ = L.record_recommendation(
                conn, "8069", MODE, SV, CAL[0], "100", stop_price="80",
                target_price="120", valid_until_session=CAL[1])
        finally:
            conn.close()
        st = sync.advance_recommendations(
            str(Path(self.tmp.name) / "l.db"), MODE, CAL[3], SV, calendar=CAL,
            extend_if=None, next_session_fn=next_in(CAL))
        self.assertIsNone(st["error"])
        self.assertEqual(st["expired"], 0)       # the entry bar was found
        conn = L.open_ledger(str(Path(self.tmp.name) / "l.db"))
        try:
            row = conn.execute("SELECT status FROM recommendations WHERE "
                               "recommendation_id = ?", (rid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "active")


# --------------------------------------------------------------------------
# the CI flow: rebuilt from the JSON every run, published without freelist
# --------------------------------------------------------------------------
class TestCiFlow(Case):
    def test_rebuild_advance_create_export_is_publishable(self):
        from tools.check_ledger_public import check
        a = self.rec()                         # will close on time
        b = self.rec(sid="8070", vu=None)      # backfilled, then waits
        self.series("8069", CAL[1], [FLAT] * 25)
        export = Path(self.tmp.name) / "recommendations.json"
        export_recommendations(self.ledger, export)

        # a CI run: fresh ledger from the JSON, advance, create, export
        ci = str(Path(self.tmp.name) / "ci.db")
        self.assertEqual(seed_from_export(ci, export), 2)
        st = sync.advance_recommendations(
            ci, MODE, CAL[12], SV, bars_for=lambda p, u: self.bars,
            calendar=CAL, extend_if=None, next_session_fn=next_in(CAL))
        self.assertEqual((st["closed"], st["backfilled"]), (1, 1))
        df = rows(("8071", True, "")).assign(Data_Date=CAL[12])
        _, at = sync.attach_recommendations(df, MODE, SV, ci, session_date=CAL[12],
                                            next_session=CAL[13], calendar=CAL)
        self.assertEqual(at["created"], 1)
        export_recommendations(ci, export)
        conn = sqlite3.connect(ci)
        try:
            self.assertEqual(conn.execute("PRAGMA freelist_count").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertEqual(check(ci), 0)

        # the next CI run starts from that JSON: the end survives the rebuild
        doc = json.loads(export.read_text(encoding="utf-8"))
        by = {r["recommendation_id"]: r for r in doc["recommendations"]}
        self.assertEqual(by[a]["status"], "closed")
        self.assertEqual(by[a]["status_reason"], "time")
        self.assertEqual(json.loads(by[a]["outcome"])["bars"], 10)
        self.assertEqual(by[b]["valid_until_session"], CAL[1])
        ci2 = str(Path(self.tmp.name) / "ci2.db")
        seed_from_export(ci2, export)
        conn = L.open_ledger(ci2)
        try:
            got = {r[0]: r[1] for r in conn.execute(
                "SELECT recommendation_id, status FROM recommendations")}
        finally:
            conn.close()
        self.assertEqual(got[a], "closed")
        self.assertEqual(sum(1 for s in got.values() if s == "active"), 2)

    def test_seed_carries_terminal_status_forward_only(self):
        a = self.rec()
        b = self.rec(sid="8070")
        c = self.rec(sid="8071")
        conn = L.open_ledger(self.ledger)
        try:
            L.transition_recommendation(conn, b, "closed", "tp", CAL[3])
            L.open_position(conn, "8071", recommendation_id=c, strategy=MODE)
        finally:
            conn.close()
        doc = {"recommendations": [
            dict(self.row(a), status="cancelled", status_reason="degraded_run",
                 status_session=CAL[0]),
            dict(self.row(b), status="active", status_reason=None,
                 status_session=None),
            dict(self.row(c), status="expired", status_reason="no_fill",
                 status_session=CAL[1])]}
        export = Path(self.tmp.name) / "in.json"
        export.write_text(json.dumps(doc), encoding="utf-8")
        report = {}
        self.assertEqual(seed_from_export(self.ledger, export, report=report), 0)
        self.assertEqual(report, {"inserted": 0, "synced": 1})
        r = self.row(a)
        self.assertEqual((r["status"], r["status_reason"], r["status_session"]),
                         ("cancelled", "degraded_run", CAL[0]))
        self.assertEqual(self.events(a)[-1], ("synced_from_export", "cancelled", CAL[0]))
        self.assertEqual(self.row(b)["status"], "closed")
        self.assertEqual(self.row(c)["status"], "converted")


if __name__ == "__main__":
    unittest.main()
