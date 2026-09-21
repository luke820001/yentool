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
HOLD_STATUSES = ("", "pending", "holding", "exit_today", "overdue", "delay")
BUY_BLOCKS = ("", "regime", "held", "unknown", "quality", "market", "rank",
              "integrity", "stale", "no_rule")
REC_STATUSES = ("active", "expired", "converted", "cancelled", "closed")
EXIT_SIGNALS = ("", "stop", "lock", "tp", "time")
CHIP_BASES = ("", "current", "lag")
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


def _on_tick(price):
    try:
        from scanner.tick import is_on_tick
    except Exception:
        return True
    return is_on_tick(price)


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
    "RS_Score":            _c("num", lo=0, hi=2000, phone=True),
    "Gain_3M_Pct":         _c("num", lo=-95, hi=2000, phone=True),
    "Gain_1M_Pct":         _c("num", lo=-95, hi=1000),
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
    "Inst_Pct":            _c("num", nullable=True, lo=-100, hi=100, phone=True),
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
    "Vol_MA20":            _c("num", lo=0),
    "Vol_MA5":             _c("num", lo=0),
    "Vol_Today":           _c("int", lo=0),
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
    "Risk_Pct":            _c("num", lo=0, hi=50, phone=True),
    "Target_Price":        _c("num", lo=0.01, phone=True),
    "Trail_Arm_Price":     _c("num", lo=0.01),
    "Trail_Lock_Price":    _c("num", lo=0.01),
    "Add_Price":           _c("num", lo=0.01),
    # optional scale-out (2026-09-21): sell half at +15% from the fill
    "Scale_Out_Price":     _c("num", lo=0.01, phone=True),
    "Core_Plus":           _c("bool"),
    "Entry_Date":          _c("date", nullable=True),
    "Exit_Date":           _c("date", nullable=True),
    # Nullable: a listed name with no ledger anchor has an UNKNOWN holding
    # day, and null is the honest value for that. Before 2026-09-21 the
    # registry demanded a number, so a single unanchored row failed the whole
    # payload -- the producer and the contract disagreed.
    "Hold_Day":            _c("int", nullable=True, lo=0, hi=400),
    "Hold_Remaining":      _c("int", nullable=True, lo=-400, hi=400),
    "Hold_Total":          _c("int", lo=1, hi=60, phone=True),
    "Hold_Cap":            _c("int", lo=1, hi=120, phone=True),
    "Hold_Status":         _c("choice", nullable=True, choices=HOLD_STATUSES),
    "Hold_Note":           _c("str", nullable=True),
    "Entry_Open":          _c("num", nullable=True, lo=0.01),
    "Fill_Stop_Loss":      _c("num", nullable=True, lo=0.01),
    "Fill_Trail_Arm_Price": _c("num", nullable=True, lo=0.01),
    "Fill_Trail_Lock_Price": _c("num", nullable=True, lo=0.01),
    "Fill_Target_Price":   _c("num", nullable=True, lo=0.01),
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
    "Buy_Ready":           _c("bool", phone=True),
    "Buy_Block":           _c("choice", nullable=True, choices=BUY_BLOCKS, phone=True),
    "Recommendation_ID":   _c("str", nullable=True, phone=True),
    "Initial_Buy_Price":   _c("num", nullable=True, lo=0.01, phone=True),
    "Initial_Stop_Price":  _c("num", nullable=True, lo=0.01),
    "Initial_Target_Price": _c("num", nullable=True, lo=0.01),
    "Recommended_On":      _c("date", nullable=True, phone=True),
    "Rec_Status":          _c("choice", nullable=True, choices=REC_STATUSES,
                              phone=True),
    "Rec_Valid_Until":     _c("date", nullable=True, phone=True),
}

# Flag prefixes data_integrity uses for UNAMBIGUOUS data errors; everything
# else it emits (jump / recent_jump / gap / short_*) is an observation about a
# series that is still trustworthy.
_HARD_FLAGS = ("nan:", "nonpos:", "ohlc:", "dup:")

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
    def __init__(self):
        self.items = []

    def add(self, level, code, column, count=1, detail="", sample=None):
        if count <= 0:
            return
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
def _check_columns(rows, rep):
    if not rows:
        return
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
                and not col.startswith(("Initial_", "Rec")):
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


def _lvl(base, pct, direction):
    """The published level for base * (1 + pct): snapped onto the exchange's
    quote ladder, the same way scan_mode and holding_tracker compute it."""
    try:
        from scanner.tick import round_to_tick
    except Exception:
        return round(base * (1 + pct), 2)
    got = round_to_tick(base * (1 + pct), direction)
    return got if got is not None else round(base * (1 + pct), 2)


def _check_rows(rows, meta, rep, scan_mode):
    if not rows:
        return
    data_date = str(meta.get("data_date") or "")[:10]
    (stop_pct, tp_pct, arm_pct, lock_pct, add_pct, out_pct,
     n_enter) = _trade_params()
    prelaunch = scan_mode == "mode_prelaunch"

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
            if stop is not None and abs(stop - _lvl(close, -stop_pct, "down")) > _PRICE_TOL:
                hit("stop_pct_mismatch", sid, "Strict_Stop_Loss")
            if tgt is not None and abs(tgt - _lvl(close, tp_pct, "up")) > _PRICE_TOL:
                hit("target_pct_mismatch", sid, "Target_Price")
            risk = _num(r.get("Risk_Pct"))
            if risk is not None and close and stop is not None:
                if abs(risk - (close - stop) / close * 100) > 0.11:
                    hit("risk_pct_mismatch", sid, "Risk_Pct")
            add = _num(r.get("Add_Price"))
            if add is not None and abs(add - _lvl(close, -add_pct, "down")) > _PRICE_TOL:
                hit("add_pct_mismatch", sid, "Add_Price")
            out = _num(r.get("Scale_Out_Price"))
            if out is not None and abs(out - _lvl(close, out_pct, "up")) > _PRICE_TOL:
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
                        if got is None or abs(got - _lvl(fill, pct, side)) > _PRICE_TOL:
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
                        hit("plan_stop_pending_mismatch", sid, "Plan_Stop")
                elif fill is not None and plan_stop is not None and _is_bool(armed):
                    want = (_lvl(fill, lock_pct, "down") if armed
                            else _lvl(fill, -stop_pct, "down"))
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
                    if abs(plan_add - _lvl(fill, -add_pct, "down")) > _PRICE_TOL:
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
                if r.get("Market") != "OTC" or r.get("Core_Plus") is not True \
                        or status != "pending" or iok is not True or rank >= n_enter:
                    hit("buy_ready_violates_gate", sid, "Buy_Ready")

        # recommendation columns travel together
        rid = r.get("Recommendation_ID")
        if not _is_null(rid):
            if _num(r.get("Initial_Buy_Price")) is None or _is_null(r.get("Recommended_On")) \
                    or _is_null(r.get("Rec_Status")):
                hit("recommendation_partial", sid, "Recommendation_ID")
        else:
            for col in ("Initial_Buy_Price", "Initial_Stop_Price",
                        "Initial_Target_Price", "Recommended_On", "Rec_Status"):
                if not _is_null(r.get(col)):
                    hit("recommendation_orphan_value", sid, col)
                    break

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
            if f is not None and not _on_tick(f):
                hit("price_off_tick", sid, col)

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
    }
    warns = {"daily_move_over_limit", "row_stale", "held_row_without_fill",
             "integrity_flags_on_ok_row", "integrity_fail_without_flags"}
    infos = {"integrity_not_ok", "hold_overdue", "exit_signal",
             "integrity_soft_flags"}
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
        "buy_ready_violates_gate": "Buy_Ready true on a row failing the OTC/Core+/pending/integrity/rank gate",
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
        "scale_out_pct_mismatch": "Scale_Out_Price is not close x (1 + PRELAUNCH_SCALE_OUT_PCT)",
        "price_off_tick": "an order level that the exchange does not quote "
                          "(not on the tick ladder, so it cannot be placed)",
        "exit_signal": "rows where the exit stack says the trade is already out",
    }
    for code, n in counters.items():
        level = "error" if code in errors else "warn" if code in warns else "info"
        rep.add(level, code, cols.get(code, ""), n, descriptions.get(code, code),
                sample=samples.get(code))


# --------------------------------------------------------------------------
# meta and cross-file checks
# --------------------------------------------------------------------------
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
    if cal and data_date and cal[-1] != data_date:
        rep.warn("calendar_vs_data_date", "meta.calendar_tail", 1,
                 "calendar ends {} data {}".format(cal[-1], data_date))
    if cal and any(not _date_ok(str(d)) for d in cal):
        rep.error("calendar_bad_date", "meta.calendar_tail", 1, "non-date entry")
    if cal != sorted(cal):
        rep.error("calendar_unsorted", "meta.calendar_tail", 1, "not ascending")

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

    missing, gap, mismatch, noname = [], [], [], []
    for r in rows:
        sid = str(r.get("Stock_ID") or "").strip()
        series = closes.get(sid)
        if not series:
            missing.append(sid)
            continue
        if series[-1] is None:
            gap.append(sid)
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
    rows = payload.get("rows") or []
    listed = {str(r.get("Stock_ID") or "").strip(): r for r in rows}
    items = recs.get("recommendations") or []
    if recs.get("count") not in (None, len(items)):
        rep.error("rec_count_mismatch", "recommendations.count", 1,
                  "count {} items {}".format(recs.get("count"), len(items)))
    detached, garbled = [], []
    for rec in items:
        sid = str(rec.get("stock_id") or "")
        if _garbled(rec.get("stock_name")):
            garbled.append(sid)
        if rec.get("status") == "active" and sid in listed:
            if _is_null(listed[sid].get("Recommendation_ID")):
                detached.append(sid)
    if garbled:
        rep.error("garbled_text", "recommendations.stock_name", len(garbled),
                  "name lost encoding", sample=garbled)
    if detached:
        rep.error("recommendation_not_attached", "Recommendation_ID", len(detached),
                  "active recommendation for a listed stock not on its row",
                  sample=detached)


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
    try:
        with sqlite3.connect(str(ledger_path)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT stock_id FROM picks WHERE scan_mode = ? AND bar_date >= ?",
                (scan_mode, str(since_date)[:10])).fetchall()
        return sorted(str(r[0]) for r in rows if r and r[0])
    except Exception:
        return []


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
    })
    runs = runs[-keep:]
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump({"version": CHECKS_VERSION, "runs": runs}, f,
                  ensure_ascii=False, indent=1)
    return len(runs)


def check_files(scan_path, quotes_path=None, recs_path=None, ledger_path=None,
                history_path=None, write=True, expected_session=None):
    """Audit the published files, write meta.checks back, return the report.

    Idempotent: running twice on the same files yields the same report."""
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
                           expected_session=expected_session)
    if write:
        meta["checks"] = report
        payload["meta"] = meta
        with open(scan_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        if history_path:
            try:
                append_history(history_path, report, meta)
            except Exception as e:
                print("  [checks] history not written: {}".format(e))
    return report
