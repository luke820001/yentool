"""
Parser and feature tests for the institutional-flow layer
(ingestion/inst_history.py, ingestion/inst_trades.py). No network.

    python -m unittest tests.test_inst_history -v
"""
import unittest

import pandas as pd

from ingestion.inst_history import parse_tpex_daily, parse_twse_t86
from ingestion.inst_trades import _streak, get_inst_features


def _k(hex_points):
    return "".join(chr(int(h, 16)) for h in hex_points.split())


# T86 field names as published since 2017-12-18 (order matters: the
# foreign-dealer net column sits BEFORE the dealer total).
T86_FIELDS_NEW = [
    _k("8b49 5238 4ee3 865f"), _k("8b49 5238 540d 7a31"),
    _k("5916 9678 8cc7 8cb7 9032 80a1 6578"), _k("5916 9678 8cc7 8ce3 51fa 80a1 6578"),
    _k("5916 9678 8cc7 8cb7 8ce3 8d85 80a1 6578"),
    _k("5916 8cc7 81ea 71df 5546 8cb7 9032 80a1 6578"),
    _k("5916 8cc7 81ea 71df 5546 8ce3 51fa 80a1 6578"),
    _k("5916 8cc7 81ea 71df 5546 8cb7 8ce3 8d85 80a1 6578"),
    _k("6295 4fe1 8cb7 9032 80a1 6578"), _k("6295 4fe1 8ce3 51fa 80a1 6578"),
    _k("6295 4fe1 8cb7 8ce3 8d85 80a1 6578"),
    _k("81ea 71df 5546 8cb7 8ce3 8d85 80a1 6578"),
    _k("81ea 71df 5546 8cb7 9032 80a1 6578"), _k("81ea 71df 5546 8ce3 51fa 80a1 6578"),
    _k("81ea 71df 5546 8cb7 8ce3 8d85 80a1 6578") + "(x)",
    _k("81ea 71df 5546 8cb7 9032 80a1 6578") + "(h)", _k("81ea 71df 5546 8ce3 51fa 80a1 6578") + "(h)",
    _k("81ea 71df 5546 8cb7 8ce3 8d85 80a1 6578") + "(h)",
    _k("4e09 5927 6cd5 4eba 8cb7 8ce3 8d85 80a1 6578"),
]

# Pre-2017-12 names: plain "wai zi" and one dealer column.
T86_FIELDS_OLD = [
    _k("8b49 5238 4ee3 865f"), _k("8b49 5238 540d 7a31"),
    _k("5916 8cc7 8cb7 9032 80a1 6578"), _k("5916 8cc7 8ce3 51fa 80a1 6578"),
    _k("5916 8cc7 8cb7 8ce3 8d85 80a1 6578"),
    _k("6295 4fe1 8cb7 9032 80a1 6578"), _k("6295 4fe1 8ce3 51fa 80a1 6578"),
    _k("6295 4fe1 8cb7 8ce3 8d85 80a1 6578"),
    _k("81ea 71df 5546 8cb7 9032 80a1 6578"), _k("81ea 71df 5546 8ce3 51fa 80a1 6578"),
    _k("81ea 71df 5546 8cb7 8ce3 8d85 80a1 6578"),
    _k("4e09 5927 6cd5 4eba 8cb7 8ce3 8d85 80a1 6578"),
]


class TwseT86(unittest.TestCase):
    def test_new_layout_reads_dealer_total_not_foreign_dealer(self):
        row = ["2330", "TSMC", "10,000,000", "3,960,600", "6,039,400",
               "5,000", "4,000", "1,000",          # foreign dealer net = 1 lot
               "700,000", "221,600", "478,400",
               "1,361,400",                        # dealer TOTAL
               "0", "0", "0", "0", "0", "0",
               "7,879,200"]
        df = parse_twse_t86({"stat": "OK", "fields": T86_FIELDS_NEW, "data": [row]},
                            "2026-09-18")
        r = df.iloc[0]
        self.assertAlmostEqual(r["Foreign_Net"], 6039.4)
        self.assertAlmostEqual(r["Trust_Net"], 478.4)
        self.assertAlmostEqual(r["Dealer_Net"], 1361.4)
        self.assertAlmostEqual(r["Inst_Net"], 7879.2)
        self.assertAlmostEqual(r["Foreign_Net"] + r["Trust_Net"] + r["Dealer_Net"],
                               r["Inst_Net"], places=1)
        self.assertEqual(r["date"], "2026-09-18")

    def test_old_layout_falls_back_to_plain_foreign_name(self):
        row = ["2330", "TSMC", "9,000,000", "2,169,800", "6,830,200",
               "100,000", "318,000", "-218,000",
               "500,000", "1,873,000", "-1,373,000",
               "5,239,200"]
        df = parse_twse_t86({"stat": "OK", "fields": T86_FIELDS_OLD, "data": [row]},
                            "2017-06-01")
        r = df.iloc[0]
        self.assertAlmostEqual(r["Foreign_Net"], 6830.2)
        self.assertAlmostEqual(r["Trust_Net"], -218.0)
        self.assertAlmostEqual(r["Dealer_Net"], -1373.0)
        self.assertAlmostEqual(r["Inst_Net"], 5239.2)

    def test_non_trading_day_is_an_empty_frame(self):
        df = parse_twse_t86({"stat": "no data", "data": []}, "2026-09-20")
        self.assertEqual(len(df), 0)
        self.assertIn("Inst_Net", df.columns)

    def test_warrants_and_etf_like_codes_are_dropped(self):
        row6 = ["030001", "warrant"] + ["0"] * 17
        row5 = ["00878", "etf"] + ["1,000"] * 17
        df = parse_twse_t86({"stat": "OK", "fields": T86_FIELDS_NEW,
                             "data": [row6, row5]}, "2026-09-18")
        self.assertEqual(list(df["stock_id"]), ["00878"])


class TpexDaily(unittest.TestCase):
    def test_positional_layout(self):
        rec = ["6201", "fund",
               "35,000", "4,000", "31,000",          # foreign ex dealer
               "0", "0", "0",                        # foreign dealer
               "35,000", "4,000", "31,000",          # foreign total
               "0", "70,000", "-70,000",             # trust
               "0", "0", "0",                        # dealer self
               "220,595", "42,000", "178,595",       # dealer hedge
               "220,595", "42,000", "178,595",       # dealer total
               "139,595"]
        j = {"stat": "ok", "tables": [{"data": [rec]}]}
        df = parse_tpex_daily(j, "2026-09-18")
        r = df.iloc[0]
        self.assertAlmostEqual(r["Foreign_Net"], 31.0)
        self.assertAlmostEqual(r["Trust_Net"], -70.0)
        self.assertAlmostEqual(r["Dealer_Net"], 178.6)
        self.assertAlmostEqual(r["Inst_Net"], 139.6)

    def test_before_history_starts_is_empty(self):
        self.assertEqual(len(parse_tpex_daily({"stat": "ok", "tables": []}, "2017-06-01")), 0)
        self.assertEqual(len(parse_tpex_daily({"stat": "ok", "tables": [{"data": []}]},
                                              "2017-06-01")), 0)

    def test_short_record_is_skipped_not_crashed(self):
        j = {"stat": "ok", "tables": [{"data": [["1234", "x", "1"]]}]}
        self.assertEqual(len(parse_tpex_daily(j, "2026-09-18")), 0)


class Streak(unittest.TestCase):
    def test_signs(self):
        self.assertEqual(_streak([1, 2, 3]), 3)
        self.assertEqual(_streak([1, -2, -3]), -2)
        self.assertEqual(_streak([1, 2, 0]), 0)
        self.assertEqual(_streak([]), 0)
        self.assertEqual(_streak([-1, 5]), 1)


class Features(unittest.TestCase):
    def frame(self):
        rows = []
        dates = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"]
        # 1815 traded every day; 6488 is absent on 09-17 (no institutional
        # trade), which must count as a zero day, not shorten the window.
        for i, d in enumerate(dates):
            rows.append(dict(stock_id="1815", Foreign_Net=100.0 * (i + 1),
                             Trust_Net=0.0, Dealer_Net=1.0, Inst_Net=100.0 * (i + 1),
                             date=d, board="OTC"))
            if d != "2026-09-17":
                rows.append(dict(stock_id="6488", Foreign_Net=-10.0, Trust_Net=0.0,
                                 Dealer_Net=0.0, Inst_Net=-10.0, date=d, board="OTC"))
            rows.append(dict(stock_id="2330", Foreign_Net=5.0, Trust_Net=0.0,
                             Dealer_Net=0.0, Inst_Net=5.0, date=d, board="TSE"))
        return pd.DataFrame(rows)

    def test_window_sums_and_streaks(self):
        f = get_inst_features(["1815", "6488", "2330"], existing=self.frame())
        a = f["1815"]
        self.assertEqual(a["Inst_Date"], "2026-09-21")
        self.assertAlmostEqual(a["Foreign_Net_5D"], 1500.0)
        self.assertEqual(a["Inst_Streak"], 5)
        self.assertEqual(a["Inst_Sessions"], 5)
        b = f["6488"]
        self.assertAlmostEqual(b["Inst_Net_5D"], -40.0)      # four -10 days, one 0
        self.assertEqual(b["Inst_Sessions"], 4)
        self.assertEqual(b["Inst_Streak"], -2)               # 0 on 09-17 broke it
        self.assertEqual(b["Inst_Buy_Days"], 0)

    def test_unknown_stock_is_absent(self):
        f = get_inst_features(["9999"], existing=self.frame())
        self.assertEqual(f, {})


if __name__ == "__main__":
    unittest.main()
