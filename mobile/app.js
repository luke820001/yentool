"use strict";

// Decision cards mirror gui/app.py MODE_RULE_CARDS. Chinese lives here (frontend),
// not in the Python backend, per the project's ASCII-in-.py rule.
const MODE_CARDS = {
  mode_prelaunch: [
    "只買OTC核心+(貼近52週高、未起漲) · 順風才進場 · 隔日開盤進 · -15%災難停損 · 漲6%後鎖利+2% · 觸+20%停利 · 抱10天(大盤弱可延至20天) · 回測勝率~71%",
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

// --- Live holding recompute -------------------------------------------------
// The scan runs after close (17:00/18:00), so the shipped Hold_Day/Hold_Status
// are frozen at scan time and read one day stale the next morning. Here we
// recompute them at VIEW time: meta.calendar_tail gives the real trading dates,
// extended past its end by plain weekdays (holidays unknown until the next scan
// refreshes the tail -- self-correcting approximation).
let CAL = [];          // extended trading calendar, "YYYY-MM-DD" ascending
let KNOWN_LAST = "";   // last REAL (db-backed) trading date in CAL
let DISTURBED = false; // TAIEX below 20MA -> exit-delay engages

function dstr(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function buildCalendar(tail) {
  CAL = (tail || []).slice();
  KNOWN_LAST = CAL.length ? CAL[CAL.length - 1] : "";
  let d = CAL.length ? new Date(CAL[CAL.length - 1] + "T00:00:00") : new Date();
  for (let i = 0; i < 45; i++) {
    d = new Date(d.getTime() + 86400000);
    const dow = d.getDay();
    if (dow !== 0 && dow !== 6) CAL.push(dstr(d));
  }
}

// Recompute {st, day, rem, exit, total, cap} as of NOW. Mirrors
// scanner/holding_tracker.py annotate_holding(). null -> fall back to shipped.
function liveHold(r) {
  if (!r.Entry_Date || !CAL.length) return null;
  const iEntry = CAL.indexOf(String(r.Entry_Date).slice(0, 10));
  if (iEntry < 0) return null;
  const now = new Date();
  const today = dstr(now);
  let iToday = -1; // last trading day <= today (weekend/holiday -> previous bar)
  for (let i = 0; i < CAL.length; i++) { if (CAL[i] <= today) iToday = i; else break; }
  if (iToday < 0) return null;

  const total = r.Hold_Total || 10, cap = r.Hold_Cap || 20;
  const iExit = iEntry + total - 1, iCap = iEntry + cap - 1;
  const day = iToday - iEntry + 1;   // trading days held incl. today
  const rem = iExit - iToday;        // to base exit; 0 = today, <0 past
  // Entry is at the open; before 09:00 on entry day the position isn't on yet.
  const beforeOpen = today === CAL[iEntry] && now.getHours() < 9;
  const afterClose = now.getHours() > 13 || (now.getHours() === 13 && now.getMinutes() >= 30);

  let st;
  if (day <= 0 || beforeOpen) st = "pending";
  else if (rem > 0) st = "holding";
  else if (iToday >= iCap) st = iToday === iCap ? "exit_today" : "overdue";
  else if (DISTURBED && cap > total) st = "delay";
  else st = rem === 0 ? "exit_today" : "overdue";

  return {
    st, day: Math.max(day, 0), rem, total, cap,
    entry: CAL[iEntry],
    exit: iExit < CAL.length ? CAL[iExit] : "",
    entryIsToday: today === CAL[iEntry],
    afterClose,
  };
}

// Holding-day banner, recomputed live (see scanner/holding_tracker.py).
function holdBanner(r) {
  if (!r.Hold_Status) return "";
  const h = liveHold(r);
  // Fallback: shipped scan-time snapshot (calendar missing from older JSON).
  const st = h ? h.st : r.Hold_Status;
  const day = h ? h.day : r.Hold_Day;
  const rem = h ? h.rem : r.Hold_Remaining;
  const exit = h ? h.exit : (r.Exit_Date || "");
  const total = (h ? h.total : r.Hold_Total) || 10;
  const cap = (h ? h.cap : r.Hold_Cap) || 20;
  let txt, cls;
  if (st === "pending") {
    txt = h && h.entryIsToday ? "今日開盤進場（09:00）"
        : h && h.entry ? `${h.entry} 開盤進場` : "明日開盤進場";
    cls = "pending";
  }
  else if (st === "delay") { txt = `⏸ 第 ${day} 天 · 大盤弱(20MA下)續抱觀察 · 最晚第 ${cap} 天`; cls = "delay"; }
  else if (st === "exit_today") {
    txt = h && h.afterClose ? `★ 已到期，今日收盤出場（第 ${day} 天）` : `★ 今日收盤出場（第 ${day} 天）`;
    cls = "exit";
  }
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
    cell("停利目標", { txt: fmt(r.Target_Price, 2), cls: "gold" }) +
    cell("起漲分", { txt: fmt(r.Launch_Score, 1) }) +
    cell("鎖利價(漲6%後)", { txt: fmt(r.Trail_Lock_Price, 2), cls: "gold" }) +
    cell("3月漲幅", { txt: fmtSigned(r.Gain_3M_Pct, 1, "%"), cls: signClass(r.Gain_3M_Pct) }) +
    cell("外資5日", { txt: fmtSigned(r.Foreign_Net_5D, 0), cls: signClass(r.Foreign_Net_5D) });
  // 核心+ = the exact validated buy rule: overall top-20 AND OTC AND the
  // entry-quality gate. TSE rows never get the plus (the edge is OTC-only).
  const corePlus = r._rank <= 20 && r.Core_Plus && String(r.Market) === "OTC";
  return `
    <div class="stock ${tierClass(r)}" data-id="${r.Stock_ID}">
      <div class="stock-head">
        <span class="rank">${r._rank}</span>
        <span class="name">${r.Stock_Name || r.Stock_ID}</span>
        <span class="code">${r.Stock_ID}</span>
        ${corePlus ? '<span class="core plus">核心+</span>'
                   : r._rank <= 20 ? '<span class="core">核心</span>' : ""}
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

// --- Freshness watchdog -------------------------------------------------------
// "Opening the app" on iOS usually RESUMES a backgrounded PWA, so nothing
// reloads by itself: without this block the user stares at yesterday's scan
// until they hit the refresh button. Rules:
//   * every resume (visibilitychange -> visible) re-fetches the JSON and
//     re-renders, which also refreshes the live holding-day math to "now";
//   * the newest session we EXPECT data for = today once the 14:30 cloud scan
//     has had time to publish (~15:30), else the previous weekday
//     (holiday-naive: on a holiday the notice shows, wording covers it);
//   * while the shipped data is older than that, an amber notice shows and the
//     app silently re-polls every 5 minutes until fresh data lands.
const STALE_POLL_MS = 5 * 60 * 1000;
const PUBLISH_HM = 15 * 60 + 30;   // today's scan should be on Pages by 15:30
let STALE_TIMER = null;

function expectedDataDate() {
  const now = new Date();
  const d = new Date(now);
  if (now.getHours() * 60 + now.getMinutes() < PUBLISH_HM) d.setDate(d.getDate() - 1);
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  return dstr(d);
}

function updateStale(dataDate) {
  const el = $("#stale");
  const want = expectedDataDate();
  const stale = !!dataDate && dataDate.slice(0, 10) < want;
  if (STALE_TIMER) { clearTimeout(STALE_TIMER); STALE_TIMER = null; }
  if (stale) {
    el.textContent = `⏳ 等待 ${want} 掃描結果（目前為 ${dataDate}；假日則無新資料）· 每 5 分鐘自動重試`;
    el.className = "stale show";
    STALE_TIMER = setTimeout(load, STALE_POLL_MS);
  } else {
    el.className = "stale";
    el.textContent = "";
  }
}

// iOS fires different events depending on how the PWA comes back (app switch,
// bfcache restore, external-link return), so listen to all three; RESUME_GATE
// keeps a single resume from triggering multiple parallel loads.
let RESUME_GATE = 0;
function onResume() {
  const now = Date.now();
  if (document.hidden || now - RESUME_GATE < 2000) return;
  RESUME_GATE = now;
  load();
  // also check for a new app shell (deploys while the PWA slept); when one is
  // found the controllerchange handler below reloads the page automatically
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.getRegistration()
      .then((reg) => reg && reg.update())
      .catch(() => {});
  }
}
document.addEventListener("visibilitychange", onResume);
window.addEventListener("pageshow", onResume);
window.addEventListener("focus", onResume);

async function load() {
  $("#status").textContent = "載入中…";
  try {
    const res = await fetch("./scan_result.json?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    ALL_ROWS = (data.rows || []).map((r, i) => ({ ...r, _rank: i + 1 }));
    const m = data.meta || {};
    REPORTS = m.reports || {};
    buildCalendar(m.calendar_tail);
    DISTURBED = !!(m.regime && m.regime.ok) && m.regime.above20 === false;
    const dataDate = ALL_ROWS.length ? (ALL_ROWS[0].Data_Date || "") : "";
    // Trading days elapsed since the data date, so a morning open clearly says
    // "prices are last night's close" instead of silently looking current.
    let age = "";
    if (dataDate && CAL.length) {
      const iData = CAL.indexOf(String(dataDate).slice(0, 10));
      const today = dstr(new Date());
      let iToday = -1;
      for (let i = 0; i < CAL.length; i++) { if (CAL[i] <= today) iToday = i; else break; }
      if (iData >= 0 && iToday > iData) age = `（收盤價為 ${iToday - iData} 個交易日前）`;
    }
    $("#meta").textContent =
      `${m.mode || ""}｜掃描 ${m.scan_time || ""}｜資料 ${dataDate}${age}`;
    updateStale(dataDate);
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
  // The freshness watchdog reloads DATA, but a suspended PWA keeps running the
  // OLD app code forever. When a new service worker takes control (= a deploy
  // landed), reload once so the page swaps to the new shell by itself.
  let swReloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (swReloaded) return;
    swReloaded = true;
    window.location.reload();
  });
}
