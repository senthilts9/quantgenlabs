"""Leverage, margin, and liquidation — the risk that ends crypto desks.

Linear (USDT-margined) perpetual convention. A position is liquidated when
account equity falls to the maintenance-margin requirement. This module
returns the liquidation price, the bankruptcy price (equity = 0), the live
margin ratio, and distance-to-liquidation — the numbers a risk system
alerts on before the exchange auto-deleverages you.

Inverse (coin-margined) contracts invert the payoff; noted where it bites.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarginState:
    liquidation_price: float
    bankruptcy_price: float
    margin_ratio: float           # maintenance margin / equity; ≥1 ⇒ liquidated
    distance_to_liq: float        # fraction move from mark to liq (signed)
    unrealised_pnl: float
    equity: float


def liquidation_price(entry: float, leverage: float, is_long: bool,
                      maintenance_margin_rate: float = 0.005) -> float:
    """Liquidation price for an isolated-margin linear perp.

    Long : P = entry·(1 − 1/L) / (1 − mmr)
    Short: P = entry·(1 + 1/L) / (1 + mmr)
    Derived from equity(P) = maintenance_margin(P).
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    mmr = maintenance_margin_rate
    if is_long:
        return entry * (1.0 - 1.0 / leverage) / (1.0 - mmr)
    return entry * (1.0 + 1.0 / leverage) / (1.0 + mmr)


def bankruptcy_price(entry: float, leverage: float, is_long: bool) -> float:
    """Price at which equity hits zero (no maintenance buffer)."""
    if is_long:
        return entry * (1.0 - 1.0 / leverage)
    return entry * (1.0 + 1.0 / leverage)


def margin_state(entry: float, mark: float, qty: float, leverage: float,
                 is_long: bool, maintenance_margin_rate: float = 0.005) -> MarginState:
    """Full margin snapshot for a live position (qty in base units)."""
    side = 1.0 if is_long else -1.0
    notional_entry = abs(qty) * entry
    initial_margin = notional_entry / leverage
    upnl = side * qty * (mark - entry) if is_long else side * abs(qty) * (mark - entry)
    upnl = side * abs(qty) * (mark - entry)
    equity = initial_margin + upnl
    maint = maintenance_margin_rate * abs(qty) * mark
    liq = liquidation_price(entry, leverage, is_long, maintenance_margin_rate)
    return MarginState(
        liquidation_price=liq,
        bankruptcy_price=bankruptcy_price(entry, leverage, is_long),
        margin_ratio=maint / equity if equity > 0 else float("inf"),
        distance_to_liq=(liq - mark) / mark,
        unrealised_pnl=upnl,
        equity=equity,
    )


def max_leverage_for_liq_buffer(entry: float, is_long: bool, buffer: float,
                                maintenance_margin_rate: float = 0.005) -> float:
    """Largest leverage keeping the liq price at least `buffer` away from entry.

    A pre-trade risk control: "don't let me size into a position that
    liquidates on a `buffer` move." Inverts the liq-price formula for L.
    """
    mmr = maintenance_margin_rate
    if is_long:
        # entry(1-1/L)/(1-mmr) = entry(1-buffer)  → solve for L
        target = (1.0 - buffer) * (1.0 - mmr)
        return 1.0 / (1.0 - target)
    target = (1.0 + buffer) * (1.0 + mmr)
    return 1.0 / (target - 1.0)
