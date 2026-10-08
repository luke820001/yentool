"""
End one published recommendation by hand -- a data repair. ASCII only.

    python tools/rec_set_status.py --id rec-5274-mode_prelaunch-1 \
        --status cancelled --reason degraded_run --session 2026-09-17

Why a tool and not an editor. data/recommendations.json is the source of
truth for the CI ledger: every cloud run rebuilds data/portfolio_ledger.db
from it (portfolio.publish.seed_from_export), and a local ledger takes a
terminal status from it forward-only. So a recommendation that should never
have existed -- rec 5274, written by the DEGRADED 2026-09-17 20:00 run, which
did not list 5274 once the feed recovered -- is ended here, in the JSON, and
everything downstream follows. The tool exists so the repair is exact and
repeatable:

  * only an ACTIVE recommendation can be ended, only into a terminal status
    (expired / closed / superseded / cancelled), and never twice;
  * only status, status_reason, status_session and outcome change; every
    price and date the system published stays byte-identical;
  * the file keeps its format (sorted keys, indent 1, its line endings) and
    is replaced atomically.

--ledger PATH additionally applies the same transition to a local ledger
(portfolio.ledger.transition_recommendation: a guarded UPDATE plus one event,
never a DELETE). Optional: without it, the next scan's seed carries the
status into the ledger anyway.

Exit codes: 0 done, 1 refused (nothing written), 2 usage.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TERMINAL = ("expired", "closed", "superseded", "cancelled")
DEFAULT_JSON = ROOT / "data" / "recommendations.json"
_REASON = re.compile(r"^[a-z0-9_:.\-]{1,64}$")
_SESSION = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Refused(Exception):
    """The repair was not applied; the message says why."""


def _load(path):
    with open(path, "rb") as f:
        raw = f.read()
    crlf = b"\r\n" in raw
    return json.loads(raw.decode("utf-8")), crlf


def _dump(path, doc, crlf):
    text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    if crlf:
        text = text.replace("\n", "\r\n")
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_status(json_path, rec_id, status, reason, session, outcome=None,
               ledger_path=None):
    """End `rec_id` in the published JSON (and optionally a local ledger).

    Returns {"rec_id", "from", "to", "ledger"} where ledger is None (not
    asked), True (transitioned) or False (the ledger row was not active).
    Raises Refused, writing nothing, when the request is not a valid repair.
    """
    if status not in TERMINAL:
        raise Refused("status must be one of {}".format("/".join(TERMINAL)))
    reason = str(reason or "")
    if not _REASON.match(reason):
        raise Refused("reason must match {}".format(_REASON.pattern))
    session = str(session or "")
    if not _SESSION.match(session):
        raise Refused("session must be YYYY-MM-DD")
    if outcome is not None and not isinstance(outcome, dict):
        raise Refused("outcome must be a JSON object")

    doc, crlf = _load(json_path)
    items = doc.get("recommendations") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise Refused("{} has no recommendations list".format(json_path))
    hits = [r for r in items if isinstance(r, dict)
            and r.get("recommendation_id") == rec_id]
    if not hits:
        raise Refused("{} not found".format(rec_id))
    if len(hits) > 1:
        raise Refused("{} appears {} times".format(rec_id, len(hits)))
    rec = hits[0]
    was = rec.get("status")
    if was != "active":
        raise Refused("{} is '{}', not active".format(rec_id, was))

    applied = None
    if ledger_path:
        from portfolio.ledger import open_ledger, transition_recommendation
        conn = open_ledger(ledger_path)
        try:
            applied = transition_recommendation(conn, rec_id, status, reason,
                                                session, outcome=outcome)
        finally:
            conn.close()

    rec["status"] = status
    rec["status_reason"] = reason
    rec["status_session"] = session
    # the ledger stores outcome as JSON text and the export copies the text
    rec["outcome"] = (json.dumps(outcome, ensure_ascii=True, sort_keys=True)
                      if outcome is not None else None)
    _dump(json_path, doc, crlf)
    return {"rec_id": rec_id, "from": was, "to": status, "ledger": applied}


def main(argv=None):
    ap = argparse.ArgumentParser(description="End one published recommendation "
                                             "(data repair).")
    ap.add_argument("--id", required=True, dest="rec_id")
    ap.add_argument("--status", required=True, choices=TERMINAL)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--outcome", default=None,
                    help="JSON object (a closed trade), default null")
    ap.add_argument("--json", default=str(DEFAULT_JSON), dest="json_path")
    ap.add_argument("--ledger", default=None,
                    help="also transition this local ledger (optional)")
    args = ap.parse_args(argv)
    try:
        outcome = json.loads(args.outcome) if args.outcome else None
        got = set_status(args.json_path, args.rec_id, args.status, args.reason,
                         args.session, outcome=outcome, ledger_path=args.ledger)
    except (Refused, ValueError) as e:
        print("[rec_set_status] refused: {}".format(e))
        return 1
    print("[rec_set_status] {rec_id}: {from} -> {to} (ledger: {ledger})".format(**got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
