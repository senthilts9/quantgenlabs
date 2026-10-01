"""Vault share accounting and the trading-PnL link.

The invariants here are the ones real vaults have lost money on: rounding
direction, first-depositor inflation, high-water marks, and pricing a deposit
against a stale NAV.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from cryptodesk.chain.strategy_vault import StaleNav, StrategyVault, VaultError
from cryptodesk.core import (
    ZERO, InstrumentRegistry, OrderRequest, OrderType, Side,
)
from cryptodesk.execution import VaultStrategyRunner
from cryptodesk.marketdata.feeds import synthetic_book
from cryptodesk.risk.limits import RiskEngine, RiskLimits
from cryptodesk.venues import PaperVenue

M = "BTC-USD-PERP"


def vault(**kw) -> StrategyVault:
    kw.setdefault("performance_fee_bps", Decimal("2000"))
    kw.setdefault("management_fee_bps", Decimal("0"))
    return StrategyVault(**kw)


def deployed(initial="100000", holder="alice", **kw):
    """A vault with all capital allocated to the venue.

    `idle` and `collateral` are disjoint, so modelling a gain means allocating
    first and then marking the grown collateral - marking a gain against a
    vault that still holds its capital idle would double-count it.
    """
    v = vault(**kw)
    v.deposit(Decimal(initial), holder)
    v.allocate(Decimal(initial))
    v.mark(collateral=Decimal(initial))
    return v


def runner(mid="60000", **kw):
    reg = InstrumentRegistry()
    venue = PaperVenue(reg, starting_collateral=Decimal("0"))
    venue.seed(synthetic_book(M, mid=Decimal(mid), levels=10,
                              depth_per_level=Decimal("1")))
    v = vault(**kw)
    return v, venue, VaultStrategyRunner(v, venue, RiskEngine(RiskLimits.permissive()))


class TestShareAccounting(unittest.TestCase):

    def test_first_deposit_is_one_to_one(self):
        v = vault()
        shares = v.deposit(Decimal("100000"), "alice")
        self.assertEqual(shares, Decimal("100000.000000"))
        self.assertEqual(v.share_price, Decimal(1))

    def test_empty_vault_has_no_share_price(self):
        self.assertIsNone(vault().share_price)

    def test_second_depositor_pays_current_price(self):
        v = deployed()
        v.mark(collateral=Decimal("110000"))  # +10% on deployed capital
        shares = v.deposit(Decimal("100000"), "bob")
        self.assertLess(shares, Decimal("100000"),
                        "bob must get fewer shares after a gain")

    def test_deposit_does_not_move_share_price(self):
        """A deposit adds assets and shares in proportion; if it moved the
        price, every deposit would dilute or enrich existing holders.

        It may drift down by at most one quantisation unit - that is the
        vault-favouring rounding, and it must never drift up.
        """
        v = deployed()
        v.mark(collateral=Decimal("120000"))
        before = v.share_price
        v.deposit(Decimal("55000"), "bob")
        after = v.share_price
        self.assertLessEqual(after, before, "price must never drift upward")
        self.assertLess(before - after, Decimal("0.000001"))

    def test_redeem_returns_value_net_of_performance_fee(self):
        """+50% on deployed capital, redeeming half the shares.

        Gross, half of a 150k vault is 75,000. The 20% performance fee takes
        10,000 of the 50,000 gain, so NAV is 140,000 and half is 70,000. The
        fee is charged against NAV, not deducted at redemption - which is why
        the payout is 70k rather than 75k less a separate charge.
        """
        v = deployed()
        v.mark(collateral=Decimal("150000"))
        self.assertAlmostEqual(float(v.fees_accrued), 10000.0, delta=1.0)
        v.release(Decimal("150000"))
        v.mark(collateral=ZERO)
        out = v.redeem(Decimal("50000"), "alice")
        self.assertAlmostEqual(float(out), 70000.0, delta=1.0)

    def test_cannot_redeem_more_than_held(self):
        v = vault()
        v.deposit(Decimal("1000"), "alice")
        with self.assertRaises(VaultError):
            v.redeem(Decimal("2000"), "alice")

    def test_cannot_redeem_capital_posted_as_collateral(self):
        """Redeeming against deployed collateral would pay out margin the
        strategy is still using - real vaults queue instead."""
        v = vault()
        v.deposit(Decimal("100000"), "alice")
        v.allocate(Decimal("90000"))
        v.mark(collateral=Decimal("90000"))
        with self.assertRaises(VaultError) as ctx:
            v.redeem(Decimal("50000"), "alice")
        self.assertIn("idle", str(ctx.exception))

    def test_zero_and_negative_deposits_rejected(self):
        v = vault()
        for bad in (Decimal("0"), Decimal("-5")):
            with self.assertRaises(VaultError):
                v.deposit(bad, "alice")


class TestSecurityProperties(unittest.TestCase):

    def test_rounding_favours_the_vault(self):
        """Deposit then immediately redeem must never return more than was put
        in - otherwise a loop extracts value from remaining holders."""
        v = deployed()
        for amount in ("1", "3", "7.77", "1000.001", "12345.6789"):
            shares = v.deposit(Decimal(amount), "bob")
            back = v.preview_redeem(shares)
            self.assertLessEqual(back, Decimal(amount),
                                 "round-trip of %s returned %s" % (amount, back))

    def test_first_depositor_inflation_is_mitigated(self):
        """Classic attack: deposit 1 unit, donate a large amount, and the next
        depositor's shares round to zero. The virtual offset must prevent the
        victim being wiped out."""
        v = vault()
        v.deposit(Decimal("1"), "attacker")
        v.allocate(Decimal("1"))
        # Donation: assets appear at the venue without shares being minted.
        v.mark(collateral=Decimal("100000"))
        victim_shares = v.deposit(Decimal("100000"), "victim")
        self.assertGreater(victim_shares, Decimal(0),
                           "victim's deposit must not round to zero shares")
        value = v.preview_redeem(victim_shares)
        self.assertGreater(value, Decimal("49000"),
                           "victim should retain most of their deposit")

    def test_deposit_minting_zero_shares_is_refused(self):
        v = vault()
        v.deposit(Decimal("1000"), "alice")
        v.allocate(Decimal("1000"))
        v.mark(collateral=Decimal("10") ** 20)
        with self.assertRaises(VaultError):
            v.deposit(Decimal("0.000001"), "dust")

    def test_stale_nav_blocks_transacting(self):
        """Converting at a stale price lets a depositor enter ahead of a known
        move, diluting existing holders."""
        v = deployed("1000", max_nav_age_ms=0)
        v._nav.timestamp_ms -= 10_000
        with self.assertRaises(StaleNav):
            v.deposit(Decimal("1000"), "bob")


class TestFees(unittest.TestCase):

    def test_performance_fee_charged_on_gain(self):
        v = deployed()
        v.mark(collateral=Decimal("110000"))
        # 20% of the 10,000 gain.
        self.assertAlmostEqual(float(v.fees_accrued), 2000.0, delta=1.0)

    def test_no_fee_on_recovery_below_high_water_mark(self):
        """A vault that loses 10% then regains it must not charge a fee - the
        depositor is only back where they started."""
        v = deployed()
        v.mark(collateral=Decimal("120000"))
        after_gain = v.fees_accrued
        hwm = v.high_water_mark
        v.mark(collateral=Decimal("100000"))   # drawdown
        v.mark(collateral=Decimal("115000"))   # partial recovery, below HWM
        self.assertEqual(v.fees_accrued, after_gain)
        self.assertEqual(v.high_water_mark, hwm)

    def test_fee_resumes_above_high_water_mark(self):
        v = deployed()
        v.mark(collateral=Decimal("120000"))
        after_gain = v.fees_accrued
        v.mark(collateral=Decimal("100000"))
        v.mark(collateral=Decimal("150000"))   # new high
        self.assertGreater(v.fees_accrued, after_gain)

    def test_high_water_mark_starts_at_one(self):
        """With a share price that starts at 1.0, a HWM of 1 is the only value
        that lets the first gain be charged at all."""
        self.assertEqual(vault().high_water_mark, Decimal(1))

    def test_zero_performance_fee_charges_nothing(self):
        v = deployed(performance_fee_bps=Decimal("0"))
        v.mark(collateral=Decimal("200000"))
        self.assertEqual(v.fees_accrued, Decimal("0"))


class TestTradingLink(unittest.TestCase):

    def test_trading_pnl_reaches_the_share_price(self):
        v, venue, run = runner()
        run.deposit(Decimal("100000"), "alice")
        run.allocate_to_venue()
        run.trade(OrderRequest(M, Side.BUY, OrderType.MARKET, Decimal("1.0")))
        before = v.share_price
        venue.seed(synthetic_book(M, mid=Decimal("66000"), levels=10,
                                  depth_per_level=Decimal("1")))
        run.mark()
        self.assertGreater(v.share_price, before)

    def test_losses_reach_the_share_price_too(self):
        v, venue, run = runner()
        run.deposit(Decimal("100000"), "alice")
        run.allocate_to_venue()
        run.trade(OrderRequest(M, Side.BUY, OrderType.MARKET, Decimal("1.0")))
        before = v.share_price
        venue.seed(synthetic_book(M, mid=Decimal("54000"), levels=10,
                                  depth_per_level=Decimal("1")))
        run.mark()
        self.assertLess(v.share_price, before)

    def test_investors_share_returns_proportionally(self):
        """The core fairness invariant: everyone in at the same price earns the
        same percentage, regardless of size."""
        v, venue, run = runner()
        run.deposit(Decimal("100000"), "alice")
        run.deposit(Decimal("50000"), "bob")
        run.allocate_to_venue()
        run.trade(OrderRequest(M, Side.BUY, OrderType.MARKET, Decimal("1.0")))
        venue.seed(synthetic_book(M, mid=Decimal("63000"), levels=10,
                                  depth_per_level=Decimal("1")))
        run.mark()
        rows = {r["address"]: r for r in run.investor_table()}
        self.assertAlmostEqual(float(rows["alice"]["roi_pct"]),
                               float(rows["bob"]["roi_pct"]), places=2)

    def test_allocation_does_not_change_share_price(self):
        """Moving assets from idle to collateral is a bucket change, not a
        valuation change."""
        v, venue, run = runner()
        run.deposit(Decimal("100000"), "alice")
        before = v.share_price
        run.allocate_to_venue()
        self.assertAlmostEqual(float(before), float(v.share_price), places=9)

    def test_reserve_is_held_back_from_deployment(self):
        v, venue, run = runner()
        run.deposit(Decimal("100000"), "alice")
        run.allocate_to_venue()
        self.assertGreaterEqual(v.idle, Decimal("19000"),
                                "20% reserve should remain idle")

    def test_redeem_works_against_the_reserve(self):
        v, venue, run = runner()
        run.deposit(Decimal("100000"), "alice")
        run.allocate_to_venue()
        out = run.redeem(Decimal("10000"), "alice")
        self.assertGreater(out, Decimal("9000"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
