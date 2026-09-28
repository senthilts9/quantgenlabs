"""Transaction cost analysis: what the execution actually cost.

Two benchmarks, and the difference between them is the point.

**Implementation shortfall** measures the realised price against the *arrival*
price — the mid at the moment the decision to trade was taken. It captures
everything: spread paid, market impact, and the drift that happened while the
order was being worked. It is the honest number, because it is the one the
portfolio actually experienced.

**VWAP slippage** measures against the volume-weighted average price over the
execution window. It is the industry's common benchmark and it is *gameable*:
an algorithm that trades only when the market is moving in its favour and
stalls otherwise can beat interval VWAP while losing money against arrival.
Reported here alongside shortfall precisely so the two can disagree visibly.

Sign convention throughout: **positive basis points are a cost.** A buy filled
above its benchmark and a sell filled below it both report positive slippage.
Without that normalisation, comparing buy and sell executions means reading
the sign backwards for half the blotter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..core.types import ZERO, Side, dec, now_ms

BPS = Decimal(10000)


@dataclass
class MarketTrade:
    """One print from the public tape, used to build the interval benchmark."""

    price: Decimal
    size: Decimal
    timestamp_ms: int

    def __post_init__(self) -> None:
        self.price = dec(self.price)
        self.size = dec(self.size)


@dataclass
class TCAResult:
    """One execution, measured."""

    market: str
    side: Side
    filled_size: Decimal
    avg_price: Decimal
    arrival_price: Optional[Decimal] = None
    interval_vwap: Optional[Decimal] = None
    fees: Decimal = ZERO
    maker_size: Decimal = ZERO
    taker_size: Decimal = ZERO
    market_volume: Decimal = ZERO
    duration_ms: int = 0
    fill_count: int = 0

    # -- cost measures -----------------------------------------------------

    def _slippage_bps(self, benchmark: Optional[Decimal]) -> Optional[Decimal]:
        """Signed so positive is always a cost, on both sides."""
        if benchmark is None or benchmark <= ZERO or self.filled_size <= ZERO:
            return None
        return (self.avg_price - benchmark) / benchmark * BPS * self.side.sign

    @property
    def implementation_shortfall_bps(self) -> Optional[Decimal]:
        """Realised price vs the price when the decision was made."""
        return self._slippage_bps(self.arrival_price)

    @property
    def vwap_slippage_bps(self) -> Optional[Decimal]:
        """Realised price vs interval VWAP. Gameable - read with shortfall."""
        return self._slippage_bps(self.interval_vwap)

    @property
    def fee_bps(self) -> Optional[Decimal]:
        """Fees as basis points of notional. Negative means a net rebate."""
        notional = self.filled_size * self.avg_price
        if notional <= ZERO:
            return None
        return self.fees / notional * BPS

    @property
    def total_cost_bps(self) -> Optional[Decimal]:
        """Shortfall plus fees - the all-in number.

        Reporting shortfall without fees flatters a taker strategy, which pays
        its cost in the fee line rather than the price line.
        """
        shortfall = self.implementation_shortfall_bps
        fees = self.fee_bps
        if shortfall is None:
            return None
        return shortfall + (fees or ZERO)

    @property
    def participation_rate(self) -> Optional[Decimal]:
        """Our share of volume over the window, as a fraction.

        High participation invalidates the benchmark: past roughly 10-15% the
        interval VWAP is substantially your own prints, so beating it means
        little.
        """
        if self.market_volume <= ZERO:
            return None
        return self.filled_size / self.market_volume

    @property
    def maker_fraction(self) -> Optional[Decimal]:
        total = self.maker_size + self.taker_size
        return None if total <= ZERO else self.maker_size / total

    def to_dict(self) -> Dict[str, object]:
        def q(v, places="0.01"):
            return str(v.quantize(Decimal(places))) if v is not None else None
        return {
            "market": self.market,
            "side": self.side.value,
            "filled_size": str(self.filled_size),
            "avg_price": str(self.avg_price),
            "arrival_price": (str(self.arrival_price)
                              if self.arrival_price else None),
            "interval_vwap": (str(self.interval_vwap)
                              if self.interval_vwap else None),
            "implementation_shortfall_bps": q(self.implementation_shortfall_bps),
            "vwap_slippage_bps": q(self.vwap_slippage_bps),
            "fee_bps": q(self.fee_bps),
            "total_cost_bps": q(self.total_cost_bps),
            "participation_rate_pct": q(
                self.participation_rate * 100 if self.participation_rate else None),
            "maker_fraction_pct": q(
                self.maker_fraction * 100 if self.maker_fraction else None),
            "fills": self.fill_count,
            "duration_s": round(self.duration_ms / 1000, 1),
        }

    def __str__(self) -> str:
        def fmt(v):
            return "%+.1f bps" % v if v is not None else "n/a"
        return ("%s %s %s @ %s | shortfall %s | vs VWAP %s | fees %s | all-in %s"
                % (self.side.value, self.filled_size, self.market, self.avg_price,
                   fmt(self.implementation_shortfall_bps),
                   fmt(self.vwap_slippage_bps), fmt(self.fee_bps),
                   fmt(self.total_cost_bps)))


def interval_vwap(trades: Sequence[MarketTrade],
                  start_ms: Optional[int] = None,
                  end_ms: Optional[int] = None) -> Optional[Decimal]:
    """Volume-weighted average price over a window of public prints.

    Returns None rather than zero for an empty window: no trades means the
    benchmark is undefined, and a zero would read as a real price and produce
    a spectacular fake slippage number.
    """
    num = ZERO
    vol = ZERO
    for t in trades:
        if start_ms is not None and t.timestamp_ms < start_ms:
            continue
        if end_ms is not None and t.timestamp_ms > end_ms:
            continue
        num += t.price * t.size
        vol += t.size
    return (num / vol) if vol > ZERO else None


def analyse(market: str, side: Side, fills: Sequence,
            arrival_price: Optional[Decimal] = None,
            trades: Optional[Sequence[MarketTrade]] = None,
            start_ms: Optional[int] = None,
            end_ms: Optional[int] = None) -> TCAResult:
    """Measure a set of fills against arrival and interval VWAP.

    `fills` are `venues.base.Fill` objects. The execution window is inferred
    from the fills themselves when not supplied, so the interval benchmark
    covers exactly the period we were in the market rather than an arbitrary
    span that would include drift we had no part in.
    """
    filled = sum((f.size for f in fills), ZERO)
    cost = sum((f.size * f.price for f in fills), ZERO)
    fees = sum((f.fee for f in fills), ZERO)
    maker = sum((f.size for f in fills if f.liquidity == "maker"), ZERO)
    taker = sum((f.size for f in fills if f.liquidity == "taker"), ZERO)

    stamps = [f.timestamp_ms for f in fills if f.timestamp_ms]
    win_start = start_ms if start_ms is not None else (min(stamps) if stamps else None)
    win_end = end_ms if end_ms is not None else (max(stamps) if stamps else None)

    bench = None
    volume = ZERO
    if trades:
        bench = interval_vwap(trades, win_start, win_end)
        volume = sum((t.size for t in trades
                      if (win_start is None or t.timestamp_ms >= win_start)
                      and (win_end is None or t.timestamp_ms <= win_end)), ZERO)

    return TCAResult(
        market=market, side=side,
        filled_size=filled,
        avg_price=(cost / filled) if filled > ZERO else ZERO,
        arrival_price=dec(arrival_price) if arrival_price is not None else None,
        interval_vwap=bench,
        fees=fees, maker_size=maker, taker_size=taker,
        market_volume=volume,
        duration_ms=((win_end - win_start) if win_start and win_end else 0),
        fill_count=len(fills),
    )


def analyse_parent(parent, trades: Optional[Sequence[MarketTrade]] = None) -> TCAResult:
    """TCA for a `ParentOrder`, flattening every child's fills.

    Measuring the parent rather than each child is the only view that means
    anything: a child that crossed the spread looks terrible in isolation and
    may have been the right slice to take.
    """
    fills = [f for child in parent.children for f in child.fills]
    return analyse(
        market=parent.request.market, side=parent.request.side, fills=fills,
        arrival_price=parent.arrival_price, trades=trades,
        start_ms=parent.arrival_ms,
    )
