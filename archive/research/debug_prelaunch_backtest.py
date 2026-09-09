"""
Backtest candidate pre-launch screens on price_volume.db.

For every eligible bar we compute the same no-lookahead features, label whether
the stock rose >= GAIN within the next FWD days, then measure each screen's
PRECISION (P(launch | screened)) against the BASE RATE (P(launch)). lift = ratio.
A useful screen has lift well above 1 while still firing on enough bars to be
practical. All strings ASCII. Run: python debug_prelaunch_backtest.py
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE

FWD, GAIN, MIN_HIST = 20, 0.30, 60


def build_table():
    con = sqlite3.connect(PRICE_VOLUME_FILE)
    df = pd.read_sql_query("SELECT * FROM data", con)
    con.close()
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date").reset_index(drop=True)
        n = len(g)
        if n < MIN_HIST + FWD + 5:
            continue
        c = pd.to_numeric(g["close"], errors="coerce").to_numpy(float)
        h = pd.to_numeric(g["high"], errors="coerce").to_numpy(float)
        l = pd.to_numeric(g["low"], errors="coerce").to_numpy(float)
        v = pd.to_numeric(g["Volume_Lot"], errors="coerce").to_numpy(float)
        for i in range(MIN_HIST, n - FWD):
            ci = c[i]
            if ci <= 0:
                continue
            fwd = np.nanmax(c[i+1:i+1+FWD]) / ci - 1
            ma5  = np.nanmean(c[i-4:i+1]);  ma10 = np.nanmean(c[i-9:i+1])
            ma20 = np.nanmean(c[i-19:i+1]); ma60 = np.nanmean(c[i-59:i+1])
            p20v = np.nanmean(v[i-20:i]); box_hi = np.nanmax(h[i-19:i+1]); box_lo = np.nanmin(l[i-19:i+1])
            up = tot = 0.0
            for k in range(i-9, i+1):
                vv = v[k]
                if np.isnan(vv):
                    continue
                tot += vv
                if c[k] > c[k-1]:
                    up += vv
            rows.append({
                "fwd": fwd,
                "label": 1 if fwd >= GAIN else 0,
                "above_ma60": ci > ma60,
                "ma_align": ma5 > ma10 > ma20,
                "ret20": ci / c[i-20] - 1 if c[i-20] > 0 else np.nan,
                "ret60": ci / c[i-60] - 1 if c[i-60] > 0 else np.nan,
                "dist_ma60": (ci - ma60) / ma60 if ma60 > 0 else np.nan,
                "dist_ma20": (ci - ma20) / ma20 if ma20 > 0 else np.nan,
                "bias10": up / tot if tot > 0 else np.nan,
                "pos_in_box": (ci - box_lo) / (box_hi - box_lo) if box_hi > box_lo else np.nan,
                "dry": v[i] / p20v if p20v and p20v > 0 else np.nan,
            })
    return pd.DataFrame(rows).dropna()


def report(name, mask, T):
    n = int(mask.sum())
    base = T["label"].mean()
    if n == 0:
        print("{:<26} fires=0".format(name)); return
    prec = T.loc[mask, "label"].mean()
    print("{:<26} fires={:>5} ({:>4.1f}%)  precision={:>5.1%}  base={:>5.1%}  lift={:>4.2f}".format(
        name, n, 100*n/len(T), prec, base, prec/base if base else 0))


def main():
    T = build_table()
    print("eligible bars: {}   launches: {} ({:.1%})\n".format(
        len(T), int(T["label"].sum()), T["label"].mean()))

    report("above_ma60 only", T["above_ma60"], T)
    report("ma_align only", T["ma_align"], T)
    report("ret60>=10%", T["ret60"] >= 0.10, T)
    report("ret60 in [10%,60%]", (T["ret60"] >= 0.10) & (T["ret60"] <= 0.60), T)
    report("bias10>=0.55", T["bias10"] >= 0.55, T)

    # composite momentum-continuation screen (the proposed mode 5)
    v1 = (
        T["above_ma60"] &
        (T["ret60"].between(0.08, 0.60)) &
        (T["ret20"].between(-0.02, 0.18)) &      # not yet exploded short-term
        (T["dist_ma60"] <= 0.25) &               # not over-extended
        (T["bias10"] >= 0.52)
    )
    report("MODE5 v1 (momentum)", v1, T)

    v2 = v1 & T["ma_align"]
    report("MODE5 v2 (+ma_align)", v2, T)

    v3 = (
        T["above_ma60"] &
        (T["ret60"].between(0.10, 0.80)) &
        (T["ret20"].between(0.0, 0.15)) &
        (T["dist_ma20"].between(-0.04, 0.08)) &  # hugging the 20MA (pullback entry)
        (T["bias10"] >= 0.55)
    )
    report("MODE5 v3 (pullback)", v3, T)

    v4 = v3 & T["ma_align"]
    report("MODE5 v4 (pullback+align)", v4, T)


if __name__ == "__main__":
    main()
