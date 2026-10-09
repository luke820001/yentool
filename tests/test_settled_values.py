"""The settled values, in ONE reviewable table. ASCII only, stdlib unittest.

Every row is (name, where it lives, the value the owner decided). A test that
reads a constant back from the module it guards can never fail when the
constant drifts; a literal in a table can, and the failure names the value
that moved. Changing a number here is a decision about the strategy, not a
refactor: see docs/STRATEGY.md section 3 and docs/BACKTEST_LOG.md first, then
change the code, the phone copy (mobile/app.js STRATEGY), the docs and this
row in the same commit.

The behaviour of each rule is pinned at its edge in tests/test_pin_*.py; this
file is the inventory (a 2026-10-09 audit found the numbers were pinned only
by accident: N_HOLD, the regime and market-leg windows, the rank cut and the
costs not at all).

    python -m unittest tests.test_settled_values -v
"""
import inspect
import unittest
from decimal import Decimal


def _mod(name):
    import importlib
    return importlib.import_module(name)


def _attr(module, name):
    return lambda: getattr(_mod(module), name)


def _default(module, func, param):
    return lambda: inspect.signature(getattr(_mod(module), func)).parameters[param].default


def _rule(key):
    return lambda: _mod("scanner.exit_rules").DEFAULT_RULE[key]


# (name, getter, expected). Percentages are fractions unless the name says pct.
SETTLED = [
    # --- entry: selection and gates -----------------------------------------
    ("N_ENTER (a new name must rank inside the top)", _attr("scanner.scan_mode", "N_ENTER"), 20),
    ("N_HOLD (a held name survives to this rank)", _attr("scanner.scan_mode", "N_HOLD"), 80),
    ("CORE+ Dist_52W_High_Pct max", _attr("scanner.scan_mode", "CORE_PLUS_DIST52_MAX"), 5.0),
    ("CORE+ Ret_5D_Pct max", _attr("scanner.scan_mode", "CORE_PLUS_RET5_MAX"), 5.0),
    ("CORE+ ATR_Pct min", _attr("scanner.scan_mode", "CORE_PLUS_ATR_MIN"), 4.5),
    ("buy-rule modes", _attr("scanner.scan_mode", "BUY_RULE_MODES"), ("mode_prelaunch",)),
    ("STRATEGY_VERSION", _attr("scanner.scan_mode", "STRATEGY_VERSION"), "prelaunch-2026-09-21"),
    ("BUY_RULE_VERSION", _attr("scanner.scan_mode", "BUY_RULE_VERSION"), "prelaunch-2026-09-21"),
    # --- exit: the order levels the scan prints -----------------------------
    ("PRELAUNCH_STOP_PCT", _attr("scanner.scan_mode", "PRELAUNCH_STOP_PCT"), 0.20),
    ("PRELAUNCH_TP_PCT", _attr("scanner.scan_mode", "PRELAUNCH_TP_PCT"), 0.20),
    ("PRELAUNCH_TRAIL_ARM", _attr("scanner.scan_mode", "PRELAUNCH_TRAIL_ARM"), 0.025),
    ("PRELAUNCH_TRAIL_LOCK", _attr("scanner.scan_mode", "PRELAUNCH_TRAIL_LOCK"), 0.02),
    ("PRELAUNCH_ADD_PCT (optional staged add)", _attr("scanner.scan_mode", "PRELAUNCH_ADD_PCT"), 0.10),
    ("PRELAUNCH_ADD_FIRST (share bought first)", _attr("scanner.scan_mode", "PRELAUNCH_ADD_FIRST"), 0.5),
    ("PRELAUNCH_SCALE_OUT_PCT (optional)", _attr("scanner.scan_mode", "PRELAUNCH_SCALE_OUT_PCT"), 0.15),
    # --- exit: the engine every replay shares -------------------------------
    ("engine stop_pct", _rule("stop_pct"), 0.20),
    ("engine tp_pct", _rule("tp_pct"), 0.20),
    ("engine arm_pct (a CLOSE at or above)", _rule("arm_pct"), 0.025),
    ("engine lock_pct (guards from the NEXT bar)", _rule("lock_pct"), 0.02),
    ("engine hold_bars (time exit)", _rule("hold_bars"), 10),
    ("engine late_from (first day of the late profit-take)", _rule("late_from"), 8),
    ("engine late_gain (close at or above +1%)", _rule("late_gain"), 0.01),
    ("engine ride_cap (latest bar of the ride)", _rule("ride_cap"), 20),
    ("engine DEFAULT_RULE has no other key", lambda: sorted(_mod("scanner.exit_rules").DEFAULT_RULE),
     ["arm_pct", "hold_bars", "late_from", "late_gain", "lock_pct", "ride_cap", "stop_pct", "tp_pct"]),
    ("tracker hold bars for the prelaunch mode", lambda: _mod("scanner.holding_tracker").HOLD_BARS_BY_MODE,
     {"mode_prelaunch": 10}),
    ("tracker STOP_PCT", _attr("scanner.holding_tracker", "STOP_PCT"), 0.20),
    ("tracker TP_PCT", _attr("scanner.holding_tracker", "TP_PCT"), 0.20),
    ("tracker TRAIL_ARM", _attr("scanner.holding_tracker", "TRAIL_ARM"), 0.025),
    ("tracker TRAIL_LOCK", _attr("scanner.holding_tracker", "TRAIL_LOCK"), 0.02),
    ("tracker ADD_PCT", _attr("scanner.holding_tracker", "ADD_PCT"), 0.10),
    ("tracker SCALE_OUT_PCT", _attr("scanner.holding_tracker", "SCALE_OUT_PCT"), 0.15),
    ("tracker LATE_FROM", _attr("scanner.holding_tracker", "LATE_FROM"), 8),
    ("tracker LATE_GAIN", _attr("scanner.holding_tracker", "LATE_GAIN"), 0.01),
    # --- costs --------------------------------------------------------------
    ("live record BUY_COST (0.1425%)", _attr("scanner.live_record", "BUY_COST"), 0.001425),
    ("live record SELL_COST (0.1425% + 0.3% tax)", _attr("scanner.live_record", "SELL_COST"), 0.001425 + 0.003),
    ("fee schedule broker rate", lambda: _mod("portfolio.money").FeeSchedule.default().broker_fee_rate, Decimal("0.001425")),
    ("fee schedule sell tax", lambda: _mod("portfolio.money").FeeSchedule.default().sell_tax_rate, Decimal("0.003")),
    ("fee schedule minimum fee (NT$)", lambda: _mod("portfolio.money").FeeSchedule.default().min_fee, Decimal("20")),
    ("fee schedule discount", lambda: _mod("portfolio.money").FeeSchedule.default().discount, Decimal("1.0")),
    ("fee schedule rounds to the dollar", lambda: _mod("portfolio.money").FeeSchedule.default().round_to_dollar, True),
    # --- the tick ladder ----------------------------------------------------
    ("share tick ladder", _attr("scanner.tick", "TICKS"),
     ((10.0, 0.01), (50.0, 0.05), (100.0, 0.1), (500.0, 0.5), (1000.0, 1.0), (None, 5.0))),
    ("ETF tick ladder", _attr("scanner.tick", "ETF_TICKS"), ((50.0, 0.01), (None, 0.05))),
    # --- the universe: prefilter and feed floors ----------------------------
    ("prefilter config of the prelaunch mode", lambda: _mod("scanner.market_filter")._MODE_CFG["mode_prelaunch"],
     {"min_vol": 0, "min_turnover": 50000000, "price_max": None, "cap": 300, "rank_by": "turnover"}),
    ("feed sanity floors (rows per exchange)", _attr("scanner.market_filter", "FEED_MIN_ROWS"),
     {"TSE": 500, "OTC": 3000}),
    ("chip_verifier MIN_HISTORY_BARS", _attr("scanner.chip_verifier", "MIN_HISTORY_BARS"), 240),
    ("quote_feed FEED_SESSIONS", _attr("scanner.quote_feed", "FEED_SESSIONS"), 30),
    ("market_snapshot BASIS_TOLERANCE_PCT", _attr("scanner.market_snapshot", "BASIS_TOLERANCE_PCT"), 0.5),
    ("tracked_rows TRACKED_SESSIONS", _attr("scanner.tracked_rows", "TRACKED_SESSIONS"), 30),
    ("tracked_rows MAX_TRACKED", _attr("scanner.tracked_rows", "MAX_TRACKED"), 60),
    # --- data integrity -----------------------------------------------------
    ("integrity LIMIT_JUMP", _attr("scanner.data_integrity", "LIMIT_JUMP"), 0.105),
    ("integrity SPLIT_RATIO", _attr("scanner.data_integrity", "SPLIT_RATIO"), 3.0),
    ("integrity MIN_BARS_MA60", _attr("scanner.data_integrity", "MIN_BARS_MA60"), 60),
    ("integrity MIN_BARS_52W", _attr("scanner.data_integrity", "MIN_BARS_52W"), 240),
    ("integrity RECENT_BARS", _attr("scanner.data_integrity", "RECENT_BARS"), 60),
    ("non-session SESSION_WINDOW", _attr("scanner.data_integrity", "SESSION_WINDOW"), 40),
    ("non-session SESSION_SHARE", _attr("scanner.data_integrity", "SESSION_SHARE"), 0.20),
    ("non-session SESSION_NEIGHBOURS", _attr("scanner.data_integrity", "SESSION_NEIGHBOURS"), 8),
    ("non-session SESSION_FLOOR", _attr("scanner.data_integrity", "SESSION_FLOOR"), 100),
    # --- restrictions: only 'suspended' blocks ------------------------------
    ("blocking restrictions", lambda: tuple(_mod("scanner.trade_restrictions").BLOCKING_RESTRICTIONS), ("suspended",)),
    ("restriction kinds, most severe first", lambda: tuple(_mod("scanner.trade_restrictions").RESTRICTION_KINDS),
     ("suspended", "disposition", "altered", "limit_lock", "unknown", "attention", "none")),
    ("TPEX look-ahead days", _attr("scanner.trade_restrictions", "TPEX_WEB_DAYS_AHEAD"), 14),
    ("TWSE look-back days", _attr("scanner.trade_restrictions", "TWSE_WEB_DAYS_BACK"), 30),
    ("restriction fetch budget (s)", _attr("scanner.trade_restrictions", "FETCH_BUDGET_S"), 75.0),
    # --- chips are display only: both legs stay OFF -------------------------
    ("chip sell leg enabled", lambda: _mod("scanner.chip_signal").CHIP_RULES["sell"]["enabled"], False),
    ("chip add leg enabled", lambda: _mod("scanner.chip_signal").CHIP_RULES["add"]["enabled"], False),
    ("chip legs that exist", lambda: sorted(_mod("scanner.chip_signal").CHIP_RULES), ["add", "sell"]),
    # --- the list is final once, from 15:00 Taipei --------------------------
    ("list freeze policy", _attr("scanner.list_freeze", "POLICY"), "final-once-v1"),
    ("list freeze: not final before", _attr("scanner.list_freeze", "FINAL_NOT_BEFORE"), "15:00"),
    # --- the record and its self-check --------------------------------------
    ("live record MIN_BARS_FOR_CORE", _attr("scanner.live_record", "MIN_BARS_FOR_CORE"), 120),
    ("live record FULL_52W_BARS", _attr("scanner.live_record", "FULL_52W_BARS"), 240),
    ("live record TRADES_KEPT", _attr("scanner.live_record", "TRADES_KEPT"), 40),
    ("live record BY_SID_KEPT", _attr("scanner.live_record", "BY_SID_KEPT"), 5),
    ("live record BENCH_POINTS", _attr("scanner.live_record", "BENCH_POINTS"), 120),
    ("live record starts (classify_signals)", _default("scanner.live_record", "classify_signals", "since"), "2026-06-25"),
    ("live record starts (build_live_record)", _default("scanner.live_record", "build_live_record", "since"), "2026-06-25"),
    ("signal ledger horizons", _attr("scanner.signal_ledger", "HORIZONS"), (5, 10, 20)),
    ("checker recommendation horizon (sessions)", _attr("scanner.result_checks", "REC_HORIZON_SESSIONS"), 25),
    ("lifecycle recommendation horizon (sessions)", _attr("portfolio.sync", "REC_HORIZON_SESSIONS"), 25),
    ("lifecycle closed grace (sessions)", _attr("portfolio.sync", "CLOSED_GRACE_SESSIONS"), 5),
    ("checker bench points", _attr("scanner.result_checks", "BENCH_MAX_POINTS"), 120),
    ("checker by-name points", _attr("scanner.result_checks", "BY_SID_MAX"), 5),
    ("checker blocking default", _attr("scanner.result_checks", "DEFAULT_BLOCKING_RESTRICTIONS"), ("suspended",)),
    ("a recommendation is a 10-day plan", _default("portfolio.ledger", "record_recommendation", "horizon_days"), 10),
]

# Settled pairs that live in two places and must stay equal.
MIRRORS = [
    ("scan_mode stop == engine stop", _attr("scanner.scan_mode", "PRELAUNCH_STOP_PCT"), _rule("stop_pct")),
    ("scan_mode target == engine tp", _attr("scanner.scan_mode", "PRELAUNCH_TP_PCT"), _rule("tp_pct")),
    ("scan_mode arm == engine arm", _attr("scanner.scan_mode", "PRELAUNCH_TRAIL_ARM"), _rule("arm_pct")),
    ("scan_mode lock == engine lock", _attr("scanner.scan_mode", "PRELAUNCH_TRAIL_LOCK"), _rule("lock_pct")),
    ("tracker hold == engine hold", lambda: _mod("scanner.holding_tracker").HOLD_BARS_BY_MODE["mode_prelaunch"],
     _rule("hold_bars")),
    ("tracker late day == engine", _attr("scanner.holding_tracker", "LATE_FROM"), _rule("late_from")),
    ("tracker late gain == engine", _attr("scanner.holding_tracker", "LATE_GAIN"), _rule("late_gain")),
    ("checker blocking == producer blocking", _attr("scanner.result_checks", "DEFAULT_BLOCKING_RESTRICTIONS"),
     lambda: tuple(_mod("scanner.trade_restrictions").BLOCKING_RESTRICTIONS)),
    ("checker bench == record bench", _attr("scanner.result_checks", "BENCH_MAX_POINTS"),
     _attr("scanner.live_record", "BENCH_POINTS")),
    ("checker by-name == record by-name", _attr("scanner.result_checks", "BY_SID_MAX"),
     _attr("scanner.live_record", "BY_SID_KEPT")),
    ("checker horizon == lifecycle horizon", _attr("scanner.result_checks", "REC_HORIZON_SESSIONS"),
     _attr("portfolio.sync", "REC_HORIZON_SESSIONS")),
    ("lifecycle horizon == ride cap + 5", _attr("portfolio.sync", "REC_HORIZON_SESSIONS"),
     lambda: _mod("scanner.exit_rules").DEFAULT_RULE["ride_cap"] + 5),
]


class SettledValues(unittest.TestCase):
    def test_every_row_still_holds_the_value_the_owner_decided(self):
        wrong = []
        for name, getter, want in SETTLED:
            try:
                got = getter()
            except Exception as exc:                    # a renamed constant is a drift too
                wrong.append("%s: cannot be read (%s: %s)" % (name, type(exc).__name__, exc))
                continue
            same = (got == want) and (type(got) is type(want) or isinstance(want, (int, float)) and
                                      isinstance(got, (int, float)) and not isinstance(got, bool)
                                      and not isinstance(want, bool))
            if not same:
                wrong.append("%s: settled %r, found %r" % (name, want, got))
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    def test_the_settled_pairs_that_live_twice_agree(self):
        wrong = []
        for name, left, right in MIRRORS:
            a, b = left(), right()
            if a != b:
                wrong.append("%s: %r != %r" % (name, a, b))
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    def test_the_table_is_not_silently_emptied(self):
        self.assertGreaterEqual(len(SETTLED), 80)
        self.assertGreaterEqual(len(MIRRORS), 12)
        names = [n for n, _, _ in SETTLED]
        self.assertEqual(len(names), len(set(names)), "duplicate row names")


class FeesAreTheTaiwanSchedule(unittest.TestCase):
    """Worked examples, so a changed rate or minimum fails with a number."""

    def schedule(self):
        from portfolio.money import FeeSchedule
        return FeeSchedule.default()

    def test_the_fee_is_0_1425_percent_truncated_to_the_dollar_with_a_floor_of_20(self):
        s = self.schedule()
        self.assertEqual(s.buy_fee(100, 100), Decimal("20"))          # 14.25 -> 14 -> floor 20
        self.assertEqual(s.buy_fee(100, 1000), Decimal("142"))        # 142.5 -> 142
        self.assertEqual(s.buy_fee(1000, 1000), Decimal("1425"))
        self.assertEqual(s.sell_fee(1000, 1000), Decimal("1425"))     # the sell side pays the same fee

    def test_the_minimum_fee_applies_below_the_break_even_amount(self):
        s = self.schedule()
        # 14,035 x 0.1425% = 19.99987 -> truncates to 19 -> floor 20 ; 14,036 -> 20.001 -> 20
        self.assertEqual(s.buy_fee(14035, 1), Decimal("20"))
        self.assertEqual(s.buy_fee(14036, 1), Decimal("20"))
        self.assertEqual(s.buy_fee(14100, 1), Decimal("20"))
        self.assertEqual(s.buy_fee(15000, 1), Decimal("21"))          # 21.375 -> 21

    def test_the_sell_tax_is_0_3_percent(self):
        s = self.schedule()
        self.assertEqual(s.sell_tax(1000, 1000), Decimal("3000"))
        self.assertEqual(s.sell_tax(100, 1000), Decimal("300"))

    def test_the_round_trip_on_a_million_is_5850(self):
        s = self.schedule()
        _, fee_in, total_in = s.buy_cost(1000, 1000)
        _, fee_out, tax, net = s.sell_proceeds(1000, 1000)
        self.assertEqual(fee_in + fee_out + tax, Decimal("5850"))     # 1425 + 1425 + 3000
        self.assertEqual(total_in - net, Decimal("5850"))

    def test_the_exact_schedule_keeps_the_same_rates_with_no_rounding(self):
        from portfolio.money import FeeSchedule
        s = FeeSchedule.exact()
        self.assertEqual(s.broker_fee_rate, Decimal("0.001425"))
        self.assertEqual(s.sell_tax_rate, Decimal("0.003"))
        self.assertEqual(s.min_fee, Decimal("0"))
        self.assertFalse(s.round_to_dollar)


if __name__ == "__main__":
    unittest.main()
