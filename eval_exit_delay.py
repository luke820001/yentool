"""
eval_exit_delay.py

Does delaying the time exit when the MARKET is disturbed actually help? A fixed
10-bar exit can dump a position into a short geopolitical shock. This tests a
rule-based alternative (no AI, fully backtestable):

  base:  enter next open, -10% disaster stop, exit close of bar 10.
  delay: same, BUT if the TAIEX is "disturbed" on the scheduled exit day, keep
         holding until it calms (or a hard cap), then exit at that close.

"Disturbed" is tested two ways -- TAIEX below its 20MA (fast) or below its 60MA
(slow). The disaster stop stays active the whole time. We judge on the full
214-day OTC replay by win / mean / alpha / both-window-halves, so a lift that is
noise or one-regime is exposed. Adopt only if it beats fixed on all of them.

Run: python eval_exit_delay.py
ASCII only.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

import eval_realtrade as er

FULL_WARMUP = "2025-06-02"
FULL_START = "2025-08-01"
BASE_HOLD = 10
DISASTER = 0.10


def taiex_flags():
    con = sqlite3.connect(er.TAIEX_DB)
    tx = pd.read_sql("SELECT date, close FROM TAIEX ORDER BY date", con)
    con.close()
    tx["date"] = tx["date"].astype(str).str[:10]
    c = pd.to_numeric(tx["close"], errors="coerce")
    tx["below20"] = c < c.rolling(20).mean()
    tx["below60"] = c < c.rolling(60).mean()
    return (tx.set_index("date")["below20"].to_dict(),
            tx.set_index("date")["below60"].to_dict())


def sim_exit(fwd, sid, date, disturbed, cap):
    """Return trade pct. Enter next open; -10% disaster stop across the hold;
    base exit at bar BASE_HOLD; if `disturbed` (a {date->bool} map) is True on the
    exit day, roll forward to the first calm day, capped at bar `cap`."""
    fb = fwd(sid, date, cap)
    if fb is None or len(fb) < BASE_HOLD:
        return None
    e = float(fb.iloc[0]["open"])
    if not np.isfinite(e) or e <= 0:
        return None
    stop = e * (1 - DISASTER)
    n = len(fb)
    # choose exit bar index (0-based): start at BASE_HOLD-1, roll while disturbed
    idx = BASE_HOLD - 1
    if disturbed is not None:
        while idx < n - 1 and disturbed.get(str(fb.iloc[idx]["date"])[:10], False):
            idx += 1
    # disaster stop can end the trade earlier than idx
    for i in range(0, idx + 1):
        b = fb.iloc[i]
        if float(b["low"]) <= stop:
            px = stop if i == 0 else min(float(b["open"]), stop)
            return px / e - 1
    return float(fb.iloc[idx]["close"]) / e - 1


def run(P, fwd, disturbed, cap):
    rows = []
    for r in P.itertuples(index=False):
        ret = sim_exit(fwd, r.sid, r.date, disturbed, cap)
        if ret is not None:
            rows.append((r.date, ret * 100))
    return pd.DataFrame(rows, columns=["date", "ret"])


def stat(out):
    if out.empty:
        return None
    r = out["ret"].to_numpy()
    days = sorted(out["date"].unique())
    mid = days[len(days) // 2]
    h1 = out[out["date"] < mid]["ret"].to_numpy()
    h2 = out[out["date"] >= mid]["ret"].to_numpy()
    return dict(n=len(r), win=100 * (r > 0).mean(), mean=r.mean(),
               worst=r.min(),
               h1=100 * (h1 > 0).mean() if len(h1) else 0,
               h2=100 * (h2 > 0).mean() if len(h2) else 0)


def show(label, out):
    s = stat(out)
    if not s:
        print("%-28s n=0" % label); return
    print("%-28s n=%4d win=%4.1f%% mean=%+5.2f worst=%+6.1f h1=%4.1f h2=%4.1f"
          % (label, s["n"], s["win"], s["mean"], s["worst"], s["h1"], s["h2"]))


def main():
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    below20, below60 = taiex_flags()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    er.WARMUP_START = FULL_WARMUP
    P = er.replay_selection(T)
    P = P[P["date"] >= FULL_START].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    otc = P[P["mkt"] == "OTC"]

    print("=== exit-delay test | OTC | base hold %d | -10%% disaster stop ===" % BASE_HOLD)
    show("fixed hold10 (current)", run(otc, fwd, None, BASE_HOLD))
    for cap in (15, 20):
        show("delay if <20MA, cap %d" % cap, run(otc, fwd, below20, cap))
        show("delay if <60MA, cap %d" % cap, run(otc, fwd, below60, cap))
    print("\nADOPT only if a delay row beats fixed on win AND mean AND both halves.")


if __name__ == "__main__":
    main()
