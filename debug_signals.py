import pandas as pd
from scanner.market_filter import get_candidate_list
from ingestion.price_volume import PriceVolumeFetcher
from analyzer.signal_evaluator import _load_stock_data, _evaluate_conditions

def main():
    candidates = get_candidate_list()
    print("\nCandidate list:")
    print(candidates[["stock_id", "stock_name", "close", "volume", "market"]].to_string())

    pv_fetcher = PriceVolumeFetcher()

    for _, row in candidates.head(5).iterrows():
        stock_id = str(row["stock_id"])
        print("\n=== {} {} ===".format(stock_id, row["stock_name"]))

        try:
            pv_fetcher.fetch_and_save(stock_id)
        except Exception as e:
            print("  fetch error: {}".format(e))
            continue

        df = _load_stock_data(stock_id)
        if df.empty:
            print("  no price data")
            continue

        ev = _evaluate_conditions(df)
        latest = ev.sort_values("date").tail(5)

        cols = ["date", "close", "Volume_Lot", "Min_Volume_20",
                "Min_Price_20", "Max_Price_20", "Volume_Bias",
                "Cond_A", "Cond_C", "Is_Golden_Signal", "Is_Breakout_Signal"]
        cols = [c for c in cols if c in latest.columns]
        print(latest[cols].to_string())

if __name__ == "__main__":
    main()
