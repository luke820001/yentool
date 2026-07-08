"""
confirm_prelaunch.py

Rigorous, repeated confirmation of the mode_prelaunch win rate using the stocks
the software actually recommends. Answers one question honestly: is the >70%
win rate real and stable, or a small-sample / single-regime artifact?

Three independent lines of evidence:

  1. LIVE LEDGER (ground truth). Take the real recommended picks from
     signal_ledger.db, dedup by (stock_id, bar_date) (playbook rule 2), and
     recompute each trade with REAL execution (next-day OPEN entry, hold 5,
     close exit) from price_volume.db -- NOT the stored close-to-close outcome,
     which overstates. This is exactly what the app told you to buy. Small n.

  2. FULL REPLAY (large sample). The selection rule (Launch_Score + hysteresis)
     is deterministic, so replaying it over the whole price_volume.db history
     reproduces what the app WOULD have recommended every day. Same real
     execution. This gives many scan days across multiple regimes.

  3. FAITHFULNESS CHECK. On the days both exist, replay picks must overlap the
     live ledger picks ~75-85% and give a similar win rate; otherwise the
     replay has drifted and evidence line 2 is not trustworthy.

Rigor: win rate is reported with a Wilson 95% interval AND a scan-day block
bootstrap 95% interval (trades within one day share the market's move, so the
naive per-trade interval is too tight). Per-day and per-month win rates and a
first-half/second-half split expose regime dependence. Whole-universe benchmark
is printed so beta is never mistaken for the strategy.

Run:  python confirm_prelaunch.py
ASCII only. Deps: pandas, numpy. Reuses eval_realtrade.py machinery.
"""
import sqlite3

import numpy as np
import pandas as pd

import eval_realtrade as er

LEDGER = "data/signal_ledger.db"
FULL_WARMUP = "2025-06-02"   # ~2 months before the first evaluated day
FULL_START = "2025-08-01"    # first scan day in the large-sample replay
HOLD = er.HOLD_BARS


# ----------------------------------------------------------------- stats
def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * (c - h) / d, 100 * (c + h) / d)


def day_bootstrap(out, iters=3000, seed=1):
    """Block bootstrap over scan-days: resample whole days with replacement,
    recompute the pooled win rate. Returns (lo, hi) 95pct on win%."""
    if out.empty:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    by_day = [g["ret"].to_numpy() for _, g in out.groupby("date")]
    ndays = len(by_day)
    wins = []
    for _ in range(iters):
        idx = rng.integers(0, ndays, ndays)
        pooled = np.concatenate([by_day[i] for i in idx])
        wins.append(100 * (pooled > 0).mean())
    return (float(np.percentile(wins, 2.5)), float(np.percentile(wins, 97.5)))


def summarize(label, out):
    if out.empty:
        print("%-24s n=0" % label)
        return None
    r = out["ret"].to_numpy()
    k, n = int((r > 0).sum()), len(r)
    wl, wh = wilson(k, n)
    bl, bh = day_bootstrap(out)
    byday = out.groupby("date")["ret"].apply(lambda x: 100 * (x > 0).mean())
    print("%-24s n=%3d days=%2d win=%4.1f%%  Wilson[%.0f-%.0f]  dayBoot[%.0f-%.0f]  "
          "mean=%+5.2f worst=%+6.1f  daypos=%3.0f%%"
          % (label, n, out["date"].nunique(), 100 * k / n, wl, wh, bl, bh,
             r.mean(), r.min(), 100 * (byday > 50).mean()))
    return {"win": 100 * k / n, "n": n, "days": out["date"].nunique(),
            "boot": (bl, bh), "byday": byday}


# ----------------------------------------------------------------- live ledger
def live_ledger_trades(df, fwd, market):
    con = sqlite3.connect(LEDGER)
    p = pd.read_sql("SELECT stock_id, bar_date, market FROM picks "
                    "WHERE scan_mode='mode_prelaunch'", con)
    con.close()
    if p.empty:
        return pd.DataFrame(columns=["date", "sid", "mkt", "ret"])
    p = p.drop_duplicates(["stock_id", "bar_date"])
    p = p.rename(columns={"stock_id": "sid", "bar_date": "date"})
    p["sid"] = p["sid"].astype(str)
    rows = er.simulate(p, fwd)                      # real execution (next open)
    rows["mkt"] = rows["sid"].map(market).fillna("?")
    return rows


# ----------------------------------------------------------------- driver
def main():
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)

    names = __import__("json").load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    # ---- line 1: live ledger, real execution -------------------------
    live = live_ledger_trades(df, fwd, market)
    print("=== 1) LIVE LEDGER (real recommended picks, next-open entry, hold %d) ===" % HOLD)
    summarize("  all markets", live)
    summarize("  OTC only", live[live["mkt"] == "OTC"])
    print("  (matured scan days = %d -- confirmation needs many; see replay below)\n"
          % live["date"].nunique())

    # ---- line 2: full replay, real execution -------------------------
    er.WARMUP_START = FULL_WARMUP
    P = er.replay_selection(T)
    P = P[P["date"] >= FULL_START].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")

    otc = er.simulate(P[P["mkt"] == "OTC"], fwd)
    allm = er.simulate(P, fwd)
    uni_all = er.simulate(T[T["date"].isin(P["date"].unique())][["date", "sid"]], fwd)
    uni = uni_all.copy(); uni["mkt"] = uni["sid"].map(market).fillna("?")

    print("=== 2) FULL REPLAY %s..%s (deterministic rule = what app recommends) ==="
          % (FULL_START, df["date"].max()))
    s_all = summarize("  all markets", allm)
    s_otc = summarize("  OTC only  <-- headline", otc)
    summarize("  universe(all) bench", uni_all)
    summarize("  universe OTC bench", uni[uni["mkt"] == "OTC"])
    if s_otc:
        ub = 100 * (uni[uni["mkt"] == "OTC"]["ret"] > 0).mean()
        print("  OTC alpha on win rate: %+.1f pp\n" % (s_otc["win"] - ub))

    # ---- repeated validation: per-month + half split -----------------
    print("=== 3) REPEATED VALIDATION -- OTC win rate is not one lucky window ===")
    otc2 = otc.copy(); otc2["ym"] = otc2["date"].str[:7]
    for ym, g in otc2.groupby("ym"):
        r = g["ret"].to_numpy()
        print("  %s  n=%3d  win=%4.1f%%  mean=%+5.2f" %
              (ym, len(r), 100 * (r > 0).mean(), r.mean()))
    days = sorted(otc["date"].unique())
    mid = days[len(days) // 2]
    h1 = otc[otc["date"] < mid]; h2 = otc[otc["date"] >= mid]
    print("  --- half split ---")
    summarize("  first half", h1)
    summarize("  second half", h2)

    # ---- faithfulness: replay vs live on shared days -----------------
    print("\n=== 4) FAITHFULNESS (replay vs live ledger on shared days) ===")
    shared = sorted(set(P["date"]) & set(live["date"]))
    for d in shared:
        a = set(P[P["date"] == d]["sid"]); b = set(live[live["date"] == d]["sid"])
        if a and b:
            ov = 100 * len(a & b) / len(b)
            print("  %s overlap=%3.0f%% (replay=%d live=%d)" % (d, ov, len(a), len(b)))

    print("\nVERDICT GUIDE: 70%+ is confirmed only if the OTC headline win rate,"
          " its day-bootstrap LOWER bound, and BOTH halves all stay >=70%.")


if __name__ == "__main__":
    main()
