"""Execution-side quote fade — where the training labels come from.

READ THIS BEFORE TRUSTING ANY METRIC PRODUCED DOWNSTREAM.

Why this module has to exist at all: `router._execute_merged` takes
`min(size, remaining)` from each level, so every venue delivers exactly what it
was allocated. `filled/requested` is therefore identically 1.0, and a label
that is always 1 carries no information. Capturing routing outcomes without an
execution step that can MISS would produce a training set of 100% positives and
a model that learns to predict 1.0 everywhere. The router allocates; something
else has to decide what actually comes back.

What this is: a simulator. No real venue is filling these orders. Its output is
stamped `outcome_source="fade-sim"` everywhere it is persisted, and any model
trained on it inherits that provenance. Concretely:

  * A model fitted on these labels has learned THIS FADE RULE, not real market
    microstructure. Its AUC is a measure of the optimiser, not of edge.
  * What genuinely improves versus the old `_synth_training` path is the
    FEATURE distribution: with `live=True` the depths, spreads and venue
    latencies come from real CCXT order books, so the model at least sees the
    covariate distribution it will be served on. The labels remain synthetic.
  * The honest claim is "the pipeline is real and end-to-end"; it is NOT
    "the model predicts real fills". Do not let the second sentence appear
    anywhere the first one is being demonstrated.

Design note -- the DGP is deliberately NOT logistic-in-linear-features. The old
generator drew labels from a logistic latent function and then fitted a
logistic model to them, so a good score mostly proved the gradient descent
worked. Here the two effects are an exponential latency race and a cubic Hill
function in depth consumption. A linear-logit model is therefore MISSPECIFIED
against this DGP by construction, which is the point: it can now be beaten by a
better model class, so the promotion gate downstream has something real to
measure.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Latency at which roughly 1/e of resting size survives at unit volatility.
TAU_MS = 140.0
# Fraction of displayed depth you can lift before queue position starts to hurt.
R_STAR = 0.35


@dataclass
class VenueFill:
    venue: str
    requested: float
    filled: float
    reachable_frac: float

    @property
    def missed(self) -> float:
        return max(0.0, self.requested - self.filled)


def _survival(latency_ms: float, volatility: float) -> float:
    """Exponential decay: slow venues lose the race to fade, and volatility
    makes quotes fade faster. Bounded (0, 1]."""
    return float(np.exp(-max(latency_ms, 0.0) * (0.5 + max(volatility, 0.0)) / TAU_MS))


def _shoulder(ratio: float) -> float:
    """Hill function in depth consumption: taking a small slice of displayed
    size is essentially free, and reachability collapses past R_STAR. Cubic so
    the knee is sharp -- a linear logit cannot represent this shape."""
    r = max(ratio, 0.0)
    return float(1.0 / (1.0 + (r / R_STAR) ** 3))


def reachable_fraction(*, latency_ms: float, volatility: float,
                       depth_ratio: float) -> float:
    return _survival(latency_ms, volatility) * _shoulder(depth_ratio)


def simulate_fills(allocation: dict, venues, *, volatility: float = 0.8,
                   seed: int | None = None, noise_sd: float = 0.08) -> dict:
    """Apply quote fade to a routing allocation. Returns {venue: VenueFill}.

    The multiplicative noise is what makes the label non-deterministic given
    the features -- without it every row with identical features would share a
    label and the calibration curve would be meaningless.
    """
    rng = np.random.default_rng(seed)
    out: dict[str, VenueFill] = {}
    for v in venues:
        requested = float(allocation.get(v.venue, 0.0) or 0.0)
        if requested <= 0:
            continue
        depth = v.depth() or 1.0
        frac = reachable_fraction(latency_ms=v.latency_ms, volatility=volatility,
                                  depth_ratio=requested / depth)
        realised = frac * (1.0 + rng.normal(0.0, noise_sd))
        realised = float(np.clip(realised, 0.0, 1.0))
        out[v.venue] = VenueFill(v.venue, requested,
                                 round(requested * realised, 8), frac)
    return out
