"""Market data for the basis backtest — one clean adapter, two sources.

`load_csv` is the production path: point it at a real Binance/Deribit export
with columns [timestamp, spot, perp_mark, funding_rate]. `simulate` is the
offline path used in this repo's demo: a funding/price series calibrated to
real crypto behaviour (mean-reverting funding that occasionally spikes,
price with volatility clustering, perp mark tethered to spot by a noisy
basis). Swapping demo → live is a one-line change of the loader call.

Funding convention matches xquant.crypto.perps: 8h intervals (3×/day),
positive funding ⇒ longs pay shorts.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

BARS_PER_DAY = 24            # hourly bars
FUNDING_EVERY = 8           # hours


@dataclass(frozen=True)
class MarketData:
    index: pd.DatetimeIndex
    spot: np.ndarray
    perp: np.ndarray            # perp mark
    funding: np.ndarray         # per-8h rate, aligned to bars (0 except at funding bars)
    is_funding_bar: np.ndarray  # bool mask

    def __len__(self) -> int:
        return len(self.spot)


def load_csv(path: str) -> MarketData:
    """Load a real exchange export. Expected columns:
    timestamp, spot, perp_mark, funding_rate (per interval, at funding bars).
    """
    df = pd.read_csv(path, parse_dates=["timestamp"]).sort_values("timestamp")
    idx = pd.DatetimeIndex(df["timestamp"])
    funding = df.get("funding_rate", pd.Series(np.zeros(len(df)))).to_numpy(float)
    is_fund = funding != 0.0
    return MarketData(idx, df["spot"].to_numpy(float),
                      df.get("perp_mark", df["spot"]).to_numpy(float),
                      np.nan_to_num(funding), is_fund)


def simulate(days: int = 365, seed: int = 7, *, s0: float = 2.10,
             ann_vol: float = 0.80, funding_mean: float = 0.0001,
             funding_vol: float = 0.00008) -> MarketData:
    """Calibrated synthetic series (hourly). Defaults ~ an XRP-like perp:
    ~80% annualised vol, funding averaging ~1bp/8h with regime spikes.
    """
    rng = np.random.default_rng(seed)
    n = days * BARS_PER_DAY
    idx = pd.date_range("2025-01-01", periods=n, freq="h")

    # --- price: GBM with GARCH-ish vol clustering ---
    dt = 1.0 / (365 * BARS_PER_DAY)
    vol = np.empty(n)
    v = ann_vol
    shocks = rng.standard_normal(n)
    logret = np.empty(n)
    for t in range(n):
        v = 0.94 * v + 0.06 * ann_vol * (1 + 0.5 * abs(shocks[t - 1] if t else 0))
        vol[t] = v
        logret[t] = -0.5 * v * v * dt + v * np.sqrt(dt) * shocks[t]
    spot = s0 * np.exp(np.cumsum(logret))

    # --- funding: Ornstein-Uhlenbeck mean-reverting, with occasional spikes ---
    is_fund = (np.arange(n) % FUNDING_EVERY == 0)
    n_fund = int(is_fund.sum())
    f = np.empty(n_fund)
    x = funding_mean
    for i in range(n_fund):
        x = x + 0.15 * (funding_mean - x) + funding_vol * rng.standard_normal()
        if rng.random() < 0.02:                        # ~2% regime spike
            x += funding_vol * 8 * rng.standard_normal()
        f[i] = x
    funding = np.zeros(n)
    funding[is_fund] = f

    # --- perp mark: spot + noisy basis that decays toward 0 ---
    basis = np.zeros(n)
    b = 0.0
    for t in range(1, n):
        b = 0.98 * b + 0.0005 * rng.standard_normal()
        basis[t] = b
    perp = spot * (1.0 + basis)

    return MarketData(idx, spot, perp, funding, is_fund)
