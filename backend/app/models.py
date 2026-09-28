"""API schemas — the wire contract, kept separate from the engine dataclasses."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---- pricing ----
class OptionRequest(BaseModel):
    spot: float = Field(gt=0)
    strike: float = Field(gt=0)
    maturity: float = Field(gt=0, description="years")
    rate: float = 0.045
    dividend: float = 0.0
    vol: float = Field(0.2, ge=0)
    kind: str = Field("call", pattern="^(call|put)$")
    exercise: str = Field("european", pattern="^(european|american)$")
    binomial_steps: int = Field(256, ge=8, le=4000)
    mc_paths: int = Field(100_000, ge=1000, le=5_000_000)


class GreeksOut(BaseModel):
    delta: float; gamma: float; vega: float; theta: float; rho: float


class PriceOut(BaseModel):
    model: str
    price: float
    greeks: GreeksOut | None = None
    stderr: float | None = None
    ci95: tuple[float, float] | None = None
    latency_ms: float
    meta: dict = {}


class CrossCheckOut(BaseModel):
    black_scholes: PriceOut
    binomial: PriceOut
    monte_carlo: PriceOut
    max_abs_diff: float


# ---- risk ----
class VaRRequest(BaseModel):
    returns: list[float] = Field(min_length=30)
    confidence: float = Field(0.99, gt=0.5, lt=1.0)
    portfolio_value: float | None = None
    methods: list[str] | None = None


class VaROut(BaseModel):
    method: str
    confidence: float
    var: float
    es: float
    latency_ms: float


class BacktestRequest(BaseModel):
    pnl: list[float] = Field(min_length=30)
    var_forecast: list[float] = Field(min_length=30)
    confidence: float = Field(0.99, gt=0.5, lt=1.0)


# ---- observability ----
class StatOut(BaseModel):
    engine: str
    impl: str
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    last_ms: float
    ewma_ms: float
