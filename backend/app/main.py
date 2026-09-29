"""QuantGenLabs — unified backend.

One FastAPI app fronting every engine:
  * core pricing/risk (Meridian)          /api/pricing/*  /api/risk/*
  * crypto quant/risk (XRPL/perps)        /api/crypto/*
  * Claude risk copilot (desk-grounded)   /api/copilot/*
  * observability (profiler snapshot)     /api/obs/*  + Prometheus /metrics

Engine selection (numpy vs compiled C++) is handled in app.core.engines and
recorded through the same obs.timed() seam, so Grafana shows cpp-vs-numpy
p50/p99 for every hot path — pricing, risk, MC, LSM and crypto alike.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000   (from backend/)
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict

import numpy as np
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic import BaseModel

from .api import (auth, connectivity, desk, bench, cryptodesk, cryptodesk_auth, frm, genai, marketdata,
                  observability, oms, dex_board, portfolio, pricing,
                  quanthub, rates, risk, sor, tca)
from .core import crypto
from .services.copilot import router as copilot_router
from .services import (crypto_quotes, engine_calibration, heartbeat,
                       warm_board)

app = FastAPI(title="Cryptolabs — Risk & Quant Console", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

HTTP_LATENCY = Histogram(
    "meridian_http_request_seconds", "HTTP request latency",
    labelnames=("method", "path", "status"),
    buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5))


@app.middleware("http")
async def instrument(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    HTTP_LATENCY.labels(request.method, path, str(response.status_code)).observe(
        time.perf_counter() - t0)
    response.headers["X-Process-Time-ms"] = f"{(time.perf_counter()-t0)*1000:.2f}"
    return response


app.mount("/metrics", make_asgi_app())

# --- core routers (pricing / risk / observability) ---
app.include_router(pricing.router)
app.include_router(risk.router)
app.include_router(observability.router)
app.include_router(sor.router)
app.include_router(oms.router)
app.include_router(marketdata.router)
app.include_router(rates.router)
app.include_router(tca.router)
app.include_router(portfolio.router)
app.include_router(bench.router)
app.include_router(auth.router)
app.include_router(connectivity.router)
app.include_router(desk.router)
app.include_router(frm.router)
app.include_router(quanthub.router)
app.include_router(genai.router)
app.include_router(cryptodesk.router)
app.include_router(cryptodesk_auth.router)
app.include_router(dex_board.router)
app.include_router(copilot_router)


# --- crypto endpoints ---
class SwapReq(BaseModel):
    reserve_in: float; reserve_out: float; amount_in: float; fee: float = 0.003

class MarginReq(BaseModel):
    entry: float; mark: float; qty: float; leverage: float
    is_long: bool = True; maintenance_margin_rate: float = 0.005

class CryptoVarReq(BaseModel):
    returns: list[float]; confidence: float = 0.99; methods: list[str] | None = None


@app.post("/api/crypto/amm/swap", tags=["crypto"])
def amm_swap(req: SwapReq) -> dict:
    # SwapResult etc. are slots=True dataclasses -- no __dict__ to spread,
    # asdict() introspects the declared fields instead (see observability.py
    # for the same fix, and why the bug is easy to miss until real data flows).
    return asdict(crypto.amm.swap_out(req.reserve_in, req.reserve_out,
                                      req.amount_in, req.fee))

@app.get("/api/crypto/amm/impermanent-loss", tags=["crypto"])
def il(price_ratio: float) -> dict:
    return {"price_ratio": price_ratio,
            "impermanent_loss": crypto.amm.impermanent_loss(price_ratio)}

@app.post("/api/crypto/margin", tags=["crypto"])
def margin(req: MarginReq) -> dict:
    return asdict(crypto.liquidation.margin_state(
        req.entry, req.mark, req.qty, req.leverage,
        req.is_long, req.maintenance_margin_rate))

@app.post("/api/crypto/risk/var", tags=["crypto"])
def crypto_var(req: CryptoVarReq) -> list[dict]:
    r = np.asarray(req.returns, float)
    chosen = req.methods or list(crypto.risk.CRYPTO_VAR_METHODS)
    return [asdict(crypto.risk.CRYPTO_VAR_METHODS[m](r, req.confidence))
            for m in chosen if m in crypto.risk.CRYPTO_VAR_METHODS]


@app.on_event("startup")
async def _start_background() -> None:
    """Engine metrics stay alive; EOD data is refreshed into SQLite; live
    crypto quotes are primed BEFORE the first request can arrive.

    The prime is awaited rather than backgrounded on purpose. Crypto is the one
    thing here that cannot be persisted -- a mid is only meaningful at the
    moment it is read -- so it is also the only thing a restart genuinely has
    to re-fetch. Paying that once at boot, where nobody is waiting on a
    response, is what keeps it off the request path. It is bounded, so
    unreachable venues cost a few seconds of startup, not a hung container.
    """
    import asyncio
    await crypto_quotes.prime()
    asyncio.create_task(heartbeat.run())
    asyncio.create_task(engine_calibration.run())
    asyncio.create_task(crypto_quotes.run())
    asyncio.create_task(warm_board.run())


@app.get("/api/health")
def health() -> dict:
    from .core.engines import engine_info
    return {"status": "ok", "service": "quantgenlabs", "version": app.version,
            "engines": engine_info(),
            "copilot": "claude" if os.getenv("ANTHROPIC_API_KEY") else "offline"}
