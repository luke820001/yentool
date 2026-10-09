"""
Market-regime signal from the cached TAIEX series. The whole momentum/surge edge
is regime-dependent: on the research data it held in trending years (lift ~1.4+)
but collapsed in the 2022 bear (lift ~1.07). So a scan should tell the user
whether the market is a tailwind or a headwind for these strategies.
ASCII only outside the user-facing banner text.
"""
import json
from pathlib import Path

import pandas as pd
from storage.data_store import load_sheet, max_stored_date
from config.settings import TAIEX_FILE, PRICE_VOLUME_FILE
from scanner.index_clean import clean_closes

_TEXT_FILE = Path(__file__).resolve().parent.parent / "config" / "regime_text.json"

# ASCII fallbacks: the Chinese banner lives in config/regime_text.json. A missing
# or unreadable file must never break the regime, only plain-English the banner.
_TEXT_FALLBACK = {
    "no_data": "Market regime: not enough data",
    "stale": "Market data is stale: TAIEX latest {as_of}, stock data {ref}; not treated as a tailwind",
    "tailwind": "Market tailwind: TAIEX above 20/60MA (60-day drawdown {dd}%)",
    "neutral": "Market neutral: TAIEX above 60MA, below 20MA; no new entries, holdings follow exit rules (60-day drawdown {dd}%)",
    "headwind": "Market headwind: TAIEX below 60MA; no new entries, holdings follow exit rules (60-day drawdown {dd}%)",
}


def _text(key, **kw):
    """Banner text for `key` from config/regime_text.json, else the ASCII
    fallback. Never raises."""
    try:
        data = json.loads(_TEXT_FILE.read_text(encoding="utf-8"))
        tpl = data.get(key)
        if not isinstance(tpl, str) or not tpl:
            tpl = _TEXT_FALLBACK[key]
    except Exception:
        tpl = _TEXT_FALLBACK[key]
    try:
        return tpl.format(**kw)
    except Exception:
        return _TEXT_FALLBACK[key].format(**kw)


def get_market_regime(data_date=None) -> dict:
    """Return {ok, risk_on, above20, enter_ok, str20, strong, text,
    as_of_date, ref_date, is_current}. risk_on=False means the momentum edge is
    unreliable (or unknown -- see below).

    `data_date` is the bar date the scan is actually working on ("YYYY-MM-DD").
    Left None it is read from price_volume.db, i.e. the newest stock bar on
    disk, because that IS the data being scanned.

    F15 (2026-09-09 audit): the only sufficiency check used to be `len(c) < 60`,
    so a TAIEX cache that stopped updating months ago still rendered as
    "big tailwind" -- the banner, the phone's entry gate and mark_buy_ready all
    read a stale opinion as a live one. Two keys close that hole:
      as_of_date  the date of the last TAIEX bar the answer was computed from.
      is_current  as_of_date is NOT older than the stock data being scanned.
    Staleness is a real gate, not a label: when is_current is False the three
    tailwind keys (risk_on / enter_ok / strong) are forced False, so a caller
    that only reads `risk_on` still cannot be handed a tailwind by a cache.
    A reference date we cannot determine counts as not-current: "we do not know
    how old this is" is not "it is fresh".

    Callers must keep treating a missing key as unknown -- gui/app.py,
    scan_mode.mark_buy_ready, holding_tracker and result_export all read this
    dict, and keys are only ever added here, never renamed or removed.
    """
    # Default = "we know nothing". risk_on used to seed True, which meant a
    # caller doing `if reg.get("risk_on")` was handed a tailwind by the very
    # dict that says ok=False. An unknown regime must fail CLOSED (F15).
    out = {"ok": False, "risk_on": False, "enter_ok": False, "strong": False,
           "as_of_date": None, "ref_date": None, "is_current": False,
           "text": _text("no_data")}
    try:
        ref_date = str(data_date or "")[:10] or max_stored_date(PRICE_VOLUME_FILE)
        out["ref_date"] = ref_date

        t = load_sheet(TAIEX_FILE, "TAIEX")
        if t.empty:
            return out
        # Only bars that are real sessions with a plausible close, and only up
        # to the session being scanned: a partial same-day print dated AFTER the
        # scan's data date must not be read as "today" (2026-10-09 audit D1-03),
        # and a corrupt or weekend bar must not move the moving averages
        # (D11-06, M-12). bars_dropped says how many were refused.
        t, dropped = clean_closes(t)
        out["bars_dropped"] = int(sum(dropped.values()))
        if ref_date:
            t = t[t["date"] <= ref_date]
        c = t["close"]
        if len(c) < 60:
            return out

        # Dates are stored as "YYYY-MM-DD" strings throughout, so a plain string
        # compare is a date compare -- no parsing, no timezone to get wrong.
        as_of = str(t["date"].iloc[-1])[:10]
        out["as_of_date"] = as_of
        is_current = bool(as_of and ref_date and as_of >= ref_date)
        out["is_current"] = is_current

        cur = float(c.iloc[-1])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1])
        dd = float((c.tail(60) / c.tail(60).cummax() - 1).min()) * 100

        above60 = cur > ma60
        above20 = cur > ma20
        out["ok"] = True
        out["risk_on"] = above60 and is_current
        # above20 stays the raw reading: it is descriptive, not a permission,
        # and holding_tracker only acts on it together with risk_on (which the
        # staleness gate already switched off).
        out["above20"] = above20    # exit-delay uses this: below 20MA = disturbed
        # enter_ok = the strict tailwind gate the prelaunch overlay backtest used
        # (TAIEX above BOTH 20 and 60MA). Only open NEW prelaunch positions here;
        # this is what lifts the OTC win rate to ~56pct / alpha +5.5pp.
        out["enter_ok"] = above60 and above20 and is_current
        # str20 = how far above the 20MA, fraction (sandbox 2026-07-11, C2):
        # >= 0.022 marked the stronger-tailwind days in BOTH 6y windows
        # (+2.5pp train / +1.4pp valid). Per the 2026-07-06 settled finding
        # (regime hard gate adds nothing, use for sizing), this is surfaced
        # as a banner tier / sizing hint, NOT a filter.
        out["str20"] = round(cur / ma20 - 1, 4) if ma20 > 0 else None
        out["strong"] = bool(out["enter_ok"] and out["str20"] is not None
                             and out["str20"] >= 0.022)

        if not is_current:
            # Say WHICH bar the opinion came from. "not enough data" would be a lie --
            # the series is long enough, it is simply not about today.
            out["text"] = _text("stale", as_of=as_of or "-", ref=ref_date or "?")
        elif above60 and above20:
            out["text"] = _text("tailwind", dd="{:.0f}".format(dd))
        elif above60:
            out["text"] = _text("neutral", dd="{:.0f}".format(dd))
        else:
            out["text"] = _text("headwind", dd="{:.0f}".format(dd))
    except Exception:
        # Anything half-computed is unusable: an exception after the keys were
        # filled must not leave a tailwind behind (fail closed, F15).
        out.update(ok=False, risk_on=False, enter_ok=False, strong=False,
                   is_current=False)
    return out
