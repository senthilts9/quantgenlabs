"""FRM Part I — Quantitative Analysis.

Bayes, the standard distributions, hypothesis tests, OLS with its diagnostics,
rank correlation, and time-series structure.

Two of these exist mainly to stop the rest from being trusted blindly:

  REGRESSION DIAGNOSTICS. R^2 rises mechanically with every regressor added,
  so adjusted R^2 is reported beside it, along with VIF for multicollinearity
  and a Breusch-Pagan test for heteroskedasticity. Under heteroskedasticity
  the coefficients stay unbiased but the STANDARD ERRORS are wrong, which
  means every t-statistic and every significance claim is wrong -- a
  regression that looks strong and is not.

  COVARIANCE STATIONARITY. An AR model on a non-stationary series produces
  forecasts that look fine and mean nothing. The AR(1) fit here reports |phi|
  and refuses to describe a unit-root series as forecastable.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats


# ---- Bayes --------------------------------------------------------------
def bayes(prior: float, sensitivity: float, false_positive: float) -> dict:
    """P(A|B) = P(B|A)P(A) / P(B), spelled out for a screening test.

    The classic result is that with a rare condition, most positives are
    FALSE positives even for an accurate test -- the base rate dominates the
    likelihood. This is the same error as reading a default-prediction model's
    hit rate without its base rate.
    """
    if not (0 <= prior <= 1 and 0 <= sensitivity <= 1 and 0 <= false_positive <= 1):
        return {"error": "all inputs must be probabilities in [0, 1]"}
    p_b = sensitivity * prior + false_positive * (1 - prior)
    if p_b == 0:
        return {"error": "P(B) is zero; posterior undefined"}
    post = sensitivity * prior / p_b
    return {
        "prior": prior, "sensitivity_p_b_given_a": sensitivity,
        "false_positive_rate": false_positive,
        "p_b_unconditional": round(p_b, 8),
        "posterior_p_a_given_b": round(post, 8),
        "lift_vs_prior": round(post / prior, 4) if prior else None,
        "note": ("posterior is below 50% despite a positive signal — the base "
                 "rate dominates" if post < 0.5 else "positive signal dominates"),
    }


# ---- discrete / continuous distributions --------------------------------
def binomial(n: int, p: float, x: int | None = None) -> dict:
    """P(X=x) = C(n,x) p^x (1-p)^(n-x); mean np, variance np(1-p)."""
    if n < 0 or not (0 <= p <= 1):
        return {"error": "n >= 0 and 0 <= p <= 1 required"}
    out = {"n": n, "p": p, "mean": round(n * p, 6),
           "variance": round(n * p * (1 - p), 6),
           "std_dev": round(math.sqrt(n * p * (1 - p)), 6)}
    if x is not None:
        out.update({"x": x, "pmf": round(float(stats.binom.pmf(x, n, p)), 8),
                    "cdf": round(float(stats.binom.cdf(x, n, p)), 8)})
    return out


def poisson(lam: float, x: int | None = None) -> dict:
    """P(X=x) = lambda^x e^-lambda / x!. Mean AND variance both equal lambda.

    The standard model for operational-risk LOSS FREQUENCY: counts of rare,
    independent events per period.
    """
    if lam < 0:
        return {"error": "lambda must be non-negative"}
    out = {"lambda": lam, "mean": lam, "variance": lam,
           "std_dev": round(math.sqrt(lam), 6)}
    if x is not None:
        out.update({"x": x, "pmf": round(float(stats.poisson.pmf(x, lam)), 8),
                    "cdf": round(float(stats.poisson.cdf(x, lam)), 8)})
    return out


def uniform(a: float, b: float, x1: float | None = None,
            x2: float | None = None) -> dict:
    """P(x1 <= X <= x2) = (x2 - x1)/(b - a) for a <= x1 < x2 <= b."""
    if b <= a:
        return {"error": "upper bound must exceed lower bound"}
    out = {"a": a, "b": b, "mean": (a + b) / 2,
           "variance": round((b - a) ** 2 / 12, 8)}
    if x1 is not None and x2 is not None:
        lo, hi = max(a, min(x1, x2)), min(b, max(x1, x2))
        out["probability"] = round(max(0.0, (hi - lo) / (b - a)), 8)
    return out


# ---- hypothesis tests ---------------------------------------------------
def t_test_mean(x: np.ndarray, mu0: float = 0.0, alpha: float = 0.05,
                tail: str = "two") -> dict:
    """t = (xbar - mu0) / (s / sqrt(n)), df = n-1."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 2:
        return {"error": "need 2+ observations"}
    s = float(x.std(ddof=1))
    if s == 0:
        return {"error": "zero sample variance"}
    se = s / math.sqrt(n)
    t = (float(x.mean()) - mu0) / se
    df = n - 1
    if tail == "two":
        p = 2 * (1 - stats.t.cdf(abs(t), df))
        crit = float(stats.t.ppf(1 - alpha / 2, df))
    elif tail == "right":
        p, crit = 1 - stats.t.cdf(t, df), float(stats.t.ppf(1 - alpha, df))
    else:
        p, crit = stats.t.cdf(t, df), float(stats.t.ppf(alpha, df))
    return {"t_stat": round(t, 5), "df": df, "p_value": round(float(p), 8),
            "critical_value": round(crit, 5), "alpha": alpha, "tail": tail,
            "sample_mean": round(float(x.mean()), 8),
            "std_error": round(se, 8),
            "reject_null": bool(p < alpha),
            "confidence_interval": [
                round(float(x.mean()) - abs(stats.t.ppf(1 - alpha / 2, df)) * se, 8),
                round(float(x.mean()) + abs(stats.t.ppf(1 - alpha / 2, df)) * se, 8)]}


def chi_squared_variance(x: np.ndarray, sigma0_sq: float,
                         alpha: float = 0.05) -> dict:
    """chi^2 = (n-1)s^2 / sigma0^2, df = n-1. Tests a variance, not a mean."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 2 or sigma0_sq <= 0:
        return {"error": "need 2+ observations and a positive hypothesised variance"}
    s2 = float(x.var(ddof=1))
    stat = (n - 1) * s2 / sigma0_sq
    df = n - 1
    p = 2 * min(stats.chi2.cdf(stat, df), 1 - stats.chi2.cdf(stat, df))
    return {"chi_squared": round(stat, 5), "df": df,
            "sample_variance": round(s2, 10), "hypothesised_variance": sigma0_sq,
            "p_value": round(float(p), 8), "reject_null": bool(p < alpha),
            "critical_lower": round(float(stats.chi2.ppf(alpha / 2, df)), 5),
            "critical_upper": round(float(stats.chi2.ppf(1 - alpha / 2, df)), 5)}


def f_test_variances(x1: np.ndarray, x2: np.ndarray, alpha: float = 0.05) -> dict:
    """F = s1^2 / s2^2. Convention: larger variance on top, so F >= 1."""
    a, b = np.asarray(x1, float), np.asarray(x2, float)
    if len(a) < 2 or len(b) < 2:
        return {"error": "need 2+ observations in each sample"}
    v1, v2 = float(a.var(ddof=1)), float(b.var(ddof=1))
    if min(v1, v2) <= 0:
        return {"error": "zero variance in one sample"}
    if v1 >= v2:
        f, d1, d2 = v1 / v2, len(a) - 1, len(b) - 1
    else:
        f, d1, d2 = v2 / v1, len(b) - 1, len(a) - 1
    p = 2 * (1 - stats.f.cdf(f, d1, d2))
    return {"f_stat": round(f, 5), "df_numerator": d1, "df_denominator": d2,
            "variance_1": round(v1, 10), "variance_2": round(v2, 10),
            "p_value": round(float(min(p, 1.0)), 8),
            "reject_equal_variances": bool(p < alpha),
            "critical_value": round(float(stats.f.ppf(1 - alpha / 2, d1, d2)), 5)}


# ---- regression ---------------------------------------------------------
def ols(y: np.ndarray, X: np.ndarray, names: list[str] | None = None) -> dict:
    """Multiple regression with the diagnostics that decide if it is usable.

    Returns TSS = ESS + RSS, R^2, adjusted R^2, coefficient t-stats, VIF per
    regressor, and a Breusch-Pagan test for heteroskedasticity.
    """
    y = np.asarray(y, float).ravel()
    X = np.atleast_2d(np.asarray(X, float))
    if X.shape[0] != len(y):
        X = X.T
    n, k = X.shape
    if n <= k + 1:
        return {"error": f"need more observations ({n}) than regressors ({k})"}
    names = names or [f"x{i+1}" for i in range(k)]

    Xd = np.column_stack([np.ones(n), X])
    coef, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    fitted = Xd @ coef
    resid = y - fitted
    tss = float(((y - y.mean()) ** 2).sum())
    rss = float((resid ** 2).sum())
    ess = tss - rss
    r2 = ess / tss if tss > 0 else None
    dof = n - k - 1
    adj = 1 - (1 - r2) * (n - 1) / dof if r2 is not None else None

    try:
        xtx_inv = np.linalg.inv(Xd.T @ Xd)
        se = np.sqrt(np.diag(xtx_inv) * rss / dof)
    except np.linalg.LinAlgError:
        se = np.full(k + 1, np.nan)

    coefs = []
    for i, nm in enumerate(["intercept"] + names):
        t = coef[i] / se[i] if se[i] and np.isfinite(se[i]) and se[i] > 0 else None
        coefs.append({
            "name": nm, "coefficient": round(float(coef[i]), 8),
            "std_error": round(float(se[i]), 8) if np.isfinite(se[i]) else None,
            "t_stat": round(float(t), 4) if t is not None else None,
            "p_value": round(float(2 * (1 - stats.t.cdf(abs(t), dof))), 6)
                       if t is not None else None,
            "significant_5pct": bool(abs(t) > stats.t.ppf(0.975, dof))
                                if t is not None else None,
        })

    # VIF_j = 1 / (1 - R^2_j) from regressing each regressor on the others.
    # Above ~10 the coefficient is not separately identified: the standard
    # error inflates, t-stats collapse, and signs can flip on resampling.
    vif = {}
    if k > 1:
        for j in range(k):
            others = np.column_stack([np.ones(n), np.delete(X, j, axis=1)])
            cj, *_ = np.linalg.lstsq(others, X[:, j], rcond=None)
            rj = X[:, j] - others @ cj
            tj = float(((X[:, j] - X[:, j].mean()) ** 2).sum())
            r2j = 1 - float((rj ** 2).sum()) / tj if tj > 0 else 0.0
            vif[names[j]] = round(1 / (1 - r2j), 3) if r2j < 0.9999 else None

    # Breusch-Pagan: regress squared residuals on the regressors. A
    # significant fit means residual variance depends on X.
    bp = None
    try:
        u2 = resid ** 2
        cb, *_ = np.linalg.lstsq(Xd, u2, rcond=None)
        u_fit = Xd @ cb
        tss_u = float(((u2 - u2.mean()) ** 2).sum())
        r2_u = 1 - float(((u2 - u_fit) ** 2).sum()) / tss_u if tss_u > 0 else 0.0
        lm = n * r2_u
        p_bp = float(1 - stats.chi2.cdf(lm, k))
        bp = {"lm_stat": round(lm, 4), "df": k, "p_value": round(p_bp, 6),
              "heteroskedastic_5pct": bool(p_bp < 0.05)}
    except Exception:
        bp = None

    f_stat = (ess / k) / (rss / dof) if rss > 0 and k > 0 else None
    return {
        "n": int(n), "k_regressors": int(k),
        "coefficients": coefs,
        "tss": round(tss, 8), "ess": round(ess, 8), "rss": round(rss, 8),
        "r_squared": round(r2, 6) if r2 is not None else None,
        "adj_r_squared": round(adj, 6) if adj is not None else None,
        "std_error_regression": round(math.sqrt(rss / dof), 8),
        "f_stat": round(float(f_stat), 4) if f_stat else None,
        "f_p_value": round(float(1 - stats.f.cdf(f_stat, k, dof)), 8) if f_stat else None,
        "vif": vif or None,
        "multicollinearity_flag": [nm for nm, v in vif.items() if v and v > 10] or None,
        "breusch_pagan": bp,
        "diagnostics_note": (
            "heteroskedasticity detected — coefficients remain unbiased but the "
            "standard errors, and therefore every t-stat and significance claim "
            "above, are unreliable; use robust errors"
            if bp and bp["heteroskedastic_5pct"] else
            "no heteroskedasticity detected at 5%"),
    }


# ---- rank correlation ---------------------------------------------------
def rank_correlations(x: np.ndarray, y: np.ndarray) -> dict:
    """Pearson, Spearman's rho and Kendall's tau side by side.

    Pearson measures LINEAR association; the rank measures capture any
    monotonic relationship and are far less sensitive to outliers. A large
    gap between Pearson and Spearman is itself the finding: it says the
    relationship is monotonic but not linear, or that a few extreme points
    are driving the linear estimate.
    """
    a, b = np.asarray(x, float), np.asarray(y, float)
    n = min(len(a), len(b))
    if n < 5:
        return {"error": "need 5+ paired observations"}
    a, b = a[-n:], b[-n:]
    pear = float(np.corrcoef(a, b)[0, 1])
    sp, sp_p = stats.spearmanr(a, b)
    kt, kt_p = stats.kendalltau(a, b)
    return {"n": int(n),
            "pearson": round(pear, 5),
            "spearman_rho": round(float(sp), 5), "spearman_p": round(float(sp_p), 6),
            "kendall_tau": round(float(kt), 5), "kendall_p": round(float(kt_p), 6),
            "pearson_minus_spearman": round(pear - float(sp), 5),
            "note": ("large Pearson/Spearman gap — the relationship is monotonic "
                     "but not linear, or outliers drive the linear estimate"
                     if abs(pear - float(sp)) > 0.15 else
                     "linear and rank measures agree")}


# ---- time series --------------------------------------------------------
def ar1(y: np.ndarray) -> dict:
    """AR(1): y_t = d + phi*y_{t-1} + e_t, with the stationarity check.

    Covariance stationarity requires |phi| < 1. Without it the mean and
    variance are not stable, the unconditional moments below do not exist,
    and any forecast from the model is meaningless however good the fit looks.
    """
    y = np.asarray(y, float).ravel()
    n = len(y)
    if n < 30:
        return {"error": f"need 30+ observations, got {n}"}
    y0, y1 = y[:-1], y[1:]
    X = np.column_stack([np.ones(len(y0)), y0])
    coef, *_ = np.linalg.lstsq(X, y1, rcond=None)
    d, phi = float(coef[0]), float(coef[1])
    resid = y1 - X @ coef
    sig2 = float((resid ** 2).sum()) / (len(y1) - 2)
    half_life = (math.log(0.5) / math.log(abs(phi))) if 0 < abs(phi) < 1 else None

    # |phi| < 1 is NOT a sufficient test in a finite sample. OLS on an
    # autoregression is biased DOWNWARD (Hurwicz bias, order 1/n), so a true
    # random walk with phi = 1 routinely estimates at 0.99 and would be
    # declared stationary. Two further guards: a near-unit-root band, and a
    # half-life that exceeds the sample -- if the series takes longer to
    # mean-revert than the data observed, the "mean" it reverts to has not
    # been identified and the unconditional moments below are fiction.
    near_unit_root = abs(phi) > 0.95
    half_life_exceeds_sample = half_life is not None and half_life > n / 2
    stationary = abs(phi) < 1 and not near_unit_root

    out = {"intercept": round(d, 8), "phi": round(phi, 6),
           "residual_variance": round(sig2, 10),
           "covariance_stationary": bool(stationary),
           "near_unit_root": bool(near_unit_root),
           "half_life_periods": round(half_life, 2) if half_life else None,
           "half_life_exceeds_half_sample": bool(half_life_exceeds_sample),
           "one_step_forecast": round(d + phi * float(y[-1]), 8),
           "n": int(n)}

    if stationary and not half_life_exceeds_sample:
        out.update({
            "unconditional_mean": round(d / (1 - phi), 8),
            "unconditional_variance": round(sig2 / (1 - phi ** 2), 10),
        })
    elif abs(phi) >= 1:
        out["warning"] = ("|phi| >= 1: unit root or explosive. NOT covariance "
                          "stationary — unconditional moments do not exist and "
                          "forecasts are meaningless. Difference the series first.")
    else:
        out["warning"] = (
            f"phi = {phi:.4f} is indistinguishable from a random walk in a sample "
            f"of {n}. OLS is biased downward here, so |phi| < 1 is not evidence of "
            f"stationarity"
            + (f"; the implied half-life of {half_life:.0f} periods also exceeds "
               f"half the sample, so the long-run mean is not identified"
               if half_life_exceeds_sample else "")
            + ". Unconditional moments are withheld — difference the series "
              "or test formally (ADF) before forecasting.")
    return out


def trend_model(y: np.ndarray, kind: str = "linear") -> dict:
    """Deterministic trend: linear, quadratic, or log-linear."""
    y = np.asarray(y, float).ravel()
    n = len(y)
    if n < 20:
        return {"error": "need 20+ observations"}
    t = np.arange(1, n + 1, dtype=float)
    if kind == "quadratic":
        X, names = np.column_stack([t, t ** 2]), ["t", "t_squared"]
        target = y
    elif kind == "log_linear":
        if np.any(y <= 0):
            return {"error": "log-linear trend needs strictly positive values"}
        X, names, target = t.reshape(-1, 1), ["t"], np.log(y)
    else:
        X, names, target = t.reshape(-1, 1), ["t"], y
    res = ols(target, X, names)
    res["trend_type"] = kind
    if kind == "log_linear" and "coefficients" in res:
        g = res["coefficients"][1]["coefficient"]
        res["implied_growth_per_period_pct"] = round((math.exp(g) - 1) * 100, 5)
    return res


def seasonality(y: np.ndarray, period: int = 12, with_trend: bool = True) -> dict:
    """Seasonal dummies (+ optional trend): y_t = b*t + sum_i g_i D_i,t.

    One dummy per season with NO intercept, which is the parameterisation
    that avoids the dummy-variable trap -- s dummies plus an intercept is
    perfectly collinear and the design matrix is singular.
    """
    y = np.asarray(y, float).ravel()
    n = len(y)
    if n < 2 * period:
        return {"error": f"need at least two full cycles ({2 * period} observations)"}
    D = np.zeros((n, period))
    D[np.arange(n), np.arange(n) % period] = 1.0
    X = np.column_stack([np.arange(1, n + 1, dtype=float), D]) if with_trend else D

    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted = X @ coef
    resid = y - fitted
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((resid ** 2).sum()) / tss if tss > 0 else None
    off = 1 if with_trend else 0
    seas = [round(float(c), 8) for c in coef[off:]]
    return {"period": period, "with_trend": with_trend,
            "trend_coefficient": round(float(coef[0]), 8) if with_trend else None,
            "seasonal_factors": seas,
            "peak_season": int(np.argmax(seas)) + 1,
            "trough_season": int(np.argmin(seas)) + 1,
            "seasonal_range": round(max(seas) - min(seas), 8),
            "r_squared": round(r2, 6) if r2 is not None else None,
            "n": int(n)}


def monte_carlo_estimate(fn, n: int = 100_000, seed: int = 7) -> dict:
    """Monte Carlo with the standard error that says whether N was enough.

    Accuracy improves as 1/sqrt(N), so cutting the error in half costs FOUR
    times the paths. Reporting the standard error alongside the estimate is
    what separates a simulation from a guess with lots of decimal places.
    """
    rng = np.random.default_rng(seed)
    draws = np.asarray(fn(rng, n), dtype=float).ravel()
    m, s = float(draws.mean()), float(draws.std(ddof=1))
    se = s / math.sqrt(len(draws))
    return {"estimate": round(m, 8), "std_dev": round(s, 8),
            "standard_error": round(se, 10), "paths": int(len(draws)),
            "ci_95": [round(m - 1.96 * se, 8), round(m + 1.96 * se, 8)],
            "paths_to_halve_error": int(len(draws) * 4)}
