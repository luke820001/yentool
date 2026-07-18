"""
sandbox_redteam3.py

Red-team round 3: four testable critiques.

  P1  Static +2% lock vs a DYNAMIC chandelier trail (running_high - k*ATR) once
      armed at +6%. Does an ATR-scaled trail let winners run further (higher
      mean) without wrecking the win rate the static lock buys?

  P2  TAIEX (TSMC-dominated) is the wrong gate for OTC small-caps. Build an
      equal-weight OTC composite index from the research db, gate on ITS
      20/60MA, and compare CORE+ win/mean vs the TAIEX gate. Also the AND-gate
      (both above 60MA).

  P3  Crash-sleeve "first up-day" definition: red-K (close>open) vs close>prev
      close (implemented) vs close>prev-low (engulfing). Quantify + fix the doc.

  P4  Rigid day-10 exit cuts right-side momentum. Add a stock-strength delay:
      if the name still closes above its own 5MA/10MA at day 10, hold until it
      breaks that MA (cap 20/30), keeping stop/tp/trail. Win, mean, worst.

Run:  python sandbox_redteam3.py [eval_from] [warmup]
ASCII only.
"""
import json
import sys

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import winrate
from eval_winrate_round2 import sim_trail
from sandbox_research_replay import RESEARCH_DB, WARMUP, EVAL_FROM
from sandbox_crash_entry import load_taiex, episodes, load_basket

STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)
BASE_HOLD = 10


def line(label, out):
    s = winrate(out)
    if not s or s["n"] == 0:
        print("  %-40s n=   0" % label)
        return
    print("  %-40s n=%4d win=%5.1f%% h1=%4.1f h2=%4.1f mean=%+6.2f worst=%+6.1f"
          % (label, s["n"], s["win"], s["h1"], s["h2"], s["mean"], out["ret"].min()))


# ---------------------------------------------------------------- P3 (fast, TAIEX only)
def _taiex_ohlc():
    """Fresh ^TWII OHLC (the cache is close-only). Falls back to None on failure."""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import yfinance as yf
            raw = yf.download("^TWII", start="2019-10-01", auto_adjust=True,
                              progress=False)
        raw = raw.reset_index()
        raw.columns = [str(c[0]).lower() if isinstance(c, tuple) else str(c).lower()
                       for c in raw.columns]
        raw["date"] = pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d")
        raw = raw[raw["close"] > 0].reset_index(drop=True)
        return raw
    except Exception as e:
        print("  (P3 skipped: yfinance OHLC fetch failed: %s)" % str(e)[:60])
        return None


def p3_confirm_defs(tx_unused, bars):
    print("\n=== P3: crash-sleeve entry-confirm definition (day<=-3% episodes, hold 20) ===")
    tx = _taiex_ohlc()
    if tx is None:
        return
    tx["r1"] = tx["close"] / tx["close"].shift(1) - 1
    eps = episodes(tx.rename(columns={}), tx["r1"] <= -0.03)
    o = tx["open"].to_numpy()
    c = tx["close"].to_numpy()
    low = tx["low"].to_numpy()
    dates = tx["date"].tolist()

    def first_bar(i, kind):
        for j in range(i + 1, min(i + 15, len(c))):
            if kind == "redK" and o is not None and c[j] > o[j]:
                return j
            if kind == "gt_prevclose" and c[j] > c[j - 1]:
                return j
            if kind == "gt_prevlow" and low is not None and c[j] > low[j - 1] and c[j] > o[j]:
                return j
        return None

    from sandbox_crash_entry import basket_stats
    for kind, lbl in (("redK", "red-K (close>open)  [doc wording]"),
                      ("gt_prevclose", "close>prev-close    [implemented]"),
                      ("gt_prevlow", "engulf low & red    [strictest]")):
        ds = [dates[first_bar(i, kind)] for i in eps if first_bar(i, kind) is not None]
        rows = basket_stats(bars, ds, holds=(20,))
        if rows:
            h, n, win, mean, worst = rows[0]
            print("  %-36s n=%3d win=%5.1f%% mean=%+6.2f worst=%+6.1f"
                  % (lbl, n, win, mean, worst))


# ---------------------------------------------------------------- shared heavy build
def build():
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    return df, T


def otc_composite(df, market):
    """Equal-weight OTC index level + its 20/60MA regime flag by date."""
    d = df.copy()
    d["mkt"] = d["stock_id"].astype(str).map(market)
    d = d[d["mkt"] == "OTC"].sort_values(["stock_id", "date"])
    d["r"] = d.groupby("stock_id")["close"].transform(lambda s: s / s.shift(1) - 1)
    daily = d.groupby("date")["r"].mean().dropna()
    lvl = (1 + daily).cumprod()
    ma20 = lvl.rolling(20).mean()
    ma60 = lvl.rolling(60).mean()
    ro = ((lvl > ma20) & (lvl > ma60))
    return ro.to_dict()


def ma_maps(df):
    d = df.rename(columns={"stock_id": "sid"}).sort_values(["sid", "date"])
    g = d.groupby("sid")["close"]
    ma5 = g.transform(lambda s: s.rolling(5).mean())
    ma10 = g.transform(lambda s: s.rolling(10).mean())
    keys = list(zip(d["sid"].astype(str), d["date"]))
    return dict(zip(keys, ma5)), dict(zip(keys, ma10))


def atr_map(df):
    d = df.rename(columns={"stock_id": "sid"}).sort_values(["sid", "date"])
    atr = ((d["high"] - d["low"]) / d["close"]).groupby(d["sid"]).transform(
        lambda s: s.rolling(20).mean())
    return dict(zip(zip(d["sid"].astype(str), d["date"]), atr))


# ---------------------------------------------------------------- P1
def sim_chandelier(rows, fwd, atrf, k, hold=BASE_HOLD, stop=0.15, tp=0.20, arm=0.06):
    """Once armed at +arm, stop = running_high*(1 - k*atr_frac). No lookahead:
    running_high updated AFTER the stop check each bar."""
    rets = []
    for r in rows.itertuples(index=False):
        fb = fwd(r.sid, r.date, hold)
        if fb is None or len(fb) < hold:
            continue
        e = float(fb.iloc[0]["open"])
        if not np.isfinite(e) or e <= 0:
            continue
        af = atrf.get((str(r.sid), r.date))
        if af is None or not np.isfinite(af) or af <= 0:
            continue
        stop_px = e * (1 - stop)
        run_hi = e
        armed = False
        ret = None
        for i in range(hold):
            b = fb.iloc[i]
            lo, hi, op = float(b["low"]), float(b["high"]), float(b["open"])
            if armed:
                stop_px = max(stop_px, run_hi * (1 - k * af))
            if lo <= stop_px:
                ret = (min(op, stop_px) if i > 0 else stop_px) / e - 1
                break
            if hi >= e * (1 + tp):
                ret = (max(op, e * (1 + tp)) if i > 0 else e * (1 + tp)) / e - 1
                break
            if hi >= e * (1 + arm):
                armed = True
            run_hi = max(run_hi, hi)
        if ret is None:
            ret = float(fb.iloc[hold - 1]["close"]) / e - 1
        rets.append((r.date, r.sid, ret * 100))
    return pd.DataFrame(rets, columns=["date", "sid", "ret"])


# ---------------------------------------------------------------- P4
def sim_strength_delay(rows, fwd, mamap, cap, hold=BASE_HOLD,
                       stop=0.15, tp=0.20, arm=0.06, lock=0.02):
    """Adopted stack, but at day `hold` if close still > its own MA, keep
    holding (checking stop/tp/trail each bar) until close < MA or bar `cap`."""
    rets = []
    for r in rows.itertuples(index=False):
        fb = fwd(r.sid, r.date, cap)
        if fb is None or len(fb) < hold:
            continue
        e = float(fb.iloc[0]["open"])
        if not np.isfinite(e) or e <= 0:
            continue
        stop_px = e * (1 - stop)
        armed = False
        ret = None
        n = len(fb)
        for i in range(n):
            b = fb.iloc[i]
            lo, hi, op, cl = float(b["low"]), float(b["high"]), float(b["open"]), float(b["close"])
            if stop_px and lo <= stop_px:
                ret = (min(op, stop_px) if i > 0 else stop_px) / e - 1
                break
            if hi >= e * (1 + tp):
                ret = (max(op, e * (1 + tp)) if i > 0 else e * (1 + tp)) / e - 1
                break
            if arm is not None and not armed and hi >= e * (1 + arm):
                armed = True
                stop_px = max(stop_px, e * (1 + lock))
            # exit decision at/after base hold
            if i >= hold - 1:
                d = str(b["date"])[:10]
                ma = mamap.get((str(r.sid), d))
                strong = ma is not None and np.isfinite(ma) and cl > float(ma)
                if not strong:
                    ret = cl / e - 1
                    break
                if i == n - 1:
                    ret = cl / e - 1
                    break
        if ret is None:
            ret = float(fb.iloc[min(hold, n) - 1]["close"]) / e - 1
        rets.append((r.date, r.sid, ret * 100))
    return pd.DataFrame(rets, columns=["date", "sid", "ret"])


def main():
    eval_from = sys.argv[1] if len(sys.argv) > 1 else EVAL_FROM
    warmup = sys.argv[2] if len(sys.argv) > 2 else WARMUP

    tx = load_taiex()
    bars = load_basket()
    p3_confirm_defs(tx, bars)

    er.DB = RESEARCH_DB
    er.WARMUP_START = warmup
    print("\nbuilding features from %s (be patient)..." % RESEARCH_DB)
    df, T = build()
    fwd = er.make_fwd(df)

    from sandbox_research_replay import research_regime
    reg = research_regime()
    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = er.replay_selection(T)
    P = P[P["date"] >= eval_from].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")
    gate = (P["rank"] < 20) & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)

    core = P[(P["mkt"] == "OTC") & P["ro"] & gate].copy()

    # P1 -------------------------------------------------------------
    print("\n=== P1: static +2%% lock vs chandelier (run_hi - k*ATR), CORE+ ===")
    atrf = atr_map(df)
    line("adopted static lock (+6%->+2%)", sim_trail(core, fwd, **STACK))
    for k in (1.0, 1.5, 2.0, 2.5, 3.0):
        line("chandelier k=%.1f*ATR" % k, sim_chandelier(core, fwd, atrf, k))

    # P2 -------------------------------------------------------------
    print("\n=== P2: OTC gate source -- TAIEX vs OTC-composite vs both ===")
    ro_otc = otc_composite(df, market)
    P["ro_otc"] = P["date"].map(lambda d: ro_otc.get(d, False))
    core_taiex = P[(P["mkt"] == "OTC") & P["ro"] & gate]
    core_otc = P[(P["mkt"] == "OTC") & P["ro_otc"] & gate]
    core_both = P[(P["mkt"] == "OTC") & P["ro"] & P["ro_otc"] & gate]
    line("gate=TAIEX (current)", sim_trail(core_taiex, fwd, **STACK))
    line("gate=OTC-composite", sim_trail(core_otc, fwd, **STACK))
    line("gate=BOTH >60/20MA", sim_trail(core_both, fwd, **STACK))
    print("  picks/day: TAIEX %.1f  OTC-comp %.1f  BOTH %.1f"
          % (core_taiex.groupby("date").size().mean() if len(core_taiex) else 0,
             core_otc.groupby("date").size().mean() if len(core_otc) else 0,
             core_both.groupby("date").size().mean() if len(core_both) else 0))

    # P4 -------------------------------------------------------------
    print("\n=== P4: rigid day-10 vs stock-strength delay (hold while > own MA) ===")
    ma5, ma10 = ma_maps(df)
    line("adopted hard hold 10 (full stack)", sim_trail(core, fwd, **STACK))
    line("delay while >5MA, cap 20", sim_strength_delay(core, fwd, ma5, 20))
    line("delay while >5MA, cap 30", sim_strength_delay(core, fwd, ma5, 30))
    line("delay while >10MA, cap 20", sim_strength_delay(core, fwd, ma10, 20))
    line("delay while >10MA, cap 30", sim_strength_delay(core, fwd, ma10, 30))


if __name__ == "__main__":
    main()
