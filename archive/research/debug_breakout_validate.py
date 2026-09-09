"""
Point-in-time validation of the mode_breakout reference-level fix (C). ASCII only.

Compares, over the whole research universe (research_prices.db, ~6y), the
forward 20-day outcome of breakout signals defined two ways:

  OLD: close > max of prior-20 CLOSES   (the pre-fix definition)
  NEW: close > max of prior-20 HIGHS    (the fix -- a real range breakout)

All conditions are strictly causal: the reference uses only prior bars (shift 1),
the outcome uses only future bars (shift -20). The decisive number is the
forward performance of the REMOVED set (OLD signals that NEW rejects): if those
are worse than the kept signals, the fix raised precision -- not a guess, a count.

    python debug_breakout_validate.py
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import DATA_DIR

RESEARCH_DB = DATA_DIR / "research_prices.db"
FWD = 20            # forward horizon (trading bars)
HIT = 0.10          # a "hit" = max forward high reaches +10% over entry close


def main():
    con = sqlite3.connect(RESEARCH_DB)
    df = pd.read_sql_query(
        "SELECT date,stock_id,high,close,Volume_Lot FROM data", con)
    con.close()
    for c in ("high", "close", "Volume_Lot"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values(["stock_id", "date"])

    agg = {k: 0.0 for k in (
        "base_n", "base_ret", "base_hit",
        "old_n", "old_ret", "old_hit",
        "new_n", "new_ret", "new_hit",
        "rem_n", "rem_ret", "rem_hit")}

    for _, g in df.groupby("stock_id", sort=False):
        if len(g) < 21 + FWD:
            continue
        c = g["close"].to_numpy(float)
        h = g["high"].to_numpy(float)
        v = g["Volume_Lot"].to_numpy(float)
        cs = pd.Series(c); hs = pd.Series(h); vs = pd.Series(v)

        close_max_prev = cs.rolling(20).max().shift(1).to_numpy()
        high_max_prev = hs.rolling(20).max().shift(1).to_numpy()
        vol_ma20 = vs.rolling(20).mean().to_numpy()
        vol_ma5_prev = vs.rolling(5).mean().shift(1).to_numpy()

        fwd_close = cs.shift(-FWD).to_numpy()                       # close[t+20]
        fwd_maxhigh = hs.rolling(FWD).max().shift(-FWD).to_numpy()  # max high t+1..t+20

        valid = ~np.isnan(close_max_prev) & ~np.isnan(high_max_prev) \
            & ~np.isnan(vol_ma20) & ~np.isnan(vol_ma5_prev) \
            & ~np.isnan(fwd_close) & ~np.isnan(fwd_maxhigh) & (c > 0)
        if not valid.any():
            continue

        ret = fwd_close / c - 1.0
        hit = (fwd_maxhigh / c - 1.0) >= HIT
        base = (vol_ma20 > 1000) & (v > vol_ma5_prev * 2)
        old = valid & base & (c > close_max_prev)
        new = valid & base & (c > high_max_prev)
        rem = old & ~new

        agg["base_n"] += valid.sum()
        agg["base_ret"] += ret[valid].sum()
        agg["base_hit"] += hit[valid].sum()
        for name, m in (("old", old), ("new", new), ("rem", rem)):
            agg[name + "_n"] += m.sum()
            agg[name + "_ret"] += ret[m].sum()
            agg[name + "_hit"] += hit[m].sum()

    def row(label, n_key):
        n = agg[n_key + "_n"]
        if n == 0:
            print("  {:<28} n=0".format(label)); return
        mret = agg[n_key + "_ret"] / n * 100
        hr = agg[n_key + "_hit"] / n * 100
        base_hr = agg["base_hit"] / agg["base_n"] * 100 if agg["base_n"] else 0
        lift = hr / base_hr if base_hr else 0
        print("  {:<28} n={:<7d} mean_fwd20={:+6.2f}%  P(+{:.0f}%)={:5.1f}%  lift={:.2f}".format(
            label, int(n), mret, HIT * 100, hr, lift))

    print("research universe: {} stocks, forward {}d, hit=+{:.0f}%\n".format(
        df["stock_id"].nunique(), FWD, HIT * 100))
    print("=== forward-20d performance of breakout signal sets ===")
    row("baseline (all bars)", "base")
    row("OLD (close > prior-20 close)", "old")
    row("NEW (close > prior-20 HIGH)", "new")
    row("REMOVED (OLD but not NEW)", "rem")
    print()
    print("verdict: if REMOVED lift/hit < NEW, the fix dropped weaker signals "
          "(precision up). REMOVED count vs OLD shows how many were affected.")


if __name__ == "__main__":
    main()
