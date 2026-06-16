"""
Validate all 5 scan modes on price_volume.db.

For each mode we replicate its EXACT scan_mode.py condition on every historical
bar (no lookahead), then simulate the real trade plan: enter at the signal-day
close, stop = our clamped structural stop, hold up to FWD days (exit on stop or
at the horizon). We report, vs the unconditional BASE over all bars:
  - up10 : P(price reaches +10% within FWD days)   -> signal quality / direction
  - win  : P(the stop-based trade closes positive)
  - avg  : mean trade return (expectancy, after stops)
A mode is "directionally correct + profitable" when win>50%, avg>0, and
up10-lift > 1. All strings ASCII.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE

FWD = 20
MIN_STOP, MAX_STOP = 0.06, 0.13


def features(g):
    c = pd.to_numeric(g["close"], errors="coerce")
    h = pd.to_numeric(g["high"], errors="coerce")
    l = pd.to_numeric(g["low"], errors="coerce")
    v = pd.to_numeric(g["Volume_Lot"], errors="coerce")

    f = {}
    f["c"], f["h"], f["l"], f["v"] = c.to_numpy(float), h.to_numpy(float), l.to_numpy(float), v.to_numpy(float)
    f["ma5"]  = c.rolling(5).mean().to_numpy(float)
    f["ma10"] = c.rolling(10).mean().to_numpy(float)
    f["ma20"] = c.rolling(20).mean().to_numpy(float)
    f["ma60"] = c.rolling(60).mean().to_numpy(float)
    f["vma20"] = v.rolling(20).mean().to_numpy(float)
    f["vma5_prior"] = v.shift(1).rolling(5).mean().to_numpy(float)
    f["dryup"] = (v / v.rolling(20).mean()).to_numpy(float)
    rt = (h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()
    f["rt"] = rt.to_numpy(float)
    cond_a = (rt < 0.08) & ((v / v.rolling(20).mean()) < 0.60)
    f["cond_a"] = cond_a.to_numpy(bool)
    f["cond_a_5d"] = cond_a.rolling(5).max().fillna(0).astype(bool).to_numpy(bool)
    f["max20_prev"] = c.rolling(20).max().shift(1).to_numpy(float)

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()
    turned = (hist > 0) & (hist.shift(1) <= 0)
    f["hist_turn"] = turned.rolling(3).max().fillna(0).astype(bool).to_numpy(bool)

    prev = c.shift(1)
    upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
    tot = upv.rolling(10).sum() + dnv.rolling(10).sum()
    f["bias10"] = (upv.rolling(10).sum() / tot.replace(0, np.nan)).to_numpy(float)

    f["gain60"] = (c / c.shift(63) - 1).to_numpy(float)
    f["gain20"] = (c / c.shift(20) - 1).to_numpy(float)
    f["amp"] = ((h - l) / l).to_numpy(float)
    f["daygain"] = (c / c.shift(1) - 1).to_numpy(float)
    f["near_high"] = ((h - c) / c).to_numpy(float)
    return f


def mode_mask(mode, f, i):
    c, ma5, ma10, ma20, ma60 = f["c"][i], f["ma5"][i], f["ma10"][i], f["ma20"][i], f["ma60"][i]
    vma20, vma5p, vnow = f["vma20"][i], f["vma5_prior"][i], f["v"][i]
    if mode == "mode_squeeze":
        return (c < 150 and vma20 > 500 and c > ma60 and (f["cond_a"][i] or f["cond_a_5d"][i]))
    if mode == "mode_breakout":
        return (vma20 > 1000 and c > f["max20_prev"][i] and vnow > vma5p * 2)
    if mode == "mode_bottom":
        tech = (vnow > vma5p * 1.5) and (c > ma10)
        return (c < ma60 and f["hist_turn"][i] and tech)   # cond_b disabled -> tech only
    if mode == "mode_short_explosion":
        return (vma20 > 1000 and f["amp"][i] >= 0.05 and f["daygain"][i] >= 0.04
                and f["near_high"][i] <= 0.015 and vnow > vma5p * 2.5
                and c > ma5 > ma10)
    if mode == "mode_momentum_leader":
        return (c > ma60 and ma5 > ma10 > ma20 and f["gain60"][i] >= 0.20
                and f["gain20"][i] >= 0.05 and f["bias10"][i] >= 0.50 and vma20 > 300)
    return False


def simulate(f, i):
    c, l = f["c"], f["l"]
    entry = c[i]
    struct = max(min(l[i-2:i+1]), f["ma10"][i])
    stop = min(max(struct, entry * (1 - MAX_STOP)), entry * (1 - MIN_STOP))
    end = min(i + FWD, len(c) - 1)
    up10 = (np.nanmax(c[i+1:i+1+FWD]) / entry - 1) >= 0.10
    for t in range(i + 1, end + 1):
        if l[t] <= stop:
            return stop / entry - 1, up10
    return c[end] / entry - 1, up10


def main():
    con = sqlite3.connect(PRICE_VOLUME_FILE)
    df = pd.read_sql_query("SELECT * FROM data", con)
    con.close()

    modes = ["mode_squeeze", "mode_breakout", "mode_bottom",
             "mode_short_explosion", "mode_momentum_leader"]
    acc = {m: {"ret": [], "up10": 0, "n": 0} for m in modes}
    base = {"ret": [], "up10": 0, "n": 0}

    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date").reset_index(drop=True)
        n = len(g)
        if n < 64 + FWD + 2:
            continue
        f = features(g)
        valid = lambda i: not (np.isnan(f["ma60"][i]) or np.isnan(f["gain60"][i]) or f["c"][i] <= 0)
        for i in range(64, n - FWD):
            if not valid(i):
                continue
            r, up10 = simulate(f, i)
            base["ret"].append(r); base["up10"] += up10; base["n"] += 1
            for m in modes:
                if mode_mask(m, f, i):
                    acc[m]["ret"].append(r); acc[m]["up10"] += up10; acc[m]["n"] += 1

    b_ret = np.array(base["ret"]); b_up10 = base["up10"] / base["n"]
    print("BASE (all bars): n={}  win={:.1%}  avg={:+.2%}  up10={:.1%}\n".format(
        base["n"], (b_ret > 0).mean(), b_ret.mean(), b_up10))
    print("{:<22} {:>6} {:>6} {:>7} {:>8} {:>7} {:>6}".format(
        "mode", "n", "%bars", "win", "avg_ret", "up10", "lift"))
    print("-" * 62)
    for m in modes:
        a = acc[m]
        if a["n"] < 30:
            print("{:<22} {:>6}  (too few signals)".format(m, a["n"])); continue
        r = np.array(a["ret"]); up10 = a["up10"] / a["n"]
        print("{:<22} {:>6} {:>5.1f}% {:>6.1%} {:>+8.2%} {:>6.1%} {:>6.2f}".format(
            m, a["n"], 100*a["n"]/base["n"], (r > 0).mean(), r.mean(), up10, up10/b_up10))


if __name__ == "__main__":
    main()
