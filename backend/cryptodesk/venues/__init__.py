"""Venue adapters: paper, CEX testnets and signed-order DEXes."""
from .base import (
    Fill, NotAuthenticated, OrderAck, VenueAdapter, VenueError, VenueUnsupported,
)
from .binance import BinanceTestnetVenue
from .coinbase import CoinbasePublicVenue, CoinbaseSandboxVenue
from .coinbase_exchange import CoinbaseExchangeVenue
from .hyperliquid import HyperliquidVenue
from .paper import PaperVenue, RestingOrder
from .paradex import ParadexVenue

VENUES = {
    "paper": PaperVenue,
    "binance": BinanceTestnetVenue,
    "paradex": ParadexVenue,
    "hyperliquid": HyperliquidVenue,
    "coinbase_public": CoinbasePublicVenue,
    "coinbase_sandbox": CoinbaseSandboxVenue,
    "coinbase_exchange": CoinbaseExchangeVenue,
}

__all__ = [
    "Fill", "NotAuthenticated", "OrderAck", "VenueAdapter", "VenueError",
    "VenueUnsupported", "BinanceTestnetVenue", "CoinbasePublicVenue",
    "CoinbaseSandboxVenue", "CoinbaseExchangeVenue", "HyperliquidVenue", "PaperVenue", "RestingOrder",
    "ParadexVenue", "VENUES",
]
