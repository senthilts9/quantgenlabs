"""ERC-4626 tokenized vaults.

A vault takes deposits of one asset and issues *shares*. The share is itself a
token, and its value is `totalAssets / totalSupply` - so yield accrues by the
share price rising rather than by shares being minted to holders. That single
mechanism is what "vault digital currency" means in practice: your position is
a transferable token whose redemption value grows.

The distinction this module is built around, because it is where the money
actually is:

* `convertToAssets` / `convertToShares` - the *ideal* exchange rate, ignoring
  fees and slippage. This is what a UI shows you.
* `previewDeposit` / `previewRedeem` - what you would *actually* receive right
  now, fees and rounding included.

The gap between them is the real entry or exit cost, and `round_trip_cost_bps`
below measures it. A vault quoting a 12% APY with 90 bps of round-trip cost
pays back its entry cost in about a month, and a strategy rotating weekly
between such vaults loses money while appearing to chase yield.

All reads are `eth_call`. Deposits and withdrawals return unsigned calldata for
the same reason as `wallet.py` - this package does not broadcast.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from ..marketdata.defi import JsonRpc
from . import abi


@dataclass
class VaultState:
    """A point-in-time read of an ERC-4626 vault."""

    address: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    asset_address: Optional[str] = None
    asset_symbol: Optional[str] = None
    asset_decimals: int = 18
    share_decimals: int = 18
    total_assets_raw: int = 0
    total_supply_raw: int = 0
    chain: str = "sepolia"

    @property
    def total_assets(self) -> Decimal:
        return Decimal(self.total_assets_raw) / (Decimal(10) ** self.asset_decimals)

    @property
    def total_supply(self) -> Decimal:
        return Decimal(self.total_supply_raw) / (Decimal(10) ** self.share_decimals)

    @property
    def share_price(self) -> Optional[Decimal]:
        """Assets per share. None on an empty vault.

        A vault with zero supply has no defined share price, and returning 1.0
        for it would hide the condition that makes empty vaults dangerous: the
        first depositor can be front-run by a donation that inflates the share
        price, so their deposit rounds down to almost no shares. Seeing `None`
        is the signal to check for that before depositing.
        """
        if self.total_supply_raw == 0:
            return None
        return (Decimal(self.total_assets_raw) / Decimal(self.total_supply_raw)
                * (Decimal(10) ** (self.share_decimals - self.asset_decimals)))

    @property
    def is_empty(self) -> bool:
        return self.total_supply_raw == 0

    def to_dict(self) -> Dict[str, object]:
        sp = self.share_price
        return {
            "address": self.address,
            "name": self.name,
            "symbol": self.symbol,
            "asset": self.asset_symbol or self.asset_address,
            "total_assets": str(self.total_assets),
            "total_supply": str(self.total_supply),
            "share_price": str(sp) if sp is not None else None,
            "empty": self.is_empty,
            "chain": self.chain,
        }


@dataclass
class VaultPosition:
    """A holder's stake in a vault."""

    vault: VaultState
    shares_raw: int
    holder: str

    @property
    def shares(self) -> Decimal:
        return Decimal(self.shares_raw) / (Decimal(10) ** self.vault.share_decimals)

    def value(self, redeemable_raw: Optional[int] = None) -> Optional[Decimal]:
        """Underlying assets the position is worth.

        Pass `redeemable_raw` from `previewRedeem` for the true exit value;
        without it this falls back to the ideal share price, which overstates
        what you would actually receive from a vault charging an exit fee.
        """
        if redeemable_raw is not None:
            return Decimal(redeemable_raw) / (Decimal(10) ** self.vault.asset_decimals)
        sp = self.vault.share_price
        return None if sp is None else self.shares * sp


class ERC4626Vault:
    """Reader and calldata builder for an ERC-4626 vault."""

    def __init__(self, address: str, rpc: Optional[JsonRpc] = None,
                 chain: str = "sepolia"):
        self.address = address
        self.rpc = rpc or JsonRpc(chain)
        self.chain = chain
        self._state: Optional[VaultState] = None

    # -- reads -------------------------------------------------------------

    def _uint(self, selector: str, *args: str) -> Optional[int]:
        raw = self.rpc.eth_call(self.address, abi.call_data(selector, *args))
        if not raw or raw == "0x":
            return None
        try:
            return abi.decode_word(raw)
        except ValueError:
            return None

    def _string(self, selector: str) -> Optional[str]:
        raw = self.rpc.eth_call(self.address, selector)
        return abi.decode_string(raw) if raw and raw != "0x" else None

    def state(self, refresh: bool = False) -> VaultState:
        """Read the vault's core state.

        `asset()` returning nothing is the check that this is an ERC-4626 vault
        at all rather than a plain ERC-20 - a distinction worth making before
        interpreting `totalSupply` as share supply.
        """
        if self._state is not None and not refresh:
            return self._state

        asset_raw = self.rpc.eth_call(self.address, "0x38d52e0f")
        asset_address = None
        if asset_raw and asset_raw != "0x":
            try:
                asset_address = abi.decode_address(asset_raw)
            except ValueError:
                pass

        share_decimals = self._uint("0x313ce567") or 18
        asset_decimals = 18
        asset_symbol = None
        if asset_address and int(asset_address, 16) != 0:
            d_raw = self.rpc.eth_call(asset_address, "0x313ce567")
            if d_raw and d_raw != "0x":
                try:
                    asset_decimals = abi.decode_word(d_raw)
                except ValueError:
                    pass
            s_raw = self.rpc.eth_call(asset_address, "0x95d89b41")
            asset_symbol = abi.decode_string(s_raw) if s_raw else None

        self._state = VaultState(
            address=self.address,
            name=self._string("0x06fdde03"),
            symbol=self._string("0x95d89b41"),
            asset_address=asset_address,
            asset_symbol=asset_symbol,
            asset_decimals=asset_decimals,
            share_decimals=share_decimals,
            total_assets_raw=self._uint("0x01e1d114") or 0,
            total_supply_raw=self._uint("0x18160ddd") or 0,
            chain=self.chain,
        )
        return self._state

    def is_erc4626(self) -> bool:
        """Cheap conformance probe: does it answer `asset()` and `totalAssets()`?"""
        try:
            asset = self.rpc.eth_call(self.address, "0x38d52e0f")
            total = self.rpc.eth_call(self.address, "0x01e1d114")
            return bool(asset and asset != "0x" and total and total != "0x")
        except Exception:
            return False

    def preview_deposit(self, assets_raw: int) -> Optional[int]:
        """Shares actually minted for `assets_raw` - fees included."""
        return self._uint("0xef8b30f7", abi.encode_uint(assets_raw))

    def preview_redeem(self, shares_raw: int) -> Optional[int]:
        """Assets actually returned for `shares_raw` - fees included."""
        return self._uint("0x4cdad506", abi.encode_uint(shares_raw))

    def convert_to_shares(self, assets_raw: int) -> Optional[int]:
        """Ideal share count, ignoring fees."""
        return self._uint("0xc6e6f592", abi.encode_uint(assets_raw))

    def convert_to_assets(self, shares_raw: int) -> Optional[int]:
        """Ideal asset value, ignoring fees."""
        return self._uint("0x07a2d13a", abi.encode_uint(shares_raw))

    def position(self, holder: str) -> Optional[VaultPosition]:
        raw = self.rpc.eth_call(
            self.address, abi.call_data("0x70a08231", abi.encode_address(holder))
        )
        if not raw or raw == "0x":
            return None
        try:
            shares = abi.decode_word(raw)
        except ValueError:
            return None
        return VaultPosition(vault=self.state(), shares_raw=shares, holder=holder)

    # -- economics ---------------------------------------------------------

    def round_trip_cost_bps(self, assets_raw: Optional[int] = None) -> Optional[Decimal]:
        """Cost in basis points of depositing then immediately redeeming.

        This is the number that decides whether a yield is reachable. It walks
        the real path - `previewDeposit` then `previewRedeem` on the resulting
        shares - so entry fees, exit fees and rounding all land in one figure.

        Sized at 1% of the vault by default, because cost is not linear: a
        probe of one wei rounds to zero and reports a free vault, while a probe
        of the whole vault reports slippage nobody would ever pay.
        """
        st = self.state()
        if st.is_empty:
            return None
        if assets_raw is None:
            assets_raw = max(st.total_assets_raw // 100, 10 ** st.asset_decimals)

        shares = self.preview_deposit(assets_raw)
        if not shares:
            return None
        back = self.preview_redeem(shares)
        if back is None:
            return None
        if assets_raw == 0:
            return None
        loss = Decimal(assets_raw - back) / Decimal(assets_raw)
        return loss * Decimal(10000)

    def fee_gap_bps(self, assets_raw: Optional[int] = None) -> Optional[Decimal]:
        """Gap between the ideal rate and the real one, on entry only.

        Non-zero means the vault charges a deposit fee (or rounds against you).
        Comparing this with `round_trip_cost_bps` separates entry cost from
        exit cost, which matters when planning how long capital must stay to
        earn back its fees.
        """
        st = self.state()
        if assets_raw is None:
            assets_raw = 10 ** st.asset_decimals
        ideal = self.convert_to_shares(assets_raw)
        actual = self.preview_deposit(assets_raw)
        if not ideal or actual is None:
            return None
        return Decimal(ideal - actual) / Decimal(ideal) * Decimal(10000)

    # -- unsigned calldata -------------------------------------------------

    def build_deposit(self, assets_raw: int, receiver: str) -> Dict[str, object]:
        """Unsigned `deposit(assets, receiver)` calldata.

        Remember the vault needs an ERC-20 approval for `assets_raw` first, or
        this reverts. `Wallet.build_approve` produces that, and
        `Wallet.allowance` checks whether it is already in place.
        """
        expected = self.preview_deposit(assets_raw)
        return {
            "to": self.address,
            "data": abi.call_data("0x6e553f65",
                                  abi.encode_uint(assets_raw),
                                  abi.encode_address(receiver)),
            "value": "0x0",
            "_note": "UNSIGNED. Requires prior ERC-20 approval to this vault.",
            "_decoded": "deposit(%d, %s)" % (assets_raw, receiver),
            "_expected_shares": expected,
        }

    def build_redeem(self, shares_raw: int, receiver: str,
                     owner: Optional[str] = None) -> Dict[str, object]:
        """Unsigned `redeem(shares, receiver, owner)` calldata."""
        expected = self.preview_redeem(shares_raw)
        return {
            "to": self.address,
            "data": abi.call_data("0xba087652",
                                  abi.encode_uint(shares_raw),
                                  abi.encode_address(receiver),
                                  abi.encode_address(owner or receiver)),
            "value": "0x0",
            "_note": "UNSIGNED. Review before signing.",
            "_decoded": "redeem(%d, %s, %s)" % (shares_raw, receiver, owner or receiver),
            "_expected_assets": expected,
        }

    def report(self, holder: Optional[str] = None) -> Dict[str, object]:
        """Everything worth knowing about the vault in one dict."""
        st = self.state()
        out: Dict[str, object] = st.to_dict()
        rt = self.round_trip_cost_bps()
        gap = self.fee_gap_bps()
        out["round_trip_cost_bps"] = (str(rt.quantize(Decimal("0.01")))
                                      if rt is not None else None)
        out["entry_fee_gap_bps"] = (str(gap.quantize(Decimal("0.01")))
                                    if gap is not None else None)
        if st.is_empty:
            out["warning"] = (
                "vault is empty - first-depositor share inflation is possible; "
                "check for a donation/dead-shares mitigation before depositing"
            )
        if holder:
            pos = self.position(holder)
            if pos is not None and pos.shares_raw > 0:
                redeemable = self.preview_redeem(pos.shares_raw)
                out["holder"] = holder
                out["holder_shares"] = str(pos.shares)
                value = pos.value(redeemable)
                out["holder_value"] = str(value) if value is not None else None
        return out


def scan_vaults(addresses: List[str], rpc: Optional[JsonRpc] = None,
                chain: str = "sepolia") -> List[Dict[str, object]]:
    """Report on several vaults, skipping anything that is not ERC-4626."""
    shared = rpc or JsonRpc(chain)
    out = []
    for addr in addresses:
        v = ERC4626Vault(addr, shared, chain)
        try:
            if not v.is_erc4626():
                out.append({"address": addr, "error": "not an ERC-4626 vault"})
                continue
            out.append(v.report())
        except Exception as exc:
            out.append({"address": addr, "error": str(exc)})
    return out
