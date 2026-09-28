"""Pricing engine — shared contracts.

Every pricer implements the same tiny interface so adding a model is a
registration, not a rewrite. Cores are pure (no I/O, no globals) and
vectorised where it matters, which keeps them unit-testable and fast.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class Exercise(str, Enum):
    EUROPEAN = "european"
    AMERICAN = "american"


@dataclass(frozen=True, slots=True)
class Option:
    """A vanilla option contract + market inputs (continuous compounding)."""
    spot: float
    strike: float
    maturity: float          # years
    rate: float              # risk-free, cont. comp.
    dividend: float = 0.0    # continuous dividend yield
    vol: float = 0.20        # annualised
    kind: OptionType = OptionType.CALL
    exercise: Exercise = Exercise.EUROPEAN

    def validate(self) -> None:
        if self.spot <= 0 or self.strike <= 0:
            raise ValueError("spot and strike must be positive")
        if self.maturity <= 0:
            raise ValueError("maturity must be positive")
        if self.vol < 0:
            raise ValueError("vol must be non-negative")


@dataclass(frozen=True, slots=True)
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0        # per 1 vol point (÷100)
    theta: float = 0.0       # per calendar day (÷365)
    rho: float = 0.0         # per 1% rate move (÷100)


@dataclass(frozen=True, slots=True)
class PricingResult:
    price: float
    greeks: Greeks = field(default_factory=Greeks)
    stderr: float | None = None            # MC only
    ci95: tuple[float, float] | None = None
    model: str = ""
    meta: dict = field(default_factory=dict)


@runtime_checkable
class PricingModel(Protocol):
    name: str
    def price(self, opt: Option) -> PricingResult: ...


# --- registry -------------------------------------------------------------
_REGISTRY: dict[str, PricingModel] = {}


def register(model: PricingModel) -> PricingModel:
    _REGISTRY[model.name] = model
    return model


def get(name: str) -> PricingModel:
    if name not in _REGISTRY:
        raise KeyError(f"unknown pricing model '{name}'. have: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def available() -> list[str]:
    return sorted(_REGISTRY)
