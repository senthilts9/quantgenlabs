"""Portfolio construction: minimum-variance, risk parity, max-Sharpe.

All three solved on the SAME covariance matrix the risk screen uses, so the
optimiser and the risk report cannot disagree -- a surprisingly common defect
when construction and measurement are separate systems.

Solvers are projected-gradient with a simplex projection (long-only, fully
invested). Deliberately not scipy.optimize: the constraint set here is simple
enough that an explicit projection is both faster and auditable, and it can
never silently return an infeasible point the way a generic solver can when
it hits its iteration cap.

RISK PARITY is the one worth reading. Equal WEIGHT is not equal RISK: in a
book where one asset has 10x the volatility, equal weights give that asset
most of the risk. Risk parity equalises each asset's CONTRIBUTION to
portfolio variance, which is what "diversified" actually means.
"""
from __future__ import annotations

import numpy as np

TRADING_DAYS = 252


def _project_simplex(v: np.ndarray) -> np.ndarray:
    """Euclidean projection onto {w : w >= 0, sum(w) = 1}."""
    n = len(v)
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - 1
    idx = np.arange(1, n + 1)
    cond = u - css / idx > 0
    rho = idx[cond][-1] if cond.any() else 1
    theta = css[rho - 1] / rho
    return np.maximum(v - theta, 0.0)


def _risk_contributions(w: np.ndarray, cov: np.ndarray) -> np.ndarray:
    var = float(w @ cov @ w)
    if var <= 0:
        return np.zeros_like(w)
    return w * (cov @ w) / var          # fractions summing to 1


def min_variance(cov: np.ndarray, iters: int = 4000, lr: float = 0.02) -> np.ndarray:
    n = cov.shape[0]
    w = np.full(n, 1.0 / n)
    scale = float(np.trace(cov)) / n or 1.0
    for _ in range(iters):
        w = _project_simplex(w - lr * (2 * cov @ w) / scale)
    return w


def risk_parity(cov: np.ndarray, iters: int = 2000, tol: float = 1e-12) -> np.ndarray:
    """Equal risk contribution via the standard fixed-point iteration.

        w_i  <-  target_i / (Sigma w)_i,   then renormalise to sum 1

    At a fixed point w_i * (Sigma w)_i is constant across i, which IS equal
    risk contribution.

    This replaces a projected-gradient attempt on the dispersion objective,
    which converged to a CORNER (100% in the highest-vol asset) on a
    two-asset book with a 10x volatility spread -- the exact opposite of risk
    parity. Gradient descent with a simplex projection can slide along a face
    to a vertex when the objective is flat in the interior; the fixed-point
    map cannot, because w_i = 0 is not a fixed point for a positive target.
    """
    n = cov.shape[0]
    target = np.full(n, 1.0 / n)
    # Inverse-volatility start: already close for a diagonal covariance, so
    # the iteration converges in far fewer steps.
    vol = np.sqrt(np.maximum(np.diag(cov), 1e-18))
    w = (1.0 / vol)
    w = w / w.sum()
    for _ in range(iters):
        mrc = cov @ w                       # marginal risk contributions
        mrc = np.where(np.abs(mrc) < 1e-18, 1e-18, mrc)
        w_new = target / mrc
        w_new = np.maximum(w_new, 0.0)
        s = w_new.sum()
        if s <= 0:
            break
        w_new = w_new / s
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    return w


def max_sharpe(mu: np.ndarray, cov: np.ndarray, rf: float = 0.0,
               iters: int = 4000, lr: float = 0.02) -> np.ndarray:
    n = cov.shape[0]
    w = np.full(n, 1.0 / n)
    for _ in range(iters):
        ex = w @ mu - rf
        var = float(w @ cov @ w)
        sd = np.sqrt(var) if var > 0 else 1e-12
        # d/dw of (w'mu - rf)/sqrt(w'Sw)
        grad = mu / sd - ex * (cov @ w) / (sd ** 3)
        w = _project_simplex(w + lr * grad)      # ascent: maximise Sharpe
    return w


def _stats(w: np.ndarray, mu: np.ndarray, cov: np.ndarray, rf: float) -> dict:
    ret = float(w @ mu)
    vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    rc = _risk_contributions(w, cov)
    return {
        "weights": [round(float(x), 6) for x in w],
        "expected_return_pct": round(ret * 100, 4),
        "volatility_pct": round(vol * 100, 4),
        "sharpe": round((ret - rf) / vol, 4) if vol > 0 else None,
        "risk_contributions_pct": [round(float(x) * 100, 3) for x in rc],
        # Spread of risk contributions: 0 = perfect risk parity. Reported for
        # every method so the reader can see what each one actually equalises.
        "risk_concentration": round(float(rc.max() - rc.min()), 5),
        "effective_n": round(float(1.0 / (w @ w)), 3) if (w @ w) > 0 else None,
    }


def optimise(symbols: list[str], returns: np.ndarray, rf_annual: float = 0.0,
             current_weights: np.ndarray | None = None) -> dict:
    """Run all methods on one covariance estimate and compare.

    `returns` is (observations x assets) of DAILY simple returns, already
    date-aligned by the caller.
    """
    if returns.ndim != 2 or returns.shape[0] < 30:
        return {"error": f"need 30+ aligned observations, got {returns.shape[0]}"}
    n = returns.shape[1]
    if n < 2:
        return {"error": "portfolio optimisation needs at least 2 assets"}

    mu = returns.mean(axis=0) * TRADING_DAYS
    cov = np.cov(returns, rowvar=False, ddof=1) * TRADING_DAYS
    rf = rf_annual

    results = {}
    for name, w in (("min_variance", min_variance(cov)),
                    ("risk_parity", risk_parity(cov)),
                    ("max_sharpe", max_sharpe(mu, cov, rf)),
                    ("equal_weight", np.full(n, 1.0 / n))):
        results[name] = _stats(w, mu, cov, rf)

    if current_weights is not None and len(current_weights) == n:
        cw = np.abs(current_weights)
        if cw.sum() > 0:
            results["current"] = _stats(cw / cw.sum(), mu, cov, rf)

    return {
        "symbols": symbols,
        "n_observations": int(returns.shape[0]),
        "risk_free_annual": rf,
        "expected_returns_pct": [round(float(x) * 100, 4) for x in mu],
        "volatilities_pct": [round(float(np.sqrt(cov[i, i])) * 100, 4) for i in range(n)],
        "methods": results,
        # Sample means are a notoriously poor expected-return estimate, and
        # max-Sharpe is the method most damaged by that. Saying so beside the
        # output is more useful than a caveat buried in a doc nobody opens.
        "caveat": ("expected returns are sample means over the window -- max-Sharpe "
                   "is highly sensitive to that estimate and will overfit; "
                   "min-variance and risk parity use only the covariance and are "
                   "far more stable out of sample"),
    }
