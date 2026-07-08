"use strict";

// Decision cards mirror gui/app.py MODE_RULE_CARDS. Chinese lives here (frontend),
// not in the Python backend, per the project's ASCII-in-.py rule.
const MODE_CARDS = {
  mode_prelaunch: [
    "決策卡（214日全期回測校正）：只做 OTC｜大盤順風才開新倉（見下方 regime 燈）｜聚焦前 20 核心｜明日開盤市價進場｜-10% 災難停損｜抱 10 個交易日收盤出場。實測 OTC 勝率約 53%，本組合升到 56%、alpha +5.5pp；強月 60%+、弱月 40%，非穩定 70%",
    "accent",
  ],
  mode_momentum_leader: [
    "警告：此模式照建議操作的實戰紀錄為負期望值（勝率 23%、59% 觸發停損），建議停用，僅供觀察",
    "red",
  ],
};
const MODE_CARD_DEFAULT = ["此模式尚無實戰驗證數據（ledger 累積中），交易計畫僅供參考", "dim"];

const $ = (s) => document.querySelector(s);
let ALL_ROWS = [];

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

// Holding-day banner. Localizes the backend Hold_Status machine code so a user
// who does not open the app daily knows exactly which day of the 5-bar hold a
// pick is on and when to sell -- see scanner/holding_tracker.py.
function holdBanner(r) {
  const st = r.Hold_Status;
  if (!st) return "";
  const day = r.Hold_Day, rem = r.Hold_Remaining, exit = r.Exit_Date || "";
  const total = r.Hold_Total || 10;
  let txt, cls;
  if (st === "pending") {
    txt = "明日開盤進場";
    cls = "pending";
  } else if (st === "exit_today") {
    txt = `★ 今日收盤出場（第 ${total} 天）`;
    cls = "exit";
  } else if (st === "overdue") {
    txt = `已第 ${day} 天 · 應已出場${exit ? "（" + exit + "）" : ""}`;
    cls = "overdue";
  } else {
    txt = `持有第 ${day}/${total} 天 · 還有 ${rem} 個交易日${exit ? " · 出場 " + exit : ""}`;
    cls = "holding";
  }
  return `<div class="hold ${cls}">${txt}</div>`;
}

function detailHtml(r) {
  const drow = (k, v) => `<div class="drow"><span class="k">${k}</span><span>${v}</span></div>`;
  const inst =
    drow("外資買賣超", fmtSigned(r.Foreign_Net, 0)) +
    drow("投信買賣超", fmtSigned(r.Trust_Net, 0)) +
    drow("外資5日累計", fmtSigned(r.Foreign_Net_5D, 0)) +
    drow("外資5日買超天", fmt(r.Inst_Buy_Days, 0));
  const chip =
    drow("400張+持股%", fmt(r.Large_Holder_Pct, 2, "%")) +
    drow("大戶週增減", fmtSigned(r.Large_Pct_Change, 4)) +
    drow("散戶持股%", fmt(r.Retail_Pct, 2, "%")) +
    drow("散戶週增減", fmtSigned(r.Retail_Pct_Change, 4));
  const dist =
    drow("距支撐%", fmt(r.Sup_Gap_Pct, 1, "%")) +
    drow("距壓力%", fmt(r.Res_Gap_Pct, 1, "%")) +
    drow("距52週高%", fmt(r.Dist_52W_High_Pct, 1, "%")) +
    drow("RS超額%", fmtSigned(r.RS_Score, 1, "%"));
  const ma =
    drow("5MA", fmt(r.MA5, 2)) + drow("10MA", fmt(r.MA10, 2)) +
    drow("20MA", fmt(r.MA20, 2)) + drow("60MA", fmt(r.MA60, 2));
  const lights =
    light("箱縮", r.Cond_A) + light("吸籌", r.Cond_C) + light("大戶B", r.Cond_B) +
    light("MA多頭", r.MA_Bull_Align) + light("Donchian突破", r.Donchian_Break) +
    light("MACD金叉", r.MACD_Cross) + light("趨勢線突破", r.Trend_Breakout) +
    light("MACD柱轉正", r.MACD_Hist_Turn) + light("52週高位", r.Near_52W_High) +
    light("RS強勢", r.RS_Strong);
  return `
    <div class="detail">
      <div class="sec-title">每日法人買賣超（張）</div>${inst}
      <div class="sec-title">集保籌碼（週更新）</div>${chip}
      <div class="sec-title">距離（%）</div>${dist}
      <div class="sec-title">均線</div>${ma}
      <div class="sec-title">訊號燈號</div><div class="lights">${lights}</div>
    </div>`;
}

function stockHtml(r, rank) {
  const grid =
    cell("收盤價", { txt: fmt(r.Close_Price, 2) }) +
    cell("進場參考", { txt: fmt(r.Suggested_Buy_Price, 2), cls: "gold" }) +
    cell("停損價", { txt: fmt(r.Strict_Stop_Loss, 2) }) +
    cell("風險%", { txt: fmt(r.Risk_Pct, 1, "%"), cls: signClass(-(num(r.Risk_Pct) || 0)) }) +
    cell("起漲分", { txt: fmt(r.Launch_Score, 1) }) +
    cell("蓄勢分", { txt: fmt(r.Explosion_Score, 1) }) +
    cell("3月漲幅", { txt: fmtSigned(r.Gain_3M_Pct, 1, "%"), cls: signClass(r.Gain_3M_Pct) }) +
    cell("外資5日", { txt: fmtSigned(r.Foreign_Net_5D, 0), cls: signClass(r.Foreign_Net_5D) });
  return `
    <div class="stock ${tierClass(r)}" data-id="${r.Stock_ID}">
      <div class="stock-head">
        <span class="rank">${rank}</span>
        <span class="name">${r.Stock_Name || r.Stock_ID}</span>
        <span class="code">${r.Stock_ID}</span>
        ${rank <= 20 ? '<span class="core">核心</span>' : ""}
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
  list.innerHTML = rows.map((r, i) => stockHtml(r, i + 1)).join("");
  list.querySelectorAll(".stock").forEach((el) => {
    el.addEventListener("click", () => el.classList.toggle("open"));
  });
}

function applySearch() {
  const q = $("#search").value.trim().toLowerCase();
  if (!q) return render(ALL_ROWS);
  render(ALL_ROWS.filter((r) =>
    String(r.Stock_ID).includes(q) ||
    String(r.Stock_Name || "").toLowerCase().includes(q)));
}

// Market-regime entry gate. prelaunch only opens NEW positions when the TAIEX
// is above both its 20 and 60 day averages (meta.regime.enter_ok).
function renderRegime(reg) {
  const el = $("#regime");
  if (!reg || !reg.ok) { el.className = "regime"; el.textContent = ""; return; }
  if (reg.enter_ok) {
    el.textContent = "大盤順風 · 可開新倉";
    el.className = "regime on";
  } else if (reg.risk_on) {
    el.textContent = "大盤中性（跌破 20MA）· 新倉保守";
    el.className = "regime mid";
  } else {
    el.textContent = "大盤逆風（跌破 60MA）· 暫緩開新倉、減碼";
    el.className = "regime off";
  }
}

async function load() {
  $("#status").textContent = "載入中…";
  try {
    const res = await fetch("./scan_result.json?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    ALL_ROWS = data.rows || [];
    const m = data.meta || {};
    const dataDate = ALL_ROWS.length ? (ALL_ROWS[0].Data_Date || "") : "";
    $("#meta").textContent =
      `${m.mode || ""}｜掃描 ${m.scan_time || ""}｜資料 ${dataDate}｜${m.count || ALL_ROWS.length} 檔`;
    const [txt, cls] = MODE_CARDS[m.mode] || MODE_CARD_DEFAULT;
    const card = $("#card");
    card.textContent = txt;
    card.className = "decision-card show " + cls;
    renderRegime(m.regime);
    applySearch();
    $("#status").textContent = "更新於 " + new Date().toLocaleTimeString("zh-TW");
  } catch (e) {
    $("#status").textContent = "載入失敗：" + e.message;
    if (!ALL_ROWS.length) $("#list").innerHTML = `<div class="empty">無法載入 scan_result.json</div>`;
  }
}

$("#refresh").addEventListener("click", load);
$("#search").addEventListener("input", applySearch);
load();

// Service worker (offline shell) only registers in a secure context
// (https or localhost). Over plain-http LAN it is silently skipped.
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}
