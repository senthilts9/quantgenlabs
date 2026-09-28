"""Perpetual futures — funding, basis, and carry.

The single most crypto-specific instrument a trading desk touches. A perp
has no expiry; the funding mechanism is what tethers its mark price to the
underlying index. A quant/risk desk cares about three things this module
computes:

  * the funding rate itself (exchange clamp formula)
  * funding PnL / carry on a held position (the dominant PnL term for
    basis and cash-and-carry books)
  * the annualised basis, which is the arbitrage-free anchor funding
    should track

Conventions: funding is paid every `interval_hours` (8h on most venues,
so 3×/day). A POSITIVE funding rate means longs pay shorts.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FUNDING_INTERVALS_PER_DAY = 3          # 8h venues
DEFAULT_CLAMP = 0.0005                  # ±0.05% per interval


def funding_rate(premium_index: float, interest_rate: float = 0.0001,
                 clamp: float = DEFAULT_CLAMP) -> float:
    """Exchange-style funding: F = premium + clamp(interest - premium).

    `premium_index` is the time-weighted (impact-price − index)/index over
    the interval; `interest_rate` is the per-interval interest differential
    (default ~0.01%). Matches the Binance/Bybit/OKX clamp construction.
    """
    diff = np.clip(interest_rate - premium_index, -clamp, clamp)
    return float(premium_index + diff)


def basis(futures_price: float, index_price: float) -> float:
    """Simple basis (mark vs index), as a fraction."""
    return futures_price / index_price - 1.0


def annualised_basis(futures_price: float, index_price: float,
                     days_to_expiry: float) -> float:
    """Annualised basis for a DATED future (perps have no expiry → use funding)."""
    if days_to_expiry <= 0:
        raise ValueError("days_to_expiry must be positive")
    return basis(futures_price, index_price) * (365.0 / days_to_expiry)


def annualised_funding(rate_per_interval: float,
                       intervals_per_day: int = FUNDING_INTERVALS_PER_DAY) -> float:
    """Turn a per-interval funding rate into an annualised carry."""
    return rate_per_interval * intervals_per_day * 365.0


@dataclass(frozen=True, slots=True)
class FundingPnL:
    gross: float            # currency paid(+)/received(-) by a LONG
    intervals: int
    mean_rate: float
    annualised_carry: float


def funding_pnl(position_notional: float, funding_rates: np.ndarray,
                intervals_per_day: int = FUNDING_INTERVALS_PER_DAY) -> FundingPnL:
    """Cumulative funding cash flow on a held position.

    Sign convention: a LONG position pays when funding is positive, so the
    long's cash flow is −notional·Σrate. Returns that long cash flow in
    `gross` (negative = paid out).
    """
    r = np.asarray(funding_rates, float)
    long_cashflow = -position_notional * r.sum()
    mean = float(r.mean()) if r.size else 0.0
    return FundingPnL(gross=float(long_cashflow), intervals=int(r.size),
                      mean_rate=mean,
                      annualised_carry=annualised_funding(mean, intervals_per_day))


def cash_and_carry_apr(funding_rates: np.ndarray, spot_borrow_apr: float = 0.0,
                       intervals_per_day: int = FUNDING_INTERVALS_PER_DAY) -> float:
    """APR of the classic delta-neutral trade: long spot / short perp.

    Short-perp collects funding when funding is positive; subtract the cost
    of funding the spot leg. This is the number a basis desk lives on.
    """
    r = np.asarray(funding_rates, float)
    collected = annualised_funding(float(r.mean()) if r.size else 0.0,
                                    intervals_per_day)
    return collected - spot_borrow_apr
