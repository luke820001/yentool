import requests
import json
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.environ.get("FINMIND_TOKEN", "")

def check_dataset(dataset: str, stock_id: str, start: str, end: str) -> None:
    params = {
        "api_token": TOKEN,
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start,
        "end_date": end,
    }
    r = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=30)
    print("HTTP status:", r.status_code)
    payload = r.json()
    print("API status:", payload.get("status"))
    print("API msg:", payload.get("msg"))
    data = payload.get("data", [])
    print("Row count:", len(data))
    if data:
        print("First row keys:", list(data[0].keys()))
        print("First row sample:", json.dumps(data[0], ensure_ascii=True, indent=2))
    else:
        print("No data returned.")

if __name__ == "__main__":
    print("=== BrokerBranch: recent 7 days ===")
    check_dataset("TaiwanStockBrokerBranch", "2330", "2026-06-07", "2026-06-14")
    print("")
    print("=== BrokerBranch: single date only ===")
    check_dataset("TaiwanStockBrokerBranch", "2330", "2026-06-13", "2026-06-13")
    print("")
    print("=== TaiwanStockPrice: sanity check ===")
    check_dataset("TaiwanStockPrice", "2330", "2026-06-07", "2026-06-14")
