"""Value-at-Risk / Expected-Shortfall engine.

All measures return VaR and ES as POSITIVE loss numbers at the given
confidence. Inputs are simple returns (fraction), not P&L, unless a
portfolio value is supplied to scale into currency.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm


@dataclass(frozen=True, slots=True)
class VaRResult:
    method: str
    confidence: float
    var: float          # positive loss, in return units (or currency if scaled)
    es: float           # expected shortfall (a.k.a. CVaR)
    horizon_days: int = 1

    def scaled(self, portfolio_value: float) -> "VaRResult":
        return VaRResult(self.method, self.confidence,
                         self.var * portfolio_value,
                         self.es * portfolio_value, self.horizon_days)


def _alpha(confidence: float) -> float:
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be in (0.5, 1.0)")
    return 1.0 - confidence


def historical(returns: np.ndarray, confidence: float = 0.99) -> VaRResult:
    r = np.sort(np.asarray(returns, float))
    a = _alpha(confidence)
    var = -np.quantile(r, a, method="linear")
    tail = r[r <= -var]
    es = -tail.mean() if tail.size else var
    return VaRResult("historical", confidence, float(var), float(es))


def parametric(returns: np.ndarray, confidence: float = 0.99) -> VaRResult:
    r = np.asarray(returns, float)
    mu, sig = r.mean(), r.std(ddof=1)
    a = _alpha(confidence)
    z = norm.ppf(a)
    var = -(mu + sig * z)
    es = -(mu - sig * norm.pdf(z) / a)
    return VaRResult("parametric", confidence, float(var), float(es))


def cornish_fisher(returns: np.ndarray, confidence: float = 0.99) -> VaRResult:
    """Parametric VaR adjusted for skew & excess kurtosis (Cornish-Fisher)."""
    r = np.asarray(returns, float)
    mu, sig = r.mean(), r.std(ddof=1)
    z = norm.ppf(_alpha(confidence))
    n = r.size
    sk = (np.sum((r - mu) ** 3) / n) / sig ** 3
    ku = (np.sum((r - mu) ** 4) / n) / sig ** 4 - 3.0
    zc = (z + (z ** 2 - 1) * sk / 6
          + (z ** 3 - 3 * z) * ku / 24
          - (2 * z ** 3 - 5 * z) * sk ** 2 / 36)
    var = -(mu + sig * zc)
    return VaRResult("cornish_fisher", confidence, float(var), float(var))


def monte_carlo(returns: np.ndarray, confidence: float = 0.99,
                sims: int = 100_000, seed: int | None = 7) -> VaRResult:
    r = np.asarray(returns, float)
    draws = np.random.default_rng(seed).normal(r.mean(), r.std(ddof=1), sims)
    a = _alpha(confidence)
    var = -np.quantile(draws, a)
    tail = draws[draws <= -var]
    es = -tail.mean() if tail.size else var
    return VaRResult("monte_carlo", confidence, float(var), float(es))


def component_var(returns_matrix: np.ndarray, weights: np.ndarray,
                  confidence: float = 0.99) -> dict:
    """Parametric marginal & component VaR decomposition.

    returns_matrix: (T, N) asset returns; weights: (N,) sum to 1.
    Component VaRs sum to the portfolio VaR (Euler allocation).
    """
    R = np.asarray(returns_matrix, float)
    w = np.asarray(weights, float)
    cov = np.cov(R, rowvar=False, ddof=1)
    port_var = float(w @ cov @ w)
    port_sig = np.sqrt(port_var)
    z = norm.ppf(_alpha(confidence))
    p_var = -z * port_sig                       # zero-mean assumption
    mctr = (cov @ w) / port_sig                 # marginal contrib to vol
    comp = w * mctr * (-z)                       # component VaR, sums to p_var
    return {
        "portfolio_var": p_var,
        "component_var": comp.tolist(),
        "pct_contribution": (comp / p_var).tolist(),
        "marginal": (mctr * (-z)).tolist(),
    }
