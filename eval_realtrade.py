"""
eval_realtrade.py

Standard real-trade evaluation for the prelaunch strategy. Read together with
docs/EVAL_PLAYBOOK.md -- that doc explains the baselines, the verdicts, and
how to interpret every number printed here.

What it does (all from the CURRENT databases, no external data):
  1. Rebuild features from data/price_volume.db and replay the SHIPPED
     mode_prelaunch selection (Launch_Score + hysteresis N_ENTER/N_HOLD)
     day by day, so we get picks for every scan day, not only the days the
     GUI was actually run.
  2. Sanity-check the replay against the real ledger picks (overlap should
     stay around 75-85%; if it drops, the shipped formula has drifted from
     this replay and the replay must be updated).
  3. Simulate REAL execution for every pick: enter next-day OPEN, hold 5
     bars, with optional overlays (disaster stop, OTC-only, rank cut,
     regime). Paper close-to-close numbers are NOT used -- they overstate.
  4. Print the whole-universe benchmark per market so tape beta is never
     mistaken for selection alpha.

Run:  python eval_realtrade.py            (evaluates every scan day that has
                                           5 forward bars, from EVAL_START)
ASCII only. No third-party deps beyond pandas/numpy.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

DB = "data/price_volume.db"
TAIEX_DB = "data/taiex.db"
NAMES = "data/stock_names.json"
LEDGER = "data/signal_ledger.db"

N_ENTER, N_HOLD, POOL = 20, 80, 300   # keep in sync with scanner/scan_mode.py
HOLD_BARS = 5
WARMUP_START = "2026-04-01"           # hysteresis warm-up; keep ~2 months before EVAL_START
EVAL_START = "2026-06-05"             # first scan day included in the report
ROUND_TRIP_COST = 0.585               # pct: fee 0.1425 x2 + tax 0.3 (reference only)


# ---------------------------------------------------------------- features
def build_features():
    con = sqlite3.connect(DB)
    df = pd.read_sql(
        "SELECT date, stock_id, open, high, low, close, Volume_Lot FROM data", con)
    con.close()
    for col in ("open", "high", "low", "close", "Volume_Lot"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = df["date"].astype(str).str[:10]
    df = df.sort_values(["stock_id", "date"])

    feats = []
    for sid, g in df.groupby("stock_id"):
        g = g.reset_index(drop=True)
        if len(g) < 70:
            continue
        c, h, l, v = g["close"], g["high"], g["low"], g["Volume_Lot"]
        prev = c.shift(1)
        upv = v.where(c > prev, 0.0)
        dnv = v.where(c < prev, 0.0)
        h52 = c.rolling(252, min_periods=63).max()
        feats.append(pd.DataFrame({
            "date": g["date"], "sid": str(sid),
            "o": g["open"], "c": c, "h": h, "l": l,
            "ma60": c.rolling(60).mean(),
            "vol_ma20": v.rolling(20).mean(),
            "ret60": c / c.shift(63) - 1,
            "ret5": c / c.shift(5) - 1,
            "rt": (h.rolling(20).max() - l.rolling(20).min())
                  / l.rolling(20).min(),
            "bias": upv.rolling(10).sum()
                    / (upv.rolling(10).sum() + dnv.rolling(10).sum()).replace(0, np.nan),
            "dist52": (h52 - c) / h52,
            "day_chg": c / prev - 1,
        }))
    return df, pd.concat(feats, ignore_index=True)


def launch_score(t):
    """SHIPPED formula -- keep in sync with scanner/scan_mode.py."""
    def c01(x, d):
        return (x / d).clip(0, 1)
    gate = ((t["c"] > t["ma60"]) & (t["vol_ma20"] > 300)).astype(float)
    mom = c01(t["ret60"].clip(lower=0), 0.5)
    young = 1.0 - c01(t["ret5"].clip(lower=0), 0.12)
    near = 1.0 - c01(t["dist52"].clip(lower=0), 0.30)
    acc = c01((t["bias"] - 0.5).clip(lower=0), 0.45)
    tight = 1.0 - c01(t["rt"].clip(lower=0), 0.25)
    return (mom * 0.30 + young * 0.25 + near * 0.20 + acc * 0.15 + tight * 0.10) * gate * 100


# ---------------------------------------------------------------- replay
def replay_selection(T):
    all_dates = sorted(T["date"].unique())
    held, streak, picks = set(), {}, []
    for d in [x for x in all_dates if x >= WARMUP_START]:
        day = T[(T["date"] == d) & (T["ls"] > 0)][["sid", "ls", "vol_ma20"]]
        if day.empty:
            held, streak = set(), {}
            continue
        pool = day.sort_values("vol_ma20", ascending=False).head(POOL)
        extra = day[day["sid"].isin(held) & ~day["sid"].isin(set(pool["sid"]))]
        pool = pd.concat([pool, extra]).sort_values(
            "ls", ascending=False).reset_index(drop=True)
        sel = []
        for rank, r in enumerate(pool.itertuples(index=False)):
            if rank < N_ENTER or (r.sid in held and rank < N_HOLD):
                sel.append((r.sid, rank, r.ls))
        new_streak = {}
        for sid, rank, ls in sel:
            new_streak[sid] = streak.get(sid, 0) + 1
            picks.append((d, sid, rank, ls, new_streak[sid]))
        held = {s for s, _, _ in sel}
        streak = new_streak
    return pd.DataFrame(picks, columns=["date", "sid", "rank", "ls", "streak"])


def sanity_vs_ledger(P):
    try:
        con = sqlite3.connect(LEDGER)
        led = pd.read_sql(
            "SELECT DISTINCT bar_date AS date, stock_id AS sid FROM picks "
            "WHERE scan_mode='mode_prelaunch'", con)
        con.close()
    except Exception:
        print("(no ledger -- sanity check skipped)")
        return
    for d in sorted(led["date"].unique()):
        a = set(led[led["date"] == d]["sid"])
        b = set(P[P["date"] == d]["sid"])
        if a and b:
            print("sanity %s: ledger=%d replay=%d overlap=%d (%.0f%%)"
                  % (d, len(a), len(b), len(a & b), 100 * len(a & b) / len(a)))


# ---------------------------------------------------------------- simulation
def make_fwd(df):
    bars = {sid: g.reset_index(drop=True)
            for sid, g in df.rename(columns={"stock_id": "sid"}).groupby("sid")}
    pos = {sid: {r: i for i, r in enumerate(g["date"])} for sid, g in bars.items()}

    def fwd(sid, d, n):
        g = bars.get(str(sid))
        if g is None:
            return None
        i = pos[str(sid)].get(d)
        if i is None or i + 1 >= len(g):
            return None
        return g.iloc[i + 1: i + 1 + n]
    return fwd


def simulate(rows, fwd, exit_mode="hold"):
    """Entry at next-day open. exit_mode: hold | stop10 (10pct intraday
    disaster stop). Requires HOLD_BARS forward bars."""
    rets = []
    for r in rows.itertuples(index=False):
        fb = fwd(r.sid, r.date, HOLD_BARS)
        if fb is None or len(fb) < HOLD_BARS:
            continue
        e = float(fb.iloc[0]["open"])
        if not np.isfinite(e) or e <= 0:
            continue
        ret = None
        if exit_mode == "stop10":
            for i in range(HOLD_BARS):
                b = fb.iloc[i]
                if float(b["low"]) <= e * 0.90:
                    px = min(float(b["open"]), e * 0.90) if i > 0 else e * 0.90
                    ret = px / e - 1
                    break
        if ret is None:
            ret = float(fb.iloc[HOLD_BARS - 1]["close"]) / e - 1
        rets.append((r.date, r.sid, ret * 100))
    return pd.DataFrame(rets, columns=["date", "sid", "ret"])


def report(label, rows, fwd, exit_mode="hold"):
    out = simulate(rows, fwd, exit_mode)
    if out.empty:
        print("%-42s n=0" % label)
        return
    byday = out.groupby("date")["ret"].mean()
    print("%-42s n=%3d (%4.1f/day, %3d sids) win%%=%3.0f mean=%+5.2f "
          "med=%+5.2f worst=%+6.1f daypos=%3.0f%%"
          % (label, len(out), len(out) / out["date"].nunique(),
             out["sid"].nunique(), 100 * (out["ret"] > 0).mean(),
             out["ret"].mean(), out["ret"].median(), out["ret"].min(),
             100 * (byday > 0).mean()))


def main():
    df, T = build_features()
    T["ls"] = launch_score(T)

    # regime from taiex.db
    tcon = sqlite3.connect(TAIEX_DB)
    tx = pd.read_sql("SELECT date, close FROM TAIEX ORDER BY date", tcon)
    tcon.close()
    tx["date"] = tx["date"].astype(str).str[:10]
    tx["risk_on"] = ((tx["close"] > tx["close"].rolling(20).mean())
                     & (tx["close"] > tx["close"].rolling(60).mean()))
    regime = tx.set_index("date")["risk_on"].to_dict()

    names = json.load(open(NAMES, encoding="utf-8"))
    market = {k: v[1] if isinstance(v, list) and len(v) > 1 else "?"
              for k, v in names.items()}

    P = replay_selection(T)
    P = P[P["date"] >= EVAL_START]
    sanity_vs_ledger(P)

    P["mkt"] = P["sid"].map(market).fillna("?")
    P["risk_on"] = P["date"].map(lambda d: regime.get(d, False))
    fwd = make_fwd(df)

    print()
    print("=== eval from %s | entry=next open | hold=%d bars | "
          "round-trip cost ~%.2f pct not deducted ===" % (EVAL_START, HOLD_BARS, ROUND_TRIP_COST))
    print()
    report("A0 all picks, hold5", P, fwd)
    report("A1 all picks + stop10", P, fwd, "stop10")
    report("B0 OTC only, hold5", P[P["mkt"] == "OTC"], fwd)
    report("B1 OTC only + stop10  <- adopted", P[P["mkt"] == "OTC"], fwd, "stop10")
    report("B2 OTC rank<20, hold5", P[(P["mkt"] == "OTC") & (P["rank"] < 20)], fwd)
    report("C0 OTC + risk_on, hold5", P[(P["mkt"] == "OTC") & P["risk_on"]], fwd)
    report("D0 TSE only, hold5 (control)", P[P["mkt"] == "TSE"], fwd)

    # whole-universe benchmark: is the edge selection or tape beta?
    print()
    print("=== benchmark: every universe stock, same entry/exit (beta control) ===")
    uni = T[T["date"].isin(sorted(P["date"].unique()))][["date", "sid"]].copy()
    uni["mkt"] = uni["sid"].map(market).fillna("?")
    for m in ("OTC", "TSE"):
        bench = simulate(uni[uni["mkt"] == m], fwd)
        pick = simulate(P[P["mkt"] == m], fwd)
        if bench.empty or pick.empty:
            continue
        print("%s universe: n=%4d mean %+5.2f win %3.0f%% | picks mean %+5.2f "
              "win %3.0f%% | ALPHA %+.2f pp"
              % (m, len(bench), bench["ret"].mean(),
                 100 * (bench["ret"] > 0).mean(), pick["ret"].mean(),
                 100 * (pick["ret"] > 0).mean(),
                 pick["ret"].mean() - bench["ret"].mean()))


if __name__ == "__main__":
    main()
