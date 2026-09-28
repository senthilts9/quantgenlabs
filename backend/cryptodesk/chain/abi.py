"""Minimal ABI encoding and decoding.

Enough of the Ethereum ABI to make `eth_call` reads and to *build* (never
broadcast) transaction calldata. Deliberately small: full ABI coding is a
solved problem in `eth-abi`, and pulling in a dependency to read a uint256 is
not worth it - but hand-rolling dynamic-type encoding for writes would be, so
this covers the static types and the two dynamic reads that actually come up
(`string` returns from `symbol()` and `name()`).

Function selectors are hardcoded as constants rather than computed, because
computing them needs keccak-256 and that is the one primitive the standard
library does not have. Every selector here is a well-known published value;
`SELECTORS` documents which signature each came from so they are checkable.
"""
from __future__ import annotations

from typing import List, Optional

#: selector -> the signature it was derived from, for auditability.
SELECTORS = {
    # ERC-20
    "0x70a08231": "balanceOf(address)",
    "0x313ce567": "decimals()",
    "0x95d89b41": "symbol()",
    "0x06fdde03": "name()",
    "0x18160ddd": "totalSupply()",
    "0xdd62ed3e": "allowance(address,address)",
    "0x095ea7b3": "approve(address,uint256)",
    "0xa9059cbb": "transfer(address,uint256)",
    # ERC-4626 tokenized vault
    "0x38d52e0f": "asset()",
    "0x01e1d114": "totalAssets()",
    "0xc6e6f592": "convertToShares(uint256)",
    "0x07a2d13a": "convertToAssets(uint256)",
    "0xef8b30f7": "previewDeposit(uint256)",
    "0x4cdad506": "previewRedeem(uint256)",
    "0xb3d7f6b9": "previewMint(uint256)",
    "0x0a28a477": "previewWithdraw(uint256)",
    "0x402d267d": "maxDeposit(address)",
    "0xce96cb77": "maxWithdraw(address)",
    "0x6e553f65": "deposit(uint256,address)",
    "0xba087652": "redeem(uint256,address,address)",
    # Chainlink
    "0xfeaf968c": "latestRoundData()",
    # Uniswap v3
    "0x3850c7bd": "slot0()",
}

ZERO_ADDRESS = "0x" + "00" * 20


def encode_address(addr: str) -> str:
    """Left-pad a 20-byte address into a 32-byte word."""
    clean = addr.lower().replace("0x", "")
    if len(clean) != 40:
        raise ValueError("not a 20-byte address: %r" % addr)
    return clean.rjust(64, "0")


def encode_uint(value: int) -> str:
    if value < 0:
        raise ValueError("uint cannot be negative: %d" % value)
    if value >= 1 << 256:
        raise ValueError("value exceeds uint256")
    return format(value, "064x")


def call_data(selector: str, *args: str) -> str:
    """Concatenate a selector with already-encoded 32-byte words."""
    sel = selector if selector.startswith("0x") else "0x" + selector
    return sel + "".join(args)


def decode_word(hexstr: str, index: int = 0) -> int:
    """Read the nth 32-byte word as an unsigned integer."""
    body = hexstr[2:] if hexstr.startswith("0x") else hexstr
    chunk = body[index * 64:(index + 1) * 64]
    if len(chunk) < 64:
        raise ValueError("return data too short for word %d: %r" % (index, hexstr))
    return int(chunk, 16)


def decode_int(hexstr: str, index: int = 0, bits: int = 256) -> int:
    """Read a word as two's-complement signed."""
    raw = decode_word(hexstr, index)
    limit = 1 << (bits - 1)
    return raw - (1 << bits) if raw >= limit else raw


def decode_address(hexstr: str, index: int = 0) -> str:
    body = hexstr[2:] if hexstr.startswith("0x") else hexstr
    chunk = body[index * 64:(index + 1) * 64]
    if len(chunk) < 64:
        raise ValueError("return data too short for address")
    return "0x" + chunk[-40:]


def decode_string(hexstr: str) -> Optional[str]:
    """Decode a `string` return, tolerating the bytes32 variant.

    Several early tokens (MKR is the canonical example) declare `symbol()` as
    returning `bytes32` rather than `string`, so a decoder that assumes the
    dynamic layout returns garbage for them. Length-32 return data with no
    plausible offset is treated as bytes32 and null-trimmed.
    """
    body = hexstr[2:] if hexstr.startswith("0x") else hexstr
    if not body:
        return None

    # bytes32 variant: exactly one word, not a valid offset.
    if len(body) == 64:
        try:
            raw = bytes.fromhex(body).rstrip(b"\x00")
            return raw.decode("utf-8") if raw else None
        except (ValueError, UnicodeDecodeError):
            return None

    try:
        offset = decode_word(body, 0)
        # Offset is a byte count; convert to hex characters.
        start = offset * 2
        length = int(body[start:start + 64], 16)
        data = body[start + 64:start + 64 + length * 2]
        return bytes.fromhex(data).decode("utf-8").rstrip("\x00") or None
    except (ValueError, UnicodeDecodeError, IndexError):
        return None


def decode_word_list(hexstr: str) -> List[int]:
    """All 32-byte words as unsigned ints - for multi-return functions."""
    body = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return [int(body[i:i + 64], 16) for i in range(0, len(body) - 63, 64)]
