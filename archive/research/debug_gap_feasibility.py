"""Feasibility probe for filling the data gaps:
  (1) how many stocks does the FULL market actually have (vs our 315)?
  (2) can yfinance give multi-year history with regime variety (a bear)?
"""
import numpy as np
import pandas as pd
from scanner.market_filter import fetch_full_market
from ingestion.price_volume_multi import fetch_yfinance

print("=== (1) full-market universe size ===")
full = fetch_full_market()
print("full-market valid stocks:", len(full))
print("  TSE:", int((full["market"] == "TSE").sum()),
      " OTC:", int((full["market"] == "OTC").sum()))
print("  vs our current db: 315 (momentum-selected subset)")

print("\n=== (2) long-history availability (yfinance) ===")
for sid, mkt in [("2330", "TSE"), ("2317", "TSE")]:
    df = fetch_yfinance(sid, market=mkt, lookback_days=1200)   # ~3.3y
    if df.empty:
        print("  {}: no data".format(sid)); continue
    c = pd.to_numeric(df["close"], errors="coerce").to_numpy(float)
    dd = (c / np.maximum.accumulate(c) - 1).min()
    print("  {}: bars={} span {}..{}  total={:+.0%}  maxDD={:.1%}".format(
        sid, len(df), df["date"].iloc[0], df["date"].iloc[-1],
        c[-1]/c[0]-1, dd))
