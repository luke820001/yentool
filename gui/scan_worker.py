import threading
import pandas as pd
from scanner.market_filter import get_candidate_list, lookup_stock_info
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import apply_scan_mode, add_trade_columns, sort_for_mode
from scanner.result_export import export_scan_result
from ingestion.price_volume_multi import resolve_market


class ScanWorker:

    def __init__(self, on_progress, on_result, on_error, on_done,
                 scan_mode="mode_squeeze"):
        self._on_progress = on_progress
        self._on_result   = on_result
        self._on_error    = on_error
        self._on_done     = on_done
        self._scan_mode   = scan_mode
        self._thread      = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._on_progress(0, 1, "Fetching market overview...")
            candidates = get_candidate_list(scan_mode=self._scan_mode)

            if candidates.empty:
                self._on_error("Failed to fetch market data. Check your connection.")
                return

            total = len(candidates)

            def progress_callback(rank, total, stock_id):
                self._on_progress(rank, total, "Scanning {} ({}/{})".format(
                    stock_id, rank, total))

            result_df = verify_candidates(candidates, progress_callback=progress_callback)
            result_df = apply_scan_mode(result_df, self._scan_mode)
            result_df = sort_for_mode(result_df, self._scan_mode)
            result_df = add_trade_columns(result_df, self._scan_mode)

            # Persist the latest result (overwrites previous) for offline review.
            try:
                path = export_scan_result(result_df, self._scan_mode)
                if path:
                    print("  [export] scan result -> {}".format(path))
            except Exception as e:
                print("  [export] failed: {}".format(e))

            self._on_result(result_df)

        except Exception as e:
            self._on_error(str(e))
        finally:
            self._on_done()


class SingleStockWorker:
    """Manual lookup: analyze ONE stock with the full pipeline and show it
    regardless of any mode filter. Buy/stop still follow the selected mode."""

    def __init__(self, stock_id, on_progress, on_result, on_error, on_done,
                 scan_mode="mode_momentum_leader"):
        self._stock_id   = str(stock_id).strip()
        self._on_progress = on_progress
        self._on_result   = on_result
        self._on_error    = on_error
        self._on_done     = on_done
        self._scan_mode   = scan_mode
        self._thread      = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._on_progress(0, 1, "Resolving {} ...".format(self._stock_id))
            # Name + market from the live snapshot; fall back to a yfinance
            # market probe (name = code) if the snapshot does not list it.
            name, market = lookup_stock_info(self._stock_id)
            if market is None:
                market = resolve_market(self._stock_id)

            candidates = pd.DataFrame([{
                "stock_id":   self._stock_id,
                "market":     market,
                "stock_name": name,
            }])

            def progress_callback(rank, total, stock_id):
                self._on_progress(rank, total, "Analyzing {} ({}/{})".format(
                    stock_id, rank, total))

            result_df = verify_candidates(candidates, progress_callback=progress_callback)

            if result_df is None or result_df.empty:
                self._on_error(
                    "No data for {} (wrong code, delisted, or no history).".format(
                        self._stock_id))
                return

            # NOTE: no apply_scan_mode here -- a manual lookup always shows the
            # stock. Buy/stop still use the selected mode's entry style.
            result_df = add_trade_columns(result_df, self._scan_mode)
            self._on_result(result_df)

        except Exception as e:
            self._on_error(str(e))
        finally:
            self._on_done()
