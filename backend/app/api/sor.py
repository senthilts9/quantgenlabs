"""Smart Order Routing API — the platform's primary workflow."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import sor
from ..core.oms import OrderType, Side as OmsSide, evaluate

router = APIRouter(prefix="/api/sor", tags=["smart-order-routing"])

# Process cache of the champion. Keyed by nothing -- there is exactly one
# champion per kind by construction -- and invalidated on promotion so a
# promote takes effect without a restart.
_MODEL: tuple | None = None


def _store():
    from .oms import oms
    return oms().store


def _model() -> tuple:
    """(FillModel, metrics) for the current champion, bootstrapping if the
    registry is empty. Metrics come from the registry rather than being
    recomputed: they belong to the held-out split from that model's own fit,
    which no longer exists in memory once the artifact is deserialised."""
    global _MODEL
    if _MODEL is None:
        _MODEL = sor.registry.get_or_bootstrap(_store())
    return _MODEL


def _invalidate_model_cache() -> None:
    global _MODEL
    _MODEL = None


class RouteReq(BaseModel):
    side: str = "buy"
    qty: float = 2500.0
    mid: float = 100.0
    use_ml: bool = True
    seed: int = 7
    live: bool = False                     # pull real books via CCXT
    symbol: str = "BTC/USDT"
    exchanges: list[str] | None = None
    volatility: float = 0.8                # model input; also drives quote fade


def _route_core(req: RouteReq):
    """Shared routing path. Returns the response dict plus the objects
    `/execute` needs to simulate fills and log training rows -- recomputing
    the route there would produce a second, different book snapshot and the
    logged features would not be the ones the decision was made on."""
    side = sor.Side(req.side)
    source = "synthetic"
    if req.live:
        exs = tuple(req.exchanges) if req.exchanges else sor.ccxt_feed.DEFAULT_EXCHANGES
        venues, mid, source = sor.ccxt_feed.live_or_synth(req.symbol, side, exs)
    else:
        venues, mid = sor.synth_market(side, mid=req.mid, seed=req.seed)
    fp = None
    ml_metrics = None
    version = None
    if req.use_ml:
        m, ml_metrics = _model()
        version = m.version
        fp = sor.fill_probabilities(m, venues, req.qty, volatility=req.volatility)
    res = sor.route(req.qty, venues, side, mid, fill_prob=fp)
    out = res.summary()
    out["data_source"] = source
    out["venues"] = [{"venue": v.venue, "type": v.vtype.value, "fee_bps": v.fee_bps,
                      "depth": round(v.depth(), 0), "top": round(v.best_price(), 4),
                      "latency_ms": v.latency_ms} for v in venues]
    out["fill_probabilities"] = fp
    out["ml_metrics"] = ml_metrics
    out["model_version"] = version
    out["explanation"] = sor.explain.explain(res, fp)["explanation"]
    return out, venues, fp, version, source, side


@router.post("/route")
def route(req: RouteReq) -> dict:
    return _route_core(req)[0]


class ExecuteReq(RouteReq):
    """Route, then book the result through the OMS as a parent + child orders."""
    reference_price: float | None = None
    fade_seed: int | None = None    # pin the fade draw; tests only


@router.post("/execute")
def execute(req: ExecuteReq) -> dict:
    """Run the router, apply quote fade, and record the outcome in the OMS.

    The parent order carries the full size and the arrival mid, so its
    implementation shortfall is measured against the price when the decision
    was made. Each venue allocation becomes a child order filled at that
    venue's own achieved price -- which is what makes venue-level TCA
    possible later, rather than collapsing everything into one average.

    The pre-trade gate runs on the PARENT before any child is created: a
    route that breaches a limit must not leave half-executed children behind.

    Children now book the FADED quantity, not the requested one. The router
    allocates against displayed size; `sor.fade` decides how much of that was
    actually reachable, so a parent can end PARTIALLY_FILLED. That is both
    more realistic and the reason the fill model has a non-degenerate label to
    learn from -- see the warnings in `core/sor/fade.py` about what the
    simulated labels do and do not prove.
    """
    from .oms import _marks, current_limits, oms

    engine = oms()
    routed, venues, fp, model_version, data_source, _side = _route_core(req)
    if not routed.get("filled_qty"):
        raise HTTPException(400, "router returned no fill")

    side = OmsSide(req.side.lower())
    symbol = req.symbol.upper()
    arrival = routed.get("arrival_mid") or req.mid

    decision = evaluate(
        current_limits(engine), symbol=symbol, side=side,
        qty=routed["filled_qty"], reference_price=arrival,
        positions=engine.store.list_positions(), marks=_marks(),
        realized_pnl_today=engine.store.realized_pnl_today())

    parent = engine.place(symbol, side, routed["filled_qty"], OrderType.MARKET,
                          algo="SOR", arrival_mid=arrival)
    if not decision.approved:
        engine.reject(parent, decision.reason or "risk gate")
        for c in decision.checks:
            if not c.passed:
                engine.store.record_breach(
                    symbol=symbol, side=side.value, qty=routed["filled_qty"],
                    gate=c.gate, detail=c.detail, observed=c.observed,
                    limit_val=c.limit, order_id=parent.id)
        return {"routing": routed, "parent": _order_dict(engine, parent.id),
                "children": [], "risk": decision.to_dict()}

    allocation = routed.get("allocation") or {}
    realised = sor.fade.simulate_fills(allocation, venues,
                                       volatility=req.volatility,
                                       seed=req.fade_seed)

    children = []
    vwap = routed["vwap"]
    for venue, qty in allocation.items():
        if qty <= 0:
            continue
        vf = realised.get(venue)
        got = round(vf.filled, 8) if vf else qty
        child = engine.place(symbol, side, qty, OrderType.MARKET,
                             parent_id=parent.id, venue=venue, algo="SOR",
                             arrival_mid=arrival)
        # A venue that faded completely leaves its child NEW with nothing
        # filled. engine.fill rejects qty<=0, and booking a zero fill would
        # also be a lie -- no execution happened there.
        if got > 0:
            # Venue-level prices aren't returned per-allocation by the router,
            # so each child books at the achieved VWAP. Flagged rather than
            # silently implying we observed a distinct print on every venue.
            engine.fill(child.id, got, vwap, venue=venue,
                        fee=got * vwap * (routed.get("fees_bps", 0) / 1e4))
        children.append(_order_dict(engine, child.id))

    # Parent state is DERIVED from the children -- filling it directly would
    # book the same quantity into the position twice.
    engine.roll_up_parent(parent.id)

    # Training capture. The parent order id doubles as the route id so a row
    # in route_observations can always be traced back to the blotter entry it
    # came from.
    observations = sor.capture.observe_route(
        route_id=parent.id, side=side.value, symbol=symbol, venues=venues,
        allocation=allocation,
        fills={k: v.filled for k, v in realised.items()},
        order_qty=req.qty, volatility=req.volatility,
        data_source=data_source, outcome_source="fade-sim",
        predicted=fp, model_version=model_version)
    engine.store.record_route_observations([o.to_row() for o in observations])

    filled_total = sum(v.filled for v in realised.values())
    requested_total = sum(allocation.values())
    return {"routing": routed, "parent": _order_dict(engine, parent.id),
            "children": children, "risk": decision.to_dict(),
            "execution": {
                "requested_qty": round(requested_total, 4),
                "filled_qty": round(filled_total, 4),
                "fill_rate": round(filled_total / requested_total, 4)
                             if requested_total else None,
                "by_venue": {k: {"requested": round(v.requested, 4),
                                 "filled": round(v.filled, 4),
                                 "reachable_frac": round(v.reachable_frac, 4)}
                             for k, v in realised.items()},
                "outcome_source": "fade-sim",
                "caveat": "fills are simulated by core/sor/fade.py, not observed "
                          "at a venue; labels derived from them are synthetic"},
            "observations_recorded": len(observations),
            "note": "child fills book at the routed VWAP; per-venue prints are not "
                    "published by the router"}


def _order_dict(engine, order_id: str) -> dict:
    from .oms import _order_out
    return _order_out(engine.store.get_order(order_id))


class PolicyReq(BaseModel):
    instruction: str


@router.post("/policy")
def policy(req: PolicyReq) -> dict:
    return sor.explain.policy_from_text(req.instruction)


# ---- model registry & training data ------------------------------------

@router.get("/models")
def models(limit: int = 50) -> dict:
    """Registry contents — version, provenance, and out-of-sample metrics.

    Bootstraps on first call so the endpoint never reports an empty registry
    while the router is serving a model; those two states disagreeing is
    exactly the confusion the registry exists to remove.
    """
    m, _ = _model()
    return {"champion_version": m.version,
            "models": sor.registry.history(_store(), limit)}


@router.get("/observations")
def observations(limit: int = 200) -> dict:
    """Captured training rows plus the counts that gate a retrain."""
    store = _store()
    stats = store.route_observation_stats()
    return {**stats,
            "min_training_rows": sor.MIN_TRAINING_ROWS,
            "ready_to_train": stats["total"] >= sor.MIN_TRAINING_ROWS,
            "rows": store.list_route_observations(limit)}


class TrainReq(BaseModel):
    promote: bool = False
    limit: int = 20000


@router.post("/train")
def train(req: TrainReq) -> dict:
    """Fit a challenger on captured rows and register it.

    Registered as `challenger` by default. Promotion is explicit here because
    the automatic champion/challenger gate is not built yet -- an endpoint
    that silently promoted whatever it just fitted would be strictly worse
    than the manual step, since nothing would be comparing the two.
    """
    store = _store()
    rows = store.list_route_observations(req.limit)
    try:
        model = sor.train_from_observations(rows)
    except ValueError as e:
        raise HTTPException(400, str(e))

    metrics = sor.training_metrics(model)
    n_test = metrics.get("n_test", 0)

    # Per-venue row counts are provenance, not a nice-to-have. The router
    # decides which venues get allocated, so it also decides which venues
    # produce labels -- a venue it never picks has ZERO training coverage and
    # the model extrapolates onto it blind. Recording coverage is what makes
    # that diagnosable after the fact instead of showing up as unexplained
    # fills that never arrive.
    coverage: dict[str, int] = {}
    for r in rows:
        coverage[r["venue"]] = coverage.get(r["venue"], 0) + 1
    metrics["venue_coverage"] = coverage

    version = sor.registry.save(
        store, model, metrics, n_train=len(rows) - n_test, n_test=n_test,
        status="champion" if req.promote else "challenger",
        notes=f"fitted on {len(rows)} captured rows; venue coverage " +
              ", ".join(f"{k}={v}" for k, v in sorted(coverage.items())))
    if req.promote:
        store.promote_model(version)
        _invalidate_model_cache()
    incumbent = sor.registry.load(store)
    return {"version": version,
            "status": "champion" if req.promote else "challenger",
            "promoted": req.promote,
            "n_rows": len(rows),
            "metrics": metrics,
            "champion_version": incumbent[0].version if incumbent else None}


@router.post("/models/{version}/promote")
def promote(version: int) -> dict:
    store = _store()
    if not store.get_model(version):
        raise HTTPException(404, f"no model version {version}")
    store.promote_model(version)
    _invalidate_model_cache()
    m, metrics = _model()
    return {"champion_version": m.version, "sample_source": m.sample_source,
            "metrics": metrics}
