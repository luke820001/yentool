"""Illustrative (CONTEMPORANEOUS, not predictive) look: does current large-holder
/ retail % relate to trailing 3-month return across the market? Joins the latest
TDCC snapshot (large_holder.db) with trailing returns from research_prices.db.
Heavily confounded (winning attracts holders); not a validation. ASCII only."""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import LARGE_HOLDER_FILE, DATA_DIR

# latest chip snapshot
con = sqlite3.connect(LARGE_HOLDER_FILE)
chip = pd.read_sql_query("SELECT stock_id,Large_Holder_Pct,Retail_Pct FROM data", con)
con.close()
chip["stock_id"] = chip["stock_id"].astype(str)

# trailing 63-day return per stock from research db
con = sqlite3.connect(DATA_DIR / "research_prices.db")
px = pd.read_sql_query("SELECT stock_id,date,close FROM data", con)
con.close()
rets = {}
for sid, g in px.groupby("stock_id"):
    c = pd.to_numeric(g.sort_values("date")["close"], errors="coerce").dropna()
    if len(c) >= 64:
        rets[str(sid)] = c.iloc[-1] / c.iloc[-64] - 1
r = pd.Series(rets, name="ret3m")

df = chip.set_index("stock_id").join(r, how="inner").dropna()
df["Large_Holder_Pct"] = pd.to_numeric(df["Large_Holder_Pct"], errors="coerce")
df["Retail_Pct"] = pd.to_numeric(df["Retail_Pct"], errors="coerce")
df = df.dropna()
print("stocks joined:", len(df))
print("corr(large%, 3m ret): {:+.3f}   corr(retail%, 3m ret): {:+.3f}\n".format(
    df["Large_Holder_Pct"].corr(df["ret3m"]), df["Retail_Pct"].corr(df["ret3m"])))

print("avg trailing 3M return by LARGE-holder % bucket:")
df["lb"] = pd.cut(df["Large_Holder_Pct"], [0, 30, 50, 70, 85, 101],
                  labels=["<30", "30-50", "50-70", "70-85", ">85"])
print(df.groupby("lb", observed=True)["ret3m"].agg(["size", "median", "mean"]).round(3).to_string())

print("\navg trailing 3M return by RETAIL % bucket:")
df["rb"] = pd.cut(df["Retail_Pct"], [0, 20, 35, 50, 101],
                  labels=["<20", "20-35", "35-50", ">50"])
print(df.groupby("rb", observed=True)["ret3m"].agg(["size", "median", "mean"]).round(3).to_string())
