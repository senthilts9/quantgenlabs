"""Domain types.

Everything monetary is `decimal.Decimal`. Binary floats cannot represent 0.1,
so `0.1 + 0.2 != 0.3`, and a tick-size check written as `price % tick == 0`
fails on values that are exactly on-tick. That is not a rounding nit: it is an
order rejected by your own validator for being correct. Prices, sizes,
balances and notionals are Decimal from parse to wire, and the only float in
the package is latency measurement.

`dec()` is the single entry point for building them, because
`Decimal(0.1)` (from a float) silently inherits the binary error that
`Decimal("0.1")` (from a string) avoids.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

ZERO = Decimal("0")


def dec(value: Any) -> Decimal:
    """Coerce to Decimal without inheriting binary float error.

    Floats are routed through `repr()` so `dec(0.1)` yields exactly
    Decimal("0.1") rather than Decimal("0.1000000000000000055511151231257827").
    Accepting floats at all is a concession to callers parsing third-party
    JSON; internal code should pass strings.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, (int, str)):
        return Decimal(value)
    raise TypeError("cannot convert %r (%s) to Decimal" % (value, type(value).__name__))


def try_dec(value: Any) -> Optional[Decimal]:
    """Decimal or None - for parsing fields that may be absent or junk."""
    if value is None:
        return None
    try:
        d = dec(value)
    except (TypeError, InvalidOperation, ValueError):
        return None
    return None if not d.is_finite() else d


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> Decimal:
        """+1 for BUY, -1 for SELL - lets position maths drop the branch."""
        return Decimal(1) if self is Side.BUY else Decimal(-1)


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"

    @property
    def needs_price(self) -> bool:
        return self in (
            OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT
        )

    @property
    def needs_trigger(self) -> bool:
        return self in (
            OrderType.STOP_LIMIT, OrderType.STOP_MARKET,
            OrderType.TAKE_PROFIT_LIMIT, OrderType.TAKE_PROFIT_MARKET,
        )


class TimeInForce(str, Enum):
    GTC = "GTC"  # rest until cancelled
    IOC = "IOC"  # fill what you can now, cancel the rest
    FOK = "FOK"  # fill entirely now or cancel entirely
    GTD = "GTD"  # rest until `expires_at`
    POST_ONLY = "POST_ONLY"  # never take; reject if it would cross


class SelfTradeMode(str, Enum):
    """What to do when an order would match the account's own resting order."""

    ALLOW = "ALLOW"
    CANCEL_TAKER = "CANCEL_TAKER"
    CANCEL_MAKER = "CANCEL_MAKER"
    CANCEL_BOTH = "CANCEL_BOTH"


class OrderStatus(str, Enum):
    """Lifecycle states. Terminal states are in `TERMINAL_STATUSES`."""

    PENDING_NEW = "PENDING_NEW"      # accepted locally, not yet acked by venue
    NEW = "NEW"                      # resting on the book
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING_CANCEL = "PENDING_CANCEL"
    #: Sent, no ack, outcome genuinely unknown. Distinct from REJECTED because
    #: an unknown order may still be live and must be reconciled, not retried.
    UNKNOWN = "UNKNOWN"


TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED, OrderStatus.CANCELLED,
    OrderStatus.REJECTED, OrderStatus.EXPIRED,
})


def now_ms() -> int:
    """Milliseconds since epoch - the unit every venue here uses on the wire."""
    return int(time.time() * 1000)


@dataclass
class OrderRequest:
    """An order as the client intends it, before any venue has seen it.

    Deliberately separate from the venue's order representation: this is what
    the validator operates on, and it carries client-side concerns (the
    idempotency key, the signature envelope) that no single venue's schema has
    all of.
    """

    market: str
    side: Side
    order_type: OrderType
    size: Decimal
    price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    post_only: bool = False
    self_trade_mode: SelfTradeMode = SelfTradeMode.CANCEL_TAKER
    #: Idempotency key. Generated if absent so every order has one - resending
    #: without a stable id is how you end up double-filled after a timeout.
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    expires_at_ms: Optional[int] = None
    #: Cap on adverse move for MARKET orders, in basis points of mark.
    max_slippage_bps: Optional[Decimal] = None
    leverage: Optional[Decimal] = None

    # -- signature envelope (venues that sign orders client-side) ----------
    signature: Optional[str] = None
    signature_timestamp_ms: Optional[int] = None
    signature_expiry_ms: Optional[int] = None
    nonce: Optional[int] = None
    account: Optional[str] = None

    def __post_init__(self) -> None:
        # Normalise once here so every downstream rule can assume Decimal.
        self.size = dec(self.size)
        if self.price is not None:
            self.price = dec(self.price)
        if self.trigger_price is not None:
            self.trigger_price = dec(self.trigger_price)
        if self.max_slippage_bps is not None:
            self.max_slippage_bps = dec(self.max_slippage_bps)
        if self.leverage is not None:
            self.leverage = dec(self.leverage)
        # POST_ONLY is expressible both as a TIF and as a flag depending on the
        # venue. Accept either spelling and keep them consistent internally.
        if self.time_in_force is TimeInForce.POST_ONLY:
            self.post_only = True

    @property
    def is_buy(self) -> bool:
        return self.side is Side.BUY

    @property
    def signed_size(self) -> Decimal:
        """Position delta if fully filled: +size for BUY, -size for SELL."""
        return self.size * self.side.sign

    def notional(self, reference_price: Optional[Decimal] = None) -> Optional[Decimal]:
        """size x price. Falls back to `reference_price` for market orders."""
        p = self.price if self.price is not None else reference_price
        return None if p is None else self.size * p

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready. Decimals become strings - never floats, which would
        reintroduce the precision loss the Decimal discipline exists to avoid."""
        out: Dict[str, Any] = {
            "market": self.market,
            "side": self.side.value,
            "type": self.order_type.value,
            "size": str(self.size),
            "time_in_force": self.time_in_force.value,
            "reduce_only": self.reduce_only,
            "post_only": self.post_only,
            "client_order_id": self.client_order_id,
        }
        if self.price is not None:
            out["price"] = str(self.price)
        if self.trigger_price is not None:
            out["trigger_price"] = str(self.trigger_price)
        if self.signature is not None:
            out["signature"] = self.signature
        if self.signature_timestamp_ms is not None:
            out["signature_timestamp"] = self.signature_timestamp_ms
        if self.nonce is not None:
            out["nonce"] = self.nonce
        return out


    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrderRequest":
        """Inverse of `to_dict`. Decimals come back from strings.

        Symmetric with `to_dict` by construction - the wire key for order type
        is "type" and for the signature timestamp is "signature_timestamp",
        and a round trip that silently dropped either would resurrect an order
        with the wrong semantics after a restart.
        """
        return cls(
            market=data["market"],
            side=Side(data["side"]),
            order_type=OrderType(data["type"]),
            size=dec(data["size"]),
            price=dec(data["price"]) if data.get("price") is not None else None,
            trigger_price=(dec(data["trigger_price"])
                           if data.get("trigger_price") is not None else None),
            time_in_force=TimeInForce(data.get("time_in_force", "GTC")),
            reduce_only=bool(data.get("reduce_only", False)),
            post_only=bool(data.get("post_only", False)),
            client_order_id=data.get("client_order_id", ""),
            signature=data.get("signature"),
            signature_timestamp_ms=data.get("signature_timestamp"),
            nonce=data.get("nonce"),
        )


@dataclass
class Position:
    """Net position in one market. `size` is signed: negative is short."""

    market: str
    size: Decimal = ZERO
    entry_price: Decimal = ZERO
    mark_price: Decimal = ZERO
    leverage: Decimal = Decimal(1)
    #: Cumulative funding paid (negative) or received (positive) on this
    #: position. Tracked separately from unrealised PnL because it is already
    #: *realised* - it has left or entered collateral and cannot be given back
    #: by a favourable price move.
    funding_paid: Decimal = ZERO

    def __post_init__(self) -> None:
        self.size = dec(self.size)
        self.entry_price = dec(self.entry_price)
        self.mark_price = dec(self.mark_price)
        self.leverage = dec(self.leverage)
        self.funding_paid = dec(self.funding_paid)

    @property
    def is_flat(self) -> bool:
        return self.size == ZERO

    @property
    def is_long(self) -> bool:
        return self.size > ZERO

    @property
    def notional(self) -> Decimal:
        return abs(self.size) * self.mark_price

    @property
    def unrealised_pnl(self) -> Decimal:
        if self.is_flat or self.mark_price == ZERO:
            return ZERO
        return (self.mark_price - self.entry_price) * self.size

    def would_reduce(self, delta: Decimal) -> bool:
        """True if applying `delta` moves the position toward flat.

        A delta larger than the position flips it rather than reducing it, and
        that is checked separately - flipping is a different rejection than
        increasing, because the fix differs (resize vs. drop reduce_only).
        """
        if self.is_flat:
            return False
        return (self.size > ZERO and delta < ZERO) or (self.size < ZERO and delta > ZERO)

    def would_flip(self, delta: Decimal) -> bool:
        if self.is_flat:
            return False
        new = self.size + delta
        return new != ZERO and (new > ZERO) != (self.size > ZERO)

    def funding_payment(self, rate: "FundingRate",
                        mark: Optional[Decimal] = None) -> Decimal:
        """Cash flow to this position holder for one funding period.

        Negative is a payment out, positive is a receipt:

            payment = -signed_size * mark * rate

        A long with a positive rate pays; a short with a positive rate
        receives. Charged on the notional **at settlement**, not at entry -
        a position that has doubled in value pays funding on the doubled
        amount, which is what makes funding compound against a winning long
        in a high-rate regime.
        """
        price = dec(mark) if mark is not None else self.mark_price
        if self.is_flat or price <= ZERO:
            return ZERO
        return -(self.size * price * rate.rate)

    # -- maintenance margin and liquidation --------------------------------

    def maintenance_margin(self, mmf: Decimal) -> Decimal:
        """Collateral that must remain against this position to keep it open.

        Charged on *mark* notional rather than entry notional: as the position
        moves against you the requirement grows, which is precisely why
        liquidation accelerates rather than arriving at a fixed price.
        """
        return abs(self.size) * self.mark_price * dec(mmf)

    def equity(self, collateral: Decimal) -> Decimal:
        """Collateral posted plus unrealised PnL."""
        return dec(collateral) + self.unrealised_pnl

    def liquidation_price(self, collateral: Decimal,
                          mmf: Decimal) -> Optional[Decimal]:
        """Mark price at which equity falls to the maintenance requirement.

        Solving `collateral + (P - entry) * size == |size| * P * mmf` for P:

            P = (entry * size - collateral) / (size * (1 - sign(size) * mmf))

        The `sign` term is what makes one expression cover both directions. A
        long liquidates on the way *down* and a short on the way *up*, and the
        maintenance requirement moves with the mark in both cases, so it cannot
        be modelled as a fixed percentage below entry - a common shortcut that
        understates risk on shorts, where the requirement grows as the price
        rises against you.

        `collateral` must be the collateral *backing this position*, not the
        free (unencumbered) remainder - free collateral is what is left after
        margin is posted, and using it puts a long's liquidation price above
        its mark, which is the wrong side of the market entirely.

        Under cross margin one pool backs every position, so a per-position
        liquidation price is exact only when it is the sole position; with
        several open, the account-level `AccountState.margin_ratio` is the
        authoritative health measure and this is an approximation that ignores
        the others' PnL.

        Returns None for a flat position, and None when the denominator
        vanishes (`mmf == 1` on a long), which is a degenerate configuration
        rather than a price.
        """
        if self.is_flat:
            return None
        mmf = dec(mmf)
        sign = Decimal(1) if self.size > ZERO else Decimal(-1)
        denominator = self.size * (Decimal(1) - sign * mmf)
        if denominator == ZERO:
            return None
        price = (self.entry_price * self.size - dec(collateral)) / denominator
        # A negative liquidation price means the position cannot be liquidated
        # by an adverse move alone - collateral exceeds the whole notional.
        return price if price > ZERO else None

    def margin_ratio(self, collateral: Decimal, mmf: Decimal) -> Optional[Decimal]:
        """equity / maintenance_margin. Below 1.0 the position is liquidatable.

        None when there is no maintenance requirement (flat, or zero mark), so
        callers cannot mistake "no requirement" for "infinitely safe" via a
        divide-by-zero sentinel.
        """
        required = self.maintenance_margin(mmf)
        if required <= ZERO:
            return None
        return self.equity(collateral) / required

    def distance_to_liquidation_bps(self, collateral: Decimal,
                                    mmf: Decimal) -> Optional[Decimal]:
        """How far the mark can move before liquidation, in basis points.

        Always positive - it is a distance, not a direction. The direction is
        implied by the side.
        """
        liq = self.liquidation_price(collateral, mmf)
        if liq is None or self.mark_price <= ZERO:
            return None
        return abs(self.mark_price - liq) / self.mark_price * Decimal(10000)


@dataclass
class FundingRate:
    """A perpetual's funding rate for one period.

    Sign convention, which is where implementations go wrong: a **positive**
    rate means the perp is trading above its index, so **longs pay shorts**.
    Negative reverses it. Getting this backwards produces a strategy that
    looks profitable precisely when it is bleeding.
    """

    market: str
    rate: Decimal
    period_hours: int = 8
    timestamp_ms: int = field(default_factory=now_ms)
    source: str = "unknown"

    def __post_init__(self) -> None:
        self.rate = dec(self.rate)

    @property
    def periods_per_year(self) -> Decimal:
        if self.period_hours <= 0:
            return Decimal(0)
        return Decimal(24 * 365) / Decimal(self.period_hours)

    @property
    def apr(self) -> Decimal:
        """Annualised, as a fraction. 0.0001 per 8h is ~10.95% a year.

        Worth computing because a per-period rate looks negligible and an
        annualised one does not - and a carry trade is judged on the second.
        """
        return self.rate * self.periods_per_year

    def age_ms(self, now: Optional[int] = None) -> int:
        return (now if now is not None else now_ms()) - self.timestamp_ms

    def __str__(self) -> str:
        return "%s %s/period (%.2f%% APR) over %dh" % (
            self.market, self.rate, self.apr * 100, self.period_hours)


@dataclass
class Balance:
    asset: str
    total: Decimal = ZERO
    available: Decimal = ZERO

    def __post_init__(self) -> None:
        self.total = dec(self.total)
        self.available = dec(self.available)


@dataclass
class AccountState:
    """Snapshot of everything the validator needs about an account.

    A snapshot rather than a live handle: validation must be deterministic and
    testable, so the caller decides when state is refreshed. `as_of_ms` lets a
    staleness rule reject decisions made on an old picture.
    """

    account_id: str = "default"
    balances: Dict[str, Balance] = field(default_factory=dict)
    positions: Dict[str, Position] = field(default_factory=dict)
    #: Resting orders keyed by venue order id - needed for self-trade checks.
    open_orders: Dict[str, "OrderRequest"] = field(default_factory=dict)
    free_collateral: Decimal = ZERO
    total_collateral: Decimal = ZERO
    onboarded: bool = True
    liquidating: bool = False
    as_of_ms: int = field(default_factory=now_ms)

    def __post_init__(self) -> None:
        self.free_collateral = dec(self.free_collateral)
        self.total_collateral = dec(self.total_collateral)

    def position(self, market: str) -> Position:
        """Never returns None - a market with no position is a flat position,
        and forcing every caller to handle None invites missed branches."""
        return self.positions.get(market) or Position(market=market)

    def available(self, asset: str) -> Decimal:
        b = self.balances.get(asset)
        return b.available if b else ZERO

    # -- cross-margin health ----------------------------------------------
    # Perp venues here margin on a cross basis: one collateral pool backs every
    # position, so health is an account-level property. A per-position view
    # would report each leg as safe while the account as a whole is not - the
    # exact failure mode of a hedged book whose legs are margined separately.

    def maintenance_margin(self, registry) -> Decimal:
        """Total maintenance requirement across all open positions."""
        total = ZERO
        for market, pos in self.positions.items():
            if pos.is_flat:
                continue
            inst = registry.get(market)
            if inst is None:
                continue
            total += pos.maintenance_margin(inst.mm_fraction)
        return total

    @property
    def unrealised_pnl(self) -> Decimal:
        return sum((p.unrealised_pnl for p in self.positions.values()), ZERO)

    @property
    def equity(self) -> Decimal:
        """Account value: collateral plus unrealised PnL on open positions."""
        return self.total_collateral + self.unrealised_pnl

    def margin_ratio(self, registry) -> Optional[Decimal]:
        """equity / maintenance_margin across the account.

        Below 1.0 the account is liquidatable. None when nothing is open.
        """
        required = self.maintenance_margin(registry)
        if required <= ZERO:
            return None
        return self.equity / required

    def is_liquidatable(self, registry) -> bool:
        ratio = self.margin_ratio(registry)
        return ratio is not None and ratio < Decimal(1)


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    """L2 snapshot. `bids` descending, `asks` ascending - enforced on build."""

    market: str
    bids: List[BookLevel] = field(default_factory=list)
    asks: List[BookLevel] = field(default_factory=list)
    timestamp_ms: int = field(default_factory=now_ms)
    source: str = "unknown"

    @classmethod
    def from_levels(
        cls,
        market: str,
        bids: List[Tuple[Any, Any]],
        asks: List[Tuple[Any, Any]],
        source: str = "unknown",
        timestamp_ms: Optional[int] = None,
    ) -> "OrderBook":
        """Build from raw (price, size) pairs, sorting and dropping zero sizes.

        Venues disagree on ordering and several include zero-size levels as
        deletion markers in snapshots; normalising here means no downstream
        rule has to care.
        """
        b = sorted(
            (BookLevel(dec(p), dec(s)) for p, s in bids if dec(s) > ZERO),
            key=lambda lv: lv.price, reverse=True,
        )
        a = sorted(
            (BookLevel(dec(p), dec(s)) for p, s in asks if dec(s) > ZERO),
            key=lambda lv: lv.price,
        )
        return cls(market, b, a, timestamp_ms or now_ms(), source)

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[Decimal]:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return None

    @property
    def spread_bps(self) -> Optional[Decimal]:
        m = self.mid
        if m is None or m == ZERO:
            return None
        return (self.asks[0].price - self.bids[0].price) / m * 10000

    @property
    def is_crossed(self) -> bool:
        """Bid >= ask. Means a stale or stitched-together snapshot; any price
        derived from it is untrustworthy, so rules reject rather than guess."""
        return bool(self.bids and self.asks and self.bids[0].price >= self.asks[0].price)

    def sweep(self, side: Side, size: Decimal) -> Tuple[Decimal, Decimal, bool]:
        """Walk the book taking `size`.

        Returns (filled_size, average_price, fully_filled). Used both to
        estimate market-order slippage before sending and to fill orders in the
        paper engine, so the pre-trade estimate and the simulated execution
        cannot drift apart.
        """
        levels = self.asks if side is Side.BUY else self.bids
        remaining = dec(size)
        cost = ZERO
        taken = ZERO
        for lv in levels:
            if remaining <= ZERO:
                break
            take = min(remaining, lv.size)
            cost += take * lv.price
            taken += take
            remaining -= take
        if taken == ZERO:
            return ZERO, ZERO, False
        return taken, cost / taken, remaining <= ZERO


@dataclass
class Quote:
    """A mark/index price with provenance and an age, so staleness is checkable."""

    market: str
    price: Decimal
    timestamp_ms: int = field(default_factory=now_ms)
    source: str = "unknown"

    def __post_init__(self) -> None:
        self.price = dec(self.price)

    def age_ms(self, now: Optional[int] = None) -> int:
        return (now if now is not None else now_ms()) - self.timestamp_ms
