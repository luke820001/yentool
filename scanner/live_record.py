"""
What the shipped rule has actually done on the signals the LIVE scanner emitted.

Why this exists (2026-09-23). The owner reported that real profit is poor
while every screen quotes the backtest (70.8% wins, +1.95% per trade). Both
were true at once, and nothing in the app could show it: the ledger stores
forward windows per pick, not "the rule as traded", and the phone's strategy
card carried the backtest only. Replaying the whole rule on the signals the
scanner really published since the ledger began gave, for 2026-06-25..09-22,
nine tradable signals at 55.6% / -1.38% -- and 54 names the list showed but
the badge refused, at 59% / -4.32%. That is the number the owner lives with,
so it is published next to the backtest.

Population (the buy rule as scan_mode.mark_buy_ready defines it, applied to
the ledger's prelaunch picks):
  * one row per (stock, bar date), the latest scan of the day;
  * first day on the list (absent on the previous ledger session);
  * OTC, rank below N_ENTER;
  * CORE+: the ledger's own core_plus when it recorded one (2026-09-09 on),
    otherwise recomputed from the price store the way the scanner does it,
    with a shorter 52-week window when the store has fewer than 240 bars
    before the signal (counted in `core_recomputed` / `core_unknown`);
  * TAIEX above its 20- and 60-day means on the signal day.

Each signal is then replayed with replay_trade (scanner.exit_rules.
replay_exit) on the bars the price store holds after the signal: next-open
entry, every leg of DEFAULT_RULE including the ride past day 10, and since
2026-10-08 the market half of that ride (TAIEX below its 20MA but above its
60MA on the day the time exit would fire, scanner/market_leg.py). A trade
whose window has not finished is "open", never guessed. Each trade row
carries entry_date / exit_date.

Trade restrictions (2026-10-08). The ledger's gate_detail carries the
restriction in force when the pick was recorded (scanner/trade_restrictions:
disposition, attention, ...). The record does NOT exclude any of them by
default -- a disposition is display + order guidance, not a buy block -- it
counts the candidates by kind (`restrictions`, 'unrecorded' before the field
existed) and splits the tradable trades by kind (`by_restriction`). A caller
can pass exclude_restrictions (e.g. trade_restrictions.BLOCKING_RESTRICTIONS)
to drop those signals; they are then counted in `restricted_excluded`.

Per-name history and a benchmark (2026-10-08, plan P1-8 / P1-5 phase B;
display only):
  * out['by_sid'] = {sid: [<= BY_SID_KEPT entries, oldest first]} over ALL
    three buckets, each {sig, bucket, rank, ret, exit, bars, entry_date,
    exit_date, restriction}. A not_core or regime_closed entry is a signal
    the rule did NOT trade, and the phone labels it so. `sids` (today's rows
    plus tracked) limits the payload; without it every candidate is listed.
  * out['bench'] = the TAIEX ('TAIEX' table) and OTC index ('TPEX' table,
    ingestion/otc_index) over the same span (since -> through), a thinned
    series for the chart, and the indices over each trade's own holding
    window (signal-day close -> exit-day close; each trade row carries
    taiex_pct / otc_pct). Missing data is None, never an exception.

Failures are swallowed by the callers; this only observes. ASCII only.
"""
import json
from datetime import datetime

import pandas as pd

from config.settings import PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE, TAIEX_FILE
from scanner.exit_rules import DEFAULT_RULE, replay_exit
from scanner.scan_mode import (
    CORE_PLUS_ATR_MIN, CORE_PLUS_DIST52_MAX, CORE_PLUS_RET5_MAX, N_ENTER,
)
from storage.data_store import load_sheet

MIN_BARS_FOR_CORE = 120     # fewer bars before the signal: CORE+ is unknown
FULL_52W_BARS = 240         # analyzer.trend_analysis._MIN_52W_BARS
TRADES_KEPT = 40            # most recent tradable trades listed in the payload
BY_SID_KEPT = 5             # most recent signals per name in out['by_sid']
BENCH_POINTS = 120          # most points in out['bench']['series']
# Round trip the way every backtest figure carries it (archive/research/
# sandbox_entry_gate): 0.1425% commission each way plus 0.3% tax on the sell.
BUY_COST = 0.001425
SELL_COST = 0.001425 + 0.003


def net_pct(gross_pct):
    """A gross exit/entry return, after commission and tax, in percent."""
    return ((1.0 + gross_pct / 100.0) * (1.0 - SELL_COST) / (1.0 + BUY_COST)
            - 1.0) * 100.0


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _regime_by_date(taiex_file=TAIEX_FILE):
    """{date: TAIEX close above both its 20- and 60-day means}."""
    t = load_sheet(taiex_file, "TAIEX")
    if t.empty or "close" not in t.columns:
        return {}
    t = t.copy()
    t["close"] = _num(t["close"])
    t = t.dropna(subset=["close"]).sort_values("date")
    c = t["close"]
    ok = (c > c.rolling(20).mean()) & (c > c.rolling(60).mean())
    return {str(d)[:10]: bool(v) for d, v in zip(t["date"], ok)}


def _restriction_of(gate_detail):
    """The Trade_Restriction a pick was recorded with (signal_ledger
    gate_detail JSON, 2026-10-08 on), else None."""
    if gate_detail is None:
        return None
    try:
        g = json.loads(gate_detail) if isinstance(gate_detail, str) else gate_detail
    except (TypeError, ValueError):
        return None
    v = g.get("restriction") if isinstance(g, dict) else None
    v = str(v).strip() if v is not None else ""
    return v or None


def _picks(ledger_file=SIGNAL_LEDGER_FILE, since=None):
    """Prelaunch picks, one per (stock, bar date), with a first-day flag and
    the recorded trade restriction (None when not recorded)."""
    import sqlite3
    try:
        # closed explicitly: the context manager only commits, and an open
        # handle keeps the file locked on Windows until garbage collection
        conn = sqlite3.connect(ledger_file)
        try:
            # gate_detail is a later column (F17); an older ledger has none
            cols = {r[1] for r in conn.execute("PRAGMA table_info(picks)")}
            extra = ", gate_detail" if "gate_detail" in cols else ""
            p = pd.read_sql_query(
                "SELECT scan_ts, stock_id, stock_name, rank, bar_date, market, "
                "core_plus, buy_ready" + extra + " FROM picks "
                "WHERE scan_mode = 'mode_prelaunch'",
                conn)
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()
    if p.empty:
        return p
    p["restriction"] = ([_restriction_of(g) for g in p["gate_detail"]]
                        if "gate_detail" in p.columns else [None] * len(p))
    p["stock_id"] = p["stock_id"].astype(str)
    p["bar_date"] = p["bar_date"].astype(str).str.slice(0, 10)
    p = p.sort_values("scan_ts").drop_duplicates(["stock_id", "bar_date"], keep="last")
    sessions = sorted(p["bar_date"].unique())
    prev = {d: (sessions[i - 1] if i else None) for i, d in enumerate(sessions)}
    on_list = set(zip(p["stock_id"], p["bar_date"]))
    p["first_day"] = [(prev[d] is None) or ((s, prev[d]) not in on_list)
                      for s, d in zip(p["stock_id"], p["bar_date"])]
    if since:
        p = p[p["bar_date"] >= str(since)[:10]]
    return p.reset_index(drop=True)


def _core_from_bars(hist):
    """(core_plus or None, recomputed_with_full_window)."""
    c = _num(hist["close"])
    if len(c) < MIN_BARS_FOR_CORE:
        return None, False
    full = len(c) >= FULL_52W_BARS
    h52 = c.rolling(252, min_periods=min(FULL_52W_BARS, len(c))).max().iloc[-1]
    if pd.isna(h52) or float(h52) <= 0:
        return None, full
    dist52 = (float(h52) - float(c.iloc[-1])) / float(h52) * 100
    if len(c) < 6 or float(c.iloc[-6]) <= 0:
        return None, full
    ret5 = (float(c.iloc[-1]) / float(c.iloc[-6]) - 1) * 100
    atr = ((_num(hist["high"]) - _num(hist["low"])) / c).rolling(20).mean().iloc[-1]
    if pd.isna(atr):
        return None, full
    core = (dist52 <= CORE_PLUS_DIST52_MAX and ret5 <= CORE_PLUS_RET5_MAX
            and float(atr) * 100 >= CORE_PLUS_ATR_MIN)
    return bool(core), full


def replay_trade(fwd, extend_if=None, hold_bars=None, ride_cap=None):
    """THE canonical replay of one trade (2026-10-08): every leg of
    DEFAULT_RULE -- next-open entry, stop/lock/tp/late, the time exit and the
    ride past it, plus the market leg when `extend_if` is given
    (scanner.market_leg.make_disturbed_fn). The holding tracker, the
    recommendation lifecycle and this record all book a trade the way this
    function does, so the card, the recommendation and the record cannot
    disagree about one trade.

    `fwd` holds the bars AFTER the signal, oldest first, with columns open /
    high / low / close and, when available, date; its first bar is the entry.
    `hold_bars` / `ride_cap` default to DEFAULT_RULE (a caller with a per-mode
    hold passes its own).

    Returns a dict:
      exited          the rule has booked an exit inside the window
      reason          'stop' | 'lock' | 'tp' | 'late' | 'time' when exited;
                      '' while the trade is open (window shorter than the
                      hold, or the ride still on); 'na' only when the entry
                      bar is unusable (no fill can be priced)
      entry_date      date of the first bar (None without a date column)
      entry_price     the fill, the first bar's open (None for 'na')
      exit_date       date of the exit bar, else None
      exit_price      the price the rule books, else None
      bars            trading bars held through the exit (1-based), else the
                      bars available so far
      ret_gross_pct   exit_price / entry_price - 1, in percent, else None
      ret_net_pct     the same after BUY_COST / SELL_COST (net_pct), else None
    """
    hold = DEFAULT_RULE["hold_bars"] if hold_bars is None else int(hold_bars)
    cap = DEFAULT_RULE["ride_cap"] if ride_cap is None else ride_cap
    n = 0 if fwd is None else len(fwd)
    dates = None
    if n and "date" in getattr(fwd, "columns", ()):
        dates = [str(d)[:10] if d is not None and d == d else None
                 for d in fwd["date"].tolist()]
    out = {"exited": False, "reason": "", "entry_date": dates[0] if dates else None,
           "entry_price": None, "exit_date": None, "exit_price": None,
           "bars": n, "ret_gross_pct": None, "ret_net_pct": None}
    if not n:
        out["reason"] = "na"
        return out
    o = _num(fwd["open"]).tolist()
    h = _num(fwd["high"]).tolist()
    l = _num(fwd["low"]).tolist()
    c = _num(fwd["close"]).tolist()
    p = replay_exit(o, h, l, c, dates=dates, hold_bars=hold, ride_cap=cap,
                    extend_if=extend_if)
    if p["entry"] is None:
        out["reason"] = "na"
        out["bars"] = 0
        return out
    out["entry_price"] = float(p["entry"])
    if p["exited"]:
        gross = float(p["ret_pct"])
        out.update(exited=True, reason=p["reason"], exit_date=p.get("date"),
                   exit_price=float(p["exit_price"]), bars=int(p["bar"]) + 1,
                   ret_gross_pct=gross, ret_net_pct=net_pct(gross))
    # Not exited: 'na' from replay_exit here means the window is shorter than
    # the hold with no price exit inside it, '' that the ride is still on --
    # both are open trades, reported as reason ''.
    return out


def _replay(fwd, extend_if=None):
    """Replay the whole shipped rule on the forward bars. Returns
    (ret_pct, reason, bars) with reason '' for a trade still open. A thin
    wrapper over replay_trade, kept for its callers."""
    t = replay_trade(fwd, extend_if=extend_if)
    if t["reason"] == "na":
        return None, "na", 0
    if t["exited"]:
        return t["ret_net_pct"], t["reason"], t["bars"]
    return None, "", len(fwd)


def _bucket(rows):
    closed = [r for r in rows if r.get("ret") is not None]
    out = {"closed": len(closed), "open": len(rows) - len(closed)}
    if closed:
        rets = [r["ret"] for r in closed]
        out["win_pct"] = round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 1)
        out["mean_pct"] = round(sum(rets) / len(rets), 2)
        out["sum_pct"] = round(sum(rets), 1)
    return out


def classify_signals(since="2026-06-25", ledger_file=SIGNAL_LEDGER_FILE,
                     price_file=PRICE_VOLUME_FILE, taiex_file=TAIEX_FILE,
                     rank_cut=N_ENTER, exclude_restrictions=()):
    """Every first-day OTC top-N pick since `since`, with its bucket.

    Returns (rows, counters). Each row: sid, sig, name, rank, core (True /
    False / None), regime (bool), bucket ('tradable' | 'regime_closed' |
    'not_core'), restriction (the recorded Trade_Restriction or None), and
    `series` (the stock's full price frame, for callers that replay something
    on it). Shared by build_live_record and tools/ledger_audit.py so both
    answer "was this a signal" the same way.

    counters["restrictions"] counts the candidates by recorded restriction
    ('unrecorded' when the pick predates the field). Candidates whose
    restriction is in `exclude_restrictions` (default none) are dropped and
    counted in counters["restricted_excluded"].
    """
    counters = {"candidates": 0, "core_recomputed": 0, "core_unknown": 0,
                "through": None, "restrictions": {}, "restricted_excluded": 0}
    picks = _picks(ledger_file, since)
    if picks.empty:
        return [], counters
    regime = _regime_by_date(taiex_file)
    cand = picks[picks["first_day"] & (picks["market"] == "OTC")
                 & (_num(picks["rank"]) < rank_cut)]
    counters["candidates"] = int(len(cand))
    counters["through"] = str(picks["bar_date"].max())[:10]
    excluded = set(str(k) for k in (exclude_restrictions or ()))
    rows = []
    series_cache = {}
    for _, r in cand.iterrows():
        sid, sig = r["stock_id"], r["bar_date"]
        restr = r.get("restriction") if "restriction" in cand.columns else None
        restr = restr if isinstance(restr, str) and restr else None
        key = restr or "unrecorded"
        counters["restrictions"][key] = counters["restrictions"].get(key, 0) + 1
        if restr is not None and restr in excluded:
            counters["restricted_excluded"] += 1
            continue
        if sid not in series_cache:
            s = load_sheet(price_file, sid)
            if not s.empty and "date" in s.columns:
                s = s.copy()
                s["date"] = pd.to_datetime(s["date"], errors="coerce").dt.strftime("%Y-%m-%d")
                s = s.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
            series_cache[sid] = s
        s = series_cache[sid]
        if s.empty or "date" not in s.columns:
            continue
        idx = s.index[s["date"] == sig]
        if not len(idx):
            continue
        i = int(idx[0])
        core = r.get("core_plus")
        if core is None or (isinstance(core, float) and core != core):
            core, full = _core_from_bars(s.iloc[:i + 1])
            if core is None:
                counters["core_unknown"] += 1
            elif not full:
                counters["core_recomputed"] += 1
        else:
            core = bool(int(core))
        reg = bool(regime.get(sig, False))
        bucket = ("tradable" if (core is True and reg)
                  else "regime_closed" if core is True else "not_core")
        rows.append({"sid": sid, "sig": sig, "name": r.get("stock_name"),
                     "rank": int(_num(pd.Series([r["rank"]])).iloc[0]),
                     "core": core, "regime": reg, "bucket": bucket,
                     "restriction": restr, "bar_index": i, "series": s})
    return rows, counters


def _index_closes(taiex_file, sheet):
    """[(date, close)] ascending from one table of taiex.db; [] when the
    table is missing or empty."""
    try:
        t = load_sheet(taiex_file, sheet)
    except Exception:
        return []
    if t.empty or "close" not in t.columns or "date" not in t.columns:
        return []
    t = t[["date", "close"]].copy()
    t["date"] = t["date"].astype(str).str.slice(0, 10)
    t["close"] = _num(t["close"])
    t = t.dropna().drop_duplicates("date", keep="last").sort_values("date")
    return [(d, float(c)) for d, c in zip(t["date"], t["close"]) if c > 0]


def _close_on_or_before(closes, day):
    """(date, close) of the last bar on or before `day`, else None."""
    best = None
    for d, c in closes:
        if d > day:
            break
        best = (d, c)
    return best


def _pct(a, b):
    return round((b / a - 1.0) * 100.0, 2) if a and b else None


def _window_pct(closes, start, end):
    """Index change from the close on `start` to the close on `end`; None
    when either bar is missing (an index table that lags must not read as
    a flat market)."""
    if not closes or not start or not end:
        return None
    a = _close_on_or_before(closes, start)
    b = _close_on_or_before(closes, end)
    if a is None or b is None or a[0] != start or b[0] != end:
        return None
    return _pct(a[1], b[1])


def _bench(taiex_file, since, through, trades):
    """out['bench'] (see the module docstring), or None when neither index
    has a bar in the span."""
    tx = _index_closes(taiex_file, "TAIEX")
    ox = _index_closes(taiex_file, "TPEX")
    since, through = str(since)[:10], str(through or "")[:10]
    if not through:
        return None
    span = {}
    for key, closes in (("taiex", tx), ("otc", ox)):
        inside = [(d, c) for d, c in closes if since <= d <= through]
        span[key] = inside
    if not span["taiex"] and not span["otc"]:
        return None
    out = {"from": since, "to": through}
    for key in ("taiex", "otc"):
        inside = span[key]
        first = inside[0] if inside else None
        last = inside[-1] if inside else None
        out[key + "_from"] = round(first[1], 2) if first else None
        out[key + "_from_date"] = first[0] if first else None
        out[key + "_to"] = round(last[1], 2) if last else None
        out[key + "_to_date"] = last[0] if last else None
        out[key + "_pct"] = (_pct(first[1], last[1])
                             if first and last and last[0] > first[0] else None)
    # the chart: both closes per date, thinned to BENCH_POINTS (first and
    # last always kept)
    tmap = dict(span["taiex"])
    omap = dict(span["otc"])
    days = sorted(set(tmap) | set(omap))
    if len(days) > BENCH_POINTS:
        step = (len(days) - 1) / float(BENCH_POINTS - 1)
        pick = sorted({int(round(i * step)) for i in range(BENCH_POINTS)})
        days = [days[i] for i in pick]
    out["series"] = [[d, round(tmap[d], 2) if d in tmap else None,
                      round(omap[d], 2) if d in omap else None] for d in days]
    # the indices over each closed tradable trade's own window. A trade
    # whose window one index cannot price (that table lags) is left out of
    # THAT index's figures, so each index also carries the trades' own
    # mean/sum over exactly the trades it priced (<key>_trade_*): compare
    # <key>_mean_pct with <key>_trade_mean_pct, never with trade_mean_pct
    # when <key>_n < n (verifier, 2026-10-08).
    win = {"n": 0, "trade_sum_pct": 0.0}
    for key in ("taiex", "otc"):
        win[key + "_n"] = 0
        win[key + "_sum_pct"] = 0.0
        win[key + "_trade_sum_pct"] = 0.0
    for t in trades:
        if t.get("ret") is None:
            continue
        win["n"] += 1
        win["trade_sum_pct"] += float(t["ret"])
        for key in ("taiex", "otc"):
            v = t.get(key + "_pct")
            if v is not None:
                win[key + "_n"] += 1
                win[key + "_sum_pct"] += float(v)
                win[key + "_trade_sum_pct"] += float(t["ret"])
    win["trade_mean_pct"] = (round(win["trade_sum_pct"] / win["n"], 2)
                             if win["n"] else None)
    win["trade_sum_pct"] = round(win["trade_sum_pct"], 2)
    for key in ("taiex", "otc"):
        n = win[key + "_n"]
        win[key + "_mean_pct"] = (round(win[key + "_sum_pct"] / n, 2)
                                  if n else None)
        win[key + "_sum_pct"] = round(win[key + "_sum_pct"], 2)
        win[key + "_trade_mean_pct"] = (
            round(win[key + "_trade_sum_pct"] / n, 2) if n else None)
        win[key + "_trade_sum_pct"] = round(win[key + "_trade_sum_pct"], 2)
    out["windows"] = win
    out["note"] = ("index closes, not total return; the span runs from the "
                   "first index close on or after `from`; a trade's window "
                   "from its signal-day close to its exit-day close")
    return out


def build_live_record(since="2026-06-25", ledger_file=SIGNAL_LEDGER_FILE,
                      price_file=PRICE_VOLUME_FILE, taiex_file=TAIEX_FILE,
                      rank_cut=N_ENTER, exclude_restrictions=(), sids=None):
    """The record, as a JSON-ready dict. See the module docstring.

    `sids` (optional iterable of stock ids: today's rows plus tracked)
    limits out['by_sid'] to those names; None lists every candidate."""
    out = {
        "since": str(since)[:10],
        "through": None,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rule": "next-open entry; stop {:.0%}, tp {:.0%}, arm {:.1%}, lock "
                "{:.0%}, late from day {} at +{:.0%}, hold {}, ride to {} while the "
                "close is above its 5-bar mean or TAIEX is below its 20MA and "
                "above its 60MA on that date".format(
                    DEFAULT_RULE["stop_pct"], DEFAULT_RULE["tp_pct"],
                    DEFAULT_RULE["arm_pct"], DEFAULT_RULE["lock_pct"],
                    DEFAULT_RULE["late_from"], DEFAULT_RULE["late_gain"],
                    DEFAULT_RULE["hold_bars"], DEFAULT_RULE["ride_cap"]),
        "candidates": 0,
        "core_recomputed": 0,
        "core_unknown": 0,
        "tradable": {"closed": 0, "open": 0, "trades": []},
        "not_core": {"closed": 0, "open": 0},
        "regime_closed": {"closed": 0, "open": 0},
        "restrictions": {},
        "restricted_excluded": 0,
        "by_restriction": {},
        "by_sid": {},
        "bench": None,
    }
    signals, counters = classify_signals(since, ledger_file, price_file,
                                         taiex_file, rank_cut,
                                         exclude_restrictions=exclude_restrictions)
    out.update({k: v for k, v in counters.items() if v is not None or k != "through"})
    if not signals:
        try:
            out["bench"] = _bench(taiex_file, out["since"], out.get("through"), [])
        except Exception:
            out["bench"] = None
        return out
    tx = _index_closes(taiex_file, "TAIEX")
    ox = _index_closes(taiex_file, "TPEX")

    # The market leg of the ride (TAIEX below its 20MA, above its 60MA on the
    # day the time exit would fire), read per date from the same TAIEX file
    # the regime buckets use. 2026-10-08: until then this record replayed the
    # rule without it while the tracker applied it off TODAY's market.
    try:
        from scanner.market_leg import make_disturbed_fn
        disturbed = make_disturbed_fn(taiex_file)
    except Exception:
        disturbed = None
    buckets = {"tradable": [], "regime_closed": [], "not_core": []}
    for sg in signals:
        s, i = sg["series"], sg["bar_index"]
        fwd = s.iloc[i + 1:i + 1 + DEFAULT_RULE["ride_cap"] + 1]
        if fwd.empty:
            row = {"sid": sg["sid"], "sig": sg["sig"], "name": sg["name"],
                   "ret": None, "exit": "",
                   "bars": 0, "entry_date": None, "exit_date": None,
                   "restriction": sg.get("restriction")}
        else:
            t = replay_trade(fwd, extend_if=disturbed)
            if t["reason"] == "na":
                continue
            ret = t["ret_net_pct"] if t["exited"] else None
            row = {"sid": sg["sid"], "sig": sg["sig"], "name": sg["name"],
                   "ret": None if ret is None else round(ret, 2),
                   "exit": t["reason"] if t["exited"] else "",
                   "bars": t["bars"] if t["exited"] else len(fwd),
                   "entry_date": t["entry_date"],
                   "exit_date": t["exit_date"] if t["exited"] else None,
                   "restriction": sg.get("restriction")}
            if row["exit_date"]:
                row["taiex_pct"] = _window_pct(tx, sg["sig"], row["exit_date"])
                row["otc_pct"] = _window_pct(ox, sg["sig"], row["exit_date"])
        row["bucket"] = sg["bucket"]
        row["rank"] = sg.get("rank")
        buckets[sg["bucket"]].append(row)

    out["tradable"] = _bucket(buckets["tradable"])
    out["tradable"]["trades"] = sorted(buckets["tradable"], key=lambda x: x["sig"])[-TRADES_KEPT:]
    out["not_core"] = _bucket(buckets["not_core"])
    out["regime_closed"] = _bucket(buckets["regime_closed"])
    # the tradable trades split by the restriction recorded on the signal
    # day (counters only; nothing is excluded unless asked)
    by = {}
    for row in buckets["tradable"]:
        by.setdefault(row.get("restriction") or "unrecorded", []).append(row)
    out["by_restriction"] = {k: _bucket(v) for k, v in sorted(by.items())}

    # per-name history over ALL buckets (P1-8 phase B)
    want = None if sids is None else {str(x).strip() for x in sids}
    hist = {}
    for rows in buckets.values():
        for row in rows:
            if want is not None and row["sid"] not in want:
                continue
            hist.setdefault(row["sid"], []).append(
                {k: row.get(k) for k in ("sig", "bucket", "rank", "ret",
                                         "exit", "bars", "entry_date",
                                         "exit_date", "restriction")})
    out["by_sid"] = {sid: sorted(v, key=lambda e: e["sig"])[-BY_SID_KEPT:]
                     for sid, v in sorted(hist.items())}

    try:
        out["bench"] = _bench(taiex_file, out["since"], out.get("through"),
                              buckets["tradable"])
    except Exception:
        out["bench"] = None
    return out
