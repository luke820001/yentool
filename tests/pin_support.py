"""Shared fixtures for the tests/test_pin_*.py files. ASCII only.

Not a test module (the name does not start with test_), so discovery skips it.
Everything here builds SYNTHETIC data: nothing reads a tracked data file and
nothing touches the network, so the pins stay valid whatever the daily scan
commits.
"""
import contextlib
import gc
import sqlite3
import tempfile
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MODE = "mode_prelaunch"
TODAY = "2026-09-09"


def collect():
    """gc.collect() without the ResourceWarning noise from storage.data_store,
    which leaves its sqlite handle to the garbage collector (on Windows the
    file stays locked until it runs)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


def read_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def taiex_frame(closes, last="2026-10-08"):
    """date/close frame of business days ending on `last`."""
    dates = pd.bdate_range(end=last, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": list(dates), "close": [float(x) for x in closes]})


@contextlib.contextmanager
def raw_index():
    """These pins exercise the MEAN rule on tapes with arbitrary steps and
    business-day dates that ignore exchange holidays. The cleaning of bad index
    bars (scanner/index_clean: non-sessions, 10 percent limit) has its own
    tests (tests/test_index_clean.py), so it is switched off here -- the three
    settings every reader shares -- and nothing else changes."""
    from unittest import mock
    from scanner import index_clean
    with mock.patch.object(index_clean, "MAX_DAILY_MOVE", 1e9),             mock.patch.object(index_clean, "_is_session", lambda day: True):
        yield


@contextlib.contextmanager
def taiex_db(closes, last="2026-10-08"):
    """A temporary taiex.db holding the TAIEX table; yields (path, dates)."""
    t = taiex_frame(closes, last)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "taiex.db"
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE TAIEX (date TEXT, close REAL)")
            conn.executemany("INSERT INTO TAIEX VALUES (?, ?)",
                             list(zip(t["date"], t["close"])))
            conn.commit()
        finally:
            conn.close()
        try:
            with raw_index():
                yield path, list(t["date"])
        finally:
            collect()
