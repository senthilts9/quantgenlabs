"""Cox-Ross-Rubinstein binomial tree — European and American exercise.

Backward induction is vectorised over each time slice with numpy, so a
several-hundred-step tree is still sub-millisecond.
"""
from __future__ import annotations

import math

import numpy as np

from .base import Exercise, Option, OptionType, PricingModel, PricingResult, register


def crr(opt: Option, steps: int = 256) -> PricingResult:
    opt.validate()
    S, K, T, r, q, s = (opt.spot, opt.strike, opt.maturity,
                        opt.rate, opt.dividend, opt.vol)
    call = opt.kind is OptionType.CALL
    american = opt.exercise is Exercise.AMERICAN

    dt = T / steps
    u = math.exp(s * math.sqrt(dt))
    d = 1.0 / u
    p = (math.exp((r - q) * dt) - d) / (u - d)
    if not 0.0 <= p <= 1.0:
        raise ValueError("risk-neutral prob out of [0,1] — check dt / vol")
    disc = math.exp(-r * dt)

    j = np.arange(steps + 1)
    ST = S * u ** (steps - j) * d ** j
    v = np.maximum((ST - K) if call else (K - ST), 0.0)

    for step in range(steps - 1, -1, -1):
        v = disc * (p * v[:-1] + (1.0 - p) * v[1:])
        if american:
            ST = S * u ** (step - np.arange(step + 1)) * d ** np.arange(step + 1)
            ex = (ST - K) if call else (K - ST)
            v = np.maximum(v, ex)

    return PricingResult(price=float(v[0]), model="binomial",
                         meta={"steps": steps, "american": american})


class Binomial(PricingModel):
    name = "binomial"
    def __init__(self, steps: int = 256):
        self.steps = steps
    def price(self, opt: Option) -> PricingResult:
        return crr(opt, self.steps)


register(Binomial())
