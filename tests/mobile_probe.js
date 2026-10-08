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
//     node tests/mobile_probe.js --fee-grid <cases.json>
//
// --fee-grid adds section F: every [schedule, considerationCents, feeCents,
// taxCents] case (computed by portfolio/money.py FeeSchedule in the Python
// test) must come out of the app's feeFor/taxFor identically.
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
const APP_SRC = fs.readFileSync(path.join(ROOT, "mobile", "app.js"), "utf8");
const REPORT_TEXT = JSON.parse(fs.readFileSync(path.join(ROOT, "config", "report_text.json"), "utf8"));
const OLD = JSON.parse(fs.readFileSync(
  path.join(ROOT, "tests", "fixtures", "mobile", "scan_2026-09-23.json"), "utf8"));
const GRID_AT = process.argv.indexOf("--fee-grid");
const FEE_GRID = GRID_AT > 0 ? process.argv[GRID_AT + 1] : null;
let gridCases = 0;
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
    addEventListener() {},
    visibilityState: "visible",
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
  const YT = sandbox.YT;
  for (let i = 0; i < 500 && !YT.STATE.loadedAt; i++) await tick();
  return { YT, els, store };
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

  const out = { pass, fail: failures.length, failures, grid_cases: gridCases };
  console.log(JSON.stringify(out, null, 1));
  process.exit(failures.length ? 1 : 0);
}

main().catch((e) => {
  console.log(JSON.stringify({ pass, fail: failures.length + 1, failures: failures.concat(["probe crashed: " + e.stack]) }));
  process.exit(1);
});
