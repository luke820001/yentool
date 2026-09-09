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

from config.settings import PORTFOLIO_LEDGER_FILE
from scanner.market_filter import get_candidate_list, get_feed_health
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import (
    apply_scan_mode, add_trade_columns, sort_for_mode, select_with_hysteresis,
    mark_buy_ready, STRATEGY_VERSION,
)
from scanner.scan_state import load_held_ids, save_held_ids
from scanner.signal_ledger import record_picks, backfill_outcomes
from scanner.holding_tracker import annotate_holding
from scanner.result_export import export_scan_result
from portfolio.sync import attach_recommendations, summarize


def _progress(rank, total, stock_id):
    # Print sparse progress so CI logs stay readable.
    if total and (rank == total or rank % 50 == 0):
        print("  scanning {}/{} ({})".format(rank, total, stock_id))


def _session_date(df):
    """The trading session this scan represents: the NEWEST bar date present.

    holding_tracker used to take row 0's Data_Date as the whole frame's "today"
    (F13). Row 0 is simply the top-ranked stock, and a single stale feed there
    would have redated the entire scan. Taking the maximum means one lagging
    stock cannot drag the session backwards -- and mark_buy_ready then blocks
    each individual row whose own bar does not match it.
    """
    if df is None or df.empty or "Data_Date" not in df.columns:
        return ""
    dates = [str(d)[:10] for d in df["Data_Date"] if str(d or "").strip()]
    return max(dates) if dates else ""


def build_market_reports(df):
    """Pre-generate one AI report per market view (ALL/OTC/TSE) so the phone can
    show the report matching its current filter -- exactly the desktop behaviour
    of "summarize what is on screen" (e.g. OTC filter -> only the OTC names go to
    the model). With no GEMINI/GROQ key set, generate_report falls back to a local
    text summary, so the phone always has something. Never aborts the scan."""
    reports = {}
    try:
        from gemini_hook.gemini_client import generate_report
    except Exception as e:
        print("  [ai] report module unavailable: {}".format(e))
        return reports

    views = {"ALL": df}
    if "Market" in df.columns:
        for mkt in ("OTC", "TSE"):
            sub = df[df["Market"].astype(str) == mkt]
            if not sub.empty:
                views[mkt] = sub
    for name, sub in views.items():
        try:
            reports[name] = generate_report(sub)
            print("  [ai] {} report generated ({} names)".format(name, len(sub)))
        except Exception as e:
            print("  [ai] {} report skipped: {}".format(name, str(e)[:80]))
    return reports


def run_scan(scan_mode="mode_prelaunch"):
    print("=== headless scan: {} ===".format(scan_mode))

    prior_ids = load_held_ids(scan_mode)
    candidates = get_candidate_list(scan_mode=scan_mode, include_ids=prior_ids)
    if candidates is None or candidates.empty:
        print("ERROR: could not fetch market data (empty candidate list).")
        return None

    # Degraded feed (an exchange snapshot failed its sanity floor): the scan
    # still runs on whatever survived so the phone is not blind, but NOTHING
    # persistent is overwritten -- held_ids keep yesterday's set (hysteresis
    # continuity) and the ledger keeps any earlier successful session of the
    # day (record_picks would DELETE it). See 2026-07-14 incident in
    # market_filter.FEED_MIN_ROWS.
    health = get_feed_health()
    degraded = None
    if not health.get("ok", True):
        degraded = "feed degraded: {} snapshot failed (TSE {} / OTC {} rows)".format(
            "+".join(health.get("missing", [])),
            health.get("tse_rows"), health.get("otc_rows"))
        print("  [guard] {} -> held_ids/ledger NOT updated".format(degraded))

    result_df = verify_candidates(candidates, progress_callback=_progress)
    result_df = apply_scan_mode(result_df, scan_mode)
    result_df = sort_for_mode(result_df, scan_mode)
    result_df, held_ids = select_with_hysteresis(result_df, prior_ids)
    if degraded is None:
        save_held_ids(scan_mode, held_ids)
    result_df = add_trade_columns(result_df, scan_mode)

    session_date = _session_date(result_df)

    try:
        result_df = annotate_holding(result_df, scan_mode)
    except Exception as e:
        print("  [holding] skipped: {}".format(e))

    # Buy_Ready must come after annotate_holding: the rule needs Hold_Status to
    # tell a fresh signal from a name that has been listed for two weeks.
    #
    # It must also come BEFORE record_picks. The two used to be the other way
    # round, so the ledger's own record of a day could never say whether a pick
    # was buyable -- it was written before anything had decided. Getting the
    # reason a name was refused into the ledger is the only way W23 (attributing
    # a loss to the signal, the price, the execution or the exit) is answerable
    # later.
    try:
        result_df = mark_buy_ready(result_df, scan_mode, session_date=session_date)
        if "Buy_Ready" in result_df.columns:
            ready = int(result_df["Buy_Ready"].sum())
            print("  [buyrule] {} of {} rows are buyable today".format(
                ready, len(result_df)))
            if not ready and len(result_df):
                blocks = result_df["Buy_Block"].value_counts().to_dict()
                print("  [buyrule] nothing buyable, reasons: {}".format(blocks))
    except Exception as e:
        print("  [buyrule] skipped: {}".format(e))

    # Freeze the first-day recommendation for anything that qualified today,
    # and hang the frozen price off every row we already have one for. After
    # this, Suggested_Buy_Price (recomputed daily) and Initial_Buy_Price (fixed)
    # are both on the row and can never again be mistaken for each other (F01).
    try:
        result_df, rec_stats = attach_recommendations(
            result_df, scan_mode, STRATEGY_VERSION, PORTFOLIO_LEDGER_FILE,
            session_date=session_date)
        print("  [rec] {}".format(summarize(rec_stats)))
    except Exception as e:
        print("  [rec] skipped: {}".format(e))

    try:
        if degraded is None:
            n = record_picks(result_df, scan_mode)
        else:
            n = 0
        filled = backfill_outcomes()
        print("  [ledger] recorded {} picks, backfilled {} outcomes".format(n, filled))
    except Exception as e:
        print("  [ledger] skipped: {}".format(e))

    # Publish FIRST, summarise second (F24). The AI call used to run before the
    # export, so a hung Gemini request delayed -- and an unconverted
    # requests.Timeout could skip past -- the market data and the user's own
    # position prices. Nobody's holdings should wait on a language model.
    try:
        path = export_scan_result(result_df, scan_mode, degraded=degraded,
                                  session_date=session_date,
                                  strategy_version=STRATEGY_VERSION)
        print("  [export] scan result -> {}".format(path))
    except Exception as e:
        print("  [export] failed: {}".format(e))

    # Per-market AI reports for the phone (matches the desktop "summarize what is
    # shown" behaviour; OTC filter -> only OTC names sent to the model). Written
    # as a second pass over the already-published payload, so a failure here
    # leaves the prices and the P&L intact and merely omits the commentary.
    reports = build_market_reports(result_df)
    if reports:
        try:
            export_scan_result(result_df, scan_mode, reports=reports,
                               degraded=degraded, session_date=session_date,
                               strategy_version=STRATEGY_VERSION)
            print("  [ai] {} report(s) attached".format(len(reports)))
        except Exception as e:
            print("  [ai] attach failed, prices already published: {}".format(e))

    print("=== done: {} picks ===".format(len(result_df)))
    return result_df


def main():
    """Exit codes distinguish the three outcomes CI has to tell apart (F18).

        0  the scan ran and published -- INCLUDING a legitimate zero-pick day.
           Nothing qualified, we said so, the phone shows "0 setups today".
        1  the market feed was unreachable, so there is no result to publish
           and the previous data must be left in place.
        2  the scan itself crashed.

    The old code returned 1 for both "no picks" and "feed dead", so a perfectly
    healthy quiet day looked like an outage in the Actions log and there was no
    way to alert on the difference.
    """
    mode = sys.argv[1] if len(sys.argv) > 1 else "mode_prelaunch"
    try:
        out = run_scan(mode)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
    if out is None:
        print("=== market feed unreachable: previous data left in place ===")
        sys.exit(1)
    if out.empty:
        print("=== healthy scan, 0 qualifying names: published count=0 ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
