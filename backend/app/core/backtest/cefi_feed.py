"""CeFi perpetual/funding adapter — the derivatives leg XRPL doesn't have.

XRPL is spot + AMM only; perpetuals and funding live on centralised venues.
This pulls the real perp mark and funding rate so the cash-and-carry basis
backtest has an honest short leg. Binance USD-M endpoints are public and
keyless for read-only market data:

  premiumIndex   → markPrice, lastFundingRate (per 8h)
  fundingRate    → historical funding series (for backtests)

Network required; offline callers fall back to the simulated series.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

BINANCE_FAPI = "https://fapi.binance.com"


def _get(path: str, params: dict, timeout: float = 10.0):
    url = f"{BINANCE_FAPI}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:      # network required
        return json.loads(r.read())


def perp_source(symbol: str = "XRPUSDT"):
    """Return a callable() -> (mark_price, funding_rate) for record()."""
    def _snap():
        d = _get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(d["markPrice"]), float(d["lastFundingRate"])
    return _snap


def funding_history(symbol: str = "XRPUSDT", limit: int = 1000) -> list[dict]:
    """Historical funding (per 8h) — the series a basis backtest consumes."""
    return _get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})


def mark_klines(symbol: str = "XRPUSDT", interval: str = "1h", limit: int = 1000):
    """Historical perp mark-price candles for the price leg."""
    rows = _get("/fapi/v1/markPriceKlines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    return [(int(r[0]), float(r[4])) for r in rows]   # (open_time_ms, close)
