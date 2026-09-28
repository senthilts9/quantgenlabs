"""On-chain wallet reads and transaction construction.

Read paths are unauthenticated `eth_call` / `eth_getBalance` against a public
RPC - any address's balances are public, and reading them needs no key at all.

Write paths **build calldata and return it unsigned**. Nothing in this module
broadcasts. That split is deliberate rather than incomplete: the moment a
library can both construct and send a transfer, a bug in address handling
becomes an irreversible loss, and there is no testnet-only guard strong enough
to make that acceptable in a package someone will copy into production. Signing
and broadcasting belong behind the vault and an explicit operator action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from ..marketdata.defi import JsonRpc
from . import abi

#: Well-known test tokens. Sepolia's ERC-20 landscape is mostly faucet tokens;
#: these are the ones with reliable liquidity and public faucets.
SEPOLIA_TOKENS = {
    "WETH": "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
    "USDC": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
    "LINK": "0x779877A7B0D9E8603169DdbD7836e478b4624789",
    "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
}

BASE_SEPOLIA_TOKENS = {
    "USDC": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "WETH": "0x4200000000000000000000000000000000000006",
}


@dataclass
class TokenBalance:
    symbol: str
    address: str
    raw: int
    decimals: int
    #: USD value where a price was resolvable, else None. Never zero-filled -
    #: an unpriced token and a worthless token are different facts.
    usd_value: Optional[Decimal] = None

    @property
    def amount(self) -> Decimal:
        return Decimal(self.raw) / (Decimal(10) ** self.decimals)

    def __str__(self) -> str:
        base = "%s %s" % (self.amount, self.symbol)
        return base + (" (~$%s)" % self.usd_value.quantize(Decimal("0.01"))
                       if self.usd_value is not None else "")


@dataclass
class WalletSnapshot:
    address: str
    chain: str
    native: Decimal = Decimal(0)
    native_symbol: str = "ETH"
    tokens: List[TokenBalance] = field(default_factory=list)
    block: Optional[int] = None

    def total_usd(self, native_price: Optional[Decimal] = None) -> Optional[Decimal]:
        """Portfolio value, or None if nothing could be priced.

        Returns None rather than a partial sum when no component priced: a
        confident "$0.00" for a wallet whose prices simply failed to load is
        the kind of number that gets screenshotted.
        """
        total = Decimal(0)
        priced = False
        if native_price is not None:
            total += self.native * native_price
            priced = True
        for t in self.tokens:
            if t.usd_value is not None:
                total += t.usd_value
                priced = True
        return total if priced else None


class Wallet:
    """Read-only view of an address, plus unsigned calldata construction."""

    def __init__(self, address: str, rpc: Optional[JsonRpc] = None,
                 chain: str = "sepolia"):
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError("not an address: %r" % address)
        self.address = address
        self.rpc = rpc or JsonRpc(chain)
        self.chain = chain

    # -- reads -------------------------------------------------------------

    def native_balance(self) -> Decimal:
        raw = self.rpc.call("eth_getBalance", [self.address, "latest"])
        return Decimal(int(str(raw), 16)) / (Decimal(10) ** 18)

    def nonce(self) -> int:
        """Transaction count - the replay-protection counter for this address."""
        raw = self.rpc.call("eth_getTransactionCount", [self.address, "latest"])
        return int(str(raw), 16)

    def token_balance(self, token_address: str,
                      symbol: Optional[str] = None) -> Optional[TokenBalance]:
        """ERC-20 balance, decimals and symbol in three calls.

        Returns None rather than zero when the call reverts - a non-contract
        address and a zero balance are different, and conflating them makes a
        typo'd token address look like an empty position.
        """
        data = abi.call_data("0x70a08231", abi.encode_address(self.address))
        raw = self.rpc.eth_call(token_address, data)
        if not raw or raw == "0x":
            return None
        try:
            amount = abi.decode_word(raw)
        except ValueError:
            return None

        decimals = 18
        dec_raw = self.rpc.eth_call(token_address, "0x313ce567")
        if dec_raw and dec_raw != "0x":
            try:
                decimals = abi.decode_word(dec_raw)
            except ValueError:
                pass

        if symbol is None:
            sym_raw = self.rpc.eth_call(token_address, "0x95d89b41")
            symbol = abi.decode_string(sym_raw) if sym_raw else None
        return TokenBalance(
            symbol=symbol or token_address[:10],
            address=token_address, raw=amount, decimals=decimals,
        )

    def snapshot(self, tokens: Optional[Dict[str, str]] = None,
                 price_lookup=None) -> WalletSnapshot:
        """Full balance sheet for the address.

        `price_lookup` is a callable `symbol -> Decimal | None`, injected rather
        than hardcoded so the caller chooses the price source (and so this stays
        testable without a network).
        """
        token_map = tokens if tokens is not None else (
            SEPOLIA_TOKENS if self.chain == "sepolia" else BASE_SEPOLIA_TOKENS
        )
        snap = WalletSnapshot(address=self.address, chain=self.chain)
        try:
            snap.native = self.native_balance()
            snap.block = self.rpc.block_number()
        except Exception:
            pass

        for symbol, addr in token_map.items():
            try:
                bal = self.token_balance(addr, symbol)
            except Exception:
                continue
            if bal is None:
                continue
            if price_lookup is not None:
                try:
                    price = price_lookup(symbol)
                    if price is not None:
                        bal.usd_value = bal.amount * price
                except Exception:
                    pass
            snap.tokens.append(bal)
        return snap

    def allowance(self, token_address: str, spender: str) -> Optional[int]:
        """How much `spender` may move on this wallet's behalf.

        Worth checking before any vault deposit: a stale unlimited approval to
        a contract that later turns out to be exploitable is one of the most
        common ways funds are lost in DeFi, and it is invisible from a balance.
        """
        data = abi.call_data("0xdd62ed3e",
                             abi.encode_address(self.address),
                             abi.encode_address(spender))
        raw = self.rpc.eth_call(token_address, data)
        if not raw or raw == "0x":
            return None
        try:
            return abi.decode_word(raw)
        except ValueError:
            return None

    # -- unsigned transaction construction ---------------------------------

    def build_approve(self, token_address: str, spender: str,
                      amount_raw: int) -> Dict[str, object]:
        """Unsigned `approve` calldata.

        Takes an explicit amount rather than defaulting to unlimited. Unlimited
        approval is the convenient choice and the one that turns a future
        contract compromise into a total loss of that token.
        """
        return {
            "to": token_address,
            "from": self.address,
            "data": abi.call_data("0x095ea7b3",
                                  abi.encode_address(spender),
                                  abi.encode_uint(amount_raw)),
            "value": "0x0",
            "_note": "UNSIGNED. Review, then sign and broadcast deliberately.",
            "_decoded": "approve(%s, %d)" % (spender, amount_raw),
        }

    def build_transfer(self, token_address: str, to: str,
                       amount_raw: int) -> Dict[str, object]:
        """Unsigned ERC-20 `transfer` calldata."""
        return {
            "to": token_address,
            "from": self.address,
            "data": abi.call_data("0xa9059cbb",
                                  abi.encode_address(to),
                                  abi.encode_uint(amount_raw)),
            "value": "0x0",
            "_note": "UNSIGNED. Review, then sign and broadcast deliberately.",
            "_decoded": "transfer(%s, %d)" % (to, amount_raw),
        }

    def __repr__(self) -> str:
        return "<Wallet %s on %s>" % (self.address, self.chain)
