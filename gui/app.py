import datetime
import json
import math
import os
import threading
import tkinter as tk
from tkinter import ttk
from gui.scan_worker import ScanWorker, SingleStockWorker
from gemini_hook.gemini_client import generate_report, GeminiError


def _load_scan_modes():
    """Load scan mode keys and labels from config/scan_modes.json."""
    cfg = os.path.join(os.path.dirname(__file__), "..", "config", "scan_modes.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        modes = data.get("modes", [])
        if modes:
            return modes
    except Exception:
        pass
    return [{"key": "mode_squeeze", "label": "Classic Squeeze"}]

WINDOW_TITLE = "Taiwan Stock Chip Scanner"
WINDOW_SIZE  = "1380x660"

# ── 配色系統 ──────────────────────────────────────────────────────────────────
BG        = "#1E1E24"   # 主背景（60%）
SURFACE   = "#25262D"   # 次要背景（30%）
HEADER_BG = "#2B2D36"   # 表頭
ROW_ALT   = "#25262D"   # 斑馬紋偶數行
ROW_HOVER = "#3A3D4A"   # Hover
ACCENT    = "#4A90E2"   # 點綴藍（10%）
FG        = "#E0E0E0"   # 主文字
DIM       = "#757575"   # 次要文字
GREEN     = "#4CAF50"   # 成立 / 正值
YELLOW    = "#FFD740"   # 爆發≥70（亮金黃，最強）
ORANGE    = "#FF8A3D"   # 爆發≥50（飽和橘，明顯偏橘）
RED       = "#E57373"   # 負值 / 警示
ROW_HIGH  = "#2E2A14"   # 爆發≥70 底色（暖金底）
ROW_MID   = "#2E2014"   # 爆發≥50 底色（暖橘底）

FONT       = "Microsoft JhengHei UI"
BOOL_TRUE  = "✓"
BOOL_FALSE = "-"

# ── 主列表欄位（比例權重）────────────────────────────────────────────────────
# Concise main list: identity + the buy verdict + the trade plan + the one
# ranking score + two pieces of at-a-glance context. Everything diagnostic
# (other scores, ATR, RS, gaps, signal lights, MAs) lives in the double-click
# detail panel below.
#
# Six of the ids below are SYNTHETIC -- Buy_State / Price_Basis / Entry_Price /
# Stop_Price / Take_Profit_Price / Rank_Score have no backing DataFrame column,
# because what belongs in them depends on the row (entered vs not) and on the
# scan mode. _render_result() stashes a sortable value under each of those ids
# in the per-row dict so _row_sort_key keeps working unchanged.
#
# Initial_Price is NOT synthetic: it is portfolio/sync.py's Initial_Buy_Price,
# the price we fixed the first day this name passed the complete gate. It sits
# next to 進場價 on purpose. Those two columns are the whole of F01 made
# visible -- 進場價 is recomputed from today's close on every single scan, 首日
# 建議 never moves again, and until now the desktop only ever showed the first
# one and called it "the suggested buy".
MAIN_COLUMNS = [
    ("Stock_ID",            "代號",      3),
    ("Stock_Name",          "名稱",      5),
    ("Market",              "市場",      2.5),
    ("Buy_State",           "買進",      6.5),
    ("Close_Price",         "收盤價",    3.5),
    ("Price_Basis",         "價格基準",  3.5),
    ("Initial_Price",       "首日建議",  3.5),
    ("Entry_Price",         "進場價",    3.5),
    ("Stop_Price",          "停損價",    3.5),
    ("Take_Profit_Price",   "停利目標",  3.5),
    ("Scale_Out_Price",     "賣半(選)",  3.5),
    ("Rank_Score",          "主分數",    3.5),
    ("Gain_3M_Pct",         "3月漲幅%",  3.5),
    ("Foreign_Net_5D",      "外資5日",   3.5),
]
_TOTAL_WEIGHT = sum(w for _, _, w in MAIN_COLUMNS)

# ── 各模式的排序主分數 ───────────────────────────────────────────────────────
# Mirrors scanner.scan_mode._SORT_KEYS. The list must SHOW and COLOUR the score
# the mode actually ranks on: the old code displayed Launch_Score while tinting
# rows by Surge_Score, so two different numbers were read as one strength.
MODE_SCORE = {
    "mode_squeeze":         ("Explosion_Score", "蓄勢分"),
    "mode_bottom":          ("Surge_Score",     "噴發分"),
    "mode_breakout":        ("Surge_Score",     "噴發分"),
    "mode_short_explosion": ("Surge_Score",     "噴發分"),
    "mode_momentum_leader": ("Surge_Score",     "噴發分"),
    "mode_prelaunch":       ("Launch_Score",    "起漲分"),
}
MODE_SCORE_DEFAULT = ("Explosion_Score", "蓄勢分")

# ── 買進閘門的不成立原因 ─────────────────────────────────────────────────────
# Reason codes come from scanner.scan_mode.mark_buy_ready (Buy_Block). The first
# five are copied verbatim from mobile/app.js BLOCK_TEXT on purpose: the same
# row must not be refused with two different explanations on the two ends. The
# rest are codes mobile's map does not carry yet; an unmapped code falls through
# as its raw ASCII rather than being silently dropped.
BLOCK_TEXT = {
    "regime":    "大盤未站上20/60MA",
    # Not the same fact: the index feed is simply behind the stock data, so the
    # regime cannot be judged. Saying "has not reclaimed its averages" there is
    # a false statement about the market (2026-09-21).
    "regime_stale": "大盤資料尚未更新到今天，本次不判定順風",
    "rank":      "非前20名",
    "market":    "非上櫃",
    "quality":   "未過品質閘門",
    "held":      "已進場·非新訊號",
    "unknown":   "持倉狀態不明",
    "integrity": "資料完整性未過",
    "stale":     "個股資料非最新",
    "no_rule":   "此模式無驗證買進規則",
}

# ── 各模式決策卡（依 signal ledger 實戰驗證，見 docs/EVAL_PLAYBOOK.md）────────
# mode_prelaunch 是目前唯一經實戰模擬驗證有正期望值的模式；其進場/出場規則
# 直接顯示在橫幅。momentum_leader 照舊建議操作的實戰紀錄為負，明確警告。
MODE_RULE_CARDS = {
    "mode_prelaunch": (
        "只買OTC核心+(貼近52週高、未起漲、日振幅≥4.5%) · 順風才進場 · 隔日開盤進 · -20%災難停損 · "
        "收盤站上+2.5%後隔日起停損上調到+2% · 觸+20%停利 · 第8天起收盤仍有實質獲利(+1%以上)就隔日開盤收下 · "
        "抱10天，第10天收盤仍站上5日均價則續抱(最晚20天) · "
        "回測(2017-2026, 556筆, 近3年, 含費稅)：71.7%勝/每筆+1.95%；更早的資料69.5%/+2.04%。不是未來勝率",
        "accent"),
    "mode_momentum_leader": (
        "警告：此模式照建議操作的實戰紀錄為負期望值（勝率 23%、59% 觸發停損），"
        "建議停用，僅供觀察",
        "red"),
}
MODE_RULE_DEFAULT = ("此模式尚無實戰驗證數據（ledger 累積中），交易計畫僅供參考", "dim")

# ── 詳細面板分區 ─────────────────────────────────────────────────────────────
DETAIL_SECTIONS = [
    ("每日法人買賣超（張）", [
        ("外資買賣超",    "Foreign_Net",     "{:+.0f}",  True),   # True = 正負上色
        ("投信買賣超",    "Trust_Net",       "{:+.0f}",  True),
        ("自營商買賣超",  "Dealer_Net",      "{:+.0f}",  True),
        ("三大法人合計",  "Inst_Net",        "{:+.0f}",  True),
        ("合計占均量%",   "Inst_Pct",        "{:+.1f}%", True),
        ("三大法人5日",   "Inst_Net_5D",     "{:+.0f}",  True),
        ("外資5日累計",   "Foreign_Net_5D",  "{:+.0f}",  True),
        ("投信5日累計",   "Trust_Net_5D",    "{:+.0f}",  True),
        ("連續買/賣超日", "Inst_Streak",     "{:+.0f}",  True),
        ("外資5日買超天", "Inst_Buy_Days",   "{:.0f}",   False),
        ("法人資料日",    "Inst_Date",       "{}",       False),
        ("隔日籌碼動作",  "Chip_Action",     "{}",       False),
    ]),
    ("集保籌碼（週更新）", [
        ("400張+持股%",   "Large_Holder_Pct",  "{:.2f}%",  False),
        ("大戶週增減",    "Large_Pct_Change",  "{:+.4f}",  True),   # True = 正負上色
        ("散戶持股%",     "Retail_Pct",        "{:.2f}%",  False),
        ("散戶週增減",    "Retail_Pct_Change", "{:+.4f}",  True),
    ]),
    ("訊號燈號", [
        ("箱縮",       "Cond_A",         "bool", False),
        ("吸籌",       "Cond_C",         "bool", False),
        ("大戶(B)",    "Cond_B",         "bool", False),
        ("MA多頭",     "MA_Bull_Align",  "bool", False),
        ("Donchian突破", "Donchian_Break", "bool", False),
        ("MACD金叉",   "MACD_Cross",     "bool", False),
    ]),
    ("距離（%）", [
        ("距支撐%",   "Sup_Gap_Pct",       "{:.1f}%", False),
        ("距壓力%",   "Res_Gap_Pct",       "{:.1f}%", False),
        ("距52週高%", "Dist_52W_High_Pct", "{:.1f}%", False),
        ("RS超額%",   "RS_Score",          "{:+.1f}%", True),
    ]),
    ("線型輔助指標", [
        ("MA糾結",        "MA_Squeeze",        "bool",     False),
        ("趨勢線突破",    "Trend_Breakout",    "bool",     False),
        ("MACD柱轉正",    "MACD_Hist_Turn",    "bool",     False),
        ("52週高位",      "Near_52W_High",     "bool",     False),
        ("RS強勢",        "RS_Strong",         "bool",     False),
        ("夾縫爆發",      "Squeeze",           "bool",     False),
    ]),
    ("均線", [
        ("5MA",   "MA5",  "{:.2f}", False),
        ("10MA",  "MA10", "{:.2f}", False),
        ("20MA",  "MA20", "{:.2f}", False),
        ("60MA",  "MA60", "{:.2f}", False),
    ]),
    ("壓力 / 支撐 / 量集中區", [
        ("關鍵支撐", "Support_Used", "{:.2f}", False),
        ("60日高(壓)", "Resist_60H",  "{:.2f}", False),
        ("20日低(近撐)", "Support_20L", "{:.2f}", False),
        ("60日低(底)", "Support_60L", "{:.2f}", False),
        ("整數關卡",   "Round_Level", "{:.2f}", False),
        ("Zone 1",    "VP_Zone1",    "{:.2f}", False),
        ("Zone 2",    "VP_Zone2",    "{:.2f}", False),
        ("Zone 3",    "VP_Zone3",    "{:.2f}", False),
    ]),
    ("缺口", [
        ("跳空支撐", "Gap_Up_Sup", "{:.2f}", False),
        ("跳空壓力", "Gap_Dn_Res", "{:.2f}", False),
    ]),
    ("噴發要素 / 原始技術指標", [
        ("起漲分",     "Launch_Score",    "{:.1f}",  False),
        ("噴發分",     "Surge_Score",     "{:.1f}",  False),
        ("近5日漲幅%", "Ret_5D_Pct",      "{:+.1f}%", True),   # 低=尚未發動(早)
        ("波動度ATR%", "ATR_Pct",         "{:.2f}%", False),
        ("蓄勢分",     "Explosion_Score", "{:.1f}",  False),
        ("箱型壓縮度", "Range_Tightness", "{:.4f}", False),
        ("量能萎縮比", "Volume_Dryup",    "{:.4f}", False),
        ("吸籌偏多度", "Volume_Bias",     "{:.4f}", False),
    ]),
]


def _num(val):
    """float(val) or None. Tolerates None / NaN / '' / non-numeric text, so
    display code never has to guard every single .get(). NaN counts as missing:
    a DataFrame column with one blank turns the whole column into floats, and
    "nan" must never reach the screen."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _text(val) -> str:
    """Display string for an identity/label field; None and NaN become ''."""
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    return str(val)


def _fmt_bool(val) -> str:
    return BOOL_TRUE if val else BOOL_FALSE


def _fmt_gap(val, is_resist=False) -> str:
    """Format Sup_Gap_Pct / Res_Gap_Pct.
    Negative Res_Gap_Pct means price already above prior resistance (breakout).
    """
    f = _num(val)
    if f is None:
        return "-"
    if is_resist and f <= 0:
        return "已突破"
    return "{:.1f}%".format(f)


def _fmt_rs(val) -> str:
    """RS 超額報酬帶正負號顯示。"""
    f = _num(val)
    return "-" if f is None else "{:+.1f}%".format(f)


def _fmt_gain(val) -> str:
    """近三月漲幅帶正負號顯示。"""
    f = _num(val)
    return "-" if f is None else "{:+.1f}%".format(f)


def _fmt_net(val) -> str:
    """法人買賣超（張）帶正負號千分位顯示。"""
    f = _num(val)
    return "-" if f is None else "{:+,.0f}".format(f)


def _fmt_price(val) -> str:
    """價格兩位小數；缺值顯示 '-'。"""
    f = _num(val)
    return "-" if f is None else "{:.2f}".format(f)


def _fmt_score(val) -> str:
    """0-100 規則分數，一位小數；缺值顯示 '-'。"""
    f = _num(val)
    return "-" if f is None else "{:.1f}".format(f)


def _truthy(val) -> bool:
    """Tolerant bool for shipped flag columns (numpy bools, 0/1, NaN, text).

    A missing or unreadable flag is FALSE, never True: the same fail-closed
    reading scanner.scan_mode._safe_bool applies to Core_Plus.
    """
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip().lower() not in ("", "0", "false", "nan", "none")
    f = _num(val)
    return False if f is None else f != 0


# ── 交易日曆（持倉天數即時重算用）────────────────────────────────────────────
# Hold_Day / Hold_Status ship FROZEN at scan time. A row scanned last night as
# "pending" is already a position the moment its entry bar opens, so rendering
# the shipped values verbatim shows yesterday's answer every morning. Mobile
# solved this with a view-time recompute (mobile/app.js holdCalc); the desktop
# does the same here off the SAME source of truth -- price_volume.db's trading
# calendar, via scanner.holding_tracker._trading_calendar().
_CAL_CACHE = []


def _calendar(refresh=False):
    """Trading dates ascending, extended forward with plain weekdays.

    price_volume.db only knows dates that already have bars, so on the morning
    of a new trading day its newest date is yesterday's and a naive lookup would
    still under-count the hold by one day. The forward projection mirrors
    mobile/app.js buildCalendar(). Known limitation (report F14): a projected
    weekday that turns out to be a market holiday is counted as a bar until the
    db catches up, so a day number can read one high right after a closure.
    Never raises -- no calendar simply means "fall back to the shipped Hold_*".
    """
    global _CAL_CACHE
    if _CAL_CACHE and not refresh:
        return _CAL_CACHE
    try:
        from scanner.holding_tracker import _trading_calendar
        cal = list(_trading_calendar())
    except Exception:
        cal = []
    if cal:
        try:
            d = datetime.date.fromisoformat(cal[-1])
            for _ in range(45):
                d += datetime.timedelta(days=1)
                if d.weekday() < 5:
                    cal.append(d.isoformat())
        except Exception:
            pass
    _CAL_CACHE = cal
    return cal


def _still_riding(row):
    """Is this stock still above its own 5-bar mean?

    The stock-strength half of the time-exit extension. Mirrors
    scanner.holding_tracker._still_strong exactly, including the STRICT
    comparison -- the two must not drift, because one is what the desktop
    shows and the other is what the phone and the ledger use.
    """
    close, ma5 = _num(row.get("Close_Price")), _num(row.get("MA5"))
    if close is None or ma5 is None:
        return False
    return close > ma5


def _live_hold(row, disturbed=False):
    """Recompute the holding day/status as of NOW for one row.

    Mirrors scanner.holding_tracker.annotate_holding() and mobile/app.js
    holdCalc(). Returns None when Entry_Date is unknown to the calendar, and the
    caller then falls back to the shipped Hold_* snapshot.
    """
    try:
        # A trade the exit stack has already closed has no live hold to
        # recompute: the calendar stopped applying on the day it closed. The
        # backend ships Hold_Status "exited" for those rows, and recomputing
        # here would put "day 12, keep riding" back on a screen the phone
        # shows as closed -- the same two-screen disagreement
        # tests/test_ui_parity.py exists to prevent.
        if _text(row.get("Exit_Signal")):
            return None
        entry = _text(row.get("Entry_Date"))[:10]
        cal = _calendar()
        if not entry or not cal:
            return None
        try:
            i_entry = cal.index(entry)
        except ValueError:
            return None
        now = datetime.datetime.now()
        today = now.strftime("%Y-%m-%d")
        i_today = -1                    # last trading day <= today
        for i, d in enumerate(cal):
            if d <= today:
                i_today = i
            else:
                break
        if i_today < 0:
            return None

        total = int(_num(row.get("Hold_Total")) or 10)
        cap = int(_num(row.get("Hold_Cap")) or 20)
        i_exit = i_entry + total - 1
        i_cap = i_entry + cap - 1
        day = i_today - i_entry + 1     # trading days held incl. today
        remaining = i_exit - i_today    # to base exit; 0 = today, <0 = past
        # Entry is at the OPEN: before 09:00 on the entry day nothing is filled
        # yet, so the row is still a plan rather than a position.
        entry_is_today = (today == cal[i_entry])
        before_open = entry_is_today and now.hour < 9
        after_close = now.hour > 13 or (now.hour == 13 and now.minute >= 30)

        if day <= 0 or before_open:
            status = "pending"
        elif remaining > 0:
            status = "holding"
        elif i_today >= i_cap:
            status = "exit_today" if i_today == i_cap else "overdue"
        elif cap > total and (disturbed or _still_riding(row)):
            # The SAME condition as scanner.holding_tracker.annotate_holding:
            # either the stock is still above its own 5-bar mean, or the market
            # is in a pullback within an uptrend. Before 2026-09-21 this branch
            # knew only about the market, so a row the backend shipped as
            # "delay" (keep riding) was re-derived here as "exit today" -- the
            # desktop telling you to sell what the phone said to hold.
            status = "delay"
        else:
            status = "exit_today" if remaining == 0 else "overdue"

        return {
            "status": status,
            "day": max(day, 0),
            "remaining": remaining,
            "total": total,
            "cap": cap,
            "entry": cal[i_entry],
            "exit": cal[i_exit] if i_exit < len(cal) else "",
            "entry_is_today": entry_is_today,
            "after_close": after_close,
        }
    except Exception:
        return None


def _hold_banner(row, disturbed=False):
    """進場計畫／出場提醒文字 + 顏色。

    Recomputed live (see _live_hold); the shipped Hold_* snapshot is only the
    fallback when the entry date is not on the calendar. The pending wording
    follows the ACTUAL entry date instead of always saying "tomorrow", and is
    phrased as a plan: the backtest fills at the open, but an exchange order is
    not a promise of the opening price (report 5.1).
    """
    try:
        status = row.get("Hold_Status")
        if status is None or (isinstance(status, float) and math.isnan(status)) \
                or status == "":
            return "", FG

        live = _live_hold(row, disturbed)
        if live:
            status = live["status"]
            day, rem = live["day"], live["remaining"]
            total, cap = live["total"], live["cap"]
            exit_d, entry_d = live["exit"], live["entry"]
            entry_is_today, after_close = live["entry_is_today"], live["after_close"]
        else:
            status = str(status)
            day = int(_num(row.get("Hold_Day")) or 0)
            rem = int(_num(row.get("Hold_Remaining")) or 0)
            total = int(_num(row.get("Hold_Total")) or 10)
            cap = int(_num(row.get("Hold_Cap")) or 20)
            exit_d = _text(row.get("Exit_Date"))
            entry_d = _text(row.get("Entry_Date"))[:10]
            entry_is_today = after_close = False

        if status == "exited":
            # The exit stack already closed this trade; the calendar stopped
            # applying on that day (scanner/holding_tracker.py).
            sig = _text(row.get("Exit_Signal"))
            when = _text(row.get("Exit_Signal_Date"))[:10]
            px = _num(row.get("Exit_Signal_Price"))
            label = {"stop": "停損", "lock": "鎖利", "tp": "停利",
                     "late": "後段獲利了結", "time": "到期"}.get(sig, sig or "規則")
            return ("出場提醒：已依{}出場（{}{}）".format(
                label, when or "日期未知",
                "，{:.2f}".format(px) if px else ""), YELLOW)
        if status == "pending":
            if entry_is_today:
                when = "今日開盤"
            elif entry_d:
                when = "{} 開盤".format(entry_d)
            else:
                when = "下一個交易日開盤"
            return ("進場計畫：{}進場（實際依券商成交回報，不保證開盤價成交）".format(when),
                    ACCENT)
        if status == "delay":
            reason = ("個股收盤仍站上5日均價" if _still_riding(row)
                      else "大盤弱（跌破20MA）")
            return ("出場提醒：第 {} 天，{}，續抱觀察，最晚第 {} 天".format(
                day, reason, cap), ORANGE)
        if status == "exit_today":
            return ("出場提醒：★ {}今日收盤出場（第 {} 天）".format(
                "已到期，" if after_close else "", day), YELLOW)
        if status == "overdue":
            return "出場提醒：已持有第 {} 天，應已出場（{}）".format(
                day, exit_d or "已過期"), RED
        exit_s = "，出場日 {}".format(exit_d) if exit_d else ""
        return "出場提醒：持有第 {}/{} 天，還有 {} 個交易日{}".format(
            day, total, rem, exit_s), GREEN
    except Exception:
        return "", FG


def _levels(row):
    """Which exit levels actually govern this row, and what they are anchored to.

    A row that has been entered carries Entry_Open + the Fill_* columns from
    scanner.holding_tracker; its close-based Strict_Stop_Loss / Target_Price are
    recomputed from TODAY's close on every scan and therefore drift away from
    the position (the 2026-08-06 audit found 92% of already-entered rows showing
    a stop that did not belong to their own fill). A row that has NOT been
    entered has no fill yet, so the close-based reference is the correct thing
    to show there. Either way the UI must SAY which of the two it is showing --
    same rule as mobile/app.js stockHtml(), so the two ends agree.
    """
    fill = _num(row.get("Entry_Open"))
    if fill is not None:
        return {
            "anchored":   True,
            "basis":      "依進場價",
            "entry":      fill,
            "stop":       _num(row.get("Fill_Stop_Loss")),
            "target":     _num(row.get("Fill_Target_Price")),
            "trail_arm":  _num(row.get("Fill_Trail_Arm_Price")),
            "trail_lock": _num(row.get("Fill_Trail_Lock_Price")),
            "scale_out":  _num(row.get("Fill_Scale_Out_Price")),
        }
    return {
        "anchored":   False,
        "basis":      "依收盤價",
        "entry":      _num(row.get("Suggested_Buy_Price")),
        "stop":       _num(row.get("Strict_Stop_Loss")),
        "target":     _num(row.get("Target_Price")),
        "trail_arm":  _num(row.get("Trail_Arm_Price")),
        "trail_lock": _num(row.get("Trail_Lock_Price")),
        "scale_out":  _num(row.get("Scale_Out_Price")),
    }


def _buy_state(row, live_status=None, enter_ok=True):
    """(顯示文字, 是否可買) for the validated buy gate.

    The BACKEND is the authority: scanner.scan_mode.mark_buy_ready ships
    Buy_Ready / Buy_Block and the desktop may only NARROW that verdict, never
    widen it (report F07). Two things legitimately age between the scan and now:

      * Hold_Status -- a row shipped "pending" is already entered once its entry
        bar opens, so a live "held" downgrades an otherwise ready row;
      * the market gate -- the regime is re-read at render time, and an unknown
        or shut gate blocks (unknown is not a tailwind).

    A row with no Buy_Ready at all (manual single-stock lookup, or a
    mark_buy_ready failure) claims nothing rather than defaulting to buyable.
    """
    try:
        if "Buy_Ready" not in row:
            return "-", False
        ready = _truthy(row.get("Buy_Ready"))
        block = _text(row.get("Buy_Block")).strip()
        if ready and live_status and live_status != "pending":
            ready, block = False, "held"
        if ready and not enter_ok:
            ready, block = False, "regime"
        if ready:
            return "✓ 可買", True
        return "× " + (BLOCK_TEXT.get(block) or block or "不符買進規則"), False
    except Exception:
        return "-", False


def _score_tag(score) -> str:
    try:
        s = float(score)
        if s >= 70:
            return "high"
        if s >= 50:
            return "mid"
    except Exception:
        pass
    return "alt"


# ── 詳細資訊面板 ─────────────────────────────────────────────────────────────

class DetailDialog(tk.Toplevel):

    def __init__(self, parent, row: dict, ctx=None):
        super().__init__(parent)
        sid   = _text(row.get("Stock_ID"))
        sname = _text(row.get("Stock_Name"))
        self.title("詳細資訊  {}  {}".format(sid, sname))
        self.geometry("720x680")
        self.configure(bg=BG)
        self.resizable(True, True)
        # ctx carries the view state the row itself cannot know: which score the
        # current mode ranks on, and the live market gate / exit-delay flags.
        self._ctx = ctx or {}
        self._build(row)

    def _build(self, row):
        # Header
        hdr = tk.Frame(self, bg=BG, padx=18, pady=14)
        hdr.pack(fill=tk.X)

        score_key, score_label = self._ctx.get("score", MODE_SCORE_DEFAULT)
        score = _num(row.get(score_key))
        score = 0.0 if score is None else score
        rs    = row.get("RS_Score")
        dist  = row.get("Dist_52W_High_Pct")

        tk.Label(hdr,
                 text="{} {}  |  收盤 {}  |  {} {:.1f}".format(
                     _text(row.get("Stock_ID")), _text(row.get("Stock_Name")),
                     _fmt_price(row.get("Close_Price")), score_label, score),
                 bg=BG, fg=ACCENT, font=(FONT, 15, "bold")).pack(anchor="w")

        # Buy verdict, straight from the backend gate (scan_mode.mark_buy_ready).
        # The desktop used to render no trace of Buy_Ready/Buy_Block at all, so
        # every listed row read as "buyable" even on days the rule bought none.
        live = _live_hold(row, self._ctx.get("disturbed", False))
        live_status = live["status"] if live else row.get("Hold_Status")
        buy_text, buy_ok = _buy_state(row, live_status,
                                      self._ctx.get("enter_ok", True))
        if buy_text != "-":
            tk.Label(hdr, text="買進判定：{}".format(buy_text),
                     bg=BG, fg=GREEN if buy_ok else DIM,
                     font=(FONT, 12, "bold")).pack(anchor="w", pady=(4, 0))

        # Sub-info row
        sub = tk.Frame(hdr, bg=BG)
        sub.pack(anchor="w", pady=(4, 0))

        # float() on a raw cell here used to be able to take the whole dialog
        # down on a non-numeric / NaN value; _num keeps display code total.
        rs_f = _num(rs)
        rs_color = DIM if rs_f is None else (
            GREEN if rs_f > 0 else (RED if rs_f < 0 else DIM))
        for text, color in [
            ("RS超額: {}".format(_fmt_rs(rs)),        rs_color),
            ("   距52W高: {}".format(_fmt_gap(dist)), FG),
        ]:
            tk.Label(sub, text=text, bg=BG, fg=color,
                     font=(FONT, 11)).pack(side=tk.LEFT)

        # Holding-day / exit reminder. Anchored to the ledger streak (see
        # scanner.holding_tracker) and recomputed live so it does not read one
        # day stale the morning after a scan.
        hold_text, hold_color = _hold_banner(row, self._ctx.get("disturbed", False))
        if hold_text:
            tk.Label(hdr, text=hold_text, bg=BG, fg=hold_color,
                     font=(FONT, 12, "bold")).pack(anchor="w", pady=(6, 0))

        # Score bar
        bar_frame = tk.Frame(hdr, bg=BG)
        bar_frame.pack(anchor="w", pady=(6, 0))
        bar_len = max(int(score * 1.8), 2)
        bar_color = YELLOW if score >= 70 else (ORANGE if score >= 50 else DIM)
        tk.Label(bar_frame,
                 text="█" * (bar_len // 10) + "░" * (18 - bar_len // 10),
                 bg=BG, fg=bar_color, font=("Consolas", 13)).pack(side=tk.LEFT)
        tk.Label(bar_frame, text="  {:.1f}/100".format(score),
                 bg=BG, fg=FG, font=(FONT, 11)).pack(side=tk.LEFT)

        tk.Frame(self, bg=HEADER_BG, height=1).pack(fill=tk.X, padx=16)

        # Scrollable body
        canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = tk.Frame(canvas, bg=BG)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))

        self._build_levels(body, row)

        for section_title, fields in DETAIL_SECTIONS:
            sec_hdr = tk.Frame(body, bg=HEADER_BG, padx=12, pady=5)
            sec_hdr.pack(fill=tk.X, padx=12, pady=(12, 2))
            tk.Label(sec_hdr, text=section_title,
                     bg=HEADER_BG, fg=ACCENT,
                     font=(FONT, 11, "bold")).pack(anchor="w")

            grid = tk.Frame(body, bg=BG)
            grid.pack(fill=tk.X, padx=20, pady=4)

            for col_i, (label, key, fmt, signed) in enumerate(fields):
                val = row.get(key)
                if fmt == "bool":
                    disp  = BOOL_TRUE if val else BOOL_FALSE
                    color = GREEN if val else DIM
                elif val is None:
                    disp, color = "-", DIM
                else:
                    try:
                        num   = float(val)
                        disp  = fmt.format(num)
                        if signed:
                            color = GREEN if num > 0 else (RED if num < 0 else DIM)
                        else:
                            color = FG
                    except Exception:
                        disp, color = str(val), FG

                cell = tk.Frame(grid, bg=BG)
                cell.grid(row=0, column=col_i, padx=10, pady=2, sticky="w")
                tk.Label(cell, text=label + ":",
                         bg=BG, fg=DIM, font=(FONT, 10)).pack(anchor="w")
                tk.Label(cell, text=disp,
                         bg=BG, fg=color,
                         font=(FONT, 13, "bold")).pack(anchor="w")

    def _build_levels(self, body, row):
        """交易水位分區：已進場看成本價水位，未進場看收盤參考水位。

        This section is built by hand instead of living in DETAIL_SECTIONS
        because its FIELDS change with the row: an entered row must be shown the
        Fill_* levels that actually govern its position (and never a "buy here"
        price), a not-yet-entered row the close-based reference. The section
        title states the anchor so the two can never be confused.
        """
        try:
            lv = _levels(row)
            if lv["anchored"]:
                title = "交易水位（依進場開盤價，模擬進場，實際成交依券商回報）"
                fields = [
                    ("進場開盤價", _fmt_price(lv["entry"])),
                    ("停損價(依進場價)", _fmt_price(lv["stop"])),
                    ("停利目標(依進場價)", _fmt_price(lv["target"])),
                    ("鎖利啟動(收盤站上+2.5%，隔日生效)", _fmt_price(lv["trail_arm"])),
                    ("鎖利價(依進場價)", _fmt_price(lv["trail_lock"])),
                ]
                close = _num(row.get("Close_Price"))
                if close is not None and lv["entry"]:
                    fields.append(("訊號後損益",
                                   "{:+.1f}%".format((close / lv["entry"] - 1) * 100)))
            else:
                title = "交易水位（依當日收盤重算的參考價，尚未進場）"
                fields = [
                    ("進場參考價", _fmt_price(lv["entry"])),
                    ("停損參考價", _fmt_price(lv["stop"])),
                    ("停利參考目標", _fmt_price(lv["target"])),
                    ("鎖利啟動(收盤站上+2.5%，隔日生效)", _fmt_price(lv["trail_arm"])),
                    ("鎖利參考價", _fmt_price(lv["trail_lock"])),
                ]
            # The frozen first-day view, when the ledger has one for this name.
            # Kept as its OWN row of fields rather than merged into the levels
            # above, because it answers a different question: not "what should I
            # do now" but "what did this system actually tell me, and when".
            # Report 5.1 -- the two must never share a column.
            initial = _num(row.get("Initial_Buy_Price"))
            if initial is not None:
                close = _num(row.get("Close_Price"))
                fields.append(("首日建議價(固定)", _fmt_price(initial)))
                fields.append(("建議日期", _text(row.get("Recommended_On")) or "-"))
                if close is not None and initial:
                    fields.append(("距首日建議",
                                   "{:+.1f}%".format((close / initial - 1) * 100)))
        except Exception:
            return

        sec_hdr = tk.Frame(body, bg=HEADER_BG, padx=12, pady=5)
        sec_hdr.pack(fill=tk.X, padx=12, pady=(12, 2))
        tk.Label(sec_hdr, text=title, bg=HEADER_BG, fg=ACCENT,
                 font=(FONT, 11, "bold")).pack(anchor="w")

        grid = tk.Frame(body, bg=BG)
        grid.pack(fill=tk.X, padx=20, pady=4)
        for col_i, (label, disp) in enumerate(fields):
            cell = tk.Frame(grid, bg=BG)
            cell.grid(row=0, column=col_i, padx=10, pady=2, sticky="w")
            tk.Label(cell, text=label + ":",
                     bg=BG, fg=DIM, font=(FONT, 10)).pack(anchor="w")
            tk.Label(cell, text=disp, bg=BG,
                     fg=FG if disp != "-" else DIM,
                     font=(FONT, 13, "bold")).pack(anchor="w")


# ── 主程式 ────────────────────────────────────────────────────────────────────

class ScannerApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry(WINDOW_SIZE)
        self.configure(bg=BG)
        self.resizable(True, True)
        self._last_result    = None
        self._row_data: dict = {}
        self._hovered_item   = None
        self._hovered_tags   = ()
        self._sort_col       = None
        self._sort_reverse   = False
        self._scan_modes     = _load_scan_modes()
        # Mode the CURRENT result was scanned with -- the combobox can be moved
        # afterwards, and the score column / buy gate must follow the data.
        self._result_mode    = None
        self._regime: dict   = {}      # cached get_market_regime() snapshot
        self._degraded       = None    # feed-health notice from ScanWorker
        # style the combobox dropdown listbox
        self.option_add("*TCombobox*Listbox.background",       SURFACE)
        self.option_add("*TCombobox*Listbox.foreground",       FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
        self.option_add("*TCombobox*Listbox.font",             (FONT, 11))
        self._build_styles()
        self._build_ui()

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview",
                     background=BG, foreground=FG, fieldbackground=BG,
                     rowheight=36, font=(FONT, 13),
                     borderwidth=0, relief="flat")
        s.configure("Treeview.Heading",
                     background=HEADER_BG, foreground=FG,
                     font=(FONT, 13, "bold"), relief="flat", borderwidth=0)
        s.map("Treeview",
              background=[("selected", ROW_HOVER)],
              foreground=[("selected", FG)])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        # Combobox dark theme
        s.configure("Mode.TCombobox",
                     fieldbackground=SURFACE, background=SURFACE,
                     foreground=FG, selectbackground=ACCENT,
                     selectforeground="#FFFFFF", arrowcolor=FG,
                     relief="flat", borderwidth=1)
        s.map("Mode.TCombobox",
              fieldbackground=[("readonly", SURFACE)],
              foreground=[("readonly", FG)],
              selectbackground=[("readonly", ACCENT)],
              selectforeground=[("readonly", "#FFFFFF")])

    def _build_ui(self):
        # Header bar
        hdr = tk.Frame(self, bg=BG, pady=12)
        hdr.pack(fill=tk.X, padx=20)
        tk.Label(hdr, text="股市掃描器",
                 bg=BG, fg=FG, font=(FONT, 20, "bold")).pack(side=tk.LEFT)

        # Manual single-stock lookup
        manual = tk.Frame(hdr, bg=BG)
        manual.pack(side=tk.LEFT, padx=(24, 0))
        tk.Label(manual, text="個股查詢:", bg=BG, fg=DIM,
                 font=(FONT, 11)).pack(side=tk.LEFT, padx=(0, 6))
        self._manual_var = tk.StringVar()
        self._manual_entry = tk.Entry(
            manual, textvariable=self._manual_var, width=8,
            bg=SURFACE, fg=FG, insertbackground=FG, relief=tk.FLAT,
            font=(FONT, 13), justify="center")
        self._manual_entry.pack(side=tk.LEFT, ipady=3)
        self._manual_entry.bind("<Return>", lambda e: self._start_single_scan())
        self._manual_btn = tk.Button(
            manual, text="分析", bg=SURFACE, fg=ACCENT, font=(FONT, 12, "bold"),
            relief=tk.FLAT, cursor="hand2", padx=14, pady=4,
            activebackground=HEADER_BG, activeforeground=ACCENT,
            command=self._start_single_scan)
        self._manual_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._scan_btn = tk.Button(
            hdr, text="開始掃描",
            bg=ACCENT, fg="#FFFFFF", font=(FONT, 14, "bold"),
            relief=tk.FLAT, cursor="hand2", padx=16, pady=5,
            activebackground="#6AAEE8", activeforeground="#FFFFFF",
            command=self._start_scan)
        self._scan_btn.pack(side=tk.RIGHT)

        self._ai_btn = tk.Button(
            hdr, text="產生 AI 報告",
            bg=SURFACE, fg=ACCENT, font=(FONT, 14, "bold"),
            relief=tk.FLAT, cursor="hand2", padx=16, pady=5,
            activebackground=HEADER_BG, activeforeground=ACCENT,
            state=tk.DISABLED, command=self._generate_ai_report)
        self._ai_btn.pack(side=tk.RIGHT, padx=(0, 10))

        # Regime/trend report: export recent-2y explosion fingerprint for review
        self._regime_btn = tk.Button(
            hdr, text="趨勢報告",
            bg=SURFACE, fg=ACCENT, font=(FONT, 14, "bold"),
            relief=tk.FLAT, cursor="hand2", padx=16, pady=5,
            activebackground=HEADER_BG, activeforeground=ACCENT,
            command=self._generate_regime_report)
        self._regime_btn.pack(side=tk.RIGHT, padx=(0, 10))

        # Scan mode combobox (packed RIGHT, appears to the left of AI button)
        mode_frame = tk.Frame(hdr, bg=BG)
        mode_frame.pack(side=tk.RIGHT, padx=(0, 16))
        tk.Label(mode_frame, text="篩選策略:",
                 bg=BG, fg=DIM, font=(FONT, 11)).pack(side=tk.LEFT, padx=(0, 6))
        mode_labels = [m["label"] for m in self._scan_modes]
        self._mode_var = tk.StringVar(value=mode_labels[0] if mode_labels else "")
        self._mode_combo = ttk.Combobox(
            mode_frame, textvariable=self._mode_var,
            values=mode_labels, state="readonly",
            width=34, font=(FONT, 11), style="Mode.TCombobox")
        self._mode_combo.pack(side=tk.LEFT)
        self._mode_combo.bind("<<ComboboxSelected>>",
                              lambda e: self._update_rule_banner())

        # OTC-only display filter. Ledger evidence (docs/EVAL_PLAYBOOK.md):
        # the prelaunch alpha is concentrated in OTC names (the full adoption gate
# rejected broadening to TSE on every metric -- STRATEGY.md appendix C),
        # so the filter defaults ON. It only hides rows from view -- the scan,
        # the ledger and the AI report still cover everything.
        self._otc_var = tk.BooleanVar(value=True)
        self._otc_chk = tk.Checkbutton(
            mode_frame, text="只看OTC", variable=self._otc_var,
            command=self._render_result,
            bg=BG, fg=FG, selectcolor=SURFACE, activebackground=BG,
            activeforeground=FG, font=(FONT, 11))
        self._otc_chk.pack(side=tk.LEFT, padx=(10, 0))

        # Status + progress
        mid = tk.Frame(self, bg=BG, padx=20)
        mid.pack(fill=tk.X)
        self._status_var = tk.StringVar(value="請按「開始掃描」開始")
        tk.Label(mid, textvariable=self._status_var,
                 bg=BG, fg=DIM, font=(FONT, 11), anchor="w").pack(fill=tk.X)
        self._prog_var  = tk.IntVar(value=0)
        self._progressbar = ttk.Progressbar(mid, variable=self._prog_var, maximum=100)
        self._progressbar.pack(fill=tk.X, pady=(4, 8))

        # Data-fault banner (ScanWorker's feed-health guard). A degraded feed
        # changes what the list MEANS -- a missing exchange snapshot is not "no
        # candidates there" -- and held_ids / the ledger were left untouched.
        # Same wording as mobile/app.js renderDegraded().
        self._degraded_var = tk.StringVar(value="")
        self._degraded_lbl = tk.Label(self, textvariable=self._degraded_var,
                                       bg=BG, fg=RED, font=(FONT, 11, "bold"),
                                       anchor="w", padx=20)
        self._degraded_lbl.pack(fill=tk.X)

        # Market-regime banner (momentum edge is regime-dependent)
        self._regime_var = tk.StringVar(value="")
        self._regime_lbl = tk.Label(self, textvariable=self._regime_var,
                                     bg=BG, fg=DIM, font=(FONT, 11, "bold"),
                                     anchor="w", padx=20)
        self._regime_lbl.pack(fill=tk.X)
        self._update_regime_banner()

        # Per-mode rule card (entry / stop / exit as validated on the ledger)
        self._rule_var = tk.StringVar(value="")
        self._rule_lbl = tk.Label(self, textvariable=self._rule_var,
                                   bg=BG, fg=ACCENT, font=(FONT, 11, "bold"),
                                   anchor="w", padx=20)
        self._rule_lbl.pack(fill=tk.X)
        self._update_rule_banner()

        # Legend
        legend = tk.Frame(self, bg=BG, padx=20, pady=2)
        legend.pack(fill=tk.X)
        # Legend note: the row colour encodes ONLY the mode's ranking score, and
        # the trade status lives in its own text column -- one channel per idea.
        for text, color in [
            ("■ 主分數≥70", YELLOW), ("■ 主分數≥50", ORANGE),
            ("✓成立 / -未成立", GREEN),
            ("✓可買＝符合買進規則、×＝不可買原因", ACCENT),
            ("價格基準：依進場價＝已進場成本、依收盤價＝尚未進場的參考", DIM),
            ("雙擊查看詳情", DIM),
        ]:
            tk.Label(legend, text=text, bg=BG, fg=color,
                     font=(FONT, 10)).pack(side=tk.LEFT, padx=(0, 14))

        # Treeview
        tree_frame = tk.Frame(self, bg=BG, padx=20)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        col_ids = [c[0] for c in MAIN_COLUMNS]
        self._tree = ttk.Treeview(tree_frame, columns=col_ids,
                                   show="headings", selectmode="browse")
        for col_id, label, _w in MAIN_COLUMNS:
            self._tree.heading(
                col_id, text=self._column_label(col_id, label),
                command=lambda c=col_id: self._sort_by_column(c))
            self._tree.column(col_id, width=60, anchor=tk.CENTER,
                              minwidth=30, stretch=tk.NO)

        self._tree.tag_configure("high",  background=ROW_HIGH, foreground=YELLOW)
        self._tree.tag_configure("mid",   background=ROW_MID,  foreground=ORANGE)
        self._tree.tag_configure("alt",   background=ROW_ALT,  foreground=FG)
        self._tree.tag_configure("alt0",  background=BG,       foreground=FG)
        self._tree.tag_configure("hover", background=ROW_HOVER, foreground=FG)

        self._tree.bind("<Double-1>",  self._on_row_double_click)
        self._tree.bind("<Configure>", self._on_tree_resize)
        self._tree.bind("<Motion>",    self._on_tree_motion)
        self._tree.bind("<Leave>",     self._on_tree_leave)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.pack(fill=tk.BOTH, expand=True)

        # Footer
        ftr = tk.Frame(self, bg=BG, padx=20, pady=8)
        ftr.pack(fill=tk.X)
        self._count_var = tk.StringVar(value="")
        tk.Label(ftr, textvariable=self._count_var,
                 bg=BG, fg=GREEN, font=(FONT, 11, "bold"), anchor="w").pack(side=tk.LEFT)
        # How many rows the validated rule actually buys today. 0 with a healthy
        # feed is a NORMAL no-trade day; the list length alone never said that.
        self._buy_var = tk.StringVar(value="")
        self._buy_lbl = tk.Label(ftr, textvariable=self._buy_var,
                                  bg=BG, fg=DIM, font=(FONT, 11, "bold"),
                                  anchor="w")
        self._buy_lbl.pack(side=tk.LEFT, padx=(18, 0))

    # ── 欄寬自動比例 ──────────────────────────────────────────────────────────

    def _on_tree_resize(self, event):
        avail = event.width
        if avail <= 0:
            return
        for col_id, _label, weight in MAIN_COLUMNS:
            self._tree.column(col_id, width=max(30, int(avail * weight / _TOTAL_WEIGHT)))

    # ── Hover 效果 ────────────────────────────────────────────────────────────

    def _on_tree_motion(self, event):
        item = self._tree.identify_row(event.y)
        if item == self._hovered_item:
            return
        if self._hovered_item:
            try:
                self._tree.item(self._hovered_item, tags=self._hovered_tags)
            except tk.TclError:
                pass
        if item:
            self._hovered_tags = self._tree.item(item, "tags")
            self._tree.item(item, tags=("hover",))
        else:
            self._hovered_tags = ()
        self._hovered_item = item

    def _on_tree_leave(self, event):
        if self._hovered_item:
            try:
                self._tree.item(self._hovered_item, tags=self._hovered_tags)
            except tk.TclError:
                pass
            self._hovered_item = None
            self._hovered_tags = ()

    # ── 雙擊詳細面板 ─────────────────────────────────────────────────────────

    def _on_row_double_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item:
            return
        row_dict = self._row_data.get(item)
        if row_dict:
            DetailDialog(self, row_dict, {
                "score": MODE_SCORE.get(self._view_mode(), MODE_SCORE_DEFAULT),
                "enter_ok": self._enter_ok(),
                "disturbed": self._disturbed(),
            })

    # ── 點擊表頭排序 ───────────────────────────────────────────────────────────

    def _row_sort_key(self, item, col_id):
        """Sort by the ORIGINAL value (number/bool/str), not the display string.
        Returns (rank, number, text); blanks get rank 2 so they stay last."""
        val = self._row_data.get(item, {}).get(col_id)
        if val is None:
            return (2, 0.0, "")
        try:
            f = float(val)              # numbers, np types, bools, numeric codes
            if math.isnan(f):
                return (2, 0.0, "")
            return (0, f, "")
        except (TypeError, ValueError):
            return (1, 0.0, str(val))   # names and other text

    def _sort_by_column(self, col_id):
        items = list(self._tree.get_children(""))
        if not items:
            return
        # same column -> toggle direction; new column -> default high->low
        if self._sort_col == col_id:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col_id
            self._sort_reverse = True

        keyed = [(self._row_sort_key(it, col_id), it) for it in items]
        blanks   = [it for k, it in keyed if k[0] == 2]
        nonblank = [(k, it) for k, it in keyed if k[0] != 2]
        nonblank.sort(key=lambda x: x[0], reverse=self._sort_reverse)
        ordered = [it for _, it in nonblank] + blanks   # blanks always last

        for idx, it in enumerate(ordered):
            self._tree.move(it, "", idx)

        self._hovered_item = None
        self._restripe()
        self._update_heading_arrows()

    def _restripe(self):
        """Re-apply zebra striping after a reorder, preserving score colours."""
        for idx, item in enumerate(self._tree.get_children("")):
            tags = self._tree.item(item, "tags")
            cur = tags[0] if tags else "alt0"
            if cur in ("high", "mid"):
                continue
            self._tree.item(item, tags=("alt" if idx % 2 else "alt0",))

    def _update_heading_arrows(self):
        for col_id, label, _w in MAIN_COLUMNS:
            text = self._column_label(col_id, label)
            if col_id == self._sort_col:
                text += " ▼" if self._sort_reverse else " ▲"
            self._tree.heading(col_id, text=text)

    # ── 掃描 ──────────────────────────────────────────────────────────────────

    def _update_regime_banner(self, refresh=True):
        """大盤橫幅：文案與顏色都從買進規則所用的同一個欄位推導。

        Two bugs lived here. (1) The advice was keyed on risk_on (TAIEX > 60MA)
        and said "halve new positions", while scan_mode.mark_buy_ready blocks
        ALL new entries unless enter_ok (above BOTH 20MA and 60MA) -- the banner
        contradicted the badge underneath it. Both now read enter_ok.
        (2) market_regime seeds risk_on=True before it knows anything, so an
        unreadable TAIEX painted the banner the same green as a genuine
        tailwind. Unknown is not a tailwind: it now fails CLOSED.
        """
        if refresh:
            try:
                from scanner.market_regime import get_market_regime
                self._regime = get_market_regime() or {}
            except Exception:
                self._regime = {}
        r = self._regime
        try:
            text = str(r.get("text") or "大盤狀態：資料不足")
            # as_of_date / is_current are added by market_regime -- read them
            # defensively, an older build does not ship them.
            as_of = str(r.get("as_of_date") or "")
            if as_of:
                stale = "" if r.get("is_current", True) else "，已非最新"
                text += "（大盤資料 {}{}）".format(as_of, stale)

            ok = _truthy(r.get("ok"))
            enter_ok = ok and _truthy(r.get("enter_ok"))
            if not ok:
                advice, color = "資料不足，一律不開新倉（未知不等於順風）", ORANGE
            elif r.get("is_current") is False:
                # A stale TAIEX cache already vetoes enter_ok upstream, but the
                # trend wording would then blame a 20/60MA break for what is
                # really a data-freshness problem. Name the real reason.
                advice, color = "大盤資料非最新，一律不開新倉", ORANGE
            elif enter_ok:
                advice = ("可開新倉" if _truthy(r.get("strong"))
                          else "可開新倉、部位減量（20MA上緣 <2.2%）")
                color = GREEN
            elif _truthy(r.get("risk_on")):
                advice, color = "暫停開新倉（跌破20MA），持股依原出場規則", ORANGE
            else:
                advice, color = "暫停開新倉（跌破60MA），持股依原出場規則", RED

            self._regime_var.set(text + "　→ 部位建議：" + advice)
            self._regime_lbl.config(fg=color)
        except Exception:
            self._regime_var.set("")

    def _enter_ok(self) -> bool:
        """The one field the buy rule gates on (TAIEX above BOTH 20 and 60MA).
        Unknown blocks -- same fail-closed reading as mark_buy_ready()."""
        return _truthy(self._regime.get("ok")) and _truthy(self._regime.get("enter_ok"))

    def _disturbed(self) -> bool:
        """The MARKET half of the exit extension: a pullback within an uptrend (below 20MA, still above 60MA): the only
        case where the exit delay engages. Mirrors holding_tracker."""
        return (_truthy(self._regime.get("ok"))
                and not _truthy(self._regime.get("above20"))
                and _truthy(self._regime.get("risk_on")))

    def _view_mode(self):
        """Mode the displayed rows belong to (falls back to the combobox)."""
        return self._result_mode or self._resolve_selected_mode()

    def _column_label(self, col_id, label):
        """Heading text. The ranking-score column is renamed per mode so the
        number under it and the row colour always mean the same thing."""
        if col_id == "Rank_Score":
            return MODE_SCORE.get(self._view_mode(), MODE_SCORE_DEFAULT)[1]
        return label

    def _after_scan_refresh(self):
        """Re-read the regime + calendar, then redraw. The banner and the buy
        badges must come from ONE snapshot, or they can disagree on screen."""
        _calendar(refresh=True)
        self._update_regime_banner()
        if self._last_result is not None:
            self._render_result()

    def _update_rule_banner(self):
        """Show the validated entry/stop/exit card for the selected mode."""
        text, tone = MODE_RULE_CARDS.get(self._resolve_selected_mode(),
                                         MODE_RULE_DEFAULT)
        self._rule_var.set(text)
        self._rule_lbl.config(fg={"accent": ACCENT, "red": RED}.get(tone, DIM))

    def _resolve_selected_mode(self):
        """Map the combobox label back to its English mode key."""
        selected_label = self._mode_var.get()
        for m in self._scan_modes:
            if m["label"] == selected_label:
                return m["key"]
        return self._scan_modes[0]["key"] if self._scan_modes else "mode_squeeze"

    def _reset_for_run(self):
        """Shared UI reset before a scan or a manual lookup."""
        self._ai_btn.config(state=tk.DISABLED)
        self._regime_btn.config(state=tk.DISABLED)
        self._mode_combo.config(state=tk.DISABLED)
        self._last_result = None
        self._row_data.clear()
        self._hovered_item = None
        self._sort_col = None
        self._sort_reverse = False
        self._prog_var.set(0)
        self._count_var.set("")
        self._buy_var.set("")
        self._degraded = None
        self._degraded_var.set("")
        for row in self._tree.get_children():
            self._tree.delete(row)
        self._update_heading_arrows()

    def _start_scan(self):
        self._scan_btn.config(state=tk.DISABLED, text="掃描中…")
        self._manual_btn.config(state=tk.DISABLED)
        self._reset_for_run()
        self._result_mode = self._resolve_selected_mode()

        ScanWorker(
            on_progress=self._cb_progress,
            on_result=self._cb_result,
            on_error=self._cb_error,
            on_done=self._cb_done,
            on_notice=self._cb_notice,
            scan_mode=self._result_mode,
        ).start()

    def _start_single_scan(self):
        code = self._manual_var.get().strip()
        if not code.isdigit() or not (4 <= len(code) <= 6):
            self._status_var.set("請輸入有效股票代號（4-6 位數字）")
            return
        self._manual_btn.config(state=tk.DISABLED, text="分析中…")
        self._scan_btn.config(state=tk.DISABLED)
        self._reset_for_run()
        self._result_mode = self._resolve_selected_mode()

        SingleStockWorker(
            stock_id=code,
            on_progress=self._cb_progress,
            on_result=self._cb_result,
            on_error=self._cb_error,
            on_done=self._cb_done,
            scan_mode=self._result_mode,
        ).start()

    def _cb_progress(self, current, total, message):
        self.after(0, lambda: self._progressbar.configure(maximum=max(total, 1)))
        self.after(0, lambda: self._prog_var.set(current))
        self.after(0, lambda: self._status_var.set(message))

    def _cb_notice(self, degraded):
        """Feed-health notice from ScanWorker (None = healthy). It was only ever
        written to the exported CSV, so a partial market feed was invisible on
        the desktop and an empty-looking list read as 'nothing qualified'."""
        self._degraded = degraded
        self.after(0, self._render_degraded)

    def _render_degraded(self):
        try:
            if not self._degraded:
                self._degraded_var.set("")
                return
            self._degraded_var.set(
                "※ 資料源異常（{}）· 缺漏市場≠空手 · 持倉照常追蹤，狀態未被覆寫".format(
                    self._degraded))
        except Exception:
            self._degraded_var.set("")

    def _cb_result(self, df):
        self._last_result = df
        self.after(0, self._render_result)

    def _render_result(self):
        """(Re)draw the tree from _last_result, honouring the OTC filter.
        Called from the scan callback and from the OTC checkbox toggle, so it
        must fully reset view state (rows, sort arrows, hover) each time."""
        self._row_data.clear()
        self._hovered_item = None
        self._sort_col = None
        self._sort_reverse = False
        for row in self._tree.get_children():
            self._tree.delete(row)
        self._update_heading_arrows()

        df = self._last_result
        if df is None:            # toggled before any scan -- nothing to show
            self._buy_var.set("")
            return
        if df.empty:
            self._count_var.set("未找到符合條件的標的")
            self._update_buy_summary(0, 0, set(), self._enter_ok())
            self._ai_btn.config(state=tk.DISABLED)
            return
        self._ai_btn.config(state=tk.NORMAL)

        otc_only = bool(self._otc_var.get())
        score_key, _score_label = MODE_SCORE.get(self._view_mode(),
                                                 MODE_SCORE_DEFAULT)
        enter_ok = self._enter_ok()
        disturbed = self._disturbed()
        shown = 0
        ready_n = 0
        quality_n = 0
        gated_n = 0
        blocks = set()
        for rank, (_, row) in enumerate(df.iterrows()):
            # rank = the SHIPPED order (mark_buy_ready's top-20 gate is defined
            # on it), so it is taken before any view filter or user sort.
            data = row.to_dict()
            lv = _levels(data)
            live = _live_hold(data, disturbed)
            live_status = live["status"] if live else data.get("Hold_Status")
            buy_text, buy_ok = _buy_state(data, live_status, enter_ok)
            if "Buy_Ready" in data:
                gated_n += 1        # rows the buy rule actually judged
            if buy_ok:
                ready_n += 1
            if (rank < 20 and _truthy(data.get("Core_Plus"))
                    and str(data.get("Market", "")) == "OTC"):
                quality_n += 1
            blocks.add(_text(data.get("Buy_Block")))

            # hide only confirmed TSE rows; unknown/missing market stays
            # visible (manual single-stock lookups may not carry Market)
            if otc_only and str(row.get("Market", "")) == "TSE":
                continue
            # Row colour = the mode's RANKING score only (the column above shows
            # that same number). Trade status is deliberately kept out of the
            # colour channel and rendered as text in 買進 instead, so a strong
            # technical score can never be misread as "the rule buys this".
            base_tag = _score_tag(data.get(score_key))
            tag = ("alt" if shown % 2 else "alt0") if base_tag == "alt" else base_tag

            item = self._tree.insert("", tk.END, tags=(tag,), values=(
                _text(data.get("Stock_ID")),
                _text(data.get("Stock_Name")),
                _text(data.get("Market")) or "-",
                buy_text,
                _fmt_price(data.get("Close_Price")),
                lv["basis"],
                _fmt_price(data.get("Initial_Buy_Price")),
                _fmt_price(lv["entry"]),
                _fmt_price(lv["stop"]),
                _fmt_price(lv["target"]),
                _fmt_price(lv["scale_out"]),
                _fmt_score(data.get(score_key)),
                _fmt_gain(data.get("Gain_3M_Pct")),
                _fmt_net(data.get("Foreign_Net_5D")),
            ))
            # Synthetic columns have no DataFrame column to sort on, so the
            # sortable value is stashed under the column id (_row_sort_key reads
            # the row dict, not the rendered text). Buy_State / Price_Basis sort
            # as ranks: 可買 and 依進場價 come first when sorting high->low.
            data["Buy_State"] = 1 if buy_ok else 0
            data["Price_Basis"] = 1 if lv["anchored"] else 0
            data["Initial_Price"] = _num(data.get("Initial_Buy_Price"))
            data["Entry_Price"] = lv["entry"]
            data["Stop_Price"] = lv["stop"]
            data["Take_Profit_Price"] = lv["target"]
            data["Scale_Out_Price"] = lv["scale_out"]
            data["Rank_Score"] = _num(data.get(score_key))
            self._row_data[item] = data
            shown += 1

        self._update_buy_summary(ready_n, quality_n, blocks, enter_ok,
                                 judged=bool(gated_n))
        total = len(df)
        if otc_only and shown == 0:
            self._count_var.set(
                "掃描完成，共 {} 檔，但無 OTC 標的（取消「只看OTC」可顯示全部）".format(total))
        elif otc_only and shown < total:
            self._count_var.set(
                "掃描完成，顯示 {} 檔 OTC / 共 {} 檔（雙擊查看詳情；取消「只看OTC」顯示全部）".format(
                    shown, total))
        else:
            self._count_var.set(
                "掃描完成，共找到 {} 檔符合訊號的標的  （雙擊任一列查看詳細資訊）".format(total))

    def _update_buy_summary(self, ready_n, quality_n, blocks, enter_ok, judged=True):
        """買進規則計數：真正照規則買幾檔，以及 0 檔時的原因。

        Mirrors mobile/app.js renderTradeable(). The list length alone never
        answered "how many does the rule buy today" -- 0 buyable with a healthy
        feed is a NORMAL no-trade day, and it must not look like a failure or
        like a degraded feed. judged=False means no row carried a buy verdict at
        all (a manual single-stock lookup runs no buy rule), so no count is made.
        """
        try:
            if self._degraded:
                # A missing exchange snapshot carries no information, so any
                # "0 buyable" count would be a claim we cannot make.
                self._buy_var.set("")
                return
            if not judged:
                self._buy_var.set("")
                return
            if "no_rule" in blocks:
                self._buy_var.set("此模式無經驗證的買進規則，僅供觀察")
                self._buy_lbl.config(fg=DIM)
                return
            if ready_n > 0:
                self._buy_var.set(
                    "今日符合買進規則（順風 + OTC 核心+ 新訊號）：{} 檔".format(ready_n))
                self._buy_lbl.config(fg=GREEN)
            elif not enter_ok:
                # Name the blocker: the list is not empty, the gate is shut --
                # and an unreadable regime is a different sentence from a
                # readable one that says "headwind".
                if not _truthy(self._regime.get("ok")):
                    why = "大盤資料不足"
                elif self._regime.get("is_current") is False:
                    why = "大盤資料非最新"
                else:
                    why = "大盤未站上 20/60MA"
                self._buy_var.set(
                    "今日 0 檔可買 · {}，{} 檔過品質閘門但一律不開新倉".format(why, quality_n)
                    if quality_n else
                    "今日 0 檔可買 · {}，暫停開新倉".format(why))
                self._buy_lbl.config(fg=ORANGE)
            else:
                self._buy_var.set("今日 0 檔符合買進規則 · 正常空手日（資料源正常，非故障）")
                self._buy_lbl.config(fg=DIM)
        except Exception:
            self._buy_var.set("")

    def _cb_error(self, msg):
        self.after(0, lambda: self._status_var.set("ERROR: {}".format(msg)))
        self.after(0, lambda: self._count_var.set("掃描失敗，請檢查網路或 API Token"))

    def _cb_done(self):
        self.after(0, lambda: self._scan_btn.config(state=tk.NORMAL, text="開始掃描"))
        self.after(0, lambda: self._manual_btn.config(state=tk.NORMAL, text="分析"))
        self.after(0, lambda: self._regime_btn.config(state=tk.NORMAL, text="趨勢報告"))
        self.after(0, lambda: self._mode_combo.config(state="readonly"))
        self.after(0, lambda: self._prog_var.set(int(self._progressbar.cget("maximum"))))
        # Regime + calendar + redraw together: the banner, the buy badges and the
        # holding days must all describe the same moment.
        self.after(0, self._after_scan_refresh)

    # ── AI 報告 ───────────────────────────────────────────────────────────────

    def _generate_regime_report(self):
        self._regime_btn.config(state=tk.DISABLED, text="分析中…")
        self._status_var.set("正在更新研究資料並分析近兩年趨勢，請稍候…")
        threading.Thread(target=self._run_regime_report, daemon=True).start()

    def _run_regime_report(self):
        from scanner.regime_report import generate_regime_report
        try:
            def prog(msg):
                self.after(0, lambda: self._status_var.set(msg))
            path = generate_regime_report(refresh=True, progress=prog)
            self.after(0, lambda: self._status_var.set("趨勢報告已產生：{}".format(path)))
            try:
                os.startfile(path)   # open the report for review (Windows)
            except Exception:
                pass
        except Exception as e:
            msg = str(e)
            self.after(0, lambda: self._status_var.set("趨勢報告失敗：{}".format(msg)))
        finally:
            self.after(0, lambda: self._regime_btn.config(state=tk.NORMAL, text="趨勢報告"))

    def _generate_ai_report(self):
        if self._last_result is None or self._last_result.empty:
            return
        self._ai_btn.config(state=tk.DISABLED, text="AI 產生中…")
        self._status_var.set("正在呼叫 Gemini 產生報告，請稍候…")
        threading.Thread(target=self._run_ai_report,
                         args=(self._last_result,), daemon=True).start()

    def _run_ai_report(self, df):
        try:
            report = generate_report(df)
            self.after(0, lambda: self._show_report_window(report))
            self.after(0, lambda: self._status_var.set("AI 報告產生完成。"))
        except GeminiError as e:
            msg = str(e)
            self.after(0, lambda: self._status_var.set("AI 報告失敗: {}".format(msg)))
        except Exception as e:
            msg = str(e)
            self.after(0, lambda: self._status_var.set("AI 報告失敗: {}".format(msg)))
        finally:
            self.after(0, lambda: self._ai_btn.config(state=tk.NORMAL, text="產生 AI 報告"))

    def _show_report_window(self, report_text):
        win = tk.Toplevel(self)
        win.title("AI 籌碼分析報告")
        win.geometry("760x660")
        win.configure(bg=BG)
        tk.Label(win, text="Gemini 每日籌碼分析報告",
                 bg=BG, fg=ACCENT, font=(FONT, 16, "bold")).pack(
                     anchor="w", padx=16, pady=(14, 6))
        txt_frame = tk.Frame(win, bg=BG)
        txt_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))
        sb = ttk.Scrollbar(txt_frame, orient=tk.VERTICAL)
        text = tk.Text(txt_frame, wrap=tk.WORD,
                       bg=SURFACE, fg=FG, insertbackground=FG,
                       font=(FONT, 14), relief=tk.FLAT, padx=12, pady=12,
                       yscrollcommand=sb.set)
        sb.config(command=text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text.insert("1.0", report_text)
        text.config(state=tk.DISABLED)


def launch():
    app = ScannerApp()
    app.mainloop()
