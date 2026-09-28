"""OMS domain model — orders, fills, positions.

Deliberately plain dataclasses with no persistence concerns: the store layer
maps these to SQLite, the engine layer owns the state transitions. Keeping
them free of I/O is what makes the P&L accounting unit-testable.

Sign convention: quantities on Position are SIGNED (long > 0, short < 0).
Order.qty is always positive and paired with a Side — mixing the two
conventions in one field is a classic source of sign bugs in blotters.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    NEW = "new"                          # accepted, nothing filled
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"                # failed a pre-trade risk gate

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                        OrderStatus.REJECTED)


@dataclass
class Order:
    """A parent or child order. `parent_id` set => this is a child slice."""
    id: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_price: float = 0.0
    parent_id: str | None = None
    portfolio_id: str = "DEFAULT"
    venue: str | None = None
    algo: str | None = None              # SOR | TWAP | MANUAL ...
    arrival_mid: float | None = None     # benchmark for implementation shortfall
    reject_reason: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def leaves_qty(self) -> float:
        """Unfilled remainder — what an algo still has left to work."""
        return max(0.0, self.qty - self.filled_qty)

    def shortfall_bps(self) -> float | None:
        """Implementation shortfall vs the mid when the order was accepted.
        Positive = worse than arrival (paid up on a buy, sold cheap on a
        sell); sign-flipped for sells so 'positive is bad' holds either way."""
        if not self.arrival_mid or self.filled_qty <= 0:
            return None
        return ((self.avg_price - self.arrival_mid) / self.arrival_mid
                * 1e4 * self.side.sign)


@dataclass
class Fill:
    id: str
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    venue: str | None = None
    fee: float = 0.0
    ts: str = field(default_factory=_now)


@dataclass
class Position:
    """Net position per (portfolio, symbol), average-cost basis."""
    symbol: str
    portfolio_id: str = "DEFAULT"
    qty: float = 0.0                     # SIGNED
    avg_price: float = 0.0               # 0 when flat
    realized_pnl: float = 0.0
    fees: float = 0.0
    updated_at: str = field(default_factory=_now)

    def unrealized_pnl(self, mark: float) -> float:
        if self.qty == 0 or not mark:
            return 0.0
        return (mark - self.avg_price) * self.qty

    def notional(self, mark: float) -> float:
        return abs(self.qty) * (mark or self.avg_price)
