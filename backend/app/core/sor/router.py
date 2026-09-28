"""Smart Order Router — cost-minimising allocation across venues.

Walking a limit-order book has convex, increasing marginal cost, so the
cost-minimising split of a parent order is obtained by a marginal-cost
merge: pool every venue's levels (price adjusted for taker fee), sort by
effective marginal price, and fill from cheapest until the order is done —
optionally weighting each venue's usable size by an ML fill-probability.

This is the same water-filling logic a production SOR uses; we report the
result against two benchmarks a desk actually compares to — top-of-book
sweep of the single best venue, and naive pro-rata — in basis points of
implementation shortfall vs arrival mid.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .book import Side, VenueBook


@dataclass
class Fill:
    venue: str
    qty: float
    avg_price: float
    fee: float


@dataclass
class RoutingResult:
    side: Side
    target_qty: float
    filled_qty: float
    vwap: float                      # fee-inclusive execution VWAP
    arrival_mid: float
    slippage_bps: float              # implementation shortfall vs arrival mid
    fees_bps: float
    fills: list[Fill]
    allocation: dict                 # venue -> qty
    benchmarks: dict                 # name -> slippage_bps

    def summary(self) -> dict:
        return {
            "side": self.side.value, "target_qty": self.target_qty,
            "filled_qty": round(self.filled_qty, 4),
            "vwap": round(self.vwap, 6), "arrival_mid": self.arrival_mid,
            "slippage_bps": round(self.slippage_bps, 3),
            "fees_bps": round(self.fees_bps, 3),
            "allocation": {k: round(v, 2) for k, v in self.allocation.items()},
            "benchmarks_bps": {k: round(v, 3) for k, v in self.benchmarks.items()},
            "improvement_vs_best_single_bps": round(
                self.benchmarks.get("best_single_venue", 0) - self.slippage_bps, 3),
        }


def _eff_price(price: float, fee_bps: float, side: Side) -> float:
    adj = price * (1 + fee_bps / 1e4) if side == Side.BUY else price * (1 - fee_bps / 1e4)
    return adj


def _execute_merged(order: float, venues: list[VenueBook], side: Side,
                    fill_prob: dict[str, float] | None):
    """Marginal-cost merge across all venues' levels."""
    heap = []   # (eff_price, venue, size)
    for v in venues:
        p = (fill_prob or {}).get(v.venue, 1.0)
        for price, size in v.levels:
            heap.append((_eff_price(price, v.fee_bps, side), v.venue, size * p, price, v.fee_bps))
    heap.sort(key=lambda x: x[0], reverse=(side == Side.SELL))

    remaining = order
    alloc: dict[str, float] = {}
    notional = 0.0
    fee_paid = 0.0
    raw_notional = 0.0
    for eff, venue, size, raw_px, fee_bps in heap:
        if remaining <= 1e-12:
            break
        take = min(size, remaining)
        remaining -= take
        alloc[venue] = alloc.get(venue, 0.0) + take
        raw_notional += take * raw_px
        fee_paid += take * raw_px * fee_bps / 1e4
        notional += take * raw_px + (take * raw_px * fee_bps / 1e4) * (1 if side == Side.BUY else -1)
    filled = order - remaining
    vwap = notional / filled if filled else float("nan")
    return alloc, vwap, filled, fee_paid, raw_notional


def _slippage_bps(vwap: float, mid: float, side: Side) -> float:
    return (vwap / mid - 1) * 1e4 * (1 if side == Side.BUY else -1)


def route(order_qty: float, venues: list[VenueBook], side: Side, arrival_mid: float,
          fill_prob: dict[str, float] | None = None) -> RoutingResult:
    alloc, vwap, filled, fee_paid, raw_notional = _execute_merged(
        order_qty, venues, side, fill_prob)
    slip = _slippage_bps(vwap, arrival_mid, side)
    fees_bps = (fee_paid / raw_notional) * 1e4 if raw_notional else 0.0

    # --- benchmarks a desk compares to ---
    # (1) sweep only the single best venue (by top-of-book)
    best_v = min(venues, key=lambda v: v.best_price()) if side == Side.BUY \
        else max(venues, key=lambda v: v.best_price())
    _, v1, f1, _, _ = _execute_merged(order_qty, [best_v], side, fill_prob)
    bench_single = _slippage_bps(v1, arrival_mid, side) if f1 else float("inf")
    # (2) naive pro-rata by top-of-book depth
    total_depth = sum(v.depth() for v in venues)
    pr_notional = 0.0
    pr_filled = 0.0
    for v in venues:
        share = order_qty * v.depth() / total_depth
        a, vw, ff, _, _ = _execute_merged(share, [v], side, fill_prob)
        if ff:
            pr_notional += vw * ff
            pr_filled += ff
    v2 = pr_notional / pr_filled if pr_filled else float("nan")
    bench_pro = _slippage_bps(v2, arrival_mid, side)

    fills = [Fill(k, q, arrival_mid, 0.0) for k, q in alloc.items()]
    return RoutingResult(side, order_qty, filled, vwap, arrival_mid, slip, fees_bps,
                         fills, alloc,
                         {"best_single_venue": bench_single, "pro_rata": bench_pro})
