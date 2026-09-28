"""Observability API — feeds the in-terminal profiling screen.

/metrics (Prometheus scrape) is mounted separately in main.py. This router
serves the human-facing percentile snapshot so the console can show engine
latency and the C++-vs-Python speedup without leaving the app.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from ..models import StatOut
from ..obs import profiler

router = APIRouter(prefix="/api/obs", tags=["observability"])


@router.get("/profiling", response_model=list[StatOut])
def profiling() -> list[StatOut]:
    # Stat is a slots=True dataclass -- it has no __dict__ to spread (that's
    # the whole point of slots); asdict() introspects the declared fields
    # instead and works regardless of slots.
    return [StatOut(**asdict(s)) for s in profiler.snapshot()]


@router.get("/speedup")
def speedup() -> dict:
    """C++ vs Python p50 speedup per engine, for the terminal badge."""
    snap = {(s.engine, s.impl): s for s in profiler.snapshot()}
    engines = {e for (e, _) in snap}
    out = {}
    for e in engines:
        cpp, py = snap.get((e, "cpp")), snap.get((e, "python"))
        if not py:
            py = snap.get((e, "numpy"))
        if cpp and py and cpp.p50_ms > 0:
            out[e] = round(py.p50_ms / cpp.p50_ms, 2)
    return out
