"""
The exit stack, as ONE parameterised implementation. ASCII only, stdlib only.

Why this file exists. The adopted exit parameters (stop 15%, take profit 20%,
arm 6%, lock 2%) were chosen by archive/research/eval_winrate_round2.sim_trail,
while live forward performance was measured by
scanner.signal_ledger._simulate_rule. Two implementations of one rule, written
separately -- and BOTH carried the same two same-bar ordering defects (F09):
arming only took effect from the next bar, and the low was tested before the
open, so a bar gapping through the target booked as a stop.

They agreed with each other to 0.005pp, which was read as cross-validation. It
was not. It meant they were wrong the same way. The parameters were then
selected on that shared mistake.

So the fix is not "correct both files". It is to have one function that every
caller shares, with the rule's numbers as ARGUMENTS rather than constants, so a
parameter search and the live ledger cannot drift apart again.

EVENT ORDER WITHIN ONE BAR. A daily bar is four numbers; it cannot tell you the
true intraday sequence. The order below is a stated assumption:

  1. THE OPEN, the one price whose timing is known -- it is the first trade of
     the day. A bar that opens through a level fills AT THE OPEN, not at the
     level: an open above the target is a take profit at the open, an open at or
     below the live stop is a stop at the open.
  2. ARMING, on the same bar. A bar whose high reaches the arm price can be
     stopped out on the lock it just armed.
  3. THE REST OF THE BAR, where high-vs-low ordering is unknowable, so the
     LOWEST exit level the bar actually touched is booked -- deliberately
     pessimistic. The stop carried into the bar is tested before the lock the
     bar itself arms, and both before the target.
"""

# The rule as adopted on 2026-08-06, kept here so callers share one definition.
# Fractions, not percents.
DEFAULT_RULE = {
    "stop_pct": 0.15,     # disaster stop below entry
    "tp_pct": 0.20,       # take profit above entry
    "arm_pct": 0.06,      # gain that arms the trailing lock
    "lock_pct": 0.02,     # where the stop moves once armed
    "hold_bars": 10,      # time exit
}


def simulate_exit(opens, highs, lows, closes, hold_bars=..., stop_pct=...,
                  tp_pct=..., arm_pct=..., lock_pct=...):
    """Replay one trade through the exit stack.

    Entry is the FIRST bar's open (the live rule is a market order at the next
    open; no limit is posted). Returns (entry, return_pct, reason) where reason
    is one of 'tp', 'lock', 'stop', 'time', or (None, None, 'na') when the
    window is too short or the open is unusable.

    Set `tp_pct` or `arm_pct` to None to disable that leg -- which is how a
    parameter search asks "what would this be worth without a take profit at
    all", rather than approximating it with a very large number.
    """
    # `...` means "use the adopted value"; None means "disable this leg" -- so a
    # search can ask what the rule is worth with no take profit at all, instead
    # of approximating that with a very large number.
    rule = DEFAULT_RULE
    hold_bars = rule["hold_bars"] if hold_bars is ... else hold_bars
    stop_pct = rule["stop_pct"] if stop_pct is ... else stop_pct
    tp_pct = rule["tp_pct"] if tp_pct is ... else tp_pct
    arm_pct = rule["arm_pct"] if arm_pct is ... else arm_pct
    lock_pct = rule["lock_pct"] if lock_pct is ... else lock_pct
    if lock_pct is None:
        lock_pct = rule["lock_pct"]

    if len(opens) < hold_bars:
        return None, None, "na"
    entry = opens[0]
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return None, None, "na"
    if not entry > 0 or entry != entry:      # NaN is never > 0, but be explicit
        return None, None, "na"

    stop_px = entry * (1 - stop_pct) if stop_pct is not None else None
    target = entry * (1 + tp_pct) if tp_pct is not None else None
    arm_px = entry * (1 + arm_pct) if arm_pct is not None else None
    lock_px = entry * (1 + lock_pct)
    armed = False

    for i in range(hold_bars):
        try:
            op, hi, lo = float(opens[i]), float(highs[i]), float(lows[i])
        except (TypeError, ValueError):
            continue
        # A bar with a missing high or low disables every exit for that bar,
        # because every comparison against NaN is False. Skip it and say so
        # rather than silently pretending the position could not be closed.
        if op != op or hi != hi or lo != lo:
            continue

        # 1. the open, against the stop carried IN to this bar
        if target is not None and op >= target:
            return entry, (op / entry - 1.0) * 100, "tp"
        if stop_px is not None and op <= stop_px:
            return entry, (op / entry - 1.0) * 100, "lock" if armed else "stop"

        # 2/3. ambiguous remainder: lowest exit level touched wins
        if stop_px is not None and lo <= stop_px:
            return (entry, (stop_px / entry - 1.0) * 100,
                    "lock" if armed else "stop")
        arm_here = (arm_px is not None) and (not armed) and hi >= arm_px
        if arm_here and lo <= lock_px:
            return entry, (lock_px / entry - 1.0) * 100, "lock"
        if target is not None and hi >= target:
            return entry, (target / entry - 1.0) * 100, "tp"

        if arm_here:
            armed = True
            stop_px = lock_px if stop_px is None else max(stop_px, lock_px)

    try:
        final = float(closes[hold_bars - 1])
    except (TypeError, ValueError):
        return None, None, "na"
    return entry, (final / entry - 1.0) * 100, "time"


def summarise(returns):
    """Win rate, payoff, expectancy and profit factor for a list of returns.

    Reported together on purpose: the 2026-09-09 analysis found the live exit
    stack raising the win rate by 24 points while removing essentially all the
    profit, which is invisible if you look at win rate alone.
    """
    values = [r for r in returns if r is not None]
    if not values:
        return None
    wins = [r for r in values if r > 0]
    losses = [r for r in values if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    payoff = abs(avg_win / avg_loss) if avg_loss else float("inf")
    return {
        "n": len(values),
        "win_rate": len(wins) / len(values) * 100,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "expectancy": sum(values) / len(values),
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        # The win rate this payoff needs just to break even. The single most
        # useful number for deciding whether a "higher win rate" change helped.
        "breakeven_win_rate": 100.0 / (1.0 + payoff) if payoff else 100.0,
    }
