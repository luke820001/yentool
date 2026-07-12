"""
tdcc_history.py -- historical TDCC shareholding-distribution collector.

The open-data endpoint (tdcc_holders.py) only serves the LATEST weekly
snapshot, but the TDCC portal's per-stock query can be driven
programmatically and reaches back ~51 weeks with the full 15-level table
(holder count / shares / percent per level). This collector builds the
panel that the big-holder behaviour study (docs/SANDBOX_PLAN.md H5)
needs, into data/tdcc_history.db:

    levels(date TEXT, stock_id TEXT, level INTEGER,
           holders INTEGER, shares INTEGER, pct REAL)

Design constraints:
  * one request per (stock, week) -- ~33k requests for the full universe,
    so the collector is RESUMABLE: (stock_id, date) pairs already stored
    are skipped, progress is committed after every stock, and the script
    can be re-run any number of times.
  * polite: single session, configurable sleep between requests, retry
    with backoff on failures, gives up on a stock after repeated errors.
  * OTC stocks first (the strategy trades OTC), then TSE.

Run:  python -m ingestion.tdcc_history            (full universe)
      python -m ingestion.tdcc_history 3088 6223  (specific stocks)
ASCII only.
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = Path(__file__).resolve().parent.parent
DB_OUT = BASE / "data" / "tdcc_history.db"
PV_DB = BASE / "data" / "price_volume.db"
NAMES = BASE / "data" / "stock_names.json"

URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
HEADERS = {"User-Agent": "Mozilla/5.0"}
SLEEP = 0.35            # seconds between requests
RETRIES = 3
ROW_RE = re.compile(
    r"<td[^>]*>\s*(\d{1,2})\s*</td>\s*"          # level 1..15
    r"<td[^>]*>\s*([\d,\-]+)\s*</td>\s*"         # range label (ignored)
    r"<td[^>]*>\s*([\d,]+)\s*</td>\s*"           # holders
    r"<td[^>]*>\s*([\d,]+)\s*</td>\s*"           # shares
    r"<td[^>]*>\s*([\d\.]+)\s*</td>", re.S)


def _num(s):
    return int(str(s).replace(",", "") or 0)


class Collector:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.token = ""
        self.dates = []

    def refresh_form(self):
        r = self.s.get(URL, timeout=30, verify=False)
        r.raise_for_status()
        m = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("no SYNCHRONIZER_TOKEN on form page")
        self.token = m.group(1)
        seen = set()
        self.dates = [d for d in re.findall(r'value="(\d{8})"', r.text)
                      if not (d in seen or seen.add(d))]

    def fetch(self, sid, ymd):
        """One (stock, week) table -> list of (level, holders, shares, pct)."""
        for attempt in range(RETRIES):
            try:
                r = self.s.post(URL, timeout=30, verify=False, data={
                    "SYNCHRONIZER_TOKEN": self.token,
                    "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
                    "method": "submit", "firDate": self.dates[0],
                    "scaDate": ymd, "sqlMethod": "StockNo",
                    "stockNo": sid, "stockName": "",
                })
                r.raise_for_status()
                rows = [(int(lv), _num(h), _num(sh), float(p))
                        for lv, _rng, h, sh, p in ROW_RE.findall(r.text)
                        if int(lv) <= 15]
                # token rotates per response
                m = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"',
                              r.text)
                if m:
                    self.token = m.group(1)
                return rows
            except Exception:
                time.sleep(1.5 * (attempt + 1))
                try:
                    self.refresh_form()
                except Exception:
                    pass
        return None


def universe():
    names = json.load(open(NAMES, encoding="utf-8"))
    with sqlite3.connect(PV_DB) as conn:
        sids = [r[0] for r in conn.execute(
            "SELECT DISTINCT stock_id FROM data")]
    def mkt(s):
        v = names.get(s)
        return v[1] if isinstance(v, list) and len(v) > 1 else "?"
    otc = [s for s in sids if mkt(s) == "OTC"]
    tse = [s for s in sids if mkt(s) == "TSE"]
    return otc + tse          # OTC first


def main(args):
    con = sqlite3.connect(DB_OUT)
    con.execute("CREATE TABLE IF NOT EXISTS levels ("
                "date TEXT, stock_id TEXT, level INTEGER, holders INTEGER, "
                "shares INTEGER, pct REAL, "
                "PRIMARY KEY (date, stock_id, level))")
    have = set(con.execute(
        "SELECT DISTINCT stock_id || '|' || date FROM levels"))
    have = {t[0] for t in have}

    c = Collector()
    c.refresh_form()
    print("weeks available: %d (%s .. %s)"
          % (len(c.dates), c.dates[-1], c.dates[0]))

    sids = args if args else universe()
    total_req = done = skipped = failed = 0
    t0 = time.time()
    for i, sid in enumerate(sids):
        wrote = 0
        for ymd in c.dates:
            iso = "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:8])
            if sid + "|" + iso in have:
                skipped += 1
                continue
            rows = c.fetch(sid, ymd)
            total_req += 1
            time.sleep(SLEEP)
            if rows is None:
                failed += 1
                continue
            if rows:
                con.executemany(
                    "INSERT OR REPLACE INTO levels VALUES (?,?,?,?,?,?)",
                    [(iso, sid, lv, h, sh, p) for lv, h, sh, p in rows])
                wrote += 1
            done += 1
        con.commit()
        if wrote or (i % 20 == 0):
            rate = total_req / max(time.time() - t0, 1)
            print("[%d/%d] %s wrote %d weeks | req=%d skip=%d fail=%d "
                  "(%.1f req/s)" % (i + 1, len(sids), sid, wrote,
                                    total_req, skipped, failed, rate),
                  flush=True)
    con.commit()
    n = con.execute("SELECT COUNT(*), COUNT(DISTINCT stock_id), "
                    "COUNT(DISTINCT date) FROM levels").fetchone()
    print("done: rows=%d stocks=%d weeks=%d (failed reqs: %d)"
          % (n[0], n[1], n[2], failed))


if __name__ == "__main__":
    main(sys.argv[1:])
