"""
The exit stack, as ONE parameterised implementation. ASCII only, stdlib only.

Why this file exists. The adopted exit parameters (stop 20%, take profit 20%,
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
  2. THE REST OF THE BAR, where high-vs-low ordering is unknowable, so the
     LOWEST exit level the bar actually touched is booked -- deliberately
     pessimistic. The stop carried INTO the bar is tested before the target.
  3. THE CLOSE, where arming is observed: a close at or above the arm price
     raises the stop to the lock, and that raised stop guards from the NEXT
     session.

WHY ARMING WAITES FOR THE CLOSE (2026-09-21, owner's decision). Until now the
lock armed the instant a bar's HIGH touched +6%, and that same bar could then
be stopped on the lock it had just armed. Nobody trades that. The scan runs
after the close, the owner reads the payload in the evening and places orders
for the next session, so a stop that the backtest exercised intraday on the
arming day never existed in the market. The gap was not cosmetic: on the 564
CORE+ first-day trades the old ordering reported 69.7% wins with a mean of
-0.43% (RECENT), while the same trades executed the way the owner actually
receives them scored 63.0% / +1.54% -- the reported win rate was seven points
of simulator.

Arming is read off the CLOSE rather than the high for the same reason: the
close is the number the payload and the phone both have (mobile/quotes.json
ships closes), so the backend, the ledger and the phone cannot disagree about
whether a position is armed. On the same trades, close-arming at the re-tuned
threshold also beat high-arming outright (67.1% vs 65.0% RECENT).

See archive/research/sandbox_lock_delay.py for the four engines, the parameter
surface and the slippage stress test.
"""

# The rule as adopted on 2026-08-06 (stop widened 2026-09-17, see
# scan_mode.PRELAUNCH_STOP_PCT; lock timing and arm threshold 2026-09-21),
# kept here so callers share one definition. Fractions, not percents.
DEFAULT_RULE = {
    "stop_pct": 0.20,     # disaster stop below entry
    "tp_pct": 0.20,       # take profit above entry
    "arm_pct": 0.025,     # CLOSE at or above this arms the trailing lock
    "lock_pct": 0.02,     # where the stop moves once armed (from the next bar)
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
    if len(opens) < hold_bars:
        return None, None, "na"
    plan = replay_exit(opens, highs, lows, closes, hold_bars=hold_bars,
                       stop_pct=stop_pct, tp_pct=tp_pct, arm_pct=arm_pct,
                       lock_pct=lock_pct)
    if plan["reason"] == "na":
        return None, None, "na"
    return plan["entry"], plan["ret_pct"], plan["reason"]


def replay_exit(opens, highs, lows, closes, dates=None, hold_bars=None,
                stop_pct=..., tp_pct=..., arm_pct=..., lock_pct=...):
    """Replay the exit stack bar by bar and report WHERE the trade stands.

    This is simulate_exit with the outcome kept, so the live scan can tell a
    holder what the rule says today (2026-09-14 request: "there is no column
    saying below what price to sell first"). Same event order, same numbers.

    `hold_bars=None` replays every bar given and never books a time exit --
    the live annotation has its own calendar-based time exit. With an
    integer, behaves exactly like simulate_exit (time exit on bar N-1's close).

    Returns a dict:
      entry       fill (first open) or None
      reason      '' still open | 'stop' | 'lock' | 'tp' | 'time' | 'na'
      exited      reason is a booked exit
      bar         index of the exit bar (None while open)
      date        dates[bar] when `dates` is given
      exit_price  the price the rule books for the exit
      ret_pct     exit_price / entry - 1, in percent
      armed       trailing lock armed at some bar's close
      stop        the stop carried out of the last bar (lock when armed) --
                  i.e. the level to place for the NEXT session
      arm_px, lock_px, target   the rule's levels off the fill
    """
    rule = DEFAULT_RULE
    stop_pct = rule["stop_pct"] if stop_pct is ... else stop_pct
    tp_pct = rule["tp_pct"] if tp_pct is ... else tp_pct
    arm_pct = rule["arm_pct"] if arm_pct is ... else arm_pct
    lock_pct = rule["lock_pct"] if lock_pct is ... else lock_pct
    if lock_pct is None:
        lock_pct = rule["lock_pct"]

    out = {"entry": None, "reason": "na", "exited": False, "bar": None,
           "date": None, "exit_price": None, "ret_pct": None, "armed": False,
           "stop": None, "arm_px": None, "lock_px": None, "target": None}
    n = len(opens) if hold_bars is None else min(hold_bars, len(opens))
    if n <= 0:
        return out
    try:
        entry = float(opens[0])
    except (TypeError, ValueError):
        return out
    if not entry > 0 or entry != entry:      # NaN is never > 0, but be explicit
        return out

    stop_px = entry * (1 - stop_pct) if stop_pct is not None else None
    target = entry * (1 + tp_pct) if tp_pct is not None else None
    arm_px = entry * (1 + arm_pct) if arm_pct is not None else None
    lock_px = entry * (1 + lock_pct)
    armed = False
    out.update(entry=entry, reason="", arm_px=arm_px, lock_px=lock_px,
               target=target, stop=stop_px)

    def booked(i, price, reason):
        out.update(reason=reason, exited=True, bar=i,
                   date=(dates[i] if dates is not None and i < len(dates) else None),
                   exit_price=price, ret_pct=(price / entry - 1.0) * 100,
                   armed=armed, stop=stop_px)
        return out

    for i in range(n):
        try:
            op, hi, lo = float(opens[i]), float(highs[i]), float(lows[i])
            cl = float(closes[i])
        except (TypeError, ValueError, IndexError):
            continue
        # A bar with a missing high or low disables every exit for that bar,
        # because every comparison against NaN is False. Skip it and say so
        # rather than silently pretending the position could not be closed.
        if op != op or hi != hi or lo != lo:
            continue

        # 1. the open, against the stop carried IN to this bar
        if target is not None and op >= target:
            return booked(i, op, "tp")
        if stop_px is not None and op <= stop_px:
            return booked(i, op, "lock" if armed else "stop")

        # 2. ambiguous remainder: the lowest exit level the bar touched wins.
        # Only the stop CARRIED IN counts -- a lock this bar is about to arm
        # protects from tomorrow, not from the rest of today.
        if stop_px is not None and lo <= stop_px:
            return booked(i, stop_px, "lock" if armed else "stop")
        if target is not None and hi >= target:
            return booked(i, target, "tp")

        # 3. the close: arming is what the evening payload can see, and the
        # raised stop is the order placed for the next session.
        if (arm_px is not None) and (not armed) and cl == cl and cl >= arm_px:
            armed = True
            stop_px = lock_px if stop_px is None else max(stop_px, lock_px)

    out.update(armed=armed, stop=stop_px)
    if hold_bars is not None:
        try:
            final = float(closes[hold_bars - 1])
        except (TypeError, ValueError, IndexError):
            out["reason"] = "na"
            return out
        if final != final:
            out["reason"] = "na"
            return out
        return booked(hold_bars - 1, final, "time")
    return out


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
