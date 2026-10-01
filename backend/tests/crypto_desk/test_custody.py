"""Key custody, ABI coding and the mainnet guard.

All offline. The ABI tests use fixed return data captured from real contract
calls, so decoding is pinned without needing an RPC.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from cryptodesk.chain import abi
from cryptodesk.chain.vaults import VaultState
from cryptodesk.config import MAINNET_ACK, MainnetBlocked, Settings
from cryptodesk.vault import (
    ChainVault, EnvVault, ExportForbidden, KeyNotFound, KmsVault, MemoryVault,
    fingerprint, redact,
)
from cryptodesk.vault.signers import DeterministicTestSigner, paradex_order_typed_data
from cryptodesk.core import OrderRequest, OrderType, Side


class TestMainnetGuard(unittest.TestCase):

    def test_testnet_is_the_default(self):
        s = Settings.from_env({})
        self.assertFalse(s.is_mainnet)
        self.assertIn("testnet", s.resolve("paradex"))

    def test_mainnet_blocked_without_acknowledgement(self):
        s = Settings.from_env({"CRYPTODESK_NETWORK": "mainnet"})
        with self.assertRaises(MainnetBlocked):
            s.resolve("paradex")

    def test_mainnet_blocked_with_wrong_acknowledgement(self):
        s = Settings.from_env({"CRYPTODESK_NETWORK": "mainnet",
                               "CRYPTODESK_ALLOW_MAINNET": "yes"})
        with self.assertRaises(MainnetBlocked):
            s.resolve("paradex")

    def test_mainnet_unlocked_with_exact_acknowledgement(self):
        s = Settings.from_env({"CRYPTODESK_NETWORK": "mainnet",
                               "CRYPTODESK_ALLOW_MAINNET": MAINNET_ACK})
        self.assertIn("prod", s.resolve("paradex"))

    def test_read_only_venue_exempt_from_guard(self):
        """Public market data has no mainnet/testnet distinction and cannot
        move funds, so it must not be blocked."""
        s = Settings.from_env({"CRYPTODESK_NETWORK": "mainnet"})
        self.assertTrue(s.resolve("coinbase_public").startswith("https://"))

    def test_sandbox_has_no_mainnet_endpoint(self):
        s = Settings.from_env({"CRYPTODESK_NETWORK": "mainnet",
                               "CRYPTODESK_ALLOW_MAINNET": MAINNET_ACK})
        with self.assertRaises(MainnetBlocked):
            s.resolve("coinbase_sandbox")

    def test_guard_never_silently_downgrades(self):
        """Asking for mainnet and getting testnet would report fills that
        never happened - it must raise, not fall back."""
        s = Settings.from_env({"CRYPTODESK_NETWORK": "mainnet"})
        try:
            url = s.resolve("binance")
        except MainnetBlocked:
            return
        self.fail("expected MainnetBlocked, got %s" % url)


class TestVaults(unittest.TestCase):

    def test_memory_roundtrip(self):
        v = MemoryVault({"k": "secret"})
        self.assertEqual(v.reveal("k"), "secret")
        self.assertEqual(v.export_count("k"), 1)

    def test_missing_key_raises(self):
        with self.assertRaises(KeyNotFound):
            MemoryVault().reveal("nope")

    def test_ref_carries_no_key_material(self):
        v = MemoryVault({"k": "supersecret"})
        ref = v.ref("k")
        self.assertNotIn("supersecret", str(ref))
        self.assertEqual(ref.fingerprint, fingerprint("supersecret"))

    def test_use_counts_as_an_export(self):
        v = MemoryVault({"k": "secret"})
        self.assertEqual(v.use("k", lambda s: s.upper()), "SECRET")
        self.assertEqual(v.export_count("k"), 1)
        self.assertIn("reason=sign", v.audit_log()[0])

    def test_kms_refuses_export(self):
        """Running a test suite against KmsVault proves which code paths never
        touch key material."""
        with self.assertRaises(ExportForbidden):
            KmsVault({"prod": "arn:..."}).reveal("prod")

    def test_chain_vault_prefers_first_backend(self):
        chain = ChainVault(MemoryVault({"k": "strong"}), MemoryVault({"k": "weak"}))
        self.assertEqual(chain.reveal("k"), "strong")
        self.assertEqual(chain.which("k"), "memory")

    def test_chain_vault_falls_through(self):
        chain = ChainVault(MemoryVault({}), MemoryVault({"k": "found"}))
        self.assertEqual(chain.reveal("k"), "found")

    def test_env_vault_name_mapping(self):
        v = EnvVault(prefix="TEST_")
        self.assertEqual(v._var("paradex-l2"), "TEST_PARADEX_L2")

    def test_redaction_catches_hex_keys(self):
        leak = '{"key":"0x%s"}' % ("ab" * 32)
        self.assertNotIn("abab", redact(leak))
        self.assertIn("<redacted>", redact(leak))

    def test_fingerprint_is_stable_and_non_reversible(self):
        self.assertEqual(fingerprint("a"), fingerprint("a"))
        self.assertNotEqual(fingerprint("a"), fingerprint("b"))
        self.assertEqual(len(fingerprint("a")), 16)


class TestSigners(unittest.TestCase):

    def test_test_signer_is_flagged(self):
        sig = DeterministicTestSigner().sign_message({"a": 1})
        self.assertTrue(sig.is_test)

    def test_canonical_form_is_key_order_independent(self):
        s = DeterministicTestSigner()
        self.assertEqual(s.sign_message({"a": 1, "b": 2}).value,
                         s.sign_message({"b": 2, "a": 1}).value)

    def test_changing_a_field_changes_the_signature(self):
        s = DeterministicTestSigner()
        self.assertNotEqual(s.sign_message({"size": "1"}).value,
                            s.sign_message({"size": "2"}).value)

    def test_paradex_scales_to_eight_decimals(self):
        """Signing the unscaled decimal produces a valid signature over the
        wrong number - the failure verifies locally and is rejected remotely."""
        o = OrderRequest("BTC-USD-PERP", Side.BUY, OrderType.LIMIT,
                         Decimal("0.5"), Decimal("60000.0"))
        td = paradex_order_typed_data(o, "SN_SEPOLIA", timestamp_ms=1700000000000)
        self.assertEqual(td["message"]["size"], "50000000")
        self.assertEqual(td["message"]["price"], "6000000000000")
        self.assertEqual(td["message"]["side"], "1")

    def test_paradex_sell_side_encoding(self):
        o = OrderRequest("BTC-USD-PERP", Side.SELL, OrderType.LIMIT,
                         Decimal("1"), Decimal("60000.0"))
        td = paradex_order_typed_data(o, "SN_SEPOLIA")
        self.assertEqual(td["message"]["side"], "2")

    def test_paradex_market_order_signs_zero_price(self):
        """A market order must sign price 0, not omit the field - an absent
        field hashes differently."""
        o = OrderRequest("BTC-USD-PERP", Side.BUY, OrderType.MARKET, Decimal("1"))
        td = paradex_order_typed_data(o, "SN_SEPOLIA")
        self.assertEqual(td["message"]["price"], "0")


class TestRealSigners(unittest.TestCase):
    """Signature schemes verified cryptographically, not just structurally.

    Skipped when the optional library is absent, so the suite still runs on a
    bare Python install. The property under test is the one that matters: a
    signature binds the order's economic terms, so altering the size after
    signing must invalidate it.
    """

    #: Hardhat account #0 - a publicly-known test key, never fund it.
    ETH_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    ETH_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    STARK_KEY = "0x1" + "0" * 62

    @unittest.skipUnless(
        __import__("cryptodesk.vault.signers", fromlist=["AVAILABLE"])
        .AVAILABLE["eth_account"], "eth-account not installed")
    def test_eip712_signature_recovers_to_signer(self):
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        from cryptodesk.vault.signers import EthSigner

        vault = MemoryVault({"k": self.ETH_KEY})
        signer = EthSigner(vault, "k")
        self.assertEqual(signer.address(), self.ETH_ADDR)

        domain = {"name": "Exchange", "version": "1", "chainId": 1337,
                  "verifyingContract": "0x" + "00" * 20}
        types = {"Agent": [{"name": "source", "type": "string"},
                           {"name": "connectionId", "type": "bytes32"}]}
        message = {"source": "a", "connectionId": b"\x11" * 32}

        sig = signer.sign_typed_data(domain, types, message)
        self.assertFalse(sig.is_test)

        full = {"types": dict(types, EIP712Domain=[
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"}]),
                "domain": domain, "primaryType": "Agent", "message": message}
        recovered = Account.recover_message(
            encode_typed_data(full_message=full),
            signature=bytes.fromhex(sig.value.replace("0x", "")))
        self.assertEqual(recovered, self.ETH_ADDR)

    @unittest.skipUnless(
        __import__("cryptodesk.vault.signers", fromlist=["AVAILABLE"])
        .AVAILABLE["starknet_py"], "starknet-py not installed")
    def test_signature_verifies_on_stark_curve(self):
        from starknet_py.hash.utils import private_to_stark_key, verify_message_signature

        from cryptodesk.vault.signers import StarknetSigner

        vault = MemoryVault({"k": self.STARK_KEY})
        signer = StarknetSigner(vault, "k", "0x" + "ab" * 31)
        o = OrderRequest("BTC-USD-PERP", Side.BUY, OrderType.LIMIT,
                         Decimal("0.5"), Decimal("60000.0"))
        td = paradex_order_typed_data(o, "PRIVATE_SN_POTC_SEPOLIA",
                                      timestamp_ms=1700000000000)
        sig = signer.sign_typed_data(td["domain"], td["types"], td["message"])
        pub = private_to_stark_key(int(self.STARK_KEY, 16))
        self.assertTrue(verify_message_signature(
            signer._last_message_hash,
            (int(sig.components["r"]), int(sig.components["s"])), pub))

    @unittest.skipUnless(
        __import__("cryptodesk.vault.signers", fromlist=["AVAILABLE"])
        .AVAILABLE["starknet_py"], "starknet-py not installed")
    def test_altering_size_invalidates_signature(self):
        """The property the whole scheme exists for: the venue cannot change
        an order's terms without breaking its signature."""
        from starknet_py.hash.utils import private_to_stark_key, verify_message_signature

        from cryptodesk.vault.signers import StarknetSigner

        vault = MemoryVault({"k": self.STARK_KEY})
        signer = StarknetSigner(vault, "k", "0x" + "ab" * 31)
        pub = private_to_stark_key(int(self.STARK_KEY, 16))

        signed = paradex_order_typed_data(
            OrderRequest("BTC-USD-PERP", Side.BUY, OrderType.LIMIT,
                         Decimal("0.5"), Decimal("60000.0")),
            "PRIVATE_SN_POTC_SEPOLIA", timestamp_ms=1700000000000)
        sig = signer.sign_typed_data(signed["domain"], signed["types"],
                                     signed["message"])
        r, s = int(sig.components["r"]), int(sig.components["s"])

        # Re-sign a tampered order to obtain its hash, then check the original
        # signature does not verify against it.
        tampered = paradex_order_typed_data(
            OrderRequest("BTC-USD-PERP", Side.BUY, OrderType.LIMIT,
                         Decimal("0.6"), Decimal("60000.0")),
            "PRIVATE_SN_POTC_SEPOLIA", timestamp_ms=1700000000000)
        signer.sign_typed_data(tampered["domain"], tampered["types"],
                               tampered["message"])
        self.assertFalse(verify_message_signature(
            signer._last_message_hash, (r, s), pub),
            "a signature must not verify against an altered order")


class TestAbi(unittest.TestCase):

    def test_encode_address_pads_to_word(self):
        enc = abi.encode_address("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        self.assertEqual(len(enc), 64)
        self.assertTrue(enc.endswith("d8da6bf26964af9d7eed9e03e53415d37aa96045"))

    def test_encode_address_rejects_bad_length(self):
        with self.assertRaises(ValueError):
            abi.encode_address("0x1234")

    def test_decode_word(self):
        self.assertEqual(abi.decode_word("0x" + "00" * 31 + "ff"), 255)

    def test_decode_signed_negative(self):
        """Chainlink's answer is int256; reading it unsigned turns a small
        negative into ~1.16e77."""
        self.assertEqual(abi.decode_int("0x" + "ff" * 32), -1)

    def test_decode_string_dynamic(self):
        # offset=0x20, length=4, "sDAI"
        data = ("0x" + format(32, "064x") + format(4, "064x")
                + b"sDAI".hex().ljust(64, "0"))
        self.assertEqual(abi.decode_string(data), "sDAI")

    def test_decode_string_bytes32_variant(self):
        """MKR-style tokens declare symbol() as bytes32; a decoder assuming the
        dynamic layout returns garbage for them."""
        data = "0x" + b"MKR".hex().ljust(64, "0")
        self.assertEqual(abi.decode_string(data), "MKR")

    def test_every_selector_is_documented(self):
        for sel, sig in abi.SELECTORS.items():
            self.assertTrue(sel.startswith("0x") and len(sel) == 10, sel)
            self.assertIn("(", sig)


class TestVaultEconomics(unittest.TestCase):

    def test_share_price_from_state(self):
        st = VaultState(address="0x0", total_assets_raw=1180 * 10 ** 18,
                        total_supply_raw=1000 * 10 ** 18)
        self.assertEqual(st.share_price, Decimal("1.18"))

    def test_share_price_handles_differing_decimals(self):
        """A USDC-denominated vault has 6-decimal assets and 18-decimal shares;
        ignoring that misprices the share by 10^12."""
        st = VaultState(address="0x0", asset_decimals=6, share_decimals=18,
                        total_assets_raw=1_000_000 * 10 ** 6,
                        total_supply_raw=1_000_000 * 10 ** 18)
        self.assertEqual(st.share_price, Decimal("1"))

    def test_empty_vault_has_no_share_price(self):
        """Returning 1.0 would hide the first-depositor inflation risk."""
        st = VaultState(address="0x0", total_assets_raw=0, total_supply_raw=0)
        self.assertIsNone(st.share_price)
        self.assertTrue(st.is_empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
