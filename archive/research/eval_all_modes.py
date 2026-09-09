"""
eval_all_modes.py

Real-trade backtest of every scan mode on the FULL price_volume.db history,
using the SAME execution the prelaunch playbook validated:
    entry = next-day OPEN (never signal-day close -- that overstates ~1-2pp)
    exit  = close of the H-th forward bar (time exit)
    stop  = optional -10% intraday disaster stop only (no tight structural stop)

For every (mode x overlay) it prints win%, mean, median, worst, and -- the only
number that proves selection skill -- ALPHA vs the whole-universe benchmark run
through the identical execution and market filter (playbook rule 3).

It also splits the headline config by TAIEX regime (risk_on/off) so a win rate
that only exists in the bull tape is exposed rather than trusted.

Modes: squeeze / breakout / bottom / short_explosion / momentum_leader.
(mode_prelaunch has its own harness in eval_realtrade.py.)

Run:  python eval_all_modes.py
ASCII only. Deps: pandas, numpy.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

from config.settings import PRICE_VOLUME_FILE, TAIEX_FILE

NAMES = "data/stock_names.json"
MODES = ["mode_squeeze", "mode_breakout", "mode_bottom",
         "mode_short_explosion", "mode_momentum_leader"]
DISASTER_STOP = 0.10
MIN_BARS = 70


# ----------------------------------------------------------------- features
def build(df):
    """Return {sid: dict-of-numpy-arrays} plus the aligned date array per sid."""
    store = {}
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < MIN_BARS:
            continue
        c = pd.to_numeric(g["close"], errors="coerce")
        h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce")
        o = pd.to_numeric(g["open"], errors="coerce")
        v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        prev = c.shift(1)
        upv = v.where(c > prev, 0.0)
        dnv = v.where(c < prev, 0.0)
        tot = upv.rolling(10).sum() + dnv.rolling(10).sum()
        rt = (h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()
        cond_a = (rt < 0.08) & ((v / v.rolling(20).mean()) < 0.60)
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()
        turned = (hist > 0) & (hist.shift(1) <= 0)
        f = {
            "date": g["date"].astype(str).str[:10].to_numpy(),
            "o": o.to_numpy(float), "c": c.to_numpy(float),
            "h": h.to_numpy(float), "l": l.to_numpy(float), "v": v.to_numpy(float),
            "ma5": c.rolling(5).mean().to_numpy(float),
            "ma10": c.rolling(10).mean().to_numpy(float),
            "ma20": c.rolling(20).mean().to_numpy(float),
            "ma60": c.rolling(60).mean().to_numpy(float),
            "vma20": v.rolling(20).mean().to_numpy(float),
            "vma5_prior": v.shift(1).rolling(5).mean().to_numpy(float),
            "max20_prev": c.rolling(20).max().shift(1).to_numpy(float),
            "cond_a": cond_a.to_numpy(bool),
            "cond_a_5d": cond_a.rolling(5).max().fillna(0).astype(bool).to_numpy(bool),
            "hist_turn": turned.rolling(3).max().fillna(0).astype(bool).to_numpy(bool),
            "bias10": (upv.rolling(10).sum() / tot.replace(0, np.nan)).to_numpy(float),
            "gain60": (c / c.shift(63) - 1).to_numpy(float),
            "gain20": (c / c.shift(20) - 1).to_numpy(float),
            "amp": ((h - l) / l).to_numpy(float),
            "daygain": (c / c.shift(1) - 1).to_numpy(float),
            "near_high": ((h - c) / c).to_numpy(float),
        }
        store[str(sid)] = f
    return store


def signal(mode, f, i):
    c, ma5, ma10, ma20, ma60 = f["c"][i], f["ma5"][i], f["ma10"][i], f["ma20"][i], f["ma60"][i]
    vma20, vma5p, vnow = f["vma20"][i], f["vma5_prior"][i], f["v"][i]
    if np.isnan(c) or np.isnan(ma60):
        return False
    if mode == "mode_squeeze":
        return c < 150 and vma20 > 500 and c > ma60 and (f["cond_a"][i] or f["cond_a_5d"][i])
    if mode == "mode_breakout":
        return vma20 > 1000 and c > f["max20_prev"][i] and vnow > vma5p * 2
    if mode == "mode_bottom":
        # shipped rule: established uptrend that dipped to ~20MA, still accumulating
        return (c > ma60 and f["gain60"][i] >= 0.10
                and 0.96 * ma20 <= c <= 1.04 * ma20
                and f["bias10"][i] >= 0.45 and vma20 > 300)
    if mode == "mode_short_explosion":
        return (vma20 > 1000 and f["amp"][i] >= 0.05 and f["daygain"][i] >= 0.04
                and f["near_high"][i] <= 0.015 and vnow > vma5p * 2.5
                and c > ma5 > ma10)
    if mode == "mode_momentum_leader":
        return (c > ma60 and ma5 > ma10 > ma20 and f["gain60"][i] >= 0.20
                and f["gain20"][i] >= 0.05 and f["bias10"][i] >= 0.50 and vma20 > 300)
    return False


def trade_return(f, i, hold, stop):
    """Enter at open[i+1], hold `hold` bars, exit at that close. If stop, exit
    early on a -10% intraday breach. Returns pct or None if not enough bars."""
    n = len(f["c"])
    if i + 1 + hold > n:
        return None
    e = f["o"][i + 1]
    if not np.isfinite(e) or e <= 0:
        return None
    if stop:
        sp = e * (1 - DISASTER_STOP)
        for t in range(i + 1, i + 1 + hold):
            if f["l"][t] <= sp:
                # first forward bar can gap through; later bars fill at min(open, stop)
                px = sp if t == i + 1 else min(f["o"][t], sp)
                return px / e - 1
    return f["c"][i + hold] / e - 1


# ----------------------------------------------------------------- driver
def collect(store, market, regime, hold, stop, otc_only):
    """Return DataFrames: per-mode picks and the universe benchmark, columns
    [date, sid, mkt, risk_on, ret]."""
    mode_rows = {m: [] for m in MODES}
    uni_rows = []
    for sid, f in store.items():
        mkt = market.get(sid, "?")
        if otc_only and mkt != "OTC":
            continue
        n = len(f["c"])
        for i in range(64, n - 1 - hold):
            if np.isnan(f["c"][i]) or np.isnan(f["ma60"][i]) or f["c"][i] <= 0:
                continue
            r = trade_return(f, i, hold, stop)
            if r is None:
                continue
            d = f["date"][i]
            ro = regime.get(d, False)
            uni_rows.append((d, sid, mkt, ro, r * 100))
            for m in MODES:
                if signal(m, f, i):
                    mode_rows[m].append((d, sid, mkt, ro, r * 100))
    cols = ["date", "sid", "mkt", "risk_on", "ret"]
    return ({m: pd.DataFrame(rows, columns=cols) for m, rows in mode_rows.items()},
            pd.DataFrame(uni_rows, columns=cols))


def stat(x):
    if len(x) == 0:
        return None
    r = x["ret"].to_numpy(float)
    byday = x.groupby("date")["ret"].mean()
    return {
        "n": len(r), "win": 100 * (r > 0).mean(), "mean": r.mean(),
        "med": float(np.median(r)), "worst": r.min(),
        "daypos": 100 * (byday > 0).mean(), "days": x["date"].nunique(),
    }


def line(label, s, alpha=None):
    if s is None:
        print("%-26s n=0" % label)
        return
    tail = "" if alpha is None else "  ALPHA=%+5.2fpp win%+3.0f" % alpha
    print("%-26s n=%5d %4.1f/day win=%3.0f%% mean=%+5.2f med=%+5.2f worst=%+6.1f daypos=%3.0f%%%s"
          % (label, s["n"], s["n"] / max(s["days"], 1), s["win"], s["mean"],
             s["med"], s["worst"], s["daypos"], tail))


def main():
    con = sqlite3.connect(PRICE_VOLUME_FILE)
    df = pd.read_sql_query(
        "SELECT date, stock_id, open, high, low, close, Volume_Lot FROM data", con)
    con.close()
    df["date"] = df["date"].astype(str).str[:10]

    names = json.load(open(NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    tcon = sqlite3.connect(TAIEX_FILE)
    tx = pd.read_sql("SELECT date, close FROM TAIEX ORDER BY date", tcon)
    tcon.close()
    tx["date"] = tx["date"].astype(str).str[:10]
    tx["ro"] = ((tx["close"] > tx["close"].rolling(20).mean())
                & (tx["close"] > tx["close"].rolling(60).mean()))
    regime = tx.set_index("date")["ro"].to_dict()

    print("building features ...")
    store = build(df)
    dmin = df["date"].min(); dmax = df["date"].max()
    print("universe stocks=%d  window=%s..%s\n" % (len(store), dmin, dmax))

    # ---- sweep: hold x market x stop -----------------------------------
    for otc_only in (False, True):
        scope = "OTC-only" if otc_only else "ALL-markets"
        for hold in (5, 10):
            for stop in (False, True):
                tag = "%s hold%d %s" % (scope, hold, "stop10" if stop else "nostop")
                modes, uni = collect(store, market, regime, hold, stop, otc_only)
                us = stat(uni)
                print("=== %-30s | universe: win=%3.0f%% mean=%+5.2f n=%d ==="
                      % (tag, us["win"], us["mean"], us["n"]))
                for m in MODES:
                    s = stat(modes[m])
                    if s is None or s["n"] < 40:
                        print("%-26s (too few: %d)" % (m, 0 if s is None else s["n"]))
                        continue
                    alpha = (s["mean"] - us["mean"], s["win"] - us["win"])
                    line(m, s, alpha)
                print()

    # ---- regime split of the strongest execution (OTC, hold5, nostop) ---
    print("=== regime split  [OTC-only hold5 nostop] ===")
    modes, uni = collect(store, market, regime, 5, False, True)
    for m in MODES:
        d = modes[m]
        if len(d) < 40:
            continue
        on = stat(d[d["risk_on"]]); off = stat(d[~d["risk_on"]])
        print("-- %s" % m)
        line("   risk_on", on); line("   risk_off", off)


if __name__ == "__main__":
    main()
