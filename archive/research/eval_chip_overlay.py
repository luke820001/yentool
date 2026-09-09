"""
eval_chip_overlay.py

DIRECTIONAL check: does institutional flow (inst_trades.db) add win rate on top
of the adopted CORE+ rule? inst_trades only reaches back to 2026-02-23, so this
CANNOT clear the full-window adoption bar -- treat any lift as observe-only
until the table has grown enough to re-run over a full year.

Run:  python eval_chip_overlay.py
ASCII only.
"""
import json
import sqlite3

import pandas as pd

import eval_realtrade as er
from eval_prelaunch_overlays import regime_map
from eval_winrate_search import simulate2, show, FULL_WARMUP

WINDOW_START = "2026-03-02"   # first date with a full 5-bar flow lookback


def flow_features():
    con = sqlite3.connect("data/inst_trades.db")
    f = pd.read_sql("SELECT date, stock_id AS sid, Foreign_Net, Trust_Net, "
                    "Inst_Net FROM data ORDER BY sid, date", con)
    con.close()
    f["date"] = f["date"].astype(str).str[:10]
    f["sid"] = f["sid"].astype(str)
    for c in ("Foreign_Net", "Trust_Net", "Inst_Net"):
        f[c] = pd.to_numeric(f[c], errors="coerce").fillna(0.0)
    g = f.groupby("sid")
    f["fn5"] = g["Foreign_Net"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    f["tn5"] = g["Trust_Net"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    f["in5"] = g["Inst_Net"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    f["buyd"] = g["Foreign_Net"].transform(
        lambda s: (s > 0).rolling(5, min_periods=5).sum())
    return f[["date", "sid", "fn5", "tn5", "in5", "buyd"]]


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
    P = P[P["date"] >= WINDOW_START].copy()
    P["mkt"] = P["sid"].map(market).fillna("?")
    P["ro"] = P["date"].map(lambda d: reg.get(d, False))
    feat = T[["date", "sid", "c", "ret5", "dist52"]].rename(columns={"c": "sig_close"})
    P = P.merge(feat, on=["date", "sid"], how="left")
    P = P.merge(flow_features(), on=["date", "sid"], how="left")

    base = P[(P["mkt"] == "OTC") & P["ro"] & (P["rank"] < 20)]
    core = base[(base["dist52"] <= 0.05) & (base["ret5"] <= 0.05)]

    print("=== window %s+ (inst_trades coverage), hold10 plain ===" % WINDOW_START)
    print("(DIRECTIONAL ONLY -- window too short for adoption)\n")
    show("base (OTC ro rank<20)", simulate2(base, fwd))
    show("CORE+", simulate2(core, fwd))
    print()
    for lbl, m in (
        ("CORE+ & fn5>0", core["fn5"] > 0),
        ("CORE+ & fn5<=0 (control)", core["fn5"] <= 0),
        ("CORE+ & buyd>=3", core["buyd"] >= 3),
        ("CORE+ & tn5>0", core["tn5"] > 0),
        ("CORE+ & in5>0", core["in5"] > 0),
        ("CORE+ & no flow data", core["fn5"].isna()),
    ):
        show(lbl, simulate2(core[m if not m.isna().all() else m.fillna(False)], fwd))


if __name__ == "__main__":
    main()
