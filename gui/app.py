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
# Concise main list: identity + the trade plan + the one ranking score + two
# pieces of at-a-glance context. Everything diagnostic (other score, ATR, RS,
# gaps, signal lights, MAs) lives in the double-click detail panel below.
MAIN_COLUMNS = [
    ("Stock_ID",             "代號",      3),
    ("Stock_Name",           "名稱",      6),
    ("Market",               "市場",      2.5),
    ("Close_Price",          "收盤價",    3.5),
    ("Suggested_Buy_Price",  "進場參考",  3.5),
    ("Strict_Stop_Loss",     "停損價",    3.5),
    ("Risk_Pct",             "風險%",     3),
    ("Launch_Score",         "起漲分",    3.5),
    ("Gain_3M_Pct",          "3月漲幅%",  3.5),
    ("Foreign_Net_5D",       "外資5日",   3.5),
]
_TOTAL_WEIGHT = sum(w for _, _, w in MAIN_COLUMNS)

# ── 各模式決策卡（依 signal ledger 實戰驗證，見 docs/EVAL_PLAYBOOK.md）────────
# mode_prelaunch 是目前唯一經實戰模擬驗證有正期望值的模式；其進場/出場規則
# 直接顯示在橫幅。momentum_leader 照舊建議操作的實戰紀錄為負，明確警告。
MODE_RULE_CARDS = {
    "mode_prelaunch": (
        "OTC · 順風才進場 · 前20核心 · 隔日開盤進 · -10%停損 · 抱10天(大盤弱可延至20天) · "
        "回測勝率約56%、alpha +5.5pp",
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
        ("外資5日累計",   "Foreign_Net_5D",  "{:+.0f}",  True),
        ("外資5日買超天", "Inst_Buy_Days",   "{:.0f}",   False),
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


def _fmt_bool(val) -> str:
    return BOOL_TRUE if val else BOOL_FALSE


def _fmt_gap(val, is_resist=False) -> str:
    """Format Sup_Gap_Pct / Res_Gap_Pct.
    Negative Res_Gap_Pct means price already above prior resistance (breakout).
    """
    if val is None:
        return "-"
    try:
        f = float(val)
        if is_resist and f <= 0:
            return "已突破"
        return "{:.1f}%".format(f)
    except Exception:
        return "-"


def _fmt_rs(val) -> str:
    """RS 超額報酬帶正負號顯示。"""
    if val is None:
        return "-"
    try:
        return "{:+.1f}%".format(float(val))
    except Exception:
        return "-"


def _fmt_gain(val) -> str:
    """近三月漲幅帶正負號顯示。"""
    if val is None:
        return "-"
    try:
        return "{:+.1f}%".format(float(val))
    except Exception:
        return "-"


def _fmt_net(val) -> str:
    """法人買賣超（張）帶正負號千分位顯示。"""
    if val is None:
        return "-"
    try:
        return "{:+,.0f}".format(float(val))
    except Exception:
        return "-"


def _hold_banner(row):
    """出場提醒文字 + 顏色，來源 scanner.holding_tracker 的 Hold_* 欄位。
    以 ledger 首次上榜日為錨，即使沒有每天掃描也算得準。"""
    status = row.get("Hold_Status")
    if status is None or (isinstance(status, float) and math.isnan(status)) or status == "":
        return "", FG
    try:
        day = int(row.get("Hold_Day") or 0)
        rem = int(row.get("Hold_Remaining") or 0)
        total = int(row.get("Hold_Total") or 10)
        cap = int(row.get("Hold_Cap") or 20)
    except Exception:
        day = rem = 0
        total = 10
        cap = 20
    exit_d = str(row.get("Exit_Date") or "")
    exit_s = "，出場日 {}".format(exit_d) if exit_d else ""
    if status == "pending":
        return "出場提醒：明日開盤市價進場（尚未進場）", ACCENT
    if status == "delay":
        return ("出場提醒：第 {} 天，大盤弱（20MA下）續抱觀察，最晚第 {} 天".format(
            day, cap), ORANGE)
    if status == "exit_today":
        return "出場提醒：★ 今日收盤出場（第 {} 天）".format(day), YELLOW
    if status == "overdue":
        return "出場提醒：已持有第 {} 天，應已出場（{}）".format(
            day, exit_d or "已過期"), RED
    return "出場提醒：持有第 {}/{} 天，還有 {} 個交易日{}".format(
        day, total, rem, exit_s), GREEN


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

    def __init__(self, parent, row: dict):
        super().__init__(parent)
        sid   = row.get("Stock_ID", "")
        sname = row.get("Stock_Name", "")
        self.title("詳細資訊  {}  {}".format(sid, sname))
        self.geometry("720x680")
        self.configure(bg=BG)
        self.resizable(True, True)
        self._build(row)

    def _build(self, row):
        # Header
        hdr = tk.Frame(self, bg=BG, padx=18, pady=14)
        hdr.pack(fill=tk.X)

        score = row.get("Surge_Score")
        score = score if score is not None else 0
        rs    = row.get("RS_Score")
        dist  = row.get("Dist_52W_High_Pct")

        tk.Label(hdr,
                 text="{} {}  |  收盤 {}  |  噴發分 {}".format(
                     row.get("Stock_ID", ""), row.get("Stock_Name", ""),
                     row.get("Close_Price", "-"), score),
                 bg=BG, fg=ACCENT, font=(FONT, 15, "bold")).pack(anchor="w")

        # Sub-info row
        sub = tk.Frame(hdr, bg=BG)
        sub.pack(anchor="w", pady=(4, 0))

        rs_color = GREEN if rs and float(rs) > 0 else (RED if rs and float(rs) < 0 else DIM)
        for text, color in [
            ("RS超額: {}".format(_fmt_rs(rs)),                         rs_color),
            ("   距52W高: {}%".format(dist if dist is not None else "-"), FG),
        ]:
            tk.Label(sub, text=text, bg=BG, fg=color,
                     font=(FONT, 11)).pack(side=tk.LEFT)

        # Holding-day / exit reminder (from scanner.holding_tracker). Anchored to
        # the ledger streak so it is correct even if the user does not scan daily.
        hold_text, hold_color = _hold_banner(row)
        if hold_text:
            tk.Label(hdr, text=hold_text, bg=BG, fg=hold_color,
                     font=(FONT, 12, "bold")).pack(anchor="w", pady=(6, 0))

        # Score bar
        bar_frame = tk.Frame(hdr, bg=BG)
        bar_frame.pack(anchor="w", pady=(6, 0))
        try:
            bar_len = max(int(float(score) * 1.8), 2)
        except Exception:
            bar_len = 2
        bar_color = YELLOW if float(score) >= 70 else (ORANGE if float(score) >= 50 else DIM)
        tk.Label(bar_frame,
                 text="█" * (bar_len // 10) + "░" * (18 - bar_len // 10),
                 bg=BG, fg=bar_color, font=("Consolas", 13)).pack(side=tk.LEFT)
        tk.Label(bar_frame, text="  {}/100".format(score),
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
        # the prelaunch alpha is concentrated in OTC names (win 71% vs 64%),
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
        for text, color in [
            ("■ 噴發≥70", YELLOW), ("■ 噴發≥50", ORANGE),
            ("✓ 成立", GREEN),     ("- 未成立", DIM),
            ("RS+ 跑贏大盤", GREEN), ("RS- 跑輸大盤", RED),
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
                col_id, text=label,
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
            DetailDialog(self, row_dict)

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
            if col_id == self._sort_col:
                arrow = " ▼" if self._sort_reverse else " ▲"
                self._tree.heading(col_id, text=label + arrow)
            else:
                self._tree.heading(col_id, text=label)

    # ── 掃描 ──────────────────────────────────────────────────────────────────

    def _update_regime_banner(self):
        try:
            from scanner.market_regime import get_market_regime
            r = get_market_regime()
            text = r["text"]
            # position advice, validated on the ledger: a hard regime gate cuts
            # the rebound cohorts too, so risk_off halves NEW positions only
            if r.get("ok"):
                text += "　→ 部位建議：" + ("正常" if r.get("risk_on") else "新倉減半")
            self._regime_var.set(text)
            self._regime_lbl.config(fg=GREEN if r.get("risk_on") else RED)
        except Exception:
            self._regime_var.set("")

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
        for row in self._tree.get_children():
            self._tree.delete(row)
        self._update_heading_arrows()

    def _start_scan(self):
        self._scan_btn.config(state=tk.DISABLED, text="掃描中…")
        self._manual_btn.config(state=tk.DISABLED)
        self._reset_for_run()

        ScanWorker(
            on_progress=self._cb_progress,
            on_result=self._cb_result,
            on_error=self._cb_error,
            on_done=self._cb_done,
            scan_mode=self._resolve_selected_mode(),
        ).start()

    def _start_single_scan(self):
        code = self._manual_var.get().strip()
        if not code.isdigit() or not (4 <= len(code) <= 6):
            self._status_var.set("請輸入有效股票代號（4-6 位數字）")
            return
        self._manual_btn.config(state=tk.DISABLED, text="分析中…")
        self._scan_btn.config(state=tk.DISABLED)
        self._reset_for_run()

        SingleStockWorker(
            stock_id=code,
            on_progress=self._cb_progress,
            on_result=self._cb_result,
            on_error=self._cb_error,
            on_done=self._cb_done,
            scan_mode=self._resolve_selected_mode(),
        ).start()

    def _cb_progress(self, current, total, message):
        self.after(0, lambda: self._progressbar.configure(maximum=max(total, 1)))
        self.after(0, lambda: self._prog_var.set(current))
        self.after(0, lambda: self._status_var.set(message))

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
            return
        if df.empty:
            self._count_var.set("未找到符合條件的標的")
            self._ai_btn.config(state=tk.DISABLED)
            return
        self._ai_btn.config(state=tk.NORMAL)

        otc_only = bool(self._otc_var.get())
        shown = 0
        for _, row in df.iterrows():
            # hide only confirmed TSE rows; unknown/missing market stays
            # visible (manual single-stock lookups may not carry Market)
            if otc_only and str(row.get("Market", "")) == "TSE":
                continue
            base_tag = _score_tag(row.get("Surge_Score", 0))
            tag = ("alt" if shown % 2 else "alt0") if base_tag == "alt" else base_tag

            item = self._tree.insert("", tk.END, tags=(tag,), values=(
                row.get("Stock_ID",            ""),
                row.get("Stock_Name",           ""),
                row.get("Market")               if row.get("Market") else "-",
                row.get("Close_Price",          ""),
                row.get("Suggested_Buy_Price")  if row.get("Suggested_Buy_Price") else "-",
                row.get("Strict_Stop_Loss")     if row.get("Strict_Stop_Loss")    else "-",
                "{:.1f}%".format(row.get("Risk_Pct"))
                    if row.get("Risk_Pct") is not None else "-",
                row.get("Launch_Score") if row.get("Launch_Score") is not None else "-",
                _fmt_gain(row.get("Gain_3M_Pct")),
                _fmt_net(row.get("Foreign_Net_5D")),
            ))
            self._row_data[item] = row.to_dict()
            shown += 1

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

    def _cb_error(self, msg):
        self.after(0, lambda: self._status_var.set("ERROR: {}".format(msg)))
        self.after(0, lambda: self._count_var.set("掃描失敗，請檢查網路或 API Token"))

    def _cb_done(self):
        self.after(0, lambda: self._scan_btn.config(state=tk.NORMAL, text="開始掃描"))
        self.after(0, lambda: self._manual_btn.config(state=tk.NORMAL, text="分析"))
        self.after(0, lambda: self._regime_btn.config(state=tk.NORMAL, text="趨勢報告"))
        self.after(0, lambda: self._mode_combo.config(state="readonly"))
        self.after(0, lambda: self._prog_var.set(int(self._progressbar.cget("maximum"))))
        self.after(0, self._update_regime_banner)

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
