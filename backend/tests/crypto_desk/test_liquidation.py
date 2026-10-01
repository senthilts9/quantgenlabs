"""Maintenance margin and liquidation.

`maintenance_margin_fraction` was parsed from venue metadata and then used by
nothing - the account could go arbitrarily underwater and the system would
happily keep adding risk. These tests cover the mechanics and, more
importantly, the two consistency properties that make them trustworthy:

* maintenance margin must be strictly *below* initial margin, or every
  position is liquidatable the instant it opens;
* the predicted liquidation price must be where liquidation actually happens.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from cryptodesk.core import (
    AccountState, Code, InstrumentRegistry, OrderRequest, OrderType, Position,
    Quote, Side,
)
from cryptodesk.marketdata.feeds import synthetic_book
from cryptodesk.validation import for_venue
from cryptodesk.venues import PaperVenue

M = "BTC-USD-PERP"
D = Decimal


def venue(collateral="5000", mid="60000"):
    reg = InstrumentRegistry()
    v = PaperVenue(reg, starting_collateral=D(collateral))
    v.seed(synthetic_book(M, D(mid), levels=20, depth_per_level=D("2")))
    return reg, v


class TestMarginFractions(unittest.TestCase):

    def test_maintenance_is_below_initial(self):
        """The invariant that makes liquidation meaningful. A fixed 0.03
        default against a 50x market's 0.02 initial requirement liquidated
        every position at the moment of opening."""
        for symbol in InstrumentRegistry().symbols():
            inst = InstrumentRegistry().require(symbol)
            self.assertLess(inst.mm_fraction, inst.im_fraction,
                            "%s: maintenance %s >= initial %s"
                            % (symbol, inst.mm_fraction, inst.im_fraction))

    def test_maintenance_defaults_to_half_of_initial(self):
        inst = InstrumentRegistry().require(M)
        self.assertEqual(inst.mm_fraction, inst.im_fraction / 2)

    def test_explicit_maintenance_above_initial_is_clamped(self):
        """Silently liquidating every new position is a worse failure than
        ignoring a bad parameter."""
        from dataclasses import replace
        inst = replace(InstrumentRegistry().require(M),
                       maintenance_margin_fraction=D("0.9"))
        self.assertLess(inst.mm_fraction, inst.im_fraction)


class TestLiquidationPrice(unittest.TestCase):

    def test_long_liquidates_below_entry(self):
        pos = Position(M, D("1"), D("60000"), D("60000"))
        liq = pos.liquidation_price(D("6000"), D("0.01"))
        self.assertIsNotNone(liq)
        self.assertLess(liq, D("60000"))

    def test_short_liquidates_above_entry(self):
        pos = Position(M, D("-1"), D("60000"), D("60000"))
        liq = pos.liquidation_price(D("6000"), D("0.01"))
        self.assertIsNotNone(liq)
        self.assertGreater(liq, D("60000"))

    def test_long_formula_is_exact(self):
        """At the liquidation price, equity must equal maintenance exactly."""
        pos = Position(M, D("1"), D("60000"), D("60000"))
        collateral, mmf = D("6000"), D("0.01")
        liq = pos.liquidation_price(collateral, mmf)
        equity = collateral + (liq - pos.entry_price) * pos.size
        maintenance = abs(pos.size) * liq * mmf
        self.assertAlmostEqual(float(equity), float(maintenance), places=6)

    def test_short_formula_is_exact(self):
        """Shorts are where a fixed-percentage shortcut breaks: the
        maintenance requirement grows as the price rises against you."""
        pos = Position(M, D("-2"), D("60000"), D("60000"))
        collateral, mmf = D("12000"), D("0.01")
        liq = pos.liquidation_price(collateral, mmf)
        equity = collateral + (liq - pos.entry_price) * pos.size
        maintenance = abs(pos.size) * liq * mmf
        self.assertAlmostEqual(float(equity), float(maintenance), places=6)

    def test_more_collateral_moves_liquidation_further_away(self):
        pos = Position(M, D("1"), D("60000"), D("60000"))
        near = pos.liquidation_price(D("6000"), D("0.01"))
        far = pos.liquidation_price(D("30000"), D("0.01"))
        self.assertLess(far, near)

    def test_flat_position_has_no_liquidation_price(self):
        self.assertIsNone(Position(M).liquidation_price(D("1000"), D("0.01")))

    def test_overcollateralised_position_cannot_be_liquidated(self):
        """Collateral exceeding the whole notional means no adverse move alone
        can liquidate - that is None, not a negative price."""
        pos = Position(M, D("1"), D("60000"), D("60000"))
        self.assertIsNone(pos.liquidation_price(D("200000"), D("0.01")))

    def test_distance_is_always_positive(self):
        for size in (D("1"), D("-1")):
            pos = Position(M, size, D("60000"), D("60000"))
            d = pos.distance_to_liquidation_bps(D("6000"), D("0.01"))
            self.assertGreater(d, 0)


class TestAccountHealth(unittest.TestCase):

    def test_margin_ratio_none_when_flat(self):
        reg = InstrumentRegistry()
        self.assertIsNone(AccountState(total_collateral=D("1000")).margin_ratio(reg))

    def test_healthy_account_is_not_liquidatable(self):
        reg, v = venue()
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("1.0")))
        self.assertFalse(v.fetch_account().is_liquidatable(reg))
        self.assertGreater(v.margin_ratio(), 1)

    def test_equity_includes_unrealised_pnl(self):
        acct = AccountState(total_collateral=D("10000"), positions={
            M: Position(M, D("1"), D("60000"), D("61000"))})
        self.assertEqual(acct.equity, D("11000"))

    def test_cross_margin_sums_across_markets(self):
        reg = InstrumentRegistry()
        acct = AccountState(total_collateral=D("10000"), positions={
            M: Position(M, D("1"), D("60000"), D("60000")),
            "ETH-USD-PERP": Position("ETH-USD-PERP", D("10"), D("2500"), D("2500")),
        })
        btc = reg.require(M).mm_fraction * D("60000")
        eth = reg.require("ETH-USD-PERP").mm_fraction * D("25000")
        self.assertAlmostEqual(float(acct.maintenance_margin(reg)),
                               float(btc + eth), places=6)


class TestLiquidationEvent(unittest.TestCase):

    def test_position_is_closed_when_ratio_falls_below_one(self):
        reg, v = venue()
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("3.0")))
        self.assertFalse(v.fetch_account().position(M).is_flat)
        v.seed(synthetic_book(M, D("58000"), levels=20, depth_per_level=D("2")))
        fills = v.check_liquidation()
        self.assertTrue(fills)
        self.assertTrue(v.fetch_account().position(M).is_flat)
        self.assertTrue(v.liquidations)

    def test_predicted_price_matches_where_it_actually_liquidates(self):
        """The consistency property. A predicted liquidation price that does
        not match the engine's behaviour is worse than none at all - it is a
        number a strategy would size against."""
        reg, v = venue()
        inst = reg.require(M)
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("3.0")))
        acct = v.fetch_account()
        predicted = acct.position(M).liquidation_price(acct.equity, inst.mm_fraction)
        self.assertIsNotNone(predicted)

        # Just above the prediction: still alive.
        v.seed(synthetic_book(M, predicted * D("1.002"), levels=20,
                              depth_per_level=D("2")))
        self.assertEqual(v.check_liquidation(), [])
        self.assertFalse(v.fetch_account().position(M).is_flat)

        # Just below: liquidated.
        v.seed(synthetic_book(M, predicted * D("0.998"), levels=20,
                              depth_per_level=D("2")))
        self.assertTrue(v.check_liquidation())
        self.assertTrue(v.fetch_account().position(M).is_flat)

    def test_liquidation_cancels_that_market_s_resting_orders(self):
        """Resting orders were sized against a position that no longer
        exists; leaving them live re-opens risk the liquidation just removed."""
        reg, v = venue()
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("3.0")))
        # Inside the 500 bps band, or the order is refused before it can rest.
        ack = v.place_order(OrderRequest(M, Side.BUY, OrderType.LIMIT,
                                         D("0.001"), D("59000.0")))
        self.assertTrue(v.fetch_open_orders(), "setup failed: %s" % ack)
        v.seed(synthetic_book(M, D("58000"), levels=20, depth_per_level=D("2")))
        v.check_liquidation()
        self.assertEqual(v.fetch_open_orders(), {})

    def test_solvent_account_survives_a_drawdown(self):
        reg, v = venue(collateral="100000")
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("1.0")))
        v.seed(synthetic_book(M, D("50000"), levels=20, depth_per_level=D("2")))
        self.assertEqual(v.check_liquidation(), [])
        self.assertFalse(v.fetch_account().position(M).is_flat)


class TestValidationRules(unittest.TestCase):

    def _ctx(self, collateral, positions):
        reg = InstrumentRegistry()
        acct = AccountState(total_collateral=D(collateral),
                            free_collateral=D(collateral), positions=positions)
        return reg, acct

    def test_underwater_account_refuses_new_risk(self):
        reg, acct = self._ctx("100", {
            M: Position(M, D("1"), D("60000"), D("60000"))})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.01"), D("59000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, mark=Quote(M, D("60000")), account=acct)
        self.assertIn(Code.BELOW_MAINTENANCE_MARGIN, report.codes)

    def test_underwater_account_still_accepts_a_closing_order(self):
        """Judged by effect, not by the reduce_only flag. Refusing an unflagged
        closing order would trap the account in the position that sank it."""
        reg, acct = self._ctx("100", {
            M: Position(M, D("1"), D("60000"), D("60000"))})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.SELL, OrderType.LIMIT, D("1.0"), D("61000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, mark=Quote(M, D("60000")), account=acct)
        self.assertNotIn(Code.BELOW_MAINTENANCE_MARGIN, report.codes)

    def test_underwater_account_refuses_a_flip(self):
        """A flip passes through flat and opens fresh risk on the other side -
        it is not a reduction."""
        reg, acct = self._ctx("100", {
            M: Position(M, D("1"), D("60000"), D("60000"))})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.SELL, OrderType.LIMIT, D("5.0"), D("61000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, mark=Quote(M, D("60000")), account=acct)
        self.assertIn(Code.BELOW_MAINTENANCE_MARGIN, report.codes)

    def test_liquidation_distance_rule_is_opt_in(self):
        """The right distance is a risk appetite, not a universal constant."""
        reg, acct = self._ctx("2000", {})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("1.0"), D("60000.0"),
                         nonce=1, signature="0x")
        self.assertNotIn(Code.LIQUIDATION_TOO_CLOSE,
                         v.check(o, mark=Quote(M, D("60000")), account=acct).codes)

    def test_liquidation_distance_rejects_a_thin_buffer(self):
        reg, acct = self._ctx("2000", {})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("1.0"), D("60000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, mark=Quote(M, D("60000")), account=acct,
                         min_liquidation_distance_bps=D("1000"))
        self.assertIn(Code.LIQUIDATION_TOO_CLOSE, report.codes)

    def test_liquidation_distance_accepts_a_healthy_buffer(self):
        reg, acct = self._ctx("60000", {})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.1"), D("60000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, mark=Quote(M, D("60000")), account=acct,
                         min_liquidation_distance_bps=D("500"))
        self.assertNotIn(Code.LIQUIDATION_TOO_CLOSE, report.codes)

    def test_affordable_is_not_the_same_as_survivable(self):
        """Initial margin passes, liquidation distance does not - which is why
        they are separate rules."""
        reg, acct = self._ctx("2000", {})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("1.0"), D("60000.0"),
                         nonce=1, signature="0x")
        without = v.check(o, mark=Quote(M, D("60000")), account=acct)
        self.assertNotIn(Code.INSUFFICIENT_MARGIN, without.codes)
        with_rule = v.check(o, mark=Quote(M, D("60000")), account=acct,
                            min_liquidation_distance_bps=D("1000"))
        self.assertIn(Code.LIQUIDATION_TOO_CLOSE, with_rule.codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPreviouslyDeadRules(unittest.TestCase):
    """Rules for constraints that were parsed or defined and never read.

    Same class of defect as the replay guard: a field arrives from the venue,
    gets stored, and nothing ever compares against it. These assert the checks
    fire rather than merely exist.
    """

    def test_position_size_cap_is_enforced(self):
        """Distinct from max_size (per-order) and max_notional (value). A
        venue may allow a 100-coin order while capping the position at 60."""
        from dataclasses import replace
        reg = InstrumentRegistry()
        reg.upsert(replace(reg.require(M), max_position_size=D("2")))
        acct = AccountState(total_collateral=D("10_000_000"),
                            free_collateral=D("10_000_000"),
                            positions={M: Position(M, D("1.5"), D("60000"), D("60000"))})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("1.0"), D("60000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, mark=Quote(M, D("60000")), account=acct)
        self.assertIn(Code.POSITION_LIMIT_EXCEEDED, report.codes)

    def test_position_size_cap_allows_staying_under(self):
        from dataclasses import replace
        reg = InstrumentRegistry()
        reg.upsert(replace(reg.require(M), max_position_size=D("2")))
        acct = AccountState(total_collateral=D("10_000_000"),
                            free_collateral=D("10_000_000"),
                            positions={M: Position(M, D("1.5"), D("60000"), D("60000"))})
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.4"), D("60000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, mark=Quote(M, D("60000")), account=acct)
        self.assertNotIn(Code.POSITION_LIMIT_EXCEEDED, report.codes)

    def test_absurd_exponent_rejected(self):
        """Decimal is unbounded, so 1E+900 is finite and passes a positivity
        check - the cost only appears when something multiplies it."""
        reg = InstrumentRegistry()
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("1E+900"), D("60000.0"),
                         nonce=1, signature="0x")
        self.assertIn(Code.PRECISION_OVERFLOW, v.check(o).codes)

    def test_normal_magnitudes_pass(self):
        reg = InstrumentRegistry()
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.001"), D("60000.0"),
                         nonce=1, signature="0x")
        self.assertNotIn(Code.PRECISION_OVERFLOW, v.check(o).codes)

    def test_daily_loss_is_distinguishable_from_a_manual_halt(self):
        """Both stop trading; the remediation differs, so the code must too."""
        from cryptodesk.risk.limits import RiskEngine, RiskLimits
        engine = RiskEngine(RiskLimits(max_daily_loss=D("1000"),
                                       require_real_market_data=False))
        engine.record_pnl(D("-1500"))
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.01"), D("60000"))
        codes = [r.code for r in engine.check(o)]
        self.assertIn(Code.DAILY_LOSS_LIMIT, codes)
        self.assertNotIn(Code.KILL_SWITCH_ACTIVE, codes)

    def test_manual_halt_still_reports_kill_switch(self):
        from cryptodesk.risk.limits import RiskEngine, RiskLimits
        engine = RiskEngine(RiskLimits.permissive())
        engine.kill_switch.trip("operator halted trading")
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.01"), D("60000"))
        self.assertIn(Code.KILL_SWITCH_ACTIVE, [r.code for r in engine.check(o)])


class TestVenuePublishedCaps(unittest.TestCase):
    """A venue that publishes a cap must have it enforced.

    Both of these were parsed from venue metadata onto the Instrument and then
    read by nothing - the same defect as the replay guard, maintenance margin
    and position-size cap before them.
    """

    def _reg(self, **overrides):
        from dataclasses import replace
        reg = InstrumentRegistry()
        reg.upsert(replace(reg.require(M), **overrides))
        return reg

    def test_venue_slippage_cap_applies_when_order_sets_none(self):
        """A market order with no explicit cap previously got no slippage
        check at all, on a venue that had published its limit."""
        reg = self._reg(max_slippage_bps=D("1"))
        v = for_venue("paradex", reg)
        thin = synthetic_book(M, D("60000"), levels=2, depth_per_level=D("0.05"))
        o = OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.1"),
                         nonce=1, signature="0x")
        report = v.check(o, book=thin, mark=Quote(M, D("60000")),
                         account=AccountState(free_collateral=D("1000000"),
                                              total_collateral=D("1000000")))
        self.assertIn(Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, report.codes)
        detail = [r for r in report.rejections
                  if r.code is Code.MARKET_ORDER_SLIPPAGE_EXCEEDED][0].detail
        self.assertEqual(detail["cap_source"], "venue")

    def test_tighter_of_order_and_venue_caps_wins(self):
        reg = self._reg(max_slippage_bps=D("1"))
        v = for_venue("paradex", reg)
        thin = synthetic_book(M, D("60000"), levels=2, depth_per_level=D("0.05"))
        o = OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.1"),
                         max_slippage_bps=D("9999"), nonce=1, signature="0x")
        report = v.check(o, book=thin, mark=Quote(M, D("60000")),
                         account=AccountState(free_collateral=D("1000000"),
                                              total_collateral=D("1000000")))
        self.assertIn(Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, report.codes)

    def test_no_cap_anywhere_means_no_slippage_rejection(self):
        reg = self._reg(max_slippage_bps=None)
        v = for_venue("paradex", reg)
        thin = synthetic_book(M, D("60000"), levels=2, depth_per_level=D("0.05"))
        o = OrderRequest(M, Side.BUY, OrderType.MARKET, D("0.1"),
                         nonce=1, signature="0x")
        report = v.check(o, book=thin, mark=Quote(M, D("60000")),
                         account=AccountState(free_collateral=D("1000000"),
                                              total_collateral=D("1000000")))
        self.assertNotIn(Code.MARKET_ORDER_SLIPPAGE_EXCEEDED, report.codes)

    def test_venue_open_order_cap_is_tighter_than_local_default(self):
        """A market advertising 150 was being policed at the local 200."""
        reg = self._reg(max_open_orders=3)
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.01"), D("59000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, book=synthetic_book(M, D("60000")),
                         mark=Quote(M, D("60000")),
                         account=AccountState(free_collateral=D("100000"),
                                              total_collateral=D("100000")),
                         open_order_count=3)
        self.assertIn(Code.MAX_OPEN_ORDERS, report.codes)
        detail = [r for r in report.rejections
                  if r.code is Code.MAX_OPEN_ORDERS][0].detail
        self.assertEqual(detail["limit_source"], "venue")

    def test_local_cap_wins_when_tighter_than_the_venue(self):
        reg = self._reg(max_open_orders=500)
        v = for_venue("paradex", reg)
        o = OrderRequest(M, Side.BUY, OrderType.LIMIT, D("0.01"), D("59000.0"),
                         nonce=1, signature="0x")
        report = v.check(o, book=synthetic_book(M, D("60000")),
                         mark=Quote(M, D("60000")),
                         account=AccountState(free_collateral=D("100000"),
                                              total_collateral=D("100000")),
                         open_order_count=5, max_open_orders=5)
        detail = [r for r in report.rejections
                  if r.code is Code.MAX_OPEN_ORDERS][0].detail
        self.assertEqual(detail["limit_source"], "local")
