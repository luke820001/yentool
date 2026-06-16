"""
Backtest entry/stop combinations on price_volume.db to fix the trade columns.

Signal = pre-launch momentum setup (mode 5). At each signal day we simulate the
forward price path: enter, then exit on stop-hit or after HOLD days. We grid the
stop width and two entry styles, and report expectancy + premature-stop rate
(stopped out but would have been GREEN at HOLD) -- the "bought then immediately
sold" complaint. All strings ASCII.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE

HOLD = 20
STOPS = [0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15]


def load():
    con = sqlite3.connect(PRICE_VOLUME_FILE)
    df = pd.read_sql_query("SELECT * FROM data", con)
    con.close()
    return df


def signals_for(g):
    """Return indices that pass the mode-5 pre-launch screen."""
    n = len(g)
    c = pd.to_numeric(g["close"], errors="coerce").to_numpy(float)
    h = pd.to_numeric(g["high"], errors="coerce").to_numpy(float)
    l = pd.to_numeric(g["low"], errors="coerce").to_numpy(float)
    v = pd.to_numeric(g["Volume_Lot"], errors="coerce").to_numpy(float)
    out = []
    for i in range(64, n - HOLD):
        if c[i] <= 0 or c[i-63] <= 0 or c[i-20] <= 0:
            continue
        ma5, ma10, ma20, ma60 = (np.mean(c[i-4:i+1]), np.mean(c[i-9:i+1]),
                                 np.mean(c[i-19:i+1]), np.mean(c[i-59:i+1]))
        g60 = c[i]/c[i-63]-1
        g20 = c[i]/c[i-20]-1
        up = tot = 0.0
        for k in range(i-9, i+1):
            if np.isnan(v[k]):
                continue
            tot += v[k]
            if c[k] > c[k-1]:
                up += v[k]
        bias = up/tot if tot > 0 else 0
        if (c[i] > ma60 and ma5 > ma10 > ma20 and g60 >= 0.20 and g20 >= 0.05
                and bias >= 0.50 and np.mean(v[i-19:i+1]) > 300):
            out.append(i)
    return out, c, h, l


def simulate(c, h, l, i, entry, stop_px):
    """Walk forward from signal i. Returns (ret, stopped, green_at_hold)."""
    end = min(i + HOLD, len(c) - 1)
    green_at_hold = c[end] / entry - 1
    for t in range(i + 1, end + 1):
        if l[t] <= stop_px:
            return stop_px / entry - 1, True, green_at_hold > 0
    return c[end] / entry - 1, False, green_at_hold > 0


def main():
    df = load()
    sig = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < 64 + HOLD + 1:
            continue
        idx, c, h, l = signals_for(g)
        for i in idx:
            sig.append((c, h, l, i))
    print("signals:", len(sig), " HOLD:", HOLD, "\n")

    # Entry A: market at signal close
    print("=== Entry = signal close (buy strength) ===")
    print("{:>6} {:>9} {:>9} {:>9} {:>11}".format("stop%", "avg_ret", "win%", "stopped%", "premature%"))
    for s in STOPS:
        rets = []; stopped = 0; premature = 0
        for c, h, l, i in sig:
            entry = c[i]
            r, st, green = simulate(c, h, l, i, entry, entry * (1 - s))
            rets.append(r); stopped += st; premature += (st and green)
        rets = np.array(rets)
        print("{:>6.0%} {:>9.2%} {:>8.1f}% {:>8.1f}% {:>10.1f}%".format(
            s, rets.mean(), 100*(rets > 0).mean(), 100*stopped/len(sig), 100*premature/len(sig)))

    # Entry B: limit at 3% pullback, fill within 5 days else skip
    print("\n=== Entry = 3% pullback limit (fill within 5d, else skip) ===")
    print("{:>6} {:>9} {:>9} {:>9} {:>9}".format("stop%", "avg_ret", "win%", "stopped%", "fills"))
    for s in STOPS:
        rets = []; stopped = 0; fills = 0
        for c, h, l, i in sig:
            limit = c[i] * 0.97
            fill_t = None
            for t in range(i + 1, min(i + 6, len(c))):
                if l[t] <= limit:
                    fill_t = t; break
            if fill_t is None:
                continue
            fills += 1
            entry = limit
            end = min(fill_t + HOLD, len(c) - 1)
            stop_px = entry * (1 - s)
            hit = False
            for t in range(fill_t + 1, end + 1):
                if l[t] <= stop_px:
                    rets.append(stop_px/entry - 1); stopped += 1; hit = True; break
            if not hit:
                rets.append(c[end]/entry - 1)
        rets = np.array(rets)
        print("{:>6.0%} {:>9.2%} {:>8.1f}% {:>8.1f}% {:>9}".format(
            s, rets.mean(), 100*(rets > 0).mean(), 100*stopped/max(len(rets),1), fills))


if __name__ == "__main__":
    main()
