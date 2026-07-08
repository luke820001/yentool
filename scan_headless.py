"""
scan_headless.py -- run one full scan with NO GUI (for cloud / CI / cron).

Mirrors gui/scan_worker.ScanWorker._run exactly, minus tkinter, so the same
validated pipeline (market_filter -> chip_verifier -> scan_mode -> hysteresis ->
trade columns -> ledger -> holding tracker -> export) runs on a GitHub Actions
runner. It refreshes the databases in-place and writes both the CSV and the
mobile PWA JSON (mobile/scan_result.json). All strings ASCII.

Usage:
    python scan_headless.py [mode]
        mode defaults to mode_prelaunch.

Exit code 0 on success, 1 if the market feed was unreachable (so CI can decide
whether to keep the previous data). Any single stage failure is logged but does
not abort the run, matching the GUI's resilience.
"""
import sys
import traceback

from scanner.market_filter import get_candidate_list
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import (
    apply_scan_mode, add_trade_columns, sort_for_mode, select_with_hysteresis,
)
from scanner.scan_state import load_held_ids, save_held_ids
from scanner.signal_ledger import record_picks, backfill_outcomes
from scanner.holding_tracker import annotate_holding
from scanner.result_export import export_scan_result


def _progress(rank, total, stock_id):
    # Print sparse progress so CI logs stay readable.
    if total and (rank == total or rank % 50 == 0):
        print("  scanning {}/{} ({})".format(rank, total, stock_id))


def run_scan(scan_mode="mode_prelaunch"):
    print("=== headless scan: {} ===".format(scan_mode))

    prior_ids = load_held_ids(scan_mode)
    candidates = get_candidate_list(scan_mode=scan_mode, include_ids=prior_ids)
    if candidates is None or candidates.empty:
        print("ERROR: could not fetch market data (empty candidate list).")
        return None

    result_df = verify_candidates(candidates, progress_callback=_progress)
    result_df = apply_scan_mode(result_df, scan_mode)
    result_df = sort_for_mode(result_df, scan_mode)
    result_df, held_ids = select_with_hysteresis(result_df, prior_ids)
    save_held_ids(scan_mode, held_ids)
    result_df = add_trade_columns(result_df, scan_mode)

    try:
        n = record_picks(result_df, scan_mode)
        filled = backfill_outcomes()
        print("  [ledger] recorded {} picks, backfilled {} outcomes".format(n, filled))
    except Exception as e:
        print("  [ledger] skipped: {}".format(e))

    try:
        result_df = annotate_holding(result_df, scan_mode)
    except Exception as e:
        print("  [holding] skipped: {}".format(e))

    try:
        path = export_scan_result(result_df, scan_mode)
        print("  [export] scan result -> {}".format(path))
    except Exception as e:
        print("  [export] failed: {}".format(e))

    print("=== done: {} picks ===".format(len(result_df)))
    return result_df


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "mode_prelaunch"
    try:
        out = run_scan(mode)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0 if out is not None and not out.empty else 1)


if __name__ == "__main__":
    main()
