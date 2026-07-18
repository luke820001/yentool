"""
sandbox_redteam2.py

Red-team round 2: four structural-blind-spot critiques, tested on the 6y
research db where testable.

  P1  Crash sleeve (no stop, correlated basket) in a STRUCTURAL/L-shaped bear:
      the 6y deep triggers were all macro V-reverts. Show the 2022 bear
      episodes' per-name basket outcomes + worst drawdowns as the closest
      proxy to a prolonged-industry-bear, to quantify the unhedged tail.

  P2  Prelaunch exit-delay contradiction: current rule delays the day-10 exit
      while TAIEX < 20MA (cap 20). Compare vs a CONDITIONAL delay that only
      rolls when TAIEX is < 20MA AND still > 60MA (pullback, not confirmed
      bear). Also isolate the picks whose exit lands while TAIEX < 60MA.

  P3  CORE+ hard-bound to OTC: apply the SAME gate to TSE and compare
      OTC-CORE+ vs TSE-CORE+ win rate by year -- is there a TSE-led rotation
      the OTC bind misses?

Run:  python sandbox_redteam2.py
ASCII only.
"""
import json

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_winrate_round2 import sim_trail
from eval_prelaunch_overlays import winrate
from sandbox_research_replay import RESEARCH_DB, WARMUP, EVAL_FROM
from sandbox_crash_entry import load_taiex, episodes, load_basket, BASKET, COOLDOWN

STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)
BASE_HOLD = 10


def line(label, out):
    s = winrate(out)
    if not s or s["n"] == 0:
        print("  %-34s n=   0" % label)
        return
    print("  %-34s n=%4d win=%5.1f%% h1=%4.1f h2=%4.1f mean=%+6.2f worst=%+6.1f"
          % (label, s["n"], s["win"], s["h1"], s["h2"], s["mean"], out["ret"].min()))


# ---------------------------------------------------------------- P1
def p1_structural_bear(tx, bars):
    print("\n=== P1: crash sleeve in the 2022 bear (structural-bear proxy) ===")
    deep = (tx["r5"] <= -0.07) | (tx["r20"] <= -0.10)
    eps = episodes(tx, deep)
    dates = [tx["date"].iloc[i] for i in eps]
    y22 = [d for d in dates if d[:4] == "2022"]
    print("  2022 deep-trigger episodes: %s" % ", ".join(y22))
    for h in (20, 40, 60):
        allr = []
        for d in y22:
            for sid, g in bars.items():
                pos = g.index[g["date"] >= d]
                if len(pos) == 0:
                    continue
                i = pos[0] + 1
                if i + h - 1 >= len(g):
                    continue
                e = float(g.loc[i, "open"])
                if e > 0:
                    allr.append(float(g.loc[i + h - 1, "close"]) / e - 1)
        if allr:
            r = np.array(allr) * 100
            print("    2022 basket hold %2d: n=%2d win=%5.1f%% mean=%+6.2f worst=%+6.1f"
                  % (h, len(r), (r > 0).mean() * 100, r.mean(), r.min()))
    # the single worst name-episode drawdown at hold 40 across ALL 6y (already
    # in the pooled 'worst' col) -- restate the tail plainly
    print("  NOTE: even in the V-reverting 6y history, the worst single basket")
    print("        name at hold 40 was about -45%; an L-shaped bear removes the")
    print("        recovery, and there is NO 6y sample of a pure industry bust.")


# ---------------------------------------------------------------- P2
def p2_exit_delay(T, df, core, taiex_below):
    print("\n=== P2: exit-delay -- current (below20) vs conditional (below20 & above60) ===")
    below20, below60 = taiex_below
    fwd = er.make_fwd(df)

    def sim_exit(rows, mode):
        rets = []
        for r in rows.itertuples(index=False):
            fb = fwd(r.sid, r.date, 20)
            if fb is None or len(fb) < BASE_HOLD:
                continue
            e = float(fb.iloc[0]["open"])
            if not np.isfinite(e) or e <= 0:
                continue
            stop = e * (1 - 0.15)
            n = len(fb)
            idx = BASE_HOLD - 1
            if mode != "fixed":
                while idx < n - 1:
                    d = str(fb.iloc[idx]["date"])[:10]
                    dist = below20.get(d, False)
                    if mode == "cond":
                        dist = dist and not below60.get(d, False)
                    if dist:
                        idx += 1
                    else:
                        break
            ended = None
            for i in range(0, idx + 1):
                b = fb.iloc[i]
                if float(b["low"]) <= stop:
                    ended = (stop if i == 0 else min(float(b["open"]), stop)) / e - 1
                    break
            if ended is None:
                ended = float(fb.iloc[idx]["close"]) / e - 1
            rets.append((r.date, r.sid, ended * 100))
        return pd.DataFrame(rets, columns=["date", "sid", "ret"])

    line("fixed hold 10", sim_exit(core, "fixed"))
    line("delay if <20MA (current)", sim_exit(core, "cur"))
    line("delay if <20MA & >60MA (conditional)", sim_exit(core, "cond"))

    # isolate picks whose day-10 exit lands while TAIEX < 60MA (bear start)
    cal = sorted(T["date"].unique())
    idxpos = {d: i for i, d in enumerate(cal)}
    bear_mask = []
    for r in core.itertuples(index=False):
        i = idxpos.get(r.date)
        exit_d = cal[i + BASE_HOLD] if (i is not None and i + BASE_HOLD < len(cal)) else None
        bear_mask.append(bool(exit_d and below60.get(exit_d, False)))
    bear = core[pd.Series(bear_mask, index=core.index)]
    print("  -- subset: exit day lands while TAIEX < 60MA (n=%d) --" % len(bear))
    line("  bear-exit: fixed hold 10", sim_exit(bear, "fixed"))
    line("  bear-exit: delay <20MA (current)", sim_exit(bear, "cur"))
    line("  bear-exit: delay <20MA & >60MA (cond)", sim_exit(bear, "cond"))


# ---------------------------------------------------------------- P3
def p3_otc_vs_tse(P, fwd):
    print("\n=== P3: CORE+ gate on OTC vs TSE (rotation blind spot) ===")
    gate = ((P["rank"] < 20) & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05))
    for mk in ("OTC", "TSE"):
        sub = P[(P["mkt"] == mk) & P["ro"] & gate]
        out = sim_trail(sub, fwd, **STACK)
        line("%s CORE+ full stack" % mk, out)
    print("  by year (win%% | mean):")
    for mk in ("OTC", "TSE"):
        sub = P[(P["mkt"] == mk) & P["ro"] & gate]
        out = sim_trail(sub, fwd, **STACK)
        if out.empty:
            continue
        cells = []
        for y, g in out.groupby(out["date"].str[:4]):
            r = g["ret"].to_numpy()
            cells.append("%s:%2.0f%%/%+4.1f(n%d)" % (y, (r > 0).mean() * 100, r.mean(), len(r)))
        print("    %-4s %s" % (mk, "  ".join(cells)))


def main():
    tx = load_taiex()
    bars = load_basket()
    p1_structural_bear(tx, bars)

    er.DB = RESEARCH_DB
    er.WARMUP_START = WARMUP
    print("\nbuilding features from %s (be patient)..." % RESEARCH_DB)
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)

    from sandbox_research_replay import research_regime
    reg = research_regime()
    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = er.replay_selection(T)
    P = P[P["date"] >= EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")

    # TAIEX MA flags from the 6y cache (research_taiex.csv via load_taiex has r*,
    # recompute MA flags here)
    c = tx["close"]
    b20 = (c < c.rolling(20).mean())
    b60 = (c < c.rolling(60).mean())
    below20 = dict(zip(tx["date"], b20))
    below60 = dict(zip(tx["date"], b60))

    core = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)
             & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)].copy()
    p2_exit_delay(T, df, core, (below20, below60))
    p3_otc_vs_tse(P, fwd)


if __name__ == "__main__":
    main()
