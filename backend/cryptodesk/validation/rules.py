"""Pre-trade validation rules.

Each rule is a small function over a `ValidationContext` that appends any
rejections it finds to a `ValidationReport`. Keeping them separate - rather
than one large `validate()` - buys three things: a rule can be unit-tested
against a single scenario, the pipeline can be reordered so expensive checks
run last, and a venue that does not enforce some rule can simply drop it from
its ruleset instead of threading a flag through a monolith.

The checks mirror what a signed-order perps DEX enforces on the way in
(Paradex/StarkEx lineage): structural validity, then the market's own
constraints, then anything requiring live prices, then account solvency, then
the signature envelope. A rule never raises - it records. The engine decides
whether to stop.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, List, Optional

from ..core.errors import Code, Rejection, Stage, ValidationReport
from ..core.instruments import Instrument, InstrumentRegistry, MarketState
from ..core.types import (
    ZERO, AccountState, OrderBook, OrderRequest, OrderType, Quote, Side,
    SelfTradeMode, TimeInForce, dec, now_ms,
)


class ValidationContext:
    """Everything a rule may need, assembled once per order.

    Optional fields are genuinely optional: an offline structural check runs
    with no book, no mark and no account, and the rules that need them skip
    rather than fail. That is what makes the same ruleset usable in a unit test
    and in front of a live venue.
    """

    def __init__(
        self,
        request: OrderRequest,
        registry: InstrumentRegistry,
        book: Optional[OrderBook] = None,
        mark: Optional[Quote] = None,
        account: Optional[AccountState] = None,
        now: Optional[int] = None,
        seen_nonces: Optional[set] = None,
        seen_client_ids: Optional[set] = None,
        open_order_count: int = 0,
        max_open_orders: int = 200,
        max_mark_age_ms: int = 5000,
        min_liquidation_distance_bps: Optional[Decimal] = None,
        signature_ttl_ms: int = 30000,
        clock_skew_tolerance_ms: int = 2000,
        require_signature: bool = False,
    ):
        self.request = request
        self.registry = registry
        self.book = book
        self.mark = mark
        self.account = account
        self.now = now if now is not None else now_ms()
        self.seen_nonces = seen_nonces if seen_nonces is not None else set()
        self.seen_client_ids = (
            seen_client_ids if seen_client_ids is not None else set()
        )
        self.open_order_count = open_order_count
        self.max_open_orders = max_open_orders
        self.max_mark_age_ms = max_mark_age_ms
        self.min_liquidation_distance_bps = min_liquidation_distance_bps
        self.signature_ttl_ms = signature_ttl_ms
        self.clock_skew_tolerance_ms = clock_skew_tolerance_ms
        self.require_signature = require_signature

    @property
    def instrument(self) -> Optional[Instrument]:
        return self.registry.get(self.request.market)

    @property
    def mark_price(self) -> Optional[Decimal]:
        """Mark from the explicit quote, else the book mid.

        Preferring an explicit mark matters on perps: the mark is an index-
        anchored price used for margin and liquidation, and it deliberately
        differs from the local mid so a thin book cannot trigger liquidations.
        """
        if self.mark is not None:
            return self.mark.price
        return self.book.mid if self.book is not None else None

    def reference_price(self) -> Optional[Decimal]:
        """Best available price for notional maths: limit price, else mark."""
        if self.request.price is not None:
            return self.request.price
        return self.mark_price


Rule = Callable[[ValidationContext, ValidationReport], None]


def _reject(report: ValidationReport, code: Code, stage: Stage,
            message: str, **detail) -> None:
    report.add(Rejection(code=code, stage=stage, message=message, detail=detail))


# ---------------------------------------------------------------------------
# Stage 1 - schema. No market data, no account. Cheapest checks first.
# ---------------------------------------------------------------------------

def rule_market_exists(ctx: ValidationContext, report: ValidationReport) -> None:
    if ctx.instrument is None:
        _reject(report, Code.MARKET_NOT_FOUND, Stage.SCHEMA,
                "no such market: %s" % ctx.request.market,
                market=ctx.request.market,
                known=ctx.registry.symbols()[:20])


def rule_numbers_finite(ctx: ValidationContext, report: ValidationReport) -> None:
    """Reject NaN/Infinity before any comparison sees them.

    NaN breaks the ordering assumptions every later rule makes: `nan > 0` and
    `nan <= 0` are both False, so a NaN size slips through both a positivity
    check and its negation. It has to die here.
    """
    r = ctx.request
    for name, value in (("size", r.size), ("price", r.price),
                        ("trigger_price", r.trigger_price)):
        if value is None:
            continue
        try:
            if not value.is_finite():
                _reject(report, Code.NON_FINITE_NUMBER, Stage.SCHEMA,
                        "%s is not a finite number: %s" % (name, value),
                        field=name, value=str(value))
        except (AttributeError, InvalidOperation):
            _reject(report, Code.NON_FINITE_NUMBER, Stage.SCHEMA,
                    "%s is not a usable number: %r" % (name, value),
                    field=name, value=repr(value))


def rule_precision_sane(ctx: ValidationContext, report: ValidationReport) -> None:
    """Refuse absurd exponents before they reach arithmetic.

    Decimal has unbounded precision, so `1E+900` is finite, passes a
    positivity check, and only becomes a problem when something multiplies it -
    at which point the cost is paid in memory and time rather than in a clean
    rejection. The bound is deliberately loose: no real price or size lives
    outside 1e-18 to 1e18.
    """
    r = ctx.request
    for name, value in (("size", r.size), ("price", r.price),
                        ("trigger_price", r.trigger_price)):
        if value is None or not value.is_finite():
            continue
        exponent = value.adjusted()  # power of ten of the leading digit
        if exponent > 18 or exponent < -18:
            _reject(report, Code.PRECISION_OVERFLOW, Stage.SCHEMA,
                    "%s has an implausible magnitude (1e%d)" % (name, exponent),
                    field=name, value=str(value), exponent=exponent)


def rule_size_positive(ctx: ValidationContext, report: ValidationReport) -> None:
    size = ctx.request.size
    if size is not None and size.is_finite() and size <= ZERO:
        _reject(report, Code.SIZE_NOT_POSITIVE, Stage.SCHEMA,
                "size must be > 0, got %s" % size, size=str(size))


def rule_price_positive(ctx: ValidationContext, report: ValidationReport) -> None:
    for name, value in (("price", ctx.request.price),
                        ("trigger_price", ctx.request.trigger_price)):
        if value is not None and value.is_finite() and value <= ZERO:
            _reject(report, Code.PRICE_NOT_POSITIVE, Stage.SCHEMA,
                    "%s must be > 0, got %s" % (name, value),
                    field=name, value=str(value))


def rule_price_presence(ctx: ValidationContext, report: ValidationReport) -> None:
    """A limit order without a price, or a market order with one, is a bug in
    the caller - usually a mis-mapped order type. Catching it here stops a
    market order from being silently sent with a stale limit price attached."""
    r = ctx.request
    if r.order_type.needs_price and r.price is None:
        _reject(report, Code.LIMIT_REQUIRES_PRICE, Stage.SCHEMA,
                "%s requires a price" % r.order_type.value,
                order_type=r.order_type.value)
    if not r.order_type.needs_price and r.price is not None:
        _reject(report, Code.MARKET_FORBIDS_PRICE, Stage.SCHEMA,
                "%s must not carry a price (got %s)" % (r.order_type.value, r.price),
                order_type=r.order_type.value, price=str(r.price))
    if r.order_type.needs_trigger and r.trigger_price is None:
        _reject(report, Code.LIMIT_REQUIRES_PRICE, Stage.SCHEMA,
                "%s requires a trigger_price" % r.order_type.value,
                order_type=r.order_type.value)


def rule_tif_compatible(ctx: ValidationContext, report: ValidationReport) -> None:
    """Time-in-force combinations that cannot mean anything.

    POST_ONLY on a market order is the important one: post-only asks to never
    take liquidity, a market order does nothing but take it. Venues differ on
    whether they reject or silently drop the flag; dropping it turns a maker
    order into a taker order, so rejecting is the safe reading.
    """
    r = ctx.request
    if r.post_only and r.order_type in (OrderType.MARKET, OrderType.STOP_MARKET,
                                        OrderType.TAKE_PROFIT_MARKET):
        _reject(report, Code.INVALID_TIME_IN_FORCE, Stage.SCHEMA,
                "post_only is meaningless on %s - it can only take liquidity"
                % r.order_type.value,
                order_type=r.order_type.value)
    if r.post_only and r.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
        _reject(report, Code.INVALID_TIME_IN_FORCE, Stage.SCHEMA,
                "post_only contradicts %s - one rests, the other must not"
                % r.time_in_force.value,
                time_in_force=r.time_in_force.value)
    if r.time_in_force is TimeInForce.GTD and r.expires_at_ms is None:
        _reject(report, Code.INVALID_TIME_IN_FORCE, Stage.SCHEMA,
                "GTD requires expires_at_ms")
    if (r.time_in_force is TimeInForce.GTD and r.expires_at_ms is not None
            and r.expires_at_ms <= ctx.now):
        _reject(report, Code.INVALID_TIME_IN_FORCE, Stage.SCHEMA,
                "GTD expiry is in the past",
                expires_at_ms=r.expires_at_ms, now_ms=ctx.now)


# ---------------------------------------------------------------------------
# Stage 2 - instrument. The market's own published rules.
# ---------------------------------------------------------------------------

def rule_market_tradeable(ctx: ValidationContext, report: ValidationReport) -> None:
    inst = ctx.instrument
    if inst is None:
        return
    if not inst.tradeable:
        _reject(report, Code.MARKET_NOT_TRADEABLE, Stage.INSTRUMENT,
                "market %s is %s" % (inst.symbol, inst.state.value),
                state=inst.state.value)
        return
    if inst.state is MarketState.REDUCE_ONLY and not ctx.request.reduce_only:
        _reject(report, Code.MARKET_REDUCE_ONLY, Stage.INSTRUMENT,
                "market %s accepts reduce-only orders" % inst.symbol,
                state=inst.state.value)
    if inst.state is MarketState.POST_ONLY and not ctx.request.post_only:
        _reject(report, Code.MARKET_POST_ONLY, Stage.INSTRUMENT,
                "market %s accepts post-only orders" % inst.symbol,
                state=inst.state.value)


def rule_order_type_supported(ctx: ValidationContext, report: ValidationReport) -> None:
    inst = ctx.instrument
    if inst is None:
        return
    if ctx.request.order_type.value not in inst.supported_order_types:
        _reject(report, Code.UNSUPPORTED_ORDER_TYPE_FOR_MARKET, Stage.INSTRUMENT,
                "%s does not support %s"
                % (inst.symbol, ctx.request.order_type.value),
                order_type=ctx.request.order_type.value,
                supported=sorted(inst.supported_order_types))


def rule_tick_size(ctx: ValidationContext, report: ValidationReport) -> None:
    inst, r = ctx.instrument, ctx.request
    if inst is None:
        return
    for name, value in (("price", r.price), ("trigger_price", r.trigger_price)):
        if value is None or not value.is_finite():
            continue
        if not inst.is_on_tick(value):
            _reject(report, Code.PRICE_NOT_MULTIPLE_OF_TICK, Stage.INSTRUMENT,
                    "%s %s is not a multiple of tick %s (nearest: %s)"
                    % (name, value, inst.tick_size, inst.round_price(value)),
                    field=name, value=str(value),
                    tick_size=str(inst.tick_size),
                    suggestion=str(inst.round_price(value)))


def rule_step_size(ctx: ValidationContext, report: ValidationReport) -> None:
    inst, r = ctx.instrument, ctx.request
    if inst is None or r.size is None or not r.size.is_finite():
        return
    if not inst.is_on_step(r.size):
        _reject(report, Code.SIZE_NOT_MULTIPLE_OF_STEP, Stage.INSTRUMENT,
                "size %s is not a multiple of step %s (nearest: %s)"
                % (r.size, inst.step_size, inst.round_size(r.size)),
                size=str(r.size), step_size=str(inst.step_size),
                suggestion=str(inst.round_size(r.size)))


def rule_size_bounds(ctx: ValidationContext, report: ValidationReport) -> None:
    inst, r = ctx.instrument, ctx.request
    if inst is None or r.size is None or not r.size.is_finite() or r.size <= ZERO:
        return
    if r.size < inst.min_size:
        _reject(report, Code.BELOW_MIN_SIZE, Stage.INSTRUMENT,
                "size %s below minimum %s" % (r.size, inst.min_size),
                size=str(r.size), min_size=str(inst.min_size))
    if r.size > inst.max_size:
        _reject(report, Code.ABOVE_MAX_SIZE, Stage.INSTRUMENT,
                "size %s above maximum %s" % (r.size, inst.max_size),
                size=str(r.size), max_size=str(inst.max_size))


def rule_notional_bounds(ctx: ValidationContext, report: ValidationReport) -> None:
    """Minimum notional is enforced on value, not size, because a venue's cost
    to carry an order is per-order, not per-coin. It needs a price, so a market
    order can only be checked once a mark is available - hence the skip."""
    inst, r = ctx.instrument, ctx.request
    if inst is None or r.size is None or not r.size.is_finite() or r.size <= ZERO:
        return
    ref = ctx.reference_price()
    if ref is None:
        return
    notional = r.size * ref
    if notional < inst.min_notional:
        _reject(report, Code.BELOW_MIN_NOTIONAL, Stage.INSTRUMENT,
                "notional %s below minimum %s" % (notional, inst.min_notional),
                notional=str(notional), min_notional=str(inst.min_notional),
                reference_price=str(ref))
    if notional > inst.max_notional:
        _reject(report, Code.ABOVE_MAX_NOTIONAL, Stage.INSTRUMENT,
                "notional %s above maximum %s" % (notional, inst.max_notional),
                notional=str(notional), max_notional=str(inst.max_notional))


# ---------------------------------------------------------------------------
# Stage 3 - market. Needs a live book or mark.
# ---------------------------------------------------------------------------

def rule_mark_fresh(ctx: ValidationContext, report: ValidationReport) -> None:
    """A stale mark makes every price-relative check below it meaningless.

    Rejecting on staleness is deliberately conservative: the alternative is
    validating a price band against a mark from before a 5% move, which passes
    exactly the orders you most wanted to stop.
    """
    if ctx.mark is None:
        if ctx.mark_price is None:
            _reject(report, Code.NO_MARK_PRICE, Stage.MARKET,
                    "no mark price or book mid available for %s"
                    % ctx.request.market)
        return
    age = ctx.mark.age_ms(ctx.now)
    if age > ctx.max_mark_age_ms:
        _reject(report, Code.STALE_MARK_PRICE, Stage.MARKET,
                "mark price is %dms old (limit %dms)"
                % (age, ctx.max_mark_age_ms),
                age_ms=age, max_age_ms=ctx.max_mark_age_ms,
                source=ctx.mark.source)


def rule_book_sane(ctx: ValidationContext, report: ValidationReport) -> None:
    book = ctx.book
    if book is None:
        return
    if not book.bids and not book.asks:
        _reject(report, Code.EMPTY_BOOK, Stage.MARKET,
                "order book for %s is empty" % book.market, source=book.source)
        return
    if book.is_crossed:
        _reject(report, Code.CROSSED_BOOK, Stage.MARKET,
                "book is crossed (bid %s >= ask %s) - snapshot is unreliable"
                % (book.best_bid, book.best_ask),
                best_bid=str(book.best_bid), best_ask=str(book.best_ask),
                source=book.source)


def rule_price_band(ctx: ValidationContext, report: ValidationReport) -> None:
    """The fat-finger guard: a limit price too far from mark is refused.

    This is the rule that catches a misplaced decimal point. A buy at 10x mark
    would sweep the entire book before resting; the band stops it at the edge
    instead of after the damage.
    """
    inst, r = ctx.instrument, ctx.request
    if inst is None or r.price is None or not r.price.is_finite():
        return
    mark = ctx.mark_price
    if mark is None or mark <= ZERO:
        return
    low, high = inst.band(mark)
    if r.price < low or r.price > high:
        deviation = (r.price - mark) / mark * Decimal(10000)
        _reject(report, Code.PRICE_BAND_EXCEEDED, Stage.MARKET,
                "price %s is %.1f bps from mark %s (band +/-%s bps)"
                % (r.price, deviation, mark, inst.price_band_bps),
                price=str(r.price), mark=str(mark),
                deviation_bps=str(deviation.quantize(Decimal("0.1"))),
                band_bps=str(inst.price_band_bps),
                band_low=str(low), band_high=str(high))


def rule_post_only_would_cross(ctx: ValidationContext,
                               report: ValidationReport) -> None:
    """Post-only exists to guarantee the maker fee. If the order would cross it
    must be rejected, not filled as a taker - the whole point is that the
    caller would rather not trade than pay the taker side."""
    r, book = ctx.request, ctx.book
    if not r.post_only or r.price is None or book is None:
        return
    if not r.price.is_finite():
        return
    if r.side is Side.BUY and book.best_ask is not None and r.price >= book.best_ask:
        _reject(report, Code.POST_ONLY_WOULD_CROSS, Stage.MARKET,
                "post-only buy at %s crosses best ask %s"
                % (r.price, book.best_ask),
                price=str(r.price), best_ask=str(book.best_ask),
                suggestion=str(book.best_ask - ctx.instrument.tick_size)
                if ctx.instrument else None)
    if r.side is Side.SELL and book.best_bid is not None and r.price <= book.best_bid:
        _reject(report, Code.POST_ONLY_WOULD_CROSS, Stage.MARKET,
                "post-only sell at %s crosses best bid %s"
                % (r.price, book.best_bid),
                price=str(r.price), best_bid=str(book.best_bid),
                suggestion=str(book.best_bid + ctx.instrument.tick_size)
                if ctx.instrument else None)


def rule_market_slippage(ctx: ValidationContext, report: ValidationReport) -> None:
    """Estimate what a market order would actually pay by walking the book.

    Uses the same `sweep` the paper engine fills with, so the pre-trade
    estimate and the simulated execution cannot disagree. An order that cannot
    be filled at all against the visible book is reported as full slippage
    rather than silently passing.
    """
    inst, r, book = ctx.instrument, ctx.request, ctx.book
    if r.order_type not in (OrderType.MARKET, OrderType.STOP_MARKET,
                            OrderType.TAKE_PROFIT_MARKET):
        return
    if book is None:
        return

    # The caller's cap wins where given, but a venue that publishes its own is
    # the floor. Without this fallback a market order with no explicit cap got
    # no slippage check at all, on a venue that had told us its limit.
    cap = r.max_slippage_bps
    cap_source = "order"
    if inst is not None and inst.max_slippage_bps is not None:
        if cap is None:
            cap, cap_source = inst.max_slippage_bps, "venue"
        elif inst.max_slippage_bps < cap:
            cap, cap_source = inst.max_slippage_bps, "venue"
    if cap is None:
        return
    mark = ctx.mark_price
    if mark is None or mark <= ZERO:
        return
    filled, avg_price, complete = book.sweep(r.side, r.size)
    if filled <= ZERO:
        _reject(report, Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, Stage.MARKET,
                "no liquidity on the %s side to fill %s"
                % (r.side.opposite.value, r.size),
                requested=str(r.size), fillable=str(filled))
        return
    # Signed so that "worse than mark" is positive for both sides.
    slip_bps = (avg_price - mark) / mark * Decimal(10000) * r.side.sign
    if slip_bps > cap:
        _reject(report, Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, Stage.MARKET,
                "estimated slippage %.1f bps exceeds the %s cap of %s bps "
                "(avg fill %s vs mark %s)"
                % (slip_bps, cap_source, cap, avg_price, mark),
                slippage_bps=str(slip_bps.quantize(Decimal("0.1"))),
                max_slippage_bps=str(cap), cap_source=cap_source,
                avg_price=str(avg_price), mark=str(mark))
    if not complete:
        report.warn(
            "book shows only %s of %s fillable for %s - remainder depends on "
            "hidden or incoming liquidity" % (filled, r.size, r.market)
        )


# ---------------------------------------------------------------------------
# Stage 4 - account. Needs positions and collateral.
# ---------------------------------------------------------------------------

def rule_account_ready(ctx: ValidationContext, report: ValidationReport) -> None:
    acct = ctx.account
    if acct is None:
        return
    if not acct.onboarded:
        _reject(report, Code.ACCOUNT_NOT_ONBOARDED, Stage.ACCOUNT,
                "account %s has not completed venue onboarding" % acct.account_id,
                account_id=acct.account_id)
    if acct.liquidating and not ctx.request.reduce_only:
        _reject(report, Code.ACCOUNT_LIQUIDATING, Stage.ACCOUNT,
                "account is being liquidated - only reduce-only orders accepted",
                account_id=acct.account_id)


def rule_reduce_only(ctx: ValidationContext, report: ValidationReport) -> None:
    """Reduce-only must genuinely reduce.

    Three distinct failures, because the fix differs for each: no position at
    all (drop the flag or don't send), wrong direction (flip the side), and
    oversized (clamp to the position). Collapsing them into one code would
    force the caller to guess.
    """
    r, acct = ctx.request, ctx.account
    if not r.reduce_only or acct is None:
        return
    pos = acct.position(r.market)
    if pos.is_flat:
        _reject(report, Code.REDUCE_ONLY_NO_POSITION, Stage.ACCOUNT,
                "reduce-only order but no open position in %s" % r.market,
                market=r.market)
        return
    delta = r.signed_size
    if not pos.would_reduce(delta):
        _reject(report, Code.REDUCE_ONLY_WOULD_INCREASE, Stage.ACCOUNT,
                "reduce-only %s would increase a %s position of %s"
                % (r.side.value, "long" if pos.is_long else "short", pos.size),
                position=str(pos.size), side=r.side.value,
                required_side=(Side.SELL if pos.is_long else Side.BUY).value)
        return
    if pos.would_flip(delta):
        _reject(report, Code.REDUCE_ONLY_WOULD_FLIP, Stage.ACCOUNT,
                "reduce-only size %s exceeds position %s and would flip it"
                % (r.size, abs(pos.size)),
                size=str(r.size), position=str(pos.size),
                max_size=str(abs(pos.size)))


def rule_self_trade(ctx: ValidationContext, report: ValidationReport) -> None:
    """Refuse an order that would match the account's own resting order.

    Self-trades are wash trades: they move no risk, pay fees both sides, and
    inflate reported volume. Only CANCEL_TAKER is enforced as a rejection here
    - the CANCEL_MAKER and CANCEL_BOTH modes are venue-side behaviours that
    cancel the resting order instead, which a pre-trade validator cannot do.
    """
    r, acct = ctx.request, ctx.account
    if acct is None or r.self_trade_mode is SelfTradeMode.ALLOW:
        return
    if r.price is None or not r.price.is_finite():
        return
    for oid, resting in acct.open_orders.items():
        if resting.market != r.market or resting.side is r.side:
            continue
        if resting.price is None:
            continue
        crosses = (
            (r.side is Side.BUY and r.price >= resting.price)
            or (r.side is Side.SELL and r.price <= resting.price)
        )
        if crosses:
            _reject(report, Code.SELF_TRADE_PREVENTED, Stage.ACCOUNT,
                    "would match own resting %s at %s (order %s)"
                    % (resting.side.value, resting.price, oid),
                    resting_order_id=oid, resting_price=str(resting.price),
                    mode=r.self_trade_mode.value)
            return


def rule_margin(ctx: ValidationContext, report: ValidationReport) -> None:
    """Initial-margin and leverage check for derivatives.

    Only charges margin for the risk-increasing part of an order: a sell that
    closes a long frees collateral rather than consuming it, and charging for
    it would block exactly the orders that de-risk the account.
    """
    inst, r, acct = ctx.instrument, ctx.request, ctx.account
    if inst is None or inst.kind.value != "PERPETUAL":
        return

    # The leverage cap is a property of the *market*, so it is checked before
    # the account guard below rather than after it. Behind that guard it was
    # unreachable for every venue whose account fetch needs credentials -
    # which is every real one - so an order ticket offering 500x on a 50x
    # market validated clean. A field that is silently not checked is worse
    # than one that is absent, because the screen presents it as checked.
    if r.leverage is not None and r.leverage > inst.max_leverage:
        _reject(report, Code.LEVERAGE_EXCEEDED, Stage.ACCOUNT,
                "requested leverage %s exceeds market maximum %s"
                % (r.leverage, inst.max_leverage),
                leverage=str(r.leverage), max_leverage=str(inst.max_leverage))

    # Everything from here needs the account: what margin an order consumes
    # depends on the position it changes, and a sell that closes a long frees
    # collateral rather than consuming it.
    if acct is None:
        return
    ref = ctx.reference_price()
    if ref is None or ref <= ZERO:
        return

    pos = acct.position(r.market)
    delta = r.signed_size
    new_size = pos.size + delta
    increase = max(ZERO, abs(new_size) - abs(pos.size))
    if increase <= ZERO:
        return

    im_fraction = inst.im_fraction
    if r.leverage is not None and r.leverage > ZERO:
        im_fraction = max(im_fraction, Decimal(1) / r.leverage)
    required = increase * ref * im_fraction

    if required > acct.free_collateral:
        _reject(report, Code.INSUFFICIENT_MARGIN, Stage.ACCOUNT,
                "initial margin %s exceeds free collateral %s"
                % (required.quantize(Decimal("0.01")), acct.free_collateral),
                required_margin=str(required.quantize(Decimal("0.01"))),
                free_collateral=str(acct.free_collateral),
                im_fraction=str(im_fraction))

    if abs(new_size) * ref > inst.max_notional:
        _reject(report, Code.POSITION_LIMIT_EXCEEDED, Stage.ACCOUNT,
                "resulting position notional %s exceeds market cap %s"
                % ((abs(new_size) * ref).quantize(Decimal("0.01")),
                   inst.max_notional),
                resulting_size=str(new_size), max_notional=str(inst.max_notional))


def rule_position_size_limit(ctx: ValidationContext,
                            report: ValidationReport) -> None:
    """Cap on net position *size*, in base units.

    Three different caps exist and they are not interchangeable: `max_size`
    bounds one order, `max_notional` bounds value, and `max_position_size`
    bounds the net position in coins. A venue that allows a single 100-coin
    order while capping the position at 60 coins is not contradicting itself -
    it is saying you may not *accumulate* past 60, and only this rule enforces
    that. It was parsed from venue metadata and read by nothing.
    """
    inst, r, acct = ctx.instrument, ctx.request, ctx.account
    if inst is None or inst.max_position_size is None or acct is None:
        return
    if inst.max_position_size <= ZERO:
        return  # venues publish 0 for "not applicable" (spot)
    projected = abs(acct.position(r.market).size + r.signed_size)
    if projected > inst.max_position_size:
        _reject(report, Code.POSITION_LIMIT_EXCEEDED, Stage.ACCOUNT,
                "resulting position %s exceeds the market cap of %s %s"
                % (projected, inst.max_position_size, inst.base),
                projected_size=str(projected),
                max_position_size=str(inst.max_position_size),
                current_size=str(acct.position(r.market).size))


def rule_maintenance_margin(ctx: ValidationContext, report: ValidationReport) -> None:
    """Refuse to add risk to an account already below its maintenance margin.

    Distinct from the initial-margin check, which asks whether a *new* position
    is affordable. This asks whether the *existing* book is solvent. An account
    under maintenance is one adverse tick from liquidation, and the only orders
    that help it are ones that reduce exposure - so everything else is refused
    even if free collateral technically covers it.
    """
    inst, r, acct = ctx.instrument, ctx.request, ctx.account
    if inst is None or acct is None or inst.kind.value != "PERPETUAL":
        return

    # De-risking is always permitted - judged by *effect*, not by the flag.
    # An unflagged sell that closes a long reduces exposure just as much as a
    # flagged one, and refusing it would trap an underwater account in the
    # position that put it there. A flip is not a reduction: it passes through
    # flat and opens fresh risk on the other side.
    if r.reduce_only:
        return
    pos = acct.position(r.market)
    delta = r.signed_size
    if pos.would_reduce(delta) and not pos.would_flip(delta):
        return

    ratio = acct.margin_ratio(ctx.registry)
    if ratio is None or ratio >= Decimal(1):
        return
    _reject(report, Code.BELOW_MAINTENANCE_MARGIN, Stage.ACCOUNT,
            "account margin ratio %s is below 1.0 - only reduce-only orders "
            "are accepted until the book is healthy"
            % ratio.quantize(Decimal("0.001")),
            margin_ratio=str(ratio.quantize(Decimal("0.001"))),
            equity=str(acct.equity.quantize(Decimal("0.01"))),
            maintenance_margin=str(
                acct.maintenance_margin(ctx.registry).quantize(Decimal("0.01"))))


def rule_liquidation_distance(ctx: ValidationContext,
                              report: ValidationReport) -> None:
    """Refuse an order that would sit too close to its own liquidation price.

    A position opened 30 bps from liquidation is not a position, it is a
    lottery ticket with fees. The initial-margin check passes it - the margin
    is affordable - which is exactly why this is a separate rule: affordability
    and survivability are different questions.

    Only fires when the operator sets a tolerance, because the right distance
    is a risk appetite, not a universal constant.
    """
    inst, r, acct = ctx.instrument, ctx.request, ctx.account
    if (inst is None or acct is None or inst.kind.value != "PERPETUAL"
            or ctx.min_liquidation_distance_bps is None or r.reduce_only):
        return
    mark = ctx.mark_price
    if mark is None or mark <= ZERO:
        return

    pos = acct.position(r.market)
    projected_size = pos.size + r.signed_size
    if projected_size == ZERO:
        return

    # Blend entry prices to get the resulting position's cost basis.
    fill_price = r.price if r.price is not None else mark
    if pos.is_flat or (pos.size > ZERO) == (r.signed_size > ZERO):
        total = abs(pos.size) * pos.entry_price + r.size * fill_price
        entry = total / abs(projected_size) if projected_size != ZERO else fill_price
    else:
        entry = pos.entry_price if abs(r.signed_size) < abs(pos.size) else fill_price

    from ..core.types import Position
    projected = Position(market=r.market, size=projected_size,
                         entry_price=entry, mark_price=mark)
    # Cross margin: the whole pool backs the position, so equity is the right
    # input. Passing free collateral would put a long's liquidation price above
    # its own mark.
    distance = projected.distance_to_liquidation_bps(
        acct.equity, inst.mm_fraction)
    if distance is None:
        return
    if distance < ctx.min_liquidation_distance_bps:
        liq = projected.liquidation_price(acct.equity, inst.mm_fraction)
        _reject(report, Code.LIQUIDATION_TOO_CLOSE, Stage.ACCOUNT,
                "resulting position would liquidate at %s, only %s bps from "
                "mark %s (minimum %s bps)"
                % (liq.quantize(Decimal("0.01")) if liq else "?",
                   distance.quantize(Decimal("0.1")), mark,
                   ctx.min_liquidation_distance_bps),
                liquidation_price=str(liq) if liq else None,
                distance_bps=str(distance.quantize(Decimal("0.1"))),
                minimum_bps=str(ctx.min_liquidation_distance_bps),
                projected_size=str(projected_size))


def rule_spot_balance(ctx: ValidationContext, report: ValidationReport) -> None:
    """Spot has no margin - you must hold what you are spending.

    A buy needs quote currency including the taker fee; a sell needs the base
    asset itself. Charging the fee here rather than at fill time is what stops
    an order that is affordable by a hair from failing at the venue.
    """
    inst, r, acct = ctx.instrument, ctx.request, ctx.account
    if inst is None or acct is None or inst.kind.value != "SPOT":
        return
    ref = ctx.reference_price()
    if ref is None or ref <= ZERO or r.size is None or not r.size.is_finite():
        return
    if r.side is Side.BUY:
        fee_mult = Decimal(1) + inst.taker_fee_bps / Decimal(10000)
        needed = r.size * ref * fee_mult
        have = acct.available(inst.quote)
        if needed > have:
            _reject(report, Code.INSUFFICIENT_BALANCE, Stage.ACCOUNT,
                    "need %s %s (incl. fee) but only %s available"
                    % (needed.quantize(Decimal("0.01")), inst.quote, have),
                    asset=inst.quote,
                    required=str(needed.quantize(Decimal("0.01"))),
                    available=str(have))
    else:
        have = acct.available(inst.base)
        if r.size > have:
            _reject(report, Code.INSUFFICIENT_BALANCE, Stage.ACCOUNT,
                    "need %s %s but only %s available" % (r.size, inst.base, have),
                    asset=inst.base, required=str(r.size), available=str(have))


# ---------------------------------------------------------------------------
# Stage 5 - auth. Signature envelope and replay protection.
# ---------------------------------------------------------------------------

def rule_signature_present(ctx: ValidationContext, report: ValidationReport) -> None:
    if not ctx.require_signature:
        return
    r = ctx.request
    if not r.signature:
        _reject(report, Code.SIGNATURE_MISSING, Stage.AUTH,
                "venue requires a signed order but no signature was attached")
    if r.signature_timestamp_ms is None:
        _reject(report, Code.SIGNATURE_MISSING, Stage.AUTH,
                "signed orders require signature_timestamp_ms")


def rule_signature_freshness(ctx: ValidationContext, report: ValidationReport) -> None:
    """Bound how long a signed order stays replayable.

    A signature with no expiry is a bearer instrument: anyone who captures it
    can resubmit it later, at a price that has since moved. The future-dated
    check is the mirror image - a client whose clock runs fast would otherwise
    mint signatures valid long past their intended window.
    """
    r = ctx.request
    ts = r.signature_timestamp_ms
    if ts is None:
        return
    if ts > ctx.now + ctx.clock_skew_tolerance_ms:
        _reject(report, Code.SIGNATURE_TIMESTAMP_IN_FUTURE, Stage.AUTH,
                "signature timestamp %d is %dms in the future (tolerance %dms)"
                % (ts, ts - ctx.now, ctx.clock_skew_tolerance_ms),
                timestamp_ms=ts, now_ms=ctx.now,
                tolerance_ms=ctx.clock_skew_tolerance_ms)
        return
    expiry = r.signature_expiry_ms or (ts + ctx.signature_ttl_ms)
    if ctx.now > expiry:
        _reject(report, Code.SIGNATURE_EXPIRED, Stage.AUTH,
                "signature expired %dms ago" % (ctx.now - expiry),
                expiry_ms=expiry, now_ms=ctx.now,
                ttl_ms=ctx.signature_ttl_ms)


def rule_nonce_unused(ctx: ValidationContext, report: ValidationReport) -> None:
    """Replay protection proper. Freshness bounds the window; the nonce closes
    it - within the valid window a captured signature is still replayable
    unless its nonce is remembered and refused the second time."""
    n = ctx.request.nonce
    if n is None:
        return
    if n in ctx.seen_nonces:
        _reject(report, Code.NONCE_REUSED, Stage.AUTH,
                "nonce %s has already been used" % n, nonce=n)


def rule_client_id_unique(ctx: ValidationContext, report: ValidationReport) -> None:
    """Idempotency. A resend after a timeout must not become a second order.

    Lives in SCHEMA, not AUTH, because it is not an authentication concern -
    it is a correctness property of the request itself, and it applies to
    venues with no signing surface at all. It was originally grouped with the
    signature rules, which meant every ruleset that legitimately drops
    per-order auth (the local engine, session-authenticated CEXes) silently
    dropped duplicate-order protection with it.
    """
    coid = ctx.request.client_order_id
    if coid and coid in ctx.seen_client_ids:
        _reject(report, Code.DUPLICATE_CLIENT_ORDER_ID, Stage.AUTH,
                "client_order_id %s already submitted" % coid,
                client_order_id=coid)


# ---------------------------------------------------------------------------
# Stage 6 - throttle.
# ---------------------------------------------------------------------------

def rule_max_open_orders(ctx: ValidationContext, report: ValidationReport) -> None:
    """Resting-order cap: the tighter of the operator's and the venue's.

    Venues publish a per-market cap and this rule ignored it, enforcing a flat
    context default instead - so a market advertising 150 was policed at 200
    and the venue did the rejecting.
    """
    limit, source = ctx.max_open_orders, "local"
    inst = ctx.instrument
    if (inst is not None and inst.max_open_orders is not None
            and inst.max_open_orders < limit):
        limit, source = inst.max_open_orders, "venue"
    if ctx.open_order_count >= limit:
        _reject(report, Code.MAX_OPEN_ORDERS, Stage.THROTTLE,
                "%d open orders is at the %s limit of %d"
                % (ctx.open_order_count, source, limit),
                open_orders=ctx.open_order_count, limit=limit,
                limit_source=source)


#: Rules grouped by stage, in execution order. The engine walks this, so
#: reordering a stage here reorders the pipeline.
RULES_BY_STAGE: Dict[Stage, List[Rule]] = {
    Stage.SCHEMA: [
        rule_market_exists,
        rule_numbers_finite,
        rule_precision_sane,
        rule_size_positive,
        rule_price_positive,
        rule_price_presence,
        rule_tif_compatible,
        rule_client_id_unique,
    ],
    Stage.INSTRUMENT: [
        rule_market_tradeable,
        rule_order_type_supported,
        rule_tick_size,
        rule_step_size,
        rule_size_bounds,
        rule_notional_bounds,
    ],
    Stage.MARKET: [
        rule_mark_fresh,
        rule_book_sane,
        rule_price_band,
        rule_post_only_would_cross,
        rule_market_slippage,
    ],
    Stage.ACCOUNT: [
        rule_account_ready,
        rule_reduce_only,
        rule_self_trade,
        rule_position_size_limit,
        rule_maintenance_margin,
        rule_margin,
        rule_liquidation_distance,
        rule_spot_balance,
    ],
    Stage.AUTH: [
        rule_signature_present,
        rule_signature_freshness,
        rule_nonce_unused,
    ],
    Stage.THROTTLE: [
        rule_max_open_orders,
    ],
}

STAGE_ORDER: List[Stage] = [
    Stage.SCHEMA, Stage.INSTRUMENT, Stage.MARKET,
    Stage.ACCOUNT, Stage.AUTH, Stage.THROTTLE,
]
