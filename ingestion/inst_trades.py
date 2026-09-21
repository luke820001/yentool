"""
Daily institutional net buy/sell (three major institutions) -- free, whole
market, same-day after close. TWSE T86 (TSE) + TPEX (OTC). Values are stored
in lots (shares / 1000). Daily snapshots accumulate in inst_trades.db so a
rolling N-day net and a buy/sell streak can be derived. All strings ASCII.

2026-09-21 (F16). The table used to be refreshed by ONE rule: "if the newest
date in the whole table is today, do nothing". TPEX could only be asked for
its latest day, so when one board published late or a run was skipped, that
board's day was lost for good, and "Foreign_Net_5D" silently became "the last
five rows we happen to have". Now each board is checked on its own against
the trading calendar and every missing recent session is fetched BY DATE
(ingestion.inst_history), so the 5-day windows and the streaks below are
computed on complete, consecutive sessions -- or say which date they stop at.
"""
import sqlite3
from datetime import datetime, date as _date, timedelta

import pandas as pd

from config.settings import DATA_DIR, PRICE_VOLUME_FILE
from ingestion.inst_history import (
    FETCHERS, fetch_tpex_inst_date, fetch_twse_inst_date, parse_tpex_daily,
    parse_twse_t86,
)

INST_DB = DATA_DIR / "inst_trades.db"
# Keep about 400 sessions (~19 months). The live features only need the last
# five, but a longer tail is what lets the chip rules be re-validated on live
# data later without another backfill.
KEEP_DAYS = 600
_EOD_HOUR = 15    # institutional report publishes after ~15:00
# How many recent sessions each board is checked for and back-filled.
FILL_SESSIONS = 12
BOARDS = ("TSE", "OTC")
_VALUE_COLS = ["Foreign_Net", "Trust_Net", "Dealer_Net", "Inst_Net"]


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


# Backwards-compatible names (tools/audit scripts import these).
def fetch_twse_inst(ymd: str) -> pd.DataFrame:
    """TWSE T86 for one date 'YYYYMMDD' (legacy signature)."""
    iso = "{}-{}-{}".format(ymd[:4], ymd[4:6], ymd[6:8])
    df = fetch_twse_inst_date(iso)
    return pd.DataFrame() if df is None else df


def fetch_tpex_inst() -> pd.DataFrame:
    """TPEX openapi latest day (legacy). Kept as the quick first attempt for
    the OTC board: one request, and it tells us which date TPEX has published."""
    import requests
    url = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                       "Accept": "application/json,*/*"}, timeout=30)
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

    def num(x):
        try:
            return float(str(x).replace(",", "").strip())
        except Exception:
            return 0.0

    fc = col("Foreign Investors include Mainland", "Dealers excluded", "Difference")
    tc = col("SecuritiesInvestmentTrustCompanies", "Difference")
    # 'Dealers-Difference' is the dealer TOTAL; 'ForeignDealers-Difference'
    # also contains both words, so match on the prefix.
    dc = next((c for c in df.columns
               if c.strip().startswith("Dealers") and "Difference" in c), None)
    tot = col("TotalDifference")
    code = df.columns[1]
    raw_date = str(df[df.columns[0]].iloc[0]).strip()
    if len(raw_date) == 7 and raw_date.isdigit():        # ROC 'YYYMMDD'
        iso = "{:04d}-{}-{}".format(int(raw_date[:3]) + 1911, raw_date[3:5], raw_date[5:7])
    elif len(raw_date) == 8 and raw_date.isdigit():      # 'YYYYMMDD'
        iso = "{}-{}-{}".format(raw_date[:4], raw_date[4:6], raw_date[6:8])
    else:
        iso = raw_date
    out = pd.DataFrame({
        "stock_id": df[code].astype(str).str.strip(),
        "Foreign_Net": df[fc].map(num) / 1000 if fc else 0.0,
        "Trust_Net": df[tc].map(num) / 1000 if tc else 0.0,
        "Dealer_Net": df[dc].map(num) / 1000 if dc else 0.0,
        "Inst_Net": df[tot].map(num) / 1000 if tot else 0.0,
        "date": iso,
    })
    out = out[out["stock_id"].map(lambda s: s.isdigit() and len(s) in (4, 5))]
    for c in _VALUE_COLS:
        out[c] = out[c].round(1)
    return out.reset_index(drop=True)


def _read_existing() -> pd.DataFrame:
    if not INST_DB.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(INST_DB) as conn:
            df = pd.read_sql_query("SELECT * FROM data", conn)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["date"] = df["date"].astype(str).str[:10]
    df["stock_id"] = df["stock_id"].astype(str)
    for c in _VALUE_COLS:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    if "board" not in df.columns:
        df["board"] = None
    return df


def _write(df: pd.DataFrame):
    INST_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(INST_DB) as conn:
        df.to_sql("data", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inst ON data(stock_id, date)")


def _recent_sessions(n: int, upto: str) -> list:
    """The last `n` trading sessions on or before `upto`, from the price
    store's calendar (a date most of the market traded). Falls back to
    weekdays when the store is unavailable, in which case a holiday costs one
    empty request and nothing else."""
    try:
        conn = sqlite3.connect(PRICE_VOLUME_FILE)
        try:
            rows = conn.execute(
                "SELECT date, COUNT(*) FROM data WHERE date <= ? GROUP BY date "
                "ORDER BY date DESC LIMIT 60", (upto,)).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []
    dates = sorted(str(d)[:10] for d, c in rows if c and c >= 100)
    # The price store can lag the institutional feed (a desktop that has not
    # scanned for a week; the cloud before its own price fetch). Weekdays
    # between the store's last session and `upto` are asked for as well; a
    # holiday among them costs one empty request and stores nothing.
    last = dates[-1] if dates else None
    extra, d = [], datetime.strptime(upto, "%Y-%m-%d").date()
    while len(extra) + len(dates) < n or (last and d.strftime("%Y-%m-%d") > last):
        iso = d.strftime("%Y-%m-%d")
        if last and iso <= last:
            break
        if d.weekday() < 5:
            extra.append(iso)
        d -= timedelta(days=1)
        if not last and len(extra) >= n:
            break
    return sorted(set(dates) | set(extra))[-n:]


def _dates_present(existing: pd.DataFrame, board: str) -> set:
    """Dates this board is already stored for. Rows written before the board
    tag existed count for both boards on any day with a whole-market row
    count (the old path always stored TSE and OTC together)."""
    if existing.empty:
        return set()
    tagged = existing[existing["board"] == board]
    have = set(tagged["date"])
    legacy = existing[existing["board"].isna()]
    if not legacy.empty:
        cnt = legacy.groupby("date")["stock_id"].size()
        have |= set(cnt[cnt >= 200].index)
    return have


def update_inst_trades(target: str = None, log=print) -> str:
    """Bring both boards up to date for the last FILL_SESSIONS sessions and
    return the newest stored date. Each board is checked separately; a day
    one board already has is never re-fetched."""
    existing = _read_existing()
    target = target or _latest_trading_day()
    sessions = _recent_sessions(FILL_SESSIONS, target)
    if target not in sessions:
        sessions.append(target)      # the calendar may not know today yet

    frames = [existing] if not existing.empty else []
    fetched = {}
    for board in BOARDS:
        have = _dates_present(existing, board)
        missing = [s for s in sessions if s not in have]
        if not missing:
            continue
        got = []
        # OTC: the openapi "latest" is one cheap request and settles which
        # date TPEX has published; anything else missing is asked for by date.
        if board == "OTC" and target in missing:
            snap = fetch_tpex_inst()
            if not snap.empty:
                d = str(snap["date"].iloc[0])[:10]
                if d in missing:
                    snap["board"] = board
                    frames.append(snap)
                    got.append(d)
                    missing = [m for m in missing if m != d]
        for iso in missing:
            if iso > target:
                continue
            df = FETCHERS[board](iso)
            if df is None:
                log("  [inst] {} {}: fetch failed".format(board, iso))
                continue
            if df.empty:            # not a session for this board, or not yet published
                continue
            df = df.copy()
            df["board"] = board
            frames.append(df)
            got.append(iso)
        fetched[board] = got

    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    combined["date"] = combined["date"].astype(str).str[:10]
    combined["stock_id"] = combined["stock_id"].astype(str)
    for c in _VALUE_COLS:
        combined[c] = pd.to_numeric(combined[c], errors="coerce").fillna(0.0)
    # keep="last": a by-date fetch for a day we already had (legacy rows)
    # replaces it, so a corrected exchange table wins over a stale snapshot.
    combined = combined.drop_duplicates(subset=["date", "stock_id"], keep="last")
    cutoff = (_date.today() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    combined = combined[combined["date"] >= cutoff]
    if any(fetched.values()):
        _write(combined)
        log("  [inst] fetched {}".format(
            ", ".join("{} {}".format(b, ",".join(d)) for b, d in fetched.items() if d)))
    return str(combined["date"].max())[:10] if not combined.empty else None


def backfill_twse_inst(days: int = 120, sleep: float = 0.25) -> int:
    """One-shot: fetch TWSE T86 for each weekday in the last `days` calendar
    days that is not stored yet. Returns days fetched. (OTC history is
    handled by tools/backfill_inst_history.py.)"""
    import time
    existing = _read_existing()
    have = set(existing["date"]) if not existing.empty else set()
    frames = [existing] if not existing.empty else []
    d = _date.today()
    fetched = 0
    for _ in range(days):
        if d.weekday() < 5:
            iso = d.strftime("%Y-%m-%d")
            if iso not in have:
                df = fetch_twse_inst_date(iso)
                if df is not None and not df.empty:
                    df = df.copy()
                    df["board"] = "TSE"
                    frames.append(df)
                    fetched += 1
                time.sleep(sleep)
        d -= timedelta(days=1)
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        combined["date"] = combined["date"].astype(str).str[:10]
        combined = combined.drop_duplicates(subset=["date", "stock_id"], keep="last")
        _write(combined)
    return fetched


def _streak(values):
    """Consecutive trailing sessions with the same sign as the last one:
    +n for n net-buy days, -n for n net-sell days, 0 when the last day is flat."""
    if not len(values):
        return 0
    last = values[-1]
    if last == 0:
        return 0
    sign = 1 if last > 0 else -1
    n = 0
    for v in reversed(values):
        if v * sign > 0:
            n += 1
        else:
            break
    return n * sign


def get_inst_features(stock_ids, days: int = 5, existing: pd.DataFrame = None) -> dict:
    """Per stock: the latest session's net buys and the rolling window.

    Returns {stock_id: {Foreign_Net, Trust_Net, Dealer_Net, Inst_Net,
    Inst_Date, Foreign_Net_5D, Trust_Net_5D, Inst_Net_5D, Inst_Buy_Days,
    Inst_Streak, Inst_Sessions}}.

    The window is the last `days` SESSIONS the table holds for the stock's
    board -- a stock absent from a day's exchange table had no institutional
    trade that day and counts as 0, it does not shorten the window.
    Inst_Sessions says how many of those sessions were actually available, so
    a caller can tell a real 5-day figure from one built on 3 days.
    """
    out = {}
    df = existing if existing is not None else _read_existing()
    if df is None or df.empty:
        return out
    wanted = {str(s) for s in stock_ids}
    sub = df[df["stock_id"].isin(wanted)]
    if sub.empty:
        return out
    # Session list per board (legacy untagged rows count for both boards).
    sessions = {}
    for board in BOARDS:
        sessions[board] = sorted(_dates_present(df, board))
    all_dates = sorted(set(df["date"]))

    for sid, g in sub.groupby("stock_id"):
        g = g.sort_values("date")
        board = g["board"].dropna().iloc[-1] if g["board"].notna().any() else None
        cal = sessions.get(board) or all_dates
        cal = [d for d in cal if d <= g["date"].max()] or list(g["date"])
        window = cal[-days:]
        by_date = g.drop_duplicates("date", keep="last").set_index("date")
        w = by_date.reindex(window)[_VALUE_COLS].fillna(0.0)
        latest = w.iloc[-1]
        inst = list(w["Inst_Net"].astype(float))
        out[str(sid)] = {
            "Foreign_Net": round(float(latest["Foreign_Net"]), 1),
            "Trust_Net": round(float(latest["Trust_Net"]), 1),
            "Dealer_Net": round(float(latest["Dealer_Net"]), 1),
            "Inst_Net": round(float(latest["Inst_Net"]), 1),
            "Inst_Date": window[-1],
            "Foreign_Net_5D": round(float(w["Foreign_Net"].sum()), 1),
            "Trust_Net_5D": round(float(w["Trust_Net"].sum()), 1),
            "Inst_Net_5D": round(float(w["Inst_Net"].sum()), 1),
            "Inst_Buy_Days": int((w["Foreign_Net"] > 0).sum()),
            "Inst_Streak": int(_streak(list(by_date.reindex(cal[-20:])["Inst_Net"]
                                            .fillna(0.0).astype(float)))),
            "Inst_Sessions": int(by_date.index.isin(window).sum()),
        }
    return out
