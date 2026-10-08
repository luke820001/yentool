"""
Per-column self-check of the published scan payload. ASCII only.

The cloud scan publishes mobile/scan_result.json (96 columns x N rows plus a
meta block), mobile/quotes.json and data/recommendations.json. Until now the
only checks were upstream guards (feed floors, per-bar integrity, the buy
gate): nothing looked at the file that actually reaches the phone. A column
could go all-null, a stop could land above its entry, a holding could lose its
quotes, and the run would still finish green.

This module reads the payload the way the phone does and audits EVERY column:

  * registry        each known column has a kind (id/str/date/num/int/bool/
                    choice), a null policy and a plausible range; unknown
                    columns are reported, missing ones are errors.
  * row consistency identities that must hold between columns (stop < entry <
                    target, Hold_Day + Hold_Remaining == Hold_Total, Buy_Ready
                    implies every gate, fill levels derive from Entry_Open,
                    Core_Plus matches its thresholds, close inside the day's
                    range, ...).
  * meta            data_date vs session/calendar/regime/quotes, count vs rows,
                    degraded / data_lag flags, reports present.
  * quotes          every listed stock is priced through today, and every stock
                    the ledger picked in the feed window is priced too (F04:
                    a holding that dropped off the list must stay valued).

The result is a small report {status, errors, warnings, items[]} that is
written back into meta.checks so the phone can show it, printed as GitHub
annotations by tools/check_scan_result.py, and kept as a rolling history in
data/scan_checks.json. status is "fail" only for rules whose meaning is not in
doubt (types, nulls, identities); range rules only warn, because a genuinely
extreme day is not a bug.

Nothing here alters selection. It reports; the pipeline fixes.
"""
import json
import math
import re
from datetime import datetime

CHECKS_VERSION = 1
HISTORY_KEEP = 60

MARKETS = ("TSE", "OTC")
HOLD_STATUSES = ("", "pending", "holding", "exit_today", "overdue",
                 "delay", "exited")
# "regime" = the index really is below its 20/60-day averages.
# "regime_stale" = the index FEED is behind the stock data, so the regime is
# unusable. Both veto a buy; they are different facts and the screens must not
# state the first when the second is true (2026-09-21, 46 rows).
# "restricted" (2026-10-08) = the row's Trade_Restriction is in
# trade_restrictions.BLOCKING_RESTRICTIONS; lowest priority, so it is the
# reason only when every other gate passed.
BUY_BLOCKS = ("", "regime", "regime_stale", "held", "unknown", "quality",
              "market", "rank", "integrity", "stale", "no_rule", "dropped",
              "restricted")
# scanner.trade_restrictions.RESTRICTION_KINDS (most severe first) and the
# default blocking set; the checker prefers the set the payload declares in
# meta.quality.restrictions.blocking, so a payload is judged by the rule that
# produced it.
RESTRICTION_KINDS = ("suspended", "disposition", "altered", "limit_lock",
                     "unknown", "attention", "none")
RESTRICTION_PREPAY = ("all", "threshold")
DEFAULT_BLOCKING_RESTRICTIONS = ("suspended",)
# meta.report_sources[market].source (gemini_hook.gemini_client.REPORT_SOURCES)
REPORT_SOURCES = ("gemini", "groq", "template")
# Company events (ingestion/company_events, 2026-10-08): display only, never
# scored. ingestion.company_events.EX_KINDS.
EX_KINDS = ("div", "right", "both")
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
# meta.live_record per-name history and benchmark (scanner/live_record:
# BY_SID_KEPT, BENCH_POINTS, the three buckets)
LIVE_RECORD_BUCKETS = ("tradable", "not_core", "regime_closed")
BY_SID_MAX = 5
BENCH_MAX_POINTS = 120
# The recommendation lifecycle (portfolio.sync, 2026-10-08): 'active' while the
# recommended trade is live, then exactly one terminal status. 'converted' is
# local only (a real position) and must never reach a public file.
REC_STATUSES = ("active", "expired", "converted", "cancelled", "closed",
                "superseded")
REC_TERMINAL = ("expired", "closed", "superseded", "cancelled")
# portfolio.sync.REC_HORIZON_SESSIONS: an active recommendation this many
# sessions past its entry session means the lifecycle's safety net did not run
REC_HORIZON_SESSIONS = 25
EXIT_SIGNALS = ("", "stop", "lock", "tp", "late", "time")
# why the card's trade starts where it does (holding_tracker.ANCHOR_KINDS)
ANCHOR_KINDS = ("", "first", "gap", "reentry", "rec")
# the previous trade, set only when a re-entry or a recommendation anchor
# opened the current one (holding_tracker.PREV_COLUMNS)
PREV_COLUMNS = (
    "Prev_Signal_Date", "Prev_Was_Signal", "Prev_Entry_Date",
    "Prev_Entry_Open", "Prev_Exit_Signal", "Prev_Exit_Signal_Date",
    "Prev_Exit_Signal_Price", "Prev_Exit_Ret_Pct", "Sessions_Since_Prev_Exit",
)
CHIP_BASES = ("", "current", "lag", "ahead")
CHIP_ACTIONS = ("", "sell", "add", "hold")

# Columns whose value is a price the owner is meant to place as an order, so
# each one must sit on the exchange's quote ladder (scanner/tick.py).
ORDER_LEVEL_COLUMNS = (
    "Strict_Stop_Loss", "Target_Price", "Trail_Arm_Price", "Trail_Lock_Price",
    "Add_Price", "Scale_Out_Price",
    "Fill_Stop_Loss", "Fill_Trail_Arm_Price", "Fill_Trail_Lock_Price",
    "Fill_Target_Price", "Fill_Scale_Out_Price",
    "Plan_Stop", "Plan_Add_Price",
    "Initial_Stop_Price", "Initial_Target_Price",
)


def _on_tick(price, stock_id=None):
    try:
        from scanner.tick import is_on_tick
    except Exception:
        return True
    return is_on_tick(price, stock_id)


_ID_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# Column registry. kind: id | str | date | num | int | bool | choice
#   nullable   None / "" allowed
#   lo, hi     plausible range (inclusive); a breach is a WARNING, not a fail
#   phone      the PWA reads this column; a missing phone column is an error,
#              a missing non-phone column a warning
#   choices    allowed values for kind == choice
def _c(kind, nullable=False, lo=None, hi=None, phone=False, choices=None):
    return {"kind": kind, "nullable": nullable, "lo": lo, "hi": hi,
            "phone": phone, "choices": choices}


# 2026-10-08 (stage F1): Gain_1M_Pct, Vol_MA20, Vol_Today, Core_Plus,
# Entry_Date, Entry_Open, Hold_Status, Fill_Target_Price and
# Initial_Stop/Target_Price became phone=True: the picks page now reads
# them (investor info, funnel line, hold group, card levels).
COLUMNS = {
    "Stock_ID":            _c("id", phone=True),
    "Stock_Name":          _c("str", phone=True),
    "Market":              _c("choice", choices=MARKETS, phone=True),
    "Data_Date":           _c("date", phone=True),
    "Close_Price":         _c("num", lo=0.01, hi=100000, phone=True),
    "Explosion_Score":     _c("num", lo=0, hi=100, phone=True),
    "Surge_Score":         _c("num", lo=0, hi=100, phone=True),
    "Launch_Score":        _c("num", lo=0, hi=100, phone=True),
    "Ret_5D_Pct":          _c("num", lo=-60, hi=100, phone=True),
    "ATR_Pct":             _c("num", lo=0, hi=50, phone=True),
    # Excess return against the index: positive is outperforming, and a name
    # that dropped off the list is routinely NEGATIVE (12 of 56 tracked rows
    # on 2026-09-21). The old floor of 0 described the daily list, not the
    # measure.
    "RS_Score":            _c("num", lo=-2000, hi=2000, phone=True),
    "Gain_3M_Pct":         _c("num", lo=-95, hi=2000, phone=True),
    "Gain_1M_Pct":         _c("num", lo=-95, hi=1000, phone=True),
    # NOT nullable: it is an entry-gate input, and a null on the daily list
    # means a row was scored on data that does not exist. A tracked row with
    # less than a year of history legitimately has none, and that surfaces as
    # a tracked_null WARNING rather than being waved through here.
    "Dist_52W_High_Pct":   _c("num", lo=0, hi=100, phone=True),
    "Sup_Gap_Pct":         _c("num", nullable=True, lo=-5, hi=100, phone=True),
    "Res_Gap_Pct":         _c("num", nullable=True, lo=-50, hi=300, phone=True),
    "Cond_A":              _c("bool", phone=True),
    "Cond_C":              _c("bool", phone=True),
    "Cond_B":              _c("bool", phone=True),
    "Squeeze":             _c("bool"),
    "Is_Golden_Signal":    _c("bool"),
    "Is_Breakout_Signal":  _c("bool"),
    "MA_Bull_Align":       _c("bool", phone=True),
    "Donchian_Break":      _c("bool", phone=True),
    "MACD_Cross":          _c("bool", phone=True),
    "MA_Squeeze":          _c("bool", phone=True),
    "Trend_Breakout":      _c("bool", phone=True),
    "MACD_Hist_Turn":      _c("bool", phone=True),
    "Near_52W_High":       _c("bool", phone=True),
    "RS_Strong":           _c("bool", phone=True),
    "Large_Holder_Pct":    _c("num", lo=0, hi=100, phone=True),
    "Large_Pct_Change":    _c("num", lo=-100, hi=100, phone=True),
    "Retail_Pct":          _c("num", lo=0, hi=100, phone=True),
    "Retail_Pct_Change":   _c("num", lo=-100, hi=100, phone=True),
    "Foreign_Net":         _c("num", phone=True),
    "Trust_Net":           _c("num", phone=True),
    "Foreign_Net_5D":      _c("num", phone=True),
    "Inst_Buy_Days":       _c("int", lo=0, hi=5, phone=True),
    # 2026-09-21 three-institution picture (ingestion/inst_trades.py)
    "Dealer_Net":          _c("num", nullable=True, phone=True),
    "Inst_Net":            _c("num", nullable=True, phone=True),
    "Inst_Net_5D":         _c("num", nullable=True, phone=True),
    "Trust_Net_5D":        _c("num", nullable=True, phone=True),
    "Inst_Streak":         _c("int", nullable=True, lo=-20, hi=20, phone=True),
    "Inst_Sessions":       _c("int", nullable=True, lo=0, hi=5, phone=True),
    "Inst_Date":           _c("date", nullable=True, phone=True),
    # chip verdict for the next open (scanner/chip_signal.py)
    # net institutional lots as a share of the 20-day average volume. A block
    # trade in a thin name legitimately exceeds one average day, so +-100 was
    # a bound that would fail the whole payload on an ordinary market event.
    "Inst_Pct":            _c("num", nullable=True, lo=-500, hi=500, phone=True),
    "Chip_Basis":          _c("choice", nullable=True, choices=CHIP_BASES, phone=True),
    "Chip_Action":         _c("choice", nullable=True, choices=CHIP_ACTIONS, phone=True),
    "Chip_Note":           _c("str", nullable=True, phone=True),
    "MA5":                 _c("num", lo=0.01),
    "MA10":                _c("num", lo=0.01),
    "MA20":                _c("num", lo=0.01),
    "MA60":                _c("num", lo=0.01),
    "Resist_60H":          _c("num", lo=0.01, phone=True),
    "Support_60L":         _c("num", lo=0.01, phone=True),
    "Support_20L":         _c("num", lo=0.01, phone=True),
    "Support_Used":        _c("num", nullable=True, lo=0.01, phone=True),
    "VP_Zone1":            _c("num", nullable=True, lo=0.01, phone=True),
    "VP_Zone2":            _c("num", nullable=True, lo=0.01, phone=True),
    "VP_Zone3":            _c("num", nullable=True, lo=0.01, phone=True),
    "Gap_Up_Sup":          _c("num", nullable=True, lo=0.01, phone=True),
    "Gap_Dn_Res":          _c("num", nullable=True, lo=0.01, phone=True),
    "Round_Level":         _c("num", nullable=True, lo=0.01, phone=True),
    "Range_Tightness":     _c("num", lo=0, hi=20, phone=True),
    "Volume_Dryup":        _c("num", lo=0, hi=20, phone=True),
    "Volume_Bias":         _c("num", lo=0, hi=5, phone=True),
    "Vol_MA20":            _c("num", lo=0, phone=True),
    "Vol_MA5":             _c("num", lo=0),
    "Vol_Today":           _c("int", lo=0, phone=True),
    "High_20_Prev":        _c("num", lo=0.01),
    "High_Today":          _c("num", lo=0.01),
    "Low_Today":           _c("num", lo=0.01),
    "Close_Prev":          _c("num", lo=0.01),
    "Min_Price_3":         _c("num", lo=0.01),
    "Cond_A_5D":           _c("bool"),
    "Integrity_OK":        _c("bool", phone=True),
    "Integrity_Flags":     _c("str", nullable=True, phone=True),
    "Recent_Jump":         _c("bool"),
    "Suggested_Buy_Price": _c("num", lo=0.01, phone=True),
    "Strict_Stop_Loss":    _c("num", lo=0.01, phone=True),
    # Derived from Strict_Stop_Loss, which is null when the close is missing
    # or zero; it used to be the constant 20.0 and could not be null at all.
    "Risk_Pct":            _c("num", nullable=True, lo=0, hi=50, phone=True),
    "Target_Price":        _c("num", lo=0.01, phone=True),
    "Trail_Arm_Price":     _c("num", lo=0.01),
    "Trail_Lock_Price":    _c("num", lo=0.01),
    "Add_Price":           _c("num", lo=0.01),
    # optional scale-out (2026-09-21): sell half at +15% from the fill
    "Scale_Out_Price":     _c("num", lo=0.01, phone=True),
    "Core_Plus":           _c("bool", phone=True),
    "Entry_Date":          _c("date", nullable=True, phone=True),
    "Exit_Date":           _c("date", nullable=True),
    # Nullable: a listed name with no ledger anchor has an UNKNOWN holding
    # day, and null is the honest value for that. Before 2026-09-21 the
    # registry demanded a number, so a single unanchored row failed the whole
    # payload -- the producer and the contract disagreed.
    "Hold_Day":            _c("int", nullable=True, lo=0, hi=400),
    "Hold_Remaining":      _c("int", nullable=True, lo=-400, hi=400),
    "Hold_Total":          _c("int", lo=1, hi=60, phone=True),
    "Hold_Cap":            _c("int", lo=1, hi=120, phone=True),
    "Hold_Status":         _c("choice", nullable=True, choices=HOLD_STATUSES,
                              phone=True),
    "Hold_Note":           _c("str", nullable=True),
    # 2026-09-23: the research definition of a new signal (absent on the
    # previous ledger session), separate from the streak behind Hold_Status.
    "First_Day":           _c("bool", nullable=True),
    "Entry_Open":          _c("num", nullable=True, lo=0.01, phone=True),
    "Fill_Stop_Loss":      _c("num", nullable=True, lo=0.01),
    "Fill_Trail_Arm_Price": _c("num", nullable=True, lo=0.01),
    "Fill_Trail_Lock_Price": _c("num", nullable=True, lo=0.01),
    "Fill_Target_Price":   _c("num", nullable=True, lo=0.01, phone=True),
    "Fill_Scale_Out_Price": _c("num", nullable=True, lo=0.01, phone=True),
    # exit plan (holding_tracker, 2026-09-14): the one price to act on
    "Plan_Stop":           _c("num", nullable=True, lo=0.01, phone=True),
    "Plan_Armed":          _c("bool", phone=True),
    # optional staged entry (2026-09-20): buy the rest at fill x (1 - ADD_PCT)
    "Plan_Add_Price":      _c("num", nullable=True, lo=0.01, phone=True),
    "Add_Hit_Date":        _c("date", nullable=True, phone=True),
    "Exit_Signal":         _c("choice", nullable=True, choices=EXIT_SIGNALS,
                              phone=True),
    "Exit_Signal_Date":    _c("date", nullable=True, phone=True),
    "Exit_Signal_Price":   _c("num", nullable=True, lo=0.01, phone=True),
    "Exit_Note":           _c("str", nullable=True),
    # segments (2026-10-08): the signal day the card's trade is anchored to,
    # and why -- first appearance, a long gap, a re-entry after the previous
    # trade closed (or was not a signal), or a recommendation
    "Hold_Anchor":         _c("date", nullable=True),
    "Hold_Anchor_Kind":    _c("choice", nullable=True, choices=ANCHOR_KINDS),
    # the previous trade (holding_tracker.PREV_COLUMNS), all null unless a
    # re-entry or a recommendation anchor opened the current one
    "Prev_Signal_Date":    _c("date", nullable=True, phone=True),
    "Prev_Was_Signal":     _c("bool", nullable=True, phone=True),
    "Prev_Entry_Date":     _c("date", nullable=True, phone=True),
    "Prev_Entry_Open":     _c("num", nullable=True, lo=0.01, phone=True),
    "Prev_Exit_Signal":    _c("choice", nullable=True, choices=EXIT_SIGNALS,
                              phone=True),
    "Prev_Exit_Signal_Date": _c("date", nullable=True, phone=True),
    "Prev_Exit_Signal_Price": _c("num", nullable=True, lo=0.01, phone=True),
    "Prev_Exit_Ret_Pct":   _c("num", nullable=True, lo=-100, hi=1000, phone=True),
    "Sessions_Since_Prev_Exit": _c("int", nullable=True, lo=0, hi=400, phone=True),
    "Buy_Ready":           _c("bool", phone=True),
    "Buy_Block":           _c("choice", nullable=True, choices=BUY_BLOCKS, phone=True),
    "Recommendation_ID":   _c("str", nullable=True, phone=True),
    "Initial_Buy_Price":   _c("num", nullable=True, lo=0.01, phone=True),
    "Initial_Stop_Price":  _c("num", nullable=True, lo=0.01, phone=True),
    "Initial_Target_Price": _c("num", nullable=True, lo=0.01, phone=True),
    "Recommended_On":      _c("date", nullable=True, phone=True),
    "Rec_Status":          _c("choice", nullable=True, choices=REC_STATUSES,
                              phone=True),
    "Rec_Valid_Until":     _c("date", nullable=True, phone=True),
    # why a recommendation ended (the exit reason of a closed one); null while
    # it is active
    "Rec_Status_Reason":   _c("str", nullable=True, phone=True),
    # trade restrictions (scanner/trade_restrictions, 2026-10-08). The kind is
    # NOT nullable: "none" is spelled out, so a missing value means the scan
    # never checked the lists.
    "Trade_Restriction":   _c("choice", choices=RESTRICTION_KINDS, phone=True),
    "Restriction_Flags":   _c("str", nullable=True, phone=True),
    "Restriction_Since":   _c("date", nullable=True, phone=True),
    "Restriction_Until":   _c("date", nullable=True, phone=True),
    "Restriction_Match_Min": _c("int", nullable=True, lo=1, hi=60, phone=True),
    "Restriction_Prepay":  _c("choice", nullable=True,
                              choices=RESTRICTION_PREPAY, phone=True),
    # company events (ingestion/company_events, 2026-10-08). DISPLAY ONLY:
    # no score, gate or exit reads them. app.js renders them (stage F1)
    # but they stay phone=False on purpose: the card simply omits the line
    # when a value is absent, and an events-feed outage must not fail the
    # whole payload (test_missing_event_column_only_warns).
    "Rev_Month":           _c("str", nullable=True),
    "Rev_Amount_K":        _c("num", nullable=True, lo=0),
    "Rev_YoY_Pct":         _c("num", nullable=True, lo=-100, hi=100000),
    "Rev_MoM_Pct":         _c("num", nullable=True, lo=-100, hi=100000),
    "Rev_Cum_YoY_Pct":     _c("num", nullable=True, lo=-100, hi=100000),
    "Ex_Date":             _c("date", nullable=True),
    "Ex_Kind":             _c("choice", nullable=True, choices=EX_KINDS),
    "Ex_Cash_Div":         _c("num", nullable=True, lo=0, hi=1000),
    "Conf_Date":           _c("date", nullable=True),
}

# Flag prefixes data_integrity uses for UNAMBIGUOUS data errors; everything
# else it emits (jump / recent_jump / gap / short_*) is an observation about a
# series that is still trustworthy.
# "split:" joins these 2026-09-22: a step beyond any legal move means the
# series carries two price bases, so it is a data error, not an observation
# about a volatile stock.
_HARD_FLAGS = ("nan:", "nonpos:", "ohlc:", "dup:", "split:")

# Tolerances for identity checks on rounded prices.
_PRICE_TOL = 0.011      # two-decimal rounding on both sides
_PCT_TOL = 0.02         # percent columns are rounded to 2 dp


# --------------------------------------------------------------------------
# value helpers
# --------------------------------------------------------------------------
def _is_null(v):
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def _num(v):
    """float or None. Accepts numeric strings (ledger money is exported as a
    two-decimal string). Never raises."""
    if _is_null(v) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _is_bool(v):
    return isinstance(v, bool)


def _is_int(v):
    """Is this a whole number?

    JSON has one number type, so 24 and 24.0 are the same value; which one
    lands in the file depends on whether pandas gave the column an int64 or a
    float64 dtype, and that in turn depends on whether ANY row in it was null.
    The 2026-09-21 end-to-end run caught exactly that: one listed name with no
    ledger anchor turned Hold_Day into float64 and every other row failed this
    rule as a "wrong type". That is an artefact of serialization, not a defect
    in the data, so an integral float counts as an integer. A fractional one
    still does not.
    """
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, float) and v == v and float(v).is_integer()


def _date_ok(v):
    return isinstance(v, str) and bool(_DATE_RE.match(v[:10])) and len(v) >= 10


def _garbled(s):
    """Replacement chars or lone surrogates mean the name lost its encoding."""
    if not isinstance(s, str):
        return False
    for ch in s:
        o = ord(ch)
        if o == 0xFFFD or 0xD800 <= o <= 0xDFFF:
            return True
    return False


class _Report:
    """Collects findings. `block` names which part of the payload is being
    audited; anything other than "rows" has its codes prefixed so a problem in
    the tracked block can never be mistaken for one in the daily list."""

    def __init__(self):
        self.items = []
        self.block = "rows"

    def add(self, level, code, column, count=1, detail="", sample=None):
        if count <= 0:
            return
        if self.block and self.block != "rows":
            code = "%s_%s" % (self.block, code)
            detail = "%s [%s]" % (detail, self.block) if detail else                 "[%s]" % self.block
        item = {"level": level, "code": code, "column": column or "",
                "count": int(count), "detail": str(detail)[:200]}
        if sample:
            item["sample"] = [str(s) for s in list(sample)[:8]]
        self.items.append(item)

    def error(self, *a, **k):
        self.add("error", *a, **k)

    def warn(self, *a, **k):
        self.add("warn", *a, **k)

    def info(self, *a, **k):
        self.add("info", *a, **k)


# --------------------------------------------------------------------------
# column registry checks
# --------------------------------------------------------------------------
def _split_truncated(row):
    """True when this row's price series was cut at an un-adjusted corporate
    action, so its rolling figures are legitimately null.

    scanner/chip_verifier drops the bars before a split-scale step rather than
    average across two price units (see scanner/data_integrity.SPLIT_RATIO).
    What is left is often too short for MA20, MA60, the 52-week distance or a
    three-month change, and NULL is the honest answer -- the alternative is the
    figure this guard exists to stop, 6949's MA20 of 511.15 against a close of
    50.80. The exemption is per ROW and keyed on that stock's own flag, so the
    contract stays intact for every other name.
    """
    return "split:" in str(row.get("Integrity_Flags") or "")


def _check_columns(rows, rep):
    if not rows:
        return
    split_rows = [r for r in rows if _split_truncated(r)]
    if split_rows:
        rep.info("split_truncated", "Integrity_Flags", len(split_rows),
                 "series cut at an un-adjusted corporate action; its rolling "
                 "columns are null on purpose",
                 sample=[str(r.get("Stock_ID")) for r in split_rows])
    present = set()
    for r in rows:
        present.update(r.keys())

    for col, spec in COLUMNS.items():
        if col not in present:
            if spec["phone"]:
                rep.error("column_missing", col, 1, "phone reads this column")
            else:
                rep.warn("column_missing", col, 1, "registered column absent")
    for col in sorted(present - set(COLUMNS)):
        rep.info("column_unregistered", col, 1,
                 "not in the registry; add it so it gets checked")

    for col, spec in COLUMNS.items():
        if col not in present:
            continue
        kind = spec["kind"]
        nulls, bad_type, out_lo, out_hi, bad_choice, garbled = 0, 0, 0, 0, 0, 0
        samples = []
        for r in rows:
            v = r.get(col)
            if _is_null(v):
                # A row whose series was cut at a corporate action is allowed
                # to be null here, and ONLY here: its type and range checks
                # below still apply to whatever it does carry.
                if not _split_truncated(r):
                    nulls += 1
                continue
            if kind == "id":
                if not (isinstance(v, str) and _ID_RE.match(v.strip())):
                    bad_type += 1
                    samples.append(v)
            elif kind == "str":
                if not isinstance(v, str):
                    bad_type += 1
                    samples.append(v)
                elif _garbled(v):
                    garbled += 1
                    samples.append(r.get("Stock_ID"))
            elif kind == "date":
                if not _date_ok(v):
                    bad_type += 1
                    samples.append(v)
            elif kind == "bool":
                if not _is_bool(v):
                    bad_type += 1
                    samples.append(v)
            elif kind == "int":
                if not _is_int(v):
                    bad_type += 1
                    samples.append(v)
                else:
                    if spec["lo"] is not None and v < spec["lo"]:
                        out_lo += 1
                        samples.append("{}={}".format(r.get("Stock_ID"), v))
                    if spec["hi"] is not None and v > spec["hi"]:
                        out_hi += 1
                        samples.append("{}={}".format(r.get("Stock_ID"), v))
            elif kind == "num":
                f = _num(v)
                if f is None or _is_bool(v):
                    bad_type += 1
                    samples.append(v)
                else:
                    if spec["lo"] is not None and f < spec["lo"]:
                        out_lo += 1
                        samples.append("{}={}".format(r.get("Stock_ID"), v))
                    if spec["hi"] is not None and f > spec["hi"]:
                        out_hi += 1
                        samples.append("{}={}".format(r.get("Stock_ID"), v))
            elif kind == "choice":
                if v not in spec["choices"]:
                    bad_choice += 1
                    samples.append(v)

        if nulls and not spec["nullable"]:
            rep.error("null", col, nulls, "null/empty in a non-nullable column")
        if nulls == len(rows) and spec["nullable"] and spec["kind"] != "str" \
                and col not in ("Entry_Date", "Exit_Date") \
                and not col.startswith(("Initial_", "Rec", "Prev_",
                                        "Sessions_Since_Prev", "Restriction_",
                                        "Ex_", "Conf_")):
            rep.info("all_null", col, nulls, "every row is null (may be normal)")
        if bad_type:
            rep.error("type", col, bad_type,
                      "wrong type for kind {}".format(kind), sample=samples)
        if garbled:
            rep.error("garbled_text", col, garbled,
                      "replacement or surrogate characters", sample=samples)
        if bad_choice:
            rep.error("choice", col, bad_choice,
                      "value outside {}".format(list(spec["choices"])),
                      sample=samples)
        if out_lo:
            rep.warn("range_low", col, out_lo, "below {}".format(spec["lo"]),
                     sample=samples)
        if out_hi:
            rep.warn("range_high", col, out_hi, "above {}".format(spec["hi"]),
                     sample=samples)

    # The recommendation family is all-null whenever no listed stock has an
    # active recommendation -- one note, not six.
    rec_cols = [c for c in ("Recommendation_ID", "Initial_Buy_Price",
                            "Recommended_On", "Rec_Status") if c in present]
    if rec_cols and all(_is_null(r.get(c)) for r in rows for c in rec_cols):
        rep.info("all_null", "Recommendation_*", len(rows),
                 "no listed stock carries a frozen recommendation")


# --------------------------------------------------------------------------
# row identities
# --------------------------------------------------------------------------
def _core_plus_expected(r):
    try:
        from scanner.scan_mode import (CORE_PLUS_DIST52_MAX, CORE_PLUS_RET5_MAX,
                                       CORE_PLUS_ATR_MIN)
    except Exception:
        return None
    d, ret5, atr = (_num(r.get("Dist_52W_High_Pct")), _num(r.get("Ret_5D_Pct")),
                    _num(r.get("ATR_Pct")))
    if d is None or ret5 is None or atr is None:
        return False
    return bool(d <= CORE_PLUS_DIST52_MAX and ret5 <= CORE_PLUS_RET5_MAX
                and atr >= CORE_PLUS_ATR_MIN)


def _trade_params():
    try:
        from scanner.scan_mode import (PRELAUNCH_STOP_PCT, PRELAUNCH_TP_PCT,
                                       PRELAUNCH_TRAIL_ARM, PRELAUNCH_TRAIL_LOCK,
                                       PRELAUNCH_ADD_PCT, PRELAUNCH_SCALE_OUT_PCT,
                                       N_ENTER)
        return (PRELAUNCH_STOP_PCT, PRELAUNCH_TP_PCT, PRELAUNCH_TRAIL_ARM,
                PRELAUNCH_TRAIL_LOCK, PRELAUNCH_ADD_PCT, PRELAUNCH_SCALE_OUT_PCT,
                N_ENTER)
    except Exception:
        return 0.20, 0.20, 0.025, 0.02, 0.10, 0.15, 20


def _lvl(base, pct, direction, stock_id=None):
    """The published level for base * (1 + pct): snapped onto the exchange's
    quote ladder, the same way scan_mode and holding_tracker compute it.

    `stock_id` picks the ladder -- an ETF steps 0.05 above 50 where an
    ordinary share steps 0.50 -- so this stays identical to the producers.
    """
    try:
        from scanner.tick import round_to_tick
    except Exception:
        return round(base * (1 + pct), 2)
    got = round_to_tick(base * (1 + pct), direction, stock_id)
    return got if got is not None else round(base * (1 + pct), 2)


def _check_prev(r, sid, data_date, hit):
    """Prev_* integrity (2026-10-08). The group is all-null, or carries the
    previous signal, its entry and an exit verdict ('' = the old trade was
    still open when the current one began); an exit verdict brings its date,
    price, net return and the session count with it. Dates run signal <
    entry <= exit <= data date, and the old trade starts before the current
    one. The group and Hold_Anchor_Kind agree: a 'reentry' anchor always has
    a previous trade, and only a 'reentry' or 'rec' anchor may carry one."""
    vals = {c: r.get(c) for c in PREV_COLUMNS}
    kind = r.get("Hold_Anchor_Kind")
    kind = "" if _is_null(kind) else str(kind).strip()
    if all(_is_null(v) for v in vals.values()):
        if kind == "reentry":
            hit("prev_segment_kind", sid, "Hold_Anchor_Kind")
        return
    hit("prev_segment", sid, "Prev_Signal_Date")
    if "Hold_Anchor_Kind" in r and kind not in ("reentry", "rec"):
        hit("prev_segment_kind", sid, "Hold_Anchor_Kind")
    psd, ped = vals["Prev_Signal_Date"], vals["Prev_Entry_Date"]
    sig = vals["Prev_Exit_Signal"]
    exit_cols = ("Prev_Exit_Signal_Date", "Prev_Exit_Signal_Price",
                 "Prev_Exit_Ret_Pct", "Sessions_Since_Prev_Exit")
    partial = _is_null(psd) or _is_null(ped) or not isinstance(sig, str)
    if isinstance(sig, str):
        if sig.strip():
            partial = partial or any(_is_null(vals[c]) for c in exit_cols)
        else:
            partial = partial or any(not _is_null(vals[c]) for c in exit_cols)
    if partial:
        hit("prev_segment_partial", sid, "Prev_Signal_Date")
        return
    psd, ped = str(psd)[:10], str(ped)[:10]
    pxd = vals["Prev_Exit_Signal_Date"]
    pxd = None if _is_null(pxd) else str(pxd)[:10]
    entry = r.get("Entry_Date")
    entry = None if _is_null(entry) else str(entry)[:10]
    anchor = r.get("Hold_Anchor")
    anchor = None if _is_null(anchor) else str(anchor)[:10]
    n = vals["Sessions_Since_Prev_Exit"]
    bad = not psd < ped
    if pxd is not None:
        bad = bad or not ped <= pxd or (bool(data_date) and pxd > data_date)
        bad = bad or (anchor is not None and pxd > anchor)
    bad = bad or (entry is not None and not ped < entry)
    bad = bad or (anchor is not None and not psd < anchor)
    bad = bad or (_is_int(n) and n < 0)
    if bad:
        hit("prev_segment_order", sid, "Prev_Signal_Date")


_REC_ROW_CODES = ("rec_segment_mismatch", "rec_state_mismatch",
                  "rec_vs_card_consistency", "rec_target_mismatch")


def _check_rec_card(r, sid, status, data_date, hit, taiex_asof=""):
    """A row carrying a recommendation shows THAT trade (holding_tracker
    rec_anchors, portfolio.sync.load_rec_anchors). Keyed on Hold_Anchor_Kind
    and Hold_Anchor: the tracker keeps the natural anchor when the
    recommendation's trade closed on or before a newer segment's start.

    `taiex_asof` is meta.regime.as_of_date. A TIME exit on a session the
    TAIEX feed has not reached is provisional -- the market leg may still
    keep the trade -- so the lifecycle leaves the recommendation active
    (portfolio.sync._leg_unknown) while the card already shows the exit."""
    rst = r.get("Rec_Status")
    if rst not in ("active", "closed"):
        return
    ron = str(r.get("Recommended_On") or "")[:10]
    vu = str(r.get("Rec_Valid_Until") or "")[:10]
    entry = str(r.get("Entry_Date") or "")[:10]
    bar = str(r.get("Data_Date") or data_date or "")[:10]
    has_kind = "Hold_Anchor_Kind" in r
    kind = r.get("Hold_Anchor_Kind")
    anchor = str(r.get("Hold_Anchor") or "")[:10]
    on_rec = (kind == "rec") if has_kind else True
    sig = r.get("Exit_Signal")

    if rst == "active":
        if has_kind and (kind != "rec" or anchor != ron):
            hit("rec_segment_mismatch", sid, "Hold_Anchor")
        elif status == "pending":
            if ron != bar:
                hit("rec_segment_mismatch", sid, "Recommended_On")
        elif vu and entry and entry != vu:
            hit("rec_segment_mismatch", sid, "Entry_Date")
        if status == "exited":
            # whichever trade the card shows, an active recommendation's
            # trade cannot be over: the lifecycle would have closed it --
            # unless it deferred a time exit the index feed cannot judge yet
            xd = str(r.get("Exit_Signal_Date") or "")[:10]
            deferred = (sig == "time" and bool(taiex_asof) and bool(xd)
                        and xd > taiex_asof)
            if not deferred:
                hit("rec_state_mismatch", sid, "Rec_Status")
        if on_rec and status == "pending" and "Plan_Stop" in r:
            want = _num(r.get("Initial_Stop_Price"))
            got = _num(r.get("Plan_Stop"))
            if want is not None and (got is None or abs(got - want) > _PRICE_TOL):
                hit("rec_vs_card_consistency", sid, "Plan_Stop")
            tgt = _num(r.get("Initial_Target_Price"))
            ref = _num(r.get("Target_Price"))
            if tgt is not None and ref is not None and abs(ref - tgt) > _PRICE_TOL:
                hit("rec_target_mismatch", sid, "Target_Price")
        return
    # closed (attached for a few sessions after its exit)
    if on_rec:
        if has_kind and anchor != ron:
            hit("rec_segment_mismatch", sid, "Hold_Anchor")
        elif vu and entry and entry != vu:
            hit("rec_segment_mismatch", sid, "Entry_Date")
        if _is_null(sig):
            hit("rec_state_mismatch", sid, "Exit_Signal")
    elif not anchor or not ron or anchor <= ron:
        # the natural anchor may only win with a NEWER trade
        hit("rec_segment_mismatch", sid, "Hold_Anchor")


def _blocking_restrictions(meta):
    """The restriction kinds that block a buy, as the PAYLOAD declares them
    (meta.quality.restrictions.blocking), else trade_restrictions' constant."""
    q = (meta or {}).get("quality") if isinstance(meta, dict) else None
    r = q.get("restrictions") if isinstance(q, dict) else None
    if isinstance(r, dict) and isinstance(r.get("blocking"), list):
        return tuple(str(k) for k in r["blocking"])
    try:
        from scanner.trade_restrictions import BLOCKING_RESTRICTIONS
        return tuple(BLOCKING_RESTRICTIONS)
    except Exception:
        return DEFAULT_BLOCKING_RESTRICTIONS


def _ex_today_ids(meta, data_date):
    """Ids with an ex-dividend / ex-rights entry on the payload's session, as
    meta.quality.restrictions.ex_today declares them. On such a day the
    exchange's limits come from the reduced reference price, which the row
    does not carry, so the raw-close recomputation below cannot judge them."""
    q = (meta or {}).get("quality") if isinstance(meta, dict) else None
    r = q.get("restrictions") if isinstance(q, dict) else None
    ex = r.get("ex_today") if isinstance(r, dict) else None
    if not isinstance(ex, dict) or not isinstance(ex.get("ids"), list):
        return frozenset()
    if data_date and str(ex.get("date") or "")[:10] not in ("", data_date):
        return frozenset()
    return frozenset(str(i) for i in ex["ids"])


_RESTRICTION_DETAIL = ("Restriction_Since", "Restriction_Until",
                       "Restriction_Match_Min", "Restriction_Prepay")


def _check_restriction(r, sid, br, bb, data_date, blocking, hit,
                       ex_ids=frozenset()):
    """The six restriction columns against each other and the buy gate."""
    kind = r.get("Trade_Restriction")
    raw = r.get("Restriction_Flags")
    flags = set() if _is_null(raw) else {
        f.strip() for f in str(raw).split(",") if f.strip()}
    if isinstance(kind, str) and kind:
        if br is True and kind in blocking:
            hit("restricted_buy_ready", sid, "Buy_Ready")
        if kind not in ("none",):
            hit("restricted_rows", "{}:{}".format(sid, kind), "Trade_Restriction")
        # the kind is the most severe flag; display-only flags never count
        ranked = [f for f in flags if f in RESTRICTION_KINDS and f != "none"]
        want = (min(ranked, key=RESTRICTION_KINDS.index) if ranked else "none")
        if kind in RESTRICTION_KINDS and kind != want:
            hit("restriction_flags_mismatch", sid, "Restriction_Flags")
    if bb == "restricted" and kind not in blocking:
        hit("restricted_block_mismatch", sid, "Buy_Block")
    if "disposition" in flags or kind == "disposition":
        until = r.get("Restriction_Until")
        if _is_null(until) or (data_date and str(until)[:10] < data_date):
            hit("restriction_until_missing", sid, "Restriction_Until")
    elif any(not _is_null(r.get(c)) for c in _RESTRICTION_DETAIL):
        hit("restriction_orphan_detail", sid, "Restriction_Since")
    # signal-day limit lock, recomputed: Close_Prev x 1.10 rounded down onto
    # the ladder (trade_restrictions.limit_flags); skipped on a recent jump,
    # on a row whose own bar is not the session's (not flagged there) and on
    # an ex-date (the limits come from the reference price, not Close_Prev)
    own = r.get("Data_Date")
    own_bar = _is_null(own) or not data_date or str(own)[:10] == data_date
    if own_bar and not r.get("Recent_Jump") and "Restriction_Flags" in r \
            and sid not in ex_ids:
        prev, close = _num(r.get("Close_Prev")), _num(r.get("Close_Price"))
        if prev and prev > 0 and close is not None:
            try:
                from scanner.tick import round_to_tick
                up = round_to_tick(prev * 1.10, "down", sid)
            except Exception:
                up = None
            if up is not None and (abs(close - up) < 1e-6) != ("limit_lock" in flags):
                hit("limit_lock_mismatch", sid, "Restriction_Flags")


_EVENT_WARN_CODES = ("rev_month_format", "rev_month_future",
                     "rev_fields_without_month", "ex_fields_without_date",
                     "ex_date_not_after_session")


def _month_index(ym):
    return int(ym[:4]) * 12 + int(ym[5:7]) - 1


def _check_event_cols(r, sid, data_date, hit):
    """Row identities of the display-only company-event columns."""
    rm = r.get("Rev_Month")
    if _is_null(rm):
        for col in ("Rev_Amount_K", "Rev_YoY_Pct", "Rev_MoM_Pct",
                    "Rev_Cum_YoY_Pct"):
            if not _is_null(r.get(col)):
                hit("rev_fields_without_month", sid, col)
                break
    elif not (isinstance(rm, str) and _MONTH_RE.match(rm)):
        hit("rev_month_format", sid, "Rev_Month")
    elif _DATE_RE.match(data_date or ""):
        if rm >= data_date[:7]:
            hit("rev_month_future", sid, "Rev_Month")
        elif _month_index(data_date[:7]) - _month_index(rm) > 2:
            hit("rev_month_old", sid, "Rev_Month")
    ex = r.get("Ex_Date")
    if _is_null(ex):
        if not _is_null(r.get("Ex_Kind")) or not _is_null(r.get("Ex_Cash_Div")):
            hit("ex_fields_without_date", sid, "Ex_Date")
    else:
        # annotate_events sets Ex_Date and Ex_Kind together; a date with no
        # kind is a half-written event the phone would show as a bare date
        if _is_null(r.get("Ex_Kind")):
            hit("ex_fields_without_date", sid, "Ex_Kind")
        if data_date and str(ex)[:10] <= data_date:
            hit("ex_date_not_after_session", sid, "Ex_Date")


def _check_rows(rows, meta, rep, scan_mode):
    if not rows:
        return
    data_date = str(meta.get("data_date") or "")[:10]
    reg = meta.get("regime") if isinstance(meta.get("regime"), dict) else {}
    taiex_asof = str(reg.get("as_of_date") or "")[:10]
    (stop_pct, tp_pct, arm_pct, lock_pct, add_pct, out_pct,
     n_enter) = _trade_params()
    prelaunch = scan_mode == "mode_prelaunch"
    blocking = _blocking_restrictions(meta)
    ex_ids = _ex_today_ids(meta, data_date)

    counters = {}
    samples = {}

    def hit(code, sid, col=""):
        counters[code] = counters.get(code, 0) + 1
        samples.setdefault(code, []).append(sid)
        cols[code] = col

    cols = {}
    ids_seen = {}
    for rank, r in enumerate(rows):
        sid = str(r.get("Stock_ID") or "").strip()
        ids_seen[sid] = ids_seen.get(sid, 0) + 1

        close = _num(r.get("Close_Price"))
        hi, lo = _num(r.get("High_Today")), _num(r.get("Low_Today"))
        prev = _num(r.get("Close_Prev"))
        if close is not None and hi is not None and lo is not None:
            if not (lo - _PRICE_TOL <= close <= hi + _PRICE_TOL) or lo > hi + _PRICE_TOL:
                hit("close_outside_range", sid, "Close_Price")
        if close is not None and prev:
            move = close / prev - 1
            if abs(move) > 0.105 and not r.get("Recent_Jump"):
                hit("daily_move_over_limit", sid, "Close_Prev")

        # stale row: its own bar is older than the session
        bar = str(r.get("Data_Date") or "")[:10]
        if data_date and bar and bar != data_date:
            hit("row_stale", sid, "Data_Date")
            if r.get("Buy_Ready") is True:
                hit("buy_ready_on_stale_row", sid, "Buy_Ready")

        # support / resistance gaps (analyzer.support_resistance.calc_squeeze_score:
        # Sup_Gap is measured against the SUPPORT, Res_Gap against the close;
        # both are None when resist <= support or a level is missing)
        sup, res = _num(r.get("Support_Used")), _num(r.get("Resist_60H"))
        sg, rg = _num(r.get("Sup_Gap_Pct")), _num(r.get("Res_Gap_Pct"))
        if close and sup and sg is not None:
            if abs((close - sup) / sup * 100 - sg) > _PCT_TOL + 0.01:
                hit("sup_gap_mismatch", sid, "Sup_Gap_Pct")
        if close and res is not None and rg is not None:
            if abs((res - close) / close * 100 - rg) > _PCT_TOL + 0.01:
                hit("res_gap_mismatch", sid, "Res_Gap_Pct")
        if sg is not None and rg is not None and _is_bool(r.get("Squeeze")):
            if r["Squeeze"] != bool(sg < 5.0 and 0 < rg < 2.0):
                hit("squeeze_mismatch", sid, "Squeeze")

        # trade levels
        buy = _num(r.get("Suggested_Buy_Price"))
        stop = _num(r.get("Strict_Stop_Loss"))
        tgt = _num(r.get("Target_Price"))
        arm = _num(r.get("Trail_Arm_Price"))
        lock = _num(r.get("Trail_Lock_Price"))
        if buy is not None and stop is not None and not stop < buy:
            hit("stop_not_below_entry", sid, "Strict_Stop_Loss")
        if buy is not None and tgt is not None and not tgt > buy:
            hit("target_not_above_entry", sid, "Target_Price")
        if arm is not None and lock is not None and not lock < arm:
            hit("trail_lock_not_below_arm", sid, "Trail_Lock_Price")
        if prelaunch and close is not None and buy is not None:
            if abs(buy - close) > _PRICE_TOL:
                hit("entry_ref_not_close", sid, "Suggested_Buy_Price")
            # Since 2026-09-21 every level is snapped onto the exchange's
            # quote ladder, so the identity is "the rounded level", not the raw
            # multiplication: 191.50 x 0.80 = 153.20 is not an orderable price.
            if stop is not None and abs(stop - _lvl(close, -stop_pct, "down", sid)) > _PRICE_TOL:
                hit("stop_pct_mismatch", sid, "Strict_Stop_Loss")
            if tgt is not None and abs(tgt - _lvl(close, tp_pct, "up", sid)) > _PRICE_TOL:
                hit("target_pct_mismatch", sid, "Target_Price")
            risk = _num(r.get("Risk_Pct"))
            if risk is not None and close and stop is not None:
                if abs(risk - (close - stop) / close * 100) > 0.11:
                    hit("risk_pct_mismatch", sid, "Risk_Pct")
            add = _num(r.get("Add_Price"))
            if add is not None and abs(add - _lvl(close, -add_pct, "down", sid)) > _PRICE_TOL:
                hit("add_pct_mismatch", sid, "Add_Price")
            out = _num(r.get("Scale_Out_Price"))
            if out is not None and abs(out - _lvl(close, out_pct, "up", sid)) > _PRICE_TOL:
                hit("scale_out_pct_mismatch", sid, "Scale_Out_Price")

        # Core_Plus derives from three columns on the same row
        if prelaunch and "Core_Plus" in r:
            exp = _core_plus_expected(r)
            if exp is not None and _is_bool(r.get("Core_Plus")) and r["Core_Plus"] != exp:
                hit("core_plus_mismatch", sid, "Core_Plus")

        # integrity flag text agrees with the flag
        #
        # data_integrity separates HARD errors (NaN / non-positive / broken
        # OHLC ordering / duplicate dates), which make a series untrustworthy,
        # from SOFT observations (a >10.5% close-to-close move, a gap, a short
        # history), which are reported and still trustworthy -- see that
        # module's docstring. Warning on any flag at all therefore fired every
        # day on perfectly good rows: the 2026-09-20 payload carried it for two
        # OTC names whose only flag was "jump". Only a HARD flag contradicts
        # Integrity_OK being true; soft ones are counted as information.
        iok = r.get("Integrity_OK")
        flags = r.get("Integrity_Flags")
        if _is_bool(iok):
            if iok and not _is_null(flags):
                if any(k in str(flags) for k in _HARD_FLAGS):
                    hit("integrity_flags_on_ok_row", sid, "Integrity_Flags")
                else:
                    counters["integrity_soft_flags"] = counters.get(
                        "integrity_soft_flags", 0) + 1
                    samples.setdefault("integrity_soft_flags", []).append(sid)
                    cols["integrity_soft_flags"] = "Integrity_Flags"
            if not iok and _is_null(flags):
                hit("integrity_fail_without_flags", sid, "Integrity_Flags")
            if not iok:
                counters["integrity_not_ok"] = counters.get("integrity_not_ok", 0) + 1

        # holding annotation identities
        status = r.get("Hold_Status")
        if isinstance(status, str) and status:
            hd, hr, ht, hc = (r.get("Hold_Day"), r.get("Hold_Remaining"),
                              r.get("Hold_Total"), r.get("Hold_Cap"))
            entry, exit_ = r.get("Entry_Date"), r.get("Exit_Date")
            fill = _num(r.get("Entry_Open"))
            if _is_int(hd) and _is_int(hr) and _is_int(ht):
                if status == "pending":
                    if hd != 0 or hr != ht:
                        hit("hold_pending_counts", sid, "Hold_Day")
                elif status == "exited" or (
                        status == "exit_today" and not _is_null(r.get("Exit_Signal"))):
                    # The exit stack closed this trade, so Hold_Day is the day
                    # it closed on and nothing remains. "day + remaining ==
                    # total" describes an OPEN hold and does not apply. Since
                    # 2026-10-08 an exit booked on TODAY's bar is exit_today
                    # with its signal, and closes the hold the same way.
                    if hr != 0:
                        hit("exited_row_has_days_left", sid, "Hold_Remaining")
                elif hd + hr != ht:
                    hit("hold_day_arithmetic", sid, "Hold_Remaining")
            if _is_int(ht) and _is_int(hc) and hc < ht:
                hit("hold_cap_below_total", sid, "Hold_Cap")
            if status == "pending":
                if fill is not None:
                    hit("fill_on_pending_row", sid, "Entry_Open")
            else:
                if _is_null(entry):
                    hit("held_row_without_entry_date", sid, "Entry_Date")
                elif data_date and str(entry)[:10] > data_date:
                    hit("entry_date_in_future", sid, "Entry_Date")
                if fill is None:
                    hit("held_row_without_fill", sid, "Entry_Open")
                else:
                    pairs = (("Fill_Stop_Loss", -stop_pct, "down"),
                             ("Fill_Trail_Arm_Price", arm_pct, "up"),
                             ("Fill_Trail_Lock_Price", lock_pct, "down"),
                             ("Fill_Target_Price", tp_pct, "up"),
                             ("Fill_Scale_Out_Price", out_pct, "up"))
                    for col, pct, side in pairs:
                        if col not in r:
                            continue
                        got = _num(r.get(col))
                        if got is None or abs(got - _lvl(fill, pct, side, sid)) > _PRICE_TOL:
                            hit("fill_level_mismatch", sid, col)
                            break
            if not _is_null(entry) and not _is_null(exit_) and str(exit_)[:10] < str(entry)[:10]:
                hit("exit_before_entry", sid, "Exit_Date")
            if status in ("overdue",):
                counters["hold_overdue"] = counters.get("hold_overdue", 0) + 1

            # exit plan: the stop the phone shows must be the rule's stop
            plan_stop = _num(r.get("Plan_Stop"))
            sig = r.get("Exit_Signal")
            armed = r.get("Plan_Armed")
            if "Plan_Stop" in r:
                if status == "pending":
                    if not _is_null(sig):
                        hit("exit_signal_on_pending_row", sid, "Exit_Signal")
                    if plan_stop is not None and stop is not None \
                            and abs(plan_stop - stop) > _PRICE_TOL:
                        # a row anchored to a recommendation shows the
                        # recommendation's frozen stop before entry
                        rec_stop = _num(r.get("Initial_Stop_Price"))
                        if rec_stop is None or abs(plan_stop - rec_stop) > _PRICE_TOL:
                            hit("plan_stop_pending_mismatch", sid, "Plan_Stop")
                elif fill is not None and plan_stop is not None and _is_bool(armed):
                    want = (_lvl(fill, lock_pct, "down", sid) if armed
                            else _lvl(fill, -stop_pct, "down", sid))
                    if abs(plan_stop - want) > _PRICE_TOL:
                        hit("plan_stop_level_mismatch", sid, "Plan_Stop")
            # staged entry (2026-09-20): the add level never moves, so it is
            # the reference before entry and fill x (1 - add) afterwards.
            if "Plan_Add_Price" in r:
                plan_add = _num(r.get("Plan_Add_Price"))
                hit_on = r.get("Add_Hit_Date")
                if status == "pending":
                    ref_add = _num(r.get("Add_Price"))
                    if plan_add is not None and ref_add is not None:
                        if abs(plan_add - ref_add) > _PRICE_TOL:
                            hit("plan_add_level_mismatch", sid, "Plan_Add_Price")
                    if not _is_null(hit_on):
                        hit("add_hit_on_pending_row", sid, "Add_Hit_Date")
                elif fill is not None and plan_add is not None:
                    if abs(plan_add - _lvl(fill, -add_pct, "down", sid)) > _PRICE_TOL:
                        hit("plan_add_level_mismatch", sid, "Plan_Add_Price")
                if not _is_null(hit_on) and not _is_null(entry):
                    if str(hit_on)[:10] < str(entry)[:10]:
                        hit("add_hit_before_entry", sid, "Add_Hit_Date")
                if not _is_null(sig):
                    if _is_null(r.get("Exit_Signal_Date")) or _num(r.get("Exit_Signal_Price")) is None:
                        hit("exit_signal_partial", sid, "Exit_Signal")
                    counters["exit_signal"] = counters.get("exit_signal", 0) + 1
                    samples.setdefault("exit_signal", []).append("{}:{}".format(sid, sig))
                    cols["exit_signal"] = "Exit_Signal"

        # buy gate: Buy_Ready implies every gate it is defined by
        br, bb = r.get("Buy_Ready"), r.get("Buy_Block")
        if _is_bool(br):
            if br and not _is_null(bb):
                hit("buy_ready_with_block", sid, "Buy_Block")
            if not br and _is_null(bb):
                hit("blocked_without_reason", sid, "Buy_Block")
            if br and prelaunch:
                # a re-entry after an absence is a fresh signal even though
                # the streak (with its holder's gap tolerance) says "held"
                fresh = status == "pending" or r.get("First_Day") is True
                if r.get("Market") != "OTC" or r.get("Core_Plus") is not True \
                        or not fresh or iok is not True or rank >= n_enter:
                    hit("buy_ready_violates_gate", sid, "Buy_Ready")
            # A buy signal's card must describe a trade that can still be
            # entered. The 2026-10-07 payload shipped 8227 as Buy_Ready on an
            # 'exited' card (the old, closed trade) and stayed green. Since
            # 2026-10-08 a re-entry after a closed trade is its own pending
            # segment, so a closed card on a buy row is a tracker fault.
            if br and isinstance(status, str):
                if status in ("exited", "exit_today", "overdue"):
                    hit("buy_ready_on_closed_segment", sid, "Hold_Status")
                elif status in ("holding", "delay"):
                    # a valid re-signal during an open trade (research counts
                    # it as a separate trade): buying doubles the position
                    hit("buy_ready_while_open", sid, "Hold_Status")

        # trade restrictions (2026-10-08): the kind agrees with its flags, a
        # disposition carries its period, a blocking kind is never buyable
        if "Trade_Restriction" in r:
            _check_restriction(r, sid, br, bb, data_date, blocking, hit, ex_ids)

        # the previous trade (holding_tracker.PREV_COLUMNS) travels together
        # and is ordered in time
        if any(c in r for c in PREV_COLUMNS):
            _check_prev(r, sid, data_date, hit)

        # recommendation columns travel together
        rid = r.get("Recommendation_ID")
        if not _is_null(rid):
            if _num(r.get("Initial_Buy_Price")) is None or _is_null(r.get("Recommended_On")) \
                    or _is_null(r.get("Rec_Status")):
                hit("recommendation_partial", sid, "Recommendation_ID")
        else:
            for col in ("Initial_Buy_Price", "Initial_Stop_Price",
                        "Initial_Target_Price", "Recommended_On", "Rec_Status",
                        "Rec_Status_Reason"):
                if not _is_null(r.get(col)):
                    hit("recommendation_orphan_value", sid, col)
                    break

        # the recommendation and the card describe the SAME trade (2026-10-08)
        if not _is_null(rid) and isinstance(status, str) and status:
            _check_rec_card(r, sid, status, data_date, hit,
                            taiex_asof=taiex_asof)

        # moving averages and volumes are positive when present
        for col in ("MA5", "MA10", "MA20", "MA60", "Vol_MA20", "Vol_MA5"):
            f = _num(r.get(col))
            if f is not None and f <= 0:
                hit("non_positive", sid, col)

        # Every level below is meant to be SENT to a broker, so it has to be a
        # price the exchange actually quotes. The owner reported 2026-09-21
        # that the published prices had decimals that cannot be entered
        # (191.50 x 0.80 = 153.20 on a 0.50 ladder); this is the rule that
        # would have caught it, so it can never come back silently.
        for col in ORDER_LEVEL_COLUMNS:
            f = _num(r.get(col))
            if f is not None and not _on_tick(f, sid):
                hit("price_off_tick", sid, col)

        _check_event_cols(r, sid, data_date, hit)

    dups = [s for s, n in ids_seen.items() if n > 1]
    if dups:
        rep.error("duplicate_stock", "Stock_ID", len(dups), "same id on more than one row",
                  sample=dups)

    errors = {
        "close_outside_range", "stop_not_below_entry", "target_not_above_entry",
        "trail_lock_not_below_arm", "entry_ref_not_close", "stop_pct_mismatch",
        "target_pct_mismatch", "risk_pct_mismatch", "core_plus_mismatch",
        "hold_pending_counts", "hold_day_arithmetic", "hold_cap_below_total",
        "fill_on_pending_row", "held_row_without_entry_date", "entry_date_in_future",
        "fill_level_mismatch", "exit_before_entry", "buy_ready_with_block",
        "blocked_without_reason", "buy_ready_violates_gate", "buy_ready_on_stale_row",
        "recommendation_partial", "recommendation_orphan_value", "non_positive",
        "sup_gap_mismatch", "res_gap_mismatch", "squeeze_mismatch",
        "add_pct_mismatch", "plan_add_level_mismatch", "add_hit_on_pending_row",
        "add_hit_before_entry",
        "exit_signal_on_pending_row", "plan_stop_pending_mismatch",
        "plan_stop_level_mismatch", "exit_signal_partial",
        "scale_out_pct_mismatch", "price_off_tick",
        "buy_ready_on_closed_segment", "prev_segment_partial",
        "prev_segment_order", "prev_segment_kind",
        "rec_segment_mismatch", "rec_state_mismatch", "rec_vs_card_consistency",
        "restricted_buy_ready", "restricted_block_mismatch",
        "restriction_until_missing", "restriction_flags_mismatch",
        "restriction_orphan_detail",
    }
    warns = {"daily_move_over_limit", "row_stale", "held_row_without_fill",
             "integrity_flags_on_ok_row", "integrity_fail_without_flags",
             "rec_target_mismatch", "limit_lock_mismatch"}
    # display-only company events never fail a scan (a fail makes the
    # scan-timer rerun the whole scan)
    warns = warns | set(_EVENT_WARN_CODES)
    if meta.get("degraded"):
        # a degraded run does not advance or create recommendations (P0-3),
        # so a card can legitimately run ahead of its recommendation
        errors = errors - set(_REC_ROW_CODES)
        warns = warns | set(_REC_ROW_CODES)
    infos = {"integrity_not_ok", "hold_overdue", "exit_signal",
             "integrity_soft_flags", "buy_ready_while_open", "prev_segment",
             "restricted_rows", "rev_month_old"}
    descriptions = {
        "close_outside_range": "close not within [Low_Today, High_Today]",
        "daily_move_over_limit": "close moved more than 10.5% vs Close_Prev without Recent_Jump",
        "row_stale": "row bar date differs from meta.data_date",
        "buy_ready_on_stale_row": "buyable row whose own bar is stale",
        "sup_gap_mismatch": "Sup_Gap_Pct != (Close - Support_Used) / Support_Used",
        "res_gap_mismatch": "Res_Gap_Pct != (Resist_60H - Close) / Close",
        "squeeze_mismatch": "Squeeze != (Sup_Gap < 5 and 0 < Res_Gap < 2)",
        "stop_not_below_entry": "Strict_Stop_Loss >= Suggested_Buy_Price",
        "target_not_above_entry": "Target_Price <= Suggested_Buy_Price",
        "trail_lock_not_below_arm": "Trail_Lock_Price >= Trail_Arm_Price",
        "entry_ref_not_close": "prelaunch entry reference is not the close",
        "stop_pct_mismatch": "stop is not close * (1 - PRELAUNCH_STOP_PCT)",
        "target_pct_mismatch": "target is not close * (1 + PRELAUNCH_TP_PCT)",
        "risk_pct_mismatch": "Risk_Pct is not the prelaunch stop percentage",
        "core_plus_mismatch": "Core_Plus disagrees with its three thresholds",
        "integrity_flags_on_ok_row": "Integrity_OK true but a HARD data-error flag is present",
        "integrity_soft_flags": "trustworthy rows carrying a soft flag (jump / gap / short history)",
        "integrity_fail_without_flags": "Integrity_OK false with empty flags",
        "integrity_not_ok": "rows chip_verifier could not vouch for (blocked from buying)",
        "hold_pending_counts": "pending row must have Hold_Day 0 and full remaining",
        "hold_day_arithmetic": "Hold_Day + Hold_Remaining != Hold_Total",
        "hold_cap_below_total": "Hold_Cap < Hold_Total",
        "fill_on_pending_row": "Entry_Open set before entry",
        "held_row_without_entry_date": "non-pending row has no Entry_Date",
        "entry_date_in_future": "Entry_Date after meta.data_date",
        "held_row_without_fill": "entered row has no Entry_Open (missing bar?)",
        "fill_level_mismatch": "Fill_* level is not Entry_Open * multiplier",
        "exit_before_entry": "Exit_Date earlier than Entry_Date",
        "hold_overdue": "rows past the exit cap still listed (hysteresis)",
        "buy_ready_with_block": "Buy_Ready true but Buy_Block non-empty",
        "blocked_without_reason": "Buy_Ready false with empty Buy_Block",
        "buy_ready_violates_gate": "Buy_Ready true on a row failing the OTC/Core+/fresh(pending or First_Day)/integrity/rank gate",
        "recommendation_partial": "Recommendation_ID without its frozen prices/dates",
        "recommendation_orphan_value": "frozen recommendation value without an id",
        "non_positive": "MA / volume average not positive",
        "exit_signal_on_pending_row": "exit booked before entry",
        "plan_stop_pending_mismatch": "pending Plan_Stop is not the reference stop",
        "plan_stop_level_mismatch": "Plan_Stop is not fill x (1 - stop) (or x 1.02 when armed)",
        "add_pct_mismatch": "Add_Price is not close * (1 - PRELAUNCH_ADD_PCT)",
        "plan_add_level_mismatch": "Plan_Add_Price is not the reference (pending) or fill x (1 - add)",
        "add_hit_on_pending_row": "staged add booked before entry",
        "add_hit_before_entry": "Add_Hit_Date earlier than Entry_Date",
        "exit_signal_partial": "Exit_Signal without its date or price",
        "exited_row_has_days_left": "a closed trade still counts hold days",
        "scale_out_pct_mismatch": "Scale_Out_Price is not close x (1 + PRELAUNCH_SCALE_OUT_PCT)",
        "price_off_tick": "an order level that the exchange does not quote "
                          "(not on the tick ladder, so it cannot be placed)",
        "exit_signal": "rows where the exit stack says the trade is already out",
        "buy_ready_on_closed_segment": "Buy_Ready on a row whose card shows a "
                                       "closed trade (exited / exit_today / overdue)",
        "buy_ready_while_open": "Buy_Ready re-signal while the name's previous "
                                "trade is still open (buying doubles the position)",
        "prev_segment_partial": "Prev_* columns set without the rest of their group",
        "prev_segment_order": "Prev_* dates out of order (signal < entry <= exit "
                              "<= data date, before the current trade)",
        "prev_segment_kind": "Prev_* disagrees with Hold_Anchor_Kind (a reentry "
                             "without its previous trade, or a previous trade "
                             "on a first/gap anchor)",
        "prev_segment": "rows whose trade follows an earlier one (re-entry or "
                        "recommendation anchor)",
        "rec_segment_mismatch": "the card is not the recommendation's trade "
                                "(anchor != Recommended_On, Entry_Date != "
                                "Rec_Valid_Until, or a pending card on an old "
                                "recommendation)",
        "rec_state_mismatch": "recommendation status contradicts its card "
                              "(active but the card's trade exited, or closed "
                              "without an exit signal)",
        "rec_vs_card_consistency": "pending card of an active recommendation "
                                   "whose Plan_Stop is not Initial_Stop_Price",
        "rec_target_mismatch": "pending card of an active recommendation whose "
                               "Target_Price is not Initial_Target_Price",
        "restricted_buy_ready": "Buy_Ready on a row whose Trade_Restriction is "
                                "in the blocking set",
        "restricted_block_mismatch": "Buy_Block 'restricted' on a row whose "
                                     "Trade_Restriction is not blocking",
        "restriction_until_missing": "disposition without Restriction_Until, or "
                                     "one that ended before the data date",
        "restriction_flags_mismatch": "Trade_Restriction is not the most severe "
                                      "of Restriction_Flags",
        "restriction_orphan_detail": "disposition period / terms set on a row "
                                     "without a disposition flag",
        "limit_lock_mismatch": "close at Close_Prev x 1.10 (ladder) disagrees "
                               "with the limit_lock flag",
        "restricted_rows": "rows under a trade restriction (disposition / "
                           "altered / suspended / limit lock / attention / "
                           "unknown), shown on the card",
        "rev_month_format": "Rev_Month is not YYYY-MM",
        "rev_month_future": "Rev_Month at or after the data month (a month "
                            "is reported only after it ends)",
        "rev_fields_without_month": "Rev_* figures without their Rev_Month "
                                    "label",
        "ex_fields_without_date": ("Ex_Kind / Ex_Cash_Div without Ex_Date, "
                                   "or Ex_Date without Ex_Kind"),
        "ex_date_not_after_session": "Ex_Date on or before the data date "
                                     "(only upcoming ex-dates are shown)",
        "rev_month_old": "latest published revenue month is more than two "
                         "months behind the data month (company late?)",
    }
    for code, n in counters.items():
        level = "error" if code in errors else "warn" if code in warns else "info"
        rep.add(level, code, cols.get(code, ""), n, descriptions.get(code, code),
                sample=samples.get(code))


# --------------------------------------------------------------------------
# meta and cross-file checks
# --------------------------------------------------------------------------
def _lm_taipei_date(value):
    """An HTTP Last-Modified ('Wed, 07 Oct 2026 15:30:10 GMT') as the Taipei
    calendar date, or None."""
    if _is_null(value):
        return None
    try:
        from datetime import timedelta
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None) - (dt.utcoffset() or timedelta(0))
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")
    except Exception:
        return None


def _check_restriction_meta(restr, has_rows, data_date, cal, rep):
    """meta.quality.restrictions (scanner/trade_restrictions.summarize)."""
    if not isinstance(restr, dict):
        if has_rows:
            rep.error("restrictions_unchecked", "meta.quality.restrictions", 1,
                      "no restriction summary: the scan never read the "
                      "disposition / attention lists")
        return
    boards = restr.get("boards") if isinstance(restr.get("boards"), dict) else {}
    failed, fallback, stale = [], [], []
    prev = None
    if data_date and cal and data_date in cal:
        i = cal.index(data_date)
        prev = cal[i - 1] if i > 0 else None
    for b in MARKETS[::-1]:
        e = boards.get(b) if isinstance(boards.get(b), dict) else {}
        if not e.get("ok"):
            failed.append("{} ({}/{})".format(b, e.get("source"), e.get("error")))
            continue
        if e.get("source") == "web":
            fallback.append(b)
        # The list a session-D scan should read covers the previous session's
        # announcements: TPEX publishes it about 23:30 on that session (Taipei
        # date >= prev), TWSE about 05:30 the next morning (Taipei date >
        # prev). A TWSE list dated `prev` is the one for the session BEFORE
        # prev -- a day of announcements behind.
        lm = _lm_taipei_date(e.get("last_modified"))
        if lm and prev and (lm < prev or (b == "TSE" and lm == prev)):
            stale.append("{} {}".format(b, lm))
    if failed:
        rep.warn("restrictions_feed_failed", "meta.quality.restrictions", len(failed),
                 "disposition list unreadable, rows shown as 'unknown': "
                 + ", ".join(failed))
    if fallback:
        rep.info("restrictions_fallback", "meta.quality.restrictions", len(fallback),
                 "disposition list read from the web bulletin: " + ", ".join(fallback))
    if stale:
        rep.warn("restrictions_stale", "meta.quality.restrictions", len(stale),
                 "disposition list older than the previous session ({}): {}".format(
                     prev, ", ".join(stale)))
    for key, code in (("attention_ok", "attention_feed_failed"),
                      ("altered_ok", "altered_feed_failed")):
        st = restr.get(key) if isinstance(restr.get(key), dict) else {}
        bad = [b for b in MARKETS if st.get(b) is False]
        if bad:
            rep.info(code, "meta.quality.restrictions." + key, len(bad),
                     "best-effort list unreadable: " + ", ".join(bad))


def _check_list_status(ls, session, rep):
    """meta.list_status (scanner/list_freeze). Absent is fine (a payload from
    before the freeze, or one export wrote and nothing checked yet); present,
    it must describe THIS payload's session and carry a known state."""
    if ls is None:
        return
    if not isinstance(ls, dict):
        rep.error("list_status_bad_state", "meta.list_status", 1,
                  "list_status is not an object")
        return
    if ls.get("state") not in ("final", "provisional"):
        rep.error("list_status_bad_state", "meta.list_status.state", 1,
                  "state {!r} is neither final nor provisional".format(
                      ls.get("state")))
    got = str(ls.get("session") or "")[:10]
    if session and got != session:
        rep.error("list_status_session_mismatch", "meta.list_status.session", 1,
                  "list_status describes {} but the payload is {}".format(
                      got or "no session", session))


def _check_meta(payload, rep, expected_session=None):
    meta = payload.get("meta") or {}
    rows = payload.get("rows") or []
    data_date = str(meta.get("data_date") or "")[:10]
    session = str(meta.get("session_date") or "")[:10]

    if not isinstance(payload.get("rows"), list):
        rep.error("payload_shape", "rows", 1, "rows is not a list")
        return
    if meta.get("count") != len(rows):
        rep.error("count_mismatch", "meta.count", 1,
                  "meta.count {} but {} rows".format(meta.get("count"), len(rows)))
    if rows and not data_date:
        rep.error("no_data_date", "meta.data_date", 1, "rows present but no data date")
    if data_date and not _date_ok(data_date):
        rep.error("bad_date", "meta.data_date", 1, data_date)
    if session and data_date and session != data_date:
        rep.error("session_vs_data_date", "meta.session_date", 1,
                  "session {} data {}".format(session, data_date))
    if rows:
        newest = max(str(r.get("Data_Date") or "")[:10] for r in rows)
        if newest != data_date:
            rep.error("data_date_vs_rows", "meta.data_date", 1,
                      "meta says {} rows say {}".format(data_date, newest))
    if not rows and not meta.get("empty_ok") and not meta.get("degraded"):
        rep.error("empty_not_flagged", "meta.empty_ok", 1,
                  "zero rows without empty_ok or degraded")

    if meta.get("degraded"):
        rep.warn("feed_degraded", "meta.degraded", 1, str(meta.get("degraded")))
        rec = meta.get("rec")
        if isinstance(rec, dict) and (
                (_num(rec.get("created")) or 0) > 0 or rec.get("writes") is True):
            rep.error("rec_written_on_degraded_run", "meta.rec", 1,
                      "a degraded run wrote recommendations (created {}, "
                      "writes {})".format(rec.get("created"), rec.get("writes")))
    quality = meta.get("quality") or {}
    if quality.get("data_lag"):
        rep.warn("data_lag", "meta.quality.data_lag", 1,
                 "have {} expected {}".format(quality.get("session_date"),
                                              quality.get("expected_session")))
    if expected_session and data_date and data_date < expected_session \
            and not quality.get("data_lag"):
        rep.warn("data_behind_clock", "meta.data_date", 1,
                 "data {} but the clock says {}".format(data_date, expected_session))

    cal = meta.get("calendar_tail") or []
    tracked = payload.get("tracked") if isinstance(payload.get("tracked"), list) else []
    _check_restriction_meta(quality.get("restrictions"), bool(rows or tracked),
                            data_date, cal, rep)
    if cal and data_date and cal[-1] != data_date:
        rep.warn("calendar_vs_data_date", "meta.calendar_tail", 1,
                 "calendar ends {} data {}".format(cal[-1], data_date))
    if cal and any(not _date_ok(str(d)) for d in cal):
        rep.error("calendar_bad_date", "meta.calendar_tail", 1, "non-date entry")
    if cal != sorted(cal):
        rep.error("calendar_unsorted", "meta.calendar_tail", 1, "not ascending")

    _check_list_status(meta.get("list_status"), session, rep)

    reg = meta.get("regime") or {}
    if not reg.get("ok"):
        rep.warn("regime_unreadable", "meta.regime", 1, "regime failed closed")
    else:
        if reg.get("is_current") is False:
            rep.warn("regime_stale", "meta.regime.as_of_date", 1,
                     "TAIEX as of {} vs data {}".format(reg.get("as_of_date"), data_date))
        if data_date and reg.get("as_of_date") and str(reg["as_of_date"])[:10] < data_date:
            rep.warn("regime_behind_data", "meta.regime.as_of_date", 1,
                     "TAIEX {} < data {}".format(reg.get("as_of_date"), data_date))
        if _garbled(reg.get("text")):
            rep.error("garbled_text", "meta.regime.text", 1, "regime text lost encoding")

    reports = meta.get("reports") or {}
    if rows and "ALL" not in reports:
        rep.warn("report_missing", "meta.reports", 1, "no ALL report attached")
    for k, v in reports.items():
        if _garbled(v):
            rep.error("garbled_text", "meta.reports." + str(k), 1, "report lost encoding")
    if rows and reports and "Market" in rows[0]:
        mkts = {str(r.get("Market")) for r in rows}
        missing = [m for m in ("OTC", "TSE") if m in mkts and m not in reports]
        if missing:
            rep.warn("report_missing", "meta.reports", len(missing),
                     "no report for " + ",".join(missing))
    _check_report_sources(meta.get("report_sources"), reports, rep)

    qmeta = meta.get("quotes") or {}
    if data_date and qmeta.get("as_of") and str(qmeta["as_of"])[:10] != data_date:
        rep.error("quotes_as_of_vs_data", "meta.quotes.as_of", 1,
                  "quotes {} data {}".format(qmeta.get("as_of"), data_date))
    if qmeta.get("missing"):
        rep.warn("quotes_missing_listed", "meta.quotes.missing", len(qmeta["missing"]),
                 "requested ids absent from the feed", sample=qmeta["missing"])


def _check_quotes(payload, quotes, rep, tracked_ids=None):
    if quotes is None:
        rep.warn("quotes_unreadable", "quotes.json", 1, "feed file missing or invalid")
        return
    rows = payload.get("rows") or []
    meta = payload.get("meta") or {}
    data_date = str(meta.get("data_date") or "")[:10]
    sessions = quotes.get("sessions") or []
    closes = quotes.get("closes") or {}
    names = quotes.get("names") or {}
    if not sessions or not isinstance(closes, dict):
        rep.error("quotes_shape", "quotes.json", 1, "no sessions or closes")
        return
    if data_date and sessions[-1] != data_date:
        rep.error("quotes_session_vs_data", "quotes.sessions", 1,
                  "feed ends {} data {}".format(sessions[-1], data_date))
    n = len(sessions)
    bad_len = [s for s, v in closes.items() if not isinstance(v, list) or len(v) != n]
    if bad_len:
        rep.error("quotes_series_length", "quotes.closes", len(bad_len),
                  "series length != sessions", sample=bad_len)

    # Names the pre-export top-up asked every source about and still found
    # nothing newer for: halted or delisted. A listed row with no close IS a
    # problem, but not one the scan can fix by running again, so it is
    # reported rather than failed -- the same treatment the tracked names get
    # a few lines below.
    ended_now = (meta.get("quotes") or {}).get("source_ended") or {}
    missing, gap, mismatch, noname, gap_ended = [], [], [], [], []
    for r in rows:
        sid = str(r.get("Stock_ID") or "").strip()
        series = closes.get(sid)
        if not series:
            missing.append(sid)
            continue
        if series[-1] is None:
            (gap_ended if sid in ended_now else gap).append(sid)
        else:
            c = _num(r.get("Close_Price"))
            if c is not None and abs(float(series[-1]) - c) > _PRICE_TOL:
                mismatch.append(sid)
        if names and sid not in names:
            noname.append(sid)
    if missing:
        rep.error("quotes_missing_row", "quotes.closes", len(missing),
                  "listed stock absent from the feed", sample=missing)
    if gap:
        rep.error("quotes_gap_row", "quotes.closes", len(gap),
                  "listed stock has no close for the session", sample=gap)
    if gap_ended:
        rep.warn("quotes_gap_row_ended", "quotes.closes", len(gap_ended),
                 "listed stock has no close and no newer bar at any source "
                 "(halted or delisted)", sample=gap_ended)
    if mismatch:
        rep.error("quotes_close_mismatch", "quotes.closes", len(mismatch),
                  "feed close != row Close_Price", sample=mismatch)
    if noname:
        rep.warn("quotes_name_missing", "quotes.names", len(noname),
                 "listed stock has no name in the feed", sample=noname)

    # F04: a stock the ledger picked inside the feed window may be held by
    # somebody who is no longer seeing it on the list; it must still be priced
    # through the latest session.
    if tracked_ids:
        listed = {str(r.get("Stock_ID") or "").strip() for r in rows}
        # Names the pre-export top-up asked the sources about and still found
        # nothing newer for: halted or delisted, reported as information.
        ended = (meta.get("quotes") or {}).get("source_ended") or {}
        t_missing, t_gap, t_ended = [], [], []
        for sid in sorted(set(str(s) for s in tracked_ids) - listed):
            series = closes.get(sid)
            if sid in ended:
                t_ended.append("{}@{}".format(sid, ended.get(sid) or "?"))
            elif not series:
                t_missing.append(sid)
            elif series[-1] is None:
                t_gap.append(sid)
        if t_missing:
            rep.warn("quotes_missing_tracked", "quotes.closes", len(t_missing),
                     "recently picked stock absent from the feed", sample=t_missing)
        if t_gap:
            rep.warn("quotes_gap_tracked", "quotes.closes", len(t_gap),
                     "recently picked stock not priced through the session "
                     "(dropped-out name stopped refreshing)", sample=t_gap)
        if t_ended:
            rep.info("quotes_source_ended", "quotes.closes", len(t_ended),
                     "recently picked stock with no newer bar at any source "
                     "(halted or delisted; last bar shown)", sample=t_ended)


def _check_recommendations(payload, recs, rep):
    if recs is None:
        return
    meta = payload.get("meta") or {}
    rows = payload.get("rows") or []
    listed = {str(r.get("Stock_ID") or "").strip(): r for r in rows}
    tracked = payload.get("tracked") if isinstance(payload.get("tracked"), list) else []
    held = {str(r.get("Stock_ID") or "").strip(): r for r in tracked
            if isinstance(r, dict)}
    items = recs.get("recommendations") or []
    if recs.get("count") not in (None, len(items)):
        rep.error("rec_count_mismatch", "recommendations.count", 1,
                  "count {} items {}".format(recs.get("count"), len(items)))
    session = str(meta.get("session_date") or meta.get("data_date") or "")[:10]
    version = str(meta.get("strategy_version") or "")
    cal = [str(d)[:10] for d in (meta.get("calendar_tail") or [])
           if not session or str(d)[:10] <= session]
    found = {k: [] for k in (
        "detached", "detached_tracked", "garbled", "status_unknown",
        "valid_until_missing", "duplicate_active", "past_horizon",
        "version_in_window", "terminal_without_reason", "closed_without_outcome")}
    active_per = {}
    for rec in items:
        sid = str(rec.get("stock_id") or "")
        rid = str(rec.get("recommendation_id") or sid)
        st = rec.get("status")
        if _garbled(rec.get("stock_name")):
            found["garbled"].append(sid)
        if st not in REC_STATUSES or st == "converted":
            found["status_unknown"].append(rid)
        if st == "active":
            key = (sid, str(rec.get("strategy") or ""))
            active_per[key] = active_per.get(key, 0) + 1
            if active_per[key] == 2:
                found["duplicate_active"].append(sid)
            if sid in listed and _is_null(listed[sid].get("Recommendation_ID")):
                found["detached"].append(sid)
            elif sid in held and _is_null(held[sid].get("Recommendation_ID")):
                found["detached_tracked"].append(sid)
            vu = str(rec.get("valid_until_session") or "")[:10]
            if not vu:
                found["valid_until_missing"].append(rid)
            else:
                if cal and sum(1 for d in cal if d > vu) > REC_HORIZON_SESSIONS:
                    found["past_horizon"].append(rid)
                if (session and vu > session and version
                        and str(rec.get("strategy_version") or "") != version):
                    found["version_in_window"].append(rid)
        elif st in REC_TERMINAL:
            if _is_null(rec.get("status_reason")) or _is_null(rec.get("status_session")):
                found["terminal_without_reason"].append(rid)
            if st == "closed" and _is_null(rec.get("outcome")):
                found["closed_without_outcome"].append(rid)
    # The lifecycle does not run on a degraded run (P0-3): its own findings
    # are reported, but must not fail -- and so retry -- that run.
    life = rep.warn if meta.get("degraded") else rep.error
    spec = (
        ("garbled", rep.error, "garbled_text", "recommendations.stock_name",
         "name lost encoding"),
        ("detached", rep.error, "recommendation_not_attached", "Recommendation_ID",
         "active recommendation for a listed stock not on its row"),
        ("detached_tracked", rep.warn, "recommendation_not_attached",
         "tracked.Recommendation_ID",
         "active recommendation for a tracked stock not on its row"),
        ("status_unknown", rep.error, "rec_status_unknown", "recommendations.status",
         "status not in {} (or 'converted', which is private)".format(
             "/".join(s for s in REC_STATUSES if s != "converted"))),
        ("duplicate_active", rep.error, "rec_duplicate_active",
         "recommendations.status", "more than one active recommendation for "
         "one stock and strategy"),
        ("valid_until_missing", life, "rec_valid_until_missing",
         "recommendations.valid_until_session",
         "active recommendation without its entry session"),
        ("past_horizon", life, "rec_active_past_horizon",
         "recommendations.status", "active more than {} sessions after its "
         "entry session (the lifecycle's safety net did not run)".format(
             REC_HORIZON_SESSIONS)),
        ("version_in_window", life, "rec_version_in_window",
         "recommendations.strategy_version",
         "active, before its entry session, under another strategy version"),
        ("terminal_without_reason", rep.warn, "rec_terminal_without_reason",
         "recommendations.status_reason",
         "ended recommendation without status_reason / status_session"),
        ("closed_without_outcome", rep.warn, "rec_closed_without_outcome",
         "recommendations.outcome", "closed recommendation without its trade"),
    )
    for key, fn, code, column, detail in spec:
        if found[key]:
            fn(code, column, len(found[key]), detail, sample=found[key])


def _check_events(payload, meta, rep):
    """meta.events (ingestion.company_events.meta_block): per-source health
    of the display-only event columns. Nothing here can fail a scan: a
    required source that failed or went stale warns, an optional one
    (mopsov) is information."""
    ev = meta.get("events")
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    if not isinstance(ev, dict) or not ev:
        rep.info("events_absent", "meta.events", 1,
                 "no company-event block (display only)")
        return
    if ev.get("carried_forward"):
        rep.info("events_carried_forward", "meta.events", 1,
                 "the previous run's block; this run built none")
    srcs = ev.get("sources") if isinstance(ev.get("sources"), dict) else {}
    found = {"failed": [], "failed_opt": [], "stale": [], "stale_opt": []}
    for name in sorted(srcs):
        h = srcs[name]
        if not isinstance(h, dict):
            continue
        opt = "_opt" if h.get("optional") else ""
        if not h.get("ok"):
            found["failed" + opt].append(name)
        if h.get("stale"):
            found["stale" + opt].append(name)
    if found["failed"]:
        rep.warn("events_source_failed", "meta.events.sources",
                 len(found["failed"]), "company-event source failed; the "
                 "last good data is shown", sample=found["failed"])
    if found["stale"]:
        rep.warn("events_stale", "meta.events.sources", len(found["stale"]),
                 "company-event source past its age limit",
                 sample=found["stale"])
    if found["failed_opt"]:
        rep.info("events_optional_source_failed", "meta.events.sources",
                 len(found["failed_opt"]), "optional (mopsov) source failed",
                 sample=found["failed_opt"])
    if found["stale_opt"]:
        rep.info("events_optional_stale", "meta.events.sources",
                 len(found["stale_opt"]), "optional (mopsov) source old",
                 sample=found["stale_opt"])
    data_date = str(meta.get("data_date") or "")[:10]
    latest = ev.get("revenue_month_latest")
    if latest and data_date and str(latest) >= data_date[:7]:
        rep.warn("events_revenue_month_future",
                 "meta.events.revenue_month_latest", 1,
                 "{} at or after the data month".format(latest))
    rev_rows = sum(int(_num((srcs.get(n) or {}).get("rows")) or 0)
                   for n in ("revenue_tse", "revenue_otc"))
    if rows and rev_rows and not ev.get("carried_forward") \
            and any("Rev_Month" in r for r in rows) \
            and all(_is_null(r.get("Rev_Month")) for r in rows):
        rep.warn("events_not_annotated", "Rev_Month", len(rows),
                 "revenue loaded ({} names) but no listed row carries "
                 "it".format(rev_rows))


def _check_live_record(meta, rep):
    """meta.live_record.by_sid / bench shape (scanner/live_record). The
    record is display only, so a malformed block warns."""
    lr = meta.get("live_record")
    if not isinstance(lr, dict) or not lr:
        return
    by = lr.get("by_sid")
    bad = []
    if by is not None and not isinstance(by, dict):
        bad.append("by_sid")
    for sid, lst in (by.items() if isinstance(by, dict) else ()):
        if not isinstance(lst, list) or not lst or len(lst) > BY_SID_MAX \
                or not all(isinstance(e, dict) for e in lst):
            bad.append(str(sid))
            continue
        sigs = [str(e.get("sig") or "") for e in lst]
        if sigs != sorted(sigs) or not all(_date_ok(s) for s in sigs) or \
                any(e.get("bucket") not in LIVE_RECORD_BUCKETS for e in lst):
            bad.append(str(sid))
    if bad:
        rep.warn("live_record_by_sid_shape", "meta.live_record.by_sid",
                 len(bad), "per-name history entries malformed (sorted by "
                 "signal date, at most {}, known bucket)".format(BY_SID_MAX),
                 sample=bad)
    b = lr.get("bench")
    if b is None:
        return
    ser = b.get("series") if isinstance(b, dict) else None
    ok = isinstance(ser, list) and len(ser) <= BENCH_MAX_POINTS and all(
        isinstance(p, list) and len(p) == 3 and _date_ok(str(p[0]))
        for p in ser)
    if ok:
        days = [str(p[0]) for p in ser]
        ok = days == sorted(days) and (not days or (
            str(b.get("from") or "") <= days[0]
            and days[-1] <= str(b.get("to") or "9999")))
    if not ok:
        rep.warn("live_record_bench_shape", "meta.live_record.bench", 1,
                 "benchmark series malformed (dated [date, taiex, otc] "
                 "points, ascending, inside from..to, at most {})".format(
                     BENCH_MAX_POINTS))


def _check_tracked(payload, meta, rep, scan_mode):
    tracked = payload.get("tracked")
    if not isinstance(tracked, list) or not tracked:
        return
    sub = _Report()
    sub.block = "tracked"
    try:
        _check_columns(tracked, sub)
        _check_rows(tracked, meta, sub, scan_mode)
    except Exception as e:
        sub.add("error", "checker_crash", "", 1,
                "{}: {}".format(type(e).__name__, e))
    for item in sub.items:
        if item["level"] == "error":
            item["level"] = "warn"
        rep.items.append(item)
    rep.add("info", "tracked_rows_checked", "", len(tracked),
            "dropped-out names audited with the same column rules")


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def check_payload(payload, quotes=None, recs=None, tracked_ids=None,
                  expected_session=None, now=None):
    """Audit one published payload. Returns the report dict (never raises)."""
    rep = _Report()
    try:
        meta = payload.get("meta") or {}
        rows = payload.get("rows") or []
        scan_mode = str(meta.get("mode") or "")
        _check_meta(payload, rep, expected_session=expected_session)
        _check_columns(rows, rep)
        _check_rows(rows, meta, rep, scan_mode)
        _check_quotes(payload, quotes, rep, tracked_ids=tracked_ids)
        _check_recommendations(payload, recs, rep)
        # The tracked block is what a HOLDER reads once a name drops off the
        # list, and until 2026-09-21 nothing checked it at all: the column
        # registry, the tick ladder and every fill-level identity ran on
        # `rows` only, while `tracked` shipped straight to the phone. It is
        # audited with the same rules, its codes prefixed so the two blocks
        # stay distinguishable, and reported at WARN: a problem there must be
        # visible, but it must not fail -- and so retry -- a scan whose actual
        # recommendation list is sound.
        _check_tracked(payload, meta, rep, scan_mode)
        # display-only blocks (2026-10-08): company events, the record's
        # per-name history and benchmark
        _check_events(payload, meta, rep)
        _check_live_record(meta, rep)
    except Exception as e:      # the checker must never take the scan down
        rep.error("checker_crash", "", 1, "{}: {}".format(type(e).__name__, e))

    errors = sum(1 for i in rep.items if i["level"] == "error")
    warnings = sum(1 for i in rep.items if i["level"] == "warn")
    status = "fail" if errors else "warn" if warnings else "ok"
    order = {"error": 0, "warn": 1, "info": 2}
    items = sorted(rep.items, key=lambda i: (order[i["level"]], i["code"], i["column"]))
    rows_out = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    cols = set()
    for r in rows_out:
        if isinstance(r, dict):
            cols.update(r.keys())
    return {
        "version": CHECKS_VERSION,
        "status": status,
        "checked_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "rows": len(rows_out),
        "columns": len(cols),
        "errors": errors,
        "warnings": warnings,
        "items": items,
    }


def format_report(report):
    lines = ["[checks] status={} rows={} columns={} errors={} warnings={}".format(
        report.get("status"), report.get("rows"), report.get("columns"),
        report.get("errors"), report.get("warnings"))]
    for it in report.get("items", []):
        s = "  {:5s} {:32s} {:24s} x{:<4d} {}".format(
            it["level"], it["code"], it["column"], it["count"], it["detail"])
        if it.get("sample"):
            s += "  e.g. " + ",".join(it["sample"][:5])
        lines.append(s)
    return "\n".join(lines)


def github_annotations(report):
    """One ::warning:: / ::error:: line per finding, for the Actions summary."""
    out = []
    for it in report.get("items", []):
        if it["level"] == "info":
            continue
        kind = "error" if it["level"] == "error" else "warning"
        out.append("::{}::[{}] {} x{} -- {}{}".format(
            kind, it["code"], it["column"], it["count"], it["detail"],
            " e.g. " + ",".join(it["sample"][:5]) if it.get("sample") else ""))
    return out


def tracked_ids_from_ledger(ledger_path, scan_mode, since_date):
    """Distinct stock ids the ledger picked on or after since_date."""
    import sqlite3
    conn = None
    try:
        # `with sqlite3.connect(...)` does not close the handle; on Windows
        # that keeps the ledger file locked until GC.
        conn = sqlite3.connect(str(ledger_path))
        rows = conn.execute(
            "SELECT DISTINCT stock_id FROM picks WHERE scan_mode = ? AND bar_date >= ?",
            (scan_mode, str(since_date)[:10])).fetchall()
        return sorted(str(r[0]) for r in rows if r and r[0])
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _check_report_sources(sources, reports, rep):
    """meta.report_sources (2026-10-08, plan P1-7): where each AI report came
    from. A template is INFO, never a warning: AI outages cluster (about a
    third of calls failed in late September) and a warning would put the
    orange self-check banner on the phone on half the days for something
    that is neither a data fault nor a reason to rescan (scan-timer retries
    only on fail). The phone marks the template itself. Older payloads carry
    no block and are not judged."""
    if sources is None:
        return
    reports = reports if isinstance(reports, dict) else {}
    if not isinstance(sources, dict):
        rep.warn("report_sources_shape", "meta.report_sources", 1,
                 "not an object")
        return
    bad = sorted(str(k) for k, v in sources.items()
                 if not isinstance(v, dict)
                 or v.get("source") not in REPORT_SOURCES
                 or str(k) not in reports)
    if sources:
        bad += sorted(str(k) for k in reports if k not in sources)
    if bad:
        rep.warn("report_sources_shape", "meta.report_sources", len(bad),
                 "no valid source for " + ",".join(bad))
    tmpl = sorted(str(k) for k, v in sources.items()
                  if isinstance(v, dict) and v.get("source") == "template")
    if tmpl:
        rep.info("report_template", "meta.report_sources", len(tmpl),
                 "local template (AI unavailable) for " + ",".join(tmpl))


def _source_names(sources):
    if not isinstance(sources, dict):
        return {}
    return {str(k): v.get("source") for k, v in sources.items()
            if isinstance(v, dict)}


def append_history(history_path, report, meta, keep=HISTORY_KEEP):
    """Rolling history of check outcomes, small enough to commit."""
    hist = _load_json(history_path) or {}
    runs = hist.get("runs") if isinstance(hist.get("runs"), list) else []
    # The scan and the workflow step both check the same publish; one line
    # per publish, not one per checker invocation.
    if runs and runs[-1].get("scan_time") == meta.get("scan_time") \
            and runs[-1].get("status") == report.get("status"):
        return len(runs)
    runs.append({
        "checked_at": report.get("checked_at"),
        "session_date": meta.get("data_date"),
        "scan_time": meta.get("scan_time"),
        "status": report.get("status"),
        "rows": report.get("rows"),
        "errors": report.get("errors"),
        "warnings": report.get("warnings"),
        "codes": sorted({i["code"] for i in report.get("items", [])
                         if i["level"] != "info"}),
        # where each AI report came from, so the template rate can be counted
        # from this file (report_template itself is info, not in codes)
        "report_sources": _source_names(meta.get("report_sources")),
    })
    runs = runs[-keep:]
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump({"version": CHECKS_VERSION, "runs": runs}, f,
                  ensure_ascii=False, indent=1)
    return len(runs)


def check_files(scan_path, quotes_path=None, recs_path=None, ledger_path=None,
                history_path=None, write=True, expected_session=None,
                revised_reason=None, list_prev=None, now=None):
    """Audit the published files, write meta.checks back, return the report.

    Idempotent: running twice on the same files yields the same report.

    With write=True it also stamps meta.list_status right after meta.checks
    (scanner/list_freeze.list_status_block, 2026-10-08), so the list's
    final / provisional state always agrees with the checks in the same file.
    `list_prev` is the block this publish follows (default: the payload's own
    stamp); `revised_reason` (force_rescan / pages_behind / restriction_info)
    bumps its revision. `now` (a datetime) pins checked_at / revised_at."""
    payload = _load_json(scan_path)
    if payload is None:
        return {"version": CHECKS_VERSION, "status": "fail", "errors": 1,
                "warnings": 0, "rows": 0, "columns": 0,
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "items": [{"level": "error", "code": "payload_unreadable",
                           "column": "", "count": 1,
                           "detail": "cannot read {}".format(scan_path)}]}
    quotes = _load_json(quotes_path) if quotes_path else None
    recs = _load_json(recs_path) if recs_path else None
    meta = payload.get("meta") or {}
    tracked = []
    if ledger_path and quotes and quotes.get("sessions"):
        tracked = tracked_ids_from_ledger(ledger_path, str(meta.get("mode") or ""),
                                          quotes["sessions"][0])
    report = check_payload(payload, quotes=quotes, recs=recs, tracked_ids=tracked,
                           expected_session=expected_session,
                           now=now if isinstance(now, datetime) else None)
    if write:
        meta["checks"] = report
        try:
            from scanner.list_freeze import list_status_block
            prev = list_prev if isinstance(list_prev, dict) else meta.get("list_status")
            meta["list_status"] = list_status_block(
                meta, prev=prev, revised_reason=revised_reason, now=now)
        except Exception as e:
            # the seeded provisional block stays: fails toward a retry
            print("  [checks] list_status not stamped: {}".format(e))
        payload["meta"] = meta
        with open(scan_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        if history_path:
            try:
                append_history(history_path, report, meta)
            except Exception as e:
                print("  [checks] history not written: {}".format(e))
    return report
