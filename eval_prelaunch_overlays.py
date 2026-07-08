"""
eval_prelaunch_overlays.py

Search for overlays that GENUINELY raise the mode_prelaunch OTC win rate, with
guards against curve-fitting. Every candidate overlay is judged on the full
replay (214 scan days, multiple regimes) by three tests at once:

  win      pooled win rate
  bootLo   lower bound of the scan-day block-bootstrap 95pct interval
  h1 / h2  win rate in the first half AND the second half of the window

An overlay is only worth adopting if it lifts win in BOTH halves and its bootLo
also rises -- a lift that lives in one half or evaporates under resampling is
noise. Alpha vs the same-overlay universe benchmark is printed so a lift that is
pure beta (e.g. just holding longer in a bull tape) is flagged, not celebrated.

Overlays tested: market (ALL/OTC), TAIEX regime (risk_on / risk_off), hold
length (5/10), Launch_Score rank cut (top 10/20), and the best combination.

Run:  python eval_prelaunch_overlays.py
ASCII only.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

import eval_realtrade as er

FULL_WARMUP = "2025-06-02"
FULL_START = "2025-08-01"


def regime_map():
    con = sqlite3.connect(er.TAIEX_DB)
    tx = pd.read_sql("SELECT date, close FROM TAIEX ORDER BY date", con)
    con.close()
    tx["date"] = tx["date"].astype(str).str[:10]
    tx["ro"] = ((tx["close"] > tx["close"].rolling(20).mean())
                & (tx["close"] > tx["close"].rolling(60).mean()))
    return tx.set_index("date")["ro"].to_dict()


def boot_lo(out, iters=2000, seed=3):
    if out.empty:
        return 0.0
    rng = np.random.default_rng(seed)
    days = [g["ret"].to_numpy() for _, g in out.groupby("date")]
    n = len(days)
    wins = [100 * (np.concatenate([days[i] for i in rng.integers(0, n, n)]) > 0).mean()
            for _ in range(iters)]
    return float(np.percentile(wins, 2.5))


def winrate(out):
    if out.empty:
        return None
    r = out["ret"].to_numpy()
    days = sorted(out["date"].unique())
    mid = days[len(days) // 2]
    h1 = out[out["date"] < mid]["ret"].to_numpy()
    h2 = out[out["date"] >= mid]["ret"].to_numpy()
    return {
        "n": len(r), "win": 100 * (r > 0).mean(), "mean": r.mean(),
        "bootLo": boot_lo(out),
        "h1": 100 * (h1 > 0).mean() if len(h1) else 0,
        "h2": 100 * (h2 > 0).mean() if len(h2) else 0,
    }


def show(label, out, bench=None):
    s = winrate(out)
    if s is None:
        print("%-34s n=0" % label)
        return
    a = ""
    if bench is not None:
        b = winrate(bench)
        if b:
            a = "  alpha=%+.1fpp" % (s["win"] - b["win"])
    print("%-34s n=%4d win=%4.1f%% bootLo=%4.1f h1=%4.1f h2=%4.1f mean=%+5.2f%s"
          % (label, s["n"], s["win"], s["bootLo"], s["h1"], s["h2"], s["mean"], a))


def main():
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = regime_map()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    er.WARMUP_START = FULL_WARMUP
    P = er.replay_selection(T)
    P = P[P["date"] >= FULL_START].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))

    uni = T[T["date"].isin(P["date"].unique())][["date", "sid"]].copy()
    uni["mkt"] = uni["sid"].map(market).fillna("?")
    uni["ro"] = uni["date"].map(lambda d: reg.get(d, False))

    def sim(picks, hold):
        er.HOLD_BARS = hold
        return er.simulate(picks, fwd)

    print("=== single overlays (OTC base unless noted), full replay ===")
    otc = P[P["mkt"] == "OTC"]
    uotc = uni[uni["mkt"] == "OTC"]
    show("OTC hold5 (current)", sim(otc, 5), sim(uotc, 5))
    show("ALL hold5", sim(P, 5), sim(uni, 5))
    show("OTC hold5 + risk_on", sim(otc[otc["ro"]], 5), sim(uotc[uotc["ro"]], 5))
    show("OTC hold5 + risk_off", sim(otc[~otc["ro"]], 5), sim(uotc[~uotc["ro"]], 5))
    show("OTC hold10", sim(otc, 10), sim(uotc, 10))
    show("OTC hold10 + risk_on", sim(otc[otc["ro"]], 10), sim(uotc[uotc["ro"]], 10))
    show("OTC hold5 rank<20", sim(otc[otc["rank"] < 20], 5), sim(uotc, 5))
    show("OTC hold5 rank<10", sim(otc[otc["rank"] < 10], 5), sim(uotc, 5))

    print("\n=== combinations ===")
    show("OTC risk_on hold10", sim(otc[otc["ro"]], 10), sim(uotc[uotc["ro"]], 10))
    show("OTC risk_on hold10 rank<20",
         sim(otc[otc["ro"] & (otc["rank"] < 20)], 10), sim(uotc[uotc["ro"]], 10))
    show("OTC risk_on hold5 rank<20",
         sim(otc[otc["ro"] & (otc["rank"] < 20)], 5), sim(uotc[uotc["ro"]], 5))
    show("ALL risk_on hold10", sim(P[P["ro"]], 10), sim(uni[uni["ro"]], 10))

    print("\nADOPT ONLY IF: win, bootLo, h1 AND h2 all clear the current row,"
          " and alpha stays > 0 (else it is beta or noise).")


if __name__ == "__main__":
    main()
