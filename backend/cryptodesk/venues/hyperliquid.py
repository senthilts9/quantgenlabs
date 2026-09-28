"""Hyperliquid testnet.

An EVM-signed perps venue with a real matching engine on testnet. Included as
the counterpoint to Paradex: same *category* of venue - on-chain settlement,
client-signed orders - but a different signing scheme (EIP-712 over a msgpack
action hash rather than SNIP-12 typed data) and a different API idiom (a single
`/info` POST endpoint discriminated by a `type` field, instead of REST paths).

Market data is public and works with no credentials. Trading is gated behind a
deliberate limitation: producing a valid Hyperliquid signature requires hashing
the action with the venue's exact msgpack encoding, and reimplementing that
from documentation is the kind of thing that produces signatures which verify
locally and fail remotely. `place_order` therefore raises with a pointer to the
official SDK rather than shipping a plausible-looking guess.

One practical note for testing: the testnet faucet has historically required a
prior mainnet deposit, so a brand-new wallet may not be fundable. Binance
testnet and the local paper engine have no such gate.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from ..config import Settings
from ..core.instruments import Instrument, InstrumentRegistry, MarketState, SettlementKind
from ..core.types import (
    ZERO, AccountState, FundingRate, OrderBook, OrderRequest, Position, Quote,
    dec, now_ms,
)
from ..marketdata.http import DEFAULT_CLIENT, HttpClient, HttpError
from ..vault.base import KeyVault, Signer
from .base import OrderAck, VenueAdapter, VenueError, VenueUnsupported


class HyperliquidVenue(VenueAdapter):
    """Hyperliquid testnet - public data now, signed trading via the SDK."""

    name = "hyperliquid"
    supports_trading = False  # see module docstring
    has_matching_engine = True
    requires_signature = True

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[InstrumentRegistry] = None,
        client: Optional[HttpClient] = None,
        vault: Optional[KeyVault] = None,
        signer: Optional[Signer] = None,
        wallet_address: Optional[str] = None,
        store=None,
    ):
        super().__init__(settings, registry, store=store)
        self.client = client or DEFAULT_CLIENT
        self.base_url = self.settings.resolve(self.name)
        self.vault = vault
        self.signer = signer
        self.wallet_address = wallet_address

    def _info(self, payload: Dict) -> object:
        """POST to `/info` - the single read endpoint for everything."""
        return self.client.post("%s/info" % self.base_url, body=payload)

    def _coin(self, market: str) -> str:
        """`BTC-USD-PERP` -> `BTC`. Hyperliquid names perps by base asset alone."""
        return market.upper().split("-")[0]

    def fetch_instruments(self) -> List[Instrument]:
        """Specs from `meta`.

        Hyperliquid publishes `szDecimals` (size decimal places) and a max
        leverage per asset, but not a tick size - price precision is defined as
        5 significant figures, which does not map onto a fixed tick. The
        conversion below picks a tick from `szDecimals` as a conservative
        approximation; the venue remains the authority.
        """
        raw = self._info({"type": "meta"})
        universe = (raw.get("universe") or []) if isinstance(raw, dict) else []
        out: List[Instrument] = []
        for a in universe:
            try:
                name = a["name"]
                sz_dec = int(a.get("szDecimals", 3))
                step = Decimal(1).scaleb(-sz_dec)
                out.append(Instrument(
                    symbol="%s-USD-PERP" % name,
                    base=name, quote="USD",
                    kind=SettlementKind.PERPETUAL,
                    state=(MarketState.PAUSED if a.get("isDelisted")
                           else MarketState.OPEN),
                    tick_size=Decimal("0.01"),
                    step_size=step, min_size=step,
                    max_size=Decimal("1000000"),
                    min_notional=Decimal("10"),
                    max_leverage=dec(a.get("maxLeverage", 20)),
                    price_band_bps=Decimal("800"),
                    maker_fee_bps=Decimal("1"), taker_fee_bps=Decimal("3.5"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def fetch_book(self, market: str, depth: int = 20) -> OrderBook:
        raw = self._info({"type": "l2Book", "coin": self._coin(market)})
        levels = (raw.get("levels") or [[], []]) if isinstance(raw, dict) else [[], []]
        # levels[0] is bids, levels[1] asks; each entry is {"px","sz","n"}.
        bids = [(lv["px"], lv["sz"]) for lv in levels[0][:depth]]
        asks = [(lv["px"], lv["sz"]) for lv in levels[1][:depth]]
        return OrderBook.from_levels(market, bids, asks, source=self.name)

    def fetch_quote(self, market: str) -> Quote:
        """Oracle/mark price from `allMids`, falling back to the book mid."""
        raw = self._info({"type": "allMids"})
        coin = self._coin(market)
        if isinstance(raw, dict) and coin in raw:
            return Quote(market, dec(raw[coin]), now_ms(), "%s:mid" % self.name)
        book = self.fetch_book(market, depth=1)
        if book.mid is None:
            raise VenueError("hyperliquid: no price for %s" % market)
        return Quote(market, book.mid, book.timestamp_ms, self.name)

    def fetch_account(self) -> AccountState:
        """Clearinghouse state for a wallet address.

        Read-only and unauthenticated - any address's state is public, which is
        a property of an on-chain venue worth noticing: position privacy does
        not exist here the way it does on a CEX.
        """
        if not self.wallet_address:
            raise VenueError("hyperliquid: wallet_address required")
        raw = self._info({"type": "clearinghouseState", "user": self.wallet_address})
        if not isinstance(raw, dict):
            raise VenueError("hyperliquid: unexpected clearinghouse response")
        margin = raw.get("marginSummary") or {}
        state = AccountState(
            account_id=self.wallet_address,
            free_collateral=dec(raw.get("withdrawable", "0")),
            total_collateral=dec(margin.get("accountValue", "0")),
        )
        for ap in raw.get("assetPositions") or []:
            p = ap.get("position") or {}
            size = dec(p.get("szi", "0"))
            if size == ZERO:
                continue
            market = "%s-USD-PERP" % p.get("coin", "")
            state.positions[market] = Position(
                market=market, size=size,
                entry_price=dec(p.get("entryPx") or "0"),
                mark_price=dec((p.get("positionValue") or "0")) / abs(size)
                if size != ZERO and p.get("positionValue") else ZERO,
                leverage=dec((p.get("leverage") or {}).get("value", 1)),
            )
        return state

    def funding_rate(self, market: str) -> Optional[Decimal]:
        """Current funding rate - the perp-specific cost of carry.

        Included because funding is the thing that makes a perp position's PnL
        diverge from spot over time, and any strategy holding overnight has to
        price it.
        """
        raw = self._info({"type": "metaAndAssetCtxs"})
        if not isinstance(raw, list) or len(raw) < 2:
            return None
        meta, ctxs = raw[0], raw[1]
        names = [a.get("name") for a in (meta.get("universe") or [])]
        coin = self._coin(market)
        if coin not in names:
            return None
        ctx = ctxs[names.index(coin)]
        funding = ctx.get("funding")
        return dec(funding) if funding is not None else None

    def market_stats(self, markets: Optional[List[str]] = None) -> List["PerpStats"]:
        """Perp board for this venue, on **this adapter's guarded network**.

        Parsing lives in `marketdata.perps` so the same sign conventions serve
        both this (testnet, order-capable) path and the public read-only feed.
        Note the network: testnet open interest is a rounding error next to
        mainnet's, so a caller rendering a board almost always wants
        `marketdata.perps.fetch_perp_stats` instead.
        """
        from ..marketdata.perps import parse_meta_ctxs
        raw = self._info({"type": "metaAndAssetCtxs"})
        network = "mainnet" if self.settings.is_mainnet else "testnet"
        return parse_meta_ctxs(raw, network, markets)

    def fetch_funding_rate(self, market: str) -> Optional[FundingRate]:
        """Wraps `funding_rate` in the common type.

        This venue settles funding **hourly**, not every eight hours, so the
        same numeric rate is eight times the annualised cost it would be
        elsewhere - which is exactly why the period travels with the rate
        rather than being assumed by the consumer.
        """
        rate = self.funding_rate(market)
        if rate is None:
            return None
        return FundingRate(market=market, rate=rate, period_hours=1,
                           source="hyperliquid")

    def place_order(self, request: OrderRequest) -> OrderAck:
        raise VenueUnsupported(
            "Hyperliquid order signing requires the venue's exact msgpack action "
            "encoding to build the EIP-712 connectionId. Reimplementing it from "
            "docs yields signatures that verify locally and fail at the venue.\n"
            "Use the official SDK (pip install hyperliquid-python-sdk) for order "
            "entry, or route to `paper` / `binance` for a matched testnet fill.\n"
            "Market data, instruments, funding and account state above all work "
            "without credentials."
        )
