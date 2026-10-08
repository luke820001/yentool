"""
sandbox_r4_p25.py -- round 4, item P2-5 (2026-10-08). ASCII only.

Question: is N_ENTER (the fresh-entry rank cut of the Launch_Score list,
scanner/scan_mode.py N_ENTER = 20, N_HOLD = 80) worth moving to 10, 15, 25
or 30? BACKTEST_LOG J.6 says N_ENTER is a real axis (unlike the rank<20
gate on a fixed list, which is a no-op because a streak==1 row always has
rank < N_ENTER); J.6 measured 20 -> 25 once (593 trades, REC 72.0% / +2.25
but OLD 69.3% < 69.5%, OLD slip 68.8 < 69.0, quarters 7/13 and 7/12) and
filed it as "not adoptable, next-round topic". 10, 15 and 30 were never run.
J.4-B's monotone-worse "deeper N" (364/355/329/312/292) was the
board-internal ranking, a different axis (already rejected).

Measured only under the shipped rule (r4_common.BASE is sandbox_money.BASE,
built from scanner.exit_rules.DEFAULT_RULE; no exit threshold is written
here), at slip 0 and at the SLIP stress, both windows (REC = sig >=
RECENT_FROM, OLD = before). N_HOLD stays at the shipped 80 and the pool at
the shipped 300: neither is ever assigned here.

Per N the selection is re-replayed (the hysteresis, hence the held set,
streak and first day all change with N) and the universe rebuilt with the
round-4 fast builder (r4_common.build_selection(n_enter=N), ~10 s per pool
instead of sandbox_entry_gate.build's ~11 min). `slowparity` replays
eval_realtrade.replay_selection VERBATIM at every N (er.N_ENTER set inside
try/finally) on the same ls>0 feature rows and asserts the selection table
equals the fast one row for row; the trade dicts are a pure function of
that table (verified identical to universe.pkl at N=20 by the harness), so
equal tables mean equal universes. The entry gate is CORE+ with the rank
cut equal to N (the live Buy_Ready test is rank < N_ENTER).

Two signal sets: research (ls>0 first, top 300 by 20-day turnover, merged
TSE+OTC ranking, as J.6) and live (section L fix-1 pool: same-day turnover
floor / cap from scanner/market_filter.py, held names force-included).

Reported per N, per window: trades, win, mean, sum, slip, bootLo win and
mean, halves (cut at the N=20 window's median signal date, the same cut for
every N), quarters vs N=20, a by-signal-day bootstrap CI of mean(N) -
mean(20) over the union of both sets (the paired dmean on the COMMON trades
is identically zero -- same trade, same exits -- and is asserted, not
used), the set difference on (sid, sig) (added / removed buckets), the
I.4 same-count random-deletion control for the removed part, a same-count
random-draw control for the added part, the J.7 ungated control (same N,
CORE+ quality legs removed) and the CORE+ gain on the added population,
frequency (per month, zero months, longest gap, rescued zero months, added
trades landing in empty months), peak concurrency, and the money view:
strict slots 3/5/8 single runs (ALL, REC alone, OLD alone) plus the
r4_common.money_band path-dependence band at slots 3/5.

    PYTHONDONTWRITEBYTECODE=1 python -X utf8 archive/research/sandbox_r4_p25.py all
Commands: build | slowparity | analyze | wband | all (default all).
Output: printed tables + JSON at $R4_OUT_DIR/P2-5.json (default
CACHE/results). $P25_BAND_ITERS (default 200) sets the band draws;
$P25_FORCE=1 rebuilds universes that are already cached.
"""
import json
import os
import re
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import r4_common as c                               # noqa: E402  (chdir ROOT)

g, m = c.g, c.m
assert c.BASE is m.BASE                             # the shipped rule, nothing else

NS = (10, 15, 20, 25, 30)
KINDS = ("research", "live")
SLOTS = (3, 5, 8)
BAND_SLOTS = (3, 5)
BAND_ITERS = int(os.environ.get("P25_BAND_ITERS", "200"))
FORCE = os.environ.get("P25_FORCE", "") == "1"
SLIPS = (0.0, c.SLIP)
SCOPES = ("ALL",) + tuple(c.WINDOWS)
# whole months per window for the per-window zero-month count (2023-09 is
# split by RECENT_FROM and left out of both)
WSPAN = {"REC": ("2023-10", c.MONTH_SPAN[1]), "OLD": (c.MONTH_SPAN[0], "2023-08")}

OUT_DIR = os.environ.get("R4_OUT_DIR") or os.path.join(c.CACHE, "results")
OUT_JSON = os.path.join(OUT_DIR, "P2-5.json")
PARITY_JSON = os.path.join(OUT_DIR, "P2-5_parity.json")

# history, quoted from docs/BACKTEST_LOG.md (J.6 and J.4-B) for comparison
HISTORY = dict(
    J6=dict(n_enter=25, ranking="merged (research pool)", trades=593, added=37,
            rec_win=72.0, rec_mean=2.25, old_win=69.3, old_base_win=69.5,
            old_slip_win=68.8, old_slip_base_win=69.0,
            quarters="7/13 and 7/12", verdict="not adoptable, next-round topic"),
    J4B=dict(axis="board-internal ranking, deeper N_ENTER (rejected)",
             trades=[364, 355, 329, 312, 292], note="monotone worse, no plateau"),
    J6_fact="rank<20 on a fixed list is a no-op: streak==1 max rank is 19 at N_ENTER 20",
)


def _say(*a):
    print(*a)
    sys.stdout.flush()


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
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


def shipped_n():
    """(N_ENTER, N_HOLD) as shipped; eval_realtrade must agree with
    scanner/scan_mode.py (read as text: no import of the live module)."""
    import eval_realtrade as er
    src = open(os.path.join("scanner", "scan_mode.py"), encoding="utf-8").read()
    ne = int(re.search(r"^N_ENTER\s*=\s*(\d+)", src, re.M).group(1))
    nh = int(re.search(r"^N_HOLD\s*=\s*(\d+)", src, re.M).group(1))
    if (ne, nh) != (er.N_ENTER, er.N_HOLD):
        raise RuntimeError("eval_realtrade N_ENTER/N_HOLD %r != scan_mode %r"
                           % ((er.N_ENTER, er.N_HOLD), (ne, nh)))
    return ne, nh


BASE_N, SHIPPED_HOLD = shipped_n()
SIDX = c.session_index()


def p_path(kind, n):
    return os.path.join(c.CACHE, "sel_P_%s_n%d.pkl" % (kind, n))


def gate_for(n, ungated=False):
    """CORE+ (or the J.7 ungated control) with the rank cut equal to N:
    the live Buy_Ready test is rank < N_ENTER."""
    return dict(c.UNGATED if ungated else g.BASE_GATE, rank=n)


# ================================================================ build
def cmd_build():
    t0 = time.time()
    feats = None
    for n in NS:
        need = tuple(k for k in KINDS
                     if FORCE or not os.path.exists(c._universe_path(k, n)))
        if not need:
            _say("  n_enter=%d: cached (%s)" % (n, ", ".join(KINDS)))
            continue
        if feats is None:
            feats = c.load_features()
        c.build_selection(kinds=need, n_enter=n, feats=feats)
    _say("build done in %.0fs" % (time.time() - t0))


def cmd_slowparity():
    """eval_realtrade.replay_selection verbatim at every N vs the fast
    replay; plus the N=20 research universe vs GATE universe.pkl."""
    import eval_realtrade as er
    t0 = time.time()
    feats = c.load_features()
    S = feats["S"]
    T = S[["date", "sid", "ls", "turn20"]]
    miss = sorted(set(feats["dates"]) - set(S["date"]))
    if miss:   # dates with no ls>0 row still reset the hysteresis
        T = pd.concat([T, pd.DataFrame({"date": miss, "sid": "", "ls": 0.0,
                                        "turn20": 0.0})], ignore_index=True)
    out = dict(rows={}, ok={})
    for n in NS:
        t1 = time.time()
        fast = pd.read_pickle(p_path("research", n)).reset_index(drop=True)
        saved_n, saved_w = er.N_ENTER, er.WARMUP_START
        try:
            er.N_ENTER = n
            er.WARMUP_START = g.WARMUP
            slow = er.replay_selection(T)
        finally:
            er.N_ENTER = saved_n
            er.WARMUP_START = saved_w
        slow = slow.reset_index(drop=True)
        ok = len(slow) == len(fast) and all(
            (slow[col].astype(str).to_numpy() == fast[col].astype(str).to_numpy()).all()
            for col in ("date", "sid", "rank", "streak")) and np.allclose(
                slow["ls"].to_numpy(float), fast["ls"].to_numpy(float), rtol=0, atol=0)
        out["rows"][n] = [len(slow), len(fast)]
        out["ok"][n] = bool(ok)
        _say("  n_enter=%2d: verbatim replay_selection %d rows, fast %d rows -> %s (%.0fs)"
             % (n, len(slow), len(fast), "IDENTICAL" if ok else "DIFFERENT",
                time.time() - t1))
    assert BASE_N == er.N_ENTER, "eval_realtrade N_ENTER was not restored"
    ok20, why = c.same_trades(g.load(), c.load_universe("research", BASE_N))
    out["universe_n20_vs_gate"] = why
    _say("  research universe n=%d vs %s: %s" % (BASE_N, g.PICKS_PKL, why))
    out["all_ok"] = bool(ok20 and all(out["ok"].values()))
    out["runtime_s"] = round(time.time() - t0, 1)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(jsonable(out), open(PARITY_JSON, "w"), indent=1)
    _say("slow parity %s (%.0fs)" % ("OK" if out["all_ok"] else "FAILED", time.time() - t0))
    return out


# ============================================================== helpers
def keys(df):
    return list(zip(df["sid"].astype(str), df["sig"]))


def wpart(df, w):
    return df[c.wmask(df, w)]


def isin(df, ks):
    return np.array([k in ks for k in keys(df)], bool)


def date_halves(bw, dw):
    """Stats of both halves of a window for the base (bw) and a candidate
    (dw), cut at the base window's median signal date for both."""
    s = np.sort(bw["sig"].to_numpy())
    cut = s[len(s) // 2]
    res = {}
    for lab, df in (("base", bw), ("cand", dw)):
        sg = df["sig"].to_numpy()
        res[lab] = [c.stats(df[sg < cut]), c.stats(df[sg >= cut])]
    res["cut"] = cut
    return res


def union_diff_ci(bw, dw, iters=1500, seed=c.SEED):
    """By-signal-day bootstrap 95% CI of mean(candidate) - mean(base) over
    the union of both sets (a shared trade has the same return in both)."""
    rb = dict(zip(keys(bw), bw["ret"].to_numpy(float)))
    rd = dict(zip(keys(dw), dw["ret"].to_numpy(float)))
    allk = sorted(set(rb) | set(rd), key=lambda k: (k[1], k[0]))
    sig = np.array([k[1] for k in allk])
    r = np.array([rd[k] if k in rd else rb[k] for k in allk])
    inb = np.array([k in rb for k in allk])
    ind = np.array([k in rd for k in allk])
    uk = np.unique(sig)
    groups = [(r[sig == u], inb[sig == u], ind[sig == u]) for u in uk]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        rr = np.concatenate([groups[i][0] for i in pick])
        bb = np.concatenate([groups[i][1] for i in pick])
        dd = np.concatenate([groups[i][2] for i in pick])
        if bb.sum() == 0 or dd.sum() == 0:
            continue
        out.append(rr[dd].mean() - rr[bb].mean())
    if not out:
        return (np.nan, np.nan)
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def addition_control(bw, added, iters=c.CTRL_ITERS, seed=c.SEED):
    """Where the ADDED bucket's win and mean fall among `iters` random
    same-count draws from the base window (mid-rank percentile). >= 95 in
    both windows = the additions are better than the signals already taken;
    ~50 = more of the same (a frequency question, not an edge); <= 5 =
    dilution."""
    r = bw["ret"].to_numpy(float)
    a = added["ret"].to_numpy(float)
    na = len(a)
    if na == 0 or len(r) == 0:
        return dict(n=na, degenerate=True, passed=False)
    rng = np.random.default_rng(seed)
    rep = na > len(r)
    sw, sm = np.empty(iters), np.empty(iters)
    for i in range(iters):
        idx = rng.choice(len(r), na, replace=rep)
        sw[i] = 100 * (r[idx] > 0).mean()
        sm[i] = r[idx].mean()
    kw, km = 100 * (a > 0).mean(), a.mean()

    def pct(sims, v):
        return 100 * ((sims < v - 1e-12).sum() + 0.5 * (np.abs(sims - v) <= 1e-12).sum()) / len(sims)
    pw, pm = pct(sw, kw), pct(sm, km)
    return dict(n=na, win=kw, mean=km, pct_win=pw, pct_mean=pm,
                rnd_win=(np.percentile(sw, 5), np.percentile(sw, 50), np.percentile(sw, 95)),
                rnd_mean=(np.percentile(sm, 5), np.percentile(sm, 50), np.percentile(sm, 95)),
                degenerate=False, passed=bool(pw >= 95 and pm >= 95))


def months(span):
    return list(pd.period_range(span[0], span[1], freq="M").astype(str))


def zero_months(df, span):
    have = set(df["sig"].str.slice(0, 7))
    return [x for x in months(span) if x not in have]


_FRAMES = {}


def frames(kind, n, ungated=False):
    """(df slip 0, df SLIP), row-aligned, for one pool / N / gate.
    Built here rather than with c.base(kind, <dict>, n_enter): the harness's
    trades() looks a dict gate up in a dict and raises TypeError, and its
    'core' path would use rank max(20, N) instead of N."""
    key = (kind, n, ungated)
    if key not in _FRAMES:
        gd = gate_for(n, ungated)
        ts = [t for t in c.load_universe(kind, n) if g.passes(t, gd)]
        _FRAMES[key] = (c.evaluate(ts), c.evaluate(ts, c.SLIP))
    return _FRAMES[key]


PAIR_SESSIONS = 20     # a displaced entry: same name, signals within this many sessions


def displaced_pairs(b0, d0, added, removed, sidx):
    """The mechanism of an N change. Moving N re-times entries: at a deeper N
    a name joins the list earlier (rank N_old..N-1), so its later first day
    at N=20 is no longer a first day (removed) and an earlier one appears
    (added); a shallower N does the reverse. Pair each removed trade with
    the nearest added trade of the same name within PAIR_SESSIONS sessions;
    the rest are pure additions (names N=20 never buys) and pure removals.
    Returns per window: pairs (n, mean ret of the N trade, of the N=20
    trade, win of each, mean session shift) and the unpaired buckets."""
    ra = d0[isin(d0, added)][["sid", "sig", "ret"]].copy()
    rr = b0[isin(b0, removed)][["sid", "sig", "ret"]].copy()
    ra["si"] = ra["sig"].map(sidx)
    rr["si"] = rr["sig"].map(sidx)
    used, pairs = set(), []
    for r in rr.itertuples(index=False):
        cand = ra[(ra["sid"] == r.sid)]
        best, bd = None, None
        for a in cand.itertuples(index=True):
            if a.Index in used or a.si != a.si or r.si != r.si:
                continue
            dd = abs(a.si - r.si)
            if dd <= PAIR_SESSIONS and (bd is None or dd < bd):
                best, bd = a, dd
        if best is not None:
            used.add(best.Index)
            pairs.append(dict(sid=r.sid, sig_base=r.sig, sig_n=best.sig, ret_base=r.ret,
                              ret_n=best.ret, shift=int(best.si - r.si)))
    P = pd.DataFrame(pairs, columns=["sid", "sig_base", "sig_n", "ret_base", "ret_n", "shift"])
    paired_rem = set(zip(P["sid"], P["sig_base"]))
    paired_add = set(zip(P["sid"], P["sig_n"]))
    out = {}
    for w in c.WINDOWS:
        pw = P[(P["sig_base"] >= c.RECENT_FROM) == (w == "REC")]
        ua = ra[[(k not in paired_add) and ((k[1] >= c.RECENT_FROM) == (w == "REC"))
                 for k in zip(ra["sid"], ra["sig"])]]
        ur = rr[[(k not in paired_rem) and ((k[1] >= c.RECENT_FROM) == (w == "REC"))
                 for k in zip(rr["sid"], rr["sig"])]]
        rn, rb = pw["ret_n"].to_numpy(float), pw["ret_base"].to_numpy(float)
        out[w] = dict(
            pairs=len(pw),
            n_trade=dict(win=100 * (rn > 0).mean() if len(rn) else np.nan,
                         mean=rn.mean() if len(rn) else np.nan),
            base_trade=dict(win=100 * (rb > 0).mean() if len(rb) else np.nan,
                            mean=rb.mean() if len(rb) else np.nan),
            shift_mean=pw["shift"].mean() if len(pw) else np.nan,
            pure_added=c.stats(ua), pure_removed=c.stats(ur))
    return out


def by_year(df):
    out = {}
    for y, x in df.groupby(df["sig"].str.slice(0, 4)):
        out[y] = c.stats(x)
    return out


def max_first_day_rank(kind, n):
    """J.6 structural fact: a streak==1 row always has rank < N_ENTER."""
    P = pd.read_pickle(p_path(kind, n))
    return int(P.loc[P["streak"] == 1, "rank"].max())


# ============================================================= analysis
def score(kind, n, B, D, UB, UD):
    """Everything for one (pool, N) against the shipped N (B = (b0, b1),
    D = (d0, d1); UB / UD the ungated frames at slip 0)."""
    b0, b1 = B
    d0, d1 = D
    kb, kd = set(keys(b0)), set(keys(d0))
    common, added, removed = kb & kd, kd - kb, kb - kd
    rb = dict(zip(keys(b0), b0["ret"]))
    rd = dict(zip(keys(d0), d0["ret"]))
    common_maxdiff = max([abs(rb[k] - rd[k]) for k in common] or [0.0])
    keep_b = isin(b0, kd)                     # base trades that survive at N
    rem_ctl = c.random_deletion_control(b0, keep_b)
    ub0, ud0 = UB, UD
    ukb, ukd = set(keys(ub0)), set(keys(ud0))
    u_added = ukd - ukb
    res = dict(kind=kind, n=n, trades=len(d0), common=len(common), added=len(added),
               removed=len(removed), common_ret_maxdiff=common_maxdiff,
               first_day_max_rank=max_first_day_rank(kind, n),
               gate_rank=gate_for(n)["rank"], windows={})
    seven, k2, ctl_ok = True, True, True
    for w in c.WINDOWS:
        bw, dw = wpart(b0, w), wpart(d0, w)
        bws, dws = wpart(b1, w), wpart(d1, w)
        sb, sd = c.stats(bw), c.stats(dw)
        slb, sld = c.stats(bws), c.stats(dws)
        blw, bdw = c.boot_lo(bw, "win"), c.boot_lo(dw, "win")
        blm, bdm = c.boot_lo(bw, "mean"), c.boot_lo(dw, "mean")
        hv = date_halves(bw, dw)
        qs = c.quarters(dw, bw)
        lo, hi = union_diff_ci(bw, dw)
        add_w = dw[isin(dw, added)]
        rem_w = bw[isin(bw, removed)]
        com_w = dw[isin(dw, common)]
        add_ctl = addition_control(bw, add_w)
        rc = rem_ctl[w]
        # J.7 ungated control and the CORE+ gain on the added population
        ubw, udw = wpart(ub0, w), wpart(ud0, w)
        ua = udw[isin(udw, u_added)]
        ua_g = ua[isin(ua, kd)]
        ua_ng = ua[~isin(ua, kd)]
        ub_g = ubw[isin(ubw, kb)]
        ub_ng = ubw[~isin(ubw, kb)]
        core_gain_base = (c.stats(ub_g)["win"] - c.stats(ub_ng)["win"],
                          c.stats(ub_g)["mean"] - c.stats(ub_ng)["mean"])
        core_gain_added = (c.stats(ua_g)["win"] - c.stats(ua_ng)["win"],
                           c.stats(ua_g)["mean"] - c.stats(ua_ng)["mean"])
        # frequency
        span = WSPAN[w]
        zb, zd = zero_months(bw, span), zero_months(dw, span)
        g1 = sd["win"] >= sb["win"]
        g2 = bdw >= blw
        g3 = all(hv["cand"][i]["win"] >= hv["base"][i]["win"] for i in (0, 1))
        g4 = qs["win"][0] >= qs["win"][1] - 1
        g6 = sld["win"] >= slb["win"]
        g7 = (len(add_w) + len(rem_w)) >= c.MIN_BUCKET
        m1 = sd["mean"] > sb["mean"]
        m2 = sd["win"] >= sb["win"] - 1.0
        m3 = bdm >= blm
        m4 = all(hv["cand"][i]["mean"] >= hv["base"][i]["mean"] for i in (0, 1))
        m5 = qs["mean"][0] >= 0.8 * qs["mean"][1]
        m6 = lo > 0
        m7 = sld["mean"] > slb["mean"]
        if n < BASE_N:
            cpass = bool(rc.get("passed"))
        elif n > BASE_N:
            cpass = bool(add_ctl.get("passed"))
        else:
            cpass = False
        res["windows"][w] = dict(
            base=sb, cand=sd, slip_base=slb, slip_cand=sld,
            boot_win=(bdw, blw), boot_mean=(bdm, blm), halves=hv, quarters=qs,
            diff_ci=(lo, hi), common=c.stats(com_w), added=c.stats(add_w),
            removed=c.stats(rem_w), removal_control=rc, addition_control=add_ctl,
            ungated=dict(base=c.stats(ubw), cand=c.stats(udw), added=c.stats(ua),
                         added_core=c.stats(ua_g), added_not_core=c.stats(ua_ng),
                         core_gain_base=core_gain_base, core_gain_added=core_gain_added),
            per_month=len(dw) / len(months(span)), zero_months=len(zd),
            zero_months_base=len(zb), rescued=sorted(set(zb) - set(zd)),
            lost=sorted(set(zd) - set(zb)),
            gates=dict(win=g1, bootlo=g2, halves=g3, quarters=g4, slip=g6, bucket=g7),
            money_gates=dict(mean=m1, win1pp=m2, bootmean=m3, halves=m4, quarters80=m5,
                             ci=m6, slip=m7),
            control_passed=cpass)
        seven = seven and g1 and g2 and g3 and g4 and g6 and g7
        k2 = k2 and m1 and m2 and m3 and m4 and m5 and m6 and m7
        ctl_ok = ctl_ok and cpass
    # whole-span frequency (J.7)
    gb, gd = c.gaps(b0), c.gaps(d0)
    zb_all = zero_months(b0, c.MONTH_SPAN)
    zd_all = zero_months(d0, c.MONTH_SPAN)
    add_all = d0[isin(d0, added)]
    add_in_empty = int(add_all["sig"].str.slice(0, 7).isin(set(zb_all)).sum())
    res["frequency"] = dict(base=gb, cand=gd, rescued=sorted(set(zb_all) - set(zd_all)),
                            lost=sorted(set(zd_all) - set(zb_all)),
                            added_in_base_empty_months=add_in_empty,
                            by_year=d0["sig"].str.slice(0, 4).value_counts().sort_index().to_dict(),
                            by_year_base=b0["sig"].str.slice(0, 4).value_counts().sort_index().to_dict())
    res["peak"] = dict(base=c.peak(b0), cand=c.peak(d0))
    res["exits"] = d0["why"].value_counts().to_dict()
    res["years"] = dict(base=by_year(b0), cand=by_year(d0))
    res["displaced"] = displaced_pairs(b0, d0, added, removed, SIDX)
    res["seven_no_plateau"] = seven
    res["money_gates_ok"] = k2
    res["control_ok"] = ctl_ok
    return res


def money_views(df0, df1):
    out = {}
    for sl, df in ((0.0, df0), (c.SLIP, df1)):
        for sc in SCOPES:
            sub = df if sc == "ALL" else wpart(df, sc)
            out["%s|%s" % (sl, sc)] = c.money(sub, SLOTS)
    return out


def money_bands(df0, df1, iters):
    out = {}
    for sl, df in ((0.0, df0), (c.SLIP, df1)):
        out[str(sl)] = c.money_band(df, BAND_SLOTS, iters=iters)
    return out


def plateau_block(rows, metric, slip=False):
    """J.7 plateau over NS with both windows overlaid, for one metric."""
    sel = "slip_cand" if slip else "cand"
    rec = [rows[n]["windows"]["REC"][sel][metric] for n in NS]
    old = [rows[n]["windows"]["OLD"][sel][metric] for n in NS]
    rb = rows[BASE_N]["windows"]["REC"][sel][metric]
    ob = rows[BASE_N]["windows"]["OLD"][sel][metric]
    p = c.plateau(list(NS), rec, old, rb, ob)
    p["per_n"] = {}
    for i, n in enumerate(NS):
        if n == BASE_N:
            continue
        nb = [j for j in (i - 1, i + 1) if 0 <= j < len(NS)]
        # the candidate and every grid neighbour (N=20 beats itself) must
        # beat base in BOTH windows; a lone cell is a spike
        p["per_n"][n] = bool(p["beats"][i] and all(p["beats"][j] for j in nb))
    return p


# ============================================================== printing
def _w(s):
    return "n=%3d %5.2f%% %+5.2f sum %+7.1f" % (s["n"], s["win"], s["mean"], s["sum"])


def print_kind(kind, rows, mv, mb, plats):
    _say("")
    _say("=" * 100)
    _say("== %s pool: N_ENTER sweep %s (N_HOLD %d fixed; gate CORE+ rank<N)"
         % (kind, list(NS), SHIPPED_HOLD))
    for n in NS:
        r = rows[n]
        _say("-- N_ENTER %d: %d trades (common %d, added %d, removed %d vs N=%d) | "
             "first-day max rank %d | common ret max|diff| %.2g"
             % (n, r["trades"], r["common"], r["added"], r["removed"], BASE_N,
                r["first_day_max_rank"], r["common_ret_maxdiff"]))
        for w in c.WINDOWS:
            x = r["windows"][w]
            hv = x["halves"]
            _say("   %s %s | slip %5.2f%% %+5.2f | bootLo win %5.1f mean %+5.2f | halves win %5.1f/%5.1f "
                 "mean %+5.2f/%+5.2f (base %5.1f/%5.1f %+5.2f/%+5.2f) | q win %d/%d mean %d/%d"
                 % (w, _w(x["cand"]), x["slip_cand"]["win"], x["slip_cand"]["mean"],
                    x["boot_win"][0], x["boot_mean"][0],
                    hv["cand"][0]["win"], hv["cand"][1]["win"], hv["cand"][0]["mean"],
                    hv["cand"][1]["mean"], hv["base"][0]["win"], hv["base"][1]["win"],
                    hv["base"][0]["mean"], hv["base"][1]["mean"],
                    x["quarters"]["win"][0], x["quarters"]["win"][1],
                    x["quarters"]["mean"][0], x["quarters"]["mean"][1]))
            if n == BASE_N:
                continue
            a, rm = x["added"], x["removed"]
            ac, rc = x["addition_control"], x["removal_control"]
            u = x["ungated"]
            _say("        dmean CI %+.2f..%+.2f | added %s | removed %s"
                 % (x["diff_ci"][0], x["diff_ci"][1],
                    "n=%d %.1f%% %+.2f" % (a["n"], a["win"], a["mean"]) if a["n"] else "none",
                    "n=%d %.1f%% %+.2f" % (rm["n"], rm["win"], rm["mean"]) if rm["n"] else "none"))
            _say("        add-ctl %s | I.4 removal-ctl %s"
                 % ("pct win %.0f mean %.0f" % (ac["pct_win"], ac["pct_mean"])
                    if not ac.get("degenerate") else "n/a",
                    "pct win %.0f mean %.0f" % (rc["pct_win"], rc["pct_mean"])
                    if not rc.get("degenerate") else "n/a"))
            _say("        ungated N: %s (ungated N=%d %s) | ungated added %s | CORE+ gain "
                 "base %+.1fpp/%+.2f added %+.1fpp/%+.2f"
                 % (_w(u["cand"]), BASE_N, _w(u["base"]),
                    "n=%d %.1f%% %+.2f" % (u["added"]["n"], u["added"]["win"], u["added"]["mean"])
                    if u["added"]["n"] else "none",
                    u["core_gain_base"][0], u["core_gain_base"][1],
                    u["core_gain_added"][0], u["core_gain_added"][1]))
            _say("        gates %s | K.2 %s | %.2f/month zero %d (base %d) rescued %s lost %s"
                 % ("".join("Y" if v else "." for v in x["gates"].values()),
                    "".join("Y" if v else "." for v in x["money_gates"].values()),
                    x["per_month"], x["zero_months"], x["zero_months_base"],
                    x["rescued"], x["lost"]))
        if n != BASE_N:
            for w in c.WINDOWS:
                dp = r["displaced"][w]
                _say("   %s re-timed pairs %d: N trade %.1f%% %+.2f vs N=%d trade %.1f%% %+.2f "
                     "(shift %+.1f sessions) | pure added %s | pure removed %s"
                     % (w, dp["pairs"], dp["n_trade"]["win"], dp["n_trade"]["mean"], BASE_N,
                        dp["base_trade"]["win"], dp["base_trade"]["mean"], dp["shift_mean"],
                        "n=%d %.1f%% %+.2f" % (dp["pure_added"]["n"], dp["pure_added"]["win"],
                                              dp["pure_added"]["mean"])
                        if dp["pure_added"]["n"] else "none",
                        "n=%d %.1f%% %+.2f" % (dp["pure_removed"]["n"], dp["pure_removed"]["win"],
                                              dp["pure_removed"]["mean"])
                        if dp["pure_removed"]["n"] else "none"))
        _say("   years: " + " ".join(
            "%s %d/%.0f%%/%+.2f" % (y, v["n"], v["win"], v["mean"])
            for y, v in sorted(r["years"]["cand"].items())))
        f = r["frequency"]
        _say("   all: %.2f/month zero %d longest gap %sd (base %.2f / %d / %sd) | rescued %s lost %s | "
             "added in empty months %d | peak %d (base %d)"
             % (f["cand"]["per_month"], f["cand"]["zero_months"], f["cand"]["longest_gap"],
                f["base"]["per_month"], f["base"]["zero_months"], f["base"]["longest_gap"],
                f["rescued"], f["lost"], f["added_in_base_empty_months"],
                r["peak"]["cand"], r["peak"]["base"]))
        for key in ("0.0|ALL", "%s|ALL" % c.SLIP, "0.0|REC", "0.0|OLD"):
            for _, row in mv[n][key].iterrows():
                _say("   strict %-10s %s" % (key, c.money_line(row)))
        for sl, tab in mb[n].items():
            for _, row in tab.iterrows():
                _say("   band slip %-6s slots %d: CAGR p10/p50/p90 %5.1f/%5.1f/%5.1f MDD p50 %6.1f taken p50 %d"
                     % (sl, row["slots"], row["cagr_p10"], row["cagr_p50"], row["cagr_p90"],
                        row["mdd_p50"], row["taken_p50"]))
    _say("-- plateau (both windows overlaid, grid %s)" % (list(NS),))
    for lab, p in plats.items():
        _say("   %-9s %s | per-N ok %s" % (lab, p["text"], p["per_n"]))
    _say("-- verdict per N")
    for n in NS:
        if n == BASE_N:
            continue
        r = rows[n]
        _say("   N=%d: seven(no plateau) %s plateau(win) %s K.2 %s control %s -> %s"
             % (n, r["seven_no_plateau"], plats["win"]["per_n"][n], r["money_gates_ok"],
                r["control_ok"], "CANDIDATE" if r["candidate"] else "not adopted"))


# ================================================================ analyze
def cmd_analyze():
    t0 = time.time()
    out = dict(item="P2-5", built=time.strftime("%Y-%m-%d %H:%M"),
               rule=dict(c.BASE), slip=c.SLIP, ns=list(NS), base_n=BASE_N,
               n_hold=SHIPPED_HOLD, slots=list(SLOTS), band_slots=list(BAND_SLOTS),
               band_iters=BAND_ITERS, ctrl_iters=c.CTRL_ITERS, seed=c.SEED,
               recent_from=c.RECENT_FROM, history=HISTORY, parity=None, kinds={})
    for kind in KINDS:
        t1 = time.time()
        B = frames(kind, BASE_N)
        UB = frames(kind, BASE_N, ungated=True)[0]
        rows, mv, mb = {}, {}, {}
        for n in NS:
            D = frames(kind, n)
            UD = frames(kind, n, ungated=True)[0]
            rows[n] = score(kind, n, B, D, UB, UD)
            mv[n] = money_views(*D)
            mb[n] = money_bands(D[0], D[1], BAND_ITERS)
            _say("  %s N=%d scored (%.0fs)" % (kind, n, time.time() - t1))
        plats = dict(win=plateau_block(rows, "win"), mean=plateau_block(rows, "mean"),
                     slip_win=plateau_block(rows, "win", slip=True),
                     slip_mean=plateau_block(rows, "mean", slip=True))
        for n in NS:
            r = rows[n]
            if n == BASE_N:
                r["candidate"] = False
                continue
            r["plateau_win"] = plats["win"]["per_n"][n]
            r["plateau_mean"] = plats["mean"]["per_n"][n]
            r["candidate"] = bool(r["seven_no_plateau"] and r["plateau_win"]
                                  and r["money_gates_ok"] and r["control_ok"])
        print_kind(kind, rows, mv, mb, plats)
        out["kinds"][kind] = dict(rows=rows, money={n: mv[n] for n in NS},
                                  band={n: mb[n] for n in NS}, plateau=plats)
    # parity of the shipped N against the known baselines
    r20 = out["kinds"]["research"]["rows"][BASE_N]
    base_ok = (r20["trades"] == 556
               and abs(r20["windows"]["REC"]["cand"]["win"] - 70.81) < 0.01
               and abs(r20["windows"]["OLD"]["cand"]["win"] - 69.52) < 0.01)
    l20 = out["kinds"]["live"]["rows"][BASE_N]
    live_ok = l20["trades"] == 478
    out["baseline_reproduced"] = dict(research_556=bool(base_ok), live_478=bool(live_ok))
    cands = {k: [n for n in NS if out["kinds"][k]["rows"][n].get("candidate")] for k in KINDS}
    out["candidates"] = cands
    out["verdict"] = "adopt" if cands["research"] else "reject"
    out["runtime_s"] = round(time.time() - t0, 1)
    # read last: `slowparity` may run in parallel with this command
    out["parity"] = json.load(open(PARITY_JSON)) if os.path.exists(PARITY_JSON) else None
    _say("")
    _say("baseline reproduced: %s | candidates: %s | verdict: %s"
         % (out["baseline_reproduced"], cands, out["verdict"]))
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(jsonable(out), open(OUT_JSON, "w"), indent=1)
    _say("wrote %s (%.0fs)" % (OUT_JSON, time.time() - t0))
    return out


def cmd_wband():
    """Money band per window (REC alone, OLD alone) at the SLIP stress, so a
    money gain can be checked in both windows like the per-trade gates.
    Merged into P2-5.json under 'window_band' (run after analyze)."""
    t0 = time.time()
    res = {}
    for kind in KINDS:
        res[kind] = {}
        for n in NS:
            d1 = frames(kind, n)[1]
            res[kind][n] = {}
            for w in c.WINDOWS:
                tab = c.money_band(wpart(d1, w), BAND_SLOTS, iters=BAND_ITERS)
                res[kind][n][w] = tab
                for _, row in tab.iterrows():
                    _say("  %-8s N=%2d %s slip %.3f slots %d: CAGR p10/p50/p90 %5.1f/%5.1f/%5.1f "
                         "MDD p50 %6.1f taken p50 %d"
                         % (kind, n, w, c.SLIP, row["slots"], row["cagr_p10"], row["cagr_p50"],
                            row["cagr_p90"], row["mdd_p50"], row["taken_p50"]))
    out = json.load(open(OUT_JSON)) if os.path.exists(OUT_JSON) else {}
    out["window_band"] = jsonable(dict(slip=c.SLIP, iters=BAND_ITERS, slots=list(BAND_SLOTS),
                                       data=res, runtime_s=round(time.time() - t0, 1)))
    json.dump(out, open(OUT_JSON, "w"), indent=1)
    _say("window bands merged into %s (%.0fs)" % (OUT_JSON, time.time() - t0))


def cmd_all():
    cmd_build()
    cmd_slowparity()
    cmd_analyze()
    cmd_wband()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    globals()["cmd_" + cmd]()
