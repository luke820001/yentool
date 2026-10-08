"""
sandbox_r4_p21.py -- round 4, item P2-1 (2026-10-08). ASCII only.

Question: should a TRADE RESTRICTION on the entry day block a CORE+ entry?
Every baseline signal (the 556-trade research set; the live-frequency set
of BACKTEST_LOG section L as a second sample) is tagged by the state the
owner would face when buying at the NEXT SESSION's open:

  (a) disp    the entry day sits inside a TPEX disposition period
              (research is OTC-only, so TPEX is "the board"; the measure,
              the matching interval and the announcement date are kept);
  (b) att     the stock was on the TPEX attention list on the signal day
              (announced that evening), the run of consecutive attention
              sessions ending on the signal day, and the count over the last
              10 sessions (the B3 Attention_Days definition);
  (c) lim_sig the signal-day close is AT the limit-up price,
              scanner.tick.round_to_tick(prev_close * 1.10, 'down', sid) on
              RAW exchange prices (TPEX dailyQuotes); ex-rights / ex-dividend
              days (exchange reference != previous close) are skipped and
              counted; the exchange's own published limit is a cross-check;
  (d) lock_ent the ENTRY day is a one-price bar at limit-up
              (open == high == low == limit-up): an order at the open cannot
              be filled. These trades are counted and the baseline is
              re-measured with them removed as no-fills.

Every bucket: n, win%, mean, sum share, slip stress, both windows; the
filter "exclude bucket" goes through r4_common.report_filter (seven gates,
K.2 money gates, the I.4 same-count random-deletion control, EV per
opportunity, frequency, strict money). The shipped rule only (r4_common
BASE = sandbox_money.BASE from scanner.exit_rules.DEFAULT_RULE); no exit
threshold is written here.

Data (all outside git):
  lists   C:/.../scratchpad/understand/tpex_{disposal,attention}_YYYY.json
          (fetched 2026-10-08); a missing year is fetched into CACHE/p21/lists
  quotes  CACHE/p21/quotes/YYYY-MM-DD.json, compact TPEX dailyQuotes
          (raw close / change / OHLC / next reference / next limits)
  live    the 11 tradable live signals since 2026-06-25 are counted from
          read-only COPIES of data/signal_ledger.db and the cloud
          price_volume.db (scratchpad/r4/p21/live/); research_prices.db
          ends 2026-09-09, so live data is used for counts only.

Commands:
  python archive/research/sandbox_r4_p21.py fetch     # lists gaps + quotes (~35 min, polite)
  python archive/research/sandbox_r4_p21.py analyze   # everything below, writes P2-1.json
Run with PYTHONDONTWRITEBYTECODE=1.
"""
import json
import os
import random
import re
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import r4_common as c                              # noqa: E402  (chdir ROOT)
from ingestion.inst_history import _K, _get_json   # noqa: E402
from scanner.tick import round_to_tick, tick_size  # noqa: E402

SCRATCH = os.environ.get(
    "P21_SCRATCH",
    "C:/Users/luke4/AppData/Local/Temp/claude/D--YenTool/"
    "1624a313-b1ce-4a13-be0b-5e480d6c444b/scratchpad")
UNDERSTAND = os.path.join(SCRATCH, "understand")
OUT_DIR = os.path.join(SCRATCH, "r4")
OUT_JSON = os.path.join(OUT_DIR, "P2-1.json")
LIVE_DIR = os.path.join(OUT_DIR, "p21", "live")
P21 = os.path.join(c.CACHE, "p21")
LIST_DIR = os.path.join(P21, "lists")
QUOTE_DIR = os.path.join(P21, "quotes")
ODDLOT_DIR = os.path.join(c.CACHE, "oddlot")      # P2-7's cache, read if present

LIMIT_MULT = 1.10            # TPEX/TWSE daily price limit since 2015-06-01
NO_LIMIT = 9990.0            # the exchange publishes 9995 / 9999.95 for "no limit"
LIVE_SINCE = "2026-06-25"
YEARS = range(2017, 2027)
PAUSE = 2.5                  # seconds between exchange requests

TPEX_DISPOSAL = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
TPEX_ATTENTION = "https://www.tpex.org.tw/www/zh-tw/bulletin/attention"
TPEX_DAILY = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"

# field keywords (codepoints keep this file ASCII)
KW_CODE = _K("4ee3 865f")                     # dai hao
KW_SEC_CODE = _K("8b49 5238 4ee3 865f")       # zheng quan dai hao
KW_CLOSE = _K("6536 76e4")                    # shou pan
KW_CHG = _K("6f32 8dcc")                      # zhang die
KW_OPEN = _K("958b 76e4")                     # kai pan
KW_HIGH = _K("6700 9ad8")                     # zui gao
KW_LOW = _K("6700 4f4e")                      # zui di
KW_REF = _K("53c3 8003 50f9")                 # can kao jia
KW_UP = _K("6f32 505c 50f9")                  # zhang ting jia
KW_DOWN = _K("8dcc 505c 50f9")                # die ting jia
KW_PUB = _K("516c 5e03 65e5 671f")            # gong bu ri qi (disposal)
KW_PERIOD = _K("8655 7f6e 8d77 8a16 6642 9593")   # chu zhi qi qi shi jian
KW_MEASURE = _K("8655 7f6e 63aa 65bd")        # chu zhi cuo shi
KW_CONTENT = _K("8655 7f6e 5167 5bb9")        # chu zhi nei rong
KW_REASON = _K("8655 7f6e 539f 56e0")         # chu zhi yuan yin
KW_ANN = _K("516c 544a 65e5 671f")            # gong gao ri qi (attention)
KW_CUM = _K("7d2f 8a08")                      # lei ji
KW_FIRST = _K("7b2c 4e00 6b21")               # di yi ci
KW_AGAIN = _K("518d 6b21")                    # zai ci
KW_MINUTE = _K("5206 9418")                   # fen zhong
KW_EVERY = _K("7d04 6bcf")                    # yue mei
KW_UNIT = _K("4ea4 6613 55ae 4f4d")           # jiao yi dan wei (size-conditional prepay)
KW_ALL_PAY = _K("5168 90e8 4e4b 8cb7 9032 50f9 91d1")  # quan bu zhi mai jin jia jin
TILDES = "~" + _K("ff5e")


def _say(*a):
    print(*a)
    sys.stdout.flush()


# ================================================================ parsing
def roc_date(s):
    """'115/10/02' -> '2026-10-02' (ROC year + 1911). None if unparsable."""
    m = re.match(r"\s*(\d{2,3})[/.](\d{1,2})[/.](\d{1,2})", str(s))
    if not m:
        return None
    return "%04d-%02d-%02d" % (int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))


def split_period(s):
    """'115/10/02~115/10/08' (or the TWSE full-width tilde) -> (start, end)."""
    parts = re.split("[%s]" % re.escape(TILDES), str(s))
    if len(parts) != 2:
        return None, None
    return roc_date(parts[0]), roc_date(parts[1])


def _field_index(fields, *kws, exact=None):
    for i, f in enumerate(fields):
        f = str(f).replace(" ", "")
        if exact is not None and f == exact:
            return i
        if kws and all(k in f for k in kws):
            return i
    return None


def _table(j):
    if not isinstance(j, dict):
        return [], []
    if "tables" in j and j["tables"]:
        t = j["tables"][0]
        return [str(f) for f in (t.get("fields") or [])], t.get("data") or []
    return [str(f) for f in (j.get("fields") or [])], j.get("data") or []


def _clean_sid(x):
    s = str(x).strip()
    m = re.match(r"([0-9A-Z]+)", s)
    return m.group(1) if m else ""


def parse_disposal(j):
    """TPEX disposal bulletin -> list of dicts (sid, pub, start, end, measure,
    again, match_min, prepay_all, reason). Placeholder rows (empty code) are
    skipped."""
    fields, data = _table(j)
    i_code = _field_index(fields, KW_SEC_CODE)
    i_pub = _field_index(fields, KW_PUB)
    i_per = _field_index(fields, KW_PERIOD)
    i_mea = _field_index(fields, KW_MEASURE)
    i_con = _field_index(fields, KW_CONTENT)
    i_rea = _field_index(fields, KW_REASON)
    out = []
    for row in data:
        if i_code is None or i_per is None:
            break
        sid = _clean_sid(row[i_code])
        if not sid:
            continue
        a, b = split_period(row[i_per])
        if not a or not b:
            continue
        mea = str(row[i_mea]) if i_mea is not None else ""
        con = str(row[i_con]) if i_con is not None else ""
        mm = re.search(KW_EVERY + r"\s*(\d+)\s*" + KW_MINUTE, con)
        out.append(dict(
            sid=sid, pub=roc_date(row[i_pub]) if i_pub is not None else None,
            start=a, end=b, measure=mea, again=(KW_AGAIN in mea),
            match_min=int(mm.group(1)) if mm else None,
            prepay_all=(KW_ALL_PAY in con and KW_UNIT not in con),
            reason=str(row[i_rea]) if i_rea is not None else ""))
    return out


def dedupe_periods(rows):
    """Yearly queries overlap at New Year: collapse on (sid, start, end)."""
    seen, out = set(), []
    for r in rows:
        k = (r["sid"], r["start"], r["end"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def parse_attention(j):
    """TPEX attention bulletin -> list of (sid, announce_date)."""
    fields, data = _table(j)
    i_code = _field_index(fields, KW_SEC_CODE)
    i_ann = _field_index(fields, KW_ANN)
    out = []
    for row in data:
        if i_code is None or i_ann is None:
            break
        sid = _clean_sid(row[i_code])
        d = roc_date(row[i_ann])
        if sid and d:
            out.append((sid, d))
    return out


def _f(x):
    s = str(x).replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        return float("nan")
    return v


def parse_daily(j):
    """TPEX dailyQuotes -> {sid: [close, chg, open, high, low, next_ref,
    next_up, next_down]} for ordinary-share codes (4 digits). Fields are
    found by name; '--' and blanks become NaN."""
    fields, data = _table(j)
    norm = [f.replace(" ", "") for f in fields]
    idx = dict(code=_field_index(norm, exact=KW_CODE),
               close=_field_index(norm, exact=KW_CLOSE),
               chg=_field_index(norm, exact=KW_CHG),
               open=_field_index(norm, exact=KW_OPEN),
               high=_field_index(norm, exact=KW_HIGH),
               low=_field_index(norm, exact=KW_LOW),
               ref=_field_index(norm, KW_REF), up=_field_index(norm, KW_UP),
               down=_field_index(norm, KW_DOWN))
    if any(v is None for v in idx.values()):
        return None
    out = {}
    for row in data:
        sid = str(row[idx["code"]]).strip()
        if not (sid.isdigit() and len(sid) == 4):
            continue
        out[sid] = [_f(row[idx[k]]) for k in
                    ("close", "chg", "open", "high", "low", "ref", "up", "down")]
    return out


Q_CLOSE, Q_CHG, Q_OPEN, Q_HIGH, Q_LOW, Q_REF, Q_UP, Q_DOWN = range(8)


# ================================================================ loading
def _list_path(kind, year):
    """Prefer the scratch copy fetched by the mapping stage, else ours."""
    a = os.path.join(UNDERSTAND, "tpex_%s_%d.json" % (kind, year))
    if os.path.exists(a):
        return a
    b = os.path.join(UNDERSTAND, "tpex_%s_%dy.json" % (kind, year))
    if os.path.exists(b):
        return b
    return os.path.join(LIST_DIR, "tpex_%s_%d.json" % (kind, year))


def load_lists():
    disp, att = [], []
    missing = []
    for y in YEARS:
        p = _list_path("disposal", y)
        if os.path.exists(p):
            disp += parse_disposal(json.load(open(p, encoding="utf-8")))
        else:
            missing.append(("disposal", y))
        p = _list_path("attention", y)
        if os.path.exists(p):
            att += parse_attention(json.load(open(p, encoding="utf-8")))
        else:
            missing.append(("attention", y))
    return dedupe_periods(disp), sorted(set(att)), missing


def _quote_path(d):
    return os.path.join(QUOTE_DIR, "%s.json" % d)


def load_quote(d):
    """Compact raw quotes for date d, or None when not cached."""
    p = _quote_path(d)
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))["rows"]
    alt = os.path.join(ODDLOT_DIR, "%s_reg.json" % d)
    if os.path.exists(alt):
        try:
            return parse_daily(json.load(open(alt, encoding="utf-8")))
        except Exception:
            return None
    return None


# ================================================================== fetch
def _needed_dates():
    cal = c.trade_calendar()
    si = c.session_index(cal)
    core, prev = set(), set()
    for kind in ("research", "live"):
        ts, _, _ = c.base(kind)
        for t in ts:
            i = si[t["sig"]]
            core.update((cal[i], cal[i + 1], t["dates"][0]))
            prev.add(cal[i - 1])
    lv = set(live_dates())
    # signal and entry days first; previous-day files only add the explicit
    # ex-rights check (the signal day's own change field gives the reference)
    return (sorted(core) + sorted(prev - core),
            sorted(lv - core - prev))


def cmd_fetch():
    os.makedirs(LIST_DIR, exist_ok=True)
    os.makedirs(QUOTE_DIR, exist_ok=True)
    _, _, missing = load_lists()
    for kind, y in missing:
        url = TPEX_DISPOSAL if kind == "disposal" else TPEX_ATTENTION
        j = _get_json(url, params={"startDate": "%d/01/01" % y, "endDate": "%d/12/31" % y,
                                   "response": "json"})
        if j is not None:
            json.dump(j, open(os.path.join(LIST_DIR, "tpex_%s_%d.json" % (kind, y)), "w",
                              encoding="utf-8"), ensure_ascii=True)
        _say("list %s %d: %s" % (kind, y, "ok" if j is not None else "FAILED"))
        time.sleep(PAUSE + random.random())
    research, live_only = _needed_dates()
    todo = [d for d in research + live_only if load_quote(d) is None]
    _say("quotes: %d dates needed, %d to fetch" % (len(research) + len(live_only), len(todo)))
    fails = []
    for k, d in enumerate(todo):
        y, mo, dd = d.split("-")
        j = _get_json(TPEX_DAILY, params={"date": "%s/%s/%s" % (y, mo, dd), "response": "json"})
        rows = parse_daily(j) if j is not None else None
        if rows is None:
            fails.append(d)
        else:
            json.dump(dict(date=d, n=len(rows), rows=rows),
                      open(_quote_path(d), "w", encoding="utf-8"))
        if k % 25 == 0 or rows is None:
            _say("  %d/%d %s %s" % (k + 1, len(todo), d,
                                    "FAILED" if rows is None else "%d rows" % len(rows)))
        time.sleep(PAUSE + random.random())
    _say("fetch done, failures: %s" % fails)


# ================================================================== live
def live_signals():
    """The tradable live signals since LIVE_SINCE, from read-only copies."""
    from pathlib import Path
    from scanner import live_record as lr
    S = Path(LIVE_DIR)
    rows, counters = lr.classify_signals(LIVE_SINCE, S / "signal_ledger.db",
                                         S / "price_volume.db", S / "taiex.db")
    out = []
    for r in rows:
        if r["bucket"] != "tradable":
            continue
        s = r["series"]
        i = r["bar_index"]
        dates = s["date"].tolist()
        out.append(dict(sid=r["sid"], sig=r["sig"],
                        prev=dates[i - 1] if i > 0 else None,
                        entry=dates[i + 1] if i + 1 < len(dates) else None))
    return out, counters


def live_dates():
    try:
        sigs, _ = live_signals()
    except Exception:
        return []
    out = []
    for s in sigs:
        out += [d for d in (s["prev"], s["sig"], s["entry"]) if d]
    return out


# ================================================================ tagging
def _eq(a, b):
    return a == a and b == b and abs(a - b) < 1e-6


def limit_up(prev_close, sid):
    """The day's limit-up price from the previous RAW close: 1.10x rounded
    DOWN onto the ladder (scanner.tick). None for an unusable price."""
    if not (prev_close == prev_close) or prev_close <= 0:
        return None
    return round_to_tick(prev_close * LIMIT_MULT, "down", sid)


def index_lists(disp, att):
    by_sid = {}
    for r in disp:
        by_sid.setdefault(r["sid"], []).append(r)
    att_by = {}
    for sid, d in att:
        att_by.setdefault(sid, set()).add(d)
    return by_sid, att_by


def tag_one(sid, sig, ent, exit_date, cal, si, disp_by, att_by, quote):
    """Restriction state of one signal. ent / exit_date may be None (live
    signal whose entry session has not traded yet). quote(d) -> rows."""
    out = dict(sid=sid, sig=sig, ent=ent)
    # (a) disposition on the entry day (and the signal day, and while held)
    per = disp_by.get(sid, [])
    hit = [p for p in per if ent and p["start"] <= ent <= p["end"]]
    hit.sort(key=lambda p: (p["start"], p["end"]))
    h = hit[-1] if hit else None
    out.update(
        ent_disp=bool(hit),
        disp_again=bool(h and h["again"]),
        disp_match_min=(h["match_min"] if h else None),
        disp_prepay_all=bool(h and h["prepay_all"]),
        disp_pub=(h["pub"] if h else None),
        disp_start=(h["start"] if h else None),
        disp_end=(h["end"] if h else None),
        # announced before the signal day = visible to the 15:00 scan;
        # announced on the signal day = published that evening
        disp_known_at_scan=bool(h and h["pub"] and h["pub"] < sig),
        disp_ann_on_sig=bool(h and h["pub"] == sig),
        disp_starts_at_entry=bool(h and h["start"] == ent),
        sig_disp=any(p["start"] <= sig <= p["end"] for p in per),
        hold_disp=bool(ent and exit_date and any(
            not (p["end"] < ent or p["start"] > exit_date) for p in per)))
    # (b) attention, on TPEX announcement dates (published after the close)
    a = att_by.get(sid, set())
    i = si.get(sig)
    run = 0
    if i is not None:
        k = i
        while k >= 0 and cal[k] in a:
            run += 1
            k -= 1
        att10 = sum(1 for k in range(max(0, i - 9), i + 1) if cal[k] in a)
        att_prev = i > 0 and cal[i - 1] in a
    else:
        att10, att_prev = None, None
    out.update(att_sig=sig in a, att_prev=bool(att_prev), att_run=run, att10=att10)
    # (c) signal-day close at limit-up, (d) entry-day one-price lock
    prev = cal[i - 1] if (i is not None and i > 0) else None
    qp = (quote(prev) or {}).get(sid) if prev else None
    qs = (quote(sig) or {}).get(sid)
    qe = (quote(ent) or {}).get(sid) if ent else None
    out.update(q_prev=qp is not None, q_sig=qs is not None, q_ent=qe is not None)
    lim_sig = lim_sig_exch = exr_sig = touch_sig = lock_sig = None
    lim_px = None
    nolim_sig = nolim_ent = None
    lim_src = None
    if qp is None and qs is not None and qs[Q_CHG] == qs[Q_CHG]:
        # fallback without the previous day's file: the exchange reference is
        # close - change (verified equal to the previous day's published
        # reference on 81,330 of 81,330 traded rows); ex-rights undetectable
        ref = qs[Q_CLOSE] - qs[Q_CHG]
        qp = [ref, float("nan"), float("nan"), float("nan"), float("nan"), ref,
              float("nan"), float("nan")]
        lim_src = "ref"
    elif qp is not None:
        lim_src = "prev"
    if qp is not None and qs is not None:
        exr_sig = not _eq(qp[Q_REF], qp[Q_CLOSE])
        # a first-five-days listing has no limit (exchange publishes 9995)
        nolim_sig = bool(qp[Q_UP] == qp[Q_UP] and qp[Q_UP] >= NO_LIMIT)
        lim_px = None if nolim_sig else limit_up(qp[Q_CLOSE], sid)
        at = lim_px is not None and qs[Q_CLOSE] >= lim_px - 1e-6
        lim_sig = bool(at and not exr_sig)
        if lim_src == "ref":
            exr_sig = None          # unknown without the previous day's file
        lim_sig_exch = (bool(qs[Q_CLOSE] >= qp[Q_UP] - 1e-6)
                        if qp[Q_UP] == qp[Q_UP] else None)
        touch_sig = bool(not at and lim_px is not None and qs[Q_HIGH] >= lim_px - 1e-6
                         and not exr_sig)
        lock_sig = bool(lim_sig and _eq(qs[Q_OPEN], qs[Q_HIGH]) and _eq(qs[Q_LOW], qs[Q_HIGH]))
    out.update(lim_src=lim_src)
    out.update(lim_sig=lim_sig, lim_sig_exch=lim_sig_exch, exr_sig=exr_sig, nolim_sig=nolim_sig,
               touch_sig=touch_sig, lock_sig=lock_sig, lim_sig_px=lim_px,
               lim_sig_px_exch=(qp[Q_UP] if qp is not None else None),
               lim_formula_agrees=(None if (qp is None or lim_px is None or exr_sig
                                            or lim_src == "ref")
                                   else _eq(lim_px, qp[Q_UP])))
    lock_ent = open_lim_ent = exr_ent = None
    lim_e = None
    if qs is not None and qe is not None:
        exr_ent = not _eq(qs[Q_REF], qs[Q_CLOSE])
        nolim_ent = bool(qs[Q_UP] == qs[Q_UP] and qs[Q_UP] >= NO_LIMIT)
        # the entry day's limit: the formula on a normal day, the exchange's
        # published limit when the entry day is an ex-rights day
        lim_e = (None if nolim_ent else qs[Q_UP] if exr_ent
                 else limit_up(qs[Q_CLOSE], sid))
        if lim_e is not None and lim_e == lim_e and qe[Q_OPEN] == qe[Q_OPEN]:
            # (the 'one' test also covers the H == L case of a halted print)
            one = _eq(qe[Q_OPEN], qe[Q_HIGH]) and _eq(qe[Q_LOW], qe[Q_HIGH])
            lock_ent = bool(one and qe[Q_OPEN] >= lim_e - 1e-6)
            open_lim_ent = bool(qe[Q_OPEN] >= lim_e - 1e-6)
    out.update(lock_ent=lock_ent, open_lim_ent=open_lim_ent, exr_ent=exr_ent,
               nolim_ent=nolim_ent,
               lim_ent_px=lim_e,
               raw_sig_close=(qs[Q_CLOSE] if qs is not None else None),
               raw_ent_open=(qe[Q_OPEN] if qe is not None else None))
    return out


_QMEMO = {}


def quote(d):
    if d not in _QMEMO:
        _QMEMO[d] = load_quote(d)
    return _QMEMO[d]


def tag_universe(ts, df, disp_by, att_by, cal, si):
    rows = []
    for t, (_, r) in zip(ts, df.iterrows()):
        assert t["sig"] == r["sig"] and str(t["sid"]) == str(r["sid"])
        rows.append(tag_one(str(t["sid"]), t["sig"], t["dates"][0], r["exit"],
                            cal, si, disp_by, att_by, quote))
    T = pd.DataFrame(rows)
    # adjusted-bar cross-check of the entry-day lock (research o/h/l)
    T["adj_one_price_ent"] = [bool(abs(t["h"][0] - t["l"][0]) < 1e-9) for t in ts]
    return T


# ================================================================ scoring
def bucket_rows(b0, b1, mask, label):
    """Per window: the bucket and its complement at slip 0 and SLIP, with
    the bucket's share of the window's return sum."""
    mask = np.asarray(mask, bool)
    out = dict(label=label, windows={})
    for w in c.WINDOWS:
        wm = c.wmask(b0, w)
        tot = b0["ret"].to_numpy(float)[wm].sum()
        res = {}
        for name, mm in (("in", mask[wm]), ("out", ~mask[wm])):
            s0 = c.stats(b0[wm][mm])
            s1 = c.stats(b1[wm][mm])
            res[name] = dict(n=s0["n"], win=s0["win"], mean=s0["mean"], sum=s0["sum"],
                             sum_share=(s0["sum"] / tot if tot else np.nan),
                             n_share=(s0["n"] / int(wm.sum()) if wm.sum() else np.nan),
                             slip_win=s1["win"], slip_mean=s1["mean"])
        out["windows"][w] = res
    return out


def print_bucket(br):
    _say("  [%s]" % br["label"])
    for w in c.WINDOWS:
        for name in ("in", "out"):
            a = br["windows"][w][name]
            _say("    %s %-3s n=%3d (%4.1f%%) win %5.1f%% mean %+6.2f sum %+7.1f (%5.1f%% of sum)"
                 " | slip %5.1f%% %+6.2f" % (w, name, a["n"], 100 * a["n_share"], a["win"],
                                             a["mean"], a["sum"], 100 * a["sum_share"],
                                             a["slip_win"], a["slip_mean"]))


def filter_summary(rf):
    """The compact part of report_filter's output for the JSON."""
    out = dict(label=rf["label"], seven_ok=rf["seven_ok"], money_ok=rf["money_ok"],
               control_ok=rf["control_ok"], candidate=rf["candidate"], windows={})
    for w, r in rf["windows"].items():
        out["windows"][w] = dict(
            base=r["base"], kept=r["kept"], removed=r["removed"],
            boot_win=r["boot_win"], boot_mean=r["boot_mean"],
            halves_kept=r["halves_kept"], halves_base=r["halves_base"],
            quarters=r["quarters"], diff_ci=r["diff_ci"], ev=r["ev"],
            slip_base=r.get("slip_base"), slip_kept=r.get("slip_kept"),
            gates=r["gates"], money_gates=r["money_gates"])
    out["gaps"] = rf["gaps"]
    out["peak"] = rf["peak"]
    if "money" in rf:
        out["money"] = {k: v.drop(columns=["yearly"]).to_dict("records")
                        for k, v in rf["money"].items()}
    return out


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return None if v != v else round(v, 4)
    if isinstance(o, pd.DataFrame):
        return jsonable(o.to_dict("records"))
    return o


# ================================================================ analysis
FLAG_LABELS = (
    ("ent_disp", "(a) entry day inside a TPEX disposition period"),
    ("disp_again", "(a) ... second-or-later disposition (full prepay)"),
    ("disp_first", "(a) ... first disposition"),
    ("disp_known_at_scan", "(a) ... announced before the signal day (visible at 15:00)"),
    ("disp_ann_on_sig", "(a) ... announced the evening of the signal day"),
    ("sig_disp", "(a') signal day itself inside a disposition period"),
    ("hold_disp", "(a'') any disposition day while held"),
    ("att_sig", "(b) on the attention list on the signal day"),
    ("att_prev", "(b) on the attention list the session before the signal"),
    ("att_any10", "(b) any attention day in the last 10 sessions"),
    ("lim_sig", "(c) signal-day close at limit-up (formula, ex-rights skipped)"),
    ("lim_sig_exch", "(c) signal-day close at the exchange's published limit-up"),
    ("lock_sig", "(c) signal day one-price at limit-up"),
    ("touch_sig", "(c) signal day touched limit-up, closed below"),
    ("open_lim_ent", "(d) entry day opened at limit-up"),
    ("lock_ent", "(d) entry day one-price lock at limit-up (unfillable)"),
)


def add_derived(T):
    T = T.copy()
    T["disp_first"] = T["ent_disp"] & ~T["disp_again"]
    T["att_any10"] = T["att10"].fillna(0) > 0
    for col in ("lim_sig", "lim_sig_exch", "lock_sig", "touch_sig", "open_lim_ent", "lock_ent"):
        T[col + "_known"] = T[col].notna()
        T[col] = T[col].fillna(False).astype(bool)
    return T


def _fmt(s):
    return "n=%d %.1f%%/%+.2f" % (s["n"], s["win"], s["mean"])


def analyze_universe(kind, disp_by, att_by, cal, si, ctrl_filters=True, quiet=False):
    ts, b0, b1 = c.base(kind)
    T = add_derived(tag_universe(ts, b0, disp_by, att_by, cal, si))
    res = dict(kind=kind, n=len(ts))
    _say("\n################ %s universe: %d trades" % (kind, len(ts)))
    base = {w: dict(slip0=c.stats(b0[c.wmask(b0, w)]), slip=c.stats(b1[c.wmask(b1, w)]))
            for w in c.WINDOWS}
    res["base"] = base
    for w in c.WINDOWS:
        _say("  base %s: n=%d win %.2f%% mean %+.2f sum %+.1f | slip %.2f%% %+.2f"
             % (w, base[w]["slip0"]["n"], base[w]["slip0"]["win"], base[w]["slip0"]["mean"],
                base[w]["slip0"]["sum"], base[w]["slip"]["win"], base[w]["slip"]["mean"]))
    cov = dict(q_prev=int(T["q_prev"].sum()), q_sig=int(T["q_sig"].sum()),
               q_ent=int(T["q_ent"].sum()),
               exr_sig=int(T["exr_sig"].fillna(False).astype(bool).sum()),
               exr_ent=int(T["exr_ent"].fillna(False).astype(bool).sum()),
               nolim_sig=int(T["nolim_sig"].fillna(False).astype(bool).sum()),
               nolim_ent=int(T["nolim_ent"].fillna(False).astype(bool).sum()),
               lim_formula_agrees=int(T["lim_formula_agrees"].fillna(False).astype(bool).sum()),
               lim_formula_checked=int(T["lim_formula_agrees"].notna().sum()),
               lock_ent_known=int(T["lock_ent_known"].sum()),
               adj_one_price_ent=int(T["adj_one_price_ent"].sum()),
               adj_one_price_and_lock=int((T["adj_one_price_ent"] & T["lock_ent"]).sum()))
    res["coverage"] = cov
    _say("  coverage: %s" % cov)
    res["buckets"] = {}
    for col, lab in FLAG_LABELS:
        br = bucket_rows(b0, b1, T[col].to_numpy(bool), "%s [%s]" % (lab, col))
        res["buckets"][col] = br
        if not quiet:
            print_bucket(br)
    res["att_run"], res["att10"] = {}, {}
    for name, col, grid in (("att_run", "att_run", (0, 1, 2, 3, 4)),
                            ("att10", "att10", (0, 1, 2, 3, 5, 7))):
        _say("  -- %s buckets" % name)
        vals = T[col].fillna(0).to_numpy(int)
        for j, k in enumerate(grid):
            hi = grid[j + 1] if j + 1 < len(grid) else None
            mm = (vals >= k) & ((vals < hi) if hi is not None else True)
            br = bucket_rows(b0, b1, mm, "%s in [%d, %s)" % (col, k, hi))
            res[name][str(k)] = br["windows"]
            a, b = br["windows"]["REC"]["in"], br["windows"]["OLD"]["in"]
            _say("    %s [%d,%s): REC n=%3d win %5.1f%% mean %+6.2f | OLD n=%3d win %5.1f%% mean %+6.2f"
                 % (col, k, hi, a["n"], a["win"], a["mean"], b["n"], b["win"], b["mean"]))
    D = T[T["ent_disp"]]
    det = {}
    for key, col in (("measure_again", "disp_again"), ("match_min", "disp_match_min"),
                     ("prepay_all", "disp_prepay_all"), ("known_at_scan", "disp_known_at_scan"),
                     ("starts_at_entry", "disp_starts_at_entry")):
        g = {}
        for v, idx in D.groupby(col).groups.items():
            for w in c.WINDOWS + ("ALL",):
                sub = b0.loc[idx]
                sub1 = b1.loc[idx]
                if w != "ALL":
                    sub, sub1 = sub[sub["window"] == w], sub1[sub1["window"] == w]
                s, sl = c.stats(sub), c.stats(sub1)
                g["%s|%s" % (v, w)] = dict(n=s["n"], win=s["win"], mean=s["mean"],
                                           sum=s["sum"], slip_win=sl["win"],
                                           slip_mean=sl["mean"])
        det[key] = g
    yr = {}
    for y, idx in T.groupby(T["sig"].str.slice(0, 4)).groups.items():
        dm = T.loc[idx, "ent_disp"].to_numpy(bool)
        sd, so = c.stats(b0.loc[idx][dm]), c.stats(b0.loc[idx][~dm])
        yr[y] = dict(n=len(idx), disp=int(dm.sum()), share=float(dm.mean()),
                     disp_win=sd["win"], disp_mean=sd["mean"], disp_sum=sd["sum"],
                     rest_win=so["win"], rest_mean=so["mean"],
                     att_sig=int(T.loc[idx, "att_sig"].sum()),
                     lim_sig=int(T.loc[idx, "lim_sig"].sum()),
                     lock_ent=int(T.loc[idx, "lock_ent"].sum()))
    det["by_year"] = yr
    # robustness: is "disposition = best bucket" a 2026 story?
    pre = (b0["sig"] < "2026-01-01").to_numpy()
    det["disp_before_2026"] = bucket_rows(b0[pre].reset_index(drop=True),
                                          b1[pre].reset_index(drop=True),
                                          T["ent_disp"].to_numpy(bool)[pre],
                                          "ent_disp, signals before 2026")
    if not quiet:
        print_bucket(det["disp_before_2026"])
    det["exits_in_disp"] = b0.loc[D.index, "why"].value_counts().to_dict()
    det["exits_all"] = b0["why"].value_counts().to_dict()
    res["disp_detail"] = det
    if not quiet:
        for key in ("measure_again", "match_min", "known_at_scan", "starts_at_entry"):
            for k, v in det[key].items():
                _say("    disp %-15s %-10s n=%3d win %5.1f%% mean %+6.2f sum %+7.1f | slip %5.1f%% %+6.2f"
                     % (key, k, v["n"], v["win"], v["mean"], v["sum"], v["slip_win"],
                        v["slip_mean"]))
        _say("  exits in disposition: %s (all: %s)" % (det["exits_in_disp"], det["exits_all"]))
        _say("  by year: %s" % json.dumps(jsonable(yr)))
    xt = {}
    for a_, b_ in (("ent_disp", "lim_sig"), ("ent_disp", "att_sig"), ("lim_sig", "att_sig"),
                   ("ent_disp", "lock_ent"), ("lim_sig", "lock_ent"), ("att_sig", "lock_ent"),
                   ("lim_sig", "open_lim_ent")):
        xt["%s x %s" % (a_, b_)] = {
            "%d%d" % (u, v): int(((T[a_] == bool(u)) & (T[b_] == bool(v))).sum())
            for u in (0, 1) for v in (0, 1)}
    res["crosstab"] = xt
    _say("  crosstabs (00/01/10/11): %s" % json.dumps(xt))
    L = T[T["lock_ent"]]
    res["lock_ent_trades"] = [
        dict(sid=r.sid, sig=r.sig, ent=r.ent, ret=float(b0.loc[i, "ret"]),
             why=b0.loc[i, "why"], window=b0.loc[i, "window"], ent_disp=bool(r.ent_disp),
             lim_sig=bool(r.lim_sig), att_sig=bool(r.att_sig))
        for i, r in L.iterrows()]
    if not quiet:
        for x in res["lock_ent_trades"]:
            _say("    lock_ent: %s" % x)
    res["filters"] = {}
    fl = [("excl_ent_disp", ~T["ent_disp"]),
          ("excl_disp_again", ~T["disp_again"]),
          ("excl_disp_known_at_scan", ~T["disp_known_at_scan"]),
          ("excl_att_sig", ~T["att_sig"]),
          ("excl_att_prev", ~T["att_prev"]),
          ("excl_att_any10", ~T["att_any10"]),
          ("excl_lim_sig", ~T["lim_sig"]),
          ("excl_lim_sig_exch", ~T["lim_sig_exch"]),
          ("excl_lock_sig", ~T["lock_sig"]),
          ("excl_lock_ent_nofill", ~T["lock_ent"]),
          ("excl_open_lim_ent", ~T["open_lim_ent"])]
    for name, keep in fl:
        rf = c.report_filter("%s / %s" % (kind, name), b0, keep.to_numpy(bool), base_slip=b1,
                             ctrl=ctrl_filters, quiet=quiet)
        res["filters"][name] = filter_summary(rf)
    # the same exclusion on signals before 2026 only (2026 holds the extreme
    # disposition year): does blocking pass anywhere?
    pre = (b0["sig"] < "2026-01-01").to_numpy()
    for name, col in (("excl_ent_disp_pre2026", "ent_disp"), ("excl_att_prev_pre2026", "att_prev")):
        rf = c.report_filter("%s / %s" % (kind, name), b0[pre].reset_index(drop=True),
                             ~T[col].to_numpy(bool)[pre], base_slip=b1[pre].reset_index(drop=True),
                             slots=(), ctrl=True, quiet=quiet)
        res["filters"][name] = filter_summary(rf)
    res["plateau"] = {}
    for col, grid in (("att_run", (1, 2, 3, 4, 5)), ("att10", (1, 2, 3, 5, 7))):
        vals = T[col].fillna(0).to_numpy(int)
        rec_w, old_w, rec_m, old_m, cells = [], [], [], [], []
        for k in grid:
            keep = vals < k
            rf = c.report_filter("%s / excl %s>=%d" % (kind, col, k), b0, keep, base_slip=b1,
                                 slots=(), ctrl=True, quiet=True)
            rec_w.append(rf["windows"]["REC"]["kept"]["win"])
            old_w.append(rf["windows"]["OLD"]["kept"]["win"])
            rec_m.append(rf["windows"]["REC"]["kept"]["mean"])
            old_m.append(rf["windows"]["OLD"]["kept"]["mean"])
            cells.append(dict(k=k, seven_ok=rf["seven_ok"], money_ok=rf["money_ok"],
                              control_ok=rf["control_ok"],
                              removed_rec=rf["windows"]["REC"]["removed"],
                              removed_old=rf["windows"]["OLD"]["removed"]))
        pw = c.plateau(list(grid), rec_w, old_w, base["REC"]["slip0"]["win"],
                       base["OLD"]["slip0"]["win"])
        pm = c.plateau(list(grid), rec_m, old_m, base["REC"]["slip0"]["mean"],
                       base["OLD"]["slip0"]["mean"])
        res["plateau"][col] = dict(grid=list(grid), rec_win=rec_w, old_win=old_w,
                                   rec_mean=rec_m, old_mean=old_m, win=pw, mean=pm, cells=cells)
        _say("  plateau excl %s>=k %s: win %s | mean %s" % (col, list(grid), pw["text"], pm["text"]))
        for cc in cells:
            _say("    k=%d seven %s money %s control %s | removed REC n=%d win %.1f mean %+.2f"
                 " OLD n=%d win %.1f mean %+.2f"
                 % (cc["k"], cc["seven_ok"], cc["money_ok"], cc["control_ok"],
                    cc["removed_rec"]["n"], cc["removed_rec"]["win"], cc["removed_rec"]["mean"],
                    cc["removed_old"]["n"], cc["removed_old"]["win"], cc["removed_old"]["mean"]))
    res["disp_slip_stress"] = disp_slip_stress(ts, b0, b1, T)
    res["tags"] = T
    return res


DISP_SLIPS = (0.01, 0.02, 0.03)


def disp_slip_stress(ts, b0, b1, T):
    """Executability stress for the disposition bucket: matching every
    2-20 minutes means a stop/lock exit can print well past its level. The
    bucket (entry inside disposition) is re-replayed with stop/lock exits
    re-priced DISP_SLIPS worse; the rest stays at SLIP. Does the bucket still
    beat the rest?"""
    out = {}
    idx = np.flatnonzero(T["ent_disp"].to_numpy(bool))
    for s in DISP_SLIPS:
        d = c.evaluate([ts[i] for i in idx], s)
        row = {}
        for w in c.WINDOWS:
            dw = d[d["window"] == w]
            rest = b1[(b1["window"] == w).to_numpy() & ~T["ent_disp"].to_numpy(bool)]
            row[w] = dict(disp=c.stats(dw), rest_at_slip=c.stats(rest))
        out[str(s)] = row
        _say("  disposition bucket with stop/lock slip %.1f%%: REC %s (rest %s) | OLD %s (rest %s)"
             % (100 * s, _fmt(row["REC"]["disp"]), _fmt(row["REC"]["rest_at_slip"]),
                _fmt(row["OLD"]["disp"]), _fmt(row["OLD"]["rest_at_slip"])))
    return out


def ungated_disp(disp_by, att_by, cal, si):
    """(a)/(b) on the ungated universe (CORE+ quality legs off): is the
    disposition effect CORE+'s or the market's? No raw quotes needed."""
    ts, b0, b1 = c.base("research", gate="ungated")
    rows = [tag_one(str(t["sid"]), t["sig"], t["dates"][0], r["exit"], cal, si,
                    disp_by, att_by, lambda d: None) for t, (_, r) in zip(ts, b0.iterrows())]
    T = pd.DataFrame(rows)
    out = dict(n=len(ts))
    for col in ("ent_disp", "disp_again", "att_sig"):
        br = bucket_rows(b0, b1, T[col].to_numpy(bool), "ungated %s" % col)
        out[col] = br["windows"]
        print_bucket(br)
    return out


def live_counts(disp_by, att_by):
    """The tradable live signals since LIVE_SINCE: which bucket each sits in.
    Calendar = the TAIEX sessions of the live taiex.db copy (the cloud price
    store keeps only ~68 names after 2026-09-23, so it cannot define the
    calendar), + the next weekday for a signal whose entry has not traded."""
    import sqlite3
    sigs, counters = live_signals()
    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(LIVE_DIR, "taiex.db"), uri=True)
    try:
        d = pd.read_sql("SELECT date FROM TAIEX", con)
    finally:
        con.close()
    cal = sorted(set(d["date"].astype(str).str.slice(0, 10)))
    si = {x: i for i, x in enumerate(cal)}
    rows = []
    for s in sigs:
        ent = s["entry"]
        pending = ent is None
        if pending:
            nd = pd.Timestamp(s["sig"]) + pd.offsets.BDay(1)
            ent = nd.strftime("%Y-%m-%d")
        r = tag_one(s["sid"], s["sig"], ent, None, cal, si, disp_by, att_by, quote)
        r["entry_traded"] = not pending
        rows.append(r)
    T = pd.DataFrame(rows)
    out = dict(n=len(T), counters=counters, signals=jsonable(T.drop(
        columns=[x for x in T.columns if x.startswith("q_")]).to_dict("records")))
    cnt = {}
    for col in ("ent_disp", "disp_again", "disp_known_at_scan", "sig_disp", "att_sig",
                "att_prev", "lim_sig", "lim_sig_exch", "lock_sig", "touch_sig",
                "open_lim_ent", "lock_ent"):
        v = T[col]
        cnt[col] = dict(yes=int((v == True).sum()), no=int((v == False).sum()),  # noqa: E712
                        unknown=int(v.isna().sum()))
    cnt["att10_ge1"] = int((T["att10"].fillna(0) >= 1).sum())
    out["counts"] = cnt
    _say("\n################ live tradable signals since %s: %d" % (LIVE_SINCE, len(T)))
    for _, r in T.iterrows():
        _say("  %s sig %s ent %s%s | disp %s%s | att_sig %s run %d att10 %s | lim_sig %s "
             "(exch %s) | lock_ent %s open_lim %s"
             % (r["sid"], r["sig"], r["ent"], "" if r["entry_traded"] else " (not yet traded)",
                r["ent_disp"], " again" if r["disp_again"] else "", r["att_sig"], r["att_run"],
                r["att10"], r["lim_sig"], r["lim_sig_exch"], r["lock_ent"], r["open_lim_ent"]))
    _say("  counts: %s" % json.dumps(cnt))
    return out


def cmd_analyze():
    t0 = time.time()
    disp, att, missing = load_lists()
    disp_by, att_by = index_lists(disp, att)
    cal = c.trade_calendar()
    si = c.session_index(cal)
    _say("lists: %d disposition periods, %d attention (sid, date); missing %s"
         % (len(disp), len(att), missing))
    out = dict(item="P2-1", built=time.strftime("%Y-%m-%d %H:%M"),
               lists=dict(disposal=len(disp), attention=len(att), missing=missing,
                          att_max=max(d for _, d in att),
                          disp_max_pub=max(r["pub"] for r in disp if r["pub"])))
    res = analyze_universe("research", disp_by, att_by, cal, si)
    T = res.pop("tags")
    os.makedirs(P21, exist_ok=True)
    T.to_pickle(os.path.join(P21, "tags_research.pkl"))
    out["research"] = res
    try:
        lres = analyze_universe("live", disp_by, att_by, cal, si, quiet=True)
        TL = lres.pop("tags")
        TL.to_pickle(os.path.join(P21, "tags_live.pkl"))
        for name, f in lres["filters"].items():
            _say("  live %-26s seven %s money %s control %s | REC kept %s OLD kept %s"
                 % (name, f["seven_ok"], f["money_ok"], f["control_ok"],
                    _fmt(f["windows"]["REC"]["kept"]), _fmt(f["windows"]["OLD"]["kept"])))
        for col in ("ent_disp", "disp_again", "att_sig", "lim_sig", "lock_ent"):
            print_bucket(lres["buckets"][col])
        out["live_set"] = lres
    except FileNotFoundError as e:
        _say("live set skipped: %s" % e)
    _say("\n################ ungated research universe (CORE+ legs off)")
    out["ungated"] = ungated_disp(disp_by, att_by, cal, si)
    out["live_signals"] = live_counts(disp_by, att_by)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(jsonable(out), open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    _say("\nwrote %s (%.0fs)" % (OUT_JSON, time.time() - t0))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    globals()["cmd_" + cmd]()
