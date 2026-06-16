"""
Multi-source price/volume fetcher.
Priority per stock: yfinance -> TWSE/TPEX official API -> FinMind (existing).
All Python strings are ASCII; no Chinese characters in this file.
"""
import logging
import time
import warnings
from datetime import date, timedelta

import requests
import pandas as pd
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# yfinance logs "possibly delisted / 404" to console when a probed ticker has no
# data (e.g. trying the .TW suffix on an OTC stock before falling back to .TWO).
# That probe is expected and harmless, so silence the noise.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

from ingestion.price_volume import PriceVolumeFetcher
from storage.data_store import upsert_and_trim, bulk_upsert_stocks
from config.settings import PRICE_VOLUME_FILE, ROLLING_DAYS

_FINMIND = PriceVolumeFetcher()

TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_STOCK_DAY_URL = (
    "https://www.tpex.org.tw/web/stock/aftertrading/"
    "daily_trading_info/st43_result.php"
)
REQUEST_TIMEOUT = 15
INTER_MONTH_SLEEP = 0.4   # seconds between consecutive month requests


# ── date helpers ──────────────────────────────────────────────────────────────

def _roc_to_iso(roc_str):
    """'113/06/03'  ->  '2024-06-03'  (ROC year + 1911 = Gregorian)"""
    parts = str(roc_str).strip().split("/")
    if len(parts) != 3:
        return None
    try:
        return "{:04d}-{:02d}-{:02d}".format(
            int(parts[0]) + 1911, int(parts[1]), int(parts[2])
        )
    except ValueError:
        return None


def _clean(s):
    return str(s).replace(",", "").strip()


def _months_for(lookback_days):
    """Return sorted list of (year, month) tuples covering lookback_days from today."""
    today = date.today()
    seen = set()
    for i in range(lookback_days + 35):
        d = today - timedelta(days=i)
        seen.add((d.year, d.month))
    return sorted(seen)


# ── TWSE official API (TSE stocks) ───────────────────────────────────────────

def _twse_one_month(stock_id, year, month):
    """Fetch one calendar month from TWSE individual-stock history endpoint."""
    try:
        resp = requests.get(
            TWSE_STOCK_DAY_URL,
            params={
                "response": "json",
                "date": "{:04d}{:02d}01".format(year, month),
                "stockNo": stock_id,
            },
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return pd.DataFrame()

    if payload.get("stat") != "OK" or not payload.get("data"):
        return pd.DataFrame()

    # fields: date, vol_shares, amount, open, high, low, close, change, txns
    rows = []
    for rec in payload["data"]:
        if len(rec) < 7:
            continue
        dt = _roc_to_iso(rec[0])
        if dt is None:
            continue
        try:
            rows.append({
                "date":         dt,
                "open":         float(_clean(rec[3])),
                "high":         float(_clean(rec[4])),
                "low":          float(_clean(rec[5])),
                "close":        float(_clean(rec[6])),
                "volume_share": float(_clean(rec[1])),
            })
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows)


def fetch_twse(stock_id, lookback_days=90):
    """Fetch TSE historical OHLCV via TWSE official API."""
    frames = []
    for year, month in _months_for(lookback_days):
        df = _twse_one_month(stock_id, year, month)
        if not df.empty:
            frames.append(df)
        time.sleep(INTER_MONTH_SLEEP)

    if not frames:
        return pd.DataFrame()

    out = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    out["stock_id"] = stock_id
    cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return out[out["date"] >= cutoff].reset_index(drop=True)


# ── TPEX official API (OTC stocks) ───────────────────────────────────────────

def _tpex_one_month(stock_id, year, month):
    """Fetch one calendar month from TPEX individual-stock history endpoint."""
    roc_year = year - 1911
    try:
        resp = requests.get(
            TPEX_STOCK_DAY_URL,
            params={
                "l":     "zh-tw",
                "d":     "{}/{:02d}".format(roc_year, month),
                "stkno": stock_id,
                "o":     "json",
            },
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return pd.DataFrame()

    # TPEX returns DataTables format: aaData or data key
    data = payload.get("aaData") or payload.get("data", [])
    if not data:
        return pd.DataFrame()

    # fields: date, vol_shares, amount, open, high, low, close, change, txns
    rows = []
    for rec in data:
        if len(rec) < 7:
            continue
        dt = _roc_to_iso(rec[0])
        if dt is None:
            continue
        try:
            rows.append({
                "date":         dt,
                "open":         float(_clean(rec[3])),
                "high":         float(_clean(rec[4])),
                "low":          float(_clean(rec[5])),
                "close":        float(_clean(rec[6])),
                "volume_share": float(_clean(rec[1])),
            })
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows)


def fetch_tpex(stock_id, lookback_days=90):
    """Fetch OTC historical OHLCV via TPEX official API."""
    frames = []
    for year, month in _months_for(lookback_days):
        df = _tpex_one_month(stock_id, year, month)
        if not df.empty:
            frames.append(df)
        time.sleep(INTER_MONTH_SLEEP)

    if not frames:
        return pd.DataFrame()

    out = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )
    out["stock_id"] = stock_id
    cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return out[out["date"] >= cutoff].reset_index(drop=True)


# ── yfinance ─────────────────────────────────────────────────────────────────

INCREMENTAL_DAYS = 7   # days to fetch when topping up existing history


def fetch_yfinance(stock_id, market="TSE", lookback_days=90):
    """Fetch OHLCV via yfinance (requires: pip install yfinance)."""
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()

    suffix = ".TWO" if market == "OTC" else ".TW"
    ticker_str = stock_id + suffix
    period = "{}d".format(lookback_days + 15)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = yf.download(
                ticker_str, period=period,
                auto_adjust=True, progress=False,
            )
    except Exception:
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy().reset_index()

    # flatten MultiIndex columns that newer yfinance versions may produce
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]

    # normalise date column (may be named 'date' or 'datetime', may carry tz)
    for dc in ("date", "datetime"):
        if dc in df.columns:
            dt_s = pd.to_datetime(df[dc])
            if dt_s.dt.tz is not None:
                dt_s = dt_s.dt.tz_convert(None)
            df["date"] = dt_s.dt.strftime("%Y-%m-%d")
            break
    else:
        return pd.DataFrame()

    df = df.rename(columns={"volume": "volume_share"})
    df["stock_id"] = stock_id

    required = ["date", "stock_id", "open", "high", "low", "close", "volume_share"]
    if any(c not in df.columns for c in required):
        return pd.DataFrame()

    return df[required].dropna().reset_index(drop=True)


# ── yfinance batch (one threaded request for many tickers) ───────────────────

YF_BATCH_SIZE = 50   # tickers per yfinance.download call


def _normalize_yf_single(sub, stock_id):
    """Normalise one ticker's slice of a yfinance frame to the storage schema."""
    sub = sub.reset_index()
    sub.columns = [str(c).lower() for c in sub.columns]
    if "date" not in sub.columns:
        if "datetime" in sub.columns:
            sub = sub.rename(columns={"datetime": "date"})
        else:
            return pd.DataFrame()
    dt_s = pd.to_datetime(sub["date"], errors="coerce")
    if getattr(dt_s.dt, "tz", None) is not None:
        dt_s = dt_s.dt.tz_convert(None)
    sub["date"] = dt_s.dt.strftime("%Y-%m-%d")
    sub = sub.rename(columns={"volume": "volume_share"})
    sub["stock_id"] = stock_id
    required = ["date", "stock_id", "open", "high", "low", "close", "volume_share"]
    if any(c not in sub.columns for c in required):
        return pd.DataFrame()
    return sub[required].dropna().reset_index(drop=True)


def fetch_yfinance_batch(ticker_map, lookback_days=ROLLING_DAYS):
    """
    Download OHLCV for many stocks in a single yfinance call (internally threaded).

    ticker_map : {stock_id: market}
    Returns {stock_id: normalised df} only for stocks yfinance actually returned.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}
    if not ticker_map:
        return {}

    yf_to_sid = {}
    for sid, market in ticker_map.items():
        suffix = ".TWO" if market == "OTC" else ".TW"
        yf_to_sid[sid + suffix] = sid
    tickers = list(yf_to_sid.keys())
    period = "{}d".format(lookback_days + 15)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = yf.download(
                tickers, period=period, auto_adjust=True,
                progress=False, group_by="ticker", threads=True,
            )
    except Exception:
        return {}

    if raw is None or raw.empty:
        return {}

    single = not isinstance(raw.columns, pd.MultiIndex)
    out = {}
    for yft, sid in yf_to_sid.items():
        try:
            if single:
                sub = raw.copy()
            else:
                if yft not in raw.columns.get_level_values(0):
                    continue
                sub = raw[yft].copy()
            norm = _normalize_yf_single(sub, sid)
            if not norm.empty:
                out[sid] = norm
        except Exception:
            continue
    return out


def resolve_market(stock_id):
    """Probe yfinance to decide TSE (.TW) vs OTC (.TWO). Defaults to TSE.
    Used by the manual single-stock lookup, which has no market snapshot."""
    for mkt in ("TSE", "OTC"):
        try:
            df = fetch_yfinance(stock_id, market=mkt, lookback_days=10)
            if df is not None and not df.empty:
                return mkt
        except Exception:
            pass
    return "TSE"


def multi_fetch_and_save_batch(stale_ids, id_to_market):
    """
    Fast path: batch-fetch stale stocks via chunked yfinance calls and persist
    them all in a single SQLite transaction.

    Returns the set of stock_ids successfully fetched+saved. Stocks yfinance
    could not return are absent; the caller falls back per stock (TWSE/TPEX).
    """
    if not stale_ids:
        return set()

    frames = {}
    for i in range(0, len(stale_ids), YF_BATCH_SIZE):
        chunk = stale_ids[i:i + YF_BATCH_SIZE]
        tmap = {sid: id_to_market.get(sid, "TSE") for sid in chunk}
        got = fetch_yfinance_batch(tmap, lookback_days=ROLLING_DAYS)
        for sid, df in got.items():
            frames[sid] = _add_derived(df)

    if frames:
        bulk_upsert_stocks(
            PRICE_VOLUME_FILE, frames,
            date_col="date", key_cols=["date", "stock_id"],
        )
    return set(frames.keys())


# ── derived columns (mirrors PriceVolumeFetcher._transform) ──────────────────

def _add_derived(df):
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume_share"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Volume_Lot"]    = (df["volume_share"] / 1000).round(0).astype("Int64")
    df = df.sort_values("date").reset_index(drop=True)
    df["MA5_Volume"]    = df["Volume_Lot"].rolling(5,  min_periods=1).mean().round(0)
    df["Min_Volume_20"] = df["Volume_Lot"].rolling(20, min_periods=1).min()
    df["Max_Price_20"]  = df["close"].rolling(20, min_periods=1).max()
    df["Min_Price_20"]  = df["close"].rolling(20, min_periods=1).min()
    return df


# ── public entry point ────────────────────────────────────────────────────────

def multi_fetch_and_save(stock_id, market="TSE", incremental=False, skip_yfinance=False):
    """
    Fetch price/volume data from the best available source and persist to Excel.

    Parameters
    ----------
    incremental : bool
        True  -> stock already has history; fetch only the last INCREMENTAL_DAYS
                 to top up missing recent bars (saves API quota).
        False -> no history yet; fetch full ROLLING_DAYS window.

    Source priority
    ---------------
    1. yfinance          (free, fast; requires `pip install yfinance`)
    2. TWSE API (TSE) or TPEX API (OTC)   (official, free, no token)
    3. FinMind           (original source; may hit 402 on free tier)
    """
    window = INCREMENTAL_DAYS if incremental else ROLLING_DAYS
    label  = "incr" if incremental else "full"

    df = pd.DataFrame()
    source = "none"

    # 1. yfinance (skipped on the batch-fallback path, where yfinance already
    #    failed to return this ticker in the bulk call)
    if not skip_yfinance:
        df = fetch_yfinance(stock_id, market=market, lookback_days=window)
        if not df.empty:
            source = "yfinance"

    # 2. official exchange API
    if df.empty:
        if market == "OTC":
            df = fetch_tpex(stock_id, lookback_days=window)
            if not df.empty:
                source = "TPEX"
        else:
            df = fetch_twse(stock_id, lookback_days=window)
            if not df.empty:
                source = "TWSE"

    # 3. FinMind fallback
    if df.empty:
        try:
            df = _FINMIND.fetch(stock_id)
            if not df.empty:
                source = "FinMind"
        except Exception as e:
            print("  [{}] pv all sources failed: {}".format(stock_id, e))
            return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    print("  [{}] pv {} {}".format(stock_id, label, source))
    df = _add_derived(df)
    return upsert_and_trim(
        file_path=PRICE_VOLUME_FILE,
        sheet_name=stock_id,
        new_df=df,
        date_col="date",
        key_cols=["date", "stock_id"],
    )
