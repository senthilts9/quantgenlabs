"""The venue adapter contract.

One interface over a local paper engine, a signed-order Starknet DEX, an EVM
perps venue and two centralised exchanges. The differences that matter are
declared as *capabilities* rather than hidden behind uniform methods that
quietly do nothing:

* `supports_trading` - false for read-only feeds.
* `has_matching_engine` - **false for the Coinbase sandbox**, which returns
  canned responses. Code that measures fills must refuse such a venue rather
  than report fictional executions as real.
* `requires_signature` - per-order client-side signing (Paradex, Hyperliquid)
  versus session auth (Binance HMAC, Coinbase JWT).

An adapter that cannot do something raises `VenueUnsupported`. It never
returns an empty success, because an empty success is indistinguishable from
"nothing happened yet" and that is how phantom positions appear.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from ..config import Settings
from ..core.errors import Rejection, ValidationReport
from ..core.instruments import Instrument, InstrumentRegistry
from ..core.replay import ReplayGuard
from ..core.types import (
    ZERO, AccountState, FundingRate, OrderBook, OrderRequest, OrderStatus,
    Quote, now_ms,
)


class VenueError(Exception):
    """Venue rejected the request or could not answer."""


class VenueUnsupported(VenueError):
    """The venue genuinely cannot do this - not a transient failure."""


class NotAuthenticated(VenueError):
    """Credentials missing or rejected."""


@dataclass
class Fill:
    """One execution. `fee` is signed: negative is a maker rebate."""

    order_id: str
    market: str
    side: str
    size: Decimal
    price: Decimal
    fee: Decimal = ZERO
    liquidity: str = "taker"  # or "maker"
    timestamp_ms: int = field(default_factory=now_ms)
    trade_id: str = ""


@dataclass
class OrderAck:
    """What a venue said about an order.

    `filled_size` and `avg_price` are populated for immediate executions so a
    caller does not need a second round trip to learn an IOC filled. `report`
    carries local pre-trade rejections; `rejections` carries the venue's own.
    """

    client_order_id: str
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_size: Decimal = ZERO
    avg_price: Optional[Decimal] = None
    fills: List[Fill] = field(default_factory=list)
    rejections: List[Rejection] = field(default_factory=list)
    report: Optional[ValidationReport] = None
    venue: str = ""
    raw: Optional[Dict] = None
    timestamp_ms: int = field(default_factory=now_ms)

    @property
    def accepted(self) -> bool:
        return self.status not in (OrderStatus.REJECTED, OrderStatus.UNKNOWN)

    @property
    def is_terminal(self) -> bool:
        from ..core.types import TERMINAL_STATUSES
        return self.status in TERMINAL_STATUSES

    def __str__(self) -> str:
        base = "%s %s" % (self.status.value, self.order_id or self.client_order_id)
        if self.filled_size > ZERO:
            base += " filled=%s@%s" % (self.filled_size, self.avg_price)
        if self.rejections:
            base += " | " + "; ".join(str(r) for r in self.rejections)
        return base


class VenueAdapter:
    """Base adapter. Subclasses override the capabilities they have."""

    name = "abstract"
    supports_trading = False
    has_matching_engine = False
    requires_signature = False
    supports_cancel = True
    #: Set on venues whose responses are mocked, so results can be labelled.
    is_mock = False

    def __init__(self, settings: Optional[Settings] = None,
                 registry: Optional[InstrumentRegistry] = None,
                 store=None):
        self.settings = settings or Settings.from_env()
        self.registry = registry or InstrumentRegistry()
        # Replay state lives on the adapter because that is where validation
        # runs. Without it every validation started from an empty nonce set and
        # the replay rule could never fire outside a test.
        #
        # Backed by the store when one is supplied: an in-memory guard is
        # emptied by a restart, which makes a client_order_id that was already
        # sent reusable again - the idempotency hole the guard exists to close.
        self.store = store
        self.replay = ReplayGuard(store=store)

    def replay_kwargs(self) -> Dict[str, object]:
        """Context kwargs carrying replay state into the validator."""
        return {"seen_nonces": self.replay.nonces,
                "seen_client_ids": self.replay.client_order_ids}

    def record_submission(self, request: OrderRequest) -> None:
        """Mark an order's nonce and id as used. Call *after* validation
        passes, or an order would collide with itself."""
        self.replay.record(request.nonce, request.client_order_id)

    # -- market data -------------------------------------------------------

    def fetch_instruments(self) -> List[Instrument]:
        raise VenueUnsupported("%s does not publish instrument specs" % self.name)

    def fetch_book(self, market: str, depth: int = 25) -> OrderBook:
        raise VenueUnsupported("%s has no order book endpoint" % self.name)

    def fetch_quote(self, market: str) -> Quote:
        book = self.fetch_book(market, depth=1)
        mid = book.mid
        if mid is None:
            raise VenueError("%s: no mid for %s" % (self.name, market))
        return Quote(market, mid, book.timestamp_ms, self.name)

    def fetch_funding_rate(self, market: str):
        """Current funding rate, or None where the venue does not publish one.

        Perp-only by nature. Returning None rather than zero is the honest
        answer for a venue we cannot ask - zero would be indistinguishable from
        "flat funding", and a carry strategy would price it as free.
        """
        raise VenueUnsupported("%s does not publish funding rates" % self.name)

    # -- account and trading ----------------------------------------------

    def fetch_account(self) -> AccountState:
        raise VenueUnsupported("%s has no account endpoint" % self.name)

    def place_order(self, request: OrderRequest) -> OrderAck:
        raise VenueUnsupported("%s is read-only" % self.name)

    def cancel_order(self, order_id: str, market: Optional[str] = None) -> OrderAck:
        raise VenueUnsupported("%s cannot cancel" % self.name)

    def cancel_all(self, market: Optional[str] = None) -> List[OrderAck]:
        """Cancel everything, best effort.

        Default walks open orders one at a time. Venues with a bulk endpoint
        override this - during a risk event the difference between one request
        and two hundred is the difference between flat and not.
        """
        out = []
        for oid, order in list(self.fetch_open_orders().items()):
            if market and order.market != market:
                continue
            try:
                out.append(self.cancel_order(oid, order.market))
            except VenueError as exc:
                out.append(OrderAck(
                    client_order_id=order.client_order_id, order_id=oid,
                    status=OrderStatus.UNKNOWN, venue=self.name,
                    raw={"error": str(exc)},
                ))
        return out

    def fetch_open_orders(self) -> Dict[str, OrderRequest]:
        raise VenueUnsupported("%s has no open-orders endpoint" % self.name)

    def fetch_fills(self, market: Optional[str] = None) -> List[Fill]:
        raise VenueUnsupported("%s has no fills endpoint" % self.name)

    # -- introspection -----------------------------------------------------

    def capabilities(self) -> Dict[str, object]:
        from ..config import ENDPOINTS
        ep = ENDPOINTS.get(self.name)
        return {
            "venue": self.name,
            "supports_trading": self.supports_trading,
            "has_matching_engine": self.has_matching_engine,
            "requires_signature": self.requires_signature,
            "supports_cancel": self.supports_cancel,
            "is_mock": self.is_mock,
            "notes": ep.notes if ep else "",
        }

    def assert_real_execution(self) -> None:
        """Guard for code that must not run against mocked responses.

        Called by anything that records fills as truth - a PnL calculation or a
        TCA report built on canned sandbox data is worse than no report,
        because it looks like evidence.
        """
        if self.is_mock or not self.has_matching_engine:
            raise VenueUnsupported(
                "%s has no matching engine (is_mock=%s). Its fills are not real "
                "executions and must not be used for PnL, TCA or fill analysis."
                % (self.name, self.is_mock)
            )

    def __repr__(self) -> str:
        return "<%s trading=%s matching=%s>" % (
            type(self).__name__, self.supports_trading, self.has_matching_engine
        )
