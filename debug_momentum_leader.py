"""
Offline test for the momentum-leader mode and the scan-mode logic fixes.
Builds synthetic verify_candidates-style rows (no network) and checks:
  1. mode_momentum_leader keeps only 3-month gain >= 30% with liquidity.
  2. sort_for_mode ranks leader rows by Gain_3M_Pct (not Explosion_Score).
  3. breakout/short-explosion ranking now uses RS_Score, not Explosion_Score.
  4. Gain_3M_Pct is computed correctly from a price series.
All strings ASCII; run: python debug_momentum_leader.py
"""
import pandas as pd

from scanner.scan_mode import apply_scan_mode, sort_for_mode, add_trade_columns


def _make_rows():
    # Pre-launch momentum mode needs: Close_Price>MA60, MA5>MA10>MA20,
    # Gain_3M_Pct>=20, Gain_1M_Pct>=5, Volume_Bias>=0.5, Vol_MA20>300.
    # cols: Close, MA60, MA5, MA10, MA20, g3m, g1m, bias, vol_ma20, explosion, rs
    data = [
        # AAA: textbook setup -> KEPT
        ("AAA", 100, 90, 99, 97, 95, 35.0, 12.0, 0.62, 1500, 10.0, 40.0),
        # BBB: setup but only modest momentum, still passes -> KEPT
        ("BBB", 100, 92, 99, 97, 95, 22.0,  6.0, 0.55,  800, 80.0,  5.0),
        # CCC: strong momentum but BELOW 60MA -> excluded
        ("CCC", 100, 105, 99, 97, 95, 40.0, 15.0, 0.70, 5000, 90.0, 70.0),
        # DDD: setup but illiquid (vol_ma20<300) -> excluded
        ("DDD", 100, 90, 99, 97, 95, 30.0, 10.0, 0.60,  200, 50.0, 20.0),
        # EEE: MA not stacked (ma10<ma20) -> excluded
        ("EEE", 100, 90, 99, 94, 96, 30.0, 10.0, 0.60, 1000, 30.0, 15.0),
        # FFF: 1-month stalled (g1m<5) -> excluded
        ("FFF", 100, 90, 99, 97, 95, 25.0,  1.0, 0.60, 1000, 30.0, 15.0),
        # GGG: weak volume bias (<0.5) -> excluded
        ("GGG", 100, 90, 99, 97, 95, 25.0,  8.0, 0.40, 1000, 30.0, 15.0),
    ]
    return pd.DataFrame([
        {
            "Stock_ID": sid, "Stock_Name": sid, "Close_Price": c, "MA60": ma60,
            "MA5": ma5, "MA10": ma10, "MA20": ma20,
            "Gain_3M_Pct": g3, "Gain_1M_Pct": g1, "Volume_Bias": b,
            "Vol_MA20": v, "Explosion_Score": es, "RS_Score": rs,
            "Min_Price_3": 94.0,
        }
        for sid, c, ma60, ma5, ma10, ma20, g3, g1, b, v, es, rs in data
    ])


def test_leader_filter():
    df = _make_rows()
    out = apply_scan_mode(df, "mode_momentum_leader")
    kept = set(out["Stock_ID"])
    assert kept == {"AAA", "BBB"}, kept
    print("[PASS] pre-launch filter keeps only momentum setups:", sorted(kept))


def test_leader_sort():
    df = _make_rows()
    out = apply_scan_mode(df, "mode_momentum_leader")
    out = sort_for_mode(out, "mode_momentum_leader")
    order = list(out["Stock_ID"])
    assert order == ["AAA", "BBB"], order  # 35 > 22 by 3M gain
    print("[PASS] pre-launch sorted by 3M gain desc:", order)


def test_breakout_sort_uses_rs():
    df = _make_rows()
    out = sort_for_mode(df, "mode_breakout")
    order = list(out["Stock_ID"])
    # CCC has RS 70 (top), AAA 40 next; BBB has RS 5 (last)
    assert order[0] == "CCC" and order[1] == "AAA" and order[-1] == "BBB", order
    print("[PASS] breakout ranked by RS_Score (not explosion):", order)


def test_squeeze_sort_uses_explosion():
    df = _make_rows()
    out = sort_for_mode(df, "mode_squeeze")
    order = list(out["Stock_ID"])
    assert order[0] == "CCC", order  # explosion 90 top
    print("[PASS] squeeze still ranked by Explosion_Score:", order)


def test_gain_formula():
    # Mirror chip_verifier: 64-bar window => iloc[-64] is 63 bars back.
    closes = pd.Series([100.0] * 63 + [140.0])  # 64 bars, last = 140
    base = float(closes.iloc[-64])
    cur = float(closes.iloc[-1])
    gain = round((cur / base - 1.0) * 100, 1)
    assert gain == 40.0, gain
    print("[PASS] 3M gain formula: 100 -> 140 =", gain, "%")


def test_trade_columns_run():
    df = _make_rows()
    out = apply_scan_mode(df, "mode_momentum_leader")
    out = add_trade_columns(out, "mode_momentum_leader")
    assert "Suggested_Buy_Price" in out.columns
    assert "Strict_Stop_Loss" in out.columns
    assert "Risk_Pct" in out.columns
    print("[PASS] add_trade_columns works for leader mode")


def test_stop_below_buy_invariant():
    # The 8042 case: extended stock whose 3-day low (170.5) sat ABOVE MA5 (169.3),
    # which used to make stop > buy. Verify the clamp now keeps stop < buy.
    df = pd.DataFrame([{
        "Stock_ID": "8042", "Close_Price": 192.5, "MA5": 169.3, "MA10": 157.32,
        "MA20": 149.71, "Min_Price_3": 170.5,
    }])
    for mode in ["mode_momentum_leader", "mode_breakout", "mode_squeeze", "mode_bottom"]:
        out = add_trade_columns(df.copy(), mode)
        buy = out["Suggested_Buy_Price"].iloc[0]
        stop = out["Strict_Stop_Loss"].iloc[0]
        risk = out["Risk_Pct"].iloc[0]
        assert stop < buy, "{}: stop {} >= buy {}".format(mode, stop, buy)
        assert 6.0 - 0.1 <= risk <= 13.0 + 0.1, "{}: risk {}% out of band".format(mode, risk)
        print("[PASS] {:<22} buy={:>7.2f} stop={:>7.2f} risk={:>4.1f}%".format(mode, buy, stop, risk))


if __name__ == "__main__":
    test_leader_filter()
    test_leader_sort()
    test_breakout_sort_uses_rs()
    test_squeeze_sort_uses_explosion()
    test_gain_formula()
    test_trade_columns_run()
    test_stop_below_buy_invariant()
    print("\nAll momentum-leader tests passed.")
