"""
Acceptance tests for portfolio/. Stdlib unittest -- pytest is not in
requirements.txt and CI installs nothing else, so these must run anywhere:

    python -m unittest discover -s tests -v

Almost every case here is lifted straight out of the report rather than
invented, because the report's numbers are the specification:

  section 6.2   the ten-day worked example, D1..D10, +10,000 cumulative
  section 6.3   fees and tax: 145.35 / 159.60 / 336.00 -> net 9,359.05 (9.16%)
  section 12    the acceptance table (fixed first day, real fill, early exit,
                expiry, resubmission, missing data, ...)

If one of these ever fails, the ledger has started lying about money.
"""
import unittest
from decimal import Decimal

from portfolio import ledger as L
from portfolio.money import (D, FeeSchedule, round_to_tick, tick_size,
                             shares_from_lots, pct)

STRATEGY = "mode_prelaunch"
SV = "prelaunch-2026-08-06"

# A plain 14-session calendar. Real closures come from trading_sessions; the
# point of passing an explicit list is that nothing here infers trading days
# from weekday arithmetic (report F14).
CAL = ["2026-09-0{}".format(d) for d in (1, 2, 3, 4, 7, 8, 9)] + \
      ["2026-09-{}".format(d) for d in (10, 11, 14, 15, 16, 17, 18)]

# section 6.2: recommendation 100, filled 102, 1,000 shares, D1..D10 closes.
CLOSES = ["103", "101", "105", "106", "104", "108", "107", "110", "109", "112"]
# section 6.2 table, in order.
EXPECT_DAY = [1000, -2000, 4000, 1000, -2000, 4000, -1000, 3000, -1000, 3000]
EXPECT_CUM = [1000, -1000, 3000, 4000, 2000, 6000, 5000, 8000, 7000, 10000]


class LedgerCase(unittest.TestCase):
    """In-memory ledger per test."""

    def setUp(self):
        self.conn = L.open_ledger(":memory:")

    def tearDown(self):
        self.conn.close()

    # -- helpers ------------------------------------------------------------
    def recommend(self, price="100", session=CAL[0], **kw):
        return L.record_recommendation(
            self.conn, "8069", STRATEGY, SV, session, price,
            stock_name="TestCo", market="OTC", stop_price="85",
            target_price="120", horizon_days=10, **kw)

    def buy(self, rec_id=None, price="102", shares=1000, session=CAL[1],
            schedule="tw-equity-exact"):
        pos_id = L.open_position(
            self.conn, "8069", "TestCo", "OTC", STRATEGY, SV,
            recommendation_id=rec_id, fee_schedule=schedule, horizon_days=10)
        L.add_execution(self.conn, pos_id, "BUY", session, shares, price)
        return pos_id

    def run_ten_days(self, pos_id):
        """Mark D1..D10 with the section 6.2 closes. D1 is CAL[1]."""
        for i, close in enumerate(CLOSES):
            L.mark_day(self.conn, pos_id, CAL[1 + i], close, calendar=CAL,
                       price_source="test")


class TestFixedFirstDay(LedgerCase):
    """Report section 12, rows '首日固定' and '首次合格'. This is F01."""

    def test_recommendation_price_never_moves(self):
        rec_id, created = self.recommend("100")
        self.assertTrue(created)
        # The next day the stock closes at 110 and the scanner would recompute
        # a "suggested buy" of 110. The recommendation must not follow it.
        again_id, created_again = self.recommend("110", session=CAL[1])
        self.assertFalse(created_again, "a second cycle must not open")
        self.assertEqual(rec_id, again_id, "same recommendation_id expected")
        row = self.conn.execute(
            "SELECT initial_buy_price, first_qualified_session FROM recommendations"
            " WHERE recommendation_id = ?", (rec_id,)).fetchone()
        self.assertEqual(row["initial_buy_price"], "100.00")
        self.assertEqual(row["first_qualified_session"], CAL[0])

    def test_rescanning_the_same_day_is_idempotent(self):
        rec_id, _ = self.recommend("100")
        for _ in range(9):   # 14:30, 17:00, 18:00 and manual re-runs
            self.recommend("100")
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM recommendations").fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertTrue(rec_id.endswith("-1"))

    def test_a_new_cycle_only_after_the_old_one_ends(self):
        rec_id, _ = self.recommend("100")
        self.conn.execute("UPDATE recommendations SET status='expired' "
                          "WHERE recommendation_id = ?", (rec_id,))
        self.conn.commit()
        second_id, created = self.recommend("130", session=CAL[5])
        self.assertTrue(created)
        self.assertNotEqual(rec_id, second_id)
        self.assertEqual(
            self.conn.execute("SELECT initial_buy_price FROM recommendations "
                              "WHERE recommendation_id = ?",
                              (rec_id,)).fetchone()["initial_buy_price"],
            "100.00", "closing a cycle must not disturb the old fixed price")

    def test_zero_or_negative_recommendation_price_refused(self):
        for bad in ("0", "-5"):
            with self.assertRaises(L.LedgerError):
                L.record_recommendation(self.conn, "9999", STRATEGY, SV,
                                        CAL[0], bad)


class TestRealFillWins(LedgerCase):
    """Report section 12, row '真實成交': recommended 100, bought 102 -> every
    P&L and risk number keys off 102, and bad input never silently becomes 100."""

    def test_pnl_uses_the_fill_not_the_recommendation(self):
        rec_id, _ = self.recommend("100")
        pos_id = self.buy(rec_id, price="102", shares=1000)
        L.mark_day(self.conn, pos_id, CAL[2], "112", calendar=CAL)
        view = L.position_view(self.conn, pos_id)
        self.assertEqual(view["avg_cost"], "102.00")
        self.assertEqual(view["initial_buy_price"], "100.00")
        # Both returns are correct answers to DIFFERENT questions and the report
        # forbids them sharing a title.
        self.assertEqual(view["return_vs_initial"], "12.00")
        self.assertEqual(view["return_vs_cost"], "9.80")

    def test_invalid_fill_input_is_refused_not_defaulted(self):
        """F11. The phone did `num(raw) || ref`, so 'abc' and 0 both stored the
        reference price as a real trade. Every one of these must raise."""
        pos_id = L.open_position(self.conn, "8069", strategy=STRATEGY)
        for bad in ("abc", "", None, "0", "-1", "  "):
            with self.assertRaises(L.LedgerError,
                                   msg="price {!r} must be refused".format(bad)):
                L.add_execution(self.conn, pos_id, "BUY", CAL[1], 1000, bad)
        for bad_shares in (0, -100, "many", 1.5):
            with self.assertRaises(L.LedgerError):
                L.add_execution(self.conn, pos_id, "BUY", CAL[1], bad_shares, "102")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM executions").fetchone()["n"],
            0, "no execution may survive a rejected input")

    def test_execution_without_a_session_date_is_refused(self):
        pos_id = L.open_position(self.conn, "8069", strategy=STRATEGY)
        with self.assertRaises(L.LedgerError):
            L.add_execution(self.conn, pos_id, "BUY", "", 1000, "102")

    def test_selling_more_than_held_is_refused(self):
        pos_id = self.buy(price="102", shares=1000)
        with self.assertRaises(L.LedgerError):
            L.add_execution(self.conn, pos_id, "SELL", CAL[3], 2000, "110")

    def test_resubmitting_the_same_fill_does_not_double_count(self):
        """Report section 12, '重送與並發'."""
        pos_id = L.open_position(self.conn, "8069", strategy=STRATEGY)
        first, created = L.add_execution(self.conn, pos_id, "BUY", CAL[1], 1000,
                                         "102", idempotency_key="phone-abc-1")
        second, created_again = L.add_execution(self.conn, pos_id, "BUY", CAL[1],
                                                1000, "102",
                                                idempotency_key="phone-abc-1")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)
        pos = self.conn.execute("SELECT open_shares FROM positions "
                                "WHERE position_id = ?", (pos_id,)).fetchone()
        self.assertEqual(int(pos["open_shares"]), 1000)


class TestTenDayPnL(LedgerCase):
    """Report section 6.2's table, reproduced exactly."""

    def setUp(self):
        super(TestTenDayPnL, self).setUp()
        rec_id, _ = self.recommend("100")
        self.pos_id = self.buy(rec_id, price="102", shares=1000)
        self.run_ten_days(self.pos_id)

    def test_daily_and_cumulative_match_the_report(self):
        marks = self.conn.execute(
            "SELECT session_date, day_index, day_pnl_gross, total_gross "
            "FROM position_daily_marks WHERE position_id = ? AND is_current = 1 "
            "ORDER BY session_date", (self.pos_id,)).fetchall()
        self.assertEqual(len(marks), 10)
        for i, m in enumerate(marks):
            self.assertEqual(m["day_index"], i + 1)
            self.assertEqual(D(m["day_pnl_gross"]), D(EXPECT_DAY[i]),
                             "D{} daily P&L".format(i + 1))
            self.assertEqual(D(m["total_gross"]), D(EXPECT_CUM[i]),
                             "D{} cumulative P&L".format(i + 1))

    def test_ten_day_total_is_the_last_value_not_the_sum(self):
        """The report's sharpest warning: summing the ten cumulative figures
        double-counts every earlier day. D10 is 10,000, not 34,000."""
        marks = self.conn.execute(
            "SELECT total_gross FROM position_daily_marks WHERE position_id = ? "
            "AND is_current = 1 ORDER BY session_date", (self.pos_id,)).fetchall()
        cumulative = [D(m["total_gross"]) for m in marks]
        self.assertEqual(cumulative[-1], D(10000))
        # Adding the ten running totals gives 45,000 -- four and a half times
        # the real answer, because every day re-counts all the days before it.
        self.assertEqual(sum(cumulative), D(45000))
        self.assertNotEqual(cumulative[-1], sum(cumulative))
        # But the DAILY figures do legitimately add to the total.
        days = self.conn.execute(
            "SELECT day_pnl_gross FROM position_daily_marks WHERE position_id = ? "
            "AND is_current = 1", (self.pos_id,)).fetchall()
        self.assertEqual(sum(D(d["day_pnl_gross"]) for d in days), D(10000))

    def test_fees_and_tax_reproduce_the_report_figure(self):
        """Report section 6.3: buy fee 145.35, sell fee 159.60, tax 336.00,
        estimated net 9,359.05, net return about 9.16%."""
        sched = FeeSchedule.exact()
        self.assertEqual(sched.buy_fee("102", 1000), D("145.35"))
        self.assertEqual(sched.sell_fee("112", 1000), D("159.60"))
        self.assertEqual(sched.sell_tax("112", 1000), D("336.00"))

        mark = self.conn.execute(
            "SELECT * FROM position_daily_marks WHERE position_id = ? "
            "AND day_index = 10 AND is_current = 1", (self.pos_id,)).fetchone()
        self.assertEqual(D(mark["cost_basis"]), D("102145.35"))
        self.assertEqual(D(mark["total_gross"]), D("10000.00"))
        self.assertEqual(D(mark["net_if_liquidated"]), D("9359.05"))
        self.assertEqual(pct(D(mark["net_if_liquidated"]), D("102145.35")),
                         D("9.16"))

    def test_three_totals_are_distinct(self):
        """gross / net_book / net_if_liquidated must never share a label."""
        mark = self.conn.execute(
            "SELECT * FROM position_daily_marks WHERE position_id = ? "
            "AND day_index = 10 AND is_current = 1", (self.pos_id,)).fetchone()
        gross = D(mark["total_gross"])
        book = D(mark["total_book"])
        liquidated = D(mark["net_if_liquidated"])
        self.assertEqual(gross, D("10000.00"))
        self.assertEqual(book, D("9854.65"))       # gross minus the buy fee
        self.assertEqual(liquidated, D("9359.05"))  # minus exit fee + tax
        self.assertEqual(len({gross, book, liquidated}), 3)

    def test_day_ten_freezes_and_day_eleven_cannot_move_it(self):
        """Report section 5.3 / section 12 '到期未賣'."""
        cycle_id, created = L.freeze_cycle(self.conn, self.pos_id, 10)
        self.assertTrue(created)
        frozen = self.conn.execute(
            "SELECT * FROM cycle_results WHERE cycle_id = ?",
            (cycle_id,)).fetchone()
        self.assertEqual(D(frozen["total_gross"]), D(10000))
        self.assertEqual(frozen["still_open"], 1)
        self.assertEqual(frozen["return_vs_initial"], "12.00")
        self.assertEqual(frozen["return_vs_cost"], "9.80")

        # D11 crashes. The position keeps being valued; the frozen row does not.
        L.mark_day(self.conn, self.pos_id, CAL[11], "80", calendar=CAL)
        again_id, created_again = L.freeze_cycle(self.conn, self.pos_id, 10)
        self.assertFalse(created_again)
        self.assertEqual(again_id, cycle_id)
        still = self.conn.execute(
            "SELECT total_gross FROM cycle_results WHERE cycle_id = ?",
            (cycle_id,)).fetchone()
        self.assertEqual(D(still["total_gross"]), D(10000))

        view = L.position_view(self.conn, self.pos_id)
        self.assertEqual(view["day_index"], 11)
        self.assertEqual(view["status"], "open",
                         "reaching D10 must not close a position nobody sold")


class TestEarlyExitAndPartials(LedgerCase):
    """Report section 12, rows '提早結束' and '數量'."""

    def test_selling_everything_on_d4_freezes_the_outcome(self):
        rec_id, _ = self.recommend("100")
        pos_id = self.buy(rec_id, price="102", shares=1000)
        for i, close in enumerate(CLOSES[:4]):
            L.mark_day(self.conn, pos_id, CAL[1 + i], close, calendar=CAL)
        L.add_execution(self.conn, pos_id, "SELL", CAL[4], 1000, "106")

        pos = self.conn.execute("SELECT * FROM positions WHERE position_id = ?",
                                (pos_id,)).fetchone()
        self.assertEqual(int(pos["open_shares"]), 0)
        self.assertEqual(pos["status"], "closed")
        self.assertEqual(pos["closed_session"], CAL[4])
        realized = D(pos["realized_net"])
        # 106,000 - 151.05 fee - 318.00 tax - 102,145.35 basis
        self.assertEqual(realized, D("3385.60"))

        # D5..D10 keep printing prices. None of them may touch the realised P&L.
        L.mark_day(self.conn, pos_id, CAL[4], "106", calendar=CAL)
        for i, close in enumerate(CLOSES[5:], start=5):
            L.mark_day(self.conn, pos_id, CAL[1 + i], close, calendar=CAL)
        after = self.conn.execute("SELECT realized_net FROM positions "
                                  "WHERE position_id = ?", (pos_id,)).fetchone()
        self.assertEqual(D(after["realized_net"]), realized)
        final = self.conn.execute(
            "SELECT total_book FROM position_daily_marks WHERE position_id = ? "
            "AND is_current = 1 ORDER BY session_date DESC LIMIT 1",
            (pos_id,)).fetchone()
        self.assertEqual(D(final["total_book"]), realized,
                         "a closed position's total is its realised result")

    def test_partial_sell_uses_moving_weighted_average(self):
        pos_id = self.buy(price="100", shares=1000)
        L.add_execution(self.conn, pos_id, "BUY", CAL[2], 1000, "110")
        pos = self.conn.execute("SELECT * FROM positions WHERE position_id = ?",
                                (pos_id,)).fetchone()
        self.assertEqual(int(pos["open_shares"]), 2000)
        self.assertEqual(D(pos["avg_cost"]), D("105.00"))
        # basis = 100,000 + 142.50 + 110,000 + 156.75 = 210,299.25
        self.assertEqual(D(pos["cost_basis"]), D("210299.25"))

        L.add_execution(self.conn, pos_id, "SELL", CAL[3], 500, "120")
        pos = self.conn.execute("SELECT * FROM positions WHERE position_id = ?",
                                (pos_id,)).fetchone()
        self.assertEqual(int(pos["open_shares"]), 1500)
        self.assertEqual(pos["status"], "open", "1,500 shares is not closed")
        # A quarter of the basis leaves with the shares; avg_cost is unchanged
        # by a sale under weighted average.
        self.assertEqual(D(pos["cost_basis"]), D("157724.44"))
        self.assertEqual(D(pos["avg_cost"]), D("105.00"))

    def test_only_zero_shares_closes_a_position(self):
        pos_id = self.buy(price="100", shares=1000)
        for _ in range(3):
            L.add_execution(self.conn, pos_id, "SELL", CAL[3], 300, "110")
        pos = self.conn.execute("SELECT open_shares, status FROM positions "
                                "WHERE position_id = ?", (pos_id,)).fetchone()
        self.assertEqual(int(pos["open_shares"]), 100)
        self.assertEqual(pos["status"], "open")
        L.add_execution(self.conn, pos_id, "SELL", CAL[4], 100, "110")
        pos = self.conn.execute("SELECT open_shares, status, cost_basis FROM "
                                "positions WHERE position_id = ?",
                                (pos_id,)).fetchone()
        self.assertEqual(int(pos["open_shares"]), 0)
        self.assertEqual(pos["status"], "closed")
        self.assertEqual(D(pos["cost_basis"]), D(0),
                         "an empty position must not carry residual cost cents")

    def test_lots_convert_to_shares(self):
        self.assertEqual(shares_from_lots(1), 1000)
        self.assertEqual(shares_from_lots("2.5"), 2500)
        self.assertEqual(shares_from_lots(3, lot_size=100), 300)


class TestDataFaults(LedgerCase):
    """Report section 12, rows '資料缺漏' and '名單變化'."""

    def test_a_missing_price_carries_forward_and_says_so(self):
        pos_id = self.buy(price="102", shares=1000)
        L.mark_day(self.conn, pos_id, CAL[1], "103", calendar=CAL)
        L.mark_day(self.conn, pos_id, CAL[2], None, calendar=CAL)
        mark = self.conn.execute(
            "SELECT * FROM position_daily_marks WHERE position_id = ? "
            "AND session_date = ? AND is_current = 1",
            (pos_id, CAL[2])).fetchone()
        self.assertEqual(mark["data_status"], "stale")
        self.assertEqual(mark["price_source"], "carried:{}".format(CAL[1]))
        self.assertEqual(D(mark["close_price"]), D("103"))
        self.assertEqual(D(mark["day_pnl_gross"]), D(0),
                         "a carried price is not a gain")

    def test_missing_with_no_history_is_missing_not_zero_profit(self):
        pos_id = self.buy(price="102", shares=1000)
        L.mark_day(self.conn, pos_id, CAL[1], None, calendar=CAL)
        mark = self.conn.execute(
            "SELECT * FROM position_daily_marks WHERE position_id = ?",
            (pos_id,)).fetchone()
        self.assertEqual(mark["data_status"], "missing")
        self.assertIsNone(mark["close_price"])
        self.assertIsNone(mark["net_if_liquidated"])

    def test_position_is_valued_even_when_the_scan_never_mentions_it(self):
        """F04: the phone looked prices up in today's scan rows only, so a
        position that dropped off the list went blank. Valuation here depends
        on nothing but the position and a close."""
        pos_id = self.buy(price="102", shares=1000)
        for i, close in enumerate(CLOSES):
            L.mark_day(self.conn, pos_id, CAL[1 + i], close, calendar=CAL)
        view = L.position_view(self.conn, pos_id)
        self.assertEqual(D(view["close_price"]), D("112"))
        self.assertEqual(view["data_status"], "current")

    def test_unknown_calendar_leaves_day_index_unknown(self):
        pos_id = self.buy(price="102", shares=1000)
        L.mark_day(self.conn, pos_id, "2027-01-04", "110", calendar=CAL)
        mark = self.conn.execute(
            "SELECT day_index FROM position_daily_marks WHERE position_id = ? "
            "AND session_date = '2027-01-04'", (pos_id,)).fetchone()
        self.assertIsNone(mark["day_index"],
                          "an unknown trading day must not be guessed")

    def test_restating_a_day_supersedes_rather_than_overwrites(self):
        pos_id = self.buy(price="102", shares=1000)
        L.mark_day(self.conn, pos_id, CAL[1], "103", calendar=CAL)
        L.mark_day(self.conn, pos_id, CAL[1], "104", calendar=CAL)
        rows = self.conn.execute(
            "SELECT revision, close_price, is_current FROM position_daily_marks "
            "WHERE position_id = ? AND session_date = ? ORDER BY revision",
            (pos_id, CAL[1])).fetchall()
        self.assertEqual([r["revision"] for r in rows], [1, 2])
        self.assertEqual([r["is_current"] for r in rows], [0, 1])
        self.assertEqual(D(rows[0]["close_price"]), D("103"),
                         "the superseded value stays on the record")


class TestPortfolioSummary(LedgerCase):
    """Report section 6.4: amounts add up, percentages do not, and mixed
    valuation dates must be declared."""

    def test_amounts_add_and_stale_positions_are_named(self):
        a = self.buy(price="100", shares=1000)
        b = L.open_position(self.conn, "1234", "Other", "TSE", STRATEGY, SV,
                            fee_schedule="tw-equity-exact")
        L.add_execution(self.conn, b, "BUY", CAL[1], 1000, "50")
        L.mark_day(self.conn, a, CAL[3], "110", calendar=CAL)
        L.mark_day(self.conn, b, CAL[1], "50", calendar=CAL)   # not updated since

        summary = L.portfolio_summary(self.conn, session_date=CAL[3])
        self.assertEqual(summary["open_positions"], 2)
        self.assertEqual(D(summary["total_gross"]), D("10000.00"))
        self.assertFalse(summary["valuation_complete"])
        self.assertEqual([s["stock_id"] for s in summary["stale_positions"]],
                         ["1234"])
        self.assertEqual(summary["valuation_dates"], [CAL[1], CAL[3]])

    def test_return_on_cost_is_none_without_a_cost_base(self):
        summary = L.portfolio_summary(self.conn)
        self.assertIsNone(summary["return_on_cost_pct"])
        self.assertTrue(summary["valuation_complete"])


class TestMoney(unittest.TestCase):
    """Report section 8: two decimals on screen is not an orderable price."""

    def test_tick_bands(self):
        self.assertEqual(tick_size("9.99"), D("0.01"))
        self.assertEqual(tick_size("10"), D("0.05"))
        self.assertEqual(tick_size("50"), D("0.1"))
        self.assertEqual(tick_size("99.9"), D("0.1"))
        self.assertEqual(tick_size("100"), D("0.5"))
        self.assertEqual(tick_size("500"), D("1"))
        self.assertEqual(tick_size("1000"), D("5"))

    def test_stops_round_down_and_targets_round_up(self):
        # 86.70 is not on the 50-100 ladder (0.1 tick) -- it is; 86.73 is not.
        self.assertEqual(round_to_tick("86.73", "down"), D("86.70"))
        self.assertEqual(round_to_tick("86.73", "up"), D("86.80"))
        self.assertEqual(round_to_tick("122.40", "up"), D("122.50"))
        self.assertEqual(round_to_tick("122.40", "down"), D("122.00"))

    def test_decimal_never_goes_through_float(self):
        self.assertEqual(D(0.1) + D(0.2), D("0.3"))
        self.assertEqual(D("0.1") * 3, D("0.3"))

    def test_realistic_schedule_applies_minimum_and_dollar_rounding(self):
        sched = FeeSchedule.default()
        # A tiny odd-lot trade hits the NT$20 floor rather than paying 1.42.
        self.assertEqual(sched.buy_fee("10", 100), D("20.00"))
        # 102,000 * 0.1425% = 145.35 -> truncated to the dollar.
        self.assertEqual(sched.buy_fee("102", 1000), D("145.00"))

    def test_pct_returns_none_rather_than_a_confident_zero(self):
        self.assertIsNone(pct(100, 0))
        self.assertEqual(pct(10, 200), D("5.00"))


class TestSchema(unittest.TestCase):
    def test_ensure_schema_is_idempotent_and_versioned(self):
        from portfolio import schema
        conn = schema.connect(":memory:")
        self.assertEqual(schema.current_version(conn), 0)
        self.assertEqual(schema.ensure_schema(conn), schema.SCHEMA_VERSION)
        self.assertEqual(schema.ensure_schema(conn), schema.SCHEMA_VERSION)
        self.assertEqual(schema.current_version(conn), schema.SCHEMA_VERSION)
        conn.close()

    def test_foreign_keys_are_enforced(self):
        import sqlite3
        conn = L.open_ledger(":memory:")
        with self.assertRaises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO executions (execution_id, position_id, side, "
                    "session_date, shares, price, recorded_at) "
                    "VALUES ('x','nope','BUY','2026-09-01',1,'1','t')")
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
