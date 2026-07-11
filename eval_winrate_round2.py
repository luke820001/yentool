"""
eval_winrate_round2.py

Round-2 win-rate search, run AFTER the CORE+ adoption (EVAL_PLAYBOOK section 9).
Base rule under test = OTC + risk_on + rank<20 + CORE+ (dist52<=5%, ret5<=5%)
+ hold 10, win 62.2% on the full replay. Dimensions this round, none of which
round 1 touched:

  A. regime source: TAIEX gate vs OWN-UNIVERSE OTC breadth (pct of OTC names
     above their 60MA) -- the strategy trades OTC but gates on TAIEX today.
  B. hold length re-tuned ON CORE+ (10 was tuned before CORE+ existed).
  C. trailing profit lock: once up arm%, raise the stop to lock% -- converts
     gave-it-all-back losers into small wins (win-rate mechanic, untested).
  D. extra entry filters: 3-month momentum band, MA20 proximity, price floor,
     rank<10 on CORE+.
  E. Launch_Score weight variants (re-replay the whole selection per variant).

Guards as always: win/bootLo/h1/h2 vs the CORE+ base, adopt only full sweeps.
Run:  python eval_winrate_round2.py
ASCII only.
"""
import json

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import regime_map, winrate
from eval_winrate_search import FULL_WARMUP, FULL_START, show

HOLD = 10


def sim_trail(rows, fwd, hold=HOLD, stop=0.15, tp=None, arm=None, lock=0.0):
    """Entry next open; disaster stop below; optional tp above; optional
    trailing lock: once high >= e*(1+arm), stop rises to e*(1+lock)."""
    rets = []
    for r in rows.itertuples(index=False):
        fb = fwd(r.sid, r.date, hold)
        if fb is None or len(fb) < hold:
            continue
        e = float(fb.iloc[0]["open"])
        if not np.isfinite(e) or e <= 0:
            continue
        stop_px = e * (1 - stop) if stop is not None else 0.0
        armed = False
        ret = None
        for i in range(hold):
            b = fb.iloc[i]
            lo, hi, op = float(b["low"]), float(b["high"]), float(b["open"])
            # arm the lock on a prior bar's high only from the NEXT bar on;
            # same-bar high->low ordering is unknowable, so be conservative:
            # a bar that both arms and breaches counts as a breach at lock px
            if stop_px and lo <= stop_px:
                px = min(op, stop_px) if i > 0 else stop_px
                ret = px / e - 1
                break
            if tp is not None and hi >= e * (1 + tp):
                px = max(op, e * (1 + tp)) if i > 0 else e * (1 + tp)
                ret = px / e - 1
                break
            if arm is not None and not armed and hi >= e * (1 + arm):
                armed = True
                stop_px = max(stop_px, e * (1 + lock))
        if ret is None:
            ret = float(fb.iloc[hold - 1]["close"]) / e - 1
        rets.append((r.date, r.sid, ret * 100))
    return pd.DataFrame(rets, columns=["date", "sid", "ret"])


def line(label, out):
    s = winrate(out)
    if not s or s["n"] == 0:
        print("%-42s n=0" % label)
        return None
    print("%-42s n=%4d win=%4.1f%% bootLo=%4.1f h1=%4.1f h2=%4.1f mean=%+5.2f worst=%+6.1f"
          % (label, s["n"], s["win"], s["bootLo"], s["h1"], s["h2"], s["mean"],
             out["ret"].min()))
    return s


def launch_score_w(t, w):
    """er.launch_score with parameterized weights (mom, young, near, acc, tight)."""
    def c01(x, d):
        return (x / d).clip(0, 1)
    gate = ((t["c"] > t["ma60"]) & (t["turn20"] >= 1.0e8)).astype(float)
    mom = c01(t["ret60"].clip(lower=0), 0.5)
    young = 1.0 - c01(t["ret5"].clip(lower=0), 0.12)
    near = 1.0 - c01(t["dist52"].clip(lower=0), 0.30)
    acc = c01((t["bias"] - 0.5).clip(lower=0), 0.45)
    tight = 1.0 - c01(t["rt"].clip(lower=0), 0.25)
    return (mom * w[0] + young * w[1] + near * w[2]
            + acc * w[3] + tight * w[4]) * gate * 100


def replay_pick_core(T, market, reg, fwd, extra_ls=None):
    """Replay selection (optionally with a custom ls) and return the CORE+ set."""
    if extra_ls is not None:
        T = T.copy()
        T["ls"] = extra_ls
    P = er.replay_selection(T)
    P = P[P["date"] >= FULL_START].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    feat = T[["date", "sid", "c", "ret5", "dist52", "ret60", "ma60"]].rename(
        columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")
    base = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)]
    return P, base[(base["dist52"] <= 0.05) & (base["ret5"] <= 0.05)]


def main():
    df, T = er.build_features()
    # ma20 for proximity filter
    grp = df.sort_values(["stock_id", "date"]).groupby("stock_id")
    d2 = df.sort_values(["stock_id", "date"]).copy()
    d2["ma20"] = grp["close"].transform(lambda s: s.rolling(20).mean())
    T = T.merge(d2.rename(columns={"stock_id": "sid"})[["date", "sid", "ma20"]],
                on=["date", "sid"], how="left")
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = regime_map()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    er.WARMUP_START = FULL_WARMUP

    # OTC breadth per date: pct of OTC names above their 60MA
    Tm = T.copy()
    Tm["mkt"] = Tm["sid"].map(market).fillna("?")
    otc_rows = Tm[(Tm["mkt"] == "OTC") & Tm["ma60"].notna()]
    breadth = (otc_rows["c"] > otc_rows["ma60"]).groupby(otc_rows["date"]).mean()
    breadth = breadth.to_dict()

    P, core = replay_pick_core(T, market, reg, fwd)
    core = core.merge(
        T[["date", "sid", "ma20"]], on=["date", "sid"], how="left")
    core["breadth"] = core["date"].map(lambda d: breadth.get(d, np.nan))

    # also need the un-gated (no risk_on) CORE+ to test regime replacements
    allro = P[(P["mkt"] == "OTC") & (P["rank"] < 20)]
    allcore = allro[(allro["dist52"] <= 0.05) & (allro["ret5"] <= 0.05)].copy()
    allcore["breadth"] = allcore["date"].map(lambda d: breadth.get(d, np.nan))

    print("=== A. regime source (CORE+ without TAIEX gate as raw material) ===")
    line("CORE+ w/ TAIEX gate (ADOPTED base)", sim_trail(core, fwd, stop=None))
    line("CORE+ no regime gate", sim_trail(allcore, fwd, stop=None))
    for th in (0.4, 0.5, 0.6):
        m = allcore["breadth"] >= th
        line("CORE+ breadth>=%.1f (replaces TAIEX)" % th,
             sim_trail(allcore[m], fwd, stop=None))
    m = core["breadth"] >= 0.5
    line("CORE+ TAIEX AND breadth>=0.5", sim_trail(core[m], fwd, stop=None))

    print("\n=== B. hold length on CORE+ (plain, no stop/tp) ===")
    for h in (5, 8, 10, 12, 15):
        line("hold %d" % h, sim_trail(core, fwd, hold=h, stop=None))

    print("\n=== C. trailing profit lock on CORE+ hold10 (disaster stop 15) ===")
    line("stop15 only (ref)", sim_trail(core, fwd, stop=0.15))
    for arm in (0.08, 0.10, 0.12):
        for lock in (0.0, 0.02):
            line("trail arm%d lock%+d" % (arm * 100, lock * 100),
                 sim_trail(core, fwd, stop=0.15, arm=arm, lock=lock))
    line("tp20+stop15 (adopted)", sim_trail(core, fwd, stop=0.15, tp=0.20))
    line("tp20+stop15+trail a10 l+2",
         sim_trail(core, fwd, stop=0.15, tp=0.20, arm=0.10, lock=0.02))

    print("\n=== D. extra entry filters on CORE+ (plain hold10) ===")
    filts = [
        ("ret60 in [0.10,0.50]", (core["ret60"] >= 0.10) & (core["ret60"] <= 0.50)),
        ("ret60 <= 0.50",        core["ret60"] <= 0.50),
        ("ret60 >= 0.10",        core["ret60"] >= 0.10),
        ("close within 5% of MA20",
         (core["sig_close"] / core["ma20"] - 1).abs() <= 0.05),
        ("close within 8% of MA20",
         (core["sig_close"] / core["ma20"] - 1).abs() <= 0.08),
        ("price >= 20",          core["sig_close"] >= 20),
        ("price >= 50",          core["sig_close"] >= 50),
        ("rank < 10",            core["rank"] < 10),
    ]
    for lbl, m in filts:
        line(lbl, sim_trail(core[m], fwd, stop=None))

    print("\n=== E. Launch_Score weight variants (full re-replay each) ===")
    weights = {
        "current (.30/.25/.20/.15/.10)": None,
        "near-heavy (.25/.25/.30/.15/.05)": (0.25, 0.25, 0.30, 0.15, 0.05),
        "acc-heavy  (.25/.20/.20/.30/.05)": (0.25, 0.20, 0.20, 0.30, 0.05),
        "young-heavy(.20/.35/.25/.15/.05)": (0.20, 0.35, 0.25, 0.15, 0.05),
        "mom-light  (.15/.30/.25/.20/.10)": (0.15, 0.30, 0.25, 0.20, 0.10),
    }
    for lbl, w in weights.items():
        if w is None:
            line(lbl, sim_trail(core, fwd, stop=None))
            continue
        _, c2 = replay_pick_core(T, market, reg, fwd,
                                 extra_ls=launch_score_w(T, w))
        line(lbl, sim_trail(c2, fwd, stop=None))

    print("\nADOPT ONLY IF: win/bootLo/h1/h2 all clear the adopted CORE+ base.")


if __name__ == "__main__":
    main()
