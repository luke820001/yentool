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

Exit codes (see main): 0 ran and published, INCLUDING a legitimate day where
nothing qualified; 1 the market feed was unreachable so the previous data is
left in place; 2 the scan crashed. Any single stage failure is logged but does
not abort the run, matching the GUI's resilience.
"""
import sys
import traceback

from config.settings import PORTFOLIO_LEDGER_FILE, RECOMMENDATIONS_EXPORT_FILE
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
from portfolio.publish import seed_from_export, export_recommendations


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


def _data_health(df, session_date, degraded):
    """Whether the data we just scanned is as fresh as it should be.

    mark_buy_ready compares each row against `session_date`, which catches ONE
    stock whose feed died. It cannot catch the case where the whole fetch
    silently returned yesterday, because the session is derived from the data
    itself.

    The obvious fix -- block everything when the data is older than
    chip_verifier._latest_trading_day() -- is wrong: that helper rolls back
    weekends but knows nothing about public holidays, so on the day after Lunar
    New Year it would declare perfectly good data stale and refuse every buy.
    Until there is a real market calendar (report 9.1's trading_sessions, still
    unbuilt), the discrepancy is REPORTED rather than enforced: the UI can say
    "data may be behind" without the scanner pretending to know the holiday
    schedule. Guessing in the blocking direction is still guessing.
    """
    health = {"session_date": session_date, "rows": 0 if df is None else len(df),
              "degraded": degraded}
    try:
        from scanner.chip_verifier import _latest_trading_day
        expected = _latest_trading_day()
        health["expected_session"] = expected
        health["data_lag"] = bool(session_date and expected
                                  and session_date < expected)
        health["calendar_confirmed"] = False   # no official calendar yet
    except Exception:
        health["expected_session"] = None
        health["data_lag"] = None
    if df is not None and not df.empty and "Buy_Ready" in df.columns:
        health["buy_ready"] = int(df["Buy_Ready"].sum())
        if "Buy_Block" in df.columns:
            health["blocks"] = {str(k): int(v) for k, v
                                in df["Buy_Block"].value_counts().items() if k}
    return health


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
    # CI starts with no ledger (the database is local and gitignored), so the
    # fixed first-day recommendations are restored from the published JSON
    # before anything reads them. Additive: an existing row is never rewritten.
    try:
        seeded = seed_from_export(PORTFOLIO_LEDGER_FILE, RECOMMENDATIONS_EXPORT_FILE)
        if seeded:
            print("  [rec] seeded {} recommendation(s) from the published export".format(seeded))
    except Exception as e:
        print("  [rec] seed skipped: {}".format(e))

    try:
        result_df, rec_stats = attach_recommendations(
            result_df, scan_mode, STRATEGY_VERSION, PORTFOLIO_LEDGER_FILE,
            session_date=session_date)
        print("  [rec] {}".format(summarize(rec_stats)))
    except Exception as e:
        print("  [rec] skipped: {}".format(e))

    # Re-publish the recommendations-only view. This -- not the database -- is
    # what gets committed to the public repo.
    try:
        n = export_recommendations(PORTFOLIO_LEDGER_FILE, RECOMMENDATIONS_EXPORT_FILE)
        print("  [rec] exported {} recommendation(s) -> {}".format(
            n, RECOMMENDATIONS_EXPORT_FILE.name))
    except Exception as e:
        print("  [rec] export skipped: {}".format(e))

    try:
        if degraded is None:
            n = record_picks(result_df, scan_mode)
        else:
            n = 0
        filled = backfill_outcomes()
        print("  [ledger] recorded {} picks, backfilled {} outcomes".format(n, filled))
    except Exception as e:
        print("  [ledger] skipped: {}".format(e))

    # F04 regression found by the 2026-09-14 column audit: only today's
    # candidates get fetched, so a name that dropped off the list stops
    # updating and its trailing closes go null in quotes.json -- 20 of the 95
    # stocks picked in the previous 30 sessions were unpriced, one for 18
    # sessions. A holder of a dropped-out name lost its valuation exactly when
    # the exit decision mattered. Top up every recent pick that is behind.
    quotes_meta = {}
    try:
        from scanner.quote_feed import refresh_tracked_prices
        topup = refresh_tracked_prices(scan_mode, session_date)
        if topup.get("fetched"):
            print("  [quotes] refreshed {} dropped-out name(s) so holdings "
                  "stay priced".format(topup["fetched"]))
        ended = topup.get("source_ended") or {}
        if ended:
            # Still behind after asking the sources again: halted or delisted,
            # not a refresh failure. Published so the checker and the phone can
            # say so instead of calling it a feed gap.
            quotes_meta["source_ended"] = ended
            print("  [quotes] {} name(s) have no newer bar at any source "
                  "(halted/delisted?): {}".format(
                      len(ended), ", ".join("{}@{}".format(k, v or "?")
                                            for k, v in sorted(ended.items()))))
    except Exception as e:
        print("  [quotes] straggler refresh skipped: {}".format(e))

    # The top-up runs AFTER verify_candidates cleaned the store, and it asks
    # the same feed, so it can bring a placeholder bar back in for the handful
    # of names it fetched (observed: 3 rows on the 2026-09-20 re-run). The
    # readers are guarded, but the store is what CI caches for tomorrow, so
    # take them out again here -- one query, and it keeps the cache honest.
    try:
        from scanner.data_integrity import purge_nonsession_bars
        again = purge_nonsession_bars()
        if again.get("rows"):
            print("  [data] purged {} placeholder bar(s) the top-up re-added "
                  "({})".format(again["rows"], ", ".join(again["dates"])))
    except Exception as e:
        print("  [data] post-top-up purge skipped: {}".format(str(e)[:80]))

    # Publish FIRST, summarise second (F24). The AI call used to run before the
    # export, so a hung Gemini request delayed -- and an unconverted
    # requests.Timeout could skip past -- the market data and the user's own
    # position prices. Nobody's holdings should wait on a language model.
    data_health = _data_health(result_df, session_date, degraded)
    if data_health.get("data_lag"):
        print("  [health] data may be behind: have {} expected {}".format(
            data_health.get("session_date"),
            data_health.get("expected_session")))

    try:
        path = export_scan_result(result_df, scan_mode, degraded=degraded,
                                  session_date=session_date,
                                  strategy_version=STRATEGY_VERSION,
                                  quality=data_health, quotes_meta=quotes_meta)
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
                               strategy_version=STRATEGY_VERSION,
                               quality=data_health, quotes_meta=quotes_meta)
            print("  [ai] {} report(s) attached".format(len(reports)))
        except Exception as e:
            print("  [ai] attach failed, prices already published: {}".format(e))

    # Last: audit every column of what was just published and stamp the
    # verdict into meta.checks (scanner/result_checks.py). Runs after the AI
    # pass because that pass rewrites the file. The workflow runs the same
    # check again as its own step for the annotations; both are idempotent.
    try:
        from config.settings import (MOBILE_DATA_FILE, MOBILE_QUOTES_FILE,
                                     SIGNAL_LEDGER_FILE, SCAN_CHECKS_FILE)
        from scanner.result_checks import check_files, format_report
        report = check_files(MOBILE_DATA_FILE, quotes_path=MOBILE_QUOTES_FILE,
                             recs_path=RECOMMENDATIONS_EXPORT_FILE,
                             ledger_path=SIGNAL_LEDGER_FILE,
                             history_path=SCAN_CHECKS_FILE,
                             expected_session=data_health.get("expected_session"))
        print(format_report(report))
    except Exception as e:
        print("  [checks] skipped: {}".format(e))

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
