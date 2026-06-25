import threading
import pandas as pd
from scanner.market_filter import get_candidate_list, lookup_stock_info
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import (
    apply_scan_mode, add_trade_columns, sort_for_mode, select_with_hysteresis,
)
from scanner.scan_state import load_held_ids, save_held_ids
from scanner.signal_ledger import record_picks, backfill_outcomes
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
            # Names held from the previous run are force-included in the
            # candidate pool so the hysteresis layer can actually retain them
            # even if their single-day volume slipped below the prefilter cap.
            prior_ids = load_held_ids(self._scan_mode)
            candidates = get_candidate_list(
                scan_mode=self._scan_mode, include_ids=prior_ids)

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
            # Hysteresis top-N: stabilizes the shortlist day-to-day (see
            # scanner.scan_mode.select_with_hysteresis). Persist the kept set so
            # the next run can hold these names through transient dips.
            result_df, held_ids = select_with_hysteresis(result_df, prior_ids)
            save_held_ids(self._scan_mode, held_ids)
            result_df = add_trade_columns(result_df, self._scan_mode)

            # Forward-performance ledger: append today's shortlist (append-only,
            # idempotent per day) and backfill any matured outcomes. Never let a
            # ledger hiccup break the scan.
            try:
                n = record_picks(result_df, self._scan_mode)
                filled = backfill_outcomes()
                print("  [ledger] recorded {} picks, backfilled {} outcomes".format(
                    n, filled))
            except Exception as e:
                print("  [ledger] skipped: {}".format(e))

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
                 scan_mode="mode_prelaunch"):
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
