"""ML layer for SOR — learned fill probability per venue.

A production SOR doesn't treat displayed size as fully fillable: quotes fade,
latency causes misses, and hidden/iceberg liquidity varies by venue. We learn
a fill-probability model P(fill | features) and use it to *discount each
venue's usable size* inside the router — so the optimiser routes to where
liquidity is not just cheap but actually reachable.

Implemented as logistic regression trained by gradient descent in numpy (no
sklearn dependency), on features every SOR has: order-size / displayed-depth
ratio, venue latency, spread, and short-horizon volatility. Swap in
GradientBoosting / a sequence model behind the same `predict` interface.

HONEST STATUS OF THIS MODEL -- read before quoting its metrics anywhere:

  * It is trained on SYNTHETIC data from `_synth_training`, whose labels come
    from a known logistic latent function. Fitting a logistic model to a
    logistic DGP mostly measures whether the optimiser works. It demonstrates
    the plumbing -- features -> probability -> size discount in the router --
    NOT predictive power on real fills.
  * Metrics are therefore reported OUT-OF-SAMPLE on a held-out split, with a
    base-rate baseline alongside. An earlier version evaluated on the training
    set with the same seed, which inflates every number and is exactly the
    kind of thing a reviewer checks first.
  * `sample_source` is stamped on the metrics so a caller can never mistake
    synthetic-trained numbers for live-trained ones. Real labelled rows come
    from `core.tca.fill_training_rows` once the OMS has accumulated enough
    history to be worth fitting.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import capture

FEATURES = capture.FEATURES

SYNTHETIC_SOURCE = ("synthetic (logistic DGP) — demonstrates the pipeline, "
                    "not predictive power on live fills")


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


@dataclass
class FillModel:
    w: np.ndarray
    b: float
    mu: np.ndarray
    sd: np.ndarray
    # Provenance travels WITH the weights, not alongside them. A model loaded
    # from the registry must be able to state what it was fitted on without
    # the caller having to remember, or synthetic-trained numbers eventually
    # get quoted as if they were live-trained.
    sample_source: str = SYNTHETIC_SOURCE
    version: int | None = None
    # (X_test, y_test) held out at fit time; never used for training
    _holdout: tuple | None = None

    def predict(self, feats: dict) -> float:
        x = np.array([feats[k] for k in FEATURES], float)
        xs = (x - self.mu) / self.sd
        return float(_sigmoid(xs @ self.w + self.b))

    # ---- registry serialisation ----
    def to_params(self) -> dict:
        return {"w": [float(x) for x in self.w], "b": float(self.b),
                "mu": [float(x) for x in self.mu], "sd": [float(x) for x in self.sd],
                "features": list(FEATURES)}

    @classmethod
    def from_params(cls, p: dict, *, sample_source: str = SYNTHETIC_SOURCE,
                    version: int | None = None) -> "FillModel":
        stored = tuple(p.get("features") or FEATURES)
        if stored != tuple(FEATURES):
            # Loading weights fitted against a different feature order would
            # silently mis-map coefficients to inputs -- worse than no model,
            # because it still returns plausible-looking probabilities.
            raise ValueError(
                f"model feature set {stored} does not match current {FEATURES}")
        return cls(np.asarray(p["w"], float), float(p["b"]),
                   np.asarray(p["mu"], float), np.asarray(p["sd"], float),
                   sample_source=sample_source, version=version)


def _synth_training(n=6000, seed=11):
    """Generate labelled data with a realistic structure: fills get LESS likely
    as size/depth and latency and volatility rise."""
    rng = np.random.default_rng(seed)
    size_ratio = rng.gamma(2.0, 0.25, n)               # ~0..2
    latency = rng.uniform(1, 80, n)
    spread = rng.gamma(2.0, 1.5, n)
    vol = rng.gamma(2.0, 0.4, n)
    X = np.column_stack([size_ratio, latency, spread, vol])
    # latent fill logit: large size, latency, vol hurt; tight spread helps
    z = (3.4 - 2.9 * size_ratio - 0.035 * latency - 0.09 * spread - 1.5 * vol
         + rng.normal(0, 0.18, n))
    y = (_sigmoid(z) > rng.uniform(size=n)).astype(float)
    return X, y


def _split(X, y, test_frac=0.3, seed=11):
    """Deterministic shuffle-split. Held-out data is never seen in training."""
    rng = np.random.default_rng(seed + 1)
    idx = rng.permutation(len(y))
    cut = int(len(y) * (1 - test_frac))
    tr, te = idx[:cut], idx[cut:]
    return X[tr], y[tr], X[te], y[te]


def _fit(X, y, epochs: int, lr: float):
    """Batch gradient descent on standardised features. Shared by the synthetic
    and the captured-data paths so both are optimised identically -- otherwise
    a champion/challenger comparison measures the optimiser, not the data."""
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xs = (X - mu) / sd
    w = np.zeros(Xs.shape[1])
    b = 0.0
    n = len(y)
    for _ in range(epochs):
        p = _sigmoid(Xs @ w + b)
        w -= lr * (Xs.T @ (p - y) / n)
        b -= lr * float((p - y).mean())
    return w, b, mu, sd


def train_fill_model(epochs=700, lr=0.4, seed=11) -> FillModel:
    X_all, y_all = _synth_training(seed=seed)
    X, y, X_te, y_te = _split(X_all, y_all, seed=seed)
    w, b, mu, sd = _fit(X, y, epochs, lr)
    model = FillModel(w, b, mu, sd, sample_source=SYNTHETIC_SOURCE)
    # Stash the held-out split on the model so metrics can never accidentally
    # be computed on training rows.
    model._holdout = (X_te, y_te)
    return model


# Below this many labelled rows a fit is noise; the caller is told to keep
# collecting rather than handed a model that looks trained.
MIN_TRAINING_ROWS = 500


def observations_to_xy(rows: list[dict]):
    """Captured rows -> (X, y) in the canonical feature order."""
    X = np.array([[float(r[k]) for k in FEATURES] for r in rows], float)
    y = np.array([float(r["label_filled"]) for r in rows], float)
    return X, y


def train_from_observations(rows: list[dict], *, epochs=700, lr=0.4, seed=11,
                            test_frac=0.3) -> FillModel:
    """Fit on rows captured by `capture.observe_route`.

    Raises rather than returning a degenerate model when the data cannot
    support a fit. Both failure modes below produce a model that scores
    beautifully and is worthless, so they are errors, not warnings:

      * too few rows -- the held-out split would be a handful of points;
      * a single class -- if every venue filled, there is nothing to separate,
        and the fitted intercept just encodes the base rate.
    """
    if len(rows) < MIN_TRAINING_ROWS:
        raise ValueError(
            f"need >= {MIN_TRAINING_ROWS} labelled rows to fit, have {len(rows)}")
    X_all, y_all = observations_to_xy(rows)
    if len(np.unique(y_all)) < 2:
        raise ValueError(
            "captured rows contain a single class — every routed venue "
            f"{'filled' if y_all[0] else 'missed'}; nothing to learn")

    X, y, X_te, y_te = _split(X_all, y_all, test_frac=test_frac, seed=seed)
    w, b, mu, sd = _fit(X, y, epochs, lr)

    sources = sorted({r.get("outcome_source", "?") for r in rows})
    feat_sources = sorted({r.get("data_source", "?") for r in rows})
    model = FillModel(w, b, mu, sd, sample_source=(
        f"captured routes (n={len(rows)}) — labels from {'/'.join(sources)}, "
        f"features from {'/'.join(feat_sources)}"))
    model._holdout = (X_te, y_te)
    return model


def fill_probabilities(model: FillModel, venues, order_qty: float,
                       volatility: float = 0.8) -> dict:
    """Map each venue to a fill probability the router uses to discount size."""
    # Features come from `capture.venue_features` -- the SAME function the
    # logging path uses. Two copies of this arithmetic is how train/serve skew
    # gets introduced silently.
    return {v.venue: model.predict(capture.venue_features(v, order_qty, volatility))
            for v in venues}


def _auc(y, p) -> float:
    """ROC-AUC via the Mann-Whitney U statistic (ties handled by average rank)."""
    order = np.argsort(p)
    ranks = np.empty(len(p), float)
    ranks[order] = np.arange(1, len(p) + 1)
    n_pos, n_neg = y.sum(), (1 - y).sum()
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def calibration_curve(y, p, bins: int = 10) -> list[dict]:
    """Predicted vs realised frequency per probability bucket.

    Discrimination (AUC) and calibration are different properties: a model can
    rank perfectly and still be systematically over-confident. The router
    multiplies size by this probability, so calibration is what actually
    matters for sizing -- an over-confident model over-allocates."""
    out = []
    edges = np.linspace(0, 1, bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.sum() == 0:
            continue
        out.append({"bucket": f"{lo:.1f}-{hi:.1f}", "n": int(m.sum()),
                    "predicted": round(float(p[m].mean()), 4),
                    "observed": round(float(y[m].mean()), 4)})
    return out


def training_metrics(model: FillModel) -> dict:
    """OUT-OF-SAMPLE metrics on the held-out split, with a baseline.

    Reported against a base-rate baseline (predict the training prevalence for
    everything). A model that cannot beat the base rate on Brier score has
    learned nothing useful, and quoting AUC without that comparison hides it.
    """
    if model._holdout is None:
        if model.sample_source != SYNTHETIC_SOURCE:
            # A model deserialised from the registry carries no eval set. Its
            # metrics were computed against ITS OWN held-out data at fit time
            # and stored beside the weights -- read them from there. Scoring a
            # captured-data model against the synthetic generator instead
            # would report a number that describes neither.
            raise ValueError(
                "no held-out split on this model; use the metrics stored in "
                "the registry for the version it was loaded from")
        X_all, y_all = _synth_training(seed=11)     # legacy synthetic model
        _, _, X, y = _split(X_all, y_all, seed=11)
    else:
        X, y = model._holdout

    Xs = (X - model.mu) / model.sd
    p = _sigmoid(Xs @ model.w + model.b)

    base_rate = float(y.mean())
    brier = float(np.mean((p - y) ** 2))
    brier_base = float(np.mean((base_rate - y) ** 2))
    eps = 1e-12
    log_loss = float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))

    return {
        "auc": round(_auc(y, p), 3),
        "brier": round(brier, 3),
        "brier_baseline": round(brier_base, 3),
        # >0 means the model beats predicting the base rate for everything.
        "brier_skill_score": round(1 - brier / brier_base, 3) if brier_base > 0 else None,
        "log_loss": round(log_loss, 4),
        "accuracy": round(float(((p > 0.5).astype(float) == y).mean()), 3),
        "base_rate": round(base_rate, 3),
        "n_test": int(len(y)),
        "evaluation": "out-of-sample (30% held out)",
        # Provenance is part of the metric. Synthetic-trained numbers must
        # never be presentable as evidence of real-world predictive power.
        "sample_source": model.sample_source,
        "model_version": model.version,
        "calibration": calibration_curve(y, p),
    }


def training_accuracy(model: FillModel) -> float:   # kept for back-compat
    return training_metrics(model)["accuracy"]
