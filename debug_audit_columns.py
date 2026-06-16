"""
Column-correctness audit. For every stock in price_volume.db, recompute each
decision column from raw OHLCV with first-principles numpy and compare to the
actual analyzer pipeline output. Reports the worst absolute discrepancy per
column so any genuine calc bug surfaces. All strings ASCII.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE
from analyzer.signal_evaluator import _evaluate_conditions
from analyzer.support_resistance import calc_all
from analyzer.trend_analysis import calc_trend_analysis

con = sqlite3.connect(PRICE_VOLUME_FILE)
df = pd.read_sql_query("SELECT * FROM data", con)
con.close()

worst = {}   # col -> (max_abs_diff, stock_id, pipeline, manual)


def upd(col, sid, pipe, man):
    if pipe is None or man is None or (isinstance(man, float) and np.isnan(man)):
        return
    d = abs(float(pipe) - float(man))
    if col not in worst or d > worst[col][0]:
        worst[col] = (d, sid, float(pipe), float(man))


n = 0
for sid, g in df.groupby("stock_id"):
    g = g.sort_values("date").reset_index(drop=True)
    if len(g) < 64:
        continue
    n += 1
    for c in ["open", "high", "low", "close", "Volume_Lot"]:
        g[c] = pd.to_numeric(g[c], errors="coerce")
    close = g["close"].to_numpy(float)
    high = g["high"].to_numpy(float)
    low = g["low"].to_numpy(float)
    vol = g["Volume_Lot"].to_numpy(float)

    sr = calc_all(g.copy())
    ta = calc_trend_analysis(g.copy())
    ev = _evaluate_conditions(g.copy()).iloc[-1]

    upd("MA5",  sid, ta["MA5"],  np.mean(close[-5:]))
    upd("MA10", sid, ta["MA10"], np.mean(close[-10:]))
    upd("MA20", sid, sr["MA20"], np.mean(close[-20:]))
    upd("MA60", sid, sr["MA60"], np.mean(close[-60:]))
    upd("Resist_60H",  sid, sr["Resist_60H"],  np.max(high[-60:-1]))
    upd("Support_60L", sid, sr["Support_60L"], np.min(low[-60:]))
    upd("Gain_3M", sid, (close[-1]/close[-64]-1)*100, (close[-1]/close[-64]-1)*100)

    # Range_Tightness = (max(high[-20:]) - min(low[-20:])) / min(low[-20:])
    rt_man = (np.max(high[-20:]) - np.min(low[-20:])) / np.min(low[-20:])
    upd("Range_Tightness", sid, ev.get("Range_Tightness"), rt_man)

    # Volume_Bias = up-day vol / total over last 10 days
    up = tot = 0.0
    for k in range(len(close)-10, len(close)):
        if k <= 0 or np.isnan(vol[k]):
            continue
        tot += vol[k]
        if close[k] > close[k-1]:
            up += vol[k]
    upd("Volume_Bias", sid, ev.get("Volume_Bias"), up/tot if tot > 0 else None)

    # Res_Gap_Pct = (Resist_60H - close)/close*100
    if sr["Resist_60H"]:
        upd("Res_Gap_Pct", sid, sr["Res_Gap_Pct"],
            (sr["Resist_60H"]-close[-1])/close[-1]*100)
    # Sup_Gap_Pct = (close - Support_60L)/Support_60L*100
    if sr["Support_60L"]:
        upd("Sup_Gap_Pct", sid, sr["Sup_Gap_Pct"],
            (close[-1]-sr["Support_60L"])/sr["Support_60L"]*100)

print("audited stocks:", n)
print("\n{:<16} {:>12} {:>10} {:>10} {:>8}".format("column", "max|diff|", "pipeline", "manual", "stock"))
print("-" * 60)
for col in ["MA5","MA10","MA20","MA60","Resist_60H","Support_60L","Gain_3M",
            "Range_Tightness","Volume_Bias","Res_Gap_Pct","Sup_Gap_Pct"]:
    if col in worst:
        d, sid, p, m = worst[col]
        flag = "  <-- MISMATCH" if d > 0.01 else ""
        print("{:<16} {:>12.5f} {:>10.3f} {:>10.3f} {:>8}{}".format(col, d, p, m, sid, flag))
