"""Signers for the venues that require client-side order signatures.

Two schemes matter here:

* **EIP-712** (Hyperliquid, and Paradex's EVM onboarding path) - structured
  data hashed with a domain separator so a signature for one chain, contract
  or version cannot be replayed against another.
* **Starknet typed data / SNIP-12** (Paradex order signing) - the same idea in
  the Starknet field, using Pedersen/Poseidon hashing over felts rather than
  keccak over bytes.

Neither is implemented from scratch. Both delegate to the ecosystem library
(`eth-account`, `starknet-py`) that the venues themselves test against. Hand-
rolling keccak, secp256k1 or the Stark curve to save a dependency would mean
every signature is a bug candidate, and a malformed signature on an order is
not a crash - it is a silent rejection you debug for a day.

`DeterministicTestSigner` produces reproducible non-signatures for tests. It
sets `is_test=True`, and every venue adapter refuses it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Dict, Optional

from ..core.types import OrderRequest, Side, now_ms
from .base import KeyVault, Signature, Signer, VaultError


def _availability() -> Dict[str, bool]:
    """Which optional signing libraries are installed."""
    out = {}
    try:
        import eth_account  # noqa: F401
        out["eth_account"] = True
    except ImportError:
        out["eth_account"] = False
    try:
        import starknet_py  # noqa: F401
        out["starknet_py"] = True
    except ImportError:
        out["starknet_py"] = False
    return out


AVAILABLE = _availability()


class SignerUnavailable(VaultError):
    """A real signer was asked for but its library is not installed."""


class DeterministicTestSigner(Signer):
    """HMAC-based stand-in. **Not a signature.** Tests only.

    Produces a stable 32-byte digest over the canonical message so tests can
    assert that the right bytes were signed, that a changed field changes the
    output, and that replay protection works - all without a crypto dependency
    and without a real key existing anywhere.

    It cannot be verified by any venue, which is the point: `is_test` is True
    and the venue adapters reject it, so this can never accidentally be what
    ships.
    """

    scheme = "test-hmac"
    is_test = True

    def __init__(self, secret: str = "test-key", account: str = "0xtest"):
        self._secret = secret.encode("utf-8")
        self._account = account

    def address(self) -> str:
        return self._account

    @staticmethod
    def canonical(payload: Dict) -> str:
        """Stable serialisation: sorted keys, no incidental whitespace.

        Dict ordering must not change the digest, or a test that passes on one
        Python version fails on another for no real reason.
        """
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    def sign_message(self, payload: Dict) -> Signature:
        msg = self.canonical(payload)
        digest = hmac.new(self._secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return Signature(value="0x" + digest, scheme=self.scheme,
                         signer=self._account, is_test=True,
                         components={"message": msg})

    def sign_typed_data(self, domain: Dict, types: Dict, message: Dict) -> Signature:
        return self.sign_message({"domain": domain, "types": types, "message": message})


class EthSigner(Signer):
    """EIP-712 signer backed by `eth-account`.

    The key is pulled from the vault only inside `_with_key`, so it exists as a
    local for the duration of one signing call rather than as an attribute on a
    long-lived object.
    """

    scheme = "eip712"

    def __init__(self, vault: KeyVault, key_name: str):
        if not AVAILABLE["eth_account"]:
            raise SignerUnavailable(
                "EIP-712 signing needs `eth-account` (pip install eth-account). "
                "Use DeterministicTestSigner for offline tests."
            )
        self.vault = vault
        self.key_name = key_name
        self._address: Optional[str] = None

    def _with_key(self, fn, reason: str):
        return self.vault.use(self.key_name, fn, reason=reason)

    def address(self) -> str:
        if self._address is None:
            from eth_account import Account
            self._address = self._with_key(
                lambda k: Account.from_key(k).address, reason="derive-address"
            )
        return self._address

    def sign_typed_data(self, domain: Dict, types: Dict, message: Dict) -> Signature:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        primary = next((t for t in types if t != "EIP712Domain"), None)
        full = {
            "types": types, "domain": domain,
            "primaryType": primary, "message": message,
        }

        def _sign(key: str):
            signable = encode_typed_data(full_message=full)
            return Account.sign_message(signable, private_key=key)

        signed = self._with_key(_sign, reason="sign-typed-data")
        return Signature(
            value=signed.signature.hex(),
            scheme=self.scheme,
            signer=self.address(),
            components={"r": hex(signed.r), "s": hex(signed.s), "v": str(signed.v)},
        )

    def sign_message(self, payload: Dict) -> Signature:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        signed = self._with_key(
            lambda k: Account.sign_message(encode_defunct(text=text), private_key=k),
            reason="sign-message",
        )
        return Signature(value=signed.signature.hex(), scheme="eip191",
                         signer=self.address())


class StarknetSigner(Signer):
    """Starknet typed-data signer backed by `starknet-py` - Paradex's scheme."""

    scheme = "starknet-typed-data"

    def __init__(self, vault: KeyVault, key_name: str, account_address: str):
        if not AVAILABLE["starknet_py"]:
            raise SignerUnavailable(
                "Starknet signing needs `starknet-py` (pip install starknet-py). "
                "Use DeterministicTestSigner for offline tests."
            )
        self.vault = vault
        self.key_name = key_name
        self.account_address = account_address

    def address(self) -> str:
        return self.account_address

    @staticmethod
    def _typed_data_cls():
        """Locate `TypedData`, which has moved between starknet-py versions.

        It lives in `utils.typed_data` currently and was re-exported from
        `net.signer.stark_curve_signer`; `net.models.typed_data` holds a
        different (TypedDict) shape. Probing in order beats pinning a version,
        because the failure mode of guessing wrong is an ImportError at order
        time rather than at startup.
        """
        for path in ("starknet_py.utils.typed_data",
                     "starknet_py.net.signer.stark_curve_signer",
                     "starknet_py.net.models.typed_data"):
            try:
                mod = __import__(path, fromlist=["TypedData"])
                cls = getattr(mod, "TypedData", None)
                if cls is not None and hasattr(cls, "from_dict"):
                    return cls
            except ImportError:
                continue
        raise SignerUnavailable(
            "could not locate TypedData in the installed starknet-py; "
            "expected it at starknet_py.utils.typed_data"
        )

    def sign_typed_data(self, domain: Dict, types: Dict, message: Dict) -> Signature:
        from starknet_py.hash.utils import message_signature
        from starknet_py.net.signer.stark_curve_signer import KeyPair

        TypedData = self._typed_data_cls()
        typed = TypedData.from_dict({
            "types": types, "domain": domain,
            "primaryType": next((t for t in types if t != "StarkNetDomain"), "Order"),
            "message": message,
        })
        account_int = int(self.account_address, 16)

        msg_hash = typed.message_hash(account_int)

        def _sign(key: str):
            kp = KeyPair.from_private_key(int(key, 16))
            return message_signature(msg_hash, kp.private_key)

        r, s = self.vault.use(self.key_name, _sign, reason="sign-order")
        self._last_message_hash = msg_hash
        return Signature(
            value=json.dumps([str(r), str(s)]),
            scheme=self.scheme,
            signer=self.account_address,
            components={"r": str(r), "s": str(s)},
        )

    def sign_message(self, payload: Dict) -> Signature:
        raise NotImplementedError(
            "Starknet signing is typed-data only; use sign_typed_data"
        )


# ---------------------------------------------------------------------------
# Venue message builders
# ---------------------------------------------------------------------------
# The typed-data *shape* is venue protocol, not cryptography, so it lives here
# in plain form. Getting a field name or a scaling factor wrong produces a
# signature that verifies locally and is rejected remotely, which is why these
# are written out explicitly rather than generated.

#: Paradex scales prices and sizes to 8 decimal places on the wire. Signing
#: the unscaled decimal produces a valid signature over the wrong number.
PARADEX_SCALE = Decimal(10) ** 8


def paradex_order_typed_data(order: OrderRequest, chain_id: str,
                             timestamp_ms: Optional[int] = None) -> Dict:
    """Build the SNIP-12 typed data Paradex expects for a new order.

    `side` is the string "1" for BUY and "2" for SELL, and the numeric fields
    are integers scaled by 1e8. A market order signs price 0 - not the absent
    field, which would hash differently.
    """
    ts = timestamp_ms if timestamp_ms is not None else now_ms()
    price = order.price if order.price is not None else Decimal(0)
    return {
        "domain": {"name": "Paradex", "chainId": chain_id, "version": "1"},
        "types": {
            "StarkNetDomain": [
                {"name": "name", "type": "felt"},
                {"name": "chainId", "type": "felt"},
                {"name": "version", "type": "felt"},
            ],
            "Order": [
                {"name": "timestamp", "type": "felt"},
                {"name": "market", "type": "felt"},
                {"name": "side", "type": "felt"},
                {"name": "orderType", "type": "felt"},
                {"name": "size", "type": "felt"},
                {"name": "price", "type": "felt"},
            ],
        },
        "message": {
            "timestamp": str(ts),
            "market": order.market,
            "side": "1" if order.side is Side.BUY else "2",
            "orderType": order.order_type.value,
            "size": str(int(order.size * PARADEX_SCALE)),
            "price": str(int(price * PARADEX_SCALE)),
        },
    }


def hyperliquid_action_typed_data(action: Dict, nonce: int,
                                  chain_id: int = 1337,
                                  vault_address: Optional[str] = None) -> Dict:
    """EIP-712 envelope for a Hyperliquid exchange action.

    Hyperliquid signs a msgpack hash of the action inside an `Agent` struct.
    Computing that hash needs `msgpack` and the venue's exact encoding, so this
    returns the envelope and leaves `connectionId` to the caller - the official
    SDK is the right place to produce it, and guessing it wrong yields a
    signature that fails only at the venue.
    """
    return {
        "domain": {
            "name": "Exchange", "version": "1",
            "chainId": chain_id,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
        },
        "message": {"source": "a", "connectionId": None},
        "_action": action,
        "_nonce": nonce,
        "_vault": vault_address,
    }


def build_signer(vault: KeyVault, key_name: str, scheme: str = "auto",
                 account_address: Optional[str] = None,
                 allow_test: bool = False) -> Signer:
    """Pick the strongest signer available for `scheme`.

    Falls back to the test signer only when `allow_test` is set explicitly.
    Without that, an environment missing the signing library fails loudly at
    startup rather than producing unusable signatures at order time.
    """
    scheme = scheme.lower()
    if scheme in ("starknet", "paradex"):
        if AVAILABLE["starknet_py"] and account_address:
            return StarknetSigner(vault, key_name, account_address)
        if not allow_test:
            raise SignerUnavailable(
                "Paradex order signing needs `starknet-py` and an account address"
            )
        return DeterministicTestSigner(account=account_address or "0xtest")

    if scheme in ("eip712", "eth", "hyperliquid", "auto"):
        if AVAILABLE["eth_account"]:
            return EthSigner(vault, key_name)
        if not allow_test:
            raise SignerUnavailable(
                "EIP-712 signing needs `eth-account` (pip install eth-account)"
            )
        return DeterministicTestSigner(account=account_address or "0xtest")

    raise SignerUnavailable("unknown signing scheme %r" % scheme)
