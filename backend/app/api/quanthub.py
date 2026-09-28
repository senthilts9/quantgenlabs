"""Risk & Quant Hub — the quantitative foundations, computed on real data.

Every panel here answers "why does this matter on a trading desk", not "here
is a formula". Each block runs on the live book's own return series wherever
one exists, so the distributions being compared are the ones the desk is
actually exposed to.

The fat-tail block is the point of the whole screen. A normal distribution is
the assumption under every parametric VaR number, and it is wrong in the one
region that matters. Showing the empirical histogram against the fitted normal
makes the failure visible: the middle fits well, and the tail -- where the
losses live -- does not.
"""
from __future__ import annotations

import math

import numpy as np
from fastapi import APIRouter, Query
from scipy import stats

router = APIRouter(prefix="/api/quant", tags=["quant-hub"])


def _series(days: int = 750) -> tuple[np.ndarray | None, dict]:
    """The book's return series, or the benchmark if the book is flat."""
    from .portfolio import BENCHMARK, _book
    from ..core.portfolio.risk import align_on_dates

    exposures, history, dates, _, _, _ = _book(days)
    syms = [s for s in exposures if s in history and s in dates]
    if syms:
        L, common = align_on_dates(history, dates, syms)
        if L.shape[0] > 60:
            R = np.diff(L, axis=0) / L[:-1]
            w = np.array([exposures[s] for s in syms], float)
            gross = float(np.abs(w).sum()) or 1.0
            return (R @ w) / gross, {"source": "live book",
                                     "symbols": syms, "n": int(L.shape[0] - 1),
                                     "from": common[1], "to": common[-1]}
    # Fall back to the benchmark rather than to simulated data: a synthetic
    # series would make every distribution on this screen look textbook-clean,
    # which is the opposite of what it is meant to demonstrate.
    if BENCHMARK in history and len(history[BENCHMARK]) > 60:
        lv = np.asarray(history[BENCHMARK], float)
        return np.diff(lv) / lv[:-1], {"source": f"{BENCHMARK} (book is flat)",
                                       "symbols": [BENCHMARK], "n": len(lv) - 1,
                                       "from": dates[BENCHMARK][1],
                                       "to": dates[BENCHMARK][-1]}
    return None, {"error": "no return history available"}


@router.get("/distributions")
def distributions(days: int = Query(750, ge=120, le=2000),
                  bins: int = Query(51, ge=21, le=101)) -> dict:
    """Empirical returns vs the fitted normal — the fat-tail exhibit.

    Returns a histogram, the normal PDF fitted to the SAME mean and variance,
    a Q-Q plot, and the tail counts that decide whether a parametric VaR is
    safe to quote.
    """
    r, meta = _series(days)
    if r is None:
        return {"empty": True, **meta}

    mu, sd = float(r.mean()), float(r.std(ddof=1))
    z = (r - mu) / sd
    lo, hi = float(np.quantile(z, 0.001)), float(np.quantile(z, 0.999))
    lo, hi = min(lo, -4.5), max(hi, 4.5)
    edges = np.linspace(lo, hi, bins + 1)
    counts, _ = np.histogram(z, bins=edges, density=True)
    centres = (edges[:-1] + edges[1:]) / 2
    normal_pdf = stats.norm.pdf(centres)

    # Tail counts: how often the market delivered a move the normal says is
    # essentially impossible. This is the number that ends the argument.
    n = len(z)
    tails = []
    for k in (1, 2, 3, 4, 5):
        emp = int((np.abs(z) > k).sum())
        exp = 2 * (1 - stats.norm.cdf(k)) * n
        tails.append({
            "sigma": k, "observed": emp, "normal_expected": round(exp, 3),
            "ratio": round(emp / exp, 2) if exp > 0 else None,
            "once_every_normal": (f"1 in {1 / (2 * (1 - stats.norm.cdf(k))):,.0f} days"
                                  if k < 7 else "never"),
        })

    # Q-Q: empirical quantiles against theoretical normal quantiles. A straight
    # line means normal; the characteristic S-bend at the ends is fat tails.
    probs = np.linspace(0.005, 0.995, 99)
    qq = [{"theoretical": round(float(stats.norm.ppf(p)), 4),
           "empirical": round(float(np.quantile(z, p)), 4)} for p in probs]

    jb_stat, jb_p = stats.jarque_bera(r)
    sk = float(stats.skew(r))
    ku = float(stats.kurtosis(r))          # already excess

    worst = float(z.min())
    return {
        "meta": meta,
        "moments": {
            "mean_daily": round(mu, 8), "vol_daily": round(sd, 8),
            "vol_annual_pct": round(sd * math.sqrt(252) * 100, 3),
            "skew": round(sk, 4), "excess_kurtosis": round(ku, 4),
            "jarque_bera": round(float(jb_stat), 3),
            "jarque_bera_p": round(float(jb_p), 8),
            "normal_rejected": bool(jb_p < 0.05),
        },
        "histogram": {
            "centres": [round(float(c), 4) for c in centres],
            "empirical": [round(float(c), 6) for c in counts],
            "normal": [round(float(v), 6) for v in normal_pdf],
        },
        "qq_plot": qq,
        "tail_counts": tails,
        "worst_day_sigma": round(worst, 2),
        "worst_day_normal_odds": (
            f"a {abs(worst):.1f}-sigma day. Under a normal that is a 1-in-"
            f"{1 / max(stats.norm.cdf(worst), 1e-300):,.0f}-day event — longer than "
            f"the age of the universe. It happened in this sample."),
        "interpretation": {
            "headline": ("Returns are NOT normal. The middle of the distribution fits "
                         "well, which is why the assumption survives casual inspection; "
                         "the tails do not, which is exactly where risk lives."
                         if jb_p < 0.05 else
                         "Normality is not rejected on this sample."),
            "skew": ("negative skew — losses are larger than gains of the same "
                     "frequency, the usual shape for a long risk book"
                     if sk < -0.1 else
                     "positive skew — the right tail is the longer one"
                     if sk > 0.1 else "roughly symmetric"),
            "kurtosis": (f"excess kurtosis {ku:.2f}: tails are far heavier than normal, "
                         f"so a parametric VaR built on this sample UNDERSTATES the "
                         f"loss it is meant to bound" if ku > 1 else
                         f"excess kurtosis {ku:.2f}: close to normal tails"),
            "why_it_matters": ("Parametric VaR multiplies volatility by a normal "
                               "quantile. If the true tail is heavier than normal, "
                               "that number is too small precisely on the days it is "
                               "supposed to protect you."),
        },
    }


@router.get("/probability")
def probability(n: int = Query(20, ge=1, le=200), p: float = Query(0.3, gt=0, lt=1),
                lam: float = Query(3.0, gt=0, le=50)) -> dict:
    """Binomial and Poisson, and the normal approximation to the binomial.

    The binomial is not an abstraction here: a VaR exception is a Bernoulli
    trial, and the count of exceptions over a backtest window is exactly
    binomial. That is the distribution Kupiec's test is built on.
    """
    ks = np.arange(0, n + 1)
    pmf = stats.binom.pmf(ks, n, p)
    mean, var = n * p, n * p * (1 - p)

    # Normal approximation is only defensible when np and n(1-p) are both >= 5;
    # stated rather than silently drawn, since it fails badly for small np.
    approx_ok = min(n * p, n * (1 - p)) >= 5
    normal_curve = stats.norm.pdf(ks, mean, math.sqrt(var)) if var > 0 else np.zeros_like(ks, float)

    pk = np.arange(0, max(int(lam * 4), 12))
    return {
        "binomial": {
            "n": n, "p": p, "mean": round(mean, 4), "variance": round(var, 4),
            "std_dev": round(math.sqrt(var), 4),
            "k": [int(k) for k in ks], "pmf": [round(float(v), 8) for v in pmf],
            "normal_approx": [round(float(v), 8) for v in normal_curve],
            "normal_approx_valid": bool(approx_ok),
            "approx_note": (f"np = {n * p:.1f} and n(1-p) = {n * (1 - p):.1f}; both "
                            f"exceed 5, so the normal approximation is reasonable"
                            if approx_ok else
                            f"np = {n * p:.1f}, n(1-p) = {n * (1 - p):.1f} — below 5, "
                            f"so the normal approximation is NOT safe here; the "
                            f"binomial is skewed and the approximation misses the tail"),
            "desk_meaning": ("A VaR exception is a Bernoulli trial with p = 1 - "
                             "confidence. Over a 250-day backtest the exception COUNT "
                             "is binomial, which is precisely what Kupiec's "
                             "proportion-of-failures test evaluates."),
        },
        "poisson": {
            "lambda": lam, "mean": lam, "variance": lam,
            "k": [int(k) for k in pk],
            "pmf": [round(float(v), 8) for v in stats.poisson.pmf(pk, lam)],
            "desk_meaning": ("Counts of rare independent events per period — the "
                             "standard model for operational-loss FREQUENCY. Mean "
                             "equals variance, which is itself a testable claim: "
                             "real loss counts are often over-dispersed."),
        },
    }


@router.get("/linear-algebra")
def linear_algebra(days: int = Query(500, ge=120, le=2000)) -> dict:
    """Covariance eigen-decomposition on the live book.

    PCA is not decoration in risk: the eigenvalues ARE the independent sources
    of variance, and a covariance matrix whose smallest eigenvalue is near zero
    is near-singular. That matters because portfolio optimisation inverts it --
    and inverting a near-singular matrix amplifies estimation noise into
    enormous, unstable position weights. The condition number is the warning.
    """
    from .portfolio import _book
    from ..core.portfolio.risk import align_on_dates

    exposures, history, dates, _, _, _ = _book(days)
    syms = [s for s in exposures if s in history and s in dates]
    if len(syms) < 2:
        return {"empty": True, "message": "need 2+ positions with history"}

    L, common = align_on_dates(history, dates, syms)
    if L.shape[0] < 40:
        return {"empty": True, "message": f"only {L.shape[0]} overlapping dates"}
    R = np.diff(L, axis=0) / L[:-1]

    cov = np.cov(R, rowvar=False, ddof=1)
    sd = np.sqrt(np.diag(cov))
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = cov / np.outer(sd, sd)
    corr = np.nan_to_num(corr, nan=0.0)

    evals, evecs = np.linalg.eigh(cov)
    order = np.argsort(evals)[::-1]
    evals, evecs = evals[order], evecs[:, order]
    total = float(evals.sum())
    explained = [float(v / total) for v in evals] if total > 0 else []
    cum = float(np.cumsum(explained)[0]) if explained else 0.0

    pos = evals[evals > 0]
    cond = float(pos.max() / pos.min()) if len(pos) and pos.min() > 0 else None
    n_for_90 = int(np.searchsorted(np.cumsum(explained), 0.90) + 1) if explained else 0

    return {
        "symbols": syms, "n_observations": int(R.shape[0]),
        "alignment": f"date-aligned on {len(common)} common trading days",
        "correlation_matrix": [[round(float(v), 4) for v in row] for row in corr],
        "eigenvalues": [round(float(v), 10) for v in evals],
        "explained_variance_pct": [round(v * 100, 3) for v in explained],
        "cumulative_pct": [round(float(v) * 100, 3) for v in np.cumsum(explained)],
        "first_factor_pct": round(cum * 100, 2),
        "factors_for_90pct": n_for_90,
        "condition_number": round(cond, 1) if cond else None,
        "effective_rank": int((evals > total * 1e-6).sum()),
        "interpretation": {
            "concentration": (f"the first principal component explains "
                              f"{cum * 100:.1f}% of variance — the book has one "
                              f"dominant risk driver, not {len(syms)} independent ones"
                              if cum > 0.5 else
                              f"variance is spread across factors; {n_for_90} are "
                              f"needed to reach 90%, so the book is genuinely "
                              f"diversified"),
            "conditioning": (
                f"condition number {cond:,.0f} — the covariance matrix is "
                f"ill-conditioned. Optimisers invert this matrix, and inversion "
                f"amplifies estimation error by roughly this factor, producing "
                f"extreme and unstable weights. Shrinkage or a factor model is "
                f"required before optimising."
                if cond and cond > 1000 else
                f"condition number {cond:,.0f} — well-conditioned enough to invert "
                f"for optimisation." if cond else "condition number unavailable"),
            "why_it_matters": ("Correlation is the reason portfolio risk is not the "
                               "sum of position risks. The eigenvalues quantify how "
                               "many INDEPENDENT bets the book actually contains, "
                               "which is almost always fewer than the position count."),
        },
    }


@router.get("/timeseries")
def timeseries(days: int = Query(750, ge=120, le=2000),
               lags: int = Query(20, ge=5, le=60)) -> dict:
    """Autocorrelation of returns vs |returns| — the volatility-clustering exhibit.

    Two results that look contradictory and are not:

      Returns are almost uncorrelated. That is market efficiency; if yesterday's
      return predicted today's, the trade would already have been done.

      ABSOLUTE returns are strongly, persistently autocorrelated. Big moves
      follow big moves regardless of sign. Volatility is forecastable even
      though direction is not, and that single fact is why GARCH and EWMA exist
      and why a constant-volatility VaR is mis-specified.
    """
    r, meta = _series(days)
    if r is None:
        return {"empty": True, **meta}

    def acf(x: np.ndarray, k: int) -> list[float]:
        x = x - x.mean()
        denom = float((x ** 2).sum())
        return [round(float((x[:-i] * x[i:]).sum() / denom), 5) if denom else 0.0
                for i in range(1, k + 1)]

    ret_acf = acf(r, lags)
    abs_acf = acf(np.abs(r), lags)
    # 95% white-noise band: +/- 1.96/sqrt(n). Bars inside it are indistinguishable
    # from zero, so the band is what makes the plot readable rather than decorative.
    band = 1.96 / math.sqrt(len(r))

    from ..core.portfolio.frm import ewma_vol, garch11
    g = garch11(r)
    e = ewma_vol(r)

    sig_ret = sum(1 for v in ret_acf if abs(v) > band)
    sig_abs = sum(1 for v in abs_acf if abs(v) > band)

    return {
        "meta": meta, "lags": lags,
        "significance_band": round(band, 5),
        "returns_acf": ret_acf,
        "abs_returns_acf": abs_acf,
        "significant_lags_returns": sig_ret,
        "significant_lags_abs": sig_abs,
        "garch": {k: g.get(k) for k in
                  ("alpha", "beta", "persistence", "long_run_vol_annual_pct",
                   "current_vol_annual_pct", "half_life_days", "impl",
                   "grid_evaluations")} if "error" not in g else g,
        "ewma": {k: e.get(k) for k in
                 ("lambda", "sigma_annual_pct", "half_life_days")} if "error" not in e else e,
        "interpretation": {
            "returns": (f"{sig_ret} of {lags} return lags exceed the white-noise band "
                        f"— returns are essentially unpredictable, which is what market "
                        f"efficiency looks like"),
            "volatility": (f"{sig_abs} of {lags} ABSOLUTE-return lags are significant "
                           f"— volatility clusters strongly. Direction is unforecastable "
                           f"while magnitude is not."),
            "consequence": ("This is why a constant-volatility VaR is mis-specified: it "
                            "under-states risk in a high-vol regime and over-states it "
                            "in a calm one. GARCH and EWMA exist to track exactly this "
                            "persistence."),
            "persistence": (f"GARCH persistence alpha+beta = {g.get('persistence')}: "
                            f"shocks to variance decay with a half-life of about "
                            f"{g.get('half_life_days')} days"
                            if "error" not in g else "GARCH unavailable"),
        },
    }


@router.get("/statistics")
def statistics(days: int = Query(750, ge=120, le=2000),
               confidence: float = Query(0.95, gt=0.5, lt=1)) -> dict:
    """Estimation uncertainty — the error bars nobody puts on a risk number.

    A VaR is an ESTIMATE from a finite sample, and it carries a standard error.
    Quoting it to the dollar while the 95% confidence interval spans tens of
    percent is false precision, and this endpoint exists to make that visible
    by bootstrapping the sampling distribution.
    """
    r, meta = _series(days)
    if r is None:
        return {"empty": True, **meta}

    n = len(r)
    mu, sd = float(r.mean()), float(r.std(ddof=1))
    se_mean = sd / math.sqrt(n)
    tcrit = float(stats.t.ppf(0.5 + confidence / 2, n - 1))

    # Bootstrap the VaR estimate. Resampling with replacement gives the
    # sampling distribution directly, with no distributional assumption --
    # which matters because the whole point is that returns are not normal.
    rng = np.random.default_rng(11)
    q = 1 - confidence
    boots = np.array([np.quantile(rng.choice(r, n, replace=True), q) for _ in range(800)])
    var_pt = float(np.quantile(r, q))
    lo, hi = np.quantile(boots, [0.025, 0.975])

    # How many observations sit in the tail the VaR is estimated from. If it is
    # a handful, the estimate is driven by a handful of days.
    tail_n = int((r <= var_pt).sum())

    return {
        "meta": meta, "n": n, "confidence": confidence,
        "mean": {"estimate": round(mu, 8), "std_error": round(se_mean, 8),
                 "ci": [round(mu - tcrit * se_mean, 8), round(mu + tcrit * se_mean, 8)],
                 "significantly_nonzero": bool(abs(mu) > tcrit * se_mean)},
        "volatility": {"daily": round(sd, 8),
                       "annual_pct": round(sd * math.sqrt(252) * 100, 3),
                       # chi-squared CI for a variance estimate
                       "annual_ci_pct": [
                           round(sd * math.sqrt((n - 1) / stats.chi2.ppf(0.975, n - 1))
                                 * math.sqrt(252) * 100, 3),
                           round(sd * math.sqrt((n - 1) / stats.chi2.ppf(0.025, n - 1))
                                 * math.sqrt(252) * 100, 3)]},
        "var_estimate": {
            "point": round(var_pt * 100, 4),
            "ci_95": [round(float(lo) * 100, 4), round(float(hi) * 100, 4)],
            "relative_width_pct": round(float(abs(hi - lo) / abs(var_pt)) * 100, 1)
                                  if var_pt else None,
            "observations_in_tail": tail_n,
            "bootstrap_samples": 800,
        },
        "interpretation": {
            "false_precision": (
                f"The {confidence:.0%} VaR point estimate is {var_pt * 100:.3f}% but its "
                f"95% confidence interval spans {abs(hi - lo) / abs(var_pt) * 100:.0f}% "
                f"of its own value. It is estimated from {tail_n} tail observations. "
                f"Quoting it to the dollar implies a precision the sample does not "
                f"support." if var_pt else ""),
            "central_limit": (
                f"The mean has a standard error of {se_mean:.2e} on {n} observations. "
                f"Standard error falls as 1/sqrt(n), so halving it needs four times "
                f"the data — and four times the history means reaching back into a "
                f"different market regime, which is the real constraint."),
            "why_it_matters": ("Risk numbers are estimates, not measurements. A model "
                               "that reports VaR without its estimation error invites "
                               "the reader to over-trust it."),
        },
    }
