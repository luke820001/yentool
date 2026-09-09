"""
Refuse to publish a trade ledger that contains anybody's actual positions.

Report section 9.2 is blunt about this: GitHub Pages is a static site and the
repository is public, so "private buy prices, positions and P&L must not be
published through a public JSON or a git commit". But the ledger genuinely
needs to persist across CI runs -- if recommendations did not survive, every
scan would think today was the first day a name qualified and the fixed
first-day price (F01) would reset daily, which is the whole thing we just
built.

So the two kinds of row are separated by WHERE THEY CAN BE WRITTEN:

    recommendations / recommendation_events   written by the scan. Public.
                                              Safe and necessary to commit.
    positions / executions / daily marks      written only by a human recording
                                              a real trade. Never on CI.

On a CI runner the second group must therefore be empty. If it is not,
something is wrong -- most likely a local database with real holdings has been
committed by accident -- and publishing it would leak the owner's finances into
a public repository permanently, because git history keeps it even after a
later delete.

Exit 0 = safe to commit. Exit 1 = private rows present, do not commit.
Exit 0 with a note = no ledger yet (nothing to publish).

Usage:
    python tools/check_ledger_public.py [path-to-ledger.db]
"""
import sqlite3
import sys
from pathlib import Path

# Python puts the SCRIPT's directory on sys.path, not the working directory, so
# `python tools/check_ledger_public.py` from the repo root cannot see config/.
# Put the project root on the path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tables that must be empty in anything we publish.
PRIVATE_TABLES = ("positions", "executions", "position_daily_marks",
                  "position_rule_events", "cycle_results")


def check(path):
    path = Path(path)
    if not path.exists():
        print("[ledger-guard] no ledger at {} -- nothing to publish".format(path))
        return 0

    conn = sqlite3.connect(str(path))
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        offenders = []
        for table in PRIVATE_TABLES:
            if table not in present:
                continue
            n = conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
            if n:
                offenders.append((table, n))

        public = 0
        if "recommendations" in present:
            public = conn.execute(
                "SELECT COUNT(*) FROM recommendations").fetchone()[0]
    finally:
        conn.close()

    if offenders:
        print("[ledger-guard] REFUSING TO PUBLISH {}".format(path))
        for table, n in offenders:
            print("  {} holds {} row(s) -- these are real trades".format(table, n))
        print("  A public repo keeps this in history forever, even if deleted")
        print("  later. Remove the private rows, or exclude this file from the")
        print("  commit, before publishing.")
        return 1

    print("[ledger-guard] OK: {} recommendation(s), no private rows".format(public))
    return 0


if __name__ == "__main__":
    from config.settings import PORTFOLIO_LEDGER_FILE
    target = sys.argv[1] if len(sys.argv) > 1 else PORTFOLIO_LEDGER_FILE
    sys.exit(check(target))
