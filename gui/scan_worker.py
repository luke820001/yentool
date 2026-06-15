import threading
import pandas as pd
from scanner.market_filter import get_candidate_list
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import apply_scan_mode


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
            self._on_result(result_df)

        except Exception as e:
            self._on_error(str(e))
        finally:
            self._on_done()
