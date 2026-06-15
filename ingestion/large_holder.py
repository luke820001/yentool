import re
import pandas as pd
from ingestion.base_fetcher import BaseFetcher
from storage.data_store import upsert_and_trim
from config.settings import LARGE_HOLDER_FILE

DATASET = "TaiwanStockShareholding"

# 400張 = 400,000 股（適用所有規模個股）
LARGE_SHARES = 400_000
# 50張以下 = 50,000 股（散戶定義）
RETAIL_SHARES = 50_000


def _lower_bound(level_str: str):
    """從集保分層字串取出下限股數（整數或 None）。"""
    cleaned = re.sub(r"[,\s]", "", str(level_str))
    m = re.match(r"(\d+)", cleaned)
    return int(m.group(1)) if m else None


def _is_large(level_str: str) -> bool:
    lb = _lower_bound(level_str)
    return lb is not None and lb >= LARGE_SHARES


def _is_retail(level_str: str) -> bool:
    lb = _lower_bound(level_str)
    return lb is not None and lb <= RETAIL_SHARES


class LargeHolderFetcher(BaseFetcher):

    def fetch(self, stock_id: str) -> pd.DataFrame:
        start_date, end_date = self._get_date_range()
        payload = self._request(DATASET, stock_id, start_date, end_date)
        raw = pd.DataFrame(payload.get("data", []))
        if raw.empty:
            return pd.DataFrame()
        return self._transform(raw)

    def _transform(self, raw: pd.DataFrame) -> pd.DataFrame:
        df = raw.copy()
        if "percent" not in df.columns:
            return pd.DataFrame()
        df["percent"] = pd.to_numeric(df["percent"], errors="coerce")

        base = df.groupby("date")["stock_id"].first().reset_index()

        # 大戶（400張以上）持股比例合計
        large_pct = (
            df[df["HoldingSharesLevel"].apply(_is_large)]
            .groupby("date")["percent"]
            .sum()
            .reset_index()
            .rename(columns={"percent": "Large_Holder_Pct"})
        )

        # 散戶（50張以下）持股比例合計
        retail_pct = (
            df[df["HoldingSharesLevel"].apply(_is_retail)]
            .groupby("date")["percent"]
            .sum()
            .reset_index()
            .rename(columns={"percent": "Retail_Pct"})
        )

        weekly = base.merge(large_pct, on="date", how="left")
        weekly = weekly.merge(retail_pct, on="date", how="left")
        weekly = weekly.sort_values("date").reset_index(drop=True)

        # 週環比變化
        weekly["Large_Pct_Change"]  = weekly["Large_Holder_Pct"].diff().round(4)
        weekly["Retail_Pct_Change"] = weekly["Retail_Pct"].diff().round(4)

        # Cond_B：大戶比例上升（若散戶同步下降則訊號更強，但非必要）
        weekly["Cond_B"] = weekly["Large_Pct_Change"] > 0

        return weekly

    def save(self, stock_id: str, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        return upsert_and_trim(
            file_path=LARGE_HOLDER_FILE,
            sheet_name=stock_id,
            new_df=df,
            date_col="date",
            key_cols=["date", "stock_id"],
        )

    def fetch_and_save(self, stock_id: str) -> pd.DataFrame:
        df = self.fetch(stock_id)
        return self.save(stock_id, df)
