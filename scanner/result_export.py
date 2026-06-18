"""
Export the latest scan result to a single CSV for offline review.

Only the most recent version is kept (the file is overwritten each scan), so
the folder never accumulates clutter. UTF-8 BOM is used so Excel opens the
Chinese stock names correctly. All Python strings are ASCII.
"""
from datetime import datetime

from config.settings import SCAN_RESULTS_DIR, SCAN_RESULT_FILE


def export_scan_result(df, scan_mode=""):
    """
    Write the full result DataFrame (all computed columns) to SCAN_RESULT_FILE,
    overwriting any previous version. Two context columns (mode + timestamp) are
    prepended so a saved file is self-describing.

    Returns the written path, or None when there is nothing to export.
    """
    if df is None or df.empty:
        return None

    SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    out = df.copy()
    out.insert(0, "Scan_Mode", scan_mode)
    out.insert(1, "Scan_Time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # utf-8-sig => Excel detects UTF-8 and renders Chinese names correctly.
    out.to_csv(SCAN_RESULT_FILE, index=False, encoding="utf-8-sig")
    return str(SCAN_RESULT_FILE)
