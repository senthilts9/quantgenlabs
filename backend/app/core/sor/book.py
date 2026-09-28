"""Smart Order Routing — venue & book model.

Asset-class-agnostic: a venue is either a central-limit order book (equities,
FX, futures, crypto CEX) or a constant-product AMM (on-chain DEX). The router
consumes a normalised view so equities and digital-asset venues route through
the same optimiser.

Adapters (live) plug in behind `VenueBook.from_levels` / `from_amm`; offline
we synthesise a realistic L2 book. Fees are per-venue taker bps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class VenueType(str, Enum):
    CLOB = "clob"        # central-limit order book
    AMM = "amm"          # constant-product pool


@dataclass
class VenueBook:
    """Normalised, one-sided liquidity for the side being executed.

    `levels` is a list of (price, size) sorted best-first (ascending price for
    a BUY that lifts asks, descending for a SELL that hits bids). AMM venues
    carry reserves and expose the same marginal-cost curve via discretisation.
    """
    venue: str
    vtype: VenueType
    fee_bps: float
    levels: list[tuple[float, float]] = field(default_factory=list)
    reserve_base: float | None = None
    reserve_quote: float | None = None
    latency_ms: float = 5.0

    @classmethod
    def from_levels(cls, venue, side, bids, asks, fee_bps=1.0, latency_ms=5.0):
        raw = asks if side == Side.BUY else bids
        lv = sorted(raw, key=lambda x: x[0], reverse=(side == Side.SELL))
        return cls(venue, VenueType.CLOB, fee_bps, lv, latency_ms=latency_ms)

    @classmethod
    def from_amm(cls, venue, side, reserve_base, reserve_quote, fee_bps=30.0,
                 n_slices=200, span=0.5, latency_ms=50.0):
        """Discretise the constant-product curve into synthetic levels so the
        AMM competes in the same marginal-cost auction as the CLOBs."""
        spot = reserve_quote / reserve_base
        lv = []
        prev_cost = 0.0
        # walk notional in small steps, price each incremental slice on x*y=k
        max_base = reserve_base * span
        step = max_base / n_slices
        filled = 0.0
        k = reserve_base * reserve_quote
        for _ in range(n_slices):
            filled += step
            if side == Side.BUY:                    # buying base, quote in
                new_base = reserve_base - filled
                if new_base <= 0:
                    break
                cost = k / new_base - reserve_quote   # cumulative quote paid
            else:                                    # selling base, quote out
                new_base = reserve_base + filled
                cost = reserve_quote - k / new_base
            marg = (cost - prev_cost) / step
            prev_cost = cost
            lv.append((abs(marg), step))
        lv.sort(key=lambda x: x[0], reverse=(side == Side.SELL))
        return cls(venue, VenueType.AMM, fee_bps, lv, reserve_base, reserve_quote,
                   latency_ms)

    def best_price(self) -> float:
        return self.levels[0][0] if self.levels else float("nan")

    def depth(self) -> float:
        return sum(sz for _, sz in self.levels)


def synth_market(side: Side = Side.BUY, mid: float = 100.0, seed: int = 7):
    """A realistic multi-venue snapshot for offline demos:
    three CLOBs with different spreads/fees/depth + one AMM pool."""
    rng = np.random.default_rng(seed)

    def book(spread_bps, depth, fee, jitter):
        half = mid * spread_bps / 1e4 / 2
        top = mid + half if side == Side.BUY else mid - half
        levels = []
        px = top
        for _ in range(12):
            sz = float(depth * rng.uniform(0.5, 1.5))
            levels.append((round(px, 4), round(sz, 2)))
            px += (mid * jitter / 1e4) * (1 if side == Side.BUY else -1)
        return levels

    asks_a = book(4, 800, 1.0, 1.5)
    asks_b = book(6, 1500, 0.5, 1.2)
    asks_c = book(3, 400, 2.0, 2.5)
    venues = [
        VenueBook.from_levels("VENUE-A", side, [], asks_a, fee_bps=1.0, latency_ms=3),
        VenueBook.from_levels("VENUE-B", side, [], asks_b, fee_bps=0.5, latency_ms=8),
        VenueBook.from_levels("VENUE-C", side, [], asks_c, fee_bps=2.0, latency_ms=2),
        VenueBook.from_amm("DEX-AMM", side, reserve_base=50_000, reserve_quote=50_000 * mid,
                           fee_bps=30.0, latency_ms=60),
    ]
    if side == Side.SELL:      # rebuild as bids for a sell
        venues = [
            VenueBook.from_levels("VENUE-A", side, book(4, 800, 1.0, 1.5), [], 1.0, 3),
            VenueBook.from_levels("VENUE-B", side, book(6, 1500, 0.5, 1.2), [], 0.5, 8),
            VenueBook.from_levels("VENUE-C", side, book(3, 400, 2.0, 2.5), [], 2.0, 2),
            VenueBook.from_amm("DEX-AMM", side, 50_000, 50_000 * mid, 30.0, latency_ms=60),
        ]
    return venues, mid
