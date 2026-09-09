"""
Publishable export of the recommendation ledger. ASCII only, stdlib only.

The problem this solves. The trade ledger has to survive between CI runs --
without it, every scan believes today is the first day each name qualified and
the fixed first-day price (F01) resets daily, which defeats the whole point.
The first attempt was to commit data/portfolio_ledger.db itself. That worked,
and it was wrong:

  * `git add -f` defeats .gitignore ONCE, but git never re-applies .gitignore to
    an already-tracked file. So committing the database once disarms the ignore
    rule permanently, and from then on a plain `git commit -a` sweeps it up.
  * The file's safety then rests on "nothing currently writes positions to it",
    which is true only by absence of callers -- a property one future feature
    silently revokes.
  * Deleting rows before committing does not help. SQLite leaves deleted content
    in freelist pages unless secure_delete is on and the file is VACUUMed, so a
    row-counting guard passes while the bytes are still recoverable.

So the published artifact is no longer the database. It is a JSON file that this
module builds by reading ONE table and naming every column explicitly. A
position, an execution or a daily mark cannot appear in it -- not because we
check, but because nothing reads them. Same principle as the quote feed
covering the whole universe: make the property structural, not vigilant.

The database stays local and stays ignored. CI rebuilds it from the JSON.
"""
import json
import sqlite3
from pathlib import Path

# Every column of `recommendations`, listed rather than SELECT *, so a column
# added later has to be considered here before it can reach a public file.
FIELDS = (
    "recommendation_id", "stock_id", "stock_name", "market", "strategy",
    "strategy_version", "cycle_seq", "first_qualified_session",
    "recommended_at", "initial_buy_price", "initial_stop_price",
    "initial_target_price", "trail_arm_price", "trail_lock_price",
    "valid_until_session", "horizon_days", "gate_snapshot", "status",
)

FORMAT_VERSION = 1


def export_recommendations(ledger_path, out_path):
    """Write the recommendation ledger to `out_path` as JSON. Returns the count.

    Reads `recommendations` and nothing else. Safe to run against a ledger that
    holds real positions: they are not selected, so they are not written.
    """
    ledger_path, out_path = Path(ledger_path), Path(out_path)
    rows = []
    if ledger_path.exists():
        conn = sqlite3.connect(str(ledger_path))
        conn.row_factory = sqlite3.Row
        try:
            have = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "recommendations" in have:
                cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(recommendations)")}
                use = [f for f in FIELDS if f in cols]
                for row in conn.execute(
                        "SELECT {} FROM recommendations ORDER BY "
                        "first_qualified_session, recommendation_id".format(
                            ", ".join(use))):
                    rows.append({f: row[f] for f in use})
        finally:
            conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"format_version": FORMAT_VERSION,
                   "table": "recommendations",
                   "count": len(rows),
                   "recommendations": rows},
                  f, ensure_ascii=False, indent=1, sort_keys=True)
    return len(rows)


def seed_from_export(ledger_path, export_path):
    """Rebuild missing recommendations in the ledger from the published JSON.

    Additive only: a recommendation already present is left exactly as it is,
    because the local copy may have been through events the export does not
    carry, and because an immutable first-day price must never be rewritten by
    a sync. Returns the number inserted.
    """
    export_path = Path(export_path)
    if not export_path.exists():
        return 0
    try:
        with open(export_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return 0
    rows = payload.get("recommendations") or []
    if not rows:
        return 0

    from portfolio.schema import open_ledger
    conn = open_ledger(ledger_path)
    inserted = 0
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT recommendation_id FROM recommendations")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)")}
        with conn:
            for row in rows:
                rid = row.get("recommendation_id")
                if not rid or rid in existing:
                    continue
                use = [f for f in FIELDS if f in cols and f in row]
                conn.execute(
                    "INSERT INTO recommendations ({}) VALUES ({})".format(
                        ", ".join(use), ", ".join("?" * len(use))),
                    [row[f] for f in use])
                inserted += 1
    finally:
        conn.close()
    return inserted
