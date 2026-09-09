"""
sandbox_confirm_live.py  (SANDBOX_PLAN.md -- C2+C3 third confirmation)

Applies the sandbox finalist (C2 taiex_str>=0.022 day condition + C3
atr_pct>=0.044 stock condition) to the LIVE price_volume.db replay -- the
same window/machinery the shipped 71% was validated on -- as the final
consistency check before wiring anything into the scanner.

Expectation from the 6-year replay: roughly neutral here (2025/2026 are
already tailwind years); the combo's value is bear/weak-year insurance.
If live shows a real regression instead, do not adopt.

Run:  python sandbox_confirm_live.py
ASCII only.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import regime_map, winrate
from eval_winrate_search import FULL_WARMUP, FULL_START
from eval_winrate_round2 import sim_trail

STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)


def live_taiex_strength():
    con = sqlite3.connect(er.TAIEX_DB)
    tx = pd.read_sql("SELECT date, close FROM TAIEX ORDER BY date", con)
    con.close()
    tx["date"] = tx["date"].astype(str).str[:10]
    tx["str20"] = tx["close"] / tx["close"].rolling(20).mean() - 1
    return tx.set_index("date")["str20"].to_dict()


def live_atr(df):
    out = []
    for sid, g in df.rename(columns={"stock_id": "sid"}).groupby("sid"):
        g = g.reset_index(drop=True)
        if len(g) < 20:
            continue
        c, h, l = g["close"], g["high"], g["low"]
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()],
                       axis=1).max(axis=1)
        out.append(pd.DataFrame({
            "date": g["date"], "sid": str(sid),
            "atr_pct": tr.rolling(14).mean() / c,
        }))
    return pd.concat(out, ignore_index=True)


def line2(label, out):
    s = winrate(out)
    if not s:
        print("%-36s n=0" % label)
        return
    print("%-36s n=%4d win=%4.1f%% bootLo=%4.1f h1=%4.1f h2=%4.1f mean=%+5.2f"
          % (label, s["n"], s["win"], s["bootLo"], s["h1"], s["h2"], s["mean"]))


def main():
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = regime_map()
    tstr = live_taiex_strength()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    er.WARMUP_START = FULL_WARMUP
    P = er.replay_selection(T)
    P = P[P["date"] >= FULL_START].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    P = P.merge(T[["date", "sid", "ret5", "dist52"]], on=["date", "sid"],
                how="left")
    core = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)
             & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)].copy()
    core = core.merge(live_atr(df), on=["date", "sid"], how="left")
    core["taiex_str"] = core["date"].map(lambda d: tstr.get(d, np.nan))

    c2 = core["taiex_str"] >= 0.022
    c3 = core["atr_pct"] >= 0.044
    s1 = core["streak"] == 1

    print("=== live-window third confirmation (%s+) ===" % FULL_START)
    line2("BASE pooled", sim_trail(core, fwd, **STACK))
    line2("C2+C3 pooled", sim_trail(core[c2 & c3], fwd, **STACK))
    line2("C3 only pooled", sim_trail(core[c3], fwd, **STACK))
    line2("BASE streak==1", sim_trail(core[s1], fwd, **STACK))
    line2("C2+C3 streak==1", sim_trail(core[s1 & c2 & c3], fwd, **STACK))
    line2("C3 only streak==1", sim_trail(core[s1 & c3], fwd, **STACK))
    b = sim_trail(core, fwd, **STACK)
    c = sim_trail(core[c2 & c3], fwd, **STACK)
    print("picks/day: base %.2f -> combo %.2f"
          % (len(b) / b["date"].nunique(), len(c) / c["date"].nunique()))


if __name__ == "__main__":
    main()
