"""
Tiny cross-run state store for the scanner. Persists the Stock_IDs selected on
the previous run of each mode so the hysteresis top-N (scanner.scan_mode
.select_with_hysteresis) can HOLD a name through day-to-day noise instead of
dropping it the moment it slips below the strict entry cutoff. ASCII only.

One small JSON per mode under data/scan_state/. Failures are non-fatal: a missing
or unreadable file just yields an empty prior set (= plain top-N, no hysteresis).

Session-aware since 2026-10-08 (plan P0-5b). The file is

    {"held_ids": [...], "session": "YYYY-MM-DD",
     "prior_ids": [...], "prior_session": "YYYY-MM-DD"}

`held_ids` is what the last run of `session` selected; `prior_ids` is the set
that session's hysteresis started from (what the PREVIOUS session held). A
second run of the same session used to start from the first run's own output,
so a name that entered at 15:00 was "held" at 21:00 and got the loose N_HOLD
band -- the list drifted on every rerun. prior_for() now hands a same-session
rerun the previous session's set instead. The old {"held_ids": [...]} format
still reads (session None), and load_held_ids / save_held_ids stay for
gui/scan_worker.py; save_held_ids keeps the other keys.
"""
import datetime as _dt
import json
from config.settings import DATA_DIR

_STATE_DIR = DATA_DIR / "scan_state"


def _today():
    """Today's date in Taipei (the CI runner clock is UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("Asia/Taipei")).date()
    except Exception:
        return (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).date()


def valid_day(v):
    """True for an ISO 'YYYY-MM-DD' (or longer, date first) that is a real date
    from 2000 up to tomorrow in Taipei. A session that is NaN, junk or in the
    future cannot be the date of any bar; one such value saved as the session
    made every later save look "older" and be refused (2026-10-09 audit
    D11-04)."""
    s = str(v or "").strip()
    if len(s) < 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        d = _dt.date.fromisoformat(s[:10])
    except ValueError:
        return False
    return _dt.date(2000, 1, 1) <= d <= _today() + _dt.timedelta(days=1)


def _path(mode: str):
    safe = "".join(ch for ch in str(mode) if ch.isalnum() or ch in ("_", "-"))
    return _STATE_DIR / "{}.json".format(safe or "default")


def _ids(v):
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else None


def _day(v):
    """The first 10 characters of a date-like value, or None when there is
    nothing there. NOT validated: see valid_day."""
    return str(v or "").strip()[:10] or None


def _good_day(v):
    d = _day(v)
    return d if d and valid_day(d) else None


def _raw(mode):
    try:
        data = json.loads(_path(mode).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(mode, data):
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _path(mode).write_text(json.dumps(data, ensure_ascii=False),
                               encoding="utf-8")
        return True
    except Exception:
        return False


def load_state(mode: str) -> dict:
    """{held_ids, session, prior_ids, prior_session}. A missing file, or the
    old format, reads as session None and prior_ids None."""
    data = _raw(mode)
    return {
        "held_ids": _ids(data.get("held_ids")) or [],
        "session": _good_day(data.get("session")),
        "prior_ids": _ids(data.get("prior_ids")),
        "prior_session": _good_day(data.get("prior_session")),
    }


def prior_for(state: dict, session) -> list:
    """The set this run's hysteresis starts from: on a rerun of the stored
    session, the set THAT session started from; otherwise what was held
    last."""
    s = _day(session)
    st = state if isinstance(state, dict) else {}
    if s and st.get("session") == s and st.get("prior_ids") is not None:
        return list(st["prior_ids"])
    return list(st.get("held_ids") or [])


def save_state(mode: str, session, held_ids, prior_ids=None) -> bool:
    """Persist what `session` selected.

    A new session rolls the stored held set into prior_ids / prior_session
    (or takes `prior_ids` when given: the set the hysteresis actually started
    from). The same session keeps the prior it already has. An OLDER session
    never overwrites a newer one (a lagging feed). Without a session this is
    save_held_ids. Returns True when written."""
    s = _day(session)
    held = [str(x) for x in (held_ids or [])]
    if not s:
        save_held_ids(mode, held)
        return True
    if not valid_day(s):
        return False        # junk or future: never becomes the stored session
    data = _raw(mode)
    st = load_state(mode)
    cur = st["session"]
    if cur and cur > s:
        return False
    if cur == s:
        prior = st["prior_ids"]
        if prior is None:
            prior = _ids(prior_ids)
        prior_session = st["prior_session"]
    else:
        prior = _ids(prior_ids)
        if prior is None:
            prior = list(st["held_ids"])
        prior_session = cur
    data.update({"held_ids": held, "session": s, "prior_ids": prior,
                 "prior_session": prior_session})
    return _write(mode, data)


def load_held_ids(mode: str) -> list:
    return list(load_state(mode)["held_ids"])


def save_held_ids(mode: str, ids) -> None:
    """Replace held_ids only; every other key (session, prior_ids, ...) is
    kept (read-modify-write)."""
    data = _raw(mode)
    data["held_ids"] = [str(x) for x in (ids or [])]
    _write(mode, data)
