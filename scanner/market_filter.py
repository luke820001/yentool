import time
import warnings
import requests
import urllib3
import pandas as pd
from config.settings import PRICE_FILTER_MAX, VOLUME_TOP_N, PREFILTER_TOP_N

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
OTC_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

REQUEST_TIMEOUT = 20
RETRY_LIMIT = 3
RETRY_SLEEP = 5


def _fetch_json(url: str, verify_ssl: bool = True) -> list:
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, verify=verify_ssl)
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
    digits_only = sid.isdigit()
    if not digits_only:
        return False
    if not (4 <= len(sid) <= 6):
        return False
    # exclude ETFs: codes starting with "00" are funds/ETFs
    if sid.startswith("00"):
        return False
    return True


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
}
_DEFAULT_CFG = {"min_vol": 0, "price_max": None, "cap": PREFILTER_TOP_N}


def apply_prefilter(df: pd.DataFrame, scan_mode: str = "") -> pd.DataFrame:
    cfg = _MODE_CFG.get(scan_mode, _DEFAULT_CFG)

    filtered = df.copy()
    if cfg["price_max"] is not None:
        filtered = filtered[filtered["close"] < cfg["price_max"]]
    if cfg["min_vol"] > 0:
        filtered = filtered[filtered["volume"] >= cfg["min_vol"]]

    filtered = filtered.sort_values("volume", ascending=False).reset_index(drop=True)
    result = filtered.head(cfg["cap"]).reset_index(drop=True)

    print("  Pre-filter [{}]: {} candidates "
          "(vol>={:.0f}k shares{}, cap {})".format(
              scan_mode or "default",
              len(result),
              cfg["min_vol"] / 1000,
              ", price<{}".format(cfg["price_max"]) if cfg["price_max"] else "",
              cfg["cap"],
          ))
    return result


def get_candidate_list(scan_mode: str = "") -> pd.DataFrame:
    full = fetch_full_market()
    if full.empty:
        print("  ERROR: could not fetch market data from either TSE or OTC.")
        return pd.DataFrame()
    return apply_prefilter(full, scan_mode=scan_mode)
