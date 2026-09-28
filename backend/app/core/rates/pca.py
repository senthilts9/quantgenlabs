"""Yield-curve factor model: PCA on daily curve changes.

This is the standard statistical model of the term structure (Litterman-
Scheinkman). Three factors typically explain 95-99% of daily variation, and
they are interpretable rather than black-box:

    PC1  LEVEL      -- all loadings the same sign; the whole curve shifts
    PC2  SLOPE      -- loadings flip sign across the curve; steepen/flatten
    PC3  CURVATURE  -- belly moves against the wings; butterfly

Why PCA rather than a supervised model: the honest task here is describing
covariance structure and generating coherent shocks, not predicting rates.
A regressor forecasting tomorrow's 10Y from lagged data will produce an
impressive in-sample R-squared and no out-of-sample edge; presenting that as
a signal would be the wrong call. Factor VaR built from PCA is what a rates
risk system actually runs.

Implementation notes worth defending in review:
  * PCA is fitted on CHANGES, not levels. Yield levels are near-unit-root,
    so PCA on levels mostly recovers the sample mean and produces factors
    that look great and mean nothing.
  * The covariance is NOT correlation-standardised. Curve changes are already
    in common units (bp), and standardising would inflate the weight of quiet
    front-end tenors relative to the long end that dominates real P&L.
  * Sign is canonicalised so PC1 loads positive. SVD sign is arbitrary, and a
    factor that silently flips between refreshes makes every scenario chart
    and every P&L attribution unreproducible.
"""
from __future__ import annotations

import numpy as np

FACTOR_NAMES = ["Level", "Slope", "Curvature", "PC4", "PC5"]


def fit(matrix, tenor_years, n_factors: int = 3) -> dict:
    """PCA on daily curve changes (percentage points).

    `matrix` is (observations x tenors) of par yields in percent, oldest
    first. Returns loadings, explained variance, factor scores and the
    per-factor daily volatility used to size shocks.
    """
    Y = np.asarray(matrix, dtype=float)
    if Y.ndim != 2 or Y.shape[0] < 30:
        raise ValueError("need at least 30 observations to estimate a curve factor model")

    dY = np.diff(Y, axis=0) * 100.0          # daily changes in basis points
    mu = dY.mean(axis=0)
    X = dY - mu

    # SVD of the centred change matrix: numerically stabler than forming and
    # eigen-decomposing the covariance, which squares the condition number.
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    n_obs = X.shape[0]
    var = S ** 2 / (n_obs - 1)
    total = var.sum()

    k = min(n_factors, Vt.shape[0])
    loadings = Vt[:k].copy()
    scores = U[:, :k] * S[:k]

    for i in range(k):
        # Canonical orientation. PC1: positive level shift. PC2: define the
        # positive direction as a STEEPENER (long end up relative to short),
        # so "slope +1sd" means the same thing on every refresh.
        if i == 0:
            flip = loadings[i].sum() < 0
        elif i == 1:
            flip = loadings[i][-1] < loadings[i][0]
        else:
            flip = loadings[i][np.argmax(np.abs(loadings[i]))] < 0
        if flip:
            loadings[i] *= -1
            scores[:, i] *= -1

    factor_vol = scores.std(axis=0, ddof=1)

    # Reconstruction error at k factors: the honest measure of what the model
    # throws away, in the same bp units as the data.
    recon = scores @ loadings
    resid = X - recon
    rmse_bp = float(np.sqrt((resid ** 2).mean()))

    return {
        "n_observations": int(n_obs),
        "tenor_years": [round(float(t), 4) for t in tenor_years],
        "factors": [
            {
                "name": FACTOR_NAMES[i] if i < len(FACTOR_NAMES) else f"PC{i+1}",
                "explained_variance_pct": round(float(var[i] / total * 100), 3),
                "cumulative_pct": round(float(var[:i + 1].sum() / total * 100), 3),
                "daily_vol_bp": round(float(factor_vol[i]), 4),
                "loadings": [round(float(v), 5) for v in loadings[i]],
            }
            for i in range(k)
        ],
        "total_explained_pct": round(float(var[:k].sum() / total * 100), 3),
        "reconstruction_rmse_bp": round(rmse_bp, 4),
        "mean_daily_change_bp": [round(float(v), 4) for v in mu],
        "_loadings": loadings,
        "_scores": scores,
    }


def factor_shocks(model: dict, n_sigma: float = 1.0) -> list[dict]:
    """+/- n-sigma shock per factor, expressed as a curve move in bp.

    These are coherent shocks: unlike an ad-hoc "+50bp on the 2Y", each one
    respects the covariance actually observed in the data, so it is a shape
    the curve has historically taken.
    """
    out = []
    for i, f in enumerate(model["factors"]):
        shock_bp = np.asarray(f["loadings"], dtype=float) * f["daily_vol_bp"] * n_sigma
        out.append({
            "factor": f["name"],
            "n_sigma": n_sigma,
            "explained_variance_pct": f["explained_variance_pct"],
            "shift_bp": [round(float(v), 3) for v in shock_bp],
        })
    return out


def factor_var(model: dict, krd_by_tenor: dict[float, float], price: float,
               confidence: float = 0.99, horizon_days: int = 1) -> dict:
    """Historical-simulation VaR of a rates position through the factor model.

    Maps the position's key-rate durations onto each tenor, then replays the
    ACTUAL historical factor scores to build a P&L distribution. Historical
    simulation rather than parametric because curve factor scores are
    visibly fat-tailed -- a normal assumption understates exactly the days
    that matter.

    Sign convention: yields UP is a loss for a long bond position.

        dP = -Duration * dy * P,  with dy in DECIMAL

    Factor scores are in basis points, so the conversion is dy = bp / 1e4.
    Getting this wrong by a factor of 100 is the classic bp/percent slip and
    it does not look obviously wrong in isolation -- it only shows up when you
    sanity-check VaR against DV01, which is why that ratio is returned below.
    """
    loadings = model["_loadings"]
    scores = model["_scores"]
    tenors = model["tenor_years"]

    # KRDs are defined at key tenors; spread them onto the model's tenor grid
    # by nearest key so the exposure vector lines up with the loadings.
    krd_vec = np.zeros(len(tenors))
    if krd_by_tenor:
        keys = np.array(sorted(krd_by_tenor))
        for i, t in enumerate(tenors):
            krd_vec[i] = krd_by_tenor[float(keys[np.argmin(np.abs(keys - t))])]
        # Preserve total duration: nearest-key assignment would otherwise
        # multiply the exposure by however many grid points map to each key.
        if krd_vec.sum() != 0:
            krd_vec *= sum(krd_by_tenor.values()) / krd_vec.sum()

    dY_hist = scores @ loadings                     # reconstructed daily bp moves
    pnl = -(dY_hist @ krd_vec) * price / 1e4 * np.sqrt(horizon_days)

    q = np.quantile(pnl, 1 - confidence)
    tail = pnl[pnl <= q]
    dv01_total = float(krd_vec.sum() * price * 1e-4)

    return {
        "confidence": confidence,
        "horizon_days": horizon_days,
        "n_scenarios": int(len(pnl)),
        "position_dv01": round(dv01_total, 4),
        "var": round(float(-q), 4),
        "expected_shortfall": round(float(-tail.mean()) if len(tail) else float(-q), 4),
        "worst_day": round(float(pnl.min()), 4),
        "best_day": round(float(pnl.max()), 4),
        "mean_daily_pnl": round(float(pnl.mean()), 4),
        "pnl_vol": round(float(pnl.std(ddof=1)), 4),
        # VaR / DV01 = the adverse yield move in bp this VaR implies. A rates
        # PM reads this instantly: single digits for a 1-day 99% number is
        # sane, hundreds means a units bug upstream.
        "implied_move_bp": round(float(-q / dv01_total), 2) if dv01_total else None,
        "method": "historical simulation through PCA factors",
    }


def public(model: dict) -> dict:
    """Strip the private numpy arrays before serialising to JSON."""
    return {k: v for k, v in model.items() if not k.startswith("_")}
