"""Monte Carlo pricer with variance reduction.

Terminal-price GBM simulation with two independent variance-reduction
levers that stack:
  * antithetic variates      (pairs z, -z)
  * control variate          (analytic BSM discounted payoff of the same draw)
  * optional Sobol QMC        (scipy.stats.qmc) for faster convergence

Returns a 95% CI so the UI can show it next to the closed-form price.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm, qmc

from .base import Option, OptionType, PricingModel, PricingResult, register
from .black_scholes import bsm


def mc_price(opt: Option, paths: int = 100_000, *, antithetic: bool = True,
             control_variate: bool = True, sobol: bool = False,
             seed: int | None = 7) -> PricingResult:
    opt.validate()
    S, K, T, r, q, s = (opt.spot, opt.strike, opt.maturity,
                        opt.rate, opt.dividend, opt.vol)
    call = opt.kind is OptionType.CALL
    disc = math.exp(-r * T)
    drift = (r - q - 0.5 * s * s) * T
    diff = s * math.sqrt(T)

    n = paths // 2 if antithetic else paths
    if sobol:                                   # low-discrepancy normals
        m = int(math.ceil(math.log2(max(n, 2))))
        u = qmc.Sobol(d=1, scramble=True, seed=seed).random_base2(m=m)[:n, 0]
        z = norm.ppf(np.clip(u, 1e-12, 1 - 1e-12))
    else:
        z = np.random.default_rng(seed).standard_normal(n)
    z = np.concatenate([z, -z]) if antithetic else z

    ST = S * np.exp(drift + diff * z)
    payoff = disc * np.maximum((ST - K) if call else (K - ST), 0.0)

    if control_variate:
        cv = disc * (ST - S * math.exp((r - q) * T))     # E[·]=0 martingale
        beta = np.cov(payoff, cv, ddof=1)[0, 1] / np.var(cv, ddof=1)
        payoff = payoff - beta * cv

    price = float(payoff.mean())
    se = float(payoff.std(ddof=1) / math.sqrt(payoff.size))
    return PricingResult(
        price=price, stderr=se,
        ci95=(price - 1.96 * se, price + 1.96 * se),
        model="monte_carlo",
        meta={"paths": int(payoff.size), "antithetic": antithetic,
              "control_variate": control_variate, "sobol": sobol,
              "analytic_ref": bsm(opt).price},
    )


class MonteCarlo(PricingModel):
    name = "monte_carlo"
    def __init__(self, paths: int = 100_000, **kw):
        self.paths, self.kw = paths, kw
    def price(self, opt: Option) -> PricingResult:
        return mc_price(opt, self.paths, **self.kw)


register(MonteCarlo())
