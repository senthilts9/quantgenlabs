"""Automated market makers — constant-product pricing & impermanent loss.

XRPL has a native AMM (constant-product with a continuous-auction slot);
Uniswap v2 is the same x·y=k curve. A crypto risk desk needs to price a
swap through the curve (slippage), and value an LP position against simply
holding — the "impermanent loss" that is the LP's true risk.

All functions are pure and closed-form; IL has an exact analytic form for
the constant-product curve so no simulation is needed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def spot_price(reserve_base: float, reserve_quote: float) -> float:
    """Marginal price of BASE in QUOTE terms for an x·y=k pool."""
    if reserve_base <= 0 or reserve_quote <= 0:
        raise ValueError("reserves must be positive")
    return reserve_quote / reserve_base


@dataclass(frozen=True, slots=True)
class SwapResult:
    amount_out: float
    execution_price: float
    spot_price_before: float
    price_impact: float          # fraction, exec vs spot
    fee_paid: float


def swap_out(reserve_in: float, reserve_out: float, amount_in: float,
             fee: float = 0.003) -> SwapResult:
    """Constant-product swap with fee (Uniswap-v2 math).

    dy = reserve_out − k / (reserve_in + amount_in·(1−fee)),  k = x·y.
    """
    if amount_in <= 0:
        raise ValueError("amount_in must be positive")
    spot = reserve_out / reserve_in
    ain_net = amount_in * (1.0 - fee)
    k = reserve_in * reserve_out
    new_in = reserve_in + ain_net
    amount_out = reserve_out - k / new_in
    exec_price = amount_out / amount_in
    return SwapResult(
        amount_out=amount_out,
        execution_price=exec_price,
        spot_price_before=spot,
        price_impact=exec_price / spot - 1.0,
        fee_paid=amount_in * fee,
    )


def impermanent_loss(price_ratio: float) -> float:
    """Exact IL for a 50/50 constant-product pool.

    price_ratio r = P_now / P_entry.  IL = 2·√r/(1+r) − 1  ≤ 0.
    r=1 → 0; r=4 → −20%; r=0.25 → −20% (symmetric in log-price).
    """
    r = float(price_ratio)
    if r <= 0:
        raise ValueError("price_ratio must be positive")
    return 2.0 * np.sqrt(r) / (1.0 + r) - 1.0


@dataclass(frozen=True, slots=True)
class LPValuation:
    lp_value: float          # value of the LP position now (quote units)
    hodl_value: float        # value if the LP had just held the initial mix
    impermanent_loss: float  # fraction (negative)
    fees_earned: float       # accumulated fees (quote units)
    net_vs_hodl: float       # lp_value + fees − hodl_value


def lp_value_vs_hodl(reserve_base0: float, reserve_quote0: float,
                     price_ratio: float, lp_share: float = 1.0,
                     fees_earned: float = 0.0) -> LPValuation:
    """Value an LP position after price moves by `price_ratio`.

    Uses the invariant: with price scaled by r, the pool rebalances so the
    LP holds √r less base and √r more quote. Value in quote terms tracks
    the IL curve exactly.
    """
    p0 = reserve_quote0 / reserve_base0
    hodl = lp_share * (reserve_base0 * (p0 * price_ratio) + reserve_quote0)
    lp = hodl * (1.0 + impermanent_loss(price_ratio))
    return LPValuation(
        lp_value=lp, hodl_value=hodl,
        impermanent_loss=impermanent_loss(price_ratio),
        fees_earned=fees_earned,
        net_vs_hodl=lp + fees_earned - hodl,
    )
