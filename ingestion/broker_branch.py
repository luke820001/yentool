import time
import pandas as pd
from datetime import datetime, timedelta
from ingestion.base_fetcher import BaseFetcher, FetchError
from storage.data_store import upsert_and_trim
from config.settings import BROKER_BRANCH_FILE, ROLLING_DAYS

DATASET = "TaiwanStockBrokerBranch"
TOP_N_BROKERS = 15
CHUNK_DAYS = 30
CHUNK_SLEEP_SECONDS = 3


def _top15_buy(series: pd.Series) -> float:
    positives = series[series > 0].nlargest(TOP_N_BROKERS)
    return positives.sum()


def _top15_sell(series: pd.Series) -> float:
    negatives = series[series < 0].nsmallest(TOP_N_BROKERS)
    return negatives.abs().sum()


class BrokerBranchFetcher(BaseFetcher):

    def _build_chunks(self):
        end = datetime.today()
        start = end - timedelta(days=ROLLING_DAYS)
        chunks = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
            chunks.append((cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
            cursor = chunk_end + timedelta(days=1)
        return chunks

    def fetch(self, stock_id: str) -> pd.DataFrame:
        chunks = self._build_chunks()
        all_parts = []
        for i, (chunk_start, chunk_end) in enumerate(chunks):
            print("      -> chunk {}/{}: {} to {}".format(
                i + 1, len(chunks), chunk_start, chunk_end))
            try:
                payload = self._request(DATASET, stock_id, chunk_start, chunk_end)
                part = pd.DataFrame(payload.get("data", []))
                if not part.empty:
                    all_parts.append(part)
            except FetchError as e:
                print("      -> WARN chunk skipped: {}".format(e))
            if i < len(chunks) - 1:
                time.sleep(CHUNK_SLEEP_SECONDS)

        if not all_parts:
            print("      -> no broker data retrieved for {}".format(stock_id))
            return pd.DataFrame()
        raw = pd.concat(all_parts, ignore_index=True).drop_duplicates()
        if raw.empty:
            return pd.DataFrame()
        return self._transform(raw, stock_id)

    def _transform(self, raw: pd.DataFrame, stock_id: str) -> pd.DataFrame:
        df = raw.copy()
        df["buy"] = pd.to_numeric(df["buy"], errors="coerce").fillna(0)
        df["sell"] = pd.to_numeric(df["sell"], errors="coerce").fillna(0)

        broker_daily = (
            df.groupby(["date", "BrokerageName"], as_index=False)
            .agg(buy=("buy", "sum"), sell=("sell", "sum"))
        )
        broker_daily["net_buy"] = broker_daily["buy"] - broker_daily["sell"]

        def _aggregate_day(grp: pd.DataFrame) -> pd.Series:
            buy_count = (grp["net_buy"] > 0).sum()
            sell_count = (grp["net_buy"] < 0).sum()
            broker_diff = int(buy_count) - int(sell_count)
            daily_total = grp["buy"].sum()
            top15_buy = _top15_buy(grp["net_buy"])
            top15_sell = _top15_sell(grp["net_buy"])
            return pd.Series({
                "stock_id": stock_id,
                "Buy_Brokers_Count": int(buy_count),
                "Sell_Brokers_Count": int(sell_count),
                "Broker_Diff": broker_diff,
                "Daily_Total_Volume": daily_total,
                "Top_15_Buy_Volume": top15_buy,
                "Top_15_Sell_Volume": top15_sell,
            })

        daily = (
            broker_daily.groupby("date")
            .apply(_aggregate_day)
            .reset_index()
        )
        daily = daily.sort_values("date").reset_index(drop=True)

        daily["Broker_Diff_Trend_Negative"] = (
            daily["Broker_Diff"]
            .rolling(window=5, min_periods=5)
            .apply(lambda x: bool((x < 0).all()), raw=True)
            .astype("boolean")
        )

        window = 5
        roll_buy = daily["Top_15_Buy_Volume"].rolling(window=window, min_periods=window).sum()
        roll_sell = daily["Top_15_Sell_Volume"].rolling(window=window, min_periods=window).sum()
        roll_total = daily["Daily_Total_Volume"].rolling(window=window, min_periods=window).sum()

        daily["Concentration_5D"] = (
            ((roll_buy - roll_sell) / roll_total.replace(0, float("nan"))) * 100
        ).round(2)

        daily["Is_Concentration_High"] = daily["Concentration_5D"] > 15.0

        return daily

    def save(self, stock_id: str, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        return upsert_and_trim(
            file_path=BROKER_BRANCH_FILE,
            sheet_name=stock_id,
            new_df=df,
            date_col="date",
            key_cols=["date", "stock_id"],
        )

    def fetch_and_save(self, stock_id: str) -> pd.DataFrame:
        df = self.fetch(stock_id)
        return self.save(stock_id, df)
