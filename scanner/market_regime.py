"""
Market-regime signal from the cached TAIEX series. The whole momentum/surge edge
is regime-dependent: on the research data it held in trending years (lift ~1.4+)
but collapsed in the 2022 bear (lift ~1.07). So a scan should tell the user
whether the market is a tailwind or a headwind for these strategies.
ASCII only.
"""
import pandas as pd
from storage.data_store import load_sheet
from config.settings import TAIEX_FILE


def get_market_regime() -> dict:
    """Return {ok, risk_on, text}. risk_on=False means momentum edge is unreliable."""
    out = {"ok": False, "risk_on": True, "text": "大盤狀態：資料不足"}
    try:
        t = load_sheet(TAIEX_FILE, "TAIEX")
        if t.empty:
            return out
        t = t.copy()
        t["close"] = pd.to_numeric(t["close"], errors="coerce")
        t = t.dropna(subset=["close"]).sort_values("date")
        c = t["close"]
        if len(c) < 60:
            return out
        cur = float(c.iloc[-1])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1])
        dd = float((c.tail(60) / c.tail(60).cummax() - 1).min()) * 100

        above60 = cur > ma60
        above20 = cur > ma20
        out["ok"] = True
        out["risk_on"] = above60
        out["above20"] = above20    # exit-delay uses this: below 20MA = disturbed
        # enter_ok = the strict tailwind gate the prelaunch overlay backtest used
        # (TAIEX above BOTH 20 and 60MA). Only open NEW prelaunch positions here;
        # this is what lifts the OTC win rate to ~56pct / alpha +5.5pp.
        out["enter_ok"] = above60 and above20
        # str20 = how far above the 20MA, fraction (sandbox 2026-07-11, C2):
        # >= 0.022 marked the stronger-tailwind days in BOTH 6y windows
        # (+2.5pp train / +1.4pp valid). Per the 2026-07-06 settled finding
        # (regime hard gate adds nothing, use for sizing), this is surfaced
        # as a banner tier / sizing hint, NOT a filter.
        out["str20"] = round(cur / ma20 - 1, 4) if ma20 > 0 else None
        out["strong"] = bool(out["enter_ok"] and out["str20"] is not None
                             and out["str20"] >= 0.022)

        if above60 and above20:
            out["text"] = "大盤順風：TAIEX 站上 20/60MA，動能策略 edge 正常（60日回檔 {:.0f}%）".format(dd)
        elif above60:
            out["text"] = "大盤中性：TAIEX 在 60MA 上、跌破 20MA，留意轉弱（60日回檔 {:.0f}%）".format(dd)
        else:
            out["text"] = ("大盤逆風：TAIEX 跌破 60MA，動能策略 edge 易失效，"
                           "建議降部位（60日回檔 {:.0f}%）".format(dd))
    except Exception:
        pass
    return out
