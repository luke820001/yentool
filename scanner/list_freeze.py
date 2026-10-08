"""
Final-once list freeze (2026-10-08, plan P0-5 / P1-9). ASCII only.

The problem. The phone's list was rewritten by every run of the day: the
scan-timer dispatch at 15:00, then the three late crons (2-12 h late, see
.github/workflows/scan.yml), each re-ranked the SAME session's bars and
re-published. Tied scores swapped, hysteresis stacked on the day's own
output, and the list the owner read at 15:05 was not the list at 21:48.

The rule, POLICY "final-once-v1":

  * finality(meta) says whether a published payload is FINAL: the rows are
    the session's own data, the feed was healthy, the column checks did not
    fail, the market regime was readable and current, a zero-row day was
    flagged as such, and the scan ran at or after FINAL_NOT_BEFORE (15:00
    Taiwan, which must equal FIRST_ATTEMPT in .github/scripts/scan_timer.sh).
    Anything else is PROVISIONAL, and the next run may replace it -- the
    timer keeps retrying until it is final.
  * result_checks.check_files stamps meta.list_status (list_status_block)
    right after meta.checks, so the two always agree, and scan_headless
    writes a final one into the ledger table list_sessions
    (signal_ledger.record_list_session), which the workflow commits.
  * A later run of a session that is already final stops before anything
    persistent happens (decide; scan_headless exit 3, EXIT_FROZEN). Two
    ways through: a manual dispatch with force_rescan (env FORCE_ENV), and
    the repair of a Pages deploy that never landed (the ledger says final,
    Pages does not). Both bump list_status.revision and say why.
  * One thing does change after the freeze. The exchanges publish the
    disposition / attention lists around 23:30, announced on D and in force
    from D+1, so a 15:00 list cannot see them. A frozen run re-reads them and,
    if the restriction columns of the PUBLISHED payload would change, amends
    those columns only (amend_restrictions; exit 4, EXIT_AMENDED): never a row
    added, removed or reordered, and never a Buy_Ready changed -- except
    True -> False with Buy_Block "restricted" when the new kind is in
    trade_restrictions.BLOCKING_RESTRICTIONS, which also retracts the
    recommendation that session created.

Exit codes 3/4 are scan_headless's (tools/check_scan_result.py --strict uses
3 for a failed check -- a different script, a different meaning).
"""
import copy
import json
import os
import time
from datetime import datetime
from pathlib import Path

POLICY = "final-once-v1"
FINAL_NOT_BEFORE = "15:00"         # == FIRST_ATTEMPT in .github/scripts/scan_timer.sh
STATE_FINAL = "final"
STATE_PROVISIONAL = "provisional"
STATES = (STATE_FINAL, STATE_PROVISIONAL)
REASON_CODES = ("data_lag", "degraded", "unchecked", "checks_fail",
                "regime_stale", "regime_unknown", "empty_unflagged",
                "too_early")
REVISED_REASONS = ("force_rescan", "pages_behind", "restriction_info")

EXIT_FROZEN = 3
EXIT_AMENDED = 4

FORCE_ENV = "YENTOOL_FORCE_RESCAN"
PAGES_ENV = "PAGES_URL"
PAGES_TIMEOUT = 10
# What the Pages site serves that the repository does not carry (they are
# gitignored under mobile/). A frozen run that republishes must put all of
# them back before the workflow uploads mobile/, or the deploy would ship the
# app without its data.
PAGES_DATA_FILES = ("scan_result.json", "quotes.json", "universe.json")

ACTION_SCAN = "scan"
ACTION_FROZEN = "frozen"

AMENDED = "amended"
UNCHANGED = "unchanged"
SKIPPED = "skipped"


def _s10(v):
    return str(v or "").strip()[:10]


def _now(now=None):
    """'YYYY-MM-DD HH:MM:SS' of `now` (a datetime or a string), default the
    wall clock."""
    if now is None:
        now = datetime.now()
    if isinstance(now, datetime):
        return now.strftime("%Y-%m-%d %H:%M:%S")
    return str(now)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# finality and the meta.list_status block
# --------------------------------------------------------------------------
def finality(meta):
    """(state, reasons) for a payload's meta. Pure; reasons are REASON_CODES.

    Final only when every one of these holds:
      data_lag         session_date set and data_date == session_date
      degraded         meta.degraded empty
      unchecked        meta.checks present (check_files ran)
      checks_fail      meta.checks.status != "fail" (warn is still final)
      regime_stale     regime.is_current is not False
      regime_unknown   regime.ok -- an unreadable regime blocks every buy,
                       so a later run must be allowed to fix it
      empty_unflagged  count > 0, or empty_ok
      too_early        scan_time >= "<session> FINAL_NOT_BEFORE"
    """
    m = meta if isinstance(meta, dict) else {}
    reasons = []
    session = _s10(m.get("session_date"))
    data_date = _s10(m.get("data_date"))
    if not session or data_date != session:
        reasons.append("data_lag")
    if m.get("degraded"):
        reasons.append("degraded")
    checks = m.get("checks") if isinstance(m.get("checks"), dict) else None
    if not checks or not checks.get("status"):
        reasons.append("unchecked")
    elif checks.get("status") == "fail":
        reasons.append("checks_fail")
    reg = m.get("regime") if isinstance(m.get("regime"), dict) else {}
    if reg.get("is_current") is False:
        reasons.append("regime_stale")
    if not reg.get("ok"):
        reasons.append("regime_unknown")
    count = m.get("count")
    if not ((isinstance(count, int) and not isinstance(count, bool)
             and count > 0) or m.get("empty_ok")):
        reasons.append("empty_unflagged")
    scan_time = str(m.get("scan_time") or "")
    if not session or scan_time[:16] < "{} {}".format(session, FINAL_NOT_BEFORE):
        reasons.append("too_early")
    return (STATE_PROVISIONAL if reasons else STATE_FINAL), reasons


def seed_block(meta):
    """The provisional block export writes before the checks have run: if the
    run dies before check_files, the payload stays provisional and the timer
    retries (fails open toward a retry, never toward a freeze)."""
    m = meta if isinstance(meta, dict) else {}
    published = str(m.get("scan_time") or "") or None
    return {
        "policy": POLICY,
        "state": STATE_PROVISIONAL,
        "reasons": ["unchecked"],
        "session": _s10(m.get("session_date")) or None,
        "published_at": published,
        "first_published_at": published,
        "revision": 1,
        "strategy_version": m.get("strategy_version") or None,
        "revised_reason": None,
        "revised_at": None,
    }


def list_status_block(meta, prev=None, revised_reason=None, now=None):
    """meta.list_status for a checked payload.

    {policy, state, reasons, session, published_at (= meta.scan_time),
     first_published_at, revision, strategy_version, revised_reason,
     revised_at}

    `prev` is the block this one follows (the payload's own stamp, or the
    final block of the same session from the ledger / Pages). Same session:
      * with `revised_reason` (force_rescan / pages_behind /
        restriction_info): first_published_at carried, revision + 1,
        revised_at = now;
      * the same publish (a re-check of the same file), or a final prev:
        first_published_at, revision, revised_reason, revised_at carried;
      * a provisional prev of another publish: a fresh block (revision 1).
    Another session, or no prev: revision 1, first_published_at = scan_time.
    """
    m = meta if isinstance(meta, dict) else {}
    state, reasons = finality(m)
    block = seed_block(m)
    block.update(state=state, reasons=reasons)
    session = block["session"]
    p = prev if isinstance(prev, dict) else None
    if not p or not session or _s10(p.get("session")) != session:
        return block
    rev = max(_int(p.get("revision"), 1), 1)
    first = (p.get("first_published_at") or p.get("published_at")
             or block["published_at"])
    if revised_reason:
        block.update(first_published_at=first, revision=rev + 1,
                     revised_reason=str(revised_reason),
                     revised_at=_now(now))
    elif (p.get("published_at") == block["published_at"]
          or p.get("state") == STATE_FINAL):
        block.update(first_published_at=first, revision=rev,
                     revised_reason=p.get("revised_reason"),
                     revised_at=p.get("revised_at"))
    return block


def ledger_block(row):
    """A list_sessions row (signal_ledger.final_list_session) as a block."""
    if not isinstance(row, dict):
        return None
    return {
        "policy": POLICY,
        "state": row.get("state"),
        "reasons": row.get("reasons") if isinstance(row.get("reasons"), list) else [],
        "session": _s10(row.get("scan_session")) or None,
        "published_at": row.get("published_at"),
        "first_published_at": row.get("first_published_at"),
        "revision": _int(row.get("revision"), 1),
        "strategy_version": row.get("strategy_version"),
        "revised_reason": row.get("revised_reason"),
        "revised_at": row.get("revised_at"),
    }


def pick_prev(session, *blocks):
    """The block a revision of `session` follows: among the candidates for
    that session, the highest revision (a final one on a tie)."""
    best = None
    for b in blocks:
        if not isinstance(b, dict) or _s10(b.get("session")) != _s10(session):
            continue
        key = (_int(b.get("revision"), 1), b.get("state") == STATE_FINAL)
        if best is None or key > best[0]:
            best = (key, b)
    return dict(best[1]) if best else None


# --------------------------------------------------------------------------
# environment and the published site
# --------------------------------------------------------------------------
def force_requested(environ=None):
    """True when the run was dispatched with force_rescan."""
    env = os.environ if environ is None else environ
    return str(env.get(FORCE_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def pages_url(environ=None):
    """The published site's base URL, or None (a local run).

    PAGES_URL comes from the workflow. GitHub can leave
    github.event.repository empty on scheduled events, which turns the
    workflow's expression into a bare 'https://<owner>.github.io/' -- the
    wrong site -- so that shape falls back to GITHUB_REPOSITORY."""
    env = os.environ if environ is None else environ
    url = str(env.get(PAGES_ENV) or "").strip().rstrip("/")
    if url and not url.lower().endswith(".github.io"):
        return url
    repo = str(env.get("GITHUB_REPOSITORY") or "").strip()
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if owner and name:
            return "https://{}.github.io/{}".format(owner.lower(), name)
    return None


def fetch_json(base, name, get=None, timeout=PAGES_TIMEOUT):
    """GET <base>/<name> as JSON, or None. Never raises. Cache-busted: the
    Pages CDN otherwise serves a copy up to ~10 minutes old."""
    if not base:
        return None
    if get is None:
        import requests
        get = requests.get
    try:
        r = get("{}/{}".format(str(base).rstrip("/"), name),
                params={"t": str(int(time.time()))}, timeout=timeout,
                headers={"Cache-Control": "no-cache"})
        if getattr(r, "status_code", None) != 200:
            return None
        return json.loads(r.content.decode("utf-8"))
    except Exception:
        return None


def fetch_pages(base=None, get=None, timeout=PAGES_TIMEOUT):
    """The published scan_result.json, or None when unreachable / not one."""
    doc = fetch_json(base, PAGES_DATA_FILES[0], get=get, timeout=timeout)
    if isinstance(doc, dict) and isinstance(doc.get("meta"), dict):
        return doc
    return None


def pages_status(payload):
    """{session, state, revision, published_at, block} of a published
    payload, or None when there is none (unreachable). A payload from before
    the freeze has no list_status: state None, which is never final."""
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        return None
    meta = payload["meta"]
    ls = meta.get("list_status") if isinstance(meta.get("list_status"), dict) else None
    return {
        "session": _s10((ls or {}).get("session") or meta.get("session_date")) or None,
        "state": (ls or {}).get("state"),
        "revision": _int((ls or {}).get("revision"), 0) or None,
        "published_at": (ls or {}).get("published_at") or meta.get("scan_time"),
        "block": ls,
    }


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------
def decide(session, ledger_row=None, pages=None, forced=False):
    """May this run publish `session`'s list?

    `ledger_row` is signal_ledger.final_list_session(mode, session) (None:
    no final marker); `pages` is pages_status(...) (None: Pages unreachable
    or not configured). Returns {action: 'scan'|'frozen', session, reason,
    revised_reason, warning, prev}:

      forced                         scan; revised_reason force_rescan when
                                     a final list exists
      Pages final for a NEWER one    frozen (pages_ahead) + warning
      ledger final:
        Pages unreachable            frozen (trust the ledger)
        Pages final for it           frozen -- unless Pages shows an OLDER
                                     revision than the ledger (the deploy of
                                     a force_rescan / pages_behind rebuild
                                     never landed): scan, pages_behind. A
                                     restriction_info revision is not rebuilt:
                                     the frozen run's amend re-derives it
                                     from the payload Pages does show.
        Pages behind / provisional   scan, revised_reason pages_behind
      no ledger final:
        Pages final for it           frozen + warning (state commit dropped)
        otherwise                    scan
    """
    s = _s10(session)
    led = None
    if (isinstance(ledger_row, dict) and ledger_row.get("state") == STATE_FINAL
            and _s10(ledger_row.get("scan_session")) == s and s):
        led = ledger_row
    pg = pages if isinstance(pages, dict) else None
    pg_final = bool(pg and pg.get("state") == STATE_FINAL)
    pg_final_s = bool(pg_final and s and pg.get("session") == s)
    prev = pick_prev(s, ledger_block(led) if led else None,
                     (pg or {}).get("block"))
    out = {"action": ACTION_SCAN, "session": s, "reason": "not_final",
           "revised_reason": None, "warning": None, "prev": None}
    if forced:
        out["reason"] = "forced"
        if led or pg_final_s:
            out.update(revised_reason="force_rescan", prev=prev)
        return out
    if pg_final and s and pg.get("session") and pg["session"] > s:
        out.update(action=ACTION_FROZEN, reason="pages_ahead",
                   warning="Pages already shows the final list of {}; not "
                           "republishing the older session {}".format(
                               pg["session"], s))
        return out
    if led:
        pg_rev = _int(pg.get("revision"), 0) if pg else 0
        led_rev = _int(led.get("revision"), 1)
        if pg is None:
            out.update(action=ACTION_FROZEN, reason="final_ledger")
        elif pg_final_s and pg_rev and pg_rev < led_rev \
                and led.get("revised_reason") != "restriction_info":
            out.update(reason="pages_behind", revised_reason="pages_behind",
                       prev=prev,
                       warning="ledger has revision {} of {} ({}) but Pages "
                               "shows revision {}; republishing".format(
                                   led_rev, s, led.get("revised_reason"), pg_rev))
        elif pg_final_s:
            out.update(action=ACTION_FROZEN, reason="final")
        else:
            out.update(reason="pages_behind", revised_reason="pages_behind",
                       prev=prev,
                       warning="ledger says {} is final (revision {}) but Pages "
                               "shows {} {}; republishing".format(
                                   s, led.get("revision"), pg.get("session"),
                                   pg.get("state") or "without a list status"))
        return out
    if pg_final_s:
        out.update(action=ACTION_FROZEN, reason="final_pages",
                   warning="ledger missing final marker for {} (state commit "
                           "dropped)".format(s))
    return out


# --------------------------------------------------------------------------
# evening restriction amend
# --------------------------------------------------------------------------
def _norm(v):
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    return s or None


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    try:
        if v != v:
            return False
    except Exception:
        return False
    return bool(v)


def restriction_feed_complete(info, published, session):
    """(ok, why): is a fresh fetch at least as complete as what the payload
    was built from? Every disposition board must read, and neither best-effort
    list (attention / altered) may go from read to unreadable -- a failed
    fetch must never wipe what the phone already shows."""
    if not isinstance(info, dict):
        return False, "restriction fetch crashed"
    if _s10(info.get("session")) != _s10(session):
        return False, "lists fetched for {} not {}".format(info.get("session"),
                                                          session)
    from scanner.trade_restrictions import BOARDS
    boards = info.get("boards") if isinstance(info.get("boards"), dict) else {}
    bad = [b for b in BOARDS if not (boards.get(b) or {}).get("ok")]
    if bad:
        return False, "disposition list unreadable: {}".format(", ".join(bad))
    pub = published if isinstance(published, dict) else {}
    for key in ("attention_ok", "altered_ok"):
        was = pub.get(key) if isinstance(pub.get(key), dict) else {}
        now = info.get(key) if isinstance(info.get(key), dict) else {}
        lost = [b for b in BOARDS if was.get(b) and not now.get(b)]
        if lost:
            return False, "{} lost for {}".format(key, ", ".join(lost))
    return True, ""


def plan_restriction_amend(payload, info, session, blocking=None):
    """The amended payload for a fresh restriction fetch. Pure.

    Returns (new_payload, changes); changes = {rows, tracked: [sid] whose
    restriction columns changed, flipped: [sid] Buy_Ready True -> False,
    supersede: [recommendation_id], kept: [sid] left alone because a
    'restricted' block is never lifted, picks: {sid: amend_picks update}}.
    Rows are never added, removed or reordered; outside RESTRICTION_COLUMNS
    a row changes only when its new kind is in `blocking`
    (default trade_restrictions.BLOCKING_RESTRICTIONS, read at call time)."""
    import scanner.trade_restrictions as tr
    from portfolio.sync import REC_COLUMNS
    if blocking is None:
        blocking = tr.BLOCKING_RESTRICTIONS
    blocking = tuple(blocking)
    s = _s10(session)
    nxt = None
    if isinstance(info, dict) and _s10(info.get("session")) == s:
        nxt = info.get("next_session")
    if not nxt and s:
        nxt = tr.next_session_after(s)
    new = copy.deepcopy(payload)
    changes = {"rows": [], "tracked": [], "flipped": [], "supersede": [],
               "kept": [], "picks": {}}
    for part in ("rows", "tracked"):
        items = new.get(part)
        if not isinstance(items, list):
            continue
        for r in items:
            if not isinstance(r, dict):
                continue
            sid = str(r.get("Stock_ID") or "").strip()
            fresh = tr.restriction_of(r, info, s, nxt)
            if all(_norm(r.get(c)) == _norm(fresh.get(c))
                   for c in tr.RESTRICTION_COLUMNS):
                continue
            kind = fresh.get("Trade_Restriction")
            if str(r.get("Buy_Block") or "") == "restricted" and kind not in blocking:
                changes["kept"].append(sid)
                continue
            for c in tr.RESTRICTION_COLUMNS:
                r[c] = fresh.get(c)
            changes[part].append(sid)
            if part != "rows":
                continue
            upd = {"restriction": kind,
                   "restriction_until": fresh.get("Restriction_Until")}
            if kind in blocking and _truthy(r.get("Buy_Ready")):
                r["Buy_Ready"] = False
                r["Buy_Block"] = "restricted"
                upd.update(buy_ready=0, buy_block="restricted")
                changes["flipped"].append(sid)
                rid = r.get("Recommendation_ID")
                if (rid and r.get("Rec_Status") == "active"
                        and _s10(r.get("Recommended_On")) == s):
                    changes["supersede"].append(str(rid))
                    for c in REC_COLUMNS:
                        if c in r:
                            r[c] = None
            changes["picks"][sid] = upd
    if not (changes["rows"] or changes["tracked"]):
        return new, changes
    import pandas as pd
    meta = new.setdefault("meta", {})
    quality = meta.get("quality") if isinstance(meta.get("quality"), dict) else {}
    rows = new.get("rows") if isinstance(new.get("rows"), list) else []
    tracked = new.get("tracked") if isinstance(new.get("tracked"), list) else []
    rows_df = pd.DataFrame(rows) if rows else None
    if changes["flipped"]:
        quality["buy_ready"] = int(sum(1 for r in rows
                                       if _truthy(r.get("Buy_Ready"))))
        blocks = {}
        for r in rows:
            b = r.get("Buy_Block")
            if b:
                blocks[str(b)] = blocks.get(str(b), 0) + 1
        quality["blocks"] = blocks
    quality["restrictions"] = tr.summarize(
        info, rows_df, pd.DataFrame(tracked) if tracked else None)
    meta["quality"] = quality
    return new, changes


def _default_paths():
    from config.settings import (MOBILE_DATA_FILE, MOBILE_QUOTES_FILE,
                                 MOBILE_UNIVERSE_FILE, RECOMMENDATIONS_EXPORT_FILE,
                                 SCAN_CHECKS_FILE, SIGNAL_LEDGER_FILE,
                                 PORTFOLIO_LEDGER_FILE)
    return {"scan_result.json": MOBILE_DATA_FILE,
            "quotes.json": MOBILE_QUOTES_FILE,
            "universe.json": MOBILE_UNIVERSE_FILE,
            "recs": RECOMMENDATIONS_EXPORT_FILE,
            "history": SCAN_CHECKS_FILE,
            "signal_ledger": SIGNAL_LEDGER_FILE,
            "portfolio_ledger": PORTFOLIO_LEDGER_FILE}


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except Exception:
        return None


def _restore(backups):
    for path, data in backups.items():
        try:
            if data is None:
                if Path(path).exists():
                    Path(path).unlink()
            else:
                Path(path).write_bytes(data)
        except Exception:
            pass


def _write_json(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))


def _superseded_doc(recs, rec_ids, session):
    """The recommendations export with `rec_ids` superseded, in memory (the
    same transition tools/rec_set_status.set_status writes)."""
    if not isinstance(recs, dict) or not rec_ids:
        return recs
    doc = copy.deepcopy(recs)
    for rec in doc.get("recommendations") or []:
        if (isinstance(rec, dict) and rec.get("recommendation_id") in rec_ids
                and rec.get("status") == "active"):
            rec.update(status="superseded", status_reason="retracted:restricted",
                       status_session=session, outcome=None)
    return doc


def amend_restrictions(payload, mode, base_url=None, ledger_row=None,
                       fetch=None, get=None, paths=None, expected_session=None,
                       now=None, blocking=None):
    """Refresh the restriction columns of a FINAL published payload.

    `payload` is the Pages scan_result.json (CI's checkout does not carry
    one), `ledger_row` the session's final list_sessions row (None: the
    state commit was dropped; the ledger is then left alone). Returns
    (outcome, detail): AMENDED (files written, the workflow republishes),
    UNCHANGED or SKIPPED (nothing written, the run stays frozen).

    Order: fetch -> completeness -> plan -> download quotes / universe ->
    check the amended payload in memory -> write -> check_files stamps
    list_status (revision + 1, revised_reason restriction_info) -> verify
    final (else every file is put back) -> ledger: picks' restriction /
    buy decision and the list_sessions row. Never raises."""
    try:
        return _amend(payload, mode, base_url, ledger_row, fetch, get, paths,
                      expected_session, now, blocking)
    except Exception as e:
        return SKIPPED, "amend crashed: {}: {}".format(type(e).__name__,
                                                      str(e)[:120])


def _amend(payload, mode, base_url, ledger_row, fetch, get, paths,
           expected_session, now, blocking):
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        return SKIPPED, "no published payload"
    session = _s10(meta.get("session_date"))
    ls = meta.get("list_status") if isinstance(meta.get("list_status"), dict) else None
    if not session or not ls or ls.get("state") != STATE_FINAL \
            or _s10(ls.get("session")) != session:
        return SKIPPED, "the published list is not final"
    if mode and meta.get("mode") and meta.get("mode") != mode:
        return SKIPPED, "published mode {} is not {}".format(meta.get("mode"), mode)
    if fetch is None:
        from scanner.trade_restrictions import fetch_restrictions as fetch
    info = fetch(session)
    quality = meta.get("quality") if isinstance(meta.get("quality"), dict) else {}
    ok, why = restriction_feed_complete(info, quality.get("restrictions"), session)
    if not ok:
        return SKIPPED, why
    new, changes = plan_restriction_amend(payload, info, session, blocking)
    n_rows = len(payload.get("rows") or [])
    if not (changes["rows"] or changes["tracked"]):
        return UNCHANGED, "restriction columns unchanged ({} rows, {} tracked{})".format(
            n_rows, len(payload.get("tracked") or []),
            ", {} restricted kept".format(len(changes["kept"])) if changes["kept"] else "")

    files = {}
    for name in PAGES_DATA_FILES[1:]:
        doc = fetch_json(base_url, name, get=get)
        if doc is None:
            return SKIPPED, "Pages {} unreachable".format(name)
        files[name] = doc

    p = dict(_default_paths())
    p.update(paths or {})
    from scanner.result_checks import check_payload, check_files, tracked_ids_from_ledger
    recs = None
    recs_path = Path(p["recs"]) if p.get("recs") else None
    if recs_path is not None and recs_path.exists():
        try:
            recs = json.loads(recs_path.read_text(encoding="utf-8"))
        except Exception:
            recs = None
    if changes["supersede"] and recs is None:
        return SKIPPED, "cannot retract {}: no recommendations export".format(
            ", ".join(changes["supersede"]))
    new_recs = _superseded_doc(recs, set(changes["supersede"]), session)
    led_path = Path(p["signal_ledger"]) if p.get("signal_ledger") else None
    led_ok = led_path is not None and led_path.exists()
    quotes = files["quotes.json"]
    tracked = []
    if led_ok and isinstance(quotes, dict) and quotes.get("sessions"):
        tracked = tracked_ids_from_ledger(led_path, str(meta.get("mode") or ""),
                                          quotes["sessions"][0])
    prev = dict(ls)
    if isinstance(ledger_row, dict) and _s10(ledger_row.get("scan_session")) == session:
        prev["revision"] = max(_int(prev.get("revision"), 1),
                               _int(ledger_row.get("revision"), 1))
    report = check_payload(new, quotes=quotes, recs=new_recs,
                           tracked_ids=tracked, expected_session=expected_session)
    trial = dict(new.get("meta") or {})
    trial["checks"] = report
    block = list_status_block(trial, prev=prev, revised_reason="restriction_info",
                              now=now)
    if block["state"] != STATE_FINAL:
        return SKIPPED, "the amended list would not be final ({}; checks {})".format(
            ",".join(block["reasons"]), report.get("status"))

    scan_path = Path(p["scan_result.json"])
    touched = [scan_path, Path(p["quotes.json"]), Path(p["universe.json"])]
    if p.get("history"):
        touched.append(Path(p["history"]))
    if recs_path is not None:
        touched.append(recs_path)
    backups = {path: _read_bytes(path) for path in touched}
    try:
        for name in PAGES_DATA_FILES[1:]:
            _write_json(p[name], files[name])
        if changes["supersede"]:
            from tools.rec_set_status import set_status
            for rid in changes["supersede"]:
                set_status(recs_path, rid, "superseded", "retracted:restricted",
                           session)
        _write_json(scan_path, new)
        check_files(scan_path, quotes_path=p["quotes.json"],
                    recs_path=recs_path if recs is not None else None,
                    ledger_path=led_path if led_ok else None,
                    history_path=p.get("history"),
                    expected_session=expected_session,
                    revised_reason="restriction_info", list_prev=prev, now=now)
        stamped = ((json.loads(scan_path.read_text(encoding="utf-8")).get("meta")
                    or {}).get("list_status") or {})
        if stamped.get("state") != STATE_FINAL:
            raise RuntimeError("stamped list_status is {} ({})".format(
                stamped.get("state"), ",".join(stamped.get("reasons") or [])))
    except Exception as e:
        _restore(backups)
        return SKIPPED, "amend rolled back: {}".format(str(e)[:160])

    notes = []
    port = p.get("portfolio_ledger")
    if changes["supersede"] and port and Path(port).exists():
        try:
            from portfolio.ledger import open_ledger, transition_recommendation
            conn = open_ledger(port)
            try:
                for rid in changes["supersede"]:
                    transition_recommendation(conn, rid, "superseded",
                                              "retracted:restricted", session)
            finally:
                conn.close()
        except Exception as e:
            notes.append("portfolio ledger not updated: {}".format(str(e)[:80]))
    if (isinstance(ledger_row, dict) and ledger_row.get("state") == STATE_FINAL
            and _s10(ledger_row.get("scan_session")) == session and led_ok):
        from scanner.signal_ledger import amend_picks, record_list_session
        rows = new.get("rows") or []
        n = amend_picks(session, mode or meta.get("mode"), changes["picks"],
                        path=led_path)
        got = record_list_session(
            mode or meta.get("mode"), stamped, rows=len(rows),
            buy_ready_ids=[str(r.get("Stock_ID")) for r in rows
                           if _truthy(r.get("Buy_Ready"))],
            path=led_path)
        notes.append("ledger: {} pick(s) amended, list session {}".format(n, got))
    else:
        notes.append("ledger has no final marker for {}: left alone".format(session))
    return AMENDED, ("{} row(s) + {} tracked re-annotated, {} buy(s) withdrawn, "
                     "{} recommendation(s) retracted; revision {}; {}".format(
                         len(changes["rows"]), len(changes["tracked"]),
                         len(changes["flipped"]), len(changes["supersede"]),
                         stamped.get("revision"), "; ".join(notes)))
