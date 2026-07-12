"""
Export the latest scan result to a single CSV for offline review.

Only the most recent version is kept (the file is overwritten each scan), so
the folder never accumulates clutter. UTF-8 BOM is used so Excel opens the
Chinese stock names correctly. All Python strings are ASCII.
"""
import json
from datetime import datetime

import pandas as pd

from config.settings import (
    SCAN_RESULTS_DIR, SCAN_RESULT_FILE, MOBILE_DIR, MOBILE_DATA_FILE,
)


def export_scan_result(df, scan_mode="", reports=None):
    """
    Write the full result DataFrame (all computed columns) to SCAN_RESULT_FILE,
    overwriting any previous version. Two context columns (mode + timestamp) are
    prepended so a saved file is self-describing. A JSON twin is also written for
    the mobile PWA (see export_scan_result_json).

    `reports` (optional) is a {market: text} dict of pre-generated AI reports the
    phone shows per market filter; only the cloud path passes it.

    Returns the written CSV path, or None when there is nothing to export.
    """
    if df is None or df.empty:
        return None

    SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = df.copy()
    out.insert(0, "Scan_Mode", scan_mode)
    out.insert(1, "Scan_Time", scan_time)

    # utf-8-sig => Excel detects UTF-8 and renders Chinese names correctly.
    out.to_csv(SCAN_RESULT_FILE, index=False, encoding="utf-8-sig")

    try:
        export_scan_result_json(df, scan_mode, scan_time, reports=reports)
    except Exception as e:
        # A mobile-feed hiccup must never break the primary CSV export.
        print("  [export] mobile json failed: {}".format(e))

    return str(SCAN_RESULT_FILE)


def export_scan_result_json(df, scan_mode="", scan_time="", reports=None):
    """
    Write the scan result as JSON for the mobile PWA. Structure:
        {"meta": {mode, scan_time, count, regime, reports}, "rows": [...]}
    `reports` is an optional {market: text} map (ALL/OTC/TSE) of AI reports.
    NaN/inf are coerced to null so the JSON is valid. Returns the written path.
    """
    MOBILE_DIR.mkdir(parents=True, exist_ok=True)

    clean = df.replace([float("inf"), float("-inf")], pd.NA)
    rows = json.loads(clean.to_json(orient="records", force_ascii=False))

    # Market regime so the phone view can show the prelaunch entry gate
    # (only open NEW positions when TAIEX is above both 20 and 60MA).
    try:
        from scanner.market_regime import get_market_regime
        reg = get_market_regime()
    except Exception:
        reg = {}

    # Trading-calendar tail so the phone can recompute holding day / entry-exit
    # status live at view time (the scan runs after close; without this, "day N"
    # and "enter tomorrow" freeze at scan time and read one day stale the next
    # morning). 40 dates comfortably covers hold 10 / delay cap 20.
    try:
        from scanner.holding_tracker import _trading_calendar
        calendar_tail = _trading_calendar()[-40:]
    except Exception:
        calendar_tail = []

    payload = {
        "meta": {
            "mode": scan_mode,
            "scan_time": scan_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "regime": {
                "ok": bool(reg.get("ok", False)),
                "risk_on": bool(reg.get("risk_on", False)),
                "enter_ok": bool(reg.get("enter_ok", False)),
                "above20": bool(reg.get("above20", True)),
                "str20": reg.get("str20"),
                "strong": bool(reg.get("strong", False)),
                "text": reg.get("text", ""),
            },
            "calendar_tail": calendar_tail,
            "reports": reports or {},
        },
        "rows": rows,
    }
    with open(MOBILE_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return str(MOBILE_DATA_FILE)
