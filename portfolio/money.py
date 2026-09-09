"""
Exact money for the trade ledger. Decimal end to end, ASCII only.

Report section 9.1 is explicit: "do not let floating point error decide whether
there was a profit". A position that is 30 cents from break-even must not flip
sign because 0.1 + 0.2 != 0.3. So every amount in portfolio/ is a Decimal, and
it is persisted as TEXT (SQLite has no decimal type; REAL would silently round
trip through float and lose the exactness we just paid for).

Three things live here:

  FeeSchedule   versioned broker fee / securities transaction tax. Report 6.3:
                "the fee rate, minimum charge and discount follow the user's
                broker; 0.1425% must not be treated as everyone's actual rate."
                So it is data, not a constant, and every stored execution keeps
                the schedule version that priced it.

  tick sizes    TWSE/TPEx quote increments. Report section 8: "showing two
                decimals does not mean the price is orderable." A plan price of
                106.37 is not a thing you can send to a broker; 106.5 is.

  rounding      round_to_tick takes an explicit direction, because rounding a
                stop DOWN and a target UP is the conservative choice and doing
                it by accident in the other direction quietly loosens risk.
"""
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING

# Money is stored to the cent. Taiwan quotes equities in whole cents, and
# fee/tax amounts are sub-dollar before the broker's own rounding step.
CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(value) -> Decimal:
    """Coerce to Decimal without ever going through binary float.

    Decimal(0.1) is 0.1000000000000000055511151231257827; Decimal("0.1") is
    0.1. Floats therefore get str()'d first -- repr of a float is the shortest
    string that round-trips, which is the number the caller meant.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return ZERO
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def money(value) -> Decimal:
    """Decimal rounded to the cent, half-up (the ordinary commercial rule)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def as_text(value) -> str:
    """Canonical DB representation. Never None -- absent amounts are 0."""
    return str(money(value))


# --- Fees and tax -----------------------------------------------------------
# Statutory / conventional Taiwan equity rates, kept as a versioned record so a
# historical execution can always be re-priced with the schedule that was in
# force when it happened, not with today's.
#
#   broker fee   0.1425% of consideration, each side, commonly discounted.
#                Most brokers apply a NT$20 minimum and truncate to the dollar.
#   sell tax     0.3% of sale consideration on ordinary equities. Day trades and
#                some instruments differ -- hence a per-schedule field rather
#                than a module constant.
#
# TWSE fee/tax guide: https://www.twse.com.tw/zh/about/company/guide.html

class FeeSchedule(object):
    """One versioned fee/tax regime.

    `round_to_dollar` and `min_fee` model what a real broker charges. They are
    deliberately switchable because the report's worked example (section 6.3)
    states its numbers "without broker dollar-rounding" -- reproducing that
    example is one of our acceptance tests, and it must not require pretending
    the realistic path does not round.
    """

    def __init__(self, version, broker_fee_rate="0.001425", discount="1.0",
                 min_fee="20", sell_tax_rate="0.003", round_to_dollar=True,
                 label=""):
        self.version = version
        self.broker_fee_rate = D(broker_fee_rate)
        self.discount = D(discount)
        self.min_fee = D(min_fee)
        self.sell_tax_rate = D(sell_tax_rate)
        self.round_to_dollar = bool(round_to_dollar)
        self.label = label

    @classmethod
    def default(cls):
        """Realistic retail defaults: 0.1425%, no discount, NT$20 floor, the
        fee truncated to the dollar the way a broker statement shows it."""
        return cls("tw-equity-v1", label="TW equity, undiscounted retail")

    @classmethod
    def exact(cls):
        """Same rates with no minimum and no dollar rounding. This is the
        regime the report's section 6.3 example is computed in (buy fee 145.35,
        sell fee 159.60, tax 336.00, net 9,359.05); keeping it as a named
        schedule is how that example stays checkable."""
        return cls("tw-equity-exact", min_fee="0", round_to_dollar=False,
                   label="TW equity, unrounded (documentation / reconciliation)")

    def _fee(self, consideration: Decimal) -> Decimal:
        fee = consideration * self.broker_fee_rate * self.discount
        if self.round_to_dollar:
            # Brokers truncate the fee to the dollar, then apply the minimum.
            fee = fee.quantize(Decimal("1"), rounding=ROUND_DOWN)
            if fee < self.min_fee:
                fee = self.min_fee
        elif self.min_fee > 0 and fee < self.min_fee:
            fee = self.min_fee
        return money(fee)

    def buy_fee(self, price, shares) -> Decimal:
        return self._fee(D(price) * D(shares))

    def sell_fee(self, price, shares) -> Decimal:
        return self._fee(D(price) * D(shares))

    def sell_tax(self, price, shares) -> Decimal:
        tax = D(price) * D(shares) * self.sell_tax_rate
        if self.round_to_dollar:
            tax = tax.quantize(Decimal("1"), rounding=ROUND_DOWN)
        return money(tax)

    def buy_cost(self, price, shares):
        """(consideration, fee, total cash out)."""
        consideration = money(D(price) * D(shares))
        fee = self.buy_fee(price, shares)
        return consideration, fee, money(consideration + fee)

    def sell_proceeds(self, price, shares):
        """(consideration, fee, tax, net cash in)."""
        consideration = money(D(price) * D(shares))
        fee = self.sell_fee(price, shares)
        tax = self.sell_tax(price, shares)
        return consideration, fee, tax, money(consideration - fee - tax)

    def exit_cost(self, price, shares) -> Decimal:
        """Fee + tax that liquidating `shares` at `price` would incur. Used for
        the "net if liquidated today" figure, which the report insists must not
        share a label with the book P&L."""
        return money(self.sell_fee(price, shares) + self.sell_tax(price, shares))

    def to_row(self):
        return {
            "version": self.version,
            "broker_fee_rate": str(self.broker_fee_rate),
            "discount": str(self.discount),
            "min_fee": str(self.min_fee),
            "sell_tax_rate": str(self.sell_tax_rate),
            "round_to_dollar": int(self.round_to_dollar),
            "label": self.label,
        }


SCHEDULES = {s.version: s for s in (FeeSchedule.default(), FeeSchedule.exact())}


def get_schedule(version=None) -> FeeSchedule:
    if version is None:
        return FeeSchedule.default()
    try:
        return SCHEDULES[version]
    except KeyError:
        raise ValueError("unknown fee schedule: {}".format(version))


# --- Tick sizes -------------------------------------------------------------
# TWSE / TPEx ordinary equity quote increments, by price band. Both exchanges
# publish the same ladder for ordinary shares.
#   TWSE  https://www.twse.com.tw/zh/products/system/trading.html
#   TPEx  https://www.tpex.org.tw/zh-tw/mainboard/trading/rules/system.html
# (band_upper_exclusive, tick); None upper bound = open-ended top band.
EQUITY_TICKS = (
    (Decimal("10"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.1")),
    (Decimal("500"), Decimal("0.5")),
    (Decimal("1000"), Decimal("1")),
    (None, Decimal("5")),
)

# Ordinary equities trade in 1,000-share lots, but odd-lot and fractional
# trading exist and other instruments differ -- so this is a per-instrument
# default, never a hard-coded assumption (report section 6.3).
DEFAULT_LOT_SIZE = 1000


def tick_size(price) -> Decimal:
    p = D(price)
    for upper, tick in EQUITY_TICKS:
        if upper is None or p < upper:
            return tick
    return EQUITY_TICKS[-1][1]


def round_to_tick(price, direction="nearest") -> Decimal:
    """Snap a price onto the exchange's quote ladder.

    direction: "down" for stops (never claim a tighter stop than is orderable),
               "up" for targets, "nearest" for display.

    Note the report's caveat: a user's AVERAGE cost across several fills is a
    real number that legitimately falls between ticks. Do not push averages
    through this -- it is for prices you intend to send as an order.
    """
    p = D(price)
    if p <= 0:
        return ZERO
    tick = tick_size(p)
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING}.get(
        direction, ROUND_HALF_UP)
    steps = (p / tick).quantize(Decimal("1"), rounding=rounding)
    return money(steps * tick)


def shares_from_lots(lots, lot_size=DEFAULT_LOT_SIZE) -> int:
    """Lots -> shares. The UI may take either; the ledger stores shares only,
    so there is exactly one quantity unit inside the system."""
    return int(D(lots) * D(lot_size))


def pct(numerator, denominator, places="0.01"):
    """Percentage as Decimal, or None when the denominator is 0/absent.

    Returning None rather than 0 matters: "no cost basis yet" and "flat" are
    different answers, and the report repeatedly insists unknown must not
    render as a confident number.
    """
    den = D(denominator)
    if den == 0:
        return None
    return (D(numerator) / den * 100).quantize(
        Decimal(places), rounding=ROUND_HALF_UP)
