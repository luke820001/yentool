"""
Daily institutional net buy/sell (three major institutions) -- free, whole
market, same-day after close. TWSE T86 (TSE) + TPEX openapi (OTC). Values are
stored in lots (shares / 1000). Daily snapshots accumulate in inst_trades.db so
a rolling N-day net can be derived. All strings ASCII.
"""
import sqlite3
from datetime import datetime, date as _date, timedelta

import requests
import urllib3
import pandas as pd

from config.settings import DATA_DIR

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

INST_DB = DATA_DIR / "inst_trades.db"
_H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALL&response=json"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
KEEP_DAYS = 220   # ~10 months, enough to backtest the flow signal
_EOD_HOUR = 15    # institutional report publishes after ~15:00


def _is_stock(sid: str) -> bool:
    return sid.isdigit() and len(sid) in (4, 5)   # drop 6-digit warrants


def _latest_trading_day() -> str:
    now = datetime.now()
    d = now.date()
    if now.hour < _EOD_HOUR:
        d = d - timedelta(days=1)
    wd = d.weekday()
    if wd == 5:
        d = d - timedelta(days=1)
    elif wd == 6:
        d = d - timedelta(days=2)
    return d.strftime("%Y-%m-%d")


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return 0.0


def fetch_twse_inst(ymd: str) -> pd.DataFrame:
    """TWSE T86 for one date 'YYYYMMDD' -> {stock_id, Foreign_Net, Trust_Net, Inst_Net} in lots."""
    try:
        r = requests.get(T86_URL.format(ymd), headers=_H, timeout=20, verify=False)
        j = r.json()
    except Exception:
        return pd.DataFrame()
    if j.get("stat") != "OK" or not j.get("data"):
        return pd.DataFrame()
    fields = j["fields"]

    def idx(*kw):
        for i, f in enumerate(fields):
            if all(k in f for k in kw):
                return i
        return None

    i_for = idx("外陸資", "買賣超")
    i_tru = idx("投信", "買賣超")
    i_tot = idx("三大法人買賣超")
    def gv(rec, i):
        if i is None or i >= len(rec):
            return 0.0
        return _num(rec[i])

    rows = []
    for rec in j["data"]:
        sid = str(rec[0]).strip()
        if not _is_stock(sid):
            continue
        rows.append({
            "stock_id":    sid,
            "Foreign_Net": round(gv(rec, i_for) / 1000, 1),
            "Trust_Net":   round(gv(rec, i_tru) / 1000, 1),
            "Inst_Net":    round(gv(rec, i_tot) / 1000, 1),
        })
    return pd.DataFrame(rows)


def fetch_tpex_inst() -> pd.DataFrame:
    """TPEX openapi (latest available day) -> same schema, in lots, plus its date."""
    try:
        r = requests.get(TPEX_URL, headers=_H, timeout=30, verify=False)
        data = r.json()
    except Exception:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame()

    def col(*kw):
        for c in df.columns:
            if all(k in c for k in kw):
                return c
        return None

    fc = col("Foreign Investors include Mainland", "Dealers excluded", "Difference")
    tc = col("SecuritiesInvestmentTrustCompanies", "Difference")
    tot = col("TotalDifference")
    code = df.columns[1]
    raw_date = str(df[df.columns[0]].iloc[0]).strip()
    if len(raw_date) == 7 and raw_date.isdigit():        # ROC date 'YYYMMDD' (115=2026)
        iso = "{:04d}-{}-{}".format(int(raw_date[:3]) + 1911, raw_date[3:5], raw_date[5:7])
    elif len(raw_date) == 8 and raw_date.isdigit():      # Gregorian 'YYYYMMDD'
        iso = "{}-{}-{}".format(raw_date[:4], raw_date[4:6], raw_date[6:8])
    else:
        iso = raw_date

    out = pd.DataFrame({
        "stock_id":    df[code].astype(str).str.strip(),
        "Foreign_Net": df[fc].map(_num) / 1000 if fc else 0.0,
        "Trust_Net":   df[tc].map(_num) / 1000 if tc else 0.0,
        "Inst_Net":    df[tot].map(_num) / 1000 if tot else 0.0,
        "date":        iso,
    })
    out = out[out["stock_id"].map(_is_stock)].reset_index(drop=True)
    for c in ["Foreign_Net", "Trust_Net", "Inst_Net"]:
        out[c] = out[c].round(1)
    return out


def _read_existing() -> pd.DataFrame:
    if not INST_DB.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(INST_DB) as conn:
            return pd.read_sql_query("SELECT * FROM data", conn)
    except Exception:
        return pd.DataFrame()


def update_inst_trades() -> str:
    """Fetch the latest trading day for both boards (unless already stored) and
    append the daily snapshot. Returns the stored latest date."""
    existing = _read_existing()
    target = _latest_trading_day()
    have = None
    if not existing.empty and "date" in existing.columns:
        have = str(existing["date"].max())[:10]
    if have is not None and have >= target:
        return have   # already current

    # TSE: T86 needs an explicit date; probe back a few weekdays
    twse = pd.DataFrame()
    probe = datetime.strptime(target, "%Y-%m-%d").date()
    for _ in range(5):
        if probe.weekday() < 5:
            twse = fetch_twse_inst(probe.strftime("%Y%m%d"))
            if not twse.empty:
                twse["date"] = probe.strftime("%Y-%m-%d")
                break
        probe -= timedelta(days=1)

    tpex = fetch_tpex_inst()
    snap = pd.concat([d for d in [twse, tpex] if not d.empty], ignore_index=True)
    if snap.empty:
        return have
    new_date = str(snap["date"].max())[:10]

    combined = pd.concat([existing, snap], ignore_index=True) if not existing.empty else snap
    combined["date"] = combined["date"].astype(str).str[:10]
    combined = combined.drop_duplicates(subset=["date", "stock_id"], keep="last")
    cutoff = (_date.today() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    combined = combined[combined["date"] >= cutoff]

    INST_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(INST_DB) as conn:
        combined.to_sql("data", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inst ON data(stock_id, date)")
    return new_date


def backfill_twse_inst(days: int = 120, sleep: float = 0.25) -> int:
    """One-shot: fetch TWSE T86 for each trading day in the last `days` calendar
    days and store it, so the institutional-flow signal can be backtested.
    Non-trading days return empty and are skipped. Returns days fetched."""
    import time
    existing = _read_existing()
    have = set(existing["date"].astype(str).str[:10]) if not existing.empty else set()
    frames = [existing] if not existing.empty else []
    d = _date.today()
    fetched = 0
    for _ in range(days):
        if d.weekday() < 5:
            iso = d.strftime("%Y-%m-%d")
            if iso not in have:
                df = fetch_twse_inst(d.strftime("%Y%m%d"))
                if not df.empty:
                    df["date"] = iso
                    frames.append(df)
                    fetched += 1
                time.sleep(sleep)
        d -= timedelta(days=1)
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        combined["date"] = combined["date"].astype(str).str[:10]
        combined = combined.drop_duplicates(subset=["date", "stock_id"], keep="last")
        INST_DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(INST_DB) as conn:
            combined.to_sql("data", conn, if_exists="replace", index=False)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inst ON data(stock_id, date)")
    return fetched


def get_inst_features(stock_ids, days: int = 5) -> dict:
    """Return {stock_id: {Foreign_Net, Trust_Net, Foreign_Net_5D, Inst_Buy_Days}}.
    Latest day's net plus a rolling N-day foreign net and up-day count."""
    out = {}
    df = _read_existing()
    if df.empty:
        return out
    df = df[df["stock_id"].astype(str).isin([str(s) for s in stock_ids])].copy()
    if df.empty:
        return out
    df["date"] = df["date"].astype(str).str[:10]
    for c in ["Foreign_Net", "Trust_Net", "Inst_Net"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for sid, g in df.sort_values("date").groupby("stock_id"):
        g = g.tail(days)
        latest = g.iloc[-1]
        out[str(sid)] = {
            "Foreign_Net":    round(float(latest["Foreign_Net"]), 1),
            "Trust_Net":      round(float(latest["Trust_Net"]), 1),
            "Foreign_Net_5D": round(float(g["Foreign_Net"].sum()), 1),
            "Inst_Buy_Days":  int((g["Foreign_Net"] > 0).sum()),
        }
    return out
