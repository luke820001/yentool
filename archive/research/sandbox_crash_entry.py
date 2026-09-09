"""
sandbox_crash_entry.py

User hypothesis: "on a panic crash (trade war, geopolitical shock), buy
IMMEDIATELY and mindlessly; the 10-bar hold is wrong for this". This is a
SEPARATE strategy (panic mean-reversion on the index / mega-cap tech) from the
scanner, so it gets its own line of verification.

Definitions:
  triggers  TAIEX day return <= -3pct | 5d <= -7pct | 20d <= -10pct
  episode   first trigger with no trigger in the prior 10 bars (one entry per
            crash, otherwise one bear market counts 30x)
  entries   index: next close after the trigger (close-to-close proxy);
            basket: next open, 9 AI-era mega caps
  exits     hold 5/10/20/40/60 bars, no stop ("mindless") + a -15pct-stop
            variant on the basket to show what a stop does to crash entries
  timing    enter t+1 vs t+3 vs after the first TAIEX up-day, hold 20

Run:  python sandbox_crash_entry.py
ASCII only.
"""
import sqlite3

import numpy as np
import pandas as pd

TAIEX_CSV = "data/research_taiex.csv"
RESEARCH_DB = "data/research_prices.db"
BASKET = ["2330", "2454", "2308", "2317", "2382", "3231", "6669", "3661", "3443"]
HOLDS = (5, 10, 20, 40, 60)
COOLDOWN = 10


def load_taiex():
    tx = pd.read_csv(TAIEX_CSV)
    tx["date"] = tx["date"].astype(str).str[:10]
    tx = tx.sort_values("date").reset_index(drop=True)
    c = tx["close"]
    tx["r1"] = c / c.shift(1) - 1
    tx["r5"] = c / c.shift(5) - 1
    tx["r20"] = c / c.shift(20) - 1
    # drawdown from the trailing 20-day high: a V-crash (one huge down day off a
    # fresh high) shows up here even when the 5d/20d SUM is diluted by prior
    # gains. This is the fix for the "single-day black swan slips between shallow
    # and deep triggers" blind spot.
    tx["dd20"] = c / c.rolling(20).max() - 1
    return tx


def episodes(tx, mask):
    """Indices of trigger days that start a new episode (cooldown bars clean)."""
    idx = list(np.flatnonzero(mask.to_numpy()))
    out, last = [], -10**9
    for i in idx:
        if i - last > COOLDOWN:
            out.append(i)
        last = i
    return out


def idx_stats(tx, eps, holds=HOLDS):
    """Index proxy: enter next close, exit N bars later (close-to-close)."""
    c = tx["close"].to_numpy()
    rows = []
    for h in holds:
        rets = [(c[i + 1 + h] / c[i + 1] - 1) * 100
                for i in eps if i + 1 + h < len(c)]
        if rets:
            r = np.array(rets)
            rows.append((h, len(r), 100 * (r > 0).mean(), r.mean(), r.min()))
    return rows


def load_basket():
    con = sqlite3.connect(RESEARCH_DB)
    q = ("SELECT date, stock_id, open, close, low FROM data WHERE stock_id IN (%s)"
         % ",".join("'%s'" % s for s in BASKET))
    df = pd.read_sql(q, con)
    con.close()
    df["date"] = df["date"].astype(str).str[:10]
    for c in ("open", "close", "low"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return {sid: g.sort_values("date").reset_index(drop=True)
            for sid, g in df.groupby("stock_id")}


def basket_stats(bars, dates, holds=HOLDS, stop=None, entry_lag=1):
    """Buy each basket name at the open `entry_lag` bars after the trigger
    date, exit at the hold-th close (optional intraday stop below entry)."""
    rows = []
    for h in holds:
        rets = []
        for d in dates:
            for sid, g in bars.items():
                pos = g.index[g["date"] == d]
                if len(pos) == 0:
                    # trigger date may be a non-trading date for this stock
                    after = g.index[g["date"] > d]
                    if len(after) == 0:
                        continue
                    i = after[0] + entry_lag - 1
                else:
                    i = pos[0] + entry_lag
                if i + h - 1 >= len(g):
                    continue
                e = float(g.loc[i, "open"])
                if not np.isfinite(e) or e <= 0:
                    continue
                ret = None
                if stop is not None:
                    for j in range(i, i + h):
                        if float(g.loc[j, "low"]) <= e * (1 - stop):
                            px = min(float(g.loc[j, "open"]), e * (1 - stop)) \
                                if j > i else e * (1 - stop)
                            ret = px / e - 1
                            break
                if ret is None:
                    ret = float(g.loc[i + h - 1, "close"]) / e - 1
                rets.append(ret * 100)
        if rets:
            r = np.array(rets)
            rows.append((h, len(r), 100 * (r > 0).mean(), r.mean(), r.min()))
    return rows


def show(title, rows):
    print("  %s" % title)
    for h, n, win, mean, worst in rows:
        print("    hold %2d bars: n=%4d win=%5.1f%% mean=%+7.2f worst=%+7.1f"
              % (h, n, win, mean, worst))


def first_up_day(tx, i):
    """Bar index of the first TAIEX up-day strictly after trigger i."""
    r1 = tx["r1"].to_numpy()
    for j in range(i + 1, min(i + 15, len(r1))):
        if r1[j] > 0:
            return j
    return None


def main():
    tx = load_taiex()
    bars = load_basket()
    dstr = tx["date"]

    trigs = {
        "day<=-3%":  tx["r1"] <= -0.03,
        "day<=-5%":  tx["r1"] <= -0.05,     # deep single-day (V-crash catcher)
        "day<=-6%":  tx["r1"] <= -0.06,
        "5d<=-7%":   tx["r5"] <= -0.07,
        "20d<=-10%": tx["r20"] <= -0.10,
        # combined V-crash rule: a big single day OR a real drawdown from the
        # recent high -- fires on 07-17-type events the 5d window misses.
        "day<=-5% OR dd20<=-8%": (tx["r1"] <= -0.05) | (tx["dd20"] <= -0.08),
    }

    for name, mask in trigs.items():
        eps = episodes(tx, mask)
        dates = [dstr.iloc[i] for i in eps]
        print("\n=== trigger %s : %d episodes ===" % (name, len(eps)))
        print("  dates: %s" % ", ".join(dates))
        show("TAIEX index (next close entry, no stop):", idx_stats(tx, eps))
        show("mega-cap basket (next open, no stop):", basket_stats(bars, dates))
        show("mega-cap basket WITH -15% stop:",
             basket_stats(bars, dates, holds=(20, 40), stop=0.15))
        ai = [d for d in dates if d >= "2024-07-01"]
        if ai:
            print("  AI-era episodes only (%d): %s" % (len(ai), ", ".join(ai)))
            show("  basket, AI-era only:", basket_stats(bars, ai, holds=(20, 40, 60)))

    # timing: immediate vs wait-3 vs first up-day (day<=-3% episodes, hold 20)
    print("\n=== timing on day<=-3%% episodes, basket hold 20 ===")
    eps = episodes(tx, trigs["day<=-3%"])
    dates = [dstr.iloc[i] for i in eps]
    show("enter t+1 (immediate):", basket_stats(bars, dates, holds=(20,)))
    show("enter t+3 (wait 2 more):", basket_stats(bars, dates, holds=(20,), entry_lag=3))
    up_dates = []
    for i in eps:
        j = first_up_day(tx, i)
        if j is not None:
            up_dates.append(dstr.iloc[j])
    show("enter after 1st TAIEX up-day:", basket_stats(bars, up_dates, holds=(20,)))


if __name__ == "__main__":
    main()
