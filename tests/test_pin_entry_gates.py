"""Pins for the ENTRY side of the settled rule. ASCII only, stdlib unittest.

Why this file exists. A mutation audit (2026-10-09) changed one settled value,
comparison or rounding direction at a time and re-ran the whole suite: 216 of
377 mutants survived. Almost every survivor sat on an edge the existing tests
never touch -- "rank < 20" versus "rank <= 20", "close > 60MA" versus
">=", "5.0 is still CORE+". These tests put a bar exactly ON each edge and one
step either side.

The numbers below are the SETTLED rule written out as literals on purpose. A
test that reads the constant back from the module it guards is self-consistent
under any drift and can never fail; the literal can. Changing a number here is
a decision about the strategy, not a refactor -- see docs/STRATEGY.md section 3
and docs/BACKTEST_LOG.md before touching one.

Covers: CORE+ gate (inclusive edges, fail-closed missing values), the order
price columns and their rounding direction, Suggested_Buy_Price == close, the
hysteresis band 20/80, the rank cutoff and Buy_Block precedence inside
mark_buy_ready, the OTC-only gate, the market regime, and the "rejected
variants stay absent" guards (chips off, only 'suspended' blocks, no
US-shock / cooldown / limit-up entry filter).

    python -m unittest tests.test_pin_entry_gates -v
"""
import inspect
import re
import unittest
from unittest import mock

import pandas as pd

import scanner.market_regime as market_regime
import scanner.scan_mode as sm
from scanner.scan_mode import (
    add_trade_columns, apply_scan_mode, mark_buy_ready, select_with_hysteresis,
    sort_for_mode,
)
from tests.pin_support import MODE, ROOT, TODAY, raw_index, taiex_frame

NAN = float("nan")


# ------------------------------------------------------------------- CORE+
def core_flags(rows):
    """Core_Plus for [(dist52, ret5, atr), ...] through the production path."""
    df = pd.DataFrame([
        {"Stock_ID": "%04d" % (3000 + i), "Close_Price": 100.0,
         "Dist_52W_High_Pct": d, "Ret_5D_Pct": r, "ATR_Pct": a}
        for i, (d, r, a) in enumerate(rows)])
    return [bool(x) for x in add_trade_columns(df, MODE)["Core_Plus"]]


class CorePlusGate(unittest.TestCase):
    """Within 5.0% of the 52-week high, 5-day return at most 5.0%, ATR at
    least 4.5% -- every edge inclusive, every unknown a refusal."""

    CASES = [
        # (dist52, ret5, atr, expected)
        (5.0, 5.0, 4.5, True),            # all three exactly on their edge
        (0.0, -20.0, 30.0, True),         # deep inside
        (5.1, 5.0, 4.5, False),           # one 0.1 step past each edge ...
        (5.0, 5.1, 4.5, False),
        (5.0, 5.0, 4.4, False),
        (5.0001, 5.0, 4.5, False),        # ... and an epsilon past it
        (5.0, 5.0001, 4.5, False),
        (5.0, 5.0, 4.4999, False),
        (4.9999, 4.9999, 4.5001, True),   # an epsilon inside
        (NAN, 1.0, 6.0, False),           # an unknown feature fails closed
        (1.0, NAN, 6.0, False),
        (1.0, 1.0, NAN, False),
        (None, 1.0, 6.0, False),
        (1.0, None, 6.0, False),
        (1.0, 1.0, None, False),
    ]

    def test_edges_and_missing_values_in_the_scan(self):
        got = core_flags([c[:3] for c in self.CASES])
        for case, g in zip(self.CASES, got):
            self.assertEqual(g, case[3], case)

    def test_each_clause_alone_can_refuse(self):
        # the other two clauses pass comfortably, so a deleted clause shows
        self.assertEqual(core_flags([(9.0, 1.0, 8.0)]), [False])
        self.assertEqual(core_flags([(1.0, 9.0, 8.0)]), [False])
        self.assertEqual(core_flags([(1.0, 1.0, 2.0)]), [False])

    def test_a_missing_column_fails_closed(self):
        for drop in ("Dist_52W_High_Pct", "Ret_5D_Pct", "ATR_Pct"):
            df = pd.DataFrame([{"Stock_ID": "3001", "Close_Price": 100.0,
                                "Dist_52W_High_Pct": 1.0, "Ret_5D_Pct": 1.0,
                                "ATR_Pct": 8.0}]).drop(columns=[drop])
            out = add_trade_columns(df, MODE)
            self.assertFalse(bool(out["Core_Plus"].iloc[0]), drop)

    def test_the_publish_time_checker_applies_the_same_edges(self):
        from scanner.result_checks import _core_plus_expected
        for d, r, a, want in self.CASES:
            row = {"Dist_52W_High_Pct": d, "Ret_5D_Pct": r, "ATR_Pct": a}
            self.assertEqual(_core_plus_expected(row), want, (d, r, a))

    def test_the_prelaunch_pool_needs_a_positive_launch_score(self):
        d = pd.DataFrame([{"Stock_ID": "1111", "Launch_Score": 0.0},
                          {"Stock_ID": "2222", "Launch_Score": 0.1},
                          {"Stock_ID": "3333", "Launch_Score": NAN}])
        self.assertEqual(list(apply_scan_mode(d, MODE)["Stock_ID"]), ["2222"])


# ------------------------------------------------------ order price columns
def levels(sid, close):
    out = add_trade_columns(pd.DataFrame([{"Stock_ID": sid, "Close_Price": close}]), MODE)
    return out.iloc[0]


class OrderPriceColumns(unittest.TestCase):
    """Every level is a price the owner types into a broker, so each one is
    the settled percentage off the close AND snapped onto the quote ladder in
    the direction that never flatters (stop and add down, target, arm and
    scale-out up, lock down). A 191.50 close sits on the 0.50 band while none
    of its six levels does, so any rounding direction or percentage that
    drifts moves a published price."""

    def test_the_six_levels_off_an_off_ladder_close(self):
        r = levels("6488", 191.5)
        self.assertEqual(r["Strict_Stop_Loss"], 153.0)      # -20%  -> 153.20 down
        self.assertEqual(r["Target_Price"], 230.0)          # +20%  -> 229.80 up
        self.assertEqual(r["Trail_Arm_Price"], 196.5)       # +2.5% -> 196.2875 up
        self.assertEqual(r["Trail_Lock_Price"], 195.0)      # +2%   -> 195.33 down
        self.assertEqual(r["Add_Price"], 172.0)             # -10%  -> 172.35 down
        self.assertEqual(r["Scale_Out_Price"], 220.5)       # +15%  -> 220.225 up
        self.assertEqual(r["Risk_Pct"], 20.1)               # (191.5 - 153) / 191.5

    def test_an_etf_uses_the_etf_ladder(self):
        # 0050 at 106.75: the ETF ladder steps 0.05, the share ladder 0.50
        r = levels("0050", 106.75)
        self.assertEqual(r["Strict_Stop_Loss"], 85.4)
        self.assertEqual(r["Trail_Lock_Price"], 108.85)     # 108.885 down; 108.50 on the share ladder
        self.assertEqual(r["Trail_Arm_Price"], 109.45)      # 109.4187 up
        self.assertEqual(r["Target_Price"], 128.1)
        self.assertEqual(r["Add_Price"], 96.05)             # 96.075 down
        self.assertEqual(r["Scale_Out_Price"], 122.8)       # 122.7625 up

    def test_float_noise_never_moves_an_on_ladder_level(self):
        # 10.35 x 0.80 = 8.28 but 8.28 / 0.01 = 827.9999999999999
        r = levels("6488", 10.35)
        self.assertEqual(r["Strict_Stop_Loss"], 8.28)
        self.assertEqual(r["Add_Price"], 9.31)              # 9.315 down on the 0.01 band

    def test_the_reference_price_is_the_signal_close_not_a_limit_below_it(self):
        # market order at the next open: Suggested_Buy_Price == Close_Price
        for close in (10.35, 49.95, 100.0, 191.5, 487.0, 1234.0):
            r = levels("6488", close)
            self.assertEqual(r["Suggested_Buy_Price"], close)

    def test_no_new_order_column_appears_unreviewed(self):
        out = add_trade_columns(pd.DataFrame([{"Stock_ID": "6488", "Close_Price": 191.5}]), MODE)
        self.assertEqual(sorted(set(out.columns) - {"Stock_ID", "Close_Price"}),
                         sorted(["Suggested_Buy_Price", "Strict_Stop_Loss", "Target_Price",
                                 "Trail_Arm_Price", "Trail_Lock_Price", "Add_Price",
                                 "Scale_Out_Price", "Risk_Pct", "Core_Plus"]))

    def test_the_optional_rungs_are_the_settled_ones(self):
        self.assertEqual(sm.PRELAUNCH_ADD_PCT, 0.10)
        self.assertEqual(sm.PRELAUNCH_ADD_FIRST, 0.5)
        self.assertEqual(sm.PRELAUNCH_SCALE_OUT_PCT, 0.15)
        self.assertEqual(sm.STRATEGY_VERSION, "prelaunch-2026-09-21")
        self.assertEqual(sm.BUY_RULE_VERSION, sm.STRATEGY_VERSION)


# ------------------------------------------------------------ hysteresis 20/80
def ranked(n=100, int_ids=False):
    ids = list(range(n))
    return pd.DataFrame({"Stock_ID": ids if int_ids else [str(i) for i in ids],
                         "Launch_Score": [200.0 - i for i in range(n)]})


def picked(df, prior):
    return select_with_hysteresis(df, prior)[1]


class Hysteresis(unittest.TestCase):
    def test_the_band_is_20_and_80(self):
        self.assertEqual((sm.N_ENTER, sm.N_HOLD), (20, 80))
        sig = inspect.signature(select_with_hysteresis).parameters
        self.assertEqual((sig["n_enter"].default, sig["n_hold"].default), (20, 80))

    def test_without_a_prior_exactly_ranks_0_to_19_enter(self):
        for prior in (None, [], set()):
            self.assertEqual(picked(ranked(), prior), [str(i) for i in range(20)])

    def test_a_held_name_survives_to_rank_79_and_drops_at_rank_80(self):
        got = picked(ranked(), ["20", "79", "80", "81"])
        self.assertIn("20", got)        # first rank past the entry cutoff
        self.assertIn("79", got)        # last rank inside the hold band
        self.assertNotIn("80", got)     # first rank outside it
        self.assertNotIn("81", got)
        self.assertEqual(got, [str(i) for i in range(20)] + ["20", "79"])

    def test_a_name_past_rank_19_enters_only_if_it_was_held(self):
        got = picked(ranked(), ["30"])
        self.assertEqual(got, [str(i) for i in range(20)] + ["30"])

    def test_everyone_held_gives_exactly_the_top_80_in_rank_order(self):
        got = picked(ranked(), [str(i) for i in range(100)])
        self.assertEqual(got, [str(i) for i in range(80)])

    def test_prior_ids_and_frame_ids_compare_as_text(self):
        want = [str(i) for i in range(20)] + ["20", "79"]
        self.assertEqual(picked(ranked(), [20, 79, 80]), want)
        self.assertEqual(picked(ranked(int_ids=True), ["20", "79", "80"]), want)

    def test_short_and_empty_frames(self):
        self.assertEqual(picked(ranked(5), ["3", "99"]), ["0", "1", "2", "3", "4"])
        self.assertEqual(select_with_hysteresis(pd.DataFrame(), ["1"])[1], [])

    def test_ties_break_on_stock_id_so_rank_20_is_reproducible(self):
        df = pd.DataFrame({"Stock_ID": ["9999", "1000", "5555"],
                           "Launch_Score": [71.3, 71.3, 71.3]})
        out = sort_for_mode(df, MODE)
        self.assertEqual(list(out["Stock_ID"]), ["1000", "5555", "9999"])


# -------------------------------------------------------------------- buy gate
class GateCase(unittest.TestCase):
    """mark_buy_ready with the market regime stubbed to a known verdict."""

    reg = {"ok": True, "enter_ok": True, "risk_on": True, "is_current": True}

    def setUp(self):
        self._p = mock.patch.object(market_regime, "get_market_regime",
                                    lambda *a, **k: dict(self.reg))
        self._p.start()
        self.addCleanup(self._p.stop)

    def run_gate(self, df):
        return mark_buy_ready(df, MODE, session_date=TODAY)


def clean_board(n, **over):
    rows = []
    for i in range(n):
        r = {"Stock_ID": "%04d" % (7000 + i), "Market": "OTC", "Data_Date": TODAY,
             "Close_Price": 100.0, "Core_Plus": True, "Integrity_OK": True,
             "Hold_Status": "pending", "Trade_Restriction": "none",
             "Launch_Score": 100.0 - i}
        r.update(over)
        rows.append(r)
    return pd.DataFrame(rows)


# one failing condition per Buy_Block code, lowest priority first
BREAKERS = [
    ("restricted", lambda d, i: d.__setitem__((i, "Trade_Restriction"), "suspended")),
    ("held", lambda d, i: d.__setitem__((i, "Hold_Status"), "holding")),
    ("unknown", lambda d, i: d.__setitem__((i, "Hold_Status"), "")),
    ("quality", lambda d, i: d.__setitem__((i, "Core_Plus"), False)),
    ("market", lambda d, i: d.__setitem__((i, "Market"), "TSE")),
    ("rank", None),                       # a position, not a value: see build()
    ("integrity", lambda d, i: d.__setitem__((i, "Integrity_OK"), False)),
    ("stale", lambda d, i: d.__setitem__((i, "Data_Date"), "2026-09-08")),
]
ORDER = [c for c, _ in BREAKERS]


def board_with(codes):
    """22 clean rows; one extra row that fails every condition in `codes`.
    Returns (frame, position of the row under test)."""
    df = clean_board(22)
    pos = 21 if "rank" in codes else 3
    for code, fn in BREAKERS:
        if code in codes and fn is not None:
            df.loc[pos, {"restricted": "Trade_Restriction", "held": "Hold_Status",
                         "unknown": "Hold_Status", "quality": "Core_Plus",
                         "market": "Market", "integrity": "Integrity_OK",
                         "stale": "Data_Date"}[code]] = {
                "restricted": "suspended", "held": "holding", "unknown": "",
                "quality": False, "market": "TSE", "integrity": False,
                "stale": "2026-09-08"}[code]
    return df, pos


class BuyGateRank(GateCase):
    def test_only_the_first_twenty_rows_can_be_bought(self):
        out = self.run_gate(clean_board(25))
        self.assertEqual(list(out["Buy_Ready"]), [True] * 20 + [False] * 5)
        self.assertEqual(list(out["Buy_Block"].iloc[:20]), [""] * 20)
        self.assertEqual(list(out["Buy_Block"].iloc[20:]), ["rank"] * 5)

    def test_the_cutoff_follows_the_shipped_order_not_the_score(self):
        # rank is the row position: a high score at position 20 is still refused
        df = clean_board(22)
        df.loc[20, "Launch_Score"] = 999.0
        out = self.run_gate(df)
        self.assertFalse(bool(out["Buy_Ready"].iloc[20]))
        self.assertTrue(bool(out["Buy_Ready"].iloc[19]))

    def test_n_enter_is_twenty(self):
        self.assertEqual(sm.N_ENTER, 20)

    def test_a_regime_lookup_that_raises_blocks_everything(self):
        def boom(*a, **k):
            raise RuntimeError("taiex db locked")
        with mock.patch.object(market_regime, "get_market_regime", boom):
            out = self.run_gate(clean_board(3))
        self.assertEqual(list(out["Buy_Ready"]), [False] * 3)
        self.assertEqual(list(out["Buy_Block"]), ["regime"] * 3)

    def test_an_unreadable_regime_blocks_even_with_enter_ok_set(self):
        for reg in ({"ok": False, "enter_ok": True, "risk_on": True, "is_current": True},
                    {"enter_ok": True, "risk_on": True, "is_current": True}):
            with mock.patch.object(market_regime, "get_market_regime",
                                   lambda *a, _r=reg, **k: dict(_r)):
                out = self.run_gate(clean_board(2))
            self.assertEqual(list(out["Buy_Ready"]), [False, False], reg)
            self.assertEqual(list(out["Buy_Block"]), ["regime", "regime"], reg)

    def test_enter_ok_is_what_opens_the_gate_not_risk_on(self):
        reg = {"ok": True, "enter_ok": False, "risk_on": True, "is_current": True}
        with mock.patch.object(market_regime, "get_market_regime", lambda *a, **k: dict(reg)):
            out = self.run_gate(clean_board(2))
        self.assertEqual(list(out["Buy_Ready"]), [False, False])
        self.assertEqual(list(out["Buy_Block"]), ["regime", "regime"])

    def test_a_stale_regime_is_its_own_reason(self):
        reg = {"ok": True, "enter_ok": True, "risk_on": True, "is_current": False}
        with mock.patch.object(market_regime, "get_market_regime", lambda *a, **k: dict(reg)):
            out = self.run_gate(clean_board(2))
        self.assertEqual(list(out["Buy_Block"]), ["regime_stale", "regime_stale"])


class BuyGateOtcOnly(GateCase):
    def test_a_tse_row_with_every_other_gate_green_is_refused_as_market(self):
        out = self.run_gate(clean_board(1, Market="TSE"))
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "market")

    def test_only_otc_rows_of_a_mixed_board_are_bought(self):
        df = clean_board(6)
        df["Market"] = ["OTC", "TSE", "OTC", "TSE", "TSE", "OTC"]
        out = self.run_gate(df)
        self.assertEqual(list(out["Buy_Ready"]), [True, False, True, False, False, True])
        self.assertEqual(list(out["Buy_Block"]), ["", "market", "", "market", "market", ""])

    def test_a_missing_market_column_blocks_every_row(self):
        df = clean_board(3).drop(columns=["Market"])
        out = self.run_gate(df)
        self.assertEqual(list(out["Buy_Ready"]), [False] * 3)
        self.assertEqual(list(out["Buy_Block"]), ["market"] * 3)


class BuyBlockPrecedence(GateCase):
    """The reason shown is the MOST actionable failing one: a later code in
    ORDER overwrites an earlier one, and the market gate overrides all."""

    def test_each_condition_alone_gives_its_own_code(self):
        for code in ORDER:
            df, pos = board_with({code})
            out = self.run_gate(df)
            self.assertFalse(bool(out["Buy_Ready"].iloc[pos]), code)
            self.assertEqual(out["Buy_Block"].iloc[pos], code)

    def test_when_two_conditions_fail_the_later_code_wins(self):
        checked = 0
        for i, a in enumerate(ORDER):
            for b in ORDER[i + 1:]:
                if {a, b} == {"held", "unknown"}:
                    continue            # one Hold_Status cannot be both
                df, pos = board_with({a, b})
                out = self.run_gate(df)
                self.assertEqual(out["Buy_Block"].iloc[pos], b, (a, b))
                self.assertFalse(bool(out["Buy_Ready"].iloc[pos]), (a, b))
                checked += 1
        self.assertEqual(checked, 27)

    def test_the_emitted_codes_are_exactly_the_reviewed_set(self):
        seen = set()
        for code in ORDER:
            df, pos = board_with({code})
            seen.add(self.run_gate(df)["Buy_Block"].iloc[pos])
        seen.add(mark_buy_ready(clean_board(1), "mode_squeeze")["Buy_Block"].iloc[0])
        reg = {"ok": True, "enter_ok": False, "risk_on": True, "is_current": True}
        with mock.patch.object(market_regime, "get_market_regime", lambda *a, **k: dict(reg)):
            seen.add(self.run_gate(clean_board(1))["Buy_Block"].iloc[0])
        reg["enter_ok"], reg["is_current"] = True, False
        with mock.patch.object(market_regime, "get_market_regime", lambda *a, **k: dict(reg)):
            seen.add(self.run_gate(clean_board(1))["Buy_Block"].iloc[0])
        self.assertEqual(seen, {"restricted", "held", "unknown", "quality", "market", "rank",
                                "integrity", "stale", "regime", "regime_stale", "no_rule"})
        from scanner.result_checks import BUY_BLOCKS
        self.assertEqual(set(BUY_BLOCKS) - {""}, seen | {"dropped"})

    def test_the_market_regime_overrides_every_other_reason(self):
        reg = {"ok": True, "enter_ok": False, "risk_on": False, "is_current": True}
        df, pos = board_with({"quality", "stale", "integrity"})
        with mock.patch.object(market_regime, "get_market_regime", lambda *a, **k: dict(reg)):
            out = self.run_gate(df)
        self.assertEqual(set(out["Buy_Block"]), {"regime"})

    def test_a_mode_without_a_validated_rule_is_never_buyable(self):
        for mode in ("mode_squeeze", "mode_breakout", "mode_bottom"):
            out = mark_buy_ready(clean_board(2), mode, session_date=TODAY)
            self.assertEqual(list(out["Buy_Ready"]), [False, False], mode)
            self.assertEqual(list(out["Buy_Block"]), ["no_rule"] * 2, mode)


class OnlySuspendedBlocks(GateCase):
    def test_the_blocking_set_is_suspended_only(self):
        import scanner.trade_restrictions as trs
        self.assertEqual(tuple(trs.BLOCKING_RESTRICTIONS), ("suspended",))
        self.assertEqual(tuple(trs.RESTRICTION_KINDS),
                         ("suspended", "disposition", "altered", "limit_lock",
                          "unknown", "attention", "none"))

    def test_every_other_kind_is_displayed_not_enforced(self):
        for kind in ("disposition", "altered", "limit_lock", "attention", "none"):
            out = self.run_gate(clean_board(1, Trade_Restriction=kind))
            self.assertTrue(bool(out["Buy_Ready"].iloc[0]), kind)
            self.assertEqual(out["Buy_Block"].iloc[0], "", kind)

    def test_suspended_refuses_when_everything_else_passes(self):
        out = self.run_gate(clean_board(1, Trade_Restriction="suspended"))
        self.assertFalse(bool(out["Buy_Ready"].iloc[0]))
        self.assertEqual(out["Buy_Block"].iloc[0], "restricted")


# ---------------------------------------------------- rejected variants stay out
HOSTILE = {
    # each was studied and REJECTED (docs/STRATEGY.md 3.7, BACKTEST_LOG): a US
    # overnight shock, a post-exit cooldown, a locked-limit-up signal day, and
    # every chip (institutional / foreign / large-holder) input
    "SOX_Chg": -6.0, "VIX": 45.0, "US_Shock": True, "Cooldown_Days": 0,
    "Days_Since_Exit": 0, "Limit_Up": True, "Prev_Limit_Up": True,
    "Restriction_Flags": "limit_lock,limit_down",
    "Foreign_Net_5D": -9.9e6, "Inst_Net": -5.0e6, "Inst_Pct": -40.0,
    "Inst_Streak": -9, "Chip_Action": "sell", "Large_Holder_Pct": 99.0,
    "Large_Holder_Chg": -25.0, "Chip_Note": "sell",
}


class RejectedVariantsStayAbsent(GateCase):
    def test_hostile_columns_leave_buy_ready_unchanged(self):
        base = self.run_gate(clean_board(25))
        hostile = clean_board(25)
        for k, v in HOSTILE.items():
            hostile[k] = v
        out = self.run_gate(hostile)
        self.assertEqual(list(out["Buy_Ready"]), list(base["Buy_Ready"]))
        self.assertEqual(list(out["Buy_Block"]), list(base["Buy_Block"]))
        self.assertEqual(int(out["Buy_Ready"].sum()), 20)

    def test_hostile_columns_leave_ranking_and_selection_unchanged(self):
        base = ranked()
        hostile = ranked()
        for k, v in HOSTILE.items():
            hostile[k] = v
        hostile.loc[::2, "Foreign_Net_5D"] = 9.9e6
        hostile.loc[::3, "Inst_Net"] = 5.0e6
        prior = ["25", "40", "79", "80"]
        self.assertEqual(list(sort_for_mode(hostile, MODE)["Stock_ID"]),
                         list(sort_for_mode(base, MODE)["Stock_ID"]))
        self.assertEqual(select_with_hysteresis(hostile, prior)[1],
                         select_with_hysteresis(base, prior)[1])
        self.assertEqual(list(apply_scan_mode(hostile, MODE)["Stock_ID"]),
                         list(apply_scan_mode(base, MODE)["Stock_ID"]))
        a = add_trade_columns(hostile.assign(Close_Price=100.0), MODE)
        b = add_trade_columns(base.assign(Close_Price=100.0), MODE)
        for col in ("Strict_Stop_Loss", "Target_Price", "Trail_Arm_Price",
                    "Trail_Lock_Price", "Add_Price", "Scale_Out_Price", "Risk_Pct"):
            self.assertEqual(list(a[col]), list(b[col]), col)

    def test_launch_score_ignores_institutional_foreign_and_holder_columns(self):
        from analyzer.trend_analysis import calc_launch_score
        n = 120
        close = [50.0 + i * 0.5 for i in range(n)]
        df = pd.DataFrame({"close": close, "high": [c + 1 for c in close],
                           "low": [c - 1 for c in close], "Volume_Lot": [3000.0] * n})
        want = calc_launch_score(df)
        self.assertGreater(want["Launch_Score"], 0.0)
        for k, v in HOSTILE.items():
            df[k] = v
        df["Foreign_Net_5D"] = [(-1) ** i * 1e7 for i in range(n)]
        self.assertEqual(calc_launch_score(df), want)

    def test_both_chip_legs_are_disabled_and_stay_inert(self):
        from scanner import chip_signal as cs
        for name, rule in cs.CHIP_RULES.items():
            self.assertIs(rule["enabled"], False, name)
        self.assertEqual(sorted(cs.CHIP_RULES), ["add", "sell"])
        self.assertFalse(cs.any_rule_enabled())
        self.assertFalse(cs.sell_signal(-90.0, 9))
        self.assertFalse(cs.add_signal(90.0, 9, 1))
        df = pd.DataFrame([
            {"Stock_ID": "8069", "Hold_Status": "holding", "Hold_Day": 2,
             "Inst_Net": net, "Vol_MA20": 1000.0, "Inst_Date": TODAY,
             "Data_Date": TODAY, "Inst_Streak": streak, "Inst_Sessions": 5}
            for net, streak in ((-900.0, -5), (900.0, 5), (0.0, 0))])
        out = cs.annotate_chip_action(df, MODE)
        self.assertEqual(list(out["Chip_Action"]), ["", "", ""])
        self.assertTrue(all("confirmation only" in n for n in out["Chip_Note"]))

    def test_no_foreign_market_or_chip_term_exists_in_production_code(self):
        # the source guard: the study that rejected these left no live code
        banned = re.compile(r"\^SOX|\^VIX|\^IXIC|\^GSPC|\^DJI|SOXX|NQ=F|ES=F|SOX_Chg|"
                            r"US_Shock|us_shock|Cooldown|cooldown|Limit_Up|limit_up")
        hits = []
        for rel in ("scanner", "analyzer", "ingestion", "storage", "portfolio",
                    "config", "gui", "tools"):
            for p in (ROOT / rel).rglob("*.py"):
                if banned.search(p.read_text(encoding="utf-8", errors="replace")):
                    hits.append(str(p.relative_to(ROOT)))
        for rel in ("mobile/app.js", "scan_headless.py", "main_gui.py"):
            if banned.search((ROOT / rel).read_text(encoding="utf-8", errors="replace")):
                hits.append(rel)
        self.assertEqual(hits, [])
        src = (ROOT / "ingestion" / "market_index.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'yf\.download\(\s*([A-Za-z_\^"]+)', src), ["TAIEX_YF"])
        self.assertIn('TAIEX_YF   = "^TWII"', src)

    def test_the_selection_never_reads_the_buy_gate_inputs_it_does_not_own(self):
        # scoring and selection code names no chip column at all
        for rel in ("analyzer/trend_analysis.py", "scanner/scan_mode.py"):
            code = re.sub(r"(?m)#.*$", "", (ROOT / rel).read_text(encoding="utf-8"))
            code = re.sub(r'"""[\s\S]*?"""', "", code)
            for token in ("Foreign", "Inst_", "Large_Holder", "Holder", "Chip_"):
                self.assertNotIn(token, code, "%s mentions %s" % (rel, token))


# ---------------------------------------------------------------- market regime
def regime_of(closes, ref=None, last="2026-10-08", sheet=None):
    """The real get_market_regime over a synthetic TAIEX series. `ref` is the
    bar date the scan works on (default: the series' own last bar)."""
    t = taiex_frame(closes, last) if sheet is None else sheet
    ref = ref or last
    with raw_index(), mock.patch.object(market_regime, "load_sheet", lambda *a, **k: t), \
            mock.patch.object(market_regime, "max_stored_date", lambda *a, **k: ref):
        return market_regime.get_market_regime(ref)


def rising(n=80):
    return [100.0 + i for i in range(n)]


class MarketRegimeTruthTable(unittest.TestCase):
    def test_above_both_means_is_the_only_tailwind(self):
        r = regime_of(rising())
        self.assertTrue(r["ok"] and r["is_current"] and r["above20"])
        self.assertTrue(r["risk_on"] and r["enter_ok"])

    def test_below_the_20_above_the_60_is_neutral_and_does_not_enter(self):
        r = regime_of([100.0 + i for i in range(100)] + [195, 192, 190, 188, 186])
        self.assertFalse(r["above20"])
        self.assertTrue(r["risk_on"])
        self.assertFalse(r["enter_ok"])
        self.assertFalse(r["strong"])

    def test_above_the_20_below_the_60_is_a_headwind(self):
        r = regime_of([200.0] * 40 + [100.0] * 19 + [130.0])
        self.assertTrue(r["ok"] and r["above20"])
        self.assertFalse(r["risk_on"])
        self.assertFalse(r["enter_ok"])

    def test_below_both_is_a_headwind(self):
        r = regime_of([200.0 - i for i in range(80)])
        self.assertTrue(r["ok"])
        self.assertFalse(r["above20"] or r["risk_on"] or r["enter_ok"])

    def test_a_close_equal_to_a_mean_is_not_above_it(self):
        # equal to the 20-day mean (110), above the 60-day mean (103.33)
        r = regime_of([100.0] * 40 + [110.0] * 20)
        self.assertFalse(r["above20"])
        self.assertTrue(r["risk_on"])
        self.assertFalse(r["enter_ok"])
        # equal to the 60-day mean (100.0), above the 20-day mean (90.5)
        r = regime_of([104.75] * 40 + [90.0] * 19 + [100.0])
        self.assertTrue(r["above20"])
        self.assertFalse(r["risk_on"])
        self.assertFalse(r["enter_ok"])
        # a flat tape is above neither
        r = regime_of([100.0] * 80)
        self.assertFalse(r["above20"] or r["risk_on"] or r["enter_ok"])

    def test_one_tick_above_the_mean_is_above(self):
        r = regime_of([100.0] * 40 + [110.0] * 19 + [110.25])
        self.assertTrue(r["above20"] and r["enter_ok"])

    def test_the_means_are_exactly_20_and_60_bars_long(self):
        # a spike on the OLDEST bar of the 20-bar window is inside it
        r = regime_of([100.0] * 40 + [200.0] + [99.0] * 18 + [101.0])
        self.assertFalse(r["above20"])          # 20-bar mean 104.15
        # ... and one bar older it is outside (a 21-bar window would see it)
        r = regime_of([100.0] * 39 + [200.0] + [99.0] * 19 + [101.0])
        self.assertTrue(r["above20"])           # 20-bar mean 99.1
        # the same on the 60-bar window
        r = regime_of([100.0] * 20 + [300.0] + [100.0] * 58 + [102.0])
        self.assertFalse(r["risk_on"])          # 60-bar mean 103.37
        r = regime_of([100.0] * 19 + [300.0] + [100.0] * 59 + [102.0])
        self.assertTrue(r["risk_on"])           # 60-bar mean 100.03

    def test_strong_starts_at_2_2_percent_above_the_20_day_mean(self):
        base = [1000.0] * 79
        r = regime_of(base + [1023.1])          # str20 0.0219
        self.assertEqual(r["str20"], 0.0219)
        self.assertTrue(r["enter_ok"])
        self.assertFalse(r["strong"])
        r = regime_of(base + [1023.2])          # str20 0.0220
        self.assertEqual(r["str20"], 0.022)
        self.assertTrue(r["strong"])


class MarketRegimeFailsClosed(unittest.TestCase):
    def test_fifty_nine_bars_is_unknown_sixty_is_enough(self):
        r = regime_of(rising(59))
        self.assertFalse(r["ok"] or r["risk_on"] or r["enter_ok"] or r["strong"])
        r = regime_of(rising(60))
        self.assertTrue(r["ok"] and r["enter_ok"])

    def test_an_empty_or_unreadable_series_is_unknown(self):
        r = regime_of([], sheet=pd.DataFrame())
        self.assertIs(r["ok"], False)
        self.assertIs(r["enter_ok"], False)

        def boom(*a, **k):
            raise OSError("taiex.db locked")
        with mock.patch.object(market_regime, "load_sheet", boom), \
                mock.patch.object(market_regime, "max_stored_date", lambda *a, **k: "2026-10-08"):
            r = market_regime.get_market_regime("2026-10-08")
        for k in ("ok", "risk_on", "enter_ok", "strong", "is_current"):
            self.assertIs(r[k], False, k)

    def test_nan_closes_are_dropped_and_row_order_is_irrelevant(self):
        t = taiex_frame(rising(80))
        t.loc[len(t)] = ["2026-10-09", float("nan")]
        r = regime_of(None, sheet=t)
        self.assertEqual(r["as_of_date"], "2026-10-08")
        self.assertTrue(r["enter_ok"])
        shuffled = taiex_frame(rising(80)).iloc[::-1].reset_index(drop=True)
        self.assertEqual(regime_of(None, sheet=shuffled), regime_of(rising(80)))

    def test_a_cache_older_than_the_scanned_bar_is_never_a_tailwind(self):
        fresh = regime_of(rising(), ref="2026-10-08", last="2026-10-08")
        self.assertTrue(fresh["is_current"] and fresh["enter_ok"])
        stale = regime_of(rising(), ref="2026-10-09", last="2026-10-08")
        self.assertFalse(stale["is_current"])
        for k in ("risk_on", "enter_ok", "strong"):
            self.assertIs(stale[k], False, k)
        self.assertTrue(stale["above20"])           # the raw reading stays

    def test_the_unknown_default_has_every_permission_off(self):
        r = regime_of([], sheet=pd.DataFrame())
        for k in ("ok", "risk_on", "enter_ok", "strong", "is_current"):
            self.assertIs(r[k], False, k)


if __name__ == "__main__":
    unittest.main()
