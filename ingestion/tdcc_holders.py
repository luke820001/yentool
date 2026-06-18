"""
TDCC shareholding-distribution fetcher (free, whole-market, weekly).

Replaces the paid FinMind chip path. One request to the TDCC open-data endpoint
returns every listed stock's holding distribution by share-count tier; we derive
the large-holder (>=400 lots) and retail (<=50 lots) percentages, store weekly
snapshots in large_holder.db, and compute the week-over-week change
(Cond_B = large-holder percentage rising). All strings ASCII.

Tier reference (TDCC level -> shares):
  1: 1-999            ... 8: 40,001-50,000    (<=50 lots  -> retail)
  11: 200,001-400,000
  12: 400,001-600,000 ... 15: over 1,000,000   (>=400 lots -> large holder)
  16: adjustment, 17: total
"""
import io
import sqlite3
from datetime import datetime, date as _date

import requests
import urllib3
import pandas as pd

from config.settings import LARGE_HOLDER_FILE

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TDCC_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
LARGE_LEVELS  = {"12", "13", "14", "15"}                       # >= 400,001 shares
RETAIL_LEVELS = {"1", "2", "3", "4", "5", "6", "7", "8"}       # <= 50,000 shares
KEEP_DAYS = 120


def fetch_tdcc_latest() -> pd.DataFrame:
    """One request -> per-stock {stock_id, date, Large_Holder_Pct, Retail_Pct}."""
    r = requests.get(TDCC_URL, timeout=60, verify=False, headers=_HEADERS)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), encoding="utf-8", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    date_c, code_c, lvl_c = df.columns[0], df.columns[1], df.columns[2]
    pct_c = df.columns[-1]

    code = df[code_c].astype(str).str.strip()
    lvl = df[lvl_c].astype(str).str.strip()
    pct = pd.to_numeric(df[pct_c].astype(str).str.replace(",", "", regex=False), errors="coerce")
    raw_date = str(df[date_c].iloc[0]).strip()
    iso = "{}-{}-{}".format(raw_date[:4], raw_date[4:6], raw_date[6:8])

    g = pd.DataFrame({"stock_id": code, "lvl": lvl, "pct": pct})
    large = g[g["lvl"].isin(LARGE_LEVELS)].groupby("stock_id")["pct"].sum()
    retail = g[g["lvl"].isin(RETAIL_LEVELS)].groupby("stock_id")["pct"].sum()
    out = pd.concat([large.rename("Large_Holder_Pct"),
                     retail.rename("Retail_Pct")], axis=1).reset_index()
    out["date"] = iso
    return out.dropna(subset=["Large_Holder_Pct"]).reset_index(drop=True)


def _read_existing() -> pd.DataFrame:
    if not LARGE_HOLDER_FILE.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(LARGE_HOLDER_FILE) as conn:
            return pd.read_sql_query("SELECT * FROM data", conn)
    except Exception:
        return pd.DataFrame()


def _latest_date(existing: pd.DataFrame):
    if existing.empty or "date" not in existing.columns:
        return None
    try:
        return str(existing["date"].max())[:10]
    except Exception:
        return None


def update_tdcc_holdings(max_age_days: int = 5) -> str:
    """Fetch the latest weekly snapshot (unless already fresh) and upsert it with
    week-over-week change + Cond_B. Returns the stored latest date string."""
    existing = _read_existing()
    latest = _latest_date(existing)
    if latest:
        try:
            age = (_date.today() - datetime.strptime(latest, "%Y-%m-%d").date()).days
            if age <= max_age_days:
                return latest   # weekly data; still fresh, no refetch
        except Exception:
            pass

    snap = fetch_tdcc_latest()
    if snap.empty:
        return latest
    new_date = str(snap["date"].iloc[0])[:10]
    if latest == new_date:
        return latest

    # most recent prior reading per stock -> week-over-week change
    prior = {}
    if not existing.empty:
        ex = existing.copy()
        ex["date"] = ex["date"].astype(str).str[:10]
        for sid, grp in ex.sort_values("date").groupby("stock_id"):
            last = grp.iloc[-1]
            prior[str(sid)] = (
                pd.to_numeric(last.get("Large_Holder_Pct"), errors="coerce"),
                pd.to_numeric(last.get("Retail_Pct"), errors="coerce"),
            )

    rows = []
    for _, r in snap.iterrows():
        sid = str(r["stock_id"])
        lg = float(r["Large_Holder_Pct"])
        rt = float(r["Retail_Pct"]) if pd.notna(r["Retail_Pct"]) else 0.0
        plg, prt = prior.get(sid, (None, None))
        lgc = round(lg - float(plg), 4) if (plg is not None and pd.notna(plg)) else None
        rtc = round(rt - float(prt), 4) if (prt is not None and pd.notna(prt)) else None
        rows.append({
            "date": new_date, "stock_id": sid,
            "Large_Holder_Pct": round(lg, 2), "Retail_Pct": round(rt, 2),
            "Large_Pct_Change": lgc, "Retail_Pct_Change": rtc,
            "Cond_B": bool(lgc is not None and lgc > 0),
        })
    newdf = pd.DataFrame(rows)

    combined = pd.concat([existing, newdf], ignore_index=True) if not existing.empty else newdf
    combined["date"] = combined["date"].astype(str).str[:10]
    combined = combined.drop_duplicates(subset=["date", "stock_id"], keep="last")
    cutoff = (_date.today() - pd.Timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    combined = combined[combined["date"] >= cutoff]

    LARGE_HOLDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(LARGE_HOLDER_FILE) as conn:
        combined.to_sql("data", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lh ON data(stock_id, date)")
    return new_date


if __name__ == "__main__":
    print("TDCC holdings updated to:", update_tdcc_holdings(max_age_days=-1))
