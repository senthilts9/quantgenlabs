"""Risk API — VaR/ES across methods and VaR model backtesting."""
from __future__ import annotations

import time
from dataclasses import asdict

import numpy as np
from fastapi import APIRouter, HTTPException

from ..core import risk as R
from ..models import BacktestRequest, VaROut, VaRRequest
from ..obs import timed

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/var/methods")
def methods() -> list[str]:
    return sorted(R.VAR_METHODS)


@router.post("/var", response_model=list[VaROut])
def value_at_risk(req: VaRRequest) -> list[VaROut]:
    r = np.asarray(req.returns, float)
    chosen = req.methods or list(R.VAR_METHODS)
    out: list[VaROut] = []
    for name in chosen:
        fn = R.VAR_METHODS.get(name)
        if fn is None:
            raise HTTPException(400, f"unknown method '{name}'")
        t0 = time.perf_counter()
        with timed(f"var_{name}", "numpy"):
            res = fn(r, req.confidence)
        if req.portfolio_value:
            res = res.scaled(req.portfolio_value)
        out.append(VaROut(method=res.method, confidence=res.confidence,
                          var=res.var, es=res.es,
                          latency_ms=(time.perf_counter() - t0) * 1000))
    return out


@router.post("/var/backtest")
def var_backtest(req: BacktestRequest) -> dict:
    with timed("var_backtest", "numpy"):
        res = R.backtest_var(np.asarray(req.pnl, float),
                             np.asarray(req.var_forecast, float),
                             req.confidence)
    return asdict(res)
