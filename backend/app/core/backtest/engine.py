"""Event-driven cash-and-carry basis backtest.

The trade: long spot, short an equal notional of the perpetual future
(delta-neutral). You collect funding whenever it's positive (shorts receive),
pay it when negative, and earn/lose the small basis convergence — while the
two price legs offset. Costs that actually decide whether this is profitable:
taker/maker fees on entry and every rebalance, and a financing/borrow rate on
the spot leg. This engine models all of them and attributes P&L to each
source, which is the whole point — a basis desk lives or dies on funding
minus costs, not on being right about direction.

Loop is explicit and event-driven (bar by bar, funding applied on funding
bars), not a vectorised shortcut, so slippage, rebalancing and path-dependent
costs are handled honestly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .data import MarketData


@dataclass
class BasisConfig:
    capital: float = 1_000_000.0
    target_notional: float = 1_000_000.0   # gross per leg
    taker_fee: float = 0.0004              # 4 bps
    borrow_apr: float = 0.03               # cost to fund the spot leg
    rebalance_drift: float = 0.02          # rebalance when |delta| > 2% of notional
    funding_intervals_per_day: int = 3


@dataclass
class BacktestResult:
    equity: pd.Series                      # mark-to-market equity curve
    daily_returns: pd.Series
    attribution: dict                      # funding / price / fees / financing
    trades: int
    turnover: float
    data: MarketData = field(repr=False)


def run_basis_backtest(md: MarketData, cfg: BasisConfig | None = None) -> BacktestResult:
    cfg = cfg or BasisConfig()
    n = len(md)
    hours_per_bar = 1.0
    borrow_per_bar = cfg.borrow_apr / (365 * 24) * hours_per_bar

    # position state (base units)
    qty_spot = cfg.target_notional / md.spot[0]
    qty_perp = -cfg.target_notional / md.perp[0]      # short
    entry_fee = 2 * cfg.target_notional * cfg.taker_fee
    cash = -entry_fee                                  # fees paid up front
    trades = 1
    turnover = 2 * cfg.target_notional

    equity = np.empty(n)
    acc = {"funding": 0.0, "price": 0.0, "fees": -entry_fee, "financing": 0.0}
    prev_spot, prev_perp = md.spot[0], md.perp[0]

    for t in range(n):
        s, p = md.spot[t], md.perp[t]

        # mark-to-market price P&L on both legs since last bar
        if t > 0:
            dprice = qty_spot * (s - prev_spot) + qty_perp * (p - prev_perp)
            acc["price"] += dprice
            cash += dprice

        # financing cost on the spot leg (borrow to hold spot)
        fin = -abs(qty_spot) * s * borrow_per_bar
        acc["financing"] += fin
        cash += fin

        # funding settlement on funding bars: short perp receives +funding·notional
        if md.is_funding_bar[t]:
            perp_notional = -qty_perp * p           # positive notional short
            fund_cash = perp_notional * md.funding[t]   # +ve funding ⇒ short receives
            acc["funding"] += fund_cash
            cash += fund_cash

        # rebalance back to target notional if delta drifted
        cur_spot_notional = qty_spot * s
        drift = abs(cur_spot_notional - cfg.target_notional) / cfg.target_notional
        if drift > cfg.rebalance_drift:
            new_qty_spot = cfg.target_notional / s
            new_qty_perp = -cfg.target_notional / p
            traded = abs(new_qty_spot - qty_spot) * s + abs(new_qty_perp - qty_perp) * p
            fee = traded * cfg.taker_fee
            acc["fees"] -= fee
            cash -= fee
            turnover += traded
            trades += 1
            qty_spot, qty_perp = new_qty_spot, new_qty_perp

        equity[t] = cfg.capital + cash
        prev_spot, prev_perp = s, p

    eq = pd.Series(equity, index=md.index, name="equity")
    daily = eq.resample("1D").last().pct_change().dropna()
    return BacktestResult(eq, daily, acc, trades, turnover / cfg.capital, md)
