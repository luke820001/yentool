"""
sandbox_bigholder.py  (SANDBOX_PLAN.md hypothesis H5 -- big-holder habits)

Studies >=400-lot holder behaviour on the TDCC weekly panel collected by
ingestion/tdcc_history.py (levels: date, stock_id, level, holders, shares,
pct). Joins weekly changes in the big-holder tiers to FORWARD price
returns from price_volume.db and reports candidate signals.

Honesty constraints (plan section 5-2): the panel is ~50 weeks of a single
regime, so there is no train/valid time split -- everything printed here
is CANDIDATE grade and must survive forward weekly validation before any
adoption. Forward returns are demeaned within each week (market-neutral
view) so a trending tape cannot masquerade as signal.

Signals:
  S1 accumulation streak: big400 pct up >=2 consecutive weeks
  S2 strong 1w jump:      big400 delta >= +0.5pp
  S3 concentration:       big400 pct up while big-holder COUNT flat/down
                          (fewer, bigger hands -- the classic quiet whale)
  S4 silent accumulation: S1 AND price 4w range < 10pct (coiled + accumulated)
  S5 distribution:        big400 pct down >=2 weeks while price near 20d high
  S6 retail handoff:      retail pct down AND big400 pct up same week

Run:  python sandbox_bigholder.py            (all stocks in the panel)
      python sandbox_bigholder.py OTC        (OTC only)
ASCII only.
"""
import json
import sqlite3
import sys

import numpy as np
import pandas as pd

HIST_DB = "data/tdcc_history.db"
PV_DB = "data/price_volume.db"
NAMES = "data/stock_names.json"

BIG = (12, 13, 14, 15)      # >= 400,001 shares (>= ~400 lots)
WHALE = (15,)               # > 1,000,000 shares
RETAIL = (1, 2, 3, 4, 5, 6, 7, 8)


def load_panel():
    con = sqlite3.connect(HIST_DB)
    lv = pd.read_sql("SELECT date, stock_id, level, holders, pct FROM levels",
                     con)
    con.close()

    def tier(levels, name):
        m = lv[lv["level"].isin(levels)]
        g = m.groupby(["date", "stock_id"]).agg(
            pct=("pct", "sum"), holders=("holders", "sum")).reset_index()
        return g.rename(columns={"pct": name + "_pct",
                                 "holders": name + "_n"})

    p = tier(BIG, "big")
    p = p.merge(tier(WHALE, "whale"), on=["date", "stock_id"], how="left")
    p = p.merge(tier(RETAIL, "retail"), on=["date", "stock_id"], how="left")
    p = p.sort_values(["stock_id", "date"]).reset_index(drop=True)

    g = p.groupby("stock_id")
    p["big_d"] = g["big_pct"].diff()
    p["whale_d"] = g["whale_pct"].diff()
    p["retail_d"] = g["retail_pct"].diff()
    p["bign_d"] = g["big_n"].diff()
    up = (p["big_d"] > 0).astype(int)
    p["up_streak"] = up.groupby(p["stock_id"]).cumsum() - \
        up.groupby(p["stock_id"]).cumsum().where(up == 0).ffill().fillna(0)
    dn = (p["big_d"] < 0).astype(int)
    p["dn_streak"] = dn.groupby(p["stock_id"]).cumsum() - \
        dn.groupby(p["stock_id"]).cumsum().where(dn == 0).ffill().fillna(0)
    return p


def weekly_prices(week_dates):
    con = sqlite3.connect(PV_DB)
    px = pd.read_sql("SELECT date, stock_id, close, high, low FROM data", con)
    con.close()
    for c in ("close", "high", "low"):
        px[c] = pd.to_numeric(px[c], errors="coerce")
    px["date"] = px["date"].astype(str).str[:10]
    px = px.sort_values("date")

    # as-of close for each TDCC week date (<= 4 calendar days back)
    all_days = sorted(px["date"].unique())
    asof = {}
    for w in week_dates:
        cand = [d for d in all_days if d <= w][-1:]
        asof[w] = cand[0] if cand and (pd.Timestamp(w) -
                                       pd.Timestamp(cand[0])).days <= 4 else None
    frames = []
    close_map = px.set_index(["date", "stock_id"])["close"]
    weeks = [w for w in week_dates if asof[w]]
    for i, w in enumerate(weeks):
        day = asof[w]
        sub = px[px["date"] == day][["stock_id", "close"]].copy()
        sub["week"] = w
        # forward closes at +1 and +4 TDCC weeks
        for k, col in ((1, "fwd1"), (4, "fwd4")):
            if i + k < len(weeks):
                nx = asof[weeks[i + k]]
                sub[col] = sub["stock_id"].map(
                    close_map.xs(nx, level="date", drop_level=True))
            else:
                sub[col] = np.nan
        # trailing 4w range pct (coil detector)
        if i >= 4:
            lo4 = asof[weeks[i - 4]]
            wnd = px[(px["date"] > lo4) & (px["date"] <= day)]
            rng = wnd.groupby("stock_id").agg(hi=("high", "max"),
                                              lo=("low", "min"))
            sub = sub.merge(((rng["hi"] - rng["lo"]) / rng["lo"])
                            .rename("range4w").reset_index(),
                            on="stock_id", how="left")
        else:
            sub["range4w"] = np.nan
        # distance to 20d high
        wnd20 = px[(px["date"] <= day)].groupby("stock_id")["high"] \
            .apply(lambda s: s.tail(20).max())
        sub["near_hi"] = sub["close"] / sub["stock_id"].map(wnd20)
        frames.append(sub)
    return pd.concat(frames, ignore_index=True)


def show(label, m, col="alpha4"):
    m = m.dropna(subset=[col])
    if len(m) < 30:
        print("  %-44s n=%4d  (too small)" % (label, len(m)))
        return
    v = m[col].to_numpy()
    print("  %-44s n=%4d win=%4.1f%% mean=%+5.2f med=%+5.2f"
          % (label, len(v), 100 * (v > 0).mean(), v.mean(),
             np.median(v)))


def main(market_filter=None):
    print("loading TDCC panel...")
    p = load_panel()
    weeks = sorted(p["date"].unique())
    print("panel: %d rows, %d stocks, %d weeks (%s .. %s)"
          % (len(p), p["stock_id"].nunique(), len(weeks), weeks[0],
             weeks[-1]))

    names = json.load(open(NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}
    if market_filter:
        p = p[p["stock_id"].map(market) == market_filter]
        print("filtered to %s: %d stocks" % (market_filter,
                                             p["stock_id"].nunique()))

    print("building weekly price grid...")
    px = weekly_prices(weeks)
    d = p.merge(px.rename(columns={"week": "date"}), on=["date", "stock_id"],
                how="inner")
    d["r1"] = (d["fwd1"] / d["close"] - 1) * 100
    d["r4"] = (d["fwd4"] / d["close"] - 1) * 100
    # market-neutral: demean within week
    for c in ("r1", "r4"):
        d[c.replace("r", "alpha")] = d[c] - d.groupby("date")[c].transform("median")

    base = d.dropna(subset=["alpha4"])
    print("\njoined observations: %d (%.0f/week)"
          % (len(base), len(base) / max(base["date"].nunique(), 1)))
    print("\n=== candidate signals (4-week forward, within-week demeaned) ===")
    show("ALL (baseline, should be ~0)", d)
    show("S1 big400 up >=2 weeks", d[d["up_streak"] >= 2])
    show("S1b big400 up >=3 weeks", d[d["up_streak"] >= 3])
    show("S2 big400 jump >= +0.5pp", d[d["big_d"] >= 0.5])
    show("S2b big400 jump >= +1.0pp", d[d["big_d"] >= 1.0])
    show("S3 concentration (pct up, count down)",
         d[(d["big_d"] > 0) & (d["bign_d"] <= 0)])
    show("S3b whale tier up, count down",
         d[(d["whale_d"] > 0) & (d["bign_d"] <= 0)])
    show("S4 silent accum (S1 + 4w range <10%)",
         d[(d["up_streak"] >= 2) & (d["range4w"] < 0.10)])
    show("S5 distribution (down 2w + near 20d high)",
         d[(d["dn_streak"] >= 2) & (d["near_hi"] >= 0.97)])
    show("S6 retail handoff (retail down, big up)",
         d[(d["retail_d"] < 0) & (d["big_d"] > 0)])

    print("\n=== dose response: big400 weekly delta quintiles ===")
    dd = d.dropna(subset=["big_d", "alpha4"])
    if len(dd) > 500:
        dd = dd.copy()
        dd["q"] = pd.qcut(dd["big_d"], 5, labels=False, duplicates="drop")
        for q, g in dd.groupby("q"):
            show("delta quintile %d (%.2f..%.2f)"
                 % (q, g["big_d"].min(), g["big_d"].max()), g)

    print("\n=== 1-week horizon for the best cells ===")
    show("S2 big400 jump >= +0.5pp (1w)", d[d["big_d"] >= 0.5], "alpha1")
    show("S3 concentration (1w)",
         d[(d["big_d"] > 0) & (d["bign_d"] <= 0)], "alpha1")
    show("S5 distribution (1w)",
         d[(d["dn_streak"] >= 2) & (d["near_hi"] >= 0.97)], "alpha1")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
