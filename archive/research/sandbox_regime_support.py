"""
sandbox_regime_support.py

Verifies two user hypotheses on the 6-year research db (2020-09..2026-06,
includes the 2022 bear), using the exact shipped selection machinery:

  H-A  "TAIEX below its 60MA (headwind) is actually the CHEAP time to open
       new positions" -- bucket the adopted rule's entries by the signal-day
       TAIEX regime and compare forward outcomes under the identical exit
       stack. Also a crash bucket (TAIEX 20d return <= -10pct).

  H-B  "Entry should be a limit at SUPPORT (not next-day open at market),
       with an immediate stop when support breaks; that should raise the
       win rate" -- simulate limit-at-support fills (MA20 and 20d-low
       variants, wait up to W bars), support-break exits, and compare
       against the adopted next-open + disaster-stop stack ON THE SAME
       PICKS, counting the opportunity cost of signals that never pull
       back to the limit.

Run:  python sandbox_regime_support.py [eval_from] [warmup]
      e.g. python sandbox_regime_support.py 2024-07-01 2024-04-01
      (defaults: the 6y window from sandbox_research_replay)
ASCII only.
"""
import json
import sys

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_winrate_round2 import sim_trail
from sandbox_research_replay import RESEARCH_DB, TAIEX_CACHE, WARMUP, EVAL_FROM

WAIT_BARS = 5      # how long a resting limit order waits for a pullback fill
HOLD = 10
TP = 0.20


def regime_states():
    """date -> (state, crash) where state in {risk_on, mid, below60}.
    risk_on = above 20MA and 60MA (the shipped entry gate);
    mid     = above 60MA but below 20MA;
    below60 = below 60MA (the user's proposed dip-buy window).
    crash   = TAIEX 20-day return <= -10pct (deep sudden dip)."""
    tx = pd.read_csv(TAIEX_CACHE)
    tx["date"] = tx["date"].astype(str).str[:10]
    c = tx["close"]
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    ret20 = c / c.shift(20) - 1
    states = {}
    for i, d in enumerate(tx["date"]):
        if np.isnan(ma60.iloc[i]):
            continue
        if c.iloc[i] > ma20.iloc[i] and c.iloc[i] > ma60.iloc[i]:
            st = "risk_on"
        elif c.iloc[i] > ma60.iloc[i]:
            st = "mid"
        else:
            st = "below60"
        states[d] = (st, bool(ret20.iloc[i] <= -0.10))
    return states


def stat_line(label, out):
    if out is None or len(out) == 0:
        print("  %-34s n=   0" % label)
        return
    r = out["ret"].to_numpy()
    days = sorted(out["date"].unique())
    mid = days[len(days) // 2] if days else ""
    h1 = out[out["date"] < mid]["ret"].to_numpy()
    h2 = out[out["date"] >= mid]["ret"].to_numpy()
    print("  %-34s n=%5d win=%5.1f%% mean=%+6.2f h1=%4.1f h2=%4.1f worst=%+6.1f"
          % (label, len(r), 100 * (r > 0).mean(), r.mean(),
             100 * (h1 > 0).mean() if len(h1) else 0,
             100 * (h2 > 0).mean() if len(h2) else 0, r.min()))


def yearly(label, out):
    if out is None or len(out) == 0:
        return
    print("  %s by year:" % label)
    for y, g in out.groupby(out["date"].str[:4]):
        r = g["ret"].to_numpy()
        print("    %s n=%4d win=%5.1f%% mean=%+6.2f"
              % (y, len(r), 100 * (r > 0).mean(), r.mean()))


def support_maps(df):
    """(sid,date) -> (ma20, min20_low) computed once over the whole db."""
    d = df.rename(columns={"stock_id": "sid"}).sort_values(["sid", "date"])
    g = d.groupby("sid")
    d = d.assign(ma20=g["close"].transform(lambda s: s.rolling(20).mean()),
                 min20=g["low"].transform(lambda s: s.rolling(20).min()))
    keys = list(zip(d["sid"].astype(str), d["date"]))
    return dict(zip(keys, zip(d["ma20"], d["min20"])))


def sim_support_entry(rows, fwd, sup_of, wait=WAIT_BARS, hold=HOLD, tp=TP,
                      brk_exit=True):
    """Limit order at the support level, resting `wait` bars. Fill = first bar
    whose low touches the limit (gap-open below fills at the open). After the
    fill: intraday tp at +tp; support-break exit = daily close below the
    support -> sell next bar's open (you cannot act before you SEE the break);
    else time exit at the hold-th bar's close from the fill bar.
    Returns (fills_df, unfilled_rows_df)."""
    rets, unfilled = [], []
    for r in rows.itertuples(index=False):
        sup = sup_of(r)
        sc = getattr(r, "sig_close", None)
        if sup is None or not np.isfinite(sup) or sup <= 0 or not sc \
                or not np.isfinite(sc) or sup >= sc:
            continue                      # no support strictly below the close
        fb = fwd(r.sid, r.date, wait + hold + 1)
        if fb is None or len(fb) < 2:
            continue
        fi, e = None, None
        for i in range(min(wait, len(fb))):
            b = fb.iloc[i]
            op, lo = float(b["open"]), float(b["low"])
            if op <= sup:
                fi, e = i, op
                break
            if lo <= sup:
                fi, e = i, sup
                break
        if fi is None:
            unfilled.append(r)
            continue
        ret = None
        last = min(fi + hold, len(fb)) - 1
        for j in range(fi, last + 1):
            b = fb.iloc[j]
            if float(b["high"]) >= e * (1 + tp):
                px = max(float(b["open"]), e * (1 + tp)) if j > fi else e * (1 + tp)
                ret = px / e - 1
                break
            if brk_exit and float(b["close"]) < sup:
                if j + 1 < len(fb):
                    ret = float(fb.iloc[j + 1]["open"]) / e - 1
                else:
                    ret = float(b["close"]) / e - 1
                break
        if ret is None:
            ret = float(fb.iloc[last]["close"]) / e - 1
        rets.append((r.date, r.sid, ret * 100))
    fills = pd.DataFrame(rets, columns=["date", "sid", "ret"])
    unf = pd.DataFrame(unfilled, columns=rows.columns) if unfilled \
        else pd.DataFrame(columns=rows.columns)
    return fills, unf


def sim_open_supportstop(rows, fwd, sup_of, hold=HOLD, tp=TP):
    """Adopted next-open entry, but the stop is the user's structural one:
    daily close below support -> sell next open. tp +20 intraday kept."""
    rets = []
    for r in rows.itertuples(index=False):
        sup = sup_of(r)
        if sup is None or not np.isfinite(sup) or sup <= 0:
            continue
        fb = fwd(r.sid, r.date, hold + 1)
        if fb is None or len(fb) < hold:
            continue
        e = float(fb.iloc[0]["open"])
        if not np.isfinite(e) or e <= 0:
            continue
        ret = None
        for j in range(hold):
            b = fb.iloc[j]
            if float(b["high"]) >= e * (1 + tp):
                px = max(float(b["open"]), e * (1 + tp)) if j > 0 else e * (1 + tp)
                ret = px / e - 1
                break
            if float(b["close"]) < sup:
                if j + 1 < len(fb):
                    ret = float(fb.iloc[j + 1]["open"]) / e - 1
                else:
                    ret = float(b["close"]) / e - 1
                break
        if ret is None:
            ret = float(fb.iloc[hold - 1]["close"]) / e - 1
        rets.append((r.date, r.sid, ret * 100))
    return pd.DataFrame(rets, columns=["date", "sid", "ret"])


def main():
    eval_from = sys.argv[1] if len(sys.argv) > 1 else EVAL_FROM
    warmup = sys.argv[2] if len(sys.argv) > 2 else WARMUP
    er.DB = RESEARCH_DB
    er.WARMUP_START = warmup
    print("window: eval_from=%s warmup=%s" % (eval_from, warmup))

    print("building features from %s (be patient)..." % RESEARCH_DB)
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    states = regime_states()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    P = er.replay_selection(T)
    P = P[P["date"] >= eval_from].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(
        columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")
    P["state"] = P["date"].map(lambda d: states.get(d, ("?", False))[0])
    P["crash"] = P["date"].map(lambda d: states.get(d, ("?", False))[1])

    quality = (P["mkt"] == "OTC") & (P["rank"] < 20) \
        & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)
    allq = P[quality]                       # regime-agnostic quality picks
    core = allq[allq["state"] == "risk_on"]  # the adopted rule's entries

    stack = dict(stop=0.15, tp=TP, arm=0.06, lock=0.02)

    print("\n=== H-A: same picks + same exit stack, bucketed by TAIEX regime ===")
    print("(signal-day regime; risk_on = shipped gate, below60 = user's dip window)")
    for st in ("risk_on", "mid", "below60"):
        stat_line(st, sim_trail(allq[allq["state"] == st], fwd, **stack))
    stat_line("below60 & crash(-10%/20d)",
              sim_trail(allq[(allq["state"] == "below60") & allq["crash"]],
                        fwd, **stack))
    print()
    yearly("below60 bucket", sim_trail(allq[allq["state"] == "below60"],
                                       fwd, **stack))

    print("\n=== H-B: entry/stop variants on the ADOPTED pick set (risk_on) ===")
    sup = support_maps(df)

    def sup_ma20(r):
        v = sup.get((str(r.sid), r.date))
        return float(v[0]) if v else None

    def sup_min20(r):
        v = sup.get((str(r.sid), r.date))
        return float(v[1]) if v else None

    base = sim_trail(core, fwd, **stack)
    stat_line("A  adopted: open entry+stop15+trail", base)
    stat_line("B1 open entry + support(MA20)-break stop",
              sim_open_supportstop(core, fwd, sup_ma20))
    stat_line("B2 open entry + support(20dLow)-break stop",
              sim_open_supportstop(core, fwd, sup_min20))

    for lbl, fn in (("MA20", sup_ma20), ("20dLow", sup_min20)):
        fills, unf = sim_support_entry(core, fwd, fn)
        n_sig = len(fills) + len(unf)
        fr = 100.0 * len(fills) / n_sig if n_sig else 0
        stat_line("C  limit@%s fill+brk stop" % lbl, fills)
        print("     fill rate %.0f%% (%d/%d signals)" % (fr, len(fills), n_sig))
        if len(unf):
            missed = sim_trail(unf, fwd, **stack)
            stat_line("   ...the UNFILLED, under adopted", missed)
        # per-signal expectancy: unfilled = 0 return (cash sat idle)
        if n_sig:
            blended = fills["ret"].sum() / n_sig
            print("     per-signal expectancy: %+0.2f (vs adopted %+0.2f)"
                  % (blended, base["ret"].mean()))


if __name__ == "__main__":
    main()
