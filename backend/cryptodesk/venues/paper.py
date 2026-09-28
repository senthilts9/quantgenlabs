"""Local paper matching engine.

Exists so the entire order path - validation, signing envelope, OMS state
machine, risk limits, reconciliation - can be exercised deterministically with
no network, no credentials and no possibility of touching funds. Every other
venue in this package is subject to a testnet being up, a faucet dispensing,
and a rate limit; this one is not.

It is a *simulator*, and the docstrings say where it is optimistic. What it
models honestly:

* price-time priority against a seeded book, walking multiple levels;
* partial fills, and the IOC/FOK/GTC distinction that follows from them;
* post-only rejection when an order would cross;
* maker versus taker fees, including maker rebates;
* position, collateral and margin updates, including a sell that closes a long
  releasing collateral rather than consuming it;
* resting orders that fill only when flow arrives (`simulate_flow`), so a
  strategy cannot assume its passive order filled the instant it was placed.

* maintenance margin and liquidation: a position whose equity falls below the
  maintenance requirement is force-closed at the mark, and the account keeps
  whatever is left.
* funding settlement on perps, including the consequence most models miss -
  funding drains collateral, which moves the liquidation price closer, so a
  position that survives every price move can still be liquidated by carry.

What it does not model: queue position ahead of you at your own price level
(it assumes you are last, the pessimistic and more useful assumption), latency,
partial cancels racing fills, funding payments, insurance-fund socialisation,
or partial (tiered) liquidation. Anything depending on those needs a real
testnet.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..core.errors import Code, Rejection, Stage
from ..core.instruments import InstrumentRegistry
from ..core.types import (
    ZERO, AccountState, Balance, BookLevel, OrderBook, OrderRequest, OrderStatus,
    OrderType, Position, Quote, Side, TimeInForce, dec, now_ms,
)
from ..validation.engine import OrderValidator, for_venue
from .base import Fill, OrderAck, VenueAdapter, VenueError

_ids = itertools.count(1)


@dataclass
class RestingOrder:
    """An order sitting on the book waiting for flow."""

    order_id: str
    request: OrderRequest
    remaining: Decimal
    filled: Decimal = ZERO
    cost: Decimal = ZERO  # notional accumulated, for average price
    status: OrderStatus = OrderStatus.NEW
    created_ms: int = field(default_factory=now_ms)

    @property
    def avg_price(self) -> Optional[Decimal]:
        return self.cost / self.filled if self.filled > ZERO else None


class PaperVenue(VenueAdapter):
    """Deterministic local venue with a real matching engine."""

    name = "paper"
    supports_trading = True
    has_matching_engine = True
    requires_signature = False
    is_mock = False  # simulated, but the matching is genuine

    def __init__(
        self,
        registry: Optional[InstrumentRegistry] = None,
        starting_collateral: Decimal = Decimal("100000"),
        balances: Optional[Dict[str, Decimal]] = None,
        validator: Optional[OrderValidator] = None,
        seed_books: Optional[Dict[str, OrderBook]] = None,
        store=None,
    ):
        super().__init__(registry=registry, store=store)
        self.validator = validator or for_venue("paper", self.registry)
        self._books: Dict[str, OrderBook] = dict(seed_books or {})
        #: Pristine copies, so consumed depth has a state to recover toward.
        self._reference_books: Dict[str, OrderBook] = {
            m: OrderBook(m, list(b.bids), list(b.asks), b.timestamp_ms, b.source)
            for m, b in (seed_books or {}).items()
        }
        self._resting: Dict[str, RestingOrder] = {}
        self._fills: List[Fill] = []
        self._liquidations: List[str] = []
        self._funding_ledger: List[str] = []
        self._account = AccountState(
            account_id="paper",
            free_collateral=dec(starting_collateral),
            total_collateral=dec(starting_collateral),
            balances={
                asset: Balance(asset, dec(amt), dec(amt))
                for asset, amt in (balances or {"USD": Decimal("100000")}).items()
            },
        )

    # -- book management ---------------------------------------------------

    def seed(self, book: OrderBook) -> None:
        """Install a book snapshot, typically pulled from a real public feed.

        Seeding from live depth is what makes paper results resemble reality:
        a synthetic book with uniform levels understates the slippage of any
        order big enough to matter.

        The snapshot is kept as the *reference* state so `replenish` has
        something to recover toward.
        """
        self._books[book.market] = book
        self._reference_books[book.market] = OrderBook(
            book.market, list(book.bids), list(book.asks),
            book.timestamp_ms, book.source)

    def replenish(self, market: str,
                  fraction: Decimal = Decimal("1")) -> None:
        """Return consumed depth toward the reference book.

        Without this the engine models a book that is eaten once and never
        refills, which makes working an order pointless by construction: six
        slices walk exactly the same levels as one, and TWAP measures
        identically to crossing the spread in a single print. That is not a
        property of the algorithm, it is an artefact of the simulation.

        Real liquidity comes back - makers replace consumed depth within
        seconds on a liquid pair - and harvesting that refresh is the entire
        reason to work an order over time. `fraction` is how much of the gap
        to the reference state is restored: 1 is full recovery between slices,
        0.3 a slower, more conservative book.
        """
        reference = self._reference_books.get(market)
        current = self._books.get(market)
        if reference is None or current is None:
            return
        frac = dec(fraction)

        def recover(ref_levels, cur_levels):
            have = {lv.price: lv.size for lv in cur_levels}
            out = []
            for lv in ref_levels:
                present = have.get(lv.price, ZERO)
                gap = lv.size - present
                size = present + (gap * frac if gap > ZERO else ZERO)
                if size > ZERO:
                    out.append(BookLevel(lv.price, size))
            return out

        current.bids = recover(reference.bids, current.bids)
        current.asks = recover(reference.asks, current.asks)

    def seed_from_feed(self, market: str, feed=None, depth: int = 25) -> OrderBook:
        from ..marketdata.feeds import best_effort_book
        book = best_effort_book(market, feeds=[feed] if feed else None, depth=depth)
        # Rewrite the market symbol: the feed knows "BTC-USD", the paper venue
        # may be trading "BTC-USD-PERP" against that same depth.
        book = OrderBook(market, book.bids, book.asks, book.timestamp_ms, book.source)
        self.seed(book)
        return book

    def fetch_book(self, market: str, depth: int = 25) -> OrderBook:
        book = self._books.get(market)
        if book is None:
            raise VenueError("paper venue has no book for %s - call seed() first" % market)
        return OrderBook(market, book.bids[:depth], book.asks[:depth],
                         book.timestamp_ms, book.source)

    def fetch_quote(self, market: str) -> Quote:
        book = self.fetch_book(market, depth=1)
        mid = book.mid
        if mid is None:
            raise VenueError("paper venue: no mid for %s" % market)
        return Quote(market, mid, now_ms(), "paper")

    # -- funding -----------------------------------------------------------

    def settle_funding(self, market: str, rate) -> Decimal:
        """Pay or receive one funding period on an open position.

        Returns the cash flow (negative is a payment out). Settlement is
        discrete here; real venues accrue continuously between settlements, so
        a position opened and closed inside one period pays nothing in this
        model and a little in reality.

        Liquidation is re-checked immediately afterwards, because this is the
        path that catches people out: funding is charged against collateral, so
        a sustained adverse rate walks the liquidation price toward the mark
        without the mark having moved at all.
        """
        pos = self._account.positions.get(market)
        if pos is None or pos.is_flat:
            return ZERO

        book = self._books.get(market)
        if book and book.mid:
            pos.mark_price = book.mid

        payment = pos.funding_payment(rate)
        if payment == ZERO:
            return ZERO

        pos.funding_paid += payment
        self._account.free_collateral += payment
        self._account.total_collateral += payment
        self._funding_ledger.append(
            "%s: %s %s at rate %s (position %s @ %s)"
            % (market, "received" if payment > ZERO else "paid",
               abs(payment).quantize(Decimal("0.01")), rate.rate,
               pos.size, pos.mark_price)
        )
        self.check_liquidation()
        return payment

    @property
    def funding_ledger(self) -> List[str]:
        return list(self._funding_ledger)

    # -- margin health -----------------------------------------------------

    def margin_ratio(self) -> Optional[Decimal]:
        """Account equity over maintenance requirement. Below 1.0 is liquidatable."""
        return self.fetch_account().margin_ratio(self.registry)

    def check_liquidation(self) -> List[Fill]:
        """Force-close any position the account can no longer support.

        Called after every fill and every re-mark, because those are the two
        events that can move the ratio. Liquidation is modelled as a full
        close at the mark: real venues liquidate in tiers and charge a penalty
        to an insurance fund, so this is optimistic about the *price* received
        while being honest about the *event* happening at all - which is the
        part that matters for a strategy that would otherwise assume a losing
        position can always be held to recovery.
        """
        account = self.fetch_account()
        if not account.is_liquidatable(self.registry):
            return []

        out: List[Fill] = []
        # Close the largest position first: it frees the most requirement per
        # close, so fewer positions get destroyed than closing smallest-first.
        ordered = sorted(
            (p for p in account.positions.values() if not p.is_flat),
            key=lambda p: p.notional, reverse=True,
        )
        for pos in ordered:
            inst = self.registry.get(pos.market)
            if inst is None or pos.mark_price <= ZERO:
                continue
            side = Side.SELL if pos.size > ZERO else Side.BUY
            size = abs(pos.size)
            fee = size * pos.mark_price * inst.taker_fee_bps / Decimal(10000)
            fill = Fill(
                order_id="liquidation-%d" % next(_ids), market=pos.market,
                side=side.value, size=size, price=pos.mark_price, fee=fee,
                liquidity="taker", trade_id="liq-%d" % next(_ids),
            )
            out.append(fill)
            self._fills.append(fill)
            self._apply_fill(fill, inst)
            self._liquidations.append(
                "%s: closed %s %s at %s (margin ratio was %s)"
                % (pos.market, side.value, size, pos.mark_price,
                   (account.margin_ratio(self.registry) or ZERO).quantize(
                       Decimal("0.001")))
            )
            # Cancel that market's resting orders - they were sized against a
            # position that no longer exists.
            for oid, r in list(self._resting.items()):
                if r.request.market == pos.market:
                    self._resting.pop(oid, None)
            if not self.fetch_account().is_liquidatable(self.registry):
                break
        self._account.liquidating = self.fetch_account().is_liquidatable(self.registry)
        return out

    @property
    def liquidations(self) -> List[str]:
        return list(self._liquidations)

    def fetch_account(self) -> AccountState:
        # Refresh marks so unrealised PnL and margin reflect the current book.
        for market, pos in self._account.positions.items():
            book = self._books.get(market)
            if book and book.mid:
                pos.mark_price = book.mid
        self._account.as_of_ms = now_ms()
        self._account.open_orders = {
            oid: r.request for oid, r in self._resting.items()
        }
        return self._account

    def fetch_open_orders(self) -> Dict[str, OrderRequest]:
        return {oid: r.request for oid, r in self._resting.items()}

    def fetch_fills(self, market: Optional[str] = None) -> List[Fill]:
        return [f for f in self._fills if market is None or f.market == market]

    # -- order entry -------------------------------------------------------

    def place_order(self, request: OrderRequest) -> OrderAck:
        """Validate, then match against the seeded book."""
        book = self._books.get(request.market)
        mark = None
        if book and book.mid:
            mark = Quote(request.market, book.mid, book.timestamp_ms, "paper")

        report = self.validator.check(
            request, book=book, mark=mark, account=self.fetch_account(),
            open_order_count=len(self._resting),
            **self.replay_kwargs()
        )
        if not report.ok:
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                rejections=list(report.rejections),
                report=report, venue=self.name,
            )

        self.record_submission(request)
        order_id = "paper-%d" % next(_ids)
        inst = self.registry.require(request.market)

        # Match against the visible book.
        fills, remaining = self._match(order_id, request, book)

        # Time-in-force decides what happens to whatever did not fill.
        if remaining > ZERO:
            if request.time_in_force is TimeInForce.FOK:
                # All-or-nothing: unwind the partial fills entirely.
                for f in fills:
                    self._unapply_fill(f, inst)
                self._fills = [f for f in self._fills if f.order_id != order_id]
                return OrderAck(
                    client_order_id=request.client_order_id, order_id=order_id,
                    status=OrderStatus.CANCELLED, venue=self.name,
                    rejections=[Rejection(
                        Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, Stage.MARKET,
                        "FOK could not be filled in full (%s of %s available)"
                        % (request.size - remaining, request.size),
                        {"requested": str(request.size),
                         "available": str(request.size - remaining)},
                    )],
                )
            if request.time_in_force is TimeInForce.IOC:
                status = (OrderStatus.PARTIALLY_FILLED if fills
                          else OrderStatus.CANCELLED)
            elif request.order_type is OrderType.MARKET:
                # A market order never rests - unfilled remainder is cancelled.
                status = OrderStatus.PARTIALLY_FILLED if fills else OrderStatus.CANCELLED
            else:
                # GTC/GTD limit: the remainder rests and waits for flow.
                self._resting[order_id] = RestingOrder(
                    order_id=order_id, request=request, remaining=remaining,
                    filled=request.size - remaining,
                    cost=sum((f.size * f.price for f in fills), ZERO),
                    status=(OrderStatus.PARTIALLY_FILLED if fills else OrderStatus.NEW),
                )
                status = self._resting[order_id].status
        else:
            status = OrderStatus.FILLED

        # A fill can push the account under maintenance - check before acking,
        # so the caller never sees a position the venue has already closed.
        self.check_liquidation()

        filled_size = sum((f.size for f in fills), ZERO)
        avg = (sum((f.size * f.price for f in fills), ZERO) / filled_size
               if filled_size > ZERO else None)

        return OrderAck(
            client_order_id=request.client_order_id, order_id=order_id,
            status=status, filled_size=filled_size, avg_price=avg,
            fills=fills, venue=self.name, report=report,
        )

    def cancel_order(self, order_id: str, market: Optional[str] = None) -> OrderAck:
        resting = self._resting.pop(order_id, None)
        if resting is None:
            # Already filled or never existed. CANCELLED-on-missing would let a
            # caller believe it pulled an order that actually traded.
            return OrderAck(
                client_order_id="", order_id=order_id,
                status=OrderStatus.UNKNOWN, venue=self.name,
                raw={"error": "no resting order %s" % order_id},
            )
        resting.status = OrderStatus.CANCELLED
        return OrderAck(
            client_order_id=resting.request.client_order_id, order_id=order_id,
            status=OrderStatus.CANCELLED, filled_size=resting.filled,
            avg_price=resting.avg_price, venue=self.name,
        )

    def cancel_all(self, market: Optional[str] = None) -> List[OrderAck]:
        return [
            self.cancel_order(oid)
            for oid, r in list(self._resting.items())
            if market is None or r.request.market == market
        ]

    # -- matching ----------------------------------------------------------

    def _match(self, order_id: str, request: OrderRequest,
               book: Optional[OrderBook]) -> Tuple[List[Fill], Decimal]:
        """Walk the book taking liquidity. Returns (fills, unfilled remainder).

        Consumes the levels it takes, so a second order in the same session
        faces the depleted book the first one left behind - without that, ten
        orders each "fill" against the same top level and the simulation
        flatters every strategy that sends size.
        """
        inst = self.registry.require(request.market)
        remaining = request.size
        fills: List[Fill] = []

        if book is None:
            return fills, remaining

        if request.post_only:
            # Validation already rejected a crossing post-only order; anything
            # reaching here rests without taking.
            return fills, remaining

        levels = book.asks if request.is_buy else book.bids
        limit = request.price
        consumed: List[BookLevel] = []

        for level in levels:
            if remaining <= ZERO:
                consumed.append(level)
                continue
            # A limit order stops at its price; a market order does not.
            if limit is not None:
                if request.is_buy and level.price > limit:
                    consumed.append(level)
                    continue
                if not request.is_buy and level.price < limit:
                    consumed.append(level)
                    continue

            take = min(remaining, level.size)
            fee = take * level.price * inst.taker_fee_bps / Decimal(10000)
            fill = Fill(
                order_id=order_id, market=request.market, side=request.side.value,
                size=take, price=level.price, fee=fee, liquidity="taker",
                trade_id="t-%d" % next(_ids),
            )
            fills.append(fill)
            self._fills.append(fill)
            self._apply_fill(fill, inst)
            remaining -= take
            leftover = level.size - take
            if leftover > ZERO:
                consumed.append(BookLevel(level.price, leftover))

        # Write the depleted side back.
        if request.is_buy:
            book.asks = consumed
        else:
            book.bids = consumed

        return fills, remaining

    def simulate_flow(self, market: str, side: Side, size: Decimal,
                      price: Optional[Decimal] = None) -> List[Fill]:
        """Push incoming flow that can fill *resting* orders.

        Without this, a passive order never fills and post-only strategies look
        broken. `side` is the incoming aggressor's side, so a BUY here fills
        resting SELL orders.
        """
        size = dec(size)
        out: List[Fill] = []
        # Price-time priority: best price first, then oldest.
        candidates = sorted(
            (r for r in self._resting.values()
             if r.request.market == market and r.request.side is side.opposite
             and r.request.price is not None),
            key=lambda r: (
                -r.request.price if side is Side.BUY else r.request.price,
                r.created_ms,
            ),
        )
        inst = self.registry.require(market)
        for resting in candidates:
            if size <= ZERO:
                break
            rp = resting.request.price
            if price is not None:
                if side is Side.BUY and rp > price:
                    continue
                if side is Side.SELL and rp < price:
                    continue
            take = min(size, resting.remaining)
            # Resting orders are makers - fee can be a rebate (negative bps).
            fee = take * rp * inst.maker_fee_bps / Decimal(10000)
            fill = Fill(
                order_id=resting.order_id, market=market,
                side=resting.request.side.value, size=take, price=rp,
                fee=fee, liquidity="maker", trade_id="t-%d" % next(_ids),
            )
            out.append(fill)
            self._fills.append(fill)
            self._apply_fill(fill, inst)
            resting.remaining -= take
            resting.filled += take
            resting.cost += take * rp
            size -= take
            if resting.remaining <= ZERO:
                resting.status = OrderStatus.FILLED
                self._resting.pop(resting.order_id, None)
            else:
                resting.status = OrderStatus.PARTIALLY_FILLED
        return out

    # -- book-keeping ------------------------------------------------------

    def _apply_fill(self, fill: Fill, inst) -> None:
        """Update position, collateral and balances for one execution."""
        side = Side(fill.side)
        delta = fill.size * side.sign
        pos = self._account.positions.get(fill.market)
        if pos is None:
            pos = Position(market=fill.market, mark_price=fill.price)
            self._account.positions[fill.market] = pos

        old_size = pos.size
        new_size = old_size + delta

        if old_size == ZERO or (old_size > ZERO) == (delta > ZERO):
            # Opening or adding: weighted-average the entry price.
            total_cost = abs(old_size) * pos.entry_price + fill.size * fill.price
            pos.entry_price = (total_cost / abs(new_size) if new_size != ZERO
                               else ZERO)
        elif abs(delta) > abs(old_size):
            # Flipped through flat: the new entry is this fill's price.
            pos.entry_price = fill.price
        # Reducing without flipping leaves entry_price untouched, which is what
        # makes realised PnL computable against it.

        pos.size = new_size
        pos.mark_price = fill.price
        if new_size == ZERO:
            pos.entry_price = ZERO

        # Collateral: fees always cost; margin is released or consumed by the
        # change in position notional.
        self._account.free_collateral -= fill.fee
        self._account.total_collateral -= fill.fee
        margin_delta = (abs(new_size) - abs(old_size)) * fill.price * inst.im_fraction
        self._account.free_collateral -= margin_delta

        # Spot also moves the underlying balances.
        if inst.kind.value == "SPOT":
            base = self._account.balances.setdefault(inst.base, Balance(inst.base))
            quote = self._account.balances.setdefault(inst.quote, Balance(inst.quote))
            notional = fill.size * fill.price
            if side is Side.BUY:
                base.total += fill.size
                base.available += fill.size
                quote.total -= notional + fill.fee
                quote.available -= notional + fill.fee
            else:
                base.total -= fill.size
                base.available -= fill.size
                quote.total += notional - fill.fee
                quote.available += notional - fill.fee

    def _unapply_fill(self, fill: Fill, inst) -> None:
        """Reverse a fill - used only to unwind a FOK that could not complete."""
        inverse = Fill(
            order_id=fill.order_id, market=fill.market,
            side=Side(fill.side).opposite.value, size=fill.size, price=fill.price,
            fee=-fill.fee, liquidity=fill.liquidity,
        )
        self._apply_fill(inverse, inst)

    # -- helpers -----------------------------------------------------------

    def reset(self, starting_collateral: Optional[Decimal] = None) -> None:
        """Clear positions, orders and fills - between test scenarios."""
        collateral = (dec(starting_collateral) if starting_collateral is not None
                      else self._account.total_collateral)
        self._resting.clear()
        self._fills.clear()
        self._account.positions.clear()
        self._account.open_orders.clear()
        self._liquidations.clear()
        self._funding_ledger.clear()
        self._account.liquidating = False
        self._account.free_collateral = collateral
        self._account.total_collateral = collateral

    def pnl_summary(self) -> Dict[str, object]:
        realised_fees = sum((f.fee for f in self._fills), ZERO)
        unrealised = sum(
            (p.unrealised_pnl for p in self._account.positions.values()), ZERO
        )
        return {
            "fills": len(self._fills),
            "liquidations": list(self._liquidations),
            "funding_paid": str(sum(
                (p.funding_paid for p in self._account.positions.values()), ZERO)),
            "funding_events": len(self._funding_ledger),
            "margin_ratio": str(self.margin_ratio()) if self.margin_ratio() else None,
            "fees_paid": str(realised_fees),
            "unrealised_pnl": str(unrealised),
            "free_collateral": str(self._account.free_collateral),
            "positions": {
                m: {"size": str(p.size), "entry": str(p.entry_price),
                    "mark": str(p.mark_price), "upnl": str(p.unrealised_pnl)}
                for m, p in self._account.positions.items() if not p.is_flat
            },
        }
