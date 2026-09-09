"""
Market-regime signal from the cached TAIEX series. The whole momentum/surge edge
is regime-dependent: on the research data it held in trending years (lift ~1.4+)
but collapsed in the 2022 bear (lift ~1.07). So a scan should tell the user
whether the market is a tailwind or a headwind for these strategies.
ASCII only outside the user-facing banner text.
"""
import pandas as pd
from storage.data_store import load_sheet, max_stored_date
from config.settings import TAIEX_FILE, PRICE_VOLUME_FILE


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
           "text": "大盤狀態：資料不足"}
    try:
        ref_date = str(data_date or "")[:10] or max_stored_date(PRICE_VOLUME_FILE)
        out["ref_date"] = ref_date

        t = load_sheet(TAIEX_FILE, "TAIEX")
        if t.empty:
            return out
        t = t.copy()
        t["close"] = pd.to_numeric(t["close"], errors="coerce")
        t = t.dropna(subset=["close"]).sort_values("date")
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
            # Say WHICH bar the opinion came from. "資料不足" would be a lie --
            # the series is long enough, it is simply not about today.
            out["text"] = ("大盤資料過期：TAIEX 最新 {}，個股資料 {}，"
                           "本次不視為順風（請先更新大盤資料）".format(
                               as_of or "無", ref_date or "未知"))
        elif above60 and above20:
            out["text"] = "大盤順風：TAIEX 站上 20/60MA，動能策略 edge 正常（60日回檔 {:.0f}%）".format(dd)
        elif above60:
            out["text"] = "大盤中性：TAIEX 在 60MA 上、跌破 20MA，留意轉弱（60日回檔 {:.0f}%）".format(dd)
        else:
            out["text"] = ("大盤逆風：TAIEX 跌破 60MA，動能策略 edge 易失效，"
                           "建議降部位（60日回檔 {:.0f}%）".format(dd))
    except Exception:
        pass
    return out
