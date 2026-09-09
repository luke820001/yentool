"""
sandbox_inst_preview.py  (SANDBOX_PLAN.md hypothesis H3 -- OBSERVATION ONLY)

Directional preview of the institutional-flow overlay on CORE+ picks.
inst_trades.db is whole-market but only starts 2026-02-23, so this is a
single-regime, small-n look; per plan section 3 nothing here may be
adopted -- it only gets recorded. Full validation ~2026-09 when the
window reaches 6 months.

Run:  python sandbox_inst_preview.py
ASCII only.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import regime_map
from eval_winrate_search import FULL_WARMUP
from eval_winrate_round2 import sim_trail

INST_DB = "data/inst_trades.db"
FROM = "2026-03-02"      # inst data starts 02-23; leave a 5-bar lookback
STACK = dict(stop=0.15, tp=0.20, arm=0.06, lock=0.02)


def inst_features():
    con = sqlite3.connect(INST_DB)
    d = pd.read_sql("SELECT stock_id AS sid, date, Foreign_Net, Trust_Net, "
                    "Inst_Net FROM data", con)
    con.close()
    d["date"] = d["date"].astype(str).str[:10]
    d = d.sort_values(["sid", "date"])
    g = d.groupby("sid")
    d["buy3"] = (d["Inst_Net"] > 0).groupby(d["sid"]).rolling(3).sum() \
        .reset_index(level=0, drop=True)
    d["net5"] = g["Inst_Net"].rolling(5).sum().reset_index(level=0, drop=True)
    d["fnet5"] = g["Foreign_Net"].rolling(5).sum().reset_index(level=0, drop=True)
    d["tnet5"] = g["Trust_Net"].rolling(5).sum().reset_index(level=0, drop=True)
    return d[["sid", "date", "buy3", "net5", "fnet5", "tnet5"]]


def show(label, m):
    if len(m) < 15:
        print("  %-34s n=%3d  (too small)" % (label, len(m)))
        return
    print("  %-34s n=%3d win=%4.1f%% mean=%+5.2f"
          % (label, len(m), 100 * (m["ret"] > 0).mean(), m["ret"].mean()))


def main():
    df, T = er.build_features()
    T["ls"] = er.launch_score(T)
    fwd = er.make_fwd(df)
    reg = regime_map()

    names = json.load(open(er.NAMES, encoding="utf-8"))
    market = {k: (v[1] if isinstance(v, list) and len(v) > 1 else "?")
              for k, v in names.items()}

    er.WARMUP_START = FULL_WARMUP
    P = er.replay_selection(T)
    P = P[P["date"] >= FROM].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    P = P.merge(T[["date", "sid", "ret5", "dist52"]], on=["date", "sid"],
                how="left")
    core = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)
             & (P["dist52"] <= 0.05) & (P["ret5"] <= 0.05)].copy()

    core = core.merge(inst_features(), on=["sid", "date"], how="left")
    sim = sim_trail(core, fwd, **STACK)
    core = core.merge(sim, on=["date", "sid"], how="inner")
    have = core[core["buy3"].notna()]

    print("=== H3 preview: CORE+ x institutional flow, %s+ ===" % FROM)
    print("trades with inst data: %d/%d" % (len(have), len(core)))
    show("ALL (baseline this window)", core)
    show("inst bought 3/3 days", have[have["buy3"] == 3])
    show("inst bought >=2/3 days", have[have["buy3"] >= 2])
    show("inst net5 > 0", have[have["net5"] > 0])
    show("inst net5 <= 0", have[have["net5"] <= 0])
    show("foreign net5 > 0", have[have["fnet5"] > 0])
    show("trust net5 > 0", have[have["tnet5"] > 0])
    show("foreign AND trust net5 > 0",
         have[(have["fnet5"] > 0) & (have["tnet5"] > 0)])
    print("(observation only -- do not adopt; revisit ~2026-09)")


if __name__ == "__main__":
    main()
