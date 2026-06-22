"""
Tiny cross-run state store for the scanner. Persists the Stock_IDs selected on
the previous run of each mode so the hysteresis top-N (scanner.scan_mode
.select_with_hysteresis) can HOLD a name through day-to-day noise instead of
dropping it the moment it slips below the strict entry cutoff. ASCII only.

One small JSON per mode under data/scan_state/. Failures are non-fatal: a missing
or unreadable file just yields an empty prior set (= plain top-N, no hysteresis).
"""
import json
from config.settings import DATA_DIR

_STATE_DIR = DATA_DIR / "scan_state"


def _path(mode: str):
    safe = "".join(ch for ch in str(mode) if ch.isalnum() or ch in ("_", "-"))
    return _STATE_DIR / "{}.json".format(safe or "default")


def load_held_ids(mode: str) -> list:
    try:
        data = json.loads(_path(mode).read_text(encoding="utf-8"))
        ids = data.get("held_ids", [])
        return [str(x) for x in ids]
    except Exception:
        return []


def save_held_ids(mode: str, ids) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _path(mode).write_text(
            json.dumps({"held_ids": [str(x) for x in (ids or [])]},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
