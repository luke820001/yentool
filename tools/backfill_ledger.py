"""
Backfill matured forward returns into the signal ledger and print a live
performance summary. ASCII only.

Run this after the daily price_volume.db update (e.g. on a schedule). It is the
feedback half of the loop: every pick the scanner ever made gets its realized
5/10/20-day outcome filled in once the bars exist, then summarized so you can
see whether the LIVE system matches the backtest -- and watch for alpha decay.

    python backfill_ledger.py
"""
import pandas as pd

from scanner.signal_ledger import backfill_outcomes, load_picks, load_outcomes

# Forward move counted as a "hit" per horizon (close-to-close, percent).
HIT_PCT = {5: 8.0, 10: 15.0, 20: 20.0}


def main():
    filled = backfill_outcomes()
    print("backfilled {} new outcome rows\n".format(filled))

    picks = load_picks()
    out = load_outcomes()
    if picks.empty:
        print("ledger is empty -- run a scan first.")
        return

    print("picks logged: {} across {} sessions, {} modes".format(
        len(picks), picks["scan_session"].nunique(),
        picks["scan_mode"].nunique()))
    print("date range: {} -> {}\n".format(
        picks["scan_session"].min(), picks["scan_session"].max()))

    if out.empty:
        print("no matured outcomes yet (need >= shortest horizon of forward "
              "bars after a pick).")
        return

    # Performance by mode x horizon: realized hit-rate and average move.
    print("=== realized forward performance (matured picks) ===")
    out = out.copy()
    out["ret"] = pd.to_numeric(out["fwd_return_pct"], errors="coerce")
    out["mfe"] = pd.to_numeric(out["mfe_pct"], errors="coerce")
    for (mode, h), g in out.groupby(["scan_mode", "horizon_days"]):
        g = g.dropna(subset=["ret"])
        if g.empty:
            continue
        thr = HIT_PCT.get(int(h), 20.0)
        hit = (g["mfe"] >= thr).mean() * 100
        print("  {:<22} {:>2}d  n={:<4d}  mean={:+6.2f}%  median={:+6.2f}%  "
              "P(MFE>={:.0f}%)={:5.1f}%".format(
                  mode, int(h), len(g), g["ret"].mean(),
                  g["ret"].median(), thr, hit))

    # Score calibration: does a higher Surge_Score actually pay? (20d window.)
    print("\n=== Surge_Score calibration (20d, all modes pooled) ===")
    merged = out[out["horizon_days"] == 20].merge(
        picks[["scan_session", "scan_mode", "stock_id", "surge_score"]],
        on=["scan_session", "scan_mode", "stock_id"], how="left")
    merged["surge_score"] = pd.to_numeric(merged["surge_score"], errors="coerce")
    merged = merged.dropna(subset=["surge_score", "ret"])
    if len(merged) >= 20:
        merged["bucket"] = pd.qcut(
            merged["surge_score"], 4, labels=["Q1", "Q2", "Q3", "Q4"],
            duplicates="drop")
        for b, g in merged.groupby("bucket", observed=True):
            print("  {}  n={:<4d}  mean_ret={:+6.2f}%  P(MFE>=20%)={:5.1f}%".format(
                b, len(g), g["ret"].mean(), (g["mfe"] >= 20.0).mean() * 100))
    else:
        print("  not enough matured 20d rows yet (need ~20+).")


if __name__ == "__main__":
    main()
