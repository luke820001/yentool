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
left in place; 2 the scan crashed; 3 the session's list is already final, so
nothing was rescanned or republished (scanner/list_freeze); 4 the same, but
the evening restriction lists changed the published restriction columns and
the amended payload must be republished. Any single stage failure is logged
but does not abort the run, matching the GUI's resilience.
"""
import sys
import threading
import traceback

from config.settings import PORTFOLIO_LEDGER_FILE, RECOMMENDATIONS_EXPORT_FILE
from scanner.market_filter import get_candidate_list, get_feed_health
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import (
    apply_scan_mode, add_trade_columns, sort_for_mode, select_with_hysteresis,
    mark_buy_ready, STRATEGY_VERSION,
)
from scanner.scan_state import load_state, prior_for, save_state
from scanner.signal_ledger import (record_picks, backfill_outcomes,
                                   final_list_session, record_list_session)
from scanner import list_freeze
from scanner.holding_tracker import annotate_holding
from scanner.result_export import export_scan_result
from portfolio.sync import (attach_recommendations, summarize,
                            advance_recommendations, summarize_advance,
                            load_rec_anchors, rec_meta)
from portfolio.publish import seed_from_export, export_recommendations
from scanner.trade_restrictions import (
    fetch_restrictions, annotate_restrictions,
    summarize as summarize_restrictions, describe as describe_restrictions,
)

# _data_health's default for a caller that never fetched restrictions (the
# summary is then left out, rather than claiming a fetch that crashed)
_NOT_FETCHED = object()

# run_scan's answer when the session's list is already final (list_freeze):
# nothing was rescanned, nothing persistent was written. AMENDED: the same,
# except the evening restriction lists changed the published restriction
# columns and the amended payload is in mobile/ for the workflow to upload.
FROZEN = "frozen"
AMENDED = "amended"
EXIT_FROZEN = list_freeze.EXIT_FROZEN       # 3
EXIT_AMENDED = list_freeze.EXIT_AMENDED     # 4


class _FreezeContext(object):
    """What the freeze gates need, read once per run: the force flag and the
    published payload on Pages (fetched lazily, at most once -- both gates and
    the amend reuse it)."""

    def __init__(self, scan_mode):
        self.scan_mode = scan_mode
        self.forced = list_freeze.force_requested()
        self.base_url = list_freeze.pages_url()
        self._pages = None
        self._fetched = False

    def pages(self):
        if not self._fetched:
            self._fetched = True
            if self.base_url:
                self._pages = list_freeze.fetch_pages(self.base_url)
                if self._pages is None:
                    print("  [freeze] Pages unreachable ({}); trusting the "
                          "ledger".format(self.base_url))
        return self._pages

    def status(self):
        return list_freeze.pages_status(self.pages())


def _say_decision(dec, stage):
    if dec.get("warning"):
        print("::warning::{}".format(dec["warning"]))
    if dec["action"] == list_freeze.ACTION_FROZEN:
        print("  [freeze] {} gate: the list of {} is already final ({}); "
              "nothing is rescanned or republished".format(
                  stage, dec.get("session") or "?", dec.get("reason")))
    elif dec.get("revised_reason"):
        print("  [freeze] {} gate: rebuilding the final list of {} "
              "(revised_reason {})".format(stage, dec.get("session"),
                                           dec["revised_reason"]))


def _pre_gate(scan_mode, ctx):
    """Before any fetch: is the session the wall clock expects already final?
    An optimisation only -- _latest_trading_day knows weekends but not
    holidays (on one it names a day with no bars, nothing matches, and the
    post-scan gate decides). Never uses the holiday calendar: a false holiday
    there would freeze a real trading day. Returns the decision or None."""
    if ctx.forced:
        print("  [freeze] force_rescan requested: the freeze is bypassed")
        return None
    try:
        from scanner.chip_verifier import _latest_trading_day
        want = _latest_trading_day()
    except Exception:
        return None
    if not want:
        return None
    row = final_list_session(scan_mode)
    session = want
    if row and str(row.get("scan_session") or "") >= want:
        session = str(row["scan_session"])
    led = row if row and str(row.get("scan_session")) == session else None
    dec = list_freeze.decide(session, led, ctx.status(), forced=False)
    if dec["action"] == list_freeze.ACTION_FROZEN:
        _say_decision(dec, "pre-scan")
    return dec


def _post_gate(scan_mode, session, ctx):
    """After the scan, before anything persistent (held ids, recommendations,
    picks, export): the authoritative check on the DATA session.

    A scan with no bar date at all (nothing verified) cannot say which
    session it is. It is judged as the newest FINAL list (ledger or Pages):
    an empty provisional payload must never replace a final list -- on a
    holiday the clock does not know it would, and save_held_ids([]) would
    wipe the held set. Forced runs skip this, as they skip every gate."""
    session = str(session or "")[:10]
    inferred = False
    if not session and not ctx.forced:
        known = []
        row = final_list_session(scan_mode)
        if row and row.get("scan_session"):
            known.append(str(row["scan_session"])[:10])
        pg = ctx.status()
        if pg and pg.get("state") == list_freeze.STATE_FINAL and pg.get("session"):
            known.append(str(pg["session"])[:10])
        if known:
            session = max(known)
            inferred = True
            print("  [freeze] the scan has no bar date; judged as the newest "
                  "final list ({})".format(session))
    dec = list_freeze.decide(session,
                             final_list_session(scan_mode, session) if session
                             else None,
                             ctx.status(), forced=ctx.forced)
    if inferred and dec["action"] != list_freeze.ACTION_FROZEN:
        # A scan with no bar date has nothing to rebuild the final list FROM:
        # pages_behind (an earlier deploy never landed) would publish an empty
        # provisional payload over it and wipe the held set. Leave it to the
        # next healthy run, which sees the same pages_behind with real data.
        dec = dict(dec, action=list_freeze.ACTION_FROZEN, reason="no_bar_date",
                   revised_reason=None, prev=None,
                   warning="the scan has no bar date; not rebuilding the final "
                           "list of {} from it ({})".format(session,
                                                            dec.get("reason")))
    _say_decision(dec, "post-scan")
    return dec


def _frozen_exit(scan_mode, ctx):
    """A frozen run's one remaining job: the restriction lists the exchanges
    publish in the evening (list_freeze.amend_restrictions). AMENDED when
    the published payload was amended, FROZEN otherwise -- including when
    Pages is unreachable (nothing to amend from)."""
    try:
        payload = ctx.pages()
        if payload is None:
            print("  [amend] no published payload to amend; done")
            return FROZEN
        session = str((payload.get("meta") or {}).get("session_date") or "")[:10]
        expected = None
        try:
            from scanner.chip_verifier import _latest_trading_day
            expected = _latest_trading_day()
        except Exception:
            pass
        outcome, detail = list_freeze.amend_restrictions(
            payload, scan_mode, base_url=ctx.base_url,
            ledger_row=final_list_session(scan_mode, session) if session else None,
            expected_session=expected)
        print("  [amend] {}: {}".format(outcome, detail))
        return AMENDED if outcome == list_freeze.AMENDED else FROZEN
    except Exception as e:
        print("  [amend] skipped: {}".format(str(e)[:120]))
        return FROZEN


def _record_list_session(scan_mode, result_df, degraded):
    """After check_files stamped meta.list_status: a FINAL list of a clean run
    becomes the session's freeze marker in the ledger (committed by the
    workflow). Provisional / degraded runs write nothing -- the next run may
    still replace them."""
    import json as _json
    from config.settings import MOBILE_DATA_FILE
    try:
        with open(MOBILE_DATA_FILE, encoding="utf-8") as f:
            block = ((_json.load(f).get("meta") or {}).get("list_status") or {})
    except Exception as e:
        print("  [freeze] no list status to record: {}".format(e))
        return None
    if degraded is not None or block.get("state") != list_freeze.STATE_FINAL:
        print("  [freeze] list of {} is {} ({}): not frozen, a later run may "
              "replace it".format(block.get("session"), block.get("state"),
                                  ",".join(block.get("reasons") or []) or
                                  "degraded"))
        return None
    ids = []
    if result_df is not None and not result_df.empty \
            and "Buy_Ready" in result_df.columns:
        ids = [str(s) for s, b in zip(result_df["Stock_ID"], result_df["Buy_Ready"])
               if b is not None and b == b and bool(b)]
    got = record_list_session(scan_mode, block,
                              rows=0 if result_df is None else len(result_df),
                              buy_ready_ids=ids)
    print("  [freeze] list of {} is final (revision {}{}): ledger marker {}".format(
        block.get("session"), block.get("revision"),
        ", " + block["revised_reason"] if block.get("revised_reason") else "",
        got))
    return got


def _progress(rank, total, stock_id):
    # Print sparse progress so CI logs stay readable.
    if total and (rank == total or rank % 50 == 0):
        print("  scanning {}/{} ({})".format(rank, total, stock_id))


# Company events (ingestion/company_events, display only): the refresh runs
# in a daemon thread from the post-gate to the export, so its few exchange
# requests overlap the rest of the scan. Its own budget is 60 s; the join
# waits a little longer, then publishes without it (the columns go null and
# meta.events is carried forward).
EVENTS_JOIN_S = 90


def _start_events_refresh():
    """(thread, box): box["events"] is set when the refresh finishes."""
    box = {}

    def run():
        try:
            from ingestion.company_events import refresh
            box["events"] = refresh()
        except Exception as e:
            print("  [events] refresh skipped: {}".format(str(e)[:100]))

    t = threading.Thread(target=run, name="company-events", daemon=True)
    t.start()
    return t, box


def _join_events(job, timeout=EVENTS_JOIN_S):
    """The refreshed events dict, or None (not started, failed, too slow)."""
    if not job:
        return None
    t, box = job
    t.join(timeout)
    if t.is_alive():
        print("  [events] refresh still running after {} s; published "
              "without it".format(timeout))
        return None
    return box.get("events")


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


def _prepare_recommendations(scan_mode, session_date, degraded):
    """Recommendation step 1 (before the cards are drawn): restore the ledger
    from the published export, move every active recommendation through its
    lifecycle, and read the anchors the cards hang off.

    CI starts with no ledger (the database is local and gitignored), so the
    recommendations are restored from data/recommendations.json first --
    additive, plus terminal statuses carried forward. advance_recommendations
    runs on non-degraded runs only: a degraded run writes nothing (P0-3).
    Returns (rec_anchors, advance_stats or None)."""
    ledger, export = PORTFOLIO_LEDGER_FILE, RECOMMENDATIONS_EXPORT_FILE
    try:
        report = {}
        seeded = seed_from_export(ledger, export, report=report)
        if seeded or report.get("synced"):
            print("  [rec] seeded {} recommendation(s) from the published export"
                  ", {} status(es) carried forward".format(
                      seeded, report.get("synced", 0)))
    except Exception as e:
        print("  [rec] seed skipped: {}".format(e))
    adv = None
    if degraded is None and session_date:
        try:
            adv = advance_recommendations(ledger, scan_mode, session_date,
                                          STRATEGY_VERSION)
            print("  [rec] {}".format(summarize_advance(adv)))
        except Exception as e:
            print("  [rec] advance skipped: {}".format(e))
    anchors = load_rec_anchors(ledger, scan_mode, session_date)
    return anchors, adv


def _create_recommendations(result_df, scan_mode, session_date, degraded,
                            anchors):
    """Recommendation step 2 (after mark_buy_ready): freeze the first-day
    recommendation for anything that qualified today, withdraw what an earlier
    run of this session recommended and this one no longer does, and hang the
    frozen columns off every row. A degraded run attaches read-only.

    A recommendation created now is anchored on today, so the rows are drawn
    again with the new anchors: their cards then show the new trade (pending,
    the recommendation's stop) instead of whatever older segment the streak
    held. Buy_Ready / Buy_Block / the recommendation columns are not touched by
    that pass. Returns (result_df, anchors, attach_stats)."""
    from scanner.market_calendar import entry_session_after
    ledger = PORTFOLIO_LEDGER_FILE
    writes = degraded is None
    stats = {"created": 0, "attached": 0, "writes": writes, "created_ids": []}
    try:
        nxt = entry_session_after(session_date) if session_date else None
        result_df, stats = attach_recommendations(
            result_df, scan_mode, STRATEGY_VERSION, ledger,
            session_date=session_date, next_session=nxt, allow_writes=writes)
        print("  [rec] {}".format(summarize(stats)))
    except Exception as e:
        print("  [rec] skipped: {}".format(e))
    if stats.get("created_ids") or stats.get("superseded"):
        try:
            anchors = load_rec_anchors(ledger, scan_mode, session_date)
            result_df = annotate_holding(result_df, scan_mode,
                                         rec_anchors=anchors)
            print("  [rec] re-anchored the cards of {} new recommendation(s)"
                  .format(len(stats.get("created_ids") or [])))
        except Exception as e:
            print("  [rec] re-anchor skipped: {}".format(e))
    return result_df, anchors, stats


def _finish_recommendations(tracked_df, scan_mode, session_date, degraded):
    """Recommendation step 3: the frozen columns on the dropped-out rows (the
    ones a HOLDER reads; read-only, nothing is ever created there), then the
    public export -- non-degraded runs only. Returns tracked_df."""
    ledger, export = PORTFOLIO_LEDGER_FILE, RECOMMENDATIONS_EXPORT_FILE
    try:
        if tracked_df is not None and not tracked_df.empty:
            tracked_df, _tstats = attach_recommendations(
                tracked_df, scan_mode, STRATEGY_VERSION, ledger,
                session_date=session_date, allow_writes=False)
    except Exception as e:
        print("  [rec] tracked attach skipped: {}".format(e))
    if degraded is not None:
        print("  [rec] degraded run: recommendations attached read-only, "
              "export skipped")
        return tracked_df
    # Re-publish the recommendations-only view. This -- not the database --
    # is what gets committed to the public repo.
    try:
        n = export_recommendations(ledger, export)
        print("  [rec] exported {} recommendation(s) -> {}".format(
            n, export.name))
    except Exception as e:
        print("  [rec] export skipped: {}".format(e))
    return tracked_df


def _data_health(df, session_date, degraded, restr_info=_NOT_FETCHED,
                 tracked=None):
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

    `restr_info` (fetch_restrictions' result, None when the fetch crashed)
    adds health["restrictions"] = trade_restrictions.summarize(...): the
    feeds' state and the counts over `df` / `tracked`. result_checks errors
    (restrictions_unchecked) when a payload with rows has none.
    """
    health = {"session_date": session_date, "rows": 0 if df is None else len(df),
              "degraded": degraded}
    if restr_info is not _NOT_FETCHED:
        try:
            health["restrictions"] = summarize_restrictions(restr_info, df,
                                                            tracked)
        except Exception as e:
            health["restrictions"] = {"ok": False, "boards": {},
                                      "error": str(e)[:120]}
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


# Market views in generation order. OTC first: it is the only market the rule
# buys, so it gets the first claim on the run's retry budget (P1-7).
AI_REPORT_ORDER = ("OTC", "ALL", "TSE")


def build_market_reports(df, sources=None):
    """Pre-generate one AI report per market view (ALL/OTC/TSE) so the phone can
    show the report matching its current filter -- exactly the desktop behaviour
    of "summarize what is on screen" (e.g. OTC filter -> only the OTC names go to
    the model). With no GEMINI/GROQ key set, generate_report falls back to a local
    text summary, so the phone always has something. Never aborts the scan.

    Returns {market: text}. When `sources` (a dict) is given it is filled with
    {market: {source, model, attempts, seconds, error}} -- published as
    meta.report_sources so the phone can mark a template. All views share
    ONE gemini_client.RetryBudget, so retries add at most RETRY_BUDGET_S
    (90 s) to the run however many reports fail (2026-10-08, plan P1-7)."""
    reports = {}
    try:
        from gemini_hook.gemini_client import generate_report_meta, RetryBudget
    except Exception as e:
        print("  [ai] report module unavailable: {}".format(e))
        return reports

    # The full-list position (what mark_buy_ready ranks), stamped before the
    # split so the OTC report can say "list #23" where its own order says 5.
    if df is not None:
        df = df.copy()
        df["List_Pos"] = range(1, len(df) + 1)
    views = {"ALL": df}
    if df is not None and "Market" in df.columns:
        for mkt in ("OTC", "TSE"):
            sub = df[df["Market"].astype(str) == mkt]
            if not sub.empty:
                views[mkt] = sub
    budget = RetryBudget()
    for name in [m for m in AI_REPORT_ORDER if m in views]:
        sub = views[name]
        try:
            res = generate_report_meta(sub, budget=budget)
            reports[name] = res["text"]
            if isinstance(sources, dict):
                sources[name] = {k: res.get(k) for k in
                                 ("source", "model", "attempts", "seconds",
                                  "error")}
            print("  [ai] {} report: {} in {}s ({} attempt(s), {} names)".format(
                name, res.get("source"), res.get("seconds"),
                res.get("attempts"), len(sub)))
        except Exception as e:
            print("  [ai] {} report skipped: {}".format(name, str(e)[:80]))
    if budget.retries:
        print("  [ai] {} retr{} used {:.0f}s of the {:.0f}s retry budget".format(
            budget.retries, "y" if budget.retries == 1 else "ies",
            budget.spent, budget.spent + max(0.0, budget.left)))
    # Generated OTC-first, published in the usual ALL / OTC / TSE order.
    return {k: reports[k] for k in ("ALL", "OTC", "TSE") if k in reports}


def run_scan(scan_mode="mode_prelaunch"):
    print("=== headless scan: {} ===".format(scan_mode))

    # Final-once (scanner/list_freeze): a session whose list is already
    # final is not rescanned. This first check needs no market data.
    freeze = _FreezeContext(scan_mode)
    pre = _pre_gate(scan_mode, freeze)
    if pre is not None and pre["action"] == list_freeze.ACTION_FROZEN:
        return _frozen_exit(scan_mode, freeze)

    # Keep the WHOLE market's daily bar current, not just the shortlist. One
    # extra request per exchange; without it, any stock outside the turnover
    # pool slowly goes stale and nothing can be computed for a position in it.
    snapshot_frame = None
    try:
        from config.settings import PRICE_VOLUME_FILE as _PV
        from scanner.market_snapshot import backfill_history, refresh_market
        _rows, _snap_health = refresh_market(_PV)
        snapshot_frame = _snap_health.get("frame")
        # The snapshot gives every instrument TODAY's bar and nothing else, so
        # a holding in one still has no averages. Fill history for a slice of
        # the under-covered names each scan, liquid ones first; the whole
        # market is covered within days rather than after three months of
        # snapshots. Names that repeatedly cannot be fetched are remembered
        # and skipped so the budget goes to the ones that can.
        backfill_history(_PV)
    except Exception as e:
        print("  [snapshot] skipped: {}".format(str(e)[:100]))

    # The OTC index next to TAIEX (ingestion/otc_index -> taiex.db table
    # 'TPEX'), for the live record's benchmark. Display only; a failure
    # leaves the benchmark's OTC side null.
    try:
        from ingestion.otc_index import refresh as refresh_otc_index
        refresh_otc_index()
    except Exception as e:
        print("  [tpex-index] skipped: {}".format(str(e)[:100]))

    # Session-aware hysteresis state (P0-5b): the held set of the last run
    # and the set its session started from. Both are carried through the
    # scan, so a same-session rerun still verifies yesterday's held names.
    state = load_state(scan_mode)
    # Force-include everything the ledger picked recently, not just the names
    # hysteresis is still holding. A stock the owner bought four days ago can
    # have left the top 80 AND the top-300 turnover pool, and then nothing --
    # not its chips, not its moving averages, not its exit plan -- would be
    # computed for it. See scanner/tracked_rows.py.
    tracked_ids = set()
    tracked_picks = {}
    try:
        from scanner.tracked_rows import recent_pick_ids
        from config.settings import PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE
        # {stock_id: last pick date} -- the dates are what lets the per-scan
        # cap drop the OLDEST names rather than the least heavily traded.
        tracked_picks = recent_pick_ids(PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE,
                                        scan_mode, with_dates=True)
        tracked_ids = set(tracked_picks)
        if tracked_ids:
            print("  [tracked] carrying {} recently recommended name(s) "
                  "through the scan".format(len(tracked_ids)))
    except Exception as e:
        print("  [tracked] id lookup skipped: {}".format(e))
    candidates = get_candidate_list(
        scan_mode=scan_mode,
        include_ids=(set(state["held_ids"]) | set(state["prior_ids"] or [])
                     | tracked_ids))
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

    verified = verify_candidates(candidates, progress_callback=_progress)

    # The candidate and backfill fetchers have now run, and they store a
    # DIVIDEND-ADJUSTED series while the whole-market snapshot stores the
    # price the exchange published. They write the same table and they run
    # last, so without this the adjusted value wins for dates the exchange has
    # also published -- a step in the stored series the size of the dividend,
    # which every moving average reads as a move. See
    # scanner/market_snapshot.reassert_exchange_prices.
    try:
        if snapshot_frame is not None and not snapshot_frame.empty:
            from scanner.market_snapshot import reassert_exchange_prices
            from config.settings import PRICE_VOLUME_FILE as _PV2
            reassert_exchange_prices(_PV2, snapshot_frame)
    except Exception as e:
        print("  [snapshot] price reassertion skipped: {}".format(str(e)[:80]))
    result_df = apply_scan_mode(verified, scan_mode)
    result_df = sort_for_mode(result_df, scan_mode)

    # The authoritative freeze check, on the DATA session, before anything
    # persistent: no held ids, recommendations, picks or export yet. A day
    # where nothing qualified still has a session: the verified bars say it.
    session_pre = (_session_date(result_df) or _session_date(verified)
                   or _session_date(candidates))
    gate = _post_gate(scan_mode, session_pre, freeze)
    if gate["action"] == list_freeze.ACTION_FROZEN:
        return _frozen_exit(scan_mode, freeze)
    revised_reason, list_prev = gate.get("revised_reason"), gate.get("prev")

    prior_ids = prior_for(state, session_pre)
    ranked_df = result_df
    result_df, held_ids = select_with_hysteresis(ranked_df, prior_ids)
    session_date = _session_date(result_df)
    if session_date and session_date != session_pre:
        # The gate, the hysteresis state and the export must name ONE
        # session. The gate's date came from every ranked row; the published
        # list is the selection, and a leading bar on a name the selection
        # dropped (a partial bar of an open market, a forced rescan at 09:30)
        # made the two differ -- the freeze marker was looked up under a
        # session nothing is published for, a force_rescan lost its revision
        # stamp, and the state was saved under the wrong session. Judge and
        # select again on the published session (a pure re-run: nothing has
        # been written yet).
        print("  [freeze] the ranked rows reach {} but the list is of {}; "
              "judging {}".format(session_pre, session_date, session_date))
        session_pre = session_date
        gate = _post_gate(scan_mode, session_pre, freeze)
        if gate["action"] == list_freeze.ACTION_FROZEN:
            return _frozen_exit(scan_mode, freeze)
        revised_reason, list_prev = gate.get("revised_reason"), gate.get("prev")
        prior_ids = prior_for(state, session_pre)
        result_df, held_ids = select_with_hysteresis(ranked_df, prior_ids)
        session_date = _session_date(result_df) or session_date

    # Company events (display only) refresh in the background from here; a
    # frozen run never reaches this line, so it never rewrites the cache.
    events_job = None
    try:
        events_job = _start_events_refresh()
    except Exception as e:
        print("  [events] not started: {}".format(e))

    if degraded is None:
        save_state(scan_mode, session_pre, held_ids, prior_ids)
    result_df = add_trade_columns(result_df, scan_mode)

    # Recommendations first (2026-10-08): restore, advance and read the
    # anchors BEFORE the cards are drawn -- a name with a live recommendation
    # gets the card of THAT trade, not of whatever older streak it had.
    rec_anchors, rec_advance = _prepare_recommendations(
        scan_mode, session_date, degraded)

    try:
        result_df = annotate_holding(result_df, scan_mode,
                                     rec_anchors=rec_anchors)
    except Exception as e:
        print("  [holding] skipped: {}".format(e))

    # Trade restrictions (2026-10-08, plan P0-1): disposition / altered /
    # suspended / attention lists, fetched once and reused for the tracked
    # rows. Display + order guidance + checks; only the kinds in
    # trade_restrictions.BLOCKING_RESTRICTIONS block a buy. Fetched on a
    # degraded run too -- nothing is written from it, and a holder still
    # needs to know the name is in disposition. A crashed fetch leaves every
    # row "unknown" (shown, and warned about in the checks).
    restr_info = None
    try:
        restr_info = fetch_restrictions(session_date)
    except Exception as e:
        print("  [restrict] fetch skipped: {}".format(str(e)[:100]))
    try:
        result_df = annotate_restrictions(result_df, restr_info, session_date)
    except Exception as e:
        print("  [restrict] annotate skipped: {}".format(str(e)[:100]))

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

    # Freeze today's new recommendations (and re-draw their cards).
    result_df, rec_anchors, rec_attach = _create_recommendations(
        result_df, scan_mode, session_date, degraded, rec_anchors)

    # Chip verdict for tomorrow (2026-09-21): today's three-institution flow
    # read against the validated rule, per held row. After the cards are
    # final (needs Hold_Status / Exit_Signal of the re-anchored trade) and
    # independent of the buy gate.
    try:
        from scanner.chip_signal import annotate_chip_action
        result_df = annotate_chip_action(result_df, scan_mode)
        if "Chip_Action" in result_df.columns:
            acts = result_df["Chip_Action"].value_counts().to_dict()
            print("  [chips] verdicts: {}".format(
                {k: int(v) for k, v in acts.items() if k}))
    except Exception as e:
        print("  [chips] skipped: {}".format(e))

    # Full rows for names that dropped off the list but could still be held.
    tracked_df = None
    try:
        from scanner.tracked_rows import annotate_tracked, split_tracked
        tracked_df = split_tracked(verified, result_df, tracked_ids,
                                   picked_on=tracked_picks)
        if tracked_df is not None and not tracked_df.empty:
            tracked_df = annotate_tracked(tracked_df, scan_mode,
                                          rec_anchors=rec_anchors)
            print("  [tracked] published full data for {} dropped-out "
                  "name(s)".format(len(tracked_df)))
    except Exception as e:
        print("  [tracked] skipped: {}".format(e))
    # the same restriction lists on the dropped-out rows a holder reads
    try:
        if tracked_df is not None and not tracked_df.empty:
            tracked_df = annotate_restrictions(tracked_df, restr_info,
                                               session_date)
    except Exception as e:
        print("  [restrict] tracked annotate skipped: {}".format(str(e)[:100]))

    # Freeze the first-day recommendation (done above) and hang the frozen
    # price off every row we already have one for. After this,
    # Suggested_Buy_Price (recomputed daily) and Initial_Buy_Price (fixed)
    # are both on the row and can never again be mistaken for each other
    # (F01). The dropped-out rows get the same columns, read-only; the
    # export is skipped on a degraded run.
    tracked_df = _finish_recommendations(tracked_df, scan_mode, session_date,
                                         degraded)

    try:
        if degraded is None:
            n = record_picks(result_df, scan_mode,
                             scan_session=session_date or None)
        else:
            n = 0
        filled = backfill_outcomes()
        print("  [ledger] recorded {} picks, backfilled {} outcomes".format(n, filled))
    except Exception as e:
        print("  [ledger] skipped: {}".format(e))

    # What the shipped rule has actually done on the signals this scanner
    # published (scanner/live_record.py), shown on the phone next to the
    # backtest. Observes only; a failure keeps the previous block.
    live_record = None
    try:
        from scanner.live_record import build_live_record
        # by_sid is limited to the names this payload carries
        sids = set()
        for frame in (result_df, tracked_df):
            if frame is not None and not frame.empty and "Stock_ID" in frame.columns:
                sids.update(str(s) for s in frame["Stock_ID"])
        live_record = build_live_record(sids=sids)
        t = live_record.get("tradable") or {}
        print("  [record] since {}: {} tradable closed, win {}%, mean {}%, "
              "{} open".format(live_record.get("since"), t.get("closed"),
                               t.get("win_pct"), t.get("mean_pct"), t.get("open")))
    except Exception as e:
        print("  [record] skipped: {}".format(e))

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

    # Compact data for the WHOLE market, so a holding the scanner never
    # recommended still has moving averages, levels and chips to show. Its own
    # file: the phone fetches it only when one of its positions is missing
    # from the list. See scanner/universe_export.py.
    try:
        import json as _json
        from config.settings import (MOBILE_UNIVERSE_FILE, PRICE_VOLUME_FILE,
                                     STOCK_NAMES_FILE)
        from scanner.universe_export import export as export_universe
        from ingestion.inst_trades import get_inst_features
        try:
            _names = _json.load(open(STOCK_NAMES_FILE, encoding="utf-8"))
        except Exception:
            _names = {}
        # Institutional flow for the WHOLE published set, not just the
        # shortlist. The call was passing a hard-coded null here -- with
        # get_inst_features imported and never used -- so every chip field in
        # universe_export.build was dead code and a holding the scanner never
        # picked was told, forever, that the scan had no institutional data
        # for it. That is half of what the module exists for. Found
        # 2026-09-21 by audit.
        try:
            _inst = get_inst_features(sorted(_names.keys())) if _names else {}
        except Exception as _e:
            _inst = {}
            print("  [universe] institutional flow unavailable: {}"
                  .format(str(_e)[:80]))
        _built = export_universe(MOBILE_UNIVERSE_FILE, PRICE_VOLUME_FILE,
                                 names=_names, inst=_inst,
                                 session_date=session_date, scan_mode=scan_mode)
        print("  [universe] {} stock(s) published for holdings lookup"
              " ({} with institutional flow)".format(_built, len(_inst)))
    except Exception as e:
        print("  [universe] skipped: {}".format(e))

    # Publish FIRST, summarise second (F24). The AI call used to run before the
    # export, so a hung Gemini request delayed -- and an unconverted
    # requests.Timeout could skip past -- the market data and the user's own
    # position prices. Nobody's holdings should wait on a language model.
    # Company events (ingestion/company_events): nine DISPLAY-ONLY columns
    # (Rev_* / Ex_* / Conf_Date) added here, after every score, gate,
    # recommendation and ledger write has run, so nothing upstream can read
    # them. A refresh that failed still adds the columns (null).
    events_meta = None
    try:
        from ingestion.company_events import annotate_events, meta_block
        events = _join_events(events_job)
        result_df = annotate_events(result_df, events, session_date)
        if tracked_df is not None and not tracked_df.empty:
            tracked_df = annotate_events(tracked_df, events, session_date)
        if events is not None:
            events_meta = meta_block(events, session_date, result_df,
                                     tracked_df)
    except Exception as e:
        print("  [events] skipped: {}".format(str(e)[:100]))

    data_health = _data_health(result_df, session_date, degraded,
                               restr_info=restr_info, tracked=tracked_df)
    if data_health.get("restrictions"):
        print("  " + describe_restrictions(data_health["restrictions"]))
    if data_health.get("data_lag"):
        print("  [health] data may be behind: have {} expected {}".format(
            data_health.get("session_date"),
            data_health.get("expected_session")))

    try:
        path = export_scan_result(result_df, scan_mode, degraded=degraded,
                                  session_date=session_date,
                                  strategy_version=STRATEGY_VERSION,
                                  quality=data_health, quotes_meta=quotes_meta,
                                  tracked=tracked_df, live_record=live_record,
                                  rec_stats=rec_meta(rec_attach, rec_advance),
                                  events_meta=events_meta)
        print("  [export] scan result -> {}".format(path))
    except Exception as e:
        print("  [export] failed: {}".format(e))

    # Per-market AI reports for the phone (matches the desktop "summarize what is
    # shown" behaviour; OTC filter -> only OTC names sent to the model). Written
    # as a second pass over the already-published payload, so a failure here
    # leaves the prices and the P&L intact and merely omits the commentary.
    report_sources = {}
    reports = build_market_reports(result_df, sources=report_sources)
    if reports:
        try:
            export_scan_result(result_df, scan_mode, reports=reports,
                               report_sources=report_sources,
                               degraded=degraded, session_date=session_date,
                               strategy_version=STRATEGY_VERSION,
                               quality=data_health, quotes_meta=quotes_meta,
                               tracked=tracked_df, live_record=live_record,
                               rec_stats=rec_meta(rec_attach, rec_advance),
                               events_meta=events_meta)
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
                             expected_session=data_health.get("expected_session"),
                             revised_reason=revised_reason, list_prev=list_prev)
        print(format_report(report))
    except Exception as e:
        print("  [checks] skipped: {}".format(e))

    # The list check_files just judged final is this session's freeze marker.
    try:
        _record_list_session(scan_mode, result_df, degraded)
    except Exception as e:
        print("  [freeze] marker skipped: {}".format(e))

    print("=== done: {} picks ===".format(len(result_df)))
    return result_df


def main():
    """Exit codes distinguish the outcomes CI has to tell apart (F18).

        0  the scan ran and published -- INCLUDING a legitimate zero-pick day.
           Nothing qualified, we said so, the phone shows "0 setups today".
        1  the market feed was unreachable, so there is no result to publish
           and the previous data must be left in place.
        2  the scan itself crashed.
        3  EXIT_FROZEN: the session's list is already final (list_freeze);
           nothing was rescanned or republished. Not an error.
        4  EXIT_AMENDED: frozen, but the evening restriction lists changed
           the published restriction columns; the amended payload is in
           mobile/ and must be uploaded (published, like 0).

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
    if isinstance(out, str):
        if out == AMENDED:
            print("=== list already final; restriction info amended, "
                  "republish ===")
            sys.exit(EXIT_AMENDED)
        print("=== list already final: nothing rescanned or republished ===")
        sys.exit(EXIT_FROZEN)
    if out.empty:
        print("=== healthy scan, 0 qualifying names: published count=0 ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
