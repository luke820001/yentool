import pandas as pd
from ingestion.base_fetcher import BaseFetcher
from storage.data_store import upsert_and_trim
from config.settings import PRICE_VOLUME_FILE

DATASET = "TaiwanStockPrice"

COLS_RENAME = {
    "Trading_Volume": "volume_share",
    "open": "open",
    "max": "high",
    "min": "low",
    "close": "close",
}

COLS_KEEP = ["date", "stock_id", "open", "high", "low", "close", "volume_share"]


class PriceVolumeFetcher(BaseFetcher):

    def fetch(self, stock_id: str) -> pd.DataFrame:
        start_date, end_date = self._get_date_range()
        payload = self._request(DATASET, stock_id, start_date, end_date)
        raw = pd.DataFrame(payload.get("data", []))
        if raw.empty:
            return pd.DataFrame()
        return self._transform(raw)

    def _transform(self, raw: pd.DataFrame) -> pd.DataFrame:
        df = raw.rename(columns=COLS_RENAME)
        df = df[[c for c in COLS_KEEP if c in df.columns]].copy()

        numeric_cols = ["open", "high", "low", "close", "volume_share"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["Volume_Lot"] = (df["volume_share"] / 1000).round(0).astype("Int64")

        df = df.sort_values("date").reset_index(drop=True)

        df["MA5_Volume"] = (
            df["Volume_Lot"].rolling(window=5, min_periods=1).mean().round(0)
        )
        df["Min_Volume_20"] = (
            df["Volume_Lot"].rolling(window=20, min_periods=1).min()
        )
        df["Max_Price_20"] = (
            df["close"].rolling(window=20, min_periods=1).max()
        )
        df["Min_Price_20"] = (
            df["close"].rolling(window=20, min_periods=1).min()
        )

        return df

    def save(self, stock_id: str, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        return upsert_and_trim(
            file_path=PRICE_VOLUME_FILE,
            sheet_name=stock_id,
            new_df=df,
            date_col="date",
            key_cols=["date", "stock_id"],
        )

    def fetch_and_save(self, stock_id: str) -> pd.DataFrame:
        df = self.fetch(stock_id)
        return self.save(stock_id, df)
