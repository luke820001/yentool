"""
Tests for the per-column self-check (scanner/result_checks.py).

A fixture payload that passes every rule, then one rule broken per test so a
regression in the checker (a rule that stops firing, or one that fires on a
clean file) is caught. Stdlib unittest, no network, no database.

    python -m unittest tests.test_result_checks -v
"""
import copy
import json
import os
import tempfile
import unittest

from scanner.tick import round_to_tick
from scanner.result_checks import (
    check_payload, check_files, COLUMNS, PREV_COLUMNS, github_annotations,
    format_report,
)
from scanner.scan_mode import (
    PRELAUNCH_ADD_PCT, PRELAUNCH_SCALE_OUT_PCT, PRELAUNCH_STOP_PCT,
    PRELAUNCH_TP_PCT, PRELAUNCH_TRAIL_ARM, PRELAUNCH_TRAIL_LOCK,
)

DATE = "2026-09-14"
SESSIONS = ["2026-09-10", "2026-09-11", DATE]


def lvl(base, pct, direction):
    """The published level: base * (1 + pct) snapped onto the quote ladder,
    exactly as scan_mode and holding_tracker compute it (2026-09-21)."""
    return round_to_tick(base * (1 + pct), direction)


def clean_row(sid="6426", close=312.0, market="OTC", status="pending",
              entry_open=None):
    r = {
        "Stock_ID": sid, "Stock_Name": "TestCo", "Market": market,
        "Data_Date": DATE, "Close_Price": close,
        "Explosion_Score": 19.9, "Surge_Score": 74.4, "Launch_Score": 72.5,
        "Ret_5D_Pct": 1.4, "ATR_Pct": 8.4, "RS_Score": 48.5,
        "Gain_3M_Pct": 49.6, "Gain_1M_Pct": 30.5, "Dist_52W_High_Pct": 3.1,
        "Sup_Gap_Pct": round((close - 292.15) / 292.15 * 100, 2),
        "Res_Gap_Pct": round((328.0 - close) / close * 100, 2),
        "Cond_A": False, "Cond_C": True, "Cond_B": True, "Squeeze": False,
        "Is_Golden_Signal": False, "Is_Breakout_Signal": False,
        "MA_Bull_Align": True, "Donchian_Break": False, "MACD_Cross": False,
        "MA_Squeeze": False, "Trend_Breakout": False, "MACD_Hist_Turn": False,
        "Near_52W_High": True, "RS_Strong": True,
        "Large_Holder_Pct": 50.5, "Large_Pct_Change": 5.3, "Retail_Pct": 28.9,
        "Retail_Pct_Change": -4.9, "Foreign_Net": 243.2, "Trust_Net": -33.0,
        "Foreign_Net_5D": 722.6, "Inst_Buy_Days": 2,
        "Dealer_Net": 12.0, "Inst_Net": 222.2, "Inst_Net_5D": 640.1,
        "Trust_Net_5D": -80.5, "Inst_Streak": 2, "Inst_Sessions": 5,
        "Inst_Date": DATE, "Inst_Pct": 6.38, "Chip_Basis": "current",
        "Chip_Action": "", "Chip_Note": "institutions net bought +222 lots (+6.4% of avg volume)",
        "MA5": 312.8, "MA10": 292.15, "MA20": 271.4, "MA60": 214.1,
        "Resist_60H": 328.0, "Support_60L": 135.25, "Support_20L": 219.6,
        "Support_Used": 292.15, "VP_Zone1": 322.4, "VP_Zone2": 306.9,
        "VP_Zone3": 217.0, "Gap_Up_Sup": 296.0, "Gap_Dn_Res": 170.2,
        "Round_Level": 300.0, "Range_Tightness": 0.49, "Volume_Dryup": 1.34,
        "Volume_Bias": 0.83, "Vol_MA20": 3483.0, "Vol_MA5": 5569.0,
        "Vol_Today": 4542, "High_20_Prev": 328.0, "High_Today": 327.0,
        "Low_Today": 293.0, "Close_Prev": 302.0, "Min_Price_3": 293.0,
        "Cond_A_5D": False, "Integrity_OK": True, "Integrity_Flags": "",
        "Recent_Jump": False,
        "Suggested_Buy_Price": close,
        "Strict_Stop_Loss": lvl(close, -PRELAUNCH_STOP_PCT, "down"),
        "Risk_Pct": round((close - lvl(close, -PRELAUNCH_STOP_PCT, "down"))
                          / close * 100, 1),
        "Target_Price": lvl(close, PRELAUNCH_TP_PCT, "up"),
        "Trail_Arm_Price": lvl(close, PRELAUNCH_TRAIL_ARM, "up"),
        "Trail_Lock_Price": lvl(close, PRELAUNCH_TRAIL_LOCK, "down"),
        "Add_Price": lvl(close, -PRELAUNCH_ADD_PCT, "down"),
        "Scale_Out_Price": lvl(close, PRELAUNCH_SCALE_OUT_PCT, "up"),
        "Core_Plus": True,
        "Entry_Date": "", "Exit_Date": "", "Hold_Day": 0, "Hold_Remaining": 10,
        "Hold_Total": 10, "Hold_Cap": 20, "Hold_Status": status,
        "Hold_Note": "next-open entry", "First_Day": True, "Entry_Open": entry_open,
        "Fill_Stop_Loss": None, "Fill_Trail_Arm_Price": None,
        "Fill_Trail_Lock_Price": None, "Fill_Target_Price": None,
        "Fill_Scale_Out_Price": None,
        "Plan_Stop": lvl(close, -PRELAUNCH_STOP_PCT, "down"),
        "Plan_Armed": False, "Exit_Signal": "", "Exit_Signal_Date": "",
        "Plan_Add_Price": lvl(close, -PRELAUNCH_ADD_PCT, "down"),
        "Add_Hit_Date": "",
        "Exit_Signal_Price": None, "Exit_Note": "reference stop",
        "Buy_Ready": False, "Buy_Block": "regime",
        "Recommendation_ID": None, "Initial_Buy_Price": None,
        "Initial_Stop_Price": None, "Initial_Target_Price": None,
        "Recommended_On": None, "Rec_Status": None, "Rec_Valid_Until": None,
        "Rec_Status_Reason": None,
        # segments (2026-10-08): anchored on today's signal, first appearance
        "Hold_Anchor": DATE, "Hold_Anchor_Kind": "first",
        "Prev_Signal_Date": None, "Prev_Was_Signal": None,
        "Prev_Entry_Date": None, "Prev_Entry_Open": None,
        "Prev_Exit_Signal": None, "Prev_Exit_Signal_Date": None,
        "Prev_Exit_Signal_Price": None, "Prev_Exit_Ret_Pct": None,
        "Sessions_Since_Prev_Exit": None,
        # trade restrictions (2026-10-08): nothing in force
        "Trade_Restriction": "none", "Restriction_Flags": None,
        "Restriction_Since": None, "Restriction_Until": None,
        "Restriction_Match_Min": None, "Restriction_Prepay": None,
        # company events (ingestion/company_events, 2026-10-08): display only
        "Rev_Month": "2026-08", "Rev_Amount_K": 149751.0, "Rev_YoY_Pct": 42.2,
        "Rev_MoM_Pct": 14.2, "Rev_Cum_YoY_Pct": 73.2,
        "Ex_Date": None, "Ex_Kind": None, "Ex_Cash_Div": None,
        "Conf_Date": None,
    }
    if status != "pending":
        fill = entry_open or 300.0
        r.update({
            "Entry_Date": "2026-09-11", "Hold_Day": 2, "Hold_Remaining": 8,
            "Hold_Anchor": "2026-09-10",
            "Entry_Open": fill,
            "Fill_Stop_Loss": lvl(fill, -PRELAUNCH_STOP_PCT, "down"),
            "Fill_Trail_Arm_Price": lvl(fill, PRELAUNCH_TRAIL_ARM, "up"),
            "Fill_Trail_Lock_Price": lvl(fill, PRELAUNCH_TRAIL_LOCK, "down"),
            "Fill_Target_Price": lvl(fill, PRELAUNCH_TP_PCT, "up"),
            "Fill_Scale_Out_Price": lvl(fill, PRELAUNCH_SCALE_OUT_PCT, "up"),
            "Hold_Note": "held 2/10", "First_Day": False,
            "Plan_Stop": lvl(fill, -PRELAUNCH_STOP_PCT, "down"),
            "Plan_Add_Price": lvl(fill, -PRELAUNCH_ADD_PCT, "down"),
            "Exit_Note": "sell if it trades below the stop",
        })
    return r


def clean_restrictions(blocking=("suspended",)):
    """meta.quality.restrictions as trade_restrictions.summarize writes it:
    both disposition lists read from the openapi, refreshed on the session."""
    board = {"ok": True, "source": "openapi", "rows": 12,
             "last_modified": "Mon, 14 Sep 2026 07:00:00 GMT", "error": None}
    return {"ok": True, "fetched_at": DATE + "T15:01:00", "session": DATE,
            "next_session": "2026-09-15",
            "boards": {"OTC": dict(board), "TSE": dict(board)},
            "attention_ok": {"OTC": True, "TSE": True},
            "altered_ok": {"OTC": True, "TSE": True},
            "blocking": list(blocking), "active": 0, "blocked": 0,
            "unknown": 0, "counts": {}}


def clean_payload(rows=None):
    rows = rows if rows is not None else [clean_row()]
    return {
        "meta": {
            "mode": "mode_prelaunch", "strategy_version": "prelaunch-2026-09-20",
            "scan_time": DATE + " 15:05:00", "session_date": DATE,
            "data_date": DATE, "count": len(rows), "empty_ok": not rows,
            "regime": {"ok": True, "risk_on": True, "enter_ok": False,
                       "above20": False, "str20": -0.0035, "strong": False,
                       "text": "regime text", "as_of_date": DATE,
                       "is_current": True},
            "calendar_tail": SESSIONS, "degraded": None,
            "quality": {"data_lag": False, "restrictions": clean_restrictions()},
            "quotes": {"file": "quotes.json", "as_of": DATE, "count": 1,
                       "sessions": 3, "missing": []},
            "reports": {"ALL": "report", "OTC": "report"},
        },
        "rows": rows,
    }


def clean_quotes(rows):
    return {
        "as_of": DATE, "sessions": SESSIONS,
        "closes": {r["Stock_ID"]: [300.0, 305.0, r["Close_Price"]] for r in rows},
        "names": {r["Stock_ID"]: r["Stock_Name"] for r in rows},
        "missing": [], "scope": "universe", "count": len(rows),
    }


def codes(report, level=None):
    return {i["code"] for i in report["items"] if level is None or i["level"] == level}


class CleanPayload(unittest.TestCase):
    def test_registry_covers_every_fixture_column(self):
        self.assertEqual(set(clean_row()), set(COLUMNS))

    def test_clean_payload_is_ok(self):
        p = clean_payload()
        rep = check_payload(p, quotes=clean_quotes(p["rows"]), recs={"count": 0, "recommendations": []})
        self.assertEqual(rep["status"], "ok", format_report(rep))
        self.assertEqual(rep["errors"], 0)
        self.assertEqual(rep["warnings"], 0)
        self.assertEqual(rep["columns"], len(COLUMNS))

    def test_entered_row_is_ok(self):
        p = clean_payload([clean_row(status="holding", entry_open=300.0)])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_empty_day_flagged_ok(self):
        p = clean_payload([])
        rep = check_payload(p, quotes={"as_of": DATE, "sessions": SESSIONS, "closes": {}})
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_empty_day_without_flag_fails(self):
        p = clean_payload([])
        p["meta"]["empty_ok"] = False
        rep = check_payload(p)
        self.assertIn("empty_not_flagged", codes(rep, "error"))


class ColumnRules(unittest.TestCase):
    def _broken(self, **changes):
        row = clean_row()
        row.update(changes)
        p = clean_payload([row])
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def test_null_in_required_column(self):
        rep = self._broken(Close_Price=None)
        self.assertIn("null", codes(rep, "error"))
        self.assertEqual(rep["status"], "fail")

    def test_wrong_type(self):
        self.assertIn("type", codes(self._broken(Cond_A="yes"), "error"))
        self.assertIn("type", codes(self._broken(Vol_Today=12.5), "error"))
        self.assertIn("type", codes(self._broken(Data_Date="14/09/2026"), "error"))

    def test_bad_choice(self):
        self.assertIn("choice", codes(self._broken(Market="EMG"), "error"))
        self.assertIn("choice", codes(self._broken(Hold_Status="lost"), "error"))
        self.assertIn("choice", codes(self._broken(Buy_Block="tired"), "error"))

    def test_range_only_warns(self):
        rep = self._broken(Launch_Score=140.0)
        self.assertIn("range_high", codes(rep, "warn"))
        self.assertEqual(rep["status"], "warn")

    def test_garbled_name(self):
        self.assertIn("garbled_text", codes(self._broken(Stock_Name="\ufffd\ufffd"), "error"))

    def test_missing_phone_column_is_error(self):
        row = clean_row()
        del row["Buy_Ready"]
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("column_missing", codes(rep, "error"))

    def test_unregistered_column_is_info(self):
        rep = self._broken(New_Thing=1)
        self.assertIn("column_unregistered", codes(rep, "info"))
        self.assertEqual(rep["status"], "ok")

    def test_duplicate_stock(self):
        p = clean_payload([clean_row(), clean_row()])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("duplicate_stock", codes(rep, "error"))


class RowIdentities(unittest.TestCase):
    def _broken(self, base=None, **changes):
        row = base or clean_row()
        row.update(changes)
        p = clean_payload([row])
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def test_close_outside_day_range(self):
        self.assertIn("close_outside_range", codes(self._broken(High_Today=310.0), "error"))

    def test_stop_above_entry(self):
        rep = self._broken(Strict_Stop_Loss=320.0)
        self.assertIn("stop_not_below_entry", codes(rep, "error"))

    def test_stop_pct_drift(self):
        self.assertIn("stop_pct_mismatch", codes(self._broken(Strict_Stop_Loss=280.0), "error"))

    def test_core_plus_mismatch(self):
        # ATR below the CORE+ floor but flagged True
        self.assertIn("core_plus_mismatch", codes(self._broken(ATR_Pct=1.0), "error"))

    def test_hold_arithmetic(self):
        base = clean_row(status="holding", entry_open=300.0)
        self.assertIn("hold_day_arithmetic", codes(self._broken(base, Hold_Remaining=3), "error"))

    def test_pending_with_fill(self):
        self.assertIn("fill_on_pending_row", codes(self._broken(Entry_Open=300.0), "error"))

    def test_fill_levels_follow_entry_open(self):
        base = clean_row(status="holding", entry_open=300.0)
        self.assertIn("fill_level_mismatch", codes(self._broken(base, Fill_Stop_Loss=280.0), "error"))

    def test_entered_row_without_entry_date(self):
        base = clean_row(status="holding", entry_open=300.0)
        self.assertIn("held_row_without_entry_date", codes(self._broken(base, Entry_Date=""), "error"))

    def test_buy_ready_with_block(self):
        self.assertIn("buy_ready_with_block", codes(self._broken(Buy_Ready=True), "error"))

    # --- segments / Prev_* (2026-10-08) ------------------------------------
    def _closed(self, status="exited"):
        base = clean_row(status="holding", entry_open=300.0)
        base.update({"Hold_Status": status, "Hold_Day": 2, "Hold_Remaining": 0,
                     "Exit_Signal": "stop", "Exit_Signal_Date": "2026-09-11",
                     "Exit_Signal_Price": 240.0})
        return base

    def test_buy_ready_on_a_closed_card_is_an_error(self):
        """The 2026-10-07 payload: 8227 Buy_Ready on an 'exited' card."""
        for status in ("exited", "exit_today"):
            with self.subTest(status=status):
                rep = self._broken(self._closed(status), Buy_Ready=True,
                                   Buy_Block="", First_Day=True)
                self.assertIn("buy_ready_on_closed_segment", codes(rep, "error"))
        rep = self._broken(clean_row(status="holding", entry_open=300.0),
                           Hold_Status="overdue", Hold_Day=12, Hold_Remaining=-2,
                           Buy_Ready=True, Buy_Block="", First_Day=True)
        self.assertIn("buy_ready_on_closed_segment", codes(rep, "error"))

    def test_a_pending_first_day_buy_passes(self):
        rep = self._broken(Buy_Ready=True, Buy_Block="")
        self.assertNotIn("buy_ready_on_closed_segment", codes(rep))
        self.assertNotIn("buy_ready_while_open", codes(rep))

    def test_a_resignal_during_an_open_trade_is_info_only(self):
        rep = self._broken(clean_row(status="holding", entry_open=300.0),
                           Buy_Ready=True, Buy_Block="", First_Day=True)
        self.assertIn("buy_ready_while_open", codes(rep, "info"))
        self.assertNotIn("buy_ready_on_closed_segment", codes(rep))

    def test_exit_today_with_its_signal_closes_the_hold(self):
        rep = self._broken(self._closed("exit_today"))
        self.assertNotIn("hold_day_arithmetic", codes(rep))
        rep = self._broken(self._closed("exit_today"), Hold_Remaining=8)
        self.assertIn("exited_row_has_days_left", codes(rep))

    def _with_prev(self, **changes):
        row = clean_row()
        row.update({"Hold_Anchor_Kind": "reentry",
                    "Prev_Signal_Date": "2026-08-28", "Prev_Was_Signal": False,
                    "Prev_Entry_Date": "2026-08-31", "Prev_Entry_Open": 250.0,
                    "Prev_Exit_Signal": "tp", "Prev_Exit_Signal_Date": "2026-09-03",
                    "Prev_Exit_Signal_Price": 300.0, "Prev_Exit_Ret_Pct": 19.4,
                    "Sessions_Since_Prev_Exit": 7})
        row.update(changes)
        return row

    def test_a_complete_previous_trade_passes(self):
        p = clean_payload([self._with_prev()])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertEqual(rep["status"], "ok", format_report(rep))
        self.assertIn("prev_segment", codes(rep, "info"))
        # an old trade still open at the new anchor has no exit columns
        open_prev = self._with_prev(Prev_Exit_Signal="", Prev_Exit_Signal_Date=None,
                                    Prev_Exit_Signal_Price=None,
                                    Prev_Exit_Ret_Pct=None,
                                    Sessions_Since_Prev_Exit=None)
        p = clean_payload([open_prev])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_a_partial_previous_trade_is_an_error(self):
        for changes in ({"Prev_Entry_Date": None},
                        {"Prev_Exit_Ret_Pct": None},
                        {"Prev_Exit_Signal": None},
                        {"Prev_Exit_Signal": "", "Prev_Exit_Signal_Price": 300.0,
                         "Prev_Exit_Signal_Date": None, "Prev_Exit_Ret_Pct": None,
                         "Sessions_Since_Prev_Exit": None}):
            with self.subTest(changes=changes):
                rep = self._broken(self._with_prev(**changes))
                self.assertIn("prev_segment_partial", codes(rep, "error"))
        rep = self._broken(Prev_Exit_Ret_Pct=1.0)
        self.assertIn("prev_segment_partial", codes(rep, "error"))

    def test_previous_trade_dates_must_be_in_order(self):
        for changes in ({"Prev_Entry_Date": "2026-08-27"},
                        {"Prev_Exit_Signal_Date": "2026-09-15"},
                        {"Prev_Signal_Date": DATE, "Prev_Entry_Date": "2026-09-15",
                         "Prev_Exit_Signal_Date": "2026-09-15"},
                        {"Sessions_Since_Prev_Exit": -1}):
            with self.subTest(changes=changes):
                rep = self._broken(self._with_prev(**changes))
                self.assertIn("prev_segment_order", codes(rep, "error"))

    def test_previous_trade_agrees_with_the_anchor_kind(self):
        """Only a re-entry or recommendation anchor carries a previous trade,
        and a re-entry always does (verifier, 2026-10-08)."""
        for kind in ("reentry", "rec"):
            with self.subTest(kind=kind):
                p = clean_payload([self._with_prev(Hold_Anchor_Kind=kind)])
                rep = check_payload(p, quotes=clean_quotes(p["rows"]))
                self.assertNotIn("prev_segment_kind", codes(rep))
        for kind in ("first", "gap", None):
            with self.subTest(kind=kind):
                rep = self._broken(self._with_prev(Hold_Anchor_Kind=kind))
                self.assertIn("prev_segment_kind", codes(rep, "error"))
        blank = {c: None for c in PREV_COLUMNS}
        rep = self._broken(clean_row(), Hold_Anchor_Kind="reentry", **blank)
        self.assertIn("prev_segment_kind", codes(rep, "error"))
        # a recommendation anchor may stand alone; so may a row from a
        # payload written before the anchor columns existed
        p = clean_payload([dict(clean_row(), Hold_Anchor_Kind="rec", **blank)])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertNotIn("prev_segment_kind", codes(rep))
        old = self._with_prev()
        del old["Hold_Anchor_Kind"]
        p = clean_payload([old])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertNotIn("prev_segment_kind", codes(rep))

    def test_a_pending_row_may_show_its_recommendation_stop(self):
        row = clean_row()
        rec_stop = round_to_tick(row["Plan_Stop"] - 5.0, "down")
        rep = self._broken(row, Plan_Stop=rec_stop, Recommendation_ID="rec-6426-1",
                           Initial_Buy_Price=312.0, Initial_Stop_Price=rec_stop,
                           Initial_Target_Price=374.5, Recommended_On=DATE,
                           Rec_Status="active")
        self.assertNotIn("plan_stop_pending_mismatch", codes(rep))
        rep = self._broken(clean_row(), Plan_Stop=rec_stop)
        self.assertIn("plan_stop_pending_mismatch", codes(rep, "error"))

    def test_blocked_without_reason(self):
        self.assertIn("blocked_without_reason", codes(self._broken(Buy_Block=""), "error"))

    def test_buy_ready_violates_gate(self):
        rep = self._broken(Buy_Ready=True, Buy_Block="", Market="TSE")
        self.assertIn("buy_ready_violates_gate", codes(rep, "error"))

    def test_buy_ready_consistent_passes(self):
        rep = self._broken(Buy_Ready=True, Buy_Block="")
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_stale_row_warns_and_stale_buy_fails(self):
        rep = self._broken(Data_Date="2026-09-11")
        self.assertIn("row_stale", codes(rep, "warn"))
        rep = self._broken(Data_Date="2026-09-11", Buy_Ready=True, Buy_Block="")
        self.assertIn("buy_ready_on_stale_row", codes(rep, "error"))

    def test_recommendation_columns_travel_together(self):
        rep = self._broken(Recommendation_ID="rec-1")
        self.assertIn("recommendation_partial", codes(rep, "error"))
        rep = self._broken(Initial_Buy_Price=100.0)
        self.assertIn("recommendation_orphan_value", codes(rep, "error"))

    def test_integrity_flag_text(self):
        rep = self._broken(Integrity_OK=False, Integrity_Flags="recent_jump")
        self.assertIn("integrity_not_ok", codes(rep, "info"))
        rep = self._broken(Integrity_OK=False)
        self.assertIn("integrity_fail_without_flags", codes(rep, "warn"))

    def test_soft_flags_on_a_trustworthy_row_are_information(self):
        # data_integrity keeps a series trustworthy through a big move or a
        # gap; warning about those fired daily on healthy OTC names.
        rep = self._broken(Integrity_OK=True, Integrity_Flags="jump:2;gap:1")
        self.assertIn("integrity_soft_flags", codes(rep, "info"))
        self.assertNotIn("integrity_flags_on_ok_row", codes(rep, "warn"))

    def test_hard_flag_on_an_ok_row_still_warns(self):
        rep = self._broken(Integrity_OK=True, Integrity_Flags="ohlc:3")
        self.assertIn("integrity_flags_on_ok_row", codes(rep, "warn"))

    def test_gap_percent_identities(self):
        self.assertIn("res_gap_mismatch", codes(self._broken(Res_Gap_Pct=99.0), "error"))
        self.assertIn("sup_gap_mismatch", codes(self._broken(Sup_Gap_Pct=99.0), "error"))
        self.assertIn("squeeze_mismatch", codes(self._broken(Squeeze=True), "error"))

    def test_exit_plan_identities(self):
        # pending rows carry the reference stop and no booked exit
        self.assertIn("plan_stop_pending_mismatch", codes(self._broken(Plan_Stop=200.0), "error"))
        self.assertIn("exit_signal_on_pending_row",
                      codes(self._broken(Exit_Signal="stop", Exit_Signal_Date=DATE,
                                         Exit_Signal_Price=250.0), "error"))
        # entered rows: the stop is fill x (1 - stop pct), or x 1.02 once armed
        base = clean_row(status="holding", entry_open=300.0)
        self.assertIn("plan_stop_level_mismatch", codes(self._broken(base, Plan_Stop=280.0), "error"))
        base = clean_row(status="holding", entry_open=300.0)
        rep = self._broken(base, Plan_Armed=True,
                           Plan_Stop=round(300.0 * (1 + PRELAUNCH_TRAIL_LOCK), 2))
        self.assertEqual(rep["status"], "ok", format_report(rep))
        # a booked exit needs its date and price, and is information
        base = clean_row(status="holding", entry_open=300.0)
        self.assertIn("exit_signal_partial", codes(self._broken(base, Exit_Signal="stop"), "error"))
        base = clean_row(status="holding", entry_open=300.0)
        rep = self._broken(base, Exit_Signal="stop", Exit_Signal_Date="2026-09-12",
                           Exit_Signal_Price=255.0)
        self.assertIn("exit_signal", codes(rep, "info"))
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_gaps_may_be_null_when_no_level(self):
        rep = self._broken(Sup_Gap_Pct=None, Res_Gap_Pct=None, Support_Used=None)
        self.assertEqual(rep["status"], "ok", format_report(rep))


class MetaRules(unittest.TestCase):
    def _meta(self, **changes):
        p = clean_payload()
        p["meta"].update(changes)
        return p, check_payload(p, quotes=clean_quotes(p["rows"]))

    def test_count_mismatch(self):
        self.assertIn("count_mismatch", codes(self._meta(count=5)[1], "error"))

    def test_session_vs_data_date(self):
        self.assertIn("session_vs_data_date", codes(self._meta(session_date="2026-09-11")[1], "error"))

    def test_data_date_vs_rows(self):
        self.assertIn("data_date_vs_rows", codes(self._meta(data_date="2026-09-11")[1], "error"))

    def test_degraded_and_lag_warn(self):
        self.assertIn("feed_degraded", codes(self._meta(degraded="feed degraded: OTC")[1], "warn"))
        self.assertIn("data_lag", codes(self._meta(quality={"data_lag": True})[1], "warn"))

    def test_regime_stale(self):
        p = clean_payload()
        p["meta"]["regime"]["is_current"] = False
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("regime_stale", codes(rep, "warn"))

    def test_calendar_behind(self):
        self.assertIn("calendar_vs_data_date", codes(self._meta(calendar_tail=SESSIONS[:-1])[1], "warn"))

    def test_report_missing(self):
        self.assertIn("report_missing", codes(self._meta(reports={})[1], "warn"))

    def test_clock_ahead_of_data_warns(self):
        p = clean_payload()
        rep = check_payload(p, quotes=clean_quotes(p["rows"]), expected_session="2026-09-15")
        self.assertIn("data_behind_clock", codes(rep, "warn"))


class QuoteRules(unittest.TestCase):
    def test_missing_feed_warns(self):
        rep = check_payload(clean_payload(), quotes=None)
        self.assertIn("quotes_unreadable", codes(rep, "warn"))

    def test_listed_stock_unpriced(self):
        p = clean_payload()
        q = clean_quotes(p["rows"])
        q["closes"]["6426"][-1] = None
        self.assertIn("quotes_gap_row", codes(check_payload(p, quotes=q), "error"))

    def test_listed_stock_absent(self):
        p = clean_payload()
        q = clean_quotes(p["rows"])
        del q["closes"]["6426"]
        self.assertIn("quotes_missing_row", codes(check_payload(p, quotes=q), "error"))

    def test_close_mismatch(self):
        p = clean_payload()
        q = clean_quotes(p["rows"])
        q["closes"]["6426"][-1] = 311.0
        self.assertIn("quotes_close_mismatch", codes(check_payload(p, quotes=q), "error"))

    def test_feed_ends_before_data(self):
        p = clean_payload()
        q = clean_quotes(p["rows"])
        q["sessions"] = SESSIONS[:-1]
        q["closes"] = {"6426": [300.0, 312.0]}
        self.assertIn("quotes_session_vs_data", codes(check_payload(p, quotes=q), "error"))

    def test_dropped_out_holding_unpriced_warns(self):
        p = clean_payload()
        q = clean_quotes(p["rows"])
        q["closes"]["2867"] = [100.0, None, None]
        rep = check_payload(p, quotes=q, tracked_ids=["2867", "6426"])
        self.assertIn("quotes_gap_tracked", codes(rep, "warn"))
        rep = check_payload(p, quotes=q, tracked_ids=["9999"])
        self.assertIn("quotes_missing_tracked", codes(rep, "warn"))

    def test_halted_name_is_information_not_a_gap(self):
        p = clean_payload()
        p["meta"]["quotes"]["source_ended"] = {"2867": "2026-08-19"}
        q = clean_quotes(p["rows"])
        q["closes"]["2867"] = [100.0, None, None]
        rep = check_payload(p, quotes=q, tracked_ids=["2867"])
        self.assertIn("quotes_source_ended", codes(rep, "info"))
        self.assertNotIn("quotes_gap_tracked", codes(rep))
        self.assertEqual(rep["status"], "ok", format_report(rep))


class RecommendationRules(unittest.TestCase):
    def test_active_rec_for_listed_stock_must_attach(self):
        p = clean_payload()
        recs = {"count": 1, "recommendations": [
            {"stock_id": "6426", "status": "active", "stock_name": "TestCo"}]}
        rep = check_payload(p, quotes=clean_quotes(p["rows"]), recs=recs)
        self.assertIn("recommendation_not_attached", codes(rep, "error"))

    def test_attached_rec_passes(self):
        # 2026-10-08: the card of a row with a recommendation is anchored on
        # it (Hold_Anchor_Kind 'rec'), and pending it shows the rec's stop
        for status in ("pending", "holding"):
            row = rec_row(status)
            p = clean_payload([row])
            recs = {"count": 1, "recommendations": [rec_item(row)]}
            rep = check_payload(p, quotes=clean_quotes(p["rows"]), recs=recs)
            self.assertEqual(rep["status"], "ok", status + format_report(rep))


def rec_row(status="pending", **changes):
    """A row carrying an ACTIVE recommendation whose card is that trade."""
    entered = status != "pending"
    r = clean_row(status=status, entry_open=300.0 if entered else None)
    anchor = "2026-09-10" if entered else DATE
    r.update({"Recommendation_ID": "rec-6426-mode_prelaunch-1",
              "Initial_Buy_Price": r["Close_Price"],
              "Initial_Stop_Price": r["Strict_Stop_Loss"],
              "Initial_Target_Price": r["Target_Price"],
              "Recommended_On": anchor, "Rec_Status": "active",
              "Rec_Valid_Until": "2026-09-11" if entered else "2026-09-15",
              "Rec_Status_Reason": None,
              "Hold_Anchor": anchor, "Hold_Anchor_Kind": "rec"})
    r.update(changes)
    return r


def rec_item(row, **changes):
    """The data/recommendations.json entry behind rec_row."""
    item = {"recommendation_id": row["Recommendation_ID"],
            "stock_id": row["Stock_ID"], "stock_name": row["Stock_Name"],
            "strategy": "mode_prelaunch",
            "strategy_version": "prelaunch-2026-09-20",
            "first_qualified_session": row["Recommended_On"],
            "valid_until_session": row["Rec_Valid_Until"],
            "status": row["Rec_Status"], "status_reason": None,
            "status_session": None, "outcome": None}
    item.update(changes)
    return item


def exited_old_segment(row):
    """The 2026-10-07 8227 shape: an active recommendation made today on a
    row whose card still shows the OLD, closed trade."""
    row.update({"Hold_Status": "exited", "Hold_Remaining": 0, "Hold_Day": 6,
                "Hold_Anchor_Kind": "first", "Hold_Anchor": "2026-09-10",
                "Exit_Signal": "tp", "Exit_Signal_Date": "2026-09-11",
                "Exit_Signal_Price": 360.0, "Exit_Date": "2026-09-11",
                "Recommended_On": DATE, "Rec_Valid_Until": "2026-09-15"})
    return row


class RecLifecycleRules(unittest.TestCase):
    """Recommendation lifecycle checks (2026-10-08, portfolio.sync)."""

    def _check(self, rows, recs=None, tracked=None, **meta):
        p = clean_payload(rows)
        p["meta"].update(meta)
        if tracked is not None:
            p["tracked"] = tracked
        return check_payload(p, quotes=clean_quotes(p["rows"]), recs=recs)

    # --- row vs card ------------------------------------------------------
    def test_card_of_an_old_segment_fails(self):
        row = exited_old_segment(rec_row("holding"))
        rep = self._check([row])
        self.assertIn("rec_segment_mismatch", codes(rep, "error"))
        self.assertIn("rec_state_mismatch", codes(rep, "error"))

    def test_same_row_in_tracked_only_warns(self):
        row = exited_old_segment(rec_row("holding"))
        rep = self._check([clean_row(sid="6427")], tracked=[row])
        self.assertIn("tracked_rec_segment_mismatch", codes(rep, "warn"))
        self.assertEqual(rep["errors"], 0, format_report(rep))

    def test_degraded_run_only_warns(self):
        row = exited_old_segment(rec_row("holding"))
        rep = self._check([row], degraded="feed degraded: OTC")
        self.assertIn("rec_segment_mismatch", codes(rep, "warn"))
        self.assertNotIn("rec_segment_mismatch", codes(rep, "error"))

    def test_pending_card_must_show_the_rec_stop(self):
        row = rec_row()
        row["Plan_Stop"] = round_to_tick(row["Plan_Stop"] - 5.0, "down")
        rep = self._check([row])
        self.assertIn("rec_vs_card_consistency", codes(rep, "error"))
        row = rec_row(Initial_Stop_Price=None)
        self.assertNotIn("rec_vs_card_consistency", codes(self._check([row])))

    def test_pending_target_mismatch_warns(self):
        row = rec_row()
        row["Initial_Target_Price"] = round_to_tick(row["Target_Price"] + 5.0, "up")
        rep = self._check([row])
        self.assertIn("rec_target_mismatch", codes(rep, "warn"))

    def test_entered_card_must_start_on_the_entry_session(self):
        row = rec_row("holding", Rec_Valid_Until="2026-09-14")
        self.assertIn("rec_segment_mismatch", codes(self._check([row]), "error"))

    def test_pending_card_on_an_old_recommendation_fails(self):
        row = rec_row(Recommended_On="2026-09-11", Hold_Anchor="2026-09-11")
        self.assertIn("rec_segment_mismatch", codes(self._check([row]), "error"))

    def test_closed_rec_in_grace(self):
        # the rec's own card, with its exit
        row = rec_row("holding", Rec_Status="closed", Rec_Status_Reason="time")
        row.update({"Hold_Status": "exited", "Hold_Remaining": 0,
                    "Exit_Signal": "time", "Exit_Signal_Date": DATE,
                    "Exit_Signal_Price": 301.0, "Exit_Date": DATE})
        self.assertNotIn("rec_state_mismatch", codes(self._check([row])))
        self.assertNotIn("rec_segment_mismatch", codes(self._check([row])))
        # ... without its exit
        row.update({"Exit_Signal": "", "Exit_Signal_Date": "",
                    "Exit_Signal_Price": None})
        self.assertIn("rec_state_mismatch", codes(self._check([row]), "error"))
        # a NEWER natural trade may take the card over
        row = rec_row("holding", Rec_Status="closed", Rec_Status_Reason="tp",
                      Recommended_On="2026-09-01", Rec_Valid_Until="2026-09-02",
                      Hold_Anchor_Kind="reentry")
        self.assertNotIn("rec_segment_mismatch", codes(self._check([row])))
        # an older one may not
        row["Recommended_On"] = "2026-09-12"
        self.assertIn("rec_segment_mismatch", codes(self._check([row]), "error"))

    def test_time_exit_past_the_taiex_feed_is_deferred_not_a_mismatch(self):
        # portfolio.sync._leg_unknown keeps the recommendation active while
        # the card already books a time exit on a session the TAIEX feed has
        # not reached (the market leg may still keep the trade)
        def exited(signal):
            row = rec_row("holding")
            row.update({"Hold_Status": "exited", "Hold_Remaining": 0,
                        "Exit_Signal": signal, "Exit_Signal_Date": "2026-09-11",
                        "Exit_Signal_Price": 301.0, "Exit_Date": "2026-09-11"})
            return row

        def regime(asof):
            reg = clean_payload()["meta"]["regime"]
            reg.update(as_of_date=asof, is_current=asof >= DATE)
            return reg

        lag = regime("2026-09-10")
        self.assertNotIn("rec_state_mismatch",
                         codes(self._check([exited("time")], regime=lag)))
        # the feed reaches the exit: the lifecycle would have closed it
        for asof in ("2026-09-11", DATE):
            self.assertIn("rec_state_mismatch", codes(
                self._check([exited("time")], regime=regime(asof)), "error"))
        # only a time exit depends on the market leg
        self.assertIn("rec_state_mismatch", codes(
            self._check([exited("stop")], regime=lag), "error"))
        # no feed date at all: nothing to excuse
        no_reg = dict(lag, as_of_date=None)
        self.assertIn("rec_state_mismatch", codes(
            self._check([exited("time")], regime=no_reg), "error"))

    def test_payload_without_anchor_columns_is_judged_on_dates(self):
        row = rec_row("holding")
        del row["Hold_Anchor_Kind"]
        rep = self._check([row])
        self.assertNotIn("rec_segment_mismatch", codes(rep))
        row["Rec_Valid_Until"] = "2026-09-14"
        self.assertIn("rec_segment_mismatch", codes(self._check([row]), "error"))

    # --- meta -------------------------------------------------------------
    def test_rec_written_on_degraded_run(self):
        rep = self._check([clean_row()], degraded="feed degraded",
                          rec={"created": 1, "writes": True})
        self.assertIn("rec_written_on_degraded_run", codes(rep, "error"))
        rep = self._check([clean_row()], degraded="feed degraded",
                          rec={"created": 0, "writes": False})
        self.assertNotIn("rec_written_on_degraded_run", codes(rep))
        rep = self._check([clean_row()], rec={"created": 2, "writes": True})
        self.assertNotIn("rec_written_on_degraded_run", codes(rep))

    # --- the recommendations file -----------------------------------------
    def _file(self, *items, **meta):
        return self._check([clean_row()], recs={
            "count": len(items), "recommendations": list(items)}, **meta)

    def _item(self, sid="9999", **changes):
        it = {"recommendation_id": "rec-{}-mode_prelaunch-1".format(sid),
              "stock_id": sid, "stock_name": "Other", "strategy": "mode_prelaunch",
              "strategy_version": "prelaunch-2026-09-20",
              "first_qualified_session": "2026-09-11",
              "valid_until_session": DATE, "status": "active",
              "status_reason": None, "status_session": None, "outcome": None}
        it.update(changes)
        return it

    def test_clean_file_passes(self):
        rep = self._file(
            self._item(),
            self._item("9998", status="closed", status_reason="time",
                       status_session="2026-09-11", outcome={"bars": 10}),
            self._item("9997", status="superseded", status_reason="rule_version",
                       status_session="2026-09-11"),
            self._item("9996", status="cancelled", status_reason="degraded_run",
                       status_session="2026-09-11"))
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_status_unknown(self):
        rep = self._file(self._item(status="converted"))
        self.assertIn("rec_status_unknown", codes(rep, "error"))
        rep = self._file(self._item(status="bogus"))
        self.assertIn("rec_status_unknown", codes(rep, "error"))

    def test_valid_until_missing(self):
        rep = self._file(self._item(valid_until_session=None))
        self.assertIn("rec_valid_until_missing", codes(rep, "error"))
        rep = self._file(self._item(valid_until_session=None), degraded="x")
        self.assertIn("rec_valid_until_missing", codes(rep, "warn"))

    def test_duplicate_active(self):
        rep = self._file(self._item(),
                         self._item(recommendation_id="rec-9999-mode_prelaunch-2"))
        self.assertIn("rec_duplicate_active", codes(rep, "error"))

    def test_active_past_horizon(self):
        import datetime as _dt
        start = _dt.date(2026, 8, 1)
        tail = [(start + _dt.timedelta(days=i)).isoformat() for i in range(30)]
        tail = [d for d in tail if d < DATE][-27:] + [DATE]
        old = tail[0]
        rep = self._file(self._item(valid_until_session=old,
                                    first_qualified_session=old),
                         calendar_tail=tail)
        self.assertIn("rec_active_past_horizon", codes(rep, "error"))
        rep = self._file(self._item(valid_until_session=tail[-20]),
                         calendar_tail=tail)
        self.assertNotIn("rec_active_past_horizon", codes(rep))

    def test_version_in_window(self):
        rep = self._file(self._item(valid_until_session="2026-09-15",
                                    first_qualified_session=DATE,
                                    strategy_version="prelaunch-2026-09-09"))
        self.assertIn("rec_version_in_window", codes(rep, "error"))
        # past its entry session the version no longer matters
        rep = self._file(self._item(strategy_version="prelaunch-2026-09-09"))
        self.assertNotIn("rec_version_in_window", codes(rep))

    def test_terminal_without_reason_and_outcome_warn(self):
        rep = self._file(self._item(status="closed"))
        self.assertIn("rec_terminal_without_reason", codes(rep, "warn"))
        self.assertIn("rec_closed_without_outcome", codes(rep, "warn"))
        self.assertEqual(rep["errors"], 0, format_report(rep))

    def test_active_rec_for_tracked_stock_must_attach(self):
        p = clean_payload([clean_row()])
        p["tracked"] = [clean_row(sid="9999")]
        recs = {"count": 1, "recommendations": [self._item()]}
        rep = check_payload(p, quotes=clean_quotes(p["rows"]), recs=recs)
        self.assertIn("recommendation_not_attached", codes(rep, "warn"))
        self.assertNotIn("recommendation_not_attached", codes(rep, "error"))


class RestrictionRules(unittest.TestCase):
    """Trade restrictions (2026-10-08, scanner/trade_restrictions): the six
    columns agree with each other, the buy gate and the feed summary."""

    DISPO = {"Trade_Restriction": "disposition",
             "Restriction_Flags": "disposition",
             "Restriction_Since": "2026-09-10", "Restriction_Until": "2026-09-16",
             "Restriction_Match_Min": 5, "Restriction_Prepay": "all"}

    def _check(self, blocking=None, tracked=None, rows=None, **changes):
        row = clean_row()
        row.update(changes)
        p = clean_payload(rows if rows is not None else [row])
        if blocking is not None:
            p["meta"]["quality"]["restrictions"]["blocking"] = list(blocking)
        if tracked is not None:
            p["tracked"] = tracked
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def test_disposition_row_is_ok_and_listed(self):
        rep = self._check(**self.DISPO)
        self.assertEqual(rep["errors"], 0, format_report(rep))
        self.assertEqual(rep["warnings"], 0, format_report(rep))
        self.assertIn("restricted_rows", codes(rep, "info"))

    def test_every_restriction_column_is_a_phone_column(self):
        for col in ("Trade_Restriction", "Restriction_Flags", "Restriction_Since",
                    "Restriction_Until", "Restriction_Match_Min",
                    "Restriction_Prepay"):
            row = clean_row()
            del row[col]
            p = clean_payload([row])
            rep = check_payload(p, quotes=clean_quotes(p["rows"]))
            self.assertIn("column_missing", codes(rep, "error"), col)

    def test_kind_is_never_empty_and_from_the_list(self):
        self.assertIn("null", codes(self._check(Trade_Restriction=None), "error"))
        self.assertIn("null", codes(self._check(Trade_Restriction=""), "error"))
        self.assertIn("choice", codes(self._check(Trade_Restriction="frozen"), "error"))
        self.assertIn("choice", codes(self._check(**dict(self.DISPO, Restriction_Prepay="half")), "error"))
        self.assertIn("range_high", codes(self._check(**dict(self.DISPO, Restriction_Match_Min=90)), "warn"))

    def test_disposition_needs_an_until_in_force(self):
        rep = self._check(**dict(self.DISPO, Restriction_Until=None))
        self.assertIn("restriction_until_missing", codes(rep, "error"))
        rep = self._check(**dict(self.DISPO, Restriction_Until="2026-09-11"))
        self.assertIn("restriction_until_missing", codes(rep, "error"))
        # a disposition flag under a more severe kind still needs its period
        rep = self._check(**dict(self.DISPO, Trade_Restriction="suspended",
                                 Restriction_Flags="disposition,suspended",
                                 Restriction_Until=None))
        self.assertIn("restriction_until_missing", codes(rep, "error"))

    def test_kind_must_be_the_most_severe_flag(self):
        rep = self._check(**dict(self.DISPO, Trade_Restriction="attention",
                                 Restriction_Flags="attention,disposition"))
        self.assertIn("restriction_flags_mismatch", codes(rep, "error"))
        rep = self._check(Trade_Restriction="none", Restriction_Flags="attention")
        self.assertIn("restriction_flags_mismatch", codes(rep, "error"))
        # display-only flags (limit_down) never set the kind
        rep = self._check(Trade_Restriction="none", Restriction_Flags="limit_down")
        self.assertNotIn("restriction_flags_mismatch", codes(rep))

    def test_detail_without_disposition_is_orphan(self):
        rep = self._check(Trade_Restriction="attention", Restriction_Flags="attention",
                          Restriction_Match_Min=5)
        self.assertIn("restriction_orphan_detail", codes(rep, "error"))

    def test_blocking_kind_cannot_be_buy_ready(self):
        rep = self._check(Trade_Restriction="suspended", Restriction_Flags="suspended",
                          Buy_Ready=True, Buy_Block="")
        self.assertIn("restricted_buy_ready", codes(rep, "error"))

    def test_disposition_is_not_blocking_by_default(self):
        rep = self._check(**dict(self.DISPO, Buy_Ready=True, Buy_Block=""))
        self.assertNotIn("restricted_buy_ready", codes(rep))

    def test_payload_declared_blocking_set_is_honoured(self):
        rep = self._check(blocking=("suspended", "disposition"),
                          **dict(self.DISPO, Buy_Ready=True, Buy_Block=""))
        self.assertIn("restricted_buy_ready", codes(rep, "error"))
        rep = self._check(blocking=("suspended", "disposition"),
                          **dict(self.DISPO, Buy_Block="restricted"))
        self.assertNotIn("restricted_block_mismatch", codes(rep))

    def test_restricted_block_needs_a_blocking_kind(self):
        rep = self._check(**dict(self.DISPO, Buy_Block="restricted"))
        self.assertIn("restricted_block_mismatch", codes(rep, "error"))
        rep = self._check(Trade_Restriction="suspended", Restriction_Flags="suspended",
                          Buy_Block="restricted")
        self.assertNotIn("restricted_block_mismatch", codes(rep))
        self.assertNotIn("choice", codes(rep))

    def test_limit_lock_recomputed_on_the_ladder(self):
        # 337 x 1.10 = 370.7 -> 370.5 on the 0.5 ladder: a lock
        locked = dict(Close_Prev=337.0, Close_Price=370.5, High_Today=370.5,
                      Low_Today=360.0)
        rep = self._check(**locked)
        self.assertIn("limit_lock_mismatch", codes(rep, "warn"))
        rep = self._check(Trade_Restriction="limit_lock",
                          Restriction_Flags="limit_lock", **locked)
        self.assertNotIn("limit_lock_mismatch", codes(rep))
        # flagged without being at the limit (302 -> 312)
        rep = self._check(Trade_Restriction="limit_lock", Restriction_Flags="limit_lock")
        self.assertIn("limit_lock_mismatch", codes(rep, "warn"))
        # an ex-rights reference makes the recompute unreliable: skipped
        rep = self._check(Recent_Jump=True, **locked)
        self.assertNotIn("limit_lock_mismatch", codes(rep))

    def test_tracked_rows_only_warn(self):
        row = clean_row(sid="6427")
        row.update(dict(self.DISPO, Restriction_Until=None))
        rep = self._check(tracked=[row], rows=[clean_row()])
        self.assertIn("restriction_until_missing",
                      {c.replace("tracked_", "", 1) for c in codes(rep, "warn")})
        self.assertEqual(rep["errors"], 0, format_report(rep))

    # --- meta.quality.restrictions ----------------------------------------
    def _meta(self, restr, rows=None, tracked=None):
        p = clean_payload(rows)
        if restr is None:
            p["meta"]["quality"].pop("restrictions", None)
        else:
            p["meta"]["quality"]["restrictions"] = restr
        if tracked is not None:
            p["tracked"] = tracked
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def test_missing_summary_is_an_error(self):
        self.assertIn("restrictions_unchecked", codes(self._meta(None), "error"))
        self.assertIn("restrictions_unchecked",
                      codes(self._meta(None, rows=[], tracked=[clean_row()]), "error"))
        # an empty day with nothing tracked has nothing to annotate
        self.assertNotIn("restrictions_unchecked", codes(self._meta(None, rows=[])))

    def test_failed_board_warns_not_fails(self):
        r = clean_restrictions()
        r["ok"] = False
        r["boards"]["TSE"] = {"ok": False, "source": None, "rows": 0,
                              "last_modified": None, "error": "fetch"}
        rep = self._meta(r)
        self.assertIn("restrictions_feed_failed", codes(rep, "warn"))
        self.assertEqual(rep["errors"], 0, format_report(rep))

    def test_fallback_and_best_effort_lists_are_info(self):
        r = clean_restrictions()
        r["boards"]["OTC"]["source"] = "web"
        r["attention_ok"]["TSE"] = False
        r["altered_ok"]["OTC"] = False
        rep = self._meta(r)
        self.assertIn("restrictions_fallback", codes(rep, "info"))
        self.assertIn("attention_feed_failed", codes(rep, "info"))
        self.assertIn("altered_feed_failed", codes(rep, "info"))
        self.assertEqual(rep["warnings"], 0, format_report(rep))

    def test_list_older_than_previous_session_warns(self):
        r = clean_restrictions()
        r["boards"]["TSE"]["last_modified"] = "Wed, 09 Sep 2026 22:00:00 GMT"
        self.assertIn("restrictions_stale", codes(self._meta(r), "warn"))
        # A scan of the 14th (previous session the 11th) must read the lists
        # covering the 11th's announcements. TWSE publishes them about 05:30
        # the NEXT morning: 11 Sep 21:30 UTC = 12 Sep 05:30 Taipei is fresh;
        # 10 Sep 21:30 UTC = 11 Sep 05:30 Taipei is the list for the 10th, a
        # day of announcements behind (verifier fix: was accepted).
        r["boards"]["TSE"]["last_modified"] = "Fri, 11 Sep 2026 21:30:00 GMT"
        self.assertNotIn("restrictions_stale", codes(self._meta(r)))
        r["boards"]["TSE"]["last_modified"] = "Thu, 10 Sep 2026 21:30:00 GMT"
        self.assertIn("restrictions_stale", codes(self._meta(r), "warn"))
        # TPEX publishes the same evening: 11 Sep 15:30 UTC = 23:30 Taipei on
        # the 11th is fresh, the 10th's evening list is not
        r = clean_restrictions()
        r["boards"]["OTC"]["last_modified"] = "Fri, 11 Sep 2026 15:30:00 GMT"
        self.assertNotIn("restrictions_stale", codes(self._meta(r)))
        r["boards"]["OTC"]["last_modified"] = "Thu, 10 Sep 2026 15:30:00 GMT"
        self.assertIn("restrictions_stale", codes(self._meta(r), "warn"))

    def test_limit_lock_not_recomputed_on_a_lagging_bar(self):
        # the row's own bar is not the session's: no limit flag either way
        locked = dict(Close_Prev=337.0, Close_Price=370.5, High_Today=370.5,
                      Low_Today=360.0, Data_Date="2026-09-11")
        rep = self._check(**locked)
        self.assertNotIn("limit_lock_mismatch", codes(rep))
        self.assertIn("row_stale", codes(rep))       # the existing rule speaks

    def test_restriction_detail_all_null_is_not_noise(self):
        rep = self._check()
        self.assertFalse([i for i in rep["items"] if i["code"] == "all_null"
                          and i["column"].startswith("Restriction_")])


class FilesAndOutput(unittest.TestCase):
    def test_check_files_writes_meta_and_history_idempotently(self):
        p = clean_payload()
        with tempfile.TemporaryDirectory() as d:
            scan = os.path.join(d, "scan_result.json")
            quotes = os.path.join(d, "quotes.json")
            hist = os.path.join(d, "scan_checks.json")
            with open(scan, "w", encoding="utf-8") as f:
                json.dump(p, f)
            with open(quotes, "w", encoding="utf-8") as f:
                json.dump(clean_quotes(p["rows"]), f)
            r1 = check_files(scan, quotes_path=quotes, history_path=hist)
            r2 = check_files(scan, quotes_path=quotes, history_path=hist)
            self.assertEqual(r1["status"], "ok")
            self.assertEqual(r1["items"], r2["items"])
            with open(scan, encoding="utf-8") as f:
                back = json.load(f)
            self.assertEqual(back["meta"]["checks"]["status"], "ok")
            self.assertEqual(back["meta"]["count"], 1)
            # The scan and the workflow step both check the SAME publish, so
            # the history keeps one line per publish, not one per invocation.
            with open(hist, encoding="utf-8") as f:
                runs = json.load(f)["runs"]
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[-1]["session_date"], DATE)

            # a genuinely new publish (new scan_time) still appends
            p2 = clean_payload()
            p2["meta"]["scan_time"] = DATE + " 18:05:00"
            with open(scan, "w", encoding="utf-8") as f:
                json.dump(p2, f)
            check_files(scan, quotes_path=quotes, history_path=hist)
            with open(hist, encoding="utf-8") as f:
                runs = json.load(f)["runs"]
            self.assertEqual(len(runs), 2)
            self.assertEqual(runs[-1]["scan_time"], DATE + " 18:05:00")

    def test_unreadable_payload_fails(self):
        rep = check_files("/nonexistent/scan_result.json", write=False)
        self.assertEqual(rep["status"], "fail")

    def test_annotations_skip_info(self):
        p = clean_payload()
        p["rows"][0]["Launch_Score"] = 150.0
        p["rows"][0]["Extra"] = 1
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        lines = github_annotations(rep)
        self.assertTrue(any(l.startswith("::warning::[range_high]") for l in lines))
        self.assertFalse(any("column_unregistered" in l for l in lines))

    def test_checker_never_raises(self):
        rep = check_payload({"meta": None, "rows": "not-a-list"})
        self.assertEqual(rep["status"], "fail")


if __name__ == "__main__":
    unittest.main()


class TickLadder(unittest.TestCase):
    """Every level the payload publishes must be a price a broker accepts
    (owner report 2026-09-21). These are the rules that keep it that way."""

    def test_off_tick_stop_is_an_error(self):
        row = clean_row()
        row["Strict_Stop_Loss"] = 153.2        # 0.50 ladder at this price
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("price_off_tick", codes(rep, "error"))

    def test_off_tick_target_is_an_error(self):
        row = clean_row()
        row["Target_Price"] = 229.8
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("price_off_tick", codes(rep, "error"))

    def test_every_published_level_sits_on_the_ladder(self):
        from scanner.result_checks import ORDER_LEVEL_COLUMNS
        from scanner.tick import is_on_tick
        for status, fill in (("pending", None), ("holding", 300.0)):
            row = clean_row(status=status, entry_open=fill)
            for col in ORDER_LEVEL_COLUMNS:
                value = row.get(col)
                if value is None:
                    continue
                self.assertTrue(is_on_tick(value), "%s %s = %s" % (status, col, value))

    def test_scale_out_level_identity(self):
        row = clean_row()
        row["Scale_Out_Price"] = row["Scale_Out_Price"] + 5
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("scale_out_pct_mismatch", codes(rep, "error"))


# --------------------------------------------------------------------------
# company events + live-record per-name history / benchmark (2026-10-08)
# --------------------------------------------------------------------------
def clean_events(**over):
    """meta.events as ingestion.company_events.meta_block writes it."""
    ok = {"ok": True, "rows": 900, "as_of": "2026-08-17",
          "fetched_at": DATE + " 15:01", "stale": False, "error": None,
          "optional": False}
    srcs = {n: dict(ok) for n in ("revenue_tse", "revenue_otc", "exdiv_tse",
                                  "exdiv_otc", "news_tse", "news_otc")}
    for n in ("revenue_mops_tse", "revenue_mops_otc", "conf_mops_tse",
              "conf_mops_otc"):
        srcs[n] = dict(ok, optional=True)
    ev = {"ok": True, "updated_at": DATE + " 15:01", "session_date": DATE,
          "sources": srcs, "revenue_month_latest": "2026-08",
          "next_revenue_deadline": "2026-10-12",
          "next_report_deadline": {"date": "2026-11-14", "what": "Q3",
                                   "approximate": True},
          "coverage": {"rows": 1, "revenue": 1, "exdiv": 0, "conf": 0},
          "note": "display only; never scored"}
    ev.update(over)
    return ev


class CompanyEventChecks(unittest.TestCase):
    def _check(self, row_over=None, events=None, **meta_over):
        row = clean_row()
        row.update(row_over or {})
        p = clean_payload([row])
        if events is not None:
            p["meta"]["events"] = events
        p["meta"].update(meta_over)
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def test_clean_events_block_is_ok(self):
        rep = self._check(events=clean_events())
        self.assertEqual(rep["status"], "ok", format_report(rep))
        self.assertNotIn("events_absent", codes(rep))

    def test_absent_block_is_information_only(self):
        rep = self._check()
        self.assertIn("events_absent", codes(rep, "info"))
        self.assertEqual(rep["status"], "ok")

    def test_required_source_failure_warns_never_fails(self):
        ev = clean_events()
        ev["sources"]["revenue_otc"].update(ok=False, error="HTTP 503")
        ev["sources"]["exdiv_tse"].update(stale=True)
        rep = self._check(events=ev)
        self.assertIn("events_source_failed", codes(rep, "warn"))
        self.assertIn("events_stale", codes(rep, "warn"))
        self.assertEqual(rep["status"], "warn")

    def test_optional_mopsov_failure_is_information(self):
        ev = clean_events()
        ev["sources"]["conf_mops_otc"].update(ok=False, stale=True,
                                              error="ConnectionError")
        rep = self._check(events=ev)
        self.assertIn("events_optional_source_failed", codes(rep, "info"))
        self.assertIn("events_optional_stale", codes(rep, "info"))
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_carried_forward_block_is_noted(self):
        rep = self._check(events=clean_events(carried_forward=True))
        self.assertIn("events_carried_forward", codes(rep, "info"))

    def test_revenue_loaded_but_no_row_annotated_warns(self):
        rep = self._check(row_over={"Rev_Month": None, "Rev_Amount_K": None,
                                    "Rev_YoY_Pct": None, "Rev_MoM_Pct": None,
                                    "Rev_Cum_YoY_Pct": None},
                          events=clean_events())
        self.assertIn("events_not_annotated", codes(rep, "warn"))

    def test_revenue_month_of_the_session_itself_warns(self):
        rep = self._check(row_over={"Rev_Month": DATE[:7]})
        self.assertIn("rev_month_future", codes(rep, "warn"))
        self.assertNotEqual(rep["status"], "fail")

    def test_bad_month_label_and_orphan_figures_warn(self):
        self.assertIn("rev_month_format",
                      codes(self._check(row_over={"Rev_Month": "2026/08"}), "warn"))
        self.assertIn("rev_fields_without_month",
                      codes(self._check(row_over={"Rev_Month": None}), "warn"))

    def test_old_revenue_month_is_information(self):
        rep = self._check(row_over={"Rev_Month": "2026-05"})
        self.assertIn("rev_month_old", codes(rep, "info"))
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_ex_date_must_be_upcoming(self):
        rep = self._check(row_over={"Ex_Date": DATE, "Ex_Kind": "div",
                                    "Ex_Cash_Div": 1.5})
        self.assertIn("ex_date_not_after_session", codes(rep, "warn"))
        rep = self._check(row_over={"Ex_Date": "2026-09-21", "Ex_Kind": "both",
                                    "Ex_Cash_Div": 1.5})
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_ex_kind_outside_the_set_and_orphan_kind(self):
        self.assertIn("choice", codes(self._check(row_over={
            "Ex_Date": "2026-09-21", "Ex_Kind": "cash"}), "error"))
        self.assertIn("ex_fields_without_date", codes(self._check(row_over={
            "Ex_Kind": "div"}), "warn"))

    def test_an_ex_date_without_its_kind_warns(self):
        # verifier 2026-10-08: the reverse half-written event (a bare date)
        rep = self._check(row_over={"Ex_Date": "2026-09-21"})
        self.assertIn("ex_fields_without_date", codes(rep, "warn"))
        self.assertNotIn("ex_fields_without_date", codes(rep, "error"))
        hits = [i for i in rep["items"]
                if i["code"] == "ex_fields_without_date"]
        self.assertEqual(hits[0]["column"], "Ex_Kind")

    def test_all_null_ex_and_conf_columns_are_not_noise(self):
        rep = self._check()
        cols = {i["column"] for i in rep["items"] if i["code"] == "all_null"}
        for c in ("Ex_Date", "Ex_Kind", "Ex_Cash_Div", "Conf_Date"):
            self.assertNotIn(c, cols)

    def test_missing_event_column_only_warns(self):
        row = clean_row()
        row.pop("Conf_Date")
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("column_missing", codes(rep, "warn"))
        self.assertNotIn("column_missing", codes(rep, "error"))


class LiveRecordShapeChecks(unittest.TestCase):
    def _check(self, lr):
        p = clean_payload()
        p["meta"]["live_record"] = lr
        return check_payload(p, quotes=clean_quotes(p["rows"]))

    def _entry(self, sig, bucket="tradable"):
        return {"sig": sig, "bucket": bucket, "rank": 3, "ret": 1.2,
                "exit": "tp", "bars": 4, "entry_date": sig,
                "exit_date": sig, "restriction": None}

    def test_well_formed_blocks_pass(self):
        lr = {"by_sid": {"6426": [self._entry("2026-08-01", "not_core"),
                                  self._entry("2026-09-01")]},
              "bench": {"from": "2026-06-25", "to": DATE,
                        "series": [["2026-06-25", 22000.0, 439.84],
                                   [DATE, 23000.0, None]]}}
        rep = self._check(lr)
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_none_bench_is_fine(self):
        rep = self._check({"by_sid": {}, "bench": None})
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_unsorted_overlong_or_unknown_bucket_warns(self):
        e = self._entry
        for lst in ([e("2026-09-01"), e("2026-08-01")],
                    [e("2026-08-0%d" % i) for i in range(1, 7)],
                    [e("2026-08-01", "mystery")]):
            rep = self._check({"by_sid": {"6426": lst}})
            self.assertIn("live_record_by_sid_shape", codes(rep, "warn"))

    def test_bench_series_outside_its_span_warns(self):
        rep = self._check({"bench": {"from": "2026-07-01", "to": DATE,
                                     "series": [["2026-06-25", 1.0, 1.0]]}})
        self.assertIn("live_record_bench_shape", codes(rep, "warn"))


class SerializationArtefacts(unittest.TestCase):
    """A number written as 24.0 instead of 24 is the same number. JSON has one
    numeric type; which one appears depends on whether any row in the column
    was null (2026-09-21 end-to-end run)."""

    def test_integral_float_counts_as_an_integer(self):
        row = clean_row()
        row["Hold_Day"] = 0.0
        row["Hold_Remaining"] = 10.0
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertEqual(rep["status"], "ok", format_report(rep))

    def test_a_fractional_value_is_still_a_type_error(self):
        row = clean_row()
        row["Hold_Day"] = 2.5
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertIn("type", codes(rep, "error"))

    def test_unanchored_row_may_report_an_unknown_holding_day(self):
        row = clean_row()
        row["Hold_Day"] = None
        row["Hold_Remaining"] = None
        row["Hold_Status"] = ""
        p = clean_payload([row])
        rep = check_payload(p, quotes=clean_quotes(p["rows"]))
        self.assertNotIn("null", codes(rep, "error"))
