"""
sandbox_r4_p24.py -- round 4, item P2-4 (2026-10-08). ASCII only.

Question: how many concurrent slots should a small-capital owner run the
shipped rule with, now that the slot simulation is STRICT (a slot freed on
day D is first reusable on the next session; sandbox_money.portfolio's
default since the round-4 harness fix)? This is information for the owner
(BACKTEST_LOG K.5 leaves the slot count to the owner). No rule change.

Measured only under the shipped rule (r4_common.BASE = sandbox_money.BASE,
built from scanner.exit_rules.DEFAULT_RULE; no exit threshold is written
here), at slip 0 and at the SLIP stress, on two signal sets:
  research  the 556 CORE+ first-day OTC risk-on trades (universe.pkl)
  live      the live-frequency set of BACKTEST_LOG section L fix-1 (478)
and in three scopes: ALL, REC (sig >= 2023-09-18) simulated on its own, and
OLD (before) simulated on its own (each starts from equity 1.0).

Book variants (strict unless the mode says otherwise):
  base   every signal competes for a free slot in rank order; the same name
         can be held twice (counted as 'dup')
  name   one position per name: a signal is skipped when the book already
         holds that name at the entry open (strict: a position exiting on
         the entry day is still held that morning). A full book is checked
         first, so skip_name counts only the signals the name rule itself
         cost
  pre    the P2-3 'B, N=-1' prefilter (drop a signal while the previous KEPT
         research trade of the same name is still open, unlimited capacity),
         then the base book -- shown only to tie P2-3 to the slot view

Metrics per run: final multiple, CAGR, realized max drawdown (the harness
convention: equity is marked only when a trade closes), mark-to-market max
drawdown (open positions valued at every session close, net of the sell
cost), Calmar, taken / offered, % skipped for lack of a slot, skipped by the
name rule, peak concurrency, slot utilisation, idle sessions (no position),
the taken trades' win / mean, per-window counts of the ALL run, yearly.
Path dependence: BAND_ITERS draws with the same-day priority shuffled (the
r4_common.money_band procedure, same seed), p10 / p50 / p90; the variants
are paired on the same draws (dCAGR, share of draws better / worse).
Slot counts: the asked-for 1/2/3/5/8 (r4_common.SLOTS_K) plus the
neighbours 4/6/10, so a recommended count can be checked for a plateau.
Fee floor: per-slot notional from a starting capital CAPITALS; the broker
minimum (portfolio.money.FeeSchedule.default()) is charged on top of the
proportional fee the research return already pays (both sides).

The fast book below re-implements sandbox_money.portfolio (strict / loose /
open_reuse) so the bands run in minutes and so the name rule can be added.
`parity` asserts it equals m.portfolio (final, CAGR, MDD, taken, offered,
peak, yearly) on every universe x slip x mode x slot count, and that its
base-variant band equals r4_common.money_band, before anything is scored.

    PYTHONDONTWRITEBYTECODE=1 python -X utf8 archive/research/sandbox_r4_p24.py
Output: printed tables + JSON at $R4_OUT_DIR/P2-4.json (default
CACHE/results). $P24_BAND_ITERS (default 200) sets the draws.
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import r4_common as c                               # noqa: E402  (chdir ROOT)

g, m = c.g, c.m

SLOTS_MAIN = c.SLOTS_K                  # (1, 2, 3, 5, 8): the slot counts asked for
SLOTS = (1, 2, 3, 4, 5, 6, 8, 10)       # plus neighbours, to see whether a pick is a plateau
PARITY_SLOTS = (1, 2, 3, 5, 8, 10, 20)
SCOPES = ("ALL",) + tuple(c.WINDOWS)
VARIANTS = ("base", "name", "pre")
MODES = ("strict", "loose", "open")
SLIPS = (0.0, c.SLIP)
UNIVERSES = ("research", "live")
CAPITALS = (30000, 50000, 70000, 100000, 150000, 300000)
BAND_ITERS = int(os.environ.get("P24_BAND_ITERS", "200"))
PARITY_BAND_ITERS = 10

OUT_DIR = os.environ.get("R4_OUT_DIR") or os.path.join(c.CACHE, "results")
OUT_JSON = os.path.join(OUT_DIR, "P2-4.json")


def _say(*a):
    print(*a)
    sys.stdout.flush()


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, pd.DataFrame):
        return jsonable(x.to_dict("records"))
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (np.floating, float)):
        xf = float(x)
        return None if xf != xf else round(xf, 4)
    return x


def fee_terms():
    """(proportional broker rate, minimum fee NT$, source)."""
    try:
        from portfolio.money import FeeSchedule
        fs = FeeSchedule.default()
        return (float(fs.broker_fee_rate * fs.discount), float(fs.min_fee),
                "portfolio.money.FeeSchedule.default()")
    except Exception as e:                       # pragma: no cover
        return g.BUY_COST, 20.0, "fallback: %r" % (e,)


FEE_RATE, FEE_MIN, FEE_SRC = fee_terms()


# ============================================================ universes
class Universe(object):
    """Base frames (slip 0 / SLIP, row-aligned), the calendar and the
    per-trade mark-to-market path (net value at each close before exit)."""

    def __init__(self, kind):
        ts, b0, b1 = c.base(kind)
        self.kind = kind
        self.ts = ts
        self.frames = {0.0: b0, c.SLIP: b1}
        self.cal = c.trade_calendar()
        self.sidx = c.session_index(self.cal)
        self.ordinal = {}
        for d in set(b0["entry"]) | set(b0["exit"]):
            self.ordinal[d] = pd.Timestamp(d).toordinal()
        assert (b0["exit_bar"].to_numpy() == b1["exit_bar"].to_numpy()).all()
        self.mtm_s, self.mtm_u = {}, {}
        for lab, t, eb in zip(b0.index, ts, b0["exit_bar"].astype(int)):
            E = float(t["o"][0])
            self.mtm_s[lab] = np.array([self.sidx[t["dates"][j]] for j in range(eb)], int)
            self.mtm_u[lab] = np.array([g.net(E, float(t["c"][j])) for j in range(eb)], float)
        self.pre_keep = prefilter_keep(b0, self.sidx)

    def frame(self, slip, scope):
        df = self.frames[slip]
        if scope == "ALL":
            return df
        return df[df["window"].to_numpy() == scope]


def prefilter_keep(df, sidx):
    """P2-3 definition B at N = -1 (sandbox_r4_cooldown.keep_prior_signal):
    walk each name in (sig, rank) order; a signal is dropped while the last
    KEPT trade of the name exits after the signal session."""
    sid = df["sid"].astype(str).to_numpy()
    sig = df["sig"].to_numpy()
    ex = df["exit"].to_numpy()
    rank = df["rank"].to_numpy()
    order = sorted(range(len(df)), key=lambda i: (sid[i], sig[i], rank[i]))
    keep = np.ones(len(df), bool)
    last = {}
    for i in order:
        s = sid[i]
        if s in last and sidx[sig[i]] - sidx[last[s]] <= -1:
            keep[i] = False
        if keep[i]:
            last[s] = ex[i]
    return pd.Series(keep, index=df.index)


# ================================================================= book
def arrays(d, U):
    """Arrays of a frame ALREADY sorted the way m.portfolio sorts it."""
    reserve = float(d["mult"].max())
    entry = d["entry"].to_numpy()
    exit_ = d["exit"].to_numpy()
    by_day = {}
    for i, e in enumerate(entry):
        by_day.setdefault(e, []).append(i)
    days = sorted(set(entry) | set(exit_))
    return dict(entry=entry, exit=exit_,
                ret=d["ret"].to_numpy(float) * d["mult"].to_numpy(float) / reserve,
                raw=d["ret"].to_numpy(float),
                ato=d["at_open"].astype(bool).to_numpy(),
                sid=d["sid"].astype(str).to_numpy(),
                lab=d.index.to_numpy(), win=d["window"].to_numpy(),
                by_day=by_day, days=days,
                day_ord=np.array([U.ordinal[x] for x in days], float),
                day_s=np.array([U.sidx[x] for x in days], int))


def run_book(A, slots, mode="strict", per_name=False, capital=None):
    """sandbox_money.portfolio, same arithmetic in the same order, plus the
    name rule and the fee floor. Returns the raw run (indices into A)."""
    loose = mode == "loose"
    open_reuse = mode == "open"
    exit_, ret, ato, sid = A["exit"], A["ret"], A["ato"], A["sid"]
    eq, open_, curve, peak, dup = 1.0, [], [], 0, 0
    taken, sizes, rets, skip_slot, skip_name = [], [], [], [], []
    for day in A["days"]:
        still, held_at_open = [], 0
        for pos in open_:
            same = pos[0] == day
            if pos[0] < day or (same and (loose or (open_reuse and pos[3]))):
                eq += pos[2] * pos[1] / 100
                if same and not pos[3]:
                    held_at_open += 1
            else:
                still.append(pos)
        open_ = still
        for i in A["by_day"].get(day, ()):
            if len(open_) >= slots:
                skip_slot.append(i)
                continue
            held = any(p[4] == sid[i] for p in open_)
            if held and per_name:
                skip_name.append(i)
                continue
            if held:
                dup += 1
            r = ret[i]
            size = eq / slots
            if capital:
                notional = capital * size
                xb = max(0.0, FEE_MIN - FEE_RATE * notional)
                xs = max(0.0, FEE_MIN - FEE_RATE * notional * (1 + r / 100))
                r = r - 100 * (xb + xs) / notional
            open_.append((exit_[i], r, size, ato[i], sid[i]))
            taken.append(i)
            sizes.append(size)
            rets.append(r)
        peak = max(peak, len(open_) + held_at_open)
        if not loose:
            still = []
            for pos in open_:
                if pos[0] <= day:
                    eq += pos[2] * pos[1] / 100
                else:
                    still.append(pos)
            open_ = still
        curve.append(eq)
    eqs = np.array(curve)
    yrs = (A["day_ord"][-1] - A["day_ord"][0]) / 365.25
    final = eqs[-1]
    return dict(eqs=eqs, final=final, cagr=100 * (final ** (1 / yrs) - 1),
                mdd=100 * (eqs / np.maximum.accumulate(eqs) - 1).min(),
                taken=np.array(taken, int), sizes=np.array(sizes), rets=np.array(rets),
                skip_slot=np.array(skip_slot, int), skip_name=np.array(skip_name, int),
                dup=dup, peak=peak, offered=len(A["entry"]))


def mtm_and_use(U, A, run, slots):
    """Mark-to-market MDD, slot utilisation and idle share over the run's
    session span (first entry .. last exit)."""
    s0, s1 = int(A["day_s"][0]), int(A["day_s"][-1])
    L = s1 - s0 + 1
    tk = run["taken"]
    labs = A["lab"][tk]
    if len(tk):
        ss = np.concatenate([U.mtm_s[l] for l in labs]) - s0
        ww = np.concatenate([U.mtm_u[l] * (sz / 100.0) for l, sz in zip(labs, run["sizes"])])
        unreal = np.bincount(ss, weights=ww, minlength=L)[:L]
    else:
        unreal = np.zeros(L)
    pos = A["day_s"] - s0
    real = np.full(L, np.nan)
    real[pos] = run["eqs"]
    idx = np.where(np.isnan(real), 0, np.arange(L))
    real = real[np.maximum.accumulate(idx)]
    eq = real + unreal
    mdd_mtm = 100 * (eq / np.maximum.accumulate(eq) - 1).min()
    occ = np.zeros(L + 1)
    if len(tk):
        a = np.array([U.sidx[x] for x in A["entry"][tk]]) - s0
        b = np.array([U.sidx[x] for x in A["exit"][tk]]) - s0
        np.add.at(occ, a, 1)
        np.add.at(occ, b + 1, -1)
    occ = np.cumsum(occ)[:L]
    return dict(mdd_mtm=mdd_mtm, util=100 * np.minimum(occ, slots).mean() / slots,
                idle=100 * (occ == 0).mean(), sessions=L)


def yearly(A, run):
    cc = pd.Series(run["eqs"], index=pd.to_datetime(A["days"]))
    ye = cc.resample("YE").last()
    y = ye.pct_change()
    y.iloc[0] = ye.iloc[0] - 1
    return {str(k.year): 100 * v for k, v in y.items()}


def summarize(U, A, run, slots, n_scope, skip_pre=0, detail=True):
    tk = run["taken"]
    mu = mtm_and_use(U, A, run, slots)
    raw = A["raw"][tk]
    out = dict(final=run["final"], cagr=run["cagr"], mdd=run["mdd"], mdd_mtm=mu["mdd_mtm"],
               calmar=run["cagr"] / abs(run["mdd"]) if run["mdd"] else np.nan,
               calmar_mtm=run["cagr"] / abs(mu["mdd_mtm"]) if mu["mdd_mtm"] else np.nan,
               offered=n_scope, taken=len(tk), skip_pre=skip_pre,
               skip_slot=len(run["skip_slot"]), skip_name=len(run["skip_name"]),
               skip_slot_pct=100.0 * len(run["skip_slot"]) / n_scope,
               skip_all_pct=100.0 * (n_scope - len(tk)) / n_scope,
               dup=run["dup"], peak=run["peak"], util=mu["util"], idle=mu["idle"],
               taken_win=100 * (raw > 0).mean() if len(raw) else np.nan,
               taken_mean=raw.mean() if len(raw) else np.nan,
               taken_mean_net_fee=run["rets"].mean() if len(raw) else np.nan)
    if detail:
        bw = {}
        for w in c.WINDOWS:
            off = A["win"] == w
            bw[w] = dict(offered=int(off.sum()), taken=int((A["win"][tk] == w).sum()),
                         skip_slot=int((A["win"][run["skip_slot"]] == w).sum()),
                         skip_name=int((A["win"][run["skip_name"]] == w).sum()))
            bw[w]["skip_slot_pct"] = (100.0 * bw[w]["skip_slot"] / bw[w]["offered"]
                                      if bw[w]["offered"] else np.nan)
        out["by_window"] = bw
        out["yearly"] = yearly(A, run)
    return out


# =============================================================== parity
def cmd_parity(Us):
    worst = 0.0
    n = 0
    for kind, U in Us.items():
        for slip in SLIPS:
            df = U.frame(slip, "ALL")
            A = arrays(df.sort_values(["entry", "rank"]), U)
            for mode in MODES:
                kw = dict(loose=mode == "loose", open_reuse=mode == "open")
                for k in PARITY_SLOTS:
                    ref = m.portfolio(df, k, **kw)
                    mine = run_book(A, k, mode)
                    assert ref["taken"] == len(mine["taken"]), (kind, slip, mode, k)
                    assert ref["offered"] == mine["offered"], (kind, slip, mode, k)
                    assert ref["peak"] == mine["peak"], (kind, slip, mode, k, ref["peak"], mine["peak"])
                    for key in ("final", "cagr", "mdd"):
                        worst = max(worst, abs(float(ref[key]) - float(mine[key])))
                    yr = yearly(A, mine)
                    assert set(yr) == set(ref["yearly"])
                    for y in yr:
                        worst = max(worst, abs(yr[y] - ref["yearly"][y]))
                    n += 1
    assert worst < 1e-9, worst
    # the band procedure: same draws as r4_common.money_band
    U = Us["research"]
    df = U.frame(0.0, "ALL")
    ref = c.money_band(df, SLOTS_MAIN, iters=PARITY_BAND_ITERS)
    rng = np.random.default_rng(c.SEED)
    res = {k: [] for k in SLOTS_MAIN}
    for _ in range(PARITY_BAND_ITERS):
        d = df.assign(rank=rng.random(len(df))).sort_values(["entry", "rank"])
        A = arrays(d, U)
        for k in SLOTS_MAIN:
            r = run_book(A, k)
            res[k].append(r["cagr"])
    bworst = 0.0
    for _, row in ref.iterrows():
        q = np.percentile(np.array(res[int(row["slots"])]), [10, 50, 90])
        bworst = max(bworst, abs(q[0] - row["cagr_p10"]), abs(q[1] - row["cagr_p50"]),
                     abs(q[2] - row["cagr_p90"]))
    assert bworst < 1e-9, bworst
    _say("parity: fast book == sandbox_money.portfolio on %d runs (max |diff| %.1e); "
         "band == r4_common.money_band (%d draws, max |diff| %.1e)"
         % (n, worst, PARITY_BAND_ITERS, bworst))
    return dict(runs=n, max_abs_diff=worst, band_draws=PARITY_BAND_ITERS, band_max_abs_diff=bworst)


# ============================================================= studies
def concurrency(U, df):
    """Unlimited-slot concurrency over sessions (exit day counted held)."""
    s = np.array([U.sidx[x] for x in df["entry"]])
    e = np.array([U.sidx[x] for x in df["exit"]])
    s0, s1 = s.min(), e.max()
    occ = np.zeros(s1 - s0 + 2)
    np.add.at(occ, s - s0, 1)
    np.add.at(occ, e - s0 + 1, -1)
    occ = np.cumsum(occ)[:s1 - s0 + 1]
    return dict(peak=int(occ.max()), peak_harness=c.peak(df), p50=float(np.median(occ)),
                p90=float(np.percentile(occ, 90)), mean=float(occ.mean()),
                idle=100 * float((occ == 0).mean()),
                over={k: 100 * float((occ > k).mean()) for k in SLOTS})


def scope_frames(U, slip, scope, variant):
    df = U.frame(slip, scope)
    if variant == "pre":
        keep = U.pre_keep.loc[df.index].to_numpy()
        return df[keep], int((~keep).sum())
    return df, 0


def singles(Us):
    rows = []
    for kind, U in Us.items():
        for slip in SLIPS:
            for scope in SCOPES:
                n_scope = len(U.frame(slip, scope))
                for variant in VARIANTS:
                    df, skip_pre = scope_frames(U, slip, scope, variant)
                    A = arrays(df.sort_values(["entry", "rank"]), U)
                    modes = MODES if (variant == "base" and scope == "ALL") else ("strict",)
                    for mode in modes:
                        for k in SLOTS:
                            run = run_book(A, k, mode, per_name=variant == "name")
                            s = summarize(U, A, run, k, n_scope, skip_pre)
                            s.update(universe=kind, slip=slip, scope=scope, variant=variant,
                                     mode=mode, slots=k, capital=None)
                            rows.append(s)
                    if scope == "ALL" and slip == c.SLIP and variant in ("base", "name"):
                        for cap in CAPITALS:
                            for k in SLOTS:
                                run = run_book(A, k, "strict", per_name=variant == "name",
                                               capital=cap)
                                s = summarize(U, A, run, k, n_scope, skip_pre)
                                s.update(universe=kind, slip=slip, scope=scope, variant=variant,
                                         mode="strict", slots=k, capital=cap)
                                rows.append(s)
    return rows


BAND_KEYS = ("cagr", "mdd", "mdd_mtm", "calmar_mtm", "final", "taken", "skip_slot_pct",
             "skip_name", "dup", "util", "idle", "taken_mean")


def bands(Us, iters):
    """Same-day priority shuffled `iters` times per (universe, scope); the
    same draw is reused for both slips, every variant, slot count and
    capital, so the cells are paired."""
    out = []
    for kind, U in Us.items():
        for scope in SCOPES:
            t0 = time.time()
            base0 = U.frame(0.0, scope)
            n_scope = len(base0)
            acc = {}
            rng = np.random.default_rng(c.SEED)
            for _ in range(iters):
                key = pd.Series(rng.random(n_scope), index=base0.index)
                order = base0.assign(rank=key).sort_values(["entry", "rank"]).index
                for slip in SLIPS:
                    df = U.frame(slip, scope).loc[order]
                    for variant in VARIANTS:
                        if variant == "pre":
                            keep = U.pre_keep.loc[order].to_numpy()
                            d, skip_pre = df[keep], int((~keep).sum())
                        else:
                            d, skip_pre = df, 0
                        A = arrays(d, U)
                        caps = (None,) + (CAPITALS if (scope == "ALL" and slip == c.SLIP
                                                       and variant in ("base", "name")) else ())
                        for cap in caps:
                            for k in SLOTS:
                                run = run_book(A, k, "strict", per_name=variant == "name",
                                               capital=cap)
                                s = summarize(U, A, run, k, n_scope, skip_pre, detail=False)
                                acc.setdefault((slip, variant, cap, k), []).append(
                                    [s[x] for x in BAND_KEYS])
            for (slip, variant, cap, k), vals in acc.items():
                a = np.array(vals, float)
                q = np.percentile(a, [10, 50, 90], axis=0)
                row = dict(universe=kind, scope=scope, slip=slip, variant=variant,
                           capital=cap, slots=k, iters=iters)
                for j, x in enumerate(BAND_KEYS):
                    row[x + "_p10"], row[x + "_p50"], row[x + "_p90"] = q[0, j], q[1, j], q[2, j]
                if variant != "base" and (slip, "base", cap, k) in acc:
                    # paired with the base book on the same draw
                    b = np.array(acc[(slip, "base", cap, k)], float)
                    for x in ("cagr", "mdd_mtm"):
                        j = BAND_KEYS.index(x)
                        dd = a[:, j] - b[:, j]
                        qd = np.percentile(dd, [10, 50, 90])
                        row["d_" + x + "_p10"], row["d_" + x + "_p50"], row["d_" + x + "_p90"] = qd
                        row["d_" + x + "_share_pos"] = 100 * float((dd > 1e-12).mean())
                        row["d_" + x + "_share_neg"] = 100 * float((dd < -1e-12).mean())
                out.append(row)
            _say("  band %s %s: %d draws, %.0fs" % (kind, scope, iters, time.time() - t0))
    return out


# ============================================================== printing
def _pick(rows, **kw):
    for r in rows:
        if all(r.get(k) == v for k, v in kw.items()):
            return r
    return None


def print_tables(Us, single, band, conc):
    for kind in UNIVERSES:
        U = Us[kind]
        b0 = U.frames[0.0]
        _say("\n==== %s set: %d signals (REC %d, OLD %d); prefilter B|-1 removes REC %d OLD %d"
             % (kind, len(b0), int((b0["window"] == "REC").sum()), int((b0["window"] == "OLD").sum()),
                int((~U.pre_keep[b0["window"] == "REC"]).sum()),
                int((~U.pre_keep[b0["window"] == "OLD"]).sum())))
        for scope in SCOPES:
            cc = conc[kind][scope]
            _say("  unlimited-slot concurrency %s: peak %d (harness peak %d) p50 %.0f p90 %.0f "
                 "mean %.2f idle %.1f%% | sessions with more than k open: %s"
                 % (scope, cc["peak"], cc["peak_harness"], cc["p50"], cc["p90"], cc["mean"],
                    cc["idle"], " ".join("k%d %.1f%%" % (k, v) for k, v in cc["over"].items())))
        for slip in SLIPS:
            for variant in ("base", "name"):
                _say("  -- %s, slip %.3f, strict, variant %s  [single = rank priority | band = "
                     "%d shuffles p50 [p10..p90]]" % (kind, slip, variant, BAND_ITERS))
                _say("     scope slots  taken/off  skip%  name dup peak util idle  tk win/mean | "
                     "CAGR   MDDreal MDDmtm | band CAGR p50 [p10..p90]  MDDreal p50  MDDmtm p50 "
                     "[p10]  skip% p50")
                for scope in SCOPES:
                    for k in SLOTS:
                        r = _pick(single, universe=kind, slip=slip, scope=scope, variant=variant,
                                  mode="strict", slots=k, capital=None)
                        b = _pick(band, universe=kind, slip=slip, scope=scope, variant=variant,
                                  slots=k, capital=None)
                        bt = ("%5.1f [%5.1f..%5.1f]  %6.1f       %6.1f [%6.1f]  %5.1f"
                              % (b["cagr_p50"], b["cagr_p10"], b["cagr_p90"], b["mdd_p50"],
                                 b["mdd_mtm_p50"], b["mdd_mtm_p10"], b["skip_slot_pct_p50"])
                              if b else "-")
                        _say("     %-4s %3d  %3d/%3d  %5.1f %4d %3d %4d %4.0f %4.0f %5.1f %+5.2f | "
                             "%5.1f %6.1f %6.1f | %s"
                             % (scope, k, r["taken"], r["offered"], r["skip_slot_pct"],
                                r["skip_name"], r["dup"], r["peak"], r["util"], r["idle"],
                                r["taken_win"], r["taken_mean"],
                                r["cagr"], r["mdd"], r["mdd_mtm"], bt))
                        if scope == "ALL":
                            bw = r["by_window"]
                            _say("            in this ALL run: REC skip %d/%d (%.1f%%) name %d | "
                                 "OLD skip %d/%d (%.1f%%) name %d"
                                 % (bw["REC"]["skip_slot"], bw["REC"]["offered"],
                                    bw["REC"]["skip_slot_pct"], bw["REC"]["skip_name"],
                                    bw["OLD"]["skip_slot"], bw["OLD"]["offered"],
                                    bw["OLD"]["skip_slot_pct"], bw["OLD"]["skip_name"]))
        _say("  -- %s, slip 0, ALL, base: loose (old K.1) / strict / open-reuse single runs"
             % kind)
        for k in SLOTS:
            parts = []
            for mode in MODES:
                r = _pick(single, universe=kind, slip=0.0, scope="ALL", variant="base", mode=mode,
                          slots=k, capital=None)
                parts.append("%s %5.1f%% / %6.1f%% peak %d" % (mode, r["cagr"], r["mdd"], r["peak"]))
            _say("     slots %d: %s" % (k, " | ".join(parts)))
        _say("  -- %s, slip %.3f, ALL, strict: fee floor (NT$%g min, %.4f%%/side) by starting capital,"
             " band p50" % (kind, c.SLIP, FEE_MIN, 100 * FEE_RATE))
        for variant in ("base", "name"):
            for k in SLOTS:
                parts = []
                b = _pick(band, universe=kind, slip=c.SLIP, scope="ALL", variant=variant,
                          slots=k, capital=None)
                parts.append("no floor %5.1f%%" % b["cagr_p50"])
                for cap in CAPITALS:
                    b = _pick(band, universe=kind, slip=c.SLIP, scope="ALL", variant=variant,
                              slots=k, capital=cap)
                    parts.append("NT$%dk %5.1f%%" % (cap // 1000, b["cagr_p50"]))
                _say("     %-4s slots %d (NT$%6.0f/slot at 100k): %s"
                     % (variant, k, 100000.0 / k, " | ".join(parts)))
        _say("  -- %s, strict: one-position-per-name ('name') and P2-3 prefilter ('pre') vs base,"
             " paired on the same %d draws: dCAGR p50 [p10..p90] (share of draws better/worse),"
             " dMDDmtm p50" % (kind, BAND_ITERS))
        for slip in SLIPS:
            for scope in SCOPES:
                for variant in ("name", "pre"):
                    parts = []
                    for k in SLOTS:
                        b = _pick(band, universe=kind, slip=slip, scope=scope, variant=variant,
                                  slots=k, capital=None)
                        parts.append("k%d %+5.1f [%+5.1f..%+5.1f] (%2.0f/%2.0f) %+5.1f"
                                     % (k, b["d_cagr_p50"], b["d_cagr_p10"], b["d_cagr_p90"],
                                        b["d_cagr_share_pos"], b["d_cagr_share_neg"],
                                        b["d_mdd_mtm_p50"]))
                    _say("     slip %.3f %-3s %-4s %s" % (slip, scope, variant, " | ".join(parts)))
        _say("  -- %s, slip %.3f, ALL, strict, name: yearly %% of the rank-priority run"
             % (kind, c.SLIP))
        for k in SLOTS:
            r = _pick(single, universe=kind, slip=c.SLIP, scope="ALL", variant="name",
                      mode="strict", slots=k, capital=None)
            _say("     slots %d: %s" % (k, " ".join("%s %+6.1f" % kv for kv in r["yearly"].items())))


def recommendation_inputs(band):
    """The table the recommendation reads: strict, one position per name,
    slip stress, NT$100k fee floor, ALL scope plus each window alone."""
    rows = []
    for kind in UNIVERSES:
        for k in SLOTS:
            b = _pick(band, universe=kind, slip=c.SLIP, scope="ALL", variant="name",
                      slots=k, capital=100000)
            rec = _pick(band, universe=kind, slip=c.SLIP, scope="REC", variant="name",
                        slots=k, capital=None)
            old = _pick(band, universe=kind, slip=c.SLIP, scope="OLD", variant="name",
                        slots=k, capital=None)
            rows.append(dict(universe=kind, slots=k,
                             cagr_p50=b["cagr_p50"], cagr_p10=b["cagr_p10"], cagr_p90=b["cagr_p90"],
                             mdd_real_p50=b["mdd_p50"], mdd_mtm_p50=b["mdd_mtm_p50"],
                             mdd_mtm_p10=b["mdd_mtm_p10"], calmar_mtm_p50=b["calmar_mtm_p50"],
                             skip_slot_pct_p50=b["skip_slot_pct_p50"], idle_p50=b["idle_p50"],
                             rec_cagr_p50=rec["cagr_p50"], rec_cagr_p10=rec["cagr_p10"],
                             rec_mdd_mtm_p50=rec["mdd_mtm_p50"],
                             old_cagr_p50=old["cagr_p50"], old_cagr_p10=old["cagr_p10"],
                             old_mdd_mtm_p50=old["mdd_mtm_p50"]))
    _say("\n==== recommendation inputs: strict, one-per-name, slip %.3f, NT$100k fee floor (ALL); "
         "windows alone without floor" % c.SLIP)
    _say("  set       k | CAGR p50 [p10..p90] | MDD real p50 | MDD mtm p50 [p10] | Calmar(mtm) | "
         "skip% | idle% || REC CAGR p50 [p10] MDDmtm | OLD CAGR p50 [p10] MDDmtm")
    for r in rows:
        _say("  %-8s %2d | %5.1f [%5.1f..%5.1f] | %6.1f | %6.1f [%6.1f] | %4.2f | %4.1f | %4.1f || "
             "%5.1f [%5.1f] %6.1f | %5.1f [%5.1f] %6.1f"
             % (r["universe"], r["slots"], r["cagr_p50"], r["cagr_p10"], r["cagr_p90"],
                r["mdd_real_p50"], r["mdd_mtm_p50"], r["mdd_mtm_p10"], r["calmar_mtm_p50"],
                r["skip_slot_pct_p50"], r["idle_p50"], r["rec_cagr_p50"], r["rec_cagr_p10"],
                r["rec_mdd_mtm_p50"], r["old_cagr_p50"], r["old_cagr_p10"], r["old_mdd_mtm_p50"]))
    _say("  robustness per k over both sets x (ALL with floor, REC, OLD): worst p50 CAGR, worst p10 "
         "CAGR, worst p50 MDDmtm, worst Calmar(mtm, ALL)")
    for k in SLOTS:
        rr = [r for r in rows if r["slots"] == k]
        p50 = [x for r in rr for x in (r["cagr_p50"], r["rec_cagr_p50"], r["old_cagr_p50"])]
        p10 = [x for r in rr for x in (r["cagr_p10"], r["rec_cagr_p10"], r["old_cagr_p10"])]
        dd = [x for r in rr for x in (r["mdd_mtm_p50"], r["rec_mdd_mtm_p50"], r["old_mdd_mtm_p50"])]
        cm = [r["calmar_mtm_p50"] for r in rr]
        for r in rr:
            r.update(worst_p50_cagr=min(p50), worst_p10_cagr=min(p10), worst_mdd_mtm=min(dd),
                     worst_calmar=min(cm))
        _say("     k%-2d worst p50 CAGR %5.1f | worst p10 CAGR %5.1f | worst MDDmtm p50 %6.1f | "
             "worst Calmar %4.2f" % (k, min(p50), min(p10), min(dd), min(cm)))
    return rows


# ================================================================== main
def main():
    t0 = time.time()
    # every replay is r4_common.replay -> sandbox_money.replay with m.BASE,
    # the shipped rule built from scanner.exit_rules.DEFAULT_RULE
    assert c.BASE is m.BASE
    Us = {k: Universe(k) for k in UNIVERSES}
    for kind, U in Us.items():
        b0, b1 = U.frames[0.0], U.frames[c.SLIP]
        _say(c._window_line(b0, "%-8s slip 0    " % kind))
        _say(c._window_line(b1, "%-8s slip %.3f" % (kind, c.SLIP)))
    par = cmd_parity(Us)
    conc = {k: {s: concurrency(U, U.frame(0.0, s)) for s in SCOPES} for k, U in Us.items()}
    single = singles(Us)
    _say("singles: %d runs (%.0fs)" % (len(single), time.time() - t0))
    band = bands(Us, BAND_ITERS) if BAND_ITERS > 0 else []
    print_tables(Us, single, band, conc)
    rec = recommendation_inputs(band)
    out = dict(item="P2-4", built=time.strftime("%Y-%m-%d %H:%M"),
               rule="sandbox_money.BASE (scanner.exit_rules.DEFAULT_RULE)",
               rule_legs={k: (list(v) if isinstance(v, tuple) else v) for k, v in m.BASE.items()},
               slip=c.SLIP,
               windows=dict(REC=">= %s" % c.RECENT_FROM, OLD="< %s" % c.RECENT_FROM),
               slots=list(SLOTS), band_iters=BAND_ITERS, seed=c.SEED,
               fee=dict(rate=FEE_RATE, min_fee=FEE_MIN, source=FEE_SRC, capitals=list(CAPITALS)),
               definitions=dict(
                   strict="slot freed on day D first reusable on the next session (m.portfolio default)",
                   loose="old K.1 same-day reuse", open="strict + at-open exits free the slot that morning",
                   base="every signal competes for a slot in rank order",
                   name="skip a signal when the book holds that name at the entry open",
                   pre="P2-3 B N=-1 prefilter then base book",
                   mdd="realized equity (marked at closes of trades), harness convention",
                   mdd_mtm="open positions valued at each session close net of sell cost",
                   skip_slot_pct="signals skipped because every slot was full / signals offered in scope",
                   scopes="ALL; REC and OLD each simulated alone from equity 1.0"),
               universes={k: dict(n=len(U.frames[0.0]),
                                  n_rec=int((U.frames[0.0]["window"] == "REC").sum()),
                                  n_old=int((U.frames[0.0]["window"] == "OLD").sum()),
                                  prefilter_removed=dict(
                                      REC=int((~U.pre_keep[U.frames[0.0]["window"] == "REC"]).sum()),
                                      OLD=int((~U.pre_keep[U.frames[0.0]["window"] == "OLD"]).sum())),
                                  concurrency=conc[k]) for k, U in Us.items()},
               parity=par, single=single, band=band, recommendation_inputs=rec,
               runtime_s=round(time.time() - t0, 1))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(jsonable(out), f, indent=1)
    _say("wrote %s (%.0fs)" % (OUT_JSON, time.time() - t0))


if __name__ == "__main__":
    main()
