// The phone's copy of the settled rule, stated as LITERALS. ASCII only.
//
// mobile/app.js re-implements parts of the strategy (the numbers on the order
// card, the quote ladder, the buy verdict, the sizing, the auto-refresh
// clock). The Python side cannot see it, and the page's own checks read the
// numbers back from the app, so a drifted value is self-consistent there.
// This probe boots the whole app in a vm context (same stub DOM as
// tests/mobile_probe.js) and compares what it exposes to the numbers the owner
// decided. Driven by tests/test_pin_phone.py, which skips when node is missing.
//
// Deliberately NOT here: activePlan (the open-position plan builder) and the
// market-leg publication; the late rung is driven through tomorrowOrders with
// a hand-built plan so it does not depend on activePlan.
//
//     node tests/pin_phone_probe.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const APP_SRC = fs.readFileSync(path.join(ROOT, "mobile", "app.js"), "utf8");
const LIVE = JSON.parse(fs.readFileSync(
  path.join(ROOT, "tests", "fixtures", "mobile", "scan_2026-10-07.json"), "utf8"));

let pass = 0;
const failures = [];
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) pass++; else failures.push(name + "  got " + g + "  want " + w);
}

function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", hidden: false, value: "", dataset: {}, style: {},
    addEventListener() {}, removeEventListener() {}, focus() {}, setSelectionRange() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; }, hasAttribute() { return false; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
}
const tick = () => new Promise((r) => setImmediate(r));

async function boot(payload) {
  const els = {};
  const el = (k) => els[k] || (els[k] = makeEl(k));
  const store = {};
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const document = {
    getElementById: (id) => (String(id).startsWith("page-") ? el(id) : null),
    querySelector: (s) => (["#tabs", "#notices", "#asof", "#status", "#toast", "#modal"].includes(s) ? el(s) : null),
    querySelectorAll: () => [], addEventListener() {}, visibilityState: "visible",
  };
  const body = JSON.stringify(payload);
  const fetch = async (url) => (String(url).startsWith("./scan_result.json")
    ? { ok: true, status: 200, text: async () => body, json: async () => JSON.parse(body) }
    : { ok: false, status: 404, text: async () => "", json: async () => ({}) });
  const sandbox = {
    document, localStorage, fetch, navigator: {}, console,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    confirm: () => false, alert() {}, location: { reload() {}, href: "https://example.test/" },
    isSecureContext: false, addEventListener() {}, scrollTo() {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(APP_SRC, sandbox, { filename: "app.js" });
  for (let i = 0; i < 500 && !sandbox.YT.STATE.loadedAt; i++) await tick();
  return { YT: sandbox.YT, ev: (s) => vm.runInContext(s, sandbox) };
}

// ---- reference ladders, written out as literals (cents) ---------------------
function refTick(p, etf) {
  if (etf) return p < 5000 ? 1 : 5;
  return p < 1000 ? 1 : p < 5000 ? 5 : p < 10000 ? 10 : p < 50000 ? 50 : p < 100000 ? 100 : 500;
}
function refRound(p, dir, etf) {
  const t = refTick(p, etf);
  const q = Math.floor(p / t), r = p - q * t;
  if (dir === "down") return q * t;
  if (dir === "up") return (r === 0 ? q : q + 1) * t;
  return (r * 2 >= t ? q + 1 : q) * t;
}

(async () => {
  const { YT, ev } = await boot(LIVE);
  const st = YT.STATE;

  // 1. the rule, as numbers
  const S = YT.STRATEGY;
  eq("STRATEGY", {
    stopPct: S.stopPct, armPct: S.armPct, lockPct: S.lockPct, targetPct: S.targetPct,
    addPct: S.addPct, addFirstPct: S.addFirstPct, scaleOutPct: S.scaleOutPct,
    lateFrom: S.lateFrom, lateGainPct: S.lateGainPct, horizon: S.horizon, cap: S.cap,
    version: S.version,
  }, {
    stopPct: -20, armPct: 2.5, lockPct: 2, targetPct: 20, addPct: -10, addFirstPct: 50,
    scaleOutPct: 15, lateFrom: 8, lateGainPct: 1, horizon: 10, cap: 20,
    version: "prelaunch-2026-09-21",
  });
  eq("N_ENTER_UI", ev("N_ENTER_UI"), 20);

  // 2. the quote ladders, literal tables and every cent from 0.01 to 2000.00
  eq("EQUITY_TICKS", ev("EQUITY_TICKS"),
    [[1000, 1], [5000, 5], [10000, 10], [50000, 50], [100000, 100], [null, 500]]);
  eq("ETF_TICKS", ev("ETF_TICKS"), [[5000, 1], [null, 5]]);
  let bad = 0, first = "";
  for (const [sid, etf] of [["2330", false], ["0050", true], ["00878", true], ["00663L", true]]) {
    for (const dir of ["down", "up", "nearest"]) {
      for (let p = 1; p <= 200000; p++) {
        const got = YT.tickRound(p, dir, sid), want = refRound(p, dir, etf);
        if (got !== want) { bad++; if (!first) first = sid + " " + dir + " " + p + " got " + got + " want " + want; }
      }
    }
  }
  eq("tickRound equals the literal ladder (" + (bad ? first : "all") + ")", bad, 0);
  eq("tickRound refuses an unusable price", [YT.tickRound(0, "down", "2330"), YT.tickRound(-5, "up", "2330"),
    YT.tickRound(null, "up", "2330")], [null, null, null]);
  // the two directions that matter, at the points the strategy actually uses
  eq("a stop rounds DOWN and a target UP on the equity ladder",
    [YT.tickRound(15320, "down", "6488"), YT.tickRound(22980, "up", "6488")], [15300, 23000]);
  eq("ETF 0050: lock 108.85 stays, 109.45 target rounds up to the 0.05 tick",
    [YT.tickRound(10885, "down", "0050"), YT.tickRound(10941, "up", "0050")], [10885, 10945]);
  eq("isEtfCode", ["0050", "00878", "00663L", "00400A", "2330", "00A", "0088888X", "006208", "", null]
    .map((s) => ev("isEtfCode")(s)), [true, true, true, true, false, false, false, true, false, false]);

  // 3. the fee schedules (mirror of portfolio/money.py)
  const v1 = ev("FEE_SCHEDULES")["tw-equity-v1"], exact = ev("FEE_SCHEDULES")["tw-equity-exact"];
  eq("fee schedule v1", [v1.feeNum, v1.feeDen, v1.discNum, v1.discDen, v1.taxNum, v1.taxDen, v1.minFeeCents, v1.roundToDollar],
    [1425, 1000000, 1, 1, 3, 1000, 2000, true]);
  eq("fee schedule exact", [exact.feeNum, exact.feeDen, exact.taxNum, exact.taxDen, exact.minFeeCents, exact.roundToDollar],
    [1425, 1000000, 3, 1000, 0, false]);
  eq("default schedule", ev("DEFAULT_SCHEDULE"), "tw-equity-v1");
  // NT$100 x 1000 shares = 10,000,000 cents: fee 142.5 -> 142, tax 300
  eq("fee and tax on NT$100,000 (cents)", [YT.feeFor(v1, 10000000), YT.taxFor(v1, 10000000)], [14200, 30000]);
  eq("minimum fee NT$20 binds below NT$14,036", [YT.feeFor(v1, 1403500), YT.feeFor(v1, 1403600), YT.feeFor(v1, 1500000)],
    [2000, 2000, 2100]);
  eq("exact schedule keeps the cents and has no floor", [YT.feeFor(exact, 1000000), YT.taxFor(exact, 1000000), YT.feeFor(exact, 100)],
    [1425, 3000, 0]);

  // 4. the late profit-take rung, driven through tomorrowOrders with a
  //    hand-built armed plan (no activePlan)
  const pos = { open_shares: 1000, avg_cost: 19150, first_buy_price: 19150, position_id: "p1", stock_id: "6488" };
  const plan = {
    stock_id: "6488", base: 19150, armed: true, add_open: false,
    target: 22980, target_orderable: 23000, scale_out: 22023, scale_out_orderable: 22050,
    stop: 19533, stop_orderable: 19500, arm: 19629, lock: 19533, add: 17235, add_orderable: 17200,
    ma5: null, riding: false, last_close: 19000, late: 19350,
  };
  // the rung's price is built by activePlan (not driven here): pin the level it builds
  eq("the late level on a 191.50 fill: +1% -> 193.415 rounded UP on the 0.5 tick",
    ev("planLevel")(19150, ev("PLAN_MILLI").late, "up", "6488"), 19350);
  const orders = (day) => ev("tomorrowOrders")(pos, plan, day, 10);
  // 191.50 x 1.01 = 193.415 -> 193.42 -> up on the 0.5 tick = 193.50 (down would read 193.00)
  eq("no late rung on day 7", [orders(7).includes("193.50"), orders(7).includes("193.00")], [false, false]);
  eq("the late rung is on the card from day 8, rounded UP", [orders(8).includes("193.50"), orders(8).includes("193.00")], [true, false]);
  eq("the late rung stays on days 9 and 10", [orders(9).includes("193.50"), orders(10).includes("193.50")], [true, true]);
  eq("an unknown day number shows no late rung", orders(null).includes("193.50"), false);

  // 5. the regime and the buy verdict: the phone only ever downgrades
  const regOk = { ok: true, is_current: true, enter_ok: true, risk_on: true, strong: false, as_of_date: "2026-09-10" };
  const setReg = (r) => { st.meta.regime = r; };
  setReg(Object.assign({}, regOk, { is_current: false }));
  eq("a stale regime never enters", ev("regimeView")().enterOk, false);
  setReg(Object.assign({}, regOk, { enter_ok: false }));
  eq("risk_on without enter_ok never enters", ev("regimeView")().enterOk, false);
  setReg(Object.assign({}, regOk, { enter_ok: false, risk_on: false }));
  eq("a headwind never enters", ev("regimeView")().enterOk, false);
  setReg({ ok: false });
  eq("an unreadable regime never enters", ev("regimeView")().enterOk, false);
  setReg(null);
  eq("a missing regime never enters", ev("regimeView")().enterOk, false);
  setReg(regOk);
  eq("a current enter_ok regime enters", ev("regimeView")().enterOk, true);
  setReg(Object.assign({}, regOk, { strong: true }));
  eq("a strong regime enters", ev("regimeView")().enterOk, true);
  setReg(regOk);

  ev("CAL_LAST = '2026-09-10'");
  const ready = { Buy_Ready: true, Data_Date: "2026-09-10" };
  const v = (row) => ev("buyVerdict")(row);
  eq("verdict ok on the newest bar", [v(ready).ok, v(ready).code], [true, ""]);
  eq("valid-until equal to the newest session is still valid",
    v(Object.assign({}, ready, { Rec_Valid_Until: "2026-09-10" })).ok, true);
  eq("valid-until one session old is stale",
    [v(Object.assign({}, ready, { Rec_Valid_Until: "2026-09-09" })).ok,
      v(Object.assign({}, ready, { Rec_Valid_Until: "2026-09-09" })).code], [false, "stale"]);
  eq("a bar one session old is stale", [v({ Buy_Ready: true, Data_Date: "2026-09-09" }).ok,
    v({ Buy_Ready: true, Data_Date: "2026-09-09" }).code], [false, "stale"]);
  eq("a bar newer than the calendar is not stale", v({ Buy_Ready: true, Data_Date: "2026-09-11" }).ok, true);
  setReg(Object.assign({}, regOk, { is_current: false }));
  eq("a fresh bar under a stale regime is downgraded to regime", [v(ready).ok, v(ready).code], [false, "regime"]);
  setReg(regOk);
  eq("a backend refusal is never turned into a buy", [
    v({ Buy_Ready: false, Buy_Block: "rank", Data_Date: "2026-09-10" }).ok,
    v({ Buy_Ready: false, Buy_Block: "rank", Data_Date: "2026-09-10" }).code,
    v({ Buy_Ready: false, Buy_Block: "rank", Data_Date: "2026-09-10" }).source,
    v({ Buy_Ready: false, Buy_Block: "", Data_Date: "2026-09-10" }).ok,
    v({ Data_Date: "2026-09-10" }).ok,
  ], [false, "rank", "backend", false, false]);
  eq("only a literal true is a buy", [v({ Buy_Ready: "true", Data_Date: "2026-09-10" }).ok,
    v({ Buy_Ready: 1, Data_Date: "2026-09-10" }).ok], [false, false]);
  ev("CAL_LAST = ''");

  // 6. sizing: defaults, bounds, and a worked example against the fee schedule
  eq("SIZING_DEFAULTS", ev("SIZING_DEFAULTS"), { slots: 8, risk_pct: 1 });
  const vs = (cap, slots, risk) => YT.validateSizing({ capital: cap, slots, risk_pct: risk }).ok;
  eq("capital bounds 10,000 .. 100,000,000", [vs("9999", "8", "1"), vs("10000", "8", "1"), vs("100000000", "8", "1"),
    vs("100000001", "8", "1"), vs("1,000,000", "8", "1")], [false, true, true, false, true]);
  eq("slot bounds 1 .. 20", [vs("1000000", "0", "1"), vs("1000000", "1", "1"), vs("1000000", "20", "1"),
    vs("1000000", "21", "1")], [false, true, true, false]);
  eq("risk bounds 0.1 .. 5 percent, two decimals", [vs("1000000", "8", "0.09"), vs("1000000", "8", "0.1"),
    vs("1000000", "8", "5"), vs("1000000", "8", "5.01"), vs("1000000", "8", "1.005")], [false, true, true, false, false]);
  const sched = YT.schedule("tw-equity-v1");
  const sz = { capital: 1000000, slots: 8, risk_pct: 1 };
  // NT$100 entry, stop NT$80: budget NT$125,000 per slot -> 1248 shares; risk NT$10,000 -> 487 shares
  let r = YT.sizePosition(10000, 8000, sz, sched, {});
  eq("sizing worked example: the smaller of the two limits", [r.budgetShares, r.riskShares, r.shares, r.limit, r.reason],
    [1248, 487, 487, "risk", "risk"]);
  eq("the loss at the stop fits the risk budget and one more share does not", [r.maxLossCents <= 1000000, r.maxLossCents],
    [true, 998000]);
  r = YT.sizePosition(10000, 9900, sz, sched, {});
  eq("a tight stop is limited by the slot budget", [r.budgetShares, r.shares, r.limit], [1248, 1248, "budget"]);
  r = YT.sizePosition(10000, 8000, sz, sched, { held: true });
  eq("a name already held gets no new size", r.reason, "held");
  r = YT.sizePosition(10000, 8000, sz, sched, { openCount: 8 });
  eq("no free slot", r.reason, "slots");
  eq("the sizing stop falls back to the rule's -20% rounded DOWN",
    [ev("sizingStop")({ Stock_ID: "6488" }, 19150), ev("sizingStop")({ Stock_ID: "0050" }, 10675)], [15300, 8540]);
  eq("the plan stop is used before entry", ev("sizingStop")({ Stock_ID: "6488", Plan_Stop: 153.0, Hold_Status: "" }, 19150), 15300);

  // 7. the auto-refresh clock
  eq("clock constants", [ev("EOD_READY_MIN"), ev("AUTO_MAX_PER_SESSION"), ev("AUTO_GAP_MS"), ev("POLL_LIMIT_MS")],
    [900, 2, 5400000, 1500000]);
  const exp = (iso) => ev("expectedSession")(new Date(iso));
  eq("14:59 Taipei is not ready", exp("2026-10-09T06:59:00Z"), "2026-10-08");
  eq("15:00 Taipei is ready", exp("2026-10-09T07:00:00Z"), "2026-10-09");
  eq("Monday before 15:00 falls back past the weekend to Friday", exp("2026-10-12T06:59:00Z"), "2026-10-09");
  eq("Monday after 15:00 is Monday", exp("2026-10-12T07:00:00Z"), "2026-10-12");
  eq("Saturday noon falls back to Friday", exp("2026-10-10T04:00:00Z"), "2026-10-09");
  eq("Sunday evening falls back to Friday", exp("2026-10-11T12:00:00Z"), "2026-10-09");

  if (failures.length) {
    console.log("FAIL " + failures.length + " (pass " + pass + ")");
    for (const f of failures) console.log("  - " + f);
    process.exit(1);
  }
  console.log("PASS " + pass);
})().catch((e) => { console.log("ERROR " + (e && e.stack || e)); process.exit(2); });
