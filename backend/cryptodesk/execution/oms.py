"""Order management: lifecycle, idempotency and reconciliation.

The OMS owns the answer to "what do we think is live right now?" and, more
importantly, owns the cases where that answer is uncertain.

Three things it exists to get right:

**Idempotency.** A resend after a timeout must not create a second order. Every
order carries a `client_order_id`; the OMS refuses a duplicate and returns the
original outcome instead. Without this, the single most common network failure
- request sent, response lost - doubles your position.

**The UNKNOWN state.** When a send times out, the order may or may not be live.
Treating that as a failure and retrying is how you get filled twice; treating
it as a success is how you get a phantom position. It becomes UNKNOWN and is
resolved by reconciliation against the venue, never by guessing.

**Legal transitions.** A FILLED order cannot become NEW. Enforcing the state
machine catches out-of-order venue messages, which arrive more often than the
happy path suggests - a fill and an ack can cross on the wire.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Set

from ..core.errors import Code, Rejection, Stage, ValidationReport
from ..core.types import (
    TERMINAL_STATUSES, ZERO, OrderRequest, OrderStatus, Quote, now_ms,
)
from ..risk.limits import RiskEngine
from ..venues.base import Fill, OrderAck, VenueAdapter, VenueError

#: Legal lifecycle transitions. Anything not listed is rejected and logged
#: rather than applied - an illegal transition means our view and the venue's
#: have diverged, and overwriting state would hide that.
LEGAL_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.PENDING_NEW: {
        OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.UNKNOWN,
        OrderStatus.EXPIRED,
    },
    OrderStatus.NEW: {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED,
        OrderStatus.PENDING_CANCEL, OrderStatus.EXPIRED, OrderStatus.UNKNOWN,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED,
        OrderStatus.PENDING_CANCEL, OrderStatus.EXPIRED, OrderStatus.UNKNOWN,
    },
    OrderStatus.PENDING_CANCEL: {
        OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
        OrderStatus.UNKNOWN,
    },
    # UNKNOWN can resolve to anything - that is the point of reconciliation.
    OrderStatus.UNKNOWN: {
        OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
    },
}


@dataclass
class TrackedOrder:
    """An order and everything that has happened to it."""

    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING_NEW
    order_id: Optional[str] = None
    venue: str = ""
    filled_size: Decimal = ZERO
    cost: Decimal = ZERO
    fills: List[Fill] = field(default_factory=list)
    rejections: List[Rejection] = field(default_factory=list)
    report: Optional[ValidationReport] = None
    created_ms: int = field(default_factory=now_ms)
    updated_ms: int = field(default_factory=now_ms)
    transitions: List[str] = field(default_factory=list)
    attempts: int = 0

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    @property
    def remaining(self) -> Decimal:
        return self.request.size - self.filled_size

    @property
    def avg_price(self) -> Optional[Decimal]:
        return self.cost / self.filled_size if self.filled_size > ZERO else None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_live(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED,
                               OrderStatus.PENDING_NEW, OrderStatus.PENDING_CANCEL)

    def transition(self, new: OrderStatus, note: str = "") -> bool:
        """Apply a status change if legal. Returns False and records if not."""
        if new is self.status:
            return True
        allowed = LEGAL_TRANSITIONS.get(self.status, set())
        stamp = "%d %s->%s" % (now_ms(), self.status.value, new.value)
        if new not in allowed:
            self.transitions.append(stamp + " REJECTED(illegal)" +
                                    (" %s" % note if note else ""))
            return False
        self.status = new
        self.updated_ms = now_ms()
        self.transitions.append(stamp + (" %s" % note if note else ""))
        return True

    def apply_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self.filled_size += fill.size
        self.cost += fill.size * fill.price
        self.updated_ms = now_ms()

    def to_record(self) -> Dict[str, object]:
        """Full durable form - enough to reconstruct after a restart.

        Distinct from `summary()`, which is a human-readable projection and is
        deliberately lossy. Persisting the summary would leave a restarted
        process unable to rebuild the original request, and therefore unable
        to cancel or reconcile the order it describes.
        """
        return {
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "market": self.request.market,
            "status": self.status.value,
            "venue": self.venue,
            "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
            "attempts": self.attempts,
            "filled_size": str(self.filled_size),
            "cost": str(self.cost),
            "request": self.request.to_dict(),
            "transitions": self.transitions[-40:],
            "fills": [
                {"order_id": f.order_id, "market": f.market, "side": f.side,
                 "size": str(f.size), "price": str(f.price), "fee": str(f.fee),
                 "liquidity": f.liquidity, "timestamp_ms": f.timestamp_ms,
                 "trade_id": f.trade_id}
                for f in self.fills
            ],
        }

    @classmethod
    def from_record(cls, record: Dict[str, object]) -> "TrackedOrder":
        from ..venues.base import Fill
        req = OrderRequest.from_dict(record["request"])
        t = cls(
            request=req,
            status=OrderStatus(record["status"]),
            order_id=record.get("order_id"),
            venue=record.get("venue", ""),
            filled_size=Decimal(str(record.get("filled_size", "0"))),
            cost=Decimal(str(record.get("cost", "0"))),
            created_ms=int(record.get("created_ms", now_ms())),
            updated_ms=int(record.get("updated_ms", now_ms())),
            attempts=int(record.get("attempts", 0)),
            transitions=list(record.get("transitions") or []),
        )
        for f in record.get("fills") or []:
            t.fills.append(Fill(
                order_id=f.get("order_id", ""), market=f.get("market", ""),
                side=f.get("side", "BUY"), size=Decimal(str(f.get("size", "0"))),
                price=Decimal(str(f.get("price", "0"))),
                fee=Decimal(str(f.get("fee", "0"))),
                liquidity=f.get("liquidity", "taker"),
                timestamp_ms=int(f.get("timestamp_ms", 0)),
                trade_id=f.get("trade_id", ""),
            ))
        t.transitions.append("%d REHYDRATED as %s" % (now_ms(), t.status.value))
        return t

    def summary(self) -> Dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "market": self.request.market,
            "side": self.request.side.value,
            "size": str(self.request.size),
            "price": str(self.request.price) if self.request.price else None,
            "status": self.status.value,
            "filled": str(self.filled_size),
            "avg_price": str(self.avg_price) if self.avg_price else None,
            "venue": self.venue,
            "attempts": self.attempts,
            "rejections": [r.code.value for r in self.rejections],
        }


class OrderManager:
    """Tracks orders across their lifetime against one venue."""

    def __init__(
        self,
        venue: VenueAdapter,
        risk: Optional[RiskEngine] = None,
        max_retries: int = 2,
        store=None,
    ):
        self.venue = venue
        self.risk = risk or RiskEngine(store=store)
        self.max_retries = max_retries
        self.orders: Dict[str, TrackedOrder] = {}
        self._by_venue_id: Dict[str, str] = {}
        self._store = store
        if store is not None:
            self._rehydrate()

    def _rehydrate(self) -> None:
        """Reload the blotter for this venue.

        Orders come back in whatever state they were last written. Anything
        still live is then `reconcile()`'s problem - which is the point: a
        restart should surface orders the venue may still be holding, not
        quietly forget them.
        """
        for coid, record in self._store.load_orders(venue=self.venue.name).items():
            try:
                tracked = TrackedOrder.from_record(record)
            except (KeyError, ValueError, TypeError):
                continue  # one unreadable row must not lose the blotter
            self.orders[coid] = tracked
            if tracked.order_id:
                self._by_venue_id[tracked.order_id] = coid

    def _persist(self, tracked: TrackedOrder) -> None:
        if self._store is not None:
            self._store.save_order(tracked.to_record())

    # -- submission --------------------------------------------------------

    def submit(self, request: OrderRequest, mark: Optional[Quote] = None,
               book=None) -> TrackedOrder:
        """Risk-check and send an order, tracking it throughout.

        Idempotent on `client_order_id`: resubmitting a known id returns the
        existing record untouched rather than sending again.
        """
        coid = request.client_order_id
        existing = self.orders.get(coid)
        if existing is not None:
            existing.transitions.append(
                "%d duplicate submit ignored (idempotency)" % now_ms()
            )
            return existing

        tracked = TrackedOrder(request=request, venue=self.venue.name)
        self.orders[coid] = tracked

        account = None
        try:
            account = self.venue.fetch_account()
        except VenueError:
            pass  # risk checks that need it will be skipped, not faked

        # Resolve a mark if the caller did not supply one. Without it a market
        # order has no price at all, and the notional limits would have nothing
        # to measure against.
        if mark is None:
            try:
                mark = self.venue.fetch_quote(request.market)
            except VenueError:
                pass

        rejections = self.risk.check(
            request, account=account, mark=mark, venue=self.venue.name,
            book_source=book.source if book is not None else None,
            open_order_count=len(self.live_orders()),
        )
        if rejections:
            tracked.rejections.extend(rejections)
            tracked.transition(OrderStatus.REJECTED, "risk")
            self._persist(tracked)
            return tracked

        # Write-ahead. The record is committed *before* the order leaves, so a
        # crash between here and the acknowledgement leaves a PENDING_NEW row
        # to reconcile against rather than no trace at all. Persisting after
        # the send would make the UNKNOWN state unreachable in exactly the
        # scenario it was designed for.
        self._persist(tracked)
        return self._send(tracked)

    def _send(self, tracked: TrackedOrder) -> TrackedOrder:
        tracked.attempts += 1
        try:
            ack = self.venue.place_order(tracked.request)
        except VenueError as exc:
            tracked.transition(OrderStatus.UNKNOWN, "venue error: %s" % exc)
            self._persist(tracked)
            return tracked

        self._apply_ack(tracked, ack)

        # Auto-retry only for rejections with a mechanical fix that has already
        # been applied - never for anything involving money or risk.
        if (tracked.status is OrderStatus.REJECTED
                and tracked.attempts <= self.max_retries
                and any(r.retryable for r in ack.rejections)):
            retryable = [r for r in ack.rejections if r.retryable]
            if all(r.code is Code.RATE_LIMITED for r in retryable):
                time.sleep(min(0.5 * tracked.attempts, 2.0))
                tracked.status = OrderStatus.PENDING_NEW
                tracked.transitions.append("%d retry after rate limit" % now_ms())
                return self._send(tracked)
        return tracked

    def _apply_ack(self, tracked: TrackedOrder, ack: OrderAck) -> None:
        if ack.order_id:
            tracked.order_id = ack.order_id
            self._by_venue_id[ack.order_id] = tracked.client_order_id
        tracked.report = ack.report or tracked.report
        if ack.rejections:
            tracked.rejections.extend(ack.rejections)
        for fill in ack.fills:
            tracked.apply_fill(fill)
        tracked.transition(ack.status, "venue ack")
        self._persist(tracked)

    # -- cancellation ------------------------------------------------------

    def cancel(self, client_order_id: str) -> TrackedOrder:
        tracked = self.orders.get(client_order_id)
        if tracked is None:
            raise KeyError("unknown client_order_id %r" % client_order_id)
        if tracked.is_terminal:
            tracked.transitions.append(
                "%d cancel ignored (already %s)" % (now_ms(), tracked.status.value)
            )
            return tracked
        if not tracked.order_id:
            tracked.transition(OrderStatus.UNKNOWN, "cancel with no venue id")
            return tracked

        tracked.transition(OrderStatus.PENDING_CANCEL, "cancel sent")
        try:
            ack = self.venue.cancel_order(tracked.order_id, tracked.request.market)
        except VenueError as exc:
            tracked.transition(OrderStatus.UNKNOWN, "cancel failed: %s" % exc)
            self._persist(tracked)
            return tracked
        self._apply_ack(tracked, ack)
        return tracked

    def cancel_all(self, market: Optional[str] = None) -> List[TrackedOrder]:
        return [
            self.cancel(t.client_order_id)
            for t in list(self.live_orders())
            if market is None or t.request.market == market
        ]

    # -- reconciliation ----------------------------------------------------

    def reconcile(self) -> Dict[str, List[str]]:
        """Compare local state against the venue and report every disagreement.

        Reports rather than silently repairs. An order the venue has and we do
        not is an *orphan* - it may be a stale id, or it may be real size
        nobody is watching. Which of those it is determines whether you cancel
        it or adopt it, and that decision is not the OMS's to make silently.
        """
        findings: Dict[str, List[str]] = {
            "resolved_unknown": [], "orphaned_at_venue": [],
            "missing_at_venue": [], "status_mismatch": [],
            "unacknowledged": [],
        }
        try:
            remote = self.venue.fetch_open_orders()
        except VenueError as exc:
            findings["status_mismatch"].append("could not fetch venue orders: %s" % exc)
            return findings

        remote_ids = set(remote)

        for tracked in list(self.orders.values()):
            # PENDING_NEW with no venue id is the most dangerous state a
            # restart can leave behind: the write-ahead record proves we were
            # about to send, but nothing proves whether it landed, and there
            # is no venue id to look it up by. It has to be checked by
            # client_order_id before anything is resent - the checks below
            # all key on order_id and would skip it in silence.
            if (tracked.status is OrderStatus.PENDING_NEW
                    and not tracked.order_id):
                findings["unacknowledged"].append(
                    "%s was written ahead of a send that never acknowledged - "
                    "query the venue by client_order_id before resending"
                    % tracked.client_order_id)
                continue

            if tracked.status is OrderStatus.UNKNOWN:
                if tracked.order_id and tracked.order_id in remote_ids:
                    tracked.transition(OrderStatus.NEW, "reconciled: live at venue")
                    findings["resolved_unknown"].append(
                        "%s is live at venue" % tracked.client_order_id)
                else:
                    # Not open at the venue. It either never landed or it is
                    # already done; the fills endpoint is the tiebreaker.
                    findings["resolved_unknown"].append(
                        "%s not open at venue - check fills before resending"
                        % tracked.client_order_id)
                continue

            if tracked.is_live and tracked.order_id and tracked.order_id not in remote_ids:
                findings["missing_at_venue"].append(
                    "%s (%s) is %s locally but not open at venue"
                    % (tracked.client_order_id, tracked.order_id, tracked.status.value))

        tracked_ids = {t.order_id for t in self.orders.values() if t.order_id}
        for oid in remote_ids - tracked_ids:
            findings["orphaned_at_venue"].append(
                "venue order %s (%s) is not tracked locally"
                % (oid, remote[oid].market))

        return findings

    # -- queries -----------------------------------------------------------

    def get(self, client_order_id: str) -> Optional[TrackedOrder]:
        return self.orders.get(client_order_id)

    def by_venue_id(self, order_id: str) -> Optional[TrackedOrder]:
        coid = self._by_venue_id.get(order_id)
        return self.orders.get(coid) if coid else None

    def live_orders(self) -> List[TrackedOrder]:
        return [t for t in self.orders.values() if t.is_live]

    def terminal_orders(self) -> List[TrackedOrder]:
        return [t for t in self.orders.values() if t.is_terminal]

    def unknown_orders(self) -> List[TrackedOrder]:
        return [t for t in self.orders.values() if t.status is OrderStatus.UNKNOWN]

    def blotter(self) -> List[Dict[str, object]]:
        return [t.summary() for t in sorted(
            self.orders.values(), key=lambda t: t.created_ms
        )]

    def stats(self) -> Dict[str, object]:
        total = len(self.orders)
        filled = sum(1 for t in self.orders.values() if t.status is OrderStatus.FILLED)
        rejected = sum(1 for t in self.orders.values()
                       if t.status is OrderStatus.REJECTED)
        by_code: Dict[str, int] = {}
        for t in self.orders.values():
            for r in t.rejections:
                by_code[r.code.value] = by_code.get(r.code.value, 0) + 1
        return {
            "total": total, "filled": filled, "rejected": rejected,
            "live": len(self.live_orders()), "unknown": len(self.unknown_orders()),
            "fill_rate": (filled / total) if total else 0.0,
            "rejections_by_code": dict(sorted(
                by_code.items(), key=lambda kv: kv[1], reverse=True)),
        }
