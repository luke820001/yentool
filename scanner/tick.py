"""
Taiwan equity price ladder: snap a computed level onto a price that can
actually be sent as an order. ASCII only, stdlib only.

Why this exists (owner, 2026-09-21): "the suggested prices have decimals that
cannot exist -- they should follow the exchange's tick table". They were
right, and it was not cosmetic. Every level this project prints is a level the
owner is meant to place with a broker:

    191.50 x 0.80 = 153.20   <- 100-500 trades in 0.50 steps, so 153.20 is not
                                an orderable price at all
    191.50 x 1.20 = 229.80   <- same
    191.50 x 0.90 = 172.35   <- same

A number that cannot be entered is not a plan, and rounding it in the user's
head at 09:00 is exactly where a -20% stop silently becomes -19.7% or -20.3%.

The TWSE / TPEX ladder for ordinary shares:

      price        tick
      < 10         0.01
      10 -  50     0.05
      50 - 100     0.10
     100 - 500     0.50
     500 - 1000    1.00
      >= 1000      5.00

ETFs AND ETNs QUOTE ON A DIFFERENT LADDER (found 2026-09-21 by audit, the day
this project started storing and advising on ETFs):

      price        tick
      < 50         0.01
      >= 50        0.05

The store proves it -- on 2026-09-18/21 in the 100-500 band, 412 of 412
ordinary shares closed on a 0.50 multiple, while 32 ETFs closed on 0.05 and
only 4 of those happened to land on 0.50 as well (0050 at 106.75, 00663L at
109.75, 00640L at 107.25, 00735 at 103.25 -- none of them placeable on the
equity ladder). Snapping an ETF level with the equity table moved a trailing
lock the WRONG WAY, 108.85 -> 108.50, i.e. a stop wider than the rule says.
So every helper here takes an optional `stock_id`; pass it whenever you have
it. Taiwan ETF and ETN codes are the 00-prefixed ones (0050, 00878, 00663L,
00400A), four to seven characters.

DIRECTION MATTERS, and the rule is always "never claim a price better than the
one you can actually place":

  * a SELL-STOP (disaster stop, trailing lock) rounds DOWN -- rounding up
    would advertise protection tighter than the ladder allows;
  * a SELL-TARGET (take profit) rounds UP -- rounding down would claim a fill
    the market never had to give;
  * a BUY-LIMIT (the staged add) rounds DOWN -- the cheaper side is the one
    that is actually orderable at that level.

Averages are NOT prices to send: a weighted average cost legitimately falls
between ticks, so it must never be pushed through here (same caveat as
portfolio/money.round_to_tick, which this module mirrors for the scanner
without dragging the Decimal money stack into it).
"""

# (upper bound exclusive, tick). The last row applies from 1000 upward.
TICKS = ((10.0, 0.01), (50.0, 0.05), (100.0, 0.1), (500.0, 0.5),
         (1000.0, 1.0), (None, 5.0))

# Beneficiary certificates -- ETFs and ETNs -- use two steps only.
ETF_TICKS = ((50.0, 0.01), (None, 0.05))


def is_etf(stock_id):
    """True for a Taiwan ETF / ETN code (00-prefixed, 4-7 characters).

    Deliberately narrow: an unknown or missing id falls back to the ordinary
    share ladder, which is what every non-00 instrument uses.
    """
    if stock_id is None:
        return False
    sid = str(stock_id).strip().upper()
    return 4 <= len(sid) <= 7 and sid.startswith("00") and sid[:4].isdigit()


def _ladder(stock_id):
    return ETF_TICKS if is_etf(stock_id) else TICKS

# Rounding a level onto the ladder moves it by at most one tick, which is
# 0.5% at the widest point of the table (a 0.05 tick just under 50). The
# adopted rule was stress-tested against a 0.5% worse fill on every stop and
# lock exit and held its win rate (66.8% vs 67.1%), so tick alignment is
# inside the tested tolerance rather than a new, unmeasured risk.


def tick_size(price, stock_id=None):
    """The quote step at `price`, or None when the price is unusable.

    Pass `stock_id` so an ETF gets the ETF ladder; without it the ordinary
    share table is used.
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p != p or p <= 0:
        return None
    ladder = _ladder(stock_id)
    for upper, tick in ladder:
        if upper is None or p < upper:
            return tick
    return ladder[-1][1]


def _snap(price, mode, stock_id=None):
    tick = tick_size(price, stock_id)
    if tick is None:
        return None
    p = float(price)
    steps = p / tick
    # Floating point: 153.2 / 0.5 is 306.40000000000003, and 229.5 / 0.5 is
    # 458.99999999999994. Nudge by a millionth of a tick before flooring or
    # ceiling so a price that IS on the ladder is never moved off it.
    eps = 1e-6
    if mode == "down":
        n = _floor(steps + eps)
    elif mode == "up":
        n = _ceil(steps - eps)
    else:
        n = _floor(steps + 0.5)
    value = n * tick
    # A tick boundary can push a price into the next band (e.g. 499.8 rounding
    # up to 500.0 where the step becomes 1.00). The value is still a valid
    # order at the boundary itself, so only re-snap when it is not.
    band = tick_size(value, stock_id)
    if band is not None and band != tick:
        steps2 = value / band
        if abs(steps2 - round(steps2)) > 1e-6:
            value = (_ceil(steps2 - eps) if mode == "up" else _floor(steps2 + eps)) * band
    return round(value, 2)


def _floor(x):
    import math
    return int(math.floor(x))


def _ceil(x):
    import math
    return int(math.ceil(x))


def round_to_tick(price, direction="nearest", stock_id=None):
    """Snap `price` onto the ladder. direction: 'down' | 'up' | 'nearest'.
    Returns None for an unusable price so a caller can keep a null rather
    than invent a number."""
    return _snap(price, direction, stock_id)


def stop_price(level, stock_id=None):
    """A sell-stop the owner can place (rounds down)."""
    return round_to_tick(level, "down", stock_id)


def target_price(level, stock_id=None):
    """A sell-target the owner can place (rounds up)."""
    return round_to_tick(level, "up", stock_id)


def buy_price(level, stock_id=None):
    """A buy-limit the owner can place (rounds down)."""
    return round_to_tick(level, "down", stock_id)


def is_on_tick(price, stock_id=None):
    """True when `price` is exactly on the ladder (what the column checker
    asks of every level it publishes)."""
    tick = tick_size(price, stock_id)
    if tick is None:
        return False
    steps = float(price) / tick
    return abs(steps - round(steps)) < 1e-6
