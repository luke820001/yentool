"""
Compact per-stock data for the WHOLE market, for advice on anything you hold.
ASCII only.

Owner, 2026-09-21: "even for a stock the system never recommended, help me
with advice on what I bought", and "the scanner should really be scanning all
of the market's data".

Both are now possible. `scanner/market_snapshot.py` keeps every listed
instrument's daily bar current from one request per exchange, so this module
can compute the handful of numbers a holding card needs -- moving averages,
the 20-day range, support, the institutional flow -- for all of them, not just
the shortlist the scan analysed in depth.

What this is NOT: the full 118-column analysis. Those columns exist to SELECT
stocks, and a name the scanner did not select does not need them. This is the
smaller set that answers "where is this stock, relative to the levels of the
position I hold in it".

The positions themselves never leave the phone. The scanner publishes the
superset; the device matches its own holdings against it.
"""
import json
import sqlite3
from datetime import datetime

import pandas as pd

# A stock appears as soon as there is anything worth saying about it, and each
# average is null until it can honestly be computed. Requiring 60 bars up front
# meant a newly covered instrument -- every ETF, on the day whole-market
# storage began -- published nothing at all for three months, when its close
# and its 5-day mean were available immediately.
MIN_BARS = 1
RECENT_BARS = 70       # how much history to read per stock


def _load(price_db, min_bars=MIN_BARS, limit_bars=RECENT_BARS):
    """The last `limit_bars` bars of every stock with at least `min_bars`."""
    conn = sqlite3.connect(str(price_db), timeout=60)
    try:
        df = pd.read_sql_query(
            "SELECT stock_id, date, open, high, low, close, Volume_Lot "
            "FROM data WHERE date >= (SELECT MIN(date) FROM (SELECT DISTINCT "
            "date FROM data ORDER BY date DESC LIMIT ?))", conn,
            params=(limit_bars,))
    finally:
        conn.close()
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "Volume_Lot"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = df["date"].astype(str).str[:10]
    df["stock_id"] = df["stock_id"].astype(str)
    try:
        from scanner.data_integrity import nonsession_dates
        counts = df.groupby("date")["stock_id"].size()
        skip = set(nonsession_dates(list(counts.items())))
        if skip:
            df = df[~df["date"].isin(skip)]
    except Exception:
        pass
    return df.sort_values(["stock_id", "date"])


def build(price_db, names=None, inst=None, min_bars=MIN_BARS):
    """Compact records keyed by stock id."""
    df = _load(price_db, min_bars=min_bars)
    if df.empty:
        return {}
    names = names or {}
    inst = inst or {}
    out = {}
    for sid, g in df.groupby("stock_id", sort=False):
        c = g["close"].dropna()
        if len(c) < min_bars:
            continue
        # Below this there is a price and a date and nothing else; the record
        # says so via "Bars" and every average stays null.
        g = g.reset_index(drop=True)
        close = float(c.iloc[-1])
        if not close > 0:
            continue
        nm = names.get(sid)
        name = (nm[0] if isinstance(nm, list) and nm else
                (nm if isinstance(nm, str) else sid))
        market = (nm[1] if isinstance(nm, list) and len(nm) > 1 else "")

        def ma(n):
            if len(c) < n:
                return None
            return round(float(c.rolling(n).mean().iloc[-1]), 2)

        hi20 = g["high"].tail(20).max()
        lo20 = g["low"].tail(20).min()
        rng = ((g["high"] - g["low"]) / g["close"]).tail(20).mean()
        rec = {
            "Bars": len(c),        # so the phone can say what it is working from
            "Stock_ID": sid,
            "Stock_Name": name,
            "Market": market,
            "Data_Date": str(g["date"].iloc[-1]),
            "Close_Price": round(close, 2),
            "High_Today": round(float(g["high"].iloc[-1]), 2),
            "Low_Today": round(float(g["low"].iloc[-1]), 2),
            "MA5": ma(5), "MA10": ma(10), "MA20": ma(20), "MA60": ma(60),
            "High_20": round(float(hi20), 2) if hi20 == hi20 else None,
            "Support_20L": round(float(lo20), 2) if lo20 == lo20 else None,
            "ATR_Pct": round(float(rng * 100), 2) if rng == rng else None,
            "Vol_MA20": round(float(g["Volume_Lot"].tail(20).mean()), 1),
            "Vol_Today": round(float(g["Volume_Lot"].iloc[-1]), 1),
        }
        if len(c) >= 2:
            rec["Close_Prev"] = round(float(c.iloc[-2]), 2)
        if len(c) >= 6:
            rec["Ret_5D_Pct"] = round((close / float(c.iloc[-6]) - 1) * 100, 2)
        if len(c) >= 64:
            rec["Gain_3M_Pct"] = round((close / float(c.iloc[-64]) - 1) * 100, 1)
        f = inst.get(sid)
        if f:
            for k in ("Foreign_Net", "Trust_Net", "Dealer_Net", "Inst_Net",
                      "Inst_Net_5D", "Foreign_Net_5D", "Inst_Streak",
                      "Inst_Sessions", "Inst_Date"):
                rec[k] = f.get(k)
            try:
                from scanner.chip_signal import inst_pct, chip_basis
                rec["Inst_Pct"] = inst_pct(f.get("Inst_Net"), rec["Vol_MA20"])
                rec["Chip_Basis"] = chip_basis(f.get("Inst_Date"),
                                               rec["Data_Date"])
            except Exception:
                pass
        out[sid] = rec
    return out


def export(path, price_db, names=None, inst=None, session_date="",
           scan_mode="", log=print):
    """Write universe.json. Returns the number of stocks written."""
    stocks = build(price_db, names=names, inst=inst)
    if not stocks:
        log("  [universe] nothing to write")
        return 0
    payload = {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_date": str(session_date or "")[:10],
        "mode": scan_mode,
        "count": len(stocks),
        "note": "every listed instrument with enough history, not only the "
                "scanned shortlist; each record carries its OWN Data_Date "
                "because a stock outside the daily analysis can be a session "
                "or two behind",
        "stocks": stocks,
    }
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return len(stocks)
