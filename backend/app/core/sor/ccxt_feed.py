"""Live multi-venue order books for SOR — via CCXT.

CCXT (open-source) gives a unified order-book API across 100+ venues, so the
router can allocate a real order across *actual* exchange books rather than a
synthetic snapshot. Routing the same parent order across Binance / OKX /
Kraken / KuCoin — each with its own depth, spread and taker fee — is a
genuine best-execution demonstration on real data.

Design:
  * per-venue taker fee pulled from the exchange's own market metadata;
  * failures (symbol not listed, rate limit, network) skip that venue rather
    than aborting the route;
  * `live_or_synth()` returns real venues when reachable, else the calibrated
    synthetic book — so the console/backtest never breaks offline.

CCXT is crypto-only; for traditional venues use the Databento / Alpaca / IBKR
adapters behind the same VenueBook interface (book.from_levels).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .book import Side, VenueBook, synth_market

DEFAULT_EXCHANGES = ("binance", "okx", "kraken", "kucoin")

# 20 is the one depth every default venue accepts: KuCoin rejects anything
# that isn't exactly 20 or 100, while Binance/OKX/Kraken all allow it.
DEFAULT_DEPTH = 20


def _levels(raw) -> list[tuple[float, float]]:
    """Normalise CCXT book levels to (price, amount).

    CCXT guarantees the first two entries are price and amount but NOT the
    length: Kraken returns [price, amount, timestamp], so unpacking with
    `for p, a in raw` raises ValueError on those venues. Index instead of
    unpacking, and skip malformed/zero-size levels.
    """
    out: list[tuple[float, float]] = []
    for lvl in raw or []:
        if lvl is None or len(lvl) < 2:
            continue
        try:
            price, amount = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError):
            continue
        if amount > 0 and price > 0:
            out.append((price, amount))
    return out


# Market metadata (symbol lists, precision, fees) per exchange. This is the
# expensive part of a CCXT call by a wide margin: load_markets() downloads the
# venue's ENTIRE instrument list -- thousands of symbols -- and a fresh
# exchange object was being constructed per venue per call, so a five-pair
# board was paying for twenty full market downloads and took ~10s. The market
# list changes on the order of days, so it is cached for an hour.
#
# Only the market DATA is cached, not the exchange instance. Instances carry
# mutable rate-limiter state and are not safe to share across the thread pool
# below; a fresh object seeded via set_markets() costs nothing and keeps each
# thread isolated.
_MARKETS: dict[str, tuple[float, dict]] = {}
_MARKETS_TTL = 3600.0
_MARKETS_LOCK = threading.Lock()


def _exchange(name: str):
    """A ready-to-use exchange instance with its market list populated."""
    import ccxt

    ex = getattr(ccxt, name)({"enableRateLimit": True})
    with _MARKETS_LOCK:
        hit = _MARKETS.get(name)
    if hit and time.time() - hit[0] < _MARKETS_TTL:
        ex.set_markets(hit[1])
    else:
        ex.load_markets()
        with _MARKETS_LOCK:
            _MARKETS[name] = (time.time(), ex.markets)
    return ex


def _one_venue(name: str, symbol: str, side: Side, depth: int):
    """Fetch one venue's book. Returns (VenueBook, mid) or None.

    The whole body stays inside the try: book parsing used to sit outside it,
    so a single venue returning an unexpected level shape aborted the entire
    fetch and silently fell back to synthetic -- the opposite of this module's
    "skip that venue" contract.
    """
    try:
        ex = _exchange(name)
        if symbol not in ex.markets:
            return None
        taker = ex.markets[symbol].get("taker")
        if taker is None:
            taker = ex.fees.get("trading", {}).get("taker", 0.001)
        t0 = time.perf_counter()
        try:
            ob = ex.fetch_order_book(symbol, limit=depth)
        except Exception:
            # Some venues only accept a fixed set of depths; fall back to the
            # exchange default rather than losing the venue.
            ob = ex.fetch_order_book(symbol)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        bids = _levels(ob.get("bids"))
        asks = _levels(ob.get("asks"))
        if not bids or not asks:
            return None
        return (VenueBook.from_levels(name.upper(), side, bids, asks,
                                      fee_bps=float(taker) * 1e4,
                                      latency_ms=latency_ms),
                (bids[0][0] + asks[0][0]) / 2.0)
    except Exception:
        return None


def load_venues(symbol: str = "BTC/USDT",
                exchanges: tuple[str, ...] = DEFAULT_EXCHANGES,
                side: Side = Side.BUY, depth: int = DEFAULT_DEPTH):
    """Fetch live L2 books via CCXT and normalise to VenueBook. Returns
    (venues, consolidated_mid). Requires `pip install ccxt` + network.

    Venues are polled CONCURRENTLY. Sequentially, the consolidated mid was as
    stale as the sum of every venue's latency -- the first book fetched had
    aged by the time the last arrived, so the venues being compared were not
    observed at the same instant. That is a real defect in a routing feed, not
    just a slow one.
    """
    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(exchanges))) as pool:
        futures = [pool.submit(_one_venue, n, symbol, side, depth) for n in exchanges]
        for f in futures:
            r = f.result()
            if r is not None:
                results.append(r)

    venues = [v for v, _ in results]
    mids = [m for _, m in results]
    mid = sum(mids) / len(mids) if mids else None
    return venues, mid


def live_or_synth(symbol: str = "BTC/USDT", side: Side = Side.BUY,
                  exchanges: tuple[str, ...] = DEFAULT_EXCHANGES):
    """Real venues if CCXT + network are available, else the synthetic book.
    Returns (venues, mid, source)."""
    try:
        venues, mid = load_venues(symbol, exchanges, side)
        if venues and mid:
            return venues, mid, "ccxt-live"
    except Exception:
        pass
    venues, mid = synth_market(side, mid=100.0)
    return venues, mid, "synthetic"
