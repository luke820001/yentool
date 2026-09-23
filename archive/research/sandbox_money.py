"""Round 3 (2026-09-23): score the rule in MONEY, not only in win rate.

Why this file exists. The owner reported that real profit is poor while every
document quotes a ~70% win rate. Both are true at once: the shipped rule wins
often and small (the lock closes 36% of trades at about +1.2%) and loses
rarely and big (the disaster stop -20.5%, the time exit -9.9%). Per trade the
edge is about +2%, which two stops erase. So this round scores every candidate
on expectancy and on a portfolio equity curve (CAGR, max drawdown at 5 and 10
concurrent slots) as well as on the project's usual win-rate gates.

Same 556 CORE+ first-day trades as sandbox_entry_gate (universe.pkl), same E3
engine (sandbox_daily_plan.run_plan), every leg of the shipped rule taken from
scanner.exit_rules.DEFAULT_RULE.

    python archive/research/sandbox_money.py base      # baseline + exit split
    python archive/research/sandbox_money.py atr       # ATR-scaled levels + buckets
    python archive/research/sandbox_money.py latecut   # cut late losers (money view)
    python archive/research/sandbox_money.py tp        # take profit 15..25
    python archive/research/sandbox_money.py half      # sell half at +15
    python archive/research/sandbox_money.py all
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import sandbox_entry_gate as g                     # noqa: E402
from sandbox_daily_plan import run_plan            # noqa: E402
from scanner.exit_rules import DEFAULT_RULE as R   # noqa: E402

RECENT_FROM = g.RECENT_FROM
BUY_COST, SELL_COST = g.BUY_COST, g.SELL_COST
SEED = 7

BASE = dict(stop=R["stop_pct"], tp=R["tp_pct"], arm=R["arm_pct"],
            lock=R["lock_pct"], late_profit=(R["late_from"], R["late_gain"]),
            ride="ma5", cap=R["ride_cap"])


def trades():
    return [t for t in g.load() if g.passes(t, g.BASE_GATE)]


def replay(t, plan, slip=0.0):
    pnl, cap, ev = run_plan(t, plan, hold=R["hold_bars"])
    bar, reason = (ev[-1][0], ev[-1][1]) if ev else (0, "na")
    r = pnl / cap * 100
    if slip and reason in ("stop", "lock"):
        E = float(t["o"][0])
        px = (r / 100 + 1) * E * (1 + BUY_COST) / (1 - SELL_COST)
        r = g.net(E, px * (1 - slip))
    dates = t["dates"]
    return dict(sig=t["sig"], sid=t["sid"], atr=t.get("atr"), rank=t["rank"],
                entry=dates[0], exit=dates[min(bar, len(dates) - 1)],
                bars=bar + 1, ret=r, why=reason)


def evaluate(ts, plan_of, slip=0.0):
    """plan_of: a plan dict, or a callable trade -> plan."""
    rows = []
    for t in ts:
        plan = plan_of(t) if callable(plan_of) else plan_of
        rows.append(replay(t, plan, slip))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ scoring
def stats(df):
    if not len(df):
        return dict(n=0, win=np.nan, mean=np.nan)
    r = df["ret"].to_numpy(float)
    return dict(n=len(r), win=100 * (r > 0).mean(), mean=r.mean())


def boot(df, what, iters=1200, seed=SEED):
    """2.5th percentile of `what` ('win' or 'mean') under a by-signal-day
    bootstrap, so a cluster of trades on one day counts once."""
    if len(df) < 20:
        return np.nan
    rng = np.random.default_rng(seed)
    groups = [x["ret"].to_numpy(float) for _, x in df.groupby("sig")]
    w = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        r = np.concatenate([groups[i] for i in pick])
        w.append(100 * (r > 0).mean() if what == "win" else r.mean())
    return np.percentile(w, 2.5)


def paired_dmean(base, cand, iters=1500, seed=SEED):
    rng = np.random.default_rng(seed)
    d = cand["ret"].to_numpy(float) - base["ret"].to_numpy(float)
    keys = base["sig"].to_numpy()
    groups = [d[keys == k] for k in np.unique(keys)]
    m = []
    for _ in range(iters):
        pick = rng.integers(0, len(groups), len(groups))
        m.append(np.concatenate([groups[i] for i in pick]).mean())
    return np.percentile(m, 2.5), np.percentile(m, 97.5)


def quarters(a, b):
    def q(df):
        key = (df["sig"].str.slice(0, 4) + "Q"
               + ((df["sig"].str.slice(5, 7).astype(int) - 1) // 3 + 1).astype(str))
        return pd.DataFrame({"q": key, "r": df["ret"]}).groupby("q")["r"].mean()
    qa, qb = q(a), q(b)
    idx = qb.index.intersection(qa.index)
    return int((qa[idx] >= qb[idx] - 1e-9).sum()), len(idx)


def portfolio(df, slots):
    """Equity curve with `slots` equal-weight slots, a signal skipped when all
    slots are full (by rank). Returns final multiple, CAGR %, max drawdown %,
    per-year %, trades taken."""
    d = df.sort_values(["entry", "rank"])
    eq, open_, curve, taken = 1.0, [], [], 0
    days = sorted(set(d.entry) | set(d.exit))
    by_entry = {k: v for k, v in d.groupby("entry")}
    for day in days:
        still = []
        for pos in open_:
            if pos["exit"] <= day:
                eq += pos["size"] * pos["ret"] / 100
            else:
                still.append(pos)
        open_ = still
        for _, r in by_entry.get(day, pd.DataFrame()).iterrows():
            if len(open_) >= slots:
                continue
            open_.append(dict(exit=r.exit, ret=r.ret, size=eq / slots))
            taken += 1
        curve.append((day, eq))
    c = pd.Series(dict(curve))
    c.index = pd.to_datetime(c.index)
    yrs = (c.index[-1] - c.index[0]).days / 365.25
    cagr = c.iloc[-1] ** (1 / yrs) - 1
    mdd = (c / c.cummax() - 1).min()
    ye = c.resample("YE").last()
    yearly = ye.pct_change()
    yearly.iloc[0] = ye.iloc[0] - 1
    return dict(final=c.iloc[-1], cagr=100 * cagr, mdd=100 * mdd, taken=taken,
                yearly={str(k.year): 100 * v for k, v in yearly.items()})


def split(df):
    return df[df["sig"] >= RECENT_FROM], df[df["sig"] < RECENT_FROM]


def report(label, base, cand, ts, plan_of):
    """The seven gates plus the money view. Prints one block per candidate."""
    if len(base) != len(cand) or (base["sig"].to_numpy() != cand["sig"].to_numpy()).any():
        raise ValueError("base and candidate must be the same trades in order")
    print("== %s" % label)
    money_ok = True
    for wname, (b, k) in zip(("RECENT", "OLD"), zip(split(base), split(cand))):
        sb, sk = stats(b), stats(k)
        h = len(b) // 2
        bi = np.argsort(b["sig"].to_numpy())
        hb = [b.iloc[bi[:h]], b.iloc[bi[h:]]]
        hk = [k.iloc[bi[:h]], k.iloc[bi[h:]]]
        win_ok = sk["win"] >= sb["win"] - 1.0
        mean_ok = (sk["mean"] > sb["mean"]
                   and boot(k, "mean") >= boot(b, "mean")
                   and stats(hk[0])["mean"] >= stats(hb[0])["mean"]
                   and stats(hk[1])["mean"] >= stats(hb[1])["mean"])
        money_ok = money_ok and win_ok and mean_ok
        print("  %-6s n=%3d win %5.1f%% (base %5.1f) mean %+5.2f (base %+5.2f) "
              "bootMean %+5.2f/%+5.2f halves %+5.2f/%+5.2f vs %+5.2f/%+5.2f  %s"
              % (wname, sk["n"], sk["win"], sb["win"], sk["mean"], sb["mean"],
                 boot(k, "mean"), boot(b, "mean"),
                 stats(hk[0])["mean"], stats(hk[1])["mean"],
                 stats(hb[0])["mean"], stats(hb[1])["mean"],
                 "ok" if (win_ok and mean_ok) else "FAIL"))
    qa, qn = quarters(cand, base)
    lo, hi = paired_dmean(base, cand)
    sl = evaluate(ts, plan_of, slip=0.005)
    slb = evaluate(ts, BASE, slip=0.005)
    rs, ro = split(sl)
    bs, bo = split(slb)
    print("  quarters mean>=base %d/%d | paired dmean CI %+.2f..%+.2f | "
          "slip0.5%%: REC %5.1f%%/%+5.2f (base %5.1f/%+5.2f) OLD %5.1f%%/%+5.2f (base %5.1f/%+5.2f)"
          % (qa, qn, lo, hi, stats(rs)["win"], stats(rs)["mean"],
             stats(bs)["win"], stats(bs)["mean"], stats(ro)["win"],
             stats(ro)["mean"], stats(bo)["win"], stats(bo)["mean"]))
    for slots in (5, 10):
        pb, pk = portfolio(base, slots), portfolio(cand, slots)
        print("  slots %2d: CAGR %5.1f%% (base %5.1f) MDD %5.1f%% (base %5.1f) "
              "final x%.2f (base x%.2f)"
              % (slots, pk["cagr"], pb["cagr"], pk["mdd"], pb["mdd"],
                 pk["final"], pb["final"]))
    ex = cand.groupby("why")["ret"].agg(["size", "mean"])
    print("  exits: " + ", ".join("%s %d @ %+.2f" % (k, v["size"], v["mean"])
                                  for k, v in ex.iterrows()))
    verdict = money_ok and qa >= int(0.8 * qn) and lo > 0
    print("  -> %s" % ("PASSES the money gates" if verdict else "not adopted"))
    return verdict


# --------------------------------------------------------------- commands
def cmd_base():
    ts = trades()
    base = evaluate(ts, BASE)
    print("shipped rule on %d trades" % len(base))
    for wname, w in zip(("RECENT", "OLD", "ALL"), (*split(base), base)):
        s = stats(w)
        print("  %-6s n=%3d win %5.1f%% mean %+5.2f bootMean %+5.2f bootWin %5.1f"
              % (wname, s["n"], s["win"], s["mean"], boot(w, "mean"), boot(w, "win")))
    ex = base.groupby("why")["ret"].agg(["size", "mean", "sum"])
    ex["share_of_pnl"] = 100 * ex["sum"] / ex["sum"].abs().sum()
    print(ex.round(2).to_string())
    for slots in (3, 5, 10, 20):
        p = portfolio(base, slots)
        print("  slots %2d: taken %3d final x%.2f CAGR %5.1f%% MDD %5.1f%% | %s"
              % (slots, p["taken"], p["final"], p["cagr"], p["mdd"],
                 " ".join("%s %+.0f" % kv for kv in p["yearly"].items())))
    by_year = base.groupby(base["sig"].str.slice(0, 4))["ret"].agg(["size", "mean"])
    by_year["win"] = base.groupby(base["sig"].str.slice(0, 4))["ret"].apply(lambda r: 100 * (r > 0).mean())
    print(by_year.round(2).to_string())


def atr_plan(ks, kt=None):
    """stop/arm/lock as multiples of the stock's own ATR% (t['atr'] is in %)."""
    def f(t):
        a = float(t["atr"]) / 100.0
        p = dict(BASE)
        p["stop"] = min(ks[0] * a, 0.30)
        p["arm"] = ks[1] * a
        p["lock"] = ks[2] * a
        if kt is not None:
            p["tp"] = kt * a
        return p
    return f


def cmd_atr():
    ts = trades()
    base = evaluate(ts, BASE)
    for ks in ((3.5, 0.4, 0.3), (3.0, 0.4, 0.3), (3.5, 0.5, 0.3), (2.5, 0.4, 0.3)):
        pl = atr_plan(ks)
        cand = evaluate(ts, pl)
        report("ATR-scaled stop %.1fx arm %.1fx lock %.1fx" % ks, base, cand, ts, pl)
        # the bucket check the last round did not do: where does the delta live?
        d = cand.assign(d=cand["ret"] - base["ret"])
        edges = (4.5, 5.0, 5.5, 6.0, 6.5, 7.5, 9.0, 99)
        for lo, hi in zip(edges[:-1], edges[1:]):
            sub = d[(d["atr"] >= lo) & (d["atr"] < hi)]
            if len(sub):
                print("     atr [%4.1f,%4.1f) n=%3d dmean %+5.2f sum %+7.1f  base win %5.1f -> %5.1f"
                      % (lo, hi, len(sub), sub["d"].mean(), sub["d"].sum(),
                         100 * (base.loc[sub.index, "ret"] > 0).mean(),
                         100 * (sub["ret"] > 0).mean()))


def cmd_latecut():
    ts = trades()
    base = evaluate(ts, BASE)
    for frm in (5, 6, 7, 8):
        for mx in (-0.08, -0.05, -0.03, 0.0):
            pl = dict(BASE, late_loss=(frm, mx))
            cand = evaluate(ts, pl)
            report("cut from day %d when close < fill %+.0f%%" % (frm, 100 * mx),
                   base, cand, ts, pl)


def cmd_tp():
    ts = trades()
    base = evaluate(ts, BASE)
    for tp in (0.15, 0.18, 0.22, 0.25, None):
        pl = dict(BASE, tp=tp)
        cand = evaluate(ts, pl)
        report("take profit %s" % ("none" if tp is None else "%+.0f%%" % (100 * tp)),
               base, cand, ts, pl)


def cmd_half():
    ts = trades()
    base = evaluate(ts, BASE)
    for lv in (0.10, 0.15):
        pl = dict(BASE, out=[(lv, 0.5)])
        cand = evaluate(ts, pl)
        report("sell half at %+.0f%%" % (100 * lv), base, cand, ts, pl)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "base"
    if cmd == "all":
        for c in (cmd_base, cmd_atr, cmd_latecut, cmd_tp, cmd_half):
            c()
    else:
        globals()["cmd_" + cmd]()
