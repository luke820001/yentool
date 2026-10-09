"""Pins for the numbers a pick is BUILT from: the Launch_Score formula and the
raw features the CORE+ gate reads, the snapshot prefilter and id rules, the
data-integrity thresholds (what counts as a jump, a split, a non-session day)
and the tick ladder. ASCII only, stdlib unittest, synthetic data only.

Every constant here is the documented rule (docs/STRATEGY.md 3.1 / 3.2 and the
data-integrity comments) written out as a literal. The existing tests derive
their expectations from the module's own constants, so a drifted weight or
window is self-consistent and passes both sides; a literal is not.

Two layers for the score: a pure-python reference written from the document
(no pandas rolling) swept over seeded frames plus hand-pinned golden values,
and boundary cases exactly at a threshold and one step either side.

    python -m unittest tests.test_pin_scoring_data -v
"""
import contextlib
import io
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from analyzer.trend_analysis import calc_52w_position, calc_launch_score, calc_surge_score
from scanner import data_integrity as di
from scanner import market_filter as mf
from scanner.tick import is_on_tick, round_to_tick, tick_size


# ----------------------------------------------------------------- Launch_Score
def _frame(seed, n=300, drift=0.004, vol=0.02, hl=0.03, lots=1500.0):
    rs = np.random.RandomState(seed)       # the legacy stream is frozen across numpy versions
    r = rs.normal(drift, vol, n)
    close = np.round(50.0 * np.exp(np.cumsum(r)), 2)
    high = np.round(close * (1 + hl * rs.uniform(0.2, 1.0, n)), 2)
    low = np.round(close * (1 - hl * rs.uniform(0.2, 1.0, n)), 2)
    volu = np.round(lots * rs.uniform(0.5, 1.5, n), 0)
    dates = pd.bdate_range(end="2026-10-08", periods=n)
    return pd.DataFrame({"date": dates, "close": close, "high": high,
                         "low": low, "Volume_Lot": volu})


def _flat(close_list, lots=1000.0, hl=0.0):
    n = len(close_list)
    c = np.array(close_list, dtype=float)
    return pd.DataFrame({"date": pd.bdate_range(end="2026-10-08", periods=n),
                         "close": c, "high": c * (1 + hl), "low": c * (1 - hl),
                         "Volume_Lot": np.full(n, float(lots))})


def _reference(df):
    """docs/STRATEGY.md 3.2, loops only. Every weight and cap a literal."""
    c = [float(x) for x in df["close"]]
    h = [float(x) for x in df["high"]]
    l = [float(x) for x in df["low"]]
    v = [float(x) for x in df["Volume_Lot"]]
    n = len(c)
    if n < 64:
        return None
    cur = c[-1]
    ma60 = sum(c[-60:]) / 60.0
    vol20 = sum(v[-20:]) / 20.0
    ret60 = cur / c[-64] - 1                      # 63 sessions back
    ret5 = cur / c[-6] - 1                        # 5 sessions back
    if n >= 240:
        h52 = max(c[-252:])
        dist = (h52 - cur) / h52
    else:
        dist = 1.0
    up = dn = 0.0
    for i in range(n - 10, n):
        if c[i] > c[i - 1]:
            up += v[i]
        elif c[i] < c[i - 1]:
            dn += v[i]
    bias = up / (up + dn) if (up + dn) > 0 else 0.5
    rt = (max(h[-20:]) - min(l[-20:])) / min(l[-20:])
    gate = 1.0 if (cur > ma60 and vol20 * cur * 1000.0 >= 1e8) else 0.0

    def clip(x):
        return max(0.0, min(1.0, x))
    mom = clip(max(ret60, 0) / 0.5)
    young = 1 - clip(max(ret5, 0) / 0.12)
    near = 1 - clip(max(dist, 0) / 0.30)
    acc = clip(max(bias - 0.5, 0) / 0.45)
    tight = 1 - clip(max(rt, 0) / 0.25)
    score = (mom * 0.30 + young * 0.25 + near * 0.20 + acc * 0.15 + tight * 0.10) * gate * 100
    return round(score, 1), round(ret5 * 100, 1)


def _components(n=300, vol=3000.0):
    """A 300-bar stock whose five components all sit strictly inside their
    caps (mom .65, young .50, near .4933, acc .5556, tight .6768) so a wrong
    weight or cap moves the score. Positions are counted from the END."""
    close = [90.0] * n
    close[n - 252] = 125.0              # the 52-week high, the oldest bar inside 252
    close[n - 64] = 80.0                # the 3-month base
    for i in range(n - 63, n - 10):
        close[i] = 100.0
    close[n - 10] = 101.0               # a heavy UP bar (volume doubled below)
    close[n - 9] = 100.0                # a down bar
    for i in (n - 8, n - 7, n - 6):
        close[i] = 100.0
    close[n - 5], close[n - 4], close[n - 3], close[n - 2], close[n - 1] = 101.0, 100.5, 102.0, 104.0, 106.0
    high = [c + 1.0 for c in close]
    low = [c - 1.0 for c in close]
    high[n - 21] = 110.0                # a spike 21 bars back: inside a 21-bar window only
    volume = [vol] * n
    volume[n - 10] = 2.0 * vol
    return pd.DataFrame({"close": close, "high": high, "low": low, "Volume_Lot": volume})


class LaunchScoreValues(unittest.TestCase):
    def test_the_hand_computed_fixture_scores_57(self):
        # mom .65 x .30 + young .50 x .25 + near .4933 x .20 + acc .5556 x .15
        # + tight .6768 x .10 = 0.5697
        df = _components()
        out = calc_launch_score(df)
        self.assertEqual(out["Launch_Score"], 57.0)
        self.assertEqual(_reference(df)[0], 57.0)
        self.assertEqual(out["Ret_5D_Pct"], 6.0)

    def test_golden_values(self):
        # seed -> (Launch_Score, Ret_5D_Pct), taken from the reference
        gold = {3: (69.4, 1.6), 11: (69.1, -5.6), 17: (0.0, -2.8), 23: (52.1, -1.7)}
        for seed, (ls, r5) in gold.items():
            df = _frame(seed, drift=0.002 + 0.0004 * (seed % 6))
            got = calc_launch_score(df)
            self.assertAlmostEqual(got["Launch_Score"], ls, places=1, msg="seed %d" % seed)
            self.assertAlmostEqual(got["Ret_5D_Pct"], r5, places=1, msg="seed %d" % seed)

    def test_matches_the_documented_formula_on_many_frames(self):
        nonzero = 0
        for seed in range(60):
            df = _frame(seed, drift=0.002 + 0.0004 * (seed % 6),
                        vol=0.012 + 0.004 * (seed % 5), hl=0.015 + 0.01 * (seed % 4))
            want = _reference(df)
            got = calc_launch_score(df)
            self.assertAlmostEqual(got["Launch_Score"], want[0], places=1, msg="seed %d" % seed)
            self.assertAlmostEqual(got["Ret_5D_Pct"], want[1], places=1, msg="seed %d" % seed)
            nonzero += want[0] > 0
        self.assertGreater(nonzero, 20, "the sweep must exercise live scores")

    def test_institutional_columns_never_enter_the_score(self):
        df = _frame(3, drift=0.002 + 0.0004 * 3)
        want = calc_launch_score(df)
        df["Foreign_Net_5D"] = [(-1) ** i * 1e7 for i in range(len(df))]
        df["Inst_Net"] = 5.0e6
        df["Large_Holder_Pct"] = 99.0
        self.assertEqual(calc_launch_score(df), want)


class LaunchScoreBoundaries(unittest.TestCase):
    def test_close_equal_to_ma60_is_gated_out(self):
        self.assertEqual(calc_launch_score(_flat([100.0] * 300))["Launch_Score"], 0.0)
        self.assertGreater(calc_launch_score(_flat([100.0] * 299 + [100.5]))["Launch_Score"], 0.0)

    def test_turnover_floor_is_one_hundred_million(self):
        ramp = list(np.linspace(40.0, 100.0, 300))
        ramp[-1] = 100.0
        at = _flat(ramp, lots=1000.0)               # 1000 lots x 100 x 1000 = 1.0e8 exactly
        below = _flat(ramp, lots=999.9)
        self.assertGreater(calc_launch_score(at)["Launch_Score"], 0.0)
        self.assertEqual(calc_launch_score(below)["Launch_Score"], 0.0)

    def test_history_guard_63_vs_64_bars(self):
        ramp = list(np.linspace(40.0, 100.0, 80))
        self.assertIsNone(calc_launch_score(_flat(ramp[-63:]))["Launch_Score"])
        self.assertIsNotNone(calc_launch_score(_flat(ramp[-64:]))["Launch_Score"])

    def test_52w_credit_needs_240_bars(self):
        base = _flat(list(np.linspace(40.0, 100.0, 300)))
        s239 = calc_launch_score(base.iloc[-239:])["Launch_Score"]
        s240 = calc_launch_score(base.iloc[-240:])["Launch_Score"]
        # same last 64 bars -> only the near-high term differs: 0.20 x 100
        self.assertAlmostEqual(s240 - s239, 20.0, places=1)

    def test_launch_score_52w_window_is_252_bars(self):
        n = 300
        inc = list(np.linspace(100.0, 120.0, n))
        exc = list(inc)
        inc[n - 252] = 200.0        # oldest bar still inside the window
        exc[n - 253] = 200.0        # one bar too old
        s_inc = calc_launch_score(_flat(inc))["Launch_Score"]
        s_exc = calc_launch_score(_flat(exc))["Launch_Score"]
        # dist 0.40 -> near 0.0 when the spike counts; near 1.0 when it does not
        self.assertAlmostEqual(s_exc - s_inc, 20.0, places=1)

    def test_52w_position_window_is_252_bars(self):
        n = 300
        for spike_at, included in ((n - 252, True), (n - 253, False)):
            c = list(np.linspace(100.0, 120.0, n))
            c[spike_at] = 200.0
            got = calc_52w_position(_flat(c))["Dist_52W_High_Pct"]
            self.assertAlmostEqual(got, 40.0 if included else 0.0, places=1)

    def test_near_52w_flag_boundary_is_15_percent(self):
        for last, want_pct, want_flag in ((85.0, 15.0, True), (84.9, 15.1, False)):
            out = calc_52w_position(_flat([100.0] * 240 + [last]))
            self.assertAlmostEqual(out["Dist_52W_High_Pct"], want_pct, places=1)
            self.assertIs(out["Near_52W_High"], want_flag)
        short = calc_52w_position(_flat([100.0] * 238 + [85.0]))     # under 240 bars: no figure
        self.assertIsNone(short["Dist_52W_High_Pct"])

    def test_distance_is_measured_against_the_high_not_the_close(self):
        out = calc_52w_position(_components())
        self.assertEqual(out["Dist_52W_High_Pct"], 15.2)             # (125 - 106) / 125
        self.assertFalse(out["Near_52W_High"])                       # 15.2 > 15.0

    def test_ret_5d_is_close_over_close_five_sessions_back(self):
        c = [100.0] * 100 + [100.0, 110.0, 111.0, 112.0, 113.0, 120.0]
        out = calc_launch_score(_flat(c))                            # closes[-6] = 100, last = 120
        self.assertAlmostEqual(out["Ret_5D_Pct"], 20.0, places=1)

    def test_atr_is_the_20_bar_mean_of_range_over_close(self):
        n = 100
        c = np.full(n, 100.0)
        df = pd.DataFrame({"date": pd.bdate_range(end="2026-10-08", periods=n),
                           "close": c, "high": c * 1.03, "low": c * 0.97,
                           "Volume_Lot": np.full(n, 1000.0)})
        df.loc[n - 21, "high"] = 110.0          # the 21st bar back: outside the window
        df.loc[n - 21, "low"] = 93.0
        self.assertAlmostEqual(calc_surge_score(df)["ATR_Pct"], 6.0, places=2)
        df.loc[n - 20, "high"] = 110.0          # the OLDEST bar of the window: inside it
        df.loc[n - 20, "low"] = 93.0
        self.assertAlmostEqual(calc_surge_score(df)["ATR_Pct"], 6.55, places=2)   # (19 x .06 + .17) / 20


# --------------------------------------------------------- ids and the prefilter
def quiet(fn, *a, **k):
    """Call fn with its progress prints swallowed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def _snap(rows):
    return pd.DataFrame(rows, columns=["stock_id", "close", "volume"])


class StockIdRules(unittest.TestCase):
    def test_only_four_and_five_digit_common_stocks_are_valid(self):
        ok = ("2330", "6488", "12345", "3105", "9999", "1000")
        bad = ("0050", "00878", "006208", "00631L", "00400A", "123", "123456",
               "abcd", "", "23 30", "2330A", None, 2330, 2330.0)
        for s in ok:
            self.assertTrue(mf._is_valid_stock_id(s), s)
        for s in bad:
            self.assertFalse(mf._is_valid_stock_id(s), repr(s))

    def test_the_double_zero_prefix_alone_excludes_a_four_or_five_digit_etf(self):
        self.assertFalse(mf._is_valid_stock_id("0050"))
        self.assertFalse(mf._is_valid_stock_id("00878"))
        self.assertTrue(mf._is_valid_stock_id("1000"))

    def test_the_government_list_is_the_documented_eleven(self):
        self.assertEqual(sorted(mf._GOVT_STOCKS),
                         ["1314", "2002", "2412", "2801", "2812", "2834", "2836",
                          "2886", "2889", "2892", "5880"])

    def test_the_market_snapshot_drops_etfs_warrants_and_government_stocks(self):
        def raw(code_key, name_key, close_key, vol_key, rows):
            return [{code_key: c, name_key: "x", close_key: "100.00", vol_key: "9,000,000"}
                    for c in rows]
        tse = raw("Code", "Name", "ClosingPrice", "TradeVolume",
                  ["2330", "0050", "00878", "030001", "2412", "2002", "1314"] +
                  ["%04d" % (1100 + i) for i in range(493)])
        otc = raw("SecuritiesCompanyCode", "CompanyName", "Close", "TradingShares",
                  ["6488", "00679B", "123456", "5880"] + ["%04d" % (3000 + i) for i in range(2996)])

        def fake(url):
            return tse if url == mf.TSE_URL else otc
        with mock.patch.object(mf, "_fetch_json", fake):
            out = quiet(mf.fetch_full_market)
        got = set(out["stock_id"])
        self.assertIn("2330", got)
        self.assertIn("6488", got)
        for gone in ("0050", "00878", "030001", "00679B", "123456", "2412", "2002", "1314", "5880"):
            self.assertNotIn(gone, got)


class FeedFloors(unittest.TestCase):
    def health(self, tse_rows, otc_rows):
        def raw(n, k):
            return [{k[0]: "%04d" % (1000 + i), k[1]: "n", k[2]: "10", k[3]: "1000"} for i in range(n)]
        tse = raw(tse_rows, ("Code", "Name", "ClosingPrice", "TradeVolume"))
        otc = raw(otc_rows, ("SecuritiesCompanyCode", "CompanyName", "Close", "TradingShares"))
        with mock.patch.object(mf, "_fetch_json", lambda url: tse if url == mf.TSE_URL else otc):
            quiet(mf.fetch_full_market)
        return mf.get_feed_health()

    def test_the_floors_are_500_tse_and_3000_otc_inclusive(self):
        h = self.health(500, 3000)
        self.assertTrue(h["ok"])
        self.assertEqual((h["tse_rows"], h["otc_rows"], h["missing"]), (500, 3000, []))
        h = self.health(499, 3000)
        self.assertFalse(h["ok"])
        self.assertEqual(h["missing"], ["TSE"])
        h = self.health(500, 2999)
        self.assertFalse(h["ok"])
        self.assertEqual(h["missing"], ["OTC"])
        h = self.health(0, 3000)
        self.assertEqual(h["missing"], ["TSE"])


class PrefilterRule(unittest.TestCase):
    def test_the_prelaunch_config_is_the_documented_one(self):
        self.assertEqual(mf._MODE_CFG["mode_prelaunch"],
                         {"min_vol": 0, "min_turnover": 50000000, "price_max": None,
                          "cap": 300, "rank_by": "turnover"})

    def test_turnover_floor_is_inclusive_and_ranking_is_by_value_not_shares(self):
        rows = [("1001", 100.0, 500_000),        # exactly 5.0e7: kept
                ("1002", 100.0, 499_999),        # 100 TWD short of it: dropped
                ("1003", 1000.0, 60_000),        # 6.0e7 from few shares
                ("1004", 10.0, 5_500_000)]       # 5.5e7 from the most shares
        out = quiet(mf.apply_prefilter, _snap(rows), scan_mode="mode_prelaunch")
        self.assertEqual(list(out["stock_id"]), ["1003", "1004", "1001"])

    def test_the_cap_is_300_names_by_turnover(self):
        rows = [("%04d" % (1000 + i), 100.0, 500_000 + i * 1000) for i in range(305)]
        out = quiet(mf.apply_prefilter, _snap(rows), scan_mode="mode_prelaunch")
        self.assertEqual(len(out), 300)
        self.assertEqual(out["stock_id"].iloc[0], "1304")
        self.assertNotIn("1004", set(out["stock_id"]))      # the lowest five are cut
        self.assertIn("1005", set(out["stock_id"]))

    def test_names_held_from_the_last_run_are_forced_in_below_the_floor(self):
        rows = [("%04d" % (1000 + i), 100.0, 500_000 + i * 1000) for i in range(305)]
        rows.append(("2000", 100.0, 100_000))
        out = quiet(mf.apply_prefilter, _snap(rows), scan_mode="mode_prelaunch", include_ids=["2000"])
        self.assertIn("2000", set(out["stock_id"]))
        out = quiet(mf.apply_prefilter, _snap(rows), scan_mode="mode_prelaunch")
        self.assertNotIn("2000", set(out["stock_id"]))


# ----------------------------------------------------------------- data integrity
def _series(prices):
    n = len(prices)
    p = np.array(prices, dtype=float)
    return pd.DataFrame({"date": pd.bdate_range(end="2026-10-08", periods=n),
                         "open": p, "high": p, "low": p, "close": p})


class IntegrityThresholds(unittest.TestCase):
    def test_a_move_over_ten_and_a_half_percent_is_a_suspect_bar(self):
        self.assertEqual(di.audit_series(_series([100.0] * 70 + [110.4]))["jumps"], 0)    # +10.4%
        self.assertEqual(di.audit_series(_series([100.0] * 70 + [110.6]))["jumps"], 1)    # +10.6%
        self.assertEqual(di.audit_series(_series([100.0] * 70 + [89.6]))["jumps"], 0)     # -10.4%
        self.assertEqual(di.audit_series(_series([100.0] * 70 + [89.4]))["jumps"], 1)     # -10.6%

    def test_a_suspect_bar_alone_is_not_a_data_error(self):
        a = di.audit_series(_series([100.0] * 70 + [110.6]))
        self.assertTrue(a["trustworthy"])
        self.assertIn("jump:1", a["flags"])

    def test_a_threefold_step_is_a_split_and_untrustworthy(self):
        up = di.audit_series(_series([100.0] * 70 + [300.0]))
        self.assertEqual(up["splits"], 1)
        self.assertFalse(up["trustworthy"])
        self.assertEqual(di.audit_series(_series([100.0] * 70 + [299.0]))["splits"], 0)
        down = di.audit_series(_series([100.0] * 70 + [33.3]))
        self.assertEqual(down["splits"], 1)
        self.assertEqual(di.audit_series(_series([100.0] * 70 + [33.5]))["splits"], 0)

    def test_split_start_marks_the_first_bar_after_the_last_split(self):
        f = _series([100.0] * 10 + [300.0] * 5)
        self.assertEqual(di.split_start(f), 10)
        self.assertEqual(di.split_start(_series([100.0] * 10 + [299.0] * 5)), 0)

    def test_history_minimums_are_60_bars_for_the_ma_and_240_for_the_year(self):
        self.assertTrue(di.audit_series(_series([100.0] * 59))["short_ma60"])
        self.assertFalse(di.audit_series(_series([100.0] * 60))["short_ma60"])
        self.assertTrue(di.audit_series(_series([100.0] * 239))["short_52w"])
        self.assertFalse(di.audit_series(_series([100.0] * 240))["short_52w"])

    def test_a_jump_taints_the_current_averages_for_60_bars(self):
        n = 100
        for k, want in ((n - 60, True), (n - 61, False)):
            prices = [100.0] * k + [110.6] * (n - k)         # one jump, at index k
            a = di.audit_series(_series(prices))
            self.assertEqual(a["jumps"], 1)
            self.assertIs(a["recent_jump"], want, k)

    def test_the_hard_errors_make_a_series_untrustworthy(self):
        bad = _series([100.0] * 70)
        bad.loc[30, "close"] = float("nan")
        self.assertFalse(di.audit_series(bad)["trustworthy"])
        bad = _series([100.0] * 70)
        bad.loc[30, "high"] = 90.0                           # high under the close
        self.assertFalse(di.audit_series(bad)["trustworthy"])
        self.assertTrue(di.audit_series(_series([100.0] * 70))["trustworthy"])


class NonSessionDates(unittest.TestCase):
    """purge_nonsession_bars is the only code that DELETEs price rows, so the
    edges of what it may call a non-session day are pinned exactly."""

    def counts(self, values, last="2026-10-08"):
        dates = pd.bdate_range(end=last, periods=len(values)).strftime("%Y-%m-%d")
        return list(zip(dates, values)), list(dates)

    def test_a_date_with_100_names_is_always_a_session_99_can_be_a_placeholder(self):
        # neighbours of 1000: 20% of the lesser neighbour is 200, the floor 100 decides
        c, d = self.counts([1000] * 10 + [99] + [1000] * 4)
        self.assertEqual(di.nonsession_dates(c), [d[10]])
        c, d = self.counts([1000] * 10 + [100] + [1000] * 4)
        self.assertEqual(di.nonsession_dates(c), [])

    def test_below_the_floor_the_bar_is_20_percent_of_the_lesser_neighbour(self):
        # neighbours of 400: the bar is 80
        c, d = self.counts([400] * 10 + [79] + [400] * 4)
        self.assertEqual(di.nonsession_dates(c), [d[10]])
        c, d = self.counts([400] * 10 + [80] + [400] * 4)
        self.assertEqual(di.nonsession_dates(c), [])

    def test_the_22_name_placeholder_among_1930_is_flagged(self):
        c, d = self.counts([1930] * 12 + [22])
        self.assertEqual(di.nonsession_dates(c), [d[12]])

    def test_the_coverage_step_from_a_shortlist_to_the_whole_market_is_not_a_holiday(self):
        # ~320 names a day, then ~2300: the shortlist days are real sessions
        c, d = self.counts([320] * 14 + [2300] * 14)
        self.assertEqual(di.nonsession_dates(c), [])
        # a 90-name day on the old regime IS thin against its 320 neighbours? no:
        # 90 < 100 and 90 >= 20% of 320 (64) so it stays
        c, d = self.counts([320] * 8 + [90] + [320] * 8)
        self.assertEqual(di.nonsession_dates(c), [])
        c, d = self.counts([320] * 8 + [63] + [320] * 8)
        self.assertEqual(di.nonsession_dates(c), [d[8]])

    def test_only_the_recent_40_dates_are_judged(self):
        vals = [1000] * 50
        vals[9] = 20            # 41 dates from the end: outside a 40-date window
        c, d = self.counts(vals)
        self.assertEqual(di.nonsession_dates(c), [])
        vals = [1000] * 50
        vals[15] = 20           # 35 from the end: inside it
        c, d = self.counts(vals)
        self.assertEqual(di.nonsession_dates(c), [d[15]])

    def test_a_series_shorter_than_5_dates_is_never_judged(self):
        c, d = self.counts([1000, 1000, 5, 1000])
        self.assertEqual(di.nonsession_dates(c), [])

    def test_a_thin_last_date_is_judged_against_the_side_it_has(self):
        c, d = self.counts([1930] * 8 + [22])
        self.assertEqual(di.nonsession_dates(c), [d[8]])

    def test_a_thin_first_date_is_judged_against_the_side_it_has(self):
        c, d = self.counts([22] + [1930] * 8)
        self.assertEqual(di.nonsession_dates(c), [d[0]])

    def test_the_comparison_is_against_four_neighbours_a_side(self):
        # four 1000s then the thin bar, then a regime of 300: the quieter side
        # is the 300 side, so 59 (< 20% of 300) is flagged and 61 is not
        c, d = self.counts([1000] * 4 + [59] + [300] * 4)
        self.assertEqual(di.nonsession_dates(c), [d[4]])
        c, d = self.counts([1000] * 4 + [61] + [300] * 4)
        self.assertEqual(di.nonsession_dates(c), [])

    def test_a_thin_old_regime_is_not_a_holiday_on_either_side_of_a_coverage_step(self):
        # sixty names a day, then the whole market (or the reverse): every
        # thin day is below the floor, so only the QUIETER-side rule keeps
        # the one next to the step from being judged against the other regime
        c, d = self.counts([60] * 10 + [2300] * 10)
        self.assertEqual(di.nonsession_dates(c), [])
        c, d = self.counts([2300] * 10 + [60] * 10)
        self.assertEqual(di.nonsession_dates(c), [])

    def test_each_side_is_the_median_of_its_four_nearest_dates(self):
        # the four dates before the thin one read 1000, 1000, 150, 150: their
        # median is 575, not 150 (two dates) and not 1000, so 50 (< 115) is flagged
        c, d = self.counts([1000, 1000, 150, 150, 50, 1000, 1000, 1000, 1000])
        self.assertEqual(di.nonsession_dates(c), [d[4]])
        c, d = self.counts([1000, 1000, 1000, 1000, 50, 150, 150, 1000, 1000])
        self.assertEqual(di.nonsession_dates(c), [d[4]])

    def test_the_purge_deletes_exactly_the_flagged_rows_and_nothing_else(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        dates = list(pd.bdate_range(end="2026-10-08", periods=12).strftime("%Y-%m-%d"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pv.db"
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE data (stock_id TEXT, date TEXT, close REAL)")
            for di_, d in enumerate(dates):
                n = 20 if di_ == 6 else 400
                conn.executemany("INSERT INTO data VALUES (?,?,?)",
                                 [("%04d" % s, d, 10.0) for s in range(1000, 1000 + n)])
            conn.commit()
            conn.close()
            out = di.purge_nonsession_bars(path)
            conn = sqlite3.connect(str(path))
            try:
                left = dict(conn.execute("SELECT date, COUNT(*) FROM data GROUP BY date").fetchall())
            finally:
                conn.close()
        self.assertEqual(out["dates"], [dates[6]])
        self.assertEqual(out["rows"], 20)
        self.assertNotIn(dates[6], left)
        self.assertEqual(len(left), 11)
        self.assertEqual(set(left.values()), {400})


# ---------------------------------------------------------------------- tick ladder
class TickLadder(unittest.TestCase):
    def test_the_share_bands_step_at_10_50_100_500_1000(self):
        cases = [(0.01, 0.01), (9.99, 0.01), (10.0, 0.05), (49.95, 0.05), (50.0, 0.1),
                 (99.9, 0.1), (100.0, 0.5), (499.5, 0.5), (500.0, 1.0), (999.0, 1.0),
                 (1000.0, 5.0), (3000.0, 5.0)]
        for price, tick in cases:
            self.assertEqual(tick_size(price), tick, price)

    def test_etf_ladder_steps_at_fifty(self):
        self.assertEqual(tick_size(49.99, "0050"), 0.01)
        self.assertEqual(tick_size(50.0, "0050"), 0.05)
        self.assertEqual(tick_size(500.0, "0050"), 0.05)
        self.assertEqual(round_to_tick(50.03, "down", "0050"), 50.0)
        self.assertEqual(round_to_tick(50.01, "up", "0050"), 50.05)
        self.assertEqual(round_to_tick(49.99, "up", "0050"), 49.99)

    def test_a_price_already_on_the_ladder_never_moves(self):
        bands = ((0.01, 10.0, 0.01), (10.0, 50.0, 0.05), (50.0, 100.0, 0.1),
                 (100.0, 500.0, 0.5), (500.0, 1000.0, 1.0), (1000.0, 2000.0, 5.0))
        moved = []
        for lo, hi, t in bands:
            k = 0
            while True:
                p = round(lo + k * t, 2)
                if p >= hi:
                    break
                for d in ("down", "up", "nearest"):
                    if round_to_tick(p, d) != p:
                        moved.append((p, d, round_to_tick(p, d)))
                k += 1
        self.assertEqual(moved, [])

    def test_float_noise_never_moves_an_on_ladder_level(self):
        # 8.28 / 0.01 = 827.9999999999999 and 229.5 / 0.5 = 458.99999999999994
        self.assertEqual(round_to_tick(8.28, "down"), 8.28)
        self.assertEqual(round_to_tick(229.5, "down"), 229.5)
        self.assertEqual(round_to_tick(0.07, "up"), 0.07)        # 0.07 / 0.01 = 7.000000000000001
        self.assertEqual(round_to_tick(10.35 * 0.80, "down"), 8.28)

    def test_off_ladder_levels_round_in_the_named_direction(self):
        self.assertEqual(round_to_tick(153.2, "down"), 153.0)
        self.assertEqual(round_to_tick(153.2, "up"), 153.5)
        self.assertEqual(round_to_tick(153.2, "nearest"), 153.0)
        self.assertEqual(round_to_tick(153.3, "nearest"), 153.5)
        self.assertEqual(round_to_tick(499.8, "up"), 500.0)       # crosses into the next band

    def test_is_on_tick_is_exact_to_a_millionth_of_a_tick(self):
        self.assertTrue(is_on_tick(10.0))
        self.assertFalse(is_on_tick(10.00001))
        self.assertTrue(is_on_tick(8.28))

    def test_an_unusable_price_has_no_tick(self):
        for bad in (None, "x", float("nan"), 0, -5):
            self.assertIsNone(tick_size(bad))
            self.assertIsNone(round_to_tick(bad, "down"))


if __name__ == "__main__":
    unittest.main()
