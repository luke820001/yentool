"use strict";

// Decision cards mirror gui/app.py MODE_RULE_CARDS. Chinese lives here (frontend),
// not in the Python backend, per the project's ASCII-in-.py rule.
const MODE_CARDS = {
  mode_prelaunch: [
    "OTC · 順風才進場 · 前20核心 · 隔日開盤進 · -10%停損 · 抱10天(大盤弱可延至20天) · 回測勝率~56%",
    "accent",
  ],
  mode_momentum_leader: [
    "警告：此模式照建議操作的實戰紀錄為負期望值（勝率 23%、59% 觸發停損），建議停用，僅供觀察",
    "red",
  ],
};
const MODE_CARD_DEFAULT = ["此模式尚無實戰驗證數據（ledger 累積中），交易計畫僅供參考", "dim"];

// Sort options (label -> {key, dir}). "rank" keeps the shipped Launch_Score order.
const SORTS = [
  ["排序：起漲分/名次", "rank", "asc"],
  ["起漲分 高→低", "Launch_Score", "desc"],
  ["蓄勢分 高→低", "Explosion_Score", "desc"],
  ["3月漲幅 高→低", "Gain_3M_Pct", "desc"],
  ["風險% 低→高", "Risk_Pct", "asc"],
  ["外資5日 高→低", "Foreign_Net_5D", "desc"],
  ["距52週高 近→遠", "Dist_52W_High_Pct", "asc"],
];

const $ = (s) => document.querySelector(s);
let ALL_ROWS = [];       // every pick, each tagged with _rank (shipped order)
let REPORTS = {};        // {ALL,OTC,TSE} pre-generated AI reports
let MARKET = "ALL";      // current market filter
let SORT_I = 0;          // index into SORTS

function num(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function fmt(v, digits, suffix) {
  const n = num(v);
  if (n === null) return "-";
  return n.toFixed(digits === undefined ? 2 : digits) + (suffix || "");
}
function fmtSigned(v, digits, suffix) {
  const n = num(v);
  if (n === null) return "-";
  const s = (n >= 0 ? "+" : "") + n.toFixed(digits === undefined ? 0 : digits);
  return s + (suffix || "");
}
function signClass(v) {
  const n = num(v);
  if (n === null) return "";
  return n > 0 ? "pos" : n < 0 ? "neg" : "";
}
function tierClass(row) {
  const e = num(row.Explosion_Score) || 0;
  if (e >= 70) return "tier-high";
  if (e >= 50) return "tier-mid";
  return "";
}

function cell(lbl, valHtml) {
  return `<div class="cell"><span class="lbl">${lbl}</span><span class="val ${valHtml.cls || ""}">${valHtml.txt}</span></div>`;
}
function light(label, on) {
  return `<span class="light ${on ? "on" : ""}">${label}</span>`;
}

// Holding-day banner (see scanner/holding_tracker.py).
function holdBanner(r) {
  const st = r.Hold_Status;
  if (!st) return "";
  const day = r.Hold_Day, rem = r.Hold_Remaining, exit = r.Exit_Date || "";
  const total = r.Hold_Total || 10, cap = r.Hold_Cap || 20;
  let txt, cls;
  if (st === "pending") { txt = "明日開盤進場"; cls = "pending"; }
  else if (st === "delay") { txt = `⏸ 第 ${day} 天 · 大盤弱(20MA下)續抱觀察 · 最晚第 ${cap} 天`; cls = "delay"; }
  else if (st === "exit_today") { txt = `★ 今日收盤出場（第 ${day} 天）`; cls = "exit"; }
  else if (st === "overdue") { txt = `已第 ${day} 天 · 應已出場${exit ? "（" + exit + "）" : ""}`; cls = "overdue"; }
  else { txt = `持有第 ${day}/${total} 天 · 還有 ${rem} 個交易日${exit ? " · 出場 " + exit : ""}`; cls = "holding"; }
  return `<div class="hold ${cls}">${txt}</div>`;
}

// Full detail parity with the desktop DetailDialog (gui/app.py DETAIL_SECTIONS).
function detailHtml(r) {
  const row = (k, v) => `<div class="drow"><span class="k">${k}</span><span>${v}</span></div>`;
  const grp = (title, body) => `<div class="sec-title">${title}</div>${body}`;
  const lgrp = (title, chips) => `<div class="sec-title">${title}</div><div class="lights">${chips}</div>`;

  const inst =
    row("外資買賣超", fmtSigned(r.Foreign_Net, 0)) +
    row("投信買賣超", fmtSigned(r.Trust_Net, 0)) +
    row("外資5日累計", fmtSigned(r.Foreign_Net_5D, 0)) +
    row("外資5日買超天", fmt(r.Inst_Buy_Days, 0));
  const chip =
    row("400張+持股%", fmt(r.Large_Holder_Pct, 2, "%")) +
    row("大戶週增減", fmtSigned(r.Large_Pct_Change, 4)) +
    row("散戶持股%", fmt(r.Retail_Pct, 2, "%")) +
    row("散戶週增減", fmtSigned(r.Retail_Pct_Change, 4));
  const signals =
    light("箱縮", r.Cond_A) + light("吸籌", r.Cond_C) + light("大戶B", r.Cond_B) +
    light("MA多頭", r.MA_Bull_Align) + light("Donchian突破", r.Donchian_Break) +
    light("MACD金叉", r.MACD_Cross);
  const dist =
    row("距支撐%", fmt(r.Sup_Gap_Pct, 1, "%")) +
    row("距壓力%", fmt(r.Res_Gap_Pct, 1, "%")) +
    row("距52週高%", fmt(r.Dist_52W_High_Pct, 1, "%")) +
    row("RS超額%", fmtSigned(r.RS_Score, 1, "%"));
  const aux =
    light("MA糾結", r.MA_Squeeze) + light("趨勢線突破", r.Trend_Breakout) +
    light("MACD柱轉正", r.MACD_Hist_Turn) + light("52週高位", r.Near_52W_High) +
    light("RS強勢", r.RS_Strong) + light("夾縫爆發", r.Squeeze);
  const ma =
    row("5MA", fmt(r.MA5, 2)) + row("10MA", fmt(r.MA10, 2)) +
    row("20MA", fmt(r.MA20, 2)) + row("60MA", fmt(r.MA60, 2));
  const sr =
    row("關鍵支撐", fmt(r.Support_Used, 2)) +
    row("60日高(壓)", fmt(r.Resist_60H, 2)) +
    row("20日低(近撐)", fmt(r.Support_20L, 2)) +
    row("60日低(底)", fmt(r.Support_60L, 2)) +
    row("整數關卡", fmt(r.Round_Level, 2)) +
    row("Zone 1", fmt(r.VP_Zone1, 2)) +
    row("Zone 2", fmt(r.VP_Zone2, 2)) +
    row("Zone 3", fmt(r.VP_Zone3, 2));
  const gap =
    row("跳空支撐", fmt(r.Gap_Up_Sup, 2)) +
    row("跳空壓力", fmt(r.Gap_Dn_Res, 2));
  const tech =
    row("起漲分", fmt(r.Launch_Score, 1)) +
    row("噴發分", fmt(r.Surge_Score, 1)) +
    row("近5日漲幅%", fmtSigned(r.Ret_5D_Pct, 1, "%")) +
    row("波動度ATR%", fmt(r.ATR_Pct, 2, "%")) +
    row("蓄勢分", fmt(r.Explosion_Score, 1)) +
    row("箱型壓縮度", fmt(r.Range_Tightness, 4)) +
    row("量能萎縮比", fmt(r.Volume_Dryup, 4)) +
    row("吸籌偏多度", fmt(r.Volume_Bias, 4));

  return `<div class="detail">
    ${grp("每日法人買賣超（張）", inst)}
    ${grp("集保籌碼（週更新）", chip)}
    ${lgrp("訊號燈號", signals)}
    ${grp("距離（%）", dist)}
    ${lgrp("線型輔助指標", aux)}
    ${grp("均線", ma)}
    ${grp("壓力 / 支撐 / 量集中區", sr)}
    ${grp("缺口", gap)}
    ${grp("噴發要素 / 原始技術指標", tech)}
  </div>`;
}

function stockHtml(r) {
  const grid =
    cell("收盤價", { txt: fmt(r.Close_Price, 2) }) +
    cell("進場參考", { txt: fmt(r.Suggested_Buy_Price, 2), cls: "gold" }) +
    cell("停損價", { txt: fmt(r.Strict_Stop_Loss, 2) }) +
    cell("風險%", { txt: fmt(r.Risk_Pct, 1, "%") }) +
    cell("起漲分", { txt: fmt(r.Launch_Score, 1) }) +
    cell("蓄勢分", { txt: fmt(r.Explosion_Score, 1) }) +
    cell("3月漲幅", { txt: fmtSigned(r.Gain_3M_Pct, 1, "%"), cls: signClass(r.Gain_3M_Pct) }) +
    cell("外資5日", { txt: fmtSigned(r.Foreign_Net_5D, 0), cls: signClass(r.Foreign_Net_5D) });
  return `
    <div class="stock ${tierClass(r)}" data-id="${r.Stock_ID}">
      <div class="stock-head">
        <span class="rank">${r._rank}</span>
        <span class="name">${r.Stock_Name || r.Stock_ID}</span>
        <span class="code">${r.Stock_ID}</span>
        ${r._rank <= 20 ? '<span class="core">核心</span>' : ""}
        <span class="market">${r.Market || ""}</span>
      </div>
      ${holdBanner(r)}
      <div class="grid">${grid}</div>
      ${detailHtml(r)}
    </div>`;
}

function render(rows) {
  const list = $("#list");
  if (!rows.length) {
    list.innerHTML = `<div class="empty">沒有符合的股票</div>`;
    return;
  }
  list.innerHTML = rows.map(stockHtml).join("");
  list.querySelectorAll(".stock").forEach((el) => {
    el.addEventListener("click", () => el.classList.toggle("open"));
  });
}

// market filter -> search -> sort, then render. Rank/core stay tied to the
// original shipped order (r._rank), not the display position.
function apply() {
  let rows = ALL_ROWS;
  if (MARKET !== "ALL") rows = rows.filter((r) => String(r.Market) === MARKET);
  const q = $("#search").value.trim().toLowerCase();
  if (q) rows = rows.filter((r) =>
    String(r.Stock_ID).includes(q) ||
    String(r.Stock_Name || "").toLowerCase().includes(q));
  const [, key, dir] = SORTS[SORT_I];
  rows = rows.slice().sort((a, b) => {
    if (key === "rank") return a._rank - b._rank;
    const av = num(a[key]), bv = num(b[key]);
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return dir === "asc" ? av - bv : bv - av;
  });
  $("#count").textContent = rows.length + " 檔";
  render(rows);
}

function renderRegime(reg) {
  const el = $("#regime");
  if (!reg || !reg.ok) { el.className = "regime"; el.textContent = ""; return; }
  if (reg.enter_ok) { el.textContent = "大盤順風 · 可開新倉"; el.className = "regime on"; }
  else if (reg.risk_on) { el.textContent = "大盤中性（跌破 20MA）· 新倉保守"; el.className = "regime mid"; }
  else { el.textContent = "大盤逆風（跌破 60MA）· 暫緩開新倉、減碼"; el.className = "regime off"; }
}

// AI report for the CURRENT market filter (OTC filter shows the OTC report),
// matching the desktop "summarize what is shown" behaviour.
function renderReport() {
  const panel = $("#aiPanel");
  const txt = REPORTS[MARKET] || REPORTS.ALL || "";
  if (!txt) { panel.textContent = "（本次掃描沒有 AI 報告；於雲端設定 API 金鑰後即會出現）"; return; }
  panel.textContent = (MARKET === "ALL" ? "" : "【" + MARKET + " 專屬】\n") + txt;
}

function buildControls() {
  // Market chips
  const mk = $("#market");
  [["ALL", "全部"], ["OTC", "OTC"], ["TSE", "TSE"]].forEach(([k, label]) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = "chip" + (MARKET === k ? " on" : "");
    b.onclick = () => {
      MARKET = k;
      mk.querySelectorAll(".chip").forEach((c) => c.classList.remove("on"));
      b.classList.add("on");
      renderReport();
      apply();
    };
    mk.appendChild(b);
  });
  // Sort select
  const sel = $("#sort");
  SORTS.forEach(([label], i) => {
    const o = document.createElement("option");
    o.value = i; o.textContent = label; sel.appendChild(o);
  });
  sel.onchange = () => { SORT_I = Number(sel.value); apply(); };
  // AI report toggle
  $("#aiBtn").onclick = () => $("#aiPanel").classList.toggle("show");
}

async function load() {
  $("#status").textContent = "載入中…";
  try {
    const res = await fetch("./scan_result.json?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    ALL_ROWS = (data.rows || []).map((r, i) => ({ ...r, _rank: i + 1 }));
    const m = data.meta || {};
    REPORTS = m.reports || {};
    const dataDate = ALL_ROWS.length ? (ALL_ROWS[0].Data_Date || "") : "";
    $("#meta").textContent =
      `${m.mode || ""}｜掃描 ${m.scan_time || ""}｜資料 ${dataDate}`;
    const [txt, cls] = MODE_CARDS[m.mode] || MODE_CARD_DEFAULT;
    const card = $("#card");
    card.textContent = txt;
    card.className = "decision-card show " + cls;
    renderRegime(m.regime);
    renderReport();
    apply();
    $("#status").textContent = "更新於 " + new Date().toLocaleTimeString("zh-TW");
  } catch (e) {
    $("#status").textContent = "載入失敗：" + e.message;
    if (!ALL_ROWS.length) $("#list").innerHTML = `<div class="empty">無法載入 scan_result.json</div>`;
  }
}

buildControls();
$("#refresh").addEventListener("click", load);
$("#search").addEventListener("input", apply);
load();

// Service worker only registers in a secure context (https or localhost).
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}
