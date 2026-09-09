"""
Data-integrity health report for price_volume.db. ASCII only.

Runs the hard, non-speculative checks in scanner.data_integrity over the whole
stored universe and prints a summary plus the names that need attention. Use it
before trusting a scan (or a backtest) -- every score is a function of this data.

    python audit_data.py
"""
from scanner.data_integrity import audit_store


def main():
    r = audit_store()
    if r.empty:
        print("price_volume.db is empty or missing.")
        return

    n = len(r)
    print("=== data-integrity report: {} stocks ===\n".format(n))

    errors = r[~r["trustworthy"]]
    print("hard data errors (NaN / OHLC / duplicate): {}".format(len(errors)))
    if not errors.empty:
        print(errors[["stock_id", "bars", "flags"]].to_string(index=False))
    print()

    jumps = r[r["jumps"] > 0]
    recent = r[r["recent_jump"]]
    print("over-limit jumps (>10.5% close-to-close): {} stocks, {} bars".format(
        len(jumps), int(r["jumps"].sum())))
    print("  -> of these, {} have a RECENT jump (<60 bars) that taints current "
          "MAs/breakout:".format(len(recent)))
    if not recent.empty:
        print(recent[["stock_id", "bars", "jumps", "last_jump_date",
                      "flags"]].to_string(index=False))
    print()

    gaps = r[r["gaps"] > 0]
    print("internal trading-day gaps: {} stocks ({} missing bars)".format(
        len(gaps), int(r["gaps"].sum())))
    short = r[r["short_ma60"]]
    print("too few bars for a real MA60 (<60): {} stocks".format(len(short)))
    if not short.empty:
        print(short[["stock_id", "bars", "flags"]].to_string(index=False))
    print("weak 52w/RS history (<240 bars): {} stocks".format(
        int(r["short_52w"].sum())))
    print()

    clean = r[(r["trustworthy"]) & (~r["recent_jump"]) & (~r["short_ma60"])]
    print("=> {} / {} stocks are fully clean for short-term signals "
          "({:.1f}%)".format(len(clean), n, 100 * len(clean) / n))
    print("   (the rest are flagged, NOT dropped -- a >10% move can be a real "
          "no-limit-board stock, so the call is yours).")


if __name__ == "__main__":
    main()
