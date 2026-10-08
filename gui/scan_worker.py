import threading
import pandas as pd
from scanner.market_filter import (
    get_candidate_list, get_feed_health, lookup_stock_info,
)
from scanner.chip_verifier import verify_candidates
from scanner.scan_mode import (
    apply_scan_mode, add_trade_columns, sort_for_mode, select_with_hysteresis,
    mark_buy_ready,
)
from scanner.scan_state import load_state, prior_for, save_state
from scanner.signal_ledger import record_picks, backfill_outcomes
from scanner.holding_tracker import annotate_holding
from scanner.result_export import export_scan_result
from ingestion.price_volume_multi import resolve_market


class ScanWorker:

    def __init__(self, on_progress, on_result, on_error, on_done,
                 scan_mode="mode_squeeze", on_notice=None):
        self._on_progress = on_progress
        self._on_result   = on_result
        self._on_error    = on_error
        self._on_done     = on_done
        # Feed-health notice (degraded string, or None when healthy). Optional so
        # callers that do not render it are unaffected.
        self._on_notice   = on_notice
        self._scan_mode   = scan_mode
        self._thread      = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _notify(self, degraded):
        """Best effort: a UI notice must never be able to abort a scan."""
        if self._on_notice is None:
            return
        try:
            self._on_notice(degraded)
        except Exception as e:
            print("  [notice] skipped: {}".format(e))

    def _run(self):
        try:
            self._on_progress(0, 1, "Fetching market overview...")
            # Names held from the previous run are force-included in the
            # candidate pool so the hysteresis layer can actually retain them
            # even if their single-day volume slipped below the prefilter cap.
            # Session-aware like scan_headless (scanner/scan_state, P0-5b):
            # the set the stored session started from is carried too, so a
            # rerun of the same session reproduces that session's list
            # instead of stacking hysteresis on its own output.
            state = load_state(self._scan_mode)
            prior_ids = set(state["held_ids"]) | set(state["prior_ids"] or [])
            # Names the ledger picked recently are force-included too, exactly
            # as scan_headless does. Without them the desktop cannot build the
            # tracked block at all, and a holding that dropped off the list
            # would lose its chips, averages and exit plan here -- the gap
            # scanner/tracked_rows.py exists to close.
            tracked_ids, tracked_picks = set(), {}
            try:
                from scanner.tracked_rows import recent_pick_ids
                from config.settings import (PRICE_VOLUME_FILE,
                                             SIGNAL_LEDGER_FILE)
                tracked_picks = recent_pick_ids(
                    PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE, self._scan_mode,
                    with_dates=True)
                tracked_ids = set(tracked_picks)
            except Exception as e:
                print("  [tracked] id lookup skipped: {}".format(e))
            candidates = get_candidate_list(
                scan_mode=self._scan_mode,
                include_ids=set(prior_ids) | tracked_ids)

            if candidates.empty:
                self._on_error("Failed to fetch market data. Check your connection.")
                return

            # Degraded feed guard (see market_filter.FEED_MIN_ROWS): scan and
            # display what survived, but never overwrite held_ids / the ledger
            # session with a partial market.
            health = get_feed_health()
            degraded = None
            if not health.get("ok", True):
                degraded = ("feed degraded: {} snapshot failed "
                            "(TSE {} / OTC {} rows)").format(
                    "+".join(health.get("missing", [])),
                    health.get("tse_rows"), health.get("otc_rows"))
                print("  [guard] {} -> held_ids/ledger NOT updated".format(
                    degraded))
            # Tell the UI too. This used to reach export_scan_result only, so a
            # partial market feed was invisible on the desktop: the shortened
            # list read as "nothing else qualified" instead of "one exchange did
            # not answer". Sent even when healthy, so the banner clears.
            self._notify(degraded)

            total = len(candidates)

            def progress_callback(rank, total, stock_id):
                self._on_progress(rank, total, "Scanning {} ({}/{})".format(
                    stock_id, rank, total))

            verified = verify_candidates(candidates,
                                         progress_callback=progress_callback)

            # The OTC index next to TAIEX (ingestion/otc_index), for the live
            # record's benchmark -- display only, as in scan_headless.
            try:
                from ingestion.otc_index import refresh as refresh_otc_index
                refresh_otc_index()
            except Exception as e:
                print("  [tpex-index] skipped: {}".format(e))

            result_df = apply_scan_mode(verified, self._scan_mode)
            result_df = sort_for_mode(result_df, self._scan_mode)
            # The DATA session (newest bar), not the wall clock: a scan run
            # after midnight still belongs to the session it read.
            from scanner.trade_restrictions import session_of
            data_session = session_of(result_df) or session_of(verified)
            # Hysteresis top-N: stabilizes the shortlist day-to-day (see
            # scanner.scan_mode.select_with_hysteresis). Persist the kept set so
            # the next run can hold these names through transient dips.
            start_ids = prior_for(state, data_session)
            result_df, held_ids = select_with_hysteresis(result_df, start_ids)
            if degraded is None:
                save_state(self._scan_mode, data_session, held_ids, start_ids)
            result_df = add_trade_columns(result_df, self._scan_mode)

            # Forward-performance ledger: append today's shortlist (append-only,
            # idempotent per day) and backfill any matured outcomes. Never let a
            # ledger hiccup break the scan.
            try:
                n = (record_picks(result_df, self._scan_mode,
                                  scan_session=data_session or None)
                     if degraded is None else 0)
                filled = backfill_outcomes()
                print("  [ledger] recorded {} picks, backfilled {} outcomes".format(
                    n, filled))
            except Exception as e:
                print("  [ledger] skipped: {}".format(e))

            # Annotate each pick with its holding day + exit date (from the
            # ledger streak + trading calendar) so a user who does not open the
            # app daily still knows which day of the 5-bar hold they are on.
            try:
                result_df = annotate_holding(result_df, self._scan_mode)
            except Exception as e:
                print("  [holding] skipped: {}".format(e))

            # Trade restrictions (scanner/trade_restrictions, 2026-10-08):
            # the same lists and columns the headless scan publishes, so the
            # desktop export does not strip them. mark_buy_ready below gets
            # no session, so the newest Data_Date of the LISTED rows stands
            # in -- for the tracked rows too, as the headless scan passes its
            # session to both. Must precede mark_buy_ready, which blocks the
            # kinds in BLOCKING_RESTRICTIONS. A fetch that fails still
            # annotates (every row 'unknown').
            restr_info, restr_session = None, None
            try:
                from scanner.trade_restrictions import fetch_restrictions, session_of
                restr_session = session_of(result_df) or None
                restr_info = fetch_restrictions(restr_session)
            except Exception as e:
                print("  [restrict] fetch skipped: {}".format(e))
            try:
                from scanner.trade_restrictions import annotate_restrictions
                result_df = annotate_restrictions(result_df, restr_info,
                                                  restr_session)
            except Exception as e:
                print("  [restrict] skipped: {}".format(e))

            # Which rows the validated rule actually buys (needs Hold_Status,
            # so it has to follow annotate_holding). See scan_mode.mark_buy_ready.
            try:
                result_df = mark_buy_ready(result_df, self._scan_mode)
            except Exception as e:
                print("  [buyrule] skipped: {}".format(e))

            # Chip verdict for tomorrow (scanner/chip_signal.py, 2026-09-21):
            # today's institutional flow read per held row. Needs Hold_Status.
            try:
                from scanner.chip_signal import annotate_chip_action
                result_df = annotate_chip_action(result_df, self._scan_mode)
            except Exception as e:
                print("  [chips] skipped: {}".format(e))

            # Full rows for names that dropped off the list but could still
            # be held. The desktop publishes the SAME payload the phone reads,
            # so leaving this out silently stripped the dropped-out holdings
            # whenever the app was opened after a cloud scan.
            tracked_df = None
            try:
                from scanner.tracked_rows import annotate_tracked, split_tracked
                tracked_df = split_tracked(verified, result_df, tracked_ids,
                                           picked_on=tracked_picks)
                if tracked_df is not None and not tracked_df.empty:
                    tracked_df = annotate_tracked(tracked_df, self._scan_mode)
                    print("  [tracked] published full data for {} dropped-out "
                          "name(s)".format(len(tracked_df)))
            except Exception as e:
                print("  [tracked] skipped: {}".format(e))

            # The rule's actual record on this scanner's own signals
            # (scanner/live_record.py), its per-name history limited to the
            # names this payload carries; the export carries the previous
            # block forward when this fails, so the phone never loses it.
            live_record = None
            try:
                from scanner.live_record import build_live_record
                sids = set()
                for frame in (result_df, tracked_df):
                    if frame is not None and not frame.empty \
                            and "Stock_ID" in frame.columns:
                        sids.update(str(s) for s in frame["Stock_ID"])
                live_record = build_live_record(sids=sids)
            except Exception as e:
                print("  [record] skipped: {}".format(e))

            # Company events (ingestion/company_events): the nine DISPLAY-ONLY
            # columns, added after every score and gate, as scan_headless
            # does. write=False: the cache file belongs to the cloud scan,
            # which commits it; the desktop refreshes in memory only.
            events_meta = None
            try:
                from ingestion.company_events import (annotate_events,
                                                      meta_block, refresh)
                events = refresh(write=False)
                ev_session = session_of(result_df) or None
                result_df = annotate_events(result_df, events, ev_session)
                if tracked_df is not None and not tracked_df.empty:
                    tracked_df = annotate_events(tracked_df, events,
                                                 ev_session)
                events_meta = meta_block(events, ev_session, result_df,
                                         tracked_df)
            except Exception as e:
                print("  [events] skipped: {}".format(e))

            # the restriction lists on the dropped-out rows too, and their
            # summary for meta.quality (result_checks errors without it)
            quality = None
            try:
                from scanner.trade_restrictions import (
                    annotate_restrictions, describe, summarize)
                if tracked_df is not None and not tracked_df.empty:
                    tracked_df = annotate_restrictions(tracked_df, restr_info,
                                                       restr_session)
                quality = {"restrictions": summarize(restr_info, result_df,
                                                     tracked_df)}
                print("  " + describe(quality["restrictions"]))
            except Exception as e:
                print("  [restrict] summary skipped: {}".format(e))

            # Persist the latest result (overwrites previous) for offline review.
            try:
                path = export_scan_result(result_df, self._scan_mode,
                                          degraded=degraded,
                                          quality=quality,
                                          tracked=tracked_df,
                                          live_record=live_record,
                                          events_meta=events_meta)
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
