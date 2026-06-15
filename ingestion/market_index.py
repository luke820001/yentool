import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from ingestion.base_fetcher import BaseFetcher, FetchError
from storage.data_store import load_sheet, upsert_and_trim
from config.settings import TAIEX_FILE, ROLLING_DAYS

TAIEX_ID   = "IR0001"
DATASET    = "TaiwanStockPrice"
TAIEX_YF   = "^TWII"       # yfinance ticker for Taiwan Weighted Index


class MarketIndexFetcher(BaseFetcher):

    def fetch(self, stock_id: str = TAIEX_ID) -> pd.DataFrame:
        start_date, end_date = self._get_date_range()
        try:
            payload = self._request(DATASET, stock_id, start_date, end_date)
        except FetchError:
            return pd.DataFrame()
        raw = pd.DataFrame(payload.get("data", []))
        if raw.empty:
            return pd.DataFrame()
        return self._transform(raw)

    def _fetch_yfinance(self) -> pd.DataFrame:
        """Fallback: fetch TAIEX via yfinance (no token required)."""
        try:
            start = (datetime.today() - timedelta(days=ROLLING_DAYS)).strftime("%Y-%m-%d")
            raw = yf.download(TAIEX_YF, start=start, progress=False, auto_adjust=True)
            if raw.empty:
                return pd.DataFrame()
            df = raw[["Close"]].reset_index()
            df.columns = ["date", "close"]
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            return df.dropna().sort_values("date").reset_index(drop=True)
        except Exception as e:
            print("  [TAIEX] yfinance fallback error: {}".format(e))
            return pd.DataFrame()

    def _transform(self, raw: pd.DataFrame) -> pd.DataFrame:
        df = raw.copy()
        df["close"] = pd.to_numeric(df.get("close", pd.Series(dtype=float)), errors="coerce")
        df = df[["date", "close"]].dropna()
        return df.sort_values("date").reset_index(drop=True)

    def _cache(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        upsert_and_trim(
            file_path=TAIEX_FILE,
            sheet_name="TAIEX",
            new_df=df,
            date_col="date",
            key_cols=["date"],
        )

    def load_cached(self) -> pd.DataFrame:
        df = load_sheet(TAIEX_FILE, "TAIEX")
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.sort_values("date").dropna(subset=["close"]).reset_index(drop=True)

    def get(self) -> pd.DataFrame:
        """1. FinMind  2. yfinance fallback  3. local cache"""
        try:
            df = self.fetch()
            if not df.empty:
                self._cache(df)
                return df
        except Exception:
            pass

        df = self._fetch_yfinance()
        if not df.empty:
            print("  [TAIEX] loaded via yfinance ({} rows)".format(len(df)))
            self._cache(df)
            return df

        return self.load_cached()
