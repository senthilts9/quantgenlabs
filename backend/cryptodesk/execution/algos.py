"""Algorithmic execution: a parent order worked as child orders over time.

A desk does not send 5 BTC as one order. It sends a *parent* — the intent —
and the algorithm slices it into *children* spread across a window, so the
order is absorbed by natural liquidity rather than paying to cross the whole
book at once.

Two schedules are implemented:

**TWAP** slices evenly in time. It assumes nothing about volume, which makes
it the honest default when you have no reliable profile — and predictable,
which is its weakness: an observer can infer the next slice.

**VWAP** weights slices by expected volume, trading more when the market
trades more. It tracks the volume-weighted benchmark closely by construction,
but it inherits the profile's errors: a bad forecast concentrates the order
into the wrong hour.

Three details decide whether this works or quietly misbehaves:

*Slices must sum to the parent exactly.* Dividing 1.0 into three and rounding
each to the step grid gives 0.333 × 3 = 0.999 and silently orphans 0.001. The
residual is carried into the final slice, so the parent always completes.

*Slicing too finely breaks the minimum notional.* Twenty slices of a small
parent produces children the venue refuses individually, and the algorithm
looks broken when it is the schedule that is wrong. The slice count is reduced
until every child clears the minimum.

*Parent state is derived from children, never tracked alongside them.* Two
counters for one quantity drift apart, and the moment they do there is no way
to tell which is right.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Sequence

from ..core.instruments import Instrument
from ..core.types import (
    ZERO, OrderRequest, OrderStatus, OrderType, Side, dec, now_ms,
)

#: A day's volume is not flat. This is the classic U-shape - heavy at the open
#: and close, quiet through the middle - normalised over 12 buckets. Crypto is
#: 24/7 so the effect is milder than in equities, but it is not absent.
DEFAULT_VOLUME_PROFILE: Sequence[float] = (
    0.14, 0.10, 0.08, 0.06, 0.06, 0.06,
    0.06, 0.06, 0.07, 0.08, 0.10, 0.13,
)


class AlgoError(Exception):
    """The schedule cannot be built as requested."""


@dataclass
class Slice:
    """One child order's worth of the parent, and when to send it."""

    index: int
    scheduled_ms: int
    size: Decimal
    #: Fraction of the parent this slice represents, for reporting.
    weight: Decimal = ZERO

    def __post_init__(self) -> None:
        self.size = dec(self.size)
        self.weight = dec(self.weight)


class ExecutionAlgo:
    """Turns a parent size into a time-ordered schedule of child sizes."""

    name = "abstract"

    def weights(self, n: int) -> List[Decimal]:
        raise NotImplementedError

    def max_slices(self, total: Decimal, instrument: Instrument,
                   reference_price: Decimal) -> int:
        """Largest slice count where every child still clears the venue floors.

        Both floors bite: a child must be at least `min_size` in base units and
        at least `min_notional` in value. Whichever is stricter sets the limit.
        """
        if total <= ZERO or reference_price <= ZERO:
            return 1
        by_size = int(total / instrument.min_size) if instrument.min_size > ZERO else 1
        notional = total * reference_price
        by_notional = (int(notional / instrument.min_notional)
                       if instrument.min_notional > ZERO else 1)
        return max(1, min(by_size, by_notional))

    def schedule(self, total: Decimal, instrument: Instrument,
                 reference_price: Decimal, slices: int,
                 duration_ms: int, start_ms: Optional[int] = None) -> List[Slice]:
        """Build the schedule, clamping slice count to what the venue accepts."""
        total = dec(total)
        if total <= ZERO:
            raise AlgoError("parent size must be positive, got %s" % total)
        if slices < 1:
            raise AlgoError("need at least one slice, got %d" % slices)
        if duration_ms < 0:
            raise AlgoError("duration must not be negative")

        allowed = self.max_slices(total, instrument, reference_price)
        n = min(slices, allowed)
        start = start_ms if start_ms is not None else now_ms()
        weights = self.weights(n)

        out: List[Slice] = []
        allocated = ZERO
        # Evenly spaced in time regardless of weighting: VWAP varies *size* per
        # bucket, not when the buckets occur.
        step_ms = duration_ms // n if n else 0
        for i in range(n):
            if i == n - 1:
                # Residual goes here, so the schedule sums to the parent exactly
                # rather than orphaning whatever rounding left behind.
                size = total - allocated
            else:
                size = instrument.round_size(total * weights[i])
                if size <= ZERO:
                    size = instrument.min_size
            allocated += size
            out.append(Slice(index=i, scheduled_ms=start + i * step_ms,
                             size=size, weight=weights[i]))

        final = out[-1]
        if final.size <= ZERO:
            # Rounding consumed the parent before the last slice. Drop it and
            # give its share back, rather than emitting a zero-size child.
            out.pop()
            if not out:
                raise AlgoError("schedule collapsed to nothing")
            out[-1].size += final.size
        return out


class TWAP(ExecutionAlgo):
    """Equal size per slice, evenly spaced in time."""

    name = "TWAP"

    def weights(self, n: int) -> List[Decimal]:
        return [Decimal(1) / Decimal(n)] * n


class VWAP(ExecutionAlgo):
    """Slice sizes follow an expected volume profile.

    The profile is resampled to the requested slice count, so a 5-slice order
    and a 50-slice order both follow the same shape.
    """

    name = "VWAP"

    def __init__(self, profile: Optional[Sequence[float]] = None):
        self.profile = tuple(profile or DEFAULT_VOLUME_PROFILE)
        if not self.profile or any(p < 0 for p in self.profile):
            raise AlgoError("volume profile must be non-empty and non-negative")

    def weights(self, n: int) -> List[Decimal]:
        buckets = len(self.profile)
        raw: List[Decimal] = []
        for i in range(n):
            # Map slice i onto the profile by proportional position.
            lo = i * buckets / n
            hi = (i + 1) * buckets / n
            acc = 0.0
            for b in range(int(math.floor(lo)), min(buckets, int(math.ceil(hi)))):
                overlap = min(hi, b + 1) - max(lo, b)
                if overlap > 0:
                    acc += self.profile[b] * overlap
            raw.append(dec(repr(acc)))
        total = sum(raw, ZERO)
        if total <= ZERO:
            return [Decimal(1) / Decimal(n)] * n
        return [w / total for w in raw]


@dataclass
class ParentOrder:
    """A working parent and the children it has produced.

    Every quantity below is *derived* from the children. There is deliberately
    no `self.filled` counter: a second source of truth for the same number is
    a bug waiting for the two to disagree.
    """

    request: OrderRequest
    algo_name: str
    slices: List[Slice]
    #: Mid at the moment the parent was accepted. The honest TCA benchmark -
    #: everything after this point is the cost of the decision to trade.
    arrival_price: Optional[Decimal] = None
    arrival_ms: int = field(default_factory=now_ms)
    children: List = field(default_factory=list)      # List[TrackedOrder]
    cancelled: bool = False

    @property
    def total_size(self) -> Decimal:
        return self.request.size

    @property
    def scheduled_size(self) -> Decimal:
        return sum((s.size for s in self.slices), ZERO)

    @property
    def filled_size(self) -> Decimal:
        return sum((c.filled_size for c in self.children), ZERO)

    @property
    def cost(self) -> Decimal:
        return sum((c.cost for c in self.children), ZERO)

    @property
    def avg_price(self) -> Optional[Decimal]:
        f = self.filled_size
        return (self.cost / f) if f > ZERO else None

    @property
    def remaining(self) -> Decimal:
        return max(ZERO, self.total_size - self.filled_size)

    @property
    def sent_slices(self) -> int:
        return len(self.children)

    @property
    def status(self) -> OrderStatus:
        if self.cancelled:
            return OrderStatus.CANCELLED
        if self.filled_size <= ZERO:
            return (OrderStatus.NEW if self.children else OrderStatus.PENDING_NEW)
        if self.remaining <= ZERO:
            return OrderStatus.FILLED
        return OrderStatus.PARTIALLY_FILLED

    @property
    def is_complete(self) -> bool:
        return self.remaining <= ZERO or self.cancelled

    def child_request(self, slice_: Slice,
                      price: Optional[Decimal] = None) -> OrderRequest:
        """Build the child order for a slice.

        The child carries its own `client_order_id` derived from the parent's,
        so a blotter row can be traced back to the parent that produced it -
        which is the whole point of having a parent.
        """
        from dataclasses import replace
        return replace(
            self.request,
            size=slice_.size,
            price=price if price is not None else self.request.price,
            client_order_id="%s-c%02d" % (self.request.client_order_id[:24],
                                          slice_.index),
        )

    def summary(self) -> dict:
        return {
            "client_order_id": self.request.client_order_id,
            "market": self.request.market,
            "side": self.request.side.value,
            "algo": self.algo_name,
            "total_size": str(self.total_size),
            "filled_size": str(self.filled_size),
            "remaining": str(self.remaining),
            "avg_price": str(self.avg_price) if self.avg_price else None,
            "arrival_price": str(self.arrival_price) if self.arrival_price else None,
            "slices": len(self.slices),
            "children_sent": self.sent_slices,
            "status": self.status.value,
        }


def participation_cap(market_volume: Decimal, max_rate: Decimal) -> Decimal:
    """Largest size that keeps us at or under `max_rate` of market volume.

    The constraint that stops an algorithm becoming the market: if you are 40%
    of the prints, the benchmark you are measured against is mostly your own
    trading, and the price you get is the price you caused.
    """
    volume, rate = dec(market_volume), dec(max_rate)
    if volume <= ZERO or rate <= ZERO:
        return ZERO
    return volume * rate


class AlgoRunner:
    """Works a parent order through its schedule, via the OMS.

    `step()` sends whichever slices are due by `now`, rather than sleeping.
    That keeps the whole thing testable - a test advances a clock instead of
    waiting five minutes - and it is also how a real scheduler would drive it.
    """

    def __init__(self, oms, algo: ExecutionAlgo,
                 max_participation: Optional[Decimal] = None):
        self.oms = oms
        self.algo = algo
        #: Cap as a fraction of observed market volume. None disables it.
        self.max_participation = (dec(max_participation)
                                  if max_participation is not None else None)

    def create(self, request: OrderRequest, instrument: Instrument,
               reference_price: Decimal, slices: int, duration_ms: int,
               arrival_price: Optional[Decimal] = None,
               start_ms: Optional[int] = None) -> ParentOrder:
        schedule = self.algo.schedule(
            request.size, instrument, reference_price, slices, duration_ms,
            start_ms=start_ms)
        return ParentOrder(
            request=request, algo_name=self.algo.name, slices=schedule,
            arrival_price=(dec(arrival_price) if arrival_price is not None
                           else dec(reference_price)),
            arrival_ms=start_ms if start_ms is not None else now_ms(),
        )

    def step(self, parent: ParentOrder, now: Optional[int] = None,
             market_volume: Optional[Decimal] = None, **submit_kw) -> List:
        """Send every slice now due. Returns the children created this step."""
        if parent.is_complete:
            return []
        clock = now if now is not None else now_ms()
        sent = []
        for sl in parent.slices[parent.sent_slices:]:
            if sl.scheduled_ms > clock:
                break
            size = sl.size
            if self.max_participation is not None and market_volume is not None:
                cap = participation_cap(market_volume, self.max_participation)
                if cap > ZERO and size > cap:
                    # Trim rather than skip: a skipped slice leaves the parent
                    # permanently short, whereas a trimmed one defers the
                    # remainder into the residual carried by the final slice.
                    size = cap
            if size <= ZERO:
                break
            child_req = parent.child_request(sl)
            child_req.size = size
            tracked = self.oms.submit(child_req, **submit_kw)
            parent.children.append(tracked)
            sent.append(tracked)
        return sent

    def run(self, parent: ParentOrder, replenish: Decimal = Decimal("1"),
            **submit_kw) -> ParentOrder:
        """Drive the whole schedule immediately, ignoring the wall clock.

        For backtests and demos; a live deployment calls `step()` from a
        scheduler so the timing is real.

        `replenish` exists because collapsing the time axis also collapses the
        thing the algorithm is harvesting. On a real venue, liquidity refreshes
        between slices - that refresh is *why* working an order beats crossing
        in one print. Running six slices against a book that never refills
        walks exactly the same depth as one order, and TWAP measures identical
        to a single sweep: an artefact of the simulation, not a property of the
        algorithm. Simulated venues are asked to recover between children; real
        ones have no `replenish` and are left alone.
        """
        recover = getattr(self.oms.venue, "replenish", None)
        for sl in parent.slices:
            if parent.is_complete:
                break
            self.step(parent, now=sl.scheduled_ms, **submit_kw)
            if recover is not None and replenish > 0:
                recover(parent.request.market, replenish)
        return parent
