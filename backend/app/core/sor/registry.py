"""Versioned model registry for the SOR fill model.

Before this, the served model was a process-global in `api/sor.py` that got
refitted from a hardcoded seed on every boot. That arrangement cannot answer
any of the questions that make a model operable:

  * which model produced yesterday's routing decisions;
  * what it was trained on, and how much of it;
  * whether the model now serving is better than the one it replaced.

So the artifact is persisted with its provenance and its out-of-sample metrics,
exactly one version per kind is `champion`, and superseded versions are archived
rather than deleted -- routing decisions already logged in `route_observations`
reference `model_version`, and those references have to stay resolvable.

Storage is the OMS SQLite database. That is a deliberate choice, not laziness:
the observations and the models that consumed them then live in one file, so a
backup can never capture one without the other.
"""
from __future__ import annotations

import json

from .ml import FillModel, train_fill_model

KIND = "sor_fill"


def save(store, model: FillModel, metrics: dict, *, n_train: int, n_test: int,
         status: str = "challenger", notes: str | None = None) -> int:
    """Persist a fitted model and return its version number."""
    return store.save_model(
        params=model.to_params(), metrics=metrics,
        sample_source=model.sample_source, n_train=n_train, n_test=n_test,
        kind=KIND, status=status, notes=notes)


def load(store, version: int | None = None) -> tuple[FillModel, dict] | None:
    """Load a model + its stored metrics. `version=None` means the champion.

    Returns None when there is nothing to load, so the caller decides whether
    to bootstrap -- silently fabricating a model here would hide the fact that
    the registry is empty.
    """
    row = store.get_model(version) if version is not None else store.champion_model(KIND)
    if not row:
        return None
    metrics = json.loads(row["metrics"])
    model = FillModel.from_params(json.loads(row["params"]),
                                  sample_source=row["sample_source"],
                                  version=row["version"])
    return model, metrics


def bootstrap(store) -> tuple[FillModel, dict]:
    """Seed an empty registry with the synthetic model as v1 champion.

    The synthetic model is a legitimate cold-start: it is better than routing
    with no fill-probability discount at all, and it is registered with its
    provenance string intact so nobody mistakes it for a trained-on-live model.
    """
    from .ml import training_metrics

    model = train_fill_model()
    metrics = training_metrics(model)
    version = save(store, model, metrics,
                   n_train=int(len(model._holdout[1]) / 0.3 * 0.7) if model._holdout else 0,
                   n_test=metrics.get("n_test", 0), status="champion",
                   notes="cold-start synthetic model; not fitted on captured routes")
    model.version = version
    metrics["model_version"] = version
    return model, metrics


def get_or_bootstrap(store) -> tuple[FillModel, dict]:
    return load(store) or bootstrap(store)


def history(store, limit: int = 50) -> list[dict]:
    """Registry contents, newest first, with metrics parsed for display."""
    out = []
    for r in store.list_models(KIND, limit):
        d = {k: r[k] for k in ("version", "created_at", "status",
                               "sample_source", "n_train", "n_test", "notes")}
        m = json.loads(r["metrics"])
        d["metrics"] = {k: m.get(k) for k in
                        ("auc", "brier", "brier_baseline", "brier_skill_score",
                         "log_loss", "accuracy", "base_rate", "n_test")}
        out.append(d)
    return out
