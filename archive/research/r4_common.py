"""
r4_common.py -- the shared round-4 research harness (2026-10-08). ASCII only.

Every round-4 study (archive/research/sandbox_r4_*.py) imports this file, so
that all of them measure the SAME thing:

  * the shipped exit rule, and only it: sandbox_money.BASE, which is built
    from scanner.exit_rules.DEFAULT_RULE (six legs + the ma5 ride, cap 20).
    No exit threshold is written in this file;
  * the slippage stress (stop/lock exits re-priced SLIP worse);
  * two windows: REC = signal day >= RECENT_FROM (2023-09-18), OLD = before;
  * the adoption gates of docs/BACKTEST_LOG.md (seven gates, the K.2 money
    gates, the I.4 same-count random-deletion control, EV per baseline
    opportunity, the J.7 zero-month / longest-gap / slots view).

Signal sets ("universes"):
  research  the 556 CORE+ first-day OTC risk-on trades of sandbox_entry_gate
            (universe.pkl). Pool = ls > 0 first, then the top 300 by 20-day
            average turnover (the research replay of eval_realtrade).
  live      the live-frequency set of BACKTEST_LOG section L fix-1: pool =
            same-day turnover >= min_turnover, top `cap` by same-day turnover
            over the whole snapshot (scanner/market_filter.py _MODE_CFG
            mode_prelaunch), held names force-included, then ls > 0 and the
            ls ranking with the N_ENTER/N_HOLD hysteresis; first day = absent
            the previous session (streak == 1); then OTC, risk_on, CORE+.

Caches (all outside git):
  CACHE        = $ROUND4_CACHE or %TEMP%/yentool_round4
  GATE_DIR     = $GATE_CACHE or CACHE/gate   (universe.pkl = research set)
  CACHE/sel_features.pkl      slim ls>0 feature table + date list (one build)
  CACHE/sel_P_<kind>_n<N>.pkl full selection table (every streak) per pool
  CACHE/universe_<kind>_n<N>.pkl trade dicts (same format as universe.pkl)

Commands:
  python archive/research/r4_common.py build     # features + both pools (~4 min)
  python archive/research/r4_common.py parity    # rebuilt research == universe.pkl
  python archive/research/r4_common.py base      # baselines, both universes
  python archive/research/r4_common.py k1        # K.1 slots, loose vs strict
Run with PYTHONDONTWRITEBYTECODE=1.
"""
import os
import pickle
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

CACHE = os.environ.get("ROUND4_CACHE", os.path.join(
    os.environ.get("TEMP", "."), "yentool_round4"))
GATE_DIR = os.environ.get("GATE_CACHE") or os.path.join(CACHE, "gate")
os.environ.setdefault("GATE_CACHE", GATE_DIR)

import sandbox_entry_gate as g                     # noqa: E402
import sandbox_money as m                          # noqa: E402
from scanner.exit_rules import DEFAULT_RULE as R   # noqa: E402

# sandbox_entry_gate read GATE_CACHE when it was first imported; pin it here
# so a study that imported it earlier still reads the same universe.
g.CACHE_DIR = GATE_DIR
g.PICKS_PKL = os.path.join(GATE_DIR, "universe.pkl")

RESEARCH_DB = g.RESEARCH_DB
RECENT_FROM = g.RECENT_FROM
SLIP = 0.005
CTRL_ITERS = 300
SEED = m.SEED
WINDOWS = ("REC", "OLD")
SLOTS_K = (1, 2, 3, 5, 8)
CAL_MIN_SIDS = 300          # a session = at least this many sids traded
MIN_BUCKET = 30             # gate 7: a removed bucket smaller than this is noise
MONTH_SPAN = ("2020-01", "2026-08")   # risk_on starts 2020-01; data ends 2026-09-09,
                                      # so 2026-08 is the last month that can hold a signal
UNGATED = dict(g.BASE_GATE, dist52=None, ret5=None, atr=None)

BASE = m.BASE               # the shipped rule; never a hand-written plan


def _say(*a):
    print(*a)
    sys.stdout.flush()


# =============================================================== universes
def _universe_path(kind, n_enter=None):
    import eval_realtrade as er
    n = er.N_ENTER if n_enter is None else n_enter
    return os.path.join(CACHE, "universe_%s_n%d.pkl" % (kind, n))


def ensure_universe():
    """The research universe.pkl: reuse GATE_DIR if present, else build it
    (fast builder below, verified identical to sandbox_entry_gate.build)."""
    if os.path.exists(g.PICKS_PKL):
        return g.PICKS_PKL
    t0 = time.time()
    _say("no universe.pkl under %s -- building" % GATE_DIR)
    build_selection(kinds=("research",))
    _say("built in %.0fs" % (time.time() - t0))
    return g.PICKS_PKL


def load_universe(kind="research", n_enter=None):
    """All cached trade dicts (rank < RANK_POOL, OTC, risk_on, first day)."""
    if kind == "research" and n_enter is None:
        ensure_universe()
        return g.load()
    path = _universe_path(kind, n_enter)
    if not os.path.exists(path):
        raise FileNotFoundError("%s missing: run `r4_common.py build`" % path)
    return pickle.load(open(path, "rb"))


def trades(kind="research", gate="core", n_enter=None):
    """Trade dicts of a universe after the entry gate.
    gate: 'core' (CORE+ as shipped), 'ungated' (CORE+ quality legs removed,
    rank cut kept -- the J.7 ungated control) or an explicit gate dict."""
    gd = {"core": g.BASE_GATE, "ungated": UNGATED}.get(gate, gate)
    if n_enter is not None and gd.get("rank") is not None:
        gd = dict(gd, rank=max(gd["rank"], n_enter))
    return [t for t in load_universe(kind, n_enter) if g.passes(t, gd)]


# ================================================================= replay
def at_open(t, why, bar):
    """True when the exit filled AT THE OPEN of its bar (the only fills that
    can fund a same-morning entry): 'late' always; tp/stop/lock when the bar
    gapped through the level. Levels are the shipped rule's (BASE)."""
    if why == "late":
        return True
    if why not in ("tp", "stop", "lock") or bar >= len(t["o"]):
        return False
    E = float(t["o"][0])
    op = float(t["o"][bar])
    if why == "tp":
        return op >= E * (1 + BASE["tp"])
    if why == "stop":
        return op <= E * (1 - BASE["stop"])
    # an armed stop sits at max(stop level, lock level) = the lock level
    return op <= max(E * (1 - BASE["stop"]), E * (1 + BASE["lock"]))


def replay(t, slip=0.0):
    """One trade under the shipped rule (sandbox_money.replay with BASE),
    plus exit_bar, at_open and the window label."""
    r = m.replay(t, BASE, slip)
    bar = int(r["bars"]) - 1
    r["exit_bar"] = bar
    r["at_open"] = at_open(t, r["why"], bar)
    r["window"] = "REC" if r["sig"] >= RECENT_FROM else "OLD"
    return r


def evaluate(ts, slip=0.0):
    return pd.DataFrame([replay(t, slip) for t in ts])


_BASE_MEMO = {}


def base(kind="research", gate="core", n_enter=None):
    """(trades, df at slip 0, df at SLIP), memoised per process. The frames
    are row-aligned: row i is the same trade in both."""
    key = (kind, gate if isinstance(gate, str) else repr(sorted(gate.items())), n_enter)
    if key not in _BASE_MEMO:
        ts = trades(kind, gate, n_enter)
        _BASE_MEMO[key] = (ts, evaluate(ts), evaluate(ts, SLIP))
    return _BASE_MEMO[key]


# ================================================================ scoring
def split(df):
    return df[df["sig"] >= RECENT_FROM], df[df["sig"] < RECENT_FROM]


def wmask(df, w):
    s = df["sig"].to_numpy()
    return s >= RECENT_FROM if w == "REC" else s < RECENT_FROM


def stats(df):
    if not len(df):
        return dict(n=0, win=np.nan, mean=np.nan, sum=0.0)
    r = df["ret"].to_numpy(float)
    return dict(n=len(r), win=100 * (r > 0).mean(), mean=r.mean(), sum=r.sum())


def boot_lo(df, what="win", iters=1200, seed=SEED):
    """2.5th percentile of win or mean under a by-signal-day bootstrap."""
    return m.boot(df, what, iters, seed)


def halves(bw, keep=None):
    """Stats of the first and second half of a window, halves cut on the
    BASE window's signal order (as sandbox_money.report does), so a filter's
    kept trades are compared with the base trades of the same dates.
    bw: one window of the base frame; keep: bool mask aligned to bw."""
    order = np.argsort(bw["sig"].to_numpy(), kind="mergesort")
    h = len(bw) // 2
    keep = np.ones(len(bw), bool) if keep is None else np.asarray(keep, bool)
    out = []
    for part in (order[:h], order[h:]):
        sel = part[keep[part]]
        out.append(stats(bw.iloc[sel]))
    return out


def _qkey(df):
    s = df["sig"]
    return s.str.slice(0, 4) + "Q" + ((s.str.slice(5, 7).astype(int) - 1) // 3 + 1).astype(str)


def quarters(kept, bw):
    """Quarters where the kept set's win (and mean) >= the base window's.
    Returns dict(win=(ok, n), mean=(ok, n))."""
    if not len(kept):
        return dict(win=(0, 0), mean=(0, 0))
    def q(df):
        r = df["ret"].to_numpy(float)
        x = pd.DataFrame({"q": _qkey(df).to_numpy(), "r": r, "w": (r > 0) * 100.0})
        return x.groupby("q")[["r", "w"]].mean()
    qk, qb = q(kept), q(bw)
    idx = qb.index.intersection(qk.index)
    win_ok = int((qk.loc[idx, "w"] >= qb.loc[idx, "w"] - 1e-9).sum())
    mean_ok = int((qk.loc[idx, "r"] >= qb.loc[idx, "r"] - 1e-9).sum())
    return dict(win=(win_ok, len(idx)), mean=(mean_ok, len(idx)))


def diff_ci(bw, keep, iters=1500, seed=SEED):
    """By-signal-day bootstrap 95% CI of mean(kept) - mean(base) inside one
    window: the filter version of sandbox_money.paired_dmean."""
    keep = np.asarray(keep, bool)
    r = bw["ret"].to_numpy(float)
    keys = bw["sig"].to_numpy()
    uk = np.unique(keys)
    groups = [(r[keys == k], keep[keys == k]) for k in uk]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        rr = np.concatenate([groups[i][0] for i in pick])
        kk = np.concatenate([groups[i][1] for i in pick])
        if kk.sum() == 0:
            continue
        out.append(rr[kk].mean() - rr.mean())
    if not out:
        return (np.nan, np.nan)
    return np.percentile(out, 2.5), np.percentile(out, 97.5)


def paired_dmean(base_df, cand_df, iters=1500, seed=SEED):
    """Same trades, different exits: CI of the paired mean difference."""
    return m.paired_dmean(base_df, cand_df, iters, seed)


def random_deletion_control(base_df, keep, iters=CTRL_ITERS, seed=SEED):
    """BACKTEST_LOG I.4: within each window draw `iters` random subsets of
    the base trades with the same count as the kept set; report where the
    kept set's win and mean fall (mid-rank percentile). A filter passes only
    at >= 95 for BOTH win and mean in BOTH windows."""
    keep = np.asarray(keep, bool)
    out = {}
    for w in WINDOWS:
        wm = wmask(base_df, w)
        r = base_df["ret"].to_numpy(float)[wm]
        k = keep[wm]
        nk = int(k.sum())
        res = dict(n_base=len(r), n_kept=nk)
        if nk == 0 or nk == len(r):
            res.update(win=np.nan, mean=np.nan, pct_win=np.nan, pct_mean=np.nan,
                       passed=False, degenerate=True)
            out[w] = res
            continue
        rng = np.random.default_rng(seed)
        sw, sm = np.empty(iters), np.empty(iters)
        for i in range(iters):
            idx = rng.choice(len(r), nk, replace=False)
            sw[i] = 100 * (r[idx] > 0).mean()
            sm[i] = r[idx].mean()
        kw, km = 100 * (r[k] > 0).mean(), r[k].mean()

        def pct(sims, v):
            return 100 * ((sims < v - 1e-12).sum() + 0.5 * (np.abs(sims - v) <= 1e-12).sum()) / len(sims)
        res.update(win=kw, mean=km, pct_win=pct(sw, kw), pct_mean=pct(sm, km),
                   rnd_win=(np.percentile(sw, 5), np.percentile(sw, 50), np.percentile(sw, 95)),
                   rnd_mean=(np.percentile(sm, 5), np.percentile(sm, 50), np.percentile(sm, 95)),
                   degenerate=False)
        res["passed"] = res["pct_win"] >= 95 and res["pct_mean"] >= 95
        out[w] = res
    out["passed"] = all(out[w]["passed"] for w in WINDOWS)
    return out


def ev_per_opportunity(base_df, keep):
    """Kept return sum / base trade count, per window (BACKTEST_LOG I.4):
    what each baseline signal is worth once the filter has run."""
    keep = np.asarray(keep, bool)
    out = {}
    for w in WINDOWS:
        wm = wmask(base_df, w)
        r = base_df["ret"].to_numpy(float)[wm]
        out[w] = dict(base=r.mean() if len(r) else np.nan,
                      kept=r[keep[wm]].sum() / len(r) if len(r) else np.nan)
    return out


def plateau(params, rec, old, rec_base, old_base, higher=True):
    """J.7 plateau check with both windows overlaid.
    params: ordered parameter values; rec/old: the metric per value;
    *_base: the base metric. Returns dict: beats (both windows) per value,
    the longest run of consecutive values beating base in both windows,
    each window's argbest, whether they agree, whether either best sits at
    an end of the grid (the 'rule switched off' end), and a one-line text."""
    sgn = 1 if higher else -1
    rec = np.asarray(rec, float)
    old = np.asarray(old, float)
    beats = [(sgn * (a - rec_base) >= 0) and (sgn * (b - old_base) >= 0)
             for a, b in zip(rec, old)]
    run, best_run, start, best_start = 0, 0, 0, 0
    for i, ok in enumerate(beats):
        if ok:
            if run == 0:
                start = i
            run += 1
            if run > best_run:
                best_run, best_start = run, start
        else:
            run = 0
    ar = int(np.nanargmax(sgn * rec))
    ao = int(np.nanargmax(sgn * old))
    edge = ar in (0, len(params) - 1) or ao in (0, len(params) - 1)
    run_vals = list(params[best_start:best_start + best_run]) if best_run else []
    text = ("plateau: both-window beats %s | longest run %s | best REC %s OLD %s%s%s"
            % ("".join("Y" if b else "." for b in beats), run_vals,
               params[ar], params[ao], "" if ar == ao else " (windows disagree)",
               " (best at grid edge)" if edge else ""))
    return dict(beats=beats, run=run_vals, best_rec=params[ar], best_old=params[ao],
                agree=ar == ao, edge=edge, ok=best_run >= 3 and not edge, text=text)


def gaps(df, span=MONTH_SPAN):
    """J.7 frequency: signals per month, zero-signal months inside `span`
    (inclusive, 'YYYY-MM'), longest gap in calendar days between signals."""
    if not len(df):
        return dict(per_month=0.0, zero_months=np.nan, longest_gap=np.nan, gaps_over_30=0)
    months = pd.period_range(span[0], span[1], freq="M").astype(str)
    have = set(df["sig"].str.slice(0, 7))
    zero = sum(1 for x in months if x not in have)
    d = pd.to_datetime(pd.Series(sorted(set(df["sig"]))))
    dd = d.diff().dt.days.dropna()
    return dict(per_month=len(df) / len(months), zero_months=zero,
                longest_gap=int(dd.max()) if len(dd) else 0,
                gaps_over_30=int((dd > 30).sum()))


def peak(df, strict=True):
    """Most trades held at once with unlimited slots. strict counts a trade
    on its exit day (it is still held at that morning's open); strict=False
    is the old K.1 count (exit day excluded)."""
    if not len(df):
        return 0
    ev = []
    for a, b in zip(df["entry"], df["exit"]):
        ev.append((a, 0, 1))
        ev.append((b, 1 if strict else -1, -1))
    cur = best = 0
    for _, _, x in sorted(ev):
        cur += x
        best = max(best, cur)
    return best


# ================================================================== money
def money(df, slots=SLOTS_K, loose=False, open_reuse=False):
    """K money view: sandbox_money.portfolio per slot count (strict by
    default) with Calmar. Returns a DataFrame, one row per slot count."""
    rows = []
    for k in slots:
        p = m.portfolio(df, k, loose=loose, open_reuse=open_reuse)
        rows.append(dict(slots=k, taken=p["taken"], offered=p["offered"],
                         final=p["final"], cagr=p["cagr"], mdd=p["mdd"],
                         calmar=p["cagr"] / abs(p["mdd"]) if p["mdd"] else np.nan,
                         peak=p["peak"], yearly=p["yearly"]))
    return pd.DataFrame(rows)


def money_band(df, slots=SLOTS_K, iters=200, seed=SEED, **kw):
    """Path dependence of the slot simulation: same-day priority (rank) is
    replaced by a random key `iters` times. With few slots one different
    pick early on changes every later pick, so a single run is a lottery
    ticket; report the band. Returns one row per slot count with the
    p10 / p50 / p90 of CAGR, MDD and final multiple. kw -> portfolio()."""
    rng = np.random.default_rng(seed)
    res = {k: [] for k in slots}
    for _ in range(iters):
        d = df.assign(rank=rng.random(len(df)))
        for k in slots:
            p = m.portfolio(d, k, **kw)
            res[k].append((p["cagr"], p["mdd"], p["final"], p["taken"]))
    rows = []
    for k in slots:
        a = np.array(res[k], float)
        q = np.percentile(a, [10, 50, 90], axis=0)
        rows.append(dict(slots=k, cagr_p10=q[0, 0], cagr_p50=q[1, 0], cagr_p90=q[2, 0],
                         mdd_p10=q[0, 1], mdd_p50=q[1, 1], mdd_p90=q[2, 1],
                         final_p50=q[1, 2], taken_p50=q[1, 3]))
    return pd.DataFrame(rows)


def money_line(row):
    return ("slots %2d: taken %3d/%3d x%5.2f CAGR %5.1f%% MDD %6.1f%% Calmar %4.2f peak %d"
            % (row["slots"], row["taken"], row["offered"], row["final"], row["cagr"],
               row["mdd"], row["calmar"], row["peak"]))


# ========================================================= filter report
def report_filter(label, base_df, keep, base_slip=None, slots=(3, 5, 8),
                  ctrl=True, quiet=False):
    """Score a FILTER (keep a subset of the base trades) under every gate.
    base_df / base_slip: row-aligned frames from base(); keep: bool mask.
    Returns a dict of the numbers and the gate flags; prints a block unless
    quiet. Plateau needs neighbouring cells, so it is NOT decided here: feed
    the per-window kept stats of a grid to plateau()."""
    keep = np.asarray(keep, bool)
    if len(keep) != len(base_df):
        raise ValueError("keep mask must align with the base frame")
    P = (lambda *a: None) if quiet else _say
    out = dict(label=label, windows={})
    P("== %s  (keep %d / %d)" % (label, int(keep.sum()), len(keep)))
    ctl = random_deletion_control(base_df, keep) if ctrl else None
    ev = ev_per_opportunity(base_df, keep)
    seven, money_ok = True, True
    for w in WINDOWS:
        wm = wmask(base_df, w)
        bw = base_df[wm]
        kw = keep[wm]
        kept, rem = bw[kw], bw[~kw]
        sb, sk, sr = stats(bw), stats(kept), stats(rem)
        blw, bkw = boot_lo(bw, "win"), boot_lo(kept, "win")
        blm, bkm = boot_lo(bw, "mean"), boot_lo(kept, "mean")
        hb, hk = halves(bw), halves(bw, kw)
        qs = quarters(kept, bw)
        lo, hi = diff_ci(bw, kw)
        res = dict(base=sb, kept=sk, removed=sr, boot_win=(bkw, blw), boot_mean=(bkm, blm),
                   halves_base=hb, halves_kept=hk, quarters=qs, diff_ci=(lo, hi), ev=ev[w])
        if base_slip is not None:
            sw = base_slip[wm]
            res["slip_base"], res["slip_kept"] = stats(sw), stats(sw[kw])
        g1 = sk["win"] >= sb["win"]
        g2 = bkw >= blw
        g3 = all(hk[i]["win"] >= hb[i]["win"] for i in (0, 1))
        g4 = qs["win"][0] >= qs["win"][1] - 1
        g6 = (base_slip is None) or res["slip_kept"]["win"] >= res["slip_base"]["win"]
        g7 = sr["n"] >= MIN_BUCKET
        k_mean = sk["mean"] > sb["mean"]
        k_win = sk["win"] >= sb["win"] - 1.0
        k_boot = bkm >= blm
        k_halves = all(hk[i]["mean"] >= hb[i]["mean"] for i in (0, 1))
        k_q = qs["mean"][0] >= 0.8 * qs["mean"][1]
        k_ci = lo > 0
        k_slip = (base_slip is None) or res["slip_kept"]["mean"] > res["slip_base"]["mean"]
        res["gates"] = dict(win=g1, bootlo=g2, halves=g3, quarters=g4, slip=g6, bucket=g7)
        res["money_gates"] = dict(mean=k_mean, win1pp=k_win, bootmean=k_boot, halves=k_halves,
                                  quarters80=k_q, ci=k_ci, slip=k_slip)
        seven = seven and g1 and g2 and g3 and g4 and g6 and g7
        money_ok = money_ok and all(res["money_gates"].values())
        out["windows"][w] = res
        P("  %s base n=%3d win %5.1f%% mean %+5.2f sum %+7.1f | kept n=%3d win %5.1f%% "
          "mean %+5.2f sum %+7.1f | removed n=%3d win %5.1f%% mean %+5.2f"
          % (w, sb["n"], sb["win"], sb["mean"], sb["sum"], sk["n"], sk["win"], sk["mean"],
             sk["sum"], sr["n"], sr["win"], sr["mean"]))
        P("      bootLo win %5.1f (base %5.1f) mean %+5.2f (base %+5.2f) | halves win "
          "%5.1f/%5.1f (base %5.1f/%5.1f) mean %+5.2f/%+5.2f (base %+5.2f/%+5.2f)"
          % (bkw, blw, bkm, blm, hk[0]["win"], hk[1]["win"], hb[0]["win"], hb[1]["win"],
             hk[0]["mean"], hk[1]["mean"], hb[0]["mean"], hb[1]["mean"]))
        P("      quarters win %d/%d mean %d/%d | dmean CI %+.2f..%+.2f | EV/opp %+.2f (base %+.2f)"
          % (qs["win"][0], qs["win"][1], qs["mean"][0], qs["mean"][1], lo, hi,
             ev[w]["kept"], ev[w]["base"]))
        if base_slip is not None:
            P("      slip %.1f%%: kept win %5.1f%% mean %+5.2f (base %5.1f%% %+5.2f)"
              % (100 * SLIP, res["slip_kept"]["win"], res["slip_kept"]["mean"],
                 res["slip_base"]["win"], res["slip_base"]["mean"]))
        if ctl is not None:
            c = ctl[w]
            if c.get("degenerate"):
                P("      I.4 control: degenerate (kept %d of %d)" % (c["n_kept"], c["n_base"]))
            else:
                P("      I.4 control (%d draws): win pct %5.1f (rnd p50 %5.1f p95 %5.1f) "
                  "mean pct %5.1f (rnd p50 %+5.2f p95 %+5.2f) %s"
                  % (CTRL_ITERS, c["pct_win"], c["rnd_win"][1], c["rnd_win"][2],
                     c["pct_mean"], c["rnd_mean"][1], c["rnd_mean"][2],
                     "PASS" if c["passed"] else "fail"))
    kept_all = base_df[keep]
    gb, gk = gaps(base_df), gaps(kept_all)
    out["gaps"] = dict(base=gb, kept=gk)
    out["peak"] = dict(base=peak(base_df), kept=peak(kept_all))
    P("  frequency: %.2f/month zero-months %s longest gap %sd (base %.2f / %s / %sd) | "
      "peak held %d (base %d)" % (gk["per_month"], gk["zero_months"], gk["longest_gap"],
                                  gb["per_month"], gb["zero_months"], gb["longest_gap"],
                                  out["peak"]["kept"], out["peak"]["base"]))
    if slots:
        mb, mk = money(base_df, slots), money(kept_all, slots)
        out["money"] = dict(base=mb, kept=mk)
        for (_, rb), (_, rk) in zip(mb.iterrows(), mk.iterrows()):
            P("  strict %s | base CAGR %5.1f%% MDD %6.1f%%"
              % (money_line(rk), rb["cagr"], rb["mdd"]))
    out["seven_ok"] = seven
    out["money_ok"] = money_ok
    out["control_ok"] = bool(ctl["passed"]) if ctl is not None else None
    out["candidate"] = bool(seven and money_ok and (ctl is None or ctl["passed"]))
    P("  -> seven gates (no plateau) %s | K.2 money %s | I.4 control %s => %s"
      % ("ok" if seven else "FAIL", "ok" if money_ok else "FAIL",
         "-" if ctl is None else ("PASS" if ctl["passed"] else "FAIL"),
         "candidate: check plateau + ungated control" if out["candidate"] else "not adopted"))
    return out


# ============================================================ data access
def trade_calendar():
    """Research sessions: dates with >= CAL_MIN_SIDS sids with Volume_Lot>0
    (the rule sandbox_entry_gate.build uses). Sorted list of 'YYYY-MM-DD'."""
    path = os.path.join(CACHE, "calendar.pkl")
    if os.path.exists(path):
        return pickle.load(open(path, "rb"))
    con = sqlite3.connect("file:%s?mode=ro" % RESEARCH_DB, uri=True)
    try:
        d = pd.read_sql("SELECT date, SUM(Volume_Lot > 0) AS nv FROM data GROUP BY date", con)
    finally:
        con.close()
    d["date"] = d["date"].astype(str).str.slice(0, 10)
    cal = sorted(d.loc[d["nv"] >= CAL_MIN_SIDS, "date"].unique())
    os.makedirs(CACHE, exist_ok=True)
    pickle.dump(cal, open(path, "wb"))
    return cal


def session_index(cal=None):
    cal = trade_calendar() if cal is None else cal
    return {d: i for i, d in enumerate(cal)}


def load_bars(sids=None, cols=("open", "high", "low", "close", "Volume_Lot")):
    """Raw research bars (read-only) for `sids` (None = all), sorted by
    stock_id, date. yfinance auto_adjust prices; Volume_Lot unadjusted."""
    con = sqlite3.connect("file:%s?mode=ro" % RESEARCH_DB, uri=True)
    try:
        q = "SELECT date, stock_id, %s FROM data" % ", ".join(cols)
        if sids is not None:
            sids = sorted(set(str(s) for s in sids))
            q += " WHERE stock_id IN (%s)" % ",".join("?" * len(sids))
            df = pd.read_sql(q, con, params=sids)
        else:
            df = pd.read_sql(q, con)
    finally:
        con.close()
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    df["stock_id"] = df["stock_id"].astype(str)
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["stock_id", "date"]).reset_index(drop=True)


# ================================================================ builder
FEATURES_PKL = os.path.join(CACHE, "sel_features.pkl")


def _risk_maps(cal):
    tw = pd.read_csv(os.path.join("data", "research_taiex.csv"))
    tw = tw[tw["date"].isin(cal)].reset_index(drop=True)
    c = pd.to_numeric(tw["close"], errors="coerce")
    tw["risk_on"] = (c > c.rolling(20).mean()) & (c > c.rolling(60).mean())
    tw["str20"] = c / c.rolling(20).mean() - 1
    return dict(zip(tw["date"], tw["risk_on"])), dict(zip(tw["date"], tw["str20"]))


def build_features(govt_excluded=True):
    """One pass of eval_realtrade.build_features on research_prices.db, then
    a slim cache of the ls > 0 rows (the only rows either pool can select)
    with every column the trade dicts carry, the live-pool membership flag,
    the replay date list and the session calendar. ~3 min."""
    import eval_realtrade as er
    from scanner import market_filter as mf
    t0 = time.time()
    old_db = er.DB
    er.DB = RESEARCH_DB
    try:
        df, T = er.build_features()
    finally:
        er.DB = old_db
    T["ls"] = er.launch_score(T)
    _say("  features %.0fs (%d rows)" % (time.time() - t0, len(T)))

    live = df[df["Volume_Lot"] > 0]
    cnt = live.groupby("date")["stock_id"].size()
    cal = sorted(cnt[cnt >= CAL_MIN_SIDS].index)

    # exactly sandbox_entry_gate.build's per-row extras
    d = df.sort_values(["stock_id", "date"])
    gb = d.groupby("stock_id")
    atr = ((d["high"] - d["low"]) / d["close"]).groupby(d["stock_id"]).transform(
        lambda s: s.rolling(20).mean()) * 100
    ma20 = gb["close"].transform(lambda s: s.rolling(20).mean())
    ma60 = gb["close"].transform(lambda s: s.rolling(60).mean())
    vol20 = gb["Volume_Lot"].transform(lambda s: s.rolling(20).mean())
    X = pd.DataFrame({"date": d["date"].to_numpy(), "sid": d["stock_id"].astype(str).to_numpy(),
                      "atr": atr.to_numpy(), "ma20d": (d["close"] / ma20 - 1).to_numpy(),
                      "ma60d": (d["close"] / ma60 - 1).to_numpy(),
                      "turn20m": (vol20 * d["close"] * 1000).to_numpy()})

    # live pool: same-day turnover over the whole snapshot (both boards),
    # floor and cap from scanner/market_filter.py mode_prelaunch
    cfg = mf._MODE_CFG["mode_prelaunch"]
    L = df[["date", "stock_id", "close", "Volume_Lot"]].copy()
    L["stock_id"] = L["stock_id"].astype(str)
    L["turn1"] = L["close"] * L["Volume_Lot"] * 1000.0
    L = L[L["turn1"] >= cfg["min_turnover"]]
    if govt_excluded:
        L = L[~L["stock_id"].isin(mf._GOVT_STOCKS)]
    L["rk"] = L.groupby("date")["turn1"].rank(method="first", ascending=False)
    top = L[L["rk"] <= cfg["cap"]]
    live_keys = set(zip(top["date"], top["stock_id"]))

    S = T[T["ls"] > 0][["date", "sid", "ls", "turn20", "c", "ret5", "dist52",
                        "rt", "bias", "ret60"]].copy()
    S = S.merge(X, on=["date", "sid"], how="left")
    S["live"] = [k in live_keys for k in zip(S["date"], S["sid"])]
    dates = sorted(T["date"].unique())
    feats = dict(S=S, dates=dates, cal=cal, govt_excluded=govt_excluded,
                 pool_cfg=dict(cfg), built=time.strftime("%Y-%m-%d %H:%M"))
    os.makedirs(CACHE, exist_ok=True)
    pickle.dump(feats, open(FEATURES_PKL, "wb"), protocol=pickle.HIGHEST_PROTOCOL)
    _say("  slim features: %d ls>0 rows, %d dates, %d sessions, %.0fs total"
         % (len(S), len(dates), len(cal), time.time() - t0))
    return feats, df


def load_features():
    if not os.path.exists(FEATURES_PKL):
        raise FileNotFoundError("%s missing: run `r4_common.py build`" % FEATURES_PKL)
    return pickle.load(open(FEATURES_PKL, "rb"))


def replay_pool(feats, kind="research", n_enter=None, n_hold=None, pool=None,
                warmup=None):
    """Day-by-day selection replay with the N_ENTER/N_HOLD hysteresis.
    kind='research' is eval_realtrade.replay_selection verbatim (ls>0, top
    `pool` by turn20, held names added, ls ranking) but grouped by date once
    instead of filtering 3.6M rows per day. kind='live' uses the same-day
    turnover pool (feats S.live). Defaults come from eval_realtrade (kept in
    sync with scanner/scan_mode.py); nothing here assigns them."""
    import eval_realtrade as er
    n_enter = er.N_ENTER if n_enter is None else n_enter
    n_hold = er.N_HOLD if n_hold is None else n_hold
    pool = er.POOL if pool is None else pool
    warmup = g.WARMUP if warmup is None else warmup
    S = feats["S"]
    by_date = {k: v[["sid", "ls", "turn20", "live"]] for k, v in S.groupby("date", sort=False)}
    held, streak, picks = set(), {}, []
    for d in [x for x in feats["dates"] if x >= warmup]:
        day = by_date.get(d)
        if day is None or day.empty:
            held, streak = set(), {}
            continue
        if kind == "research":
            pl = day.sort_values("turn20", ascending=False).head(pool)
        elif kind == "live":
            pl = day[day["live"].to_numpy()]
        else:
            raise ValueError(kind)
        extra = day[day["sid"].isin(held) & ~day["sid"].isin(set(pl["sid"]))]
        pl = pd.concat([pl, extra]).sort_values("ls", ascending=False).reset_index(drop=True)
        sel = []
        for rank, (sid, ls) in enumerate(zip(pl["sid"], pl["ls"])):
            if rank < n_enter or (sid in held and rank < n_hold):
                sel.append((sid, rank, ls))
        new_streak = {}
        for sid, rank, ls in sel:
            new_streak[sid] = streak.get(sid, 0) + 1
            picks.append((d, sid, rank, ls, new_streak[sid]))
        held = {s for s, _, _ in sel}
        streak = new_streak
    return pd.DataFrame(picks, columns=["date", "sid", "rank", "ls", "streak"])


def build_universe(P, feats, bars=None):
    """sandbox_entry_gate.build from the selection table P onward: OTC,
    risk_on, first day, rank < RANK_POOL, forward bars on the session
    calendar. Returns the trade dicts (universe.pkl format)."""
    import json
    import eval_realtrade as er
    cal = feats["cal"]
    risk_on, str20 = _risk_maps(cal)
    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}
    P = P[P["date"] >= g.EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    S = feats["S"][["date", "sid", "c", "ret5", "dist52", "rt", "bias", "ret60",
                    "atr", "ma20d", "ma60d", "turn20m"]]
    P = P.merge(S, on=["date", "sid"], how="left")
    P["ro"] = P["date"].map(lambda x: bool(risk_on.get(x, False)))
    P["str20"] = P["date"].map(lambda x: str20.get(x, np.nan))
    keep = ((P["mkt"] == "OTC") & P["ro"] & (P["streak"] == 1)
            & (P["rank"] < g.RANK_POOL))
    G = P[keep]
    if bars is None:
        raw = load_bars(sorted(set(G["sid"])))
        cs = set(cal)
        raw = raw[raw["date"].isin(cs) & (raw["Volume_Lot"] > 0)]
        bars = {sid: b.reset_index(drop=True) for sid, b in raw.groupby("stock_id")}
    out = []
    for r in G.itertuples(index=False):
        b = bars.get(str(r.sid))
        if b is None:
            continue
        idx = b.index[b["date"] == r.date]
        if len(idx) == 0:
            continue
        i = int(idx[0])
        fb = b.iloc[i + 1: i + 1 + g.FWD_BARS]
        if len(fb) < g.HOLD:
            continue
        out.append({
            "sig": r.date, "sid": str(r.sid), "rank": int(r.rank),
            "dist52": r.dist52, "ret5": r.ret5, "atr": r.atr, "ls": r.ls,
            "rt": r.rt, "bias": r.bias, "ret60": r.ret60,
            "ma20d": r.ma20d, "ma60d": r.ma60d, "turn20": r.turn20m,
            "str20": r.str20,
            "dates": fb["date"].tolist(),
            "o": fb["open"].to_numpy(float), "h": fb["high"].to_numpy(float),
            "l": fb["low"].to_numpy(float), "c": fb["close"].to_numpy(float),
        })
    return out


def build_selection(kinds=("research", "live"), n_enter=None, feats=None):
    """Replay the pools and cache P tables + universes. Builds the slim
    feature cache first when it is missing. The research universe for the
    shipped N_ENTER is also written to GATE_DIR/universe.pkl when that file
    does not exist yet."""
    import eval_realtrade as er
    n = er.N_ENTER if n_enter is None else n_enter
    t0 = time.time()
    if feats is None:
        feats = load_features() if os.path.exists(FEATURES_PKL) else build_features()[0]
    out = {}
    for kind in kinds:
        t1 = time.time()
        P = replay_pool(feats, kind, n_enter=n)
        P.to_pickle(os.path.join(CACHE, "sel_P_%s_n%d.pkl" % (kind, n)))
        U = build_universe(P, feats)
        pickle.dump(U, open(_universe_path(kind, n), "wb"))
        if kind == "research" and n == er.N_ENTER and not os.path.exists(g.PICKS_PKL):
            os.makedirs(GATE_DIR, exist_ok=True)
            pickle.dump(U, open(g.PICKS_PKL, "wb"))
        out[kind] = (P, U)
        _say("  %s n_enter=%d: P %d rows, universe %d trades, %.0fs"
             % (kind, n, len(P), len(U), time.time() - t1))
    _say("  build_selection total %.0fs" % (time.time() - t0))
    return out


def same_trades(a, b):
    """Exact equality of two trade-dict lists (order, keys, values)."""
    if len(a) != len(b):
        return False, "length %d vs %d" % (len(a), len(b))
    for i, (x, y) in enumerate(zip(a, b)):
        if set(x) != set(y):
            return False, "row %d keys differ" % i
        for k in x:
            u, v = x[k], y[k]
            if isinstance(u, np.ndarray) or isinstance(v, np.ndarray):
                if not np.array_equal(np.asarray(u, float), np.asarray(v, float), equal_nan=True):
                    return False, "row %d %s differs" % (i, k)
            elif isinstance(u, float) or isinstance(v, float):
                if not ((u is None and v is None) or (u == v) or (u != u and v != v)):
                    return False, "row %d %s %r vs %r" % (i, k, u, v)
            elif u != v:
                return False, "row %d %s %r vs %r" % (i, k, u, v)
    return True, "identical"


# =============================================================== commands
def _window_line(df, label):
    rec, old = split(df)
    a, b = stats(rec), stats(old)
    return ("%s REC n=%3d %5.2f%% / %+5.2f (sum %+6.1f) | OLD n=%3d %5.2f%% / %+5.2f (sum %+6.1f)"
            % (label, a["n"], a["win"], a["mean"], a["sum"], b["n"], b["win"], b["mean"], b["sum"]))


def cmd_build():
    t0 = time.time()
    feats, _ = build_features()
    build_selection(feats=feats)
    _say("build done in %.0fs" % (time.time() - t0))


def cmd_parity():
    """The rebuilt research universe must equal the GATE cache universe."""
    ref = g.load()
    new = pickle.load(open(_universe_path("research"), "rb"))
    ok, why = same_trades(ref, new)
    _say("research universe rebuilt vs %s: %s (%d vs %d)" % (g.PICKS_PKL, why, len(ref), len(new)))
    return ok


def cmd_base():
    for kind in ("research", "live"):
        try:
            ts, b0, b1 = base(kind)
        except FileNotFoundError as e:
            _say("%s: %s" % (kind, e))
            continue
        _say("== %s universe: %d trades, signals %s..%s" % (kind, len(ts), b0["sig"].min(), b0["sig"].max()))
        _say("  " + _window_line(b0, "slip 0    "))
        _say("  " + _window_line(b1, "slip %.3f" % SLIP))
        _say("  exits: " + ", ".join("%s %d" % kv for kv in b0["why"].value_counts().items())
             + " | at_open exits %d" % int(b0["at_open"].sum()))
        gp = gaps(b0)
        _say("  %.2f/month, zero months %d, longest gap %dd, gaps>30d %d, peak held %d (old count %d)"
             % (gp["per_month"], gp["zero_months"], gp["longest_gap"], gp["gaps_over_30"],
                peak(b0), peak(b0, strict=False)))


def cmd_k1():
    """K.1 money view, loose (old) vs strict (fixed) vs open-reuse."""
    slots = (1, 2, 3, 5, 8, 10, 20)
    for kind in ("research", "live"):
        try:
            ts, b0, b1 = base(kind)
        except FileNotFoundError as e:
            _say("%s: %s" % (kind, e))
            continue
        for sl, df in (("0", b0), ("%.3f" % SLIP, b1)):
            _say("== %s, slip %s (%d trades)" % (kind, sl, len(df)))
            for mode, kw in (("loose", dict(loose=True)), ("strict", {}),
                             ("open-reuse", dict(open_reuse=True))):
                tab = money(df, slots, **kw)
                for _, r in tab.iterrows():
                    _say("  %-10s %s" % (mode, money_line(r)))


def cmd_k1band():
    """K.1 with the same-day priority shuffled (100 draws): loose vs strict."""
    for kind in ("research", "live"):
        ts, b0, b1 = base(kind)
        for mode, kw in (("loose", dict(loose=True)), ("strict", {}),
                         ("open-reuse", dict(open_reuse=True))):
            t0 = time.time()
            tab = money_band(b0, SLOTS_K, iters=100, **kw)
            for _, r in tab.iterrows():
                _say("  %-8s %-10s slots %d: CAGR p10/p50/p90 %5.1f/%5.1f/%5.1f  MDD %6.1f/%6.1f/%6.1f"
                     "  final p50 x%.2f taken p50 %d"
                     % (kind, mode, r["slots"], r["cagr_p10"], r["cagr_p50"], r["cagr_p90"],
                        r["mdd_p10"], r["mdd_p50"], r["mdd_p90"], r["final_p50"], r["taken_p50"]))
            _say("  (%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "base"
    globals()["cmd_" + cmd]()
