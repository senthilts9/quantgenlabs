"""Public exchange market data - no API key, no account, read-only.

Every feed here is callable anonymously. That is the point: market data should
never be the reason a credential has to exist, and a read-only key that cannot
place an order is still a key that can be leaked.

Symbol mapping is handled per-feed. Venues disagree even on what to call
Bitcoin (`BTC-USD`, `XXBTZUSD`, `BTCUSDT`), and letting that leak into strategy
code means every caller reimplements the same lookup slightly differently.
Canonical symbols in this package are `BASE-QUOTE`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from ..core.types import ZERO, OrderBook, Quote, Side, dec, now_ms
from .http import DEFAULT_CLIENT, HttpClient, HttpError, TransportError


class Feed:
    """Base class for a read-only market data source."""

    name = "feed"
    base_url = ""

    def __init__(self, client: Optional[HttpClient] = None,
                 base_url: Optional[str] = None):
        self.client = client or DEFAULT_CLIENT
        if base_url:
            self.base_url = base_url

    def symbol_for(self, canonical: str) -> str:
        raise NotImplementedError

    def fetch_book(self, canonical: str, depth: int = 25) -> OrderBook:
        raise NotImplementedError

    def fetch_quote(self, canonical: str) -> Quote:
        """Default: derive a mid from the book.

        Overridden where a venue exposes a dedicated ticker, which is both
        cheaper and less likely to be rate limited than a full book pull.
        """
        book = self.fetch_book(canonical, depth=1)
        mid = book.mid
        if mid is None:
            raise TransportError("%s: no mid available for %s" % (self.name, canonical))
        return Quote(canonical, mid, book.timestamp_ms, self.name)

    def healthy(self) -> bool:
        try:
            self.fetch_quote("BTC-USD")
            return True
        except (HttpError, TransportError, KeyError, ValueError):
            return False


class CoinbasePublicFeed(Feed):
    """Coinbase Exchange public REST.

    Note this is the *Exchange* public feed, not Advanced Trade. It needs no
    authentication and cannot place orders, so it is safe to call from anywhere
    in the system without the mainnet guard applying.
    """

    name = "coinbase"
    base_url = "https://api.exchange.coinbase.com"

    def symbol_for(self, canonical: str) -> str:
        return canonical.upper().replace("_", "-")

    def fetch_book(self, canonical: str, depth: int = 25) -> OrderBook:
        product = self.symbol_for(canonical)
        # level=2 is the aggregated top-50; level=3 is the full order-by-order
        # book, which is far heavier and never needed for a mid or a sweep.
        level = 1 if depth <= 1 else 2
        raw = self.client.get(
            "%s/products/%s/book" % (self.base_url, product), params={"level": level}
        )
        # Coinbase levels are [price, size, num_orders]; we ignore the third.
        bids = [(lv[0], lv[1]) for lv in raw.get("bids", [])[:depth]]
        asks = [(lv[0], lv[1]) for lv in raw.get("asks", [])[:depth]]
        return OrderBook.from_levels(canonical, bids, asks, source=self.name)

    def fetch_quote(self, canonical: str) -> Quote:
        product = self.symbol_for(canonical)
        raw = self.client.get("%s/products/%s/ticker" % (self.base_url, product))
        price = raw.get("price")
        if price is None:
            raise TransportError("coinbase: no price for %s" % canonical)
        return Quote(canonical, dec(price), now_ms(), self.name)

    def products(self) -> List[Dict]:
        return self.client.get("%s/products" % self.base_url)


class KrakenPublicFeed(Feed):
    """Kraken public REST.

    Kraken wraps every response in `{"error": [...], "result": {...}}` and
    returns HTTP 200 even for logical errors, so the error array has to be
    checked explicitly - a bare status check would read failures as successes.
    """

    name = "kraken"
    base_url = "https://api.kraken.com/0/public"

    #: Kraken uses its own asset codes; XBT for BTC is the well-known trap.
    _SYMBOLS = {
        "BTC-USD": "XBTUSD", "ETH-USD": "ETHUSD", "SOL-USD": "SOLUSD",
        "BTC-USDT": "XBTUSDT", "ETH-USDT": "ETHUSDT",
    }

    def symbol_for(self, canonical: str) -> str:
        return self._SYMBOLS.get(canonical.upper(), canonical.upper().replace("-", ""))

    def _result(self, raw) -> Dict:
        if isinstance(raw, dict) and raw.get("error"):
            raise TransportError("kraken: %s" % "; ".join(raw["error"]))
        return raw.get("result", {}) if isinstance(raw, dict) else {}

    def fetch_book(self, canonical: str, depth: int = 25) -> OrderBook:
        pair = self.symbol_for(canonical)
        raw = self.client.get("%s/Depth" % self.base_url,
                              params={"pair": pair, "count": depth})
        result = self._result(raw)
        if not result:
            raise TransportError("kraken: empty result for %s" % pair)
        # Kraken echoes back its own normalised pair name (XXBTZUSD), which
        # does not match what we asked for - take whatever single key is there.
        entry = next(iter(result.values()))
        bids = [(lv[0], lv[1]) for lv in entry.get("bids", [])]
        asks = [(lv[0], lv[1]) for lv in entry.get("asks", [])]
        return OrderBook.from_levels(canonical, bids, asks, source=self.name)


class BinanceTestnetFeed(Feed):
    """Binance Spot testnet public data.

    The testnet has its own genuinely separate order book driven by testnet
    participants. Prices drift from mainnet - sometimes a long way - which is
    a feature when testing: it exercises the price-band and staleness paths
    that a mainnet-mirrored feed never would.
    """

    name = "binance_testnet"
    base_url = "https://testnet.binance.vision"

    def symbol_for(self, canonical: str) -> str:
        sym = canonical.upper().replace("-", "")
        # The testnet lists USDT pairs, not USD.
        return sym.replace("USD", "USDT") if sym.endswith("USD") else sym

    def fetch_book(self, canonical: str, depth: int = 20) -> OrderBook:
        # Binance only accepts specific depth values; snap up to the next legal one.
        legal = [5, 10, 20, 50, 100, 500, 1000, 5000]
        limit = next((d for d in legal if d >= depth), 100)
        raw = self.client.get("%s/api/v3/depth" % self.base_url,
                              params={"symbol": self.symbol_for(canonical), "limit": limit})
        return OrderBook.from_levels(
            canonical, raw.get("bids", []), raw.get("asks", []), source=self.name
        )

    def fetch_quote(self, canonical: str) -> Quote:
        raw = self.client.get("%s/api/v3/ticker/price" % self.base_url,
                              params={"symbol": self.symbol_for(canonical)})
        return Quote(canonical, dec(raw["price"]), now_ms(), self.name)


#: Feeds that need no credentials at all, in preference order.
PUBLIC_FEEDS = {
    "coinbase": CoinbasePublicFeed,
    "kraken": KrakenPublicFeed,
    "binance_testnet": BinanceTestnetFeed,
}


def synthetic_book(market: str, mid: Decimal = Decimal("60000"),
                   spread_bps: Decimal = Decimal("2"),
                   levels: int = 10,
                   depth_per_level: Decimal = Decimal("0.5")) -> OrderBook:
    """A deterministic book for offline work.

    Not a simulation of anything - it is a fixture with a plausible shape, so
    the validation and execution paths can be exercised in CI and on a plane.
    Its `source` is `"synthetic"` so no caller can mistake it for real depth.
    """
    mid = dec(mid)
    half = mid * dec(spread_bps) / Decimal(20000)
    bids, asks = [], []
    for i in range(levels):
        step = half * (1 + i)
        # Size grows with distance, as a real book's does.
        size = depth_per_level * (1 + Decimal(i) / 2)
        bids.append((mid - step, size))
        asks.append((mid + step, size))
    return OrderBook.from_levels(market, bids, asks, source="synthetic")


def best_effort_book(
    market: str,
    feeds: Optional[List[Feed]] = None,
    depth: int = 25,
    fallback_mid: Decimal = Decimal("60000"),
) -> OrderBook:
    """First feed that answers wins; synthetic book if none do.

    Degrading to synthetic rather than raising is a deliberate call for a
    *research* stack: the console and backtests keep working offline. It is the
    wrong call for live order routing, which is why `OrderBook.source` is
    always set - execution code checks it and refuses to trade on `synthetic`.
    """
    for feed in (feeds or [f() for f in PUBLIC_FEEDS.values()]):
        try:
            book = feed.fetch_book(market, depth=depth)
            if book.bids and book.asks and not book.is_crossed:
                return book
        except Exception:
            continue
    return synthetic_book(market, fallback_mid)


def consolidated_mid(market: str, feeds: Optional[List[Feed]] = None) -> Optional[Quote]:
    """Median mid across venues.

    Median, not mean: one venue printing a stale or manipulated price moves a
    mean and does not move a median. With two sources it degenerates to the
    mean, which is why three feeds are configured by default.
    """
    prices: List[Decimal] = []
    sources: List[str] = []
    for feed in (feeds or [f() for f in PUBLIC_FEEDS.values()]):
        try:
            q = feed.fetch_quote(market)
            if q.price > ZERO:
                prices.append(q.price)
                sources.append(feed.name)
        except Exception:
            continue
    if not prices:
        return None
    prices.sort()
    n = len(prices)
    median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return Quote(market, median, now_ms(), "consolidated(%s)" % ",".join(sources))
