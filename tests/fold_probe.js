// Run the ledger's pure fold helpers for real, outside a browser.
//
// mobile/app.js cannot be imported: it touches window, navigator and document
// at load. So the handful of PURE functions are cut out by brace matching and
// evaluated on their own. That is enough to check the arithmetic the editable
// archive depends on, which source-level assertions cannot.
//
//     node tests/fold_probe.js          (run from the repo root)
//
// Driven by tests/test_fold_probe.py, which skips when node is unavailable.
const fs = require("fs");
const path = require("path");

const APP = path.join(__dirname, "..", "mobile", "app.js");
const src = fs.readFileSync(APP, "utf8");

function grab(sig) {
  const i = src.indexOf(sig);
  if (i < 0) throw new Error("not found: " + sig);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    const ch = src[j];
    if (ch === "{") { depth++; started = true; }
    else if (ch === "}") { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error("unbalanced: " + sig);
}

function line(re) {
  const m = src.match(re);
  if (!m) throw new Error("not found: " + re);
  return m[0];
}

const code = [
  line(/^const SUB = .*$/m),
  grab("function divRound("),
  line(/^const toSub = .*$/m),
  line(/^const fromSub = .*$/m),
  grab("function sortExecutions("),
  grab("function replay("),
  grab("function plannedExecutions("),
  grab("function heldBefore("),
  grab("function firstOversell("),
].join("\n");
eval(code);

const E = (id, side, date, shares, price) => ({
  execution_id: id, side, session_date: date, executed_at: "", recorded_at: date,
  shares, price_cents: price * 100, fee_cents: 0, tax_cents: 0, is_current: 1,
});

let pass = 0;
const failures = [];
function ok(name, cond, extra) {
  if (cond) pass++;
  else failures.push(name + (extra === undefined ? "" : "  (got " + extra + ")"));
}

// --- the bound for a SELL -------------------------------------------------
// A closed round trip. open_shares is 0, which is what the old code used.
const round = [E("b1", "BUY", "2026-09-01", 2000, 100),
               E("s1", "SELL", "2026-09-05", 2000, 120)];
ok("a closed round trip leaves zero shares", replay(round, null).shares === 0);

let p = plannedExecutions(round, "s1", E("__probe__", "SELL", "2026-09-05", 0, 0));
ok("the bound for the closing sell is 2000, not 0",
   heldBefore(p.list, p.index) === 2000, heldBefore(p.list, p.index));

// A staged exit on a live position: the same bug, before any archiving.
const staged = [E("b1", "BUY", "2026-09-01", 2000, 100),
                E("s1", "SELL", "2026-09-05", 1000, 120)];
p = plannedExecutions(staged, "s1", E("__probe__", "SELL", "2026-09-05", 0, 0));
ok("the bound for a partial sell is the shares held at that moment",
   heldBefore(p.list, p.index) === 2000, heldBefore(p.list, p.index));

// A backfill appends rather than replaces.
p = plannedExecutions(staged, null, E("__new__", "SELL", "2026-09-09", 0, 0));
ok("a backfilled sell is bounded by what is left", heldBefore(p.list, p.index) === 1000,
   heldBefore(p.list, p.index));

// --- the fills AFTER the edited one ---------------------------------------
const two = [E("b1", "BUY", "2026-09-01", 2000, 100),
             E("s1", "SELL", "2026-09-03", 1000, 110),
             E("s2", "SELL", "2026-09-08", 1000, 120)];
ok("a clean history has no oversell", firstOversell(two) === null);

p = plannedExecutions(two, "s1", E("__new__", "SELL", "2026-09-03", 2000, 110));
let bad = firstOversell(p.list);
ok("amending the first sell up to 2000 is caught",
   bad !== null && bad.exe.execution_id === "s2", bad && bad.exe.execution_id);
ok("the refusal reports the shares held at the conflict",
   bad !== null && bad.held === 0, bad && bad.held);

// The mirror case a per-row bound cannot see.
p = plannedExecutions(two, "b1", E("__new__", "BUY", "2026-09-01", 1000, 100));
bad = firstOversell(p.list);
ok("amending the earlier buy down is caught",
   bad !== null && bad.exe.execution_id === "s2", bad && bad.exe.execution_id);

// What the unguarded fold actually does to the money, which is why it matters.
const broken = [E("b1", "BUY", "2026-09-01", 1000, 100),
                E("s1", "SELL", "2026-09-03", 1000, 110),
                E("s2", "SELL", "2026-09-08", 1000, 120)];
ok("an unguarded fold silently drops the later sell entirely",
   replay(broken, null).realized_net === replay(broken.slice(0, 2), null).realized_net,
   replay(broken, null).realized_net + " vs " + replay(broken.slice(0, 2), null).realized_net);

// --- voiding ---------------------------------------------------------------
const twoBuys = [E("b1", "BUY", "2026-09-01", 1000, 100),
                 E("b2", "BUY", "2026-09-02", 1000, 105),
                 E("s1", "SELL", "2026-09-05", 2000, 120)];
ok("voiding nothing is fine", firstOversell(twoBuys) === null);
ok("voiding one of two buys behind one sell is caught",
   firstOversell(twoBuys.filter((e) => e.execution_id !== "b1")) !== null);
ok("voiding the sell is always fine",
   firstOversell(twoBuys.filter((e) => e.execution_id !== "s1")) === null);

// --- ordering --------------------------------------------------------------
// The probe carries zero shares, so it must sort where the real row will.
const outOfOrder = [E("b1", "BUY", "2026-09-05", 1000, 100),
                    E("b0", "BUY", "2026-09-01", 1000, 90)];
p = plannedExecutions(outOfOrder, null, E("__probe__", "SELL", "2026-09-03", 0, 0));
ok("a probe sorts by its date, not its arrival", heldBefore(p.list, p.index) === 1000,
   heldBefore(p.list, p.index));

if (failures.length) {
  failures.forEach((f) => console.log("FAIL " + f));
  console.log(failures.length + " FAILED of " + (pass + failures.length));
  process.exit(1);
}
console.log("ALL " + pass + " FOLD CHECKS PASS");
