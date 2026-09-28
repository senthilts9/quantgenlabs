"""Crypto risk engine — built for fat tails and a 24/7 clock.

Crypto returns are more leptokurtic than equities and trade continuously,
so two defaults change vs. a TradFi book:

  * annualisation uses 365 days (24/7), and √(365·24) for hourly bars;
  * tail risk uses Extreme Value Theory (peaks-over-threshold / GPD),
    because a normal or even empirical VaR badly understates the tail
    on assets that routinely move 5σ.

Also provides EWMA vol (RiskMetrics) scaling so a static historical VaR is
re-scaled to the current volatility regime — essential when a quiet week
precedes a violent one.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import genpareto, norm

CRYPTO_DAYS = 365
EWMA_LAMBDA = 0.94


def ewma_vol(returns: np.ndarray, lam: float = EWMA_LAMBDA) -> float:
    """RiskMetrics EWMA volatility (latest conditional σ)."""
    r = np.asarray(returns, float)
    w = (1 - lam) * lam ** np.arange(r.size)[::-1]
    mu = r.mean()
    var = np.sum(w * (r - mu) ** 2) / w.sum()
    return float(np.sqrt(var))


def annualise_vol(sigma_per_bar: float, bars_per_day: float = 1.0) -> float:
    return sigma_per_bar * np.sqrt(CRYPTO_DAYS * bars_per_day)


@dataclass(frozen=True, slots=True)
class TailRisk:
    method: str
    confidence: float
    var: float               # positive loss (return units)
    es: float                # expected shortfall / CVaR
    xi: float | None = None  # GPD shape (tail index); >0 ⇒ heavy tail
    beta: float | None = None
    threshold: float | None = None
    n_exceed: int | None = None


def historical_var(returns: np.ndarray, confidence: float = 0.99) -> TailRisk:
    r = np.sort(np.asarray(returns, float))
    a = 1 - confidence
    var = -np.quantile(r, a)
    tail = r[r <= -var]
    es = -tail.mean() if tail.size else var
    return TailRisk("historical", confidence, float(var), float(es))


def evt_var(returns: np.ndarray, confidence: float = 0.99,
            threshold_q: float = 0.95) -> TailRisk:
    """Peaks-over-threshold VaR/ES via a fitted Generalised Pareto tail.

    losses = −returns; fit GPD to exceedances over the `threshold_q`
    quantile of losses. Closed-form VaR/ES from the GPD parameters.
    """
    losses = -np.asarray(returns, float)
    n = losses.size
    u = np.quantile(losses, threshold_q)
    exceed = losses[losses > u] - u
    nu = exceed.size
    if nu < 10:                     # too few tail points → fall back
        hist = historical_var(returns, confidence)
        return TailRisk("evt_fallback_historical", confidence, hist.var, hist.es)

    xi, _, beta = genpareto.fit(exceed, floc=0.0)   # shape, loc=0, scale
    p = 1 - confidence
    if abs(xi) < 1e-6:
        var = u + beta * np.log((nu / n) / p)
    else:
        var = u + (beta / xi) * (((n / nu) * p) ** (-xi) - 1.0)
    es = (var + beta - xi * u) / (1.0 - xi) if xi < 1 else float("inf")
    return TailRisk("evt_pot_gpd", confidence, float(var), float(es),
                    xi=float(xi), beta=float(beta), threshold=float(u), n_exceed=int(nu))


def ewma_scaled_var(returns: np.ndarray, confidence: float = 0.99,
                    lam: float = EWMA_LAMBDA) -> TailRisk:
    """Historical VaR re-scaled to the CURRENT EWMA volatility regime.

    Filtered-historical-simulation-lite: standardise returns by trailing
    EWMA σ, take the empirical quantile of the standardised series, then
    re-inflate by the latest σ.
    """
    r = np.asarray(returns, float)
    sig_now = ewma_vol(r, lam)
    sig_hist = r.std(ddof=1)
    scale = sig_now / sig_hist if sig_hist > 0 else 1.0
    base = historical_var(r, confidence)
    return TailRisk("ewma_scaled_hs", confidence,
                    base.var * scale, base.es * scale)


def sharpe_after_funding(returns: np.ndarray, funding_rates: np.ndarray,
                         bars_per_day: float = 1.0) -> float:
    """Annualised Sharpe of a perp position NET of funding drag.

    Funding is a real, persistent cost/carry a naive Sharpe ignores. Aligns
    funding to the return series (broadcast if scalar mean supplied).
    """
    r = np.asarray(returns, float)
    f = np.asarray(funding_rates, float)
    if f.size == 1:
        f = np.full_like(r, f.item())
    net = r - f[: r.size]
    mu, sd = net.mean(), net.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(mu / sd * np.sqrt(CRYPTO_DAYS * bars_per_day))


CRYPTO_VAR_METHODS = {
    "historical": historical_var,
    "evt_pot": evt_var,
    "ewma_scaled": ewma_scaled_var,
}
