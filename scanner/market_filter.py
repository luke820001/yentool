import json
import time
import warnings
import requests
import urllib3
import pandas as pd
from config.settings import PRICE_FILTER_MAX, VOLUME_TOP_N, PREFILTER_TOP_N, DATA_DIR

_NAME_CACHE_FILE = DATA_DIR / "stock_names.json"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
OTC_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

REQUEST_TIMEOUT = 20
RETRY_LIMIT = 3
RETRY_SLEEP = 5


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def _fetch_json(url: str, verify_ssl: bool = True) -> list:
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.get(
                url, headers=_HEADERS, timeout=REQUEST_TIMEOUT, verify=verify_ssl
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print("      -> attempt {}/{} failed for {}: {}".format(
                attempt, RETRY_LIMIT, url, e))
            if attempt < RETRY_LIMIT:
                time.sleep(RETRY_SLEEP)
    return []


def _normalize_tse(raw: list) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    rename_map = {
        "Code": "stock_id",
        "Name": "stock_name",
        "ClosingPrice": "close",
        "TradeVolume": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    required = ["stock_id", "stock_name", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print("      -> WARN TSE missing columns: {}".format(missing))
        return pd.DataFrame()
    df = df[required].copy()
    df["market"] = "TSE"
    return df


def _normalize_otc(raw: list) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    rename_map = {
        "SecuritiesCompanyCode": "stock_id",
        "CompanyName": "stock_name",
        "Close": "close",
        "TradingShares": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    required = ["stock_id", "stock_name", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print("      -> WARN OTC missing columns: {}".format(missing))
        return pd.DataFrame()
    df = df[required].copy()
    df["market"] = "OTC"
    return df


def _clean_numeric(df: pd.DataFrame) -> pd.DataFrame:
    for col in ["close", "volume"]:
        df[col] = (
            df[col]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.strip()
        )
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _is_valid_stock_id(sid: str) -> bool:
    if not isinstance(sid, str):
        return False
    if not sid.isdigit():
        return False
    # regular stocks: 4 digits (TSE) or 5 digits (OTC small caps)
    # 6-digit codes are warrants/derivatives (call/put warrants) -- exclude
    if not (4 <= len(sid) <= 5):
        return False
    # exclude ETFs / leveraged ETFs: codes starting with "00"
    if sid.startswith("00"):
        return False
    return True


# State-owned / government-controlled listed companies.
# Price action on these is driven by policy, not market supply/demand,
# so chip signals are misleading.
_GOVT_STOCKS = {
    "2412",   # Chunghwa Telecom    -- Ministry of Transportation majority
    "2002",   # China Steel         -- NDRC / government significant stake
    "2886",   # Mega Financial      -- Ministry of Finance ~22%
    "2892",   # First Financial     -- Ministry of Finance ~19%
    "5880",   # Taiwan Cooperative  -- Ministry of Finance ~24%
    "2834",   # Taiwan Business Bank -- Ministry of Finance majority
    "2801",   # Chang Hwa Bank      -- Ministry of Finance ~13%
    "2812",   # Taichung Bank       -- Taichung City Government
    "2836",   # Bank of Kaohsiung   -- Kaohsiung City Government
    "2889",   # Waterland Financial -- Ministry of Finance stake
    "1314",   # China Petrochemical  -- state-linked
}


def fetch_full_market() -> pd.DataFrame:
    print("  Fetching TSE market data ...")
    tse_raw = _fetch_json(TSE_URL)
    tse_df = _normalize_tse(tse_raw)
    print("  -> TSE raw rows: {}".format(len(tse_df)))

    print("  Fetching OTC market data ...")
    otc_raw = _fetch_json(OTC_URL, verify_ssl=False)
    otc_df = _normalize_otc(otc_raw)
    print("  -> OTC raw rows: {}".format(len(otc_df)))

    combined = pd.concat([tse_df, otc_df], ignore_index=True)
    combined = _clean_numeric(combined)
    combined = combined.dropna(subset=["close", "volume"])
    combined = combined[combined["stock_id"].apply(_is_valid_stock_id)]
    combined = combined[~combined["stock_id"].isin(_GOVT_STOCKS)]
    combined = combined[combined["close"] > 0]

    return combined


# Per-mode snapshot pre-filter config.
# min_vol : minimum single-day trading volume in shares (stock snapshot)
#           derived from the mode's Vol_MA20 requirement with a 0.5x safety factor
#           e.g. Vol_MA20 > 1000 lots -> today >= 500 lots = 500,000 shares
# price_max: hard price ceiling (None = no limit)
# cap      : max candidates to forward to per-stock history fetch
_MODE_CFG = {
    "mode_squeeze":         {"min_vol":   500_000, "price_max": 150,  "cap": 150},
    "mode_breakout":        {"min_vol": 1_000_000, "price_max": None, "cap": 100},
    "mode_bottom":          {"min_vol":   300_000, "price_max": None, "cap": 150},
    "mode_short_explosion": {"min_vol": 1_000_000, "price_max": None, "cap": 100},
    # Leader study casts a wide net (modest liquidity floor, larger cap) so the
    # 3-month >=30% gainers across the whole market are captured for inspection.
    "mode_momentum_leader": {"min_vol":   300_000, "price_max": None, "cap": 250},
    # Pre-launch wants the broadest net of all: the whole liquid above-trend
    # universe is scored, then ranked + held by hysteresis downstream.
    "mode_prelaunch":       {"min_vol":   300_000, "price_max": None, "cap": 300},
}
_DEFAULT_CFG = {"min_vol": 0, "price_max": None, "cap": PREFILTER_TOP_N}


def apply_prefilter(df: pd.DataFrame, scan_mode: str = "",
                    include_ids=None) -> pd.DataFrame:
    cfg = _MODE_CFG.get(scan_mode, _DEFAULT_CFG)

    filtered = df.copy()
    if cfg["price_max"] is not None:
        filtered = filtered[filtered["close"] < cfg["price_max"]]
    if cfg["min_vol"] > 0:
        filtered = filtered[filtered["volume"] >= cfg["min_vol"]]

    filtered = filtered.sort_values("volume", ascending=False).reset_index(drop=True)
    result = filtered.head(cfg["cap"]).reset_index(drop=True)

    # Force-include names held from the previous run so the downstream
    # hysteresis can retain them even if today's single-day volume dropped them
    # below the cap/floor. They must still appear in today's market snapshot.
    forced = 0
    if include_ids:
        want = set(str(x) for x in include_ids)
        have = set(result["stock_id"].astype(str))
        need = want - have
        if need:
            extra = df[df["stock_id"].astype(str).isin(need)]
            if not extra.empty:
                result = pd.concat([result, extra], ignore_index=True)
                result = result.drop_duplicates(subset="stock_id").reset_index(drop=True)
                forced = len(extra)

    print("  Pre-filter [{}]: {} candidates "
          "(vol>={:.0f}k shares{}, cap {}{})".format(
              scan_mode or "default",
              len(result),
              cfg["min_vol"] / 1000,
              ", price<{}".format(cfg["price_max"]) if cfg["price_max"] else "",
              cfg["cap"],
              ", +{} held".format(forced) if forced else "",
          ))
    return result


def _load_name_cache() -> dict:
    try:
        return json.loads(_NAME_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_names(df: pd.DataFrame, market: str, cache: dict) -> None:
    for _, r in df.iterrows():
        sid = str(r.get("stock_id", "")).strip()
        nm = str(r.get("stock_name", "")).strip()
        if sid and nm:
            cache[sid] = [nm, market]


def lookup_stock_info(stock_id: str):
    """
    Resolve (stock_name, market) for a single code. Checks a persistent local
    cache first (instant, no network); on a miss, fetches both exchange
    snapshots, caches EVERY name in them (~2000), then answers. Subsequent
    lookups of any mainstream code are then offline. Returns (stock_id, None)
    when the code is not found on either board.
    """
    sid = str(stock_id).strip()
    cache = _load_name_cache()
    if sid in cache:
        nm, mkt = cache[sid]
        return nm, mkt

    tse = _normalize_tse(_fetch_json(TSE_URL))
    if not tse.empty:
        _cache_names(tse, "TSE", cache)
    otc = _normalize_otc(_fetch_json(OTC_URL, verify_ssl=False))
    if not otc.empty:
        _cache_names(otc, "OTC", cache)

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _NAME_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    if sid in cache:
        nm, mkt = cache[sid]
        return nm, mkt
    return sid, None


def get_candidate_list(scan_mode: str = "", include_ids=None) -> pd.DataFrame:
    full = fetch_full_market()
    if full.empty:
        print("  ERROR: could not fetch market data from either TSE or OTC.")
        return pd.DataFrame()
    return apply_prefilter(full, scan_mode=scan_mode, include_ids=include_ids)
