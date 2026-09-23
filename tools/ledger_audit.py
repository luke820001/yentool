"""
Audit the owner's real trades (a phone backup) against the rule they follow.

Why (2026-09-23). The owner reported poor real profit. The strategy was then
measured on the scanner's own signals (scanner/live_record.py) -- but that
measures the RULE, not the owner's execution. Every real trade can differ
from the rule in four ways, and one -20% trade that ignored the stop costs
ten rule-following trades. This tool answers, per position:

  1. Was the buy a signal at all?  (the buy rule, via live_record.classify_signals:
     a first-day OTC top-20 name that was CORE+ on a tailwind day = 可買訊號;
     on the list but refused = 名單上不可買 / 核心+但大盤未順風; else 名單外)
  2. What would the rule have done from the OWNER'S OWN fill?  The fill
     replaces the first bar's open and scanner/exit_rules.replay_exit runs
     the whole shipped rule (stop, lock, target, late take, hold, ride).
  3. What did the owner actually do?  Sells from the ledger: average price,
     last sell date, sessions held.
  4. The difference, and a flag for the four ways it can go wrong:
     stop_not_taken, sold_early, held_past_cap, no_signal.

Usage:
    python tools/ledger_audit.py path/to/yentool-ledger-YYYY-MM-DD.json
        [--since 2026-06-25] [--json out.json]

The backup is mobile/app.js exportBackup(): {schema: "yentool-mobile-ledger",
positions: [...], executions: [...]}. It stays on this machine: nothing here
uploads, and data/ never receives it. ASCII only; Chinese only in output labels.
"""
import argparse
import json
import os
import sys

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from config.settings import PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE, TAIEX_FILE  # noqa: E402
from scanner.exit_rules import DEFAULT_RULE, replay_exit                         # noqa: E402
from scanner.live_record import classify_signals, net_pct                        # noqa: E402
from storage.data_store import load_sheet                                        # noqa: E402

RESEARCH_PRICES = os.path.join(ROOT, "data", "research_prices.db")

BUCKET_LABEL = {
    "tradable": u"可買訊號",                              # 可買訊號
    "not_core": u"名單上不可買",                  # 名單上不可買
    "regime_closed": u"核心+但大盤未順風",  # 核心+但大盤未順風
    "none": u"名單外",                                        # 名單外
}
SIGNAL_LOOKBACK = 5     # sessions before the buy in which a signal counts


# ---------------------------------------------------------------- the ledger
def load_backup(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema") != "yentool-mobile-ledger":
        raise ValueError("not a yentool-mobile-ledger backup")
    return data.get("positions") or [], data.get("executions") or []


def fills(executions, position_id):
    """Live (not voided) fills of one position, oldest first."""
    rows = [e for e in executions
            if e.get("position_id") == position_id and not e.get("voided_at")]
    rows.sort(key=lambda e: (str(e.get("session_date") or ""), str(e.get("created_at") or "")))
    return rows


def fill_summary(rows):
    """First buy, average cost, sells, what is left. Prices in dollars."""
    buys = [e for e in rows if e.get("side") == "BUY"]
    sells = [e for e in rows if e.get("side") == "SELL"]
    if not buys:
        return None
    b_sh = sum(float(e.get("shares") or 0) for e in buys)
    b_amt = sum(float(e.get("shares") or 0) * float(e.get("price_cents") or 0) / 100 for e in buys)
    s_sh = sum(float(e.get("shares") or 0) for e in sells)
    s_amt = sum(float(e.get("shares") or 0) * float(e.get("price_cents") or 0) / 100 for e in sells)
    first = buys[0]
    return {
        "first_buy_date": str(first.get("session_date") or "")[:10],
        "first_buy_price": float(first.get("price_cents") or 0) / 100,
        "buy_shares": b_sh, "avg_cost": (b_amt / b_sh) if b_sh else None,
        "sell_shares": s_sh, "avg_sell": (s_amt / s_sh) if s_sh else None,
        "last_sell_date": str(sells[-1].get("session_date") or "")[:10] if sells else None,
        "open_shares": b_sh - s_sh,
        "n_buys": len(buys), "n_sells": len(sells),
    }


# ----------------------------------------------------------------- the prices
def price_series(sid, price_file=PRICE_VOLUME_FILE, research_file=RESEARCH_PRICES):
    """Research history first (to 2026-09), then the live store on top."""
    frames = []
    if research_file and os.path.exists(research_file):
        import sqlite3
        conn = sqlite3.connect(research_file)
        try:
            frames.append(pd.read_sql_query(
                "SELECT date, open, high, low, close FROM data WHERE stock_id = ?",
                conn, params=(sid,)))
        finally:
            conn.close()
    live = load_sheet(price_file, sid)
    if not live.empty and "date" in live.columns:
        frames.append(live[["date", "open", "high", "low", "close"]])
    if not frames:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    d = pd.concat(frames, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d.dropna(subset=["date"]).drop_duplicates("date", keep="last").sort_values("date")
    for c in ("open", "high", "low", "close"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d.reset_index(drop=True)


# ------------------------------------------------------------------ the rule
def rule_from_fill(series, fill_date, fill_price):
    """Replay the shipped rule with the owner's fill as the entry.

    The fill is the open of the first bar by definition of the rule, so the
    bar of `fill_date` has its open replaced by `fill_price`. Returns the
    replay_exit dict plus 'sessions' (bars available) and 'exit_date'.
    """
    idx = series.index[series["date"] >= fill_date]
    if not len(idx):
        return None
    i = int(idx[0])
    fwd = series.iloc[i:i + DEFAULT_RULE["ride_cap"] + 1].copy()
    if fwd.empty:
        return None
    o = fwd["open"].tolist()
    o[0] = float(fill_price)
    p = replay_exit(o, fwd["high"].tolist(), fwd["low"].tolist(), fwd["close"].tolist(),
                    dates=fwd["date"].tolist(), hold_bars=DEFAULT_RULE["hold_bars"])
    p["sessions"] = len(fwd)
    p["exit_date"] = p.get("date")
    return p


def sessions_between(series, a, b):
    """Trading sessions from a (inclusive) to b (inclusive) in the series."""
    if not a or not b:
        return None
    d = series["date"]
    return int(((d >= a) & (d <= b)).sum())


# ---------------------------------------------------------------- the match
def match_signal(signals, sid, buy_date, series):
    """The nearest signal of this stock in the SIGNAL_LOOKBACK sessions
    before the buy (a signal on day D is bought on D+1)."""
    dates = series["date"].tolist()
    if buy_date in dates:
        k = dates.index(buy_date)
        window = set(dates[max(0, k - SIGNAL_LOOKBACK):k])
    else:
        window = set(d for d in dates if d < buy_date)
    best = None
    for sg in signals:
        if sg["sid"] == sid and sg["sig"] in window:
            if best is None or sg["sig"] > best["sig"]:
                best = sg
    return best


# --------------------------------------------------------------- the verdict
def audit_position(pos, execs, signals, series_of):
    fs = fill_summary(fills(execs, pos.get("position_id")))
    sid = str(pos.get("stock_id") or "")
    row = {"position_id": pos.get("position_id"), "sid": sid,
           "name": pos.get("stock_name"), "status": pos.get("status")}
    if fs is None:
        row.update(verdict="no_fills")
        return row
    row.update(fs)
    series = series_of(sid)
    if series.empty:
        row.update(verdict="no_prices")
        return row
    sg = match_signal(signals, sid, fs["first_buy_date"], series)
    row["signal_date"] = sg["sig"] if sg else None
    row["bucket"] = sg["bucket"] if sg else "none"
    row["bucket_label"] = BUCKET_LABEL[row["bucket"]]

    rule = rule_from_fill(series, fs["first_buy_date"], fs["first_buy_price"])
    flags = []
    if row["bucket"] != "tradable":
        flags.append("no_signal")
    if rule is None or rule.get("entry") is None:
        row.update(verdict="no_bars_after_fill", flags=flags)
        return row
    row["rule_exit"] = rule["reason"] or ("riding" if rule.get("riding") else "open")
    row["rule_exit_date"] = rule.get("exit_date")
    row["rule_exit_price"] = None if rule.get("exit_price") is None else round(float(rule["exit_price"]), 2)
    row["rule_ret_net"] = None if rule.get("ret_pct") is None else round(net_pct(float(rule["ret_pct"])), 2)
    row["rule_stop_now"] = None if rule.get("stop") is None else round(float(rule["stop"]), 2)

    closed = fs["open_shares"] <= 1e-9 and fs["sell_shares"] > 0
    if closed:
        gross = (fs["avg_sell"] / fs["avg_cost"] - 1) * 100
        row["actual_ret_net"] = round(net_pct(gross), 2)
        row["held_sessions"] = sessions_between(series, fs["first_buy_date"], fs["last_sell_date"])
    else:
        last = float(series["close"].iloc[-1])
        row["actual_ret_net"] = None
        row["mark_ret_net"] = round(net_pct((last / fs["avg_cost"] - 1) * 100), 2)
        row["held_sessions"] = sessions_between(series, fs["first_buy_date"], str(series["date"].iloc[-1]))

    cap = DEFAULT_RULE["ride_cap"]
    if row["held_sessions"] is not None and row["held_sessions"] > cap:
        flags.append("held_past_cap")
    if rule.get("exited"):
        rd, rp = rule["exit_date"], float(rule["exit_price"])
        if closed:
            if fs["last_sell_date"] > rd and rule["reason"] in ("stop", "lock") and fs["avg_sell"] < rp:
                flags.append("stop_not_taken")
            elif fs["last_sell_date"] < rd and row["actual_ret_net"] < row["rule_ret_net"]:
                flags.append("sold_early")
        elif rule["reason"] in ("stop", "lock") and float(series["close"].iloc[-1]) < rp:
            flags.append("stop_not_taken")
    if closed and row.get("rule_ret_net") is not None:
        row["leak_pct"] = round(row["actual_ret_net"] - row["rule_ret_net"], 2)
    row["flags"] = flags
    row["verdict"] = "ok" if not flags else ",".join(flags)
    return row


def audit(backup_path, since="2026-06-25", ledger_file=SIGNAL_LEDGER_FILE,
          price_file=PRICE_VOLUME_FILE, taiex_file=TAIEX_FILE,
          research_file=RESEARCH_PRICES):
    positions, executions = load_backup(backup_path)
    signals, _ = classify_signals(since, ledger_file, price_file, taiex_file)
    cache = {}

    def series_of(sid):
        if sid not in cache:
            cache[sid] = price_series(sid, price_file, research_file)
        return cache[sid]

    rows = [audit_position(p, executions, signals, series_of)
            for p in positions if p.get("status") != "void"]
    return rows, summary(rows)


def summary(rows):
    done = [r for r in rows if r.get("actual_ret_net") is not None]
    out = {"positions": len(rows), "closed": len(done),
           "by_bucket": {}, "flags": {}}
    for r in rows:
        b = r.get("bucket_label") or r.get("verdict")
        out["by_bucket"][b] = out["by_bucket"].get(b, 0) + 1
        for f in r.get("flags") or []:
            out["flags"][f] = out["flags"].get(f, 0) + 1
    if done:
        a = [r["actual_ret_net"] for r in done]
        out["actual_mean_pct"] = round(sum(a) / len(a), 2)
        out["actual_win_pct"] = round(100 * sum(1 for x in a if x > 0) / len(a), 1)
        rr = [r for r in done if r.get("rule_ret_net") is not None]
        if rr:
            out["rule_mean_pct_same_trades"] = round(
                sum(r["rule_ret_net"] for r in rr) / len(rr), 2)
            out["leak_sum_pct"] = round(sum(r["leak_pct"] for r in rr), 1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("backup")
    ap.add_argument("--since", default="2026-06-25")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    rows, sm = audit(a.backup, since=a.since)
    cols = ["sid", "name", "first_buy_date", "first_buy_price", "bucket_label",
            "last_sell_date", "actual_ret_net", "mark_ret_net", "rule_exit",
            "rule_exit_date", "rule_ret_net", "leak_pct", "held_sessions", "verdict"]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    pd.set_option("display.width", 220)
    print(df[cols].to_string(index=False))
    print()
    print(json.dumps(sm, ensure_ascii=False, indent=1))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "summary": sm}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
