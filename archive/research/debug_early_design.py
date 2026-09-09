"""
debug_early_design.py

Design + validate an EARLY-launch detector on the research db. Goal: flag a name
BEFORE its explosive run (low trailing 5d return at selection), with high forward
lift and high day-to-day stability -- the opposite of the breakout/short modes,
which flag the climax (ret5 ~ +10% then fade).

Builds a rich per-bar feature table once, caches it to data/_feat_cache.pkl, then
scores arbitrary candidate definitions and reports persist / survival / lift /
ret5@sel / fwd20 so variants can be compared cheaply.

ASCII only.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import DATA_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
CACHE = DATA_DIR / "_feat_cache.pkl"
FWD = 20
BIG = 0.25


def build_features():
    con = sqlite3.connect(RESEARCH_DB)
    df = pd.read_sql_query(
        "SELECT stock_id,date,open,high,low,close,Volume_Lot FROM data", con)
    con.close()
    out = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date"); n = len(g)
        if n < 64 + FWD + 5:
            continue
        c = pd.to_numeric(g["close"], errors="coerce")
        h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce")
        v = pd.to_numeric(g["Volume_Lot"], errors="coerce")

        ma5  = c.rolling(5).mean();  ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean(); ma60 = c.rolling(60).mean()
        prev_max20 = c.rolling(20).max().shift(1)
        prev_max40 = c.rolling(40).max().shift(1)
        vol_ma20 = v.rolling(20).mean()
        vol_ma5  = v.rolling(5).mean()

        ret60 = c / c.shift(63) - 1
        ret20 = c / c.shift(20) - 1
        ret10 = c / c.shift(10) - 1
        ret5  = c / c.shift(5) - 1
        atr   = ((h - l) / c).rolling(20).mean()

        rt = (h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()
        prev = c.shift(1)
        upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = upv.rolling(10).sum() / (upv.rolling(10).sum()
                                        + dnv.rolling(10).sum()).replace(0, np.nan)
        h52 = c.rolling(252, min_periods=63).max()
        dist52 = (h52 - c) / h52
        vsurge = vol_ma5 / vol_ma20.shift(5)

        fwd_max = c[::-1].rolling(FWD, min_periods=1).max()[::-1].shift(-1)
        up_big  = (fwd_max / c - 1) >= BIG
        fwd20   = c.shift(-FWD) / c - 1

        sub = pd.DataFrame({
            "date": g["date"].astype(str).str[:10].values, "sid": str(sid),
            "c": c.values, "ma5": ma5.values, "ma10": ma10.values,
            "ma20": ma20.values, "ma60": ma60.values,
            "prev_max20": prev_max20.values, "prev_max40": prev_max40.values,
            "vol_ma20": vol_ma20.values, "vol_now": v.values,
            "ret60": ret60.values, "ret20": ret20.values,
            "ret10": ret10.values, "ret5": ret5.values,
            "atr": atr.values, "rt": rt.values, "bias": bias.values,
            "dist52": dist52.values, "vsurge": vsurge.values,
            "up_big": up_big.values, "fwd20": fwd20.values,
        })
        out.append(sub.iloc[64:n - FWD])
    T = pd.concat(out, ignore_index=True).dropna(subset=["up_big"])
    return T


def load_features():
    if CACHE.exists():
        return pd.read_pickle(CACHE)
    print("Building feature cache (one time, ~1 min) ...")
    T = build_features()
    T.to_pickle(CACHE)
    return T


def persistence(T, mask):
    sel = T[mask.fillna(False)]
    by_date = sel.groupby("date")["sid"].apply(set)
    dates = sorted(by_date.index); ov = []
    for a, b in zip(dates, dates[1:]):
        sa, sb = by_date[a], by_date[b]; u = sa | sb
        ov.append(len(sa & sb) / len(u) if u else np.nan)
    return np.nanmean(ov) if ov else np.nan


def survival(T, mask):
    runs = []
    tmp = T.assign(_m=mask.fillna(False).values)
    for sid, g in tmp.sort_values("date").groupby("sid"):
        run = 0
        for x in g["_m"].values:
            if x: run += 1
            elif run: runs.append(run); run = 0
        if run: runs.append(run)
    return np.median(runs) if runs else np.nan


def report(T, name, mask):
    base = T["up_big"].mean(); nd = T["date"].nunique()
    sel = T[mask.fillna(False)]
    if sel.empty:
        print("{:<16} (no selections)".format(name)); return
    print("{:<16} {:>7.1f} {:>8.2f} {:>8.1f} {:>7.1%} {:>7.2f} {:>9.1%} {:>7.1%}".format(
        name, len(sel) / nd, persistence(T, mask), survival(T, mask),
        sel["up_big"].mean(), sel["up_big"].mean() / base,
        sel["ret5"].median(), sel["fwd20"].median()))


def main():
    T = load_features()
    base = T["up_big"].mean()
    print("stock-bars {}  days {}  base25={:.1%}\n".format(
        len(T), T["date"].nunique(), base))
    print("{:<16} {:>7} {:>8} {:>8} {:>7} {:>7} {:>9} {:>7}".format(
        "variant", "pass/d", "persist", "surviv", "sel25", "lift", "ret5@sel", "fwd20"))
    print("-" * 74)

    up = T["c"] > T["ma60"]
    liq = T["vol_ma20"] > 300

    # current best baseline: leader
    leader = (up & (T["ma5"] > T["ma10"]) & (T["ma10"] > T["ma20"])
              & (T["ret60"] >= 0.20) & (T["ret20"] >= 0.05)
              & (T["bias"] >= 0.50) & liq)
    report(T, "leader(current)", leader)

    # E1: coil near highs, NOT yet in a 5d climax (the core 'early' idea)
    e1 = (up & (T["ma20"] > T["ma60"])
          & (T["ret60"].between(0.05, 0.60)) & (T["ret5"] < 0.06)
          & (T["c"] >= T["prev_max20"] * 0.90) & (T["c"] <= T["prev_max20"] * 1.02)
          & (T["rt"] < 0.18) & (T["bias"] >= 0.50) & liq)
    report(T, "E1 coil_nohighs", e1)

    # E2: same but require tighter base + accumulation, looser price band
    e2 = (up & (T["ma20"] > T["ma60"])
          & (T["ret60"].between(0.05, 0.80)) & (T["ret5"] < 0.08)
          & (T["c"] >= T["prev_max40"] * 0.85)
          & (T["rt"] < 0.20) & (T["bias"] >= 0.55) & liq)
    report(T, "E2 base_accum", e2)

    # E3: early stack -- short MAs just turning up, momentum young, pre-climax
    e3 = (up & (T["ma5"] > T["ma20"]) & (T["ma20"] > T["ma60"])
          & (T["ret60"].between(0.05, 0.50)) & (T["ret5"].between(-0.02, 0.07))
          & (T["bias"] >= 0.55) & (T["dist52"] <= 0.20) & liq)
    report(T, "E3 early_stack", e3)

    # E4: E3 + volume still quiet (no climax spike yet)
    e4 = e3 & (T["vol_now"] < T["vol_ma20"] * 1.8)
    report(T, "E4 +quiet_vol", e4)

    # --- continuous EARLY score; take top decile per day ------------------
    def clip01(x, d):
        return (x / d).clip(0, 1)
    gate = up.astype(float) * liq.astype(float)

    def show(name, score, qcut=0.95):
        Tt = T.assign(_s=score * gate)
        q = Tt.groupby("date")["_s"].transform(lambda s: s.rank(pct=True))
        report(Tt, name, (q >= qcut) & (Tt["_s"] > 0))

    mom60 = clip01(T["ret60"].clip(lower=0), 0.5)
    mom20 = clip01(T["ret20"].clip(lower=0), 0.25)
    young = 1.0 - clip01(T["ret5"].clip(lower=0), 0.12)
    near  = 1.0 - clip01(T["dist52"].clip(lower=0), 0.30)
    acc   = clip01((T["bias"] - 0.5).clip(lower=0), 0.45)
    tight = 1.0 - clip01(T["rt"].clip(lower=0), 0.25)
    pivot = 1.0 - clip01((T["prev_max20"] - T["c"]).clip(lower=0)
                         / T["c"], 0.10)               # near/at 20d pivot
    vexp  = clip01((T["vsurge"] - 0.9).clip(lower=0), 0.8)  # early vol expansion
    # Goldilocks 5d: peak reward 0..4%, fade out by 10%, penalize falling knife
    gold5 = (1.0 - clip01((T["ret5"] - 0.04).clip(lower=0), 0.08)) \
            * clip01((T["ret5"] + 0.05).clip(lower=0), 0.05)

    show("S0 baseline",
         mom60*0.30 + young*0.25 + near*0.20 + acc*0.15 + tight*0.10)
    show("S1 +pivot",
         mom60*0.25 + young*0.20 + near*0.15 + acc*0.15 + pivot*0.15 + tight*0.10)
    show("S2 mom20",
         mom20*0.30 + young*0.20 + near*0.15 + acc*0.15 + pivot*0.20)
    show("S3 +volexp",
         mom60*0.22 + young*0.18 + near*0.15 + acc*0.15 + pivot*0.15 + vexp*0.15)
    show("S4 gold5",
         mom60*0.30 + gold5*0.25 + near*0.15 + acc*0.15 + pivot*0.15)
    show("S5 gold5+vexp",
         mom60*0.25 + gold5*0.22 + near*0.13 + acc*0.15 + pivot*0.13 + vexp*0.12)
    show("S4 gold5 top10%",
         mom60*0.30 + gold5*0.25 + near*0.15 + acc*0.15 + pivot*0.15, qcut=0.90)

    # --- hysteresis on the chosen S0 score: enter strict, exit loose -------
    s0 = (mom60*0.30 + young*0.25 + near*0.20 + acc*0.15 + tight*0.10) * gate
    Th = T.assign(_s=s0)
    Th["_q"] = Th.groupby("date")["_s"].transform(lambda s: s.rank(pct=True))

    def hysteresis(enter_q, hold_q):
        held = set()
        flags = np.zeros(len(Th), dtype=bool)
        # iterate by date in order; vectorize within a date
        idx_by_date = {d: g.index for d, g in Th.groupby("date")}
        for d in sorted(idx_by_date):
            gi = idx_by_date[d]
            q = Th.loc[gi, "_q"]; sids = Th.loc[gi, "sid"]
            pos = (Th.loc[gi, "_s"] > 0)
            enter = (q >= enter_q) & pos
            stay  = (q >= hold_q) & pos & sids.isin(held)
            sel = enter | stay
            flags[Th.index.get_indexer(gi)] = sel.values
            held = set(sids[sel].values)
        return pd.Series(flags, index=Th.index)

    for eq, hq, nm in [(0.95, 0.85, "hys 95/85"), (0.95, 0.80, "hys 95/80"),
                       (0.97, 0.85, "hys 97/85")]:
        report(Th, nm, hysteresis(eq, hq))


if __name__ == "__main__":
    main()
