"""
sandbox_lock_delay.py -- when does the trailing lock actually protect you?
ASCII only.

The shipped exit stack arms the +2% lock the moment a bar's HIGH touches +6%,
and lets that same bar be stopped on it (exit_rules.replay_exit, event order
step 2). Nobody trades that: the owner reads the payload after the close and
places orders for the next session. A lock the backtest exercises intraday on
the arming day is a lock the live trade never had.

Owner's decision 2026-09-21: the lock takes effect the NEXT session. This
script measures what that costs or earns, on the same 564 CORE+ first-day
trades as the stop/ladder study (%TEMP%/yentool_ladder/picks.pkl), and then
re-searches the arm/lock pair under the corrected engine -- the pair was
selected on the old simulator, which EVAL_PLAYBOOK already flags as chosen on
a defective event order.

Engines:
  live      exit_rules.replay_exit as shipped (same-bar lock)
  eod       shipped, but a same-bar lock exit is taken at the NEXT open --
            what the owner would really have done with the payload's advice
  hi_next   arming detected on the bar's HIGH, lock guards from the next bar
  close_next  arming detected on the bar's CLOSE, lock guards from the next
            bar -- the definition the phone already uses (it only has closes)

Commands:
  python archive/research/sandbox_lock_delay.py engines   # the four, paired
  python archive/research/sandbox_lock_delay.py grid      # arm/lock search
  python archive/research/sandbox_lock_delay.py gate      # adoption gate
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.getcwd())
from scanner.exit_rules import replay_exit  # noqa: E402

CACHE_DIR = os.environ.get("LADDER_CACHE", os.path.join(
    os.environ.get("TEMP", "."), "yentool_ladder"))
PICKS_PKL = os.path.join(CACHE_DIR, "picks.pkl")

RECENT_FROM = "2023-09-18"
HOLD = 10
BUY_COST = 0.001425
SELL_COST = 0.001425 + 0.003
STOP, TP, ARM, LOCK = 0.20, 0.20, 0.06, 0.02
SEED = 7


def load_trades():
    t = pickle.load(open(PICKS_PKL, "rb"))
    return [x for x in t if len(x["o"]) >= HOLD]


def net_ret(entry, exit_px):
    return (exit_px * (1 - SELL_COST) / (entry * (1 + BUY_COST)) - 1) * 100


# ------------------------------------------------------------------ engines
def run_live(t, hold=HOLD, stop=STOP, tp=TP, arm=ARM, lock=LOCK):
    o, h, l, c = (t[x][:hold] for x in ("o", "h", "l", "c"))
    p = replay_exit(o, h, l, c, hold_bars=hold, stop_pct=stop, tp_pct=tp,
                    arm_pct=arm, lock_pct=lock)
    return p["reason"], net_ret(o[0], p["exit_price"]), p["bar"]


def run_eod(t, hold=HOLD, stop=STOP, tp=TP, arm=ARM, lock=LOCK):
    """live, but a lock booked on the arming bar is executed at the next open."""
    o, h, l, c = (t[x][:hold] for x in ("o", "h", "l", "c"))
    p = replay_exit(o, h, l, c, hold_bars=hold, stop_pct=stop, tp_pct=tp,
                    arm_pct=arm, lock_pct=lock)
    b = p["bar"]
    arm_px = float(o[0]) * (1 + arm)
    same_bar = (p["reason"] == "lock" and b is not None
                and all(float(h[i]) < arm_px for i in range(b))
                and float(h[b]) >= arm_px)
    if same_bar and b + 1 < len(t["o"]):
        px = float(t["o"][b + 1])
        return "lock", net_ret(o[0], px), b + 1
    return p["reason"], net_ret(o[0], p["exit_price"]), b


def run_delayed(t, on="high", hold=HOLD, stop=STOP, tp=TP, arm=ARM, lock=LOCK):
    """Arming is observed on `on` (high or close) and the raised stop only
    guards from the NEXT bar. Same event order otherwise: the open first,
    then the lowest level the bar actually touched, then the target."""
    o, h, l, c = (t[x][:hold] for x in ("o", "h", "l", "c"))
    E = float(o[0])
    stop_px = E * (1 - stop) if stop is not None else None
    target = E * (1 + tp) if tp is not None else None
    arm_px = E * (1 + arm) if arm is not None else None
    lock_px = E * (1 + lock)
    armed = False
    n = len(o)
    for i in range(n):
        op, hi, lo, cl = float(o[i]), float(h[i]), float(l[i]), float(c[i])
        if target is not None and op >= target:
            return "tp", net_ret(E, op), i
        if stop_px is not None and op <= stop_px:
            return ("lock" if armed else "stop"), net_ret(E, op), i
        if stop_px is not None and lo <= stop_px:
            return ("lock" if armed else "stop"), net_ret(E, stop_px), i
        if target is not None and hi >= target:
            return "tp", net_ret(E, target), i
        # arming is only observed at the END of the bar, and the raised stop
        # is placed for the next session.
        if arm_px is not None and not armed:
            seen = hi if on == "high" else cl
            if seen >= arm_px:
                armed = True
                stop_px = lock_px if stop_px is None else max(stop_px, lock_px)
    return "time", net_ret(E, float(c[n - 1])), n - 1


ENGINES = {
    "live": run_live,
    "eod": run_eod,
    "hi_next": lambda t, **k: run_delayed(t, on="high", **k),
    "close_next": lambda t, **k: run_delayed(t, on="close", **k),
}


# ------------------------------------------------------------------ stats
def summarize(rets):
    r = np.asarray(rets, float)
    return 100 * (r > 0).mean(), r.mean(), np.median(r), np.percentile(r, 10)


def boot_lo(rets, sigs, iters=2000, seed=SEED):
    """2.5th percentile of the win rate, resampling signal DAYS (trades on one
    day are not independent)."""
    rng = np.random.default_rng(seed)
    days = pd.Series(sigs)
    groups = [np.asarray(rets, float)[(days == d).to_numpy()] for d in days.unique()]
    wins = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        pooled = np.concatenate([groups[i] for i in pick])
        wins.append(100 * (pooled > 0).mean())
    return np.percentile(wins, 2.5)


def paired_ci(a, b, sigs, iters=2000, seed=SEED):
    """95% CI of mean(b) - mean(a), clustered by signal day."""
    rng = np.random.default_rng(seed)
    d = np.asarray(b, float) - np.asarray(a, float)
    days = pd.Series(sigs)
    groups = [d[(days == x).to_numpy()] for x in days.unique()]
    out = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        out.append(np.concatenate([groups[i] for i in pick]).mean())
    return np.percentile(out, 2.5), np.percentile(out, 97.5)


def windows(sigs):
    s = np.asarray(sigs)
    return [("RECENT", s >= RECENT_FROM), ("OLD", s < RECENT_FROM),
            ("ALL", np.ones(len(s), bool))]


def halves(mask, sigs):
    idx = np.where(mask)[0]
    order = idx[np.argsort(np.asarray(sigs)[idx])]
    h = len(order) // 2
    return order[:h], order[h:]


# ------------------------------------------------------------------ commands
def cmd_engines():
    trades = load_trades()
    sigs = [t["sig"] for t in trades]
    print("trades %d (%s..%s)\n" % (len(trades), min(sigs), max(sigs)))
    results = {}
    for name, fn in ENGINES.items():
        out = [fn(t) for t in trades]
        results[name] = (np.array([r[1] for r in out]),
                         pd.Series([r[0] for r in out]))
    base = results["live"][0]
    for name, (rets, reasons) in results.items():
        print("== %s" % name)
        for wname, m in windows(sigs):
            w, mu, med, p10 = summarize(rets[m])
            lo = boot_lo(rets[m], np.asarray(sigs)[m])
            mix = reasons[m].value_counts(normalize=True).round(2).to_dict()
            line = ("  %-7s n=%3d win %5.1f%% (bootLo %4.1f) mean %+5.2f med %+5.2f "
                    "p10 %+6.2f  %s" % (wname, m.sum(), w, lo, mu, med, p10, mix))
            print(line)
        if name != "live":
            for wname, m in windows(sigs)[:2]:
                ci = paired_ci(base[m], rets[m], np.asarray(sigs)[m])
                print("    vs live %-6s dwin %+5.1fpp dmean %+5.2f (CI %+.2f..%+.2f)"
                      % (wname, summarize(rets[m])[0] - summarize(base[m])[0],
                         rets[m].mean() - base[m].mean(), ci[0], ci[1]))
        print()


def _grid_rows(trades, sigs, engine, arms, locks, stop=STOP, tp=TP):
    fn = ENGINES[engine]
    rows = []
    for arm in arms:
        for lock in locks:
            if lock is not None and arm is not None and lock >= arm:
                continue
            rets = np.array([fn(t, stop=stop, tp=tp, arm=arm, lock=lock)[1]
                             for t in trades])
            row = {"arm": arm, "lock": lock}
            for wname, m in windows(sigs):
                w, mu, med, p10 = summarize(rets[m])
                row[wname + "_win"] = w
                row[wname + "_mean"] = mu
                row[wname + "_p10"] = p10
            row["RECENT_lo"] = boot_lo(rets[windows(sigs)[0][1]],
                                       np.asarray(sigs)[windows(sigs)[0][1]])
            h1, h2 = halves(windows(sigs)[0][1], sigs)
            row["h1"] = 100 * (rets[h1] > 0).mean()
            row["h2"] = 100 * (rets[h2] > 0).mean()
            rows.append(row)
    return pd.DataFrame(rows)


def cmd_grid():
    trades = load_trades()
    sigs = [t["sig"] for t in trades]
    arms = [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12]
    locks = [0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
    for engine in ("close_next", "hi_next"):
        print("\n=== %s: arm/lock grid (stop %.2f tp %.2f hold %d) ===" % (
            engine, STOP, TP, HOLD))
        g = _grid_rows(trades, sigs, engine, arms, locks)
        g = g.sort_values("RECENT_win", ascending=False)
        print(g.to_string(index=False, float_format=lambda x: "%6.2f" % x))
        print("-- no lock at all (arm disabled):")
        fn = ENGINES[engine]
        rets = np.array([fn(t, arm=None)[1] for t in trades])
        for wname, m in windows(sigs):
            print("   %-7s win %5.1f%% mean %+5.2f" % ((wname,) + summarize(rets[m])[:2]))


def cmd_wide():
    """Extend the arm axis below the first grid's edge (close_next kept
    improving down to 0.03, which is a boundary, not a plateau) and sweep the
    stop as well -- it was chosen on the same defective simulator."""
    trades = load_trades()
    sigs = [t["sig"] for t in trades]
    for engine in ("close_next", "hi_next"):
        print()
        print("=== %s: arm axis extended (stop %.2f) ===" % (engine, STOP))
        fn = ENGINES[engine]
        for arm in (0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05, 0.06):
            for lock in (0.01, 0.02):
                if lock >= arm:
                    continue
                rets = np.array([fn(t, arm=arm, lock=lock)[1] for t in trades])
                cells = []
                for wname, m in windows(sigs)[:2]:
                    w, mu, med, p10 = summarize(rets[m])
                    cells.append("%s %5.1f%% %+5.2f" % (wname, w, mu))
                h1, h2 = halves(windows(sigs)[0][1], sigs)
                print("  arm %.3f lock %.2f | %s | %s | bootLo %4.1f | h1 %4.1f h2 %4.1f"
                      % (arm, lock, cells[0], cells[1],
                         boot_lo(rets[windows(sigs)[0][1]],
                                 np.asarray(sigs)[windows(sigs)[0][1]]),
                         100 * (rets[h1] > 0).mean(), 100 * (rets[h2] > 0).mean()))
    print()
    print("=== stop sweep at the best arm/lock per engine ===")
    for engine, arm, lock in (("close_next", 0.03, 0.02), ("hi_next", 0.05, 0.03)):
        fn = ENGINES[engine]
        print("-- %s arm %.2f lock %.2f" % (engine, arm, lock))
        for stop in (0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30, None):
            rets = np.array([fn(t, stop=stop, arm=arm, lock=lock)[1] for t in trades])
            cells = []
            for wname, m in windows(sigs)[:2]:
                w, mu, med, p10 = summarize(rets[m])
                cells.append("%s %5.1f%% %+5.2f p10 %+6.2f" % (wname, w, mu, p10))
            print("   stop %-5s | %s | %s" % (stop, cells[0], cells[1]))
    print()
    print("=== take-profit sweep (close_next arm 0.03 lock 0.02) ===")
    fn = ENGINES["close_next"]
    for tp in (0.10, 0.15, 0.20, 0.30, None):
        rets = np.array([fn(t, tp=tp, arm=0.03, lock=0.02)[1] for t in trades])
        cells = []
        for wname, m in windows(sigs)[:2]:
            w, mu, med, p10 = summarize(rets[m])
            cells.append("%s %5.1f%% %+5.2f" % (wname, w, mu))
        print("   tp %-5s | %s | %s" % (tp, cells[0], cells[1]))


def cmd_slip():
    """Is a tight lock real, or an artefact of assuming perfect fills?

    A lock that sits +1% above the fill turns a trade into a scalp: the win is
    a fraction of a percent after the 0.585% round trip, so anything that eats
    a few tenths -- a stop order filling below its trigger, a thin OTC book --
    can flip the sign of the whole win. The sweep re-prices every STOP-side
    exit (stop and lock, the orders that execute into a falling market)
    `slip` worse and re-reads the win rate. A parameter whose win rate is
    real survives; one that is buying wins a tenth of a percent at a time
    collapses.
    """
    trades = load_trades()
    sigs = [t["sig"] for t in trades]
    fn = ENGINES["close_next"]
    print("=== close_next: win rate vs slippage on stop/lock fills ===")
    print("%-18s %s" % ("arm/lock", "  ".join("slip %.1f%%" % (s * 100)
                                              for s in (0, 0.002, 0.005, 0.01))))
    for arm, lock in ((0.02, 0.01), (0.025, 0.02), (0.03, 0.02), (0.035, 0.02),
                      (0.04, 0.02), (0.05, 0.02), (0.06, 0.02), (0.06, 0.03),
                      (0.08, 0.04)):
        cells = []
        for slip in (0.0, 0.002, 0.005, 0.01):
            rets = []
            for t in trades:
                reason, r, bar = fn(t, arm=arm, lock=lock)
                if reason in ("stop", "lock") and slip:
                    # re-price that exit `slip` lower
                    E = float(t["o"][0])
                    px = (r / 100 + 1) * E * (1 + BUY_COST) / (1 - SELL_COST)
                    r = net_ret(E, px * (1 - slip))
                rets.append(r)
            rets = np.array(rets)
            m = windows(sigs)[0][1]
            cells.append("%5.1f/%+5.2f" % (summarize(rets[m])[0], rets[m].mean()))
        print("  arm %.3f lock %.2f  %s" % (arm, lock, "  ".join(cells)))
    print("  (each cell: RECENT win%% / mean%%; slip applies to stop+lock exits only)")


def cmd_local():
    """Map the surface around the slippage-robust corner (lock >= 0.02) with
    the slippage stress applied, so a point is only reported as good if it is
    good under a 0.5% worse fill on every stop/lock exit."""
    trades = load_trades()
    sigs = [t["sig"] for t in trades]
    fn = ENGINES["close_next"]

    def run(arm, lock, slip=0.0):
        out = []
        for t in trades:
            reason, r, bar = fn(t, arm=arm, lock=lock)
            if reason in ("stop", "lock") and slip:
                E = float(t["o"][0])
                px = (r / 100 + 1) * E * (1 + BUY_COST) / (1 - SELL_COST)
                r = net_ret(E, px * (1 - slip))
            out.append(r)
        return np.array(out)

    print("=== close_next: local surface (each cell win%% / mean; s5 = with 0.5%% slippage) ===")
    for lock in (0.015, 0.02, 0.025, 0.03):
        for arm in (lock + 0.005, lock + 0.0075, lock + 0.01, lock + 0.015,
                    lock + 0.02, lock + 0.03):
            arm = round(arm, 4)
            r0 = run(arm, lock)
            r5 = run(arm, lock, 0.005)
            cells = []
            for rets, tag in ((r0, ""), (r5, "s5 ")):
                for wname, m in windows(sigs)[:2]:
                    w, mu, _, _ = summarize(rets[m])
                    cells.append("%s%s %5.1f/%+5.2f" % (tag, wname[:3], w, mu))
            h1, h2 = halves(windows(sigs)[0][1], sigs)
            print("  arm %.4f lock %.3f | %s | bootLo %4.1f h1 %4.1f h2 %4.1f"
                  % (arm, lock, " ".join(cells),
                     boot_lo(r0[windows(sigs)[0][1]],
                             np.asarray(sigs)[windows(sigs)[0][1]]),
                     100 * (r0[h1] > 0).mean(), 100 * (r0[h2] > 0).mean()))
        print()


def cmd_gate():
    """The project's adoption gate for a candidate (arm, lock) under the
    corrected engine: win / bootLo / h1 / h2 in RECENT and OLD, plus quarterly
    stability, against the CURRENT rule measured on the SAME engine (so the
    comparison is about the parameters, not about the engine change)."""
    trades = load_trades()
    sigs = np.asarray([t["sig"] for t in trades])
    engine = os.environ.get("ENGINE", "close_next")
    fn = ENGINES[engine]
    base = np.array([fn(t)[1] for t in trades])
    cands = [(a, l) for a in (0.04, 0.05, 0.06, 0.07, 0.08)
             for l in (0.01, 0.02, 0.03, 0.04) if l < a]
    print("=== adoption gate, engine=%s, base arm %.2f lock %.2f ===" % (engine, ARM, LOCK))
    for arm, lock in cands:
        rets = np.array([fn(t, arm=arm, lock=lock)[1] for t in trades])
        ok = True
        parts = []
        for wname, m in windows(sigs)[:2]:
            bw = summarize(base[m])[0]
            cw = summarize(rets[m])[0]
            blo = boot_lo(base[m], sigs[m])
            clo = boot_lo(rets[m], sigs[m])
            h1, h2 = halves(m, sigs)
            bh = (100 * (base[h1] > 0).mean(), 100 * (base[h2] > 0).mean())
            ch = (100 * (rets[h1] > 0).mean(), 100 * (rets[h2] > 0).mean())
            passed = (cw >= bw and clo >= blo and ch[0] >= bh[0] and ch[1] >= bh[1])
            ok = ok and passed
            parts.append("%s win %5.1f/%5.1f lo %4.1f/%4.1f h1 %4.1f/%4.1f h2 %4.1f/%4.1f %s"
                         % (wname, cw, bw, clo, blo, ch[0], bh[0], ch[1], bh[1],
                            "PASS" if passed else "fail"))
        q = pd.Series(sigs).str.slice(0, 4) + "Q" + \
            ((pd.Series(sigs).str.slice(5, 7).astype(int) - 1) // 3 + 1).astype(str)
        qd = pd.DataFrame({"q": q, "b": base > 0, "c": rets > 0})
        agg = qd.groupby("q").mean()
        nq = int((agg["c"] >= agg["b"]).sum())
        mean_delta = rets.mean() - base.mean()
        print("  arm %.2f lock %.2f | %s | %s | quarters %d/%d | dmean %+5.2f | %s"
              % (arm, lock, parts[0], parts[1], nq, len(agg), mean_delta,
                 "ADOPTABLE" if ok and nq == len(agg) else "no"))


def cmd_final():
    """Decide between the plateau's interior and its best corner: exit mix,
    quarterly stability and the paired difference, all on close_next, all
    against the CURRENT arm/lock measured on the SAME engine (so the
    comparison is about the parameters and not about the engine change)."""
    trades = load_trades()
    sigs = np.asarray([t["sig"] for t in trades])
    fn = ENGINES["close_next"]

    def run(arm, lock):
        out = [fn(t, arm=arm, lock=lock) for t in trades]
        return np.array([r[1] for r in out]), pd.Series([r[0] for r in out])

    base, base_mix = run(ARM, LOCK)
    cands = [(0.025, 0.020), (0.030, 0.020), (0.030, 0.025), (0.035, 0.025),
             (0.025, 0.015), (0.035, 0.020)]
    q = pd.Series(sigs).str.slice(0, 4) + "Q" +         ((pd.Series(sigs).str.slice(5, 7).astype(int) - 1) // 3 + 1).astype(str)
    print("=== close_next, current arm %.2f lock %.2f as the baseline ===" % (ARM, LOCK))
    for wname, m in windows(sigs)[:2]:
        w, mu, _, p10 = summarize(base[m])
        print("  base %-7s win %5.1f%% mean %+5.2f p10 %+6.2f  %s"
              % (wname, w, mu, p10, base_mix[m].value_counts(normalize=True).round(2).to_dict()))
    print()
    for arm, lock in cands:
        rets, mix = run(arm, lock)
        line = []
        allpass = True
        for wname, m in windows(sigs)[:2]:
            w, mu, _, p10 = summarize(rets[m])
            bw = summarize(base[m])[0]
            lo, blo = boot_lo(rets[m], sigs[m]), boot_lo(base[m], sigs[m])
            h1, h2 = halves(m, sigs)
            ch = (100 * (rets[h1] > 0).mean(), 100 * (rets[h2] > 0).mean())
            bh = (100 * (base[h1] > 0).mean(), 100 * (base[h2] > 0).mean())
            ok = w >= bw and lo >= blo and ch[0] >= bh[0] and ch[1] >= bh[1]
            allpass = allpass and ok
            line.append("%s win %5.1f (+%4.1f) lo %4.1f h %4.1f/%4.1f %s"
                        % (wname[:3], w, w - bw, lo, ch[0], ch[1], "ok" if ok else "FAIL"))
        qd = pd.DataFrame({"q": q, "b": base > 0, "c": rets > 0}).groupby("q").mean()
        nq = int((qd["c"] >= qd["b"]).sum())
        ci = paired_ci(base, rets, sigs)
        m = windows(sigs)[0][1]
        print("  arm %.3f lock %.3f | %s | %s | quarters %2d/%2d | dmean %+5.2f (CI %+.2f..%+.2f) | %s"
              % (arm, lock, line[0], line[1], nq, len(qd),
                 rets.mean() - base.mean(), ci[0], ci[1],
                 "ADOPTABLE" if allpass and nq >= len(qd) - 1 else "no"))
        print("        exits RECENT %s" % mix[m].value_counts(normalize=True).round(2).to_dict())
        wins = rets[m][rets[m] > 0]
        loss = rets[m][rets[m] <= 0]
        print("        avg win %+5.2f avg loss %+6.2f payoff %.2f breakeven win %4.1f%%"
              % (wins.mean(), loss.mean(), abs(wins.mean() / loss.mean()),
                 100 / (1 + abs(wins.mean() / loss.mean()))))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "engines"
    {"engines": cmd_engines, "grid": cmd_grid, "gate": cmd_gate,
     "wide": cmd_wide, "slip": cmd_slip, "local": cmd_local, "final": cmd_final}[cmd]()
