import time
import requests
import pandas as pd
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from config.settings import FINMIND_API_URL, FINMIND_TOKEN, ROLLING_DAYS

REQUEST_DELAY_SECONDS = 1.5


class FetchError(Exception):
    pass


class BaseFetcher(ABC):

    def __init__(self):
        self.api_url = FINMIND_API_URL
        self.token = FINMIND_TOKEN

    def _get_date_range(self):
        end = datetime.today()
        start = end - timedelta(days=ROLLING_DAYS)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    def _request(self, dataset: str, stock_id: str, start_date: str, end_date: str) -> dict:
        params = {
            "api_token": self.token,
            "dataset": dataset,
            "data_id": stock_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        time.sleep(REQUEST_DELAY_SECONDS)
        response = requests.get(self.api_url, params=params, timeout=30)
        if response.status_code != 200:
            raise FetchError(
                "HTTP {} for dataset={} stock={}".format(
                    response.status_code, dataset, stock_id
                )
            )
        payload = response.json()
        if payload.get("status") != 200:
            raise FetchError(
                "API error for dataset={} stock={}: {}".format(
                    dataset, stock_id, payload.get("msg", "unknown")
                )
            )
        return payload

    @abstractmethod
    def fetch(self, stock_id: str) -> pd.DataFrame:
        pass

    def fetch_all(self, watchlist: list) -> dict:
        results = {}
        for stock_id in watchlist:
            try:
                results[stock_id] = self.fetch(stock_id)
            except FetchError as e:
                print("WARN: fetch failed for {} - {}".format(stock_id, e))
                results[stock_id] = pd.DataFrame()
        return results
