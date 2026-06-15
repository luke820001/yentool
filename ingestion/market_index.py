import pandas as pd
from ingestion.base_fetcher import BaseFetcher, FetchError
from storage.data_store import load_sheet, upsert_and_trim
from config.settings import TAIEX_FILE

# FinMind 台灣加權指數代碼
TAIEX_ID = "IR0001"
DATASET  = "TaiwanStockPrice"


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
        """先嘗試拉最新資料並快取，失敗時回傳本機快取。"""
        try:
            df = self.fetch()
            if not df.empty:
                self._cache(df)
                return df
        except Exception:
            pass
        return self.load_cached()
