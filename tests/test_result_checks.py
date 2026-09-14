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

from scanner.result_checks import (
    check_payload, check_files, COLUMNS, github_annotations, format_report,
)
from scanner.scan_mode import (
    PRELAUNCH_STOP_PCT, PRELAUNCH_TP_PCT, PRELAUNCH_TRAIL_ARM,
    PRELAUNCH_TRAIL_LOCK,
)

DATE = "2026-09-14"
SESSIONS = ["2026-09-10", "2026-09-11", DATE]


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
        "Strict_Stop_Loss": round(close * (1 - PRELAUNCH_STOP_PCT), 2),
        "Risk_Pct": round(PRELAUNCH_STOP_PCT * 100, 1),
        "Target_Price": round(close * (1 + PRELAUNCH_TP_PCT), 2),
        "Trail_Arm_Price": round(close * (1 + PRELAUNCH_TRAIL_ARM), 2),
        "Trail_Lock_Price": round(close * (1 + PRELAUNCH_TRAIL_LOCK), 2),
        "Core_Plus": True,
        "Entry_Date": "", "Exit_Date": "", "Hold_Day": 0, "Hold_Remaining": 10,
        "Hold_Total": 10, "Hold_Cap": 20, "Hold_Status": status,
        "Hold_Note": "next-open entry", "Entry_Open": entry_open,
        "Fill_Stop_Loss": None, "Fill_Trail_Arm_Price": None,
        "Fill_Trail_Lock_Price": None, "Fill_Target_Price": None,
        "Plan_Stop": round(close * (1 - PRELAUNCH_STOP_PCT), 2),
        "Plan_Armed": False, "Exit_Signal": "", "Exit_Signal_Date": "",
        "Exit_Signal_Price": None, "Exit_Note": "reference stop",
        "Buy_Ready": False, "Buy_Block": "regime",
        "Recommendation_ID": None, "Initial_Buy_Price": None,
        "Initial_Stop_Price": None, "Initial_Target_Price": None,
        "Recommended_On": None, "Rec_Status": None, "Rec_Valid_Until": None,
    }
    if status != "pending":
        fill = entry_open or 300.0
        r.update({
            "Entry_Date": "2026-09-11", "Hold_Day": 2, "Hold_Remaining": 8,
            "Entry_Open": fill,
            "Fill_Stop_Loss": round(fill * (1 - PRELAUNCH_STOP_PCT), 2),
            "Fill_Trail_Arm_Price": round(fill * (1 + PRELAUNCH_TRAIL_ARM), 2),
            "Fill_Trail_Lock_Price": round(fill * (1 + PRELAUNCH_TRAIL_LOCK), 2),
            "Fill_Target_Price": round(fill * (1 + PRELAUNCH_TP_PCT), 2),
            "Hold_Note": "held 2/10",
            "Plan_Stop": round(fill * (1 - PRELAUNCH_STOP_PCT), 2),
            "Exit_Note": "sell if it trades below the stop",
        })
    return r


def clean_payload(rows=None):
    rows = rows if rows is not None else [clean_row()]
    return {
        "meta": {
            "mode": "mode_prelaunch", "strategy_version": "prelaunch-2026-09-09",
            "scan_time": DATE + " 15:05:00", "session_date": DATE,
            "data_date": DATE, "count": len(rows), "empty_ok": not rows,
            "regime": {"ok": True, "risk_on": True, "enter_ok": False,
                       "above20": False, "str20": -0.0035, "strong": False,
                       "text": "regime text", "as_of_date": DATE,
                       "is_current": True},
            "calendar_tail": SESSIONS, "degraded": None,
            "quality": {"data_lag": False},
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
        # entered rows: the stop is fill x 0.85, or x 1.02 once armed
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
        row = clean_row()
        row.update({"Recommendation_ID": "rec-6426-1", "Initial_Buy_Price": 300.0,
                    "Initial_Stop_Price": 255.0, "Initial_Target_Price": 360.0,
                    "Recommended_On": "2026-09-11", "Rec_Status": "active"})
        p = clean_payload([row])
        recs = {"count": 1, "recommendations": [
            {"stock_id": "6426", "status": "active", "stock_name": "TestCo"}]}
        rep = check_payload(p, quotes=clean_quotes(p["rows"]), recs=recs)
        self.assertEqual(rep["status"], "ok", format_report(rep))


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
            with open(hist, encoding="utf-8") as f:
                runs = json.load(f)["runs"]
            self.assertEqual(len(runs), 2)
            self.assertEqual(runs[-1]["session_date"], DATE)

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
