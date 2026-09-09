import sqlite3
import pandas as pd
from config.settings import PRICE_VOLUME_FILE
from analyzer.signal_evaluator import _evaluate_conditions

con = sqlite3.connect(PRICE_VOLUME_FILE)
g = pd.read_sql_query("SELECT * FROM data WHERE stock_id='3236' ORDER BY date", con)
con.close()

print("bars:", len(g), " latest:", g["date"].max())
ev = _evaluate_conditions(g.copy())
cols = ["date", "close", "Volume_Lot", "Range_Tightness",
        "Volume_Dryup_Ratio", "Volume_Bias", "Explosion_Score"]
tail = ev[cols].tail(8).reset_index(drop=True)
pd.set_option("display.width", 160)
print(tail.to_string(index=False))

# day-over-day explosion score delta
print("\nExplosion_Score last 8 deltas:")
es = ev["Explosion_Score"].tail(8).reset_index(drop=True)
dts = ev["date"].tail(8).reset_index(drop=True)
for i in range(1, len(es)):
    print("  {} -> {}:  {:.1f} -> {:.1f}   (delta {:+.1f})".format(
        dts[i-1], dts[i], es[i-1], es[i], es[i]-es[i-1]))
