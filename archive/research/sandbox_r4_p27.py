"""
sandbox_r4_p27.py -- round 4, item P2-7 (2026-10-08). ASCII only.

Question: the research fill is the BOARD-LOT open of the first forward bar.
A small-capital owner buys ODD LOTS (intraday odd-lot trading, TPEX since
2020-10-26: first call auction 09:10, then periodic auctions). What price does
the odd-lot book actually give on the entry day, relative to that open, and
what does the shipped rule earn when the entry is filled there?

What is measured (TPEX oddQuote, one file per date, no timestamps):
  odd_first  = the day's FIRST odd-lot match price (field "shou bi cheng jiao
               jia"). Normally the 09:10 auction; when nothing crosses there it
               is a later auction -- the endpoint gives no time.
  odd_vwap   = odd-lot traded value / traded shares (a whole-session average,
               i.e. "buy at a random time of the day")
  odd_last   = the last odd-lot match (13:30 auction), the exit-side analogue
               of the board-lot close for a time exit
  open/close = the RAW board-lot open / close of the same sid and date from
               TPEX dailyQuotes (P2-1 cache, else fetched here). Same day, same
               raw basis, so ratio = odd / raw cancels the yfinance dividend
               adjustment of the research bars. Odd prices are NEVER compared
               with the research (adjusted) open directly.
  bps        = (odd - board) / board * 1e4. Positive = the odd lot cost MORE.

Re-measure under the shipped rule (r4_common.replay -> sandbox_money.BASE ->
scanner.exit_rules.DEFAULT_RULE; no thresholds in this file), both windows,
slip 0 and the r4 slip stress:
  B  "app levels": the exit path is unchanged (holding_tracker anchors every
     level to Entry_Open, the board-lot open -- what the app tells the owner);
     only the entry price moves to open * ratio.
       ret_B = (1 + ret/100) * E / E_odd - 1
  A  "fill levels" (brief's method): o[0] := clip(o[0] * ratio, l0, h0) and
     replay, so stop/lock/tp/late re-anchor to the odd fill.
  RT round trip, on top of B: exits that fill AT THE OPEN (late, gap through
     tp/stop/lock) are moved to the exit day's odd_first / raw open; time exits
     to odd_last / raw close; intraday limit exits (stop/lock/tp touched inside
     the bar) keep the research price (a resting odd-lot limit order).
  'pre'    entry before 2020-10-26 (no intraday odd-lot market)
  'noreg'  no raw TPEX quote for the sid that day (board switcher)
  'nofill' no odd-lot trade that day
  Trades without an odd price keep the board-lot open ("hybrid" set), and the
  paired comparison is also shown on the odd-priced subset only.

Commands:
  python archive/research/sandbox_r4_p27.py fetch     # ~35 min, resumable
  python archive/research/sandbox_r4_p27.py analyze   # -> scratchpad/r4/P2-7.json
Run with PYTHONDONTWRITEBYTECODE=1.
"""
import json
import math
import os
import random
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import r4_common as c                              # noqa: E402  (chdir ROOT)
import sandbox_r4_p21 as p21                       # noqa: E402  raw-quote parser + cache
from ingestion.inst_history import _K, _get_json   # noqa: E402
from scanner.tick import tick_size                 # noqa: E402

g, m = c.g, c.m

SCRATCH = os.environ.get(
    "P27_SCRATCH",
    "C:/Users/luke4/AppData/Local/Temp/claude/D--YenTool/"
    "1624a313-b1ce-4a13-be0b-5e480d6c444b/scratchpad")
OUT_DIR = os.path.join(SCRATCH, "r4")
OUT_JSON = os.path.join(OUT_DIR, "P2-7.json")
ODD_DIR = os.path.join(OUT_DIR, "oddlot")

ODD_FROM = "2020-10-26"        # TPEX intraday odd-lot trading began
ERA_SPLIT = "2022-12-19"       # reported switch of the auction interval (label only)
PAUSE = 2.0                    # seconds between exchange requests (+0..1 random)

TPEX_ODD = "https://www.tpex.org.tw/www/zh-tw/afterTrading/oddQuote"
TPEX_DAILY = p21.TPEX_DAILY

# oddQuote field names (codepoints keep this file ASCII)
KW_CODE = _K("4ee3 865f")                          # dai hao
KW_LAST = _K("6700 5f8c 6210 4ea4 50f9")           # zui hou cheng jiao jia
KW_CHG = _K("6f32 8dcc")                           # zhang die
KW_FIRST = _K("9996 7b46 6210 4ea4 50f9")          # shou bi cheng jiao jia
KW_HIGH = _K("6700 9ad8")                          # zui gao
KW_LOW = _K("6700 4f4e")                           # zui di
KW_SHARES = _K("6210 4ea4 80a1 6578")              # cheng jiao gu shu
KW_VALUE = _K("6210 4ea4 91d1 984d")               # cheng jiao jin e (prefix)
KW_TRADES = _K("6210 4ea4 7b46 6578")              # cheng jiao bi shu
KW_BID = _K("6700 5f8c 8cb7 50f9")                 # zui hou mai jia
KW_BIDQ = _K("6700 5f8c 8cb7 91cf")                # zui hou mai liang (prefix)
KW_ASK = _K("6700 5f8c 8ce3 50f9")                 # zui hou mai(sell) jia
KW_ASKQ = _K("6700 5f8c 8ce3 91cf")                # zui hou mai(sell) liang (prefix)

ODD_COLS = ("last", "chg", "first", "high", "low", "shares", "value", "trades",
            "bid", "bidq", "ask", "askq")
O = {k: i for i, k in enumerate(ODD_COLS)}
_PRICE = ("last", "first", "high", "low", "bid", "ask")
_QUOTE = ("bid", "ask")
# 9990 / 9995 / 9999.95 sit in the last-bid/ask columns as "market order"
# placeholders. Only the QUOTE columns are screened: a TRADE price can be
# real above 9990 (5274 trades near NT$18,000 in 2026; parser v1 blanked it).
NO_PRICE = (9990.0, 9995.0, 9999.95)
PARSER = 2
Q_OPEN, Q_CLOSE, Q_HIGH, Q_LOW, Q_REF = p21.Q_OPEN, p21.Q_CLOSE, p21.Q_HIGH, p21.Q_LOW, p21.Q_REF


def _say(*a):
    print(*a)
    sys.stdout.flush()


# ================================================================ parsing
def parse_odd(j):
    """TPEX oddQuote JSON -> {sid: [ODD_COLS...]} for 4-digit ordinary shares.
    Fields are found by NAME. '--', blanks and the 0.00 'no trade' price
    become NaN; a bid/ask placeholder (NO_PRICE) becomes NaN. Returns None
    when the payload lacks any required field (shape change -> refetch)."""
    fields, data = p21._table(j)
    norm = [str(f).replace(" ", "") for f in fields]

    def exact(kw):
        return p21._field_index(norm, exact=kw)

    def prefix(kw):
        for i, f in enumerate(norm):
            if f.startswith(kw):
                return i
        return None
    idx = dict(code=exact(KW_CODE), last=exact(KW_LAST), chg=exact(KW_CHG),
               first=exact(KW_FIRST), high=exact(KW_HIGH), low=exact(KW_LOW),
               shares=exact(KW_SHARES), value=prefix(KW_VALUE), trades=exact(KW_TRADES),
               bid=exact(KW_BID), bidq=prefix(KW_BIDQ), ask=exact(KW_ASK), askq=prefix(KW_ASKQ))
    need = ("code", "last", "first", "high", "low", "shares", "value")
    if any(idx[k] is None for k in need):
        return None
    out = {}
    for row in data:
        sid = str(row[idx["code"]]).strip()
        if not (sid.isdigit() and len(sid) == 4):
            continue
        vals = []
        for k in ODD_COLS:
            i = idx[k]
            v = p21._f(row[i]) if (i is not None and i < len(row)) else float("nan")
            vals.append(v)
        sh = vals[O["shares"]]
        for k in _PRICE:
            v = vals[O[k]]
            if v != v or v <= 0 or (k in _QUOTE and any(abs(v - x) < 1e-6 for x in NO_PRICE)):
                vals[O[k]] = float("nan")
        if not (sh == sh and sh > 0):
            for k in ("last", "first", "high", "low"):
                vals[O[k]] = float("nan")
        out[sid] = vals
    return out


def _odd_path(d):
    return os.path.join(ODD_DIR, "odd_%s.json" % d)


def _reg_path(d):
    return os.path.join(ODD_DIR, "reg_%s.json" % d)


_ODD, _REG = {}, {}


def load_odd(d):
    """{sid: [ODD_COLS]} for date d, or None when not cached."""
    if d not in _ODD:
        p = _odd_path(d)
        _ODD[d] = json.load(open(p, encoding="utf-8"))["rows"] if os.path.exists(p) else None
    return _ODD[d]


def load_reg(d):
    """Raw TPEX board-lot quotes {sid: [close, chg, open, high, low, next_ref,
    next_up, next_down]} (p21 layout): the P2-1 cache first, else ours."""
    if d not in _REG:
        q = p21.load_quote(d)
        if q is None and os.path.exists(_reg_path(d)):
            q = json.load(open(_reg_path(d), encoding="utf-8"))["rows"]
        _REG[d] = q
    return _REG[d]


# ================================================================== fetch
def needed_dates():
    """Entry dates (research + live universes) on/after ODD_FROM first, then
    the exit dates the round-trip view needs."""
    ent, ex = set(), set()
    for kind in ("research", "live"):
        ts, b0, _ = c.base(kind)
        ent.update(t["dates"][0] for t in ts)
        ex.update(b0["exit"])
    ent = sorted(d for d in ent if d >= ODD_FROM)
    ex = sorted(d for d in ex if d >= ODD_FROM and d not in set(ent))
    return ent, ex


def _stale_odd(d):
    """A parser-v1 file holding a traded row whose prices were blanked (the
    old >= 9990 screen hit real prices): refetch it."""
    body = json.load(open(_odd_path(d), encoding="utf-8"))
    if body.get("parser", 1) >= PARSER:
        return False
    for v in body["rows"].values():
        sh, fi = v[O["shares"]], v[O["first"]]
        if sh == sh and sh > 0 and fi != fi:
            return True
    return False


def _ymd(d):
    y, mo, dd = d.split("-")
    return "%s/%s/%s" % (y, mo, dd)


def cmd_fetch():
    os.makedirs(ODD_DIR, exist_ok=True)
    ent, ex = needed_dates()
    todo = []
    for d in ent + ex:
        if not os.path.exists(_odd_path(d)) or _stale_odd(d):
            todo.append(("odd", d))
        if p21.load_quote(d) is None and not os.path.exists(_reg_path(d)):
            todo.append(("reg", d))
    _say("dates: %d entry + %d exit-only; %d requests to make" % (len(ent), len(ex), len(todo)))
    fails = []
    t0 = time.time()
    for k, (what, d) in enumerate(todo):
        url = TPEX_ODD if what == "odd" else TPEX_DAILY
        j = _get_json(url, params={"date": _ymd(d), "response": "json"})
        rows = None
        if j is not None:
            rows = parse_odd(j) if what == "odd" else p21.parse_daily(j)
        if not rows:            # None (shape) or {} (no data) -> retry later
            fails.append((what, d))
        else:
            path = _odd_path(d) if what == "odd" else _reg_path(d)
            body = dict(date=d, n=len(rows), rows=rows)
            if what == "odd":
                body["cols"] = list(ODD_COLS)
                body["parser"] = PARSER
            json.dump(body, open(path, "w", encoding="utf-8"))
        if k % 25 == 0 or not rows:
            _say("  %d/%d %s %s %s (%.0fs)" % (k + 1, len(todo), what, d,
                                               "FAILED" if not rows else "%d rows" % len(rows),
                                               time.time() - t0))
        time.sleep(PAUSE + random.random())
    _say("fetch done in %.0fs, failures (%d): %s" % (time.time() - t0, len(fails), fails))


# ================================================================ analysis
_TAGS = {}


def _tags(kind):
    """P2-1 per-trade restriction tags (disposal on the entry day, signal-day
    limit-up close, entry open at the limit), keyed (sid, sig). Empty when
    the P2-1 cache is absent -- the cross-tabs are then skipped."""
    if kind not in _TAGS:
        p = os.path.join(c.CACHE, "p21", "tags_%s.pkl" % kind)
        out = {}
        if os.path.exists(p):
            T = pd.read_pickle(p)
            for r in T.to_dict("records"):
                out[(str(r["sid"]), r["sig"])] = r
        _TAGS[kind] = out
    return _TAGS[kind]


def _fin(x):
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _bps(a, b):
    return (a / b - 1.0) * 1e4 if (_fin(a) and _fin(b) and b > 0) else np.nan


BAND = p21.LIMIT_MULT - 1 + 0.005   # daily limit band (+ rounding slack)


def _sane(px, q):
    """A trade print must sit inside the day's limit band around the board
    reference (close - change). The exchange sometimes leaves a 9995
    placeholder in a TRADE column (8102 on 2025-12-23: first = high = 9995
    with a 58 vwap); such a print is a data error, not a price."""
    if not _fin(px) or px <= 0:
        return False
    if q is None or not _fin(q[Q_CLOSE]) or not _fin(q[p21.Q_CHG]):
        return True
    ref = q[Q_CLOSE] - q[p21.Q_CHG]
    return ref <= 0 or abs(px / ref - 1) <= BAND


def _tick_bps(px, sid):
    t = tick_size(px, sid) if _fin(px) and px > 0 else None
    return float(t) / px * 1e4 if t else np.nan


def exit_leg(why, at_open, ex_date, sid):
    """Exit-day odd print vs the board price the research exit used.
    Returns (status, ratio). 'limit' = an intraday stop/lock/tp touch (a
    resting limit order; research price kept)."""
    if why in ("tp", "stop", "lock") and not at_open:
        return "limit", np.nan
    if ex_date < ODD_FROM:
        return "pre", np.nan
    xq = (load_reg(ex_date) or {}).get(sid)
    if xq is None:
        return "noreg", np.nan
    xo = (load_odd(ex_date) or {}).get(sid)
    if xo is None:
        return "nofill", np.nan
    if at_open:
        a, b = xo[O["first"]], xq[Q_OPEN]
    else:                                  # time exit at the close
        a, b = xo[O["last"]], xq[Q_CLOSE]
    if _fin(a) and _fin(b) and b > 0 and _sane(a, xq):
        return "ok", a / b
    return "nofill", np.nan


def entry_table(kind):
    """One row per baseline trade, row-aligned with r4_common.base(kind)."""
    ts, b0, _ = c.base(kind)
    tags = _tags(kind)
    rows = []
    for i, (t, ex_date, why, at_open) in enumerate(zip(ts, b0["exit"], b0["why"], b0["at_open"])):
        sid, sig, ent = str(t["sid"]), t["sig"], t["dates"][0]
        r = dict(i=i, sid=sid, sig=sig, ent=ent, exit=ex_date, why=why, at_open=bool(at_open),
                 window="REC" if sig >= c.RECENT_FROM else "OLD", year=ent[:4],
                 era=("pre" if ent < ODD_FROM else "3min" if ent < ERA_SPLIT else "1min"),
                 raw_open=np.nan, raw_high=np.nan, raw_low=np.nan, raw_close=np.nan,
                 gap_bps=np.nan, basis_ok=None, odd_first=np.nan, odd_last=np.nan,
                 odd_high=np.nan, odd_low=np.nan, odd_vwap=np.nan, odd_shares=np.nan,
                 odd_trades=np.nan, odd_value=np.nan, bps_first=np.nan, bps_vwap=np.nan,
                 bps_low=np.nan, first_in_range=None, tick_bps=np.nan)
        tg = tags.get((sid, sig), {})
        r["ent_disp"] = tg.get("ent_disp")
        r["lim_sig"] = tg.get("lim_sig")
        r["open_lim_ent"] = tg.get("open_lim_ent")
        q = (load_reg(ent) or {}).get(sid)
        sq = (load_reg(sig) or {}).get(sid)
        if q is not None:
            r.update(raw_open=q[Q_OPEN], raw_high=q[Q_HIGH], raw_low=q[Q_LOW], raw_close=q[Q_CLOSE])
            if sq is not None and _fin(sq[Q_REF]):
                ref = sq[Q_REF]                       # the entry day's reference price
            elif _fin(q[Q_CLOSE]) and _fin(q[p21.Q_CHG]):
                ref = q[Q_CLOSE] - q[p21.Q_CHG]
            else:
                ref = np.nan
            r["gap_bps"] = _bps(q[Q_OPEN], ref)
            # basis check: research bar / raw bar must be ONE factor on o and c
            fo = float(t["o"][0]) / q[Q_OPEN] if _fin(q[Q_OPEN]) and q[Q_OPEN] > 0 else np.nan
            fc = float(t["c"][0]) / q[Q_CLOSE] if _fin(q[Q_CLOSE]) and q[Q_CLOSE] > 0 else np.nan
            r["basis_ok"] = bool(_fin(fo) and _fin(fc) and abs(fo / fc - 1) < 0.005)
        oddf = load_odd(ent) if ent >= ODD_FROM else None
        odd = (oddf or {}).get(sid)
        if ent < ODD_FROM:
            st = "pre"
        elif oddf is None:
            st = "nofile"
        elif q is None or not _fin(q[Q_OPEN]):
            st = "noreg"
        elif odd is None or not _fin(odd[O["first"]]) or not (odd[O["shares"]] > 0):
            st = "nofill"
        elif not _sane(odd[O["first"]], q):
            st = "bad"
        else:
            st = "ok"
        r["status"] = st
        if odd is not None:
            sh, val = odd[O["shares"]], odd[O["value"]]
            vwap = val / sh if (_fin(sh) and sh > 0 and _fin(val)) else np.nan
            r.update(odd_first=odd[O["first"]], odd_last=odd[O["last"]], odd_high=odd[O["high"]],
                     odd_low=odd[O["low"]], odd_vwap=vwap, odd_shares=sh,
                     odd_trades=odd[O["trades"]], odd_value=val)
        if st == "ok":
            ro = q[Q_OPEN]
            r["bps_first"] = _bps(r["odd_first"], ro)
            r["bps_vwap"] = _bps(r["odd_vwap"], ro)
            r["bps_low"] = _bps(r["odd_low"], ro)
            r["first_in_range"] = bool(q[Q_LOW] - 1e-9 <= r["odd_first"] <= q[Q_HIGH] + 1e-9)
            r["tick_bps"] = _tick_bps(ro, sid)
        xs, xr = exit_leg(why, bool(at_open), ex_date, sid)
        r["exit_status"] = xs
        r["exit_ratio"] = xr
        r["exit_bps"] = (xr - 1) * 1e4 if _fin(xr) else np.nan
        rows.append(r)
    return pd.DataFrame(rows)


def dist(x, keys=None, iters=1000, seed=c.SEED):
    """Distribution of a bps series; 95% CI of the mean by a date-clustered
    bootstrap when keys (dates) are given."""
    x = np.asarray(x, float)
    ok = np.isfinite(x)
    if keys is not None:
        keys = np.asarray(keys)[ok]
    x = x[ok]
    if not len(x):
        return dict(n=0)
    q = np.percentile(x, [5, 10, 25, 50, 75, 90, 95])
    tm = x[(x >= q[0]) & (x <= q[6])].mean()
    out = dict(n=int(len(x)), mean=float(x.mean()), trim90_mean=float(tm), median=float(q[3]),
               p5=float(q[0]), p10=float(q[1]), p25=float(q[2]), p75=float(q[4]),
               p90=float(q[5]), p95=float(q[6]),
               sd=float(x.std(ddof=1)) if len(x) > 1 else np.nan,
               share_neg=float((x < -0.5).mean() * 100),
               share_zero=float((np.abs(x) <= 0.5).mean() * 100),
               share_pos=float((x > 0.5).mean() * 100))
    if keys is not None and len(x) >= 10:
        uk = np.unique(keys)
        groups = [x[keys == k] for k in uk]
        rng = np.random.default_rng(seed)
        bs = []
        for _ in range(iters):
            pick = rng.integers(0, len(groups), len(groups))
            bs.append(np.concatenate([groups[j] for j in pick]).mean())
        out["mean_ci"] = (float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)))
    return out


def dline(label, d):
    if not d.get("n"):
        return "    %-30s n=0" % label
    ci = d.get("mean_ci")
    return ("    %-30s n=%4d mean %+6.1f%s med %+6.1f trim %+6.1f | p10 %+6.1f p25 %+6.1f "
            "p75 %+6.1f p90 %+6.1f | <0 %4.1f%% =0 %4.1f%% >0 %4.1f%%"
            % (label, d["n"], d["mean"], (" [%+.1f..%+.1f]" % ci) if ci else "", d["median"],
               d["trim90_mean"], d["p10"], d["p25"], d["p75"], d["p90"],
               d["share_neg"], d["share_zero"], d["share_pos"]))


def cross_section(dates):
    """Market control: every 4-digit OTC sid with an odd trade on the same
    entry dates (first vs open)."""
    rows = []
    for d in dates:
        reg, odd = load_reg(d), load_odd(d)
        if not reg or not odd:
            continue
        for sid, o in odd.items():
            q = reg.get(sid)
            if (q is None or not _sane(o[O["first"]], q) or not _fin(q[Q_OPEN])
                    or q[Q_OPEN] <= 0):
                continue
            ref = q[Q_CLOSE] - q[p21.Q_CHG] if _fin(q[p21.Q_CHG]) else np.nan
            rows.append((d, _bps(o[O["first"]], q[Q_OPEN]), _bps(q[Q_OPEN], ref),
                         o[O["trades"]]))
    return pd.DataFrame(rows, columns=["ent", "bps_first", "gap_bps", "odd_trades"])


def remeasure(kind, E):
    """Frames row-aligned with base(kind), at slip 0 and SLIP:
      base  board-lot open entry (the research fill)
      B     entry at the odd first match, exit path unchanged (app levels)
      A     entry at the odd first match, levels re-anchored (replayed)
      RT    B plus the exit-side odd print where it applies
    Trades without an odd price keep the board fill (hybrid)."""
    ts, b0, b1 = c.base(kind)
    ok = (E["status"] == "ok").to_numpy()
    ratio = np.where(ok, (E["odd_first"] / E["raw_open"]).to_numpy(float), 1.0)
    xr = E["exit_ratio"].to_numpy(float)
    xr = np.where(np.isfinite(xr), xr, 1.0)
    out = {}
    for sl, base_df in ((0.0, b0), (c.SLIP, b1)):
        r = base_df["ret"].to_numpy(float)
        rb = ((1 + r / 100) / ratio - 1) * 100
        rt = ((1 + r / 100) / ratio * xr - 1) * 100
        A, clipped = [], 0
        for t, k, ra in zip(ts, ok, ratio):
            if not k:
                A.append(c.replay(t, sl))
                continue
            t2 = dict(t)
            t2["o"] = np.array(t["o"], float).copy()
            e = float(t["o"][0]) * ra
            e2 = min(max(e, float(t["l"][0])), float(t["h"][0]))
            clipped += int(abs(e2 - e) > 1e-9)
            t2["o"][0] = e2
            A.append(c.replay(t2, sl))
        A = pd.DataFrame(A)
        if not (A["sig"].to_numpy() == base_df["sig"].to_numpy()).all():
            raise RuntimeError("variant A lost row alignment")
        out[sl] = dict(base=base_df, B=base_df.assign(ret=rb), RT=base_df.assign(ret=rt),
                       A=A, clipped=clipped)
    return out


def limit_at_open(E, F, okm):
    """Order-guidance variant (info only, not a signal filter): an odd-lot
    BUY limit at the board-lot open price, placed after 09:00 and before the
    09:10 auction. Filled at odd_first when odd_first <= open; else at the
    open when the odd day-low reaches it later (path approximated by the
    research bar from the open); else missed. Odd-priced subset only."""
    first = E["odd_first"].to_numpy(float)
    low = E["odd_low"].to_numpy(float)
    ro = E["raw_open"].to_numpy(float)
    with np.errstate(invalid="ignore"):
        at_first = okm & (first <= ro + 1e-9)
        later = okm & ~at_first & (low <= ro + 1e-9)
    filled = at_first | later
    ratio = np.where(at_first, first / ro, 1.0)
    out = {}
    for sl in (0.0, c.SLIP):
        base_df = F[sl]["base"]
        r = base_df["ret"].to_numpy(float)
        rl = ((1 + r / 100) / ratio - 1) * 100
        L = base_df.assign(ret=rl)
        res = {}
        for w in c.WINDOWS:
            wm = c.wmask(base_df, w)
            sub = wm & okm
            n_opp = int(sub.sum())
            if not n_opp:
                continue
            fs = c.stats(L[sub & filled])
            ms = c.stats(base_df[sub & ~filled])
            bs = c.stats(base_df[sub])
            Bs = c.stats(F[sl]["B"][sub])
            res[w] = dict(opp=n_opp, filled=fs["n"], at_first=int((sub & at_first).sum()),
                          later=int((sub & later).sum()), fill_rate=100.0 * fs["n"] / n_opp,
                          filled_win=fs["win"], filled_mean=fs["mean"],
                          missed_n=ms["n"], missed_win=ms["win"], missed_mean=ms["mean"],
                          ev_limit=fs["sum"] / n_opp, ev_base=bs["mean"], ev_B=Bs["mean"])
        out["slip_%.3f" % sl] = res
        _say("  limit@open (slip %.3f): " % sl + " | ".join(
            "%s fill %d/%d (%.0f%%; at 09:10 %d, later %d) filled %.1f%% / %+.2f, missed n=%d "
            "%.1f%% / %+.2f | EV/opp limit %+.2f vs market-odd %+.2f vs board %+.2f"
            % (w, v["filled"], v["opp"], v["fill_rate"], v["at_first"], v["later"], v["filled_win"],
               v["filled_mean"], v["missed_n"], v["missed_win"] if v["missed_n"] else float("nan"),
               v["missed_mean"] if v["missed_n"] else float("nan"), v["ev_limit"], v["ev_B"],
               v["ev_base"]) for w, v in res.items()))
    return out


def wstats(df, mask=None):
    res = {}
    for w in c.WINDOWS:
        wm = c.wmask(df, w)
        if mask is not None:
            wm = wm & mask
        res[w] = c.stats(df[wm])
    return res


def fee_floor(E, frames):
    """Per-trade notional view with portfolio.money.FeeSchedule.default()
    (NT$20 minimum fee, dollar truncation) vs FeeSchedule.exact() (no floor),
    on the B hybrid at slip 0. Shares = floor(notional / raw entry price);
    odd lots allow any count. The raw exit price is the raw entry times the
    gross price ratio of the B trade, so the dividend basis cancels."""
    from portfolio.money import FeeSchedule
    real, exact = FeeSchedule.default(), FeeSchedule.exact()
    B = frames[0.0]["B"]
    ok = (E["status"] == "ok").to_numpy()
    raw_e = np.where(ok, E["odd_first"].to_numpy(float), E["raw_open"].to_numpy(float))
    rets = B["ret"].to_numpy(float)
    win = np.where(B["sig"].to_numpy() >= c.RECENT_FROM, "REC", "OLD")
    res = {}
    for notional in (5000, 10000, 20000, 30000, 100000):
        rr, rx, ww = [], [], []
        for k in range(len(B)):
            pe = raw_e[k]
            if not _fin(pe) or pe <= 0:
                continue
            sh = int(notional // pe)
            if sh < 1:
                continue
            gross = (1 + rets[k] / 100) * (1 + g.BUY_COST) / (1 - g.SELL_COST)
            pe = round(float(pe), 4)
            px = round(float(pe * gross), 4)
            out_r = float(real.buy_cost(pe, sh)[2])
            in_r = float(real.sell_proceeds(px, sh)[3])
            out_x = float(exact.buy_cost(pe, sh)[2])
            in_x = float(exact.sell_proceeds(px, sh)[3])
            rr.append((in_r / out_r - 1) * 100)
            rx.append((in_x / out_x - 1) * 100)
            ww.append(win[k])
        rr, rx, ww = np.array(rr), np.array(rx), np.array(ww)
        pm = round(float(np.nanmedian(raw_e)), 2)
        sh = max(1, int(notional // pm))
        flat = (float(real.sell_proceeds(pm, sh)[3]) / float(real.buy_cost(pm, sh)[2]) - 1) * 100
        flatx = (float(exact.sell_proceeds(pm, sh)[3]) / float(exact.buy_cost(pm, sh)[2]) - 1) * 100
        row = dict(n=int(len(rr)), mean_floor=float(rr.mean()), mean_prop=float(rx.mean()),
                   win_floor=float((rr > 0).mean() * 100), win_prop=float((rx > 0).mean() * 100),
                   drag_pp=float(rr.mean() - rx.mean()),
                   flat_rt_floor=flat, flat_rt_prop=flatx, median_px=pm)
        for w in c.WINDOWS:
            s = ww == w
            row[w] = dict(n=int(s.sum()), mean_floor=float(rr[s].mean()), mean_prop=float(rx[s].mean()),
                          win_floor=float((rr[s] > 0).mean() * 100),
                          win_prop=float((rx[s] > 0).mean() * 100))
        res[notional] = row
    return res


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, pd.DataFrame):
        return [jsonable(r) for r in o.to_dict("records")]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 4)
    return o


def _bucket_dists(K, by):
    """The first-vs-open distribution split by the axes that could move it."""
    for y, s in K.groupby("year"):
        by["year_" + y] = dist(s["bps_first"], s["ent"])
        _say(dline("first, year " + y, by["year_" + y]))
    for e, s in K.groupby("era"):
        by["era_" + e] = dist(s["bps_first"], s["ent"])
        _say(dline("first, auction era " + e, by["era_" + e]))
    gb = pd.cut(K["gap_bps"], [-1e9, -100, 0.5, 200, 500, 1e9],
                labels=["gap<=-1%", "gap(-1%,0]", "gap(0,2%]", "gap(2,5%]", "gap>5%"])
    for lab, s in K.groupby(gb, observed=True):
        by["gap_" + str(lab)] = dist(s["bps_first"], s["ent"])
        _say(dline("first, open " + str(lab), by["gap_" + str(lab)]))
    for col, nm in (("ent_disp", "disposal@entry"), ("lim_sig", "sig-day limit-up close"),
                    ("open_lim_ent", "entry opens at limit")):
        if K[col].notna().any():
            for v in (True, False):
                s = K[K[col].fillna(False).astype(bool) == v]
                by["%s_%s" % (col, v)] = dist(s["bps_first"], s["ent"])
                _say(dline("first, %s=%s" % (nm, v), by["%s_%s" % (col, v)]))
    pb = pd.cut(K["raw_open"], [0, 30, 100, 300, 1e9],
                labels=["px<30", "px30-100", "px100-300", "px>=300"])
    for lab, s in K.groupby(pb, observed=True):
        by["price_" + str(lab)] = dist(s["bps_first"], s["ent"])
        _say(dline("first, " + str(lab) + " (tick %.0fbps)" % s["tick_bps"].median(),
                   by["price_" + str(lab)]))
    tq = pd.qcut(K["odd_trades"].rank(method="first"), 3, labels=["low", "mid", "high"])
    for lab, s in K.groupby(tq, observed=True):
        by["oddtrades_" + str(lab)] = dist(s["bps_first"], s["ent"])
        _say(dline("first, odd trades %s (med %d)" % (lab, s["odd_trades"].median()),
                   by["oddtrades_" + str(lab)]))


def analyze_kind(kind, money=True, band_iters=100):
    _say("\n################ %s universe" % kind)
    E = entry_table(kind)
    res = dict(n=len(E))
    cov = {}
    for w in ("ALL",) + c.WINDOWS:
        sub = E if w == "ALL" else E[E["window"] == w]
        cov[w] = dict(sub["status"].value_counts().to_dict(), n=len(sub))
    res["coverage"] = cov
    res["exit_coverage"] = E["exit_status"].value_counts().to_dict()
    nq = int(E["raw_open"].notna().sum())
    res["basis_ok"] = dict(ok=int((E["basis_ok"] == True).sum()), with_raw=nq)  # noqa: E712
    okm = (E["status"] == "ok").to_numpy()
    _say("coverage: " + " | ".join("%s %s" % (w, cov[w]) for w in cov))
    _say("exit side: %s" % res["exit_coverage"])
    K = E[okm].copy()
    res["first_in_range_pct"] = float(K["first_in_range"].astype(bool).mean() * 100) if len(K) else np.nan
    _say("odd first inside the board-lot day [low, high]: %.1f%% ; research/raw one-factor basis on %d of %d"
         % (res["first_in_range_pct"], res["basis_ok"]["ok"], nq))
    D = {}
    _say("\n(odd - board)/board in bps; + = the odd lot paid more (entry) / received more (exit)")
    D["first_vs_open"] = dist(K["bps_first"], K["ent"])
    D["vwap_vs_open"] = dist(K["bps_vwap"], K["ent"])
    D["low_vs_open"] = dist(K["bps_low"], K["ent"])
    _say(dline("first vs open", D["first_vs_open"]))
    _say(dline("vwap vs open", D["vwap_vs_open"]))
    _say(dline("odd day-low vs open", D["low_vs_open"]))
    for w in c.WINDOWS:
        s = K[K["window"] == w]
        D["first_vs_open_" + w] = dist(s["bps_first"], s["ent"])
        D["vwap_vs_open_" + w] = dist(s["bps_vwap"], s["ent"])
        _say(dline("first vs open " + w, D["first_vs_open_" + w]))
        _say(dline("vwap vs open " + w, D["vwap_vs_open_" + w]))
    D["by"] = {}
    _bucket_dists(K, D["by"])
    D["ticks_first"] = dist((K["bps_first"] / K["tick_bps"]).to_numpy(float))
    _say(dline("first vs open in TICKS", D["ticks_first"]))
    X = E[E["exit_status"] == "ok"]
    D["exit_open"] = dist(X[X["at_open"]]["exit_bps"], X[X["at_open"]]["exit"])
    D["exit_close"] = dist(X[~X["at_open"]]["exit_bps"], X[~X["at_open"]]["exit"])
    _say(dline("exit: odd first vs open", D["exit_open"]))
    _say(dline("exit: odd last vs close", D["exit_close"]))
    XS = cross_section(sorted(set(K["ent"])))
    D["market_same_dates"] = dist(XS["bps_first"], XS["ent"])
    _say(dline("ALL OTC, same entry dates", D["market_same_dates"]))
    liq = XS[XS["odd_trades"] >= K["odd_trades"].quantile(0.10)]
    D["market_liquid"] = dist(liq["bps_first"], liq["ent"])
    _say(dline("OTC with odd trades >= signal p10", D["market_liquid"]))
    gl = pd.cut(liq["gap_bps"], [-1e9, -100, 0.5, 200, 500, 1e9],
                labels=["gap<=-1%", "gap(-1%,0]", "gap(0,2%]", "gap(2,5%]", "gap>5%"])
    D["market_liquid_by_gap"] = {}
    for lab, s in liq.groupby(gl, observed=True):
        D["market_liquid_by_gap"][str(lab)] = dist(s["bps_first"], s["ent"], iters=300)
        _say(dline("  market liquid, open " + str(lab), D["market_liquid_by_gap"][str(lab)]))
    # signal names vs liquid market at the same gap mix: reweight the market
    # gap buckets to the signal set's bucket shares
    gk = pd.cut(K["gap_bps"], [-1e9, -100, 0.5, 200, 500, 1e9],
                labels=["gap<=-1%", "gap(-1%,0]", "gap(0,2%]", "gap(2,5%]", "gap>5%"])
    share = gk.value_counts(normalize=True)
    mm = sum(share.get(k, 0) * v["mean"] for k, v in D["market_liquid_by_gap"].items() if v.get("n"))
    md = sum(share.get(k, 0) * v["median"] for k, v in D["market_liquid_by_gap"].items() if v.get("n"))
    D["market_gapmix"] = dict(mean=float(mm), median_mix=float(md))
    _say("    liquid market reweighted to the signal gap mix: mean %+.1f bps (median mix %+.1f)" % (mm, md))
    res["dist"] = D
    F = remeasure(kind, E)
    RM = {}
    for sl in (0.0, c.SLIP):
        fr = F[sl]
        rm = dict(clipped_A=fr["clipped"])
        _say("\n-- re-measure, slip %.3f (A: odd fill clipped to the bar range on %d)" % (sl, fr["clipped"]))
        for name in ("base", "B", "A", "RT"):
            rm[name] = wstats(fr[name])
            rm[name + "_oddsubset"] = wstats(fr[name], okm)
            _say("  %-4s " % name + " | ".join(
                "%s n=%3d %5.2f%% / %+5.2f sum %+6.1f" % (w, rm[name][w]["n"], rm[name][w]["win"],
                                                          rm[name][w]["mean"], rm[name][w]["sum"])
                for w in c.WINDOWS) + "   [odd subset " + " | ".join(
                "%s n=%3d %5.2f%% / %+5.2f" % (w, rm[name + "_oddsubset"][w]["n"],
                                               rm[name + "_oddsubset"][w]["win"],
                                               rm[name + "_oddsubset"][w]["mean"])
                for w in c.WINDOWS) + "]")
        for name in ("B", "A", "RT"):
            ci = {}
            for w in c.WINDOWS:
                wm = c.wmask(fr["base"], w) & okm
                if wm.sum() < 2:
                    ci[w] = dict(dmean=np.nan, lo=np.nan, hi=np.nan, flips=0)
                    continue
                lo, hi = c.paired_dmean(fr["base"][wm], fr[name][wm])
                d = fr[name]["ret"].to_numpy(float)[wm] - fr["base"]["ret"].to_numpy(float)[wm]
                flips = int(((fr["base"]["ret"].to_numpy(float)[wm] > 0)
                             != (fr[name]["ret"].to_numpy(float)[wm] > 0)).sum())
                ci[w] = dict(dmean=float(d.mean()), lo=float(lo), hi=float(hi), flips=flips)
            rm["paired_" + name] = ci
            _say("  paired %-2s - base, odd subset: " % name + " | ".join(
                "%s dmean %+.3f CI %+.3f..%+.3f sign flips %d" % (w, ci[w]["dmean"], ci[w]["lo"],
                                                                  ci[w]["hi"], ci[w]["flips"])
                for w in c.WINDOWS))
        RM["slip_%.3f" % sl] = rm
    res["remeasure"] = RM
    # trades an odd-lot-only buyer could not have bought (no odd trade that day)
    nf = (E["status"] == "nofill").to_numpy()
    res["nofill_base"] = {("slip_%.3f" % sl): wstats(F[sl]["base"], nf) for sl in (0.0, c.SLIP)}
    _say("  no odd trade on the entry day (base fill): " + " | ".join(
        "%s n=%d %s" % (w, v["n"], ("%.1f%% / %+.2f" % (v["win"], v["mean"])) if v["n"] else "-")
        for w, v in res["nofill_base"]["slip_0.000"].items()))
    res["limit_at_open"] = limit_at_open(E, F, okm)
    res["exit_mix"] = dict(base=F[0.0]["base"]["why"].value_counts().to_dict(),
                           A=F[0.0]["A"]["why"].value_counts().to_dict())
    _say("  exit mix base %s | A %s" % (res["exit_mix"]["base"], res["exit_mix"]["A"]))
    if money:
        MO = {}
        fr = F[c.SLIP]
        for name in ("base", "B", "RT"):
            tab = c.money(fr[name], (3, 5, 8))
            MO[name] = tab.drop(columns=["yearly"])
            for _, r in tab.iterrows():
                _say("  strict slip %-4s %s" % (name, c.money_line(r)))
        if band_iters:
            for name in ("base", "B", "RT"):
                t0 = time.time()
                bt = c.money_band(fr[name], (3, 5, 8), iters=band_iters)
                MO[name + "_band"] = bt
                for _, r in bt.iterrows():
                    _say("  band slip %-4s slots %d: CAGR p10/p50/p90 %5.1f/%5.1f/%5.1f MDD p50 %6.1f"
                         % (name, r["slots"], r["cagr_p10"], r["cagr_p50"], r["cagr_p90"], r["mdd_p50"]))
                _say("  (%.0fs)" % (time.time() - t0))
        res["money"] = MO
    ff = fee_floor(E, F)
    res["fee_floor"] = ff
    for k, v in ff.items():
        _say("  notional NT$%6d: mean %+5.2f%% with the NT$20 floor vs %+5.2f%% no floor (drag %+.2fpp) "
             "win %4.1f/%4.1f%% | REC %+5.2f/%+5.2f OLD %+5.2f/%+5.2f | flat round trip at NT$%.0f: "
             "%.2f%% vs %.2f%%"
             % (k, v["mean_floor"], v["mean_prop"], v["drag_pp"], v["win_floor"], v["win_prop"],
                v["REC"]["mean_floor"], v["REC"]["mean_prop"], v["OLD"]["mean_floor"],
                v["OLD"]["mean_prop"], v["median_px"], v["flat_rt_floor"], v["flat_rt_prop"]))
    res["entries"] = E[["sid", "sig", "ent", "window", "status", "raw_open", "odd_first", "odd_vwap",
                        "odd_trades", "bps_first", "bps_vwap", "gap_bps", "ent_disp", "exit", "why",
                        "at_open", "exit_status", "exit_bps"]].to_dict("records")
    return res


def cmd_selftest():
    """Pure checks: the by-name parser, ratio 1.0 == the base replay, and the
    clip to the bar range."""
    fields = [KW_CODE, _K("540d 7a31"), KW_LAST, KW_CHG, KW_FIRST, KW_HIGH, KW_LOW, KW_SHARES,
              KW_VALUE + "(" + _K("5143") + ")", " " + KW_TRADES + " ", KW_BID,
              KW_BIDQ + "(" + _K("80a1") + ")", KW_ASK, KW_ASKQ + "(" + _K("80a1") + ")"]
    rev = list(reversed(range(len(fields))))          # shuffled column order
    data = [["1234", "x", "10.5", "0.1", "10.4", "10.6", "10.3", "1,000", "10,450", "5",
             "10.4", "10", "9995.00", "3"],
            ["2345", "y", "0.00", "0.00", "0.00", "0.00", "0.00", "0", "0", "0", "--", "0", "", "0"],
            ["006201", "etf", "1", "0", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"],
            ["5274", "z", "17,820.00", "5", "17,815.00", "18,100.00", "17,390.00", "100",
             "1,780,000", "9", "17,800.00", "1", "9999.95", "2"]]
    j = dict(tables=[dict(fields=[fields[i] for i in rev], data=[[r[i] for i in rev] for r in data])])
    p = parse_odd(j)
    assert set(p) == {"1234", "2345", "5274"}, p
    assert p["5274"][O["first"]] == 17815.0 and p["5274"][O["bid"]] == 17800.0
    assert p["5274"][O["ask"]] != p["5274"][O["ask"]]          # 9999.95 placeholder
    assert p["1234"][O["first"]] == 10.4 and p["1234"][O["value"]] == 10450.0
    assert p["1234"][O["ask"]] != p["1234"][O["ask"]]          # 9995 placeholder -> NaN
    assert all(p["2345"][O[k]] != p["2345"][O[k]] for k in ("first", "last", "bid", "ask"))
    assert parse_odd(dict(tables=[dict(fields=fields[:3], data=[])])) is None
    q8102 = [57.1, -2.0, 58.6, 58.6, 57.0, 57.1, 9995.0, 0.01]   # 2025-12-23 raw row
    assert not _sane(9995.0, q8102) and _sane(57.9, q8102) and not _sane(float("nan"), q8102)
    ts, b0, b1 = c.base("research")
    for sl, bdf in ((0.0, b0), (c.SLIP, b1)):
        for t, ret, why in zip(ts, bdf["ret"], bdf["why"]):
            t2 = dict(t)
            t2["o"] = np.array(t["o"], float).copy()
            r = c.replay(t2, sl)
            assert abs(r["ret"] - ret) < 1e-12 and r["why"] == why
    t = ts[0]
    e = float(t["o"][0]) * 10
    assert min(max(e, float(t["l"][0])), float(t["h"][0])) == float(t["h"][0])
    _say("selftest ok (%d trades x 2 slips: ratio 1.0 reproduces the base replay)" % len(ts))


def cmd_analyze():
    t0 = time.time()
    out = dict(item="P2-7", built=time.strftime("%Y-%m-%d %H:%M"),
               odd_from=ODD_FROM, slip=c.SLIP, recent_from=c.RECENT_FROM,
               measured=("odd_first = TPEX oddQuote first odd-lot match of the entry day "
                         "(no timestamp; normally the 09:10 auction); board = raw TPEX "
                         "dailyQuotes open of the same sid/date; bps = (odd/board - 1) * 1e4"))
    for kind in ("research", "live"):
        out[kind] = analyze_kind(kind, money=True, band_iters=100 if kind == "research" else 0)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(jsonable(out), open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    _say("\nwrote %s (%.0fs)" % (OUT_JSON, time.time() - t0))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    globals()["cmd_" + cmd.replace("-", "_")]()
