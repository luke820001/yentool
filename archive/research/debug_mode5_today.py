"""Apply the pre-launch momentum screen to the LATEST bar of each stock in the
db, to show what mode 5 would surface right now."""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE

con = sqlite3.connect(PRICE_VOLUME_FILE)
df = pd.read_sql_query("SELECT * FROM data", con)
con.close()

picks = []
for sid, g in df.groupby("stock_id"):
    g = g.sort_values("date").reset_index(drop=True)
    if len(g) < 64:
        continue
    c = pd.to_numeric(g["close"], errors="coerce").to_numpy(float)
    v = pd.to_numeric(g["Volume_Lot"], errors="coerce").to_numpy(float)
    i = len(c) - 1
    close = c[i]
    ma5, ma10, ma20, ma60 = (np.nanmean(c[i-4:i+1]), np.nanmean(c[i-9:i+1]),
                             np.nanmean(c[i-19:i+1]), np.nanmean(c[i-59:i+1]))
    g60 = (close / c[i-63] - 1) * 100 if c[i-63] > 0 else -999
    g20 = (close / c[i-20] - 1) * 100 if c[i-20] > 0 else -999
    up = tot = 0.0
    for k in range(i-9, i+1):
        if np.isnan(v[k]):
            continue
        tot += v[k]
        if c[k] > c[k-1]:
            up += v[k]
    bias = up / tot if tot > 0 else 0
    vol_ma20 = np.nanmean(v[i-19:i+1])

    if (close > ma60 and ma5 > ma10 > ma20 and g60 >= 20 and g20 >= 5
            and bias >= 0.50 and vol_ma20 > 300):
        picks.append((sid, round(close, 1), round(g60, 1), round(g20, 1),
                      round(bias, 2), int(vol_ma20)))

picks.sort(key=lambda r: r[2], reverse=True)
print("as-of date:", df["date"].max())
print("mode 5 picks today:", len(picks))
print("{:<8}{:>8}{:>8}{:>8}{:>7}{:>9}".format("id", "close", "g3m%", "g1m%", "bias", "volMA20"))
for r in picks[:25]:
    print("{:<8}{:>8}{:>8}{:>8}{:>7}{:>9}".format(*r))
