"""
Strict JSON for the files the phone parses.

Python's json.dump writes NaN / Infinity as bare tokens, which are not JSON:
the phone's JSON.parse rejects the whole file, so one non-finite number in any
block (a live-record mean, a quote, a level) would blank the app
(2026-10-09 audit D9-01). Every published file is written through dumps_strict
instead: non-finite numbers become null, and allow_nan=False makes any other
way of smuggling one in an error rather than a bad file. ASCII only.
"""
import json
import math


def clean(obj):
    """A copy of `obj` with every non-finite float replaced by None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    try:                                     # numpy scalars
        import numpy as np
        if isinstance(obj, np.floating):
            f = float(obj)
            return f if math.isfinite(f) else None
    except Exception:
        pass
    return obj


def dump_strict(obj, fp, **kw):
    """json.dump with non-finite numbers nulled and allow_nan=False."""
    kw["allow_nan"] = False
    json.dump(clean(obj), fp, **kw)


def dumps_strict(obj, **kw):
    kw["allow_nan"] = False
    return json.dumps(clean(obj), **kw)


NON_FINITE_TOKENS = ("NaN", "Infinity", "-Infinity")


def has_non_finite_token(text):
    """True when `text` (a JSON document) contains a bare NaN / Infinity
    token outside a string. A cheap scan: tokens inside strings are skipped."""
    in_str = False
    esc = False
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "N" and text.startswith("NaN", i):
            return True
        elif ch == "I" and text.startswith("Infinity", i):
            return True
        elif ch == "-" and text.startswith("-Infinity", i):
            return True
        i += 1
    return False
