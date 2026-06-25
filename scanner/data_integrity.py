"""
Per-stock data-integrity audit for the price series. ASCII only.

Garbage in, garbage out: every score in this scanner is a deterministic function
of the stored OHLCV, so a single bad bar silently corrupts MA / breakout / RS /
forward-return for that name. This module checks each series against HARD,
non-speculative rules and reports -- it never alters prices or scores.

Rules (all checkable, no guessing):
  * NaN / non-positive close, open, high, low                  -> data error
  * OHLC ordering: low <= open/close <= high, low <= high      -> data error
  * duplicate (stock_id, date) rows                            -> data error
  * close-to-close move > +-10.5%                              -> suspect bar
        Taiwan main/OTC boards cap a day at +-10%, so a larger move is an
        un-adjusted corporate action (capital reduction / stock dividend), a
        feed glitch, or a no-price-limit emerging/innovation-board stock. We
        flag it; whether to trade it is the caller's policy, not ours.
  * internal trading-day gaps (missing bars inside the range)  -> gap
  * fewer bars than a window needs (MA60 / 52w-RS)             -> short history

`trustworthy` is conservative: it is False ONLY for unambiguous data errors
(NaN / OHLC / duplicate). Suspect jumps, gaps and short history are surfaced as
flags so the caller can decide, because -- as verified on the live db -- a >10%
jump is often a legitimate move, not an error.
"""
import pandas as pd

# Taiwan daily price limit is +-10%; 10.5% leaves room for tick rounding.
LIMIT_JUMP    = 0.105
MIN_BARS_MA60 = 60     # below this the 60-day MA (a trend gate) is not real
MIN_BARS_52W  = 240    # below this 52-week-high / 63-day RS are weak
RECENT_BARS   = 60     # a suspect jump within this many bars still taints the
                       # longest MA used as a live gate (and all shorter ones)

_PRICE_COLS = ("open", "high", "low", "close")


def audit_series(df: pd.DataFrame, calendar=None) -> dict:
    """
    Audit ONE stock's price history (a DataFrame with date/open/high/low/close).
    Returns a dict of measured facts plus `trustworthy` and a `flags` list.
    Pure: no I/O, no mutation of the input.

    `calendar`, when given, is the set of real trading dates (e.g. the union of
    every stock's dates across the whole market). Internal gaps are then counted
    EXACTLY -- a missing bar is a calendar date inside the stock's own range that
    the stock lacks -- instead of a holiday-fooled day-difference heuristic.
    Without a calendar the gap check is skipped (reported as 0) rather than
    guessed.
    """
    out = {
        "bars":           0,
        "nan":            0,
        "nonpositive":    0,
        "ohlc_bad":       0,
        "dup_dates":      0,
        "jumps":          0,   # bars with |close-to-close| > LIMIT_JUMP
        "recent_jump":    False,
        "last_jump_date": None,
        "gaps":           0,   # missing trading days inside the stock's range
        "short_ma60":     True,
        "short_52w":      True,
        "trustworthy":    False,
        "flags":          [],
    }
    if df is None or df.empty:
        out["flags"] = ["empty"]
        return out

    d = df[["date"] + [c for c in _PRICE_COLS if c in df.columns]].copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    for c in _PRICE_COLS:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.sort_values("date").reset_index(drop=True)

    out["bars"] = len(d)
    out["short_ma60"] = len(d) < MIN_BARS_MA60
    out["short_52w"] = len(d) < MIN_BARS_52W

    close = d.get("close")
    out["nan"] = int(d[list(_PRICE_COLS)].isna().any(axis=1).sum()
                     if all(c in d.columns for c in _PRICE_COLS) else
                     close.isna().sum())
    if close is not None:
        out["nonpositive"] = int((close <= 0).sum())

    # OHLC ordering (only where all four exist)
    if all(c in d.columns for c in _PRICE_COLS):
        o, h, l, c = d["open"], d["high"], d["low"], d["close"]
        bad = (h < l) | (h < o) | (h < c) | (l > o) | (l > c)
        out["ohlc_bad"] = int(bad.fillna(False).sum())

    out["dup_dates"] = int(d["date"].duplicated().sum())

    # close-to-close jumps beyond the daily limit
    if close is not None and len(d) > 1:
        ret = close.pct_change()
        jump_mask = ret.abs() > LIMIT_JUMP
        out["jumps"] = int(jump_mask.fillna(False).sum())
        if out["jumps"]:
            jdates = d.loc[jump_mask.fillna(False), "date"]
            out["last_jump_date"] = jdates.max().strftime("%Y-%m-%d")
            # a jump inside the last RECENT_BARS taints the current MAs/breakout
            recent_idx = jump_mask.fillna(False).iloc[-RECENT_BARS:]
            out["recent_jump"] = bool(recent_idx.any())

    # internal trading-day gaps, counted EXACTLY against the real trading
    # calendar (the whole-market union of dates). A gap is a calendar date that
    # falls inside this stock's own [first, last] range but is missing here.
    if calendar is not None and len(d) > 1:
        cal = pd.DatetimeIndex(sorted(pd.DatetimeIndex(calendar).unique()))
        own = pd.DatetimeIndex(d["date"].dropna().unique())
        if len(own) > 1 and len(cal):
            lo, hi = own.min(), own.max()
            span = cal[(cal >= lo) & (cal <= hi)]
            out["gaps"] = int(len(span) - len(own))

    # verdict + flags
    flags = []
    data_error = (out["nan"] or out["nonpositive"]
                  or out["ohlc_bad"] or out["dup_dates"])
    if out["nan"]:         flags.append("nan:{}".format(out["nan"]))
    if out["nonpositive"]: flags.append("nonpos:{}".format(out["nonpositive"]))
    if out["ohlc_bad"]:    flags.append("ohlc:{}".format(out["ohlc_bad"]))
    if out["dup_dates"]:   flags.append("dup:{}".format(out["dup_dates"]))
    if out["jumps"]:       flags.append("jump:{}".format(out["jumps"]))
    if out["recent_jump"]: flags.append("recent_jump")
    if out["gaps"]:        flags.append("gap:{}".format(out["gaps"]))
    if out["short_ma60"]:  flags.append("short_ma60")
    elif out["short_52w"]: flags.append("short_52w")

    out["flags"] = flags
    out["trustworthy"] = not bool(data_error)
    return out


def audit_store(price_db_path=None, stock_ids=None) -> pd.DataFrame:
    """
    Audit every stock (or a subset) in price_volume.db in one pass. Returns a
    DataFrame: one row per stock with the measured facts + flags. Used by the
    standalone health report; the live scan calls audit_series per stock.
    """
    from config.settings import PRICE_VOLUME_FILE
    import sqlite3

    path = price_db_path or PRICE_VOLUME_FILE
    if not path.exists():
        return pd.DataFrame()

    q = "SELECT date,stock_id,open,high,low,close FROM data"
    params = ()
    if stock_ids:
        placeholders = ",".join("?" for _ in stock_ids)
        q += " WHERE stock_id IN ({})".format(placeholders)
        params = tuple(str(s) for s in stock_ids)
    with sqlite3.connect(path) as conn:
        df = pd.read_sql_query(q, conn, params=params)
    if df.empty:
        return pd.DataFrame()

    # The union of every stock's dates is the real trading calendar (this is a
    # whole-market db), so gaps are measured exactly, not guessed from holidays.
    calendar = pd.DatetimeIndex(
        sorted(pd.to_datetime(df["date"], errors="coerce").dropna().unique()))

    rows = []
    for sid, g in df.groupby("stock_id"):
        a = audit_series(g, calendar=calendar)
        a["stock_id"] = str(sid)
        rows.append(a)
    report = pd.DataFrame(rows)
    cols = ["stock_id", "bars", "trustworthy", "flags", "jumps",
            "recent_jump", "last_jump_date", "gaps", "nan", "ohlc_bad",
            "dup_dates", "short_ma60", "short_52w"]
    return report[[c for c in cols if c in report.columns]]
