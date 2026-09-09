import sys
from config.settings import WATCHLIST
from ingestion.price_volume import PriceVolumeFetcher
from ingestion.large_holder import LargeHolderFetcher
from ingestion.broker_branch import BrokerBranchFetcher
from analyzer.signal_evaluator import SignalEvaluator, run_all_evaluations


def fetch_all_data(watchlist: list) -> None:
    pv_fetcher = PriceVolumeFetcher()
    lh_fetcher = LargeHolderFetcher()
    bb_fetcher = BrokerBranchFetcher()

    for stock_id in watchlist:
        print("[1/3] Fetching price/volume data for {} ...".format(stock_id))
        try:
            df = pv_fetcher.fetch_and_save(stock_id)
            print("      -> {} rows saved.".format(len(df)))
        except Exception as e:
            print("      -> ERROR: {}".format(e))

        # [2/3] Large holder data (Condition B) is bypassed:
        # TaiwanStockHoldingSharesPer requires a paid FinMind plan.
        # Re-enable this block when a valid data source is confirmed.
        # print("[2/3] Fetching large holder data for {} ...".format(stock_id))
        # try:
        #     df = lh_fetcher.fetch_and_save(stock_id)
        #     print("      -> {} rows saved.".format(len(df)))
        # except Exception as e:
        #     print("      -> ERROR: {}".format(e))

        print("[3/3] Fetching broker branch data for {} ...".format(stock_id))
        try:
            df = bb_fetcher.fetch_and_save(stock_id)
            print("      -> {} rows saved.".format(len(df)))
        except Exception as e:
            print("      -> ERROR: {}".format(e))

        print("--------------------------------------")


def run_signal_analysis(watchlist: list) -> None:
    print("Running signal analysis for all stocks ...")
    evaluator = SignalEvaluator()

    for stock_id in watchlist:
        result = evaluator.evaluate(stock_id)
        golden_count = (
            int(result.signal_rows["Is_Golden_Signal"].sum())
            if not result.signal_rows.empty and "Is_Golden_Signal" in result.signal_rows.columns
            else 0
        )
        breakout_count = (
            int(result.signal_rows["Is_Breakout_Signal"].sum())
            if not result.signal_rows.empty and "Is_Breakout_Signal" in result.signal_rows.columns
            else 0
        )
        print(
            "  [{}] Golden Signal hits (last 5 days): {}  |  "
            "Breakout Signal hits (last 5 days): {}".format(
                stock_id, golden_count, breakout_count
            )
        )

    print("")
    print("Exporting combined signal report ...")
    combined = run_all_evaluations(watchlist)

    if combined.empty:
        print("No signals triggered in the last 5 trading days across all stocks.")
    else:
        print(
            "Export complete. {} signal row(s) written to signal_log.xlsx.".format(
                len(combined)
            )
        )
        print("")
        print("--- Signal Summary ---")
        print(combined[["date", "stock_id", "Is_Golden_Signal", "Is_Breakout_Signal"]].to_string(index=False))


def main() -> None:
    print("======================================")
    print("  Taiwan Stock Signal Monitor - MVP   ")
    print("======================================")
    print("Watchlist: {}".format(", ".join(WATCHLIST)))
    print("")

    fetch_all_data(WATCHLIST)
    run_signal_analysis(WATCHLIST)

    print("")
    print("Done.")


if __name__ == "__main__":
    main()
