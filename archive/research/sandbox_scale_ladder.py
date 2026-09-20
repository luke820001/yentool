"""
sandbox_scale_ladder.py

User question 2026-09-17 (after 1815 fell -11% by day 6 and the app still said
"hold"): would the recommendation be better if it
  (a) sold PART of the position once the price broke some level, and bought it
      back lower,
  (b) ADDED to the position when it fell to some level (average down),
  (c) sold in BATCHES as it rose,
  (d) folded in market-moving news (Fed decisions, wars) every day?

(d) cannot be replayed as "an AI read the news" -- a model reading 2019 news
today knows how 2019 ended. What CAN be replayed without lookahead is the
market's own reaction to that news, available before the Taiwan open: the
previous US session's SOX / VIX. That is what the N* overlays test.

Everything runs on the SAME trades: the adopted CORE+ entries (OTC, TAIEX
risk_on, rank<20, dist52<=5%, ret5<=5%, ATR>=4.5, first day listed), entered
at the next open. Differences between plans are therefore paired.

Windows: RECENT = last 3 years (the user's main question: the market changes
fast, old data may dilute), OLD = everything before, as a direction check only.

Accounting per trade, in units of the plan's reserved budget:
  pnl       sum of fills, net of costs (buy 0.1425%, sell 0.1425% + 0.3% tax)
  win       pnl > 0 for the WHOLE position cycle (several small sells of one
            position are one trade, not several wins)
  cap_days  cost basis held at each bar close, summed (capital actually used)

Intra-bar order is unknowable from daily bars. Main numbers use O-L-H-C on up
bars and O-H-L-C on down bars; `worst` re-runs each trade with both fixed
orders and keeps the worse result.

Run from the repo root:
  python archive/research/sandbox_scale_ladder.py build   (slow, once)
  python archive/research/sandbox_scale_ladder.py sim
ASCII only.
"""
import json
import os
import pickle
import sys
import warnings

import numpy as np
import pandas as pd

import eval_realtrade as er

RESEARCH_DB = "data/research_prices.db"
CACHE_DIR = os.environ.get("LADDER_CACHE", os.path.join(
    os.environ.get("TEMP", "."), "yentool_ladder"))
PICKS_PKL = os.path.join(CACHE_DIR, "picks.pkl")
US_PKL = os.path.join(CACHE_DIR, "us.pkl")

WARMUP = "2017-03-01"
EVAL_FROM = "2017-06-01"
RECENT_FROM = "2023-09-18"
AI_FROM = "2024-07-01"
FWD_BARS = 25
ATR_MIN = 4.5

BUY_COST = 0.001425
SELL_COST = 0.001425 + 0.003


# ------------------------------------------------------------------ data
def yf_close(ticker, start="2016-01-01"):
    import yfinance as yf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = yf.download(ticker, start=start, auto_adjust=False, progress=False)
    raw = raw.reset_index()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(c[0]).lower() for c in raw.columns]
    else:
        raw.columns = [str(c).lower() for c in raw.columns]
    out = pd.DataFrame({"date": pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d"),
                        "close": pd.to_numeric(raw["close"], errors="coerce")})
    return out.dropna()


def build():
    os.makedirs(CACHE_DIR, exist_ok=True)
    er.DB = RESEARCH_DB
    er.WARMUP_START = WARMUP
    print("building features (slow)...")
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)

    # Trading calendar = days most of the tape traded; drops yfinance's
    # synthetic holiday bars (volume 0) from the regime and the forward bars.
    live = df[df["Volume_Lot"] > 0]
    cnt = live.groupby("date")["stock_id"].size()
    cal = set(cnt[cnt >= 300].index)

    tw = yf_close("^TWII")
    tw = tw[tw["date"].isin(cal)].reset_index(drop=True)
    c = tw["close"]
    tw["risk_on"] = (c > c.rolling(20).mean()) & (c > c.rolling(60).mean())
    risk_on = dict(zip(tw["date"], tw["risk_on"]))

    d = df.sort_values(["stock_id", "date"])
    atr = ((d["high"] - d["low"]) / d["close"]).groupby(d["stock_id"]).transform(
        lambda s: s.rolling(20).mean()) * 100
    amap = dict(zip(zip(d["stock_id"].astype(str), d["date"]), atr))

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    print("replaying selection...")
    P = er.replay_selection(T)
    P = P[P["date"] >= EVAL_FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P = P.merge(T[["date", "sid", "c", "ret5", "dist52"]], on=["date", "sid"], how="left")
    P["atr"] = [amap.get((str(s), x)) for s, x in zip(P["sid"], P["date"])]
    P["ro"] = P["date"].map(lambda x: bool(risk_on.get(x, False)))
    gate = ((P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20) & (P["dist52"] <= 0.05)
            & (P["ret5"] <= 0.05) & (P["atr"] >= ATR_MIN) & (P["streak"] == 1))
    G = P[gate].copy()
    print("CORE+ first-day entries:", len(G))

    bars = {sid: g[g["date"].isin(cal) & (g["Volume_Lot"] > 0)].reset_index(drop=True)
            for sid, g in live.groupby("stock_id")}
    trades = []
    for r in G.itertuples(index=False):
        g = bars.get(str(r.sid))
        if g is None:
            continue
        idx = g.index[g["date"] == r.date]
        if len(idx) == 0:
            continue
        i = int(idx[0])
        fb = g.iloc[i + 1: i + 1 + FWD_BARS]
        if len(fb) == 0:
            continue
        trades.append({
            "sig": r.date, "sid": str(r.sid), "rank": int(r.rank),
            "dates": fb["date"].tolist(),
            "o": fb["open"].to_numpy(float), "h": fb["high"].to_numpy(float),
            "l": fb["low"].to_numpy(float), "c": fb["close"].to_numpy(float),
        })
    pickle.dump(trades, open(PICKS_PKL, "wb"))

    us = {t: yf_close(t) for t in ("^SOX", "^VIX", "^GSPC")}
    pickle.dump(us, open(US_PKL, "wb"))
    print("cached %d trades -> %s" % (len(trades), PICKS_PKL))


# ------------------------------------------------------------------ engine
class Book:
    """One position cycle, budget-normalised."""

    def __init__(self, entry):
        self.E = entry
        self.qty = 0.0        # shares, in budget units / price
        self.basis = 0.0      # cost basis still held
        self.cash = 0.0       # realised, net of costs
        self.max_basis = 0.0
        self.done = set()     # one-shot triggers already used
        self.armed = False
        self.stop = None      # live stop (fraction of E), None = no stop
        self.events = []

    def buy(self, frac, px, tag):
        if frac <= 0:
            return
        q = frac / px
        self.qty += q
        self.basis += frac
        self.cash -= frac * (1 + BUY_COST)
        self.max_basis = max(self.max_basis, self.basis)
        self.events.append((tag, px))

    def sell_frac_of_budget(self, frac, px, tag):
        if self.qty <= 1e-12:
            return
        q = min(self.qty, frac / self.E)   # tranche sized off the planned entry
        self._sell(q, px, tag)

    def sell_all(self, px, tag):
        if self.qty > 1e-12:
            self._sell(self.qty, px, tag)

    def _sell(self, q, px, tag):
        avg = self.basis / self.qty
        self.cash += q * px * (1 - SELL_COST)
        self.basis -= q * avg
        self.qty -= q
        if self.qty <= 1e-12:
            self.qty, self.basis = 0.0, 0.0
        self.events.append((tag, px))


def triggers(plan, bk):
    """Active (level_price, side, key, action) given the book state."""
    E, out = bk.E, []
    flat = bk.qty <= 1e-12
    if not flat and bk.stop is not None:
        out.append((E * bk.stop, "dn", "stop", ("sell_all",)))
    for k, (pct, frac) in enumerate(plan.get("adds", ())):
        key = "add%d" % k
        if key not in bk.done and not flat:
            out.append((E * (1 - pct), "dn", key, ("buy", frac)))
    for k, (pct, frac) in enumerate(plan.get("cuts", ())):
        key = "cut%d" % k
        if key not in bk.done and not flat:
            out.append((E * (1 - pct), "dn", key, ("sell", frac)))
    for k, (pct, frac) in enumerate(plan.get("rebuys", ())):
        key = "rebuy%d" % k
        if key not in bk.done and "cut%d" % k in bk.done:
            out.append((E * (1 - pct), "dn", key, ("buy", frac)))
    for k, (pct, frac) in enumerate(plan.get("tps", ())):
        key = "tp%d" % k
        if key not in bk.done and not flat:
            out.append((E * (1 + pct), "up", key, ("sell", frac)))
    if plan.get("tp_all") is not None and not flat:
        out.append((E * (1 + plan["tp_all"]), "up", "tp_all", ("sell_all",)))
    if plan.get("arm") is not None and not bk.armed and not flat:
        out.append((E * (1 + plan["arm"]), "up", "arm", ("arm",)))
    return out


def act(plan, bk, key, action, px):
    kind = action[0]
    if key not in ("stop", "tp_all", "arm"):
        bk.done.add(key)
    if kind == "sell_all":
        bk.sell_all(px, key)
    elif kind == "sell":
        bk.sell_frac_of_budget(action[1], px, key)
        if key.startswith("tp") and plan.get("be_after_tp") is not None:
            lvl = 1 + plan["be_after_tp"]
            bk.stop = lvl if bk.stop is None else max(bk.stop, lvl)
        if key.startswith("cut") and plan.get("stop_after_cut") is not None:
            bk.stop = 1 - plan["stop_after_cut"]
    elif kind == "buy":
        bk.buy(action[1], px, key)
        if key.startswith("rebuy") and plan.get("stop_after_rebuy") is not None:
            bk.stop = 1 - plan["stop_after_rebuy"]
    elif kind == "arm":
        bk.armed = True
        lvl = 1 + plan["lock"]
        bk.stop = lvl if bk.stop is None else max(bk.stop, lvl)


def move(plan, bk, p0, p1, gap):
    """Walk the price from p0 to p1, firing triggers in the order touched.
    A gap (previous close -> open) fills everything it jumps over AT p1.
    A trigger already satisfied at the current price fires there; ties go
    stop first, then partial cuts, then everything else (conservative)."""
    prio = {"stop": 0}
    cur = p0
    for _ in range(50):
        best = None
        for lvl, side, key, action in triggers(plan, bk):
            if side == "dn":
                if lvl >= cur:
                    dist = 0.0
                elif p1 <= lvl:
                    dist = cur - lvl
                else:
                    continue
            else:
                if lvl <= cur:
                    dist = 0.0
                elif p1 >= lvl:
                    dist = lvl - cur
                else:
                    continue
            rank = (dist, prio.get(key, 1 if key.startswith("cut") else 2))
            if best is None or rank < best[0]:
                best = (rank, lvl, key, action)
        if best is None:
            return
        (dist, _), lvl, key, action = best
        if gap:
            px = p1
        else:
            px = cur if dist == 0.0 else lvl
        act(plan, bk, key, action, px)
        cur = p1 if gap else px


def run_trade(plan, t, order="neutral", shock=None):
    hold = plan.get("hold", 10)
    o, h, l, c = t["o"], t["h"], t["l"], t["c"]
    if len(o) < hold:
        return None
    E = o[0]
    if not (E > 0):
        return None
    bk = Book(E)
    bk.stop = (1 - plan["stop"]) if plan.get("stop") is not None else None
    cap_days = 0.0
    for i in range(hold):
        if i == 0:
            bk.buy(plan.get("init", 1.0), E, "entry")
            prev = E
        else:
            prev = c[i - 1]
            if shock is not None and shock[i] and bk.qty > 0:
                bk.sell_all(o[i], "shock")
            move(plan, bk, prev, o[i], gap=True)
        if order == "ohlc" or (order == "neutral" and c[i] < o[i]):
            path = (o[i], h[i], l[i], c[i])
        else:
            path = (o[i], l[i], h[i], c[i])
        for a, b in zip(path[:-1], path[1:]):
            move(plan, bk, a, b, gap=False)
        cap_days += bk.basis
        if bk.qty <= 1e-12 and not plan.get("rebuys"):
            break
    if bk.qty > 1e-12:
        bk.sell_all(c[hold - 1], "time")
    budget = plan.get("budget", 1.0)
    return {"pnl": bk.cash / budget * 100, "cap_days": cap_days / budget,
            "max_cap": bk.max_basis / budget, "events": bk.events}


# ------------------------------------------------------------------ plans
ADOPTED_UP = dict(arm=0.06, lock=0.02, tp_all=0.20)
PLANS = {
    # references
    "R0 adopted stop15+lock+tp20":   dict(stop=0.15, **ADOPTED_UP),
    "R1 raw hold10":                 dict(),
    "R2 stop15+tp20 (no lock)":      dict(stop=0.15, tp_all=0.20),
    # (a) sell part on a break, buy back lower
    "A1 cut50@-8 rest stop15":       dict(stop=0.15, cuts=[(0.08, 0.5)], **ADOPTED_UP),
    "A2 cut50@-10 rest stop15":      dict(stop=0.15, cuts=[(0.10, 0.5)], **ADOPTED_UP),
    "A3 cut50@-8 rebuy@-15 stop25":  dict(stop=0.25, cuts=[(0.08, 0.5)], rebuys=[(0.15, 0.5)], **ADOPTED_UP),
    "A4 cut50@-10 rebuy@-18 stop25": dict(stop=0.25, cuts=[(0.10, 0.5)], rebuys=[(0.18, 0.5)], **ADOPTED_UP),
    # (b) add on the way down (budget = full plan size)
    "B1 half, +half@-5, stop15":     dict(init=0.5, adds=[(0.05, 0.5)], stop=0.15, **ADOPTED_UP),
    "B2 half, +half@-8, stop15":     dict(init=0.5, adds=[(0.08, 0.5)], stop=0.15, **ADOPTED_UP),
    "B3 half, +half@-10, stop20":    dict(init=0.5, adds=[(0.10, 0.5)], stop=0.20, **ADOPTED_UP),
    "B4 full, +half@-8, stop15":     dict(init=1.0, adds=[(0.08, 0.5)], stop=0.15, budget=1.5, **ADOPTED_UP),
    # (c) sell in batches on the way up (downside = stop15)
    "C1 1/3@+10 1/3@+20 rest time, BE": dict(stop=0.15, tps=[(0.10, 1/3.), (0.20, 1/3.)], be_after_tp=0.0),
    "C2 1/2@+10 rest tp30, BE":      dict(stop=0.15, tps=[(0.10, 0.5)], tp_all=0.30, be_after_tp=0.0),
    "C3 1/2@+10 rest time":          dict(stop=0.15, tps=[(0.10, 0.5)]),
    "C4 1/3@+8 1/3@+15 rest@+25":    dict(stop=0.15, tps=[(0.08, 1/3.), (0.15, 1/3.)], tp_all=0.25),
    "C5 1/2@+10 rest tp20, BE":      dict(stop=0.15, tps=[(0.10, 0.5)], tp_all=0.20, be_after_tp=0.0),
    # combos
    "X1 B2 down + C1 up":            dict(init=0.5, adds=[(0.08, 0.5)], stop=0.15, tps=[(0.10, 1/3.), (0.20, 1/3.)], be_after_tp=0.0),
    "X2 B2 down + C2 up":            dict(init=0.5, adds=[(0.08, 0.5)], stop=0.15, tps=[(0.10, 0.5)], tp_all=0.30, be_after_tp=0.0),
    "X3 A1 down + C1 up":            dict(stop=0.15, cuts=[(0.08, 0.5)], tps=[(0.10, 1/3.), (0.20, 1/3.)], be_after_tp=0.0),
}


# ------------------------------------------------------------------ stats
def summary(rows):
    if not rows:
        return None
    p = np.array([r["pnl"] for r in rows])
    cd = np.array([r["cap_days"] for r in rows])
    wins, losses = p[p > 0], p[p <= 0]
    return {
        "n": len(p), "win": 100 * (p > 0).mean(), "mean": p.mean(),
        "med": float(np.median(p)), "p10": float(np.percentile(p, 10)),
        "worst": p.min(),
        "pf": wins.sum() / abs(losses.sum()) if losses.sum() else float("inf"),
        "per10cd": p.sum() / cd.sum() * 10 if cd.sum() else 0.0,
    }


def paired_ci(base, cand, sigs, iters=2000, seed=7):
    """Mean-pnl and win-rate difference, cluster bootstrap by signal date."""
    b, c = np.array(base), np.array(cand)
    s = np.array(sigs)
    days = np.unique(s)
    idx = {d: np.where(s == d)[0] for d in days}
    rng = np.random.default_rng(seed)
    dm, dw = [], []
    for _ in range(iters):
        pick = np.concatenate([idx[d] for d in rng.choice(days, len(days))])
        dm.append(c[pick].mean() - b[pick].mean())
        dw.append(100 * ((c[pick] > 0).mean() - (b[pick] > 0).mean()))
    return (np.percentile(dm, 2.5), np.percentile(dm, 97.5),
            np.percentile(dw, 2.5), np.percentile(dw, 97.5))


def us_flags(us):
    """signal/entry date -> readouts of the last US session BEFORE the Taiwan
    open of the entry bar (US date strictly earlier than the entry date)."""
    sox, vix = us["^SOX"].copy(), us["^VIX"].copy()
    sox["r1"] = sox["close"].pct_change()
    sox["r5"] = sox["close"] / sox["close"].shift(5) - 1
    sd, vd = sox["date"].to_numpy(), vix["date"].to_numpy()

    def last_before(dates, d):
        k = np.searchsorted(dates, d) - 1
        return k if k >= 0 else None

    def at(d):
        ks, kv = last_before(sd, d), last_before(vd, d)
        return {"sox1": sox["r1"].iloc[ks] if ks is not None else np.nan,
                "sox5": sox["r5"].iloc[ks] if ks is not None else np.nan,
                "vix": vix["close"].iloc[kv] if kv is not None else np.nan}
    return at


def sim():
    trades = pickle.load(open(PICKS_PKL, "rb"))
    us = pickle.load(open(US_PKL, "rb"))
    at = us_flags(us)
    print("trades cached:", len(trades))

    windows = {
        "RECENT 3y (%s+)" % RECENT_FROM: lambda t: t["sig"] >= RECENT_FROM,
        "AI era (%s+)" % AI_FROM: lambda t: t["sig"] >= AI_FROM,
        "OLD (<%s)" % RECENT_FROM: lambda t: t["sig"] < RECENT_FROM,
    }

    results = {}
    for name, plan in PLANS.items():
        neut, worst = [], []
        for t in trades:
            a = run_trade(plan, t, "neutral")
            if a is None:
                continue
            b = run_trade(plan, t, "olhc")
            c = run_trade(plan, t, "ohlc")
            w = min((a, b, c), key=lambda x: x["pnl"])
            neut.append(dict(a, sig=t["sig"], sid=t["sid"]))
            worst.append(dict(w, sig=t["sig"], sid=t["sid"]))
        results[name] = (neut, worst)

    base_name = "R0 adopted stop15+lock+tp20"
    for wname, keep in windows.items():
        print("\n==== %s ====" % wname)
        print("%-34s %4s %6s %7s %6s %6s %7s %5s %7s %6s | %-24s | %s"
              % ("plan", "n", "win%", "mean%", "med", "p10", "worst", "PF",
                 "/10capd", "worstW", "d-mean vs R0 [95%]", "d-win vs R0 [95%]"))
        bn = [r for r in results[base_name][0] if keep(r)]
        for name, (neut, worst) in results.items():
            rows = [r for r in neut if keep(r)]
            wr = [r for r in worst if keep(r)]
            s, sw = summary(rows), summary(wr)
            if not s:
                continue
            ci = ""
            if name != base_name:
                lo_m, hi_m, lo_w, hi_w = paired_ci([r["pnl"] for r in bn],
                                                   [r["pnl"] for r in rows],
                                                   [r["sig"] for r in rows])
                ci = "%+5.2f [%+5.2f,%+5.2f] | %+5.1f [%+5.1f,%+5.1f]" % (
                    s["mean"] - summary(bn)["mean"], lo_m, hi_m,
                    s["win"] - summary(bn)["win"], lo_w, hi_w)
            print("%-34s %4d %6.1f %+7.2f %+6.2f %+6.1f %+7.1f %5.2f %+7.2f %6.1f | %s"
                  % (name, s["n"], s["win"], s["mean"], s["med"], s["p10"],
                     s["worst"], s["pf"], s["per10cd"], sw["win"], ci))

    print("\n==== by year (win%% / mean%%, neutral order) ====")
    years = sorted({r["sig"][:4] for r in results[base_name][0]})
    print("%-34s " % "plan" + " ".join("%11s" % y for y in years))
    for name, (neut, _) in results.items():
        cells = []
        for y in years:
            s = summary([r for r in neut if r["sig"][:4] == y])
            cells.append("%4.0f/%+5.1f" % (s["win"], s["mean"]) if s else "%11s" % "-")
        print("%-34s " % name + " ".join("%11s" % x for x in cells))

    # ---------------------------------------------------- news proxies
    print("\n==== news proxy: previous US session before the entry open ====")
    base_plan = PLANS[base_name]
    filt = {
        "N0 no filter": lambda f: False,
        "N1 skip if SOX 1d <= -3%": lambda f: f["sox1"] <= -0.03,
        "N2 skip if VIX >= 25": lambda f: f["vix"] >= 25,
        "N3 skip if SOX 5d <= -6%": lambda f: f["sox5"] <= -0.06,
        "N4 skip if SOX 1d <= -2%": lambda f: f["sox1"] <= -0.02,
    }
    for wname, keep in windows.items():
        print("  -- %s" % wname)
        pool = [t for t in trades if keep(t)]
        for fname, fn in filt.items():
            rows, skipped = [], []
            for t in pool:
                r = run_trade(base_plan, t, "neutral")
                if r is None:
                    continue
                f = at(t["dates"][0])
                (skipped if fn(f) else rows).append(r)
            s, k = summary(rows), summary(skipped)
            print("    %-28s kept n=%4d win=%5.1f mean=%+6.2f | skipped n=%3d win=%s mean=%s"
                  % (fname, s["n"], s["win"], s["mean"], len(skipped),
                     "%5.1f" % k["win"] if k else "  -  ", "%+6.2f" % k["mean"] if k else "  -  "))

    # exit on a US shock while holding
    print("\n==== news proxy: exit at the open after a US shock while holding ====")
    for wname, keep in windows.items():
        pool = [t for t in trades if keep(t)]
        for thr in (None, -0.03, -0.04, -0.05):
            rows = []
            for t in pool:
                shock = None
                if thr is not None:
                    shock = [False] + [at(d)["sox1"] <= thr for d in t["dates"][1:10]]
                r = run_trade(base_plan, t, "neutral", shock=shock)
                if r is not None:
                    rows.append(r)
            s = summary(rows)
            print("  %-26s exit if SOX 1d <= %-5s n=%4d win=%5.1f mean=%+6.2f p10=%+6.1f"
                  % (wname, "none" if thr is None else "%d%%" % (thr * 100),
                     s["n"], s["win"], s["mean"], s["p10"]))

    pickle.dump({k: v[0] for k, v in results.items()},
                open(os.path.join(CACHE_DIR, "results.pkl"), "wb"))


# ------------------------------------------------------------------ gate
def boot_lo(rows, iters=2000, seed=3):
    """2.5th percentile of the win rate, resampling signal DAYS."""
    by = {}
    for r in rows:
        by.setdefault(r["sig"], []).append(r["pnl"])
    days = [np.array(v) for v in by.values()]
    rng = np.random.default_rng(seed)
    n = len(days)
    wins = [100 * (np.concatenate([days[i] for i in rng.integers(0, n, n)]) > 0).mean()
            for _ in range(iters)]
    return float(np.percentile(wins, 2.5))


def gate():
    """Project adoption gate for the 2026-09-17 candidates: win, bootLo, h1, h2
    all >= base, quarterly stability, and the stop-width surface (plateau, not
    a peak)."""
    trades = pickle.load(open(PICKS_PKL, "rb"))
    up = dict(arm=0.06, lock=0.02, tp_all=0.20)
    plans = [
        ("base stop15", dict(stop=0.15, **up)),
        ("stop17", dict(stop=0.17, **up)),
        ("stop20", dict(stop=0.20, **up)),
        ("stop22", dict(stop=0.22, **up)),
        ("stop25", dict(stop=0.25, **up)),
        ("stop30", dict(stop=0.30, **up)),
        ("no stop", dict(stop=None, **up)),
        ("half+add@-10 stop20", dict(init=0.5, adds=[(0.10, 0.5)], stop=0.20, **up)),
    ]
    runs = {}
    for name, plan in plans:
        rows = []
        for t in trades:
            r = run_trade(plan, t)
            if r is not None:
                rows.append(dict(r, sig=t["sig"]))
        runs[name] = rows
    windows = {"ALL": lambda s: True, "RECENT": lambda s: s >= RECENT_FROM,
               "OLD": lambda s: s < RECENT_FROM}
    for w, keep in windows.items():
        print("\n==== gate: %s ====" % w)
        base = None
        for name, _ in plans:
            rows = [r for r in runs[name] if keep(r["sig"])]
            p = np.array([r["pnl"] for r in rows])
            days = sorted({r["sig"] for r in rows})
            mid = days[len(days) // 2]
            h1 = np.array([r["pnl"] for r in rows if r["sig"] < mid])
            h2 = np.array([r["pnl"] for r in rows if r["sig"] >= mid])
            s = {"win": 100 * (p > 0).mean(), "boot": boot_lo(rows),
                 "h1": 100 * (h1 > 0).mean(), "h2": 100 * (h2 > 0).mean(),
                 "mean": p.mean(), "h1m": h1.mean(), "h2m": h2.mean()}
            q = {}
            for r in rows:
                k = r["sig"][:4] + "Q%d" % ((int(r["sig"][5:7]) - 1) // 3 + 1)
                q.setdefault(k, []).append(r["pnl"])
            s["q"] = q
            flag = ""
            if base is None:
                base = s
            else:
                keys = [k for k in q if len(q[k]) >= 5 and k in base["q"]]
                qw = sum((np.array(q[k]) > 0).mean() >= (np.array(base["q"][k]) > 0).mean() for k in keys)
                qm = sum(np.mean(q[k]) >= np.mean(base["q"][k]) for k in keys)
                ok = all(s[x] >= base[x] for x in ("win", "boot", "h1", "h2"))
                flag = "win-gate %s | quarters win>=base %d/%d mean>=base %d/%d" % (
                    "PASS" if ok else "fail", qw, len(keys), qm, len(keys))
            print("  %-20s win %5.1f bootLo %5.1f h1 %5.1f h2 %5.1f | mean %+5.2f h1 %+5.2f h2 %+5.2f | worst %+6.1f  %s"
                  % (name, s["win"], s["boot"], s["h1"], s["h2"], s["mean"], s["h1m"],
                     s["h2m"], p.min(), flag))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sim"
    if cmd == "build":
        build()
    elif cmd == "gate":
        gate()
    else:
        sim()
