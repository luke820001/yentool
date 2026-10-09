"""
Holding-day / exit-date tracker. ASCII only.

The trade plan is DEFAULT_RULE (docs/STRATEGY.md 3.5): enter at the next open
after the signal; stop / lock / take profit / late profit-take throughout; hold
10 bars and, at the day-10 close and every close after it, ride on while the
close is above its own 5-bar mean OR the market is in a pullback inside an
uptrend on THAT day (TAIEX below its 20MA, above its 60MA), capped at 20 bars.
A user who does not open the scanner every day cannot tell which day of the
hold a given pick is on. This module answers that per stock, anchored to
reality rather than to how often the app is opened:

  * entry is anchored to the signal day that opened the current SEGMENT of the
    name's appearances in the signal ledger (see "Segments" below), not to
    today.
  * all day math is in TRADING days off price_volume.db's calendar, so skipped
    weekends/holidays/unopened days never miscount.
  * the trade is replayed with THE canonical rule -- scanner.exit_rules.
    replay_exit(hold_bars=10, ride_cap=20, extend_if=<per-date market leg>)
    -- the same call live_record.replay_trade and the recommendation
    lifecycle make, so the card, the recommendation and the live record
    cannot disagree about one trade (2026-10-08).

It adds these columns to the result DataFrame:
  Entry_Date     the open you would have bought (next trading day after signal)
  Exit_Date      the base N-th trading bar's date (blank if still in the future)
  Hold_Day       which trading day of the hold today is (0 = not entered yet);
                 on a closed trade, the day it closed on
  Hold_Remaining trading days until the base time exit (0 = today, <0 = past);
                 0 on a closed trade
  Hold_Status    machine code for the UI (UIs render their own localized text):
                   pending     signal today, buy at the next open
                   holding     inside the base hold, no exit booked
                   delay       past the base hold and the rule is riding on
                   exit_today  the rule books its exit on today's bar
                   exited      the rule booked its exit on an earlier bar
                   overdue     past the ride cap with no exit in the store
                               (missing bars -- a data gap, not a decision)
  Hold_Note      ASCII plain-language action for CSV review
  Hold_Anchor    the signal date the current segment is anchored to
  Hold_Anchor_Kind  first | gap | reentry | rec  (why that date)
  Entry_Open     the actual OPEN price on Entry_Date (None while still pending)
  Fill_Stop_Loss / Fill_Trail_Arm_Price / Fill_Trail_Lock_Price /
  Fill_Target_Price
                 the exit levels recomputed off Entry_Open
  Plan_Add_Price / Add_Hit_Date
                 the optional staged-entry level (reference before entry,
                 fill * (1 - ADD_PCT) after) and the first date it traded
                 there while the position was still open (2026-09-17)
  Prev_*         the PREVIOUS trade when the current segment was opened by a
                 re-entry or a recommendation anchor (see PREV_COLUMNS)

Segments (2026-10-08). A name's ledger appearances are split into segments,
each one a trade anchored on its first day:
  * a gap of more than `hold` sessions starts a new segment (as before);
  * a RE-ENTRY day d -- the name was absent on the previous ledger session,
    i.e. First_Day at d, the research signal definition -- starts a new
    segment when the current segment's canonical trade has exited on or
    before d, or when the current segment's anchor was not a signal (ledger
    buy_ready == 0 on its latest scan; NULL, i.e. before 2026-09-09, counts
    as a signal). A re-signal while a real trade is still open keeps the
    segment, so a holder's calendar is never reset under them.
Until this change the streak alone decided, with a ten-session tolerance, so a
name that closed its trade and came back as a fresh signal (8227 and 3498 on
2026-10-06/07) showed the OLD closed trade on a Buy_Ready card.

Recommendation anchors (`rec_anchors`, built by portfolio.sync) override the
natural anchor when their date is in the price calendar -- unless the
recommendation's own trade closed on or before the start of a newer natural
segment, in which case the newer segment is the live one.

Why Entry_Open exists (2026-08-06 audit): add_trade_columns derives
Suggested_Buy_Price / Strict_Stop_Loss / Target_Price / Trail_Lock_Price from
TODAY's close every scan. That is right for a row that has not been entered
yet, but a row already mid-hold (hysteresis keeps names listed for weeks) then
shows a stop that drifts with the market instead of sitting at the fill. On
the live 2026-08-06 payload 36 of the 39 already-entered rows (92%) showed a
stop that did not belong to their position, mean error 6.5% and max 26.7%, and
9 rows showed a "profit lock" price BELOW their own fill. The levels that
matter are anchored to the fill, so they are computed here where Entry_Date is
known and shipped alongside the close-based ones.

Nothing here changes selection; it only annotates. Failures are swallowed so a
tracker problem can never break a scan.
"""
import bisect
import sqlite3

import pandas as pd

from config.settings import PRICE_VOLUME_FILE, SIGNAL_LEDGER_FILE
from scanner.exit_rules import DEFAULT_RULE as _RULE
from scanner.scan_mode import (
    PRELAUNCH_ADD_PCT as ADD_PCT,
    PRELAUNCH_SCALE_OUT_PCT as SCALE_OUT_PCT,
    PRELAUNCH_STOP_PCT as STOP_PCT,
    PRELAUNCH_TP_PCT as TP_PCT,
    PRELAUNCH_TRAIL_ARM as TRAIL_ARM,
    PRELAUNCH_TRAIL_LOCK as TRAIL_LOCK,
)
from scanner.tick import round_to_tick

# Late profit-taking, shared with the shared exit stack (see exit_rules).
LATE_FROM = _RULE["late_from"]
LATE_GAIN = _RULE["late_gain"]

# The previous trade, shown when the current segment was opened by a re-entry
# or a recommendation anchor (all None otherwise):
#   Prev_Signal_Date        the previous segment's anchor (signal day)
#   Prev_Was_Signal         True / False from the ledger's buy_ready on that
#                           day's latest scan; None when unknown (pre-09-09)
#   Prev_Entry_Date         next trading session after Prev_Signal_Date
#   Prev_Entry_Open         its open (None when the stock has no bar there)
#   Prev_Exit_Signal        stop | lock | tp | late | time, or '' when the old
#                           trade was still open at the new anchor
#   Prev_Exit_Signal_Date / Prev_Exit_Signal_Price   where the rule booked it
#   Prev_Exit_Ret_Pct       NET percent after costs (live_record.net_pct)
#   Sessions_Since_Prev_Exit  trading sessions from that exit to the anchor
PREV_COLUMNS = (
    "Prev_Signal_Date", "Prev_Was_Signal", "Prev_Entry_Date",
    "Prev_Entry_Open", "Prev_Exit_Signal", "Prev_Exit_Signal_Date",
    "Prev_Exit_Signal_Price", "Prev_Exit_Ret_Pct", "Sessions_Since_Prev_Exit",
)
ANCHOR_KINDS = ("", "first", "gap", "reentry", "rec")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _still_strong(row):
    """Is this stock still trending on its own terms -- today's close STRICTLY
    above its own 5-bar mean?

    2026-09-21 (archive/research/sandbox_daily_plan.py). Asked what to do when
    a position is still rising on exit day, the owner's instinct was to keep
    riding, and on the corrected exit engine that is the single cleanest
    win-rate gain available: extending past the 10-bar exit while the close
    holds above its own 5-bar mean scores RECENT 69.1% / OLD 67.0% against
    67.1% / 65.1% for the fixed exit, with no cost in mean return, no extra
    capital and no change to the stop. Both window halves and both windows
    improve.

    The market-based delay (below 20MA but above 60MA) survives alongside it
    but is nearly inert on its own (+0.2pp RECENT); taking EITHER condition
    scored best in the older window, so both are kept and OR'd.

    Since 2026-10-08 the tracker no longer decides the ride from TODAY's row:
    the canonical replay asks the same question at every close past the base
    hold (exit_rules.replay_exit). This predicate stays as the single-row
    statement of the stock leg; the desktop mirrors it (tests/test_ui_parity).
    """
    close, ma5 = _num(row.get("Close_Price")), _num(row.get("MA5"))
    if close is None or ma5 is None:
        return False
    return close > ma5


def _lvl(fill, pct, direction, stock_id=None):
    """A level off the fill, on the exchange's quote ladder. See
    scanner/tick.py for why an un-snapped price is not a plan, and why the
    stock id matters (an ETF steps 0.05 above 50, an ordinary share 0.50)."""
    if not fill:
        return None
    return round_to_tick(fill * (1 + pct), direction, stock_id)

# Time-exit horizon per mode (trading bars). Modes without a validated time
# exit are left out and simply get no holding annotation.
# prelaunch moved 5 -> 10 on 2026-07-07: the 214-day overlay sweep
# (eval_prelaunch_overlays.py) showed OTC + risk_on + top-20 + hold 10 lifts the
# win rate 53 -> 56pct in BOTH window halves and raises alpha to +5.5pp, and
# hold 10 also matches the hysteresis hold band (names stay listed ~6-11 days).
HOLD_BARS_BY_MODE = {
    "mode_prelaunch": 10,
}

# The ride cap: the latest bar a position may be held to (DEFAULT_RULE
# ride_cap). Past the base hold the rule rides on while the close is above its
# own 5-bar mean or the market is in a pullback inside an uptrend.
#
# Market-shock exit delay (validated 2026-07-08, eval_exit_delay.py): if the
# TAIEX is below its 20MA on the scheduled exit day, keep holding until it climbs
# back above 20MA, capped at this many bars. On the 214-day OTC replay this beat
# the fixed 10-bar exit on win AND mean AND both window halves (mean +3.98 ->
# +4.44) by not dumping a position into a brief market shock.
#
# 2026-07-18 CONDITIONAL refinement (sandbox_redteam2.py, 6y OOS): the delay
# must ALSO require TAIEX > 60MA (a pullback WITHIN an uptrend), not just
# < 20MA. On the full 6y window the unconditional delay is ~noise overall
# (50.3 vs 49.7 fixed), but on the subset whose exit lands while TAIEX < 60MA
# (a confirmed bear, n=196) it is actively WORSE than fixed (24.0 vs 26.5,
# mean -4.23 vs -4.16) -- holding high-beta OTC longer into a below-60MA
# breakdown. The conditional delay reverts to a plain 10-bar exit in that case
# (26.5, identical to fixed) and is unchanged in normal pullbacks, so it is
# strictly >= the old rule.
#
# 2026-10-08: the market leg is read on the date of EACH close past the base
# hold (scanner.market_leg), not off today's regime -- a trade whose day 10
# fell weeks ago used to be judged by the market of the day the scan ran.
EXIT_DELAY_CAP_BY_MODE = {
    "mode_prelaunch": 20,
}


def _trading_calendar():
    """Sorted distinct trading dates ('YYYY-MM-DD') from price_volume.db.

    A date only counts when the market traded it. A feed that hands back a
    placeholder bar for a session that has not happened yet would otherwise
    add a day to every hold count and hand a pick an Entry_Date on a day the
    exchange was shut (2026-09-20). scan-time purging is the real fix; this is
    the guard for a store that was written before it, or by something else.
    """
    try:
        conn = sqlite3.connect(PRICE_VOLUME_FILE)
        try:
            rows = conn.execute(
                "SELECT date, COUNT(*) FROM data GROUP BY date").fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    try:
        from scanner.data_integrity import nonsession_dates
        skip = set(nonsession_dates(rows))
    except Exception:
        skip = set()
    dates = sorted({str(r[0])[:10] for r in rows if r and r[0]} - skip)
    return dates


def _entry_opens(pairs):
    """{(stock_id, date): open} for the given (stock_id, date) pairs.

    One query per scan (the pick list is ~40 rows), so this costs nothing next
    to the scan itself. Missing bars simply do not appear in the map.
    """
    pairs = [(str(s), str(d)[:10]) for s, d in pairs if s and d]
    if not pairs:
        return {}
    ids = sorted({s for s, _ in pairs})
    dates = sorted({d for _, d in pairs})
    try:
        conn = sqlite3.connect(PRICE_VOLUME_FILE)
        try:
            rows = conn.execute(
                "SELECT stock_id, date, open FROM data WHERE stock_id IN (%s)"
                " AND date IN (%s)"
                % (",".join("?" * len(ids)), ",".join("?" * len(dates))),
                ids + dates,
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for sid, d, op in rows:
        try:
            op = float(op)
        except (TypeError, ValueError):
            continue
        if op > 0:
            # A quoted price has two decimals; the batch feed returns values
            # a hair off it (2402.926758 for a 2402.93 close). EVERY published
            # level is this number times something, so that hair becomes a
            # full tick once the product is snapped onto the ladder --
            # measured 2026-09-21 at 3,086 level computations across the
            # stored opens since 2026-08-01, worst case 5.00. New bars are
            # rounded on the way in and a repair pass covers the rest; this
            # covers a bar neither has reached yet.
            out[(str(sid), str(d)[:10])] = round(op, 2)
    return out


def _ledger_bar_dates(scan_mode):
    """{stock_id: sorted list of distinct signal bar_dates} for this mode, or
    None when the ledger cannot be read (missing file, not a database, no
    picks table, locked).

    None is not {}: "no history" would make every name a first-day signal and
    drop the 'held' block, so a name already bought would be advertised as a
    new buy on a FINAL list (2026-10-09 audit M-15 / D11-01). annotate_holding
    reads None as "cannot place any trade" and leaves every status blank, which
    mark_buy_ready blocks as 'unknown'."""
    if not SIGNAL_LEDGER_FILE.exists():
        return None
    try:
        conn = sqlite3.connect(SIGNAL_LEDGER_FILE)
        try:
            rows = conn.execute(
                "SELECT stock_id, bar_date FROM picks WHERE scan_mode = ?",
                (scan_mode,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    out = {}
    for sid, bd in rows:
        out.setdefault(str(sid), set()).add(str(bd)[:10])
    return {sid: sorted(s) for sid, s in out.items()}


THIN_SESSION_RATIO = 0.5       # a list under half its usual size is not a list
THIN_SESSION_LOOKBACK = 5      # sessions that define "usual"
THIN_SESSION_MIN_USUAL = 10    # below this there is no "usual" to compare to


def _thin_previous_session(led, today):
    """The previous ledger session, when its list is far smaller than the
    sessions before it. A depleted session (2026-08-28 holds ONE row where 49
    are usual) makes every name look absent from yesterday's list, so
    First_Day reads True for 49 of 50 names on the next scan (2026-10-09
    audit D3-02). Returns (session, count, usual) or None."""
    counts = {}
    for ds in led.values():
        for d in ds:
            if d < today:
                counts[d] = counts.get(d, 0) + 1
    days = sorted(counts)
    if len(days) < 3:
        return None
    prev = days[-1]
    before = [counts[d] for d in days[-1 - THIN_SESSION_LOOKBACK:-1]]
    usual = sorted(before)[len(before) // 2]
    if usual >= THIN_SESSION_MIN_USUAL and counts[prev] < THIN_SESSION_RATIO * usual:
        return prev, counts[prev], usual
    return None


def _ledger_buy_flags(scan_mode):
    """{stock_id: {bar_date: 1 | 0 | None}} -- buy_ready as recorded by the
    LATEST scan of each (stock, bar date), the way live_record._picks reads
    the ledger (a local and a cloud scan of one session can disagree; the
    later one is what was published). None = not recorded (every row before
    2026-09-09). Any failure returns {}, which reads as "all unknown"."""
    try:
        if not SIGNAL_LEDGER_FILE.exists():
            return {}
        conn = sqlite3.connect(SIGNAL_LEDGER_FILE)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(picks)")}
            if "buy_ready" not in cols:
                return {}
            rows = conn.execute(
                "SELECT stock_id, bar_date, scan_ts, buy_ready FROM picks "
                "WHERE scan_mode = ?", (scan_mode,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    latest = {}
    for sid, bd, ts, br in rows:
        key = (str(sid), str(bd)[:10])
        ts = str(ts or "")
        if key not in latest or ts >= latest[key][0]:
            latest[key] = (ts, br)
    out = {}
    for (sid, bd), (_, br) in latest.items():
        try:
            v = None if br is None else int(br)
        except (TypeError, ValueError):
            v = None
        out.setdefault(sid, {})[bd] = v
    return out


def _disturbed_fn():
    """The market leg of the ride as an extend_if callback (one TAIEX read per
    scan, cached in scanner.market_leg). None when it cannot be built: the
    replay then rides on the stock leg alone, which is also what a missing
    TAIEX bar means."""
    try:
        from scanner.market_leg import make_disturbed_fn
        return make_disturbed_fn()
    except Exception:
        return None


def _canonical(bars, hold, cap, extend_if):
    """THE rule over `bars` [(date, open, high, low, close), ...], first bar =
    entry. Same call as live_record.replay_trade."""
    from scanner.exit_rules import replay_exit
    return replay_exit([b[1] for b in bars], [b[2] for b in bars],
                       [b[3] for b in bars], [b[4] for b in bars],
                       dates=[b[0] for b in bars], hold_bars=hold,
                       stop_pct=STOP_PCT, tp_pct=TP_PCT, arm_pct=TRAIL_ARM,
                       lock_pct=TRAIL_LOCK, late_from=LATE_FROM,
                       late_gain=LATE_GAIN, ride_cap=cap, extend_if=extend_if)


def _segment_walk(known, idx_of, gap_tol, is_reentry, flag_of, exit_of):
    """Split a name's sorted list dates into trade segments.

    Returns [{"start": date, "kind": first|gap|reentry, "dates": [...]}, ...].
    A gap of more than `gap_tol` sessions starts a new segment. A re-entry day
    d (`is_reentry(d)`) starts one when the current segment's anchor was not a
    signal (`flag_of(anchor) == 0`) or its trade exited on or before d
    (`exit_of(anchor)` -> exit date or None). Otherwise d joins the segment:
    a re-signal while a real trade is open keeps the holder's calendar.
    """
    segs = []
    for d in known:
        if not segs:
            segs.append({"start": d, "kind": "first", "dates": [d]})
            continue
        cur = segs[-1]
        if idx_of[d] - idx_of[cur["dates"][-1]] > gap_tol:
            segs.append({"start": d, "kind": "gap", "dates": [d]})
            continue
        if is_reentry(d):
            split = flag_of(cur["start"]) == 0
            if not split:
                ex = exit_of(cur["start"])
                split = bool(ex) and ex <= d
            if split:
                segs.append({"start": d, "kind": "reentry", "dates": [d]})
                continue
        cur["dates"].append(d)
    return segs


def _choose_anchor(segs, rec_anchor, idx_of, today, gap_tol, exit_of):
    """(anchor, kind, prev_anchor) for one row.

    The natural anchor is the last segment's start; its previous segment is
    reported only when a re-entry opened it. A recommendation anchor in the
    price calendar overrides it, unless the recommendation's own trade closed
    on or before a NEWER natural segment's start (that segment is then the
    live trade, e.g. a fresh signal days after the recommended trade's exit).
    """
    nat = segs[-1] if segs else None
    anchor, kind, prev = (nat["start"], nat["kind"], None) if nat else (None, "", None)
    if nat is not None and nat["kind"] == "reentry" and len(segs) > 1:
        prev = segs[-2]["start"]
    a = str(rec_anchor or "")[:10]
    if not a or a not in idx_of or a > today:
        return anchor, kind, prev
    if nat is not None and nat["start"] > a:
        ex = exit_of(a)
        if ex and ex <= nat["start"]:
            return anchor, kind, prev
    prev = None
    ci = None
    for i, s in enumerate(segs):
        if s["start"] <= a:
            ci = i
    if ci is not None:
        c = segs[ci]
        if c["start"] < a:
            last_le = max(d for d in c["dates"] if d <= a)
            if idx_of[a] - idx_of[last_le] <= gap_tol:
                prev = c["start"]
        elif c["kind"] == "reentry" and ci > 0:
            prev = segs[ci - 1]["start"]
    return a, "rec", prev


def _prev_columns(prev, anchor, sbars, cal, idx_of, flag_of, hold, cap, extend_if):
    """The PREV_COLUMNS dict for the trade anchored at `prev`, replayed with the
    canonical rule from its entry through `anchor` inclusive."""
    out = dict.fromkeys(PREV_COLUMNS)
    if (not prev or not anchor or prev not in idx_of or anchor not in idx_of
            or prev >= anchor):
        return out
    ped = cal[idx_of[prev] + 1]
    flag = flag_of(prev)
    seg = [b for b in sbars if prev < b[0] <= anchor]
    op = next((_num(b[1]) for b in seg if b[0] == ped), None)
    out.update(Prev_Signal_Date=prev,
               Prev_Was_Signal=None if flag is None else bool(flag),
               Prev_Entry_Date=ped,
               Prev_Entry_Open=round(op, 2) if op and op > 0 else None,
               Prev_Exit_Signal="")
    if seg:
        p = _canonical(seg, hold, cap, extend_if)
        when = p.get("date")
        if p.get("exited") and when and when in idx_of:
            from scanner.live_record import net_pct
            gross = float(p["ret_pct"])
            out.update(Prev_Exit_Signal=p["reason"],
                       Prev_Exit_Signal_Date=when,
                       Prev_Exit_Signal_Price=round(float(p["exit_price"]), 2),
                       Prev_Exit_Ret_Pct=round(net_pct(gross), 2),
                       Sessions_Since_Prev_Exit=int(idx_of[anchor] - idx_of[when]))
    return out


def annotate_holding(df, scan_mode, rec_anchors=None, add_own_bar=True):
    """Return df with the holding / exit-plan / Prev_* columns added (best
    effort). Modes without a validated time exit are returned unchanged.

    rec_anchors   {stock_id: {"anchor": 'YYYY-MM-DD', "stop": float,
                  "target": float, "rec_id": str}} -- recommendation anchors
                  that override the natural one (see the module docstring);
                  a pending row with one shows the recommendation's stop.
    add_own_bar   count the row's own Data_Date as a list appearance. False
                  for tracked rows (names that dropped off the list): they
                  were not on today's list, so today must not re-anchor them.
    """
    if df is None or df.empty or "Stock_ID" not in df.columns:
        return df
    hold = HOLD_BARS_BY_MODE.get(scan_mode)
    if not hold:
        return df
    cap = EXIT_DELAY_CAP_BY_MODE.get(scan_mode, hold)   # >= hold; == hold disables

    cal = _trading_calendar()
    if not cal:
        return df
    # Today is the NEWEST valid bar date in the frame (the session the scan
    # is about; result_export and mark_buy_ready read it the same way), not
    # row 0's: one stale row at the top must not wind the calendar back for
    # every other row.
    today = _frame_today(df) or cal[-1]
    # The calendar ends at today, as it does in a live scan: a replay of a
    # past session must not date a pending entry, or price its fill, from a
    # session that had not happened yet.
    cal = [d for d in cal if d <= today]
    if not cal:
        return df
    idx_of = {d: i for i, d in enumerate(cal)}

    led = _ledger_bar_dates(scan_mode)
    ledger_down = led is None
    if ledger_down:
        # No history to place a trade against: every row keeps a blank
        # status (blocked 'unknown'), and the checker raises ledger_unreadable.
        print("  [holding] signal ledger unreadable: no row can be placed, "
              "all statuses left blank")
        led = {}
    thin = _thin_previous_session(led, today)
    if thin:
        print("  [holding] the ledger's previous session {} holds {} names "
              "(usually {}): First_Day cannot be judged, all statuses left "
              "blank".format(*thin))
        ledger_down = True
    flags = _ledger_buy_flags(scan_mode)
    extend_if = _disturbed_fn()
    rec_anchors = rec_anchors or {}
    df = df.copy()

    # The previous LEDGER session: the newest bar date any pick was recorded
    # on before today. Measured against the ledger rather than the calendar so
    # a day the scan did not run cannot turn every name into a "new" signal.
    sessions = sorted({d for ds in led.values() for d in ds})
    prev_session = max((d for d in sessions if d < today), default=None)

    ids = [str(s).strip() for s in df["Stock_ID"]]
    knowns = []
    for sid, (_, r) in zip(ids, df.iterrows()):
        dates = set(led.get(sid, []))
        if add_own_bar:
            # this pick's own signal day, so a just-written (or just-missed)
            # row still anchors correctly
            dates.add(str(r.get("Data_Date") or today)[:10])
        knowns.append(sorted(d for d in dates if d in idx_of and d <= today))

    # One price read for every name, from its earliest possible anchor.
    starts = [(sid, k[0]) for sid, k in zip(ids, knowns) if k]
    for sid in ids:
        ra = rec_anchors.get(sid) or {}
        a = str(ra.get("anchor") or "")[:10]
        if a in idx_of:
            starts.append((sid, a))
    raw = _bars_since(starts, today)
    bars_by = {sid: [b for b in rows if b[0] in idx_of]
               for sid, rows in raw.items()}

    trade_cache = {}

    def trade(sid, anchor):
        """Canonical replay of the trade signalled on `anchor`, through today."""
        key = (sid, anchor)
        if key not in trade_cache:
            seg = [b for b in bars_by.get(sid, []) if b[0] > anchor]
            trade_cache[key] = _canonical(seg, hold, cap, extend_if) if seg else None
        return trade_cache[key]

    def exit_date(sid, anchor):
        p = trade(sid, anchor)
        return (p.get("date") or None) if p and p.get("exited") else None

    anchors, kinds, prevs, first_days = [], [], [], []
    for sid, known in zip(ids, knowns):
        own = set(led.get(sid, []))
        sflags = flags.get(sid, {})
        ra = rec_anchors.get(sid) or {}
        try:
            segs = _segment_walk(
                known, idx_of, hold,
                is_reentry=lambda d, own=own: _reentry(sessions, own, d),
                flag_of=sflags.get,
                exit_of=lambda a, sid=sid: exit_date(sid, a))
            anchor, kind, prev = _choose_anchor(
                segs, ra.get("anchor"), idx_of, today, hold,
                lambda a, sid=sid: exit_date(sid, a))
        except Exception as e:  # one bad row must not cost the others
            # No anchor -> blank status -> the row cannot be bought
            # (mark_buy_ready blocks an unknown status), which is the safe
            # reading of "the tracker could not place this trade".
            print("  [holding] {}: anchor skipped: {}".format(sid, e))
            anchor, kind, prev = None, "", None
        anchors.append(anchor)
        kinds.append(kind if anchor else "")
        prevs.append(prev)
        # FIRST DAY ON THE LIST, the research definition (2026-09-23): the
        # name was not on the previous session's list. The buy rule was
        # validated on "absent the previous session"; reading it off a streak
        # with a ten-session tolerance made the live scanner drop 177 of the
        # rule's 479 signals (2020-01..2026-09). docs/BACKTEST_LOG.md
        # section L. The segments above use the same definition for a
        # re-entry, so a fresh signal and its card describe the same trade.
        first_days.append(prev_session is None or prev_session not in own)

    rows = []
    for i_row, (sid, (_, r)) in enumerate(zip(ids, df.iterrows())):
        anchor = anchors[i_row]
        ra = rec_anchors.get(sid) or {}
        ref_stop = _num(r.get("Strict_Stop_Loss"))
        if kinds[i_row] == "rec" and _num(ra.get("stop")) is not None:
            ref_stop = _num(ra.get("stop"))
        info = {"anchor": anchor, "kind": kinds[i_row],
                "ref_stop": ref_stop, "ref_add": _num(r.get("Add_Price"))}
        try:
            info.update(_prev_columns(
                prevs[i_row], anchor, bars_by.get(sid, []), cal, idx_of,
                (flags.get(sid, {})).get, hold, cap, extend_if))
        except Exception as e:
            # A half-filled previous trade is worse than none (result_checks
            # errors on a partial Prev_* set): blank them all.
            print("  [holding] {}: previous trade skipped: {}".format(sid, e))
            info.update({c: None for c in PREV_COLUMNS})
        rows.append(info)

    entry_dates = []
    for info in rows:
        a = info["anchor"]
        e = idx_of[a] + 1 if a in idx_of else None
        entry_dates.append(cal[e] if e is not None and e < len(cal) else "")
    opens = _entry_opens(zip(ids, entry_dates))
    fills = [opens.get((sid, ed)) for sid, ed in zip(ids, entry_dates)]

    try:
        plan = _add_exit_plan(df, ids, rows, fills, bars_by, cal, idx_of, today,
                              hold, cap, extend_if)
    except Exception as e:      # an annotation problem must never break a scan
        # An UNKNOWN status (blank) blocks buying downstream, which is the
        # safe reading of "the tracker could not say where this trade is".
        print("  [holding] exit plan skipped: {}".format(e))
        n = len(ids)
        plan = {"Entry_Date": entry_dates, "Exit_Date": [""] * n,
                "Hold_Day": [None] * n, "Hold_Remaining": [None] * n,
                "Hold_Status": [""] * n, "Hold_Note": [""] * n,
                "Plan_Stop": [None] * n, "Plan_Add_Price": [None] * n,
                "Add_Hit_Date": [""] * n, "Plan_Armed": [False] * n,
                "Exit_Signal": [""] * n, "Exit_Signal_Date": [""] * n,
                "Exit_Signal_Price": [None] * n, "Exit_Note": [""] * n}

    for col in ("Entry_Date", "Exit_Date", "Hold_Day", "Hold_Remaining"):
        df[col] = plan[col]
    df["Hold_Total"] = hold          # base N in "day X of N"
    df["Hold_Cap"] = cap             # latest exit bar when riding
    for col in ("Hold_Status", "Hold_Note"):
        df[col] = plan[col]
    if ledger_down:
        # Whatever the replay above worked out came from a history of nothing;
        # a blank status is the one reading mark_buy_ready refuses to buy.
        df["Hold_Status"] = ""
        df["Hold_Note"] = ""
    df["First_Day"] = first_days
    df["Hold_Anchor"] = [info["anchor"] or "" for info in rows]
    df["Hold_Anchor_Kind"] = [info["kind"] for info in rows]

    # Exit levels anchored to the price actually paid (see module docstring).
    # A pending row has no fill yet, so its Entry_Open stays None and the UI
    # keeps showing the close-based reference -- which is correct there.
    df["Entry_Open"] = fills
    df["Fill_Stop_Loss"] = [_lvl(f, -STOP_PCT, "down", i)
                            for f, i in zip(fills, ids)]
    df["Fill_Trail_Arm_Price"] = [_lvl(f, TRAIL_ARM, "up", i)
                                  for f, i in zip(fills, ids)]
    df["Fill_Trail_Lock_Price"] = [_lvl(f, TRAIL_LOCK, "down", i)
                                   for f, i in zip(fills, ids)]
    df["Fill_Target_Price"] = [_lvl(f, TP_PCT, "up", i)
                               for f, i in zip(fills, ids)]
    df["Fill_Scale_Out_Price"] = [_lvl(f, SCALE_OUT_PCT, "up", i)
                                  for f, i in zip(fills, ids)]

    # Exit plan (2026-09-14): one column that says below what price to sell
    # first, and whether the rule has already taken the trade out.
    for col in ("Plan_Stop", "Plan_Add_Price", "Add_Hit_Date", "Plan_Armed",
                "Exit_Signal", "Exit_Signal_Date", "Exit_Signal_Price",
                "Exit_Note"):
        df[col] = plan[col]
    for col in PREV_COLUMNS:
        df[col] = [info[col] for info in rows]
    return df


def _frame_today(df):
    """Newest valid 'YYYY-MM-DD' Data_Date in the frame, or ''."""
    if "Data_Date" not in df.columns:
        return ""
    best = ""
    for v in df["Data_Date"].tolist():
        d = str(v)[:10] if v is not None else ""
        if (len(d) == 10 and d[:4].isdigit() and d[4] == "-" and d[7] == "-"
                and d[5:7].isdigit() and d[8:].isdigit() and d > best):
            best = d
    return best


def _reentry(sessions, own, d):
    """Was the name absent on the ledger session before d (First_Day at d)?"""
    i = bisect.bisect_left(sessions, d)
    prev = sessions[i - 1] if i > 0 else None
    return prev is None or prev not in own


def _bars_since(pairs, upto):
    """{stock_id: [(date, open, high, low, close), ...]} from the earliest
    requested date per stock through `upto`, one query for the whole list."""
    pairs = [(str(s), str(d)[:10]) for s, d in pairs if s and d]
    if not pairs:
        return {}
    ids = sorted({s for s, _ in pairs})
    start = min(d for _, d in pairs)
    try:
        conn = sqlite3.connect(PRICE_VOLUME_FILE)
        try:
            rows = conn.execute(
                "SELECT stock_id, date, open, high, low, close FROM data "
                "WHERE stock_id IN (%s) AND date >= ? AND date <= ? "
                "ORDER BY stock_id, date" % ",".join("?" * len(ids)),
                ids + [start, str(upto)[:10]],
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for sid, d, o, h, l, c in rows:
        out.setdefault(str(sid), []).append((str(d)[:10], o, h, l, c))
    return out


def _mean5_note(bars):
    """(last close, its 5-bar mean since entry) for the ride note."""
    closes = [_num(b[4]) for b in bars]
    closes = [c for c in closes if c is not None]
    if not closes:
        return None, None
    window = closes[-5:]
    return closes[-1], sum(window) / len(window)


def _add_exit_plan(df, ids, rows, fills, bars_by, cal, idx_of, today, hold,
                   cap, extend_if):
    """Status, hold counts and the exit plan per row, from THE canonical replay
    of each row's current trade. Returns {column: [values]}."""
    cols = {c: [] for c in (
        "Entry_Date", "Exit_Date", "Hold_Day", "Hold_Remaining", "Hold_Status",
        "Hold_Note", "Plan_Stop", "Plan_Add_Price", "Add_Hit_Date",
        "Plan_Armed", "Exit_Signal", "Exit_Signal_Date", "Exit_Signal_Price",
        "Exit_Note")}
    last_idx = len(cal) - 1
    today_idx = idx_of.get(today, last_idx)

    for sid, info, fill in zip(ids, rows, fills):
        try:
            row = _plan_row(sid, info, fill, bars_by.get(sid, []), cal, idx_of,
                            today, today_idx, hold, cap, extend_if)
        except Exception as e:  # one bad row must not blank the others
            print("  [holding] {}: exit plan skipped: {}".format(sid, e))
            row = _blank_plan(info, cal, idx_of)
        for c in cols:
            cols[c].append(row[c])
    return cols


def _blank_plan(info, cal, idx_of):
    """The plan of a row the tracker could not work out: entry date kept,
    status UNKNOWN (blank), which blocks buying downstream."""
    a = info.get("anchor")
    e = idx_of[a] + 1 if a in idx_of else None
    return {"Entry_Date": cal[e] if e is not None and e < len(cal) else "",
            "Exit_Date": "", "Hold_Day": None, "Hold_Remaining": None,
            "Hold_Status": "", "Hold_Note": "", "Plan_Stop": None,
            "Plan_Add_Price": None, "Add_Hit_Date": "", "Plan_Armed": False,
            "Exit_Signal": "", "Exit_Signal_Date": "", "Exit_Signal_Price": None,
            "Exit_Note": ""}


def _plan_row(sid, info, fill, sbars, cal, idx_of, today, today_idx, hold, cap,
              extend_if):
    last_idx = len(cal) - 1
    anchor = info["anchor"]
    ref, ref_add = info["ref_stop"], info["ref_add"]
    out = {"Entry_Date": "", "Exit_Date": "", "Hold_Day": None,
           "Hold_Remaining": None, "Hold_Status": "", "Hold_Note": "",
           "Plan_Stop": round(ref, 2) if ref else None,
           "Plan_Add_Price": round(ref_add, 2) if ref_add else None,
           "Add_Hit_Date": "", "Plan_Armed": False, "Exit_Signal": "",
           "Exit_Signal_Date": "", "Exit_Signal_Price": None,
           "Exit_Note": "reference stop {}; becomes fill x {:.2f} after entry"
                        .format(round(ref, 2) if ref else "n/a", 1 - STOP_PCT)}
    if not anchor or anchor not in idx_of:
        return out

    entry_idx = idx_of[anchor] + 1          # buy the open AFTER the signal
    exit_idx = entry_idx + hold - 1         # close of the base N-th bar
    cap_idx = entry_idx + cap - 1           # latest bar the ride may reach
    ed = cal[entry_idx] if entry_idx <= last_idx else ""
    out["Entry_Date"] = ed
    out["Exit_Date"] = cal[exit_idx] if exit_idx <= last_idx else ""
    day_no = today_idx - entry_idx + 1      # trading days held incl. today
    remaining = exit_idx - today_idx        # to base exit; 0 = today, <0 past
    out["Hold_Day"] = max(day_no, 0)
    out["Hold_Remaining"] = remaining

    if day_no <= 0:
        out["Hold_Status"] = "pending"
        out["Hold_Note"] = "next-open entry (signal {})".format(anchor)
        return out

    def calendar_status():
        if remaining > 0:
            return "holding", "held {}/{}, exit in {} trading day(s)".format(
                day_no, hold, remaining)
        if remaining == 0:
            return "exit_today", "day {}, exit at close".format(day_no)
        return "overdue", "day {} past the time exit with no result in the " \
                          "price store".format(day_no)

    bars = [b for b in sbars if b[0] >= ed] if ed else []
    if not fill:
        st, note = calendar_status()
        out["Hold_Status"], out["Hold_Note"] = st, note
        out["Exit_Note"] = "no bars since entry yet"
        return out

    add_px = _lvl(fill, -ADD_PCT, "down", sid)
    out["Plan_Add_Price"] = add_px
    fill_stop = _lvl(fill, -STOP_PCT, "down", sid)
    if not bars:
        st, note = calendar_status()
        out.update(Hold_Status=st, Hold_Note=note, Plan_Stop=fill_stop,
                   Exit_Note="sell if it trades below {} (fill {:.2f} x {:.2f}, "
                             "on the tick ladder); no bars since entry in the "
                             "store".format(fill_stop, fill, 1 - STOP_PCT))
        return out

    p = _canonical(bars, hold, cap, extend_if)
    armed = bool(p.get("armed"))
    stop = p.get("stop")
    # The replay works in exact prices; the owner has to place the order,
    # so the published level is the one on the quote ladder.
    stop = round_to_tick(float(stop), "down", sid) if stop else fill_stop
    out["Plan_Armed"] = armed
    out["Plan_Stop"] = stop

    if p.get("exited"):
        reason = p["reason"]
        when = p.get("date") or ""
        price = round(float(p["exit_price"]), 2)
        days = int(p["bar"]) + 1 if p.get("bar") is not None else day_no
        # A booked exit ENDS the hold; nothing after it is a hold day (the
        # 2026-09-21 audit found "day 12, keep riding" next to "the lock
        # closed this trade on day 6" on 18 of 101 rows).
        out.update(Hold_Status="exited" if when and when < today else "exit_today",
                   Hold_Day=days, Hold_Remaining=0, Exit_Signal=reason,
                   Exit_Signal_Date=when, Exit_Signal_Price=price)
        if reason == "time":
            out["Hold_Note"] = "time exit on day {} ({}) at close {:.2f}".format(
                days, when or "?", price)
            out["Exit_Note"] = "time exit {} at close {:.2f}".format(when or "?", price)
            # a time exit is at the close: the add window includes that day
            out["Add_Hit_Date"] = _add_hit_date(bars, add_px, {"exited": False}, when)
        else:
            out["Hold_Note"] = "closed by the {} on day {} ({}) at {:.2f}".format(
                reason, days, when or "?", price)
            out["Exit_Note"] = "{} exit booked {} at {:.2f} ({:+.1f}%)".format(
                reason, when or "?", price, p.get("ret_pct") or 0.0)
            out["Add_Hit_Date"] = _add_hit_date(bars, add_px, p)
        return out

    out["Add_Hit_Date"] = _add_hit_date(bars, add_px, p)
    if today_idx >= cap_idx:
        # The cap bar has come and the store has no exit: bars are missing.
        out["Hold_Status"] = "overdue"
        out["Hold_Note"] = ("day {} reached the ride cap {} with {} bar(s) in "
                            "the store and no exit booked: check the price "
                            "data".format(day_no, cap, len(bars)))
    elif p.get("riding"):
        last, mean5 = _mean5_note(bars)
        out["Hold_Status"] = "delay"
        if last is not None and mean5 is not None and last > mean5:
            out["Hold_Note"] = ("day {}: close {} still above its 5-bar mean "
                                "{:.2f}, keep riding (cap day {})".format(
                                    day_no, last, mean5, cap))
        else:
            out["Hold_Note"] = ("day {}: TAIEX below its 20MA but above its "
                                "60MA, keep riding (cap day {})".format(day_no, cap))
    elif len(bars) < hold:
        out["Hold_Status"] = "holding"
        out["Hold_Note"] = ("held {}/{}, exit in {} trading day(s)".format(
            day_no, hold, remaining) if remaining > 0 else
            "day {}: {} bar(s) traded since entry, the time exit comes after "
            "bar {}".format(day_no, len(bars), hold))
    else:
        st, note = calendar_status()
        out["Hold_Status"], out["Hold_Note"] = st, note

    if p.get("late_due"):
        # Decided by tonight's close, acted on at tomorrow's open.
        out["Exit_Note"] = ("in profit on day {}+: sell at the next open "
                            "(close {} at or above the fill {:.2f})".format(
                                LATE_FROM, bars[-1][4], fill))
    elif armed:
        out["Exit_Note"] = ("lock armed: sell if it trades below {} "
                            "(fill {:.2f} x {:.2f})".format(stop, fill, 1 + TRAIL_LOCK))
    else:
        out["Exit_Note"] = ("sell if it trades below {} (fill {:.2f} x {:.2f}); the "
                            "lock arms on a CLOSE at or above {}".format(
                                stop, fill, 1 - STOP_PCT,
                                _lvl(fill, TRAIL_ARM, "up", sid)))
    return out


def _add_hit_date(bars, add_px, plan, time_exit_date=None):
    """First bar date the staged-entry limit at `add_px` would have filled
    while the position was still open, else ''.

    A bar fills the limit when it opens or trades at or below the level. The
    add only counts up to the exit: on the exit bar itself it counts when the
    exit was the stop (the price has to pass the add level on its way down to
    a stop that sits below it), not on a lock/target bar, whose intraday order
    against a low that deep is unknowable. A calendar time exit ends the
    window at that day's close.
    """
    if not add_px:
        return ""
    exit_bar = plan.get("bar") if plan.get("exited") else None
    for i, (d, o, h, l, c) in enumerate(bars):
        if exit_bar is not None and (i > exit_bar or (
                i == exit_bar and plan.get("reason") != "stop")):
            return ""
        if time_exit_date and d > time_exit_date:
            return ""
        try:
            lo = min(float(o), float(l))
        except (TypeError, ValueError):
            continue
        if lo <= add_px:
            return d
    return ""
