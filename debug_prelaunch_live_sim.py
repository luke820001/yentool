"""
debug_prelaunch_live_sim.py

Faithful replay of the LIVE mode_prelaunch flow on the research db, to confirm
the shipped pipeline behaves as designed:

  per day:
    1. gate   : close > 60MA and liquid (Launch_Score gate)
    2. pool   : top 300 by 20d-avg volume   (market_filter prefilter proxy)
                + force-include names HELD from yesterday (include_ids)
    3. rank   : by the SHIPPED Launch_Score formula (S0)
    4. select : hysteresis top-N -- enter top 25, hold prior names to top 50
                (scanner.scan_mode.select_with_hysteresis, N_ENTER/N_HOLD)

Reports the selected-set persistence / survival / lift / ret5@sel / fwd20, the
exact metrics the user cares about. Reuses data/_feat_cache.pkl. ASCII only.
"""
import numpy as np
import pandas as pd
from debug_early_design import load_features
from scanner.scan_mode import N_ENTER, N_HOLD

POOL = 300


def launch_score(T):
    def c01(x, d): return (x / d).clip(0, 1)
    gate = ((T["c"] > T["ma60"]) & (T["vol_ma20"] > 300)).astype(float)
    mom   = c01(T["ret60"].clip(lower=0), 0.5)
    young = 1.0 - c01(T["ret5"].clip(lower=0), 0.12)
    near  = 1.0 - c01(T["dist52"].clip(lower=0), 0.30)
    acc   = c01((T["bias"] - 0.5).clip(lower=0), 0.45)
    tight = 1.0 - c01(T["rt"].clip(lower=0), 0.25)
    return (mom*0.30 + young*0.25 + near*0.20 + acc*0.15 + tight*0.10) * gate * 100


def main():
    T = load_features()
    T = T.assign(ls=launch_score(T))
    base = T["up_big"].mean()

    flags = np.zeros(len(T), dtype=bool)
    held = set()
    idx_by_date = {d: g.index for d, g in T.groupby("date")}
    for d in sorted(idx_by_date):
        gi = idx_by_date[d]
        day = T.loc[gi, ["sid", "ls", "vol_ma20"]].copy()
        day = day[day["ls"] > 0]
        if day.empty:
            held = set(); continue
        # prefilter pool: top POOL by liquidity, plus force-include held names
        pool = day.sort_values("vol_ma20", ascending=False).head(POOL)
        extra = day[day["sid"].isin(held) & ~day["sid"].isin(set(pool["sid"]))]
        pool = pd.concat([pool, extra])
        # rank by Launch_Score, hysteresis top-N
        pool = pool.sort_values("ls", ascending=False).reset_index()
        sel_sids = []
        for rank, r in enumerate(pool.itertuples(index=False)):
            sid = r.sid
            if rank < N_ENTER or (sid in held and rank < N_HOLD):
                sel_sids.append(sid)
        sel_sids = set(sel_sids)
        sel_rows = pool[pool["sid"].isin(sel_sids)]["index"].values
        flags[T.index.get_indexer(sel_rows)] = True
        held = sel_sids

    T = T.assign(_sel=flags)
    sel = T[T["_sel"]]
    by_date = sel.groupby("date")["sid"].apply(set)
    dates = sorted(by_date.index); ov = []
    for a, b in zip(dates, dates[1:]):
        sa, sb = by_date[a], by_date[b]; u = sa | sb
        ov.append(len(sa & sb)/len(u) if u else np.nan)
    runs = []
    for sid, g in T.sort_values("date").groupby("sid"):
        run = 0
        for x in g["_sel"].values:
            if x: run += 1
            elif run: runs.append(run); run = 0
        if run: runs.append(run)

    nd = T["date"].nunique()
    print("=== LIVE mode_prelaunch replay (enter top {}, hold top {}, pool {}) ===".format(
        N_ENTER, N_HOLD, POOL))
    print("base25={:.1%}".format(base))
    print("pass/day  {:.1f}".format(len(sel)/nd))
    print("persist   {:.2f}   (day-to-day list overlap; was 0.05-0.13 for short/breakout)".format(
        np.nanmean(ov)))
    print("survival  {:.1f} days   (median time a name stays on the list)".format(np.median(runs)))
    print("sel25     {:.1%}   lift {:.2f}".format(sel["up_big"].mean(), sel["up_big"].mean()/base))
    print("ret5@sel  {:+.1%}   (trailing 5d at selection; want LOW = flagged early)".format(
        sel["ret5"].median()))
    print("fwd20     {:+.1%}   (forward 20d median; short/breakout were negative)".format(
        sel["fwd20"].median()))


if __name__ == "__main__":
    main()
