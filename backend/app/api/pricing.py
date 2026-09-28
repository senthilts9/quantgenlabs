"""Pricing API — thin router over the core engine. All calls are `timed`."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from ..core import engines as E
from ..core import pricing as P
from ..models import CrossCheckOut, GreeksOut, OptionRequest, PriceOut
from ..obs import timed

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


def _to_option(r: OptionRequest) -> P.Option:
    return P.Option(
        spot=r.spot, strike=r.strike, maturity=r.maturity, rate=r.rate,
        dividend=r.dividend, vol=r.vol,
        kind=P.OptionType(r.kind), exercise=P.Exercise(r.exercise),
    )


def _out(res: P.PricingResult, ms: float) -> PriceOut:
    g = res.greeks
    greeks = None
    if any((g.delta, g.gamma, g.vega, g.theta, g.rho)):
        greeks = GreeksOut(delta=g.delta, gamma=g.gamma, vega=g.vega,
                           theta=g.theta, rho=g.rho)
    return PriceOut(model=res.model, price=res.price, greeks=greeks,
                    stderr=res.stderr, ci95=res.ci95, latency_ms=ms,
                    meta=res.meta)


@router.get("/models")
def models() -> list[str]:
    return P.available()


# These route through core.engines, NOT the numpy functions directly. The
# selector picks the compiled C++ path when the extension is loaded and
# labels the metric with the impl it actually used. Calling P.bsm() here with
# a hardcoded "python" label meant /api/health could report cpp_available
# while every price on the platform was computed in numpy -- the dashboard
# and the reality disagreed, which is the one thing observability must not do.
@router.post("/black-scholes", response_model=PriceOut)
def price_bsm(req: OptionRequest) -> PriceOut:
    opt = _to_option(req)
    t0 = time.perf_counter()
    d = E.price_black_scholes(opt)
    g = d.get("greeks") or {}
    res = P.PricingResult(
        price=d["price"], model="black_scholes",
        greeks=P.Greeks(**{k: g.get(k, 0.0) for k in
                           ("delta", "gamma", "vega", "theta", "rho")}),
        meta={"impl": d["impl"]})
    return _out(res, (time.perf_counter() - t0) * 1000)


@router.post("/binomial", response_model=PriceOut)
def price_binomial(req: OptionRequest) -> PriceOut:
    opt = _to_option(req)
    t0 = time.perf_counter()
    d = E.price_binomial(opt, req.binomial_steps)
    res = P.PricingResult(price=d["price"], model="binomial",
                          meta={"impl": d["impl"], "steps": req.binomial_steps})
    return _out(res, (time.perf_counter() - t0) * 1000)


@router.post("/lsm", response_model=PriceOut)
def price_lsm(req: OptionRequest, paths: int = 200_000, steps: int = 50) -> PriceOut:
    """American option by Longstaff-Schwartz. The C++ LSM engine, finally wired.

    `meta.early_exercise_premium` is the honest self-check: for an American put
    it must be strictly positive, and it is what distinguishes this from the
    European Monte Carlo that sits one endpoint above.
    """
    opt = _to_option(req)
    t0 = time.perf_counter()
    d = E.price_lsm(opt, paths, steps)
    res = P.PricingResult(
        price=d["price"], model="longstaff_schwartz", stderr=d.get("stderr"),
        meta={k: v for k, v in d.items() if k not in ("price", "stderr")})
    return _out(res, (time.perf_counter() - t0) * 1000)


@router.post("/monte-carlo", response_model=PriceOut)
def price_mc(req: OptionRequest) -> PriceOut:
    opt = _to_option(req)
    t0 = time.perf_counter()
    d = E.price_monte_carlo(opt, req.mc_paths)
    ci = d.get("ci95")
    res = P.PricingResult(
        price=d["price"], model="monte_carlo", stderr=d.get("stderr"),
        ci95=(tuple(ci) if ci else None),
        meta={"impl": d["impl"], "paths": d.get("paths", req.mc_paths)})
    return _out(res, (time.perf_counter() - t0) * 1000)


@router.post("/cross-check", response_model=CrossCheckOut)
def cross_check(req: OptionRequest) -> CrossCheckOut:
    """Price three ways; agreement within cents validates the engine."""
    bs = price_bsm(req)
    bn = price_binomial(req)
    mc = price_mc(req)
    prices = [bs.price, bn.price, mc.price]
    return CrossCheckOut(black_scholes=bs, binomial=bn, monte_carlo=mc,
                         max_abs_diff=max(prices) - min(prices))
