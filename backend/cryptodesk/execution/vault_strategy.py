"""Wiring a `StrategyVault` to a trading venue.

The missing link: depositors put capital into a vault, the vault allocates it
to a venue as collateral, a strategy trades it, and the resulting PnL shows up
in the share price. Redeemers get out at whatever that price has become.

The ordering rules here are the whole point, and getting them wrong is how real
vaults mis-price:

* **Mark before every deposit or redemption.** A deposit converted at a stale
  NAV either dilutes existing holders or hands the depositor a discount. The
  vault refuses a stale NAV; this runner is what keeps it fresh.
* **Mark after every fill.** A fill changes collateral and position, so the
  share price it produced is only correct once the venue has been re-read.
* **Never allocate more than idle.** Collateral posted at a venue cannot be
  redeemed against without closing positions, so the vault keeps a buffer.

`VaultStrategyRunner` is deliberately thin. It does not decide *what* to trade -
that is a strategy's job - it decides *when the books are correct*.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from ..chain.strategy_vault import StrategyVault, VaultError
from ..core.types import ZERO, OrderRequest, dec
from ..risk.limits import RiskEngine
from ..venues.base import VenueAdapter
from .oms import OrderManager, TrackedOrder


class VaultStrategyRunner:
    """Runs a strategy against a venue with vault share accounting on top."""

    def __init__(
        self,
        vault: StrategyVault,
        venue: VenueAdapter,
        risk: Optional[RiskEngine] = None,
        #: Fraction of NAV kept idle so small redemptions do not require
        #: unwinding positions. 0.2 is a starting point, not a law.
        reserve_fraction: Decimal = Decimal("0.2"),
    ):
        self.vault = vault
        self.venue = venue
        self.oms = OrderManager(venue, risk)
        self.reserve_fraction = dec(reserve_fraction)
        self._allocated: Decimal = ZERO

    # -- investor flows ----------------------------------------------------

    def deposit(self, assets: Decimal, investor: str) -> Decimal:
        """Mark, then deposit. Returns shares minted."""
        self.mark()
        return self.vault.deposit(assets, investor)

    def redeem(self, shares: Decimal, investor: str) -> Decimal:
        """Mark, then redeem. Returns assets paid out."""
        self.mark()
        return self.vault.redeem(shares, investor)

    # -- capital deployment ------------------------------------------------

    def deployable(self) -> Decimal:
        """Idle assets above the reserve buffer."""
        reserve = self.vault.total_assets * self.reserve_fraction
        return max(ZERO, self.vault.idle - reserve)

    def allocate_to_venue(self, amount: Optional[Decimal] = None) -> Decimal:
        """Post idle assets to the venue as trading collateral.

        Capped at `deployable()` so the reserve survives - a vault that deploys
        everything cannot honour any redemption without closing positions,
        which turns an ordinary withdrawal into a forced unwind.
        """
        target = dec(amount) if amount is not None else self.deployable()
        target = min(target, self.deployable())
        if target <= ZERO:
            return ZERO
        self.vault.allocate(target)
        self._allocated += target
        # The paper venue holds collateral directly; a real venue would need a
        # deposit transaction here.
        account = self.venue.fetch_account()
        account.free_collateral += target
        account.total_collateral += target
        self.mark()
        return target

    def release_from_venue(self, amount: Decimal) -> Decimal:
        amount = dec(amount)
        account = self.venue.fetch_account()
        if amount > account.free_collateral:
            raise VaultError(
                "cannot release %s: only %s is free at the venue (the rest is "
                "margin on open positions)" % (amount, account.free_collateral)
            )
        account.free_collateral -= amount
        account.total_collateral -= amount
        self.vault.release(amount)
        self._allocated -= amount
        self.mark()
        return amount

    # -- trading -----------------------------------------------------------

    def trade(self, request: OrderRequest, **kw) -> TrackedOrder:
        """Submit an order, then re-mark so the share price reflects the fill."""
        tracked = self.oms.submit(request, **kw)
        self.mark()
        return tracked

    def mark(self):
        """Re-read the venue and recompute NAV."""
        return self.vault.mark_from_venue(self.venue)

    # -- reporting ---------------------------------------------------------

    def report(self) -> Dict[str, object]:
        out = dict(self.vault.report())
        out["allocated_to_venue"] = str(self._allocated)
        out["deployable"] = str(self.deployable().quantize(Decimal("0.01")))
        out["orders"] = self.oms.stats()
        return out

    def investor_table(self) -> List[Dict[str, object]]:
        return [self.vault.position_of(a) for a in sorted(self.vault.holders)]
