"""
sandbox_r4_cooldown.py -- round 4, P2-3 cooldown definitions (2026-10-08). ASCII only.

The cleaned-up successor of two scratch prototypes (research_cooldown.py and
control_R.py, 2026-10-08), rebuilt on the round-4 harness (r4_common): the
same trades, the same shipped-rule replay (r4_common.replay = sandbox_money
BASE, built from scanner.exit_rules.DEFAULT_RULE), the same session calendar.
No exit threshold is written here, and nothing in eval_realtrade is assigned.

A COOLDOWN blocks a signal on day `sig` when a PRIOR trade on the same name
is still open, or exited N or fewer sessions before `sig`:

    gap = session_index(sig) - session_index(prior exit)
    blocked  <=>  a prior exists and gap <= N
    N = -1  "open": the prior is still held after the signal close (it exits
            on a later session) -- the overlap that double-allocates a slot
    N =  0  also blocks a prior that exited ON the signal day (the
            prototype's N=0)

What counts as "the prior trade":

  Def B  (prior research signal) the last KEPT research trade of the same
         name (sequential: a blocked signal is never bought, so it never
         becomes anyone's prior; docs brief research.md P2-3).
         keep_prior_signal(..., sequential=False) is the prototype's naive
         reading (the previous signal, kept or not), for reproduction only.
  Def A  (prior list-streak trade, the OLD holding tracker) the trade
         entered at the next open after the start of the name's current
         list streak, where a streak tolerates absences of up to GAP_TOL
         sessions (holding_tracker._streak_start as of git HEAD, gap_tol =
         hold). The anchor may be a PHANTOM: a list day that was never a
         signal (failed CORE+ / regime / board). Only a streak that started
         BEFORE sig gives a prior.
  Def A2 (chained segments, the NEW tracker of stage B1) as A, but a
         re-entry day inside a streak re-anchors the segment when the
         segment's trade had already exited by that day or its anchor was
         not a signal; the prior of `sig` is the segment in force just
         before `sig`.
  *_phantom  block only when the prior's anchor was NOT a signal.

    python archive/research/sandbox_r4_cooldown.py selftest   # synthetic checks
    python archive/research/sandbox_r4_cooldown.py proto      # prototype tables
Run with PYTHONDONTWRITEBYTECODE=1. The gated study is sandbox_r4_p23.py.
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import r4_common as c                               # noqa: E402  (chdir ROOT)

GAP_TOL = c.R["hold_bars"]     # the tracker's gap_tol is its hold (10)
N_OPEN = -1                    # "open overlap only"


def _say(*a):
    print(*a)
    sys.stdout.flush()


# =============================================================== streaks
def streak_start(bar_dates, idx_of, gap_tol=GAP_TOL):
    """Earliest date of the latest block of `bar_dates` whose consecutive
    members are at most `gap_tol` sessions apart (holding_tracker.
    _streak_start as of git HEAD; stage B1 replaces it in the live tree)."""
    known = [d for d in bar_dates if d in idx_of]
    if not known:
        return None
    start = known[-1]
    for earlier in reversed(known[:-1]):
        if idx_of[start] - idx_of[earlier] <= gap_tol:
            start = earlier
        else:
            break
    return start


def list_dates(P, cal_idx):
    """{sid: sorted list dates on the session calendar} and the set of
    (sid, date) that are re-entry days (streak == 1)."""
    Q = P[P["date"].isin(set(cal_idx))]
    dates = {s: sorted(g["date"]) for s, g in Q.groupby("sid")}
    first = set(zip(Q.loc[Q["streak"] == 1, "sid"], Q.loc[Q["streak"] == 1, "date"]))
    return dates, first


# ============================================================ prior trades
class Trades:
    """Shipped-rule replays of arbitrary (sid, list day) entries: the entry
    is the next session's open, forward bars on the session calendar with
    Volume_Lot > 0 (r4_common.build_universe's rule). Base trades are
    served from the base frame so a signal anchor is the very same trade."""

    def __init__(self, sids, base_df=None, cal=None):
        cal = c.trade_calendar() if cal is None else cal
        cs = set(cal)
        raw = c.load_bars(sorted(set(str(s) for s in sids)))
        raw = raw[raw["date"].isin(cs) & (raw["Volume_Lot"] > 0)]
        self.bars = {}
        for sid, b in raw.groupby("stock_id"):
            self.bars[sid] = (b["date"].to_numpy().astype("U10"),
                              b["open"].to_numpy(float), b["high"].to_numpy(float),
                              b["low"].to_numpy(float), b["close"].to_numpy(float))
        self.memo = {}
        self.replayed = 0
        if base_df is not None:
            for r in base_df[["sid", "sig", "exit", "why", "ret"]].itertuples(index=False):
                self.memo[(str(r.sid), r.sig)] = dict(exit=r.exit, why=r.why, ret=float(r.ret),
                                                      base=True)

    def make(self, sid, d, rank=-1):
        b = self.bars.get(str(sid))
        if b is None:
            return None
        dates, o, h, lo, cl = b
        i = int(np.searchsorted(dates, d))
        if i >= len(dates) or dates[i] != d:
            return None
        s = slice(i + 1, i + 1 + c.g.FWD_BARS)
        if len(dates[s]) < c.g.HOLD:
            return None
        return {"sig": d, "sid": str(sid), "rank": int(rank), "atr": None,
                "dates": dates[s].tolist(), "o": o[s], "h": h[s], "l": lo[s], "c": cl[s]}

    def get(self, sid, d):
        key = (str(sid), d)
        if key not in self.memo:
            t = self.make(sid, d)
            if t is None:
                self.memo[key] = None
            else:
                r = c.replay(t)
                self.replayed += 1
                self.memo[key] = dict(exit=r["exit"], why=r["why"], ret=float(r["ret"]),
                                      base=False)
        return self.memo[key]


# ================================================================ Def B
def keep_prior_signal(df, idx_of, n, sequential=True):
    """Def B keep mask (aligned with df) and the gap used per row.
    sequential: only KEPT trades update the name's last exit."""
    sid = df["sid"].astype(str).to_numpy()
    sig = df["sig"].to_numpy()
    ex = df["exit"].to_numpy()
    rank = df["rank"].to_numpy()
    order = sorted(range(len(df)), key=lambda i: (sid[i], sig[i], rank[i]))
    keep = np.ones(len(df), bool)
    gap = np.full(len(df), np.nan)
    last = {}
    for i in order:
        s = sid[i]
        if s in last:
            gap[i] = idx_of[sig[i]] - idx_of[last[s]]
            if gap[i] <= n:
                keep[i] = False
        if keep[i] or not sequential:
            last[s] = ex[i]
    return keep, gap


# ================================================================ Def A
def prior_list_streak(df, P, idx_of, trades, signal_keys, gap_tol=GAP_TOL):
    """Def A (old tracker): per row of df the streak anchor, whether that
    anchor was a signal, and the anchor trade's exit / reason / return /
    gap. A row whose streak starts on sig itself has no prior (gap NaN)."""
    dates, _ = list_dates(P, idx_of)
    rows = []
    for sid, sig in zip(df["sid"].astype(str), df["sig"]):
        ds = dates.get(sid, [])
        ds = ds[:int(np.searchsorted(ds, sig, side="right"))]
        anc = streak_start(ds, idx_of, gap_tol)
        rec = dict(anchor=anc, anchor_signal=None, prior_exit=None, prior_why=None,
                   prior_ret=np.nan, gap=np.nan, missing=False)
        if anc is not None and anc < sig:
            rec["anchor_signal"] = (sid, anc) in signal_keys
            p = trades.get(sid, anc)
            if p is None:
                rec["missing"] = True
            else:
                rec.update(prior_exit=p["exit"], prior_why=p["why"], prior_ret=p["ret"],
                           gap=idx_of[sig] - idx_of[p["exit"]])
        rows.append(rec)
    return pd.DataFrame(rows, index=df.index)


def prior_segment_chain(df, P, idx_of, trades, signal_keys, gap_tol=GAP_TOL):
    """Def A2 (stage B1 segments): walk each name's list days; a gap of more
    than gap_tol sessions starts a natural segment; a re-entry day (streak
    == 1) re-anchors when the segment's trade exited on or before it or the
    segment's anchor was not a signal. The prior of a df row is the segment
    in force just before its signal day (None after a natural break)."""
    dates, first = list_dates(P, idx_of)
    want = {}
    for i, (sid, sig) in enumerate(zip(df["sid"].astype(str), df["sig"])):
        want.setdefault(sid, {})[sig] = i
    out = [dict(anchor=None, anchor_signal=None, prior_exit=None, prior_why=None,
                prior_ret=np.nan, gap=np.nan, missing=False, reanchors=0)
           for _ in range(len(df))]
    for sid, rowsig in want.items():
        seg, last, nre = None, None, 0
        for d in dates.get(sid, []):
            if seg is not None and idx_of[d] - idx_of[last] > gap_tol:
                seg, nre = None, 0
            if d in rowsig and seg is not None:
                rec = out[rowsig[d]]
                rec.update(anchor=seg, anchor_signal=(sid, seg) in signal_keys, reanchors=nre)
                p = trades.get(sid, seg)
                if p is None:
                    rec["missing"] = True
                else:
                    rec.update(prior_exit=p["exit"], prior_why=p["why"], prior_ret=p["ret"],
                               gap=idx_of[d] - idx_of[p["exit"]])
            if seg is None:
                seg = d
            elif (sid, d) in first:
                p = trades.get(sid, seg)
                done = p is None or p["exit"] <= d
                if done or (sid, seg) not in signal_keys:
                    seg, nre = d, nre + 1
            last = d
    return pd.DataFrame(out, index=df.index)


def keep_prior(info, n, phantom_only=False, signal_only=False):
    """Keep mask from a Def A / A2 info frame."""
    g = info["gap"].to_numpy(float)
    blk = ~np.isnan(g) & (np.nan_to_num(g, nan=1e9) <= n)
    sig = info["anchor_signal"].fillna(False).to_numpy(bool)
    if phantom_only:
        blk &= ~sig
    if signal_only:
        blk &= sig
    return ~blk


# =============================================================== commands
def cmd_selftest():
    """research.md P2-3 test cases on a synthetic calendar."""
    cal = pd.bdate_range("2024-01-01", "2024-03-29").strftime("%Y-%m-%d").tolist()
    idx = {d: i for i, d in enumerate(cal)}

    def frame(rows):
        return pd.DataFrame(rows, columns=["sid", "sig", "exit", "rank"])

    ok = True

    def check(name, got, want):
        nonlocal ok
        good = list(map(bool, got)) == want
        ok = ok and good
        _say("  %-58s %s %s" % (name, "ok" if good else "FAIL", list(map(bool, got))))

    # trade 1 exits 2024-01-10; trade 2 signals 2024-01-08 (still held)
    df = frame([("1", "2024-01-02", "2024-01-10", 0), ("1", "2024-01-08", "2024-01-22", 0)])
    check("re-signal while held: dropped at N=0", keep_prior_signal(df, idx, 0)[0], [True, False])
    check("re-signal while held: dropped at N=-1 (open)", keep_prior_signal(df, idx, -1)[0],
          [True, False])
    # trade 2 signals 3 sessions after the exit
    df = frame([("1", "2024-01-02", "2024-01-10", 0), ("1", "2024-01-15", "2024-01-29", 0)])
    check("3 sessions after the exit: kept at N=0", keep_prior_signal(df, idx, 0)[0], [True, True])
    check("3 sessions after the exit: dropped at N=5", keep_prior_signal(df, idx, 5)[0],
          [True, False])
    # exit on the signal day: N=0 blocks, open-only does not
    df = frame([("1", "2024-01-02", "2024-01-10", 0), ("1", "2024-01-10", "2024-01-24", 0)])
    check("exit on the signal day: N=0 blocks", keep_prior_signal(df, idx, 0)[0], [True, False])
    check("exit on the signal day: open-only keeps", keep_prior_signal(df, idx, -1)[0],
          [True, True])
    # a dropped trade does not reset last_exit
    df = frame([("1", "2024-01-02", "2024-01-10", 0), ("1", "2024-01-15", "2024-01-31", 0),
                ("1", "2024-01-18", "2024-02-01", 0)])
    check("dropped trade does not reset last_exit (3rd kept at N=5)",
          keep_prior_signal(df, idx, 5)[0], [True, False, True])
    check("naive reading: 3rd measured from the dropped 2nd",
          keep_prior_signal(df, idx, 5, sequential=False)[0], [True, False, False])
    # other names never interact
    df = frame([("1", "2024-01-02", "2024-01-10", 0), ("2", "2024-01-08", "2024-01-22", 0)])
    check("different names are independent", keep_prior_signal(df, idx, 20)[0], [True, True])
    # streak_start
    d = ["2024-01-02", "2024-01-03", "2024-01-12", "2024-02-05"]
    good = (streak_start(d[:3], idx, 10) == "2024-01-02" and streak_start(d, idx, 10) == "2024-02-05"
            and streak_start(d[:3], idx, 6) == "2024-01-12")
    ok = ok and good
    _say("  %-58s %s" % ("streak_start gap_tol", "ok" if good else "FAIL"))

    # Def A / A2 on a hand-made list: phantom anchor, then two re-entries
    P = pd.DataFrame([("9", "2024-01-02", 1), ("9", "2024-01-03", 2), ("9", "2024-01-09", 1),
                      ("9", "2024-01-16", 1)], columns=["sid", "date", "streak"])

    class T:
        ex = {"2024-01-02": "2024-01-05", "2024-01-09": "2024-01-23", "2024-01-16": "2024-01-30"}

        def get(self, sid, d):
            return dict(exit=self.ex[d], why="time", ret=0.0)
    sig = pd.DataFrame({"sid": ["9"], "sig": ["2024-01-16"]})
    a = prior_list_streak(sig, P, idx, T(), signal_keys={("9", "2024-01-09")})
    a2 = prior_segment_chain(sig, P, idx, T(), signal_keys={("9", "2024-01-09")})
    good = (a["anchor"].iloc[0] == "2024-01-02" and a["gap"].iloc[0] == 7
            and not a["anchor_signal"].iloc[0]
            and a2["anchor"].iloc[0] == "2024-01-09" and a2["gap"].iloc[0] == -5
            and bool(a2["anchor_signal"].iloc[0]))
    ok = ok and good
    _say("  %-58s %s (A anchor %s gap %s | A2 anchor %s gap %s)"
         % ("Def A vs A2 on a phantom-then-signal chain", "ok" if good else "FAIL",
            a["anchor"].iloc[0], a["gap"].iloc[0], a2["anchor"].iloc[0], a2["gap"].iloc[0]))
    _say("selftest %s" % ("PASSED" if ok else "FAILED"))
    return ok


def cmd_proto():
    """The prototype tables (fresh.md P2-3) on the harness: Def B naive and
    Def A at N = 0/5/10/20, removed / kept per window."""
    ts, b0, b1 = c.base()
    cal = c.trade_calendar()
    idx = c.session_index(cal)
    P = pd.read_pickle(os.path.join(c.CACHE, "sel_P_research_n20.pkl"))
    tr = Trades(set(b0["sid"]), b0, cal)
    keys = set(zip(b0["sid"].astype(str), b0["sig"]))
    A = prior_list_streak(b0, P, idx, tr, keys)
    _, gb = keep_prior_signal(b0, idx, 10 ** 6, sequential=False)
    _say("Def B naive: %d with a prior, %d still open, median gap %.1f"
         % (int(np.isfinite(gb).sum()), int((gb < 0).sum()), np.nanmedian(gb)))
    ga = A["gap"].to_numpy(float)
    _say("Def A: %d with a prior, %d still open, median gap %.1f, %d anchor trades replayed"
         % (int(np.isfinite(ga).sum()), int((ga < 0).sum()), np.nanmedian(ga), tr.replayed))
    for name, keep_of in (("B naive", lambda n: keep_prior_signal(b0, idx, n, False)[0]),
                          ("B seq", lambda n: keep_prior_signal(b0, idx, n)[0]),
                          ("A", lambda n: keep_prior(A, n))):
        for n in (0, 5, 10, 20):
            k = keep_of(n)
            parts = []
            for w in c.WINDOWS:
                wm = c.wmask(b0, w)
                rm, kp = c.stats(b0[wm & ~k]), c.stats(b0[wm & k])
                parts.append("%s removed %3d %5.1f%% %+5.2f kept %5.1f%% %+5.2f"
                             % (w, rm["n"], rm["win"], rm["mean"], kp["win"], kp["mean"]))
            _say("  %-7s N=%2d  %s" % (name, n, " | ".join(parts)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    globals()["cmd_" + cmd]()
