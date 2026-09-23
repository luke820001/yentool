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

Each signal is then replayed with scanner.exit_rules.replay_exit on the bars
the price store holds after the signal: next-open entry, every leg of
DEFAULT_RULE including the ride past day 10. A trade whose window has not
finished is "open", never guessed.

Failures are swallowed by the callers; this only observes. ASCII only.
"""
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


def _picks(ledger_file=SIGNAL_LEDGER_FILE, since=None):
    """Prelaunch picks, one per (stock, bar date), with a first-day flag."""
    import sqlite3
    try:
        # closed explicitly: the context manager only commits, and an open
        # handle keeps the file locked on Windows until garbage collection
        conn = sqlite3.connect(ledger_file)
        try:
            p = pd.read_sql_query(
                "SELECT scan_ts, stock_id, stock_name, rank, bar_date, market, "
                "core_plus, buy_ready FROM picks WHERE scan_mode = 'mode_prelaunch'",
                conn)
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()
    if p.empty:
        return p
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


def _replay(fwd):
    """Replay the whole shipped rule on the forward bars. Returns
    (ret_pct, reason, bars) with reason '' for a trade still open."""
    o = _num(fwd["open"]).tolist()
    h = _num(fwd["high"]).tolist()
    l = _num(fwd["low"]).tolist()
    c = _num(fwd["close"]).tolist()
    p = replay_exit(o, h, l, c, hold_bars=DEFAULT_RULE["hold_bars"])
    if p["entry"] is None:
        return None, "na", 0
    if p["exited"]:
        return net_pct(float(p["ret_pct"])), p["reason"], int(p["bar"]) + 1
    # 'na' here means the window is shorter than the hold with no price exit
    # inside it, '' means the ride is still on: both are open trades.
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
                     rank_cut=N_ENTER):
    """Every first-day OTC top-N pick since `since`, with its bucket.

    Returns (rows, counters). Each row: sid, sig, name, rank, core (True /
    False / None), regime (bool), bucket ('tradable' | 'regime_closed' |
    'not_core'), and `series` (the stock's full price frame, for callers that
    replay something on it). Shared by build_live_record and
    tools/ledger_audit.py so both answer "was this a signal" the same way.
    """
    counters = {"candidates": 0, "core_recomputed": 0, "core_unknown": 0,
                "through": None}
    picks = _picks(ledger_file, since)
    if picks.empty:
        return [], counters
    regime = _regime_by_date(taiex_file)
    cand = picks[picks["first_day"] & (picks["market"] == "OTC")
                 & (_num(picks["rank"]) < rank_cut)]
    counters["candidates"] = int(len(cand))
    counters["through"] = str(picks["bar_date"].max())[:10]
    rows = []
    series_cache = {}
    for _, r in cand.iterrows():
        sid, sig = r["stock_id"], r["bar_date"]
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
                     "bar_index": i, "series": s})
    return rows, counters


def build_live_record(since="2026-06-25", ledger_file=SIGNAL_LEDGER_FILE,
                      price_file=PRICE_VOLUME_FILE, taiex_file=TAIEX_FILE,
                      rank_cut=N_ENTER):
    """The record, as a JSON-ready dict. See the module docstring."""
    out = {
        "since": str(since)[:10],
        "through": None,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rule": "next-open entry; stop {:.0%}, tp {:.0%}, arm {:.1%}, lock "
                "{:.0%}, late from day {} at +{:.0%}, hold {}, ride to {}".format(
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
    }
    signals, counters = classify_signals(since, ledger_file, price_file,
                                         taiex_file, rank_cut)
    out.update({k: v for k, v in counters.items() if v is not None or k != "through"})
    if not signals:
        return out

    buckets = {"tradable": [], "regime_closed": [], "not_core": []}
    for sg in signals:
        s, i = sg["series"], sg["bar_index"]
        fwd = s.iloc[i + 1:i + 1 + DEFAULT_RULE["ride_cap"] + 1]
        if fwd.empty:
            row = {"sid": sg["sid"], "sig": sg["sig"], "ret": None, "exit": "", "bars": 0}
        else:
            ret, why, bars = _replay(fwd)
            if why == "na":
                continue
            row = {"sid": sg["sid"], "sig": sg["sig"], "name": sg["name"],
                   "ret": None if ret is None else round(ret, 2),
                   "exit": why, "bars": bars}
        buckets[sg["bucket"]].append(row)

    out["tradable"] = _bucket(buckets["tradable"])
    out["tradable"]["trades"] = sorted(buckets["tradable"], key=lambda x: x["sig"])[-TRADES_KEPT:]
    out["not_core"] = _bucket(buckets["not_core"])
    out["regime_closed"] = _bucket(buckets["regime_closed"])
    return out
