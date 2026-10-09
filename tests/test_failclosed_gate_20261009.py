"""
Fail-closed pins, the buy gate's inputs and the published JSON
(2026-10-09 audit D11-07, D9-01). A value nobody measured must not pass a
gate, and a file the phone cannot parse must not be written. ASCII only.

    python -m unittest tests.test_failclosed_gate_20261009 -v
"""
import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scanner import json_safe, result_checks
from scanner.scan_mode import _safe_bool, add_trade_columns, mark_buy_ready
from tests.test_scan_pipeline import GateCase, MODE, TODAY, frame

INF = float("inf")


class SafeBoolIsTypeStrict(unittest.TestCase):
    def test_text_and_non_finite_numbers_do_not_pass(self):
        df = pd.DataFrame({"x": ["False", "True", "0", "1", "", None, float("nan"),
                                 0, 1, INF, -INF, "yes", "no", " TRUE "]})
        self.assertEqual(_safe_bool(df, "x").tolist(),
                         [False, True, False, True, False, False, False,
                          False, True, False, False, True, False, True])

    def test_real_booleans_and_missing_columns_are_unchanged(self):
        df = pd.DataFrame({"b": [True, False, True]})
        self.assertEqual(_safe_bool(df, "b").tolist(), [True, False, True])
        self.assertEqual(_safe_bool(df, "none").tolist(), [False] * 3)

    def test_an_empty_frame_is_fine(self):
        self.assertEqual(len(_safe_bool(pd.DataFrame({"x": []}), "x")), 0)


class CorePlusNeedsMeasuredFeatures(unittest.TestCase):
    def _core(self, **feat):
        row = {"Stock_ID": "8069", "Close_Price": 100.0, "Dist_52W_High_Pct": 3.0,
               "Ret_5D_Pct": 2.0, "ATR_Pct": 5.0}
        row.update(feat)
        return bool(add_trade_columns(pd.DataFrame([row]), MODE)["Core_Plus"].iloc[0])

    def test_control_passes(self):
        self.assertTrue(self._core())

    def test_infinite_features_fail(self):
        self.assertFalse(self._core(ATR_Pct=INF))
        self.assertFalse(self._core(Dist_52W_High_Pct=-INF))
        self.assertFalse(self._core(Ret_5D_Pct=-INF))

    def test_missing_features_still_fail(self):
        self.assertFalse(self._core(ATR_Pct=float("nan")))


class GateRefusesDataItCannotVouchFor(GateCase):
    def test_non_finite_or_missing_close_blocks_integrity(self):
        for bad in (float("nan"), INF, 0.0, -5.0):
            ready, block = self.block_of(frame(Close_Price=bad))
            self.assertFalse(ready, bad)
            self.assertEqual(block, "integrity", bad)

    def test_a_session_bar_that_traded_nothing_blocks_integrity(self):
        # D11-13: zero (or unknown) volume is a placeholder or a halt
        for bad in (0, 0.0, float("nan"), None, -3):
            ready, block = self.block_of(frame(Vol_Today=bad))
            self.assertFalse(ready, bad)
            self.assertEqual(block, "integrity", bad)
        self.assertTrue(self.block_of(frame(Vol_Today=1))[0])

    def test_a_duplicated_stock_id_blocks_both_rows(self):
        df = pd.concat([frame(), frame()], ignore_index=True)
        out = mark_buy_ready(df, MODE, session_date=TODAY)
        self.assertEqual(out["Buy_Ready"].tolist(), [False, False])
        self.assertEqual(out["Buy_Block"].tolist(), ["integrity", "integrity"])

    def test_text_flags_do_not_open_the_gate(self):
        self.assertFalse(self.block_of(frame(Integrity_OK="False"))[0])
        self.assertFalse(self.block_of(frame(Core_Plus="False"))[0])

    def test_the_clean_row_still_buys(self):
        self.assertTrue(self.block_of(frame())[0])


class PublishedJsonIsStrict(unittest.TestCase):
    def test_clean_nulls_every_non_finite_number(self):
        doc = {"a": float("nan"), "b": [1.0, INF, {"c": -INF}], "d": "NaN",
               "e": (2.0, float("nan"))}
        out = json_safe.clean(doc)
        self.assertEqual(out, {"a": None, "b": [1.0, None, {"c": None}],
                               "d": "NaN", "e": [2.0, None]})

    def test_numpy_scalars_are_handled(self):
        import numpy as np
        self.assertIsNone(json_safe.clean(np.float64("nan")))
        self.assertEqual(json_safe.clean(np.float64(1.5)), 1.5)

    def test_dumps_is_valid_json(self):
        text = json_safe.dumps_strict({"x": float("nan"), "y": [INF]})
        self.assertEqual(json.loads(text), {"x": None, "y": [None]})
        for tok in ("NaN", "Infinity"):
            self.assertNotIn(tok, text)

    def test_token_scanner_ignores_strings(self):
        self.assertFalse(json_safe.has_non_finite_token('{"a":"NaN","b":"-Infinity"}'))
        self.assertFalse(json_safe.has_non_finite_token('{"a":"say \\"NaN\\""}'))
        self.assertTrue(json_safe.has_non_finite_token('{"a":NaN}'))
        self.assertTrue(json_safe.has_non_finite_token('[1,Infinity]'))
        self.assertTrue(json_safe.has_non_finite_token('[1,-Infinity]'))

    def test_the_exports_write_strict_json(self):
        import scanner.result_export as rx
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            out = t / "scan_result.json"
            from unittest import mock
            with mock.patch.object(rx, "MOBILE_DIR", t), \
                    mock.patch.object(rx, "MOBILE_DATA_FILE", out), \
                    mock.patch.object(rx, "_publish_quotes", lambda df, names=None: {}), \
                    mock.patch.object(rx, "_regime", lambda *a, **k: {"ok": True}), \
                    mock.patch.object(rx, "_calendar_tail", lambda: []):
                rx.export_scan_result_json(
                    frame(), MODE, scan_time=TODAY + " 15:05:00",
                    session_date=TODAY,
                    live_record={"tradable": {"mean_pct": float("nan"),
                                              "win_pct": INF}})
            text = out.read_text(encoding="utf-8")
        self.assertFalse(json_safe.has_non_finite_token(text))
        doc = json.loads(text)
        self.assertIsNone(doc["meta"]["live_record"]["tradable"]["mean_pct"])

    def test_the_checker_flags_a_bare_token_it_is_handed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "scan_result.json"
            p.write_text('{"meta":{"mode":"m","x":NaN},"rows":[{"Stock_ID":"1"}]}',
                         encoding="utf-8")
            rep = result_checks.check_files(p, write=False)
        errs = {i["code"] for i in rep["items"] if i["level"] == "error"}
        self.assertIn("non_finite_json", errs)


class MarketLegIsPublished(unittest.TestCase):
    """M-04: the phone applies the market half of the ride rule from
    meta.market_leg; absent means "not published", never "not disturbed"."""

    def _export(self, leg_table):
        import scanner.result_export as rx
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            out = t / "scan_result.json"
            with mock.patch.object(rx, "MOBILE_DIR", t),                     mock.patch.object(rx, "MOBILE_DATA_FILE", out),                     mock.patch.object(rx, "_publish_quotes", lambda df, names=None: {}),                     mock.patch.object(rx, "_regime", lambda *a, **k: {"ok": True}),                     mock.patch.object(rx, "_calendar_tail", lambda: []),                     mock.patch("scanner.market_leg.disturbed_by_date",
                               lambda *a, **k: leg_table):
                rx.export_scan_result_json(frame(), MODE,
                                           scan_time=TODAY + " 15:05:00",
                                           session_date=TODAY)
            return json.loads(out.read_text(encoding="utf-8"))["meta"]

    def test_the_last_sessions_are_published_as_booleans(self):
        table = {"2026-08-%02d" % d: (d % 2 == 0) for d in range(1, 29)}
        table.update({"2026-09-%02d" % d: True for d in range(1, 20)})
        meta = self._export(table)
        leg = meta["market_leg"]
        self.assertEqual(len(leg), 30)
        self.assertEqual(sorted(leg), sorted(table)[-30:])
        self.assertTrue(all(isinstance(v, bool) for v in leg.values()))

    def test_an_unreadable_index_publishes_nothing(self):
        self.assertNotIn("market_leg", self._export({}))

    def test_checks_report_missing_info_and_malformed_warn(self):
        rows = [{"Stock_ID": "1"}]
        rep = result_checks.check_payload({"meta": {"mode": MODE}, "rows": rows})
        got = {(i["level"], i["code"]) for i in rep["items"]}
        self.assertIn(("info", "market_leg_missing"), got)
        rep = result_checks.check_payload(
            {"meta": {"mode": MODE, "market_leg": {"2026-10-08": True}}, "rows": rows})
        self.assertFalse([i for i in rep["items"] if "market_leg" in i["code"]])
        rep = result_checks.check_payload(
            {"meta": {"mode": MODE, "market_leg": {"x": 1}}, "rows": rows})
        self.assertIn(("warn", "market_leg_malformed"),
                      {(i["level"], i["code"]) for i in rep["items"]})


if __name__ == "__main__":
    unittest.main()
