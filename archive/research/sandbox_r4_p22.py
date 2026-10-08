"""
sandbox_r4_p22.py -- round 4, item P2-2 (2026-10-08). ASCII only.

Question: should a signal be skipped when a VOLUME-CLIMAX REVERSAL bar sits
just before it (the 8227 shape: a huge-volume black candle on 2026-10-01,
four sessions before its 10-07 re-signal)?

Rule tested (an exclusion filter on the shipped CORE+ trades):
  skip the signal when, among the stock's last w trading bars, some bar had
      vr  = Volume_Lot / mean(Volume_Lot of the 20 bars BEFORE it)  >= k
  AND a reversal candle:
      close < open  (black candle)  OR  an upper shadow >= the body
      (shadow = high - max(open, close) must be > 0: a flat one-price bar
       is not a shadow; the literal "0 >= 0" reading is counted separately)
  grid: w in W_GRID, k in K_GRID (the task grid); K_CONTEXT adds k = 2, 3
  because the motivating 8227 bar is only vr ~3.8 (its 20-day mean was
  already inflated by the run-up) -- the task grid cannot see it.
  Two anchors: 'incl' = the w bars ending AT the signal bar (the signal bar
  is the day before the entry, as plan line 247 words it); 'excl' = the w
  bars strictly before the signal bar ("within the last w sessions before
  the signal"). Both are known at the signal close, so neither looks ahead.

Measured only under the shipped rule (r4_common.BASE = sandbox_money.BASE,
built from scanner.exit_rules.DEFAULT_RULE), slip 0 and the SLIP stress,
REC (sig >= 2023-09-18) and OLD windows, every gate of r4_common.
report_filter (seven gates, K.2 money gates, the I.4 same-count random
deletion control at 300 draws), the plateau over k and over w, the J.7
ungated control and the live-frequency set.

Data: research_prices.db, read-only, through r4_common.load_bars; bars on
the research session calendar with Volume_Lot > 0 (the universe builder's
rule), so w counts the stock's own trading bars. Price-seam guard: a bar
whose close moved more than LIMIT_GUARD from the previous close cannot be a
real session move under the 10% limit band, so its vr is not used.

    PYTHONDONTWRITEBYTECODE=1 python -X utf8 archive/research/sandbox_r4_p22.py
Output: printed tables + JSON at $R4_OUT_DIR/P2-2.json (default CACHE/results).
Optional: $R4_8227_CSV = a Date,Open,High,Low,Close,Volume CSV of 8227 to
print the out-of-sample motivating bar (the research DB ends 2026-09-09).
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import r4_common as c                               # noqa: E402

W_GRID = (1, 3, 5)
K_GRID = (5, 8, 12, 16)          # the task grid
K_CONTEXT = (2, 3)               # context cells (8227's bar is vr ~3.8)
K_ALL = tuple(sorted(K_CONTEXT + K_GRID))
ANCHORS = ("incl", "excl")
LIMIT_GUARD = 0.11               # beyond the 10% daily limit band = a price seam
VR_LOOKBACK = 20
MONEY_SLOTS = (3, 5, 8)
DOSE_EDGES = (1.5, 2.0, 3.0, 5.0, 8.0)

OUT_DIR = os.environ.get("R4_OUT_DIR") or os.path.join(c.CACHE, "results")
OUT_JSON = os.path.join(OUT_DIR, "P2-2.json")


def _say(*a):
    print(*a)
    sys.stdout.flush()


# ================================================================== bars
def bar_features(b):
    """Per-stock bar flags. b: one stock's bars sorted by date (session
    calendar, Volume_Lot > 0). Returns a dict of numpy arrays."""
    o = b["open"].to_numpy(float)
    h = b["high"].to_numpy(float)
    lo = b["low"].to_numpy(float)
    cl = b["close"].to_numpy(float)
    v = b["Volume_Lot"].to_numpy(float)
    vs = pd.Series(v)
    vma = vs.shift(1).rolling(VR_LOOKBACK, min_periods=VR_LOOKBACK).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = v / vma
        prev = np.r_[np.nan, cl[:-1]]
        seam = np.abs(cl / prev - 1) > LIMIT_GUARD
    vr_raw = vr.copy()
    vr = np.where(seam, np.nan, vr)
    body = np.abs(cl - o)
    ush = h - np.maximum(o, cl)
    red = cl < o
    shadow = (ush > 0) & (ush >= body)
    bad = red | shadow
    bad_lit = red | (ush >= body)          # literal reading: a flat bar counts
    rng = h - lo
    with np.errstate(divide="ignore", invalid="ignore"):
        cpos = np.where(rng > 0, (cl - lo) / rng, np.nan)
        v5 = vs.rolling(5, min_periods=5).mean().to_numpy()
        v20 = vs.rolling(20, min_periods=20).mean().to_numpy()
        dry = v5 / v20
    return dict(date=b["date"].to_numpy().astype("U10"), vr=vr, vr_raw=vr_raw,
                seam=seam, bad=bad, bad_lit=bad_lit, red=red, shadow=shadow,
                cpos=cpos, dry=dry)


def load_bar_map(sids):
    cal = set(c.trade_calendar())
    raw = c.load_bars(sids)
    raw = raw[raw["date"].isin(cal) & (raw["Volume_Lot"] > 0)]
    return {sid: bar_features(b.reset_index(drop=True)) for sid, b in raw.groupby("stock_id")}


def _nanmax(x):
    x = x[~np.isnan(x)]
    return float(x.max()) if len(x) else np.nan


def climax_table(df, bars):
    """One row per trade (aligned with df): for each anchor and w the
    largest vr among the window's reversal bars (bad), all bars (all), the
    non-reversal bars (good) and the literal-shadow reading (badlit); plus
    signal-day candle position and dryup for the overlap check."""
    rows = []
    for sid, sig in zip(df["sid"].astype(str), df["sig"]):
        b = bars.get(sid)
        rec = dict(sig_bar=False, cpos=np.nan, dry=np.nan, seam_hits=0)
        if b is None:
            rows.append(rec)
            continue
        d = b["date"]
        i_in = int(np.searchsorted(d, sig, side="right")) - 1
        i_ex = int(np.searchsorted(d, sig, side="left")) - 1
        rec["sig_bar"] = bool(i_in >= 0 and d[i_in] == sig)
        if rec["sig_bar"]:
            rec["cpos"] = b["cpos"][i_in]
            rec["dry"] = b["dry"][i_in]
        for anchor, i in (("incl", i_in), ("excl", i_ex)):
            for w in W_GRID:
                key = "%s_w%d" % (anchor, w)
                if i < 0:
                    for kind in ("bad", "all", "good", "badlit"):
                        rec["%s_%s" % (key, kind)] = np.nan
                    continue
                seg = slice(max(i - w + 1, 0), i + 1)
                vr, bad, badl = b["vr"][seg], b["bad"][seg], b["bad_lit"][seg]
                rec[key + "_bad"] = _nanmax(vr[bad])
                rec[key + "_all"] = _nanmax(vr)
                rec[key + "_good"] = _nanmax(vr[~bad])
                rec[key + "_badlit"] = _nanmax(vr[badl])
                if anchor == "incl" and w == max(W_GRID):
                    # bars whose raw vr would have flagged but the seam guard dropped
                    raw = b["vr_raw"][seg]
                    rec["seam_hits"] = int(np.sum(b["seam"][seg] & bad
                                                  & (np.nan_to_num(raw) >= min(K_ALL))))
        rows.append(rec)
    return pd.DataFrame(rows)


def flag_of(X, anchor, w, k, kind="bad"):
    x = X["%s_w%d_%s" % (anchor, w, kind)].to_numpy(float)
    return np.nan_to_num(x, nan=-1.0) >= k


# =============================================================== scoring
def _f(x):
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return x
    return None if xf != xf else round(xf, 4)


def cell_summary(r, flag, base_df):
    """Compact, JSON-ready summary of one report_filter result."""
    out = dict(seven_ok=r["seven_ok"], money_ok=r["money_ok"],
               control_ok=r["control_ok"], candidate=r["candidate"],
               flagged=int(flag.sum()), windows={})
    for w in c.WINDOWS:
        x = r["windows"][w]
        wm = c.wmask(base_df, w)
        ctl = x.get("_ctl")
        out["windows"][w] = dict(
            base=x["base"], kept=x["kept"], removed=x["removed"],
            slip_base=x.get("slip_base"), slip_kept=x.get("slip_kept"),
            boot_win=x["boot_win"], boot_mean=x["boot_mean"],
            halves_base=x["halves_base"], halves_kept=x["halves_kept"],
            quarters=x["quarters"], diff_ci=x["diff_ci"], ev=x["ev"],
            gates=x["gates"], money_gates=x["money_gates"],
            removed_n=int((flag & wm).sum()), control=ctl)
    out["gaps"] = r["gaps"]
    out["peak"] = r["peak"]
    if "money" in r:
        out["money"] = {k: v.drop(columns=["yearly"]).to_dict("records")
                        for k, v in r["money"].items()}
    return out


def run_cell(label, b0, b1, flag, slots=MONEY_SLOTS, ctrl=True):
    keep = ~flag
    ctl = c.random_deletion_control(b0, keep) if ctrl else None
    r = c.report_filter(label, b0, keep, base_slip=b1, slots=slots, ctrl=False, quiet=True)
    # report_filter was run without its own control (to keep one draw set);
    # fold the control in exactly as report_filter would
    if ctl is not None:
        r["control_ok"] = bool(ctl["passed"])
        r["candidate"] = bool(r["seven_ok"] and r["money_ok"] and ctl["passed"])
        for w in c.WINDOWS:
            r["windows"][w]["_ctl"] = {k: v for k, v in ctl[w].items()}
    return cell_summary(r, flag, b0)


def cell_line(tag, s):
    parts = [tag]
    for w in c.WINDOWS:
        x = s["windows"][w]
        rm, kp, bs = x["removed"], x["kept"], x["base"]
        ctl = x.get("control") or {}
        pw = ctl.get("pct_win", np.nan)
        pm = ctl.get("pct_mean", np.nan)
        sk = x.get("slip_kept") or {}
        parts.append("%s rem %3d %5.1f%% %+6.2f | kept %5.1f%% %+5.2f (base %5.1f %+5.2f) "
                     "slip %5.1f %+5.2f | ctl %3.0f/%3.0f"
                     % (w, rm["n"], rm["win"] if rm["n"] else np.nan,
                        rm["mean"] if rm["n"] else np.nan, kp["win"], kp["mean"],
                        bs["win"], bs["mean"], sk.get("win", np.nan), sk.get("mean", np.nan),
                        pw, pm))
    parts.append("7g %s K2 %s I4 %s" % ("ok" if s["seven_ok"] else "x",
                                       "ok" if s["money_ok"] else "x",
                                       "ok" if s["control_ok"] else "x"))
    return " || ".join(parts)


def grid(b0, b1, X, label, kinds=("bad",), ks=K_ALL, slots=MONEY_SLOTS, ctrl=True):
    out = {}
    for kind in kinds:
        for anchor in ANCHORS:
            for w in W_GRID:
                for k in ks:
                    flag = flag_of(X, anchor, w, k, kind)
                    tag = "%s %-6s %s w=%d k=%2d" % (label, kind, anchor, w, k)
                    s = run_cell(tag, b0, b1, flag, slots=slots if k in K_GRID else None,
                                 ctrl=ctrl)
                    out["%s|%s|%d|%d" % (kind, anchor, w, k)] = s
                    _say(cell_line(tag, s))
    return out


def plateaus(cells, b0, kind="bad", ks=K_ALL):
    """J.7 plateau, both windows overlaid: over k for each (anchor, w) and
    over w for each (anchor, k), on kept win and on kept mean."""
    rec_b, old_b = c.stats(b0[c.wmask(b0, "REC")]), c.stats(b0[c.wmask(b0, "OLD")])
    out = {}

    def one(keys, params, tag):
        res = {}
        for metric in ("win", "mean"):
            rec = [cells[k]["windows"]["REC"]["kept"][metric] for k in keys]
            old = [cells[k]["windows"]["OLD"]["kept"][metric] for k in keys]
            p = c.plateau(list(params), rec, old, rec_b[metric], old_b[metric])
            res[metric] = dict(beats=p["beats"], run=p["run"], best_rec=p["best_rec"],
                               best_old=p["best_old"], agree=p["agree"], edge=p["edge"],
                               ok=p["ok"], text=p["text"])
            _say("  %-26s %-4s %s" % (tag, metric, p["text"]))
        return res

    for anchor in ANCHORS:
        for w in W_GRID:
            keys = ["%s|%s|%d|%d" % (kind, anchor, w, k) for k in ks]
            out["%s|w%d|over_k" % (anchor, w)] = one(keys, ks, "%s w=%d over k" % (anchor, w))
        for k in ks:
            keys = ["%s|%s|%d|%d" % (kind, anchor, w, k) for w in W_GRID]
            out["%s|k%d|over_w" % (anchor, k)] = one(keys, W_GRID, "%s k=%d over w" % (anchor, k))
    return out


def bucket_stats(df, mask):
    out = {}
    for w in c.WINDOWS:
        s = c.stats(df[mask & c.wmask(df, w)])
        out[w] = s
    pooled = c.stats(df[mask])
    out["ALL"] = pooled
    return out


def _bline(name, st):
    def one(s):
        return ("n=%3d %5.1f%% %+6.2f" % (s["n"], s["win"], s["mean"])) if s["n"] else "n=  0"
    return "  %-22s REC %s | OLD %s | ALL %s" % (name, one(st["REC"]), one(st["OLD"]), one(st["ALL"]))


def dose(df, X, anchor="incl", w=5, kind="bad"):
    x = X["%s_w%d_%s" % (anchor, w, kind)].to_numpy(float)
    edges = (-np.inf,) + DOSE_EDGES + (np.inf,)
    out = {}
    none = np.isnan(x)
    out["no bar"] = bucket_stats(df, none)
    _say(_bline("no %s bar w/ vr" % kind, out["no bar"]))
    for a, b in zip(edges[:-1], edges[1:]):
        m = (~none) & (x >= a) & (x < b)
        name = "vr %s..%s" % ("" if a == -np.inf else "%g" % a, "" if b == np.inf else "%g" % b)
        out[name] = bucket_stats(df, m)
        _say(_bline(name, out[name]))
    return out


def wilson(k, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (100 * (mid - half), 100 * (mid + half))


# ======================================================= side analyses
def _disposal_periods():
    """TPEX disposal periods (sid, start, end) from the P2-1 scratch JSON,
    when $R4_DISPOSAL_DIR holds tpex_disposal_YYYY.json. Fields by name."""
    d = os.environ.get("R4_DISPOSAL_DIR")
    if not d or not os.path.isdir(d):
        return None
    code_f = u"\u8b49\u5238\u4ee3\u865f"
    per_f = u"\u8655\u7f6e\u8d77\u8a16\u6642\u9593"
    seen = set()
    for name in sorted(os.listdir(d)):
        if not (name.startswith("tpex_disposal_") and name[14:18].isdigit()):
            continue
        j = json.load(open(os.path.join(d, name), encoding="utf-8"))
        for t in j.get("tables", []):
            f = t.get("fields", [])
            if code_f not in f or per_f not in f:
                continue
            ic, ip = f.index(code_f), f.index(per_f)
            for row in t.get("data", []):
                sid = str(row[ic]).strip()
                per = str(row[ip]).replace(u"\uff5e", "~")
                if not sid or "~" not in per:
                    continue
                try:
                    a, b = [x.strip().split("/") for x in per.split("~")[:2]]
                    s = "%04d-%02d-%02d" % (int(a[0]) + 1911, int(a[1]), int(a[2]))
                    e = "%04d-%02d-%02d" % (int(b[0]) + 1911, int(b[1]), int(b[2]))
                except (ValueError, IndexError):
                    continue
                seen.add((sid, s, e))
    return sorted(seen)


def ent_disp_flags(df, periods):
    by = {}
    for sid, s, e in periods:
        by.setdefault(sid, []).append((s, e))
    return np.array([any(s <= ent <= e for s, e in by.get(str(sid), []))
                     for sid, ent in zip(df["sid"], df["entry"])])


def case_8227():
    """The motivating bar, out of sample (research DB ends 2026-09-09)."""
    p = os.environ.get("R4_8227_CSV")
    if not p or not os.path.exists(p):
        return None
    d = pd.read_csv(p).dropna(subset=["Open", "High", "Low", "Close"])
    b = pd.DataFrame({"date": d["Date"].astype(str), "open": d["Open"], "high": d["High"],
                      "low": d["Low"], "close": d["Close"], "Volume_Lot": d["Volume"] / 1000.0})
    out = {}
    for i in range(VR_LOOKBACK, len(b)):
        v = float(b["Volume_Lot"].iloc[i])
        m = float(b["Volume_Lot"].iloc[i - VR_LOOKBACK:i].mean())
        o, h, cl = (float(b[x].iloc[i]) for x in ("open", "high", "close"))
        ush, body = h - max(o, cl), abs(cl - o)
        out[b["date"].iloc[i]] = dict(vr=round(v / m, 2), red=cl < o,
                                      shadow=bool(ush > 0 and ush >= body))
    return dict(bars=len(b), first=b["date"].iloc[0], last=b["date"].iloc[-1], vr=out)


# ================================================================== main
def main():
    t0 = time.time()
    res = dict(item="P2-2", built=time.strftime("%Y-%m-%d %H:%M"),
               grid=dict(w=W_GRID, k=K_GRID, k_context=K_CONTEXT, anchors=ANCHORS,
                         limit_guard=LIMIT_GUARD, vr_lookback=VR_LOOKBACK),
               rule=dict(c.BASE), slip=c.SLIP, ctrl_iters=c.CTRL_ITERS)

    U = {}
    for name, kind, gate in (("research", "research", "core"),
                             ("ungated", "research", "ungated"),
                             ("live", "live", "core")):
        ts, b0, b1 = c.base(kind, gate)
        U[name] = (b0, b1)
    sids = sorted(set().union(*[set(v[0]["sid"].astype(str)) for v in U.values()]))
    bars = load_bar_map(sids)
    _say("bars for %d sids loaded (%.0fs)" % (len(bars), time.time() - t0))

    X = {name: climax_table(v[0], bars) for name, v in U.items()}
    b0, b1 = U["research"]
    Xr = X["research"]
    res["base"] = {w: dict(slip0=c.stats(b0[c.wmask(b0, w)]), slip=c.stats(b1[c.wmask(b1, w)]))
                   for w in c.WINDOWS}
    res["coverage"] = {name: dict(trades=len(x), sig_bar=int(x["sig_bar"].sum()),
                                  seam_dropped=int(x["seam_hits"].sum()))
                       for name, x in X.items()}
    _say("baseline research %s" % {w: (v["slip0"]["n"], round(v["slip0"]["win"], 2),
                                       round(v["slip0"]["mean"], 2)) for w, v in res["base"].items()})
    _say("coverage %s" % res["coverage"])

    # vr distribution on the signal bar and the 5-bar window (context for k)
    q = [50, 90, 99, 99.9]
    res["vr_quantiles"] = dict(
        sig_bar=np.nanpercentile(Xr["incl_w1_all"], q).round(2).tolist(),
        w5_max=np.nanpercentile(Xr["incl_w5_all"], q).round(2).tolist(),
        w5_bad_max=np.nanpercentile(Xr["incl_w5_bad"].dropna(), q).round(2).tolist(),
        pct=q)
    _say("vr quantiles p50/p90/p99/p99.9: %s" % res["vr_quantiles"])

    # where do volume bursts sit relative to the signal bar? (offset 0 = the
    # signal bar). The Launch_Score 'young' leg rewards a small ret5, so a
    # name tends to enter the list when its burst bar becomes the ret5 base.
    prof = []
    for off in range(8):
        vals, badv = [], []
        for sid, sig in zip(b0["sid"].astype(str), b0["sig"]):
            b = bars.get(sid)
            if b is None:
                continue
            j = int(np.searchsorted(b["date"], sig, side="right")) - 1 - off
            if j >= 0 and b["vr"][j] == b["vr"][j]:
                vals.append(b["vr"][j])
                badv.append(bool(b["bad"][j]))
        v = np.array(vals)
        bv = np.array(badv)
        prof.append(dict(offset=off, n=len(v), median=float(np.median(v)),
                         p90=float(np.percentile(v, 90)), share_ge3=float((v >= 3).mean()),
                         share_ge5=float((v >= 5).mean()),
                         share_ge5_bad=float(((v >= 5) & bv).mean())))
    res["offset_profile"] = prof
    _say("vr by offset before the signal bar (research CORE+):")
    for r in prof:
        _say("  off %d: median %.2f p90 %.2f  share vr>=3 %.3f  vr>=5 %.3f  vr>=5&reversal %.3f"
             % (r["offset"], r["median"], r["p90"], r["share_ge3"], r["share_ge5"],
                r["share_ge5_bad"]))

    # flagged counts per cell (n per cell) for every reading of the rule
    counts = {}
    for kind in ("bad", "badlit", "all"):
        for anchor in ANCHORS:
            for w in W_GRID:
                for k in K_ALL:
                    f = flag_of(Xr, anchor, w, k, kind)
                    counts["%s|%s|%d|%d" % (kind, anchor, w, k)] = dict(
                        REC=int((f & c.wmask(b0, "REC")).sum()),
                        OLD=int((f & c.wmask(b0, "OLD")).sum()))
    res["counts"] = counts
    _say("\n== n flagged per cell (REC/OLD), reversal candle required")
    for anchor in ANCHORS:
        for w in W_GRID:
            _say("  %s w=%d: %s" % (anchor, w, "  ".join(
                "k=%d %d/%d" % (k, counts["bad|%s|%d|%d" % (anchor, w, k)]["REC"],
                                counts["bad|%s|%d|%d" % (anchor, w, k)]["OLD"]) for k in K_ALL)))
    diff = sum(abs(counts["badlit" + k[3:]]["REC"] - counts[k]["REC"])
               + abs(counts["badlit" + k[3:]]["OLD"] - counts[k]["OLD"])
               for k in counts if k.startswith("bad|"))
    res["literal_shadow_extra_flags"] = int(diff)
    _say("  literal 'shadow >= body' reading (flat bars count) adds %d flags over all cells" % diff)

    _say("\n== research CORE+ (556): the filter, every cell (I.4 control %d draws)" % c.CTRL_ITERS)
    cells = grid(b0, b1, Xr, "core")
    res["cells"] = cells
    _say("\n== plateau (both windows overlaid), reversal-candle filter")
    res["plateau"] = plateaus(cells, b0)

    _say("\n== no-candle control: volume alone (vr >= k), research CORE+")
    res["cells_volume_only"] = grid(b0, b1, Xr, "core", kinds=("all",), slots=None)
    _say("\n== complement: high volume WITHOUT a reversal candle")
    res["cells_good_candle"] = grid(b0, b1, Xr, "core", kinds=("good",), slots=None)

    _say("\n== dose response: max vr of a reversal bar in the 5 bars ending at the signal")
    res["dose_bad_incl_w5"] = dose(b0, Xr, "incl", 5, "bad")
    _say("== dose response: max vr of ANY bar in the 5 bars ending at the signal")
    res["dose_all_incl_w5"] = dose(b0, Xr, "incl", 5, "all")
    _say("== dose response: excl anchor (5 bars before the signal bar), reversal bars")
    res["dose_bad_excl_w5"] = dose(b0, Xr, "excl", 5, "bad")

    # pooled removed bucket for the largest task cells, with a Wilson CI
    res["pooled_removed"] = {}
    _say("\n== pooled removed bucket (both windows), Wilson 95% CI on win")
    for anchor in ANCHORS:
        for k in (3, 5):
            f = flag_of(Xr, anchor, 5, k)
            r = b0["ret"].to_numpy(float)[f]
            lo, hi = wilson(int((r > 0).sum()), len(r))
            res["pooled_removed"]["%s|5|%d" % (anchor, k)] = dict(
                n=len(r), win=_f(100 * (r > 0).mean()) if len(r) else None,
                mean=_f(r.mean()) if len(r) else None, wilson=(_f(lo), _f(hi)))
            _say("  %s w=5 k=%d: n=%d win %.1f%% [%.1f..%.1f] mean %+.2f (base all %.1f%% %+.2f)"
                 % (anchor, k, len(r), 100 * (r > 0).mean() if len(r) else np.nan, lo, hi,
                    r.mean() if len(r) else np.nan,
                    100 * (b0["ret"] > 0).mean(), b0["ret"].mean()))

    # J.7 ungated control and the live-frequency set
    for name in ("ungated", "live"):
        u0, u1 = U[name]
        _say("\n== %s set (%d trades): the same filter" % (name, len(u0)))
        for w in c.WINDOWS:
            s = c.stats(u0[c.wmask(u0, w)])
            _say("  base %s n=%d %.2f%% %+.2f" % (w, s["n"], s["win"], s["mean"]))
        res["cells_" + name] = grid(u0, u1, X[name], name, slots=None)

    # overlap with the rejected candle_pos / dryup axes and with ATR
    _say("\n== overlap: signal-day candle position and dryup, flagged vs not (incl w=5)")
    res["overlap"] = {}
    for k in (3, 5):
        f = flag_of(Xr, "incl", 5, k)
        o = dict(
            cpos_flag=_f(np.nanmedian(Xr["cpos"][f])) if f.any() else None,
            cpos_rest=_f(np.nanmedian(Xr["cpos"][~f])),
            dry_flag=_f(np.nanmedian(Xr["dry"][f])) if f.any() else None,
            dry_rest=_f(np.nanmedian(Xr["dry"][~f])))
        at = b0["atr"].to_numpy(float)
        cuts = np.nanpercentile(at, [100 / 3.0, 200 / 3.0])
        terc = np.digitize(at, cuts)
        o["atr_cuts"] = [_f(x) for x in cuts]
        o["atr"] = {}
        for tb in range(3):
            m_ = terc == tb
            o["atr"]["T%d" % (tb + 1)] = dict(flag=bucket_stats(b0, m_ & f),
                                             rest=bucket_stats(b0, m_ & ~f))
            _say(_bline("k=%d ATR T%d flagged" % (k, tb + 1), o["atr"]["T%d" % (tb + 1)]["flag"]))
            _say(_bline("k=%d ATR T%d rest" % (k, tb + 1), o["atr"]["T%d" % (tb + 1)]["rest"]))
        _say("  k=%d median candle_pos flagged %s rest %s | dryup flagged %s rest %s"
             % (k, o["cpos_flag"], o["cpos_rest"], o["dry_flag"], o["dry_rest"]))
        res["overlap"]["incl|5|%d" % k] = o

    periods = _disposal_periods()
    if periods is not None:
        ed = ent_disp_flags(b0, periods)
        res["disposal"] = dict(periods=len(periods), ent_disp=int(ed.sum()), cross={})
        _say("\n== cross-tab with P2-1 entry-inside-disposal (%d periods, %d trades in disposal)"
             % (len(periods), int(ed.sum())))
        for anchor in ANCHORS:
            for k in (3, 5):
                f = flag_of(Xr, anchor, 5, k)
                tab = {}
                for fn, fm in (("flag", f), ("rest", ~f)):
                    for dn, dm in (("disp", ed), ("nodisp", ~ed)):
                        tab["%s_%s" % (fn, dn)] = bucket_stats(b0, fm & dm)
                res["disposal"]["cross"]["%s|5|%d" % (anchor, k)] = tab
                _say("  %s w=5 k=%d: flagged&disp %s | flagged&not %s"
                     % (anchor, k, tab["flag_disp"]["ALL"]["n"], tab["flag_nodisp"]["ALL"]["n"]))
                _say(_bline("  flagged & disp", tab["flag_disp"]))
                _say(_bline("  flagged & not", tab["flag_nodisp"]))

    cs = case_8227()
    if cs is not None:
        res["case_8227"] = cs
        sel = {d: v for d, v in cs["vr"].items() if d >= "2026-09-08"}
        _say("\n== 8227 out of sample (vr vs prior 20 bars): %s" % sel)

    # verdict inputs
    task = {k: v for k, v in cells.items() if int(k.split("|")[3]) in K_GRID}
    res["summary"] = dict(
        any_candidate=any(v["candidate"] for v in task.values()),
        any_seven=any(v["seven_ok"] for v in task.values()),
        any_control=any(v["control_ok"] for v in task.values()),
        max_removed=max(v["windows"]["REC"]["removed"]["n"] + v["windows"]["OLD"]["removed"]["n"]
                        for v in task.values()),
        cells_bucket_ok=sum(1 for v in task.values()
                            if all(v["windows"][w]["gates"]["bucket"] for w in c.WINDOWS)),
        both_windows_removed_worse=[k for k, v in task.items()
                                    if all(v["windows"][w]["removed"]["n"] > 0
                                           and v["windows"][w]["removed"]["mean"]
                                           < v["windows"][w]["base"]["mean"]
                                           for w in c.WINDOWS)])
    _say("\nsummary %s" % res["summary"])
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="ascii") as fh:
        json.dump(_clean(res), fh, default=_json_default, indent=1, ensure_ascii=True,
                  allow_nan=False)
    _say("wrote %s (%.0fs)" % (OUT_JSON, time.time() - t0))
    return res


def _clean(o):
    """Recursively turn numpy scalars / NaN into JSON-safe values."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, pd.DataFrame):
        return _clean(o.to_dict("records"))
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if (o != o or o in (np.inf, -np.inf)) else round(float(o), 4)
    return o


def _json_default(o):
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if o != o else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, pd.DataFrame):
        return o.to_dict("records")
    if isinstance(o, tuple):
        return list(o)
    return str(o)


def strict_plateau(cells, prefix="bad", ks=K_ALL):
    """c.plateau counts a no-op cell (nothing removed, kept == base) as
    'beats'; at k >= 12 most cells remove nothing, so that reads as a fake
    plateau. Here a cell beats only when it removes >= 1 trade in BOTH
    windows and kept >= base in both windows."""
    out = {}
    for anchor in ANCHORS:
        for w in W_GRID:
            for metric in ("win", "mean"):
                beats = []
                for k in ks:
                    x = cells["%s|%s|%d|%d" % (prefix, anchor, w, k)]["windows"]
                    ok = all(x[v]["removed"]["n"] > 0
                             and x[v]["kept"][metric] is not None
                             and x[v]["kept"][metric] >= x[v]["base"][metric] - 1e-9
                             for v in c.WINDOWS)
                    beats.append(bool(ok))
                run, best, cur = [], [], []
                for k, b in zip(ks, beats):
                    cur = cur + [k] if b else []
                    if len(cur) > len(best):
                        best = list(cur)
                key = "%s|w%d|%s" % (anchor, w, metric)
                out[key] = dict(ks=list(ks), beats=beats, longest_run=best, ok=len(best) >= 3)
                _say("  strict %-12s %-4s beats %s longest run %s"
                     % ("%s w=%d" % (anchor, w), metric,
                        "".join("Y" if b else "." for b in beats), best))
    return out


def cmd_post():
    """Add the strict plateau to an existing P2-2.json (no re-run)."""
    res = json.load(open(OUT_JSON, encoding="ascii"))
    _say("== strict plateau (cells that remove >= 1 trade in both windows), research CORE+")
    res["plateau_strict"] = strict_plateau(res["cells"])
    for name in ("ungated", "live"):
        if "cells_" + name in res:
            _say("== strict plateau, %s set" % name)
            res["plateau_strict_" + name] = strict_plateau(res["cells_" + name])
    with open(OUT_JSON, "w", encoding="ascii") as fh:
        json.dump(_clean(res), fh, indent=1, ensure_ascii=True, allow_nan=False)
    _say("updated %s" % OUT_JSON)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "post":
        cmd_post()
    else:
        main()
        cmd_post()
