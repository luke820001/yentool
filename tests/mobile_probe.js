// Boot the whole phone app outside a browser and render every page.
//
// mobile/app.js touches window, document, navigator, localStorage and
// IndexedDB at load, so fold_probe.js cuts pure functions out of it. This
// probe goes the other way: it runs the ENTIRE file in a vm context with a
// stub DOM (every page container is a plain object whose innerHTML is
// captured), IndexedDB absent (the app's own "private window" path) and a
// fetch that serves one payload as ./scan_result.json. That proves the 2026-10-08
// investor views degrade on an OLD payload and render on a new one, and lets
// the arithmetic (fees, sizing) be checked against the backend's pins.
//
//     node tests/mobile_probe.js                       (run from anywhere)
//     node tests/mobile_probe.js --fee-grid <cases.json> --plan-grid <plan.json>
//                                --trade-grid <trade.json>
//
// --fee-grid adds section F: every [schedule, considerationCents, feeCents,
// taxCents] case (computed by portfolio/money.py FeeSchedule in the Python
// test) must come out of the app's feeFor/taxFor identically.
//
// --plan-grid adds section M: {levels, armed, ride} computed by the BACKEND
// (holding_tracker._lvl, exit_rules.replay_exit) -- every plan level, the
// arming test and the ride verdict of activePlan must equal them.
//
// --trade-grid adds section P: closed-recommendation records built by the
// BACKEND (live_record.replay_trade + trade_record, stored through
// portfolio.sync._outcome) with the owner's-fills reconciliation cases; the
// phone must print exactly the numbers the backend wrote (tests/test_mobile_trade_record.py).
// Section O needs no grid: it runs the same checks on frozen backend records.
//
// Payloads:
//   old   tests/fixtures/mobile/scan_2026-09-23.json -- the 09-23 publish
//         (all 41 rows, 12 of its 58 tracked rows, meta untouched). The live
//         mobile/scan_result.json is gitignored, so CI never has it.
//   live  tests/fixtures/mobile/scan_2026-10-07.json -- the 10-07 publish,
//         trimmed to its rows and the tracked rows the hold group needs
//   new   the live payload plus every field stages B1-B6 add (built below)
//
// Driven by tests/test_mobile_probe.py, which skips when node is missing.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
// YT_APP_JS points the probe at another copy of the app (a deliberately broken
// one, to prove the checks below can fail); unset, it is mobile/app.js.
const APP_SRC = fs.readFileSync(process.env.YT_APP_JS || path.join(ROOT, "mobile", "app.js"), "utf8");
const REPORT_TEXT = JSON.parse(fs.readFileSync(path.join(ROOT, "config", "report_text.json"), "utf8"));
const OLD = JSON.parse(fs.readFileSync(
  path.join(ROOT, "tests", "fixtures", "mobile", "scan_2026-09-23.json"), "utf8"));
const GRID_AT = process.argv.indexOf("--fee-grid");
const FEE_GRID = GRID_AT > 0 ? process.argv[GRID_AT + 1] : null;
const PLAN_AT = process.argv.indexOf("--plan-grid");
const PLAN_GRID = PLAN_AT > 0 ? process.argv[PLAN_AT + 1] : null;
const TRADE_AT = process.argv.indexOf("--trade-grid");
const TRADE_GRID = TRADE_AT > 0 ? process.argv[TRADE_AT + 1] : null;
let gridCases = 0;
let planCases = 0;
let tradeCases = 0;
let reconCases = 0;
const LIVE = JSON.parse(fs.readFileSync(
  path.join(ROOT, "tests", "fixtures", "mobile", "scan_2026-10-07.json"), "utf8"));

let pass = 0;
const failures = [];
function ok(name, cond, extra) {
  if (cond) pass++;
  else failures.push(name + (extra === undefined ? "" : "  (got " + String(extra).slice(0, 300) + ")"));
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

async function boot(payload, opts) {
  const o = opts || {};
  const els = {};
  const listeners = {};
  const el = (k) => els[k] || (els[k] = makeEl(k));
  const store = Object.assign({}, o.storage || {});
  const localStorage = o.storageThrows
    ? { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() {} }
    : { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; } };
  const document = {
    getElementById: (id) => (String(id).startsWith("page-") ? el(id) : null),
    querySelector: (s) => (["#tabs", "#notices", "#asof", "#status", "#toast", "#modal"].includes(s) ? el(s) : null),
    querySelectorAll: () => [],
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    visibilityState: "visible",
  };
  const body = JSON.stringify(payload);
  // o.recs is served as ./recommendations.json (an object, or text as it is);
  // absent, that file is a 404. o.fetchLog collects every request the app makes,
  // so a test can see exactly what left the phone.
  let recsHits = 0;
  const recsBody = o.recs === undefined ? null : (typeof o.recs === "string" ? o.recs : JSON.stringify(o.recs));
  const serve = (text) => ({ ok: true, status: 200, text: async () => text, json: async () => JSON.parse(text) });
  const fetch = async (url, init) => {
    if (o.fetchLog) o.fetchLog.push({ url: String(url), init: init === undefined ? null : init });
    // a page that asks for the same file in a loop must fail a check, not hang the run
    if (String(url) === "./recommendations.json" && ++recsHits > 25) return new Promise(() => {});
    if (String(url).startsWith("./scan_result.json")) return serve(body);
    if (recsBody !== null && String(url) === "./recommendations.json") return serve(recsBody);
    return { ok: false, status: 404, text: async () => "", json: async () => ({}) };
  };
  const sandbox = {
    document, localStorage, fetch, navigator: {}, console,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    confirm: () => false, alert() {}, location: { reload() {}, href: "https://example.test/" },
    isSecureContext: false, addEventListener() {}, scrollTo() {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(APP_SRC, sandbox, { filename: "app.js" });
  const YT = sandbox.YT;
  for (let i = 0; i < 500 && !YT.STATE.loadedAt; i++) await tick();
  return { YT, els, store, listeners };
}

function renderAll(env) {
  const out = {};
  for (const id of ["today", "picks", "positions", "perf", "research"]) {
    env.YT.STATE.page = id;
    env.YT.render();
    out[id] = env.els["page-" + id].innerHTML;
  }
  out.notices = env.els["#notices"] ? env.els["#notices"].innerHTML : "";
  out.asof = env.els["#asof"] ? env.els["#asof"].textContent : "";
  return out;
}

function sids(list) { return list.map((r) => String(r.Stock_ID)); }
function cardOf(env, sid, group) {
  const r = env.YT.listRowFor(sid);
  return r ? env.YT.pickCard(r, group) : "";
}

// --- the synthetic NEW payload ----------------------------------------------
function addDays(d, n) {
  const t = new Date(d + "T00:00:00Z");
  t.setUTCDate(t.getUTCDate() + n);
  return t.toISOString().slice(0, 10);
}

function synthetic(base, listState) {
  const p = JSON.parse(JSON.stringify(base));
  const m = p.meta;
  m.scan_time = "2026-10-07 15:42:10";
  m.list_status = listState === "provisional"
    ? { policy: "final-once-v1", state: "provisional", reasons: ["data_lag", "checks_fail"],
        session: "2026-10-07", published_at: "2026-10-07 15:42:10", first_published_at: null,
        revision: 0, strategy_version: m.strategy_version, revised_reason: null, revised_at: null }
    : { policy: "final-once-v1", state: "final", reasons: [], session: "2026-10-07",
        published_at: "2026-10-07 15:42:10", first_published_at: "2026-10-07 15:42:10",
        revision: 1, strategy_version: m.strategy_version, revised_reason: null, revised_at: null };
  m.report_sources = {
    ALL: { source: "gemini", model: "gemini-2.5-flash", attempts: 1, seconds: 11.2, error: null },
    OTC: { source: "template", model: null, attempts: 3, seconds: 40.1, error: "quota" },
    TSE: { source: "groq", model: "llama", attempts: 2, seconds: 9.9, error: null },
  };
  m.events = {
    ok: true, updated_at: "2026-10-07 15:30:00", session_date: "2026-10-07", sources: {},
    revenue_month_latest: "2026-09", next_revenue_deadline: "2026-10-12",
    next_report_deadline: { date: "2026-11-14", what: "Q3", approximate: true },
    coverage: { rows: 44, revenue: 40, exdiv: 3, conf: 2 }, note: "display only; never scored",
  };
  m.rec = { created: 1, attached: 3, closed: 1, expired: 0, superseded: 0, backfilled: 0,
            moved: 0, deferred: 1, writes: 2, advanced: true };
  m.quality = Object.assign({}, m.quality, {
    restrictions: { ok: true, counts: { disposition: 1, attention: 1, limit_lock: 1, suspended: 1 } } });
  const blank = {
    Trade_Restriction: "none", Restriction_Flags: null, Restriction_Since: null,
    Restriction_Until: null, Restriction_Match_Min: null, Restriction_Prepay: null,
    Rev_Month: null, Rev_Amount_K: null, Rev_YoY_Pct: null, Rev_MoM_Pct: null,
    Rev_Cum_YoY_Pct: null, Ex_Date: null, Ex_Kind: null, Ex_Cash_Div: null, Conf_Date: null,
    Prev_Signal_Date: null, Prev_Was_Signal: null, Prev_Entry_Date: null, Prev_Entry_Open: null,
    Prev_Exit_Signal: null, Prev_Exit_Signal_Date: null, Prev_Exit_Signal_Price: null,
    Prev_Exit_Ret_Pct: null, Sessions_Since_Prev_Exit: null, Rec_Status_Reason: null,
  };
  for (const r of p.rows.concat(p.tracked)) Object.assign(r, blank);
  const row = (sid) => p.rows.find((r) => r.Stock_ID === sid) || p.tracked.find((r) => r.Stock_ID === sid);
  // 8227: a new trade (B1 re-anchored it) on a disposition, with its old one
  // in Prev_*, revenue and an ex-dividend inside the week.
  Object.assign(row("8227"), {
    Hold_Status: "pending", Exit_Signal: null, Exit_Signal_Date: null, Exit_Signal_Price: null,
    Entry_Date: null, Entry_Open: null, Plan_Stop: 297.5, Rec_Valid_Until: "2026-10-08",
    Prev_Signal_Date: "2026-09-16", Prev_Was_Signal: true, Prev_Entry_Date: "2026-09-17",
    Prev_Entry_Open: 273, Prev_Exit_Signal: "tp", Prev_Exit_Signal_Date: "2026-09-29",
    Prev_Exit_Signal_Price: 327.6, Prev_Exit_Ret_Pct: 19.41, Sessions_Since_Prev_Exit: 6,
    Trade_Restriction: "disposition", Restriction_Flags: "disposition",
    Restriction_Since: "2026-10-01", Restriction_Until: "2026-10-15",
    Restriction_Match_Min: 2, Restriction_Prepay: "all",
    Rev_Month: "2026-09", Rev_Amount_K: 812345, Rev_YoY_Pct: 35.2, Rev_MoM_Pct: -3.1,
    Rev_Cum_YoY_Pct: 20.4, Ex_Date: "2026-10-12", Ex_Kind: "div", Ex_Cash_Div: 5.5,
    Conf_Date: "2026-10-20",
  });
  const tse = p.rows.filter((r) => r.Market === "TSE");
  Object.assign(tse[0], { Trade_Restriction: "attention", Restriction_Flags: "attention" });
  Object.assign(tse[1], { Trade_Restriction: "limit_lock", Restriction_Flags: "limit_lock" });
  Object.assign(tse[2], { Trade_Restriction: "suspended", Restriction_Flags: "suspended",
                          Buy_Block: "restricted", Buy_Ready: false });
  Object.assign(tse[3], { Trade_Restriction: "none", Restriction_Flags: "limit_down" });
  // 3498: its active recommendation's trade replaced the old streak
  Object.assign(row("3498"), { Rec_Status_Reason: null });
  // a closed recommendation on a tracked name
  const t = row("1815");
  Object.assign(t, { Rec_Status: "closed", Rec_Status_Reason: "stop", Hold_Status: "exited" });
  // live_record: dates on every trade, the bench and by_sid
  const lr = m.live_record;
  for (const x of lr.tradable.trades) {
    x.entry_date = addDays(x.sig, 1);
    x.exit_date = x.ret === null || x.ret === undefined ? null : addDays(x.sig, 1 + 2 * (x.bars || 1));
    x.restriction = "none";
  }
  const series = [];
  for (let i = 0; i <= 100; i++) {
    const d = addDays(lr.since, i);
    series.push([d, 22000 + i * 15, i % 7 === 3 ? null : 250 + i * 0.4]);
  }
  lr.bench = {
    from: lr.since, to: lr.through,
    taiex_from: 22000, taiex_from_date: lr.since, taiex_to: 23500, taiex_to_date: "2026-10-07", taiex_pct: 6.82,
    otc_from: 250, otc_from_date: lr.since, otc_to: 290, otc_to_date: "2026-10-06", otc_pct: 16.0,
    series,
    windows: { n: 9, trade_sum_pct: -17.2, trade_mean_pct: -1.91,
               taiex_n: 9, taiex_sum_pct: 6.3, taiex_mean_pct: 0.7, taiex_trade_sum_pct: -17.2, taiex_trade_mean_pct: -1.91,
               otc_n: 8, otc_sum_pct: 8.8, otc_mean_pct: 1.1, otc_trade_sum_pct: -12.0, otc_trade_mean_pct: -1.5 },
    note: "index closes, not total return",
  };
  lr.by_sid = {
    "8227": [{ sig: "2026-09-16", bucket: "not_core", rank: 7, ret: 19.41, exit: "tp", bars: 9,
               entry_date: "2026-09-17", exit_date: "2026-09-30", restriction: "none" },
             { sig: "2026-10-07", bucket: "tradable", rank: 2, ret: null, exit: "", bars: 0,
               entry_date: null, exit_date: null, restriction: "disposition" }],
  };
  lr.by_restriction = { none: { closed: 9, open: 2, win_pct: 55.6, mean_pct: -1.91, sum_pct: -17.2 } };
  return p;
}

async function main() {
  // --- A. the OLD payload (09-23 publish, trimmed fixture) ------------------
  {
    const env = await boot(OLD);
    const Y = env.YT;
    let pages = null, err = null;
    try { pages = renderAll(env); } catch (e) { err = e; }
    ok("old: every page renders", !err, err && err.stack);
    if (pages) {
      ok("old: picks page has the three groups",
         pages.picks.includes('data-group="buy"') && pages.picks.includes('data-group="hold"') &&
         pages.picks.includes('details class="grp" data-group="ref"'));
      ok("old: list stamp never claims final", !pages.picks.includes("已定案") && !pages.picks.includes("已凍結"));
      ok("old: list text from scan_time", Y.listStatusText(null).startsWith("產生於 19:33"), Y.listStatusText(null));
      ok("old: asof has no list suffix", !pages.asof.includes("清單"), pages.asof);
      ok("old: perf shows the system record", pages.perf.includes("系統訊號紀錄（非你的實際損益）"));
      const closed = OLD.meta.live_record.tradable.trades.filter((t) => t.ret !== null && t.ret !== undefined).length;
      const m = /<svg class="eq"[^>]*data-n="(\d+)"/.exec(pages.perf);
      ok("old: equity curve over every closed trade (index axis)", m && Number(m[1]) === closed, m && m[1]);
      ok("old: index axis when trades carry no dates", pages.perf.includes("橫軸：交易順序"));
      ok("old: no bench lines without a bench", !pages.perf.includes('class="eq-taiex"'));
      ok("old: research lists the new meta as absent",
         pages.research.includes("建議紀錄（meta.rec）") && pages.research.includes("本次掃描未提供"));
      ok("old: the TSE template text is recognised", Y.aiReportView("TSE").template === true);
      ok("old: the gemini ALL text is not", Y.aiReportView("ALL").template === false);
      ok("old: today chip says 今日可買", pages.today.includes("今日可買 "));
      ok("old: the strategy card keeps the record", pages.today.includes("帳本實際"));
      ok("old: strategy card quotes the strict M.4 slot band, not K.1/L",
         pages.today.includes("BACKTEST_LOG M.4 嚴格格數") && !pages.today.includes("+25% / −26%") &&
         !pages.today.includes("六年半年化約 +18%"), "");
      ok("old: 1815 delay exit guide", Y.exitGuideHtml(Y.listRowFor("1815")).includes("續抱中"), "");
    }
    const g = Y.pickGroups();
    const listed = g.buy.concat(g.hold, g.ref).filter((r) => !r._tracked);
    ok("old: groups cover every list row once",
       listed.length === OLD.rows.length && new Set(sids(listed)).size === OLD.rows.length, listed.length);
    ok("old: tracked rows only join hold", g.ref.every((r) => !r._tracked) && g.buy.every((r) => !r._tracked));
    for (const r of Y.STATE.rows.concat(Y.STATE.tracked)) {
      let html = "", e2 = null;
      try { html = Y.pickCard(r); } catch (e) { e2 = e; }
      if (e2) { ok("old: card " + r.Stock_ID + " renders", false, e2.stack); break; }
    }
    let det = null;
    try { Y.openDetail(Y.STATE.rows[0].Stock_ID); det = env.els["#modal"].innerHTML; } catch (e) { det = "THROW " + e.stack; }
    ok("old: detail renders with the new groups", det.includes("投資人資訊") && det.includes("系統訊號紀錄（同名）"), det.slice(0, 200));
    ok("old: summary line is a funnel", /^今日可買 \d+｜/.test(Y.picksSummaryText()), Y.picksSummaryText());
  }

  // --- B. the LIVE 10-07 payload --------------------------------------------
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    let pages = null, err = null;
    try { pages = renderAll(env); } catch (e) { err = e; }
    ok("live: every page renders", !err, err && err.stack);
    const sum = Y.picksSummaryText();
    ok("live: the 10-07 funnel",
       sum === "今日可買 1｜上櫃前20 5（CORE+ 2，非新訊號 1）｜上市 15（僅參考）", sum);
    const g = Y.pickGroups();
    ok("live: buy group is 8227", JSON.stringify(sids(g.buy)) === '["8227"]', sids(g.buy));
    for (const sid of ["3498", "1569", "1815", "5274"]) {
      ok("live: " + sid + " in the hold group", sids(g.hold).includes(sid), sids(g.hold));
    }
    ok("live: no TSE row outside ref", g.buy.concat(g.hold).every((r) => r.Market !== "TSE"));
    ok("live: ref holds every TSE row", g.ref.filter((r) => r.Market === "TSE").length === LIVE.rows.filter((r) => r.Market === "TSE").length);
    const c8227 = cardOf(env, "8227", "buy");
    ok("live: 8227 stop is the recommendation's 297.50", c8227.includes("297.50") && !c8227.includes("278.00"), "");
    ok("live: 8227 target is the recommendation's 446.50", c8227.includes("446.50"));
    ok("live: 8227 old tp exit is history, not a badge",
       !c8227.includes("達到目標 · 出場") && c8227.includes("上一段模擬"));
    ok("live: 8227 card has investor info, sizing prompt, order guide",
       c8227.includes("投資人資訊") && c8227.includes("尚未設定資金") && c8227.includes('class="order"'));
    ok("live: 8227 volume ratio is shown", /今日量\/20日均量[\s\S]*?0\.10×/.test(c8227));
    const c3498 = cardOf(env, "3498", "hold");
    ok("live: 3498 stale late exit is not a badge", !c3498.includes("後期仍有獲利") && c3498.includes("上一段模擬"));
    ok("live: 3498 shows its recommendation stop", c3498.includes("187.50") && !c3498.includes(">162.00<"));
    const c1815 = cardOf(env, "1815", "hold");
    ok("live: 1815 old rec is not 'enter at the next open'",
       c1815.includes("09-09 建議，進場日已過但沒有模擬進場紀錄") && !c1815.includes("下一個交易日開盤進場") &&
       !c1815.includes("待進場：下一個交易日開盤照規則進場"), "");
    ok("live: 1815 exit guide says the rule will not buy late", c1815.includes("規則不會補買"), "");
    ok("live: 1815 recView flags the missed entry",
       Y.recView(Y.listRowFor("1815"), "hold").missed === true && Y.recView(Y.listRowFor("1815"), "hold").pending === false);
    ok("live: 8227 buy rec is not missed", Y.recView(Y.listRowFor("8227"), "buy").missed === false);
    {
      // MOB-1: a reference row is not the owner's trade; its simulated exit is
      // a muted history line, never the red/orange exit badge
      const withExit = g.ref.filter((r) => r.Exit_Signal);
      ok("live: the reference group has exited rows to judge", withExit.length >= 10, withExit.length);
      const bad = withExit.filter((r) => /<span class="verdict (no|held)">⚠/.test(Y.pickCard(r, "ref")));
      ok("live: no reference card carries an exit badge", bad.length === 0, sids(bad));
      const quiet = withExit.filter((r) => !Y.pickCard(r, "ref").includes('<div class="prev-seg">上一段模擬：'));
      ok("live: reference exits read as history", quiet.length === 0, sids(quiet));
      // the same row in the hold group keeps its badge
      const heldExit = g.hold.filter((r) => r.Exit_Signal && !Y.staleExit(r, "hold"));
      ok("live: hold-group exit badges are unchanged",
         heldExit.every((r) => /<span class="verdict (no|held)">⚠/.test(Y.pickCard(r, "hold"))), sids(heldExit));
    }
    {
      // a pending tracked row without a recommendation and not buyable
      const r = Object.assign({}, Y.listRowFor("1815"), { Recommendation_ID: null, Rec_Status: null });
      const g = Y.exitGuideHtml(r);
      ok("live: a non-buy pending row is told not to buy",
         g.includes("尚未進場，今天不是買點（") && !g.includes("待進場：下一個交易日開盤照規則進場"), g);
    }
    ok("live: uncapped record has no kept-only note", pages && !pages.perf.includes("逐筆只保留最近"));
    ok("live: slot guidance cites M.4", Y.SLOT_GUIDANCE.includes("（BACKTEST_LOG M.4）") &&
       Y.SLOT_GUIDANCE.includes("5～8 個等權格") && !/35\.8|71\.7/.test(Y.SLOT_GUIDANCE));
    const order = Y.pickGroups().hold.map((r) => Y.holdPriority(r));
    ok("live: hold priorities are numbers", order.every((x) => typeof x === "number"));
    ok("live: ref group collapsed by default", pages && !/details class="grp" data-group="ref" open/.test(pages.picks));
    Y.STATE.market = "TSE"; Y.renderPicks();
    ok("live: TSE filter forces ref open", /details class="grp" data-group="ref" open data-forced/.test(env.els["page-picks"].innerHTML));
    Y.STATE.market = "OTC"; Y.renderPicks();
    ok("live: OTC filter shows the OTC template badge", env.els["page-picks"].innerHTML.includes("模板·非 AI"));
    Y.STATE.market = "ALL";
    if (pages) {
      const m = /<svg class="eq"[^>]*data-n="(\d+)"/.exec(pages.perf);
      ok("live: equity data-n = closed tradable trades", m && Number(m[1]) === 9, m && m[1]);
      ok("live: batch exclusion line", pages.perf.includes("起始日（06-25）批次 5 筆") &&
         pages.perf.includes("排除後：4 筆已結束，勝率 50.0%，平均 -0.52%，合計 -2.07%"), "");
      ok("live: per-reason table", pages.perf.includes("<td>停利</td><td>2</td><td>+19.30%</td>") &&
         pages.perf.includes("<td>期滿</td><td>3</td><td>-13.18%</td>") &&
         pages.perf.includes("<td>停損</td><td>1</td><td>-20.47%</td>"), "");
      ok("live: not_core and regime_closed lines", pages.perf.includes("60 筆已結束 · 勝率 63.3%") &&
         pages.perf.includes("9 筆已結束 · 勝率 66.7%"));
    }
    // sizing: no settings -> prompt; with settings -> shares
    env.YT.saveSizing({ capital: 300000, slots: 5, risk_pct: 1 });
    const s = Y.sizingFor(Y.listRowFor("8227"));
    ok("live: 8227 at 300k/5/1% buys 39 shares", s && s.shares === 39 && s.lots === 0 && s.odd === 39, s && s.shares);
    ok("live: sizing persisted to localStorage", typeof env.store.yt_sizing_v1 === "string");
    const c2 = cardOf(env, "8227", "buy");
    ok("live: card prints the suggestion", c2.includes("建議股數 39 股") && c2.includes("0.572%"), "");
    ok("live: odd-lot-only order guide has no board-lot step", c2.includes("ord-odd") && !c2.includes("ord-lot"));
    ok("live: guide carries the M.7 cost and the anchor note",
       c2.includes("+0.2%～+0.3%") && c2.includes("Entry_Open"));
  }

  // --- C. the synthetic NEW payload ---------------------------------------
  {
    const env = await boot(synthetic(LIVE, "final"));
    const Y = env.YT;
    let pages = null, err = null;
    try { pages = renderAll(env); } catch (e) { err = e; }
    ok("new: every page renders", !err, err && err.stack);
    if (pages) {
      ok("new: frozen stamp", pages.picks.includes("本日清單已凍結・15:42 定案"));
      ok("new: asof suffix", pages.asof.endsWith(" · 清單 15:42 已定案"), pages.asof);
      ok("new: no provisional notice", !pages.notices.includes("暫定版"));
      ok("new: perf has date axis and both index lines",
         pages.perf.includes("橫軸：出場日") && pages.perf.includes('class="eq-taiex"') && pages.perf.includes('class="eq-otc"'));
      ok("new: bench compares like with like",
         pages.perf.includes("櫃買：同樣 8 筆的持有期間，系統平均 -1.50%、指數平均 +1.10%") &&
         pages.perf.includes("加權：同樣 9 筆的持有期間，系統平均 -1.91%、指數平均 +0.70%"), "");
      ok("new: each index labelled with its own end date",
         pages.perf.includes("加權指數 +6.82%（06-25→10-07）") && pages.perf.includes("櫃買指數 +16.00%（06-25→10-06）"), "");
      ok("new: research shows meta.rec / events / AI sources",
         pages.research.includes("新建 1、附上 3、結案 1") && pages.research.includes("最新營收月份 2026-09") &&
         pages.research.includes("OTC：模板（非 AI）"), "");
    }
    const c = cardOf(env, "8227", "buy");
    ok("new: disposition stays buyable", c.includes('<span class="verdict ok">可買</span>') && !c.includes("不可買"));
    ok("new: parsed match minutes and prepay rendered",
       c.includes("約每 2 分鐘撮合一次") && c.includes("每筆委託都需全額預收款券"), "");
    ok("new: disposition note cites M.1, not a warning against buying", c.includes("BACKTEST_LOG M.1"));
    ok("new: Prev_* segment rendered",
       c.includes("上一段模擬：09-16 訊號 → 09-17 開盤 273.00 進場 → 09-29 停利 @327.60，淨 +19.41%（距今 6 個交易日）"), "");
    ok("new: revenue month labelled, uncoloured", c.includes("2026 年 9 月營收 年增 +35.2%") && !/class="[^"]*pos[^"]*">[^<]*年增/.test(c));
    ok("new: ex-dividend badge within the week", c.includes("除息 10-12"));
    ok("new: rec valid-until label", c.includes("有效・10-08 開盤進場"));
    ok("new: history from by_sid labels the bucket", c.includes("09-16 訊號 → 停利 +19.41%（9 日）（非核心，未計入）"), "");
    const tse = Y.STATE.rows.filter((r) => r.Market === "TSE");
    const cs = Y.pickCard(tse[2], "ref");
    ok("new: suspended uses the blocking sentence", cs.includes("暫停交易：屬規則的封鎖類別，規則不買") && cs.includes("交易受限·無法下單"), "");
    ok("new: attention is info, not a block", Y.pickCard(tse[0], "ref").includes('<span class="tag info">注意股</span>'));
    ok("new: limit_down flag is shown", Y.pickCard(tse[3], "ref").includes('<span class="tag info">跌停</span>'));
    const c1815 = cardOf(env, "1815", "hold");
    ok("new: closed rec shows its reason", c1815.includes("已結案・停損"), "");
    ok("new: summary unchanged by restrictions except the suffix",
       Y.picksSummaryText().startsWith("今日可買 1｜上櫃前20 5（CORE+ 2，非新訊號 1）｜上市 15（僅參考）｜交易受限 1"), Y.picksSummaryText());
    ok("new: OTC template from report_sources", Y.aiReportView("OTC").template === true && Y.aiReportView("ALL").template === false);
    let det = "";
    try { Y.openDetail("8227"); det = env.els["#modal"].innerHTML; } catch (e) { det = "THROW " + e.stack; }
    ok("new: detail has restriction, events and history",
       det.includes("交易限制") && det.includes("營收／除權息／法說") && det.includes("09-16 訊號"), det.slice(0, 200));
  }

  // --- D. provisional / revised list states, regime off, storage blocked ---
  {
    const env = await boot(synthetic(LIVE, "provisional"), { storageThrows: true });
    const Y = env.YT;
    let pages = null, err = null;
    try { pages = renderAll(env); } catch (e) { err = e; }
    ok("prov: renders with storage blocked", !err, err && err.stack);
    if (pages) {
      ok("prov: provisional notice", pages.notices.includes("暫定版") && pages.notices.includes("資料尚未更新到今天、欄位自檢未通過"), pages.notices);
      ok("prov: asof suffix", pages.asof.endsWith(" · 暫定版"), pages.asof);
    }
    {
      // MOB-2: every fault at once; the worst notices come first
      const p2 = synthetic(LIVE, "provisional");
      p2.meta.degraded = "feed degraded: OTC snapshot failed";
      p2.meta.checks = { status: "fail", errors: 1, warnings: 0, items: [] };
      p2.meta.quality = Object.assign({}, p2.meta.quality, { restrictions: Object.assign({}, (p2.meta.quality || {}).restrictions, { ok: false }) });
      const env2 = await boot(p2, { storageThrows: true });
      renderAll(env2);
      const html = env2.els["#notices"].innerHTML;
      const tones = [...html.matchAll(/class="notice (err|warn|info)"/g)].map((m) => m[1]);
      const rank = { err: 0, warn: 1, info: 2 };
      ok("prov: several notices at once", tones.length >= 4, tones);
      ok("prov: notices are ordered errors, warnings, info",
         tones.every((t, i) => i === 0 || rank[tones[i - 1]] <= rank[t]), tones);
      ok("prov: the self-check failure is above the provisional warning",
         html.indexOf("欄位自我檢測未通過") >= 0 && html.indexOf("欄位自我檢測未通過") < html.indexOf("暫定版"), html.slice(0, 300));
    }
    ok("prov: sizing load survives a throwing storage", Y.loadSizing() === null);
    ok("prov: save reports it could not persist", Y.saveSizing({ capital: 100000, slots: 8, risk_pct: 1 }) === false);
    ok("prov: in-memory copy still used", Y.loadSizing() && Y.loadSizing().slots === 8);
    const rev = Y.listStatusText({ state: "final", revision: 2, revised_reason: "restriction_info",
      revised_at: "2026-10-07 23:40:00", first_published_at: "2026-10-07 15:42:10", published_at: "2026-10-07 23:40:00" });
    ok("prov: revised final text", rev.includes("第 2 版") && rev.includes("只改限制欄位") && rev.includes("首次發布 15:42"), rev);
  }
  {
    const p = synthetic(LIVE, "final");
    p.meta.regime = Object.assign({}, p.meta.regime, { enter_ok: false, strong: false });
    const env = await boot(p);
    const s = env.YT.picksSummaryText();
    ok("regime off: summary shows the regime text", s.startsWith("今日可買 0｜大盤中性"), s);
  }

  {
    // provisional time exit: the day-10 close is past the regime's as-of date
    const p = synthetic(LIVE, "final");
    p.meta.regime = Object.assign({}, p.meta.regime, { as_of_date: "2026-10-06" });
    const r5274 = p.tracked.find((r) => r.Stock_ID === "5274");
    Object.assign(r5274, { Hold_Status: "exit_today", Exit_Signal: "time", Exit_Signal_Date: "2026-10-07",
                           Exit_Signal_Price: 18275, Rec_Status: "active" });
    // report_sources as bare strings; by_sid as a flat list; a capped trade list
    p.meta.report_sources = { ALL: "gemini", OTC: "template" };
    const lr = p.meta.live_record;
    lr.by_sid = [
      { sid: "8227", sig: "2026-10-07", bucket: "tradable", rank: 2, ret: null, exit: "", bars: 0 },
      { sid: "8227", sig: "2026-09-16", bucket: "not_core", rank: 7, ret: 19.41, exit: "tp", bars: 9 },
      { sid: "3498", sig: "2026-09-02", bucket: "tradable", rank: 4, ret: 3.1, exit: "late", bars: 9 },
    ];
    const kept = lr.tradable.trades.length;
    lr.tradable.closed = (Number(lr.tradable.closed) || 0) + 30;
    const env = await boot(p);
    const Y = env.YT;
    let pages = null, err = null;
    try { pages = renderAll(env); } catch (e) { err = e; }
    ok("edge: every page renders", !err, err && err.stack);
    const row = Y.listRowFor("5274");
    ok("edge: provisional time exit detected", Y.provisionalTimeExit(row) === true);
    const c = cardOf(env, "5274", "hold");
    ok("edge: provisional label on the rec line", c.includes("期滿出場待大盤資料確認"), "");
    ok("edge: exit guide explains the provisional time exit",
       c.includes("這筆期滿出場是暫定的：大盤資料只到 10-06") && c.includes("下一次掃描會確認"), "");
    ok("edge: bare-string report source marks the template", Y.aiReportView("OTC").template === true &&
       Y.aiReportView("OTC").source === "template" && Y.aiReportView("ALL").template === false);
    ok("edge: research lists bare-string sources", Y.reportSourcesText(p.meta.report_sources) === "ALL：Gemini｜OTC：模板（非 AI）",
       Y.reportSourcesText(p.meta.report_sources));
    const h = Y.nameHistory("8227");
    ok("edge: flat by_sid read per name, oldest first",
       h.length === 2 && h[0].sig === "2026-09-16" && Y.nameHistory("1569").length === 0, JSON.stringify(h));
    if (pages) {
      ok("edge: capped list says the curve covers the kept trades only",
         pages.perf.includes(`逐筆只保留最近 ${kept} 筆（全部 `) && pages.perf.includes(`data-capped="${kept}"`) &&
         pages.perf.includes("只含保留的最近") && pages.perf.includes(`出場原因（最近 ${kept} 筆）`), "");
      ok("edge: batch exclusion labelled as within the kept trades",
         pages.perf.includes(`排除後（保留的 ${kept} 筆內）`), "");
      ok("edge: the trade list says only the latest", pages.perf.includes("（只列最近的）"));
    }
  }

  // --- E. pure pieces: text parity, fees, sizing, validation ---------------
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    for (const [k, v] of Object.entries(REPORT_TEXT.restriction_note)) {
      ok("RESTRICT_NOTE." + k + " matches config/report_text.json", Y.RESTRICT_NOTE[k] === v, Y.RESTRICT_NOTE[k]);
    }
    for (const [k, v] of Object.entries(REPORT_TEXT.restriction_label)) {
      if (k === "none") continue;
      ok("RESTRICT_TEXT." + k + " matches restriction_label", Y.RESTRICT_TEXT[k] === v, Y.RESTRICT_TEXT[k]);
    }
    const sched = Y.schedule("tw-equity-v1");
    ok("fee pin 20.35 x 1000 -> 28", Y.feeFor(sched, 2035000) === 2800, Y.feeFor(sched, 2035000));
    ok("tax pin 10.10 x 99 -> 2", Y.taxFor(sched, 99990) === 200, Y.taxFor(sched, 99990));
    ok("min fee binds at 14,035", Y.feeBindsMin(sched, 1403500) === true);
    ok("min fee does not bind at 14,036", Y.feeBindsMin(sched, 1403600) === false);
    ok("min fee floor", Y.feeFor(sched, 100000) === 2000);
    ok("exact schedule has no floor", Y.feeFor(Y.schedule("tw-equity-exact"), 100000) === 143);
    const a = Y.sizePosition(37200, 29750, { capital: 300000, slots: 5, risk_pct: 1 }, sched, {});
    ok("size 300k/5/1%", a.shares === 39 && a.budgetShares === 161 && a.riskShares === 39 && a.limit === "risk",
       JSON.stringify(a));
    ok("size 300k max loss 2,979.50", a.maxLossCents === 297950, a.maxLossCents);
    ok("size 300k round trip 0.572%", a.rtPct === 0.572, a.rtPct);
    ok("size 300k: NT$14,508 is past the min-fee point", a.minFeeBinds === false);
    const b = Y.sizePosition(37200, 29750, { capital: 100000, slots: 5, risk_pct: 1 }, sched, {});
    ok("size 100k/5/1%", b.shares === 12 && b.rtPct === 1.187 && b.minFeeBinds === true, JSON.stringify(b));
    const c = Y.sizePosition(37200, 29750, { capital: 1000000, slots: 5, risk_pct: 1 }, sched, {});
    ok("size 1M/5/1%", c.shares === 131 && c.maxLossCents === 999950, JSON.stringify(c));
    const d = Y.sizePosition(1827500, 1462000, { capital: 300000, slots: 5, risk_pct: 1 }, sched, {});
    ok("size 5274 risk-limited to zero", d.shares === 0 && d.limit === "risk" && d.budgetShares === 3, JSON.stringify(d));
    const e = Y.sizePosition(37200, 37200, { capital: 300000, slots: 5, risk_pct: 1 }, sched, {});
    ok("size refuses a stop at the price", e.shares === 0 && e.limit === "stop");
    const f = Y.sizePosition(37200, 29750, { capital: 300000, slots: 8, risk_pct: 1 }, sched, { openCount: 8 });
    ok("size reports full slots", f.reason === "slots" && f.slotsFree === 0);
    const h = Y.sizePosition(37200, 29750, { capital: 300000, slots: 8, risk_pct: 1 }, sched, { held: true });
    ok("size reports a held name", h.reason === "held");
    // the loss bound really is the largest n
    const lossAt = (n) => n * (37200 - 29750) + Y.feeFor(sched, n * 37200) + Y.feeFor(sched, n * 29750) + Y.taxFor(sched, n * 29750);
    ok("size is maximal", lossAt(39) <= 300000 && lossAt(40) > 300000);
    const bad = [
      [{ capital: "abc", slots: "8", risk_pct: "1" }, "capital"],
      [{ capital: "5000", slots: "8", risk_pct: "1" }, "capital"],
      [{ capital: "300000.5", slots: "8", risk_pct: "1" }, "capital"],
      [{ capital: "300000", slots: "0", risk_pct: "1" }, "slots"],
      [{ capital: "300000", slots: "21", risk_pct: "1" }, "slots"],
      [{ capital: "300000", slots: "8", risk_pct: "6" }, "risk_pct"],
      [{ capital: "300000", slots: "8", risk_pct: "1.234" }, "risk_pct"],
      [{ capital: "300000", slots: "8", risk_pct: "0.05" }, "risk_pct"],
    ];
    for (const [input, key] of bad) {
      const v = Y.validateSizing(input);
      ok("validate refuses " + JSON.stringify(input), !v.ok && v.errs[key], JSON.stringify(v));
    }
    const good = Y.validateSizing({ capital: "300,000", slots: "8", risk_pct: "1.25" });
    ok("validate accepts a good input", good.ok && good.value.capital === 300000 && good.value.risk_pct === 1.25, JSON.stringify(good));
    ok("default slots is 8", /SIZING_DEFAULTS = \{ slots: 8,/.test(APP_SRC));
    const meta = Y.sizingToMeta({ capital: 300000, slots: 8, risk_pct: 1.25 });
    ok("sizing meta row shape", meta.capital_cents === 30000000 && meta.max_slots === 8 && meta.risk_pct === 1.25,
       JSON.stringify(meta));
    const back = Y.sizingFromMeta(meta);
    ok("sizing meta round-trips", back && back.capital === 300000 && back.slots === 8 && back.risk_pct === 1.25,
       JSON.stringify(back));
    for (const bad of [null, "x", { capital_cents: 30000050, max_slots: 8, risk_pct: 1 },
                       { capital_cents: 30000000, max_slots: 0, risk_pct: 1 },
                       { capital_cents: 30000000, max_slots: 8, risk_pct: 10 },
                       { capital_cents: -100, max_slots: 8, risk_pct: 1 }, { max_slots: 8, risk_pct: 1 }]) {
      ok("sizing meta refuses " + JSON.stringify(bad), Y.sizingFromMeta(bad) === null);
    }
    ok("no hardcoded disposition minutes", !/\d+\s*分鐘撮合/.test(JSON.stringify(Y.ORDER_RULES)) &&
       Y.RESTRICT_NOTE.disposition.includes("{match}"));
    for (const code of ["", "regime", "regime_stale", "held", "unknown", "quality", "market", "rank",
                        "integrity", "stale", "no_rule", "dropped", "restricted"]) {
      if (code) ok("BLOCK_TEXT." + code, typeof Y.BLOCK_TEXT[code] === "string" && Y.BLOCK_TEXT[code].length > 0);
    }
  }

  // --- F. fee/tax parity grid against portfolio/money.py ------------------
  if (FEE_GRID) {
    const env = await boot(LIVE);
    const Y = env.YT;
    const cases = JSON.parse(fs.readFileSync(FEE_GRID, "utf8"));
    let bad = 0;
    for (const [name, cons, fee, tax] of cases) {
      const s = Y.schedule(name);
      if (s.version !== name) { bad++; ok("fee grid: unknown schedule " + name, false); continue; }
      const f = Y.feeFor(s, cons), t = Y.taxFor(s, cons);
      if (f !== fee || t !== tax) {
        bad++;
        if (bad <= 10) ok("fee grid " + name + " " + cons, false, "fee " + f + " want " + fee + ", tax " + t + " want " + tax);
      }
    }
    gridCases = cases.length;
    ok("fee grid: " + cases.length + " cases, 0 mismatches", cases.length > 0 && bad === 0, bad);
  }

  // --- G. the ride past the time exit, with the MARKET leg (audit M-04) ------
  // scanner.exit_rules.replay_exit keeps a position past its time exit while
  // the close is above its own 5-bar mean OR the market leg is on for that
  // session's date, bar by bar, until the cap; the first close that is neither
  // ends it. meta.market_leg = {date: bool} carries the leg; a date it does not
  // carry is unknown, and a payload without the map behaves as before.
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    const dayStr = (k) => addDays("2026-09-01", k);
    const mkPos = (over) => Object.assign({
      position_id: "pz", stock_id: "2330", stock_name: "測試股", market: "TSE", status: "open",
      open_shares: 1000, avg_cost: 10000, first_buy_price: 10000, cost_basis: 10014250,
      realized_net: 0, opened_session: dayStr(0), cycle_buys: 1, horizon_days: 10, cap_days: 20,
    }, over || {});
    const mkMark = (k, close) => ({
      position_id: "pz", session_date: dayStr(k), day_index: k + 1, close_price: close,
      data_status: close === null ? "missing" : "current", price_source: "quote",
      total_book: 0, total_gross: 0, day_pnl_gross: 0, net_if_liquidated: 0,
    });
    const scene = (closes, leg, posOver) => {
      const pos = mkPos(posOver);
      Y.STATE.positions = [pos];
      Y.STATE.marksByPos = { pz: closes.map((c, k) => mkMark(k, c)) };
      if (leg === undefined) delete Y.STATE.meta.market_leg; else Y.STATE.meta.market_leg = leg;
      const plan = Y.activePlan(pos);
      const n = closes.length;
      return { pos, plan, rv: Y.rideView(plan, n, 10), card: Y.positionCard(pos), pend: Y.pendingItems() };
    };
    // days 1-9 climb, day 10 closes UNDER its own 5-bar mean (107.20)
    const up9 = [10100, 10200, 10300, 10400, 10500, 10600, 10700, 10800, 10900];
    const d10 = up9.concat([10600]);
    const dateOf = (k) => dayStr(k - 1);               // 1-based day -> date

    let g = scene(d10, { [dateOf(10)]: true });
    ok("G1 disturbed day: still riding", g.plan.riding === true && g.plan.ride_via === "market" && g.plan.own_leg === false, JSON.stringify(g.plan));
    ok("G1 disturbed day: the sentence says the market keeps it",
       g.rv.state === "market" && g.rv.time.includes("大盤正在回檔") && !g.rv.time.includes("於收盤出場"), g.rv.time);
    ok("G1 disturbed day: card chip is 規則續抱, not 待登錄賣出",
       g.card.includes("規則續抱") && !g.card.includes("待登錄賣出"), g.card.slice(0, 300));
    ok("G1 disturbed day: to-do does not ask for a sell",
       g.pend.some((x) => x.kind === "d10" && x.text.includes("規則續抱中") && !x.text.includes("尚未登錄賣出")),
       JSON.stringify(g.pend.map((x) => x.text)));

    g = scene(d10, { [dateOf(10)]: false });
    ok("G2 undisturbed day: the same bar exits", g.plan.riding === false && g.rv.state === "exit", JSON.stringify(g.rv));
    ok("G2 undisturbed day: sentence says exit at the close", g.rv.time.includes("規則於收盤出場"), g.rv.time);
    ok("G2 undisturbed day: card chip asks for the sell", g.card.includes("待登錄賣出"));
    ok("G2 undisturbed day: to-do asks for the sell",
       g.pend.some((x) => x.kind === "d10" && x.text.includes("尚未登錄賣出")));

    g = scene(d10, undefined);
    ok("G3 no map: behaves as before (not riding, sell asked for)",
       g.plan.riding === false && g.card.includes("待登錄賣出") && g.plan.market_published === false, JSON.stringify(g.rv));
    ok("G3 no map: says so and does not claim an exit it cannot know",
       g.rv.state === "pending" && g.rv.time.includes("這份資料沒有大盤判定") && !g.rv.time.includes("規則於收盤出場"), g.rv.time);

    g = scene(d10, { [dayStr(0)]: false });
    ok("G4 map without that date: pending, worded as 'not yet'",
       g.rv.state === "pending" && g.rv.time.includes("這天的大盤判定還沒有") && g.plan.riding === false, g.rv.time);
    ok("G4 a non-object map is ignored", (() => {
      const q = scene(d10, [true, false]);
      return q.plan.market_published === false && q.plan.riding === false;
    })());

    // the chain: a close that is neither above its mean nor on a disturbed day
    // ENDS the ride there, whatever later closes do (replay_exit returns on it)
    g = scene(d10.concat([11200]), { [dateOf(10)]: false, [dateOf(11)]: false });
    ok("G5 a broken ride stays broken after a rebound",
       g.plan.riding === false && g.plan.ride_broken_on === dateOf(10) && g.rv.state === "broken", JSON.stringify(g.plan));
    ok("G5 the broken sentence names the day it ended", g.rv.time.includes(dateOf(10).slice(5)), g.rv.time);
    g = scene(d10.concat([11200]), { [dateOf(10)]: true, [dateOf(11)]: false });
    ok("G6 kept by the market on 10, own mean on 11", g.plan.riding === true && g.plan.ride_via === "own" && !g.plan.ride_broken_on, JSON.stringify(g.plan));
    g = scene(d10.concat([10000]), { [dateOf(10)]: true, [dateOf(11)]: false });
    ok("G7 kept on 10, neither leg on 11: exits on 11, not 'broken earlier'",
       g.plan.riding === false && g.plan.ride_broken_on === "" && g.rv.state === "exit", JSON.stringify(g.rv));
    g = scene(d10.concat([10000]), { [dateOf(10)]: true, [dateOf(11)]: true });
    ok("G8 kept by the market two days running", g.plan.riding === true && g.plan.ride_via === "market", JSON.stringify(g.plan));
    g = scene(up9.concat([11000]), { [dateOf(10)]: false });
    ok("G9 own mean alone keeps it with the leg off", g.plan.riding === true && g.plan.ride_via === "own", JSON.stringify(g.plan));
    // an unpriced day-10 close decides nothing, even on a disturbed date
    g = scene(up9.concat([null]), { [dateOf(10)]: true });
    ok("G10 unpriced close is not a ride", g.plan.riding === false && g.rv.state === "nodata", JSON.stringify(g.rv));
    // the cap: bar 20 is the last one
    const climb = []; for (let i = 0; i < 20; i++) climb.push(10100 + i * 50);
    g = scene(climb, {});
    ok("G11 the cap day closes it", g.plan.cap_hit === true && g.plan.riding === false && g.rv.state === "cap" &&
       g.rv.time.includes("最晚持有日"), JSON.stringify(g.rv));
    g = scene(climb.slice(0, 19), {});
    ok("G11 day 19 is still inside the cap", g.plan.riding === true && g.plan.cap_hit === false);
    // before the time exit
    g = scene(up9, { [dateOf(9)]: false });
    ok("G12 day 9: not past the plan yet", g.rv.state === "before" && g.rv.time.includes("大盤正在回檔"), g.rv.time);
    ok("G12 day 9: the card is an ordinary holding", g.card.includes("持有中 · D9 / 10"));
    // fewer than five marks: no own mean, but the market leg still counts
    g = scene([10100, 10200, 10300], { [dateOf(3)]: true }, { horizon_days: 3 });
    ok("G13 no 5-bar mean yet: the market leg alone still keeps it",
       g.plan.ma5 === null && g.plan.riding === true && g.plan.ride_via === "market", JSON.stringify(g.plan));
    // exact mean: 5 x close against the sum, never a rounded cent
    {
      const pos = mkPos({ first_buy_price: 800, avg_cost: 800 });
      Y.STATE.positions = [pos];
      Y.STATE.marksByPos = { pz: [800, 801, 800, 800, 801, 800, 800, 800, 800, 801, 800, 801].map((c, k) => mkMark(k, c)) };
      delete Y.STATE.meta.market_leg;
      const p = Y.activePlan(pos);
      // last five: 800 800 801 800 801 -> sum 4002, mean 800.4; close 801 > 800.4
      ok("G14 exact mean 8.004 printed whole", p.ma5_text === "8.004" && p.own_leg === true, p.ma5_text + " " + p.own_leg);
    }
    ok("G15 marketLegFor is tri-state", (() => {
      Y.STATE.meta.market_leg = { "2026-09-10": true, "2026-09-11": false };
      const a = Y.marketLegFor("2026-09-10"), b = Y.marketLegFor("2026-09-11"), c = Y.marketLegFor("2026-09-12");
      delete Y.STATE.meta.market_leg;
      return a === true && b === false && c === null && Y.marketLegFor("2026-09-10") === null;
    })());
    // the five other places that used to say "above its own mean" alone
    const texts = [Y.exitGuideHtml({ Stock_ID: "1815", Hold_Status: "delay", Exit_Signal: "", Exit_Signal_Date: "" }), (() => { Y.STATE.marksByPos = { pz: [] }; return Y.tomorrowOrders(mkPos(), Y.activePlan(mkPos()), 9, 10); })()];
    ok("G16 delay/exit texts mention the market leg", texts.every((t) => t.includes("大盤")), texts.map((t) => t.length));
    ok("G16 the strategy card and glossary say the market leg too",
       renderAll(env).today.includes("當天大盤正在回檔") && APP_SRC.includes("第 10 天起每天收盤：站上自己的 5 日均價，或當天大盤正在回檔"));
  }

  // --- H. plan levels: one rounding, on the ladder (audit M-02) -------------
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    const pos = (base, sid) => ({ position_id: "ph", stock_id: sid || "2330", open_shares: 1000, avg_cost: base,
                                  first_buy_price: base, cycle_buys: 1 });
    const plan = (base, sid, closes) => {
      Y.STATE.marksByPos = { ph: (closes || []).map((c, k) => ({ session_date: addDays("2026-09-01", k), close_price: c, day_index: k + 1 })) };
      return Y.activePlan(pos(base, sid));
    };
    // the verifier's pins: entry 10.05 arms at 10.35 (10.30125 rounded UP to the tick), not 10.30
    let p = plan(1005, "6488", [1030]);
    ok("H1 entry 10.05: arm level is 10.35", p.arm === 1035, p.arm);
    ok("H1 entry 10.05: a 10.30 close does not arm", p.armed === false && p.stop === p.initial_stop, JSON.stringify(p));
    p = plan(1005, "6488", [1035]);
    ok("H1 entry 10.05: a 10.35 close arms", p.armed === true && p.stop === p.lock, JSON.stringify(p));
    p = plan(121, "6488", [124]);
    ok("H2 entry 1.21: a 1.24 close does not arm (needs 1.24025)", p.armed === false, JSON.stringify(p));
    p = plan(121, "6488", [125]);
    ok("H2 entry 1.21: a 1.25 close arms", p.armed === true);
    // exact-ratio direction: down levels never above, up levels never below the exact value
    for (const [base, sid] of [[1005, "6488"], [3337, "6488"], [18425, "6488"], [10525, "0050"], [4333, "00878"], [15650, "00679B"]]) {
      p = plan(base, sid);
      const ex = (m) => base * m / 1000;
      ok("H3 " + sid + " " + base + ": down levels <= exact, up levels >= exact",
         p.initial_stop <= ex(800) && p.lock <= ex(1020) && p.add <= ex(900) &&
         p.arm >= ex(1025) && p.target >= ex(1200) && p.scale_out >= ex(1150) && p.late >= ex(1010), JSON.stringify(p));
      ok("H3 " + sid + " " + base + ": each level is within one tick of the exact value",
         p.initial_stop > ex(800) - Y.tickSize(p.initial_stop, sid) && p.target < ex(1200) + Y.tickSize(p.target, sid), JSON.stringify(p));
    }
    // sizing's default stop uses the same single rounding
    ok("H4 sizingStop snaps the exact -20% once",
       Y.sizingStop({ Stock_ID: "6488", Hold_Status: "" }, 1005) === 804 &&
       Y.sizingStop({ Stock_ID: "6488", Hold_Status: "" }, 10525) === 8420 && // 84.20 exactly
       Y.sizingStop({ Stock_ID: "0050", Hold_Status: "" }, 10525) === 8420);
  }

  // --- I. tomorrow's orders: what is 必守 and what the stop row says (M-03) --
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    const pos = { position_id: "pi", stock_id: "2330", open_shares: 1000, avg_cost: 10000, first_buy_price: 10000, cycle_buys: 1 };
    Y.STATE.marksByPos = { pi: [] };
    const plan = Y.activePlan(pos);
    const html = Y.tomorrowOrders(pos, plan, 9, 10);
    const rows = [...html.matchAll(/<tr class="[^"]*">\s*<td><b>([^<]*)<\/b><\/td>\s*<td>([^<]*)<br><span class="hint">([^<]*)<\/span><\/td>\s*<td>(必守|選用)<\/td><\/tr>/g)]
      .map((m) => ({ price: m[1], label: m[2], note: m[3], flag: m[4] }));
    const by = (frag) => rows.find((r) => r.label.includes(frag));
    ok("I1 the table has the six rungs on day 9", rows.length === 6, rows.map((r) => r.label));
    ok("I2 take-profit, the late take and the stop are 必守",
       by("全部停利").flag === "必守" && by("收盤在這之上").flag === "必守" && by("跌破全部出場").flag === "必守");
    ok("I3 sell-half, the add and the arm hint are 選用",
       by("可賣一半").flag === "選用" && by("可加碼").flag === "選用" && by("隔日起停損上調").flag === "選用");
    const stopNote = by("跌破全部出場").note;
    ok("I4 the stop note no longer claims to be the only must-keep",
       !/唯一|只有.*必守|只有這/.test(stopNote) && stopNote.includes("停損") && stopNote.includes("停利") &&
       stopNote.includes("後期收利") && stopNote.includes("只有加碼與賣一半是選用"), stopNote);
    const must = rows.filter((r) => r.flag === "必守").map((r) => r.label);
    ok("I5 every 必守 row is a rule exit the note names", must.length === 3, must);
    ok("I6 the late rung appears on day 8, not on day 7",
       Y.tomorrowOrders(pos, plan, 8, 10).includes("收盤在這之上") && !Y.tomorrowOrders(pos, plan, 7, 10).includes("收盤在這之上"));
    ok("I7 prices in the table are the plan's own ladder prices",
       by("全部停利").price === "120.00" && by("跌破全部出場").price === "80.00" && by("可賣一半").price === "115.00" &&
       by("可加碼").price === "90.00" && by("收盤在這之上").price === "101.00", JSON.stringify(rows.map((r) => r.price)));
  }

  // --- J. the optional sell-half price of an ENTERED row is the fill's (M-47) --
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    const base = JSON.parse(JSON.stringify(Y.listRowFor("2305")));
    const kvOf = (html) => { const m = /<div class="kv"><span class="kv-l">可賣一半（選用）<\/span>[\s\S]*?<\/div>/.exec(html); return m ? m[0] : ""; };
    const entered = Object.assign({}, base, { Hold_Status: "holding", Entry_Open: 59.7,
                                              Scale_Out_Price: 67.8, Fill_Scale_Out_Price: 68.7 });
    const card = Y.pickCard(entered, "hold");
    const seg = kvOf(card);
    ok("J1 an entered row shows the fill-based sell-half price", seg.includes("68.70") && !seg.includes("67.80"), seg.slice(0, 300));
    ok("J1 and says what it is anchored to", seg.includes("推估成交價 59.70 +15%"), seg.slice(0, 300));
    const noFill = Object.assign({}, entered, { Fill_Scale_Out_Price: null });
    ok("J2 an entered row without the fill-based value prints no sell-half price",
       !Y.pickCard(noFill, "hold").includes("可賣一半"), "");
    const pending = Object.assign({}, base, { Hold_Status: "pending", Entry_Open: null, Fill_Scale_Out_Price: null,
                                              Scale_Out_Price: 67.8, Recommendation_ID: null, Rec_Status: null });
    const segP = kvOf(Y.pickCard(pending, "hold"));
    ok("J3 before the fill the close-based reference is shown, labelled as a reference",
       segP.includes("67.80") && segP.includes("參考價") && segP.includes("成交後改以成交價 +15% 為準"), segP.slice(0, 300));
    ok("J4 helper: hidden means nothing", Y.scaleOutKv(entered, true, true) === "");
  }

  // --- K. a typed stock id (D7-04) ---------------------------------------
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    const cases = [["00679b", "00679B"], [" 2330 ", "2330"], ["00878", "00878"], ["6533", "6533"], ["00400a", "00400A"],
                   ["00663L", "00663L"], ["\t3088\n", "3088"],
                   ["２３３０", ""], ["233", ""], ["2330 5", ""], ["abc-12", ""], ["", ""], [null, ""], [undefined, ""],
                   ["00679BX9", ""], ["23３0", ""]];
    for (const [raw, want] of cases) {
      ok("K1 normStockId(" + JSON.stringify(raw) + ")", Y.normStockId(raw) === want, Y.normStockId(raw));
    }
    Y.STATE.query = "00679b"; Y.STATE.market = "ALL";
    const hit = Y.viewRows([{ Stock_ID: "00679B", Stock_Name: "元大美債20年", Market: "TSE" },
                            { Stock_ID: "2330", Stock_Name: "台積電", Market: "TSE" }]);
    ok("K2 searching a lower-case ETF code finds it", hit.length === 1 && hit[0].Stock_ID === "00679B", hit.map((r) => r.Stock_ID));
    Y.STATE.query = "tsmc";
    ok("K2 name search is still case-insensitive", Y.viewRows([{ Stock_ID: "2330", Stock_Name: "TSMC", Market: "TSE" }]).length === 1);
    Y.STATE.query = "";
    Y.STATE.meta.live_record = Object.assign({}, Y.STATE.meta.live_record, {
      by_sid: [{ sid: "00679B", sig: "2026-09-02", bucket: "tradable", rank: 1, ret: 3.1, exit: "time", bars: 10 }] });
    ok("K3 per-name history finds a lower-case id", Y.nameHistory("00679b").length === 1 && Y.nameHistory(" 00679B ").length === 1);
    ok("K4 the entry form no longer hints numeric-only",
       !/field\("stock_id", "股票代號", "", \{ inputmode: "numeric"/.test(APP_SRC) && APP_SRC.includes("00679B（英文字母會自動轉大寫）"));
    ok("K5 the save path stores the normalised id",
       APP_SRC.includes("normStockId(v.stock_id)") && !/String\(v\.stock_id \|\| ""\)\.trim\(\)/.test(APP_SRC));
  }

  // --- L. regime words, the downgrade-only verdict, the hold clock ----------
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    const base = Y.STATE.meta.regime;
    const states = {
      red: { ok: true, risk_on: false, enter_ok: false, strong: false, is_current: true, as_of_date: "2026-10-07" },
      mid: { ok: true, risk_on: true, enter_ok: false, strong: false, is_current: true, as_of_date: "2026-10-07" },
      weak: { ok: true, risk_on: true, enter_ok: true, strong: false, str20: 0.01, is_current: true, as_of_date: "2026-10-07" },
      strong: { ok: true, risk_on: true, enter_ok: true, strong: true, str20: 0.04, is_current: true, as_of_date: "2026-10-07" },
      stale: { ok: true, risk_on: false, enter_ok: false, strong: false, is_current: false, as_of_date: "2026-10-01" },
      off: { ok: false },
    };
    for (const [k, reg] of Object.entries(states)) {
      Y.STATE.meta.regime = reg;
      const t = Y.regimeView().text;
      ok("L1 regime '" + k + "' never says to cut what is held", !/減碼|減倉|降部位|部位減量|減量/.test(t), t);
      if (k === "red" || k === "mid") {
        ok("L1 regime '" + k + "' says new entries pause and holdings keep their exit rules",
           t.includes("暫停開新倉") && t.includes("已持有的依各自出場規則"), t);
      }
    }
    // M-41: the verdict only ever downgrades the backend's Buy_Ready
    const NOW = "2099-01-01", OLDD = "2000-01-01";
    let violations = 0, combos = 0, okCount = 0;
    for (const [rk, reg] of Object.entries(states)) {
      Y.STATE.meta.regime = reg;
      const regOk = Y.regimeView().enterOk;
      for (const ready of [true, false, undefined, null, "true", 1, 0, "yes"]) {
        for (const block of ["", "market", "regime", undefined, "nonsense"]) {
          for (const bar of [NOW, OLDD, ""]) {
            for (const until of ["", NOW, OLDD]) {
              const row = { Stock_ID: "2330", Data_Date: bar, Rec_Valid_Until: until };
              if (ready !== undefined) row.Buy_Ready = ready;
              if (block !== undefined) row.Buy_Block = block;
              const v = Y.buyVerdict(row);
              combos++;
              if (v.ok) okCount++;
              const allowed = ready === true && regOk && bar !== OLDD && until !== OLDD;
              if (v.ok !== allowed) violations++;
              if (ready !== true && v.ok) violations++;       // a refusal never becomes a buy
            }
          }
        }
      }
    }
    ok("L2 buyVerdict is exactly 'Buy_Ready === true and no downgrade'", violations === 0, violations);
    ok("L2 the matrix really covers both outcomes", combos > 1000 && okCount > 0 && okCount < combos, combos + "/" + okCount);
    Y.STATE.meta.regime = base;
    // D5-04 / D5-05: the hold clock and the late profit-take on the simulated cards
    const row = (over) => Object.assign({}, Y.listRowFor("3029"), over);
    let h = Y.exitGuideHtml(row({ Hold_Status: "holding", Hold_Day: 8, Hold_Remaining: 2, Hold_Total: 10, Hold_Cap: 20,
                                 Exit_Date: "2026-10-12", Exit_Signal: "", Exit_Note: "sell if it trades below 97 (fill 121.50 x 0.80)" }));
    ok("L3 holding card carries day N/T, days left and the exit date",
       h.includes("持有第 8/10 天，還有 2 個交易日，出場日 10-12"), h);
    h = Y.exitGuideHtml(row({ Hold_Status: "holding", Hold_Day: 8, Hold_Remaining: 5, Hold_Total: 10, Exit_Signal: "" }));
    ok("L3 an inconsistent clock is not printed", !h.includes("持有第"), h);
    h = Y.exitGuideHtml(row({ Hold_Status: "holding", Hold_Day: 8, Hold_Remaining: 2, Hold_Total: 10, Exit_Date: "", Exit_Signal: "" }));
    ok("L3 no exit date yet: the clock still shows, without a date", h.includes("還有 2 個交易日") && !h.includes("出場日"), h);
    h = Y.exitGuideHtml(row({ Hold_Status: "holding", Hold_Day: null, Hold_Remaining: null, Hold_Total: 10, Exit_Signal: "" }));
    ok("L3 unknown day: nothing invented", !h.includes("持有第"), h);
    h = Y.exitGuideHtml(row({ Hold_Status: "delay", Hold_Day: 12, Hold_Remaining: -2, Hold_Total: 10, Hold_Cap: 20, Exit_Signal: "" }));
    ok("L3 riding card says how far past the plan and the cap", h.includes("持有第 12 天，已過計畫的 10 天，續抱中，最晚第 20 天"), h);
    h = Y.exitGuideHtml(row({ Hold_Status: "holding", Hold_Day: 8, Hold_Remaining: 2, Hold_Total: 10, Exit_Signal: "",
                              Exit_Note: "in profit on day 8+: sell at the next open (close 123.00 at or above the fill 121.50)" }));
    ok("L4 late-due card says: sell at the next open", h.includes("下一個交易日開盤收下") && h.includes("+1%"), h);
    ok("L4 late-due is recognised only for the backend sentence",
       Y.lateDueNow({ Hold_Status: "holding", Exit_Note: "in profit on day 8+: sell at the next open (close 1 at or above the fill 1)" }) === true &&
       Y.lateDueNow({ Hold_Status: "holding", Exit_Note: "lock armed: sell if it trades below 100 (fill 98 x 1.02)" }) === false &&
       Y.lateDueNow({ Hold_Status: "exit_today", Exit_Note: "in profit on day 8+: sell at the next open" }) === false &&
       Y.lateDueNow({ Hold_Status: "holding", Exit_Signal: "stop", Exit_Note: "in profit on day 8+: sell at the next open" }) === false &&
       Y.lateDueNow({ Hold_Status: "holding" }) === false);
  }

  // --- N. which session the phone expects, and how often it asks (S-s7-21) --
  // The audit found this CONFORMS; pinned so it stays that way. Weekends are
  // skipped, holidays are not knowable on the phone (capped instead).
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    const at = (s) => new Date(Date.parse(s + "+08:00"));
    const cases = [
      ["2026-10-13T14:59:59", "2026-10-12"],   // Tue just before 15:00: Monday's close
      ["2026-10-13T15:00:00", "2026-10-13"],   // Tue 15:00 sharp: today's close
      ["2026-10-13T08:00:00", "2026-10-12"],
      ["2026-10-17T12:00:00", "2026-10-16"],   // Sat -> Fri
      ["2026-10-18T23:00:00", "2026-10-16"],   // Sun -> Fri
      ["2026-10-19T09:30:00", "2026-10-16"],   // Mon during the session -> Fri
      ["2026-10-19T14:59:00", "2026-10-16"],
      ["2026-10-19T15:00:00", "2026-10-19"],
      ["2026-10-20T00:00:00", "2026-10-19"],
    ];
    const bad = cases.filter(([t, want]) => Y.expectedSession(at(t)) !== want)
      .map(([t, want]) => t + " -> " + Y.expectedSession(at(t)) + " want " + want);
    ok("N1 the expected session turns over at 15:00 Taipei and skips weekends", bad.length === 0, bad);
    ok("N2 auto-dispatch is capped: 2 per session, 90 minutes apart; polling gives up at 25 minutes",
       Y.AUTO_MAX_PER_SESSION === 2 && Y.AUTO_GAP_MS === 90 * 60000 && Y.POLL_LIMIT_MS === 25 * 60000,
       [Y.AUTO_MAX_PER_SESSION, Y.AUTO_GAP_MS, Y.POLL_LIMIT_MS]);
    // the home banner is driven by the payload's own calendar, never by the clock
    const fresh = renderAll(await boot(synthetic(LIVE, "final"))).notices;
    ok("N3 no stale banner while the data date is the newest known session", !fresh.includes("已知最新交易日"), fresh);
    const lag = synthetic(LIVE, "final");
    lag.meta.calendar_tail = (lag.meta.calendar_tail || []).concat(["2099-01-05"]);
    const lagged = renderAll(await boot(lag)).notices;
    ok("N4 the stale banner names both dates when the calendar is ahead of the data",
       lagged.includes("掃描結果為") && lagged.includes("已知最新交易日為 2099-01-05"), lagged);
  }

  // --- M. parity with the BACKEND (--plan-grid) ----------------------------
  if (PLAN_GRID) {
    const env = await boot(LIVE);
    const Y = env.YT;
    const grid = JSON.parse(fs.readFileSync(PLAN_GRID, "utf8"));
    const DIR = { stop: "down", lock: "down", add: "down", arm: "up", target: "up", scale: "up", late: "up" };
    let bad = 0;
    for (const [sid, base, key, want] of grid.levels) {
      const got = Y.planLevel(base, Y.PLAN_MILLI[key], DIR[key], sid);
      if (got !== want) { bad++; if (bad <= 10) ok("plan grid " + sid + " " + base + " " + key, false, "got " + got + " want " + want); }
    }
    ok("plan grid: " + grid.levels.length + " levels, 0 mismatches", grid.levels.length > 1000 && bad === 0, bad);
    let badArm = 0;
    for (const [sid, base, close, want] of grid.armed) {
      Y.STATE.marksByPos = { pm: [{ session_date: "2026-09-02", close_price: close, day_index: 1 }] };
      const got = Y.activePlan({ position_id: "pm", stock_id: sid, open_shares: 1, avg_cost: base, first_buy_price: base, cycle_buys: 1 }).armed;
      if (got !== want) { badArm++; if (badArm <= 10) ok("armed " + sid + " " + base + " " + close, false, "got " + got + " want " + want); }
    }
    ok("plan grid: " + grid.armed.length + " arming probes, 0 mismatches", grid.armed.length > 500 && badArm === 0, badArm);
    let badRide = 0, ridden = 0, exited = 0, partial = 0;
    for (const c of grid.ride) {
      const pos = { position_id: "pr", stock_id: "2330", open_shares: 1, avg_cost: 10000, first_buy_price: 10000, cycle_buys: 1,
                    horizon_days: 10, cap_days: 20 };
      const marks = c.closes.map((cl, k) => ({ session_date: c.dates[k], close_price: cl, day_index: k + 1 }));
      Y.STATE.marksByPos = { pr: marks };
      Y.STATE.meta.market_leg = c.leg;
      const r = Y.rideState(pos, marks);
      // the day the phone says the ride ended: the day it broke, else the last bar
      const phoneExit = r.riding ? null : (r.broken_on ? c.dates.indexOf(r.broken_on) + 1 : c.closes.length);
      let bad;
      if (c.complete) {
        bad = r.riding !== c.riding || phoneExit !== c.exit_day;
        if (c.exit_day === null) ridden++; else exited++;
      } else {
        // dates the map does not carry are UNKNOWN: the backend reads them as 'off',
        // the phone as 'cannot prove it broke' -- so a backend ride is always a phone ride
        bad = c.riding && !r.riding;
        partial++;
      }
      if (bad) {
        badRide++;
        if (badRide <= 10) ok("ride case " + JSON.stringify(c).slice(0, 200), false, "got riding " + r.riding + " exit " + phoneExit + " want riding " + c.riding + " exit " + c.exit_day);
      }
    }
    delete Y.STATE.meta.market_leg;
    ok("plan grid: " + grid.ride.length + " ride replays, 0 mismatches",
       grid.ride.length > 200 && badRide === 0 && ridden > 20 && exited > 20 && partial > 20,
       badRide + " ridden " + ridden + " exited " + exited + " partial " + partial);
    planCases = grid.levels.length + grid.armed.length + grid.ride.length;
  }

  // --- O/P. the complete record of a closed recommendation (2026-10-09) -----
  // The phone prints what the backend stored (scanner.live_record.trade_record,
  // kept by portfolio.sync._outcome in recommendations.outcome) and never
  // recomputes it: dates, prices, price basis, costs, excursions and one line
  // per held session, in the order an owner reads a trade. Then it lines the
  // owner's OWN fills up against the rule's. O runs everywhere on frozen
  // records; P (--trade-grid) runs the same checks on records the backend
  // builds live (tests/test_mobile_trade_record.py), numbers formatted by
  // Python, so a change on either side fails here.
  {
    const env = await boot(LIVE);
    const Y = env.YT;
    // a failing check prints the value it saw, objects as JSON
    const chk = (name, cond, extra) => ok(name, cond, extra === undefined || cond ? undefined : (typeof extra === "string" ? extra : JSON.stringify(extra)));
  // frozen from scanner.live_record.trade_record via portfolio.sync._outcome on synthetic
  // bars; tests/test_mobile_trade_record.py rebuilds the same cases live (--trade-grid)
  const P = (date, open, high, low, close, close_ret_pct, status, stop, day, ride) =>
    ({ date, open, high, low, close, close_ret_pct, status, stop, day, ride: !!ride });
  const FIX = {
    stop: { sid: "1101", signal: "2026-09-07", out: {
      entry_date: "2026-09-08", entry_price: 87.3, exit_date: "2026-09-10", exit_price: 69.84,
      reason: "stop", bars: 3, ret_gross_pct: -20.0, ret_net_pct: -20.47, exit_decision_date: "2026-09-10",
      exit_fill_date: "2026-09-10", exit_basis: "level", live_fill_date: null, live_fill_price: null,
      calendar_days: 2, mfe_pct: 0.8, mae_pct: -20.96, cost_pct: 0.47, record_schema: 1,
      signal_session: "2026-09-07", planned_entry_session: "2026-09-08",
      path: [
        P("2026-09-08", 87.3, 88.0, 86.5, 87.0, -0.34, "holding", 69.84, 1, 0),
        P("2026-09-09", 86.8, 87.5, 85.0, 86.0, -1.49, "holding", 69.84, 2, 0),
        P("2026-09-10", 85.0, 85.4, 69.0, 70.5, -19.24, "exit_stop", null, 3, 0),
      ] } },
    tp: { sid: "2201", signal: "2026-09-07", out: {
      entry_date: "2026-09-08", entry_price: 52.4, exit_date: "2026-09-10", exit_price: 62.88, reason: "tp",
      bars: 3, ret_gross_pct: 20.0, ret_net_pct: 19.3, exit_decision_date: "2026-09-10",
      exit_fill_date: "2026-09-10", exit_basis: "level", live_fill_date: null, live_fill_price: null,
      calendar_days: 2, mfe_pct: 21.18, mae_pct: -0.76, cost_pct: 0.7, record_schema: 1,
      signal_session: "2026-09-07", planned_entry_session: "2026-09-08",
      path: [
        P("2026-09-08", 52.4, 53.0, 52.0, 52.8, 0.76, "holding", 41.92, 1, 0),
        P("2026-09-09", 53.0, 54.0, 52.5, 53.5, 2.1, "holding", 41.92, 2, 0),
        P("2026-09-10", 54.0, 63.5, 53.8, 62.0, 18.32, "exit_tp", null, 3, 0),
      ] } },
    late: { sid: "4401", signal: "2026-09-07", out: {
      entry_date: "2026-09-08", entry_price: 100.0, exit_date: "2026-09-18", exit_price: 101.5,
      reason: "late", bars: 9, ret_gross_pct: 1.5, ret_net_pct: 0.91, exit_decision_date: "2026-09-17",
      exit_fill_date: "2026-09-18", exit_basis: "open", live_fill_date: null, live_fill_price: null,
      calendar_days: 10, mfe_pct: 2.0, mae_pct: -0.5, cost_pct: 0.59, record_schema: 1,
      signal_session: "2026-09-07", planned_entry_session: "2026-09-08",
      path: [
        P("2026-09-08", 100.0, 100.5, 99.5, 100.0, 0.0, "holding", 80.0, 1, 0),
        P("2026-09-09", 100.0, 100.5, 99.5, 100.0, 0.0, "holding", 80.0, 2, 0),
        P("2026-09-10", 100.0, 100.5, 99.5, 100.0, 0.0, "holding", 80.0, 3, 0),
        P("2026-09-11", 100.0, 100.5, 99.5, 100.0, 0.0, "holding", 80.0, 4, 0),
        P("2026-09-14", 100.0, 100.5, 99.5, 100.0, 0.0, "holding", 80.0, 5, 0),
        P("2026-09-15", 100.0, 100.5, 99.5, 100.0, 0.0, "holding", 80.0, 6, 0),
        P("2026-09-16", 100.0, 100.5, 99.5, 100.0, 0.0, "holding", 80.0, 7, 0),
        P("2026-09-17", 100.0, 101.4, 99.8, 101.2, 1.2, "sell_next_open", 80.0, 8, 0),
        P("2026-09-18", 101.5, 102.0, 100.8, 101.0, 1.0, "exit_late", null, 9, 0),
      ] } },
    time: { sid: "5501", signal: "2026-09-07", out: {
      entry_date: "2026-09-08", entry_price: 100.0, exit_date: "2026-09-21", exit_price: 96.2,
      reason: "time", bars: 10, ret_gross_pct: -3.8, ret_net_pct: -4.36, exit_decision_date: "2026-09-21",
      exit_fill_date: "2026-09-21", exit_basis: "close", live_fill_date: "2026-09-22", live_fill_price: 96.0,
      calendar_days: 13, mfe_pct: 0.2, mae_pct: -4.1, cost_pct: 0.56, record_schema: 1,
      signal_session: "2026-09-07", planned_entry_session: "2026-09-08",
      path: [
        P("2026-09-08", 100.0, 100.2, 99.5, 99.8, -0.2, "holding", 80.0, 1, 0),
        P("2026-09-09", 99.6, 99.8, 99.1, 99.4, -0.6, "holding", 80.0, 2, 0),
        P("2026-09-10", 99.2, 99.4, 98.7, 99.0, -1.0, "holding", 80.0, 3, 0),
        P("2026-09-11", 98.8, 99.0, 98.3, 98.6, -1.4, "holding", 80.0, 4, 0),
        P("2026-09-14", 98.4, 98.6, 97.9, 98.2, -1.8, "holding", 80.0, 5, 0),
        P("2026-09-15", 98.0, 98.2, 97.5, 97.8, -2.2, "holding", 80.0, 6, 0),
        P("2026-09-16", 97.6, 97.8, 97.1, 97.4, -2.6, "holding", 80.0, 7, 0),
        P("2026-09-17", 97.2, 97.4, 96.7, 97.0, -3.0, "holding", 80.0, 8, 0),
        P("2026-09-18", 96.8, 97.0, 96.3, 96.6, -3.4, "holding", 80.0, 9, 0),
        P("2026-09-21", 96.4, 96.6, 95.9, 96.2, -3.8, "exit_time", null, 10, 0),
      ] } },
    ride: { sid: "6601", signal: "2026-09-07", out: {
      entry_date: "2026-09-08", entry_price: 100.0, exit_date: "2026-09-23", exit_price: 100.0,
      reason: "time", bars: 12, ret_gross_pct: 0.0, ret_net_pct: -0.58, exit_decision_date: "2026-09-23",
      exit_fill_date: "2026-09-23", exit_basis: "close", live_fill_date: "2026-09-24",
      live_fill_price: 100.0, calendar_days: 15, mfe_pct: 1.2, mae_pct: -0.9, cost_pct: 0.58,
      record_schema: 1, signal_session: "2026-09-07", planned_entry_session: "2026-09-08",
      path: [
        P("2026-09-08", 100.0, 100.3, 99.6, 100.0, 0.0, "holding", 80.0, 1, 0),
        P("2026-09-09", 99.5, 99.8, 99.1, 99.5, -0.5, "holding", 80.0, 2, 0),
        P("2026-09-10", 99.8, 100.1, 99.4, 99.8, -0.2, "holding", 80.0, 3, 0),
        P("2026-09-11", 99.6, 99.9, 99.2, 99.6, -0.4, "holding", 80.0, 4, 0),
        P("2026-09-14", 99.9, 100.2, 99.5, 99.9, -0.1, "holding", 80.0, 5, 0),
        P("2026-09-15", 100.0, 100.3, 99.6, 100.0, 0.0, "holding", 80.0, 6, 0),
        P("2026-09-16", 100.1, 100.4, 99.7, 100.1, 0.1, "holding", 80.0, 7, 0),
        P("2026-09-17", 100.3, 100.6, 99.9, 100.3, 0.3, "holding", 80.0, 8, 0),
        P("2026-09-18", 100.5, 100.8, 100.1, 100.5, 0.5, "holding", 80.0, 9, 0),
        P("2026-09-21", 100.8, 101.1, 100.4, 100.8, 0.8, "holding", 80.0, 10, 1),
        P("2026-09-22", 100.9, 101.2, 100.5, 100.9, 0.9, "holding", 80.0, 11, 1),
        P("2026-09-23", 100.0, 100.3, 99.6, 100.0, 0.0, "exit_time", null, 12, 0),
      ] } },
  };
    const f2 = (n) => n.toFixed(2);
    const sg = (n) => (n >= 0 ? "+" : "") + n.toFixed(2);
    const JUNK = /NaN|undefined|\[object|\bnull\b/;
    const REASON = { tp: "停利", stop: "停損", lock: "鎖利", late: "後期收下", time: "期滿" };
    const BASIS = { level: "盤中觸價", open: "開盤跳空或隔日開盤賣", close: "收盤價" };
    const STATUS = { holding: "持有", armed: "鎖利啟動", sell_next_open: "收盤達標，隔日開盤收下" };
    const statusTxt = (s) => STATUS[s] || (s.startsWith("exit_") ? "出場：" + REASON[s.slice(5)] : s);
    const plain = (h) => h.replace(/<br>/g, " ").replace(/<[^>]+>/g, "");
    const rowsOf = (html) => html.split('<div class="drow">').slice(1).map((chunk) => {
      const m = /^<span class="k">([^<]*)<\/span><span>([\s\S]*)<\/span>$/.exec(chunk.slice(0, chunk.indexOf("</div>")));
      return m ? [m[1], m[2].replace(/<br>/g, " / ").replace(/<[^>]+>/g, "")] : null;
    }).filter(Boolean);
    const kvOf = (html) => {
      const out = {};
      const re = /<span class="kv-l">([^<]*)<\/span><span class="kv-v [^"]*">([^<]*)<\/span>/g;
      let m;
      while ((m = re.exec(html))) out[m[1]] = m[2];
      return out;
    };
    const lateWords = (n) => (n === 0 ? "同一個交易日" : n > 0 ? "晚 " + n + " 個交易日" : "早 " + (-n) + " 個交易日");
    const dayWords = (n) => (n === 0 ? "同一天" : n > 0 ? "晚 " + n + " 日曆天" : "早 " + (-n) + " 日曆天");

    // what the owner must see for one stored record: derived from the outcome the
    // way tests/test_mobile_trade_record.py derives it from the backend's
    function expectFrom(out, signal, legacy) {
      const e = {
        reason: out.reason, basis: legacy ? null : out.exit_basis, bars: out.bars, signal,
        entry_date: out.entry_date, entry_price: f2(out.entry_price),
        exit_date: out.exit_date, exit_price: f2(out.exit_price),
        gross: sg(out.ret_gross_pct), net: sg(out.ret_net_pct),
        calendar_days: legacy ? Math.round((Date.parse(out.exit_date) - Date.parse(out.entry_date)) / 864e5) : out.calendar_days,
        cost: f2(legacy ? Math.round((out.ret_gross_pct - out.ret_net_pct) * 100) / 100 : out.cost_pct),
        decision: legacy ? null : out.exit_decision_date,
        mfe: legacy ? null : sg(out.mfe_pct), mae: legacy ? null : sg(out.mae_pct),
        path: [], live: null, ride_days: 0,
      };
      if (!legacy) {
        e.path = (out.path || []).map((p) => ({
          date: p.date, day: p.day, status: p.status, ride: p.ride, open: f2(p.open), high: f2(p.high),
          low: f2(p.low), close: f2(p.close), ret: sg(p.close_ret_pct), stop: p.stop === null ? null : f2(p.stop) }));
        e.ride_days = e.path.filter((p) => p.ride).length;
        if (out.reason === "time" && out.live_fill_date) {
          e.live = { date: out.live_fill_date, price: f2(out.live_fill_price),
                     gap: sg(Math.round((out.live_fill_price / out.exit_price - 1) * 10000) / 100) };
        }
      }
      return e;
    }
    function rowOf(sid, signal, outcome, over) {
      return Object.assign({
        recommendation_id: "rec-" + sid + "-mode_prelaunch-1", stock_id: sid, stock_name: "T" + sid,
        status: "closed", status_reason: outcome ? outcome.reason : null,
        status_session: outcome ? outcome.exit_date : null,
        first_qualified_session: signal, valid_until_session: outcome ? outcome.entry_date : null, outcome }, over || {});
    }
    const SUMMARY = ["entry_date", "entry_price", "exit_date", "exit_price", "reason", "bars", "ret_gross_pct", "ret_net_pct"];
    const legacyOf = (out) => { const o = {}; for (const k of SUMMARY) o[k] = out[k]; return o; };

    // one stored record, every line the owner reads
    function checkRecord(label, row, e) {
      const L = (s) => label + ": " + s;
      const v = Y.tradeRecordView(row);
      const html = Y.tradeRecordHtml(v, null, false);
      chk(L("no NaN / undefined / null in the page"), !JUNK.test(html), (html.match(JUNK) || [])[0]);
      chk(L("the outcome was read"), v.has_outcome === true);
      chk(L("signal day"), v.signal_session === e.signal, v.signal_session);
      chk(L("rule entry day and open"), v.entry_date === e.entry_date && f2(v.entry_price) === e.entry_price, v.entry_date);
      chk(L("exit fill day and price"), v.exit_fill_date === e.exit_date && f2(v.exit_price) === e.exit_price, v.exit_fill_date);
      chk(L("exit decision day"), v.exit_decision_date === (e.decision || null), v.exit_decision_date);
      chk(L("price basis code"), v.exit_basis === (e.basis || ""), v.exit_basis);
      chk(L("reason code"), v.reason === e.reason, v.reason);
      chk(L("sessions and calendar days held"), v.bars === e.bars && v.calendar_days === e.calendar_days, [v.bars, v.calendar_days]);
      chk(L("gross, net and cost"), sg(v.ret_gross) === e.gross && sg(v.ret_net) === e.net && f2(v.cost) === e.cost, [v.ret_gross, v.ret_net, v.cost]);
      chk(L("best and worst excursion"), e.mfe ? sg(v.mfe) === e.mfe && sg(v.mae) === e.mae : v.mfe === null && v.mae === null, [v.mfe, v.mae]);
      chk(L("a stored record is consistent with itself"), v.issues.length === 0, v.issues);
      // the chain, in the owner's order
      const rows = rowsOf(html);
      chk(L("the chain is in the owner's order"),
         JSON.stringify(rows.map((r) => r[0])) === JSON.stringify(["訊號日", "規則進場", "出場判定日", "出場成交", "出場原因", "持有"]),
         rows.map((r) => r[0]));
      const val = (k) => (rows.find((r) => r[0] === k) || [])[1];
      chk(L("signal day printed"), val("訊號日") === e.signal, val("訊號日"));
      chk(L("entry day and open printed"), val("規則進場") === e.entry_date + "｜開盤 " + e.entry_price, val("規則進場"));
      chk(L("decision day printed"), val("出場判定日") === (e.decision || "-"), val("出場判定日"));
      chk(L("fill day, price and basis printed"),
         val("出場成交") === e.exit_date + "｜" + e.exit_price + "｜" + (e.basis ? BASIS[e.basis] : "價格依據未記錄"), val("出場成交"));
      chk(L("reason printed"), val("出場原因") === REASON[e.reason], val("出場原因"));
      chk(L("hold printed"), val("持有") === e.bars + " 個交易日 / " + e.calendar_days + " 日曆天", val("持有"));
      const kvs = kvOf(html);
      chk(L("gross printed"), kvs["毛報酬"] === e.gross + "%", kvs["毛報酬"]);
      chk(L("net printed"), kvs["淨報酬"] === e.net + "%", kvs["淨報酬"]);
      chk(L("cost printed"), kvs["成本"] === e.cost + "%", kvs["成本"]);
      chk(L("MFE / MAE printed"), kvs["最高（MFE）"] === (e.mfe ? e.mfe + "%" : "-") && kvs["最低（MAE）"] === (e.mae ? e.mae + "%" : "-"), [kvs["最高（MFE）"], kvs["最低（MAE）"]]);
      const at = (s) => html.indexOf(s);
      chk(L("hold comes before the returns, the returns before the excursions"),
         at('<span class="k">持有</span>') > 0 && at('<span class="k">持有</span>') < at("毛報酬") &&
         at("毛報酬") < at("淨報酬") && at("淨報酬") < at("成本") && at("成本") < at("最高（MFE）") && at("最高（MFE）") < at("最低（MAE）"));
      // a time exit books the close; the order placed that evening fills at the next open
      if (e.reason === "time") {
        const hint = "實盤：當晚下單，隔日開盤";
        if (e.live) {
          chk(L("the live next-open fill is shown beside the booked close"),
             html.includes(hint + " " + e.live.date + " 約 " + e.live.price + "（帳上是收盤價 " + e.exit_price + "，差 " + e.live.gap + "%）"), plain(html).slice(-300));
        } else {
          chk(L("a time exit with no next bar yet says so, not a price"), html.includes(hint + "成交；隔日行情還沒有，價格待補"), plain(html).slice(-300));
        }
        chk(L("the live line sits after the excursions"), at(hint) > at("最低（MAE）"));
      } else {
        chk(L("only a time exit carries the live line"), !html.includes("實盤："));
      }
      // the day-by-day table
      chk(L("a collapsible table iff there is a path"), html.includes('<details class="strategy dim"') === (e.path.length > 0));
      if (e.path.length) {
        const body = html.split("<tbody>")[1].split("</tbody>")[0];
        const trs = body.split("<tr").slice(1).map((s) => plain(s.replace(/^[^>]*>/, "")));
        chk(L("one line per held session"), trs.length === e.path.length, trs.length);
        let bad = 0;
        e.path.forEach((p, i) => {
          const want = ["D" + p.day, p.date, "開 " + p.open + " 高 " + p.high, "低 " + p.low + " 收 " + p.close, p.ret + "%",
                        statusTxt(p.status) + (p.ride ? "・期滿後續抱" : ""), "停損 " + (p.stop === null ? "-" : p.stop)];
          let pos = 0;
          for (const w of want) {
            const k = (trs[i] || "").indexOf(w, pos);
            if (k < 0) { bad++; if (bad <= 4) chk(L("path day " + (i + 1) + " shows '" + w + "'"), false, trs[i]); break; }
            pos = k + w.length;
          }
        });
        chk(L("every path day matches the backend's"), bad === 0, bad);
        chk(L("sessions kept after the day-10 test are flagged as riding"),
           (html.match(/期滿後續抱/g) || []).length === e.ride_days && v.ride_days === e.ride_days, v.ride_days);
        chk(L("the exit row is the last"), body.lastIndexOf("出場：") > body.lastIndexOf("持有") || e.path.length === 1);
      } else {
        chk(L("an older record says it has no day-by-day path"), html.includes("早期紀錄，沒有逐日路徑") && !html.includes("<details"));
      }
      return html;
    }

    // the owner's fills against the rule's
    function checkRecon(label, c) {
      const L = (s) => label + ": " + s;
      const v = Y.tradeRecordView(c.rec);
      const r = Y.reconcileTrade(v, c.execs, c.calendars);
      const e = c.expect;
      chk(L("a reconciliation exists"), r !== null);
      if (!r) return;
      chk(L("fill counts and shares left"), r.n_buys === e.n_buys && r.n_sells === e.n_sells && r.open_shares === e.open_shares, [r.n_buys, r.n_sells, r.open_shares]);
      chk(L("my first buy: day and price"), r.buy.date === e.buy.date && r.buy.price_cents === e.buy.price_cents, [r.buy.date, r.buy.price_cents]);
      chk(L("average buy price"), r.buy.avg_cents === e.buy.avg_cents, r.buy.avg_cents);
      chk(L("buy lateness in sessions (mine minus the rule's)"), r.buy.gap.sessions === e.buy.gap, r.buy.gap);
      chk(L("buy lateness in calendar days"), r.buy.gap.days === e.buy.days, r.buy.gap);
      chk(L("buy price difference, sign and size"), sg(r.buy.diff_pct) === e.buy.diff, r.buy.diff_pct);
      const html = Y.reconcileHtml(r);
      chk(L("no NaN / undefined / null in the reconciliation"), !JUNK.test(html), (html.match(JUNK) || [])[0]);
      chk(L("buy lateness in words"), html.includes(e.buy.gap === null ? dayWords(e.buy.days) : lateWords(e.buy.gap)), plain(html).slice(0, 200));
      chk(L("buy difference printed with its sign"), html.includes("價差 " + e.buy.diff + "%"), plain(html).slice(0, 300));
      if (e.sell === null) {
        chk(L("no sell, no result"), r.sell === null && r.result === null && !html.includes("實際結果"));
        return;
      }
      chk(L("my sell: day and price"), r.sell.date === e.sell.date && r.sell.price_cents === e.sell.price_cents, [r.sell.date, r.sell.price_cents]);
      chk(L("sell lateness vs the rule's fill (sessions, days)"), r.sell.gap.sessions === e.sell.gap && r.sell.gap.days === e.sell.days, r.sell.gap);
      chk(L("sell price difference vs the rule's fill"), sg(r.sell.diff_pct) === e.sell.diff, r.sell.diff_pct);
      chk(L("sell lateness in words"), html.includes(e.sell.gap === null ? dayWords(e.sell.days) : lateWords(e.sell.gap)));
      if (e.sell.live) {
        chk(L("against the live next-open fill: lateness and difference"),
           r.sell.live !== null && r.sell.live.date === e.sell.live.date && r.sell.live.gap.sessions === e.sell.live.gap &&
           sg(r.sell.live.diff_pct) === e.sell.live.diff, r.sell.live);
        chk(L("the live comparison is printed"), html.includes("對照實盤隔日開盤 " + e.sell.live.date) && html.includes("價差 " + e.sell.live.diff + "%"));
      } else {
        chk(L("no live comparison without a live fill"), r.sell.live === null && !html.includes("對照實盤"));
      }
      chk(L("my result in cents"), r.result !== null && r.result.net_cents === e.result.net_cents, r.result);
      chk(L("my net %, the rule's, and the gap in points"),
         sg(r.result.net_pct) === e.result.net_pct && sg(r.result.rule_net_pct) === e.result.rule_net && sg(r.result.delta_pp) === e.result.delta,
         [r.result.net_pct, r.result.rule_net_pct, r.result.delta_pp]);
      chk(L("the result is printed"), html.includes(e.result.net_pct + "%") && html.includes("規則淨 " + e.result.rule_net + "%") && html.includes("差 " + e.result.delta + " 個百分點"), plain(html).slice(-300));
    }

    // ---- O1-O3: five frozen backend records, as an object and as JSON text -----
    const KINDS = { stop: "stop / level", tp: "take profit / level", late: "late profit-take", time: "time exit + live fill", ride: "ride past day 10" };
    const htmls = {};
    for (const k of Object.keys(FIX)) {
      const f = FIX[k];
      const e = expectFrom(f.out, f.signal, false);
      htmls[k] = checkRecord("O1 " + KINDS[k], rowOf(f.sid, f.signal, f.out), e);
      const text = checkRecord("O2 " + KINDS[k] + " as JSON text", rowOf(f.sid, f.signal, JSON.stringify(f.out)), e);
      chk("O2 " + k + ": JSON text and object render identically", text === htmls[k]);
      checkRecord("O3 " + KINDS[k] + " (older record)", rowOf(f.sid, f.signal, legacyOf(f.out)), expectFrom(legacyOf(f.out), f.signal, true));
    }
    chk("O1 the fixtures cover every price basis", ["level", "open", "close"].every((b) => Object.values(FIX).some((f) => f.out.exit_basis === b)));
    chk("O1 the stop and the take-profit fill at their level, inside the day", FIX.stop.out.exit_basis === "level" && FIX.tp.out.exit_basis === "level");
    {
      const v = Y.tradeRecordView(rowOf(FIX.late.sid, FIX.late.signal, FIX.late.out));
      chk("O4 a late profit-take is decided the session BEFORE it fills", v.exit_decision_date < v.exit_fill_date && v.exit_decision_date === FIX.late.out.path[FIX.late.out.path.length - 2].date, [v.exit_decision_date, v.exit_fill_date]);
      const rows = rowsOf(htmls.late);
      chk("O4 both days are printed, decision first", rows[2][1] === v.exit_decision_date && rows[3][1].startsWith(v.exit_fill_date), rows.slice(2, 4));
      chk("O4 the day before the fill is marked 'sell at the next open'", htmls.late.includes("收盤達標，隔日開盤收下") && FIX.late.out.path[FIX.late.out.path.length - 2].status === "sell_next_open");
      const t = Y.tradeRecordView(rowOf(FIX.time.sid, FIX.time.signal, FIX.time.out));
      chk("O5 a time exit: booked on the decision day, live fill on the next session", t.exit_decision_date === t.exit_fill_date && t.live_fill_date > t.exit_fill_date && t.basis_text === "收盤價", [t.live_fill_date, t.basis_text]);
      const noLive = JSON.parse(JSON.stringify(FIX.time.out));
      noLive.live_fill_date = null; noLive.live_fill_price = null;
      checkRecord("O5 time exit without a next bar", rowOf(FIX.time.sid, FIX.time.signal, noLive), expectFrom(noLive, FIX.time.signal, false));
      const rideV = Y.tradeRecordView(rowOf(FIX.ride.sid, FIX.ride.signal, FIX.ride.out));
      chk("O6 the ride is flagged on exactly the sessions the backend flagged", JSON.stringify(rideV.path.filter((p) => p.ride).map((p) => p.day)) === "[10,11]" && rideV.bars === 12, rideV.path.filter((p) => p.ride).map((p) => p.day));
      chk("O6 a ride's flat result is a flat zero, never a blank", FIX.ride.out.ret_gross_pct === 0 && kvOf(htmls.ride)["毛報酬"] === "+0.00%", kvOf(htmls.ride)["毛報酬"]);
    }

    // ---- O7: malformed and partial data degrade to dashes, never to junk ------
    {
      const base = () => JSON.parse(JSON.stringify(FIX.stop.out));
      const show = (label, outcome, over) => {
        const row = rowOf("1101", "2026-09-07", outcome, over);
        const v = Y.tradeRecordView(row);
        const html = Y.tradeRecordHtml(v, null, false);
        chk(label + ": no NaN / undefined / null / [object", !JUNK.test(html), (html.match(JUNK) || [])[0]);
        return { v, html };
      };
      let o = base(); o.path = "oops";
      let s = show("O7 a path that is not a list", o);
      chk("O7 ... says so and shows no table", s.v.path_state === "malformed" && s.html.includes("逐日路徑格式不符") && !s.html.includes("<details"));
      o = base(); o.path = [];
      s = show("O7 an empty path", o);
      chk("O7 ... says it is empty", s.v.path_state === "empty" && s.html.includes("逐日路徑是空的"));
      o = base(); o.path = [null, 5, "x", [], {}, { date: "bad", close: "abc" }, { date: "2026-09-08", open: NaN, high: Infinity, low: "", close: "87,0", close_ret_pct: null, status: 7, stop: undefined, day: -2, ride: "yes" },
                            { date: "2026-09-09", open: 86.8, high: 87.5, low: 85, close: 86, close_ret_pct: -1.49, status: "holding", stop: 69.84, day: 2, ride: false }];
      s = show("O7 a partial path", o);
      chk("O7 ... keeps the readable days, counts the unreadable", s.v.path.length === 2 && s.v.path_dropped === 6 && s.html.includes("有 6 筆格式不符的日資料已略過"), [s.v.path.length, s.v.path_dropped]);
      chk("O7 ... the damaged day is dashes, not zeros", /<td>-<br>/.test(s.html) && s.html.includes("開 - 高 -<br>低 - 收 -"), plain(s.html).slice(0, 400));
      chk("O7 ... a non-true ride flag does not flag a ride", s.v.ride_days === 0 && !s.html.includes("期滿後續抱"));
      o = base(); o.entry_price = NaN; o.exit_price = "n/a"; o.ret_net_pct = {}; o.ret_gross_pct = true; o.bars = -3; o.calendar_days = 2.5; o.exit_basis = 5; o.reason = null; o.mfe_pct = "abc"; o.cost_pct = []; o.exit_decision_date = "2026-13-45"; o.entry_date = "nope"; o.path = null;
      s = show("O7 nonsense in every field", o, { status_reason: null, first_qualified_session: null, stock_name: null });
      const kv = kvOf(s.html);
      chk("O7 ... every number is a dash", ["毛報酬", "淨報酬", "最高（MFE）"].every((k) => kv[k] === "-") && rowsOf(s.html).find((r) => r[0] === "持有")[1] === "- 個交易日 / - 日曆天", [kv, rowsOf(s.html).map((r) => r.join("="))]);
      chk("O7 ... the basis falls back to 'not recorded'", rowsOf(s.html).find((r) => r[0] === "出場成交")[1].endsWith("價格依據未記錄"));
      s = show("O7 outcome is invalid JSON text", "{not json");
      chk("O7 ... a closed row with no readable outcome says so", !s.v.has_outcome && s.html.includes("沒有成交摘要"));
      s = show("O7 outcome missing", null);
      chk("O7 ... same", !s.v.has_outcome && s.html.includes("沒有成交摘要"));
      s = show("O7 outcome is a list", [1, 2]);
      chk("O7 ... same for a list", !s.v.has_outcome);
      o = base(); o.reason = "mystery"; o.exit_basis = "teleport"; o.path[0].status = "exit_"; o.path[1].status = "weird";
      s = show("O7 unknown codes", o);
      chk("O7 ... an unknown code is shown as itself, not hidden", s.html.includes("mystery") && s.html.includes("weird") && rowsOf(s.html).find((r) => r[0] === "出場成交")[1].endsWith("價格依據未記錄"));
      o = base(); o.reason = "constructor"; o.exit_basis = "toString";
      s = show("O7 names that live on Object.prototype", o);
      chk("O7 ... are not looked up as labels", s.html.includes("constructor") && !/function|native code/.test(s.html));
      // a record that disagrees with itself is labelled, never silently corrected
      o = base(); o.bars = 5;
      s = show("O7 bars disagree with the path", o);
      chk("O7 ... is labelled and keeps the stored number", s.v.issues.includes("bars_vs_path") && s.html.includes("這筆紀錄自己對不上") && rowsOf(s.html).find((r) => r[0] === "持有")[1].startsWith("5 個交易日"));
      o = base(); o.exit_price = 75;
      s = show("O7 exit price does not give the gross return", o);
      chk("O7 ... is labelled", s.v.issues.includes("ret_vs_prices"));
      o = base(); o.path = o.path.slice().reverse();
      s = show("O7 path out of order", o);
      chk("O7 ... is labelled", s.v.issues.includes("path_order"));
      o = base(); o.entry_date = "2026-09-09";
      s = show("O7 entry day is not the first path day", o);
      chk("O7 ... is labelled", s.v.issues.includes("entry_vs_path") && s.v.issues.includes("planned_entry"));
      o = base(); o.exit_fill_date = "2026-09-09";
      s = show("O7 exit day is not the last path day", o);
      chk("O7 ... is labelled", s.v.issues.includes("exit_vs_path"));
    }

    // ---- O8: nothing from the data reaches the page as markup -------------------
    {
      const evil = '<img src=x onerror=alert(1)>';
      const o = JSON.parse(JSON.stringify(FIX.time.out));
      o.reason = '<b>r</b>'; o.exit_basis = '<i>b</i>'; o.path[0].status = '<u>s</u>'; o.path[1].date = '"><script>1</script>';
      const row = rowOf('"><script>', "2026-09-07", o, { stock_name: evil, recommendation_id: evil });
      const v = Y.tradeRecordView(row);
      const html = Y.tradeRecordHtml(v, Y.reconcileTrade(v, [
        { side: "BUY", session_date: "2026-09-08", price_cents: 10000, shares: 1000, fee_cents: 1, tax_cents: 0, is_current: 1 }], []), true);
      chk("O8 hostile names, codes and statuses are escaped", !/<img|<script|<u>|<i>|<\/b>r|<b>r/.test(html) && html.includes("&lt;img"), (html.match(/<img|<script|<u>|<i>/) || [])[0]);
      chk("O8 a hostile id cannot break the attribute it is written into", !/data-rid="[^"]*"[^>]*onerror/.test(html) && /data-rid="&lt;img/.test(html), html.slice(0, 120));
    }

    // ---- O9: the list of closed recommendations ------------------------------
    {
      const a = rowOf("1101", "2026-09-07", FIX.stop.out);
      const b = rowOf("2201", "2026-09-07", FIX.tp.out, { recommendation_id: "rec-2201-x-1" });
      const c = rowOf("5501", "2026-09-07", FIX.time.out);
      const junk = [null, 5, "x", { status: "closed" }, rowOf("9", "2026-09-07", FIX.ride.out, { status: "active" }),
                    rowOf("8", "2026-09-07", FIX.ride.out, { status: "expired" }), rowOf("7", "2026-09-07", FIX.ride.out, { status: "cancelled" })];
      const list = Y.closedRecords([a, b, c].concat(junk));
      chk("O9 only closed recommendations are listed, newest exit first",
         list.length === 4 && list[0].stock_id === "5501" && list[1].stock_id === "1101" && list[2].stock_id === "2201" && list[3].stock_id === undefined,
         list.map((q) => q && q.stock_id));
      chk("O9 not a list: nothing", Y.closedRecords(null).length === 0 && Y.closedRecords({}).length === 0);
      const saved = { recs: Y.STATE.recs, st: Y.STATE.recsState, pos: Y.STATE.positions, ex: Y.STATE.execsByPos };
      Y.STATE.recs = null; Y.STATE.recsState = "missing";
      chk("O9 no recommendations.json: says so", Y.recordsSectionHtml().includes("這次部署沒有 recommendations.json"));
      Y.STATE.recsState = "loading";
      chk("O9 while loading: says so", Y.recordsSectionHtml().includes("讀取建議紀錄中"));
      Y.STATE.recs = [rowOf("9", "2026-09-07", FIX.ride.out, { status: "active" })]; Y.STATE.recsState = "ready";
      chk("O9 no closed recommendation: says so", Y.recordsSectionHtml().includes("目前沒有已結案的建議"));
      const many = [];
      for (let i = 0; i < 27; i++) many.push(rowOf("S" + i, "2026-09-07", FIX.stop.out, { recommendation_id: "rec-" + i }));
      Y.STATE.recs = many;
      const sec = Y.recordsSectionHtml();
      chk("O9 only the most recent " + Y.TR_RECORDS_KEPT + " are listed, and it says how many exist",
         (sec.match(/<article/g) || []).length === Y.TR_RECORDS_KEPT && sec.includes("只列最近 " + Y.TR_RECORDS_KEPT + " 筆（共 27 筆）"), (sec.match(/<article/g) || []).length);
      Y.STATE.recs = [a, c]; Y.STATE.recsState = "missing";
      chk("O9 a failed refresh keeps the last records and says it failed", Y.recordsSectionHtml().includes("更新失敗，顯示的是上次讀到的內容") && Y.recordsSectionHtml().includes("<article"));
      chk("O9 nothing in the section tells the owner to trim a position", !/減碼|減倉|降部位|部位減量|提前賣|先賣一半/.test(sec));
      Y.STATE.recs = saved.recs; Y.STATE.recsState = saved.st; Y.STATE.positions = saved.pos; Y.STATE.execsByPos = saved.ex;
    }

    // ---- O10: the owner's fills against the rule's, by hand ------------------
    {
      const cal = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
                   "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"];
      const out = FIX.time.out;            // entry 100.00 on 09-08, booked 96.20 on 09-21, live fill 96.00 on 09-22
      const row = rowOf(FIX.time.sid, FIX.time.signal, out);
      const v = Y.tradeRecordView(row);
      const fill = (side, date, price_cents, shares, fee_cents, tax_cents) =>
        ({ side, session_date: date, price_cents, shares, fee_cents, tax_cents: tax_cents || 0, is_current: 1, executed_at: "", recorded_at: "2026-10-09 10:00:00" });
      // bought one session late, 0.45% dearer; sold two sessions after the rule's fill, 0.62% under it
      const r = Y.reconcileTrade(v, [fill("BUY", "2026-09-09", 10045, 1000, 14300), fill("SELL", "2026-09-23", 9560, 1000, 13600, 28700)], [cal]);
      chk("O10 bought 1 session late", r.buy.gap.sessions === 1 && r.buy.gap.days === 1, r.buy.gap);
      chk("O10 bought 0.45% above the rule's open (positive = I paid more)", r.buy.diff_pct === 0.45, r.buy.diff_pct);
      chk("O10 sold 2 sessions after the rule's fill, 0.62% below it (negative = I got less)", r.sell.gap.sessions === 2 && r.sell.diff_pct === -0.62, [r.sell.gap, r.sell.diff_pct]);
      chk("O10 against the live next open (09-22 at 96.00): 1 session late, 0.42% below", r.sell.live.gap.sessions === 1 && r.sell.live.diff_pct === -0.42, r.sell.live);
      chk("O10 result: -541,600 cents = -5.38% against the rule's -4.36%, 1.02 points worse",
         r.result.net_cents === -541600 && r.result.net_pct === -5.38 && r.result.rule_net_pct === -4.36 && r.result.delta_pp === -1.02, r.result);
      const html = Y.reconcileHtml(r);
      chk("O10 printed: late / dearer / lower / worse", html.includes("晚 1 個交易日") && html.includes("買得比規則貴") && html.includes("賣得比規則低") && html.includes("晚 2 個交易日") && html.includes("-5.38%") && html.includes("-1.02 個百分點"), plain(html));
      // early and cheaper; sold on the rule's own day for more; two sells (weighted average)
      const r2 = Y.reconcileTrade(v, [fill("BUY", "2026-09-07", 9900, 500, 7000), fill("SELL", "2026-09-21", 9700, 300, 4000, 8700), fill("SELL", "2026-09-21", 9800, 200, 3000, 5900)], [cal]);
      chk("O10 bought a session EARLY and cheaper", r2.buy.gap.sessions === -1 && r2.buy.diff_pct === -1.0, [r2.buy.gap, r2.buy.diff_pct]);
      chk("O10 sold on the rule's day: 0 sessions, weighted average 97.40 (+1.25%)", r2.sell.gap.sessions === 0 && r2.sell.price_cents === 9740 && r2.sell.diff_pct === 1.25 && r2.n_sells === 2, [r2.sell.gap, r2.sell.price_cents, r2.sell.diff_pct]);
      chk("O10 ... the words", Y.reconcileHtml(r2).includes("早 1 個交易日") && Y.reconcileHtml(r2).includes("同一個交易日") && Y.reconcileHtml(r2).includes("賣得比規則高") && Y.reconcileHtml(r2).includes("買得比規則便宜"));
      // exactly equal prices
      const r3 = Y.reconcileTrade(v, [fill("BUY", "2026-09-08", 10000, 100, 100), fill("SELL", "2026-09-21", 9620, 100, 100, 100)], [cal]);
      chk("O10 the same price is 0.00, worded as the same price", r3.buy.diff_pct === 0 && r3.sell.diff_pct === 0 && Y.reconcileHtml(r3).split("同價").length === 3, [r3.buy.diff_pct, r3.sell.diff_pct]);
      // still holding
      const r4 = Y.reconcileTrade(v, [fill("BUY", "2026-09-08", 10000, 1000, 14300), fill("SELL", "2026-09-21", 9620, 400, 5000, 11500)], [cal]);
      chk("O10 still holding 600: no sell line, no result", r4.sell === null && r4.result === null && r4.open_shares === 600 && Y.reconcileHtml(r4).includes("你還有 600 股沒賣"), r4);
      const r5 = Y.reconcileTrade(v, [fill("BUY", "2026-09-08", 10000, 1000, 14300)], [cal]);
      chk("O10 bought, not sold yet: says how many shares are still open", r5.sell === null && r5.open_shares === 1000 && Y.reconcileHtml(r5).includes("你還有 1,000 股沒賣"));
      // nothing to reconcile
      chk("O10 no executions: nothing", Y.reconcileTrade(v, [], [cal]) === null);
      chk("O10 only a sell: nothing", Y.reconcileTrade(v, [fill("SELL", "2026-09-21", 9620, 100, 1, 1)], [cal]) === null);
      chk("O10 superseded and malformed fills are ignored", Y.reconcileTrade(v, [Object.assign(fill("BUY", "2026-09-08", 10000, 100, 1), { is_current: 0 }), null, {}, fill("BUY", "bad", 10000, 100, 1), fill("BUY", "2026-09-08", 0, 100, 1), fill("BUY", "2026-09-08", 10000, 0, 1), fill("BUY", "2026-09-08", 100.5, 1, 1)], [cal]) === null);
      chk("O10 a record with no outcome has nothing to reconcile against", Y.reconcileTrade(Y.tradeRecordView(rowOf("1", "2026-09-07", null)), [fill("BUY", "2026-09-08", 10000, 100, 1)], [cal]) === null);
      // dates the calendars do not know: calendar days, said as such
      const r6 = Y.reconcileTrade(v, [fill("BUY", "2026-09-10", 10000, 100, 100), fill("SELL", "2026-09-30", 9600, 100, 100, 100)], []);
      chk("O10 no calendar: days, not sessions", r6.buy.gap.sessions === null && r6.buy.gap.days === 2 && r6.sell.gap.sessions === null && r6.sell.gap.days === 9, [r6.buy.gap, r6.sell.gap]);
      chk("O10 ... and worded as calendar days", Y.reconcileHtml(r6).includes("晚 2 日曆天") && Y.reconcileHtml(r6).includes("晚 9 日曆天"));
      // a fill without fee or tax gives no honest percentage
      const r7 = Y.reconcileTrade(v, [fill("BUY", "2026-09-08", 10000, 100, 100), Object.assign(fill("SELL", "2026-09-21", 9620, 100, 100, 100), { tax_cents: undefined })], [cal]);
      chk("O10 a sell with no tax recorded: no result, and it says why", r7.sell !== null && r7.result === null && Y.reconcileHtml(r7).includes("算不出淨報酬"));
      // the sessions on the trade's own path count too (the app's calendar may not reach back)
      chk("O10 sessionGap uses the first calendar that knows both days", Y.sessionGap("2026-09-08", "2026-09-10", [[], ["2026-09-08", "2026-09-09", "2026-09-10"]]).sessions === 2 &&
         Y.sessionGap("2026-09-08", "2026-09-10", [["2026-09-08", "2026-09-10"], ["2026-09-08", "2026-09-09", "2026-09-10"]]).sessions === 1 &&
         Y.sessionGap("2026-09-08", "bad", [cal]).sessions === null && Y.sessionGap(null, null, null).days === null);
    }

    // ---- O11: the page itself, the owner's private ledger, and the network ----
    {
      const log = [];
      const e2 = await boot(LIVE, { fetchLog: log, recs: { count: 2, format_version: 1, recommendations: [
        rowOf(FIX.time.sid, FIX.time.signal, JSON.stringify(FIX.time.out)),
        rowOf(FIX.stop.sid, FIX.stop.signal, FIX.stop.out),
        rowOf("9999", "2026-09-07", FIX.tp.out, { status: "active" })] } });
      const Y2 = e2.YT;
      Y2.STATE.page = "perf";
      Y2.render();
      for (let i = 0; i < 50 && Y2.STATE.recsState !== "ready"; i++) await tick();
      let page = e2.els["page-perf"].innerHTML;
      chk("O11 the performance page fetches ./recommendations.json once and shows the closed ones",
         Y2.STATE.recsState === "ready" && (page.match(/<article class="card state-closed"/g) || []).length === 2 &&
         log.filter((x) => x.url === "./recommendations.json").length === 1, [Y2.STATE.recsState, log.map((x) => x.url)]);
      chk("O11 the record section sits right under the system record", page.indexOf("系統訊號紀錄") < page.indexOf("已結案建議的完整紀錄") && page.indexOf("已結案建議的完整紀錄") < page.indexOf("已凍結的十日成果"));
      chk("O11 no fills registered: no reconciliation block (no nagging)", !page.includes("對帳"));
      // register the owner's fills against one recommendation
      Y2.STATE.positions = [
        { position_id: "pa", recommendation_id: "rec-5501-mode_prelaunch-1", status: "closed", stock_id: "5501" },
        { position_id: "pv", recommendation_id: "rec-1101-mode_prelaunch-1", status: "void", stock_id: "1101" },
        { position_id: "pm", recommendation_id: null, status: "closed", stock_id: "1101" }];
      const ex = (id, side, date, px, sh, fee, tax) => ({ execution_id: id, position_id: "pa", side, session_date: date, price_cents: px, shares: sh, fee_cents: fee, tax_cents: tax, is_current: 1, executed_at: "", recorded_at: "2026-10-09 10:00:00" });
      Y2.STATE.execsByPos = {
        pa: [ex("e1", "BUY", "2026-09-09", 10045, 1000, 14300, 0), ex("e2", "SELL", "2026-09-23", 9560, 1000, 13600, 28700),
              Object.assign(ex("e3", "BUY", "2026-09-08", 1, 1, 1, 0), { is_current: 0 })],
        pv: [Object.assign(ex("e4", "BUY", "2026-09-08", 9999, 1000, 1, 0), { position_id: "pv" })],
        pm: [Object.assign(ex("e5", "BUY", "2026-09-08", 9998, 1000, 1, 0), { position_id: "pm" })] };
      chk("O11 the fills are matched by recommendation_id, void and unlinked positions are not", Y2.recExecsFor("rec-5501-mode_prelaunch-1").length === 2 && Y2.recExecsFor("rec-1101-mode_prelaunch-1").length === 0 &&
         Y2.recExecsFor(null).length === 0 && Y2.recExecsFor("").length === 0 && Y2.recExecsFor(undefined).length === 0);
      Y2.renderPerf();
      page = e2.els["page-perf"].innerHTML;
      chk("O11 the reconciliation appears under the matching record only", (page.match(/對帳：你的成交 vs 規則/g) || []).length === 1 &&
         page.indexOf("對帳：你的成交 vs 規則") < page.indexOf("T1101") || page.indexOf("T5501") < page.indexOf("對帳：你的成交 vs 規則"), (page.match(/對帳：/g) || []).length);
      const mine = page.slice(page.indexOf("對帳：你的成交 vs 規則"));
      chk("O11 ... with the owner's own numbers", mine.includes("-5.38%") && mine.includes("買得比規則貴") && mine.includes("賣得比規則低"));
      const secOnly = page.slice(page.indexOf("已結案建議的完整紀錄"), page.indexOf("已凍結的十日成果"));
      chk("O11 the record section is free of NaN / undefined / null", secOnly.length > 500 && !JUNK.test(secOnly), (secOnly.match(JUNK) || [])[0]);
      const prod = log.filter((x) => x.url.includes("recommendations")).map((x) => JSON.stringify(x));
      chk("O11 PRIVACY: no request carries a fill, an id or a price - only plain GETs for the app's own files",
         log.every((x) => /^\.\/[a-z_]+\.json$/.test(x.url) && (x.init === null || JSON.stringify(Object.keys(x.init)) === '["cache"]')) &&
         !log.some((x) => /10045|9560|rec-5501|execution/.test(x.url + JSON.stringify(x.init))), log.map((x) => x.url).join(","));
      // the day-by-day table remembers whether the owner opened it, across re-renders
      const rid = "rec-5501-mode_prelaunch-1";
      const toggle = (open) => (e2.listeners.toggle || []).forEach((fn) => fn({ target: {
        matches: (sel) => sel === "details[data-trec]", getAttribute: () => rid, hasAttribute: () => false, open } }));
      chk("O11 a table starts closed", page.includes('data-trec="' + rid + '"') && !page.includes('data-trec="' + rid + '" open'));
      toggle(true);
      Y2.renderPerf();
      page = e2.els["page-perf"].innerHTML;
      chk("O11 opening a table is remembered by the next render", Y2.STATE.recOpen[rid] === true && page.includes('data-trec="' + rid + '" open>'));
      chk("O11 ... and only for that recommendation", !page.includes('data-trec="rec-1101-mode_prelaunch-1" open'));
      toggle(false);
      Y2.renderPerf();
      chk("O11 closing it is remembered too", !e2.els["page-perf"].innerHTML.includes('data-trec="' + rid + '" open'));
      // a refresh re-arms the fetch; a 404 is a state, not a crash
      const e3 = await boot(LIVE, { fetchLog: [] });
      e3.YT.STATE.page = "perf";
      e3.YT.render();
      for (let i = 0; i < 50 && e3.YT.STATE.recsState === "idle"; i++) await tick();
      for (let i = 0; i < 20; i++) await tick();
      chk("O11 a deploy without the file: labelled, no crash", e3.YT.STATE.recsState === "missing" && e3.els["page-perf"].innerHTML.includes("這次部署沒有 recommendations.json"), e3.YT.STATE.recsState);
      const e4 = await boot(LIVE, { recs: "{\"recommendations\": 7}" });
      e4.YT.STATE.page = "perf";
      e4.YT.render();
      for (let i = 0; i < 50 && e4.YT.STATE.recsState !== "missing"; i++) await tick();
      chk("O11 a file of the wrong shape is a missing file, not half a page", e4.YT.STATE.recsState === "missing" && e4.YT.STATE.recs === null);
      const e5 = await boot(LIVE, { recs: JSON.stringify([rowOf(FIX.stop.sid, FIX.stop.signal, FIX.stop.out), 5, null]) });
      e5.YT.STATE.page = "perf";
      e5.YT.render();
      for (let i = 0; i < 50 && e5.YT.STATE.recsState !== "ready"; i++) await tick();
      chk("O11 a bare list is accepted and unusable rows are dropped", e5.YT.STATE.recsState === "ready" && e5.YT.STATE.recs.length === 1);
    }

    // ---- P: the same checks on records the BACKEND builds (--trade-grid) -------
    if (TRADE_GRID) {
      const grid = JSON.parse(fs.readFileSync(TRADE_GRID, "utf8"));
      for (const c of grid.cases) {
        const e = Object.assign({ path: [], live: null, ride_days: 0, decision: null, mfe: null, mae: null }, c.expect);
        const html = checkRecord("P " + c.name, c.rec, e);
        chk("P " + c.name + ": backend and phone agree there is a table", html.includes("<details") === c.expect.has_path);
      }
      for (const c of grid.recon) checkRecon("P " + c.name, c);
      chk("P the backend grid is not empty", grid.cases.length >= 20 && grid.recon.length >= 5, [grid.cases.length, grid.recon.length]);
      tradeCases = grid.cases.length;
      reconCases = grid.recon.length;
    }
  }

  const out = { pass, fail: failures.length, failures, grid_cases: gridCases, plan_cases: planCases,
                trade_cases: tradeCases, recon_cases: reconCases };
  console.log(JSON.stringify(out, null, 1));
  process.exit(failures.length ? 1 : 0);
}

main().catch((e) => {
  console.log(JSON.stringify({ pass, fail: failures.length + 1, failures: failures.concat(["probe crashed: " + e.stack]) }));
  process.exit(1);
});
