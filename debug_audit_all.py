"""
Comprehensive column-calculation audit. For every stock, run the real analyzer
pipeline and compare EVERY output column against an independent first-principles
numpy recompute. Numeric cols -> worst absolute diff; boolean cols -> mismatch
count. Anything beyond rounding tolerance is a real calc bug. ASCII only.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE
from analyzer.signal_evaluator import _evaluate_conditions
from analyzer.support_resistance import calc_all
from analyzer.trend_analysis import calc_trend_analysis
from scanner.chip_verifier import _get_volume_stats


def _round_level(c):
    step = 1.0 if c < 10 else 5.0 if c < 50 else 10.0 if c < 100 else 50.0 if c < 500 else 100.0
    return round(round(c / step) * step, 2)

con = sqlite3.connect(PRICE_VOLUME_FILE)
allrows = pd.read_sql_query("SELECT * FROM data", con)
con.close()

num_worst = {}   # col -> (max|diff|, sid, pipe, man)
bool_bad = {}    # col -> count mismatches
bool_tot = {}


def un(col, sid, pipe, man):
    if pipe is None or man is None:
        return
    try:
        if isinstance(man, float) and np.isnan(man):
            return
        d = abs(float(pipe) - float(man))
    except Exception:
        return
    if col not in num_worst or d > num_worst[col][0]:
        num_worst[col] = (d, sid, float(pipe), float(man))


def ub(col, pipe, man):
    bool_tot[col] = bool_tot.get(col, 0) + 1
    if bool(pipe) != bool(man):
        bool_bad[col] = bool_bad.get(col, 0) + 1


n = 0
for sid, g in allrows.groupby("stock_id"):
    g = g.sort_values("date").reset_index(drop=True)
    if len(g) < 70:
        continue
    n += 1
    for c in ["open", "high", "low", "close", "Volume_Lot", "Max_Price_20", "MA5_Volume"]:
        g[c] = pd.to_numeric(g[c], errors="coerce")
    # mirror the pipeline fix: recompute rolling-derived cols from raw close/volume
    g["Max_Price_20"] = g["close"].rolling(20, min_periods=1).max()
    g["MA5_Volume"]   = g["Volume_Lot"].rolling(5, min_periods=1).mean()
    close = g["close"].to_numpy(float); high = g["high"].to_numpy(float)
    low = g["low"].to_numpy(float); vol = g["Volume_Lot"].to_numpy(float)

    sr = calc_all(g.copy()); ta = calc_trend_analysis(g.copy())
    ev = _evaluate_conditions(g.copy()); last = ev.iloc[-1]

    # --- moving averages ---
    un("MA5",  sid, ta["MA5"],  np.mean(close[-5:]))
    un("MA10", sid, ta["MA10"], np.mean(close[-10:]))
    un("MA20", sid, sr["MA20"], np.mean(close[-20:]))
    un("MA60", sid, sr["MA60"], np.mean(close[-60:]))
    # --- support / resistance ---
    un("Resist_60H",  sid, sr["Resist_60H"],  np.max(high[-60:-1]))
    un("Support_60L", sid, sr["Support_60L"], np.min(low[-60:]))
    if sr["Support_60L"]:
        un("Sup_Gap_Pct", sid, sr["Sup_Gap_Pct"], (close[-1]-sr["Support_60L"])/sr["Support_60L"]*100)
    if sr["Resist_60H"]:
        un("Res_Gap_Pct", sid, sr["Res_Gap_Pct"], (sr["Resist_60H"]-close[-1])/close[-1]*100)
    # --- tightness / dryup / bias / explosion ---
    rt = (np.max(high[-20:])-np.min(low[-20:]))/np.min(low[-20:])
    un("Range_Tightness", sid, last.get("Range_Tightness"), rt)
    un("Volume_Dryup", sid, last.get("Volume_Dryup_Ratio"), vol[-1]/np.mean(vol[-20:]))
    up = dn = 0.0
    for k in range(len(close)-10, len(close)):
        if k <= 0 or np.isnan(vol[k]):
            continue
        if close[k] > close[k-1]: up += vol[k]
        elif close[k] < close[k-1]: dn += vol[k]
    bias = up/(up+dn) if (up+dn) > 0 else np.nan
    un("Volume_Bias", sid, last.get("Volume_Bias"), bias)
    ts = max(0.0, (0.20-min(rt, 0.20))/0.20*35)
    ds = (1.0-min(vol[-1]/np.mean(vol[-20:]), 1.0))*35
    bs = (min(max(bias, 0.5), 1.0)-0.5)/0.5*30 if not np.isnan(bias) else np.nan
    if not np.isnan(bs):
        un("Explosion_Score", sid, last.get("Explosion_Score"), ts+ds+bs)
    # --- 52w / round ---
    h52 = np.max(close[-252:]) if len(close) >= 63 else np.nan
    if h52 and not np.isnan(h52):
        un("Dist_52W_High_Pct", sid, ta["Dist_52W_High_Pct"], (h52-close[-1])/h52*100)
    un("Round_Level", sid, sr["Round_Level"], None)  # checked structurally below
    # --- volume stats: compare the real _get_volume_stats vs manual ---
    vs = _get_volume_stats(g.copy())
    un("Vol_MA20", sid, vs["Vol_MA20"], round(np.mean(vol[-20:]), 0))
    un("Vol_MA5", sid, vs["Vol_MA5"], round(np.mean(vol[-6:-1]), 0))
    un("Max_Price_20_Prev", sid, vs["Max_Price_20_Prev"], round(np.max(close[-21:-1]), 2))
    un("Min_Price_3", sid, vs["Min_Price_3"], round(np.min(low[-3:]), 2))
    un("High_Today", sid, vs["High_Today"], round(high[-1], 2))
    un("Low_Today", sid, vs["Low_Today"], round(low[-1], 2))
    un("Close_Prev", sid, vs["Close_Prev"], round(close[-2], 2))
    # --- round level ---
    un("Round_Level", sid, sr["Round_Level"], _round_level(close[-1]))
    # --- MACD (recompute) ---
    s = pd.Series(close)
    e12 = s.ewm(span=12, adjust=False).mean(); e26 = s.ewm(span=26, adjust=False).mean()
    macd = e12 - e26; sig = macd.ewm(span=9, adjust=False).mean(); hist = macd - sig
    cross = any(macd.iloc[i] > sig.iloc[i] and macd.iloc[i-1] <= sig.iloc[i-1] for i in range(-3, 0))
    hturn = any(hist.iloc[i] > 0 and hist.iloc[i-1] <= 0 for i in range(-3, 0))
    if len(close) >= 35:
        ub("MACD_Cross", ta["MACD_Cross"], cross)
        ub("MACD_Hist_Turn", ta["MACD_Hist_Turn"], hturn)
    # --- MA squeeze ---
    vals = [np.mean(close[-5:]), np.mean(close[-10:]), np.mean(close[-20:])]
    spread = (max(vals) - min(vals)) / min(vals)
    ub("MA_Squeeze", ta["MA_Squeeze"], (spread < 0.03 and close[-1] > min(vals)))
    # --- Donchian (window 40, exclude today) ---
    if len(close) >= 41:
        ph = pd.Series(high[:-1]).rolling(40).max().iloc[-1]
        pl = pd.Series(low[:-1]).rolling(40).min().iloc[-1]
        if pl > 0:
            don = ((ph - pl) / pl < 0.15) and (close[-1] >= ph * 0.97)
            ub("Donchian_Break", ta["Donchian_Break"], don)
    # --- booleans ---
    m5, m10, m20, m60 = np.mean(close[-5:]), np.mean(close[-10:]), np.mean(close[-20:]), np.mean(close[-60:])
    ub("MA_Bull_Align", ta["MA_Bull_Align"], (m5 > m10 > m20 > m60))
    ub("Near_52W_High", ta["Near_52W_High"], (h52 and (h52-close[-1])/h52*100 <= 15.0))
    ub("Cond_A", last.get("Cond_A"), (rt < 0.08 and vol[-1]/np.mean(vol[-20:]) < 0.60))
    ub("Cond_C", last.get("Cond_C"), (not np.isnan(bias) and bias >= 0.60))

print("audited stocks:", n)
print("\n=== NUMERIC columns (worst abs diff; >0.01 = BUG) ===")
print("{:<20}{:>12}{:>11}{:>11}{:>8}".format("column", "max|diff|", "pipeline", "manual", "stock"))
for col in sorted(num_worst):
    d, sid, p, m = num_worst[col]
    flag = "  <-- CHECK" if d > 0.01 else ""
    print("{:<20}{:>12.5f}{:>11.3f}{:>11.3f}{:>8}{}".format(col, d, p, m, sid, flag))

print("\n=== BOOLEAN columns (mismatch / total; >0 = CHECK) ===")
for col in sorted(bool_tot):
    bad = bool_bad.get(col, 0)
    flag = "  <-- CHECK" if bad > 0 else ""
    print("  {:<18} {}/{}{}".format(col, bad, bool_tot[col], flag))
