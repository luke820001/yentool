"""
sandbox_r4_p23.py -- round 4, item P2-3 (2026-10-08). ASCII only.

Question: should a signal be skipped during a COOLDOWN after a replayed
exit on the same name (plan P0-2 / P2-3; the 3498 10-06 and 8227 10-07
re-entries)? Nothing in research or live has a cooldown today: research
streak == 1 equals live First_Day, and both count a re-entry as a signal.

Definitions (sandbox_r4_cooldown.py; brief fresh.md P2-3):
  B           prior = the last KEPT research trade of the name (sequential)
  A           prior = the trade of the name's current list streak (old
              tracker, gap_tol = hold), phantom (non-signal) anchors included
  A_phantom   A, but block only when that anchor was not a signal
  A2          prior = the chained segment of the new tracker (stage B1)
  A2_phantom  A2, phantom anchors only
  blocked <=> prior exists and sessions(prior exit -> sig) <= N
  N grid N_GRID; N = -1 is the open-overlap-only variant (prior still held
  after the signal close); the plan's values are N_HEAD.

Measured only under the shipped rule (r4_common.BASE = sandbox_money.BASE,
from scanner.exit_rules.DEFAULT_RULE), slip 0 and the SLIP stress, REC (sig
>= 2023-09-18) and OLD windows, through r4_common.report_filter: seven gates,
K.2 money gates, the I.4 same-count random-deletion control (300 draws per
window, >= 95th percentile in BOTH windows for win AND mean), EV per
opportunity, frequency, strict slots; then the plateau over N (both windows
overlaid), the J.7 ungated control, the live-frequency set, and the strict
slot band (same-day priority shuffled) for the headline cells.

    PYTHONDONTWRITEBYTECODE=1 python -X utf8 archive/research/sandbox_r4_p23.py
    PYTHONDONTWRITEBYTECODE=1 python -X utf8 archive/research/sandbox_r4_p23.py moneyctl
Output: printed tables + JSON at $R4_OUT_DIR/P2-3.json (default CACHE/results);
`moneyctl` (run after the main pass) merges the money control into it: the
same-count random deletion applied to the strict slot book, because a
capacity-bound book can gain CAGR from any deletion.
$P23_BAND_ITERS (default 100; 0 = skip) sets the slot-band draws,
$P23_MONEY_CTRL_ITERS (default 100) the money-control draws.
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
import sandbox_r4_cooldown as cd                    # noqa: E402

N_GRID = (-1, 0, 2, 5, 7, 10, 15, 20, 30)
N_HEAD = (-1, 0, 5, 10, 20)
VARIANTS = ("B", "A", "A_phantom", "A2", "A2_phantom")
MONEY_SLOTS = (3, 5, 8)
BAND_SLOTS = (3, 5, 8)
BAND_CELLS = (("B", -1), ("B", 10), ("A", 10), ("A2", 10))
BAND_ITERS = int(os.environ.get("P23_BAND_ITERS", "100"))
UNIVERSES = (("research", "research", "core"), ("ungated", "research", "ungated"),
             ("live", "live", "core"))

OUT_DIR = os.environ.get("R4_OUT_DIR") or os.path.join(c.CACHE, "results")
OUT_JSON = os.path.join(OUT_DIR, "P2-3.json")


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
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        xf = float(x)
        return None if xf != xf else round(xf, 4)
    return x


# ================================================================= masks
def prior_tables(name, kind, b0, cal, idx):
    P = pd.read_pickle(os.path.join(c.CACHE, "sel_P_%s_n20.pkl" % kind))
    tr = cd.Trades(set(b0["sid"]), b0, cal)
    keys = set(zip(b0["sid"].astype(str), b0["sig"]))
    A = cd.prior_list_streak(b0, P, idx, tr, keys)
    A2 = cd.prior_segment_chain(b0, P, idx, tr, keys)
    # parity: a signal anchor replayed from bars equals the base trade
    chk = 0
    for (sid, d), v in list(tr.memo.items()):
        if v is not None and v.get("base"):
            t = tr.make(sid, d)
            if t is not None:
                r = c.replay(t)
                assert r["exit"] == v["exit"] and abs(r["ret"] - v["ret"]) < 1e-9, (sid, d)
                chk += 1
    return P, tr, A, A2, chk


def keep_mask(variant, n, b0, idx, A, A2):
    if variant == "B":
        return cd.keep_prior_signal(b0, idx, n)[0]
    if variant == "B_naive":
        return cd.keep_prior_signal(b0, idx, n, sequential=False)[0]
    info = A if variant.startswith("A") and not variant.startswith("A2") else A2
    return cd.keep_prior(info, n, phantom_only=variant.endswith("_phantom"),
                         signal_only=variant.endswith("_signal"))


def prior_summary(info, b0):
    g = info["gap"].to_numpy(float)
    has = np.isfinite(g)
    sig = info["anchor_signal"].fillna(False).to_numpy(bool)
    out = dict(with_prior=int(has.sum()), still_open=int((g < 0).sum()),
               exit_on_sig=int((g == 0).sum()),
               median_gap_all=float(np.median(g[has])) if has.any() else None,
               median_gap_nonneg=float(np.median(g[has & (g >= 0)])) if (has & (g >= 0)).any() else None,
               phantom_anchor=int((has & ~sig).sum()), signal_anchor=int((has & sig).sum()),
               missing_bars=int(info["missing"].sum()))
    if "reanchors" in info.columns:
        out["rows_after_reanchor"] = int((has & (info["reanchors"] > 0)).sum())
    pw = info["prior_why"][has].value_counts().to_dict()
    out["prior_exit_mix"] = {str(k): int(v) for k, v in pw.items()}
    return out


def b_summary(b0, idx):
    _, g = cd.keep_prior_signal(b0, idx, 10 ** 6, sequential=False)
    has = np.isfinite(g)
    return dict(with_prior=int(has.sum()), still_open=int((g < 0).sum()),
                exit_on_sig=int((g == 0).sum()),
                median_gap_all=float(np.median(g[has])) if has.any() else None,
                median_gap_nonneg=float(np.median(g[has & (g >= 0)])) if has.any() else None)


# =============================================================== scoring
def run_cell(b0, b1, keep, slots):
    ctl = c.random_deletion_control(b0, keep)
    r = c.report_filter("", b0, keep, base_slip=b1, slots=slots, ctrl=False, quiet=True)
    r["control_ok"] = bool(ctl["passed"])
    r["candidate"] = bool(r["seven_ok"] and r["money_ok"] and ctl["passed"])
    out = dict(seven_ok=r["seven_ok"], money_ok=r["money_ok"], control_ok=r["control_ok"],
               candidate=r["candidate"], removed_total=int((~keep).sum()), windows={},
               gaps=r["gaps"], peak=r["peak"])
    for w in c.WINDOWS:
        x = r["windows"][w]
        out["windows"][w] = dict(
            base=x["base"], kept=x["kept"], removed=x["removed"],
            slip_base=x.get("slip_base"), slip_kept=x.get("slip_kept"),
            boot_win=x["boot_win"], boot_mean=x["boot_mean"],
            halves_base=x["halves_base"], halves_kept=x["halves_kept"],
            quarters=x["quarters"], diff_ci=x["diff_ci"], ev=x["ev"],
            gates=x["gates"], money_gates=x["money_gates"],
            control={k: v for k, v in ctl[w].items()})
    if "money" in r:
        out["money"] = {k: v.drop(columns=["yearly"]).to_dict("records")
                        for k, v in r["money"].items()}
    return out


def cell_line(tag, s):
    parts = [tag]
    for w in c.WINDOWS:
        x = s["windows"][w]
        rm, kp, bs = x["removed"], x["kept"], x["base"]
        ctl = x["control"]
        sk = x.get("slip_kept") or {}
        parts.append("%s rem %3d %5.1f%% %+6.2f | kept %5.1f%% %+5.2f (base %5.1f %+5.2f) "
                     "slip %5.1f %+5.2f | ctl %3.0f/%3.0f"
                     % (w, rm["n"], rm["win"] if rm["n"] else np.nan,
                        rm["mean"] if rm["n"] else np.nan, kp["win"], kp["mean"],
                        bs["win"], bs["mean"], sk.get("win", np.nan), sk.get("mean", np.nan),
                        ctl.get("pct_win", np.nan), ctl.get("pct_mean", np.nan)))
    parts.append("7g %s K2 %s I4 %s" % ("ok" if s["seven_ok"] else "x",
                                       "ok" if s["money_ok"] else "x",
                                       "ok" if s["control_ok"] else "x"))
    return " || ".join(parts)


def failed_gates(s):
    bad = []
    for w in c.WINDOWS:
        x = s["windows"][w]
        bad += ["%s:%s" % (w, k) for k, v in x["gates"].items() if not v]
        bad += ["%s:K2.%s" % (w, k) for k, v in x["money_gates"].items() if not v]
        if not x["control"].get("passed"):
            bad.append("%s:I4" % w)
    return bad


def plateaus(cells, b0, variant):
    rec_b = c.stats(b0[c.wmask(b0, "REC")])
    old_b = c.stats(b0[c.wmask(b0, "OLD")])
    out = {}
    for metric in ("win", "mean"):
        rec = [cells["%s|%d" % (variant, n)]["windows"]["REC"]["kept"][metric] for n in N_GRID]
        old = [cells["%s|%d" % (variant, n)]["windows"]["OLD"]["kept"][metric] for n in N_GRID]
        p = c.plateau(list(N_GRID), rec, old, rec_b[metric], old_b[metric])
        out[metric] = dict(beats=p["beats"], run=p["run"], best_rec=p["best_rec"],
                           best_old=p["best_old"], agree=p["agree"], edge=p["edge"],
                           ok=p["ok"], text=p["text"])
        _say("  %-11s %-4s %s" % (variant, metric, p["text"]))
    return out


def by_year(b0, keep):
    yr = b0["sig"].str.slice(0, 4)
    tot = yr.value_counts().sort_index()
    rem = yr[~keep].value_counts().reindex(tot.index).fillna(0).astype(int)
    return {y: [int(rem[y]), int(tot[y])] for y in tot.index}


def exit_mix(df):
    g = df.groupby("why")["ret"].agg(["size", "mean"])
    return {str(k): [int(v["size"]), round(float(v["mean"]), 2)] for k, v in g.iterrows()}


def removed_by_prior_outcome(b0, keep, info):
    """Descriptive only: the removed bucket split by whether the PRIOR trade
    won. Not a candidate (a second axis on a small bucket)."""
    out = {}
    pr = info["prior_ret"].to_numpy(float)
    for lab, m in (("prior_won", pr > 0), ("prior_lost", pr <= 0)):
        for w in c.WINDOWS:
            wm = c.wmask(b0, w)
            out["%s|%s" % (lab, w)] = c.stats(b0[wm & ~keep & m])
    return out


# ========================================================= money control
MONEY_CTRL_SLOTS = (3, 5, 8)
MONEY_CTRL_ITERS = int(os.environ.get("P23_MONEY_CTRL_ITERS", "100"))
MONEY_CTRL_CELLS = (("B", -1), ("B", 0), ("B", 5), ("B", 10), ("B", 20), ("A", 10),
                    ("A_phantom", 10), ("A2", 10))


def money_control(b0, keep, slots=MONEY_CTRL_SLOTS, iters=MONEY_CTRL_ITERS, seed=c.SEED):
    """The I.4 idea applied to MONEY: delete the same number of trades per
    window at random `iters` times and run the same strict slot book (rank
    priority). A filter's slot CAGR only counts if it beats >= 95% of the
    random deletions; a capacity-bound book can gain from ANY deletion."""
    rng = np.random.default_rng(seed)
    sel = {w: np.flatnonzero(c.wmask(b0, w)) for w in c.WINDOWS}
    nk = {w: int(keep[sel[w]].sum()) for w in c.WINDOWS}
    cand = {k: c.m.portfolio(b0[keep], k) for k in slots}
    sims = {k: [] for k in slots}
    for _ in range(iters):
        mask = np.zeros(len(b0), bool)
        for w in c.WINDOWS:
            mask[rng.choice(sel[w], nk[w], replace=False)] = True
        d = b0[mask]
        for k in slots:
            p = c.m.portfolio(d, k)
            sims[k].append((p["cagr"], p["mdd"]))
    out = {}
    for k in slots:
        a = np.array(sims[k], float)
        cg, md = cand[k]["cagr"], cand[k]["mdd"]
        out[k] = dict(cagr=cg, mdd=md, taken=cand[k]["taken"],
                      pct_cagr=100 * float((a[:, 0] < cg).mean()),
                      pct_mdd=100 * float((a[:, 1] < md).mean()),
                      rnd_cagr=np.percentile(a[:, 0], [5, 50, 95]).tolist(),
                      rnd_mdd=np.percentile(a[:, 1], [5, 50, 95]).tolist())
    return out


def cmd_moneyctl():
    """Money control for the headline cells (research and live); merged into
    the JSON written by main()."""
    t0 = time.time()
    cal = c.trade_calendar()
    idx = c.session_index(cal)
    res = json.load(open(OUT_JSON, encoding="utf-8")) if os.path.exists(OUT_JSON) else {}
    res["money_control"] = dict(iters=MONEY_CTRL_ITERS, slots=MONEY_CTRL_SLOTS, seed=c.SEED)
    for name, kind, gate in UNIVERSES:
        if name == "ungated":
            continue
        ts, b0, b1 = c.base(kind, gate)
        P, tr, A, A2, _ = prior_tables(name, kind, b0, cal, idx)
        base = {k: c.m.portfolio(b0, k) for k in MONEY_CTRL_SLOTS}
        U = dict(base={k: dict(cagr=v["cagr"], mdd=v["mdd"], taken=v["taken"])
                       for k, v in base.items()})
        _say("== money control, %s (%d draws, strict, rank priority)" % (name, MONEY_CTRL_ITERS))
        for k, v in base.items():
            _say("  base slots %d: CAGR %5.1f%% MDD %6.1f%% taken %d"
                 % (k, v["cagr"], v["mdd"], v["taken"]))
        for v, n in MONEY_CTRL_CELLS:
            keep = keep_mask(v, n, b0, idx, A, A2)
            mc = money_control(b0, keep)
            U["%s|%d" % (v, n)] = mc
            for k, x in mc.items():
                _say("  %-11s N=%3d slots %d: CAGR %5.1f%% (pct %3.0f; rnd p5/p50/p95 %5.1f/%5.1f/%5.1f)"
                     " MDD %6.1f%% (pct %3.0f; rnd p50 %6.1f)"
                     % (v, n, k, x["cagr"], x["pct_cagr"], x["rnd_cagr"][0], x["rnd_cagr"][1],
                        x["rnd_cagr"][2], x["mdd"], x["pct_mdd"], x["rnd_mdd"][1]))
        res["money_control"][name] = U
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(jsonable(res), open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    _say("merged money_control into %s (%.0fs)" % (OUT_JSON, time.time() - t0))


# ================================================================== main
def main():
    t0 = time.time()
    cal = c.trade_calendar()
    idx = c.session_index(cal)
    res = dict(item="P2-3", built=time.strftime("%Y-%m-%d %H:%M"), rule=dict(c.BASE),
               slip=c.SLIP, ctrl_iters=c.CTRL_ITERS, gap_tol=cd.GAP_TOL,
               n_grid=N_GRID, n_head=N_HEAD, variants=VARIANTS, universes={})
    keep_store = {}
    for name, kind, gate in UNIVERSES:
        ts, b0, b1 = c.base(kind, gate)
        P, tr, A, A2, chk = prior_tables(name, kind, b0, cal, idx)
        U = dict(trades=len(b0), base={w: dict(slip0=c.stats(b0[c.wmask(b0, w)]),
                                               slip=c.stats(b1[c.wmask(b1, w)]))
                                       for w in c.WINDOWS},
                 anchor_trades_replayed=tr.replayed, parity_checked=chk,
                 prior=dict(B=b_summary(b0, idx), A=prior_summary(A, b0),
                            A2=prior_summary(A2, b0)))
        _say("\n==== %s (%d trades) base %s" % (
            name, len(b0), " | ".join("%s n=%d %.2f%% %+.2f (slip %.2f%% %+.2f)"
                                      % (w, v["slip0"]["n"], v["slip0"]["win"], v["slip0"]["mean"],
                                         v["slip"]["win"], v["slip"]["mean"])
                                      for w, v in U["base"].items())))
        _say("  priors: %s" % json.dumps(jsonable(U["prior"])))
        _say("  anchor trades replayed from bars %d; signal-anchor parity checked %d"
             % (tr.replayed, chk))

        # prototype reproduction (stats only, no gates)
        proto = {}
        for v in ("B_naive", "B", "A"):
            for n in (0, 5, 10, 20):
                k = keep_mask(v, n, b0, idx, A, A2)
                proto["%s|%d" % (v, n)] = {w: dict(removed=c.stats(b0[c.wmask(b0, w) & ~k]),
                                                   kept=c.stats(b0[c.wmask(b0, w) & k]))
                                           for w in c.WINDOWS}
        U["proto"] = proto

        cells = {}
        _say("  -- cells (I.4 control %d draws; money %s on %s)"
             % (c.CTRL_ITERS, MONEY_SLOTS, "every cell" if name == "research" else "N_HEAD"))
        for v in VARIANTS:
            for n in N_GRID:
                k = keep_mask(v, n, b0, idx, A, A2)
                keep_store[(name, v, n)] = k
                slots = MONEY_SLOTS if (name == "research" or n in N_HEAD) else None
                s = run_cell(b0, b1, k, slots)
                s["failed"] = failed_gates(s)
                cells["%s|%d" % (v, n)] = s
                _say(cell_line("  %-10s N=%3d" % (v, n), s))
        U["cells"] = cells
        _say("  -- plateau over N (both windows overlaid)")
        U["plateau"] = {v: plateaus(cells, b0, v) for v in VARIANTS}

        if name == "research":
            diag = {}
            for v, n in (("B", -1), ("B", 0), ("B", 10), ("A", -1), ("A", 10), ("A", 20),
                         ("A_phantom", 10), ("A2", 10)):
                k = keep_store[(name, v, n)]
                d = dict(by_year=by_year(b0, k), exits_removed=exit_mix(b0[~k]),
                         exits_kept=exit_mix(b0[k]))
                if v != "B":
                    info = A2 if v.startswith("A2") else A
                    d["removed_by_prior_outcome"] = removed_by_prior_outcome(b0, k, info)
                diag["%s|%d" % (v, n)] = d
            # how much the definitions overlap at N = 10
            ov = {}
            for x, y in (("A", "B"), ("A", "A2"), ("A2", "B")):
                kx, ky = keep_store[(name, x, 10)], keep_store[(name, y, 10)]
                ov["%s_vs_%s" % (x, y)] = dict(both=int((~kx & ~ky).sum()),
                                               only_first=int((~kx & ky).sum()),
                                               only_second=int((kx & ~ky).sum()))
            diag["overlap_N10"] = ov
            U["diag"] = diag
            _say("  -- diagnostics")
            for k_, d in diag.items():
                _say("  %s: %s" % (k_, json.dumps(jsonable(d))))
        res["universes"][name] = U

        if name in ("research", "live") and BAND_ITERS > 0:
            band = {}
            tb = time.time()
            band["base"] = c.money_band(b0, BAND_SLOTS, iters=BAND_ITERS)
            for v, n in BAND_CELLS:
                band["%s|%d" % (v, n)] = c.money_band(b0[keep_store[(name, v, n)]], BAND_SLOTS,
                                                      iters=BAND_ITERS)
            _say("  -- strict slot band (%d draws, same-day priority shuffled), %.0fs"
                 % (BAND_ITERS, time.time() - tb))
            for k_, tab in band.items():
                for _, r in tab.iterrows():
                    _say("  %-8s slots %d: CAGR p10/p50/p90 %5.1f/%5.1f/%5.1f  MDD p50 %6.1f  "
                         "taken p50 %d" % (k_, r["slots"], r["cagr_p10"], r["cagr_p50"],
                                           r["cagr_p90"], r["mdd_p50"], r["taken_p50"]))
            U["money_band"] = {k_: v.to_dict("records") for k_, v in band.items()}

    # ---- verdict: a cell is a candidate only if every gate passes in the
    # research set, the plateau holds for that variant, and the same cell
    # does not reverse in the ungated control / live set.
    R = res["universes"]["research"]
    cand = []
    for v in VARIANTS:
        for n in N_GRID:
            s = R["cells"]["%s|%d" % (v, n)]
            if s["candidate"]:
                cand.append("%s|%d" % (v, n))
    res["candidates_research"] = cand
    res["control_pass_cells"] = {u: [k for k, s in res["universes"][u]["cells"].items()
                                     if s["control_ok"]] for u in res["universes"]}
    res["seven_pass_cells"] = {u: [k for k, s in res["universes"][u]["cells"].items()
                                   if s["seven_ok"]] for u in res["universes"]}
    res["plateau_ok"] = {u: {v: {m: res["universes"][u]["plateau"][v][m]["ok"]
                                 for m in ("win", "mean")} for v in VARIANTS}
                         for u in res["universes"]}
    _say("\n== summary")
    _say("  research candidates (seven + K.2 + I.4): %s" % (cand or "none"))
    _say("  I.4 control passes: %s" % json.dumps(res["control_pass_cells"]))
    _say("  seven-gate passes: %s" % json.dumps(res["seven_pass_cells"]))
    _say("  plateau ok: %s" % json.dumps(res["plateau_ok"]))
    res["runtime_s"] = round(time.time() - t0, 1)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(jsonable(res), open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    _say("wrote %s (%.0fs)" % (OUT_JSON, time.time() - t0))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "main"
    if cmd == "main":
        main()
    else:
        globals()["cmd_" + cmd]()
