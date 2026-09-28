"""Network selection and the mainnet guard.

The single most expensive bug in a trading system is pointing a *working*
order path at production by accident. Every venue in this package resolves its
base URL through `resolve(...)`, which refuses to hand back a mainnet endpoint
unless the operator has opted in twice: `CRYPTODESK_NETWORK=mainnet` **and**
`CRYPTODESK_ALLOW_MAINNET=i-understand-this-is-real-money`.

Two env vars rather than one is deliberate. A single flag gets flipped during
debugging and left on; a flag whose value is a sentence you have to type out
does not get flipped by accident, and it shows up in `git diff` and shell
history as something a reviewer will notice.

The guard is enforced at URL resolution rather than at request time so that a
misconfigured venue fails at construction — before any key is loaded, signed
with, or sent anywhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

TESTNET = "testnet"
MAINNET = "mainnet"

#: The exact value `CRYPTODESK_ALLOW_MAINNET` must hold to unlock live trading.
MAINNET_ACK = "i-understand-this-is-real-money"


class MainnetBlocked(RuntimeError):
    """Raised when a mainnet URL is requested without the explicit acknowledgement."""


@dataclass(frozen=True)
class VenueEndpoints:
    """Per-network URLs for one venue.

    `mainnet_rest` is Optional because several venues in this package are
    testnet-only by design (the paper engine) or are sandboxes with no
    production counterpart worth exposing here.
    """

    name: str
    testnet_rest: str
    testnet_ws: Optional[str] = None
    mainnet_rest: Optional[str] = None
    mainnet_ws: Optional[str] = None
    #: Free-form notes surfaced in the CLI so operators see the caveats.
    notes: str = ""
    #: A real path to probe for liveness. Probing the bare base URL returns 404
    #: on most venues, which proves the host is up but reads like a failure in
    #: a health report - so each venue names an endpoint that actually answers.
    health_path: str = ""


# Endpoints verified against vendor documentation, September 2026.
ENDPOINTS: Dict[str, VenueEndpoints] = {
    "paradex": VenueEndpoints(
        name="paradex",
        testnet_rest="https://api.testnet.paradex.trade/v1",
        testnet_ws="wss://ws.api.testnet.paradex.trade/v1",
        mainnet_rest="https://api.prod.paradex.trade/v1",
        mainnet_ws="wss://ws.api.prod.paradex.trade/v1",
        notes="Starknet perps DEX. Real matching engine on testnet. "
              "Onboarding + JWT required; orders are signed with Starknet typed data.",
        health_path="/markets",
    ),
    "hyperliquid": VenueEndpoints(
        name="hyperliquid",
        testnet_rest="https://api.hyperliquid-testnet.xyz",
        testnet_ws="wss://api.hyperliquid-testnet.xyz/ws",
        mainnet_rest="https://api.hyperliquid.xyz",
        mainnet_ws="wss://api.hyperliquid.xyz/ws",
        notes="Real matching engine on testnet. EIP-712 signed actions. "
              "Testnet faucet historically requires a prior mainnet deposit.",
        health_path="/info",
    ),
    "binance": VenueEndpoints(
        name="binance",
        testnet_rest="https://testnet.binance.vision",
        testnet_ws="wss://stream.testnet.binance.vision",
        mainnet_rest="https://api.binance.com",
        mainnet_ws="wss://stream.binance.com:9443",
        notes="Spot testnet with a real matching engine. Free API keys, "
              "no mainnet account needed. Balances reset periodically.",
        health_path="/api/v3/ping",
    ),
    "coinbase_sandbox": VenueEndpoints(
        name="coinbase_sandbox",
        testnet_rest="https://api-sandbox.coinbase.com/api/v3/brokerage",
        mainnet_rest=None,  # deliberately unreachable: see notes
        notes="MOCKED. Static pre-defined responses, Accounts/Orders only, no "
              "auth, no matching engine. Use for request-shape and error-path "
              "conformance ONLY. Coinbase has no real-money-free trading venue; "
              "production Advanced Trade keys move real funds.",
        health_path="/accounts",
    ),
    "coinbase_exchange": VenueEndpoints(
        name="coinbase_exchange",
        testnet_rest="https://api-public.sandbox.exchange.coinbase.com",
        testnet_ws="wss://ws-feed-public.sandbox.exchange.coinbase.com",
        mainnet_rest="https://api.exchange.coinbase.com",
        mainnet_ws="wss://ws-feed.exchange.coinbase.com",
        notes="Coinbase EXCHANGE sandbox - a real matching engine over a subset "
              "of production order books, with fake funds. Not to be confused "
              "with the Advanced Trade sandbox, which returns static fixtures. "
              "Exposes L3 order-by-order depth, so queue position is knowable.",
        health_path="/products",
    ),
    "coinbase_public": VenueEndpoints(
        name="coinbase_public",
        # Public market data is identical on either network - there is no
        # "testnet" order book, so both fields point at the real read-only feed.
        testnet_rest="https://api.exchange.coinbase.com",
        mainnet_rest="https://api.exchange.coinbase.com",
        notes="Read-only public market data. No API key, no account, no "
              "order placement. Safe to call unconditionally.",
        health_path="/products/BTC-USD/ticker",
    ),
}


@dataclass
class Settings:
    """Resolved runtime configuration.

    Built from the environment once at startup; pass it explicitly rather than
    reading `os.environ` deep in the call stack, so tests can construct an
    isolated Settings without mutating process state.
    """

    network: str = TESTNET
    mainnet_ack: str = ""
    #: Venues whose market data is read-only and therefore exempt from the guard.
    read_only_venues: frozenset = field(
        default_factory=lambda: frozenset({"coinbase_public"})
    )

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Settings":
        src = os.environ if env is None else env
        return cls(
            network=src.get("CRYPTODESK_NETWORK", TESTNET).strip().lower(),
            mainnet_ack=src.get("CRYPTODESK_ALLOW_MAINNET", "").strip(),
        )

    @property
    def is_mainnet(self) -> bool:
        return self.network == MAINNET

    @property
    def mainnet_unlocked(self) -> bool:
        return self.is_mainnet and self.mainnet_ack == MAINNET_ACK

    def resolve(self, venue: str, ws: bool = False) -> str:
        """Return the base URL for `venue`, enforcing the mainnet guard.

        Raises MainnetBlocked rather than silently falling back to testnet: a
        caller who asked for mainnet and got testnet would report fills that
        never happened.
        """
        try:
            ep = ENDPOINTS[venue]
        except KeyError:
            raise KeyError(
                "unknown venue %r; known venues: %s"
                % (venue, ", ".join(sorted(ENDPOINTS)))
            )

        if not self.is_mainnet or venue in self.read_only_venues:
            url = ep.testnet_ws if ws else ep.testnet_rest
            if url is None:
                raise MainnetBlocked(
                    "venue %r has no %s testnet endpoint"
                    % (venue, "websocket" if ws else "REST")
                )
            return url

        if not self.mainnet_unlocked:
            raise MainnetBlocked(
                "refusing to resolve a mainnet URL for %r.\n"
                "CRYPTODESK_NETWORK=mainnet is set, but CRYPTODESK_ALLOW_MAINNET "
                "is %r.\nSet it to exactly %r to trade real funds."
                % (venue, self.mainnet_ack, MAINNET_ACK)
            )

        url = ep.mainnet_ws if ws else ep.mainnet_rest
        if url is None:
            raise MainnetBlocked(
                "venue %r has no mainnet endpoint (it is a sandbox/mock venue)" % venue
            )
        return url


def describe_guard(settings: Optional[Settings] = None) -> str:
    """One-line banner for the CLI so the active network is never a guess."""
    s = settings or Settings.from_env()
    if not s.is_mainnet:
        return "network=TESTNET  (mainnet locked - no real funds reachable)"
    if s.mainnet_unlocked:
        return "network=MAINNET  *** REAL FUNDS UNLOCKED ***"
    return "network=mainnet requested but LOCKED (missing acknowledgement)"
