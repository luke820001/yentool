"""
Evaluate two proposed adjustments on our own data (no code changes to the app):
  (1) Market-regime filter: does gating by TAIEX above/below its MA improve the
      edge? (compares forward outcomes in up vs down regime)
  (2) Strength score vs Explosion score: which one's deciles actually rank
      forward returns (monotonic = predictive)?
FWD = 20 trading days; "up10" = price reaches +10% within FWD. ASCII only.
"""
import sqlite3
import numpy as np
import pandas as pd
from config.settings import PRICE_VOLUME_FILE, TAIEX_FILE

FWD = 20


def load_taiex_regime():
    con = sqlite3.connect(TAIEX_FILE)
    try:
        t = pd.read_sql_query("SELECT * FROM TAIEX", con)
    except Exception:
        t = pd.read_sql_query("SELECT * FROM data", con)
    con.close()
    t["date"] = pd.to_datetime(t["date"], errors="coerce")
    t["close"] = pd.to_numeric(t["close"], errors="coerce")
    t = t.dropna().sort_values("date").reset_index(drop=True)
    t["ma20"] = t["close"].rolling(20).mean()
    t["ma60"] = t["close"].rolling(60).mean()
    t["up20"] = t["close"] > t["ma20"]
    t["up60"] = t["close"] > t["ma60"]
    key = t["date"].dt.strftime("%Y-%m-%d")
    return (dict(zip(key, t["up20"])), dict(zip(key, t["up60"])),
            float(t["up20"].mean()), float(t["up60"].mean()), len(t))


def build():
    con = sqlite3.connect(PRICE_VOLUME_FILE)
    df = pd.read_sql_query("SELECT * FROM data", con)
    con.close()
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date").reset_index(drop=True)
        n = len(g)
        if n < 64 + FWD + 2:
            continue
        c = pd.to_numeric(g["close"], errors="coerce")
        h = pd.to_numeric(g["high"], errors="coerce")
        l = pd.to_numeric(g["low"], errors="coerce")
        v = pd.to_numeric(g["Volume_Lot"], errors="coerce")
        date = g["date"].astype(str).str[:10].to_numpy()
        cN, hN, lN, vN = c.to_numpy(float), h.to_numpy(float), l.to_numpy(float), v.to_numpy(float)
        ma5 = c.rolling(5).mean().to_numpy(float)
        ma10 = c.rolling(10).mean().to_numpy(float)
        ma20 = c.rolling(20).mean().to_numpy(float)
        ma60 = c.rolling(60).mean().to_numpy(float)
        vma20 = v.rolling(20).mean().to_numpy(float)
        rt = ((h.rolling(20).max() - l.rolling(20).min()) / l.rolling(20).min()).to_numpy(float)
        dry = (v / v.rolling(20).mean()).to_numpy(float)
        prev = c.shift(1)
        upv = v.where(c > prev, 0.0); dnv = v.where(c < prev, 0.0)
        bias = (upv.rolling(10).sum() / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan)).to_numpy(float)
        h52 = c.rolling(252, min_periods=63).max().to_numpy(float)
        for i in range(64, n - FWD):
            if cN[i] <= 0 or np.isnan(ma60[i]) or np.isnan(cN[i-63]) or np.isnan(bias[i]):
                continue
            ret60 = cN[i] / cN[i-63] - 1
            ret20 = cN[i] / cN[i-20] - 1
            dist60 = (cN[i] - ma60[i]) / ma60[i]
            above60 = cN[i] > ma60[i]
            align = ma5[i] > ma10[i] > ma20[i]
            dist52 = (h52[i] - cN[i]) / h52[i] if h52[i] > 0 else 1.0
            # Explosion score (current pipeline formula)
            expl = (max(0.0, (0.20 - min(rt[i], 0.20)) / 0.20 * 35)
                    + (1.0 - min(dry[i], 1.0)) * 35
                    + (min(max(bias[i], 0.5), 1.0) - 0.5) / 0.5 * 30)
            # Strength score (proposed; built from validated momentum features)
            strn = (min(max(ret60, 0) / 0.6, 1) * 30
                    + min(max(ret20, 0) / 0.3, 1) * 20
                    + (15 if above60 else 0)
                    + (15 if align else 0)
                    + min(max(bias[i] - 0.5, 0) / 0.5, 1) * 10
                    + (10 if dist52 <= 0.15 else 0))
            fwd_up10 = (np.nanmax(cN[i+1:i+1+FWD]) / cN[i] - 1) >= 0.10
            fwd_ret = cN[min(i+FWD, n-1)] / cN[i] - 1
            ml = (above60 and align and ret60 >= 0.20 and ret20 >= 0.05
                  and bias[i] >= 0.50 and vma20[i] > 300)
            rows.append((date[i], expl, strn, fwd_up10, fwd_ret, ml))
    return pd.DataFrame(rows, columns=["date", "expl", "strn", "up10", "fwd", "ml"])


def decile_table(T, col):
    T = T.copy()
    T["dec"] = pd.qcut(T[col], 10, labels=False, duplicates="drop")
    g = T.groupby("dec").agg(n=("up10", "size"), up10=("up10", "mean"), fwd=("fwd", "mean"))
    return g


def main():
    up20, up60, frac20, frac60, ntx = load_taiex_regime()
    print("TAIEX bars: {}  | days above MA20: {:.0%}  above MA60: {:.0%}\n".format(
        ntx, frac20, frac60))

    T = build()
    base = T["up10"].mean()
    print("stock-bars: {}  base up10: {:.1%}  base fwd: {:+.2%}\n".format(
        len(T), base, T["fwd"].mean()))

    # ---- (1) regime filter ----
    T["reg20"] = T["date"].map(up20)
    T["reg60"] = T["date"].map(up60)
    print("=== (1) Market regime filter (all stock-bars) ===")
    for col, lab in [("reg20", "TAIEX>MA20"), ("reg60", "TAIEX>MA60")]:
        for state, name in [(True, lab + " UP"), (False, lab + " DOWN")]:
            sub = T[T[col] == state]
            if len(sub) < 50:
                print("  {:<18} n={:>5}  (too few)".format(name, len(sub))); continue
            print("  {:<18} n={:>6}  up10={:.1%}  fwd={:+.2%}".format(
                name, len(sub), sub["up10"].mean(), sub["fwd"].mean()))
    print("\n  -- momentum_leader signals, split by regime --")
    ml = T[T["ml"]]
    for col, lab in [("reg60", "TAIEX>MA60")]:
        for state, name in [(True, "UP"), (False, "DOWN")]:
            sub = ml[ml[col] == state]
            if len(sub) < 30:
                print("  ML in {} {:<6} n={:>5} (too few)".format(lab, name, len(sub))); continue
            print("  ML when {} {:<5} n={:>5}  up10={:.1%}  fwd={:+.2%}  win={:.1%}".format(
                lab, name, len(sub), sub["up10"].mean(), sub["fwd"].mean(), (sub["fwd"] > 0).mean()))

    # ---- (2) score comparison ----
    print("\n=== (2) Explosion vs Strength score -- decile up10 (monotonic = predictive) ===")
    for col, lab in [("expl", "Explosion"), ("strn", "Strength")]:
        g = decile_table(T, col)
        line = "  ".join("{:.0%}".format(x) for x in g["up10"].values)
        top = g["up10"].iloc[-1]; bot = g["up10"].iloc[0]
        print("  {:<10} D0->D9 up10: {}".format(lab, line))
        print("  {:<10} top decile {:.1%} (lift {:.2f})  bottom {:.1%} (lift {:.2f})  spread {:+.1%}\n".format(
            lab, top, top/base, bot, bot/base, top-bot))


if __name__ == "__main__":
    main()
