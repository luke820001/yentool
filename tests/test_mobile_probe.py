"""
Run tests/mobile_probe.js, which boots the WHOLE phone app (mobile/app.js) in
a node vm context with a stub DOM and renders every page against three
payloads: the 2026-09-23 publish trimmed into
tests/fixtures/mobile/scan_2026-09-23.json (an OLD payload, no B1-B6 fields),
the 2026-10-07 publish trimmed into tests/fixtures/mobile/scan_2026-10-07.json,
and a synthetic payload carrying every field stages B1-B6 added (list freeze,
restrictions, Prev_*, Rec_Status_Reason, events, report sources, live_record
bench / by_sid).

It is the proof that the 2026-10-08 investor views degrade on an old payload
and that their arithmetic (fees, sizing) matches the backend's pins. The fee
grid (section F of the probe) is computed here with
portfolio.money.FeeSchedule and must come out of the app's feeFor/taxFor
unchanged, case by case.

mobile/scan_result.json is gitignored (CI never has it); the probe does not
read it. When it exists locally the test still checks it was not written.

Skipped when node is not installed, so a Python-only environment still runs
the rest of the suite. No network.

    python -m unittest tests.test_mobile_probe -v
"""
import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from portfolio.money import FeeSchedule

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "mobile_probe.js"
SCAN = ROOT / "mobile" / "scan_result.json"
NODE = shutil.which("node")

# tick bands of the TWSE/TPEX ladder: (low, high, tick)
_BANDS = ((1, 10, "0.01"), (10, 50, "0.05"), (50, 100, "0.1"),
          (100, 500, "0.5"), (500, 1000, "1"), (1000, 1500, "5"))


def _digest(path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fee_grid(seed=20261008, prices_per_band=40, shares_per_price=12):
    """[[schedule, considerationCents, feeCents, taxCents], ...] from
    portfolio/money.py, both schedules, prices across every tick band
    (1-1500), shares sampled from 1-5000, plus the two regression pins
    (20.35 x 1000 -> fee 28; 10.10 x 99 -> tax 2) and the min-fee edge."""
    rnd = random.Random(seed)
    prices = []
    for lo, hi, tick in _BANDS:
        t = Decimal(tick)
        steps = int((Decimal(hi) - Decimal(lo)) / t)
        for k in rnd.sample(range(steps), prices_per_band):
            prices.append(Decimal(lo) + t * k)
    pins = [(Decimal("20.35"), 1000), (Decimal("10.10"), 99),
            (Decimal("140.35"), 100), (Decimal("140.36"), 100),
            (Decimal("1500"), 5000), (Decimal("1"), 1)]
    cases = []
    for sched in (FeeSchedule.default(), FeeSchedule.exact()):
        pairs = list(pins)
        for p in prices:
            for n in rnd.sample(range(1, 5001), shares_per_price):
                pairs.append((p, n))
        for p, n in pairs:
            cons = int((p * 100).to_integral_value()) * n
            cases.append([sched.version, cons,
                          int(sched.buy_fee(p, n) * 100),
                          int(sched.sell_tax(p, n) * 100)])
            assert sched.sell_fee(p, n) == sched.buy_fee(p, n)
    return cases


def plan_grid(seed=20261009):
    """The BACKEND's answers, for the phone to reproduce (probe section M):

      levels  [stock_id, fill_cents, key, level_cents] from
              holding_tracker._lvl (round_to_tick of fill x (1 + pct)), both
              ladders, every cent of 1.00-30.00 plus samples up to 3000;
      armed   [stock_id, fill_cents, close_cents, armed] from
              exit_rules.replay_exit, closes within three ticks of the exact
              +2.5% threshold;
      ride    series of closes, the market leg per date and what replay_exit
              (hold 10, cap 20, extend_if) says. `complete` = the map carries
              every date; otherwise some dates are unknown, which the backend
              reads as 'off' and the phone as 'cannot prove it broke'.
    """
    import datetime
    from scanner import holding_tracker as ht
    from scanner.exit_rules import replay_exit
    from scanner.tick import is_on_tick, round_to_tick

    rnd = random.Random(seed)
    spec = {"stop": (-ht.STOP_PCT, "down"), "lock": (ht.TRAIL_LOCK, "down"),
            "add": (-ht.ADD_PCT, "down"), "arm": (ht.TRAIL_ARM, "up"),
            "target": (ht.TP_PCT, "up"), "scale": (ht.SCALE_OUT_PCT, "up")}
    levels = []
    for sid, hi in (("6488", 100000), ("0050", 30000), ("00878", 30000)):
        cents = list(range(100, 3001))
        cents += rnd.sample(range(3001, hi + 1), 1500)
        for c in cents:
            fill = c / 100.0
            for key, (pct, direction) in spec.items():
                levels.append([sid, c, key,
                               int(round(ht._lvl(fill, pct, direction, sid) * 100))])
            levels.append([sid, c, "late", int(round(
                round_to_tick(fill * (1 + ht.LATE_GAIN), "up", sid) * 100))])

    armed = []
    for sid in ("6488", "0050"):
        for base in rnd.sample(range(100, 20001), 500):
            exact = base * (1 + ht.TRAIL_ARM)
            first = int(exact) - 6
            for close in range(first, first + 14):
                if close < 1 or not is_on_tick(close / 100.0, sid):
                    continue
                res = replay_exit([base / 100.0], [close / 100.0], [close / 100.0],
                                  [close / 100.0])
                armed.append([sid, base, close, bool(res["armed"])])

    days = [(datetime.date(2026, 9, 1) + datetime.timedelta(days=k)).isoformat()
            for k in range(20)]
    ride = []
    while len(ride) < 900:
        n = rnd.randint(10, 20)
        closes, c = [], 10000
        for _ in range(n):
            step = 0 if rnd.random() < 0.2 else rnd.randint(-80, 90)
            c = min(max(c + step, 9300), 10095)     # under the late +1% line
            closes.append(c)
        opens = [10000] + closes[:-1]
        leg = {d: rnd.random() < 0.35 for d in days[:n]}
        complete = len(ride) % 2 == 0
        if not complete:
            for d in rnd.sample(days[:n], max(1, n // 4)):
                leg.pop(d)
        res = replay_exit([o / 100.0 for o in opens],
                          [max(o, c) / 100.0 + 0.1 for o, c in zip(opens, closes)],
                          [min(o, c) / 100.0 - 0.1 for o, c in zip(opens, closes)],
                          [c / 100.0 for c in closes], dates=days[:n], hold_bars=10,
                          ride_cap=20, extend_if=lambda i, d, m=leg: m.get(d, False))
        if res["exited"] and res["reason"] == "time":
            riding, exit_day = False, res["bar"] + 1
        elif res["riding"] and not res["exited"]:
            riding, exit_day = True, None
        else:
            continue
        ride.append({"closes": closes, "dates": days[:n], "leg": leg,
                     "complete": complete, "riding": riding, "exit_day": exit_day})
    return {"levels": levels, "armed": armed, "ride": ride}


class TheProbeNeedsNoLocalPayload(unittest.TestCase):
    def test_old_payload_is_a_committed_fixture(self):
        # mobile/scan_result.json is gitignored: a probe that read it would
        # crash in CI. The 09-23 publish lives in tests/fixtures instead.
        src = PROBE.read_text(encoding="utf-8")
        self.assertNotIn('"mobile", "scan_result.json"', src)
        fx = ROOT / "tests" / "fixtures" / "mobile" / "scan_2026-09-23.json"
        self.assertIn('"scan_2026-09-23.json"', src)
        data = json.loads(fx.read_text(encoding="utf-8"))
        self.assertEqual(data["meta"]["scan_time"], "2026-09-23 19:33:57")
        self.assertEqual(len(data["rows"]), 41)
        self.assertNotIn("bench", data["meta"]["live_record"])
        self.assertNotIn("list_status", data["meta"])


class FeeGridIsWhatTheTestSays(unittest.TestCase):
    def test_pins_in_the_python_grid(self):
        by = {(c[0], c[1]): c for c in fee_grid()}
        self.assertEqual(by[("tw-equity-v1", 2035000)][2], 2800)
        self.assertEqual(by[("tw-equity-v1", 99990)][3], 200)
        self.assertEqual(by[("tw-equity-v1", 1403500)][2], 2000)
        self.assertEqual(by[("tw-equity-exact", 2035000)][2], 2900)


@unittest.skipIf(NODE is None, "node is not installed")
class MobileAppRendersEveryPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = fee_grid()
        cls.plan = plan_grid()
        fd, cls.grid_path = tempfile.mkstemp(suffix=".json", prefix="yt_fee_grid_")
        with os.fdopen(fd, "w") as fh:
            json.dump(cls.cases, fh)
        fd, cls.plan_path = tempfile.mkstemp(suffix=".json", prefix="yt_plan_grid_")
        with os.fdopen(fd, "w") as fh:
            json.dump(cls.plan, fh)
        cls.before = _digest(SCAN)
        try:
            cls.run_ = subprocess.run([NODE, str(PROBE), "--fee-grid", cls.grid_path,
                                       "--plan-grid", cls.plan_path],
                                      cwd=str(ROOT), capture_output=True, timeout=300)
        finally:
            os.remove(cls.grid_path)
            os.remove(cls.plan_path)
        cls.after = _digest(SCAN)
        text = cls.run_.stdout.decode("utf-8", "replace")
        cls.err = cls.run_.stderr.decode("utf-8", "replace")
        try:
            cls.out = json.loads(text)
        except ValueError:
            cls.out = {"pass": 0, "fail": -1,
                       "failures": ["unparseable probe output: " + text[-2000:]]}

    def test_every_probe_check_passes(self):
        self.assertEqual(
            self.out.get("failures"), [],
            "mobile probe failures:\n%s\n%s" % (
                "\n".join(self.out.get("failures") or []), self.err[-2000:]))
        self.assertEqual(self.run_.returncode, 0, self.err[-2000:])

    def test_the_probe_actually_ran_its_checks(self):
        self.assertGreaterEqual(self.out.get("pass", 0), 100)

    def test_the_fee_grid_was_checked_case_by_case(self):
        self.assertGreater(len(self.cases), 5000)
        self.assertEqual(self.out.get("grid_cases"), len(self.cases))

    def test_the_plan_grid_was_checked_against_the_backend(self):
        want = len(self.plan["levels"]) + len(self.plan["armed"]) + len(self.plan["ride"])
        self.assertGreater(len(self.plan["levels"]), 40000)
        self.assertEqual(self.out.get("plan_cases"), want)

    def test_the_local_payload_is_never_written(self):
        # gitignored, so absent in CI: both digests are then None
        self.assertEqual(self.before, self.after,
                         "the probe must never write mobile/scan_result.json")


if __name__ == "__main__":
    unittest.main()
