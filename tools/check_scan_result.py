"""
Audit the files the phone reads, column by column, after a scan. ASCII only.

Runs on the cloud runner right after scan_headless.py (and can be run locally
against any published payload). It:

  1. checks every column of mobile/scan_result.json against the registry in
     scanner/result_checks.py, plus the cross-column identities, the meta block,
     mobile/quotes.json and data/recommendations.json;
  2. writes the outcome back into meta.checks so the phone shows it;
  3. appends one line to data/scan_checks.json (rolling history, committed);
  4. prints one GitHub annotation per finding (--annotate) and exposes
     checks=<ok|warn|fail> as a step output when GITHUB_OUTPUT is set.

Exit 0 unless --strict is given and the status is "fail" (exit 3). The
workflow does NOT block publishing on a failed check: the phone shows a red
banner with the reasons and scan-timer keeps retrying through the afternoon.
Withholding the file would leave the phone on yesterday's data with no
explanation, which is the failure mode this whole tool exists to end.

Usage:
    python tools/check_scan_result.py [--annotate] [--strict] [--no-write]
                                      [--url URL | SCAN_JSON [QUOTES_JSON]]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (  # noqa: E402
    MOBILE_DATA_FILE, MOBILE_QUOTES_FILE, RECOMMENDATIONS_EXPORT_FILE,
    SIGNAL_LEDGER_FILE, SCAN_CHECKS_FILE,
)
from scanner.result_checks import (  # noqa: E402
    check_files, format_report, github_annotations,
)


def _expected_session():
    try:
        from scanner.chip_verifier import _latest_trading_day
        return _latest_trading_day()
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("scan", nargs="?", default=str(MOBILE_DATA_FILE))
    ap.add_argument("quotes", nargs="?", default=str(MOBILE_QUOTES_FILE))
    ap.add_argument("--recs", default=str(RECOMMENDATIONS_EXPORT_FILE))
    ap.add_argument("--ledger", default=str(SIGNAL_LEDGER_FILE))
    ap.add_argument("--history", default=str(SCAN_CHECKS_FILE))
    ap.add_argument("--annotate", action="store_true",
                    help="print ::warning::/::error:: lines for GitHub Actions")
    ap.add_argument("--strict", action="store_true",
                    help="exit 3 when the status is fail")
    ap.add_argument("--no-write", action="store_true",
                    help="report only; do not touch the payload or history")
    args = ap.parse_args(argv)

    report = check_files(
        args.scan, quotes_path=args.quotes, recs_path=args.recs,
        ledger_path=args.ledger if Path(args.ledger).exists() else None,
        history_path=None if args.no_write else args.history,
        write=not args.no_write, expected_session=_expected_session())

    print(format_report(report))
    if args.annotate:
        for line in github_annotations(report):
            print(line)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("checks={}\n".format(report.get("status")))
            f.write("check_errors={}\n".format(report.get("errors", 0)))
            f.write("check_warnings={}\n".format(report.get("warnings", 0)))
    if args.strict and report.get("status") == "fail":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
