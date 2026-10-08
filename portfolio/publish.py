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
    # the lifecycle (schema v2, 2026-10-08): why and on which session a
    # recommendation left 'active', and the replayed trade when it closed.
    # recommendation_events are never exported, so a later run (which
    # rebuilds the ledger from this file) only knows what these carry.
    "status_reason", "status_session", "outcome",
)

# Statuses a recommendation can END in (portfolio.ledger.TERMINAL_STATUSES).
TERMINAL = ("expired", "closed", "superseded", "cancelled")

FORMAT_VERSION = 1


def _cell(value):
    """A JSON value as a TEXT cell: an object (outcome, gate_snapshot written
    by hand) is stored as its JSON text, the way the ledger writes it."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return value


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


def seed_from_export(ledger_path, export_path, report=None):
    """Rebuild missing recommendations in the ledger from the published JSON.

    Additive for prices: a recommendation already present keeps every fixed
    value, because an immutable first-day price must never be rewritten by a
    sync. Returns the number inserted.

    One exception, forward only (2026-10-08): when the local copy is still
    'active' and the published one has ENDED (expired / closed / superseded /
    cancelled), the end is copied over -- status, status_reason,
    status_session and outcome -- with a 'synced_from_export' event.
    Otherwise a local ledger that missed the end (a data repair applied to the
    JSON, or a cloud run that closed it) would export it as active again and
    resurrect it. Never terminal -> active, never a 'converted' row.
    `report`, when a dict, receives {"inserted": n, "synced": n}.
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
    from portfolio.ledger import now_ts
    conn = open_ledger(ledger_path)
    inserted = synced = 0
    try:
        existing = {r[0]: r[1] for r in conn.execute(
            "SELECT recommendation_id, status FROM recommendations")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendations)")}
        with conn:
            for row in rows:
                rid = row.get("recommendation_id")
                if not rid:
                    continue
                if rid in existing:
                    if (existing[rid] == "active"
                            and row.get("status") in TERMINAL):
                        sync = [f for f in ("status", "status_reason",
                                            "status_session", "outcome")
                                if f in cols]
                        cur = conn.execute(
                            "UPDATE recommendations SET {} WHERE "
                            "recommendation_id = ? AND status = 'active'".format(
                                ", ".join("{} = ?".format(f) for f in sync)),
                            [_cell(row.get(f)) for f in sync] + [rid])
                        if cur.rowcount == 1:
                            conn.execute(
                                "INSERT INTO recommendation_events "
                                "(recommendation_id, event_type, "
                                "effective_session, recorded_at, reason_code) "
                                "VALUES (?,?,?,?,?)",
                                (rid, "synced_from_export",
                                 row.get("status_session"), now_ts(),
                                 str(row.get("status"))))
                            synced += 1
                    continue
                use = [f for f in FIELDS if f in cols and f in row]
                conn.execute(
                    "INSERT INTO recommendations ({}) VALUES ({})".format(
                        ", ".join(use), ", ".join("?" * len(use))),
                    [_cell(row[f]) for f in use])
                inserted += 1
    finally:
        conn.close()
    if isinstance(report, dict):
        report.update(inserted=inserted, synced=synced)
    return inserted
