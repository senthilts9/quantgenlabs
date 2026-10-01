"""Perpetual funding: the cost of carry, and what it does to liquidation.

`funding_rate()` existed on one adapter and was consumed by nothing, so a perp
held overnight had PnL the system could not see and the vault's share price
could not reflect.

Two properties carry the weight:

* **Sign.** A positive rate means longs pay shorts. Getting this backwards
  produces a strategy that reads as profitable exactly when it is bleeding.
* **Funding erodes collateral, so it moves the liquidation price toward the
  mark.** A position that survives every price move can still be liquidated by
  carry alone - the failure mode a mark-to-market-only model cannot express.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from cryptodesk.core import InstrumentRegistry, OrderRequest, OrderType, Position, Side
from cryptodesk.core.types import FundingRate
from cryptodesk.marketdata.feeds import synthetic_book
from cryptodesk.venues import PaperVenue

M = "BTC-USD-PERP"
D = Decimal


def rate(value="0.0001", hours=8) -> FundingRate:
    return FundingRate(M, D(value), period_hours=hours)


def long_position(size="1", entry="60000", mark="60000") -> Position:
    return Position(M, D(size), D(entry), D(mark))


def venue(collateral="2600", mid="60000"):
    reg = InstrumentRegistry()
    v = PaperVenue(reg, starting_collateral=D(collateral))
    v.seed(synthetic_book(M, D(mid), levels=20, depth_per_level=D("3")))
    return reg, v


class TestSignConvention(unittest.TestCase):
    """The bug that makes a losing carry trade look like a winning one."""

    def test_long_pays_when_rate_is_positive(self):
        self.assertLess(long_position().funding_payment(rate()), 0)

    def test_short_receives_when_rate_is_positive(self):
        self.assertGreater(long_position(size="-1").funding_payment(rate()), 0)

    def test_long_receives_when_rate_is_negative(self):
        self.assertGreater(
            long_position().funding_payment(rate("-0.0001")), 0)

    def test_short_pays_when_rate_is_negative(self):
        self.assertLess(
            long_position(size="-1").funding_payment(rate("-0.0001")), 0)

    def test_payment_is_symmetric(self):
        """What one side pays, the other receives - funding moves value
        between traders, it is not a fee to the venue."""
        long_pay = long_position(size="2").funding_payment(rate())
        short_pay = long_position(size="-2").funding_payment(rate())
        self.assertEqual(long_pay, -short_pay)

    def test_magnitude_is_notional_times_rate(self):
        p = long_position(size="2", mark="60000")
        self.assertEqual(abs(p.funding_payment(rate("0.001"))), D("120.000"))

    def test_flat_position_pays_nothing(self):
        self.assertEqual(Position(M).funding_payment(rate()), 0)


class TestNotionalBasis(unittest.TestCase):

    def test_charged_on_mark_not_entry(self):
        """A long that has doubled pays funding on the doubled notional. That
        is what makes funding compound against a winning position."""
        at_entry = long_position(entry="60000", mark="60000")
        doubled = long_position(entry="60000", mark="120000")
        self.assertEqual(abs(doubled.funding_payment(rate())),
                         abs(at_entry.funding_payment(rate())) * 2)

    def test_explicit_mark_overrides_position_mark(self):
        p = long_position(mark="60000")
        self.assertEqual(abs(p.funding_payment(rate(), mark=D("30000"))),
                         abs(p.funding_payment(rate())) / 2)


class TestAnnualisation(unittest.TestCase):

    def test_eight_hourly_apr(self):
        self.assertAlmostEqual(float(rate("0.0001", 8).apr), 0.1095, places=4)

    def test_hourly_rate_is_eight_times_the_apr(self):
        """The same numeric rate means very different money depending on the
        period, which is why the period travels with the rate rather than
        being assumed by whatever consumes it."""
        self.assertAlmostEqual(
            float(rate("0.0001", 1).apr),
            float(rate("0.0001", 8).apr) * 8, places=6)

    def test_zero_period_does_not_divide_by_zero(self):
        self.assertEqual(rate("0.0001", 0).apr, 0)


class TestSettlement(unittest.TestCase):

    def test_settlement_moves_collateral(self):
        reg, v = venue()
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("1.0")))
        before = v.fetch_account().free_collateral
        paid = v.settle_funding(M, rate("0.001"))
        self.assertLess(paid, 0)
        self.assertEqual(v.fetch_account().free_collateral, before + paid)

    def test_short_is_credited(self):
        reg, v = venue(collateral="20000")
        v.place_order(OrderRequest(M, Side.SELL, OrderType.MARKET, D("1.0")))
        before = v.fetch_account().free_collateral
        self.assertGreater(v.settle_funding(M, rate("0.001")), 0)
        self.assertGreater(v.fetch_account().free_collateral, before)

    def test_flat_account_settles_nothing(self):
        reg, v = venue()
        self.assertEqual(v.settle_funding(M, rate("0.001")), 0)

    def test_funding_is_realised_not_unrealised(self):
        """It has left collateral and a favourable price move cannot return
        it, so it is tracked apart from unrealised PnL."""
        reg, v = venue()
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("1.0")))
        v.settle_funding(M, rate("0.001"))
        self.assertLess(v.fetch_account().position(M).funding_paid, 0)

    def test_ledger_records_each_settlement(self):
        reg, v = venue()
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("1.0")))
        for _ in range(3):
            v.settle_funding(M, rate("0.0001"))
        self.assertEqual(len(v.funding_ledger), 3)


class TestFundingAndLiquidation(unittest.TestCase):
    """The connection a mark-to-market-only model cannot express."""

    def test_funding_moves_liquidation_toward_the_mark(self):
        reg, v = venue()
        inst = reg.require(M)
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("2.0")))
        a = v.fetch_account()
        before = a.position(M).liquidation_price(a.equity, inst.mm_fraction)

        v.settle_funding(M, rate("0.001"))
        a = v.fetch_account()
        after = a.position(M).liquidation_price(a.equity, inst.mm_fraction)
        self.assertGreater(after, before,
                           "a long's liquidation price must rise toward the mark")

    def test_carry_alone_can_liquidate_a_position(self):
        """The mark never moves. The position dies anyway."""
        reg, v = venue()
        v.place_order(OrderRequest(M, Side.BUY, OrderType.MARKET, D("2.0")))
        self.assertFalse(v.fetch_account().position(M).is_flat)

        for _ in range(20):
            v.settle_funding(M, rate("0.001"))
            if v.fetch_account().position(M).is_flat:
                break

        self.assertTrue(v.fetch_account().position(M).is_flat,
                        "sustained adverse funding should have liquidated it")
        self.assertTrue(v.liquidations)
        # The book was never re-seeded, so the mark is unchanged throughout.
        self.assertEqual(v.fetch_book(M).mid, synthetic_book(
            M, D("60000"), levels=20, depth_per_level=D("3")).mid)

    def test_favourable_funding_does_not_liquidate(self):
        reg, v = venue(collateral="20000")
        v.place_order(OrderRequest(M, Side.SELL, OrderType.MARKET, D("1.0")))
        for _ in range(20):
            v.settle_funding(M, rate("0.001"))
        self.assertFalse(v.fetch_account().position(M).is_flat)


class TestVaultSeesFunding(unittest.TestCase):

    def test_funding_reaches_the_share_price(self):
        """Funding leaves collateral, and the vault marks from collateral, so
        the share price must fall even with the mark unchanged."""
        from cryptodesk.chain import StrategyVault
        from cryptodesk.execution import VaultStrategyRunner
        from cryptodesk.risk.limits import RiskEngine, RiskLimits

        reg = InstrumentRegistry()
        v = PaperVenue(reg, starting_collateral=D("0"))
        v.seed(synthetic_book(M, D("60000"), levels=20, depth_per_level=D("3")))
        vault = StrategyVault(management_fee_bps=D("0"))
        run = VaultStrategyRunner(vault, v, RiskEngine(RiskLimits.permissive()))

        run.deposit(D("100000"), "alice")
        run.allocate_to_venue()
        run.trade(OrderRequest(M, Side.BUY, OrderType.MARKET, D("1.0")))
        before = vault.share_price

        v.settle_funding(M, rate("0.001"))
        run.mark()
        self.assertLess(vault.share_price, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
