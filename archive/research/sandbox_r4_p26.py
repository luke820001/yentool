"""
sandbox_r4_p26.py -- round 4, item P2-6 (2026-10-08). ASCII only.

Question: does the point-in-time monthly-revenue YoY of a signal name sort
the CORE+ signals into better / worse groups? DISPLAY ONLY: whatever this
finds, nothing is shipped as a gate or a score weight in round 4.

Data: FinMind TaiwanStockMonthRevenue, the anonymous per-stock endpoint
(research.md section 5), one request per signal name, cached as raw JSON in
REV_DIR (default scratchpad/r4/revenue/). The fetch is resumable and polite
(SPACING seconds between requests, default 12 s = 300 per hour, the
anonymous quota); a quota reply makes it wait and retry. Anonymous on
purpose: the owner's FINMIND_TOKEN (.env) feeds the live chip fetch, so this
study does not spend its quota.

Point in time (no look-ahead): revenue month M is PUBLIC from its legal
deadline, the 10th of month M+1. When the 10th is not a trading session the
deadline rolls to the next session (a deadline on a holiday moves to the
next working day). A signal on day D sees month M only when D > that date
(the 15:00 scan on the deadline day itself does not count it). For each
signal the LATEST visible month is used; if FinMind has no row for it, the
month before is used (flagged stale), else the signal is 'missing'.
Sensitivity: 'plain10' = the 10th without the roll.
FinMind keeps the latest (possibly revised) figure, not the first print:
a mild look-ahead, noted in the result.

Features per signal: YoY = rev(M) / rev(M-12) - 1 (NaN when M-12 is missing
or <= 0), MoM, 3-month YoY (sum M-2..M over sum M-14..M-12) and a 12-month
high flag (rev(M) >= max of the 11 months before; needs 12 months).
Buckets: missing, YoY < 0, 0-20%, 20-50%, >= 50% (0.20 falls in 20-50).

Scoring: the shipped rule only (r4_common.BASE = sandbox_money.BASE from
scanner.exit_rules.DEFAULT_RULE; no exit threshold is written here), slip 0
and r4_common.SLIP, REC / OLD windows, both the research set (556) and the
live-frequency set (478). Per bucket: n, win, mean, sum, share, bootLo,
slip. A pre-declared filter grid ("drop known YoY < t", "drop known
YoY >= t", "drop one bucket") goes through r4_common.report_filter (seven
gates, K.2 money gates, I.4 same-count random-deletion control) plus the
plateau check over each threshold family.

Commands:
  python archive/research/sandbox_r4_p26.py fetch     # ~1 h anonymous, resumable
  python archive/research/sandbox_r4_p26.py coverage  # what is cached
  python archive/research/sandbox_r4_p26.py spot      # MOPS spot check of 5 values
  python archive/research/sandbox_r4_p26.py analyze   # writes P2-6.json
Run with PYTHONDONTWRITEBYTECODE=1.
"""
import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import r4_common as c                              # noqa: E402  (chdir ROOT)
from ingestion.inst_history import _K              # noqa: E402

SCRATCH = os.environ.get(
    "P26_SCRATCH",
    "C:/Users/luke4/AppData/Local/Temp/claude/D--YenTool/"
    "1624a313-b1ce-4a13-be0b-5e480d6c444b/scratchpad")
OUT_DIR = os.path.join(SCRATCH, "r4")
OUT_JSON = os.path.join(OUT_DIR, "P2-6.json")
REV_DIR = os.environ.get("P26_REV_DIR") or os.path.join(OUT_DIR, "revenue")
FAIL_LOG = os.path.join(REV_DIR, "_failures.json")

API = "https://api.finmindtrade.com/api/v4/data"
DATASET = "TaiwanStockMonthRevenue"
START = "2016-01-01"
SPACING = float(os.environ.get("P26_SPACING", "12"))
QUOTA_WAIT = 600            # seconds to wait after a quota reply
QUOTA_MAX_WAITS = 8
DEADLINE_DAY = 10           # monthly revenue legal deadline: the 10th of M+1

BUCKETS = ("missing", "neg", "0-20", "20-50", "50+")
EDGES = (0.0, 0.20, 0.50)
LOW_GRID = (-0.30, -0.20, -0.10, 0.0, 0.10, 0.20, 0.30, 0.50)   # drop YoY < t
HIGH_GRID = (0.30, 0.50, 0.75, 1.00, 1.50, 2.00)                # drop YoY >= t
Y3_GRID = (-0.20, -0.10, 0.0, 0.10, 0.20)                      # post-hoc only
KINDS = ("research", "live")


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


# ================================================================== fetch
def signal_sids():
    """Research-set names first (most signals first), then live-only names,
    so a partial fetch covers as many signals as possible."""
    out, seen = [], set()
    for kind in KINDS:
        _, d0, _ = c.base(kind)
        cnt = d0["sid"].astype(str).value_counts()
        for sid in sorted(cnt.index, key=lambda s: (-cnt[s], s)):
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def _rev_path(sid):
    return os.path.join(REV_DIR, "%s.json" % sid)


def _cached_ok(sid):
    p = _rev_path(sid)
    if not os.path.exists(p):
        return False
    try:
        return json.load(open(p, encoding="utf-8")).get("ok") is True
    except Exception:
        return False


def _fetch_one(sid):
    """-> ('ok', rows) | ('quota', msg) | ('error', msg)."""
    import requests
    params = dict(dataset=DATASET, data_id=sid, start_date=START)
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(API, params=params, headers=headers, timeout=30)
    except Exception as e:                       # network
        return "error", repr(e)[:200]
    try:
        j = r.json()
    except Exception:
        j = {}
    msg = str(j.get("msg", ""))[:200]
    if r.status_code == 402 or "upper limit" in msg.lower():
        return "quota", "%s %s" % (r.status_code, msg)
    if r.status_code != 200 or j.get("status") not in (200, None):
        return "error", "%s %s" % (r.status_code, msg)
    return "ok", j.get("data") or []


def cmd_fetch():
    os.makedirs(REV_DIR, exist_ok=True)
    sids = signal_sids()
    todo = [s for s in sids if not _cached_ok(s)]
    _say("P2-6 fetch: %d signal names, %d cached, %d to fetch, spacing %.1fs"
         % (len(sids), len(sids) - len(todo), len(todo), SPACING))
    fails = {}
    waits = 0
    i = 0
    while i < len(todo):
        sid = todo[i]
        st, res = _fetch_one(sid)
        if st == "quota":
            waits += 1
            _say("  %s quota reply (%s); wait %d/%d of %ds"
                 % (sid, res, waits, QUOTA_MAX_WAITS, QUOTA_WAIT))
            if waits > QUOTA_MAX_WAITS:
                _say("  giving up on the quota; rerun later to resume")
                break
            time.sleep(QUOTA_WAIT)
            continue
        if st == "ok":
            json.dump(dict(ok=True, sid=sid, fetched=dt.datetime.now().isoformat(timespec="seconds"),
                           n=len(res), data=res),
                      open(_rev_path(sid), "w", encoding="utf-8"))
            fails.pop(sid, None)
        else:
            fails[sid] = res
            _say("  %s error %s" % (sid, res))
        i += 1
        if i % 20 == 0:
            _say("  %d/%d done" % (i, len(todo)))
        time.sleep(SPACING)
    json.dump(fails, open(FAIL_LOG, "w", encoding="utf-8"), indent=1)
    _say("fetch done: %d failures" % len(fails))


def cmd_coverage():
    sids = signal_sids()
    ok = [s for s in sids if _cached_ok(s)]
    _say("cached %d / %d signal names" % (len(ok), len(sids)))


# ========================================================= point in time
def load_revenue(sid):
    """{(year, month): revenue} for one sid, or None when not fetched."""
    p = _rev_path(sid)
    if not os.path.exists(p):
        return None
    j = json.load(open(p, encoding="utf-8"))
    if not j.get("ok"):
        return None
    out = {}
    for r in j.get("data") or []:
        try:
            y, mth, v = int(r["revenue_year"]), int(r["revenue_month"]), float(r["revenue"])
        except Exception:
            continue
        out[(y, mth)] = v
    return out


def _add_months(y, mth, k):
    z = y * 12 + (mth - 1) + k
    return z // 12, z % 12 + 1


def avail_date(y, mth, cal=None):
    """The day revenue month (y, mth) becomes public: the 10th of the next
    month, rolled to the first session on or after it when `cal` (sorted
    'YYYY-MM-DD' sessions) is given."""
    ny, nm = _add_months(y, mth, 1)
    d = "%04d-%02d-%02d" % (ny, nm, DEADLINE_DAY)
    if cal is not None:
        import bisect
        i = bisect.bisect_left(cal, d)
        if i < len(cal):
            d = cal[i]
    return d


def visible_month(sig, cal=None):
    """Latest revenue month public on signal day `sig` (strictly after its
    availability date)."""
    y, mth = int(sig[:4]), int(sig[5:7])
    for k in (1, 2, 3):
        cy, cm = _add_months(y, mth, -k)
        if sig > avail_date(cy, cm, cal):
            return cy, cm
    return _add_months(y, mth, -3)


def bucket_of(yoy):
    if yoy is None or yoy != yoy:
        return "missing"
    if yoy < EDGES[0]:
        return "neg"
    if yoy < EDGES[1]:
        return "0-20"
    if yoy < EDGES[2]:
        return "20-50"
    return "50+"


def features(rev, sig, cal=None):
    """Point-in-time revenue features of one signal."""
    out = dict(rev_month=None, stale=False, yoy=np.nan, mom=np.nan, yoy3=np.nan,
               high12=None, has_data=rev is not None and len(rev) > 0)
    if not rev:
        return out
    y, mth = visible_month(sig, cal)
    if (y, mth) not in rev:
        y, mth = _add_months(y, mth, -1)
        out["stale"] = True
        if (y, mth) not in rev:
            return out
    v = rev[(y, mth)]
    out["rev_month"] = "%04d-%02d" % (y, mth)

    def get(k):
        return rev.get(_add_months(y, mth, k))
    p12, p1 = get(-12), get(-1)
    if p12 is not None and p12 > 0:
        out["yoy"] = v / p12 - 1
    if p1 is not None and p1 > 0:
        out["mom"] = v / p1 - 1
    cur3 = [get(k) for k in (0, -1, -2)]
    old3 = [get(k) for k in (-12, -13, -14)]
    if all(x is not None for x in cur3 + old3) and sum(old3) > 0:
        out["yoy3"] = sum(cur3) / sum(old3) - 1
    prev11 = [get(k) for k in range(-11, 0)]
    if all(x is not None for x in prev11):
        out["high12"] = bool(v >= max(prev11))
    return out


def tag(df, cal, roll=True):
    """df: a base frame (sig, sid). Adds the revenue feature columns."""
    cache = {}
    rows = []
    for sig, sid in zip(df["sig"], df["sid"].astype(str)):
        if sid not in cache:
            cache[sid] = load_revenue(sid)
        rows.append(features(cache[sid], sig, cal if roll else None))
    f = pd.DataFrame(rows, index=df.index)
    f["fetched"] = [cache[s] is not None for s in df["sid"].astype(str)]
    f["bucket"] = [bucket_of(x) for x in f["yoy"]]
    return pd.concat([df, f], axis=1)


# ================================================================ scoring
def bucket_table(d0, d1, col="bucket", order=BUCKETS):
    """Per bucket per window: n, win, mean, sum, share of the window sum,
    bootLo win/mean, slip win/mean."""
    out = {}
    for w in c.WINDOWS:
        wm = c.wmask(d0, w)
        bw, sw = d0[wm], d1[wm]
        tot = bw["ret"].sum()
        rows = {}
        for b in order:
            k = (bw[col] == b).to_numpy()
            s = c.stats(bw[k])
            ss = c.stats(sw[k])
            rows[str(b)] = dict(n=s["n"], win=s["win"], mean=s["mean"], sum=s["sum"],
                                share=100 * s["sum"] / tot if tot else np.nan,
                                boot_win=c.boot_lo(bw[k], "win"),
                                boot_mean=c.boot_lo(bw[k], "mean"),
                                slip_win=ss["win"], slip_mean=ss["mean"])
        rows["_all"] = dict(c.stats(bw), slip_win=c.stats(sw)["win"],
                            slip_mean=c.stats(sw)["mean"])
        out[w] = rows
    return out


def print_table(title, tab, order):
    _say("  " + title)
    for w in c.WINDOWS:
        a = tab[w]["_all"]
        _say("    %s all n=%3d win %5.1f%% mean %+5.2f sum %+7.1f | slip %5.1f%% %+5.2f"
             % (w, a["n"], a["win"], a["mean"], a["sum"], a["slip_win"], a["slip_mean"]))
        for b in order:
            r = tab[w][str(b)]
            if not r["n"]:
                _say("      %-8s n=  0" % b)
                continue
            _say("      %-8s n=%3d win %5.1f%% mean %+6.2f sum %+7.1f (%5.1f%%) "
                 "bootLo %5.1f/%+5.2f | slip %5.1f%% %+5.2f"
                 % (b, r["n"], r["win"], r["mean"], r["sum"], r["share"],
                    r["boot_win"] if r["boot_win"] == r["boot_win"] else float("nan"),
                    r["boot_mean"] if r["boot_mean"] == r["boot_mean"] else float("nan"),
                    r["slip_win"], r["slip_mean"]))


def dose_response(d0):
    """Do the four known buckets order the same way in both windows?
    Spearman rho of YoY vs ret (known YoY only) per window, and the
    bucket-index slope sign of win and mean."""
    from scipy.stats import spearmanr
    out = {}
    known = [b for b in BUCKETS if b != "missing"]
    for w in c.WINDOWS:
        bw = d0[c.wmask(d0, w)]
        k = bw[bw["bucket"] != "missing"]
        rho, p = spearmanr(k["yoy"], k["ret"]) if len(k) > 5 else (np.nan, np.nan)
        wins = [c.stats(k[k["bucket"] == b])["win"] for b in known]
        means = [c.stats(k[k["bucket"] == b])["mean"] for b in known]
        x = np.arange(len(known), dtype=float)
        okw = ~np.isnan(np.asarray(wins, float))
        slope_w = np.polyfit(x[okw], np.asarray(wins, float)[okw], 1)[0] if okw.sum() > 1 else np.nan
        slope_m = np.polyfit(x[okw], np.asarray(means, float)[okw], 1)[0] if okw.sum() > 1 else np.nan
        out[w] = dict(n_known=len(k), rho=rho, p=p, win_by_bucket=wins, mean_by_bucket=means,
                      slope_win=slope_w, slope_mean=slope_m,
                      monotone_win=bool(np.all(np.diff(wins) >= 0) or np.all(np.diff(wins) <= 0)),
                      monotone_mean=bool(np.all(np.diff(means) >= 0) or np.all(np.diff(means) <= 0)))
    sw = [np.sign(out[w]["slope_win"]) for w in c.WINDOWS]
    sm = [np.sign(out[w]["slope_mean"]) for w in c.WINDOWS]
    out["same_sign_win"] = bool(sw[0] == sw[1] and sw[0] != 0)
    out["same_sign_mean"] = bool(sm[0] == sm[1] and sm[0] != 0)
    out["same_sign_rho"] = bool(np.sign(out["REC"]["rho"]) == np.sign(out["OLD"]["rho"]))
    return out


def _slim(rep):
    """The parts of a report_filter result worth keeping in JSON."""
    keep = dict(label=rep["label"], seven_ok=rep["seven_ok"], money_ok=rep["money_ok"],
                control_ok=rep["control_ok"], candidate=rep["candidate"],
                gaps=rep["gaps"], peak=rep["peak"], windows={})
    for w, r in rep["windows"].items():
        keep["windows"][w] = dict(
            base=r["base"], kept=r["kept"], removed=r["removed"], boot_win=r["boot_win"],
            boot_mean=r["boot_mean"], halves_kept=r["halves_kept"], halves_base=r["halves_base"],
            quarters=r["quarters"], diff_ci=r["diff_ci"], ev=r["ev"],
            slip_base=r.get("slip_base"), slip_kept=r.get("slip_kept"),
            gates=r["gates"], money_gates=r["money_gates"])
    if "money" in rep:
        keep["money"] = dict(base=rep["money"]["base"].drop(columns=["yearly"]),
                             kept=rep["money"]["kept"].drop(columns=["yearly"]))
    return keep


def _failed(rep):
    bad = []
    for w, r in rep["windows"].items():
        bad += ["%s:%s" % (w, k) for k, v in r["gates"].items() if not v]
        bad += ["%s:K2-%s" % (w, k) for k, v in r["money_gates"].items() if not v]
    if rep["control_ok"] is False:
        bad.append("I.4")
    return bad


def ctl_pcts(d0, keep):
    """The I.4 same-count random-deletion percentiles per window (r4_common
    keeps only the pass flag in report_filter's result)."""
    ctl = c.random_deletion_control(d0, keep)
    return {w: dict(pct_win=ctl[w].get("pct_win"), pct_mean=ctl[w].get("pct_mean"),
                    n_kept=ctl[w]["n_kept"], passed=ctl[w]["passed"]) for w in c.WINDOWS}


def bucket_controls(d0, col="bucket", order=BUCKETS):
    """Is a bucket different from a random same-size subset of its window?
    Percentile of the bucket's win / mean among CTRL_ITERS random draws
    (>= 95 good, <= 5 bad). Descriptive only."""
    out = {}
    for b in order:
        k = (d0[col] == b).to_numpy()
        out[str(b)] = ctl_pcts(d0, k)
    return out


def filter_grid(d0, d1):
    """Pre-declared filters. Missing-revenue signals are never removed
    (unknown is not evidence). Returns (results, plateaus, masks)."""
    yoy = d0["yoy"].to_numpy(float)
    known = ~np.isnan(yoy)
    res, masks = {}, {}
    fam = {"drop_below": [], "drop_above": []}
    for t in LOW_GRID:
        keep = ~(known & (yoy < t))
        lab = "drop YoY<%+.2f" % t
        res[lab], masks[lab] = c.report_filter(lab, d0, keep, d1, quiet=True), keep
        fam["drop_below"].append((t, lab))
    for t in HIGH_GRID:
        keep = ~(known & (yoy >= t))
        lab = "drop YoY>=%.2f" % t
        res[lab], masks[lab] = c.report_filter(lab, d0, keep, d1, quiet=True), keep
        fam["drop_above"].append((t, lab))
    for b in BUCKETS:
        if b == "missing":
            continue
        keep = (d0["bucket"] != b).to_numpy()
        lab = "drop bucket %s" % b
        res[lab], masks[lab] = c.report_filter(lab, d0, keep, d1, quiet=True), keep
    plats = {}
    for name, items in fam.items():
        params = [t for t, _ in items]
        for what in ("win", "mean"):
            rec = [res[l]["windows"]["REC"]["kept"][what] for _, l in items]
            old = [res[l]["windows"]["OLD"]["kept"][what] for _, l in items]
            rb = res[items[0][1]]["windows"]["REC"]["base"][what]
            ob = res[items[0][1]]["windows"]["OLD"]["base"][what]
            plats["%s_%s" % (name, what)] = c.plateau(params, rec, old, rb, ob, higher=True)
    return res, plats, masks


def grid_lines(res):
    for lab, r in res.items():
        rw, ow = r["windows"]["REC"], r["windows"]["OLD"]
        ctl = r.get("control_ok")
        _say("    %-18s REC kept %3d %5.1f%% %+5.2f (rm %3d %5.1f%% %+5.2f) | OLD kept %3d %5.1f%% "
             "%+5.2f (rm %3d %5.1f%% %+5.2f) | 7g %s K2 %s I.4 %s"
             % (lab, rw["kept"]["n"], rw["kept"]["win"], rw["kept"]["mean"], rw["removed"]["n"],
                rw["removed"]["win"] if rw["removed"]["n"] else float("nan"),
                rw["removed"]["mean"] if rw["removed"]["n"] else float("nan"),
                ow["kept"]["n"], ow["kept"]["win"], ow["kept"]["mean"], ow["removed"]["n"],
                ow["removed"]["win"] if ow["removed"]["n"] else float("nan"),
                ow["removed"]["mean"] if ow["removed"]["n"] else float("nan"),
                "ok" if r["seven_ok"] else "x", "ok" if r["money_ok"] else "x",
                "PASS" if ctl else "x"))


def analyze_kind(kind, cal, verbose=True):
    ts, b0, b1 = c.base(kind)
    d0 = tag(b0, cal, roll=True)
    d1 = b1.copy()
    for col in ("bucket", "yoy"):
        d1[col] = d0[col].to_numpy()
    out = dict(kind=kind, n=len(d0))
    cov = dict(names=int(d0["sid"].nunique()),
               names_fetched=int(d0.loc[d0["fetched"], "sid"].nunique()),
               signals_fetched=int(d0["fetched"].sum()),
               signals_with_yoy=int((d0["bucket"] != "missing").sum()),
               stale=int(d0["stale"].sum()))
    for w in c.WINDOWS:
        wm = c.wmask(d0, w)
        cov[w] = dict(n=int(wm.sum()), fetched=int(d0.loc[wm, "fetched"].sum()),
                      with_yoy=int((d0.loc[wm, "bucket"] != "missing").sum()))
    miss = d0[d0["bucket"] == "missing"]
    cov["missing_reasons"] = dict(
        not_fetched=int((~miss["fetched"]).sum()),
        no_rows=int((miss["fetched"] & ~miss["has_data"]).sum()),
        month_absent=int((miss["has_data"] & miss["rev_month"].isna()).sum()),
        no_yoy_base=int((miss["rev_month"].notna()).sum()))
    out["coverage"] = cov
    if verbose:
        _say("== %s set: %d signals, %d names; fetched %d names / %d signals; YoY known %d; stale %d"
             % (kind, len(d0), cov["names"], cov["names_fetched"], cov["signals_fetched"],
                cov["signals_with_yoy"], cov["stale"]))
        _say("   missing: %s" % json.dumps(cov["missing_reasons"]))
    tab = bucket_table(d0, d1)
    out["buckets"] = tab
    out["bucket_control"] = bucket_controls(d0)
    if verbose:
        print_table("YoY buckets (rolled 10th, sig > deadline)", tab, BUCKETS)
        for b in BUCKETS:
            x = out["bucket_control"][b]
            _say("      %-8s vs random same-size subset: REC win pct %s mean pct %s | "
                 "OLD win pct %s mean pct %s" % tuple(
                     [b] + ["%5.1f" % v if v is not None and v == v else "  -  "
                            for w in c.WINDOWS for v in (x[w]["pct_win"], x[w]["pct_mean"])]))
    # the 12-month-high flag and the 3-month YoY sign as extra groupings
    d0["high12_s"] = d0["high12"].map({True: "new12h", False: "not12h"}).fillna("na")
    d1["high12_s"] = d0["high12_s"].to_numpy()
    out["high12"] = bucket_table(d0, d1, "high12_s", ("new12h", "not12h", "na"))
    y3 = d0["yoy3"].to_numpy(float)
    d0["yoy3_s"] = np.where(np.isnan(y3), "na", np.where(y3 >= 0, "y3>=0", "y3<0"))
    d1["yoy3_s"] = d0["yoy3_s"].to_numpy()
    out["yoy3"] = bucket_table(d0, d1, "yoy3_s", ("y3<0", "y3>=0", "na"))
    if verbose:
        print_table("12-month revenue high", out["high12"], ("new12h", "not12h", "na"))
        print_table("3-month YoY sign", out["yoy3"], ("y3<0", "y3>=0", "na"))
    # exit mix per bucket (all windows)
    out["exit_mix"] = {b: d0.loc[d0["bucket"] == b, "why"].value_counts().to_dict()
                       for b in BUCKETS}
    # per-year bucket counts (frequency of each group)
    yr = d0["sig"].str.slice(0, 4)
    out["by_year"] = pd.crosstab(yr, d0["bucket"]).reindex(columns=list(BUCKETS), fill_value=0) \
        .to_dict("index")
    dr = dose_response(d0)
    out["dose_response"] = dr
    if verbose:
        for w in c.WINDOWS:
            x = dr[w]
            _say("  dose %s: rho %+.3f (p %.3f, n %d) | win by bucket %s | mean %s"
                 % (w, x["rho"], x["p"], x["n_known"],
                    " ".join("%.1f" % v for v in x["win_by_bucket"]),
                    " ".join("%+.2f" % v for v in x["mean_by_bucket"])))
        _say("  same sign across windows: win %s mean %s rho %s"
             % (dr["same_sign_win"], dr["same_sign_mean"], dr["same_sign_rho"]))
    # sensitivity: the plain 10th (no roll to the next session)
    p0 = tag(b0, cal, roll=False)
    p1 = b1.copy()
    p1["bucket"] = p0["bucket"].to_numpy()
    out["plain10"] = dict(changed=int((p0["bucket"] != d0["bucket"]).sum()),
                          buckets=bucket_table(p0, p1))
    if verbose:
        _say("  plain-10th sensitivity: %d signals change bucket" % out["plain10"]["changed"])
    # filters
    res, plats, masks = filter_grid(d0, d1)
    if verbose:
        _say("  filter grid (missing never removed):")
        grid_lines(res)
        for k, p in plats.items():
            _say("    %s %s" % (k, p["text"]))
    passing = []
    for lab, r in res.items():
        fam = "drop_below" if lab.startswith("drop YoY<") else (
            "drop_above" if lab.startswith("drop YoY>=") else None)
        plat_ok = None
        if fam is not None:
            plat_ok = plats["%s_win" % fam]["ok"] and plats["%s_mean" % fam]["ok"]
        full = bool(r["candidate"] and (plat_ok is True))
        if full:
            passing.append(lab)
    out["filters"] = {lab: dict(_slim(r), failed=_failed(r), control=ctl_pcts(d0, masks[lab]))
                      for lab, r in res.items()}
    out["plateau"] = plats
    out["filters_passing_all"] = passing
    out["filters_passing_control"] = [lab for lab, r in res.items() if r["control_ok"]]
    out["filters_passing_seven"] = [lab for lab, r in res.items() if r["seven_ok"]]
    if verbose:
        _say("  filters passing I.4: %s | seven: %s | ALL (+K.2 +plateau): %s"
             % (out["filters_passing_control"], out["filters_passing_seven"], passing))
    # POST-HOC (chosen after seeing the tables above, so a pass here would
    # still need a fresh sample): the 3-month YoY grid and the 12-month high.
    ph, phm = {}, {}
    y3 = d0["yoy3"].to_numpy(float)
    for t in Y3_GRID:
        keep = ~(~np.isnan(y3) & (y3 < t))
        lab = "posthoc drop yoy3<%+.2f" % t
        ph[lab], phm[lab] = c.report_filter(lab, d0, keep, d1, quiet=True), keep
    keep = (d0["high12_s"] != "new12h").to_numpy()
    lab = "posthoc drop new12h"
    ph[lab], phm[lab] = c.report_filter(lab, d0, keep, d1, quiet=True), keep
    labs = ["posthoc drop yoy3<%+.2f" % t for t in Y3_GRID]
    ph_plat = {}
    for what in ("win", "mean"):
        rec = [ph[l]["windows"]["REC"]["kept"][what] for l in labs]
        old_ = [ph[l]["windows"]["OLD"]["kept"][what] for l in labs]
        ph_plat["yoy3_%s" % what] = c.plateau(
            list(Y3_GRID), rec, old_, ph[labs[0]]["windows"]["REC"]["base"][what],
            ph[labs[0]]["windows"]["OLD"]["base"][what], higher=True)
    out["posthoc"] = {lab: dict(_slim(r), failed=_failed(r), control=ctl_pcts(d0, phm[lab]))
                      for lab, r in ph.items()}
    out["posthoc_plateau"] = ph_plat
    if verbose:
        _say("  POST-HOC filters (picked after looking; missing never removed):")
        grid_lines(ph)
        for lab in ph:
            x = out["posthoc"][lab]["control"]
            _say("    %-24s I.4 pct REC win %.1f mean %.1f | OLD win %.1f mean %.1f"
                 % (lab, x["REC"]["pct_win"], x["REC"]["pct_mean"],
                    x["OLD"]["pct_win"], x["OLD"]["pct_mean"]))
        for k_, p_ in ph_plat.items():
            _say("    %s %s" % (k_, p_["text"]))
    mrows = d0[d0["bucket"] == "missing"]
    out["missing_signals"] = mrows[["sig", "sid", "window", "ret", "why", "rev_month",
                                    "stale"]].to_dict("records")
    if verbose:
        _say("  missing-YoY signals: %s" % "; ".join(
            "%s %s %+.1f %s" % (r["sig"], r["sid"], r["ret"], r["why"]) for _, r in mrows.iterrows()))
    # the signal-level table (for the Write stage / spot checks)
    cols = ["sig", "sid", "window", "ret", "why", "rev_month", "stale", "yoy", "mom",
            "yoy3", "high12", "bucket"]
    out["signals"] = d0[cols].to_dict("records")
    return out


# ============================================================== spot check
SPOT_JSON = os.path.join(OUT_DIR, "p26_spot.json")
_KW_THIS = _K("672c 6708")                    # ben yue (this month)
_KW_LASTY = _K("53bb 5e74 540c 671f")         # qu nian tong qi (same month last year)


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def cmd_spot(n=6):
    """Compare FinMind rev(M) and rev(M-12) of n random research signals
    (seeded) with MOPS t05st10_ifrs (thousand NT$, per company and month).
    Read-only query; a failure is recorded, not fatal."""
    import requests
    cal = c.trade_calendar()
    _, b0, _ = c.base("research")
    d0 = tag(b0, cal)
    k = d0[d0["bucket"] != "missing"]
    rng = np.random.default_rng(c.SEED)
    pick = k.iloc[sorted(rng.choice(len(k), min(n, len(k)), replace=False))]
    res = []
    for _, row in pick.iterrows():
        sid = str(row["sid"])
        y, mth = int(row["rev_month"][:4]), int(row["rev_month"][5:7])
        rev = load_revenue(sid) or {}
        fm, fm12 = rev.get((y, mth)), rev.get(_add_months(y, mth, -12))
        body = dict(companyId=sid, dataType="2", month="%02d" % mth, year=str(y - 1911),
                    subsidiaryCompanyId="")
        rec = dict(sid=sid, sig=row["sig"], rev_month=row["rev_month"], finmind=fm,
                   finmind_m12=fm12, yoy_finmind=row["yoy"])
        try:
            r = requests.post("https://mops.twse.com.tw/mops/api/t05st10_ifrs", json=body,
                              headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            data = (r.json().get("result") or {}).get("data") or []
            vals = {str(a[0]).strip(): _num(a[1]) for a in data if len(a) >= 2}
            this = vals.get(_KW_THIS)
            lasty = vals.get(_KW_LASTY)
            rec.update(mops=this, mops_m12=lasty)
            if this is not None and fm is not None:
                rec["match"] = abs(fm / 1000.0 - this) <= 1.0
            if lasty is not None and fm12 is not None:
                rec["match_m12"] = abs(fm12 / 1000.0 - lasty) <= 1.0
        except Exception as e:
            rec["err"] = repr(e)[:200]
        res.append(rec)
        _say("  %s" % json.dumps(jsonable(rec)))
        time.sleep(3)
    json.dump(jsonable(res), open(SPOT_JSON, "w", encoding="utf-8"), indent=1)
    _say("spot check -> %s" % SPOT_JSON)


# ================================================================== main
def cmd_analyze():
    cal = c.trade_calendar()
    t0 = time.time()
    res = dict(item="P2-6", rule="sandbox_money.BASE (scanner.exit_rules.DEFAULT_RULE)",
               slip=c.SLIP, recent_from=c.RECENT_FROM, source="FinMind %s anonymous" % DATASET,
               availability="10th of M+1 rolled to the next session; visible when sig > it",
               buckets=list(BUCKETS), low_grid=list(LOW_GRID), high_grid=list(HIGH_GRID))
    for kind in KINDS:
        res[kind] = analyze_kind(kind, cal)
    fails = {}
    if os.path.exists(FAIL_LOG):
        fails = json.load(open(FAIL_LOG, encoding="utf-8"))
    res["fetch_failures"] = fails
    if os.path.exists(SPOT_JSON):
        res["mops_spot_check"] = json.load(open(SPOT_JSON, encoding="utf-8"))
    allp = res["research"]["filters_passing_all"] + res["live"]["filters_passing_all"]
    res["verdict"] = "info" if not allp else "candidate (not shipped this round)"
    res["runtime_s"] = round(time.time() - t0, 1)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(jsonable(res), open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    _say("verdict: %s | wrote %s (%.0fs)" % (res["verdict"], OUT_JSON, res["runtime_s"]))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    globals()["cmd_" + cmd]()


if __name__ == "__main__":
    main()
