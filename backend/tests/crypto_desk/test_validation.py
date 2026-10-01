"""The validation scenario matrix.

Every rejection code in the taxonomy gets at least one test that provokes it
and one that confirms the adjacent valid case still passes. The second half is
the part that catches real bugs: a rule that rejects everything passes any
test suite that only checks rejections.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from cryptodesk.core import (
    AccountState, Balance, Code, InstrumentRegistry, MarketState, OrderBook,
    OrderRequest, OrderType, Position, Quote, SelfTradeMode, Side, TimeInForce,
    now_ms,
)
from cryptodesk.validation import for_venue, offline

PERP = "BTC-USD-PERP"
SPOT = "BTC-USD"


def book(mid="60000", spread="2", levels=5, size="1.0", market=PERP) -> OrderBook:
    mid_d = Decimal(mid)
    half = mid_d * Decimal(spread) / Decimal(20000)
    bids = [(mid_d - half * (i + 1), Decimal(size)) for i in range(levels)]
    asks = [(mid_d + half * (i + 1), Decimal(size)) for i in range(levels)]
    return OrderBook.from_levels(market, bids, asks, source="test")


def mark(price="60000", age_ms=0, market=PERP) -> Quote:
    return Quote(market, Decimal(price), now_ms() - age_ms, "test")


def account(collateral="100000", positions=None, balances=None,
            open_orders=None, **kw) -> AccountState:
    return AccountState(
        account_id="test",
        free_collateral=Decimal(collateral),
        total_collateral=Decimal(collateral),
        positions=positions or {},
        balances=balances or {},
        open_orders=open_orders or {},
        **kw
    )


def order(**kw) -> OrderRequest:
    """A valid perp limit order; override any field."""
    params = dict(
        market=PERP, side=Side.BUY, order_type=OrderType.LIMIT,
        size=Decimal("0.01"), price=Decimal("59000.0"),
        nonce=1, signature="0xsig", signature_timestamp_ms=now_ms(),
    )
    params.update(kw)
    return OrderRequest(**params)


class ValidationCase(unittest.TestCase):
    def setUp(self):
        self.reg = InstrumentRegistry()
        self.v = for_venue("paradex", self.reg)

    def check(self, req, **kw):
        kw.setdefault("book", book())
        kw.setdefault("mark", mark())
        kw.setdefault("account", account())
        return self.v.check(req, **kw)

    def assertRejects(self, req, code, **kw):
        report = self.check(req, **kw)
        self.assertFalse(report.ok, "expected rejection %s, got ACCEPTED" % code.value)
        self.assertIn(code, report.codes,
                      "expected %s, got %s" % (code.value,
                                               [c.value for c in report.codes]))
        return report

    def assertAccepts(self, req, **kw):
        report = self.check(req, **kw)
        self.assertTrue(report.ok, "expected ACCEPTED, got %s" % report)
        return report


# ---------------------------------------------------------------------------
class TestSchema(ValidationCase):

    def test_valid_order_accepted(self):
        self.assertAccepts(order())

    def test_unknown_market(self):
        self.assertRejects(order(market="DOGE-USD-PERP"), Code.MARKET_NOT_FOUND)

    def test_zero_size(self):
        self.assertRejects(order(size=Decimal("0")), Code.SIZE_NOT_POSITIVE)

    def test_negative_size(self):
        self.assertRejects(order(size=Decimal("-1")), Code.SIZE_NOT_POSITIVE)

    def test_negative_price(self):
        self.assertRejects(order(price=Decimal("-100")), Code.PRICE_NOT_POSITIVE)

    def test_nan_size_rejected(self):
        """NaN must die before any comparison sees it - `nan > 0` and
        `nan <= 0` are both False, so it slips past a naive positivity check."""
        self.assertRejects(order(size=Decimal("NaN")), Code.NON_FINITE_NUMBER)

    def test_infinite_price_rejected(self):
        self.assertRejects(order(price=Decimal("Infinity")), Code.NON_FINITE_NUMBER)

    def test_limit_without_price(self):
        self.assertRejects(order(price=None), Code.LIMIT_REQUIRES_PRICE)

    def test_market_with_price(self):
        self.assertRejects(
            order(order_type=OrderType.MARKET, price=Decimal("60000")),
            Code.MARKET_FORBIDS_PRICE)

    def test_market_without_price_accepted(self):
        self.assertAccepts(order(order_type=OrderType.MARKET, price=None))

    def test_stop_without_trigger(self):
        self.assertRejects(
            order(order_type=OrderType.STOP_LIMIT, trigger_price=None),
            Code.LIMIT_REQUIRES_PRICE)

    def test_post_only_market_contradiction(self):
        self.assertRejects(
            order(order_type=OrderType.MARKET, price=None, post_only=True),
            Code.INVALID_TIME_IN_FORCE)

    def test_post_only_ioc_contradiction(self):
        self.assertRejects(
            order(post_only=True, time_in_force=TimeInForce.IOC),
            Code.INVALID_TIME_IN_FORCE)

    def test_gtd_without_expiry(self):
        self.assertRejects(order(time_in_force=TimeInForce.GTD),
                           Code.INVALID_TIME_IN_FORCE)

    def test_gtd_expiry_in_past(self):
        self.assertRejects(
            order(time_in_force=TimeInForce.GTD, expires_at_ms=now_ms() - 1000),
            Code.INVALID_TIME_IN_FORCE)

    def test_gtd_valid(self):
        self.assertAccepts(
            order(time_in_force=TimeInForce.GTD, expires_at_ms=now_ms() + 60000))


# ---------------------------------------------------------------------------
class TestInstrument(ValidationCase):

    def test_off_tick_price(self):
        r = self.assertRejects(order(price=Decimal("59000.07")),
                               Code.PRICE_NOT_MULTIPLE_OF_TICK)
        self.assertEqual(r.rejections[0].detail["suggestion"], "59000.1")

    def test_on_tick_price_accepted(self):
        """The float-arithmetic trap: 0.1-multiples that a float modulo
        would report as off-tick."""
        for p in ("59000.1", "59000.2", "59000.3", "59000.7"):
            self.assertAccepts(order(price=Decimal(p)))

    def test_off_step_size(self):
        self.assertRejects(order(size=Decimal("0.0123456")),
                           Code.SIZE_NOT_MULTIPLE_OF_STEP)

    def test_below_min_size(self):
        self.assertRejects(order(size=Decimal("0.0001")), Code.BELOW_MIN_SIZE)

    def test_above_max_size(self):
        self.assertRejects(order(size=Decimal("500")), Code.ABOVE_MAX_SIZE)

    def test_below_min_notional(self):
        self.assertRejects(order(size=Decimal("0.001"), price=Decimal("1000.0")),
                           Code.BELOW_MIN_NOTIONAL)

    def test_market_paused(self):
        self.reg.set_state(PERP, MarketState.PAUSED)
        self.assertRejects(order(), Code.MARKET_NOT_TRADEABLE)

    def test_market_reduce_only_requires_flag(self):
        self.reg.set_state(PERP, MarketState.REDUCE_ONLY)
        self.assertRejects(order(), Code.MARKET_REDUCE_ONLY)

    def test_market_reduce_only_accepts_reduce_only(self):
        self.reg.set_state(PERP, MarketState.REDUCE_ONLY)
        acct = account(positions={PERP: Position(PERP, Decimal("1"),
                                                 Decimal("60000"), Decimal("60000"))})
        self.assertAccepts(order(side=Side.SELL, reduce_only=True,
                                 price=Decimal("61000.0")), account=acct)

    def test_unsupported_order_type(self):
        self.assertRejects(
            order(market="SOL-USD-PERP", order_type=OrderType.STOP_LIMIT,
                  price=Decimal("100.0"), trigger_price=Decimal("99.0"),
                  size=Decimal("1.0")),
            Code.UNSUPPORTED_ORDER_TYPE_FOR_MARKET)


# ---------------------------------------------------------------------------
class TestMarket(ValidationCase):

    def test_fat_finger_high(self):
        self.assertRejects(order(price=Decimal("600000.0")),
                           Code.PRICE_BAND_EXCEEDED)

    def test_fat_finger_low(self):
        self.assertRejects(order(price=Decimal("6000.0")),
                           Code.PRICE_BAND_EXCEEDED)

    def test_price_just_inside_band(self):
        """BTC-USD-PERP band is 500bps; 59000 is ~167bps below 60000."""
        self.assertAccepts(order(price=Decimal("57100.0")))

    def test_stale_mark(self):
        self.assertRejects(order(), Code.STALE_MARK_PRICE, mark=mark(age_ms=10000))

    def test_fresh_mark_accepted(self):
        self.assertAccepts(order(), mark=mark(age_ms=1000))

    def test_crossed_book(self):
        b = OrderBook.from_levels(PERP, [("60010", "1")], [("60000", "1")],
                                  source="test")
        self.assertRejects(order(), Code.CROSSED_BOOK, book=b)

    def test_empty_book(self):
        b = OrderBook.from_levels(PERP, [], [], source="test")
        self.assertRejects(order(), Code.EMPTY_BOOK, book=b)

    def test_post_only_crossing_buy(self):
        self.assertRejects(order(price=Decimal("60100.0"), post_only=True),
                           Code.POST_ONLY_WOULD_CROSS)

    def test_post_only_crossing_sell(self):
        self.assertRejects(
            order(side=Side.SELL, price=Decimal("59900.0"), post_only=True),
            Code.POST_ONLY_WOULD_CROSS)

    def test_post_only_resting_accepted(self):
        self.assertAccepts(order(price=Decimal("59000.0"), post_only=True))

    def test_market_slippage_cap_breached(self):
        """Thin book: a large market order walks far past the mark."""
        thin = book(levels=2, size="0.05")
        self.assertRejects(
            order(order_type=OrderType.MARKET, price=None, size=Decimal("0.1"),
                  max_slippage_bps=Decimal("1")),
            Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, book=thin)

    def test_market_slippage_within_cap(self):
        self.assertAccepts(
            order(order_type=OrderType.MARKET, price=None, size=Decimal("0.01"),
                  max_slippage_bps=Decimal("50")))

    def test_market_order_against_one_sided_book(self):
        """A book with bids but no asks is not *empty* - it is unfillable on
        one side. The slippage rule catches it with the more actionable
        message ("no liquidity on the SELL side"), which is why EMPTY_BOOK
        deliberately requires both sides to be missing."""
        one_sided = OrderBook.from_levels(PERP, [("59990", "1")], [], source="test")
        r = self.assertRejects(
            order(order_type=OrderType.MARKET, price=None,
                  max_slippage_bps=Decimal("10")),
            Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, book=one_sided)
        self.assertIn("no liquidity", r.rejections[0].message)


# ---------------------------------------------------------------------------
class TestAccount(ValidationCase):

    def test_not_onboarded(self):
        self.assertRejects(order(), Code.ACCOUNT_NOT_ONBOARDED,
                           account=account(onboarded=False))

    def test_liquidating_blocks_new_risk(self):
        self.assertRejects(order(), Code.ACCOUNT_LIQUIDATING,
                           account=account(liquidating=True))

    def test_liquidating_allows_reduce_only(self):
        acct = account(liquidating=True,
                       positions={PERP: Position(PERP, Decimal("1"),
                                                 Decimal("60000"), Decimal("60000"))})
        self.assertAccepts(order(side=Side.SELL, reduce_only=True,
                                 price=Decimal("61000.0")), account=acct)

    def test_reduce_only_no_position(self):
        self.assertRejects(order(reduce_only=True), Code.REDUCE_ONLY_NO_POSITION)

    def test_reduce_only_would_increase(self):
        acct = account(positions={PERP: Position(PERP, Decimal("1"),
                                                 Decimal("60000"), Decimal("60000"))})
        self.assertRejects(order(side=Side.BUY, reduce_only=True),
                           Code.REDUCE_ONLY_WOULD_INCREASE, account=acct)

    def test_reduce_only_would_flip(self):
        acct = account(positions={PERP: Position(PERP, Decimal("0.5"),
                                                 Decimal("60000"), Decimal("60000"))})
        self.assertRejects(
            order(side=Side.SELL, size=Decimal("2.0"), reduce_only=True,
                  price=Decimal("61000.0")),
            Code.REDUCE_ONLY_WOULD_FLIP, account=acct)

    def test_reduce_only_exact_close_accepted(self):
        acct = account(positions={PERP: Position(PERP, Decimal("0.5"),
                                                 Decimal("60000"), Decimal("60000"))})
        self.assertAccepts(
            order(side=Side.SELL, size=Decimal("0.5"), reduce_only=True,
                  price=Decimal("61000.0")), account=acct)

    def test_insufficient_margin(self):
        self.assertRejects(order(size=Decimal("50"), price=Decimal("59000.0")),
                           Code.INSUFFICIENT_MARGIN, account=account("100"))

    def test_leverage_exceeded(self):
        self.assertRejects(order(leverage=Decimal("200")), Code.LEVERAGE_EXCEEDED)

    def test_closing_order_needs_no_margin(self):
        """A sell that closes a long frees collateral - charging margin for it
        would block exactly the orders that de-risk the account."""
        acct = account("1", positions={
            PERP: Position(PERP, Decimal("1"), Decimal("60000"), Decimal("60000"))})
        self.assertAccepts(order(side=Side.SELL, size=Decimal("1.0"),
                                 price=Decimal("61000.0")), account=acct)

    def test_self_trade_prevented(self):
        resting = OrderRequest(PERP, Side.SELL, OrderType.LIMIT,
                               Decimal("0.01"), Decimal("59000.0"))
        acct = account(open_orders={"o1": resting})
        self.assertRejects(order(price=Decimal("59000.0")),
                           Code.SELF_TRADE_PREVENTED, account=acct)

    def test_self_trade_allowed_when_mode_allows(self):
        resting = OrderRequest(PERP, Side.SELL, OrderType.LIMIT,
                               Decimal("0.01"), Decimal("59000.0"))
        acct = account(open_orders={"o1": resting})
        self.assertAccepts(
            order(price=Decimal("59000.0"), self_trade_mode=SelfTradeMode.ALLOW),
            account=acct)

    def test_self_trade_same_side_is_fine(self):
        resting = OrderRequest(PERP, Side.BUY, OrderType.LIMIT,
                               Decimal("0.01"), Decimal("59000.0"))
        acct = account(open_orders={"o1": resting})
        self.assertAccepts(order(price=Decimal("59000.0")), account=acct)

    def test_spot_insufficient_quote_balance(self):
        acct = account(balances={"USD": Balance("USD", Decimal("10"), Decimal("10"))})
        self.assertRejects(
            order(market=SPOT, size=Decimal("1.0"), price=Decimal("59000.00")),
            Code.INSUFFICIENT_BALANCE,
            book=book(market=SPOT), mark=mark(market=SPOT), account=acct)

    def test_spot_insufficient_base_balance_on_sell(self):
        acct = account(balances={"BTC": Balance("BTC", Decimal("0.001"),
                                                Decimal("0.001"))})
        self.assertRejects(
            order(market=SPOT, side=Side.SELL, size=Decimal("1.0"),
                  price=Decimal("61000.00")),
            Code.INSUFFICIENT_BALANCE,
            book=book(market=SPOT), mark=mark(market=SPOT), account=acct)

    def test_spot_sufficient_balance_accepted(self):
        acct = account(balances={"USD": Balance("USD", Decimal("100000"),
                                                Decimal("100000"))})
        self.assertAccepts(
            order(market=SPOT, size=Decimal("0.5"), price=Decimal("59000.00")),
            book=book(market=SPOT), mark=mark(market=SPOT), account=acct)


# ---------------------------------------------------------------------------
class TestAuth(ValidationCase):

    def check(self, req, **kw):
        kw.setdefault("require_signature", True)
        return super().check(req, **kw)

    def test_missing_signature(self):
        self.assertRejects(order(signature=None), Code.SIGNATURE_MISSING)

    def test_expired_signature(self):
        self.assertRejects(
            order(signature_timestamp_ms=now_ms() - 120000), Code.SIGNATURE_EXPIRED)

    def test_future_signature(self):
        self.assertRejects(
            order(signature_timestamp_ms=now_ms() + 60000),
            Code.SIGNATURE_TIMESTAMP_IN_FUTURE)

    def test_small_clock_skew_tolerated(self):
        self.assertAccepts(order(signature_timestamp_ms=now_ms() + 1000))

    def test_nonce_replay(self):
        self.assertRejects(order(nonce=42), Code.NONCE_REUSED,
                           seen_nonces={42})

    def test_duplicate_client_order_id(self):
        self.assertRejects(order(client_order_id="abc"),
                           Code.DUPLICATE_CLIENT_ORDER_ID,
                           seen_client_ids={"abc"})

    def test_explicit_expiry_respected(self):
        ts = now_ms() - 5000
        self.assertRejects(
            order(signature_timestamp_ms=ts, signature_expiry_ms=ts + 1000),
            Code.SIGNATURE_EXPIRED)


# ---------------------------------------------------------------------------
class TestPipelineBehaviour(ValidationCase):

    def test_collects_multiple_rejections_in_one_stage(self):
        """A caller fixing an order should see every problem at once, not one
        per round trip."""
        report = self.check(order(price=Decimal("59000.07"),
                                  size=Decimal("0.0123456")))
        self.assertIn(Code.PRICE_NOT_MULTIPLE_OF_TICK, report.codes)
        self.assertIn(Code.SIZE_NOT_MULTIPLE_OF_STEP, report.codes)

    def test_stage_gating_stops_cascade(self):
        """An unknown market must not also produce tick/step/margin noise."""
        report = self.check(order(market="NOPE-PERP"))
        self.assertEqual(report.codes, [Code.MARKET_NOT_FOUND])

    def test_strict_mode_short_circuits(self):
        v = for_venue("paradex", self.reg, strict=True)
        report = v.check(order(price=Decimal("59000.07"), size=Decimal("0.0123456")),
                         book=book(), mark=mark(), account=account())
        self.assertEqual(len(report.rejections), 1)

    def test_offline_validator_needs_no_market_data(self):
        report = offline(self.reg).check(order())
        self.assertTrue(report.ok)

    def test_binance_ruleset_drops_signature_rules(self):
        """CEXes authenticate the session, not each order - the per-order
        signature rules must not fire there."""
        v = for_venue("binance", self.reg)
        report = v.check(order(signature=None, signature_timestamp_ms=None),
                         book=book(), mark=mark(), account=account(),
                         require_signature=True)
        self.assertTrue(report.ok, report)


class TestAutofix(ValidationCase):

    def test_repairs_tick_and_step(self):
        o = order(price=Decimal("59000.07"), size=Decimal("0.0123456"))
        fixed, report, changes = self.v.validate_and_repair(
            o, book=book(), mark=mark(), account=account())
        self.assertTrue(report.ok, report)
        self.assertEqual(fixed.price, Decimal("59000.0"))
        self.assertEqual(fixed.size, Decimal("0.012"))
        self.assertEqual(len(changes), 2)

    def test_repair_rounds_buy_price_down(self):
        """Conservative rounding: snapping must never make an order more
        aggressive than the caller asked for."""
        o = order(price=Decimal("59000.07"))
        fixed, _, _ = self.v.validate_and_repair(
            o, book=book(), mark=mark(), account=account())
        self.assertLess(fixed.price, Decimal("59000.07"))

    def test_repair_rounds_sell_price_up(self):
        o = order(side=Side.SELL, price=Decimal("61000.07"))
        fixed, _, _ = self.v.validate_and_repair(
            o, book=book(), mark=mark(), account=account())
        self.assertGreater(fixed.price, Decimal("61000.07"))

    def test_repairs_post_only_cross(self):
        o = order(price=Decimal("60100.0"), post_only=True)
        fixed, report, changes = self.v.validate_and_repair(
            o, book=book(), mark=mark(), account=account())
        self.assertTrue(report.ok, report)
        self.assertLess(fixed.price, book().best_ask)

    def test_does_not_repair_margin(self):
        """Autofix must never resize an order to fit collateral - that is a
        risk decision, not a rounding correction."""
        o = order(size=Decimal("50"), price=Decimal("59000.0"))
        fixed, report, changes = self.v.validate_and_repair(
            o, book=book(), mark=mark(), account=account("100"))
        self.assertFalse(report.ok)
        self.assertEqual(changes, [])
        self.assertEqual(fixed.size, Decimal("50"))

    def test_repair_is_non_mutating(self):
        o = order(price=Decimal("59000.07"))
        original = o.price
        self.v.validate_and_repair(o, book=book(), mark=mark(), account=account())
        self.assertEqual(o.price, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
