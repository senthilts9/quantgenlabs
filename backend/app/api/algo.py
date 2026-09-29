"""Algorithmic execution over HTTP: TWAP, VWAP, POV against the simulated venue.

Each desk session owns a runner. Slices are driven by an explicit `/step` call
rather than a background thread, and that is deliberate for a simulation: a timer
thread would make the behaviour depend on wall-clock scheduling that a test
cannot control, and "it filled differently that time" is the least debuggable
class of bug. A production deployment replaces `/step` with a scheduler; the
runner does not change, because its clock is already injected.
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core.auth import Permission, require
from ..core.execution import Algo, AlgoRunner, ScheduleError, build
from ..obs import trading_metrics as tm

router = APIRouter(prefix="/api/algo", tags=["execution algos"])

_runners: dict[str, AlgoRunner] = {}
_lock = threading.Lock()


def _session(desk_id: str):
    from .desk import _desks
    s = _desks.get(desk_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"No desk session '{desk_id}'")
    return s


def _runner(desk_id: str) -> AlgoRunner:
    """One runner per desk, wired to that desk's venue."""
    with _lock:
        if desk_id in _runners:
            return _runners[desk_id]

    s = _session(desk_id)

    def send(symbol: str, side: str, qty: float, limit_px: float):
        """Send one child order through the same path a manual order takes.

        Going through the desk rather than straight to the matching engine means
        children hit pre-trade risk, publish events and land in the blotter --
        an algo that bypasses the controls a human order passes is an algo that
        can breach a limit nobody is watching.
        """
        from .desk import _now_ns, _publish_order_events
        with s.lock:
            s.seq += 1
            cl = f"ALGO-{s.seq:04d}"
            s.desk.submit(cl, symbol, side, qty, limit_px, "2", "3",  # IOC child
                          _now_ns(s))
            row = next((r for r in s.desk.blotter() if r["cl_ord_id"] == cl), None)
        # Children are orders. Counting them only at the HTTP endpoint made every
        # algo-generated order invisible to the order-flow panels -- the desk
        # looked idle while an algo was working it. Found by deploying and
        # reading /metrics, not by a test.
        tm.ORDERS_SUBMITTED.labels(
            symbol=symbol, side="buy" if side == "1" else "sell",
            ord_type="limit").inc()
        _publish_order_events(s, desk_id, cl, symbol)
        if row is None:
            return (cl, 0.0, 0.0, "unknown")
        return (cl, float(row["cum_qty"]), float(row["avg_px"]), row["status"])

    r = AlgoRunner(send)
    with _lock:
        _runners[desk_id] = r
    return r


def _prices(desk_id: str) -> tuple[float | None, float | None, float | None]:
    """(mid, far_touch_buy, far_touch_sell) from the venue book.

    The MID is the arrival benchmark: implementation shortfall must be measured
    against the price that existed when the decision to trade was made.

    The FAR TOUCH is where an aggressive child is priced. Anchoring an IOC child
    at the mid means a buy limit that never reaches the ask, so every child
    cancels unfilled -- which is exactly what the first version of this did.
    """
    import mkt_connectivity as mc
    s = _session(desk_id)
    with s.lock:
        b = s.desk.book()
        ts = s.desk.tick_size()
        bid, ask = b.best(mc.Side.BID), b.best(mc.Side.ASK)
    if not (bid and ask):
        return (None, None, None)
    return (((bid[0] + ask[0]) / 2.0) * ts, ask[0] * ts, bid[0] * ts)


def _reference_px(desk_id: str) -> float | None:
    return _prices(desk_id)[0]


def _limit_anchor(desk_id: str, side: str) -> float | None:
    mid, far_buy, far_sell = _prices(desk_id)
    return far_buy if side == "1" else far_sell


# ------------------------------------------------------------------ preview
class PreviewRequest(BaseModel):
    algo: str = Field("twap", pattern="^(twap|vwap|pov)$")
    total_qty: float = Field(..., gt=0, le=1e9)
    slices: int = Field(10, ge=1, le=500)
    duration_s: float = Field(300.0, ge=0, le=86400)
    min_slice_qty: float = Field(1.0, gt=0)
    round_to: float = Field(1.0, gt=0)


@router.post("/preview")
def preview(req: PreviewRequest) -> dict:
    """Build a schedule without sending anything.

    Worth having on its own: a trader should be able to see what an algo intends
    to do before committing to it, and the slice sizes are the whole decision.
    """
    try:
        s = build(Algo(req.algo), req.total_qty, req.slices, req.duration_s,
                  min_slice_qty=req.min_slice_qty, round_to=req.round_to)
    except ScheduleError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {
        "algo": s.algo.value,
        "slices": [{"index": x.index, "offset_s": x.offset_s,
                    "qty": x.qty, "weight": x.weight} for x in s.slices],
        "requested_slices": req.slices,
        "scheduled_slices": len(s),
        "total_qty": s.total_qty,
        "scheduled_qty": s.scheduled_qty,
        "exact": abs(s.scheduled_qty - s.total_qty) < 1e-9,
        "clamped": len(s) < req.slices,
        "note": ("Slice count was clamped so no child falls below the venue "
                 "minimum." if len(s) < req.slices else
                 "Schedule sums exactly to the parent."),
    }


# -------------------------------------------------------------------- start
class StartRequest(BaseModel):
    algo: str = Field("twap", pattern="^(twap|vwap|pov)$")
    side: str = Field(..., pattern="^[12]$")
    total_qty: float = Field(..., gt=0, le=1e7)
    slices: int = Field(10, ge=1, le=200)
    duration_s: float = Field(60.0, ge=0, le=86400)
    symbol: str = Field("AAPL", min_length=1, max_length=20)
    pov_rate: float = Field(0.0, ge=0, le=1)
    limit_offset_bps: float = Field(0.0, ge=-500, le=500)
    min_slice_qty: float = Field(1.0, gt=0)


@router.post("/{desk_id}/start",
             dependencies=[Depends(require(Permission.SUBMIT_ORDER))])
def start(desk_id: str, req: StartRequest) -> dict:
    if req.algo == "pov" and req.pov_rate <= 0:
        raise HTTPException(
            status_code=422,
            detail="POV needs a participation rate above zero; without one it "
                   "has no basis on which to size a slice.")
    r = _runner(desk_id)
    arrival = _reference_px(desk_id)
    try:
        p = r.create(symbol=req.symbol, side=req.side, total_qty=req.total_qty,
                     algo=Algo(req.algo), slices=req.slices,
                     duration_s=req.duration_s, arrival_mid=arrival,
                     pov_rate=req.pov_rate, min_slice_qty=req.min_slice_qty,
                     limit_offset_bps=req.limit_offset_bps)
    except ScheduleError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    tm.ALGO_PARENTS.labels(algo=req.algo, symbol=req.symbol).inc()
    tm.ALGO_ACTIVE.labels(algo=req.algo).set(
        len([x for x in r.active() if x.algo.value == req.algo]))
    return {**p.to_dict(),
            "arrival_note": ("Benchmark set from the current mid; everything "
                             "after this is a cost of the decision to trade."
                             if arrival else
                             "No two-sided book, so there is no arrival "
                             "benchmark and shortfall cannot be measured.")}


@router.post("/{desk_id}/{parent_id}/step",
             dependencies=[Depends(require(Permission.SUBMIT_ORDER))])
def step(desk_id: str, parent_id: str, observed_volume: float = 0.0) -> dict:
    """Fire every slice now due.

    Every due slice, not one: a runner that sends at most one per call can never
    catch up after a stall and drifts further behind with each tick.
    """
    r = _runner(desk_id)
    try:
        p0 = r.get(parent_id)
        result = r.step(parent_id, observed_volume=observed_volume,
                        reference_px=_reference_px(desk_id),
                        limit_anchor=_limit_anchor(desk_id, p0.side))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    p = r.get(parent_id)
    if result["fired"]:
        tm.ALGO_SLICES.labels(algo=p.algo.value, symbol=p.symbol).inc(result["fired"])
    sf = p.shortfall_bps()
    if sf is not None and p.state.value in ("completed", "partial", "cancelled"):
        tm.ALGO_SHORTFALL_BPS.labels(algo=p.algo.value).observe(sf)
    tm.ALGO_ACTIVE.labels(algo=p.algo.value).set(
        len([x for x in r.active() if x.algo is p.algo]))
    return {**result, "parent": p.to_dict()}


@router.post("/{desk_id}/{parent_id}/run",
             dependencies=[Depends(require(Permission.SUBMIT_ORDER))])
def run_to_completion(desk_id: str, parent_id: str,
                      max_steps: int = 200) -> dict:
    """Drive the parent until it reaches a terminal state.

    Bounded by max_steps: an algo that cannot finish should stop and say so
    rather than looping forever inside a request.
    """
    r = _runner(desk_id)
    try:
        r.get(parent_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    side = r.get(parent_id).side
    steps = 0
    for _ in range(max_steps):
        steps += 1
        res = r.step(parent_id, reference_px=_reference_px(desk_id),
                     limit_anchor=_limit_anchor(desk_id, side))
        if res["state"] in ("completed", "partial", "cancelled"):
            break
    p = r.get(parent_id)
    # /step counted slices and /run did not, so driving a parent to completion
    # left the slice panel flat while children were genuinely being sent.
    if p.slices_sent:
        tm.ALGO_SLICES.labels(algo=p.algo.value, symbol=p.symbol).inc(p.slices_sent)
    tm.ALGO_ACTIVE.labels(algo=p.algo.value).set(
        len([x for x in r.active() if x.algo is p.algo]))
    sf = p.shortfall_bps()
    if sf is not None:
        tm.ALGO_SHORTFALL_BPS.labels(algo=p.algo.value).observe(sf)
    return {"steps": steps, "parent": p.to_dict(),
            "terminated_early": steps >= max_steps and p.state.value == "working"}


@router.post("/{desk_id}/{parent_id}/pause",
             dependencies=[Depends(require(Permission.CANCEL_ORDER))])
def pause(desk_id: str, parent_id: str) -> dict:
    try:
        return _runner(desk_id).pause(parent_id).to_dict()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{desk_id}/{parent_id}/resume",
             dependencies=[Depends(require(Permission.SUBMIT_ORDER))])
def resume(desk_id: str, parent_id: str) -> dict:
    try:
        return _runner(desk_id).resume(parent_id).to_dict()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{desk_id}/{parent_id}/cancel",
             dependencies=[Depends(require(Permission.CANCEL_ORDER))])
def cancel(desk_id: str, parent_id: str) -> dict:
    try:
        return _runner(desk_id).cancel(parent_id).to_dict()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/{desk_id}")
def list_parents(desk_id: str) -> dict:
    r = _runner(desk_id)
    parents = [p.to_dict() for p in r.list_parents()]
    filled = [p for p in parents if p["shortfall_bps"] is not None]
    return {
        "desk_id": desk_id,
        "parents": parents,
        "active": len(r.active()),
        "tca": {
            "orders_measured": len(filled),
            "avg_shortfall_bps": (sum(p["shortfall_bps"] for p in filled) / len(filled)
                                  if filled else None),
            "avg_participation": (sum(p["participation"] for p in parents) / len(parents)
                                  if parents else None),
            "note": ("Shortfall is signed so POSITIVE IS ALWAYS BAD: a buyer "
                     "paying above arrival and a seller receiving below it are "
                     "the same failure, so the two aggregate."),
        },
        "simulated": True,
    }


@router.get("/{desk_id}/{parent_id}")
def get_parent(desk_id: str, parent_id: str) -> dict:
    try:
        return _runner(desk_id).get(parent_id).to_dict()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
