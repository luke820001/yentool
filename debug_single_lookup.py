"""Exercise the manual single-stock lookup path (no GUI): resolve market ->
verify_candidates -> add_trade_columns, and print the resulting row."""
import sys
import pandas as pd
from ingestion.price_volume_multi import resolve_market
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import add_trade_columns

code = sys.argv[1] if len(sys.argv) > 1 else "2330"
mode = sys.argv[2] if len(sys.argv) > 2 else "mode_momentum_leader"

market = resolve_market(code)
print("resolved market:", market)

cands = pd.DataFrame([{"stock_id": code, "market": market, "stock_name": code}])
res = verify_candidates(cands, progress_callback=lambda r, t, m: None)
if res is None or res.empty:
    print("NO DATA for", code)
    sys.exit(0)

res = add_trade_columns(res, mode)
row = res.iloc[0]
show = ["Stock_ID", "Close_Price", "Suggested_Buy_Price", "Strict_Stop_Loss",
        "Risk_Pct", "Explosion_Score", "RS_Score", "Gain_3M_Pct", "Gain_1M_Pct",
        "Dist_52W_High_Pct", "MA5", "MA10", "MA20", "MA60",
        "MA_Bull_Align", "MACD_Cross", "Cond_A", "Cond_C"]
print("\n--- single-stock result ({} / {}) ---".format(code, mode))
for k in show:
    if k in res.columns:
        print("  {:<20} {}".format(k, row.get(k)))
assert row["Strict_Stop_Loss"] < row["Suggested_Buy_Price"], "stop >= buy!"
print("\n[OK] stop < buy, row produced with all columns.")
