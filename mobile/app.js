"use strict";
/* ============================================================================
 * YenTool mobile PWA.
 *
 * Five pages (report 7.1): overview / today's recommendations / my positions /
 * performance & history / research & data status.
 *
 * Three rules shape everything below and are worth stating once:
 *
 *  1. THE BACKEND DECIDES, THE CLIENT RENDERS. The buy rule lives in
 *     scanner/scan_mode.mark_buy_ready() and arrives as Buy_Ready/Buy_Block.
 *     This file may only DOWNGRADE that verdict (mark it stale), never upgrade
 *     a refusal into a buy (F07). Re-deriving the rule here is what let the
 *     phone and the desktop disagree about what was tradable.
 *
 *  2. A POSITION IS DERIVED FROM ITS EXECUTIONS. Nothing writes a fill by
 *     overwriting a field; a correction is a new execution revision and the
 *     whole position is rebuilt from history. Mirrors portfolio/ledger.py so
 *     both ends produce the same numbers to the cent.
 *
 *  3. PRICES FOR POSITIONS COME FROM quotes.json, NEVER FROM THE SCAN ROWS.
 *     A holding that drops off the shortlist has not stopped existing; only
 *     our willingness to price it had (F04).
 *
 * Money is integer cents end to end (see section 2). Traditional Chinese in
 * the UI strings, English in the comments.
 * ==========================================================================*/

/* ============================================================================
 * 0. Configuration
 * ==========================================================================*/

const DATA_URL = "./scan_result.json";
const QUOTES_URL = "./quotes.json";
// Compact data for every stock the scan examined. Fetched ONLY when a
// registered holding is missing from the list, so most sessions never pay for
// it (owner, 2026-09-21: advice on a stock the scanner never recommended).
const UNIVERSE_URL = "./universe.json";
const TZ = "Asia/Taipei";

// The strategy parameters the old UI hard-coded. They are the CURRENT strategy
// version's numbers, not proven optima (report 5.4 is explicit about that), so
// they live in one labelled place and every position stores the version that
// priced it.
const STRATEGY = {
  version: "prelaunch-2026-09-21",
  stopPct: -20,      // initial stop, from the FIRST fill (see basePrice below)
  // 2026-09-21: the lock ARMS on a CLOSE at or above this, and the raised stop
  // is the order you place for the NEXT session. It used to arm intraday at
  // +6% and the backtest let the same bar be stopped on it -- a protection
  // nobody could have placed. Re-measured the executable way, the old rule
  // scored 63.0% and this one 67.1% (recent 3 years).
  armPct: 2.5,
  lockPct: 2,        // stop moves here once armed; ratchets up only
  targetPct: 20,     // take-profit target
  addPct: -10,       // optional staged entry: buy the rest here (2026-09-20)
  addFirstPct: 50,   // how much of the planned size the first buy is
  scaleOutPct: 15,   // optional: sell half here (2026-09-21)
  // Late profit-taking (2026-09-21): from this trading day onward, a CLOSE at
  // or above the fill + lateGainPct sells at the NEXT open rather than
  // carrying the profit into the last day. The time exit was closing 28% of
  // trades at -6.9%; this lifts the win rate 69.1 -> 70.8% at no cost in mean.
// (2026-09-22: the 71.7% this comment used to quote was the +0% threshold,
//  which the backtest log records as REJECTED. The shipped +1% gives 70.8%.)
  lateFrom: 8,
  lateGainPct: 1,
  horizon: 10,       // base hold, trading days
  cap: 20,           // maximum hold when the exit is delayed (see rideNote)
};

// Strategy blurb. Report 7.4: this used to be a fixed header block eating half
// a phone screen, so it is now a one-line summary with an expandable body.
const MODE_CARDS = {
  mode_prelaunch: {
    tone: "accent",
    summary: "起漲前埋伏：只買後端標示「可買」的列，隔日開盤計畫進場，抱 10 天",
    body:
      "買進條件（全部成立才算可買）：大盤站上 20MA 與 60MA · 上櫃 · 出貨排名前 20 · 通過核心+ 品質閘門 · 資料完整性通過 · 當日資料 · 第一天入榜的新訊號。\n" +
      "兩種買法擇一，出場價位完全相同（都以第一筆成交價計算）：一次買滿；或先買一半、跌到成交價 -10% 再補另一半。\n" +
      "出場計畫：災難停損 -20% · 收盤站上 +2.5% 後隔一個交易日起停損上調到 +2% · 目標 +20% · 第 8 天起收盤仍有實質獲利（+1% 以上）就隔日開盤收下 · 基本抱 10 個交易日，第 10 天收盤若仍站上自己的 5 日均價就續抱，最晚第 20 天。\n" +
      "回測（2017-2026，556 筆，近 3 年，含手續費與證交稅）：70.8% 勝、每筆平均 +1.95%；更早的資料 69.5% / +2.04%。19 個有效季度裡沒有一季低於 60%。歷史統計，不是未來勝率。\n" +
      "「勝」在這裡指「有賺錢」。若改用「至少賺 25% 才算贏」，這套規則只有 2.3%，因為 +20% 就停利了。那個目標有另一套規則（見回測登錄簿 G 節）可達 37.6%，但只有 54.9% 的交易賺錢、資金要卡 16 天。兩者已完整比較過，你選擇維持這一套。\n" +
      "2026-09-21 程式碼稽核修正（與規則無關，但會改變畫面上的數字）：① ETF 的升降單位跟個股不同（50 元以上跳 0.05 而不是 0.50），舊版把 0050 的鎖利價算成 108.50、比規則更寬，已修。②「後段獲利了結」這一行早了一天，正確是第 8 天，不是第 7 天。③價格出場之後不再繼續數持有天數。\n" +
      "2026-09-21 修正：鎖利原本「當天盤中觸及 +6%」就算數，但你是收盤後才看到、隔天才下得了單。改成收盤判定、隔日生效後重算同一批交易，舊規則真實勝率 63.0%、新規則 67.1%——先前畫面上的約 70% 有一大半是模擬器產生的。\n" +
      "2026-09-20 起停損由 -15% 放寬到 -20%，既有持倉一併套用，所以舊部位畫面上的停損價會往下移。\n" +
      "名單上其餘的列是觀察與持倉追蹤，不是買點。",
  },
  mode_momentum_leader: {
    tone: "red",
    summary: "警告：此模式實戰為負期望值，建議停用，僅供觀察",
    body:
      "照建議操作的實戰紀錄：勝率 23%、59% 觸發停損。此模式僅保留觀察用途，不應據以下單。",
  },
};
const MODE_CARD_DEFAULT = {
  tone: "dim",
  summary: "此模式尚無實戰驗證數據，交易計畫僅供參考",
  body: "ledger 仍在累積樣本。沒有回測與實戰紀錄的模式，畫面上的價位只是規則推算值。",
};

// F22: colour by the score the mode actually RANKS on. The old card tinted by
// Explosion_Score while the desktop ranked and sorted by something else, so the
// same stock looked "hot" on one screen and ordinary on the other.
const RANK_SCORE = {
  mode_prelaunch: { key: "Launch_Score", label: "起漲條件分" },
  mode_momentum_leader: { key: "Surge_Score", label: "動能分" },
};
const RANK_SCORE_DEFAULT = { key: "Launch_Score", label: "起漲條件分" };

// Sort options. Labels follow report section 8's naming table.
const SORTS = [
  ["排序：後端名次", "rank", "asc"],
  ["起漲條件分 高→低", "Launch_Score", "desc"],
  ["動能分 高→低", "Surge_Score", "desc"],
  ["盤整蓄勢分 高→低", "Explosion_Score", "desc"],
  ["近63日漲幅 高→低", "Gain_3M_Pct", "desc"],
  ["停損距離% 低→高", "Risk_Pct", "asc"],
  ["外資5日 高→低", "Foreign_Net_5D", "desc"],
  ["距近一年最高收盤 近→遠", "Dist_52W_High_Pct", "asc"],
];

// Buy_Block reason codes from scanner/scan_mode.mark_buy_ready(). "unknown"
// and "no_rule" must read as REFUSALS: report section 8, "未知不能當通過".
const BLOCK_TEXT = {
  regime: "大盤未站上20/60MA",
  regime_stale: "大盤資料尚未更新到今天，本次不判定順風",
  stale: "資料非當日，需重新確認",
  integrity: "資料完整性未通過",
  rank: "非前20名",
  market: "非上櫃",
  quality: "未過品質閘門",
  held: "已進場·非新訊號",
  unknown: "後端未提供買進判定（不視為可買）",
  no_rule: "此模式未定義買進規則",
};

const DATA_STATUS_TEXT = {
  current: "當日收盤",
  stale: "沿用前一交易日收盤",
  missing: "無報價",
};

const PAGES = [
  ["today", "總覽"],
  ["picks", "建議"],
  ["positions", "持倉"],
  ["perf", "績效"],
  ["research", "研究"],
];

/* ============================================================================
 * 1. Tiny helpers
 * ==========================================================================*/

const $ = (s) => document.querySelector(s);

// Every string that reaches innerHTML goes through this. Stock names come from
// a JSON file we do not author; treating them as markup is a bug waiting for a
// name with an ampersand in it.
function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

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
  if (n === null || n === 0) return "flat";
  return n > 0 ? "pos" : "neg";
}

// Report 7.4: "do not rely on red/green alone". Every P&L therefore carries a
// sign AND a word, so the number survives colour-blindness and greyscale.
function signWord(v) {
  const n = num(v);
  if (n === null) return "";
  if (n > 0) return "獲利";
  if (n < 0) return "虧損";
  return "持平";
}

let TOAST_TIMER = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  if (TOAST_TIMER) clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => { el.hidden = true; }, 3200);
}

/* ============================================================================
 * 2. Exact money: integer cents
 *
 * Report 9.1: "do not let floating-point error decide whether there was a
 * profit". A position 30 cents from break-even must not flip sign because
 * 0.1 + 0.2 !== 0.3. So every amount below is an INTEGER NUMBER OF CENTS and
 * every division rounds explicitly. This mirrors portfolio/money.py, whose
 * Decimal arithmetic is the reference implementation.
 * ==========================================================================*/

const NUMERIC_RE = /^-?\d{1,15}(\.\d+)?$/;

// Integer division with half-up rounding, ties away from zero (Decimal's
// ROUND_HALF_UP). Written the long way because Math.round() is float rounding
// and rounds -0.5 to -0 rather than away from zero.
function divRound(numerator, denominator) {
  const sign = (numerator < 0) !== (denominator < 0) ? -1 : 1;
  const a = Math.abs(numerator), b = Math.abs(denominator);
  let q = Math.floor(a / b);
  let r = a - q * b;
  // Correct the float division at very large magnitudes, where a/b can land
  // one ulp on the wrong side of an integer boundary.
  while (r < 0) { q -= 1; r += b; }
  while (r >= b) { q += 1; r -= b; }
  return sign * (r * 2 >= b ? q + 1 : q);
}

// Parse a user- or JSON-supplied amount to integer cents. Returns null for
// anything we are not sure about -- refusing is the whole point of F11.
function cents(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    value = String(value);   // shortest round-tripping repr, like money.D()
  }
  let s = String(value).trim().replace(/,/g, "").replace(/\s+/g, "");
  if (s === "") return null;
  if (!NUMERIC_RE.test(s)) return null;
  let neg = false;
  if (s[0] === "-") { neg = true; s = s.slice(1); }
  const dot = s.indexOf(".");
  const whole = dot < 0 ? s : s.slice(0, dot);
  const fracAll = dot < 0 ? "" : s.slice(dot + 1);
  const frac = (fracAll + "00").slice(0, 2);
  let c = Number(whole) * 100 + Number(frac);
  // Sub-cent input rounds half-up to the cent rather than being rejected: the
  // user's AVERAGE cost legitimately has more decimals than a quote does.
  if (fracAll.length > 2 && Number(fracAll[2]) >= 5) c += 1;
  if (!Number.isSafeInteger(c)) return null;
  return neg ? -c : c;
}

// Sub-cent accumulator scale. portfolio/ledger.py carries full Decimal
// precision through the cost-basis loop and only quantizes on write; carrying
// 1e-4 of a cent here keeps partial-sell cost allocation matching it to the
// cent without ever leaving integer arithmetic.
const SUB = 10000;
const toSub = (c) => c * SUB;
const fromSub = (u) => divRound(u, SUB);

function fmtCents(c, opts) {
  if (c === null || c === undefined) return "-";
  const o = opts || {};
  const neg = c < 0;
  const a = Math.abs(c);
  const whole = Math.floor(a / 100);
  const rest = a % 100;
  let s = whole.toLocaleString("en-US");
  // Show cents only when there are any: "+12,350" reads better than
  // "+12,350.00" on a phone, but 9,359.05 must never be shown as 9,359.
  if (rest !== 0 || o.always) s += "." + String(rest).padStart(2, "0");
  const sign = o.signed ? (neg ? "-" : "+") : (neg ? "-" : "");
  return sign + s;
}

// A PRICE always shows two decimals: "144" and "144.00" are the same number,
// but only the second one reads as a quote. Amounts (P&L, fees) keep the
// trailing cents only when they have any.
function fmtPrice(c) {
  return c === null || c === undefined ? "-" : fmtCents(c, { always: true });
}

// Money with its sign AND a word (報告 7.4).
function fmtPnl(c) {
  if (c === null || c === undefined) return "-";
  return signWord(c) + " " + fmtCents(c, { signed: true }) + " 元";
}

// Percentage as a 2dp string, computed on integers so a 9.804 never becomes
// 9.81 by accident. Returns null when the denominator is absent -- "no cost
// basis yet" and "flat" are different answers (money.pct's rationale).
function pctOf(numeratorCents, denominatorCents) {
  if (!denominatorCents) return null;
  const bp = divRound(numeratorCents * 10000, denominatorCents);
  return bp / 100;
}

function fmtPct(p, digits) {
  if (p === null || p === undefined) return "-";
  const d = digits === undefined ? 2 : digits;
  return (p >= 0 ? "+" : "") + p.toFixed(d) + "%";
}

/* ============================================================================
 * 3. Fee schedules and tick sizes  (mirrors portfolio/money.py)
 * ==========================================================================*/

// Rates are integer numerator/denominator pairs so the fee is one rounding
// step, not a chain of float multiplications. Report 6.3: 0.1425% is the
// undiscounted convention, NOT everyone's actual rate -- hence a versioned
// record the user can change, and every execution stores the version that
// priced it.
const FEE_SCHEDULES = {
  "tw-equity-v1": {
    version: "tw-equity-v1",
    label: "台股一般（0.1425%、最低 20 元、元位無條件捨去）",
    feeNum: 1425, feeDen: 1000000,
    discNum: 1, discDen: 1,
    taxNum: 3, taxDen: 1000,
    minFeeCents: 2000,
    roundToDollar: true,
  },
  "tw-equity-exact": {
    version: "tw-equity-exact",
    label: "台股未取整（對帳與文件核算用）",
    feeNum: 1425, feeDen: 1000000,
    discNum: 1, discDen: 1,
    taxNum: 3, taxDen: 1000,
    minFeeCents: 0,
    roundToDollar: false,
  },
};
const DEFAULT_SCHEDULE = "tw-equity-v1";

function schedule(version) {
  return FEE_SCHEDULES[version] || FEE_SCHEDULES[DEFAULT_SCHEDULE];
}

function feeFor(sched, considerationCents) {
  let fee = divRound(considerationCents * sched.feeNum * sched.discNum,
                     sched.feeDen * sched.discDen);
  if (sched.roundToDollar) {
    fee = Math.floor(fee / 100) * 100;             // brokers truncate to the dollar
    if (fee < sched.minFeeCents) fee = sched.minFeeCents;
  } else if (sched.minFeeCents > 0 && fee < sched.minFeeCents) {
    fee = sched.minFeeCents;
  }
  return fee;
}

function taxFor(sched, considerationCents) {
  let tax = divRound(considerationCents * sched.taxNum, sched.taxDen);
  if (sched.roundToDollar) tax = Math.floor(tax / 100) * 100;
  return tax;
}

// What liquidating `shares` at `price` would cost. Kept separate because the
// report forbids it sharing a label with the book P&L (6.3).
function exitCost(sched, priceCents, shares) {
  if (!shares || priceCents === null) return 0;
  const consideration = priceCents * shares;
  return feeFor(sched, consideration) + taxFor(sched, consideration);
}

// TWSE/TPEx quote ladder, in cents: [upper bound exclusive, tick].
const EQUITY_TICKS = [
  [1000, 1], [5000, 5], [10000, 10], [50000, 50], [100000, 100], [null, 500],
];

// ETFs and ETNs quote on their OWN ladder: 0.01 below 50, 0.05 at or above.
// Found 2026-09-21, the day this app started advising on held ETFs. On the
// equity table 0050 at 106.75 would have had its trailing lock snapped from
// 108.85 down to 108.50 -- a stop WIDER than the rule, in the one direction
// that costs money. Taiwan ETF/ETN codes are the 00-prefixed ones, 4 to 7
// characters (0050, 00878, 00663L, 00400A).
const ETF_TICKS = [[5000, 1], [null, 5]];

function isEtfCode(stockId) {
  const sid = String(stockId || "").trim().toUpperCase();
  return sid.length >= 4 && sid.length <= 7 && sid.startsWith("00")
    && /^[0-9]{4}$/.test(sid.slice(0, 4));
}

function tickSize(priceCents, stockId) {
  const ladder = isEtfCode(stockId) ? ETF_TICKS : EQUITY_TICKS;
  for (const [upper, tick] of ladder) {
    if (upper === null || priceCents < upper) return tick;
  }
  return ladder[ladder.length - 1][1];
}

// Snap a PLAN price onto the exchange ladder. Direction matters: a stop rounds
// DOWN and a target UP, because doing it the other way quietly tightens the
// claim. Never push an average cost through here -- an average legitimately
// falls between ticks (money.round_to_tick's caveat).
function tickRound(priceCents, dir, stockId) {
  if (priceCents === null || priceCents <= 0) return null;
  const t = tickSize(priceCents, stockId);
  const steps = dir === "down" ? Math.floor(priceCents / t)
              : dir === "up" ? Math.ceil(priceCents / t)
              : divRound(priceCents, t);
  return steps * t;
}

/* ============================================================================
 * 4. Trading calendar, in Asia/Taipei
 *
 * F14: the old build extrapolated plain weekdays past the end of the known
 * calendar and inferred "market closed" from the ABSENCE of data, then dated
 * everything with the device's local clock. A phone in London therefore rolled
 * the hold-day counter eight hours early, and every typhoon closure ran the
 * counts one day high.
 *
 * Now: meta.calendar_tail and quotes.sessions are the only sources of trading
 * dates, nothing is extended past them, and "today" is Taipei's today.
 * ==========================================================================*/

let CAL = [];        // known trading dates, ascending, no guesses
let CAL_LAST = "";   // newest known trading date

const TW_DATE_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
});
const TW_TIME_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false,
});

// Assembled from parts rather than trusting any locale's date order.
function taipeiDate(d) {
  const p = {};
  for (const part of TW_DATE_FMT.formatToParts(d || new Date())) p[part.type] = part.value;
  return `${p.year}-${p.month}-${p.day}`;
}

function taipeiMinutes(d) {
  const p = {};
  for (const part of TW_TIME_FMT.formatToParts(d || new Date())) p[part.type] = part.value;
  const h = Number(p.hour) % 24;
  return h * 60 + Number(p.minute);
}

function buildCalendar(tail, sessions) {
  const seen = new Set();
  for (const list of [tail || [], sessions || []]) {
    for (const d of list) {
      const s = String(d || "").slice(0, 10);
      if (s) seen.add(s);
    }
  }
  CAL = Array.from(seen).sort();
  CAL_LAST = CAL.length ? CAL[CAL.length - 1] : "";
}

function calIndex(date) {
  return CAL.indexOf(String(date || "").slice(0, 10));
}

// Trading days from `from` to `to`, inclusive of both ends. null when either
// end is outside the known calendar -- an honest "unknown" beats a guess
// (portfolio/ledger._day_index makes the same choice).
function dayIndexBetween(from, to, calendar) {
  const cal = calendar || CAL;
  const a = cal.indexOf(String(from || "").slice(0, 10));
  const b = cal.indexOf(String(to || "").slice(0, 10));
  if (a < 0 || b < 0) return null;
  return b - a + 1;
}

// Last known trading session on or before `date`. "" when the date precedes
// every session we know about.
function sessionOnOrBefore(date) {
  let out = "";
  for (const c of CAL) { if (c <= date) out = c; else break; }
  return out;
}

/* ============================================================================
 * 5. IndexedDB
 *
 * Four stores (the ledger tables of report 9.1, minus everything the phone has
 * no business owning):
 *
 *   positions   one container per real holding. Every money field on it is
 *               DERIVED from `executions` and rewritten on every change.
 *   executions  the only source of truth for a trade. Append-only: an
 *               amendment writes a new revision and demotes the old row
 *               (is_current 0), a mis-entry is voided, nothing is deleted.
 *   marks       one row per position per session: close, shares, cost, the
 *               four totals and the day's P&L. Rebuilt from quotes.json, but
 *               PERSISTED so an older deploy without quotes.json can still
 *               show the last honest valuation and say how old it is.
 *   meta        settings, migration flags and frozen cycle results
 *               ("cycle:<position>:<horizon>:<basis>" -- report 5.3's D10
 *               result, which later prices must never rewrite).
 * ==========================================================================*/

const DB_NAME = "yentool_ledger";
const DB_VERSION = 1;
let DB = null;
// A private-mode or storage-blocked browser can refuse IndexedDB outright. The
// market pages must keep working in that case, so every ledger read checks
// this flag instead of throwing into the middle of a render.
let DB_OK = true;
let LEDGER_ERROR = "";

function openDB() {
  return new Promise((resolve, reject) => {
    if (!DB_OK) return reject(new Error("本機資料庫不可用"));
    if (DB) return resolve(DB);
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = (e) => {
      const db = req.result;
      if (!db.objectStoreNames.contains("positions")) {
        const s = db.createObjectStore("positions", { keyPath: "position_id" });
        s.createIndex("by_stock", "stock_id", { unique: false });
        s.createIndex("by_status", "status", { unique: false });
      }
      if (!db.objectStoreNames.contains("executions")) {
        const s = db.createObjectStore("executions", { keyPath: "execution_id" });
        s.createIndex("by_position", "position_id", { unique: false });
        s.createIndex("by_idem", "idempotency_key", { unique: true });
      }
      if (!db.objectStoreNames.contains("marks")) {
        const s = db.createObjectStore("marks", { keyPath: ["position_id", "session_date"] });
        s.createIndex("by_position", "position_id", { unique: false });
      }
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta", { keyPath: "key" });
      }
      void e;
    };
    req.onsuccess = () => { DB = req.result; resolve(DB); };
    req.onerror = () => reject(req.error || new Error("IndexedDB 無法開啟"));
  });
}

function txDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("交易中止"));
  });
}

function reqDone(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function dbGetAll(store, indexName, key) {
  const db = await openDB();
  const tx = db.transaction(store, "readonly");
  const src = indexName ? tx.objectStore(store).index(indexName) : tx.objectStore(store);
  return reqDone(src.getAll(key === undefined ? undefined : key));
}

async function dbGet(store, key) {
  const db = await openDB();
  const tx = db.transaction(store, "readonly");
  return reqDone(tx.objectStore(store).get(key));
}

async function dbPut(store, value) {
  const db = await openDB();
  const tx = db.transaction(store, "readwrite");
  tx.objectStore(store).put(value);
  return txDone(tx);
}

async function dbPutMany(store, values) {
  if (!values.length) return;
  const db = await openDB();
  const tx = db.transaction(store, "readwrite");
  const os = tx.objectStore(store);
  for (const v of values) os.put(v);
  return txDone(tx);
}

async function dbDelete(store, key) {
  const db = await openDB();
  const tx = db.transaction(store, "readwrite");
  tx.objectStore(store).delete(key);
  return txDone(tx);
}

async function metaGet(key, dflt) {
  const row = await dbGet("meta", key);
  return row === undefined ? dflt : row.value;
}

async function metaSet(key, value) {
  return dbPut("meta", { key, value, updated_at: nowStamp() });
}

function nowStamp() {
  const d = new Date();
  return taipeiDate(d) + " " + TW_TIME_FMT.format(d);
}

function newId(prefix) {
  const rnd = (typeof crypto !== "undefined" && crypto.getRandomValues)
    ? Array.from(crypto.getRandomValues(new Uint8Array(6)))
        .map((b) => b.toString(16).padStart(2, "0")).join("")
    : Math.random().toString(16).slice(2, 14);
  return prefix + "-" + rnd;
}

/* ============================================================================
 * 6. The ledger: positions derived from executions
 *    (mirrors portfolio/ledger._recompute_position)
 * ==========================================================================*/

class LedgerError extends Error {}

function sortExecutions(execs) {
  return execs.slice().sort((a, b) =>
    (a.session_date || "").localeCompare(b.session_date || "") ||
    (a.executed_at || "").localeCompare(b.executed_at || "") ||
    (a.recorded_at || "").localeCompare(b.recorded_at || ""));
}

function currentExecutions(execs) {
  return sortExecutions(execs.filter((e) => e.is_current === 1));
}

// Replay the trade history up to and including `through` (all of it when
// `through` is falsy). Moving weighted average, exactly as ledger.py: buy fees
// go into cost_basis and stay out of gross_cost, a sell removes a pro-rata
// slice of both, and realised P&L is proceeds minus fees, tax and that slice.
function replay(execsSorted, through) {
  let shares = 0;
  let costU = 0;          // book cost incl. buy fees, in sub-cents
  let grossU = 0;         // consideration only, in sub-cents
  let realizedNetU = 0;
  let realizedGrossU = 0;
  let opened = null, closed = null, buys = 0, sells = 0;
  // The FIRST buy of the position, kept because every plan level is anchored
  // to it (2026-09-20). Staged entry adds a second buy at a lower price, so
  // the average cost is no longer the price the rule was measured against.
  let firstBuy = null, cycleBuys = 0;

  for (const e of execsSorted) {
    if (through && e.session_date > through) break;
    const sh = e.shares;
    const consideration = e.price_cents * sh;    // exact: both are integers
    if (e.side === "BUY") {
      buys += 1;
      if (shares === 0 && cycleBuys === 0) firstBuy = e.price_cents;
      cycleBuys += 1;
      if (!opened) opened = e.session_date;
      shares += sh;
      costU += toSub(consideration + e.fee_cents);
      grossU += toSub(consideration);
      closed = null;
    } else {
      sells += 1;
      if (shares <= 0) continue;   // defensive; addExecution refuses this
      const removedBook = divRound(costU * sh, shares);
      const removedGross = divRound(grossU * sh, shares);
      realizedNetU += toSub(consideration - e.fee_cents - e.tax_cents) - removedBook;
      realizedGrossU += toSub(consideration) - removedGross;
      costU -= removedBook;
      grossU -= removedGross;
      shares -= sh;
      if (shares === 0) { closed = e.session_date; cycleBuys = 0; firstBuy = null; }
    }
  }
  if (shares === 0) { costU = 0; grossU = 0; }   // clear rounding residue

  return {
    shares,
    cost_basis: fromSub(costU),
    gross_cost: fromSub(grossU),
    realized_net: fromSub(realizedNetU),
    realized_gross: fromSub(realizedGrossU),
    // Average cost is a per-share price derived from an integer total; it is
    // NOT snapped to a tick, because an average genuinely falls between ticks.
    avg_cost: shares ? fromSub(divRound(grossU, shares)) : 0,
    opened_session: opened,
    closed_session: shares === 0 ? closed : null,
    first_buy_price: firstBuy,
    cycle_buys: cycleBuys,
    buys, sells,
  };
}

// --- what a proposed edit would make the history look like ---------------
//
// The share bound for a SELL used to be `pos.open_shares`, which is the state
// AFTER the whole history. For an archived or closed record that is 0 by
// construction, so correcting the sell that closed the trade -- the commonest
// archive edit there is -- was rejected with 0 shares held, against the state
// that same sell had produced. The bound has to be the shares held at the
// moment the fill being edited sits in, which means folding the proposed
// history rather than reading a summary field.

// `cur` with the row being amended replaced by `candidate`, or `candidate`
// appended for a backfill, in fold order.
function plannedExecutions(cur, editingId, candidate) {
  const others = editingId ? cur.filter((e) => e.execution_id !== editingId) : cur.slice();
  const list = sortExecutions(others.concat([candidate]));
  return { list, index: list.indexOf(candidate) };
}

// Shares held immediately before `index` in that order.
function heldBefore(list, index) {
  return replay(list.slice(0, index), null).shares;
}

// The first SELL in `list` that exceeds the shares held at that point, or null.
//
// Bounding only the EDITED row says nothing about the fills after it. Amend a
// sell upward and the next sell can be left folding against zero shares, where
// replay() simply `continue`s past it (its comment called that "defensive;
// addExecution refuses this" -- which was not true until this function) and
// the whole of that sell's proceeds vanish from realised P&L with no error and
// no label. The mirror case is amending an earlier BUY down: replay then
// removes more basis than exists and lands open_shares negative.
function firstOversell(list) {
  let shares = 0;
  for (const e of list) {
    if (e.side === "BUY") { shares += e.shares; continue; }
    if (e.shares > shares) return { exe: e, held: shares };
    shares -= e.shares;
  }
  return null;
}

function oversellMessage(bad, verb) {
  return `${verb} ${bad.exe.session_date} 的賣出 ${bad.exe.shares.toLocaleString("en-US")} 股`
    + ` 會超過當時持有的 ${bad.held.toLocaleString("en-US")} 股，請先更正那一筆`;
}

function applyDerived(pos, execsSorted) {
  const d = replay(execsSorted, null);
  pos.open_shares = d.shares;
  pos.avg_cost = d.avg_cost;
  pos.cost_basis = d.cost_basis;
  pos.gross_cost = d.gross_cost;
  pos.realized_net = d.realized_net;
  pos.realized_gross = d.realized_gross;
  pos.opened_session = d.opened_session;
  pos.closed_session = d.closed_session;
  pos.first_buy_price = d.first_buy_price;
  pos.cycle_buys = d.cycle_buys;
  // An archived or voided position keeps that state; otherwise shares decide.
  if (pos.status !== "archived" && pos.status !== "void") {
    pos.status = d.shares > 0 ? "open" : (execsSorted.length ? "closed" : "open");
  } else if (pos.status === "archived" && d.shares !== 0) {
    // A correction gave this record shares again -- the closing sell was
    // voided, or a buy was backfilled. Leaving it archived would hide a LIVE
    // holding: activePositions filters it off 持倉, pendingItems never raises
    // its stop or its day 10, ensureUniverseFor never fetches its quote --
    // while rebuildMarks (which skips only void) keeps valuing it and
    // portfolioSummary keeps counting it. The owner would own shares the app
    // never mentions again.
    //
    // Note what this is NOT: dropping "archived" from the condition above is
    // the smaller-looking change and the destructive one, because then the
    // next unrelated correction on any archived record would silently flip it
    // back to "closed" -- archiving undone by a price typo fix. This branch
    // fires only in the one dangerous state.
    //
    // `!== 0` rather than `> 0`: with the oversell guard in place a negative
    // count is unreachable, but visible-and-wrong beats invisible-and-wrong.
    // archived_at is KEPT: this codebase supersedes and never deletes, and the
    // filing timestamp is the one hand-written audit datum on the record.
    pos.status = "open";
    pos.unarchived_at = nowStamp();
  }
  pos.needs_shares = execsSorted.length === 0 && pos.origin === "migrated";
  pos.updated_at = nowStamp();
  return pos;
}

// Validate and record one fill. Deliberately strict (F11): the old code did
// `num(raw) || ref`, so "abc" and "0" both silently stored the REFERENCE price
// as if the user had traded at it. A price we are unsure of is not a price.
function validateExecution(input, pos, heldShares) {
  const errs = {};
  const side = String(input.side || "").toUpperCase();
  if (side !== "BUY" && side !== "SELL") errs.side = "買賣別必須是買進或賣出";

  const date = String(input.session_date || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    errs.session_date = "請填成交日期（YYYY-MM-DD）；沒有成交日就無法放上損益時間軸";
  } else if (date > taipeiDate(new Date())) {
    errs.session_date = "成交日期不能是未來日期";
  }

  const rawShares = String(input.shares === undefined ? "" : input.shares).trim();
  let shares = null;
  if (rawShares === "") errs.shares = "請填股數（1 張 = 1,000 股）";
  else if (!/^\d{1,12}$/.test(rawShares)) errs.shares = `「${rawShares}」不是整數股數，請只填數字`;
  else {
    shares = Number(rawShares);
    if (shares <= 0) errs.shares = "股數必須大於 0";
    else if (side === "SELL" && shares > heldShares) {
      errs.shares = `只持有 ${heldShares.toLocaleString("en-US")} 股，不能賣出 ${shares.toLocaleString("en-US")} 股`;
    }
  }

  const rawPrice = String(input.price === undefined ? "" : input.price).trim();
  const priceCents = cents(rawPrice);
  if (rawPrice === "") errs.price = "請填實際成交價；系統不會用參考價代替";
  else if (priceCents === null) errs.price = `「${rawPrice}」不是有效價格，請重新輸入數字`;
  else if (priceCents <= 0) errs.price = "成交價必須大於 0";

  const sched = schedule(input.fee_schedule || (pos && pos.fee_schedule));
  const consideration = (priceCents && shares) ? priceCents * shares : 0;

  let feeCents = null;
  const rawFee = String(input.fee === undefined ? "" : input.fee).trim();
  if (rawFee === "") feeCents = consideration ? feeFor(sched, consideration) : 0;
  else {
    feeCents = cents(rawFee);
    if (feeCents === null || feeCents < 0) errs.fee = `「${rawFee}」不是有效手續費`;
  }

  let taxCents = null;
  const rawTax = String(input.tax === undefined ? "" : input.tax).trim();
  if (rawTax === "") taxCents = (side === "SELL" && consideration) ? taxFor(sched, consideration) : 0;
  else {
    taxCents = cents(rawTax);
    if (taxCents === null || taxCents < 0) errs.tax = `「${rawTax}」不是有效交易稅`;
  }

  return {
    ok: Object.keys(errs).length === 0,
    errs,
    value: {
      side, session_date: date, shares, price_cents: priceCents,
      fee_cents: feeCents, tax_cents: taxCents, fee_schedule: sched.version,
      consideration,
      note: String(input.note || "").slice(0, 200),
      executed_at: String(input.executed_at || "").slice(0, 5),
    },
  };
}

async function addExecution(positionId, value, opts) {
  const o = opts || {};
  const pos = await dbGet("positions", positionId);
  if (!pos) throw new LedgerError("找不到這筆持倉");
  const execs = await dbGetAll("executions", "by_position", positionId);
  const row = {
    execution_id: newId("exe"),
    position_id: positionId,
    side: value.side,
    session_date: value.session_date,
    executed_at: value.executed_at || "",
    shares: value.shares,
    price_cents: value.price_cents,
    fee_cents: value.fee_cents,
    tax_cents: value.tax_cents,
    fee_schedule: value.fee_schedule,
    note: value.note || "",
    is_current: 1,
    revision: o.revision || 1,
    supersedes: o.supersedes || null,
    void_reason: null,
    idempotency_key: o.idempotency_key || newId("idem"),
    recorded_at: nowStamp(),
  };
  // Refuse before the transaction opens, not after: a fold that cannot be
  // replayed must never reach the store. This guard holds for EVERY caller,
  // not only the form.
  const keptPre = execs.filter((e) => e.execution_id !== (o.supersedes || null));
  const plannedPre = currentExecutions(keptPre.concat([row]));
  const badPre = firstOversell(plannedPre);
  if (badPre) throw new LedgerError(oversellMessage(badPre, "更正後"));

  const db = await openDB();
  const tx = db.transaction(["executions", "positions"], "readwrite");
  const eos = tx.objectStore("executions");
  if (o.supersedes) {
    const old = execs.find((e) => e.execution_id === o.supersedes);
    if (old) {
      old.is_current = 0;
      old.superseded_by = row.execution_id;
      eos.put(old);
    }
  }
  eos.put(row);
  applyDerived(pos, plannedPre);      // the same fold the guard above checked
  tx.objectStore("positions").put(pos);
  await txDone(tx);
  // A corrected fill changes the ten-day result, so the frozen snapshot has to
  // be restated rather than silently kept (report 11.6). The old value is kept
  // inside the meta record's revision list.
  // A BACKFILL changes the ten-day result exactly as much as a correction
  // does. Until 2026-09-22 only a correction restated it, so a backfilled
  // fill moved realized_net and the marks while the frozen figure kept its old
  // value and grew no 已更正 label at all -- strictly worse than the corrected
  // path, because nothing on the page said the two numbers disagreed.
  // restateCycle returns immediately when the position has no frozen cycle, so
  // the first buy of a new position is a no-op.
  await restateCycle(positionId, o.supersedes ? "成交更正" : "補登成交");
  return row;
}

// 撤銷誤登: the execution never happened. It is NOT deleted -- report 5.2
// requires the event to survive even when it is cancelled.
async function voidExecution(executionId, reason) {
  const exe = await dbGet("executions", executionId);
  if (!exe) throw new LedgerError("找不到這筆成交紀錄");
  const pos = await dbGet("positions", exe.position_id);
  if (!pos) throw new LedgerError("找不到這筆持倉");
  exe.is_current = 0;
  exe.void_reason = reason || "user_void";
  exe.voided_at = nowStamp();
  const all = await dbGetAll("executions", "by_position", exe.position_id);
  const kept = all.map((e) => (e.execution_id === executionId ? exe : e));
  // Voiding the first of two buys that a single sell sits on top of is exactly
  // the case a per-row bound cannot see. Refuse with the conflict named rather
  // than let replay() silently swallow the sell (F11: a refusal with a reason,
  // never a silent coercion).
  const badVoid = firstOversell(currentExecutions(kept));
  if (badVoid) throw new LedgerError(oversellMessage(badVoid, "撤銷這筆後，"));
  const db = await openDB();
  const tx = db.transaction(["executions", "positions"], "readwrite");
  tx.objectStore("executions").put(exe);
  applyDerived(pos, currentExecutions(kept));
  tx.objectStore("positions").put(pos);
  await txDone(tx);
  await restateCycle(exe.position_id, "撤銷誤登");
}

// Remove a record and everything derived from it, in one transaction.
//
// This project's rule is SUPERSEDE, NEVER DELETE (report 5.2): a trade that
// happened must survive being cancelled, which is what 撤銷誤登 is for. But a
// record the owner created by mistake and then cancelled entirely has no event
// to preserve -- and until 2026-09-22 there was no way to get rid of it. It
// stayed on 持倉 claiming 持有中 with 0 shares, nagged from 待處理 every day,
// and the only exits were 封存 or 撤銷, each of which just moves the ghost to a
// different permanent table.
//
// So deletion exists, and it tells the truth about what it destroys: the
// caller must show what is going, because from here nothing is recoverable.
// Everything keyed to the position goes together, or the leftovers become
// orphans that portfolioSummary and latestMark would keep reading.
async function deletePosition(posId) {
  const execs = await dbGetAll("executions", "by_position", posId);
  const marks = await dbGetAll("marks", "by_position", posId);
  const pos = await dbGet("positions", posId);
  const horizon = (pos && pos.horizon_days) || STRATEGY.horizon;
  const db = await openDB();
  const tx = db.transaction(["positions", "executions", "marks", "meta"], "readwrite");
  for (const e of execs) tx.objectStore("executions").delete(e.execution_id);
  for (const m of marks) tx.objectStore("marks").delete([m.position_id, m.session_date]);
  tx.objectStore("meta").delete(`cycle:${posId}:${horizon}:position`);
  tx.objectStore("positions").delete(posId);
  await txDone(tx);
  return { executions: execs.length, marks: marks.length };
}

// What deleting this record would actually destroy, in the owner's terms.
function deleteCost(pos) {
  const live = (STATE.execsByPos[pos.position_id] || []).filter((e) => e.is_current === 1);
  const realized = pos.realized_net || 0;
  const empty = live.length === 0 && realized === 0;
  return { live: live.length, realized, empty };
}

async function createPosition(fields) {
  const pos = Object.assign({
    position_id: newId("pos"),
    account_id: "default",
    stock_id: "",
    stock_name: "",
    market: "",
    strategy: STATE.meta.mode || "",
    strategy_version: STATE.meta.strategy_version || STRATEGY.version,
    recommendation_id: null,
    initial_buy_price: null,     // frozen copy of the first-day recommendation
    rec_recommended_on: null,
    fee_schedule: STATE.settings.fee_schedule || DEFAULT_SCHEDULE,
    horizon_days: STRATEGY.horizon,
    cap_days: STRATEGY.cap,
    status: "open",
    origin: "manual",
    note: "",
    open_shares: 0,
    avg_cost: 0,
    cost_basis: 0,
    gross_cost: 0,
    realized_net: 0,
    realized_gross: 0,
    dividends: 0,
    opened_session: null,
    closed_session: null,
    needs_shares: false,
    created_at: nowStamp(),
    updated_at: nowStamp(),
  }, fields || {});
  await dbPut("positions", pos);
  return pos;
}

/* ============================================================================
 * 7. Daily marks and the frozen ten-day cycle
 *
 * Report 6.2 is emphatic and gives the failing number: summing each day's
 * CUMULATIVE P&L yields 45,000 on its own example instead of 10,000. So the
 * cumulative total is stored per day and the DAY's P&L is the DIFFERENCE
 * between consecutive totals -- never a sum of cumulatives.
 * ==========================================================================*/

function buildMarks(pos, execsSorted, closes, sessions, calendar) {
  const sched = schedule(pos.fee_schedule);
  const marks = [];
  const opened = pos.opened_session;
  if (!opened) return marks;

  let prevGross = 0, prevBook = 0;
  let carriedClose = null, carriedFrom = "";

  for (const s of sessions) {
    if (s < opened) continue;
    const snap = replay(execsSorted, s);
    if (!snap.buys) continue;                       // nothing owned yet

    let close = closes ? closes[s] : undefined;
    let status = "current";
    let source = "quotes";
    if (close === undefined || close === null) {
      // A missing bar is not a zero and not a holiday. Carrying the last known
      // close AND SAYING SO is what report 7.2's own mock does ("still valued
      // at the 9/8 close"); inventing today's price is not an option.
      if (carriedClose !== null) {
        close = carriedClose; status = "stale"; source = "carried:" + carriedFrom;
      } else {
        close = null; status = "missing"; source = "";
      }
    } else {
      carriedClose = close; carriedFrom = s;
    }

    const shares = snap.shares;
    const marketValue = close === null ? 0 : close * shares;
    const unrealizedBook = close === null ? 0 : marketValue - snap.cost_basis;
    const unrealizedGross = close === null ? 0 : marketValue - snap.gross_cost;
    const totalBook = snap.realized_net + unrealizedBook + (pos.dividends || 0);
    const totalGross = snap.realized_gross + unrealizedGross;
    const netIfLiq = close === null ? null
      : totalBook - (shares ? exitCost(sched, close, shares) : 0);

    marks.push({
      position_id: pos.position_id,
      session_date: s,
      day_index: dayIndexBetween(opened, s, calendar),
      close_price: close,
      price_source: source,
      price_basis: STATE.quotes ? (STATE.quotes.price_basis || "unverified") : "",
      open_shares: shares,
      cost_basis: snap.cost_basis,
      gross_cost: snap.gross_cost,
      market_value: marketValue,
      unrealized_book: unrealizedBook,
      unrealized_gross: unrealizedGross,
      realized_net: snap.realized_net,
      total_book: totalBook,
      total_gross: totalGross,
      day_pnl_gross: totalGross - prevGross,
      day_pnl_book: totalBook - prevBook,
      net_if_liquidated: netIfLiq,
      data_status: status,
      computed_at: nowStamp(),
    });
    prevGross = totalGross;
    prevBook = totalBook;

    // Stop the day after the position closed: after D4's sale, D5's market
    // move must not touch this trade's P&L (report 5.3).
    if (snap.shares === 0 && snap.sells) break;
  }
  return marks;
}

async function rebuildMarks() {
  if (!DB_OK) return;
  const sessions = STATE.quotes && STATE.quotes.sessions ? STATE.quotes.sessions : [];
  if (!sessions.length) return;   // no feed: keep the marks we already have
  const closesAll = (STATE.quotes && STATE.quotes.closes) || {};
  const lo = sessions[0], hi = sessions[sessions.length - 1];
  const fresh = [];
  const touched = [];             // every non-void position this pass visited
  for (const pos of STATE.positions) {
    if (pos.status === "void") continue;
    touched.push(pos);
    const execs = currentExecutions(STATE.execsByPos[pos.position_id] || []);
    if (!execs.length) continue;  // no current fills: the whole series is baseless
    const series = closesAll[pos.stock_id];
    const closes = {};
    if (series) {
      sessions.forEach((s, i) => {
        const c = cents(series[i]);
        if (c !== null) closes[s] = c;
      });
    }
    for (const m of buildMarks(pos, execs, closes, sessions, CAL)) fresh.push(m);
  }

  // Marks were the one derived store that was written and never deleted, so an
  // edit that SHORTENS a history -- the sell moved earlier, the first buy
  // voided -- left the old rows behind. latestMark() then returns a valuation
  // day the corrected fills no longer reach, and portfolioSummary keeps adding
  // that row's total_book while realized_net has already moved: 帳面總損益 and
  // 已實現淨損益 disagree by the whole correction, permanently, with nothing on
  // screen saying which is current.
  //
  // ONLY inside the quote window. buildMarks iterates `sessions`, so it can
  // never emit a mark outside [lo, hi]; an unbounded sweep would delete every
  // older mark for every position on plain startup -- and marks are NOT in the
  // backup, so that loss is permanent. The `if (!sessions.length) return`
  // above must stay above this: on a feed-outage day `keep` is empty.
  const keep = new Set(fresh.map((m) => m.position_id + " " + m.session_date));
  const stale = [];
  for (const pos of touched) {
    for (const m of (STATE.marksByPos[pos.position_id] || [])) {
      if (m.session_date < lo || m.session_date > hi) continue;
      if (keep.has(pos.position_id + " " + m.session_date)) continue;
      stale.push([pos.position_id, m.session_date]);
    }
  }
  const db = await openDB();
  const tx = db.transaction("marks", "readwrite");
  const mos = tx.objectStore("marks");
  for (const k of stale) mos.delete(k);
  for (const m of fresh) mos.put(m);
  await txDone(tx);
  await freezeDueCycles(fresh, touched);
}

// Report 5.3: at D10 the ten-day result is FIXED. If the user still holds, the
// position keeps being valued (D11, D12...) and stays on the 待處理 list, but
// this number stops moving.
// `touched` is every non-void position this pass visited, NOT only the ones
// that produced marks. A record whose fills were all voided produces none, and
// driving the loop off the marks alone would leave needs_rebuild set forever --
// a 已更正 label hanging over a pre-void number nothing will ever recompute.
async function freezeDueCycles(marks, touched) {
  const byPos = {};
  for (const m of marks) {
    (byPos[m.position_id] = byPos[m.position_id] || []).push(m);
  }
  const visit = touched || Object.keys(byPos).map(
    (id) => STATE.positions.find((p) => p.position_id === id)).filter(Boolean);
  for (const pos of visit) {
    const posId = pos.position_id;
    const list = byPos[posId] || [];
    const horizon = pos.horizon_days || STRATEGY.horizon;
    const key = `cycle:${posId}:${horizon}:position`;
    const existing = await metaGet(key);
    // Already frozen and untouched since: leave it. Report 5.3 -- at D10 the
    // number STOPS moving, and later prices must not rewrite it.
    if (existing && !existing.needs_rebuild) continue;
    if (!list.length) {
      if (existing) {
        await metaSet(key, Object.assign({}, existing, {
          needs_rebuild: false,
          rebuild_failed: "這筆已沒有任何有效成交，十日成果無法重算",
          rebuild_checked_at: nowStamp(),
        }));
      }
      continue;
    }
    let mark = list.find((m) => m.day_index === horizon);
    if (!mark && (pos.status === "closed" || pos.status === "archived")) {
      mark = list[list.length - 1];
    }
    if (!mark) continue;
    if (!existing) {
      await metaSet(key, cycleFrom(pos, mark, horizon));
      continue;
    }
    // A corrected or voided fill changed the history behind this number, and
    // restateCycle() flagged it. Until 2026-09-22 NOTHING read that flag: the
    // history page grew a "已更正" label while the figures stayed the ones
    // computed from the fill the owner had just corrected. Rebuild it from the
    // corrected marks, keeping the restatement trail so "yesterday's number
    // changed" stays answerable.
    const rebuilt = cycleFrom(pos, mark, horizon);
    const sameSession = rebuilt.session_date === existing.session_date;
    // The quote feed keeps 30 sessions. An archived trade older than about six
    // weeks has no reconstructible timeline: buildMarks emits a single mark at
    // sessions[0], and rebuilding off it would rewrite the valuation day to
    // ~30 sessions ago with a null day_index and the close of a stock the
    // owner no longer holds -- then compute a return from that.
    if (!sameSession && pos.open_shares > 0) {
      // Still held, so that last mark is TODAY's unrealised figure, not day
      // 10's. Say so rather than overwrite a frozen number with it.
      await metaSet(key, Object.assign({}, existing, {
        needs_rebuild: false,
        rebuild_failed: "這段期間的收盤資料已不在手機上（只保留最近 30 個交易日），"
          + "十日成果無法重算",
        rebuild_checked_at: nowStamp(),
      }));
      continue;
    }
    if (!sameSession) {
      // Flat position: replay() zeroes the cost basis at zero shares, so the
      // marked value is 0 and total_gross/total_book are the realised figures
      // whatever session the mark sits on. The MONEY is therefore terminal and
      // follows the corrected fills; the VALUATION identity (which day, which
      // close) is not reconstructible, so it keeps the frozen values and the
      // row says that it did.
      Object.assign(rebuilt, {
        session_date: existing.session_date,
        day_index: existing.day_index,
        close_price: existing.close_price,
        return_vs_initial: existing.return_vs_initial,
        return_vs_cost: existing.return_vs_cost,
      });
    }
    // A rebuild must never turn a non-null field null.
    if (rebuilt.return_vs_cost === null && existing.return_vs_cost !== null) {
      rebuilt.return_vs_cost = existing.return_vs_cost;
    }
    if (rebuilt.day_index === null && existing.day_index !== null) {
      rebuilt.day_index = existing.day_index;
    }
    await metaSet(key, Object.assign(rebuilt, {
      frozen_at: existing.frozen_at,
      revisions: existing.revisions || [],
      restated_at: existing.restated_at || null,
      restated_reason: existing.restated_reason || null,
      rebuilt_at: nowStamp(),
      needs_rebuild: false,
      valuation_stale: sameSession ? null : true,
      rebuild_failed: null,
    }));
  }
}

function cycleFrom(pos, mark, horizon) {
  const initial = pos.initial_buy_price;
  return {
    position_id: pos.position_id,
    stock_id: pos.stock_id,
    stock_name: pos.stock_name,
    horizon_days: horizon,
    basis: "position",
    session_date: mark.session_date,
    day_index: mark.day_index,
    close_price: mark.close_price,
    open_shares: mark.open_shares,
    cost_basis: mark.cost_basis,
    total_gross: mark.total_gross,
    total_book: mark.total_book,
    net_if_liquidated: mark.net_if_liquidated,
    return_vs_initial: (initial && mark.close_price !== null)
      ? pctOf(mark.close_price - initial, initial) : null,
    // replay() zeroes avg_cost the moment shares hit 0, so reading
    // pos.avg_cost here silently blanks 相對實際成本 on every rebuild of a
    // closed or archived record -- editing a trade would LOSE a column. The
    // mark carries the cost basis AT that session, which is the number this
    // ratio was always meant to use; buildMarks writes both from one replay
    // snapshot, so it is definitionally the same quantity.
    return_vs_cost: (() => {
      const avg = (mark.open_shares && mark.gross_cost != null)
        ? fromSub(divRound(toSub(mark.gross_cost), mark.open_shares))
        : pos.avg_cost;
      return (avg && mark.close_price !== null)
        ? pctOf(mark.close_price - avg, avg) : null;
    })(),
    still_open: pos.status === "open",
    frozen_at: nowStamp(),
    revisions: [],
  };
}

// An amended or voided fill changes history. The frozen result is REPLACED,
// and the superseded value is kept inside the record so "yesterday's number
// changed" stays answerable.
async function restateCycle(posId, reason) {
  const pos = await dbGet("positions", posId);
  if (!pos) return;
  const horizon = pos.horizon_days || STRATEGY.horizon;
  const key = `cycle:${posId}:${horizon}:position`;
  const old = await metaGet(key);
  if (!old) return;
  old.restated_reason = reason;
  old.restated_at = nowStamp();
  const revisions = (old.revisions || []).concat([{
    total_gross: old.total_gross, total_book: old.total_book,
    net_if_liquidated: old.net_if_liquidated, frozen_at: old.frozen_at,
    reason,
  }]);
  await metaSet(key, Object.assign({}, old, { revisions, needs_rebuild: true }));
}

async function loadCycles() {
  const rows = await dbGetAll("meta");
  const out = {};
  for (const r of rows) {
    if (String(r.key).startsWith("cycle:")) out[r.value.position_id] = r.value;
  }
  return out;
}

/* ============================================================================
 * 8. Application state and data loading
 * ==========================================================================*/

const STATE = {
  meta: {},
  rows: [],
  // Full rows for names that dropped off the list but were recommended
  // recently, so a position in one keeps its chips, moving averages and exit
  // plan (owner, 2026-09-21). The backend publishes the superset; matching it
  // against what is actually held happens here and never leaves the phone.
  tracked: [],
  universe: null,          // {stock_id: row} once fetched
  universeState: "idle",   // idle | loading | ready | missing

  reports: {},
  quotes: null,
  quotesError: "",
  scanError: "",
  positions: [],
  execsByPos: {},
  marksByPos: {},
  cycles: {},
  settings: { fee_schedule: DEFAULT_SCHEDULE },
  page: "today",
  market: "ALL",
  sortIndex: 0,
  query: "",
  loadedAt: "",
};

async function fetchJson(url) {
  // No cache-busting query string. F20: `?t=<Date.now()>` made every request a
  // unique cache key, so caches.match() never matched offline and the cache
  // grew one dead entry per app open. The service worker keys on the bare URL
  // and `cache: "no-store"` is what actually defeats the HTTP cache.
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const text = await res.text();
  if (!text.trim()) throw new Error("檔案是空的");
  return JSON.parse(text);
}

async function loadScan() {
  const data = await fetchJson(DATA_URL);
  const meta = data.meta || {};
  STATE.meta = meta;
  STATE.reports = meta.reports || {};
  // The shipped order IS the rank. A user's sort must never change it
  // (report 7.4: sorting changes the view, not the recommendation).
  STATE.rows = (data.rows || []).map((r, i) => Object.assign({}, r, { _rank: i + 1 }));
  STATE.tracked = (data.tracked || []).map((r) => Object.assign({}, r, { _tracked: true }));
  STATE.scanError = "";
}

async function loadQuotes() {
  try {
    const q = await fetchJson(QUOTES_URL);
    if (!q || !Array.isArray(q.sessions)) throw new Error("格式不符");
    STATE.quotes = q;
    STATE.quotesError = "";
  } catch (e) {
    // An older deploy has no quotes.json at all. That is a degraded state, not
    // a crash: positions fall back to the marks already in IndexedDB and the
    // valuation date is shown so nobody mistakes them for today's.
    STATE.quotes = null;
    STATE.quotesError = e.message || String(e);
  }
}

// Lazy, and at most once per app session. A miss is not an error: an older
// deploy simply has no universe.json, and the card says what it can.
async function loadUniverse() {
  if (STATE.universeState === "ready" || STATE.universeState === "loading") return;
  STATE.universeState = "loading";
  try {
    const u = await fetchJson(UNIVERSE_URL);
    STATE.universe = (u && u.stocks) || null;
    STATE.universeMeta = u || null;
    STATE.universeState = STATE.universe ? "ready" : "missing";
  } catch (e) {
    STATE.universe = null;
    STATE.universeState = "missing";
  }
  render();
}

// Any holding that is not in the list needs the wider file; ask for it once.
function ensureUniverseFor(positions) {
  if (STATE.universeState !== "idle") return;
  const need = (positions || []).some((p) => {
    if (p.status === "void" || p.status === "archived" || p.status === "closed") return false;
    const id = String(p.stock_id);
    return !STATE.rows.some((r) => String(r.Stock_ID) === id) &&
           !STATE.tracked.some((r) => String(r.Stock_ID) === id);
  });
  if (need) loadUniverse().catch(() => {});
}

async function loadLedger() {
  if (!DB_OK) return;
  STATE.positions = await dbGetAll("positions");
  const execs = await dbGetAll("executions");
  STATE.execsByPos = {};
  for (const e of execs) {
    (STATE.execsByPos[e.position_id] = STATE.execsByPos[e.position_id] || []).push(e);
  }
  // Positions written before 2026-09-20 have no first_buy_price. Derive it in
  // memory from the executions we just loaded rather than rewriting the store:
  // the plan levels need it, and a read must not mutate the ledger.
  for (const pos of STATE.positions) {
    if (pos.first_buy_price) continue;
    const execs = currentExecutions(STATE.execsByPos[pos.position_id] || []);
    if (!execs.length) continue;
    const d = replay(execs, null);
    pos.first_buy_price = d.first_buy_price;
    pos.cycle_buys = d.cycle_buys;
  }
  const marks = await dbGetAll("marks");
  STATE.marksByPos = {};
  for (const m of marks) {
    (STATE.marksByPos[m.position_id] = STATE.marksByPos[m.position_id] || []).push(m);
  }
  for (const list of Object.values(STATE.marksByPos)) {
    list.sort((a, b) => a.session_date.localeCompare(b.session_date));
  }
  STATE.cycles = await loadCycles();
}

async function load() {
  setStatus("載入中…");
  try {
    await loadScan();
  } catch (e) {
    // Report 7.4: a failed refresh keeps the last data on screen and labels it,
    // instead of blanking the app.
    STATE.scanError = e.message || String(e);
  }
  await loadQuotes();
  buildCalendar(STATE.meta.calendar_tail, STATE.quotes && STATE.quotes.sessions);
  await loadLedger();
  await rebuildMarks();
  await loadLedger();               // pick the rebuilt marks back up
  ensureUniverseFor(STATE.positions);
  STATE.loadedAt = nowStamp();
  render();
  setStatus(STATE.scanError ? "更新失敗，顯示上次資料" : "更新於 " + STATE.loadedAt);
  // Never awaited: a slow or failing GitHub call must not hold up the screen.
  maybeAutoRefresh().catch(() => {});
}

function setStatus(text) {
  const el = $("#status");
  if (el) el.textContent = text;
}

/* ============================================================================
 * 9. Derived views
 * ==========================================================================*/

// The bar date the ROWS actually represent. An older backend ships no
// meta.data_date, and falling back to "the newest session we know about" made
// a July scan announce itself as today's -- the exact confusion report section
// 8 warns about (Data_Date and Scan_Time are different facts). So fall back to
// the rows themselves, and only then admit we do not know.
function effectiveDataDate() {
  const m = String(STATE.meta.data_date || "").slice(0, 10);
  if (m) return m;
  let best = "";
  for (const r of STATE.rows) {
    const d = String(r.Data_Date || "").slice(0, 10);
    if (d > best) best = d;
  }
  return best;
}

function rankScore() {
  return RANK_SCORE[STATE.meta.mode] || RANK_SCORE_DEFAULT;
}

// F22: the technical-score tier tints a CHIP, never the whole card, so it can
// never be confused with the trade-state colour (and never hides anything).
function scoreTier(row) {
  const v = num(row[rankScore().key]);
  if (v === null) return "";
  if (v >= 70) return "score-high";
  if (v >= 50) return "score-mid";
  return "";
}

// The regime, as ONE source for both the banner and the rule. The old build
// had a banner saying "neutral, be conservative" while buyRuleBlock() hard-
// blocked every buy -- two different sentences about the same fact.
function regimeView() {
  const reg = STATE.meta.regime || {};
  if (!reg.ok) {
    return { tone: "off", text: "大盤狀態無法判讀 · 不視為順風，暫停開新倉", enterOk: false, asOf: reg.as_of_date || "" };
  }
  const asOf = reg.as_of_date || "";
  // F15: a cached regime that merely LOOKS like a tailwind is not one.
  if (reg.is_current === false) {
    return {
      tone: "off", enterOk: false, asOf,
      text: `大盤判定非最新（資料日 ${asOf || "未知"}）· 不視為順風，暫停開新倉`,
    };
  }
  if (reg.enter_ok && reg.strong) {
    return { tone: "on", enterOk: true, asOf, text: "大盤強順風 · 可開新倉" };
  }
  if (reg.enter_ok) {
    return { tone: "on", enterOk: true, asOf, text: "大盤順風（20MA 上緣 <2.2%）· 可開新倉、部位減量" };
  }
  if (reg.risk_on) {
    return { tone: "mid", enterOk: false, asOf, text: "大盤中性（跌破 20MA）· 依規則暫停開新倉" };
  }
  return { tone: "off", enterOk: false, asOf, text: "大盤逆風（跌破 60MA）· 暫緩開新倉、減碼" };
}

// F07: read the backend's verdict. The ONLY thing we may do locally is
// downgrade -- an old data date, or a regime that is no longer current, turns
// a "buy" into "confirm first". We never turn a refusal into a buy.
function buyVerdict(row) {
  const backendReady = row.Buy_Ready === true;
  const backendBlock = String(row.Buy_Block || "");

  if (row.Buy_Ready === undefined && !backendBlock) {
    return { ok: false, code: "unknown", text: BLOCK_TEXT.unknown, source: "client" };
  }
  if (!backendReady) {
    const code = backendBlock || "unknown";
    return { ok: false, code, text: BLOCK_TEXT[code] || code, source: "backend" };
  }
  // Downgrades, most actionable first.
  const bar = String(row.Data_Date || "").slice(0, 10);
  if (CAL_LAST && bar && bar < CAL_LAST) {
    return { ok: false, code: "stale", text: `${BLOCK_TEXT.stale}（資料 ${bar}，最新交易日 ${CAL_LAST}）`, source: "client" };
  }
  const validUntil = String(row.Rec_Valid_Until || "").slice(0, 10);
  if (validUntil && CAL_LAST && validUntil < CAL_LAST) {
    return { ok: false, code: "stale", text: `建議有效期已過（${validUntil}）`, source: "client" };
  }
  const reg = regimeView();
  if (!reg.enterOk) {
    return { ok: false, code: "regime", text: reg.text, source: "client" };
  }
  return { ok: true, code: "", text: "可買", source: "backend" };
}

function latestMark(posId) {
  const list = STATE.marksByPos[posId];
  return list && list.length ? list[list.length - 1] : null;
}

function activePlan(pos) {
  // Every level is derived from the FIRST fill and stored as a strategy
  // snapshot, not from a drifting close price. Report 5.4: the protective stop
  // ratchets UP only -- a falling market must never recompute a lower stop and
  // quietly widen the risk.
  //
  // 2026-09-20: the anchor is the first buy, not the average cost. Staged entry
  // buys the second half lower, which would otherwise drag the whole plan down
  // with it -- and the staged rule was measured with the levels held still.
  // Positions with one buy are unaffected: their first buy IS their average.
  if (!pos.open_shares || !pos.avg_cost) return null;
  const base = pos.first_buy_price || pos.avg_cost;
  const stop0 = divRound(base * (100 + STRATEGY.stopPct), 100);
  const arm = divRound(base * (100 + STRATEGY.armPct), 100);
  const lock = divRound(base * (100 + STRATEGY.lockPct), 100);
  const target = divRound(base * (100 + STRATEGY.targetPct), 100);
  const marks = STATE.marksByPos[pos.position_id] || [];
  let high = null, highOn = "";
  for (const m of marks) {
    if (m.close_price !== null && (high === null || m.close_price > high)) {
      high = m.close_price; highOn = m.session_date;
    }
  }
  // The lock is armed by a CLOSE at or above the arm price, and the raised
  // stop is the order placed for the NEXT session -- which is exactly what
  // `marks` are (one close per session), so the phone and the backend read
  // the same event. 2026-09-21: the backend used to arm on the intraday high
  // and let that same bar be stopped on it; see STRATEGY.armPct.
  const armed = high !== null && high >= arm;
  // Staged entry: the second half is still outstanding while the position has
  // had exactly one buy. A buy limit rounds DOWN to a real tick, the same
  // "never claim a better price than is orderable" rule the stop follows.
  const add = divRound(base * (100 + STRATEGY.addPct), 100);
  const scaleOut = divRound(base * (100 + STRATEGY.scaleOutPct), 100);
  const staged = (pos.cycle_buys || 0) <= 1;
  // The stock's own 5-bar mean. The time exit is extended while the close is
  // above it (2026-09-21), so this has to be the SAME five sessions the
  // backend used, not merely the last five numbers available.
  //
  // It used to drop the unpriced sessions first and then take five, so one
  // quote gap silently stretched the "5-day mean" across six or more
  // sessions while the backend's spanned exactly five -- two screens, two
  // verdicts, same position. Now the last five SESSIONS are taken first, and
  // if any of them is unpriced there is no mean to show.
  const last5marks = marks.slice(-5);
  const last5 = last5marks.map((m) => m.close_price);
  const ma5 = (last5.length === 5 && last5.every((v) => v !== null))
    ? divRound(last5.reduce((a, b) => a + b, 0), 5) : null;
  const priced = marks.filter((m) => m.close_price !== null);
  const lastClose = priced.length ? priced[priced.length - 1].close_price : null;
  const sid = pos.stock_id;
  return {
    stock_id: sid,
    stop: armed ? Math.max(stop0, lock) : stop0,
    stop_orderable: tickRound(armed ? Math.max(stop0, lock) : stop0, "down", sid),
    initial_stop: stop0,
    arm, lock, target,
    target_orderable: tickRound(target, "up", sid),
    add, add_orderable: tickRound(add, "down", sid), add_open: staged,
    scale_out: scaleOut, scale_out_orderable: tickRound(scaleOut, "up", sid),
    base,
    armed, armed_on: armed ? highOn : "",
    highest_close: high,
    ma5, last_close: lastClose,
    riding: ma5 !== null && lastClose !== null && lastClose > ma5,
  };
}

// --- tomorrow's orders ------------------------------------------------------
// Owner's request 2026-09-21: "at the close, tell me tomorrow's plan too --
// below which price do I cut or add, above which do I take some profit or keep
// riding". Every rung below is a price you can place as an order tonight, and
// each one says whether the rule requires it or merely allows it.
//
// What is NOT here is as deliberate as what is: reducing part of the position
// on a break has now been tested twice, on two different engines, and loses
// win rate AND money both times, so there is no "cut some on the way down"
// rung to place.
function tomorrowOrders(pos, plan, dayIdx, horizon) {
  if (!plan) return "";
  const rows = [];
  const past = dayIdx !== null && dayIdx >= horizon;

  rows.push({
    side: "sell", must: true,
    price: plan.target_orderable,
    label: "全部停利",
    note: `成交價 +${STRATEGY.targetPct}%`,
  });
  if (plan.scale_out < plan.target) {
    rows.push({
      side: "sell", must: false,
      price: plan.scale_out_orderable,
      label: "可賣一半（選用）",
      note: `成交價 +${STRATEGY.scaleOutPct}%；回測勝率 +0.5pp、平均報酬 -0.15pp`,
    });
  }
  if (!plan.armed) {
    rows.push({
      side: "watch", must: false,
      price: tickRound(plan.arm, "up", plan.stock_id),
      label: "收盤站上這裡 → 隔日起停損上調",
      note: `收盤 ≥ 成交價 +${STRATEGY.armPct}%，隔一個交易日起停損改掛 ${fmtPrice(tickRound(plan.lock, "down", plan.stock_id))}`,
    });
  }
  // The late profit-take only exists once the hold is far enough along, so it
  // appears on the card exactly when it becomes actionable.
  //
  // `dayIdx` is 1-BASED ("day 8 of 10"), and so is the backend: exit_rules
  // fires when (bar index + 1) >= late_from, i.e. on day 8. This used to read
  // `lateFrom - 1` and put the rung on the card a day early -- on a rung
  // labelled MUST, so the owner would have sold at day 8's open while the
  // backend was still waiting for day 9's. Found 2026-09-21 by audit.
  if (dayIdx !== null && dayIdx >= STRATEGY.lateFrom) {
    const lateAt = tickRound(divRound(plan.base * (100 + STRATEGY.lateGainPct), 100), "up", plan.stock_id);
    rows.push({
      side: "watch", must: true,
      price: lateAt,
      label: "收盤在這之上 → 隔日開盤就收下",
      note: `第 ${STRATEGY.lateFrom} 天起，只要收盤還高於成交價 +${STRATEGY.lateGainPct}%（已蓋過手續費與證交稅）就先出場，不要把獲利帶進最後一天`,
    });
  }
  rows.push({
    side: "stop", must: true,
    price: plan.stop_orderable,
    label: plan.armed ? "跌破全部出場（鎖利價）" : "跌破全部出場（災難停損）",
    note: plan.armed
      ? `鎖利已啟動，這個價位只升不降`
      : `成交價 ${STRATEGY.stopPct}%；這是唯一非守不可的價位`,
  });
  if (plan.add_open && !past) {
    rows.push({
      side: "buy", must: false,
      price: plan.add_orderable,
      label: "可加碼（選用）",
      note: `成交價 ${STRATEGY.addPct}%；先買一半的買法在此補滿。加碼會放大最大虧損，出場價位仍以第一筆成交價計算`,
    });
  }

  const order = { sell: 0, watch: 1, stop: 2, buy: 3 };
  rows.sort((a, b) => (order[a.side] - order[b.side]) || (b.price - a.price));

  const timeLine = dayIdx === null
    ? "持有天數未知，請先確認成交日期。"
    : past
      ? `第 ${horizon} 天已到：收盤若仍站上 5 日均價 ${plan.ma5 === null ? "（資料不足）" : fmtPrice(plan.ma5)} 就續抱，否則收盤出場（最晚第 ${STRATEGY.cap} 天）。`
      : `第 ${dayIdx}/${horizon} 天。到第 ${horizon} 天收盤出場，但當天收盤若仍站上自己的 5 日均價就續抱，最晚第 ${STRATEGY.cap} 天。`;

  const ride = plan.ma5 === null ? "" :
    `<div class="hint">目前收盤 ${fmtPrice(plan.last_close)}｜5 日均價 ${fmtPrice(plan.ma5)}｜${
      plan.riding ? "站上，到期可續抱" : "跌破，到期就出場"}</div>`;

  return `<div class="sec-title">明日委託（收盤後更新，價位皆可直接掛單）</div>` +
    `<table class="tbl"><thead><tr><th>價位</th><th>動作</th><th>必守</th></tr></thead><tbody>${
      rows.map((r) => `<tr class="${r.side === "stop" ? "neg" : ""}">
        <td><b>${esc(fmtPrice(r.price))}</b></td>
        <td>${esc(r.label)}<br><span class="hint">${esc(r.note)}</span></td>
        <td>${r.must ? "必守" : "選用"}</td></tr>`).join("")
    }</tbody></table>` +
    `<div class="plan">時間：${esc(timeLine)}</div>` + ride;
}

// The one place that decides what needs a human today (report 7.2).
function pendingItems() {
  const out = [];
  for (const pos of STATE.positions) {
    if (pos.status === "void" || pos.status === "archived") continue;
    const name = `${(pos.stock_name && pos.stock_name !== pos.stock_id ? pos.stock_name : stockName(pos.stock_id)) || pos.stock_id}（${pos.stock_id}）`;

    if (pos.needs_shares) {
      out.push({ kind: "shares", pos, text: `${name}：由舊版匯入，缺少股數，請補登實際成交` });
      continue;
    }
    if (pos.status === "closed") continue;

    const m = latestMark(pos.position_id);
    if (!m) {
      out.push({ kind: "gap", pos, text: `${name}：尚無任何收盤估值（quotes.json 未涵蓋此檔）` });
      continue;
    }
    const horizon = pos.horizon_days || STRATEGY.horizon;
    if (m.day_index !== null && m.day_index >= horizon && pos.open_shares > 0) {
      out.push({
        kind: "d10", pos,
        text: `${name}：第 ${m.day_index} 個交易日已到（計畫 ${horizon} 天），尚未登錄賣出`,
      });
    }
    if (m.data_status === "missing") {
      out.push({
        kind: "gap", pos,
        text: `${name}：${m.session_date} 無收盤價，未實現損益無法估值（不影響其他持倉）`,
      });
    } else if (m.data_status !== "current") {
      out.push({
        kind: "gap", pos,
        text: `${name}：資料缺漏，損益仍以 ${m.price_source.replace("carried:", "")} 收盤估值`,
      });
    }
    const plan = activePlan(pos);
    if (plan && plan.armed) {
      out.push({
        kind: "trail", pos,
        text: `${name}：鎖利條件已成立（曾達 ${fmtPrice(plan.highest_close)}），查看下個交易日計畫`,
      });
    }
    if (plan && m.close_price !== null && m.close_price <= plan.stop) {
      out.push({
        kind: "stop", pos,
        text: `${name}：收盤 ${fmtPrice(m.close_price)} 已在有效停損 ${fmtPrice(plan.stop)} 之下，請確認出場計畫`,
      });
    } else if (plan && plan.add_open && m.close_price !== null && m.close_price <= plan.add
               && !(m.day_index !== null && m.day_index >= horizon)) {
      // Past the planned hold the trade is on its way out; telling someone to
      // buy the second half of a position they should be closing is worse
      // than saying nothing.
      out.push({
        kind: "add", pos,
        text: `${name}：收盤 ${fmtPrice(m.close_price)} 已到加碼價 ${fmtPrice(plan.add)}（分批買法才補另一半；一次買滿的話忽略）`,
      });
    }
  }
  return out;
}

// Report 6.4: amounts add up, PERCENTAGES DO NOT, and a total built from mixed
// valuation dates has to say so.
function portfolioSummary() {
  let totalBook = 0, totalGross = 0, realized = 0, dayPnl = 0, invested = 0;
  let netIfLiq = 0, netIfLiqKnown = true;
  let openN = 0, valuationDate = "";
  const staleList = [];
  const unpriced = [];
  let dayPnlComplete = true;

  // Only a VOIDED position leaves the totals. Archiving files a finished trade
  // away from the working list; it does not un-earn the money, and a realised
  // profit that disappears when you tidy up is a falsified history.
  for (const pos of STATE.positions) {
    if (pos.status === "void") continue;
    realized += pos.realized_net || 0;
    if (pos.status === "open") { openN += 1; invested += pos.cost_basis || 0; }
    const m = latestMark(pos.position_id);
    if (!m) {
      if (pos.status === "open") staleList.push({ pos, as_of: null });
      continue;
    }
    if (!valuationDate || m.session_date > valuationDate) valuationDate = m.session_date;
  }
  for (const pos of STATE.positions) {
    if (pos.status === "void") continue;
    const m = latestMark(pos.position_id);
    if (!m) continue;
    totalBook += m.total_book;
    totalGross += m.total_gross;
    // An unpriced holding contributes its REALISED part and nothing else. The
    // account total therefore has a known hole in it, and must say so.
    if (m.close_price === null && pos.status === "open") unpriced.push(pos.stock_id);
    if (m.net_if_liquidated === null) netIfLiqKnown = false;
    else netIfLiq += m.net_if_liquidated;
    if (m.session_date === valuationDate) dayPnl += m.day_pnl_gross;
    else if (pos.status === "open") {
      // Only a position we still HOLD can be under-valued. A trade that closed
      // last week is not "stale"; it is finished.
      dayPnlComplete = false;
      staleList.push({ pos, as_of: m.session_date });
    }
    if (m.data_status !== "current" && pos.status === "open") {
      staleList.push({ pos, as_of: m.session_date });
    }
  }

  return {
    open_positions: openN,
    invested_cost: invested,
    total_book: totalBook,
    total_gross: totalGross,
    realized_net: realized,
    day_pnl: dayPnl,
    day_pnl_complete: dayPnlComplete,
    net_if_liquidated: netIfLiqKnown ? netIfLiq : null,
    return_on_cost: invested > 0 ? pctOf(totalBook, invested) : null,
    valuation_date: valuationDate,
    valuation_complete: staleList.length === 0,
    stale: staleList,
    unpriced,
  };
}

// Everything that still needs a human. A CLOSED position stays here until the
// user archives it: 登錄賣出 and 封存 are two different actions (report 7.4),
// and auto-hiding a position the moment its last share is sold would leave
// 封存 with nothing to act on.
function activePositions() {
  return STATE.positions.filter((p) => p.status !== "void" && p.status !== "archived");
}

/* ============================================================================
 * 10. Shared render pieces
 * ==========================================================================*/

function kv(label, value, cls, sub) {
  return `<div class="kv"><span class="kv-l">${esc(label)}</span>` +
    `<span class="kv-v ${cls || ""}">${value}</span>` +
    (sub ? `<span class="kv-s">${esc(sub)}</span>` : "") + `</div>`;
}

function drow(k, v) {
  return `<div class="drow"><span class="k">${esc(k)}</span><span>${v}</span></div>`;
}

function lightChip(label, on) {
  return `<span class="light ${on ? "on" : ""}">${esc(label)}</span>`;
}

function noticeHtml(tone, text) {
  return `<div class="notice ${tone}">${esc(text)}</div>`;
}

function btn(action, label, cls, data) {
  const attrs = Object.entries(data || {})
    .map(([k, v]) => ` data-${k}="${esc(v)}"`).join("");
  return `<button type="button" class="btn ${cls || ""}" data-act="${esc(action)}"${attrs}>${esc(label)}</button>`;
}

// Global banners: scan/quote faults, degraded feed, stale scan. These use
// .notice, which is a DATA notice. F10: the old build shared the class name
// `.stale` between "the data is old" (display:none until shown) and "this card
// is past its exit date", so an overdue holding inherited display:none and
// became completely invisible. Data notices and card states now have
// disjoint class namespaces, and no card state ever hides anything.
function renderNotices() {
  const out = [];
  const m = STATE.meta;
  if (LEDGER_ERROR) {
    out.push(noticeHtml("err", `⚠ 無法開啟本機資料庫，持倉功能停用（${LEDGER_ERROR}）· 行情與建議仍可使用`));
  }
  if (STATE.scanError) {
    out.push(noticeHtml("err", `⚠ 無法更新掃描結果（${STATE.scanError}）· 以下為上次成功載入的資料`));
  }
  if (m.degraded) {
    out.push(noticeHtml("err", `⚠ 資料源異常（${m.degraded}）· 缺漏市場≠空手 · 持倉照常追蹤，狀態未被覆寫`));
  }
  if (STATE.quotesError) {
    out.push(noticeHtml("warn",
      `⚠ 報價檔 quotes.json 無法讀取（${STATE.quotesError}）· 持倉沿用先前估值，日期見各卡片`));
  }
  const dataDate = effectiveDataDate();
  if (dataDate && CAL_LAST && dataDate < CAL_LAST) {
    out.push(noticeHtml("warn", `⏳ 掃描結果為 ${dataDate}，已知最新交易日為 ${CAL_LAST}，建議重新整理`));
  }
  if (!STATE.meta.data_date && dataDate) {
    out.push(noticeHtml("info", `ℹ 舊版資料格式（未附 meta.data_date），行情日期取自個股 Data_Date：${dataDate}`));
  }
  if (m.empty_ok) {
    out.push(noticeHtml("info", "今日 0 檔入選 · 這是正常的空結果（資料源正常，非故障）"));
  }
  // Column self-check (scanner/result_checks.py). The cloud audits every
  // column of this file before publishing; a fail means at least one column
  // is wrong and scan-timer is already retrying -- do not act on the numbers.
  const chk = m.checks;
  if (chk && chk.status === "fail") {
    out.push(noticeHtml("err", `⚠ 欄位自我檢測未通過（${chk.errors} 項錯誤、${chk.warnings} 項警告：${checkCodes(chk)}）· 雲端會自動重掃 · 明細見「研究」頁`));
  } else if (chk && chk.status === "warn") {
    out.push(noticeHtml("warn", `⚠ 欄位自我檢測 ${chk.warnings} 項警告（${checkCodes(chk)}）· 明細見「研究」頁`));
  }
  $("#notices").innerHTML = out.join("");
}

function checkCodes(chk) {
  const codes = [];
  for (const it of (chk && chk.items) || []) {
    if (it.level === "info" || codes.includes(it.code)) continue;
    codes.push(it.code);
    if (codes.length >= 3) break;
  }
  return codes.join("、") || "無";
}

function tabBar() {
  return PAGES.map(([id, label]) =>
    `<button type="button" class="tab ${STATE.page === id ? "on" : ""}" data-act="page" data-page="${id}">${esc(label)}</button>`).join("");
}

function render() {
  renderNotices();
  $("#tabs").innerHTML = tabBar();
  const dd = effectiveDataDate();
  $("#asof").textContent = dd ? `行情截至 ${dd} 收盤` : "尚無行情日期";
  for (const [id] of PAGES) {
    const el = document.getElementById("page-" + id);
    el.hidden = id !== STATE.page;
  }
  const fn = { today: renderToday, picks: renderPicks, positions: renderPositions,
               perf: renderPerf, research: renderResearch }[STATE.page];
  if (fn) fn();
}

/* ============================================================================
 * 11. The five pages
 * ==========================================================================*/

// --- 11.1 今日總覽 (report 7.2) ---------------------------------------------
function renderToday() {
  const s = portfolioSummary();
  const pending = pendingItems();
  const picks = STATE.rows.filter((r) => buyVerdict(r).ok);
  const reg = regimeView();
  const m = STATE.meta;
  const horizon = STRATEGY.horizon;
  const d10 = pending.filter((p) => p.kind === "d10").length;

  const head = `
    <div class="asof-block">
      <div class="asof-line"><span>行情截至</span><b>${esc(effectiveDataDate() || "未知")} 收盤</b></div>
      <div class="asof-line"><span>最近成功更新</span><b>${esc(String(m.scan_time || "未知"))}</b></div>
      <div class="asof-line"><span>資料完整度</span><b>${esc(dataCompletenessText())}</b></div>
      <div class="asof-line"><span>估值日期</span><b>${esc(s.valuation_date || "尚無估值")}${s.valuation_complete ? "" : " · 估值不完整"}</b></div>
    </div>`;

  // The four totals stay apart. Report 6.3 forbids gross / book / net-if-
  // liquidated sharing one "總獲利" label, so each carries its own basis.
  const totals = `
    <div class="totals">
      ${totalCard("帳面總損益", s.total_book,
        "已實現淨損益＋未實現帳面損益（未扣未來賣出費稅）" +
        (s.unpriced.length ? `｜${s.unpriced.length} 檔無報價未計入未實現部分：${s.unpriced.join("、")}` : ""))}
      ${totalCard("今日損益", s.day_pnl, s.day_pnl_complete ? "同一估值日的價差變動" : "部分持倉估值日不同，僅計最新估值日")}
      ${totalCard("已實現淨損益", s.realized_net, "已扣實際手續費與交易稅")}
      ${totalCard("價差總損益", s.total_gross, "只算價差，不含任何費稅")}
      ${totalCard("若今日全數賣出估計淨損益", s.net_if_liquidated, "帳面總損益扣掉賣出費稅的估計值")}
    </div>
    <div class="hint">報酬率不可相加：帳面總損益 ÷ 投入成本 ${
      s.return_on_cost === null ? "（尚無成本基礎）" : "＝ " + fmtPct(s.return_on_cost)
    }（靜態同批投入口徑）</div>`;

  const chips = `
    <div class="chip-row">
      <button type="button" class="stat ${pending.length ? "alert" : ""}" data-act="page" data-page="positions">待處理 ${pending.length}</button>
      <button type="button" class="stat" data-act="page" data-page="picks">今日新建議 ${picks.length}</button>
      <button type="button" class="stat" data-act="page" data-page="positions">持倉 ${s.open_positions}</button>
      <button type="button" class="stat ${d10 ? "alert" : ""}" data-act="page" data-page="positions">D${horizon}到期 ${d10}</button>
    </div>`;

  const pendingHtml = pending.length
    ? `<ul class="pending">${pending.map((p) =>
        `<li class="p-${esc(p.kind)}">${esc(p.text)}</li>`).join("")}</ul>`
    : `<div class="empty-inline">目前沒有待處理事項。</div>`;

  document.getElementById("page-today").innerHTML =
    head +
    `<div class="sec"><h2>總損益</h2>${totals}</div>` +
    chips +
    `<div class="sec"><h2>待處理 ${pending.length}</h2>${pendingHtml}</div>` +
    `<div class="sec"><h2>大盤</h2><div class="notice ${reg.tone === "on" ? "ok" : reg.tone === "mid" ? "warn" : "err"}">` +
      `${esc(reg.text)}${reg.asOf ? esc(` · 判定資料日 ${reg.asOf}`) : ""}</div>` +
      (m.regime && m.regime.text ? `<div class="hint">${esc(m.regime.text)}</div>` : "") +
    `</div>` +
    strategyCardHtml();
}

function totalCard(label, valueCents, sub) {
  const cls = valueCents === null ? "flat" : signClass(valueCents);
  const val = valueCents === null ? "-" : fmtPnl(valueCents);
  return `<div class="total"><span class="t-l">${esc(label)}</span>` +
    `<span class="t-v ${cls}">${esc(val)}</span>` +
    `<span class="t-s">${esc(sub)}</span></div>`;
}

function dataCompletenessText() {
  const q = STATE.meta.quotes || {};
  const missing = Array.isArray(q.missing) ? q.missing.length : 0;
  if (!STATE.quotes) return "報價檔缺席（沿用先前估值）";
  const parts = [`報價 ${STATE.quotes.count || 0} 檔 / ${(STATE.quotes.sessions || []).length} 個交易日`];
  if (missing) parts.push(`待補 ${missing} 檔`);
  if (STATE.quotes.price_basis && STATE.quotes.price_basis !== "raw") {
    parts.push(`價格基準 ${STATE.quotes.price_basis}（尚未與券商對帳）`);
  }
  return parts.join(" · ");
}

// Report 7.4: the long strategy blurb becomes a collapsible summary instead of
// a fixed header eating the top of a 375px screen.
function strategyCardHtml() {
  const card = MODE_CARDS[STATE.meta.mode] || MODE_CARD_DEFAULT;
  return `<details class="strategy ${card.tone}">
    <summary>${esc(card.summary)}</summary>
    <div class="strategy-body">${esc(card.body)}</div>
    <div class="strategy-meta">模式 ${esc(STATE.meta.mode || "未知")}｜策略版本 ${esc(STATE.meta.strategy_version || "未提供")}</div>
  </details>`;
}

// --- 11.2 今日建議 -----------------------------------------------------------
function renderPicks() {
  const rows = filteredRows();
  const ready = rows.filter((r) => buyVerdict(r).ok);
  const others = rows.filter((r) => !buyVerdict(r).ok);
  const reg = regimeView();

  const controls = `
    <div class="controls">
      <div class="chips">${["ALL", "OTC", "TSE"].map((k) =>
        `<button type="button" class="chip ${STATE.market === k ? "on" : ""}" data-act="market" data-market="${k}">${k === "ALL" ? "全部" : k}</button>`).join("")}</div>
      <select class="sort" data-act="sort">${SORTS.map(([label], i) =>
        `<option value="${i}"${i === STATE.sortIndex ? " selected" : ""}>${esc(label)}</option>`).join("")}</select>
      <span class="count">${rows.length} 檔</span>
    </div>
    <input id="search" class="search" type="search" placeholder="搜尋代號 / 名稱" value="${esc(STATE.query)}" data-act="search" />`;

  const banner = `<div class="notice ${reg.enterOk ? "ok" : "warn"}">${esc(reg.text)}${
    reg.asOf ? esc(` · 判定資料日 ${reg.asOf}`) : ""}</div>` +
    exitSignalSummary(rows) +
    `<div class="hint">買進資格由後端 Buy_Ready / Buy_Block 決定，本畫面只會把過期資料降級，不會把「不可買」改成「可買」。每張卡片的「停損（跌破先出）」是唯一要盯的價位。</div>`;

  const readyHtml = ready.length
    ? ready.map(pickCard).join("")
    : `<div class="empty-inline">今日 0 檔符合完整買進規則${reg.enterOk ? "（資料源正常，屬正常空手日）" : "（原因：" + esc(reg.text) + "）"}。</div>`;

  const aiText = STATE.reports[STATE.market] || STATE.reports.ALL || "";
  const ai = `<details class="strategy dim"><summary>AI 報告${STATE.market === "ALL" ? "" : "（" + esc(STATE.market) + "）"}</summary>` +
    `<div class="strategy-body">${esc(aiText || "（本次掃描沒有 AI 報告；於雲端設定 API 金鑰後即會出現）")}</div></details>`;

  document.getElementById("page-picks").innerHTML =
    banner + controls +
    `<div class="sec"><h2>符合買進規則 ${ready.length}</h2>${readyHtml}</div>` +
    `<div class="sec"><h2>其他候選 ${others.length}（附不成立原因）</h2>${
      others.length ? others.map(pickCard).join("") : `<div class="empty-inline">沒有其他候選。</div>`}</div>` +
    ai;

  const box = document.getElementById("search");
  if (box) {
    box.addEventListener("input", () => { STATE.query = box.value; renderPicks(); });
    if (STATE.query) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  }
}

function filteredRows() {
  let rows = STATE.rows;
  if (STATE.market !== "ALL") rows = rows.filter((r) => String(r.Market) === STATE.market);
  const q = STATE.query.trim().toLowerCase();
  if (q) {
    rows = rows.filter((r) => String(r.Stock_ID).includes(q) ||
      String(r.Stock_Name || "").toLowerCase().includes(q));
  }
  const [, key, dir] = SORTS[STATE.sortIndex];
  return rows.slice().sort((a, b) => {
    if (key === "rank") return a._rank - b._rank;
    const av = num(a[key]), bv = num(b[key]);
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return dir === "asc" ? av - bv : bv - av;
  });
}

// --- exit plan columns (scanner/holding_tracker.py, 2026-09-14) -------------
// Plan_Stop is the ONE price to act on: "sell first if it trades below this".
// Before entry it is the close-based reference; once the row is entered it is
// fill x 0.80, raised to fill x 1.02 once a CLOSE at or above fill x 1.025
// arms the lock -- effective the NEXT session. Exit_Signal
// is what the shared exit stack (scanner/exit_rules.py) says already happened.
const EXIT_LABEL = {
  stop: "已跌破停損 · 先出場",
  lock: "鎖利停損觸發 · 出場",
  tp: "達到目標 · 出場",
  late: "後期仍有獲利 · 隔日開盤先收下",
  time: "持有期滿 · 出場",
};

function exitSignalBadge(r) {
  const sig = String(r.Exit_Signal || "");
  if (!sig || !EXIT_LABEL[sig]) return "";
  const when = String(r.Exit_Signal_Date || "").slice(5, 10);
  const px = cents(r.Exit_Signal_Price);
  return `<span class="verdict ${sig === "stop" || sig === "lock" ? "no" : "held"}">⚠ ${esc(EXIT_LABEL[sig])}${
    when ? `（${esc(when)}${px !== null ? " @" + esc(fmtPrice(px)) : ""}）` : ""}</span>`;
}

function planStopKv(r) {
  const stop = cents(r.Plan_Stop);
  const fallback = cents(r.Strict_Stop_Loss);
  const status = String(r.Hold_Status || "");
  if (stop === null) {
    return kv("參考停損", esc(fmtPrice(fallback)), "", "依參考價推算，未成交前非實際風控");
  }
  if (!status || status === "pending") {
    return kv("停損（跌破先出）", esc(fmtPrice(stop)), "", "成交後改以成交價 × 0.80 為準，只升不降");
  }
  const sub = r.Exit_Signal
    ? "此價位為出場當時的有效停損"
    : r.Plan_Armed
      ? "鎖利已啟動：停損已上調到成交價 × 1.02，只升不降"
      : `推估成交價 ${esc(fmtPrice(cents(r.Entry_Open)))} × 0.80；收盤站上 +${STRATEGY.armPct}% 後，隔一個交易日起上調到 +${STRATEGY.lockPct}%`;
  return kv("停損（跌破先出）", esc(fmtPrice(stop)), r.Exit_Signal ? "" : "gold", sub);
}

// --- chip verdict (scanner/chip_signal.py, 2026-09-21) ---------------------
// Inst_Net is today's three-institution net in lots, Inst_Pct the same as a
// share of the 20-day average volume. Chip_Action is the backend's verdict
// for the NEXT open on a held row: "sell" / "add" / "hold"; empty when the
// row is not a position, the flow lags the price data, or no rule is
// validated. The phone renders; it never derives an action of its own.
const CHIP_ACTION_TEXT = {
  sell: "隔日開盤先出場",
  add: "隔日開盤補另一半（分批買法）",
  hold: "續抱，籌碼無動作理由",
};
// 2026-09-21 實測結論（archive/research/sandbox_chip_manage.py）：
// 「法人賣超就隔天賣」勝率 67.1% → 47.4%；「法人買超就加碼」也是負的。
// 原因是訊號全在隔日跳空裡：今日法人買賣超對隔日「收盤對收盤」相關 +0.041
// (t=11.6)，但對隔日「開盤對收盤」只有 -0.003 (t=-0.9)，而你最快只能在隔日
// 開盤成交。所以籌碼在這裡只是確認欄位，不產生動作。
const CHIP_CONFIRM_ONLY = "籌碼僅供確認，不產生買賣動作（實測：照籌碼進出會降低勝率）";

function chipSummary(r) {
  const net = num(r.Inst_Net);
  if (net === null) return "";
  const pct = num(r.Inst_Pct);
  const streak = num(r.Inst_Streak);
  const parts = [`三大法人 ${fmtSigned(net, 0)} 張`];
  if (pct !== null) parts.push(`占均量 ${fmtSigned(pct, 1)}%`);
  if (streak !== null && Math.abs(streak) >= 2) parts.push(`連 ${Math.abs(streak)} 日${streak > 0 ? "買超" : "賣超"}`);
  const d5 = num(r.Inst_Net_5D);
  if (d5 !== null) parts.push(`5日 ${fmtSigned(d5, 0)}`);
  return parts.join(" · ");
}

function chipKv(r) {
  const net = num(r.Inst_Net);
  if (net === null) {
    return kv("外資5日", esc(fmtSigned(r.Foreign_Net_5D, 0)), signClass(r.Foreign_Net_5D), "最近5筆法人資料（張）");
  }
  const basis = String(r.Chip_Basis || "");
  const when = String(r.Inst_Date || "").slice(5, 10);
  const sub = (basis === "lag" || basis === "ahead")
    ? `法人資料 ${when} 與行情日不同，暫無判定`
    : (r.Chip_Action ? CHIP_ACTION_TEXT[r.Chip_Action] || r.Chip_Action : "確認欄位，不進評分") +
      (num(r.Inst_Sessions) !== null && num(r.Inst_Sessions) < 5 ? `（僅 ${num(r.Inst_Sessions)}/5 日有資料）` : "");
  return kv("法人今日", esc(chipSummary(r)), signClass(net), esc(sub));
}

// For a position card: the verdict sentence, or an honest "no data" line.
// The tracked block can be CARRIED FORWARD: a publisher that does not rebuild
// it (the desktop app) keeps the previous one rather than deleting the rows a
// holder depends on. When that happens the block is older than the list, and
// the card has to say so rather than presenting stale chips as today's.
// Returns the session it was built for, or "" when it is current.
function trackedStale() {
  const t = (STATE.meta && STATE.meta.tracked) || null;
  if (!t || !t.carried_forward) return "";
  const built = String(t.built_for || "").slice(0, 10);
  const today = String((STATE.meta && STATE.meta.session_date) || "").slice(0, 10);
  return built && built !== today ? built : "";
}

// A holding is looked up in the list FIRST and in the tracked set second, so
// a name that left the list still has every column it had while it was on it.
// The name of a stock, wherever it is known from: today's list, the recent
// recommendations, the whole-market file, or the quote feed's name map.
function stockName(stockId) {
  const id = String(stockId || "").trim();
  if (!id) return "";
  const r = rowFor(id);
  if (r && r.Stock_Name) return String(r.Stock_Name);
  const q = STATE.quotes && STATE.quotes.names && STATE.quotes.names[id];
  return q ? String(q) : "";
}

function rowFor(stockId) {
  const id = String(stockId);
  return STATE.rows.find((x) => String(x.Stock_ID) === id) ||
         STATE.tracked.find((x) => String(x.Stock_ID) === id) ||
         (STATE.universe && STATE.universe[id]) || null;
}

function chipAdvice(stockId) {
  const r = rowFor(stockId);
  if (!r) return `<div class="plan dim">籌碼：此檔不在今日名單，也不在近期推薦名單，因此沒有當日法人資料。</div>`;
  const net = num(r.Inst_Net);
  if (net === null) return `<div class="plan dim">籌碼：本次掃描沒有這檔的法人資料。</div>`;
  const basis = String(r.Chip_Basis || "");
  if (basis === "lag" || basis === "ahead") {
    // Which side is behind matters: TWSE's whole-market endpoint can publish a
    // session later than the institutional table, so the PRICE can be the
    // stale one. Either way the pair is unmatched and no verdict is given.
    const note = basis === "ahead"
      ? `行情資料只到 ${esc(String(r.Data_Date || "").slice(0, 10))}，比法人資料（${esc(String(r.Inst_Date || "").slice(0, 10))}）舊`
      : `法人資料只到 ${esc(String(r.Inst_Date || "").slice(0, 10))}，比行情資料舊`;
    return `<div class="plan dim">籌碼：${note}，兩者不同日就不做籌碼判定。${esc(chipSummary(r))}</div>`;
  }
  const act = String(r.Chip_Action || "");
  const cls = act === "sell" ? "plan alert" : act === "add" ? "plan armed" : "plan";
  const head = act ? `隔日籌碼動作：${CHIP_ACTION_TEXT[act] || act}` : "籌碼（確認用）";
  const tail = act ? "" : `<br><span class="hint">${esc(CHIP_CONFIRM_ONLY)}</span>`;
  return `<div class="${cls}">${esc(head)} · ${esc(chipSummary(r))}${tail}</div>`;
}

// The optional staged entry (scan_mode.PRELAUNCH_ADD_PCT, 2026-09-20). Shown
// as a plain price with what it is FOR: buying the second half is a choice,
// so the row must never read like an order.
function planAddKv(r) {
  const add = cents(r.Plan_Add_Price);
  // A trade the exit stack has already taken out cannot be added to: showing a
  // buy level under a "已出場" badge would read as an instruction to re-enter.
  if (add === null || r.Exit_Signal) return "";
  const status = String(r.Hold_Status || "");
  const hitOn = String(r.Add_Hit_Date || "").slice(0, 10);
  if (!status || status === "pending") {
    return kv("加碼價（分批買法）", esc(fmtPrice(add)), "",
              "先買一半者在此補另一半；成交後改以成交價 × 0.90 為準");
  }
  if (hitOn) {
    return kv("加碼價（分批買法）", esc(fmtPrice(add)), "gold",
              `${esc(hitOn)} 已到過此價位；一次買滿的話忽略這列`);
  }
  return kv("加碼價（分批買法）", esc(fmtPrice(add)), "",
            "尚未到價 · 一次買滿的話忽略這列");
}

function exitSignalSummary(rows) {
  const hit = rows.filter((r) => r.Exit_Signal && EXIT_LABEL[r.Exit_Signal]);
  if (!hit.length) return "";
  const stops = hit.filter((r) => r.Exit_Signal === "stop" || r.Exit_Signal === "lock").length;
  return noticeHtml(stops ? "err" : "warn",
    `⚠ ${hit.length} 檔已觸發出場訊號（停損/鎖利 ${stops}、目標/期滿 ${hit.length - stops}）· 若持有請先出場，卡片上有日期與價位`);
}

function pickCard(r) {
  const v = buyVerdict(r);
  const sc = rankScore();
  const held = STATE.positions.some((p) =>
    p.stock_id === String(r.Stock_ID) && p.status === "open");

  // Report section 8 / 5.1: the FIXED first-day price and today's recomputed
  // reference are two different facts and must never share a column name.
  const initial = cents(r.Initial_Buy_Price);
  const latestRef = cents(r.Suggested_Buy_Price);
  const close = cents(r.Close_Price);
  const dataDate = String(r.Data_Date || "").slice(0, 10);

  const badge = v.ok
    ? `<span class="verdict ok">可買</span>`
    : `<span class="verdict no">不可買 · ${esc(v.text)}</span>`;

  const grid =
    kv("最新收盤價", esc(fmtPrice(close)), "", dataDate ? `資料日 ${dataDate}` : "") +
    (initial !== null
      ? kv("首日建議價", esc(fmtPrice(initial)), "gold", `固定 · ${esc(String(r.Recommended_On || "").slice(0, 10) || "首次建議日未提供")}`)
      : kv("首日建議價", "-", "", "後端尚未提供固定首日價")) +
    kv("最新觀察參考", esc(fmtPrice(latestRef)), "", "每次掃描重算，非新的買進指令") +
    kv("停損距離%", esc(fmt(r.Risk_Pct, 1, "%")), "", "價格到停損的距離，不是虧損機率") +
    planStopKv(r) +
    planAddKv(r) +
    kv("參考停利目標", esc(fmtPrice(cents(r.Target_Price))), "gold", "條件價，不代表已達成") +
    (cents(r.Scale_Out_Price) === null ? "" :
      kv("可賣一半（選用）", esc(fmtPrice(cents(r.Scale_Out_Price))), "",
         "成交後改以成交價 +15% 為準；選用，會降低平均報酬")) +
    kv(sc.label, esc(fmt(r[sc.key], 1)), "", "規則分數，不是上漲機率") +
    chipKv(r);

  const valid = String(r.Rec_Valid_Until || "").slice(0, 10);
  const recLine = r.Recommendation_ID
    ? `<div class="hint">建議編號 ${esc(r.Recommendation_ID)}｜狀態 ${esc(r.Rec_Status || "未提供")}${valid ? `｜有效至 ${esc(valid)}` : ""}</div>`
    : `<div class="hint">此列尚無固定建議編號（後端未建立 recommendation）。</div>`;

  return `<article class="card pick ${scoreTier(r)} ${v.ok ? "state-buy" : ""}">
    <div class="card-head">
      <span class="rank">${r._rank}</span>
      <span class="name">${esc(r.Stock_Name || r.Stock_ID)}</span>
      <span class="code">${esc(r.Stock_ID)}</span>
      <span class="market">${esc(r.Market || "")}</span>
    </div>
    <div class="badges">${badge}${held ? '<span class="verdict held">已有持倉</span>' : ""}${
      r.Integrity_OK === false ? '<span class="verdict no">資料完整性未通過</span>' : ""}${exitSignalBadge(r)}</div>
    ${recLine}
    ${held && r.Exit_Signal ? `<div class="hint">這個出場訊號算的是<b>系統假設的進場</b>（${esc(String(r.Entry_Date || "").slice(0, 10) || "進場日未知")} 開盤 ${esc(fmtPrice(cents(r.Entry_Open)))}）。你的持倉以<b>你登錄的成交價</b>另外計算，請以「持倉」頁的建議為準。</div>` : ""}
    <div class="kv2">${grid}</div>
    <div class="btns">
      ${btn("buy", "登錄買入", "primary", { id: r.Stock_ID })}
      ${btn("detail", "指標詳情", "", { id: r.Stock_ID })}
    </div>
  </article>`;
}

// --- 11.3 我的持倉 (report 7.3) ----------------------------------------------
function renderPositions() {
  const list = activePositions();
  const pending = pendingItems();
  const pendingIds = new Set(pending.map((p) => p.pos.position_id));

  // Report 7.4: anything expired, data-broken or awaiting a fill is PINNED to
  // the top and can never be hidden by a score or by dropping off the list.
  const bucket = (p) => (pendingIds.has(p.position_id) ? 0 : p.status === "closed" ? 2 : 1);
  list.sort((a, b) => {
    const d = bucket(a) - bucket(b);
    if (d) return d;
    return String(a.opened_session || "").localeCompare(String(b.opened_session || ""));
  });

  const backup = `<div class="notice warn">🔒 持倉與成交只存在這支手機（IndexedDB），清除瀏覽器資料就會消失。請定期「匯出備份」。</div>
    <div class="btns">${btn("export", "匯出備份 JSON", "")}${btn("import", "匯入備份", "")}</div>`;

  const body = list.length
    ? list.map((p) => positionCard(p, pendingIds.has(p.position_id))).join("")
    : `<div class="empty-inline">尚未登錄任何持倉。到「建議」頁選一檔後按「登錄買入」，或按下方手動新增。</div>`;

  document.getElementById("page-positions").innerHTML =
    backup +
    `<div class="btns">${btn("buy", "手動新增持倉", "primary", { id: "" })}</div>` +
    body;
}

function positionCard(pos, pinned) {
  const m = latestMark(pos.position_id);
  const plan = activePlan(pos);
  const horizon = pos.horizon_days || STRATEGY.horizon;
  const listed = STATE.rows.some((r) => String(r.Stock_ID) === pos.stock_id);
  const tracked = !listed && STATE.tracked.some((r) => String(r.Stock_ID) === pos.stock_id);
  const inList = listed || tracked;

  if (pos.needs_shares) {
    return `<article class="card pos state-attention pin">
      <div class="card-head">
        <span class="name">${esc(pos.stock_name && pos.stock_name !== pos.stock_id ? pos.stock_name : (stockName(pos.stock_id) || pos.stock_id))}</span>
        <span class="code">${esc(pos.stock_id)}</span>
        <span class="state-chip attention">待補股數</span>
      </div>
      <div class="notice warn">由舊版手機資料匯入。舊格式沒有股數，系統不會替你猜一個數量，請補登實際成交。</div>
      ${kv("匯入的成交價參考", esc(pos.legacy_fill ? fmtPrice(pos.legacy_fill) : "-"), "",
           pos.legacy_entry ? `舊資料進場日 ${pos.legacy_entry}` : "")}
      <div class="btns">
        ${btn("buy", "補登實際成交", "primary", { pos: pos.position_id })}
        ${btn("void-pos", "撤銷誤登", "", { pos: pos.position_id })}
      </div>
    </article>`;
  }

  // The holding-day count comes from the CALENDAR, not from the price feed:
  // a missing quote should not also cost us the answer to "how long have I
  // held this". Only a date the calendar does not know yields "unknown".
  // A closed position is settled: its P&L is realised, there is nothing left to
  // value and no exit plan to follow. It stays listed until 封存 so that
  // "sold" and "filed away" remain two separate, explicit steps.
  if (pos.status === "closed") {
    return `<article class="card pos state-closed">
      <div class="card-head">
        <span class="name">${esc(pos.stock_name && pos.stock_name !== pos.stock_id ? pos.stock_name : (stockName(pos.stock_id) || pos.stock_id))}</span>
        <span class="code">${esc(pos.stock_id)}</span>
        <span class="state-chip">已平倉 · 待封存</span>
      </div>
      <div class="kv2">
        ${kv("已實現淨損益", esc(fmtPnl(pos.realized_net)), signClass(pos.realized_net), "已扣實際手續費與交易稅")}
        ${kv("持有期間", esc(`${pos.opened_session || "?"} → ${pos.closed_session || "?"}`), "",
             `共 ${dayIndexBetween(pos.opened_session, pos.closed_session, CAL) || "?"} 個交易日`)}
      </div>
      <div class="plan">此筆已無持股，之後的行情不再影響它的損益。封存後仍可在「績效與歷史」查到。</div>
      <div class="btns">
        ${btn("archive", "封存已結束紀錄", "primary", { pos: pos.position_id })}
        ${btn("execs", "補登／更正成交", "", { pos: pos.position_id })}
        ${btn("cycle", `${horizon}日明細`, "", { pos: pos.position_id })}
        ${btn("pos-delete", "刪除這筆紀錄", "danger", { pos: pos.position_id })}
      </div>
    </article>`;
  }

  const dayIdx = (m && m.day_index !== null)
    ? m.day_index
    : dayIndexBetween(pos.opened_session, sessionOnOrBefore(taipeiDate(new Date())), CAL);
  let stateText, stateCls;
  if (dayIdx === null) { stateText = "持有中 · 天數未知（日期不在已知交易日曆內）"; stateCls = "attention"; }
  else if (dayIdx > horizon) { stateText = `已超過計畫 · D${dayIdx} / ${horizon}`; stateCls = "overdue"; }
  else if (dayIdx === horizon) { stateText = `第 ${horizon} 天已到 · 待登錄賣出`; stateCls = "exit"; }
  else { stateText = `持有中 · D${dayIdx} / ${horizon}`; stateCls = "holding"; }

  // With no close there is no valuation, and "持平 +0 元" would be a lie of the
  // exact kind report 4.3 forbids: a missing bar is not a flat day.
  const priced = !!(m && m.close_price !== null);
  const cum = priced ? m.total_gross : null;
  const cumPct = (priced && pos.avg_cost)
    ? pctOf(m.close_price - pos.avg_cost, pos.avg_cost) : null;
  const day = priced ? m.day_pnl_gross : null;

  const pl = `<div class="pl-row">
    <div class="pl">
      <span class="pl-l">累計損益（價差）</span>
      <span class="pl-v ${signClass(cum)}">${cum === null ? "無法估值" : esc(fmtPnl(cum))}</span>
      <span class="pl-s">${cumPct === null ? "此檔無收盤價，僅已實現部分為確定值"
        : esc(fmtPct(cumPct)) + "（未扣未來賣出費稅）"}</span>
    </div>
    <div class="pl">
      <span class="pl-l">今日損益</span>
      <span class="pl-v ${signClass(day)}">${day === null ? "無法估值" : esc(fmtPnl(day))}</span>
      <span class="pl-s">${m ? esc(`估值：${m.session_date} ${DATA_STATUS_TEXT[m.data_status] || ""}`) : "尚無估值"}</span>
    </div>
  </div>`;

  const grid =
    kv("實際成本（移動加權平均）", esc(fmtPrice(pos.avg_cost)), "gold",
       `帳面成本 ${fmtCents(pos.cost_basis)} 元（含買入手續費）`) +
    kv("持有股數", esc(pos.open_shares.toLocaleString("en-US")) + " 股",
       "", `${(pos.open_shares / 1000).toFixed(pos.open_shares % 1000 ? 3 : 0)} 張`) +
    kv("最新收盤價", m && m.close_price !== null ? esc(fmtPrice(m.close_price)) : "-", "",
       m ? `${m.session_date}｜${DATA_STATUS_TEXT[m.data_status] || m.data_status}` : "無報價") +
    kv("首日建議價", pos.initial_buy_price ? esc(fmtPrice(pos.initial_buy_price)) : "-", "",
       pos.initial_buy_price ? "固定，不隨掃描改動" : "此持倉未連結固定建議") +
    kv("有效停損", plan ? esc(fmtPrice(plan.stop_orderable)) : "-", "",
       plan ? `可直接掛單（已對齊升降單位）` : "") +
    kv("加碼價（分批買法）", plan && plan.add_open ? esc(fmtPrice(plan.add_orderable)) : "-", "",
       plan
         ? (plan.add_open
             ? `第一筆成交價 ${fmtPrice(plan.base)} × 0.90，已對齊升降單位；一次買滿就不用`
             : "此筆已有兩次以上買進，不再加碼")
         : "") +
    kv("停利目標", plan ? esc(fmtPrice(plan.target_orderable)) : "-", "gold",
       plan ? "條件價 · 已對齊升降單位" : "") +
    kv("已實現淨損益", esc(fmtPnl(pos.realized_net)), signClass(pos.realized_net), "已扣實際費稅") +
    kv("若今日全數賣出", m && m.net_if_liquidated !== null ? esc(fmtPnl(m.net_if_liquidated)) : "-",
       m && m.net_if_liquidated !== null ? signClass(m.net_if_liquidated) : "",
       "估計值，扣掉賣出費稅");

  // Report 5.4: "+2% 這個數字不代表已經保住該獲利" -- the threshold is a
  // CONDITION, and armed / not-armed must look obviously different.
  const trail = plan
    ? (plan.armed
        ? `<div class="plan armed">鎖利：已啟動（收盤曾達 ${esc(fmtPrice(plan.highest_close))}，${esc(plan.armed_on)}）· 有效停損已上調至 ${esc(fmtPrice(plan.stop_orderable))}，只升不降</div>`
        : `<div class="plan">鎖利：尚未啟動；需要<b>收盤</b>站上 ${esc(fmtPrice(tickRound(plan.arm, "up", plan.stock_id)))}（條件，非已達成），隔一個交易日起生效 · 未啟動前停損維持 ${esc(fmtPrice(tickRound(plan.initial_stop, "down", plan.stock_id)))}</div>`)
    : "";

  // "續抱" on its own is what the 2026-09-17 complaint was about: a position
  // 10% under water read exactly like one 10% up. The prices that decide what
  // to do next belong in the sentence.
  const holdLine = plan
    ? `建議（以你登錄的成交價 ${fmtPrice(plan.base)} 計算）：續抱。跌破 ${fmtPrice(plan.stop_orderable)} 先出場${
        plan.add_open ? `；分批買法可在 ${fmtPrice(plan.add_orderable)} 補另一半` : ""
      }；${plan.armed ? "鎖利已啟動" : `收盤站上 ${fmtPrice(tickRound(plan.arm, "up", plan.stock_id))} 後，隔一個交易日起停損上調到 ${fmtPrice(tickRound(plan.lock, "down", plan.stock_id))}`}。`
    : "建議：續抱，下一個交易日重新評估（收盤後更新）。";
  // Everything above is computed from what YOU registered -- your fill price,
  // your fill date -- and the market data is only used to say where the stock
  // is relative to those levels (owner, 2026-09-21).
  const mkt = rowFor(pos.stock_id);
  const marketLine = !mkt ? "" : (() => {
    // Prices in this app are integer cents; cents() is the only correct way in.
    const ma5 = cents(mkt.MA5), close = cents(mkt.Close_Price);
    const bits = [];
    if (close !== null) bits.push(`收盤 ${fmtPrice(close)}（資料日 ${esc(String(mkt.Data_Date || "").slice(5, 10))}）`);
    if (ma5 !== null && close !== null) {
      bits.push(`5 日均價 ${fmtPrice(ma5)}，${close > ma5 ? "站上（到期可續抱）" : "跌破（到期就出場）"}`);
    } else if (num(mkt.Bars) !== null && num(mkt.Bars) < 5) {
      // A newly covered instrument (every ETF, the day whole-market storage
      // began) has a price but not yet an average. Say which, rather than
      // leaving a gap that looks like a fault.
      bits.push(`目前只有 ${num(mkt.Bars)} 根日 K，均價還算不出來`);
    }
    const inst = num(mkt.Inst_Net);
    if (inst !== null) bits.push(`三大法人 ${fmtSigned(inst, 0)} 張`);
    return bits.length
      ? `<div class="plan dim">個股現況：${esc(bits.join(" · "))}</div>` : "";
  })();

  const advice = dayIdx === null
    ? `<div class="plan">建議：無法計算持有天數，請確認成交日期。</div>`
    : dayIdx >= horizon
      ? `<div class="plan alert">建議：第 ${horizon} 個交易日已到。收盤若仍站上自己的 5 日均價就續抱（最晚第 ${STRATEGY.cap} 天），否則依策略於收盤出場；實際賣出以你的成交回報為準。</div>`
      : `<div class="plan">${esc(holdLine)}</div>`;

  return `<article class="card pos state-${stateCls}${pinned ? " pin" : ""}">
    <div class="card-head">
      <span class="name">${esc(pos.stock_name && pos.stock_name !== pos.stock_id ? pos.stock_name : (stockName(pos.stock_id) || pos.stock_id))}</span>
      <span class="code">${esc(pos.stock_id)}</span>
      <span class="state-chip ${stateCls}">${esc(stateText)}</span>
    </div>
    ${listed ? "" : (tracked
        ? (trackedStale()
            ? `<div class="hint warn">已不在今日名單 · 這份追蹤資料是 ${esc(trackedStale())} 那一次掃描留下的，尚未跟著今天更新</div>`
            : `<div class="hint">已不在今日名單，但仍在近期推薦追蹤中 · 法人、均線、出場計畫照常更新</div>`)
        : mkt
          ? `<div class="hint">系統沒有推薦過這檔 · 仍從當日全市場掃描結果取得法人與均線資料</div>`
          : STATE.universeState === "loading"
            ? `<div class="hint">正在取得這檔的全市場資料…</div>`
            : `<div class="hint">這檔不在當日掃描範圍內（成交值太小），只能用報價估值；停損與出場日仍照你登錄的成交價計算</div>`)}
    ${pl}
    <div class="kv2">${grid}</div>
    ${trail}
    ${marketLine}
    ${advice}
    ${chipAdvice(pos.stock_id)}
    ${tomorrowOrders(pos, plan, dayIdx, horizon)}
    <div class="btns">
      ${btn("sell", "登錄賣出", "primary", { pos: pos.position_id })}
      ${btn("execs", "補登／更正成交", "", { pos: pos.position_id })}
      ${btn("cycle", `${horizon}日明細`, "", { pos: pos.position_id })}
      ${pos.open_shares === 0 ? btn("archive", "封存已結束紀錄", "", { pos: pos.position_id }) : ""}
      ${btn("pos-delete", "刪除這筆紀錄", "danger", { pos: pos.position_id })}
    </div>
  </article>`;
}

// --- 11.4 績效與歷史 ---------------------------------------------------------
function renderPerf() {
  const closed = STATE.positions.filter((p) => p.status === "closed" || p.status === "archived");
  const voided = STATE.positions.filter((p) => p.status === "void");
  // loadCycles keys by position_id with no status filter, so this table could
  // show a profit for a position the section below declares 不計入損益.
  const cycles = Object.values(STATE.cycles).filter((c) => {
    const p = STATE.positions.find((x) => x.position_id === c.position_id);
    return p && p.status !== "void";
  });
  const s = portfolioSummary();

  const cycleRows = cycles.length ? `<table class="tbl">
    <thead><tr><th>股票</th><th>估值日</th><th>D</th><th>價差損益</th><th>相對首日建議</th><th>相對實際成本</th><th>狀態</th></tr></thead>
    <tbody>${cycles.map((c) => `<tr>
      <td>${esc(c.stock_name || c.stock_id)}<br><span class="sm">${esc(c.stock_id)}</span></td>
      <td>${esc(c.session_date)}</td>
      <td>${c.day_index === null ? "-" : c.day_index}</td>
      <td class="${signClass(c.total_gross)}">${esc(fmtCents(c.total_gross, { signed: true }))}</td>
      <td>${esc(fmtPct(c.return_vs_initial))}</td>
      <td>${esc(fmtPct(c.return_vs_cost))}</td>
      <td>${c.still_open ? "仍持有" : "已平倉"}${c.restated_at ? " · 已更正" : ""}${
        c.rebuild_failed ? " · 十日成果未重算"
          : (c.valuation_stale ? " · 估值日維持原凍結" : "")}</td>
    </tr>`).join("")}</tbody></table>
    <div class="hint">十日成果在第 ${STRATEGY.horizon} 個交易日凍結，之後的價格不會改寫它；仍持有的部位另在「持倉」頁繼續估值。</div>`
    : `<div class="empty-inline">尚無已凍結的十日成果（需要持倉滿 ${STRATEGY.horizon} 個交易日，或提前平倉）。</div>`;

  const closedRows = closed.length ? `<table class="tbl">
    <thead><tr><th>股票</th><th>期間</th><th>已實現淨損益</th><th>狀態</th></tr></thead>
    <tbody>${closed.map((p) => `<tr>
      <td>${esc(p.stock_name || p.stock_id)}<br><span class="sm">${esc(p.stock_id)}</span></td>
      <td>${esc(p.opened_session || "?")} → ${esc(p.closed_session || "?")}</td>
      <td class="${signClass(p.realized_net)}">${esc(fmtPnl(p.realized_net))}</td>
      <td>${p.status === "archived" ? "已封存" : "已平倉"}<div class="btns">${
        btn("execs", "更正", "", { pos: p.position_id })}${
        btn("pos-delete", "刪除", "danger", { pos: p.position_id })}</div></td>
    </tr>`).join("")}</tbody></table>`
    : `<div class="empty-inline">尚無已平倉紀錄。</div>`;

  const gapNote = s.valuation_complete ? ""
    : `<div class="notice warn">估值不完整：${esc(s.stale.map((x) =>
        `${x.pos.stock_id}${x.as_of ? "（沿用 " + x.as_of + "）" : "（無估值）"}`).join("、"))}</div>`;

  document.getElementById("page-perf").innerHTML =
    gapNote +
    `<div class="sec"><h2>已凍結的十日成果</h2>${cycleRows}</div>` +
    `<div class="sec"><h2>已平倉／封存</h2>${closedRows}` +
    `<div class="hint">封存只是把紀錄移出持倉清單，金額仍計入帳戶總額。按「更正」可以修改成交價、股數、日期，或撤銷誤登；舊版本一律保留。更正後若又有持股，這筆會自動移回「持倉」。</div></div>` +
    (voided.length ? `<div class="sec"><h2>已撤銷（誤登）</h2><div class="hint">資料保留、不計入損益：${
      esc(voided.map((p) => p.stock_id).join("、"))}</div></div>` : "") +
    `<div class="sec"><h2>建議 vs 實際</h2><div class="hint">
      「相對首日建議」用固定的 Initial_Buy_Price 當分母，「相對實際成本」用你的移動加權平均成本。
      兩個數字都可能是對的，但不能共用同一個標題（報告 6.2）。</div></div>`;
}

// --- 11.4 雲端更新（workflow_dispatch）-----------------------------------------
//
// GitHub's own cron lands a median ~2h and up to 12h late (docs/排程與即時行情.md),
// so the phone can start the scan itself. It calls workflow_dispatch with a
// fine-grained token that holds Actions: read/write on this one repo and nothing
// else -- it cannot rewrite app.js, which is the reason dispatch was chosen over
// repository_dispatch. The token lives ONLY in this device's IndexedDB: it is
// never rendered back, never exported in a backup, never accepted from an import.
const GH_API = "https://api.github.com/repos/luke820001/yentool/actions";
const GH_WORKFLOW = "scan.yml";
// Key-less fallback: GitHub's own run page for this workflow (needs only a
// GitHub login in the phone browser, no token anywhere).
const GH_RUN_PAGE = "https://github.com/luke820001/yentool/actions/workflows/" + GH_WORKFLOW;
const GH_TOKEN_KEY = "gh_dispatch_token";
const AUTO_KEY = "auto_dispatch";
const PRIVATE_META = new Set([GH_TOKEN_KEY, AUTO_KEY]);
const EOD_READY_MIN = 15 * 60;         // same as scan-timer's first attempt (TPEX margin)
const AUTO_MAX_PER_SESSION = 2;        // a holiday or a feed outage must not loop
const AUTO_GAP_MS = 90 * 60 * 1000;
const POLL_MS = 15000;
const POLL_LIMIT_MS = 25 * 60 * 1000;

const REFRESH = { busy: false, text: "", tone: "info", runUrl: "", hasToken: false };

function setRefresh(text, tone, runUrl) {
  REFRESH.text = text;
  REFRESH.tone = tone || "info";
  if (runUrl !== undefined) REFRESH.runUrl = runUrl;
  if (STATE.page === "research") renderResearch();
}

// The newest session whose close should already be published. Weekends are
// skipped; exchange holidays are not knowable here, which is why auto-dispatch
// is capped per session rather than trusted to stop by itself.
function expectedSession(now) {
  let date = taipeiDate(now);
  let ready = taipeiMinutes(now) >= EOD_READY_MIN;
  for (let i = 0; i < 7; i++) {
    const dow = new Date(date + "T00:00:00Z").getUTCDay();
    if (ready && dow !== 0 && dow !== 6) return date;
    const d = new Date(date + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - 1);
    date = d.toISOString().slice(0, 10);
    ready = true;
  }
  return date;
}

function dataIsStale() {
  const have = effectiveDataDate();
  return !have || have < expectedSession(new Date());
}

async function ghFetch(path, init) {
  const token = await metaGet(GH_TOKEN_KEY, "");
  if (!token) throw new Error("尚未設定更新金鑰");
  const res = await fetch(GH_API + path, Object.assign({ cache: "no-store" }, init, {
    headers: {
      Authorization: "Bearer " + token,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
    },
  }));
  if (res.status === 401) throw new Error("金鑰無效或已過期，請重新設定");
  if (res.status === 403 || res.status === 404) {
    throw new Error(`金鑰權限不足（HTTP ${res.status}），需要 yentool 的 Actions: Read and write`);
  }
  if (!res.ok) throw new Error("GitHub 回應 HTTP " + res.status);
  return res.status === 204 ? null : res.json();
}

async function latestRuns() {
  const data = await ghFetch(`/workflows/${GH_WORKFLOW}/runs?per_page=5`);
  return (data && data.workflow_runs) || [];
}

// manual=true: the button. manual=false: the stale-data check after a load.
async function cloudRefresh(manual) {
  if (REFRESH.busy) { if (manual) toast("更新已在進行中"); return; }
  if (!DB_OK) { if (manual) toast("此瀏覽器無法儲存金鑰，無法從手機觸發更新"); return; }
  const token = await metaGet(GH_TOKEN_KEY, "");
  REFRESH.hasToken = !!token;
  if (!token) {
    // No key on this phone. Nothing here can mint one (a token is a GitHub
    // credential; only the account owner can create it), so fall back to the
    // path that needs no key at all: GitHub's own "Run workflow" page, which
    // works with the GitHub login already in the phone's browser. The daily
    // update itself never needed a key -- scan-timer runs it in the cloud.
    if (manual) {
      window.open(GH_RUN_PAGE, "_blank", "noopener");
      setRefresh("已開啟 GitHub 的「Run workflow」頁：登入後按右側綠色 Run workflow → Run workflow，約 3～4 分鐘後回來下拉重新整理即可。不需要金鑰。", "info", GH_RUN_PAGE);
    }
    return;
  }

  REFRESH.busy = true;
  const before = effectiveDataDate();
  try {
    setRefresh("檢查雲端掃描狀態…", "info");
    let runs = await latestRuns();
    let run = runs.find((r) => r.status !== "completed");
    if (run) {
      setRefresh("雲端已有掃描在執行，等待完成…", "info", run.html_url);
    } else {
      const since = Date.now() - 60000;
      await ghFetch(`/workflows/${GH_WORKFLOW}/dispatches`, {
        method: "POST", body: JSON.stringify({ ref: "main" }),
      });
      setRefresh("已觸發雲端掃描，約 3～4 分鐘完成…", "info", "");
      // A dispatch returns no run id; find the run it created by time.
      const findUntil = Date.now() + 2 * 60000;
      while (!run && Date.now() < findUntil) {
        await new Promise((r) => setTimeout(r, 5000));
        runs = await latestRuns();
        run = runs.find((r) => r.event === "workflow_dispatch" && Date.parse(r.created_at) >= since);
      }
      if (!run) throw new Error("已送出，但 2 分鐘內沒看到執行出現，請到 GitHub Actions 確認");
      setRefresh("雲端掃描執行中…", "info", run.html_url);
    }

    const started = Date.now();
    while (run.status !== "completed") {
      if (Date.now() - started > POLL_LIMIT_MS) throw new Error("等待超過 25 分鐘，請到 GitHub Actions 確認");
      await new Promise((r) => setTimeout(r, POLL_MS));
      run = await ghFetch(`/runs/${run.id}`);
      const mins = Math.max(1, Math.round((Date.now() - Date.parse(run.created_at)) / 60000));
      setRefresh(`雲端掃描執行中（${run.status === "queued" ? "排隊中" : "第 " + mins + " 分鐘"}）…`, "info", run.html_url);
    }
    if (run.conclusion !== "success") {
      throw new Error(`雲端掃描失敗（${run.conclusion}），請查看執行紀錄`);
    }
    // The deploy job is part of the run, but the CDN can trail it by seconds.
    await new Promise((r) => setTimeout(r, 8000));
    await load();
    const after = effectiveDataDate();
    if (after && after > (before || "")) {
      setRefresh(`更新完成：行情資料日 ${after}`, "ok", run.html_url);
      toast("資料已更新到 " + after);
    } else if (dataIsStale()) {
      // Exit code 1 in the workflow: feed not published yet, old data kept.
      setRefresh(`掃描跑完，但資料源還沒有 ${expectedSession(new Date())} 的收盤（可能是休市，或交易所尚未發布）。資料維持 ${after || "未知"}。`, "warn", run.html_url);
    } else {
      setRefresh(`已是最新：行情資料日 ${after}`, "ok", run.html_url);
    }
  } catch (e) {
    setRefresh("更新失敗：" + (e.message || String(e)), "err");
    if (manual) toast("更新失敗：" + (e.message || String(e)));
  } finally {
    REFRESH.busy = false;
    if (STATE.page === "research") renderResearch();
  }
}

// Runs after every load. Cheap when the data is fresh (no network at all).
async function maybeAutoRefresh() {
  if (REFRESH.busy || !DB_OK || !dataIsStale()) return;
  if (!(await metaGet(GH_TOKEN_KEY, ""))) return;
  const session = expectedSession(new Date());
  const rec = await metaGet(AUTO_KEY, {});
  const count = rec.session === session ? rec.count || 0 : 0;
  if (count >= AUTO_MAX_PER_SESSION) return;
  if (rec.session === session && Date.now() - (rec.at || 0) < AUTO_GAP_MS) return;
  await metaSet(AUTO_KEY, { session, count: count + 1, at: Date.now() });
  cloudRefresh(false);
}

function openTokenForm() {
  openModal("設定更新金鑰", `
    <div class="hint">手機要能直接啟動雲端掃描，需要一把只給這個 repo「觸發 Actions」權限的 GitHub 金鑰。</div>
    <div class="hint">建立位置：github.com/settings/personal-access-tokens/new<br>
      Repository access → Only select repositories → <b>yentool</b><br>
      Repository permissions → <b>Actions: Read and write</b>，其他一律不要給。</div>
    <div class="notice warn">金鑰只存在這支手機，不會上傳、不會出現在備份檔。到期後按鈕會顯示「金鑰無效」，記得換新。</div>
    ${field("token", "金鑰（github_pat_…）", "", { type: "password", attrs: 'autocomplete="off" autocapitalize="off" spellcheck="false"' })}`, {
    submit: "token-save", submitLabel: "儲存並更新",
  });
}

async function saveToken() {
  const modal = $("#modal");
  const input = modal.querySelector("[name=token]");
  const err = modal.querySelector("[data-err=token]");
  const token = String((input && input.value) || "").trim();
  if (!/^(github_pat_|ghp_)[A-Za-z0-9_]{20,}$/.test(token)) {
    err.textContent = "格式不符，應以 github_pat_ 開頭";
    return;
  }
  await metaSet(GH_TOKEN_KEY, token);
  REFRESH.hasToken = true;
  closeModal();
  toast("金鑰已儲存");
  await cloudRefresh(true);
}

async function clearToken() {
  if (!confirm("刪除這支手機上的更新金鑰？之後需要重新設定才能從手機更新。")) return;
  await dbDelete("meta", GH_TOKEN_KEY);
  REFRESH.hasToken = false;
  setRefresh("", "info", "");
  toast("已刪除金鑰");
}

function refreshBlockHtml() {
  const have = effectiveDataDate() || "未知";
  const want = expectedSession(new Date());
  const stale = dataIsStale();
  const status = REFRESH.text
    ? `<div class="notice ${esc(REFRESH.tone)}">${esc(REFRESH.text)}${REFRESH.runUrl
        ? `<br><a href="${esc(REFRESH.runUrl)}" target="_blank" rel="noopener">查看執行紀錄</a>` : ""}</div>`
    : "";
  return `<div class="sec"><h2>資料更新</h2>` +
    drow("目前行情資料日", esc(have)) +
    drow("應有資料日", esc(want) + (stale ? "（落後）" : "（最新）")) +
    status +
    `<div class="btns">${REFRESH.busy
      ? `<button type="button" class="btn primary" disabled>更新中…</button>`
      : btn("cloud-refresh", "立即更新", "primary")}${
      REFRESH.hasToken ? btn("token-clear", "刪除金鑰", "") : btn("token-set", "進階：設定金鑰", "")}</div>
    <div class="hint">${REFRESH.hasToken
      ? `自動更新：開啟 App 時若資料落後，會自動觸發雲端掃描（每個交易日最多 ${AUTO_MAX_PER_SESSION} 次，間隔至少 90 分鐘）。`
      : "每個交易日 15:00 起雲端自動掃描並自我檢測（scan-timer），不需要電腦、不需要金鑰。想提前手動重掃：按「立即更新」會開啟 GitHub 的 Run workflow 頁，用手機上的 GitHub 登入按一下即可；只有想在 App 內一鍵觸發、自動輪詢時才需要設定金鑰。"}</div>
  </div>`;
}

// --- 11.5 研究與資料狀態 ------------------------------------------------------
function renderResearch() {
  const m = STATE.meta;
  const q = STATE.quotes;
  const test = selfTest();
  const reg = m.regime || {};

  const quality = m.quality && Object.keys(m.quality).length
    ? Object.entries(m.quality).map(([k, v]) =>
        drow(k, esc(typeof v === "object" ? JSON.stringify(v) : String(v)))).join("")
    : `<div class="hint">本次掃描沒有附品質欄位。</div>`;

  // Per-column self-check written by the cloud (meta.checks). Every column of
  // scan_result.json, quotes.json and recommendations.json is audited against
  // a registry and the cross-column identities before the file is published.
  const chk = m.checks;
  const chkTone = { ok: "ok", warn: "warn", fail: "err" };
  const chkLabel = { ok: "通過", warn: "有警告", fail: "未通過" };
  const chkLevel = { error: "錯誤", warn: "警告", info: "資訊" };
  const checksBlock = chk
    ? `<div class="notice ${chkTone[chk.status] || "info"}">狀態：${esc(chkLabel[chk.status] || chk.status)}｜${
        chk.rows} 列 × ${chk.columns} 欄｜${chk.errors} 錯誤、${chk.warnings} 警告｜檢查於 ${esc(chk.checked_at || "?")}</div>` +
      ((chk.items || []).length
        ? `<table class="tbl"><thead><tr><th>等級</th><th>項目</th><th>欄位</th><th>筆數</th><th>說明</th></tr></thead><tbody>${
            chk.items.map((it) => `<tr class="${it.level === "error" ? "neg" : ""}"><td>${esc(chkLevel[it.level] || it.level)}</td><td>${esc(it.code)}</td><td>${esc(it.column || "-")}</td><td>${it.count}</td><td>${esc(it.detail || "")}${
              it.sample && it.sample.length ? `<br><span class="hint">例：${esc(it.sample.slice(0, 5).join("、"))}</span>` : ""}</td></tr>`).join("")
          }</tbody></table>`
        : `<div class="hint">全部欄位通過，沒有任何發現。</div>`) +
      `<div class="hint">「錯誤」= 型別、空值或欄位間恆等式被破壞，雲端 scan-timer 會視為未完成並自動重掃；「警告」= 數值超出常見範圍或報價檔有缺口，資料仍可用但請留意。</div>`
    : `<div class="hint">本次掃描沒有附欄位自我檢測結果（舊版雲端流程）。</div>`;

  const quotesBlock = q
    ? drow("報價檔", `${esc(q.as_of || "?")}｜${q.count || 0} 檔｜${(q.sessions || []).length} 個交易日`) +
      drow("涵蓋持倉", esc(String(q.tracked_count === undefined ? "未提供" : q.tracked_count))) +
      drow("價格基準", `${esc(q.price_basis || "未標示")}${
        q.price_basis === "unverified" ? "（尚未與券商對帳，僅供估值）" : ""}`) +
      drow("缺漏", esc((q.missing || []).join("、") || "無"))
    : `<div class="notice warn">quotes.json 不可用（${esc(STATE.quotesError || "未知")}）。持倉估值沿用先前存在裝置上的 marks，日期見各卡片。</div>`;

  const calBlock =
    drow("已知交易日", `${CAL.length} 天${CAL.length ? `（${esc(CAL[0])} → ${esc(CAL_LAST)}）` : ""}`) +
    drow("今日（台北時區）", esc(taipeiDate(new Date()))) +
    drow("對應最後交易日", esc(sessionOnOrBefore(taipeiDate(new Date())) || "未知")) +
    drow("超出日曆之後的日期", "顯示為未知，不外推平日（F14）");

  const testBlock = `<div class="notice ${test.pass ? "ok" : "err"}">
      內建計算自我檢查（報告 6.2 例子）：${test.pass ? "通過" : "失敗"}</div>
    <table class="tbl"><thead><tr><th>項目</th><th>應為</th><th>實際</th></tr></thead><tbody>
    ${test.checks.map((c) => `<tr class="${c.ok ? "" : "neg"}"><td>${esc(c.name)}</td><td>${esc(c.want)}</td><td>${esc(c.got)}</td></tr>`).join("")}
    </tbody></table>
    <div class="hint">此檢查在你的手機上實際跑一次持倉引擎：建議價 100、成交 102、1,000 股，10 天收盤
      103,101,105,106,104,108,107,110,109,112。它同時驗證「不可把每日累計值相加」——相加會得到 45,000。</div>`;

  const glossary = [
    ["最新收盤價", "本次分析序列的最新收盤，附資料日期；不是盤中價"],
    ["首日建議價", "第一次通過完整買進規則時固定下來，之後不再改動"],
    ["最新觀察參考", "每次掃描重算的參考價，不是新的買進指令"],
    ["停損距離%", "價格到停損價的距離，不是虧損機率，也不是帳戶風險"],
    ["停損（跌破先出）", "第一筆成交價 × 0.80，鎖利啟動後上調到 × 1.02，只升不降"],
    ["加碼價（分批買法）", "第一筆成交價 × 0.90。只有「先買一半」的買法要用；一次買滿就忽略"],
    ["明日委託", "收盤後就把隔天要掛的單算好：停利、選用的賣一半、鎖利啟動門檻、停損、選用的加碼。每個價位都已對齊台股升降單位，可以直接掛"],
    ["鎖利啟動", "要「收盤」站上成交價 +2.5%，而且是隔一個交易日才生效——因為你收盤後才看得到，隔天才下得了單"],
    ["續抱（到期不賣）", "第 10 天收盤若仍站上自己的 5 日均價就續抱，最晚第 20 天"],
    ["後期收下獲利", "第 8 天起，收盤只要還高於成交價 +1%（扣掉費稅後仍為正）就隔日開盤出場。實測：期滿才出場的那一群平均 -6.9%，是整套規則唯一的虧損來源"],
    ["20日平均日振幅%", "20日平均 (最高-最低)/收盤，未含前收跳空，故不等於標準 ATR"],
    ["通道上緣接近", "壓縮區間且收盤接近前40日高的97%，不一定真的突破"],
    ["近3日均量/20日均量", "量能萎縮比，不是當日單日量縮"],
    ["距近一年最高收盤", "比較約252筆的收盤最高，不是盤中歷史最高"],
    ["起漲條件分 / 動能分 / 盤整蓄勢分", "都是規則分數，不是上漲機率"],
    ["上漲日量能占比", "上漲日成交量佔上漲＋下跌日成交量的比例，不等於主力吸籌證據"],
    ["法人今日 / 合計占均量%", "當日三大法人買賣超（張）除以 20 日均量；資料日必須等於行情日才做判定"],
    ["隔日籌碼動作", "目前一律空白：2026-09-21 用 562 筆交易實測，照法人買賣超決定隔天賣出或加碼，勝率反而從 67.1% 掉到 47.4%。原因是訊號全在隔日跳空裡（對隔日收盤相關 +0.041、對隔日開盤後那段只有 -0.003），而你最快只能在隔日開盤成交。籌碼因此只當確認欄位"],
  ].map(([k, v]) => drow(k, esc(v))).join("");

  document.getElementById("page-research").innerHTML =
    refreshBlockHtml() +
    `<div class="sec"><h2>資料狀態</h2>` +
      drow("模式", esc(m.mode || "未知")) +
      drow("策略版本", esc(m.strategy_version || "未提供")) +
      drow("掃描時間", esc(m.scan_time || "未知")) +
      drow("行情資料日", esc(effectiveDataDate() || "未知") + (m.data_date ? "" : "（取自個股 Data_Date）")) +
      drow("交易日 session", esc(m.session_date || "未知")) +
      drow("入選檔數", esc(String(m.count === undefined ? STATE.rows.length : m.count))) +
      drow("資料源異常", esc(m.degraded || "無")) +
      quotesBlock +
    `</div>` +
    `<div class="sec"><h2>大盤判定</h2>` +
      drow("可否開新倉", reg.enter_ok ? "是" : "否") +
      drow("判定資料日", esc(reg.as_of_date || "未提供")) +
      drow("判定是否為最新", reg.is_current === false ? "否（不視為順風）" : reg.is_current === true ? "是" : "未提供") +
      drow("說明", esc(reg.text || "-")) +
    `</div>` +
    `<div class="sec"><h2>交易日曆</h2>${calBlock}</div>` +
    `<div class="sec"><h2>掃描品質欄位</h2>${quality}</div>` +
    `<div class="sec"><h2>欄位自我檢測（雲端）</h2>${checksBlock}</div>` +
    `<div class="sec"><h2>自我檢查</h2>${testBlock}</div>` +
    `<div class="sec"><h2>資料管理</h2>
      <div class="hint">持倉資料只存在本機瀏覽器，換手機或清除資料都會消失。</div>
      <div class="btns">${btn("export", "匯出備份 JSON", "primary")}${btn("import", "匯入備份", "")}</div>
      <div class="hint">費率設定：${esc(schedule(STATE.settings.fee_schedule).label)}</div>
      <div class="btns">${btn("fees", "切換費率版本", "")}</div>
    </div>` +
    `<div class="sec"><h2>欄位含義（報告第 8 節）</h2>${glossary}</div>` +
    strategyCardHtml() +
    `<div class="sec"><h2>免責</h2><div class="hint">所有分數與價位都是規則計算結果，不是報酬保證。「隔日開盤進場」是計畫，實際成交依券商回報；集合競價只接受限價委託，不保證以開盤價成交。</div></div>`;
}

/* ============================================================================
 * 12. Modal, forms and actions
 * ==========================================================================*/

function openModal(title, bodyHtml, opts) {
  const o = opts || {};
  const modal = $("#modal");
  modal.innerHTML = `<div class="sheet" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="sheet-head"><h3>${esc(title)}</h3>
      <button type="button" class="x" data-act="close">✕</button></div>
    <div class="sheet-body">${bodyHtml}</div>
    ${o.noFoot ? "" : `<div class="sheet-foot">${
      btn("close", "取消", "")}${o.submit ? btn(o.submit, o.submitLabel || "儲存", "primary", o.submitData || {}) : ""}</div>`}
  </div>`;
  modal.hidden = false;
  if (o.onOpen) o.onOpen(modal);
}

function closeModal() {
  const modal = $("#modal");
  modal.hidden = true;
  modal.innerHTML = "";
}

function field(name, label, value, opts) {
  const o = opts || {};
  return `<label class="field">
    <span class="f-l">${esc(label)}</span>
    <input class="f-i" name="${esc(name)}" type="${o.type || "text"}"
      inputmode="${o.inputmode || "text"}" value="${esc(value === null || value === undefined ? "" : value)}"
      placeholder="${esc(o.placeholder || "")}" ${o.attrs || ""} />
    ${o.hint ? `<span class="f-h">${esc(o.hint)}</span>` : ""}
    <span class="f-e" data-err="${esc(name)}"></span>
  </label>`;
}

// The buy / sell / amend form. F11: this replaces prompt(). Bad input is
// REFUSED with the reason, never silently swapped for a reference price.
async function openExecutionForm(cfg) {
  const pos = cfg.position || null;
  const row = cfg.row || null;
  const editing = cfg.execution || null;
  const side = cfg.side || (editing ? editing.side : "BUY");
  const isBuy = side === "BUY";

  const today = taipeiDate(new Date());
  // A NEW execution defaults to today, full stop. This used to default to
  // sessionOnOrBefore(today), which answers a different question: "what is the
  // newest bar we hold data for". That is yesterday until the post-close scan
  // runs, so a trade entered this morning was silently pre-dated to yesterday
  // -- the calendar's data lag is not the user's trade date. Amending an
  // existing execution keeps its own recorded date.
  const defDate = editing ? editing.session_date : today;
  // Today having no published bar yet is normal (mid-session, or the scan has
  // not run). It is also what a weekend or holiday looks like. We cannot tell
  // those apart without an official calendar (F14), so say what we know and
  // let the user correct the date rather than choosing one for them.
  const dateNote = (!editing && calIndex(today) < 0)
    ? "今日尚無收盤資料（盤中、或非交易日）。日期仍預設今天；若不是今天成交請直接改。"
    : "";
  // The hint has to agree with the bound the SAVE will apply. Reading
  // pos.open_shares here would put 目前持有 0 股 beside a field that now
  // accepts 1,000 on an archived record -- the contradiction the owner sees
  // first. Same fold, same answer. (Static if the date is changed afterwards,
  // exactly as before; the save-time check is the authority.)
  let held = 0;
  if (pos) {
    const curForHint = currentExecutions(
      await dbGetAll("executions", "by_position", pos.position_id));
    const ph = plannedExecutions(curForHint, editing ? editing.execution_id : null, {
      execution_id: "__probe__", side, session_date: defDate,
      executed_at: "", recorded_at: nowStamp(),
      shares: 0, price_cents: 0, fee_cents: 0, tax_cents: 0, is_current: 1,
    });
    held = heldBefore(ph.list, ph.index);
  }
  const defPrice = editing ? (editing.price_cents / 100).toFixed(2) : "";
  const defShares = editing ? String(editing.shares) : "";

  const stockLine = pos
    ? `${pos.stock_name || pos.stock_id}（${pos.stock_id}）`
    : row ? `${row.Stock_Name || row.Stock_ID}（${row.Stock_ID}）` : "";

  const refNote = [];
  if (row) {
    const init = cents(row.Initial_Buy_Price);
    if (init !== null) refNote.push(`首日建議價 ${fmtCents(init)}（固定）`);
    const ref = cents(row.Suggested_Buy_Price);
    if (ref !== null) refNote.push(`最新觀察參考 ${fmtCents(ref)}`);
    const close = cents(row.Close_Price);
    if (close !== null) refNote.push(`最新收盤 ${fmtCents(close)}`);
  }

  const manual = !pos && !row;
  const body = `
    ${stockLine ? `<div class="form-title">${esc(stockLine)}</div>` : ""}
    ${manual ? field("stock_id", "股票代號", "", { inputmode: "numeric", hint: "例如 3088" }) +
               field("stock_name", "股票名稱（可留空）", "") : ""}
    ${refNote.length ? `<div class="hint">參考價：${esc(refNote.join("｜"))}。這些只是參考，系統不會替你填成交價。</div>` : ""}
    ${field("session_date", "成交日期", defDate, { type: "date",
      hint: dateNote || "沒有成交日就無法放上損益時間軸" })}
    ${field("price", isBuy ? "實際成交價" : "實際賣出價", defPrice, { inputmode: "decimal", hint: "必填。輸入非數字或 0 會被拒絕，不會用參考價代替" })}
    ${field("shares", "股數", defShares, { inputmode: "numeric",
      hint: isBuy ? "1 張 = 1,000 股；零股請直接填股數"
        : editing ? `這筆成交當下可賣 ${held.toLocaleString("en-US")} 股（已扣除原紀錄）`
                  : `目前持有 ${held.toLocaleString("en-US")} 股，可部分賣出` })}
    ${field("fee", "手續費（留空＝依費率自動計算）", editing ? (editing.fee_cents / 100).toFixed(2) : "", { inputmode: "decimal" })}
    ${isBuy ? "" : field("tax", "交易稅（留空＝賣出金額 0.3%）", editing ? (editing.tax_cents / 100).toFixed(2) : "", { inputmode: "decimal" })}
    ${field("note", "備註", editing ? editing.note : "")}
    <div class="calc" id="calcBox">－</div>
    <div class="hint">費率版本：${esc(schedule(STATE.settings.fee_schedule).label)}。與券商對帳單不同時，直接填入對帳單金額。</div>`;

  openModal(isBuy ? (editing ? "更正買入成交" : "登錄買入") : (editing ? "更正賣出成交" : "登錄賣出"), body, {
    submit: "exec-save",
    submitLabel: "儲存",
    submitData: {
      pos: pos ? pos.position_id : "",
      row: row ? row.Stock_ID : "",
      side,
      edit: editing ? editing.execution_id : "",
    },
    onOpen(modal) {
      // The name should never have to be typed: fill it from whatever data we
      // have as soon as the code is entered (owner, 2026-09-21). The
      // whole-market file is what makes this work for a stock the scanner
      // never recommended, so ask for it when the form opens.
      const idField = modal.querySelector('input[name="stock_id"]');
      const nameField = modal.querySelector('input[name="stock_name"]');
      if (idField && nameField) {
        loadUniverse().catch(() => {});
        let touched = false;
        nameField.addEventListener("input", () => { touched = true; });
        const fill = () => {
          if (touched && nameField.value.trim()) return;
          const n = stockName(idField.value);
          nameField.value = n;
          nameField.placeholder = n ? "" : "查不到這個代號，可自行輸入";
        };
        idField.addEventListener("input", fill);
        idField.addEventListener("blur", fill);
        fill();
      }
      const recalc = () => {
        const v = readForm(modal);
        const p = cents(v.price), sh = /^\d+$/.test(String(v.shares || "")) ? Number(v.shares) : null;
        const box = modal.querySelector("#calcBox");
        if (!p || !sh) { box.textContent = "填入成交價與股數後顯示總金額"; return; }
        const sched = schedule(STATE.settings.fee_schedule);
        const consideration = p * sh;
        const fee = cents(v.fee) !== null && String(v.fee).trim() !== "" ? cents(v.fee) : feeFor(sched, consideration);
        const tax = isBuy ? 0
          : (cents(v.tax) !== null && String(v.tax).trim() !== "" ? cents(v.tax) : taxFor(sched, consideration));
        box.textContent = isBuy
          ? `成交金額 ${fmtCents(consideration)}＋手續費 ${fmtCents(fee)} ＝ 總投入 ${fmtCents(consideration + fee)} 元`
          : `賣出金額 ${fmtCents(consideration)}－手續費 ${fmtCents(fee)}－交易稅 ${fmtCents(tax)} ＝ 淨收 ${fmtCents(consideration - fee - tax)} 元`;
      };
      modal.querySelectorAll("input").forEach((i) => i.addEventListener("input", recalc));
      recalc();
    },
  });
}

function readForm(modal) {
  const out = {};
  modal.querySelectorAll("input").forEach((i) => { out[i.name] = i.value; });
  return out;
}

function showErrors(modal, errs) {
  modal.querySelectorAll("[data-err]").forEach((e) => { e.textContent = ""; });
  for (const [k, v] of Object.entries(errs)) {
    const el = modal.querySelector(`[data-err="${k}"]`);
    if (el) el.textContent = v;
  }
}

async function saveExecution(data) {
  const modal = $("#modal");
  const v = readForm(modal);
  let pos = data.pos ? await dbGet("positions", data.pos) : null;
  const row = data.row ? STATE.rows.find((r) => String(r.Stock_ID) === data.row) : null;

  // A manual entry needs a stock id before anything else can be validated.
  if (!pos && !row) {
    const sid = String(v.stock_id || "").trim();
    if (!/^[0-9A-Za-z]{2,8}$/.test(sid)) {
      showErrors(modal, { stock_id: "請填有效的股票代號" });
      return;
    }
  }

  // The SELL bound is the shares held AT THIS FILL, not the shares held after
  // the whole history. pos.open_shares is the latter, and it is 0 for every
  // archived and closed record, so correcting the sell that closed a trade was
  // rejected against the state that same sell produced. Share count does not
  // affect sort order (sortExecutions keys on session_date, executed_at,
  // recorded_at), so a zero-share probe with the same keys sorts exactly where
  // the real row will.
  let cur = [];
  let held = 0;
  if (pos) {
    cur = currentExecutions(await dbGetAll("executions", "by_position", pos.position_id));
    const probe = {
      execution_id: "__probe__", side: data.side,
      session_date: String(v.session_date || "").slice(0, 10),
      executed_at: "", recorded_at: nowStamp(),
      shares: 0, price_cents: 0, fee_cents: 0, tax_cents: 0, is_current: 1,
    };
    const p = plannedExecutions(cur, data.edit || null, probe);
    held = heldBefore(p.list, p.index);
  }

  const check = validateExecution({
    side: data.side,
    session_date: v.session_date,
    price: v.price,
    shares: v.shares,
    fee: v.fee,
    tax: v.tax,
    note: v.note,
    fee_schedule: STATE.settings.fee_schedule,
  }, pos, held);

  if (!check.ok) { showErrors(modal, check.errs); return; }

  // The per-row bound says nothing about the fills AFTER this one.
  if (pos) {
    const cand = Object.assign({
      execution_id: "__new__", is_current: 1, recorded_at: nowStamp(),
      executed_at: check.value.executed_at || "",
    }, check.value);
    const p2 = plannedExecutions(cur, data.edit || null, cand);
    const bad = firstOversell(p2.list);
    if (bad) { showErrors(modal, { shares: oversellMessage(bad, "更正後") }); return; }
  }
  showErrors(modal, {});

  if (!pos) {
    const sid = row ? String(row.Stock_ID) : String(v.stock_id || "").trim();
    // Never open a second automatic cycle while one is still in flight
    // (ledger._open_cycle's rule): reuse the open position for this name.
    pos = STATE.positions.find((p) => p.stock_id === sid && p.status === "open" && !p.needs_shares) || null;
    if (!pos) {
      pos = await createPosition({
        stock_id: sid,
        stock_name: row ? (row.Stock_Name || sid)
          : (String(v.stock_name || "").trim() || stockName(sid) || sid),
        market: row ? (row.Market || "") : "",
        recommendation_id: row ? (row.Recommendation_ID || null) : null,
        initial_buy_price: row ? cents(row.Initial_Buy_Price) : null,
        rec_recommended_on: row ? (String(row.Recommended_On || "").slice(0, 10) || null) : null,
        horizon_days: row && row.Hold_Total ? Number(row.Hold_Total) : STRATEGY.horizon,
        cap_days: row && row.Hold_Cap ? Number(row.Hold_Cap) : STRATEGY.cap,
        origin: row ? "recommended" : "manual",
      });
    }
  } else if (pos.needs_shares && row === null && data.side === "BUY") {
    pos.needs_shares = false;
    await dbPut("positions", pos);
  }

  const wasArchived = pos.status === "archived";
  try {
    await addExecution(pos.position_id, check.value,
      data.edit ? { supersedes: data.edit, revision: 2 } : {});
  } catch (e) {
    // The ledger's own refusals are about SHARES, not the price.
    showErrors(modal, { shares: e.message || String(e) });
    return;
  }
  closeModal();
  // toast() shares one timer, so a second call within 3.2s replaces the first.
  // The archive-exit notice has to REPLACE the ordinary toast, not follow it.
  const note = await unarchivedNote(pos.position_id, wasArchived);
  toast(note || (data.side === "BUY" ? "已登錄買入" : "已登錄賣出"));
  await load();
}

// A record that acquires shares again leaves the archive (see applyDerived).
// That is a visible move between two pages, so it is announced.
async function unarchivedNote(posId, wasArchived) {
  if (!wasArchived) return null;
  const after = await dbGet("positions", posId);
  return (after && after.status !== "archived")
    ? "這筆又有持股，已移回持倉清單" : null;
}

async function openExecutionList(posId) {
  const pos = await dbGet("positions", posId);
  const all = sortExecutions(await dbGetAll("executions", "by_position", posId));
  const rows = all.map((e) => {
    const dead = e.is_current !== 1;
    return `<div class="exe ${dead ? "dead" : ""}">
      <div class="exe-h"><b>${e.side === "BUY" ? "買進" : "賣出"}</b> ${esc(e.session_date)}
        ${dead ? `<span class="verdict no">${esc(e.void_reason ? "已撤銷" : "已被更正取代")}</span>` : ""}</div>
      <div class="sm">${esc(e.shares.toLocaleString("en-US"))} 股 @ ${esc(fmtCents(e.price_cents, { always: true }))}
        ｜費 ${esc(fmtCents(e.fee_cents))}${e.side === "SELL" ? `｜稅 ${esc(fmtCents(e.tax_cents))}` : ""}
        ｜登錄 ${esc(e.recorded_at)}</div>
      ${dead ? "" : `<div class="btns">
        ${btn("exec-edit", "更正這筆", "", { exe: e.execution_id })}
        ${btn("exec-void", "撤銷誤登", "", { exe: e.execution_id })}</div>`}
    </div>`;
  }).join("");

  openModal(`成交明細 · ${pos.stock_name || pos.stock_id}`,
    `<div class="hint">更正會寫入新版本並保留舊紀錄；撤銷誤登不會刪除資料，只是不再計入損益。</div>` +
    (rows || `<div class="empty-inline">尚無成交紀錄。</div>`) +
    // Every archived record has open_shares === 0 by the archive gate, and the
    // `sell` branch can only answer 這筆持倉沒有可賣股數. A button that can
    // only ever toast an error is worse than no button: backfill the buy
    // first (which returns the record to 持倉), then sell from the card.
    `<div class="btns">${btn("buy", "補登買入", "primary", { pos: posId })}${
      pos.open_shares > 0 ? btn("sell", "補登賣出", "", { pos: posId }) : ""}</div>`,
    { noFoot: true });
}

async function openCycleDetail(posId) {
  const pos = await dbGet("positions", posId);
  const marks = (STATE.marksByPos[posId] || []);
  const cycle = STATE.cycles[posId];
  const horizon = pos.horizon_days || STRATEGY.horizon;

  const rows = marks.map((m) => `<tr class="${m.day_index === horizon ? "hl" : ""}">
    <td>${m.day_index === null ? "?" : "D" + m.day_index}</td>
    <td>${esc(m.session_date)}</td>
    <td>${m.close_price === null ? "-" : esc(fmtPrice(m.close_price))}</td>
    <td class="${signClass(m.day_pnl_gross)}">${esc(fmtCents(m.day_pnl_gross, { signed: true }))}</td>
    <td class="${signClass(m.total_gross)}">${esc(fmtCents(m.total_gross, { signed: true }))}</td>
    <td>${m.data_status === "current" ? "" : esc(DATA_STATUS_TEXT[m.data_status] || m.data_status)}</td>
  </tr>`).join("");

  const frozen = cycle
    ? `<div class="notice ok">已凍結：${esc(cycle.session_date)}${
        cycle.day_index === null ? "（估值日不在已知交易日曆內）" : `（D${cycle.day_index}）`} 價差損益 ${
        esc(fmtCents(cycle.total_gross, { signed: true }))} 元｜相對首日建議 ${esc(fmtPct(cycle.return_vs_initial))}｜相對實際成本 ${esc(fmtPct(cycle.return_vs_cost))}${
        cycle.net_if_liquidated !== null ? `｜全數賣出估計淨額 ${esc(fmtCents(cycle.net_if_liquidated, { signed: true }))}` : ""}</div>` +
      (cycle.rebuild_failed
        ? `<div class="notice warn">已實現損益已依更正後的成交重算，但${esc(cycle.rebuild_failed)}，上面的十日成果仍是更正前的數字。</div>` : "") +
      (cycle.valuation_stale
        ? `<div class="hint">損益金額已依更正後的成交重算；估值日與當日收盤超出手機保留的 30 個交易日，維持原凍結值。</div>` : "") +
      (cycle.revisions && cycle.revisions.length
        ? `<div class="hint">曾更正 ${cycle.revisions.length} 次（保留舊值供追溯）。</div>` : "")
    : `<div class="hint">尚未達第 ${horizon} 個交易日，或尚無足夠報價。</div>`;

  openModal(`${horizon}日明細 · ${pos.stock_name || pos.stock_id}`,
    frozen +
    `<table class="tbl"><thead><tr><th>持倉日</th><th>交易日</th><th>收盤</th><th>當日損益</th><th>累計損益</th><th></th></tr></thead>
     <tbody>${rows || `<tr><td colspan="6">尚無估值</td></tr>`}</tbody></table>
     <div class="hint">當日損益＝當日累計 −前一日累計。累計欄不可再相加（報告 6.2）。</div>`,
    { noFoot: true });
}

function openDetail(stockId) {
  const r = rowFor(stockId);
  if (!r) return;
  const sc = rankScore();
  const grp = (title, body) => `<div class="sec-title">${esc(title)}</div>${body}`;
  const lgrp = (title, chips) => `<div class="sec-title">${esc(title)}</div><div class="lights">${chips}</div>`;

  const body =
    grp("規則分數（皆為規則計算，不是機率）",
      drow("起漲條件分" + (sc.key === "Launch_Score" ? "（本模式排序依據）" : ""), esc(fmt(r.Launch_Score, 1))) +
      drow("動能分" + (sc.key === "Surge_Score" ? "（本模式排序依據）" : ""), esc(fmt(r.Surge_Score, 1))) +
      drow("盤整蓄勢分", esc(fmt(r.Explosion_Score, 1))) +
      drow("停損距離%", esc(fmt(r.Risk_Pct, 1, "%"))) +
      drow("20日平均日振幅%", esc(fmt(r.ATR_Pct, 2, "%"))) +
      drow("近5交易日漲幅%", esc(fmtSigned(r.Ret_5D_Pct, 1, "%"))) +
      drow("近63交易日漲幅%", esc(fmtSigned(r.Gain_3M_Pct, 1, "%"))) +
      drow("箱型壓縮度（比值）", esc(fmt(r.Range_Tightness, 4))) +
      drow("近3日均量/20日均量", esc(fmt(r.Volume_Dryup, 4))) +
      drow("上漲日量能占比", esc(fmt(r.Volume_Bias, 4)))) +
    grp("每日法人買賣超（張）",
      drow("外資", esc(fmtSigned(r.Foreign_Net, 0))) +
      drow("投信", esc(fmtSigned(r.Trust_Net, 0))) +
      drow("自營商", esc(fmtSigned(r.Dealer_Net, 0))) +
      drow("三大法人合計", esc(fmtSigned(r.Inst_Net, 0))) +
      drow("合計占20日均量%", esc(fmtSigned(r.Inst_Pct, 1, "%"))) +
      drow("三大法人近5日累計", esc(fmtSigned(r.Inst_Net_5D, 0))) +
      drow("外資近5日累計", esc(fmtSigned(r.Foreign_Net_5D, 0))) +
      drow("投信近5日累計", esc(fmtSigned(r.Trust_Net_5D, 0))) +
      drow("連續買(+)/賣(−)超日數", esc(fmtSigned(r.Inst_Streak, 0))) +
      drow("外資近5日買超天數", esc(fmt(r.Inst_Buy_Days, 0))) +
      drow("法人資料日", esc(String(r.Inst_Date || "-"))) +
      drow("隔日籌碼動作", esc(r.Chip_Action ? (CHIP_ACTION_TEXT[r.Chip_Action] || r.Chip_Action) : "無（非持倉或無驗證規則）")) +
      drow("說明", esc(String(r.Chip_Note || "-")))) +
    grp("集保籌碼（週更新，400,001股以上級距）",
      drow("400張+持股%", esc(fmt(r.Large_Holder_Pct, 2, "%"))) +
      drow("大戶變動（百分點）", esc(fmtSigned(r.Large_Pct_Change, 4))) +
      drow("散戶持股%", esc(fmt(r.Retail_Pct, 2, "%"))) +
      drow("散戶變動（百分點）", esc(fmtSigned(r.Retail_Pct_Change, 4)))) +
    lgrp("訊號燈號",
      lightChip("箱縮", r.Cond_A) + lightChip("上漲日量能偏多", r.Cond_C) +
      lightChip("大戶增加", r.Cond_B) + lightChip("MA多頭排列", r.MA_Bull_Align) +
      lightChip("通道上緣接近", r.Donchian_Break) + lightChip("MACD金叉", r.MACD_Cross)) +
    grp("距離（%）",
      drow("距支撐%", esc(fmt(r.Sup_Gap_Pct, 1, "%"))) +
      drow("距壓力%", esc(fmt(r.Res_Gap_Pct, 1, "%"))) +
      drow("距近一年最高收盤%", esc(fmt(r.Dist_52W_High_Pct, 1, "%"))) +
      drow("RS超額（百分點，對 TAIEX）", esc(fmtSigned(r.RS_Score, 1)))) +
    lgrp("線型輔助指標",
      lightChip("MA糾結", r.MA_Squeeze) + lightChip("趨勢線突破", r.Trend_Breakout) +
      lightChip("MACD柱轉正", r.MACD_Hist_Turn) + lightChip("近一年高位", r.Near_52W_High) +
      lightChip("RS強勢", r.RS_Strong) + lightChip("夾縫爆發", r.Squeeze)) +
    grp("均線",
      drow("5MA", esc(fmt(r.MA5, 2))) + drow("10MA", esc(fmt(r.MA10, 2))) +
      drow("20MA", esc(fmt(r.MA20, 2))) + drow("60MA", esc(fmt(r.MA60, 2)))) +
    grp("壓力 / 支撐 / 收盤價量分布區",
      drow("關鍵支撐", esc(fmt(r.Support_Used, 2))) +
      drow("前60交易日高", esc(fmt(r.Resist_60H, 2))) +
      drow("20日低", esc(fmt(r.Support_20L, 2))) +
      drow("60日低", esc(fmt(r.Support_60L, 2))) +
      drow("整數關卡", esc(fmt(r.Round_Level, 2))) +
      drow("Zone 1", esc(fmt(r.VP_Zone1, 2))) +
      drow("Zone 2", esc(fmt(r.VP_Zone2, 2))) +
      drow("Zone 3", esc(fmt(r.VP_Zone3, 2)))) +
    grp("缺口（不保證仍未回補）",
      drow("跳空支撐", esc(fmt(r.Gap_Up_Sup, 2))) +
      drow("跳空壓力", esc(fmt(r.Gap_Dn_Res, 2)))) +
    grp("資料品質",
      drow("完整性", r.Integrity_OK === false ? "未通過" : r.Integrity_OK === true ? "通過" : "未提供") +
      drow("旗標", esc(r.Integrity_Flags || "無")) +
      drow("資料日", esc(String(r.Data_Date || "").slice(0, 10) || "未知")));

  openModal(`${r.Stock_Name || r.Stock_ID}（${r.Stock_ID}）`, body, { noFoot: true });
}

/* ============================================================================
 * 13. Migration, export and import
 * ==========================================================================*/

const LEGACY_KEY = "yt_holdings_v1";

// Report 11.5: import the phone's old localStorage holdings, but the old shape
// {id,name,market,entry,fill,total,cap,added} has NO SHARE COUNT. Inventing a
// quantity would fabricate a P&L, so the position is created empty, flagged
// 待補股數, and the user is asked. The legacy blob is kept in `meta` untouched.
async function migrateLegacy() {
  if (await metaGet("migrated_localstorage")) return 0;
  let legacy = {};
  try { legacy = JSON.parse(localStorage.getItem(LEGACY_KEY)) || {}; }
  catch (e) { legacy = {}; }
  const items = Object.values(legacy);
  await metaSet("legacy_backup", legacy);
  let n = 0;
  for (const h of items) {
    const sid = String(h.id || "").trim();
    if (!sid) continue;
    if (STATE.positions.some((p) => p.stock_id === sid && p.origin === "migrated")) continue;
    await createPosition({
      stock_id: sid,
      stock_name: h.name || sid,
      market: h.market || "",
      origin: "migrated",
      needs_shares: true,
      legacy_fill: cents(h.fill),
      legacy_entry: String(h.entry || "").slice(0, 10) || null,
      horizon_days: Number(h.total) || STRATEGY.horizon,
      cap_days: Number(h.cap) || STRATEGY.cap,
      note: "由舊版 localStorage 匯入，缺少股數",
    });
    n += 1;
  }
  await metaSet("migrated_localstorage", { at: nowStamp(), count: n });
  return n;
}

async function exportBackup() {
  const positions = await dbGetAll("positions");
  const executions = await dbGetAll("executions");
  const meta = await dbGetAll("meta");
  const payload = {
    schema: "yentool-mobile-ledger",
    version: 1,
    exported_at: nowStamp(),
    positions, executions,
    // The dispatch token must never leave the device, least of all in a file
    // that gets mailed to yourself or dropped in a cloud drive.
    meta: meta.filter((m) => !String(m.key).startsWith("legacy_backup") && !PRIVATE_META.has(m.key)),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `yentool-ledger-${taipeiDate(new Date())}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  toast("已匯出備份檔");
}

function openImport() {
  openModal("匯入備份", `
    <div class="notice warn">匯入會以檔案內容覆蓋相同 ID 的持倉與成交紀錄。建議先匯出目前資料。</div>
    <input class="f-i" type="file" id="importFile" accept="application/json,.json" />
    <div class="f-e" id="importErr"></div>`, {
    submit: "import-run", submitLabel: "匯入",
  });
}

async function runImport() {
  const input = document.getElementById("importFile");
  const err = document.getElementById("importErr");
  const file = input && input.files && input.files[0];
  if (!file) { err.textContent = "請先選擇檔案"; return; }
  let data;
  try { data = JSON.parse(await file.text()); }
  catch (e) { err.textContent = "不是有效的 JSON 檔"; return; }
  if (!data || data.schema !== "yentool-mobile-ledger" || !Array.isArray(data.positions)) {
    err.textContent = "檔案格式不符（需要 YenTool 匯出的備份）";
    return;
  }
  // Validate before writing anything: report 11.5 requires stock id, price and
  // date to be checked on import rather than trusted.
  for (const p of data.positions) {
    if (!p.position_id || !p.stock_id) { err.textContent = "持倉資料缺少必要欄位"; return; }
  }
  for (const e of (data.executions || [])) {
    if (!e.execution_id || !e.position_id || !Number.isInteger(e.shares) ||
        !Number.isInteger(e.price_cents) || !/^\d{4}-\d{2}-\d{2}$/.test(String(e.session_date || ""))) {
      err.textContent = `成交紀錄 ${e.execution_id || "?"} 欄位不完整或格式錯誤`;
      return;
    }
  }
  await dbPutMany("positions", data.positions);
  await dbPutMany("executions", data.executions || []);
  for (const m of (data.meta || [])) {
    if (m && m.key && !PRIVATE_META.has(m.key)) await dbPut("meta", m);
  }
  closeModal();
  toast(`已匯入 ${data.positions.length} 筆持倉`);
  await load();
}

/* ============================================================================
 * 14. Self-test: report section 6.2's worked example
 *
 * This runs on the user's own device, on the same code path the app uses, so
 * "the numbers match the spec" is something the phone can demonstrate rather
 * than something the README claims.
 * ==========================================================================*/

function selfTest() {
  const sessions = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
                    "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16"];
  const closeList = [103, 101, 105, 106, 104, 108, 107, 110, 109, 112];
  const closes = {};
  sessions.forEach((s, i) => { closes[s] = cents(closeList[i]); });

  // The report's example is stated "without broker dollar rounding", which is
  // exactly what the tw-equity-exact schedule is for.
  const sched = FEE_SCHEDULES["tw-equity-exact"];
  const priceCents = cents(102);
  const shares = 1000;
  const buyFee = feeFor(sched, priceCents * shares);
  const pos = {
    position_id: "selftest", stock_id: "0000", stock_name: "自我檢查",
    fee_schedule: "tw-equity-exact", dividends: 0,
    opened_session: sessions[0], horizon_days: 10,
    initial_buy_price: cents(100), avg_cost: priceCents,
  };
  const execs = [{
    execution_id: "t1", position_id: "selftest", side: "BUY",
    session_date: sessions[0], executed_at: "", shares,
    price_cents: priceCents, fee_cents: buyFee, tax_cents: 0, is_current: 1,
    recorded_at: "",
  }];
  const marks = buildMarks(pos, execs, closes, sessions, sessions);
  const last = marks[marks.length - 1];

  const days = marks.map((m) => m.day_pnl_gross);
  const wantDays = [1000, -2000, 4000, 1000, -2000, 4000, -1000, 3000, -1000, 3000]
    .map((v) => v * 100);
  // The trap the report names: summing the CUMULATIVE column gives 45,000.
  const sumCumulative = marks.reduce((a, m) => a + m.total_gross, 0);
  const retInitial = pctOf(last.close_price - pos.initial_buy_price, pos.initial_buy_price);
  const retCost = pctOf(last.close_price - priceCents, priceCents);

  const checks = [
    { name: "買入手續費", want: "145.35", got: fmtCents(buyFee, { always: true }) },
    { name: "D10 累計價差損益", want: "10,000.00", got: fmtCents(last.total_gross, { always: true }) },
    { name: "D10 全數賣出估計淨額", want: "9,359.05", got: fmtCents(last.net_if_liquidated, { always: true }) },
    { name: "相對首日建議價 100", want: "+12.00%", got: fmtPct(retInitial) },
    { name: "相對實際成本 102", want: "+9.80%", got: fmtPct(retCost) },
    { name: "每日損益序列", want: wantDays.map((v) => v / 100).join(","), got: days.map((v) => v / 100).join(",") },
    { name: "累計值相加（示範不可這樣算）", want: "45,000.00", got: fmtCents(sumCumulative, { always: true }) },
    { name: "D10 天數索引", want: "10", got: String(last.day_index) },
  ].map((c) => Object.assign(c, { ok: c.want === c.got }));

  return { pass: checks.every((c) => c.ok), checks, marks };
}

/* ============================================================================
 * 15. Events, resume watchdog, boot
 * ==========================================================================*/

// One delegated listener. Re-rendering a page therefore never leaks handlers
// and never leaves a dead button behind.
document.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act;
  const d = el.dataset;
  try {
    if (act === "page") { STATE.page = d.page; render(); window.scrollTo(0, 0); return; }
    if (act === "market") { STATE.market = d.market; renderPicks(); return; }
    if (act === "close") { closeModal(); return; }
    if (act === "refresh") { await load(); return; }
    if (act === "detail") { openDetail(d.id); return; }
    if (act === "export") { await exportBackup(); return; }
    if (act === "import") { openImport(); return; }
    if (act === "import-run") { await runImport(); return; }
    if (act === "cycle") { await openCycleDetail(d.pos); return; }
    if (act === "execs") { await openExecutionList(d.pos); return; }
    if (act === "fees") { await toggleFees(); return; }
    if (act === "cloud-refresh") { await cloudRefresh(true); return; }
    if (act === "token-set") { openTokenForm(); return; }
    if (act === "token-save") { await saveToken(); return; }
    if (act === "token-clear") { await clearToken(); return; }

    if (act === "buy") {
      const pos = d.pos ? await dbGet("positions", d.pos) : null;
      const row = d.id ? STATE.rows.find((r) => String(r.Stock_ID) === d.id) : null;
      await openExecutionForm({ side: "BUY", position: pos, row });
      return;
    }
    if (act === "sell") {
      const pos = await dbGet("positions", d.pos);
      if (!pos || pos.open_shares <= 0) { toast("這筆持倉沒有可賣股數"); return; }
      await openExecutionForm({ side: "SELL", position: pos });
      return;
    }
    if (act === "exec-save") { await saveExecution(d); return; }
    if (act === "exec-edit") {
      const exe = await dbGet("executions", d.exe);
      const pos = await dbGet("positions", exe.position_id);
      await openExecutionForm({ side: exe.side, position: pos, execution: exe });
      return;
    }
    if (act === "exec-void") {
      if (!confirm("撤銷誤登：這筆成交將不再計入損益，但紀錄會保留。確定嗎？")) return;
      const exe0 = await dbGet("executions", d.exe);
      const pos0 = exe0 ? await dbGet("positions", exe0.position_id) : null;
      const wasArchived = !!pos0 && pos0.status === "archived";
      await voidExecution(d.exe, "user_void");
      closeModal();
      const note = pos0 ? await unarchivedNote(pos0.position_id, wasArchived) : null;
      toast(note || "已撤銷該筆成交");
      await load();
      return;
    }
    if (act === "archive") {
      const pos = await dbGet("positions", d.pos);
      if (pos.open_shares > 0) { toast("尚有持股，請先登錄賣出"); return; }
      if (!confirm("封存這筆已結束的紀錄？資料會保留在「績效與歷史」，只是不再出現在持倉清單。")) return;
      pos.status = "archived";
      pos.archived_at = nowStamp();
      await dbPut("positions", pos);
      toast("已封存");
      await load();
      return;
    }
    if (act === "pos-delete") {
      const pos = await dbGet("positions", d.pos);
      if (!pos) { toast("找不到這筆紀錄"); return; }
      const cost = deleteCost(pos);
      const name = pos.stock_name || pos.stock_id;
      const msg = cost.empty
        ? `刪除「${name}」？

這筆沒有任何有效成交，也沒有已實現損益，刪除後不會留下紀錄。`
        : `刪除「${name}」？

會一併移除 ${cost.live} 筆有效成交與已實現淨損益 `
          + `${fmtPnl(cost.realized)}，帳戶總額會跟著改變。

`
          + `這個動作無法復原。若只是想把它從清單收起來，請改用「封存已結束紀錄」。`;
      if (!confirm(msg)) return;
      const gone = await deletePosition(d.pos);
      closeModal();
      toast(`已刪除（成交 ${gone.executions} 筆）`);
      await load();
      return;
    }
    if (act === "void-pos") {
      if (!confirm("撤銷誤登整筆持倉？資料會保留在歷史頁，但不再計入任何損益。")) return;
      const pos = await dbGet("positions", d.pos);
      pos.status = "void";
      pos.voided_at = nowStamp();
      await dbPut("positions", pos);
      toast("已撤銷");
      await load();
      return;
    }
  } catch (e) {
    toast("操作失敗：" + (e.message || String(e)));
  }
});

document.addEventListener("change", (ev) => {
  const el = ev.target.closest("[data-act='sort']");
  if (!el) return;
  STATE.sortIndex = Number(el.value);
  renderPicks();
});

async function toggleFees() {
  const next = STATE.settings.fee_schedule === "tw-equity-v1" ? "tw-equity-exact" : "tw-equity-v1";
  STATE.settings.fee_schedule = next;
  await metaSet("settings", STATE.settings);
  toast("費率版本改為：" + schedule(next).label);
  render();
}

// Freshness watchdog. iOS usually RESUMES a backgrounded PWA rather than
// reloading it, so without this the user stares at yesterday's scan. All three
// events are needed because iOS picks a different one depending on how the app
// came back (app switcher, bfcache, external link).
let RESUME_GATE = 0;
function onResume() {
  const now = Date.now();
  if (document.hidden || now - RESUME_GATE < 2000) return;
  RESUME_GATE = now;
  load();
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.getRegistration().then((reg) => reg && reg.update()).catch(() => {});
  }
}
document.addEventListener("visibilitychange", onResume);
window.addEventListener("pageshow", onResume);
window.addEventListener("focus", onResume);

// Taipei-date rollover.
//
// Every "today" on screen is computed at RENDER time -- the hold-day counter,
// the overview header, the execution form's default date. On a phone the app
// normally just sits there, so nothing redraws at midnight and all of them keep
// yesterday's date. A trade entered at 00:30 was pre-dated by a full day, which
// is the one thing an execution record must never get wrong.
//
// A plain interval rather than a timer aimed at midnight: a suspended phone does
// not fire a timer that came due while it slept, and the clock can also move
// because of a timezone change or a manual correction. Comparing the actual
// Taipei date every half minute costs nothing and handles all three.
let TW_DAY = taipeiDate(new Date());
function checkDayRollover() {
  const now = taipeiDate(new Date());
  if (now === TW_DAY) return false;
  TW_DAY = now;
  render();
  return true;
}
setInterval(checkDayRollover, 30000);
// Also on resume: a phone that slept through midnight fires no interval, and
// waking up is exactly when the user looks at the screen.
document.addEventListener("visibilitychange", checkDayRollover);
window.addEventListener("pageshow", checkDayRollover);
window.addEventListener("focus", checkDayRollover);

async function boot() {
  $("#tabs").innerHTML = tabBar();
  try {
    await openDB();
    STATE.settings = Object.assign(STATE.settings, await metaGet("settings", {}));
    REFRESH.hasToken = !!(await metaGet(GH_TOKEN_KEY, ""));
    await loadLedger();
    const migrated = await migrateLegacy();
    if (migrated) toast(`已從舊版匯入 ${migrated} 筆持倉，請補登股數`);
  } catch (e) {
    // A private-mode browser can refuse IndexedDB entirely. The market pages
    // must still work; only the ledger is unavailable.
    DB_OK = false;
    LEDGER_ERROR = e.message || String(e);
  }
  await load();
}

boot().catch((e) => {
  setStatus("啟動失敗：" + (e.message || e));
});

// Service worker only registers in a secure context (https or localhost).
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("./sw.js").catch(() => {});
  // The data watchdog reloads DATA, but a suspended PWA keeps running the OLD
  // app code forever. A new controller means a deploy landed: reload once.
  let swReloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (swReloaded) return;
    swReloaded = true;
    window.location.reload();
  });
}

// Exposed for the browser console and for the research page's self-check.
window.YT = {
  STATE, selfTest, buildMarks, replay, cents, fmtCents, pctOf, feeFor, taxFor,
  buyVerdict, portfolioSummary, pendingItems, tickRound, taipeiDate, load,
};
