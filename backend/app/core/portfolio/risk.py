"""Portfolio risk: factor exposures, VaR, and risk attribution.

Built on the REAL OMS book and REAL FRED history -- no simulated returns.

The piece worth reviewing is COMPONENT VaR. Marginal VaR alone tells you the
sensitivity to a small increase in a holding; component VaR tells you how much
of today's portfolio risk that holding is actually responsible for, and under
Euler allocation the components SUM EXACTLY to portfolio VaR. That additivity
is not decoration -- it is what lets a PM say "cut this position, lose that
much risk" and have the numbers tie out. It is asserted here, not assumed.

Two VaR methods are reported side by side on purpose:

  * PARAMETRIC (variance-covariance) supports the exact Euler decomposition,
    but assumes normality and will understate tails.
  * HISTORICAL simulation makes no distributional assumption and captures the
    fat tails that actually hurt, but decomposes only approximately.

Quoting one without the other hides whichever weakness matters that day.
"""
from __future__ import annotations

import math

import numpy as np

# 99% one-tailed normal quantile. Hardcoded rather than pulled from scipy so
# the risk core has no heavyweight dependency.
_Z = {0.90: 1.281552, 0.95: 1.644854, 0.975: 1.959964, 0.99: 2.326348,
      0.995: 2.575829}


def _z(conf: float) -> float:
    """One-tailed normal quantile.

    The table covers the confidences a risk report actually uses. The fallback
    was a hand-rolled rational approximation that turned out to be accurate
    only in the far tail -- it is replaced by scipy, which is already a
    dependency. Guessing at a quantile is not worth avoiding an import.
    """
    if conf in _Z:
        return _Z[conf]
    from scipy.stats import norm
    return float(norm.ppf(conf))


def returns_from_levels(levels: list[float]) -> np.ndarray:
    """Simple returns from a price/level series, dropping non-finite rows."""
    x = np.asarray(levels, dtype=float)
    x = x[np.isfinite(x) & (x != 0)]
    if len(x) < 2:
        return np.array([])
    return np.diff(x) / x[:-1]


def align_on_dates(levels: dict[str, list[float]],
                   dates: dict[str, list[str]],
                   symbols: list[str]) -> tuple[np.ndarray, list[str]]:
    """Return (levels matrix, dates) on the INTERSECTION of trading dates.

    Taking "the last n of each series" is not alignment. FRED series have
    different holiday calendars and publication lags: SP500 and VIXCLS can
    both return 260 rows that end on different days and skip different
    holidays. Pairing them positionally silently shifts one series against
    the other, and the damage is not subtle -- it flipped measured SP500/VIX
    correlation from -0.83 to +0.63, which would make a volatility hedge look
    like it ADDED risk. Align on the date key or do not align at all.
    """
    common = None
    for s in symbols:
        ds = set(dates.get(s) or [])
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    if not common:
        return np.empty((0, len(symbols))), []
    cols = []
    for s in symbols:
        by_date = dict(zip(dates[s], levels[s]))
        cols.append([by_date[d] for d in common])
    return np.column_stack(cols), common


def analyse(exposures: dict[str, float],
            return_series: dict[str, list[float]],
            benchmark: str | None = None,
            confidence: float = 0.99,
            horizon_days: int = 1,
            dates: dict[str, list[str]] | None = None) -> dict:
    """Full risk decomposition for a book of signed exposures (currency).

    `exposures` is SIGNED notional per symbol (short = negative), which is
    what makes a hedged book show low risk instead of double-counting gross.

    `dates` is REQUIRED for a multi-asset book -- see align_on_dates for why
    positional alignment is not safe.
    """
    symbols = [s for s in exposures if s in return_series]
    if not symbols:
        return {"error": "no return history for any position",
                "symbols_requested": sorted(exposures)}

    all_syms = symbols + ([benchmark] if benchmark and benchmark in return_series
                          and benchmark not in symbols else [])
    if dates and all(s in dates for s in all_syms):
        L, common_dates = align_on_dates(return_series, dates, all_syms)
        if L.shape[0] < 31:
            return {"error": f"insufficient overlapping history "
                             f"({L.shape[0]} common dates, need 31+)",
                    "symbols": symbols}
        R_all = np.diff(L, axis=0) / L[:-1]
        R = R_all[:, :len(symbols)]
        bench_aligned = (R_all[:, all_syms.index(benchmark)]
                         if benchmark in all_syms else None)
        n = R.shape[0]
        alignment = f"date-aligned on {len(common_dates)} common trading days"
    else:
        # Single asset, or caller supplied no dates: positional is safe only
        # when there is nothing to mis-align against.
        series = [returns_from_levels(return_series[s]) for s in symbols]
        n = min((len(r) for r in series), default=0)
        if n < 30:
            return {"error": f"insufficient history ({n} observations, need 30+)",
                    "symbols": symbols}
        R = np.column_stack([r[-n:] for r in series])
        bench_aligned = None
        alignment = ("positional (single asset)" if len(symbols) == 1
                     else "POSITIONAL — dates not supplied, correlations unreliable")
    w = np.array([exposures[s] for s in symbols], dtype=float)
    total_abs = float(np.abs(w).sum())
    if total_abs == 0:
        return {"error": "book is flat"}

    cov = np.cov(R, rowvar=False, ddof=1)
    if cov.ndim == 0:
        cov = cov.reshape(1, 1)

    port_ret = R @ w                                     # currency P&L per day
    sigma_p = float(np.sqrt(w @ cov @ w))
    z = _z(confidence)
    scale = math.sqrt(horizon_days)

    var_param = z * sigma_p * scale
    hist_q = float(np.quantile(port_ret, 1 - confidence))
    var_hist = -hist_q * scale
    tail = port_ret[port_ret <= hist_q]
    es_hist = float(-tail.mean()) * scale if len(tail) else var_hist

    # Euler / component VaR. MVaR_i = z * (cov @ w)_i / sigma_p, and
    # CVaR_i = w_i * MVaR_i sums exactly to portfolio VaR.
    marginal = (z * (cov @ w) / sigma_p * scale) if sigma_p > 0 else np.zeros_like(w)
    component = w * marginal

    bench_ret = bench_aligned
    if bench_ret is None and benchmark and benchmark in return_series:
        b = returns_from_levels(return_series[benchmark])
        if len(b) >= n:
            bench_ret = b[-n:]

    positions = []
    for i, s in enumerate(symbols):
        beta = None
        if bench_ret is not None:
            bv = float(np.var(bench_ret, ddof=1))
            if bv > 0:
                beta = float(np.cov(R[:, i], bench_ret, ddof=1)[0, 1] / bv)
        positions.append({
            "symbol": s,
            "exposure": round(float(w[i]), 2),
            "weight_pct": round(float(w[i] / total_abs * 100), 2),
            "vol_annual_pct": round(float(R[:, i].std(ddof=1) * math.sqrt(252) * 100), 3),
            "marginal_var": round(float(marginal[i]), 4),
            "component_var": round(float(component[i]), 4),
            "pct_of_risk": round(float(component[i] / var_param * 100), 2) if var_param else 0.0,
            "beta": round(beta, 4) if beta is not None else None,
        })

    corr = None
    if len(symbols) > 1:
        sd = np.sqrt(np.diag(cov))
        with np.errstate(divide="ignore", invalid="ignore"):
            c = cov / np.outer(sd, sd)
        corr = [[round(float(v), 4) if np.isfinite(v) else None for v in row] for row in c]

    port_beta = None
    if bench_ret is not None:
        bv = float(np.var(bench_ret, ddof=1))
        if bv > 0:
            port_beta = float(np.cov(port_ret, bench_ret, ddof=1)[0, 1] / bv) / total_abs

    return {
        "symbols": symbols,
        "n_observations": int(n),
        "confidence": confidence,
        "horizon_days": horizon_days,
        "gross_exposure": round(total_abs, 2),
        "net_exposure": round(float(w.sum()), 2),
        "portfolio_vol_daily": round(sigma_p, 4),
        "portfolio_vol_annual_pct": round(
            float(sigma_p / total_abs * math.sqrt(252) * 100), 3),
        "var_parametric": round(float(var_param), 4),
        "var_historical": round(float(var_hist), 4),
        "es_historical": round(float(es_hist), 4),
        # >1 means the empirical tail is fatter than the normal assumption --
        # the single most useful diagnostic on this whole screen.
        "tail_ratio_hist_over_param": round(float(var_hist / var_param), 3) if var_param else None,
        "portfolio_beta": round(port_beta, 4) if port_beta is not None else None,
        "benchmark": benchmark,
        "alignment": alignment,
        "positions": positions,
        "correlation": corr,
        "component_var_sum": round(float(component.sum()), 4),
        # Euler additivity check. If this drifts from zero the decomposition
        # is wrong and no attribution below it can be trusted.
        "euler_check_diff": round(abs(float(component.sum() - var_param)), 8),
        "_returns": R,
        "_weights": w,
        "_symbols": symbols,
        "_port_ret": port_ret,
    }


def public(res: dict) -> dict:
    return {k: v for k, v in res.items() if not k.startswith("_")}
