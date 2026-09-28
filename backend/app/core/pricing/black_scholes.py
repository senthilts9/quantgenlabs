"""Black-Scholes-Merton closed form + analytic Greeks."""
from __future__ import annotations

import math

from .base import Greeks, Option, OptionType, PricingModel, PricingResult, register

SQRT2PI = math.sqrt(2.0 * math.pi)


def _n(x: float) -> float:      # standard normal pdf
    return math.exp(-0.5 * x * x) / SQRT2PI


def _N(x: float) -> float:      # standard normal cdf (erf-based, full precision)
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bsm(opt: Option) -> PricingResult:
    opt.validate()
    S, K, T, r, q, s = (opt.spot, opt.strike, opt.maturity,
                        opt.rate, opt.dividend, opt.vol)
    call = opt.kind is OptionType.CALL
    sgn = 1.0 if call else -1.0

    if s <= 0 or T <= 0:                       # degenerate → intrinsic
        fwd = S * math.exp((r - q) * T)
        price = math.exp(-r * T) * max(sgn * (fwd - K), 0.0)
        return PricingResult(price=price, model="black_scholes")

    st = s * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * s * s) * T) / st
    d2 = d1 - st
    df, dq = math.exp(-r * T), math.exp(-q * T)

    price = sgn * (S * dq * _N(sgn * d1) - K * df * _N(sgn * d2))
    delta = sgn * dq * _N(sgn * d1)
    gamma = dq * _n(d1) / (S * st)
    vega = S * dq * _n(d1) * math.sqrt(T) / 100.0
    theta = (-S * dq * _n(d1) * s / (2 * math.sqrt(T))
             - sgn * r * K * df * _N(sgn * d2)
             + sgn * q * S * dq * _N(sgn * d1)) / 365.0
    rho = sgn * K * T * df * _N(sgn * d2) / 100.0

    return PricingResult(
        price=price,
        greeks=Greeks(delta, gamma, vega, theta, rho),
        model="black_scholes",
    )


class BlackScholes(PricingModel):
    name = "black_scholes"
    def price(self, opt: Option) -> PricingResult:
        return bsm(opt)


register(BlackScholes())
