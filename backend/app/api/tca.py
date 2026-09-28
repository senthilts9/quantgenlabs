"""TCA API — execution quality measured from the OMS's own fill history."""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..core import tca

router = APIRouter(prefix="/api/tca", tags=["tca"])


@router.get("/analysis")
def analysis(limit: int = Query(500, le=2000),
             desk: str = Query("traditional",
                               pattern="^(traditional|digital|all)$")) -> dict:
    """Execution quality for one desk.

    Scoped by default: crypto fills come from 24/7 venues with their own fee
    schedules and tick regimes. Averaging their slippage together with an FX
    book's produces a firm-wide bps number that describes neither.
    """
    from .oms import _in_desk, _marks, oms

    engine = oms()
    orders = [
        {**{k: (v.value if hasattr(v, "value") else v)
            for k, v in o.__dict__.items()}}
        for o in engine.store.list_orders(limit)
        if _in_desk(o.symbol, desk)
    ]
    fills = [
        {**{k: (v.value if hasattr(v, "value") else v)
            for k, v in f.__dict__.items()}}
        for f in engine.store.list_fills(limit)
        if _in_desk(f.symbol, desk)
    ]
    from ..core import sor
    from .sor import _model

    out = tca.analyse(orders, fills, _marks())
    rows = tca.fill_training_rows(orders, fills)

    # Metrics come from the registered champion, not a fresh fit. This used to
    # call train_fill_model() inline, which ran 700 gradient-descent epochs on
    # every page load AND could report a different model than the router was
    # actually serving.
    model, metrics = _model()
    obs = engine.store.route_observation_stats()
    out["fill_model"] = {
        **metrics,
        "model_version": model.version,
        # Order-level history, kept for the blotter view. NOT the model's
        # training set: `label_complete` is per-ORDER while the model's
        # features are per-VENUE, so these rows cannot label it. The venue-
        # level training set lives in route_observations below.
        "order_level_rows": len(rows),
        "order_level_complete_rate": (
            round(sum(r["label_complete"] for r in rows) / len(rows), 3)
            if rows else None),
        "captured_venue_rows": obs["total"],
        "captured_by_source": obs["by_outcome_source"],
        "retrain_threshold": sor.MIN_TRAINING_ROWS,
        "using_live_data": model.sample_source != sor.ml.SYNTHETIC_SOURCE,
    }
    return out
