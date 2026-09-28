"""Instrument specifications - the per-market trading rules.

These are the constraints a venue publishes and then enforces: tick size, lot
step, minimum notional, leverage cap, market state. Validating against a local
copy turns a network round trip into a microsecond check, and more importantly
lets the same rules run in tests where no venue is reachable.

The specs below are defaults for offline work. `InstrumentRegistry.refresh`
replaces them with the venue's own metadata when a connection is available -
venues change tick sizes, and a stale local copy is itself a source of
rejections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from enum import Enum
from typing import Dict, Iterable, Optional

from .types import ZERO, Side, dec


class MarketState(str, Enum):
    """Lifecycle of a market. Only OPEN accepts arbitrary new orders."""

    OPEN = "OPEN"
    #: Accepts only orders that reduce an existing position - used during
    #: delisting and in risk-off modes.
    REDUCE_ONLY = "REDUCE_ONLY"
    #: Accepts only resting (maker) orders - used to rebuild a book after a halt.
    POST_ONLY = "POST_ONLY"
    PAUSED = "PAUSED"
    DELISTED = "DELISTED"


class SettlementKind(str, Enum):
    SPOT = "SPOT"
    PERPETUAL = "PERPETUAL"


@dataclass(frozen=True)
class Instrument:
    """One tradeable market.

    Frozen because a spec changing under a half-validated order is a race no
    amount of downstream checking recovers from; `refresh` swaps whole objects.
    """

    symbol: str
    base: str
    quote: str
    kind: SettlementKind = SettlementKind.PERPETUAL
    state: MarketState = MarketState.OPEN

    tick_size: Decimal = Decimal("0.1")
    step_size: Decimal = Decimal("0.001")
    min_size: Decimal = Decimal("0.001")
    max_size: Decimal = Decimal("1000")
    min_notional: Decimal = Decimal("10")
    max_notional: Decimal = Decimal("5000000")
    #: Cap on net position *size* in base units, distinct from `max_size`
    #: (per-order) and `max_notional` (value). Venues publish all three and
    #: they are not interchangeable - Paradex caps BTC position at 60 coins
    #: while allowing a single 100-coin order.
    max_position_size: Optional[Decimal] = None
    #: Venue's own cap on market-order slippage, in bps. None means unset.
    max_slippage_bps: Optional[Decimal] = None
    #: Per-market resting-order cap.
    max_open_orders: Optional[int] = None
    #: Hours between funding settlements. 8 is the common default; some venues
    #: settle hourly, which changes the APR of the same per-period rate by 8x.
    funding_period_hours: int = 8
    #: Venue cap on the per-period rate, so a runaway basis cannot produce an
    #: unbounded charge.
    max_funding_rate: Optional[Decimal] = None

    max_leverage: Decimal = Decimal("20")
    #: Fraction of notional required as initial margin. Derived from
    #: max_leverage unless a venue publishes a separate tiered figure.
    initial_margin_fraction: Optional[Decimal] = None
    #: Fraction of notional that must remain to avoid liquidation. Defaults to
    #: half the initial requirement - see `mm_fraction`.
    maintenance_margin_fraction: Optional[Decimal] = None

    #: Limit price must sit within this many basis points of mark. The classic
    #: fat-finger guard: it is what stops a misplaced decimal point from
    #: crossing the entire book.
    price_band_bps: Decimal = Decimal("1000")  # +/-10%

    maker_fee_bps: Decimal = Decimal("-0.5")   # negative = rebate
    taker_fee_bps: Decimal = Decimal("3.0")

    supported_order_types: frozenset = field(
        default_factory=lambda: frozenset({"LIMIT", "MARKET"})
    )

    @property
    def im_fraction(self) -> Decimal:
        """Initial margin fraction, defaulting to 1/max_leverage."""
        if self.initial_margin_fraction is not None:
            return self.initial_margin_fraction
        if self.max_leverage <= ZERO:
            return Decimal(1)
        return Decimal(1) / self.max_leverage

    @property
    def mm_fraction(self) -> Decimal:
        """Maintenance margin fraction, defaulting to half of initial.

        **Must be strictly below `im_fraction`.** Maintenance is the floor a
        position may fall to; initial is what opening it costs. If maintenance
        were the higher of the two, every position would be liquidatable the
        instant it opened - which is exactly what a fixed 0.03 default did on a
        50x market whose initial requirement is 0.02.

        Half of initial matches what the perp venues here publish
        (`mmf_factor = 0.5`). An explicit value above initial is treated as a
        misconfiguration and clamped, because silently liquidating every new
        position is a worse failure than ignoring a bad parameter.
        """
        im = self.im_fraction
        if self.maintenance_margin_fraction is None:
            return im / 2
        return min(self.maintenance_margin_fraction, im / 2)

    @property
    def tradeable(self) -> bool:
        return self.state in (
            MarketState.OPEN, MarketState.REDUCE_ONLY, MarketState.POST_ONLY
        )

    # -- tick / step arithmetic -------------------------------------------
    # All of these rely on Decimal's exact remainder. The equivalent float
    # expressions give false negatives on prices that are exactly on-tick.

    def is_on_tick(self, price: Decimal) -> bool:
        if self.tick_size <= ZERO:
            return True
        return dec(price) % self.tick_size == ZERO

    def is_on_step(self, size: Decimal) -> bool:
        if self.step_size <= ZERO:
            return True
        return dec(size) % self.step_size == ZERO

    def round_price(self, price: Decimal, side: Optional[Side] = None) -> Decimal:
        """Snap a price to the tick grid.

        With `side`, rounds *conservatively* - a buy rounds down, a sell rounds
        up - so snapping never makes an order more aggressive than intended.
        Without it, rounds to nearest.
        """
        p = dec(price)
        if self.tick_size <= ZERO:
            return p
        if side is None:
            return (p / self.tick_size).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            ) * self.tick_size
        rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP
        units = (p / self.tick_size).quantize(Decimal(1), rounding=rounding)
        return units * self.tick_size

    def round_size(self, size: Decimal) -> Decimal:
        """Snap size down to the step grid - never round size up, which would
        submit more risk than the caller asked for."""
        s = dec(size)
        if self.step_size <= ZERO:
            return s
        return (s / self.step_size).quantize(
            Decimal(1), rounding=ROUND_DOWN
        ) * self.step_size

    def band(self, mark: Decimal) -> tuple:
        """(low, high) acceptable limit prices around `mark`."""
        m = dec(mark)
        delta = m * self.price_band_bps / Decimal(10000)
        return m - delta, m + delta


def _perp(symbol: str, base: str, **kw) -> Instrument:
    return Instrument(symbol=symbol, base=base, quote="USD",
                      kind=SettlementKind.PERPETUAL, **kw)


#: Offline defaults, shaped to match what the major perp venues actually
#: publish. Replaced by live metadata wherever `refresh` succeeds.
DEFAULT_INSTRUMENTS: Dict[str, Instrument] = {
    i.symbol: i for i in [
        _perp("BTC-USD-PERP", "BTC",
              tick_size=Decimal("0.1"), step_size=Decimal("0.001"),
              min_size=Decimal("0.001"), max_size=Decimal("100"),
              min_notional=Decimal("10"), max_leverage=Decimal("50"),
              price_band_bps=Decimal("500"),
              supported_order_types=frozenset(
                  {"LIMIT", "MARKET", "STOP_LIMIT", "STOP_MARKET"})),
        _perp("ETH-USD-PERP", "ETH",
              tick_size=Decimal("0.01"), step_size=Decimal("0.01"),
              min_size=Decimal("0.01"), max_size=Decimal("2000"),
              min_notional=Decimal("10"), max_leverage=Decimal("50"),
              price_band_bps=Decimal("500"),
              supported_order_types=frozenset(
                  {"LIMIT", "MARKET", "STOP_LIMIT", "STOP_MARKET"})),
        _perp("SOL-USD-PERP", "SOL",
              tick_size=Decimal("0.001"), step_size=Decimal("0.1"),
              min_size=Decimal("0.1"), max_size=Decimal("50000"),
              min_notional=Decimal("10"), max_leverage=Decimal("20")),
        Instrument(symbol="BTC-USD", base="BTC", quote="USD",
                   kind=SettlementKind.SPOT,
                   tick_size=Decimal("0.01"), step_size=Decimal("0.00000001"),
                   min_size=Decimal("0.000016"), max_size=Decimal("500"),
                   min_notional=Decimal("1"), max_leverage=Decimal("1"),
                   price_band_bps=Decimal("1000"),
                   maker_fee_bps=Decimal("40"), taker_fee_bps=Decimal("60")),
        Instrument(symbol="ETH-USD", base="ETH", quote="USD",
                   kind=SettlementKind.SPOT,
                   tick_size=Decimal("0.01"), step_size=Decimal("0.00000001"),
                   min_size=Decimal("0.00022"), max_size=Decimal("5000"),
                   min_notional=Decimal("1"), max_leverage=Decimal("1"),
                   price_band_bps=Decimal("1000"),
                   maker_fee_bps=Decimal("40"), taker_fee_bps=Decimal("60")),
    ]
}


class InstrumentRegistry:
    """Lookup table for instrument specs, refreshable from a venue."""

    def __init__(self, instruments: Optional[Iterable[Instrument]] = None):
        src = instruments if instruments is not None else DEFAULT_INSTRUMENTS.values()
        self._by_symbol: Dict[str, Instrument] = {i.symbol: i for i in src}

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._by_symbol

    def __len__(self) -> int:
        return len(self._by_symbol)

    def get(self, symbol: str) -> Optional[Instrument]:
        return self._by_symbol.get(symbol)

    def require(self, symbol: str) -> Instrument:
        inst = self._by_symbol.get(symbol)
        if inst is None:
            raise KeyError("unknown market %r" % symbol)
        return inst

    def symbols(self) -> list:
        return sorted(self._by_symbol)

    def upsert(self, instrument: Instrument) -> None:
        self._by_symbol[instrument.symbol] = instrument

    def set_state(self, symbol: str, state: MarketState) -> None:
        """Transition a market's state, replacing the frozen spec."""
        from dataclasses import replace
        self._by_symbol[symbol] = replace(self.require(symbol), state=state)

    def refresh(self, venue) -> int:
        """Pull live specs from a venue adapter exposing `fetch_instruments()`.

        Failure is non-fatal and leaves the existing specs in place: trading on
        slightly stale tick sizes beats halting because a metadata endpoint
        blipped. Returns the number of instruments updated.
        """
        try:
            fetched = venue.fetch_instruments()
        except Exception:
            return 0
        n = 0
        for inst in fetched:
            self.upsert(inst)
            n += 1
        return n
