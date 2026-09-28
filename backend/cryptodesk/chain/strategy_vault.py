"""A tokenized strategy vault: trading PnL expressed as a share price.

This is the piece that joins the two halves of the package. Depositors put in
a settlement asset and receive *shares*. The strategy trades the pooled
collateral on a venue. Profit and loss accrue to the share price rather than to
individual balances, so a depositor's claim grows or shrinks with performance
and their share count never changes.

That is the same mechanism as an on-chain ERC-4626 vault - and deliberately so:
`ERC4626Vault` in this package *reads* deployed vaults, and this class
*implements* the identical accounting off-chain against a trading venue. The
conversion maths, rounding rules and previews match, so a strategy developed
here maps onto a real deployment without the accounting changing underneath it.

Three properties are load-bearing, and each is a place real vaults have lost
money:

**Rounding always favours the vault.** A deposit rounds shares *down*, a
redemption rounds assets *down*. If rounding ever favoured the transacting
user, a loop of tiny deposits and redemptions would extract value from the
remaining holders one wei at a time.

**Virtual offset against first-depositor inflation.** On an empty vault, an
attacker deposits 1 wei, donates a large amount directly to the vault, and the
next depositor's shares round to zero - their deposit is absorbed. Adding a
virtual asset/share offset to every conversion makes that attack cost more than
it yields. This mirrors OpenZeppelin's mitigation.

**NAV is marked before any deposit or redemption.** Converting at a stale share
price lets a depositor buy in ahead of a known gain, diluting existing holders.
`deposit` and `redeem` refuse to run against a NAV older than `max_nav_age_ms`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Dict, List, Optional

from ..core.types import ZERO, dec, now_ms

#: Virtual assets added to the denominator of every conversion. Always 1, as
#: in OpenZeppelin - it is what stops a division by zero on an empty vault and
#: what makes a donation attack cost the attacker more than it yields.
VIRTUAL_ASSETS = Decimal(1)

#: Default decimals offset. Virtual *shares* are 10**offset.
#:
#: 0 is OpenZeppelin's default and means the share price starts at exactly
#: 1.0, which is what every dashboard, LP and high-water mark assumes. Raising
#: it strengthens inflation resistance - an attacker must donate 10**offset
#: times more to move the price a given amount - at the cost of a share price
#: that no longer starts at 1.0, because shares become denominated in units
#: 10**offset finer than the asset.
DEFAULT_DECIMALS_OFFSET = 0


class VaultError(Exception):
    """Deposit, redemption or accounting failure."""


class StaleNav(VaultError):
    """NAV has not been marked recently enough to transact against."""


@dataclass
class Holder:
    """One depositor's position."""

    address: str
    shares: Decimal = ZERO
    #: Cumulative assets deposited, for reporting realised return.
    deposited: Decimal = ZERO
    withdrawn: Decimal = ZERO

    @property
    def net_deposited(self) -> Decimal:
        return self.deposited - self.withdrawn


@dataclass
class NavSnapshot:
    """A point-in-time valuation of everything the vault owns."""

    total_assets: Decimal
    idle: Decimal
    collateral: Decimal
    unrealised_pnl: Decimal
    timestamp_ms: int = field(default_factory=now_ms)
    source: str = "unknown"

    def age_ms(self, now: Optional[int] = None) -> int:
        return (now if now is not None else now_ms()) - self.timestamp_ms


class StrategyVault:
    """ERC-4626-compatible share accounting over a trading strategy.

    `total_assets` is the vault's NAV: idle settlement balance, plus collateral
    posted at the venue, plus unrealised PnL on open positions, minus accrued
    but uncrystallised performance fees.
    """

    def __init__(
        self,
        name: str = "Strategy Vault",
        symbol: str = "svUSD",
        asset: str = "USDC",
        decimals: int = 6,
        performance_fee_bps: Decimal = Decimal("2000"),   # 20% of new profit
        management_fee_bps: Decimal = Decimal("200"),     # 2% per year
        max_nav_age_ms: int = 60_000,
        decimals_offset: int = DEFAULT_DECIMALS_OFFSET,
    ):
        self.name = name
        self.symbol = symbol
        self.asset = asset
        self.decimals = decimals
        self.performance_fee_bps = dec(performance_fee_bps)
        self.management_fee_bps = dec(management_fee_bps)
        self.max_nav_age_ms = max_nav_age_ms
        self.decimals_offset = decimals_offset
        #: Virtual shares in the conversion numerator: 10**offset.
        self.virtual_shares = Decimal(10) ** decimals_offset

        self.total_supply: Decimal = ZERO
        self.holders: Dict[str, Holder] = {}

        self._idle: Decimal = ZERO
        self._nav: Optional[NavSnapshot] = None
        #: Highest share price ever reached. Performance fee is charged only on
        #: gains above it, so a depositor never pays twice for recovering the
        #: same loss.
        self._high_water_mark: Decimal = Decimal(1)
        self._fees_accrued: Decimal = ZERO
        self._last_fee_accrual_ms: int = now_ms()
        self.ledger: List[str] = []

    # -- NAV ---------------------------------------------------------------

    @property
    def idle(self) -> Decimal:
        return self._idle

    def mark(self, collateral: Decimal = ZERO, unrealised_pnl: Decimal = ZERO,
             source: str = "manual") -> NavSnapshot:
        """Recompute NAV from the strategy's current state.

        `idle` and `collateral` are **disjoint** buckets, and NAV is their sum
        plus open-position PnL. Capital moves between them via `allocate` and
        `release`; it is never in both.

        So marking a vault holding 100k idle with `collateral=110000` describes
        210k of assets, not a 10% gain. To model a gain: allocate the capital
        first, then mark with the grown collateral figure. Getting this
        backwards silently doubles NAV and inflates the share price, which is
        why the two buckets are named rather than collapsed into one total.
        """
        collateral, unrealised_pnl = dec(collateral), dec(unrealised_pnl)
        self._accrue_management_fee()
        total = self._idle + collateral + unrealised_pnl - self._fees_accrued
        self._nav = NavSnapshot(
            total_assets=max(ZERO, total), idle=self._idle,
            collateral=collateral, unrealised_pnl=unrealised_pnl, source=source,
        )
        self._crystallise_performance_fee()
        return self._nav

    def mark_from_venue(self, venue) -> NavSnapshot:
        """Mark NAV from a venue adapter's account state.

        Reads posted collateral and open-position PnL straight from the venue,
        so the share price cannot drift from what the strategy actually holds.
        """
        account = venue.fetch_account()
        upnl = sum((p.unrealised_pnl for p in account.positions.values()), ZERO)
        return self.mark(collateral=account.total_collateral,
                         unrealised_pnl=upnl,
                         source="venue:%s" % getattr(venue, "name", "?"))

    @property
    def nav(self) -> NavSnapshot:
        if self._nav is None:
            # An unmarked vault is all idle cash - true before any trading.
            self._nav = NavSnapshot(self._idle, self._idle, ZERO, ZERO,
                                    source="initial")
        return self._nav

    @property
    def total_assets(self) -> Decimal:
        return self.nav.total_assets

    def _require_fresh_nav(self) -> None:
        age = self.nav.age_ms()
        if age > self.max_nav_age_ms:
            raise StaleNav(
                "NAV is %dms old (limit %dms) - mark before transacting, or a "
                "depositor converts at a price that no longer reflects the "
                "portfolio" % (age, self.max_nav_age_ms)
            )

    # -- conversions (ERC-4626 semantics) ----------------------------------

    def _convert_to_shares(self, assets: Decimal, total_assets: Decimal) -> Decimal:
        """assets -> shares, rounding DOWN, with the virtual offset applied.

        Rounding down is the security-relevant direction: it means a depositor
        can never receive more claim than they paid for, so a deposit/redeem
        loop cannot drain the pool.
        """
        numerator = assets * (self.total_supply + self.virtual_shares)
        denominator = total_assets + VIRTUAL_ASSETS
        if denominator <= ZERO:
            return ZERO
        return (numerator / denominator).quantize(
            Decimal(1).scaleb(-self.decimals), rounding=ROUND_DOWN)

    def _convert_to_assets(self, shares: Decimal, total_assets: Decimal) -> Decimal:
        """shares -> assets, rounding DOWN."""
        denominator = self.total_supply + self.virtual_shares
        if denominator <= ZERO:
            return ZERO
        numerator = shares * (total_assets + VIRTUAL_ASSETS)
        return (numerator / denominator).quantize(
            Decimal(1).scaleb(-self.decimals), rounding=ROUND_DOWN)

    def convert_to_shares(self, assets: Decimal) -> Decimal:
        """Ideal rate, ignoring fees - the number a UI displays."""
        return self._convert_to_shares(dec(assets), self.total_assets)

    def convert_to_assets(self, shares: Decimal) -> Decimal:
        return self._convert_to_assets(dec(shares), self.total_assets)

    def preview_deposit(self, assets: Decimal) -> Decimal:
        """Shares actually minted, fees included.

        Identical to `convert_to_shares` here because this vault charges no
        entry fee - performance fees come out of NAV, not out of the deposit.
        Kept as a separate method because ERC-4626 callers are expected to use
        it, and a vault that later adds an entry fee changes only this.
        """
        return self.convert_to_shares(assets)

    def preview_redeem(self, shares: Decimal) -> Decimal:
        return self.convert_to_assets(shares)

    @property
    def share_price(self) -> Optional[Decimal]:
        """Assets per share. None on an empty vault.

        None rather than 1.0 for the same reason as the on-chain reader: an
        empty vault is the condition where inflation is possible, and a
        plausible default hides it.
        """
        if self.total_supply <= ZERO:
            return None
        return self.total_assets / self.total_supply

    # -- deposits and redemptions -----------------------------------------

    def deposit(self, assets: Decimal, receiver: str) -> Decimal:
        """Deposit `assets`, mint shares to `receiver`. Returns shares minted."""
        assets = dec(assets)
        if assets <= ZERO:
            raise VaultError("deposit must be positive, got %s" % assets)
        self._require_fresh_nav()

        shares = self.preview_deposit(assets)
        if shares <= ZERO:
            raise VaultError(
                "deposit of %s would mint zero shares at a share price of %s - "
                "refusing, as the assets would be absorbed by existing holders"
                % (assets, self.share_price)
            )

        holder = self.holders.setdefault(receiver, Holder(receiver))
        holder.shares += shares
        holder.deposited += assets
        self.total_supply += shares
        self._idle += assets
        # NAV grows by exactly the deposit; re-marking keeps the share price
        # unchanged, which is the invariant a deposit must preserve.
        self._nav = NavSnapshot(
            total_assets=self.nav.total_assets + assets,
            idle=self._idle, collateral=self.nav.collateral,
            unrealised_pnl=self.nav.unrealised_pnl, source="deposit",
        )
        self.ledger.append("deposit %s %s -> %s shares to %s"
                           % (assets, self.asset, shares, receiver))
        return shares

    def redeem(self, shares: Decimal, owner: str) -> Decimal:
        """Burn `shares` from `owner`, return assets. Returns assets paid out."""
        shares = dec(shares)
        if shares <= ZERO:
            raise VaultError("redemption must be positive, got %s" % shares)
        holder = self.holders.get(owner)
        if holder is None or holder.shares < shares:
            raise VaultError(
                "%s holds %s shares, cannot redeem %s"
                % (owner, holder.shares if holder else 0, shares)
            )
        self._require_fresh_nav()

        assets = self.preview_redeem(shares)
        if assets > self._idle:
            # The strategy's capital is at the venue, not in the vault. Real
            # vaults solve this with a withdrawal queue; surfacing it is
            # better than paying out collateral the strategy is still using.
            raise VaultError(
                "redemption needs %s %s but only %s is idle - %s is posted as "
                "collateral. Close positions or use a withdrawal queue."
                % (assets, self.asset, self._idle, self.nav.collateral)
            )

        holder.shares -= shares
        holder.withdrawn += assets
        self.total_supply -= shares
        self._idle -= assets
        self._nav = NavSnapshot(
            total_assets=self.nav.total_assets - assets,
            idle=self._idle, collateral=self.nav.collateral,
            unrealised_pnl=self.nav.unrealised_pnl, source="redeem",
        )
        self.ledger.append("redeem %s shares -> %s %s to %s"
                           % (shares, assets, self.asset, owner))
        return assets

    # -- capital movement between vault and venue --------------------------

    def allocate(self, amount: Decimal) -> Decimal:
        """Move idle assets out to the trading venue as collateral.

        Does not change NAV or share price - the assets move between buckets,
        they do not appear or disappear.
        """
        amount = dec(amount)
        if amount > self._idle:
            raise VaultError("cannot allocate %s, only %s idle"
                             % (amount, self._idle))
        self._idle -= amount
        self.ledger.append("allocate %s %s to venue" % (amount, self.asset))
        return amount

    def release(self, amount: Decimal) -> Decimal:
        """Bring collateral back from the venue into idle assets."""
        amount = dec(amount)
        self._idle += amount
        self.ledger.append("release %s %s from venue" % (amount, self.asset))
        return amount

    # -- fees --------------------------------------------------------------

    def _accrue_management_fee(self) -> None:
        """Time-based fee on assets under management, accrued continuously."""
        if self.management_fee_bps <= ZERO or self.total_supply <= ZERO:
            self._last_fee_accrual_ms = now_ms()
            return
        now = now_ms()
        elapsed_ms = now - self._last_fee_accrual_ms
        if elapsed_ms <= 0:
            return
        year_ms = Decimal(365 * 24 * 60 * 60 * 1000)
        base = self._idle + (self._nav.collateral if self._nav else ZERO)
        fee = (base * self.management_fee_bps / Decimal(10000)
               * Decimal(elapsed_ms) / year_ms)
        if fee > ZERO:
            self._fees_accrued += fee
        self._last_fee_accrual_ms = now

    def _crystallise_performance_fee(self) -> None:
        """Charge performance fee on gains above the high-water mark.

        The high-water mark is what stops a vault that loses 20% and then
        regains it from charging a fee on the recovery - the depositor is only
        back where they started, and a fee there is a fee on nothing.
        """
        if self.total_supply <= ZERO or self.performance_fee_bps <= ZERO:
            return
        price = self.share_price
        if price is None or price <= self._high_water_mark:
            return
        gain_per_share = price - self._high_water_mark
        fee = gain_per_share * self.total_supply * self.performance_fee_bps / Decimal(10000)
        if fee <= ZERO:
            return
        self._fees_accrued += fee
        if self._nav is not None:
            self._nav = NavSnapshot(
                total_assets=self._nav.total_assets - fee,
                idle=self._nav.idle, collateral=self._nav.collateral,
                unrealised_pnl=self._nav.unrealised_pnl,
                timestamp_ms=self._nav.timestamp_ms, source=self._nav.source,
            )
        new_price = self.share_price
        if new_price is not None:
            self._high_water_mark = max(self._high_water_mark, new_price)
        self.ledger.append("performance fee %s (hwm -> %s)"
                           % (fee, self._high_water_mark))

    @property
    def fees_accrued(self) -> Decimal:
        return self._fees_accrued

    @property
    def high_water_mark(self) -> Decimal:
        return self._high_water_mark

    # -- reporting ---------------------------------------------------------

    def position_of(self, address: str) -> Dict[str, object]:
        holder = self.holders.get(address)
        if holder is None or holder.shares <= ZERO:
            return {"address": address, "shares": "0", "value": "0"}
        value = self.preview_redeem(holder.shares)
        pnl = value - holder.net_deposited
        roi = (pnl / holder.net_deposited * Decimal(100)
               if holder.net_deposited > ZERO else ZERO)
        return {
            "address": address,
            "shares": str(holder.shares),
            "value": str(value),
            "net_deposited": str(holder.net_deposited),
            "pnl": str(pnl.quantize(Decimal("0.01"))),
            "roi_pct": str(roi.quantize(Decimal("0.01"))),
        }

    def report(self) -> Dict[str, object]:
        price = self.share_price
        return {
            "name": self.name,
            "symbol": self.symbol,
            "asset": self.asset,
            "total_assets": str(self.total_assets.quantize(Decimal("0.01"))),
            "total_supply": str(self.total_supply),
            "share_price": str(price) if price is not None else None,
            "high_water_mark": str(self._high_water_mark),
            "idle": str(self._idle.quantize(Decimal("0.01"))),
            "deployed_collateral": str(self.nav.collateral.quantize(Decimal("0.01"))),
            "unrealised_pnl": str(self.nav.unrealised_pnl.quantize(Decimal("0.01"))),
            "fees_accrued": str(self._fees_accrued.quantize(Decimal("0.01"))),
            "holders": len(self.holders),
            "nav_age_ms": self.nav.age_ms(),
            "nav_source": self.nav.source,
        }

    # -- durability --------------------------------------------------------

    def save(self, store, name: str = "default") -> None:
        """Persist the share ledger and the scalars that cannot be recomputed.

        The high-water mark is the one that matters most. Share counts could in
        principle be rebuilt from a deposit log, but the HWM is pure history:
        lose it and it resets to 1.0, so a fund down 20% starts charging
        performance fees all the way back up - the exact double-charge the
        high-water mark exists to prevent. That is a money bug produced purely
        by a storage gap.
        """
        store.put("vault:%s" % name, {
            "name": self.name,
            "symbol": self.symbol,
            "asset": self.asset,
            "decimals": self.decimals,
            "decimals_offset": self.decimals_offset,
            "total_supply": str(self.total_supply),
            "idle": str(self._idle),
            "high_water_mark": str(self._high_water_mark),
            "fees_accrued": str(self._fees_accrued),
            "last_fee_accrual_ms": self._last_fee_accrual_ms,
            "performance_fee_bps": str(self.performance_fee_bps),
            "management_fee_bps": str(self.management_fee_bps),
            "nav": {
                "total_assets": str(self.nav.total_assets),
                "idle": str(self.nav.idle),
                "collateral": str(self.nav.collateral),
                "unrealised_pnl": str(self.nav.unrealised_pnl),
                "timestamp_ms": self.nav.timestamp_ms,
                "source": self.nav.source,
            },
        })
        for address, holder in self.holders.items():
            store.save_holder(name, address, holder.shares,
                              holder.deposited, holder.withdrawn)

    def load(self, store, name: str = "default") -> bool:
        """Restore from a store. Returns False if nothing was saved.

        The restored NAV keeps its original timestamp rather than being stamped
        fresh, so `_require_fresh_nav` refuses deposits and redemptions until
        the caller re-marks. That is deliberate: after a restart the portfolio
        has moved, and converting at a pre-outage share price would hand the
        first depositor back a stale rate.
        """
        state = store.get("vault:%s" % name)
        if not state:
            return False
        self.name = state.get("name", self.name)
        self.symbol = state.get("symbol", self.symbol)
        self.asset = state.get("asset", self.asset)
        self.decimals = int(state.get("decimals", self.decimals))
        self.decimals_offset = int(state.get("decimals_offset",
                                             self.decimals_offset))
        self.virtual_shares = Decimal(10) ** self.decimals_offset
        self.total_supply = dec(state.get("total_supply", "0"))
        self._idle = dec(state.get("idle", "0"))
        self._high_water_mark = dec(state.get("high_water_mark", "1"))
        self._fees_accrued = dec(state.get("fees_accrued", "0"))
        self._last_fee_accrual_ms = int(
            state.get("last_fee_accrual_ms", now_ms()))
        self.performance_fee_bps = dec(state.get("performance_fee_bps",
                                                 self.performance_fee_bps))
        self.management_fee_bps = dec(state.get("management_fee_bps",
                                                self.management_fee_bps))
        nav = state.get("nav") or {}
        if nav:
            self._nav = NavSnapshot(
                total_assets=dec(nav.get("total_assets", "0")),
                idle=dec(nav.get("idle", "0")),
                collateral=dec(nav.get("collateral", "0")),
                unrealised_pnl=dec(nav.get("unrealised_pnl", "0")),
                timestamp_ms=int(nav.get("timestamp_ms", now_ms())),
                source="%s (restored)" % nav.get("source", "unknown"),
            )
        self.holders = {
            address: Holder(address=address, shares=row["shares"],
                            deposited=row["deposited"], withdrawn=row["withdrawn"])
            for address, row in store.load_holders(name).items()
        }
        self.ledger.append("restored from store as %r" % name)
        return True

    def __repr__(self) -> str:
        return "<StrategyVault %s price=%s supply=%s>" % (
            self.symbol, self.share_price, self.total_supply)
