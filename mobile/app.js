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
// The recommendation ledger's public export (data/recommendations.json),
// published beside scan_result.json. Fetched lazily by the performance page
// for the complete record of closed recommendations; a miss is a labelled
// state, not an error.
const RECS_URL = "./recommendations.json";
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
      "出場計畫：災難停損 -20% · 收盤站上 +2.5% 後隔一個交易日起停損上調到 +2% · 目標 +20% · 第 8 天起收盤仍有實質獲利（+1% 以上）就隔日開盤收下 · 基本抱 10 個交易日；第 10 天起每天收盤，只要站上自己的 5 日均價、或當天大盤正在回檔（加權指數低於 20 日均線但仍高於 60 日均線）就續抱，最晚第 20 天。\n" +
      "回測（2017-2026，556 筆，近 3 年，含手續費與證交稅）：70.8% 勝、每筆平均 +1.95%；更早的資料 69.5% / +2.04%。19 個有效季度裡沒有一季低於 60%。歷史統計，不是未來勝率。\n" +
      "這 +1.95% 是怎麼來的（2026-09-23 拆解）：獲利有 4 成來自 17% 碰到 +20% 停利的交易；鎖利出場（36%）平均只有 +1.2%；時間出場（20%）平均 −9.9%、停損（6%）−20.5%。所以勝率高不代表一直在賺——沒有大波段的那幾個月，合計就是負的（2022 年全年 −6.6%、2026-07~09 實際訊號合計為負）。固定資金分 5～8 個等權格、有訊號就買的話，到 2025 年底每年約 +12%（研究樣本）～+17%（線上訊號頻率），格數越多回撤越淺，2026 年至今是離群的一年；避開 1～3 格（BACKTEST_LOG M.4 嚴格格數；舊版引用的格數年化讓出場當天騰出的格子當天就重用，偏高，已更正）。看下面「帳本實際」那一行，那才是你這段時間真的會拿到的數字。\n" +
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
// Every code in scanner/result_checks.BUY_BLOCKS needs a label here
// (tests/test_mobile_picks_ui.py); the wording follows config/report_text.json
// block_label, which the AI report and the desktop use. 2026-10-08: "held"
// used to read 已進場, but it only means "on the list the session before" --
// the rule may never have bought it (that day could have been blocked too).
const BLOCK_TEXT = {
  regime: "大盤未站上20/60MA",
  regime_stale: "大盤資料尚未更新到今天，本次不判定順風",
  stale: "資料非當日，需重新確認",
  integrity: "資料完整性未通過",
  rank: "非前20名",
  market: "上市股·規則只買上櫃",
  quality: "未過 CORE+ 時機門檻",
  held: "非新訊號·前日已在清單",
  unknown: "後端未提供買進判定（不視為可買）",
  no_rule: "此模式未定義買進規則",
  dropped: "已掉出清單·僅追蹤出場",
  restricted: "交易受限·無法下單",
};

// Trade_Restriction kinds (scanner/trade_restrictions.RESTRICTION_KINDS) and
// the display-only flag limit_down. Labels mirror config/report_text.json
// restriction_label. Only "suspended" blocks a buy (BLOCKING_RESTRICTIONS);
// the rest are ORDER GUIDANCE, never "cannot buy": signals that entered
// during a disposition were the best bucket in the backtest (BACKTEST_LOG M.1).
const RESTRICT_TEXT = {
  suspended: "暫停交易",
  disposition: "處置股",
  altered: "變更交易方法",
  limit_lock: "漲停鎖住",
  attention: "注意股",
  unknown: "限制資料未取得",
  limit_down: "跌停",
};
const RESTRICT_TONE = {
  suspended: "err", disposition: "warn", altered: "warn", limit_lock: "warn",
  unknown: "warn", attention: "info", limit_down: "info",
};
// Sentences, same templates as config/report_text.json restriction_note
// (config/ is not deployed to Pages, so the phone carries its own copy;
// tests/test_mobile_probe.py checks the two agree). {match} and {prepay} are
// built from the PARSED Restriction_Match_Min / Restriction_Prepay -- the
// minutes are never written here (TPEX has mostly matched every ~2 minutes
// since 2026-08-10, not the 5 the old rules of thumb say).
const RESTRICT_NOTE = {
  suspended: "暫停交易中，無法下單",
  disposition: "處置股{until}{match}{prepay}；不影響規則買進判定，但開盤委託依分盤集合競價撮合，停損、鎖利等出場也可能延後成交",
  altered: "變更交易方法（如全額交割）：買進需先預收款項、撮合較慢；不影響規則買進判定，但出場也可能延後成交",
  limit_lock: "今日收盤漲停鎖住：隔日開盤可能跳空或買不到，進場以實際成交價為準；不影響規則買進判定",
  unknown: "交易限制資料本次未取得，下單前請自行確認是否為處置或注意股",
  attention: "注意股（交易所提醒，非處置）：不影響規則買進判定；若後續轉為處置股，撮合與預收方式會改變",
  blocking: "{label}{until}{match}{prepay}：屬規則的封鎖類別，規則不買",
  until: "（至 {until}）",
  match: "，約每 {min} 分鐘撮合一次",
  prepay_all: "，每筆委託都需全額預收款券",
  prepay_threshold: "，單筆 10 張或當日累計 30 張以上需預收款券",
  // phone-only: a disposition row whose interval was not parsed
  match_unknown: "，撮合間隔以交易所公告為準",
  limit_down: "今日收盤跌停（僅供參考，不影響規則判定）",
};

// Order mechanics, dated. These are exchange RULES, not strategy numbers, and
// they change (the odd-lot session has been re-timed before), so they live in
// one dated block and the card prints the date. Sources: TWSE / TPEx trading
// rules as checked 2026-10-08; the odd-lot cost figures are BACKTEST_LOG M.7
// (information only, they passed no gate).
const ORDER_RULES = {
  as_of: "2026-10-08",
  source: "證交所／櫃買中心交易制度，2026-10-08 查核，以交易所公告為準",
  auction: "08:30–09:00 開盤集合競價只收限價（ROD），成交價是競價結果，不一定是你掛的價",
  odd_first: "09:10",
  odd_interval_sec: 5,
  odd_note: "盤中零股 09:10 第一次撮合，之後約每 5 秒撮合一次，只收限價",
  odd_cost: "研究（BACKTEST_LOG M.7）：零股 09:10 第一次撮合平均比整股開盤貴約 +0.2%～+0.3%，中位數 0，約八成落在 -1.7%～+2.9%",
  odd_no_open_limit: "不要把零股限價單剛好掛在整股開盤價：常常買不到，錯過的正是漲最多的那些",
  odd_min_fee: "每筆至少約 NT$14,035 才不會被 NT$20 最低手續費墊高成本；零股最低手續費依券商而定",
  anchor_note: "卡片上的停損、鎖利、停利以整股開盤價（Entry_Open）計算；「持倉」頁改以你登錄的第一筆成交價計算，兩者可能不同",
  odd_change_note: "交易所已預告零股撮合時間將調整，實施日以證交所公告為準",
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

// Integer floor division (Decimal ROUND_DOWN on the non-negative amounts it
// is used for). A BigInt operand -- see mulExact -- is divided exactly.
// Positive denominators only.
function divFloor(numerator, denominator) {
  if (typeof numerator === "bigint" || typeof denominator === "bigint") {
    const n = BigInt(numerator), d = BigInt(denominator), zero = BigInt(0);
    let q = n / d;                              // BigInt division truncates
    if (n % d !== zero && n < zero) q -= BigInt(1);
    return Number(q);
  }
  let q = Math.floor(numerator / denominator);
  let r = numerator - q * denominator;
  while (r < 0) { q -= 1; r += denominator; }
  while (r >= denominator) { q += 1; r -= denominator; }
  return q;
}

// a x b x c for non-negative integers, exactly: a Number while the product
// is a safe integer, a BigInt beyond that.
function mulExact(a, b, c) {
  const k = c === undefined ? 1 : c;
  const p = a * b * k;
  if (Number.isSafeInteger(p)) return p;
  return BigInt(a) * BigInt(b) * BigInt(k);
}

// portfolio/money.py FeeSchedule._fee: consideration x rate x discount,
// TRUNCATED to the dollar straight from the exact product, then the minimum.
// 2026-10-08: this used to round to the cent first and truncate after, which
// turns NT$20,350 x 0.1425% = 28.99875 into 29.00 -> 29 where the broker and
// money.py charge 28 (and 10.10 x 99 shares' tax 2.9997 into 3, not 2).
function feeFor(sched, considerationCents) {
  if (sched.roundToDollar) {
    let fee = divFloor(mulExact(considerationCents, sched.feeNum, sched.discNum),
                       sched.feeDen * sched.discDen * 100) * 100;
    if (fee < sched.minFeeCents) fee = sched.minFeeCents;
    return fee;
  }
  let fee = divRound(considerationCents * sched.feeNum * sched.discNum,
                     sched.feeDen * sched.discDen);
  if (sched.minFeeCents > 0 && fee < sched.minFeeCents) fee = sched.minFeeCents;
  return fee;
}

function taxFor(sched, considerationCents) {
  if (sched.roundToDollar) {
    return divFloor(mulExact(considerationCents, sched.taxNum), sched.taxDen * 100) * 100;
  }
  return divRound(considerationCents * sched.taxNum, sched.taxDen);
}

// True when the minimum fee, not the rate, sets the fee on this consideration
// (below about NT$14,035 at 0.1425% and NT$20).
function feeBindsMin(sched, considerationCents) {
  if (!(sched.minFeeCents > 0)) return false;
  const raw = mulExact(considerationCents, sched.feeNum, sched.discNum);
  const lim = sched.minFeeCents * sched.feeDen * sched.discDen;
  return typeof raw === "bigint" ? raw < BigInt(lim) : raw < lim;
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

// A typed stock id, the way the backend keeps it (str(...).strip(), and the
// exchange codes are upper-case): trimmed and upper-cased, so 00679b is the
// ETF 00679B and not a different name. Returns "" for anything that is not a
// plain half-width code -- a full-width digit, a space inside, a symbol -- so a
// caller can reject it instead of saving an id no quote will ever match.
// Four to seven characters: ordinary shares are 4, warrants and some listings
// 5-6, ETFs/ETNs up to 7 (isEtfCode).
const STOCK_ID_RE = /^[0-9A-Z]{4,7}$/;
function normStockId(raw) {
  const s = String(raw === null || raw === undefined ? "" : raw).trim().toUpperCase();
  return STOCK_ID_RE.test(s) ? s : "";
}

function isEtfCode(stockId) {
  const sid = String(stockId || "").trim().toUpperCase();
  return sid.length >= 4 && sid.length <= 7 && sid.startsWith("00")
    && /^[0-9]{4}$/.test(sid.slice(0, 4));
}

// `scale` lets a caller pass the price in finer units than cents (planLevel
// works in thousandths of a cent): the band bounds are scaled instead of the
// price being divided, so the comparison stays integer.
function tickSizeAt(price, stockId, scale) {
  const ladder = isEtfCode(stockId) ? ETF_TICKS : EQUITY_TICKS;
  const k = scale || 1;
  for (const [upper, tick] of ladder) {
    if (upper === null || price < upper * k) return tick;
  }
  return ladder[ladder.length - 1][1];
}

// The tick (in cents) for a price in cents. tests/test_audit_fixes_20260921.py
// pins this exact signature.
function tickSize(priceCents, stockId) {
  return tickSizeAt(priceCents, stockId, 1);
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

// The strategy's percentages as integer thousandths of the fill (2.5% -> 1025),
// so a plan level is one exact integer ratio and never a rounded float.
// Whole tenths of a percent only: tests/test_mobile_plan_parity.py pins that.
function milliOf(pct) { return Math.round(1000 + pct * 10); }
const PLAN_MILLI = {
  stop: milliOf(STRATEGY.stopPct), arm: milliOf(STRATEGY.armPct),
  lock: milliOf(STRATEGY.lockPct), target: milliOf(STRATEGY.targetPct),
  add: milliOf(STRATEGY.addPct), scale: milliOf(STRATEGY.scaleOutPct),
  late: milliOf(STRATEGY.lateGainPct),
};

// One plan level on the quote ladder, rounded ONCE from the exact ratio
// fill x milli / 1000 (scanner/tick.py round_to_tick of fill x (1 + pct)).
// 2026-10-09: this used to round to the nearest cent first and to the tick
// second, which put a lock or add level one tick off the backend on about one
// in nine sub-NT$100 fills (10.05 x 1.025 = 10.30125 became 10.30, so a 10.30
// close "armed" a lock the rule arms at 10.35). Down-levels (stop, lock, add)
// floor, up-levels (arm, target, scale-out, late take) ceil.
function planLevel(baseCents, milli, dir, stockId) {
  if (!(baseCents > 0)) return null;
  const n = baseCents * milli;                          // thousandths of a cent
  const t = tickSizeAt(n, stockId, 1000);               // tick (cents), chosen by the raw level
  const down = dir !== "up";
  const steps = (x, step) => (down ? divFloor(x, step) : -divFloor(-x, step));
  let v = steps(n, t * 1000) * t;                       // cents
  // scanner.tick._snap: a result that crossed into the next band must still be
  // on that band's ladder (a no-op on both ladders today, kept for parity).
  const t2 = tickSize(v, stockId);
  if (t2 !== t && v % t2 !== 0) v = steps(v, t2) * t2;
  return v > 0 ? v : null;
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
  const keep = new Set(fresh.map((m) => m.position_id + "\u0000" + m.session_date));
  const stale = [];
  for (const pos of touched) {
    for (const m of (STATE.marksByPos[pos.position_id] || [])) {
      if (m.session_date < lo || m.session_date > hi) continue;
      if (keep.has(pos.position_id + "\u0000" + m.session_date)) continue;
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
  recs: null,              // recommendations.json rows once fetched
  recsState: "idle",       // idle | loading | ready | missing
  recOpen: {},             // {recommendation_id: true} for a day-by-day table the user opened

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
  refOpen: false,          // the 參考 group's <details>, kept across re-renders
  orderOpen: {},           // {stock_id: false} for an order guide the user closed
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
  if (STATE.recsState !== "loading") STATE.recsState = "idle";   // re-read on the next perf render
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
    return { tone: "on", enterOk: true, asOf, text: "大盤順風（20MA 上緣 <2.2%）· 可開新倉，新倉部位不加大" };
  }
  if (reg.risk_on) {
    return { tone: "mid", enterOk: false, asOf, text: "大盤中性（跌破 20MA）· 暫停開新倉；已持有的依各自出場規則" };
  }
  return { tone: "off", enterOk: false, asOf, text: "大盤逆風（跌破 60MA）· 暫停開新倉；已持有的依各自出場規則，不因大盤提前賣" };
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

// The market leg of the ride, per date: meta.market_leg = {"YYYY-MM-DD": bool}
// (scanner/market_leg.disturbed_by_date, the last ~30 sessions). true = TAIEX
// closed below its 20-day mean and above its 60-day mean that day. A date the
// map does not carry is UNKNOWN -- the backend treats it as "leg off" (an
// unknown market never extends a trade) and so does the boolean below, but the
// caller can tell "false" from "not published" and word it honestly. An older
// payload, or a cached copy from before the field existed, has no map at all:
// every date is then unknown and the phone behaves as it did without the leg.
function marketLegFor(date) {
  const map = STATE.meta && STATE.meta.market_leg;
  if (!map || typeof map !== "object" || Array.isArray(map)) return null;
  const v = map[String(date || "").slice(0, 10)];
  return v === true ? true : v === false ? false : null;
}

// A 5-bar mean that is not a whole cent (sum / 5 has a tenth of a cent in it)
// printed exactly: 8.006, not 8.01. The ride decision compares against the
// exact mean, so the screen must not show a rounded one next to a verdict the
// rounded one contradicts.
function fmtMean5(sumCents) {
  const mills = sumCents * 2;                      // sum / 5 cents, in 1/1000 TWD
  const whole = Math.floor(mills / 1000);
  const frac = mills % 1000;
  const f = frac % 10 === 0 ? String(frac / 10).padStart(2, "0") : String(frac).padStart(3, "0");
  return whole.toLocaleString("en-US") + "." + f;
}

// The ride past the time exit, bar by bar, the way scanner.exit_rules.
// replay_exit decides it. From the time exit's close on, the position is kept
// at EVERY close that is above its own 5-bar mean OR falls on a day the market
// leg is on, and only until the cap; the first close that is neither ends it
// there, whatever later closes do. Judging the latest bar alone (what this
// used to do) says "keep riding" on day 12 about a trade the rule closed at
// day 10's close, and "exit" on a day the market leg keeps it.
//
// own  = this close > the mean of the five closes ending here (exact: 5 x close
//        against the sum), null when any of the five is unpriced;
// mk   = marketLegFor(that session), null when unknown;
// A bar whose verdict is unknown (null where the other leg is false) never
// proves the ride broke: only own === false AND mk === false does.
function rideState(pos, marks) {
  const horizon = pos.horizon_days || STRATEGY.horizon;
  const cap = pos.cap_days || STRATEGY.cap;
  const legAt = (k) => {
    const m = marks[k];
    const win = k >= 4 ? marks.slice(k - 4, k + 1) : [];
    const sum = win.length === 5 && win.every((x) => x.close_price !== null)
      ? win.reduce((a, x) => a + x.close_price, 0) : null;
    const own = sum !== null && m.close_price !== null ? m.close_price * 5 > sum : null;
    const mk = marketLegFor(m.session_date);
    // an unpriced close decides nothing (the backend books "na", not a ride)
    return { own, mk, sum, keep: m.close_price !== null && (own === true || mk === true) };
  };
  const n = marks.length;
  const out = {
    horizon, cap, own: null, market: null, sum: null, riding: false, via: "",
    broken_on: "", cap_hit: false, market_published: marketLegPublished(),
  };
  if (!n) return out;
  const last = legAt(n - 1);
  const day = marks[n - 1].day_index;
  out.own = last.own; out.market = last.mk; out.sum = last.sum;
  out.cap_hit = day !== null && day >= cap;
  if (day !== null && day > horizon) {
    for (let k = 0; k < n - 1; k++) {
      const m = marks[k];
      if (m.day_index === null || m.day_index < horizon) continue;
      const e = legAt(k);
      if (!e.keep && e.own === false && e.mk === false) { out.broken_on = m.session_date; break; }
    }
  }
  out.riding = last.keep && !out.cap_hit && !out.broken_on;
  out.via = !out.riding ? "" : last.own === true ? "own" : "market";
  return out;
}

function marketLegPublished() {
  const map = STATE.meta && STATE.meta.market_leg;
  return !!map && typeof map === "object" && !Array.isArray(map);
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
  const sid = pos.stock_id;
  // Each level once, from the exact ratio, already on the quote ladder: a stop,
  // lock or add level rounds DOWN, an arm, target, scale-out or late-take level
  // rounds UP -- "never claim a better price than is orderable".
  const stop0 = planLevel(base, PLAN_MILLI.stop, "down", sid);
  const arm = planLevel(base, PLAN_MILLI.arm, "up", sid);
  const lock = planLevel(base, PLAN_MILLI.lock, "down", sid);
  const target = planLevel(base, PLAN_MILLI.target, "up", sid);
  const add = planLevel(base, PLAN_MILLI.add, "down", sid);
  const scaleOut = planLevel(base, PLAN_MILLI.scale, "up", sid);
  const late = planLevel(base, PLAN_MILLI.late, "up", sid);
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
  // Decided on the EXACT threshold (close x 1000 >= fill x 1025), not on the
  // snapped arm level: backend replay_exit compares against fill x 1.025.
  const armed = high !== null && high * 1000 >= base * PLAN_MILLI.arm;
  // Staged entry: the second half is still outstanding while the position has
  // had exactly one buy.
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
  const ride = rideState(pos, marks);
  const priced = marks.filter((m) => m.close_price !== null);
  const lastClose = priced.length ? priced[priced.length - 1].close_price : null;
  const stop = armed ? Math.max(stop0, lock) : stop0;
  return {
    stock_id: sid,
    // every price below is on the ladder; the *_orderable names stay for callers
    stop, stop_orderable: stop,
    initial_stop: stop0,
    arm, lock, target, target_orderable: target,
    add, add_orderable: add, add_open: staged,
    scale_out: scaleOut, scale_out_orderable: scaleOut,
    late,
    base,
    armed, armed_on: armed ? highOn : "",
    highest_close: high,
    ma5: ride.sum !== null ? divRound(ride.sum, 5) : null,
    ma5_text: ride.sum !== null ? fmtMean5(ride.sum) : "",
    last_close: lastClose,
    horizon: ride.horizon, cap: ride.cap,
    own_leg: ride.own, market_leg: ride.market, market_published: ride.market_published,
    riding: ride.riding, ride_via: ride.via,
    ride_broken_on: ride.broken_on, cap_hit: ride.cap_hit,
  };
}

// --- the ride, in words -----------------------------------------------------
// ONE place decides what the ride sentence says, so the order table, the
// position card's advice, the state chip and the to-do list cannot disagree.
// The rule (STRATEGY.md 3.5, scanner/exit_rules.replay_exit): from the time
// exit's close on, keep the position while the close is above the stock's own
// 5-day mean OR the market is pulling back inside an uptrend that day (TAIEX
// below its 20-day mean, above its 60-day mean), at most until the cap.
const MARKET_LEG_TEXT = "加權指數收盤低於 20 日均線、仍高於 60 日均線";

function rideView(plan, dayIdx, horizon) {
  const cap = plan.cap;
  const ma = plan.ma5 === null ? "" : plan.ma5_text;
  const close = plan.last_close === null ? "-" : fmtPrice(plan.last_close);
  const head = plan.ma5 === null ? "" : `目前收盤 ${close}｜5 日均價 ${ma}｜`;
  const noMarket = plan.market_published
    ? "這天的大盤判定還沒有" : "這份資料沒有大盤判定";
  const pastHorizon = dayIdx !== null && dayIdx >= horizon;
  let state;
  if (dayIdx === null) state = "unknown";
  else if (!pastHorizon) state = "before";
  else if (plan.ride_broken_on) state = "broken";
  else if (plan.cap_hit || dayIdx >= cap) state = "cap";
  else if (plan.riding) state = plan.ride_via === "own" ? "own" : "market";
  else if (plan.ma5 === null) state = "nodata";
  else if (plan.market_leg === null) state = "pending";
  else state = "exit";

  const out = { state, alert: true, time: "", hint: "" };
  switch (state) {
    case "unknown":
      out.time = "持有天數未知，請先確認成交日期。";
      break;
    case "before": {
      out.alert = false;
      out.time = `第 ${dayIdx}/${horizon} 天。到第 ${horizon} 天收盤出場，但當天收盤若仍站上自己的 5 日均價、` +
        `或當天大盤正在回檔（${MARKET_LEG_TEXT}），就續抱，最晚第 ${cap} 天。`;
      if (plan.ma5 !== null) {
        out.hint = head + (plan.own_leg
          ? "站上，到期可續抱"
          : plan.market_leg === true
            ? "跌破，但今天大盤正在回檔；到期日若仍如此，規則續抱"
            : plan.market_leg === false
              ? `跌破，到期就出場（到期日若大盤回檔，規則改為續抱）`
              : `跌破；到期日若${MARKET_LEG_TEXT}，規則改為續抱（${noMarket}）`);
      }
      break;
    }
    case "own":
      out.alert = false;
      out.time = `第 ${dayIdx} 天（計畫 ${horizon} 天）：收盤站上自己的 5 日均價 ${ma}，規則續抱；每天收盤重新判斷，最晚第 ${cap} 天。`;
      out.hint = head + "站上，續抱";
      break;
    case "market":
      out.alert = false;
      out.time = `第 ${dayIdx} 天（計畫 ${horizon} 天）：收盤沒有站上自己的 5 日均價 ${ma}，但當天大盤正在回檔（${MARKET_LEG_TEXT}），規則續抱；` +
        `每天收盤重新判斷，最晚第 ${cap} 天。`;
      out.hint = head + "跌破，但大盤回檔中，續抱";
      break;
    case "exit":
      out.time = `第 ${dayIdx} 天（計畫 ${horizon} 天）：收盤沒有站上自己的 5 日均價 ${ma}，當天大盤也沒有在回檔，規則於收盤出場。`;
      out.hint = head + "跌破，大盤也沒在回檔，到期就出場";
      break;
    case "pending":
      out.time = `第 ${dayIdx} 天（計畫 ${horizon} 天）：收盤沒有站上自己的 5 日均價 ${ma}。若當天${MARKET_LEG_TEXT}，規則改為續抱——` +
        `${noMarket}，下一次掃描會確認，確認前照期滿處理。`;
      out.hint = head + "跌破；大盤是否回檔待確認";
      break;
    case "nodata":
      out.time = `第 ${dayIdx} 天（計畫 ${horizon} 天）：自己的 5 日均價資料不足，無法判斷是否續抱；` +
        `規則是收盤站上 5 日均價、或當天大盤回檔就續抱，請以券商行情自行確認。`;
      break;
    case "broken":
      out.time = `規則在 ${mmdd(plan.ride_broken_on)} 收盤就該出場（當天收盤沒有站上自己的 5 日均價，大盤也沒有在回檔）；` +
        `若你還持有，請依你的成交回報盡快處理。`;
      break;
    default:  // cap
      out.time = `第 ${dayIdx} 天已到最晚持有日（第 ${cap} 天）：規則於今天收盤出場。`;
  }
  return out;
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
    price: plan.target,
    label: "全部停利",
    note: `成交價 +${STRATEGY.targetPct}%`,
  });
  if (plan.scale_out < plan.target) {
    rows.push({
      side: "sell", must: false,
      price: plan.scale_out,
      label: "可賣一半（選用）",
      note: `成交價 +${STRATEGY.scaleOutPct}%；回測勝率 +0.5pp、平均報酬 -0.15pp`,
    });
  }
  if (!plan.armed) {
    rows.push({
      side: "watch", must: false,
      price: plan.arm,
      label: "收盤站上這裡 → 隔日起停損上調",
      note: `收盤 ≥ 成交價 +${STRATEGY.armPct}%，隔一個交易日起停損改掛 ${fmtPrice(plan.lock)}`,
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
    rows.push({
      side: "watch", must: true,
      price: plan.late,
      label: "收盤在這之上 → 隔日開盤就收下",
      note: `第 ${STRATEGY.lateFrom} 天起，只要收盤還高於成交價 +${STRATEGY.lateGainPct}%（已蓋過手續費與證交稅）就先出場，不要把獲利帶進最後一天`,
    });
  }
  rows.push({
    side: "stop", must: true,
    price: plan.stop,
    label: plan.armed ? "跌破全部出場（鎖利價）" : "跌破全部出場（災難停損）",
    note: plan.armed
      ? `鎖利已啟動，這個價位只升不降`
      : `成交價 ${STRATEGY.stopPct}%；災難停損，最大虧損的保護價位。標「必守」的都是規則本身的出場（停損、停利、後期收利），只有加碼與賣一半是選用`,
  });
  if (plan.add_open && !past) {
    rows.push({
      side: "buy", must: false,
      price: plan.add,
      label: "可加碼（選用）",
      note: `成交價 ${STRATEGY.addPct}%；先買一半的買法在此補滿。加碼會放大最大虧損，出場價位仍以第一筆成交價計算`,
    });
  }

  const order = { sell: 0, watch: 1, stop: 2, buy: 3 };
  rows.sort((a, b) => (order[a.side] - order[b.side]) || (b.price - a.price));

  const rv = rideView(plan, dayIdx, horizon);
  const ride = rv.hint ? `<div class="hint">${esc(rv.hint)}</div>` : "";

  return `<div class="sec-title">明日委託（收盤後更新，價位皆可直接掛單）</div>` +
    `<table class="tbl"><thead><tr><th>價位</th><th>動作</th><th>必守</th></tr></thead><tbody>${
      rows.map((r) => `<tr class="${r.side === "stop" ? "neg" : ""}">
        <td><b>${esc(fmtPrice(r.price))}</b></td>
        <td>${esc(r.label)}<br><span class="hint">${esc(r.note)}</span></td>
        <td>${r.must ? "必守" : "選用"}</td></tr>`).join("")
    }</tbody></table>` +
    `<div class="plan">時間：${esc(rv.time)}</div>` + ride;
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
    const plan = activePlan(pos);
    if (m.day_index !== null && m.day_index >= horizon && pos.open_shares > 0) {
      // A position the rule keeps (own 5-day mean or the market leg) is not
      // waiting for a sell to be logged; say what it IS waiting for.
      const keeps = plan && plan.riding;
      out.push({
        kind: "d10", pos,
        text: keeps
          ? `${name}：第 ${m.day_index} 個交易日已過計畫（${horizon} 天），規則續抱中（${
            plan.ride_via === "own" ? "收盤站上自己的 5 日均價" : "大盤回檔"}），收盤後再確認`
          : `${name}：第 ${m.day_index} 個交易日已到（計畫 ${horizon} 天），尚未登錄賣出`,
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
  // List freeze (scanner/list_freeze.py). A provisional list can still change;
  // a revised final one says why it changed.
  const ls = listStatus();
  if (ls && ls.state === "provisional") {
    out.push(noticeHtml("warn", `⏳ 名單${listStatusText(ls)}`));
  } else if (ls && ls.state === "final" && (Number(ls.revision) || 1) > 1) {
    out.push(noticeHtml("info", `ℹ 名單${listStatusText(ls)}`));
  }
  const rq = m.quality && m.quality.restrictions;
  if (rq && typeof rq === "object" && rq.ok === false) {
    out.push(noticeHtml("warn", "⚠ 處置／注意股名單本次未取得：卡片上的交易限制可能不完整，下單前請在券商 App 確認"));
  }
  // Worst first (errors, then warnings, then info): the notices live in the
  // sticky top bar and are height-capped there (styles.css .notices), so the
  // ones that fit must be the ones that matter. Array.sort is stable.
  const tone = (h) => (h.startsWith('<div class="notice err"') ? 0
    : h.startsWith('<div class="notice warn"') ? 1 : 2);
  out.sort((a, b) => tone(a) - tone(b));
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
  $("#asof").textContent = (dd ? `行情截至 ${dd} 收盤` : "尚無行情日期") + listAsofSuffix();
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
      <div class="asof-line"><span>清單狀態</span><b>${esc(listStatusText(listStatus()))}</b></div>
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
      <button type="button" class="stat" data-act="page" data-page="picks">今日可買 ${picks.length}</button>
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
// The rule's ACTUAL record on the signals this scanner published, built by
// scanner/live_record.py and shipped in meta.live_record. 2026-09-23: the
// owner reported poor real profit while this card quoted only the backtest
// (70.8% / +1.95%). Both were true -- 2026-06-25..09-22 produced nine
// tradable signals and a negative sum -- and nothing on the phone said so.
// Three buckets, because the list shows more than the rule buys: what the
// rule bought, what the list showed but the badge refused, and CORE+ names
// on days the market gate was shut.
function liveRecordHtml() {
  const rec = STATE.meta && STATE.meta.live_record;
  if (!rec || !rec.tradable) return "";
  const line = (b) => {
    if (!b || !b.closed) return "0 筆已結束";
    return `${b.closed} 筆 · 勝率 ${esc(fmt(b.win_pct, 1))}% · 平均 ${esc(fmtSigned(b.mean_pct, 2))}% · 合計 ${esc(fmtSigned(b.sum_pct, 1))}%`;
  };
  const t = rec.tradable;
  const open = t.open ? `，進行中 ${t.open} 筆` : "";
  const carried = rec.carried_forward ? "，沿用上一次掃描算的" : "";
  return `<div class="strategy-meta live-record">
    <b>帳本實際（${esc(rec.since || "?")} 起，到 ${esc(rec.through || "?")}${carried}）</b><br>
    符合完整買進規則：${line(t)}${open}<br>
    名單上但不可買（未過核心+）：${line(rec.not_core)}<br>
    核心+ 但大盤未順風：${line(rec.regime_closed)}<br>
    同一套出場、隔日開盤進場、含手續費與證交稅。回測是 2017–2026 的平均；這裡是這支掃描器真的發出的訊號。
  </div>`;
}

function strategyCardHtml() {
  const card = MODE_CARDS[STATE.meta.mode] || MODE_CARD_DEFAULT;
  return `<details class="strategy ${card.tone}">
    <summary>${esc(card.summary)}</summary>
    <div class="strategy-body">${esc(card.body)}</div>
    ${liveRecordHtml()}
    <div class="strategy-meta">模式 ${esc(STATE.meta.mode || "未知")}｜策略版本 ${esc(STATE.meta.strategy_version || "未提供")}</div>
  </details>`;
}

/* ============================================================================
 * 10b. Investor views (2026-10-08): picks groups, list status, investor info,
 *      sizing, order guide, system record, per-name history, events
 *
 * Everything below READS backend columns and meta and degrades to nothing,
 * or to an honest "not provided", when a field is absent: an older payload
 * (no meta.list_status / report_sources / events, no Prev_* / restriction /
 * event columns, a live_record without by_sid / bench / dates) renders as
 * before, minus the new lines. None of it changes what is buyable:
 * buyVerdict() is still the only gate, and it only ever downgrades.
 * ==========================================================================*/

const mmdd = (d) => String(d || "").slice(5, 10);
const hhmm = (ts) => String(ts || "").slice(11, 16);
const N_ENTER_UI = 20;      // scanner/scan_mode.N_ENTER (display only)

// Days since the epoch for a YYYY-MM-DD string (null when it is not one).
function dayNum(d) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d || ""));
  if (!m) return null;
  return Math.round(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / 86400000);
}

function fillTpl(tpl, vals) {
  return String(tpl).replace(/\{(\w+)\}/g, (all, k) =>
    (vals[k] === undefined || vals[k] === null ? "" : String(vals[k])));
}

// Money in NT$ (not cents) as 億 / 萬, for turnover-sized numbers.
function fmtBigNtd(ntd) {
  const n = num(ntd);
  if (n === null) return "-";
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + " 億";
  if (Math.abs(n) >= 1e4) return Math.round(n / 1e4).toLocaleString("en-US") + " 萬";
  return Math.round(n).toLocaleString("en-US") + " 元";
}

// --- groups (P1-1) -----------------------------------------------------------
// buy  = the backend says Buy_Ready and nothing downgraded it;
// hold = the owner holds it, an active recommendation follows it, or the
//        OTC simulation is holding it / exits it today. Tracked rows join
//        here only, de-duplicated by stock id with the list row winning;
// ref  = everything else (every TSE row, OTC rows that exited earlier).
const HOLD_ACTIVE = new Set(["holding", "delay", "exit_today", "overdue"]);
const HOLD_TEXT = {
  pending: "待進場", holding: "模擬持有中", delay: "續抱中",
  exit_today: "今日出場", overdue: "資料缺漏（逾期）", exited: "已出場",
};

function heldIds() {
  return new Set(STATE.positions.filter((p) => p.status === "open")
    .map((p) => String(p.stock_id)));
}

function exitIsToday(r) {
  const xd = String(r.Exit_Signal_Date || "").slice(0, 10);
  return !!(r.Exit_Signal && xd && xd === effectiveDataDate());
}

function pickGroup(r, held) {
  if (buyVerdict(r).ok) return "buy";
  const sid = String(r.Stock_ID);
  if (held && held.has(sid)) return "hold";
  const rs = String(r.Rec_Status || "");
  if (r.Recommendation_ID && (rs === "active" || rs === "converted")) return "hold";
  if (String(r.Market || "") === "OTC") {
    if (HOLD_ACTIVE.has(String(r.Hold_Status || ""))) return "hold";
    if (exitIsToday(r)) return "hold";
  }
  return "ref";
}

function pickGroups() {
  const held = heldIds();
  const out = { buy: [], hold: [], ref: [] };
  const seen = new Set();
  for (const r of STATE.rows) {
    seen.add(String(r.Stock_ID));
    out[pickGroup(r, held)].push(r);
  }
  for (const r of STATE.tracked) {
    const sid = String(r.Stock_ID);
    if (seen.has(sid)) continue;
    seen.add(sid);
    if (pickGroup(r, held) === "hold") out.hold.push(r);
  }
  return out;
}

// Today's actions first: exits due, then the extended holds, then the rest.
function holdPriority(r) {
  const st = String(r.Hold_Status || "");
  if (st === "exit_today" || st === "overdue" || exitIsToday(r)) return 0;
  if (st === "delay") return 1;
  if (st === "holding") return 2;
  if (st === "pending" || !st) return 3;
  return 4;
}

// The one-line funnel, from Buy_Block and the list order only.
function picksSummaryText(groups) {
  const g = groups || pickGroups();
  const rows = STATE.rows;
  const buy = g.buy.length;
  if (!rows.length) return `今日可買 ${buy}｜今日 0 檔入選`;
  const reg = regimeView();
  const gate = rows.find((r) => r.Buy_Block === "regime" || r.Buy_Block === "regime_stale");
  if (!reg.enterOk || gate) {
    return `今日可買 ${buy}｜${!reg.enterOk ? reg.text : BLOCK_TEXT[gate.Buy_Block]}`;
  }
  const top = rows.filter((r) => r._rank && r._rank <= N_ENTER_UI);
  const otc = top.filter((r) => String(r.Market) === "OTC");
  const coreOk = (r) => (typeof r.Core_Plus === "boolean" ? r.Core_Plus
    : ["", "held", "restricted", "unknown"].includes(String(r.Buy_Block || "")));
  const core = otc.filter(coreOk).length;
  const held = otc.filter((r) => r.Buy_Block === "held").length;
  const tse = top.filter((r) => String(r.Market) === "TSE").length;
  let s = `今日可買 ${buy}｜上櫃前${N_ENTER_UI} ${otc.length}（CORE+ ${core}，非新訊號 ${held}）｜上市 ${tse}（僅參考）`;
  const restricted = rows.filter((r) => r.Buy_Block === "restricted").length;
  if (restricted) s += `｜交易受限 ${restricted}`;
  const disp = top.filter((r) => r.Trade_Restriction === "disposition").length;
  if (disp) s += `｜處置 ${disp}`;
  return s;
}

// --- list status (P1-9, scanner/list_freeze.py meta.list_status) ------------
const LIST_REASON_TEXT = {
  data_lag: "資料尚未更新到今天",
  degraded: "資料源異常",
  unchecked: "尚未完成欄位自檢",
  checks_fail: "欄位自檢未通過",
  regime_stale: "大盤資料尚未更新",
  regime_unknown: "大盤狀態無法判讀",
  empty_unflagged: "零檔但未標示為正常空結果",
  too_early: "15:00 前的執行",
};
const LIST_REVISED_TEXT = {
  force_rescan: "手動強制重建",
  pages_behind: "上次發布未成功，重新發布",
  restriction_info: "晚間處置／注意股名單補充，只改限制欄位",
};

function listStatus() {
  const ls = STATE.meta && STATE.meta.list_status;
  return ls && typeof ls === "object" && ls.state ? ls : null;
}

// One sentence about how final this list is. An old payload has no
// list_status: then only the time of day is known, and it is never "final".
function listStatusText(ls) {
  if (!ls) {
    const h = hhmm(STATE.meta && STATE.meta.scan_time);
    if (!/^\d{2}:\d{2}$/.test(h)) return "產生時間未知";
    const mins = Number(h.slice(0, 2)) * 60 + Number(h.slice(3, 5));
    if (mins < 15 * 60) return `產生於 ${h}（15:00 前的版本）`;
    if (mins >= 18 * 60) return `產生於 ${h}（晚間重跑版本，名單可能與下午不同）`;
    return `產生於 ${h}`;
  }
  const first = hhmm(ls.first_published_at || ls.published_at);
  if (ls.state === "final") {
    const rev = Number(ls.revision) || 1;
    if (rev > 1) {
      const why = LIST_REVISED_TEXT[ls.revised_reason] || ls.revised_reason || "修訂";
      return `已定案・第 ${rev} 版（${why}${ls.revised_at ? "，" + hhmm(ls.revised_at) : ""}；首次發布 ${first}）`;
    }
    return `已定案（${first} 發布，今晚重跑不會改名單）`;
  }
  const why = (Array.isArray(ls.reasons) ? ls.reasons : [])
    .map((c) => LIST_REASON_TEXT[c] || c).join("、");
  return `暫定版（${hhmm(ls.published_at) || "?"} 產生${why ? "；原因：" + why : ""}；雲端會繼續重試，名單可能變動）`;
}

function listStampHtml() {
  const ls = listStatus();
  const dd = effectiveDataDate();
  const head = `名單資料日 ${dd ? mmdd(dd) : "未知"}（盤後）`;
  const final1 = !!(ls && ls.state === "final" && (Number(ls.revision) || 1) === 1);
  const state = final1
    ? `本日清單已凍結・${hhmm(ls.first_published_at || ls.published_at)} 定案，今晚重跑不會改名單`
    : listStatusText(ls);
  const cls = ls ? (ls.state === "final" ? "final" : "prov") : "";
  return `<div class="list-state ${cls}">${esc(head)} · ${esc(state)}</div>`;
}

function listAsofSuffix() {
  const ls = listStatus();
  if (!ls) return "";
  if (ls.state === "final") return ` · 清單 ${hhmm(ls.first_published_at || ls.published_at)} 已定案`;
  return " · 暫定版";
}

// --- restrictions (P0-1 phone half) ------------------------------------------
function restrictionInfo(r) {
  const kind = String(r.Trade_Restriction || "");
  const flags = String(r.Restriction_Flags || "").split(",").map((s) => s.trim()).filter(Boolean);
  return {
    kind: kind === "none" ? "" : kind,
    flags,
    since: String(r.Restriction_Since || "").slice(0, 10),
    until: String(r.Restriction_Until || "").slice(0, 10),
    match: num(r.Restriction_Match_Min),
    prepay: String(r.Restriction_Prepay || ""),
    limitDown: flags.includes("limit_down"),
  };
}

function restrictionNote(r) {
  const x = restrictionInfo(r);
  if (!x.kind) return x.limitDown ? RESTRICT_NOTE.limit_down : "";
  const parts = {
    until: x.until ? fillTpl(RESTRICT_NOTE.until, { until: x.until }) : "",
    match: x.match !== null ? fillTpl(RESTRICT_NOTE.match, { min: x.match })
      : (x.kind === "disposition" ? RESTRICT_NOTE.match_unknown : ""),
    prepay: x.prepay === "all" ? RESTRICT_NOTE.prepay_all
      : x.prepay === "threshold" ? RESTRICT_NOTE.prepay_threshold : "",
    label: RESTRICT_TEXT[x.kind] || x.kind,
  };
  const tpl = String(r.Buy_Block || "") === "restricted"
    ? RESTRICT_NOTE.blocking : (RESTRICT_NOTE[x.kind] || parts.label);
  return fillTpl(tpl, parts);
}

function restrictionBadge(r) {
  const x = restrictionInfo(r);
  const out = [];
  if (x.kind) {
    out.push(`<span class="tag ${RESTRICT_TONE[x.kind] || "info"}">${esc(RESTRICT_TEXT[x.kind] || x.kind)}${
      x.until ? esc(` 至 ${mmdd(x.until)}`) : ""}</span>`);
  }
  if (x.limitDown) out.push(`<span class="tag info">${esc(RESTRICT_TEXT.limit_down)}</span>`);
  return out.join("");
}

// The order-side explanation for a restricted name. It explains EXECUTION
// (periodic call auction, prepayment, exits that may fill late) and never
// discourages the trade: disposition-entry signals were the best bucket
// (BACKTEST_LOG M.1). Minutes and prepayment come from the parsed columns.
function restrictionGuidance(r) {
  const x = restrictionInfo(r);
  const note = restrictionNote(r);
  if (!note) return "";
  let extra = "";
  if (x.kind === "disposition") {
    extra = "撮合間隔與預收方式取自交易所公告；回測中處置期間進場的訊號並沒有比較差（BACKTEST_LOG M.1），這裡只說明下單方式。";
  } else if (x.kind === "unknown") {
    extra = "名單讀取失敗不代表沒有限制。";
  }
  return noticeHtml(RESTRICT_TONE[x.kind] || "info", note) +
    (extra ? `<div class="hint">${esc(extra)}</div>` : "");
}

// --- events (P1-6 phone half; ingestion/company_events.py) -------------------
// Display only. The revenue month is always labelled and never coloured as
// good or bad (BACKTEST_LOG M.6: as a filter every grouping was rejected).
const EX_KIND_TEXT = { div: "除息", right: "除權", both: "除權息" };

function eventsLine(r) {
  const parts = [];
  const rm = String(r.Rev_Month || "");
  if (/^\d{4}-\d{2}$/.test(rm)) {
    const bits = [];
    if (num(r.Rev_YoY_Pct) !== null) bits.push(`年增 ${fmtSigned(r.Rev_YoY_Pct, 1)}%`);
    if (num(r.Rev_MoM_Pct) !== null) bits.push(`月增 ${fmtSigned(r.Rev_MoM_Pct, 1)}%`);
    if (num(r.Rev_Cum_YoY_Pct) !== null) bits.push(`累計年增 ${fmtSigned(r.Rev_Cum_YoY_Pct, 1)}%`);
    parts.push(`${rm.slice(0, 4)} 年 ${Number(rm.slice(5, 7))} 月營收${bits.length ? " " + bits.join("、") : ""}`);
  }
  const ex = String(r.Ex_Date || "").slice(0, 10);
  if (ex) {
    const cash = num(r.Ex_Cash_Div);
    parts.push(`${EX_KIND_TEXT[r.Ex_Kind] || "除權息"} ${mmdd(ex)}${cash !== null && cash > 0 ? `（現金 ${fmt(cash, 2)} 元）` : ""}`);
  }
  const cf = String(r.Conf_Date || "").slice(0, 10);
  if (cf) parts.push(`法說會 ${mmdd(cf)}`);
  return parts.join("｜");
}

// A badge when an ex-date or an investor conference is within a week.
function eventBadge(r) {
  const base = dayNum(effectiveDataDate());
  if (base === null) return "";
  const out = [];
  const ex = dayNum(r.Ex_Date);
  if (ex !== null && ex - base >= 0 && ex - base <= 7) {
    out.push(`<span class="tag info">${esc((EX_KIND_TEXT[r.Ex_Kind] || "除權息") + " " + mmdd(r.Ex_Date))}</span>`);
  }
  const cf = dayNum(r.Conf_Date);
  if (cf !== null && cf - base >= 0 && cf - base <= 7) {
    out.push(`<span class="tag info">${esc("法說 " + mmdd(r.Conf_Date))}</span>`);
  }
  return out.join("");
}

// --- per-name history (P1-8; meta.live_record.by_sid, else its trades) -------
const EXIT_REASON_TEXT = {
  tp: "停利", stop: "停損", lock: "鎖利", late: "後期收下", time: "期滿", "": "進行中",
};

// null when the payload has no record at all; [] when it has one and this
// name has no signal in it. Oldest first.
function nameHistory(sid) {
  const rec = STATE.meta && STATE.meta.live_record;
  if (!rec) return null;
  const id = String(sid).trim().toUpperCase();
  // live_record ships {sid: [entries]}; a flat [entries with sid] list is
  // read too, so a reshaped payload degrades to "no history", never to a
  // wrong name's history.
  if (Array.isArray(rec.by_sid)) {
    return rec.by_sid.filter((e) => e && typeof e === "object" && String(e.sid).toUpperCase() === id)
      .sort((a, b) => String(a.sig).localeCompare(String(b.sig)));
  }
  if (rec.by_sid && typeof rec.by_sid === "object") {
    return (Array.isArray(rec.by_sid[id]) ? rec.by_sid[id] : [])
      .filter((e) => e && typeof e === "object");
  }
  const trades = (rec.tradable && Array.isArray(rec.tradable.trades)) ? rec.tradable.trades : [];
  return trades.filter((t) => String(t.sid) === id)
    .map((t) => Object.assign({ bucket: "tradable" }, t))
    .sort((a, b) => String(a.sig).localeCompare(String(b.sig)));
}

function historyEntryText(e) {
  const out = num(e.ret) !== null
    ? `${EXIT_REASON_TEXT[e.exit] || e.exit || "出場"} ${fmtSigned(e.ret, 2)}%${num(e.bars) !== null ? `（${e.bars} 日）` : ""}`
    : "進行中";
  const bucket = e.bucket === "not_core" ? "（非核心，未計入）"
    : e.bucket === "regime_closed" ? "（大盤未順風，未計入）" : "";
  return `${mmdd(e.sig)} 訊號 → ${out}${bucket}`;
}

// The latest signal BEFORE today's (today's own signal is the card itself).
function lastSignalText(sid) {
  const h = nameHistory(sid);
  if (h === null) return "";
  const today = effectiveDataDate();
  const prev = h.filter((e) => String(e.sig || "") < today);
  return prev.length ? historyEntryText(prev[prev.length - 1]) : "首次";
}

function nameHistoryLine(sid) {
  const h = nameHistory(sid);
  if (h === null) return "";
  if (!h.length) return `<div class="hint">系統紀錄：近期無系統訊號紀錄</div>`;
  return `<div class="hint">系統紀錄：${esc(h.slice(-3).map(historyEntryText).join("；"))}</div>`;
}

// --- investor info (P1-2) ----------------------------------------------------
function investorInfoHtml(r) {
  const dist = num(r.Dist_52W_High_Pct);
  const vt = num(r.Vol_Today), vm = num(r.Vol_MA20), close = num(r.Close_Price);
  const ratio = vt !== null && vm ? vt / vm : null;
  const x = restrictionInfo(r);
  const rnote = restrictionNote(r);
  const last = lastSignalText(r.Stock_ID);
  const ev = eventsLine(r);
  const rlabel = x.kind ? (RESTRICT_TEXT[x.kind] || x.kind)
    : x.limitDown ? RESTRICT_TEXT.limit_down
    : (r.Trade_Restriction !== undefined ? "無" : "未提供");
  const cells =
    kv("近20日漲幅", esc(fmtSigned(r.Gain_1M_Pct, 1, "%")), "", "約 20 個交易日") +
    kv("距近一年最高收盤", dist === null ? "-" : dist === 0 ? "在高點" : esc(`-${fmt(Math.abs(dist), 1)}%`), "",
       "近一年收盤最高，不是盤中") +
    kv("今日量/20日均量", ratio === null ? "-" : esc(ratio.toFixed(2) + "×"), ratio !== null && ratio < 0.5 ? "warn" : "",
       vt === null ? "" : `今日 ${vt.toLocaleString("en-US")} 張`) +
    kv("成交金額約", vt !== null && close !== null ? esc(fmtBigNtd(close * vt * 1000)) : "-", "",
       "收盤價 × 成交量估算") +
    kv("交易限制", esc(rlabel), x.kind ? "warn" : "", rnote) +
    (last ? kv("上次系統訊號", esc(last), "", "") : "");
  return `<div class="kv2 info">${cells}</div>` +
    (ev ? `<div class="hint">${esc(ev)}（月營收為最新已公布月份，不代表好壞）</div>` : "");
}

// --- recommendation view (P0-2 / P0-6 phone half) ----------------------------
const REC_STATUS_TEXT = {
  active: "有效", expired: "已失效", converted: "已轉為持倉", cancelled: "已取消",
  closed: "已結案", superseded: "已撤回",
};
const REC_REASON_TEXT = {
  time: "期滿出場", stop: "停損", lock: "鎖利", tp: "停利", late: "後期收下",
  no_fill: "進場日未成交", horizon_elapsed: "逾時未結案", rule_version: "規則版本更新",
  degraded_run: "由資料源異常的掃描建立", off_list: "已掉出清單",
};

function recReasonText(reason) {
  const s = String(reason || "");
  if (!s) return "";
  if (s.startsWith("retracted:")) {
    const code = s.slice("retracted:".length);
    return "撤回：" + (REC_REASON_TEXT[code] || BLOCK_TEXT[code] || code);
  }
  return REC_REASON_TEXT[s] || s;
}

// A time exit dated after the regime's own date is waiting for the index:
// the backend keeps such a recommendation active until the market leg of the
// ride can be judged (portfolio/sync.py "exit_deferred").
function provisionalTimeExit(r) {
  const reg = (STATE.meta && STATE.meta.regime) || {};
  const asOf = String(reg.as_of_date || "").slice(0, 10);
  const xd = String(r.Exit_Signal_Date || "").slice(0, 10);
  return String(r.Rec_Status || "") === "active" && r.Exit_Signal === "time" &&
    !!xd && !!asOf && xd > asOf;
}

function recView(r, group) {
  const rs = String(r.Rec_Status || "");
  const st = String(r.Hold_Status || "");
  const valid = String(r.Rec_Valid_Until || "").slice(0, 10);
  const stop = cents(r.Initial_Stop_Price), target = cents(r.Initial_Target_Price);
  const waiting = !!r.Recommendation_ID && rs === "active" && (group === "buy" || !st || st === "pending");
  // Still "pending" although the entry session is already in the data (an
  // older recommendation on a row the tracker re-anchored, e.g. 1815 on
  // 10-07: recommended 09-09, pending, dropped). Never "enter at the next
  // open" -- the rule only enters at the first open after its signal.
  const on = String(r.Recommended_On || "").slice(0, 10);
  const today = effectiveDataDate();
  const missed = waiting && group !== "buy" && !!today &&
    (valid ? valid <= today : (!!on && on < today));
  let label = "";
  if (r.Recommendation_ID) {
    if (rs === "active") {
      label = missed ? `有效・${on ? mmdd(on) + " 建議，" : ""}進場日已過但沒有模擬進場紀錄（不是今天的買點）`
        : waiting ? (valid ? `有效・${mmdd(valid)} 開盤進場` : "有效・下一個交易日開盤進場")
        : "已進場（模擬）";
      if (provisionalTimeExit(r)) label += "・期滿出場待大盤資料確認";
    } else if (rs === "closed") {
      label = `已結案・${recReasonText(r.Rec_Status_Reason) || "原因未提供"}`;
    } else {
      label = (REC_STATUS_TEXT[rs] || rs || "狀態未提供") +
        (r.Rec_Status_Reason ? `・${recReasonText(r.Rec_Status_Reason)}` : "");
    }
  }
  return { pending: waiting && !missed && stop !== null, missed, stop, target, label, status: rs };
}

// An exit signal that belongs to an OLDER trade than the card is about: a
// buy-group row exited on an earlier date, or an active recommendation newer
// than the exit. Older payloads ship the tracker's old streak on such rows
// (8227 on 10-07: tp 09-29 beside a new buy signal).
function staleExit(r, group) {
  if (!r.Exit_Signal) return false;
  const xd = String(r.Exit_Signal_Date || "").slice(0, 10);
  if (!xd) return false;
  if (group === "buy" && xd < effectiveDataDate()) return true;
  const on = String(r.Recommended_On || "").slice(0, 10);
  return String(r.Rec_Status || "") === "active" && !!on && xd < on;
}

// The previous trade, from the Prev_* columns (holding_tracker.PREV_COLUMNS).
function prevSegmentHtml(r) {
  const psd = String(r.Prev_Signal_Date || "").slice(0, 10);
  if (!psd) return "";
  const ped = String(r.Prev_Entry_Date || "").slice(0, 10);
  const sig = String(r.Prev_Exit_Signal || "");
  const parts = [`${mmdd(psd)} 訊號${r.Prev_Was_Signal === false ? "（當時不是買訊）" : ""}`];
  if (ped) parts.push(`${mmdd(ped)} 開盤 ${fmtPrice(cents(r.Prev_Entry_Open))} 進場`);
  let tail = "";
  if (sig) {
    const px = cents(r.Prev_Exit_Signal_Price);
    const ret = num(r.Prev_Exit_Ret_Pct);
    parts.push(`${mmdd(r.Prev_Exit_Signal_Date)} ${EXIT_REASON_TEXT[sig] || sig}${px !== null ? " @" + fmtPrice(px) : ""}${
      ret !== null ? `，淨 ${fmtSigned(ret, 2)}%` : ""}`);
    const n = num(r.Sessions_Since_Prev_Exit);
    if (n !== null) tail = `（距今 ${n} 個交易日）`;
  } else {
    parts.push("尚未出場就又出現訊號");
  }
  return `<div class="prev-seg">上一段模擬：${esc(parts.join(" → ") + tail)}</div>`;
}

// Old payloads only: the old streak's exit, shown as history, not as a badge.
function staleExitHtml(r) {
  const ed = String(r.Entry_Date || "").slice(0, 10);
  const fill = cents(r.Entry_Open), px = cents(r.Exit_Signal_Price);
  const pct = fill && px !== null ? pctOf(px - fill, fill) : null;
  const sig = String(r.Exit_Signal || "");
  return `<div class="prev-seg">上一段模擬：${esc(`${ed ? mmdd(ed) + " 進場" : "進場日未知"} → ${
    mmdd(r.Exit_Signal_Date)} ${EXIT_REASON_TEXT[sig] || sig}${pct !== null ? " " + fmtPct(pct) + "（未扣費稅）" : ""}`)}</div>`;
}

// --- sizing and fees (P1-3) --------------------------------------------------
// The durable copy is IndexedDB meta "sizing" = {capital_cents, max_slots,
// risk_pct}, beside "settings": exportBackup exports every non-private meta
// row, so the three numbers travel with a backup and runImport validates them.
// localStorage keeps a second copy (a DB-less private window still remembers
// them for the session). Every access is wrapped -- a private window or
// blocked storage throws -- and the in-memory copy, loaded from the DB at
// boot and replaced on save / import, wins over localStorage.
const SIZING_KEY = "yt_sizing_v1";
const SIZING_META = "sizing";
const SIZING_DEFAULTS = { slots: 8, risk_pct: 1 };
let SIZING_MEM = null;
const SIZING_CAPTION = "依你設定的資金與風險自動換算，非投資建議";
// BACKTEST_LOG M.4. Deliberately no single-slot-count CAGR: the few-slot
// figures are concentration, not edge.
const SLOT_GUIDANCE =
  "格數：建議 5～8 個等權格（預設 8），同一檔只持有一個部位，今天騰出的格子下一個交易日再用；" +
  "格數越多回撤越淺，避免 1～3 格。NT$20 最低手續費在 5 格約需 NT$70k、8 格約需 NT$112k 資金才不再墊高成本。" +
  "到 2025 年底這本帳每年約 12%（研究樣本）～17%（線上訊號頻率），2026 年至今是離群的一年，不要拿它當預期（BACKTEST_LOG M.4）。";

function validateSizing(input) {
  const v = input || {};
  const errs = {};
  const str = (x) => (x === null || x === undefined ? "" : String(x)).replace(/,/g, "").trim();
  const capRaw = str(v.capital), slotsRaw = str(v.slots), riskRaw = str(v.risk_pct);
  let capital = null, slots = null, risk = null;
  if (!/^\d{1,9}$/.test(capRaw)) errs.capital = "請填整數金額（新台幣元）";
  else {
    capital = Number(capRaw);
    if (capital < 10000 || capital > 100000000) errs.capital = "資金需介於 10,000 與 100,000,000 元";
  }
  if (!/^\d{1,2}$/.test(slotsRaw)) errs.slots = "請填 1～20 的整數";
  else {
    slots = Number(slotsRaw);
    if (slots < 1 || slots > 20) errs.slots = "格數需介於 1 與 20";
  }
  if (!/^\d{1,2}(\.\d{1,2})?$/.test(riskRaw)) errs.risk_pct = "請填 0.1～5 的數字（最多兩位小數）";
  else {
    risk = Number(riskRaw);
    if (risk < 0.1 || risk > 5) errs.risk_pct = "單筆風險需介於 0.1% 與 5%";
  }
  if (Object.keys(errs).length) return { ok: false, errs };
  return { ok: true, value: { capital, slots, risk_pct: risk } };
}

// {capital, slots, risk_pct} <-> the meta row {capital_cents, max_slots,
// risk_pct}. fromMeta returns null for anything validateSizing refuses.
function sizingToMeta(v) {
  return { capital_cents: v.capital * 100, max_slots: v.slots, risk_pct: v.risk_pct };
}

function sizingFromMeta(m) {
  if (!m || typeof m !== "object") return null;
  const cc = Number(m.capital_cents);
  if (!Number.isInteger(cc) || cc % 100 !== 0) return null;
  const v = validateSizing({ capital: String(cc / 100), slots: String(m.max_slots), risk_pct: String(m.risk_pct) });
  return v.ok ? v.value : null;
}

function loadSizing() {
  if (SIZING_MEM) {
    const m = validateSizing(SIZING_MEM);
    if (m.ok) return m.value;
  }
  let obj = null;
  try {
    const raw = localStorage.getItem(SIZING_KEY);
    if (raw) obj = JSON.parse(raw);
  } catch (e) { obj = null; }
  if (!obj) return null;
  const v = validateSizing(obj);
  return v.ok ? v.value : null;
}

// true when at least one durable copy was written (IndexedDB is async: its
// write is started here and a failure only costs the backup copy).
function saveSizing(value) {
  SIZING_MEM = value;
  let kept = false;
  try {
    localStorage.setItem(SIZING_KEY, JSON.stringify(value));
    kept = true;
  } catch (e) { /* blocked storage: the DB copy below, or memory only */ }
  if (DB_OK) {
    try {
      metaSet(SIZING_META, sizingToMeta(value)).catch(() => {});
      kept = true;
    } catch (e) { /* openDB refused synchronously */ }
  }
  return kept;
}

// Shares for one slot: the largest n whose cost fits the slot budget, and the
// largest whose loss at the stop (price gap + both fees + tax) fits the risk
// budget; the smaller one wins. Fees step, so each is a binary search over a
// monotone test rather than a division. Pure: no STATE.
function sizePosition(priceCents, stopCents, sizing, sched, opts) {
  const o = opts || {};
  const capCents = sizing.capital * 100;
  const slots = sizing.slots;
  const budgetCents = Math.floor(capCents / slots);
  const riskCents = Math.floor(capCents * Math.round(sizing.risk_pct * 100) / 10000);
  const P = priceCents, S = stopCents;
  const out = {
    shares: 0, lots: 0, odd: 0, budgetShares: 0, riskShares: 0,
    buyFeeCents: 0, rtPct: null, minFeeBinds: false, maxLossCents: null,
    budgetCents, riskCents, slots,
    slotsFree: Math.max(slots - (o.openCount || 0), 0),
    held: !!o.held, limit: "", reason: "",
  };
  if (!P || P <= 0) { out.limit = "price"; out.reason = out.held ? "held" : "price"; return out; }
  const largest = (hi, ok) => {
    let lo = 0;
    let top = Math.max(hi, 0);
    while (lo < top) {
      const mid = Math.floor((lo + top + 1) / 2);
      if (ok(mid)) lo = mid; else top = mid - 1;
    }
    return lo;
  };
  out.budgetShares = largest(Math.floor(budgetCents / P),
    (n) => n * P + feeFor(sched, n * P) <= budgetCents);
  const stopOk = S !== null && S !== undefined && S > 0 && S < P;
  if (stopOk) {
    const lossAt = (n) => n * (P - S) + feeFor(sched, n * P) + feeFor(sched, n * S) + taxFor(sched, n * S);
    out.riskShares = largest(Math.floor(riskCents / (P - S)), (n) => lossAt(n) <= riskCents);
  }
  out.shares = Math.min(out.budgetShares, out.riskShares);
  if (!stopOk) out.limit = "stop";
  else if (out.shares === 0) out.limit = out.budgetShares === 0 ? "price" : "risk";
  else out.limit = out.riskShares < out.budgetShares ? "risk" : "budget";
  out.lots = Math.floor(out.shares / 1000);
  out.odd = out.shares % 1000;
  if (out.shares > 0) {
    const cons = out.shares * P;
    out.buyFeeCents = feeFor(sched, cons);
    out.rtPct = divRound((2 * out.buyFeeCents + taxFor(sched, cons)) * 100000, cons) / 1000;
    out.minFeeBinds = feeBindsMin(sched, cons);
    out.maxLossCents = out.shares * (P - S) + out.buyFeeCents +
      feeFor(sched, out.shares * S) + taxFor(sched, out.shares * S);
  }
  out.reason = out.held ? "held" : out.slotsFree <= 0 ? "slots" : out.limit;
  return out;
}

// The stop sizing assumes: the recommendation's when there is an active one,
// else the card's plan stop before entry, else the rule's -20% off the price.
function sizingStop(r, P) {
  const init = cents(r.Initial_Stop_Price);
  if (r.Recommendation_ID && init !== null && String(r.Rec_Status || "") === "active") return init;
  const st = String(r.Hold_Status || "");
  const plan = cents(r.Plan_Stop);
  if (plan !== null && (!st || st === "pending")) return plan;
  return planLevel(P, PLAN_MILLI.stop, "down", r.Stock_ID);
}

function sizingFor(r) {
  const sz = loadSizing();
  if (!sz) return null;
  const init = cents(r.Initial_Buy_Price);
  const P = init !== null ? init : cents(r.Close_Price);
  if (!P) return null;
  const S = sizingStop(r, P);
  const held = heldIds();
  const res = sizePosition(P, S, sz, schedule(STATE.settings.fee_schedule),
    { held: held.has(String(r.Stock_ID)), openCount: held.size });
  return Object.assign({ sizing: sz, priceCents: P, stopCents: S }, res);
}

const SIZE_LIMIT_TEXT = { budget: "每格資金", risk: "單筆風險", price: "每格資金", stop: "停損價" };

function sizingHtml(r, s) {
  if (!s) {
    return `<div class="sizing hint">尚未設定資金：設定後會換算建議股數（${esc(SIZING_CAPTION)}）。` +
      `<div class="btns">${btn("sizing", "設定資金與格數", "")}</div></div>`;
  }
  const sz = s.sizing;
  const lines = [];
  if (s.shares > 0) {
    lines.push(`<b>建議股數 ${s.shares.toLocaleString("en-US")} 股</b>（${s.lots} 張 + ${s.odd} 股零股）`);
  } else if (s.limit === "stop") {
    lines.push("<b>停損價不低於參考價，無法換算股數</b>");
  } else if (s.limit === "price") {
    lines.push(`<b>每格資金 NT$${esc(fmtCents(s.budgetCents))} 買不起 1 股</b>`);
  } else {
    lines.push(`<b>單筆風險上限 NT$${esc(fmtCents(s.riskCents))} 連 1 股都不夠</b>（每股到停損約 NT$${esc(fmtCents(s.priceCents - s.stopCents))}）`);
  }
  lines.push(`以參考價 ${esc(fmtPrice(s.priceCents))}、停損 ${esc(fmtPrice(s.stopCents))} 估算；每格 NT$${esc(fmtCents(s.budgetCents))}（資金 ÷ ${sz.slots} 格）、單筆風險上限 ${esc(String(sz.risk_pct))}%＝NT$${esc(fmtCents(s.riskCents))}，取較小者（${esc(SIZE_LIMIT_TEXT[s.limit] || s.limit)}限制）`);
  if (s.rtPct !== null) {
    lines.push(`手續費＋稅 來回約 ${esc(s.rtPct.toFixed(3))}%（回測假設 0.585%）`);
  }
  if (s.minFeeBinds) {
    lines.push(`<span class="warn">買進金額低於約 NT$14,035，NT$20 最低手續費會墊高成本</span>`);
  }
  if (s.maxLossCents !== null) {
    lines.push(`觸停損最大虧損 約 NT$${esc(fmtCents(s.maxLossCents))}（含費稅，跳空跌破時會更多）`);
  }
  if (s.held) lines.push(`<span class="warn">你已持有此檔：同一檔只持有一個部位</span>`);
  else if (s.slotsFree <= 0) lines.push(`<span class="warn">${sz.slots} 格已滿：騰出的格子下一個交易日再用</span>`);
  else lines.push(`可用倉位 ${s.slotsFree}/${sz.slots}`);
  return `<div class="sizing plan">${lines.join("<br>")}` +
    `<div class="hint">${esc(SIZING_CAPTION)}；實際成交價是隔日開盤，股數請自行填寫。</div>` +
    `<div class="btns">${btn("sizing", "調整資金設定", "")}</div></div>`;
}

function openSizingForm() {
  const cur = loadSizing();
  const body =
    `<div class="hint">${esc(SIZING_CAPTION)}。設定只存在這支手機，會跟著「匯出備份」一起帶走，不會上傳。</div>` +
    field("capital", "投入這套規則的資金（新台幣元）", cur ? cur.capital : "",
      { inputmode: "numeric", hint: "整數，10,000～100,000,000" }) +
    field("slots", "格數（同時最多持有幾檔）", cur ? cur.slots : SIZING_DEFAULTS.slots,
      { inputmode: "numeric", hint: "1～20，預設 8；建議 5～8" }) +
    field("risk_pct", "單筆觸停損時最多虧損資金的 %", cur ? cur.risk_pct : SIZING_DEFAULTS.risk_pct,
      { inputmode: "decimal", hint: "0.1～5，預設 1" }) +
    `<div class="plan">${esc(SLOT_GUIDANCE)}</div>`;
  openModal("資金設定", body, { submit: "sizing-save", submitLabel: "儲存" });
}

function saveSizingForm() {
  const modal = $("#modal");
  const chk = validateSizing(readForm(modal));
  if (!chk.ok) {
    showErrors(modal, chk.errs);
    toast("資金設定有誤，未儲存");
    return;
  }
  const kept = saveSizing(chk.value);
  closeModal();
  toast(kept ? "已儲存資金設定" : "已套用；這個瀏覽器無法永久保存，下次開啟需重設");
  render();
}

function sizingSummaryText() {
  const sz = loadSizing();
  if (!sz) return "尚未設定";
  return `資金 NT$${sz.capital.toLocaleString("en-US")}｜${sz.slots} 格｜單筆風險 ${sz.risk_pct}%`;
}

// --- order guide (P1-4) ------------------------------------------------------
function limitUpCents(r) {
  const c = cents(r.Close_Price);
  if (!c) return null;
  return tickRound(divFloor(c * 110, 100), "down", r.Stock_ID);
}

function exitReminderText(r, rv) {
  const stop = rv && rv.pending ? rv.stop : cents(r.Plan_Stop);
  const fillTarget = cents(r.Fill_Target_Price);
  const target = rv && rv.pending ? rv.target
    : (fillTarget !== null ? fillTarget : cents(r.Target_Price));
  return `停損 ${stop !== null ? fmtPrice(stop) : "-"} 跌破先出、停利 ${target !== null ? fmtPrice(target) : "-"}（+${STRATEGY.targetPct}%）；` +
    `收盤站上 +${STRATEGY.armPct}% 後，隔一個交易日起停損上調到 +${STRATEGY.lockPct}%；` +
    `第 ${STRATEGY.lateFrom} 天起收盤仍有 +${STRATEGY.lateGainPct}% 以上就隔日開盤收下；` +
    `基本抱 ${STRATEGY.horizon} 個交易日；第 ${STRATEGY.horizon} 天起每天收盤判斷是否續抱：收盤站上自己的 5 日均價，或當天大盤正在回檔（${MARKET_LEG_TEXT}）就續抱，最晚第 ${STRATEGY.cap} 天。賣出一律掛限價`;
}

function orderGuideHtml(r, size, rv) {
  const sid = String(r.Stock_ID);
  const valid = String(r.Rec_Valid_Until || "").slice(0, 10);
  const when = valid ? `${valid}（下一個交易日）開盤前` : "下一個交易日開盤前";
  const lu = limitUpCents(r);
  const known = !!(size && size.shares > 0);
  const steps = [];
  steps.push(`<li>時間：${esc(when)}。規則假設以隔日開盤價進場。</li>`);
  if (!known || size.lots > 0) {
    steps.push(`<li class="ord-lot">整股${known ? ` ${size.lots} 張` : "（1 張 = 1,000 股）"}：${esc(ORDER_RULES.auction)}。` +
      `要確保開盤成交，限價可掛到你願意付的上限${lu ? esc(`（漲停價約 ${fmtPrice(lu)}）`) : ""}；` +
      `掛在收盤價附近或以下，開高時就買不到。</li>`);
  }
  if (!known || size.odd > 0) {
    steps.push(`<li class="ord-odd">零股${known ? ` ${size.odd} 股` : ""}：${esc(ORDER_RULES.odd_note)}，與整股分開下單。` +
      `${esc(ORDER_RULES.odd_cost)}。${esc(ORDER_RULES.odd_no_open_limit)}。${esc(ORDER_RULES.odd_min_fee)}。</li>`);
  }
  const rg = restrictionGuidance(r);
  if (rg) steps.push(`<li class="ord-restrict">${rg}</li>`);
  steps.push(`<li>出場：${esc(exitReminderText(r, rv))}。</li>`);
  const open = STATE.orderOpen[sid] !== false;
  return `<details class="order" data-order="${esc(sid)}"${open ? " open" : ""}>
    <summary>下單步驟（制度資料 ${esc(ORDER_RULES.as_of)}）</summary>
    <ol class="steps">${steps.join("")}</ol>
    <div class="hint">${esc(ORDER_RULES.anchor_note)}。處置／注意股名單約在前一晚（上櫃約 23:30、上市約隔日清晨）才公告，下午的清單看不到隔天才開始的處置，下單前請在券商 App 確認。${esc(ORDER_RULES.odd_change_note)}；${esc(ORDER_RULES.source)}。</div>
  </details>`;
}

// The hold clock the desktop prints (gui/app.py): day N of the plan, trading
// days left to the time exit, the exit date once the calendar knows it. These
// columns are display-only (not phone-critical in the registry), so they are
// shown only when they agree with one another -- the identity result_checks
// enforces (Hold_Day + Hold_Remaining == Hold_Total) -- and never guessed.
function holdClock(r) {
  const day = num(r.Hold_Day), rem = num(r.Hold_Remaining), total = num(r.Hold_Total);
  if (day === null || rem === null || total === null) return null;
  if (!Number.isInteger(day) || !Number.isInteger(rem) || !Number.isInteger(total)) return null;
  if (day < 1 || day + rem !== total) return null;
  const ex = String(r.Exit_Date || "").slice(0, 10);
  const cap = num(r.Hold_Cap);
  return {
    day, remaining: rem, total, cap: cap !== null && Number.isInteger(cap) ? cap : STRATEGY.cap,
    exit: /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(ex) ? ex : "",
  };
}

function holdClockText(r) {
  const c = holdClock(r);
  if (!c) return "";
  const st = String(r.Hold_Status || "");
  if (st === "delay" || c.day > c.total) {
    return `持有第 ${c.day} 天，已過計畫的 ${c.total} 天，續抱中，最晚第 ${c.cap} 天`;
  }
  if (c.remaining === 0) return `持有第 ${c.day}/${c.total} 天，今天收盤到期${c.exit ? `（${mmdd(c.exit)}）` : ""}`;
  return `持有第 ${c.day}/${c.total} 天，還有 ${c.remaining} 個交易日${c.exit ? `，出場日 ${mmdd(c.exit)}` : ""}`;
}

// The late profit-take: from day 8, a close at or above the fill +1% is sold at
// the NEXT open (scanner/exit_rules step 4). The backend carries the decision
// only as the free-text Exit_Note it writes for that state
// (holding_tracker._plan_row), so that sentence is the signal; a test pins the
// phone's pattern to what the backend really writes.
const LATE_DUE_NOTE = /^in profit on day [0-9]+\+: sell at the next open/;
function lateDueNow(r) {
  const st = String(r.Hold_Status || "");
  if (st !== "holding" && st !== "delay") return false;
  if (r.Exit_Signal) return false;
  return LATE_DUE_NOTE.test(String(r.Exit_Note || ""));
}

// Hold-group cards: only the exit side -- today's action.
function exitGuideHtml(r) {
  const st = String(r.Hold_Status || "");
  const sig = String(r.Exit_Signal || "");
  let head = "";
  if (lateDueNow(r)) {
    head = `今天的動作：第 ${STRATEGY.lateFrom} 天起收盤仍有成交價 +${STRATEGY.lateGainPct}% 以上，規則在下一個交易日開盤收下（以限價賣出；要確保成交可掛低一點，成交價是開盤競價結果）`;
  } else if (st === "exit_today" || exitIsToday(r)) {
    head = `今天的動作：已觸發「${EXIT_REASON_TEXT[sig] || "出場"}」，下一個交易日開盤以限價賣出（要確保成交可掛低一點，成交價是開盤競價結果）`;
    if (provisionalTimeExit(r)) {
      const asOf = String(((STATE.meta && STATE.meta.regime) || {}).as_of_date || "").slice(0, 10);
      head += `。這筆期滿出場是暫定的：大盤資料只到 ${mmdd(asOf)}，若 ${mmdd(r.Exit_Signal_Date)} 大盤屬於「跌破 20 日均線、仍站上 60 日均線」的多頭回檔，規則會改為續抱；下一次掃描會確認，確認前照期滿處理`;
    }
  } else if (st === "overdue") {
    head = `資料缺漏：超過 ${STRATEGY.cap} 天上限仍無出場紀錄，請以「持倉」頁與券商成交為準`;
  } else if (st === "delay") {
    head = `續抱中（第 ${STRATEGY.horizon} 天後延長：收盤站上自己的 5 日均價，或當天大盤回檔）：每天收盤檢查，最晚第 ${STRATEGY.cap} 天出場`;
  } else if (st === "holding") {
    const stop = cents(r.Plan_Stop);
    head = `續抱：停損 ${stop !== null ? fmtPrice(stop) : "-"}，跌破先出（下一個交易日開盤以限價處理）`;
  } else if (st === "pending" || !st) {
    const v = buyVerdict(r);
    const rv = recView(r, "hold");
    head = rv.missed
      ? "沒有模擬進場紀錄：建議的進場日已過，規則不會補買；若你自己有買，以「持倉」頁為準"
      : v.ok ? "待進場：下一個交易日開盤照規則進場"
      : `尚未進場，今天不是買點（${v.text}）：規則只在可買訊號後的第一個開盤進場，不要自行補買；若你自己有買，以「持倉」頁為準`;
  } else if (st === "exited") {
    head = "這筆模擬交易已出場";
  }
  const rn = restrictionNote(r);
  const clock = st === "holding" || st === "delay" ? holdClockText(r) : "";
  return `<div class="plan exit-guide">${esc(head)}${clock ? `<br><span class="hint">${esc(clock)}</span>` : ""}${rn ? `<br><span class="hint">${esc(rn)}</span>` : ""}` +
    `<br><span class="hint">賣出一律掛限價；卡片價位以整股開盤價計算，你自己的持倉以「持倉」頁為準。</span></div>`;
}

// --- AI report marker (P1-7 phone half) --------------------------------------
// meta.report_sources[market].source says where the text came from; an older
// payload has only the text, whose template header is recognisable.
function aiReportView(market) {
  const reports = STATE.reports || {};
  const sources = (STATE.meta && STATE.meta.report_sources) || {};
  const key = reports[market] ? market : (reports.ALL ? "ALL" : market);
  const text = String(reports[key] || "");
  // {market: {source, model, ...}} (result_export._report_sources); a bare
  // {market: "template"} string is read the same way.
  const raw = sources && typeof sources === "object" ? sources[key] : null;
  const srcObj = typeof raw === "string" ? { source: raw } : (raw && typeof raw === "object" ? raw : null);
  const source = srcObj && srcObj.source ? String(srcObj.source) : "";
  const template = source ? source === "template" : /^\s*【(模板|本地)報告/.test(text);
  return { key, text, template, fellBack: key !== market, source,
           model: srcObj && srcObj.model ? String(srcObj.model) : "" };
}

// --- system record (P1-5; meta.live_record) ----------------------------------
function bucketLine(b) {
  if (!b || !b.closed) return `0 筆已結束${b && b.open ? `，進行中 ${b.open} 筆` : ""}`;
  return `${b.closed} 筆已結束 · 勝率 ${fmt(b.win_pct, 1)}% · 平均 ${fmtSigned(b.mean_pct, 2)}% · 合計 ${fmtSigned(b.sum_pct, 1)}%${
    b.open ? `；進行中 ${b.open} 筆` : ""}`;
}

function closedStats(list) {
  const c = list.filter((t) => num(t.ret) !== null);
  const sum = c.reduce((a, t) => a + num(t.ret), 0);
  return {
    n: c.length,
    win: c.length ? 100 * c.filter((t) => num(t.ret) > 0).length / c.length : null,
    mean: c.length ? sum / c.length : null,
    sum,
  };
}

// Cumulative return of equal-sized trades, in %, with the two indices when
// the record carries a benchmark. x is the exit DATE when every trade has
// one (and the index lines can share the axis), else the trade's order.
function equitySvg(rec, capped) {
  const closed = ((rec.tradable && rec.tradable.trades) || []).filter((t) => num(t.ret) !== null);
  if (!closed.length) return "";
  const dated = closed.every((t) => dayNum(t.exit_date) !== null);
  const list = closed.slice().sort((a, b) => (dated
    ? String(a.exit_date).localeCompare(String(b.exit_date)) || String(a.sig).localeCompare(String(b.sig))
    : String(a.sig).localeCompare(String(b.sig))));
  const pts = [];
  let cum = 0;
  if (dated) {
    const x0 = dayNum(rec.since);
    pts.push({ x: x0 !== null ? x0 : dayNum(list[0].exit_date), y: 0 });
    for (const t of list) { cum += num(t.ret); pts.push({ x: dayNum(t.exit_date), y: cum }); }
  } else {
    pts.push({ x: 0, y: 0 });
    list.forEach((t, i) => { cum += num(t.ret); pts.push({ x: i + 1, y: cum }); });
  }
  const lines = [{ cls: "eq-line", pts }];
  const series = rec.bench && Array.isArray(rec.bench.series) ? rec.bench.series : null;
  if (dated && series && series.length > 1) {
    [[1, "eq-taiex"], [2, "eq-otc"]].forEach(([col, cls]) => {
      const vals = series.filter((p) => Array.isArray(p) && num(p[col]) !== null && dayNum(p[0]) !== null);
      if (vals.length < 2) return;
      const base = num(vals[0][col]);
      if (!base) return;
      lines.push({ cls, pts: vals.map((p) => ({ x: dayNum(p[0]), y: (num(p[col]) / base - 1) * 100 })) });
    });
  }
  let xmin = Infinity, xmax = -Infinity, ymin = 0, ymax = 0;
  for (const ln of lines) {
    for (const p of ln.pts) {
      xmin = Math.min(xmin, p.x); xmax = Math.max(xmax, p.x);
      ymin = Math.min(ymin, p.y); ymax = Math.max(ymax, p.y);
    }
  }
  if (!(xmax > xmin)) xmax = xmin + 1;
  if (!(ymax > ymin)) ymax = ymin + 1;
  const W = 320, H = 140, pad = 6;
  const sx = (x) => (pad + (x - xmin) / (xmax - xmin) * (W - 2 * pad)).toFixed(1);
  const sy = (y) => (H - pad - (y - ymin) / (ymax - ymin) * (H - 2 * pad)).toFixed(1);
  const poly = lines.map((ln) =>
    `<polyline class="${ln.cls}" points="${ln.pts.map((p) => sx(p.x) + "," + sy(p.y)).join(" ")}" />`).join("");
  const legend = [`<span class="lg eq-line">系統（${capped ? `只含保留的最近 ${list.length} 筆已結束，` : ""}每筆等額累計 ${esc(fmtSigned(cum, 1))}%）</span>`];
  if (lines.some((l) => l.cls === "eq-taiex")) legend.push(`<span class="lg eq-taiex">加權指數</span>`);
  if (lines.some((l) => l.cls === "eq-otc")) legend.push(`<span class="lg eq-otc">櫃買指數</span>`);
  return `<svg class="eq" viewBox="0 0 ${W} ${H}" data-n="${list.length}" role="img" aria-label="系統訊號累計報酬">` +
    `<line class="eq-zero" x1="${pad}" x2="${W - pad}" y1="${sy(0)}" y2="${sy(0)}" />${poly}</svg>` +
    `<div class="legend">${legend.join("")}<span class="lg-note">${dated ? "橫軸：出場日" : "橫軸：交易順序"}；單位 %，每筆等額、不複利</span></div>`;
}

function benchHtml(b) {
  if (!b || typeof b !== "object") return "";
  const parts = [];
  if (num(b.taiex_pct) !== null) {
    parts.push(`加權指數 ${fmtSigned(b.taiex_pct, 2)}%（${mmdd(b.taiex_from_date)}→${mmdd(b.taiex_to_date)}）`);
  }
  if (num(b.otc_pct) !== null) {
    parts.push(`櫃買指數 ${fmtSigned(b.otc_pct, 2)}%（${mmdd(b.otc_from_date)}→${mmdd(b.otc_to_date)}）`);
  }
  const w = b.windows || {};
  const cmp = [];
  for (const [key, label] of [["otc", "櫃買"], ["taiex", "加權"]]) {
    const n = num(w[key + "_n"]);
    if (!n) continue;
    cmp.push(`${label}：同樣 ${n} 筆的持有期間，系統平均 ${fmtSigned(w[key + "_trade_mean_pct"], 2)}%、指數平均 ${fmtSigned(w[key + "_mean_pct"], 2)}%`);
  }
  if (!parts.length && !cmp.length) return "";
  return `<div class="hint">同期指數：${esc(parts.join("；") || "無資料")}${
    cmp.length ? "<br>" + esc(cmp.join("；")) : ""}<br>指數是收盤價、不含股息；每筆期間從訊號日收盤到出場日收盤。</div>`;
}

function systemRecordHtml() {
  const rec = STATE.meta && STATE.meta.live_record;
  const head = `<div class="sec sysrec"><h2>系統訊號紀錄（非你的實際損益）</h2>`;
  if (!rec || !rec.tradable) {
    return head + `<div class="hint">本次掃描沒有附系統訊號紀錄（meta.live_record）。</div></div>`;
  }
  const t = rec.tradable;
  const trades = Array.isArray(t.trades) ? t.trades : [];
  const since = String(rec.since || "");
  const headline = `上線以來（${mmdd(since) || "?"} 起，到 ${mmdd(rec.through) || "?"}）符合完整買進規則：${bucketLine(t)}`;
  // live_record.TRADES_KEPT: the payload lists only the most recent trades.
  // Everything computed from the list (curve, batch exclusion, reasons) then
  // covers those alone and says so; the headline is the backend's full count.
  const total = (num(t.closed) || 0) + (num(t.open) || 0);
  const capped = trades.length < total;
  const keptNote = capped ? `逐筆只保留最近 ${trades.length} 筆（全部 ${total} 筆），下面的曲線、排除批次與出場原因只就這 ${trades.length} 筆計算` : "";
  const batch = since ? trades.filter((x) => String(x.sig || "") === since) : [];
  let batchLine = "";
  if (batch.length) {
    const rest = closedStats(trades.filter((x) => String(x.sig || "") !== since));
    batchLine = `起始日（${mmdd(since)}）批次 ${batch.length} 筆是上線當天一次列入的訊號；排除後${capped ? `（保留的 ${trades.length} 筆內）` : ""}：${rest.n} 筆已結束` +
      (rest.n ? `，勝率 ${fmt(rest.win, 1)}%，平均 ${fmtSigned(rest.mean, 2)}%，合計 ${fmtSigned(rest.sum, 2)}%` : "");
  }
  const byReason = {};
  for (const x of trades) {
    const k = num(x.ret) === null ? "" : String(x.exit || "");
    (byReason[k] = byReason[k] || []).push(x);
  }
  const order = ["tp", "lock", "late", "time", "stop", ""];
  for (const k of Object.keys(byReason)) if (!order.includes(k)) order.splice(order.length - 1, 0, k);
  const reasonRows = order.filter((k) => byReason[k]).map((k) => {
    const s = closedStats(byReason[k]);
    return `<tr><td>${esc(EXIT_REASON_TEXT[k] || k)}</td><td>${byReason[k].length}</td>` +
      `<td>${s.n ? esc(fmtSigned(s.mean, 2)) + "%" : "-"}</td><td>${s.n ? esc(fmtSigned(s.sum, 2)) + "%" : "-"}</td></tr>`;
  }).join("");
  const reasonTbl = reasonRows
    ? `<table class="tbl reasons"><thead><tr><th>出場原因${capped ? `（最近 ${trades.length} 筆）` : ""}</th><th>筆數</th><th>平均</th><th>合計</th></tr></thead><tbody>${reasonRows}</tbody></table>`
    : "";
  const tradeRows = trades.slice().sort((a, b) => String(b.sig).localeCompare(String(a.sig))).map((x) => {
    const tags = (since && String(x.sig || "") === since ? `<span class="tag">起始日批次</span>` : "") +
      (x.restriction && x.restriction !== "none" && RESTRICT_TEXT[x.restriction]
        ? `<span class="tag warn">${esc(RESTRICT_TEXT[x.restriction])}</span>` : "");
    const res = num(x.ret) === null ? "進行中"
      : `${EXIT_REASON_TEXT[x.exit] || x.exit || "出場"} ${fmtSigned(x.ret, 2)}%`;
    return `<tr><td>${esc(mmdd(x.sig))} ${esc(x.sid)} ${esc(x.name || stockName(x.sid) || "")}${tags}</td>` +
      `<td class="${signClass(x.ret)}">${esc(res)}</td><td>${num(x.bars) === null ? "-" : esc(String(x.bars)) + " 日"}</td></tr>`;
  }).join("");
  const byR = rec.by_restriction && typeof rec.by_restriction === "object"
    ? Object.entries(rec.by_restriction).filter(([, b]) => b && (b.closed || b.open)) : [];
  const byRLine = byR.some(([k]) => k !== "unrecorded" && k !== "none")
    ? `<div class="hint">依進場時的交易限制：${esc(byR.map(([k, b]) =>
        `${k === "unrecorded" ? "未記錄" : k === "none" ? "無限制" : RESTRICT_TEXT[k] || k} ${bucketLine(b)}`).join("；"))}</div>` : "";
  return head +
    `<div class="plan">${esc(headline)}${rec.built_at ? `<br><span class="hint">計算於 ${esc(rec.built_at)}${rec.carried_forward ? "，沿用上一次掃描" : ""}</span>` : ""}</div>` +
    (keptNote ? `<div class="hint" data-capped="${trades.length}">${esc(keptNote)}</div>` : "") +
    (batchLine ? `<div class="hint">${esc(batchLine)}</div>` : "") +
    equitySvg(rec, capped) +
    benchHtml(rec.bench) +
    reasonTbl +
    `<div class="hint">名單上但未過 CORE+（未計入）：${esc(bucketLine(rec.not_core))}<br>CORE+ 但大盤未順風（未計入）：${esc(bucketLine(rec.regime_closed))}</div>` +
    byRLine +
    (tradeRows ? `<details class="strategy dim"><summary>逐筆 ${trades.length} 筆${capped ? "（只列最近的）" : ""}</summary>` +
      `<table class="tbl"><tbody>${tradeRows}</tbody></table></details>` : "") +
    `<div class="hint">這是規則在這支掃描器發出的訊號上的模擬：隔日開盤進場、同一套出場、含手續費與證交稅；不是你的帳本。你自己的成交與損益在下方。</div>` +
    `</div>`;
}

/* ============================================================================
 * 10c. Closed-trade record (2026-10-09) and the owner's own reconciliation
 *
 * A recommendation that closes keeps the COMPLETE trade in its `outcome`
 * (scanner/live_record.trade_record, stored by portfolio/sync._outcome):
 * the signal day, the rule's next-open entry, the day the exit was decided,
 * the day and the price it was booked at and WHY that price (a level touched
 * inside the day, a gap or next-open sale, or the close), the best/worst
 * excursions, the costs, and one entry per held session. This file only
 * DISPLAYS it: nothing here decides an exit, re-prices a trade or edits a
 * record. `outcome` is an object, or JSON text in some exports; an older
 * closed recommendation carries the eight summary keys and no `path`, and
 * renders as a summary that says so.
 *
 * The reconciliation lines up the owner's OWN fills (IndexedDB, entered from
 * that recommendation: positions[].recommendation_id) against the rule's.
 * Those fills are private: they are read here, shown here and sent nowhere --
 * no fetch carries them, nothing is written back, nothing is logged.
 *
 * Every figure goes through a strict reader first (tnum / tdate / tint): a
 * null, a NaN, a boolean or a malformed string becomes null and renders as a
 * dash, never as "NaN", "undefined" or a silent 0.
 * ==========================================================================*/

const TR_BASIS_TEXT = {
  level: "盤中觸價",
  open: "開盤跳空或隔日開盤賣",
  close: "收盤價",
};
const TR_STATUS_TEXT = {
  holding: "持有",
  armed: "鎖利啟動",
  sell_next_open: "收盤達標，隔日開盤收下",
};
const TR_ISSUE_TEXT = {
  bars_vs_path: "持有天數與逐日路徑筆數不同",
  entry_vs_path: "進場日不是路徑的第一天",
  exit_vs_path: "出場成交日不是路徑的最後一天",
  ret_vs_prices: "毛報酬與進出場價對不上",
  path_order: "逐日路徑日期沒有由小到大",
  planned_entry: "預定進場日與實際進場日不同",
};
const TR_RECORDS_KEPT = 20;     // most recent closed recommendations listed

function own(map, key) {
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
}

// Strict readers. A string that is not a number, a boolean, an array or a
// non-finite number is null; -0 is 0.
function tnum(v) {
  let n = v;
  if (typeof n === "string") {
    if (!n.trim()) return null;
    n = Number(n);
  } else if (typeof n !== "number") {
    return null;
  }
  if (!Number.isFinite(n)) return null;
  return n === 0 ? 0 : n;
}

function tint(v) {
  const n = tnum(v);
  return n !== null && Number.isInteger(n) && n >= 0 ? n : null;
}

// "YYYY-MM-DD" (a longer timestamp is cut to its date) that is a real
// calendar day, else null.
function tdate(v) {
  if (typeof v !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v);
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return d.getUTCFullYear() === Number(m[1]) && d.getUTCMonth() === Number(m[2]) - 1 &&
    d.getUTCDate() === Number(m[3]) ? v.slice(0, 10) : null;
}

const dashText = (s) => (s === null || s === undefined || s === "" ? "-" : String(s));
const trPrice = (v) => (tnum(v) === null ? "-" : tnum(v).toFixed(2));
const trPct = (v) => (tnum(v) === null ? "-" : fmtSigned(tnum(v), 2) + "%");
const trPlain = (v) => (tnum(v) === null ? "-" : tnum(v).toFixed(2) + "%");

function recOutcome(rec) {
  if (!rec || typeof rec !== "object") return null;
  let o = rec.outcome;
  if (typeof o === "string") {
    if (!o.trim()) return null;
    try { o = JSON.parse(o); } catch (e) { return null; }
  }
  return o && typeof o === "object" && !Array.isArray(o) ? o : null;
}

function trReasonText(code) {
  const s = typeof code === "string" ? code : "";
  if (!s) return "";
  return own(EXIT_REASON_TEXT, s) || own(REC_REASON_TEXT, s) || s;
}

function trStatusText(status) {
  const s = typeof status === "string" ? status : "";
  if (!s) return "-";
  if (own(TR_STATUS_TEXT, s)) return own(TR_STATUS_TEXT, s);
  if (s.indexOf("exit_") === 0 && s.length > 5) return "出場：" + trReasonText(s.slice(5));
  return s;
}

// The day-by-day path of an outcome: {state, rows, dropped}. state is 'none'
// (an older record), 'malformed' (not a list), 'empty' (nothing readable) or
// 'ok'. A row that is not an object, or has neither a date nor a close, is
// dropped and counted; a row with some fields missing is kept, dashes there.
function recPath(o) {
  if (!o || o.path === undefined || o.path === null) return { state: "none", rows: [], dropped: 0 };
  if (!Array.isArray(o.path)) return { state: "malformed", rows: [], dropped: 0 };
  const rows = [];
  let dropped = 0;
  for (const p of o.path) {
    if (!p || typeof p !== "object" || Array.isArray(p)) { dropped++; continue; }
    const row = {
      date: tdate(p.date), open: tnum(p.open), high: tnum(p.high), low: tnum(p.low),
      close: tnum(p.close), ret: tnum(p.close_ret_pct),
      status: typeof p.status === "string" ? p.status : "",
      stop: tnum(p.stop), day: tint(p.day), ride: p.ride === true,
    };
    if (row.date === null && row.close === null) { dropped++; continue; }
    rows.push(row);
  }
  return { state: rows.length ? "ok" : "empty", rows, dropped };
}

function dayGap(from, to) {
  const a = dayNum(tdate(from)), b = dayNum(tdate(to));
  return a === null || b === null ? null : b - a;
}

// The view of one closed recommendation: every field the record page prints,
// already read strictly. `rec` is a row of recommendations.json.
function tradeRecordView(rec) {
  const r0 = rec && typeof rec === "object" ? rec : {};
  const o = recOutcome(r0);
  const g = o || {};
  const sid = String(r0.stock_id === null || r0.stock_id === undefined ? "" : r0.stock_id).trim();
  let name = r0.stock_name ? String(r0.stock_name) : "";
  if (!name && sid) name = stockName(sid);
  const p = recPath(g);
  const v = {
    rid: typeof r0.recommendation_id === "string" ? r0.recommendation_id : "",
    sid, name: name || sid,
    has_outcome: !!o,
    schema: tint(g.record_schema),
    path_state: p.state, path: p.rows, path_dropped: p.dropped,
    // the older summary keeps no signal day of its own; the recommendation row
    // does (portfolio/sync passes first_qualified_session as the signal)
    signal_session: tdate(g.signal_session) || tdate(r0.first_qualified_session),
    planned_entry_session: tdate(g.planned_entry_session) || tdate(r0.valid_until_session),
    entry_date: tdate(g.entry_date), entry_price: tnum(g.entry_price),
    exit_decision_date: tdate(g.exit_decision_date),
    exit_fill_date: tdate(g.exit_fill_date) || tdate(g.exit_date),
    exit_price: tnum(g.exit_price),
    exit_basis: typeof g.exit_basis === "string" ? g.exit_basis : "",
    reason: (typeof g.reason === "string" && g.reason) ? g.reason
      : (typeof r0.status_reason === "string" ? r0.status_reason : ""),
    bars: tint(g.bars),
    ret_gross: tnum(g.ret_gross_pct), ret_net: tnum(g.ret_net_pct),
    mfe: tnum(g.mfe_pct), mae: tnum(g.mae_pct),
    live_fill_date: tdate(g.live_fill_date), live_fill_price: tnum(g.live_fill_price),
  };
  v.basis_text = own(TR_BASIS_TEXT, v.exit_basis) || "";
  v.reason_text = trReasonText(v.reason);
  v.calendar_days = tint(g.calendar_days);
  if (v.calendar_days === null) v.calendar_days = dayGap(v.entry_date, v.exit_fill_date);
  v.cost = tnum(g.cost_pct);
  if (v.cost === null && v.ret_gross !== null && v.ret_net !== null) {
    v.cost = Math.round((v.ret_gross - v.ret_net) * 100) / 100;
  }
  // The booked close of a time exit is not what the evening order gets: the
  // order placed that night fills at the NEXT open (live_fill_*).
  v.live_expected = v.reason === "time";
  v.live_gap_pct = v.live_fill_price !== null && v.exit_price ? Math.round((v.live_fill_price / v.exit_price - 1) * 10000) / 100 : null;
  v.ride_days = v.path.filter((q) => q.ride).length;
  v.issues = tradeRecordIssues(v);
  return v;
}

// Self-checks of a stored record against itself. They only LABEL a record
// that disagrees with itself; the numbers shown are always the stored ones.
function tradeRecordIssues(v) {
  const bad = [];
  const path = v.path;
  if (v.path_state === "ok") {
    if (v.bars !== null && v.bars !== path.length) bad.push("bars_vs_path");
    if (v.entry_date && path[0].date && v.entry_date !== path[0].date) bad.push("entry_vs_path");
    if (v.exit_fill_date && path[path.length - 1].date && v.exit_fill_date !== path[path.length - 1].date) bad.push("exit_vs_path");
    const ds = path.map((q) => q.date);
    if (ds.every((d) => d !== null) && ds.some((d, i) => i > 0 && !(d > ds[i - 1]))) bad.push("path_order");
  }
  if (v.entry_price && v.exit_price !== null && v.ret_gross !== null &&
      Math.abs((v.exit_price / v.entry_price - 1) * 100 - v.ret_gross) > 0.03) bad.push("ret_vs_prices");
  if (v.planned_entry_session && v.entry_date && v.planned_entry_session !== v.entry_date) bad.push("planned_entry");
  return bad;
}

// Closed recommendations, newest exit first.
function closedRecords(recs) {
  const list = (Array.isArray(recs) ? recs : []).filter((q) => q && typeof q === "object" && q.status === "closed");
  const key = (q) => {
    const o = recOutcome(q) || {};
    return tdate(o.exit_fill_date) || tdate(o.exit_date) || tdate(q.status_session) || "";
  };
  return list.slice().sort((a, b) => key(b).localeCompare(key(a)) ||
    String(a.recommendation_id || "").localeCompare(String(b.recommendation_id || "")));
}

// --- the owner's fills against the rule's -----------------------------------
// Sessions between two dates on the first calendar that knows both (the
// app's trading calendar, or the trade's own path), else null; days is the
// plain calendar difference. mine minus the rule's: positive = later.
function sessionGap(from, to, calendars) {
  const a = tdate(from), b = tdate(to);
  if (!a || !b) return { sessions: null, days: null };
  let sessions = null;
  for (const cal of Array.isArray(calendars) ? calendars : []) {
    if (!Array.isArray(cal)) continue;
    const i = cal.indexOf(a), j = cal.indexOf(b);
    if (i >= 0 && j >= 0) { sessions = j - i; break; }
  }
  return { sessions, days: dayGap(a, b) };
}

function gapText(gap) {
  if (!gap) return "";
  if (gap.sessions !== null) {
    return gap.sessions === 0 ? "同一個交易日"
      : gap.sessions > 0 ? `晚 ${gap.sessions} 個交易日` : `早 ${-gap.sessions} 個交易日`;
  }
  if (gap.days !== null) {
    return gap.days === 0 ? "同一天"
      : gap.days > 0 ? `晚 ${gap.days} 日曆天` : `早 ${-gap.days} 日曆天`;
  }
  return "";
}

function priceDiffPct(mineCents, ruleCents) {
  if (!Number.isInteger(mineCents) || !Number.isInteger(ruleCents) || ruleCents <= 0) return null;
  return pctOf(mineCents - ruleCents, ruleCents);
}

// `execs` are the owner's CURRENT executions for one recommendation; null
// when there is no buy among them (nothing to reconcile, nothing shown).
function reconcileTrade(v, execs, calendars) {
  if (!v || !v.has_outcome || !Array.isArray(execs)) return null;
  const good = execs.filter((e) => e && typeof e === "object" && (e.side === "BUY" || e.side === "SELL") &&
    e.is_current !== 0 && tdate(e.session_date) !== null &&
    Number.isInteger(e.price_cents) && e.price_cents > 0 && Number.isInteger(e.shares) && e.shares > 0);
  const list = sortExecutions(good);
  const buys = list.filter((e) => e.side === "BUY");
  const sells = list.filter((e) => e.side === "SELL");
  if (!buys.length) return null;
  const cal = Array.isArray(calendars) ? calendars : [];
  const avg = (rows) => divRound(rows.reduce((a, e) => a + e.price_cents * e.shares, 0),
                                 rows.reduce((a, e) => a + e.shares, 0));
  const ruleEntry = v.entry_price !== null ? Math.round(v.entry_price * 100) : null;
  const ruleExit = v.exit_price !== null ? Math.round(v.exit_price * 100) : null;
  const fold = replay(list, null);
  const out = {
    n_buys: buys.length, n_sells: sells.length, open_shares: fold.shares,
    buy: {
      date: buys[0].session_date, price_cents: buys[0].price_cents, avg_cents: avg(buys),
      rule_date: v.entry_date, rule_price_cents: ruleEntry,
      gap: sessionGap(v.entry_date, buys[0].session_date, cal),
      diff_pct: priceDiffPct(buys[0].price_cents, ruleEntry),
    },
    sell: null, result: null,
  };
  if (!sells.length || fold.shares > 0) return out;
  const last = sells[sells.length - 1];
  const px = avg(sells);
  out.sell = {
    date: last.session_date, price_cents: px,
    rule_date: v.exit_fill_date, rule_price_cents: ruleExit,
    gap: sessionGap(v.exit_fill_date, last.session_date, cal),
    diff_pct: priceDiffPct(px, ruleExit),
    live: null,
  };
  if (v.live_expected && v.live_fill_date && v.live_fill_price !== null) {
    const lp = Math.round(v.live_fill_price * 100);
    out.sell.live = {
      date: v.live_fill_date, price_cents: lp,
      gap: sessionGap(v.live_fill_date, last.session_date, cal),
      diff_pct: priceDiffPct(px, lp),
    };
  }
  // The result needs every fee the ledger would have booked; a fill without
  // them gives no honest percentage.
  const feesOk = list.every((e) => Number.isInteger(e.fee_cents) && (e.side === "BUY" || Number.isInteger(e.tax_cents)));
  if (feesOk) {
    const cost = buys.reduce((a, e) => a + e.price_cents * e.shares + e.fee_cents, 0);
    const pct = pctOf(fold.realized_net, cost);
    out.result = {
      net_cents: fold.realized_net, cost_cents: cost, net_pct: pct,
      rule_net_pct: v.ret_net,
      delta_pp: pct !== null && v.ret_net !== null ? Math.round((pct - v.ret_net) * 100) / 100 : null,
    };
  }
  return out;
}

function recExecsFor(rid) {
  if (!rid || typeof rid !== "string") return [];
  const out = [];
  for (const pos of STATE.positions || []) {
    if (!pos || pos.recommendation_id !== rid || pos.status === "void") continue;
    for (const e of currentExecutions((STATE.execsByPos || {})[pos.position_id] || [])) out.push(e);
  }
  return out;
}

function recCalendars(v) {
  const ownDays = v.path.map((q) => q.date).filter((d) => d !== null);
  return [CAL, ownDays];
}

// --- html --------------------------------------------------------------------
function signedWord(n, up, down, same) {
  return n === null ? "" : n > 0 ? up : n < 0 ? down : same;
}

function reconcileHtml(rc) {
  if (!rc) return "";
  const b = rc.buy;
  const lines = [];
  const priceLine = (who, price, rulePrice, diff, up, down, same) =>
    `${who} ${esc(fmtPrice(price))}｜規則 ${rulePrice === null ? "-" : esc(fmtPrice(rulePrice))}｜價差 ${
      diff === null ? "-" : esc(fmtPct(diff))}${diff === null ? "" : "（" + signedWord(diff, up, down, same) + "）"}`;
  lines.push(drow("買進",
    `${esc(dashText(b.date))}（規則 ${esc(dashText(b.rule_date))}，${esc(gapText(b.gap) || "-")}）<br>` +
    priceLine("你", b.price_cents, b.rule_price_cents, b.diff_pct, "買得比規則貴", "買得比規則便宜", "同價") +
    (rc.n_buys > 1 ? `<br><span class="sm">共 ${rc.n_buys} 筆買進，日期與價格以第一筆為準（加權均價 ${esc(fmtPrice(b.avg_cents))}）</span>` : "")));
  if (rc.sell) {
    const s = rc.sell;
    lines.push(drow("賣出",
      `${esc(dashText(s.date))}（規則成交 ${esc(dashText(s.rule_date))}，${esc(gapText(s.gap) || "-")}）<br>` +
      priceLine("你", s.price_cents, s.rule_price_cents, s.diff_pct, "賣得比規則高", "賣得比規則低", "同價") +
      (s.live ? `<br><span class="sm">對照實盤隔日開盤 ${esc(s.live.date)} 約 ${esc(fmtPrice(s.live.price_cents))}：${
        esc(gapText(s.live.gap) || "-")}，價差 ${s.live.diff_pct === null ? "-" : esc(fmtPct(s.live.diff_pct))}</span>` : "") +
      (rc.n_sells > 1 ? `<br><span class="sm">共 ${rc.n_sells} 筆賣出，日期取最後一筆、價格為加權均價</span>` : "")));
    if (rc.result) {
      const z = rc.result;
      lines.push(drow("實際結果",
        `你的淨報酬 <b class="${signClass(z.net_pct)}">${esc(z.net_pct === null ? "-" : fmtPct(z.net_pct))}</b>（${esc(fmtPnl(z.net_cents))}）<br>` +
        `規則淨 ${esc(trPct(z.rule_net_pct))}｜差 ${z.delta_pp === null ? "-" : esc(fmtSigned(z.delta_pp, 2))} 個百分點`));
    } else {
      lines.push(drow("實際結果", `<span class="sm">成交缺少手續費或交易稅，算不出淨報酬</span>`));
    }
  } else {
    lines.push(drow("賣出", `<span class="sm">你還有 ${esc(rc.open_shares.toLocaleString("en-US"))} 股沒賣，尚未結案，不比較結果</span>`));
  }
  return `<div class="sub-h">對帳：你的成交 vs 規則</div>${lines.join("")}` +
    `<div class="hint">你的成交只存在這支手機，對帳只比較、不改任何紀錄。</div>`;
}

function pathTableHtml(v) {
  const rows = v.path.map((q) => {
    const exit = q.status.indexOf("exit_") === 0;
    const st = trStatusText(q.status) + (q.ride ? "・期滿後續抱" : "");
    return `<tr${exit ? ' class="hl"' : ""}>` +
      `<td>${q.day === null ? "-" : "D" + esc(q.day)}<br><span class="sm">${esc(dashText(q.date))}</span></td>` +
      `<td>開 ${esc(trPrice(q.open))} 高 ${esc(trPrice(q.high))}<br>低 ${esc(trPrice(q.low))} 收 ${esc(trPrice(q.close))}</td>` +
      `<td class="${signClass(q.ret)}">${esc(trPct(q.ret))}</td>` +
      `<td>${esc(st)}<br><span class="sm">停損 ${esc(trPrice(q.stop))}</span></td></tr>`;
  }).join("");
  return `<table class="tbl"><thead><tr><th>日</th><th>開高低收</th><th>收盤報酬</th><th>狀態・收盤後的停損單</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function tradeRecordHtml(v, recon, isOpen) {
  if (!v.has_outcome) {
    return `<article class="card state-closed" data-rid="${esc(v.rid)}"><div class="card-head">` +
      `<span class="name">${esc(v.name)}</span><span class="code">${esc(v.sid)}</span>` +
      `<span class="tag">${esc(v.reason_text || "已結案")}</span></div>` +
      `<div class="hint">這筆已結案，但紀錄裡沒有成交摘要，無法顯示明細。</div></article>`;
  }
  const entry = `${esc(dashText(v.entry_date))}｜開盤 ${esc(trPrice(v.entry_price))}`;
  const exitLine = `${esc(dashText(v.exit_fill_date))}｜${esc(trPrice(v.exit_price))}｜${
    esc(v.basis_text || "價格依據未記錄")}`;
  const hold = `${v.bars === null ? "-" : esc(v.bars)} 個交易日 / ${v.calendar_days === null ? "-" : esc(v.calendar_days)} 日曆天`;
  const chain =
    drow("訊號日", esc(dashText(v.signal_session))) +
    drow("規則進場", entry) +
    drow("出場判定日", esc(dashText(v.exit_decision_date))) +
    drow("出場成交", exitLine) +
    drow("出場原因", esc(dashText(v.reason_text))) +
    drow("持有", hold);
  const nums = `<div class="kv2">` +
    kv("毛報酬", esc(trPct(v.ret_gross)), signClass(v.ret_gross)) +
    kv("淨報酬", esc(trPct(v.ret_net)), signClass(v.ret_net), "已扣手續費與證交稅") +
    kv("成本", esc(trPlain(v.cost)), "", "來回手續費與賣出稅") +
    kv("最高（MFE）", esc(trPct(v.mfe)), signClass(v.mfe), "持有期間盤中最高，對進場價") +
    kv("最低（MAE）", esc(trPct(v.mae)), signClass(v.mae), "持有期間盤中最低，對進場價") +
    `</div>`;
  let live = "";
  if (v.live_expected) {
    live = v.live_fill_date && v.live_fill_price !== null
      ? `<div class="plan alert">實盤：當晚下單，隔日開盤 ${esc(v.live_fill_date)} 約 ${esc(trPrice(v.live_fill_price))}` +
        `（帳上是收盤價 ${esc(trPrice(v.exit_price))}，差 ${esc(trPct(v.live_gap_pct))}）</div>`
      : `<div class="plan">實盤：當晚下單，隔日開盤成交；隔日行情還沒有，價格待補。帳上是收盤價 ${esc(trPrice(v.exit_price))}。</div>`;
  }
  let pathBlock;
  if (v.path_state === "ok") {
    pathBlock = `<details class="strategy dim" data-trec="${esc(v.rid)}"${isOpen ? " open" : ""}>` +
      `<summary>逐日明細（${v.path.length} 個交易日）</summary>${pathTableHtml(v)}` +
      (v.path_dropped ? `<div class="hint">有 ${esc(v.path_dropped)} 筆格式不符的日資料已略過</div>` : "") +
      `</details>`;
  } else if (v.path_state === "none") {
    pathBlock = `<div class="hint">早期紀錄，沒有逐日路徑。</div>`;
  } else if (v.path_state === "empty") {
    pathBlock = `<div class="hint">逐日路徑是空的，無法顯示。</div>`;
  } else {
    pathBlock = `<div class="hint">逐日路徑格式不符，無法顯示。</div>`;
  }
  const issues = v.issues.length
    ? `<div class="notice warn">這筆紀錄自己對不上：${esc(v.issues.map((k) => own(TR_ISSUE_TEXT, k) || k).join("；"))}</div>` : "";
  return `<article class="card state-closed" data-rid="${esc(v.rid)}">` +
    `<div class="card-head"><span class="name">${esc(v.name)}</span><span class="code">${esc(v.sid)}</span>` +
    `<span class="tag">${esc(v.reason_text || "已結案")}</span>` +
    `<span class="${signClass(v.ret_net)}">${esc(trPct(v.ret_net))}</span></div>` +
    issues + chain + nums + live + pathBlock + reconcileHtml(recon) + `</article>`;
}

function recordsSectionHtml() {
  const head = `<div class="sec trec"><h2>已結案建議的完整紀錄</h2>`;
  if (!STATE.recs) {
    return head + (STATE.recsState === "missing"
      ? `<div class="hint">這次部署沒有 recommendations.json，看不到建議的逐日出場紀錄。</div>`
      : `<div class="hint">讀取建議紀錄中…</div>`) + `</div>`;
  }
  const all = closedRecords(STATE.recs);
  if (!all.length) return head + `<div class="hint">目前沒有已結案的建議。</div></div>`;
  const cards = all.slice(0, TR_RECORDS_KEPT).map((q) => {
    const v = tradeRecordView(q);
    return tradeRecordHtml(v, reconcileTrade(v, recExecsFor(v.rid), recCalendars(v)), STATE.recOpen[v.rid] === true);
  }).join("");
  return head +
    (STATE.recsState === "missing" ? `<div class="notice warn">更新失敗，顯示的是上次讀到的內容</div>` : "") +
    `<div class="hint">規則的進場、出場與逐日路徑都是系統當時存下的紀錄，這裡只顯示、不重算。` +
    `期滿出場的帳上價格是收盤價，實盤要等隔日開盤才賣得到，兩者的差距另列。` +
    (all.length > TR_RECORDS_KEPT ? `只列最近 ${TR_RECORDS_KEPT} 筆（共 ${all.length} 筆）。` : "") + `</div>` +
    cards + `</div>`;
}

async function loadRecs() {
  STATE.recsState = "loading";
  try {
    const d = await fetchJson(RECS_URL);
    const list = Array.isArray(d) ? d : (d && Array.isArray(d.recommendations) ? d.recommendations : null);
    if (!list) throw new Error("格式不符");
    if (STATE.recsState !== "loading") return;     // superseded by a newer load
    STATE.recs = list.filter((q) => q && typeof q === "object" && !Array.isArray(q));
    STATE.recsState = "ready";
  } catch (e) {
    if (STATE.recsState !== "loading") return;
    STATE.recsState = "missing";
  }
  if (STATE.page === "perf") render();
}

function ensureRecs() {
  if (STATE.recsState !== "idle") return;
  loadRecs().catch(() => {});
}

// --- research-page lines for the new meta blocks ----------------------------
function recMetaText(rec) {
  if (!rec || typeof rec !== "object") return "本次掃描未提供";
  if (rec.error) return "錯誤：" + rec.error;
  const n = (k) => Number(rec[k]) || 0;
  return `新建 ${n("created")}、附上 ${n("attached")}、結案 ${n("closed")}、失效 ${n("expired")}、撤回 ${n("superseded")}` +
    (n("deferred") ? `、延後結案 ${n("deferred")}` : "") +
    (n("backfilled") ? `、補建 ${n("backfilled")}` : "");
}

function eventsMetaText(ev) {
  if (!ev || typeof ev !== "object") return "本次掃描未提供";
  const parts = [];
  parts.push(ev.ok === false ? "部分來源未取得" : "正常");
  if (ev.revenue_month_latest) parts.push(`最新營收月份 ${ev.revenue_month_latest}`);
  if (ev.next_revenue_deadline) parts.push(`下次月營收截止 ${ev.next_revenue_deadline}`);
  const rd = ev.next_report_deadline;
  if (rd && rd.date) parts.push(`財報截止 ${rd.date}${rd.what ? "（" + rd.what + (rd.approximate ? "，約略" : "") + "）" : ""}`);
  const c = ev.coverage;
  if (c && typeof c === "object" && c.rows) {
    parts.push(`涵蓋 ${c.rows} 列：營收 ${c.revenue || 0}、除權息 ${c.exdiv || 0}、法說 ${c.conf || 0}`);
  }
  if (ev.updated_at) parts.push(`更新 ${ev.updated_at}`);
  return parts.join("｜");
}

function reportSourcesText(rs) {
  if (!rs || typeof rs !== "object" || !Object.keys(rs).length) return "未提供（舊版掃描，模板以內文開頭辨識）";
  const label = { gemini: "Gemini", groq: "Groq", template: "模板（非 AI）" };
  return Object.entries(rs).map(([k, v]) => {
    const s = typeof v === "string" ? { source: v } : (v && typeof v === "object" ? v : {});
    return `${k}：${label[s.source] || s.source || "?"}${s.model ? " " + s.model : ""}${s.error ? "（" + s.error + "）" : ""}`;
  }).join("｜");
}

// The buy form's share hint: the suggestion, never a pre-filled value.
function buyQtyHint(row) {
  const base = "1 張 = 1,000 股；零股請直接填股數";
  if (!row) return base;
  const s = sizingFor(row);
  if (!s || !(s.shares > 0)) return base;
  return `${base}。依你的資金設定建議 ${s.shares.toLocaleString("en-US")} 股（${s.lots} 張 + ${s.odd} 股），請填實際成交股數`;
}

// openDetail's per-name history block.
function nameHistoryDetail(sid) {
  const h = nameHistory(sid);
  if (h === null) return `<div class="hint">本次掃描沒有附系統訊號紀錄。</div>`;
  if (!h.length) return `<div class="hint">近期無系統訊號紀錄</div>`;
  return `<ul class="hist">${h.slice().reverse().map((e) => `<li>${esc(historyEntryText(e))}</li>`).join("")}</ul>` +
    `<div class="hint">系統模擬（隔日開盤進場、同一套出場、含費稅），最多列最近 5 次；不是你的成交。</div>`;
}

// --- 11.2 今日建議 -----------------------------------------------------------
function renderPicks() {
  const g = pickGroups();
  const reg = regimeView();
  const buy = viewRows(g.buy);
  // Hold: today's actions first; the chosen sort orders within each tier.
  const hold = viewRows(g.hold).map((r, i) => [holdPriority(r), i, r])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => x[2]);
  const ref = viewRows(g.ref);
  const shown = buy.length + hold.length + ref.length;

  const controls = `
    <div class="controls">
      <div class="chips">${["ALL", "OTC", "TSE"].map((k) =>
        `<button type="button" class="chip ${STATE.market === k ? "on" : ""}" data-act="market" data-market="${k}">${k === "ALL" ? "全部" : k}</button>`).join("")}</div>
      <select class="sort" data-act="sort">${SORTS.map(([label], i) =>
        `<option value="${i}"${i === STATE.sortIndex ? " selected" : ""}>${esc(label)}</option>`).join("")}</select>
      <span class="count">${shown} 檔</span>
    </div>
    <input id="search" class="search" type="search" placeholder="搜尋代號 / 名稱" value="${esc(STATE.query)}" data-act="search" />`;

  const banner = listStampHtml() +
    `<div class="funnel">${esc(picksSummaryText(g))}</div>` +
    `<div class="notice ${reg.enterOk ? "ok" : "warn"}">${esc(reg.text)}${
      reg.asOf ? esc(` · 判定資料日 ${reg.asOf}`) : ""}</div>` +
    `<div class="hint">買進資格由後端 Buy_Ready / Buy_Block 決定，本畫面只會把過期資料降級，不會把「不可買」改成「可買」。每張卡片的「停損（跌破先出）」是最先要盯的價位。</div>`;

  const ai = aiReportView(STATE.market);
  const aiHtml = `<details class="strategy dim"><summary>AI 報告${ai.key === "ALL" ? "" : "（" + esc(ai.key) + "）"}${
      ai.template ? ` <span class="tag warn">模板·非 AI</span>` : ""}</summary>` +
    `<div class="strategy-body">${esc(ai.text || "（本次掃描沒有 AI 報告；於雲端設定 API 金鑰後即會出現）")}</div>` +
    (ai.template ? `<div class="hint">這段是 AI 服務無法使用時由程式套版產生的摘要，不是 AI 的判讀。</div>` : "") +
    (ai.fellBack && ai.text ? `<div class="hint">這個市場沒有單獨的報告，顯示的是全市場版本。</div>` : "") +
    `</details>`;

  const buyHtml = buy.length
    ? buy.map((r) => pickCard(r, "buy")).join("")
    : `<div class="empty-inline">今日 0 檔符合完整買進規則${reg.enterOk ? "（資料源正常，屬正常空手日）" : "（原因：" + esc(reg.text) + "）"}。</div>`;
  const holdLive = hold.filter((r) => !staleExit(r, "hold"));
  const holdHtml = hold.length
    ? exitSignalSummary(holdLive) + hold.map((r) => pickCard(r, "hold")).join("")
    : `<div class="empty-inline">目前沒有模擬持有中、待進場或你已持有的名單股。</div>`;
  // Forced open while searching or filtering to TSE: the result is in here.
  const forced = ref.length > 0 && (!!STATE.query.trim() || STATE.market === "TSE");
  const refOpen = STATE.refOpen || forced;

  document.getElementById("page-picks").innerHTML =
    banner + aiHtml + controls +
    `<section class="sec" data-group="buy"><h2>今日可買 ${buy.length}</h2>${buyHtml}</section>` +
    `<section class="sec" data-group="hold"><h2>模擬持有中（今天動作） ${hold.length}</h2>${holdHtml}</section>` +
    `<details class="grp" data-group="ref"${refOpen ? " open" : ""}${forced ? " data-forced" : ""}>` +
      `<summary>參考（上市、不可買，${ref.length} 檔）</summary>` +
      `<div class="hint">規則只買上櫃前 ${N_ENTER_UI} 名且過 CORE+ 的新訊號；這裡是名單上其他的股票，每張卡片附不成立原因。</div>` +
      (ref.length ? ref.map((r) => pickCard(r, "ref")).join("") : `<div class="empty-inline">沒有其他候選。</div>`) +
    `</details>`;

  const box = document.getElementById("search");
  if (box) {
    box.addEventListener("input", () => { STATE.query = box.value; renderPicks(); });
    if (STATE.query) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  }
}

// The market filter, the search box and the chosen sort, over any list.
// Sorting changes the view, never the recommendation (report 7.4).
function viewRows(list) {
  let rows = list || [];
  if (STATE.market !== "ALL") rows = rows.filter((r) => String(r.Market) === STATE.market);
  const q = STATE.query.trim().toLowerCase();
  if (q) {
    const qid = q.toUpperCase();
    rows = rows.filter((r) => String(r.Stock_ID).toUpperCase().includes(qid) ||
      String(r.Stock_Name || "").toLowerCase().includes(q));
  }
  const [, key, dir] = SORTS[STATE.sortIndex];
  return rows.slice().sort((a, b) => {
    if (key === "rank") return (a._rank || 9999) - (b._rank || 9999);
    const av = num(a[key]), bv = num(b[key]);
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return dir === "asc" ? av - bv : bv - av;
  });
}

function filteredRows() {
  return viewRows(STATE.rows);
}

// A row from today's list or the tracked set -- never the whole-market file,
// whose rows carry no recommendation and no plan.
function listRowFor(stockId) {
  const id = String(stockId);
  return STATE.rows.find((x) => String(x.Stock_ID) === id) ||
         STATE.tracked.find((x) => String(x.Stock_ID) === id) || null;
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
  const id = String(stockId || "").trim().toUpperCase();
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

// The optional sell-half level. Before a fill it is the close-based reference;
// once the row is entered it must be the FILL-based one (Fill_Scale_Out_Price),
// the same basis as the stop and target next to it -- the close-based number
// keeps moving with each scan and no longer belongs to the position. An
// entered row with no fill-based value prints nothing rather than the wrong one.
function scaleOutKv(r, hidden, entered) {
  if (hidden) return "";
  if (entered) {
    const fill = cents(r.Fill_Scale_Out_Price);
    if (fill === null) return "";
    return kv("可賣一半（選用）", esc(fmtPrice(fill)), "",
      `推估成交價 ${fmtPrice(cents(r.Entry_Open))} +${STRATEGY.scaleOutPct}%；選用，會降低平均報酬`);
  }
  const ref = cents(r.Scale_Out_Price);
  if (ref === null) return "";
  return kv("可賣一半（選用）", esc(fmtPrice(ref)), "",
    `參考價；成交後改以成交價 +${STRATEGY.scaleOutPct}% 為準；選用，會降低平均報酬`);
}

function pickCard(r, group) {
  const grp = group || pickGroup(r, heldIds());
  const v = buyVerdict(r);
  const sc = rankScore();
  const held = STATE.positions.some((p) =>
    p.stock_id === String(r.Stock_ID) && p.status === "open");
  const rv = recView(r, grp);
  const stale = staleExit(r, grp);
  const st = String(r.Hold_Status || "");

  // Report section 8 / 5.1: the FIXED first-day price and today's recomputed
  // reference are two different facts and must never share a column name.
  const initial = cents(r.Initial_Buy_Price);
  const latestRef = cents(r.Suggested_Buy_Price);
  const close = cents(r.Close_Price);
  const dataDate = String(r.Data_Date || "").slice(0, 10);

  // Which levels the card prints. A recommendation still waiting for its
  // entry -- or a row whose plan columns still describe an OLDER trade --
  // shows the recommendation's own fixed stop and target; an entered row
  // shows the fill-based ones.
  const useRec = rv.stop !== null && (rv.pending || (stale && rv.status === "active"));
  const entered = !stale && ["holding", "delay", "exit_today", "overdue", "exited"].includes(st);
  const fillTarget = cents(r.Fill_Target_Price);
  const stopKv = useRec
    ? kv("停損（跌破先出）", esc(fmtPrice(rv.stop)), "",
         "建議當日固定；成交後改以成交價 × 0.80 為準，只升不降")
    : planStopKv(r);
  const targetKv = useRec && rv.target !== null
    ? kv("停利目標", esc(fmtPrice(rv.target)), "gold", `建議當日固定（+${STRATEGY.targetPct}%）；成交後改以成交價計算`)
    : entered && fillTarget !== null
      ? kv("停利目標", esc(fmtPrice(fillTarget)), "gold", `推估成交價 ${fmtPrice(cents(r.Entry_Open))} +${STRATEGY.targetPct}%`)
      : kv("參考停利目標", esc(fmtPrice(cents(r.Target_Price))), "gold", "條件價，不代表已達成");

  const badge = v.ok
    ? `<span class="verdict ok">可買</span>`
    : `<span class="verdict no">不可買 · ${esc(v.text)}</span>`;
  const holdChip = grp === "hold" && HOLD_TEXT[st] && !stale
    ? `<span class="verdict held">${esc(HOLD_TEXT[st])}</span>` : "";

  const grid =
    kv("最新收盤價", esc(fmtPrice(close)), "", dataDate ? `資料日 ${dataDate}` : "") +
    (initial !== null
      ? kv("首日建議價", esc(fmtPrice(initial)), "gold", `固定 · ${esc(String(r.Recommended_On || "").slice(0, 10) || "首次建議日未提供")}`)
      : kv("首日建議價", "-", "", "後端尚未提供固定首日價")) +
    kv("最新觀察參考", esc(fmtPrice(latestRef)), "", "每次掃描重算，非新的買進指令") +
    kv("停損距離%", esc(fmt(r.Risk_Pct, 1, "%")), "", "價格到停損的距離，不是虧損機率") +
    stopKv +
    (stale || useRec ? "" : planAddKv(r)) +
    targetKv +
    scaleOutKv(r, stale || useRec, entered) +
    kv(sc.label, esc(fmt(r[sc.key], 1)), "", "規則分數，不是上漲機率") +
    chipKv(r);

  const recLine = r.Recommendation_ID
    ? `<div class="hint">建議編號 ${esc(r.Recommendation_ID)}｜${esc(rv.label)}</div>`
    : `<div class="hint">此列尚無固定建議編號（後端未建立 recommendation）。</div>`;

  // The previous trade: the backend's Prev_* columns when present; on an
  // older payload, the stale exit itself, as history rather than a badge.
  const prev = prevSegmentHtml(r) || (stale ? staleExitHtml(r) : "");
  // A reference row (non-held TSE, or any row that is neither a buy nor a
  // position) is never the owner's trade: its tracker exit is a simulated
  // trade of the past. Show it as the muted history line, not as the
  // red/orange "exit today" badge the hold group uses -- thirty of those in
  // one collapsed list read as thirty pending sell orders.
  const refExit = grp === "ref" && !stale && !!r.Exit_Signal;
  const refHist = refExit ? staleExitHtml(r) : "";

  let extra = "";
  let size = null;
  if (grp === "buy") {
    size = sizingFor(r);
    extra = `<div class="sub-h">投資人資訊</div>${investorInfoHtml(r)}` +
      `<div class="sub-h">股數與費用</div>${sizingHtml(r, size)}` +
      orderGuideHtml(r, size, rv) + nameHistoryLine(r.Stock_ID);
  } else if (grp === "hold") {
    extra = exitGuideHtml(r) + nameHistoryLine(r.Stock_ID);
  } else {
    const rn = restrictionNote(r);
    if (rn) extra = `<div class="hint">${esc(rn)}</div>`;
  }

  return `<article class="card pick ${scoreTier(r)} ${v.ok ? "state-buy" : ""}" data-sid="${esc(r.Stock_ID)}" data-group="${esc(grp)}">
    <div class="card-head">
      <span class="rank">${r._rank || "–"}</span>
      <span class="name">${esc(r.Stock_Name || r.Stock_ID)}</span>
      <span class="code">${esc(r.Stock_ID)}</span>
      <span class="market">${esc(r.Market || "")}</span>
      ${r._tracked ? '<span class="tag">追蹤</span>' : ""}
    </div>
    <div class="badges">${badge}${holdChip}${held ? '<span class="verdict held">已有持倉</span>' : ""}${
      r.Integrity_OK === false ? '<span class="verdict no">資料完整性未通過</span>' : ""}${
      stale || refExit ? "" : exitSignalBadge(r)}${restrictionBadge(r)}${eventBadge(r)}</div>
    ${recLine}
    ${prev}${refHist}
    ${held && r.Exit_Signal && !stale ? `<div class="hint">這個出場訊號算的是<b>系統假設的進場</b>（${esc(String(r.Entry_Date || "").slice(0, 10) || "進場日未知")} 開盤 ${esc(fmtPrice(cents(r.Entry_Open)))}）。你的持倉以<b>你登錄的成交價</b>另外計算，請以「持倉」頁的建議為準。</div>` : ""}
    <div class="kv2">${grid}</div>
    ${extra}
    <div class="btns">
      ${btn("buy", "登錄買入", grp === "buy" ? "primary" : "", { id: r.Stock_ID })}
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
  else if (dayIdx >= horizon && plan && plan.riding) {
    // the rule keeps it (own 5-day mean or the market leg): not "sell now"
    stateText = dayIdx === horizon ? `第 ${horizon} 天已到 · 規則續抱` : `已超過計畫 · D${dayIdx} / ${horizon} · 規則續抱`;
    stateCls = "holding";
  }
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
    kv("有效停損", plan ? esc(fmtPrice(plan.stop)) : "-", "",
       plan ? `可直接掛單（已對齊升降單位）` : "") +
    kv("加碼價（分批買法）", plan && plan.add_open ? esc(fmtPrice(plan.add)) : "-", "",
       plan
         ? (plan.add_open
             ? `第一筆成交價 ${fmtPrice(plan.base)} × 0.90，已對齊升降單位；一次買滿就不用`
             : "此筆已有兩次以上買進，不再加碼")
         : "") +
    kv("停利目標", plan ? esc(fmtPrice(plan.target)) : "-", "gold",
       plan ? "條件價 · 已對齊升降單位" : "") +
    kv("已實現淨損益", esc(fmtPnl(pos.realized_net)), signClass(pos.realized_net), "已扣實際費稅") +
    kv("若今日全數賣出", m && m.net_if_liquidated !== null ? esc(fmtPnl(m.net_if_liquidated)) : "-",
       m && m.net_if_liquidated !== null ? signClass(m.net_if_liquidated) : "",
       "估計值，扣掉賣出費稅");

  // Report 5.4: "+2% 這個數字不代表已經保住該獲利" -- the threshold is a
  // CONDITION, and armed / not-armed must look obviously different.
  const trail = plan
    ? (plan.armed
        ? `<div class="plan armed">鎖利：已啟動（收盤曾達 ${esc(fmtPrice(plan.highest_close))}，${esc(plan.armed_on)}）· 有效停損已上調至 ${esc(fmtPrice(plan.stop))}，只升不降</div>`
        : `<div class="plan">鎖利：尚未啟動；需要<b>收盤</b>站上 ${esc(fmtPrice(plan.arm))}（條件，非已達成），隔一個交易日起生效 · 未啟動前停損維持 ${esc(fmtPrice(plan.initial_stop))}</div>`)
    : "";

  // "續抱" on its own is what the 2026-09-17 complaint was about: a position
  // 10% under water read exactly like one 10% up. The prices that decide what
  // to do next belong in the sentence.
  const holdLine = plan
    ? `建議（以你登錄的成交價 ${fmtPrice(plan.base)} 計算）：續抱。跌破 ${fmtPrice(plan.stop)} 先出場${
        plan.add_open ? `；分批買法可在 ${fmtPrice(plan.add)} 補另一半` : ""
      }；${plan.armed ? "鎖利已啟動" : `收盤站上 ${fmtPrice(plan.arm)} 後，隔一個交易日起停損上調到 ${fmtPrice(plan.lock)}`}。`
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
      bits.push(`5 日均價 ${fmtPrice(ma5)}，${close > ma5
        ? "站上（個股這一關過，到期可續抱）"
        : "跌破（個股這一關沒過，到期要看當天大盤是否回檔）"}`);
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

  const rideNow = plan ? rideView(plan, dayIdx, horizon) : null;
  const advice = dayIdx === null
    ? `<div class="plan">建議：無法計算持有天數，請確認成交日期。</div>`
    : dayIdx >= horizon
      ? (rideNow
        ? `<div class="plan${rideNow.alert ? " alert" : ""}">建議：${esc(rideNow.time)}實際賣出以你的成交回報為準。</div>`
        : `<div class="plan alert">建議：第 ${horizon} 個交易日已到。收盤若仍站上自己的 5 日均價、或當天大盤正在回檔（${MARKET_LEG_TEXT}），就續抱（最晚第 ${STRATEGY.cap} 天），否則依策略於收盤出場；實際賣出以你的成交回報為準。</div>`)
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

  ensureRecs();
  document.getElementById("page-perf").innerHTML =
    systemRecordHtml() +
    recordsSectionHtml() +
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
    ["續抱（到期不賣）", "第 10 天起每天收盤：站上自己的 5 日均價，或當天大盤正在回檔（加權指數低於 20 日均線、仍高於 60 日均線）就續抱，最晚第 20 天"],
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
      drow("清單狀態", esc(listStatusText(listStatus()))) +
      drow("建議紀錄（meta.rec）", esc(recMetaText(m.rec))) +
      drow("營收／除權息資料", esc(eventsMetaText(m.events))) +
      drow("AI 報告來源", esc(reportSourcesText(m.report_sources))) +
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
      <div class="hint">資金設定（換算建議股數用）：${esc(sizingSummaryText())}</div>
      <div class="btns">${btn("sizing", "資金與格數設定", "")}</div>
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
    ${manual ? field("stock_id", "股票代號", "", { inputmode: "text", hint: "半形英數字，例如 3088、00878、00679B（英文字母會自動轉大寫）" }) +
               field("stock_name", "股票名稱（可留空）", "") : ""}
    ${refNote.length ? `<div class="hint">參考價：${esc(refNote.join("｜"))}。這些只是參考，系統不會替你填成交價。</div>` : ""}
    ${field("session_date", "成交日期", defDate, { type: "date",
      hint: dateNote || "沒有成交日就無法放上損益時間軸" })}
    ${field("price", isBuy ? "實際成交價" : "實際賣出價", defPrice, { inputmode: "decimal", hint: "必填。輸入非數字或 0 會被拒絕，不會用參考價代替" })}
    ${field("shares", "股數", defShares, { inputmode: "numeric",
      hint: isBuy ? buyQtyHint(row)
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
  const row = data.row ? listRowFor(data.row) : null;

  // A manual entry needs a stock id before anything else can be validated.
  if (!pos && !row) {
    if (!normStockId(v.stock_id)) {
      showErrors(modal, { stock_id: "請填有效的股票代號：4～7 碼半形英數字（例如 3088、00878、00679B），不接受全形字" });
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
    const sid = row ? String(row.Stock_ID) : normStockId(v.stock_id);
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

  const evLine = eventsLine(r);
  const body =
    grp("投資人資訊", investorInfoHtml(r)) +
    (restrictionNote(r) ? grp("交易限制", restrictionGuidance(r)) : "") +
    grp("營收／除權息／法說",
      (evLine ? `<div class="plan">${esc(evLine)}</div>` : `<div class="hint">本次掃描未提供營收／除權息／法說資料。</div>`) +
      `<div class="hint">月營收是掃描當時最新已公布的月份（每月 10 日前公布上月），只供參考、不代表好壞；回測中用營收分組篩選訊號都沒有改善（BACKTEST_LOG M.6）。</div>`) +
    grp("系統訊號紀錄（同名）", nameHistoryDetail(r.Stock_ID)) +
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
  const sizingRow = (data.meta || []).find((m) => m && m.key === SIZING_META);
  const importedSizing = sizingRow ? sizingFromMeta(sizingRow.value) : null;
  if (sizingRow && !importedSizing) { err.textContent = "備份裡的資金設定格式錯誤（資金、格數或風險 % 超出範圍）"; return; }
  await dbPutMany("positions", data.positions);
  await dbPutMany("executions", data.executions || []);
  for (const m of (data.meta || [])) {
    if (m && m.key && !PRIVATE_META.has(m.key)) await dbPut("meta", m);
  }
  if (importedSizing) {
    SIZING_MEM = importedSizing;
    try { localStorage.setItem(SIZING_KEY, JSON.stringify(importedSizing)); } catch (e) { /* memory + DB */ }
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
    if (act === "sizing") { openSizingForm(); return; }
    if (act === "sizing-save") { saveSizingForm(); return; }
    if (act === "cloud-refresh") { await cloudRefresh(true); return; }
    if (act === "token-set") { openTokenForm(); return; }
    if (act === "token-save") { await saveToken(); return; }
    if (act === "token-clear") { await clearToken(); return; }

    if (act === "buy") {
      const pos = d.pos ? await dbGet("positions", d.pos) : null;
      const row = d.id ? listRowFor(d.id) : null;
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

// <details> open state across re-renders. "toggle" does not bubble, hence the
// capture phase. A group forced open by a search is not the user's choice and
// is not remembered.
document.addEventListener("toggle", (ev) => {
  const el = ev.target;
  if (!el || !el.matches) return;
  if (el.matches("details.grp[data-group='ref']")) {
    if (!el.hasAttribute("data-forced")) STATE.refOpen = el.open;
  } else if (el.matches("details.order[data-order]")) {
    STATE.orderOpen[el.getAttribute("data-order")] = el.open;
  } else if (el.matches("details[data-trec]")) {
    STATE.recOpen[el.getAttribute("data-trec")] = el.open;
  }
}, true);

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
    const sz = sizingFromMeta(await metaGet(SIZING_META, null));
    if (sz) SIZING_MEM = sz;
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
  // 2026-10-08 investor views (tests/mobile_probe.js drives these)
  pickGroup, pickGroups, picksSummaryText, holdPriority, listStatus, listStatusText,
  restrictionInfo, restrictionNote, recView, staleExit, sizePosition, sizingFor,
  validateSizing, loadSizing, saveSizing, sizingToMeta, sizingFromMeta, schedule, feeBindsMin, nameHistory, historyEntryText,
  eventsLine, aiReportView, systemRecordHtml, equitySvg, pickCard, render, renderPicks,
  renderToday, renderPerf, renderResearch, openDetail, listRowFor,
  exitGuideHtml, reportSourcesText, provisionalTimeExit,
  // 2026-10-09 conformance fixes (tests/mobile_probe.js sections G-L)
  activePlan, tomorrowOrders, positionCard, marketLegFor, planLevel, tickSize, rideState, rideView,
  normStockId, scaleOutKv, holdClock, holdClockText, lateDueNow, regimeView, viewRows, sizingStop, PLAN_MILLI,
  expectedSession, dataIsStale, AUTO_MAX_PER_SESSION, AUTO_GAP_MS, POLL_LIMIT_MS,
  BLOCK_TEXT, RESTRICT_TEXT, RESTRICT_NOTE, ORDER_RULES, STRATEGY, SLOT_GUIDANCE,
  // 2026-10-09 complete record of a closed recommendation + reconciliation
  // (tests/mobile_probe.js sections O-P)
  recOutcome, tradeRecordView, tradeRecordHtml, tradeRecordIssues, closedRecords, recordsSectionHtml,
  reconcileTrade, reconcileHtml, sessionGap, gapText, recExecsFor, recCalendars, recPath, pathTableHtml,
  tnum, tint, tdate, trStatusText, trReasonText, loadRecs, ensureRecs,
  TR_BASIS_TEXT, TR_STATUS_TEXT, TR_ISSUE_TEXT, TR_RECORDS_KEPT,
};
