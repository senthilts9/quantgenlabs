"""FRM Part I — Foundations of Risk Management.

CAPM, APT, Fama-French, and the four performance ratios, on real return
series rather than worked examples.

The ratios differ only in what goes in the DENOMINATOR, and that choice is
the whole point:

  Sharpe  -- total volatility. Penalises upside deviation too, which is why a
             convex payoff can look worse than it is.
  Treynor -- beta, i.e. SYSTEMATIC risk only. Meaningless for a book carrying
             large idiosyncratic exposure, because the risk being divided out
             is not the risk being run. Beta is always reported next to it so
             that judgement is possible.
  Sortino -- downside deviation below a Minimum Acceptable Return. Upside
             dispersion is not risk to an investor.
  IR      -- tracking error. Answers a different question from the others:
             skill against a benchmark, not return per unit of risk.

Jensen's alpha is a residual, not a ratio: return in excess of what CAPM says
the beta alone should have earned.
"""
from __future__ import annotations

import math

import numpy as np

TRADING_DAYS = 252


def _ann(r: np.ndarray) -> tuple[float, float]:
    """Annualised (mean, stdev) from a daily series."""
    return (float(r.mean()) * TRADING_DAYS,
            float(r.std(ddof=1)) * math.sqrt(TRADING_DAYS))


def beta(asset: np.ndarray, market: np.ndarray) -> dict:
    """beta_i = cov(R_i, R_M) / var(R_M), plus the R^2 that qualifies it.

    R^2 is reported because beta alone is silent about how much of the
    asset's variance the market actually explains. A beta of 1.1 with an R^2
    of 0.04 is not a systematic exposure worth hedging -- it is noise, and
    Treynor / Jensen numbers built on it are not meaningful.
    """
    a, m = np.asarray(asset, float), np.asarray(market, float)
    n = min(len(a), len(m))
    if n < 30:
        return {"error": f"need 30+ paired observations, got {n}"}
    a, m = a[-n:], m[-n:]
    var_m = float(np.var(m, ddof=1))
    if var_m <= 0:
        return {"error": "market series has zero variance"}
    cov = float(np.cov(a, m, ddof=1)[0, 1])
    b = cov / var_m
    corr = float(np.corrcoef(a, m)[0, 1])
    return {"beta": round(b, 5), "covariance": round(cov, 10),
            "market_variance": round(var_m, 10),
            "correlation": round(corr, 5), "r_squared": round(corr ** 2, 5),
            "n": int(n),
            "systematic_share_pct": round(corr ** 2 * 100, 2),
            "idiosyncratic_share_pct": round((1 - corr ** 2) * 100, 2)}


def capm(beta_i: float, rf_annual: float, market_return_annual: float) -> dict:
    """Security Market Line: E(R_i) = Rf + beta_i * [E(R_M) - Rf]."""
    erp = market_return_annual - rf_annual
    return {"expected_return_pct": round((rf_annual + beta_i * erp) * 100, 4),
            "risk_free_pct": round(rf_annual * 100, 4),
            "equity_risk_premium_pct": round(erp * 100, 4),
            "beta": beta_i,
            "note": "equilibrium return for this beta; alpha is the residual "
                    "an actual portfolio earns above it"}


def apt(rf_annual: float, betas: dict[str, float],
        premia: dict[str, float]) -> dict:
    """E(R) = Rf + sum_j b_j * RP_j.

    APT specifies the STRUCTURE of returns but not the factors. Factors named
    here are an assumption of the user, not a result of the model -- which is
    exactly the freedom that makes APT both more general and less falsifiable
    than CAPM. CAPM is the special case with one factor.
    """
    missing = [k for k in betas if k not in premia]
    if missing:
        return {"error": f"no risk premium supplied for factor(s): {missing}"}
    contrib = {k: betas[k] * premia[k] for k in betas}
    total = sum(contrib.values())
    return {
        "expected_return_pct": round((rf_annual + total) * 100, 4),
        "risk_free_pct": round(rf_annual * 100, 4),
        "factor_contributions_pct": {k: round(v * 100, 4) for k, v in contrib.items()},
        "total_risk_premium_pct": round(total * 100, 4),
        "factors": sorted(betas),
    }


def fama_french(asset: np.ndarray, mkt_rf: np.ndarray, smb: np.ndarray,
                hml: np.ndarray, rf: np.ndarray | None = None) -> dict:
    """Three-factor model by OLS: R_i - Rf = a + b*(Mkt-Rf) + s*SMB + h*HML.

    The intercept is the alpha that SURVIVES size and value exposure. A
    manager whose CAPM alpha vanishes here was not adding skill; they were
    being paid for tilting small and cheap, which an index fund also does.
    """
    a = np.asarray(asset, float)
    X_cols = [np.asarray(x, float) for x in (mkt_rf, smb, hml)]
    n = min([len(a)] + [len(x) for x in X_cols])
    if n < 40:
        return {"error": f"need 40+ observations, got {n}"}
    y = a[-n:] - (np.asarray(rf, float)[-n:] if rf is not None else 0.0)
    X = np.column_stack([np.ones(n)] + [x[-n:] for x in X_cols])

    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    tss = float(((y - y.mean()) ** 2).sum())
    rss = float((resid ** 2).sum())
    r2 = 1 - rss / tss if tss > 0 else None
    k = X.shape[1] - 1
    adj = (1 - (1 - r2) * (n - 1) / (n - k - 1)) if r2 is not None and n > k + 1 else None

    # Standard errors from the OLS covariance matrix, so the loadings can be
    # judged for significance rather than just reported.
    dof = n - k - 1
    se = None
    if dof > 0:
        try:
            xtx_inv = np.linalg.inv(X.T @ X)
            se = np.sqrt(np.diag(xtx_inv) * rss / dof)
        except np.linalg.LinAlgError:
            se = None
    names = ["alpha", "mkt_rf", "smb", "hml"]
    out = {}
    for i, nm in enumerate(names):
        d = {"coefficient": round(float(coef[i]), 6)}
        if se is not None and se[i] > 0:
            d["std_error"] = round(float(se[i]), 6)
            d["t_stat"] = round(float(coef[i] / se[i]), 3)
            d["significant_5pct"] = bool(abs(coef[i] / se[i]) > 1.96)
        out[nm] = d
    out.update({
        "alpha_annualised_pct": round(float(coef[0]) * TRADING_DAYS * 100, 4),
        "r_squared": round(r2, 5) if r2 is not None else None,
        "adj_r_squared": round(adj, 5) if adj is not None else None,
        "n": int(n),
    })
    return out


def performance_suite(r: np.ndarray, benchmark: np.ndarray | None = None,
                      rf_annual: float = 0.0, mar_annual: float = 0.0) -> dict:
    """Sharpe, Sortino, Treynor, Jensen, IR, tracking error, Calmar."""
    r = np.asarray(r, float)
    if len(r) < 30:
        return {"error": f"need 30+ observations, got {len(r)}"}
    rf_d, mar_d = rf_annual / TRADING_DAYS, mar_annual / TRADING_DAYS
    mu_a, sd_a = _ann(r)

    sharpe = (mu_a - rf_annual) / sd_a if sd_a > 0 else None

    # Sortino: deviation below the MAR, not below zero and not total vol.
    # The mean is taken over ALL observations, not just the losing ones --
    # dividing by the count of downside days instead inflates the ratio.
    short = np.minimum(r - mar_d, 0.0)
    dd = float(np.sqrt((short ** 2).mean())) * math.sqrt(TRADING_DAYS)
    sortino = (mu_a - mar_annual) / dd if dd > 0 else None

    eq = np.cumprod(1.0 + r)
    max_dd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
    years = len(r) / TRADING_DAYS
    cagr = float(eq[-1] ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else None

    out = {
        "annual_return_pct": round(mu_a * 100, 4),
        "annual_vol_pct": round(sd_a * 100, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "sortino": round(sortino, 4) if sortino is not None else None,
        "downside_deviation_pct": round(dd * 100, 4),
        "minimum_acceptable_return_pct": round(mar_annual * 100, 3),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "calmar": round(cagr / abs(max_dd), 4) if (cagr and max_dd < 0) else None,
        "risk_free_pct": round(rf_annual * 100, 3),
        "observations": int(len(r)),
    }

    if benchmark is not None:
        b = np.asarray(benchmark, float)
        n = min(len(r), len(b))
        r2s, b2 = r[-n:], b[-n:]
        bi = beta(r2s, b2)
        if "error" not in bi:
            bt = bi["beta"]
            bench_a = float(b2.mean()) * TRADING_DAYS
            active = r2s - b2
            te = float(active.std(ddof=1)) * math.sqrt(TRADING_DAYS)
            out.update({
                "beta": bt,
                "r_squared_vs_benchmark": bi["r_squared"],
                "treynor": round((mu_a - rf_annual) / bt, 5) if bt else None,
                "jensens_alpha_pct": round(
                    ((mu_a - rf_annual) - bt * (bench_a - rf_annual)) * 100, 4),
                "tracking_error_pct": round(te * 100, 4),
                "information_ratio": round(
                    float(active.mean()) * TRADING_DAYS / te, 4) if te > 0 else None,
                "benchmark_return_pct": round(bench_a * 100, 4),
            })
            if bi["r_squared"] < 0.25:
                out["treynor_warning"] = (
                    f"R^2 vs benchmark is only {bi['r_squared']:.2f} — beta explains "
                    "little of this book's variance, so Treynor and Jensen are "
                    "dividing out risk the portfolio is not actually taking")
    return out
