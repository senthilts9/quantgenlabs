"""Market-connectivity API — the C++ order path, driven over HTTP.

Two things this router deliberately does NOT do:

  * It does not invent numbers. The /performance endpoint serves the JSON that
    `./benchmarks/run.sh` wrote, verbatim, including the environment block and the
    per-figure spread. If the file is absent it says so and returns 404 rather
    than producing a plausible-looking default. A benchmark number with no
    recorded hardware, compiler and methodology is not a measurement.

  * It does not pretend the venue is real. Every response that touches order flow
    carries `"simulated": true`. Spec rule 6 and rule 4: simulated/testnet data
    must be clearly labelled, and there is no path from here to a live exchange.

Every response reports `impl` ("cpp" or "python"), so a reader of a latency or
throughput figure always knows which implementation produced it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..core import connectivity as conn
from ..core.auth import Permission, require
from ..obs import trading_metrics as tm

router = APIRouter(prefix="/api/connectivity", tags=["connectivity"])

# benchmarks/ sits at the repo root: app/api/connectivity.py -> app -> backend -> root
_BENCH_DIR = Path(__file__).resolve().parents[3] / "benchmarks"


@router.get("/status")
def status() -> dict:
    return {
        "impl": conn.impl_name(),
        "cpp_extension_built": conn.HAVE_CPP,
        "components": {
            "order_book": "cpp" if conn.HAVE_CPP else "python",
            "feed_validator": "cpp" if conn.HAVE_CPP else "python",
            # These have no fallback on purpose -- see core/connectivity/__init__.
            "matching_engine": "cpp" if conn.HAVE_CPP else "unavailable",
            "pretrade_gate": "cpp" if conn.HAVE_CPP else "unavailable",
            "fix_parser": "cpp" if conn.HAVE_CPP else "unavailable",
        },
        "note": (
            "Matching engine, pre-trade gate and FIX parsing have no Python "
            "fallback by design: a second, unverified implementation of a risk "
            "control or a matching engine is worse than a clear error. "
            "Build with ./scripts/build_bindings.sh"
        ),
        "simulated": True,
    }


# ----------------------------------------------------------------- performance
@router.get("/performance")
def performance() -> dict:
    """Serve the measured benchmark results. Never synthesised."""
    out: dict = {"available": False, "runs": {}}
    for label, fname in (("baseline", "results-baseline.json"), ("avx2", "results-avx2.json")):
        p = _BENCH_DIR / fname
        if p.exists():
            try:
                out["runs"][label] = json.loads(p.read_text())
                out["available"] = True
            except json.JSONDecodeError as e:
                out["runs"][label] = {"error": f"unreadable: {e}"}

    if not out["available"]:
        raise HTTPException(
            status_code=404,
            detail=("No benchmark results on disk. Run ./benchmarks/run.sh on an "
                    "idle machine. This endpoint will not synthesise numbers."),
        )

    base = out["runs"].get("baseline") or next(iter(out["runs"].values()))
    mc = base.get("monte_carlo", {})
    out["summary"] = {
        "cpu": base.get("environment", {}).get("cpu"),
        "physical_cores": base.get("environment", {}).get("physical_cores"),
        "logical_cores": base.get("environment", {}).get("logical_cores"),
        "eight_physical_core_measured": mc.get("eight_physical_core_measured", False),
        "eight_physical_core_note": mc.get("eight_physical_core_note"),
        "timesteps_per_path": mc.get("timesteps_per_path"),
        "deterministic_across_threads": mc.get("bitwise_identical_across_thread_counts"),
    }
    # If both builds are present, the AVX2 gain is a measured ratio, not a claim.
    b, a = out["runs"].get("baseline"), out["runs"].get("avx2")
    if b and a and "monte_carlo" in b and "monte_carlo" in a:
        out["avx2_gain"] = [
            {"threads": rb["threads"],
             "baseline_paths_per_sec": rb["paths_per_sec"],
             "avx2_paths_per_sec": ra["paths_per_sec"],
             "gain": round(ra["paths_per_sec"] / rb["paths_per_sec"], 3)}
            for rb, ra in zip(b["monte_carlo"]["scaling"], a["monte_carlo"]["scaling"])
        ]
        out["price_identical_across_builds"] = (
            b["monte_carlo"]["price"] == a["monte_carlo"]["price"]
        )
    return out


# ----------------------------------------------------------------- order book
class BookUpdate(BaseModel):
    side: int = Field(..., ge=0, le=1, description="0 = bid, 1 = ask")
    price_ticks: int
    qty: int = Field(..., ge=0)


class BookRequest(BaseModel):
    floor_ticks: int = 1
    n_ticks: int = Field(50_000, ge=2, le=1_000_000)
    updates: list[BookUpdate] = Field(default_factory=list, max_length=50_000)
    depth_levels: int = Field(10, ge=1, le=100)


@router.post("/book")
def build_book(req: BookRequest) -> dict:
    t0 = time.perf_counter()
    book = conn.Ladder(req.floor_ticks, req.n_ticks)
    rejected = 0
    for u in req.updates:
        if not book.upsert(u.side, u.price_ticks, u.qty):
            rejected += 1
    elapsed = time.perf_counter() - t0

    best_bid, best_ask = book.best(0), book.best(1)
    return {
        "impl": conn.impl_name(),
        "applied": len(req.updates) - rejected,
        "rejected": rejected,
        "elapsed_us": round(elapsed * 1e6, 1),
        # Per-update Python loop: the boundary is crossed once per update here, which
        # is exactly what the batch entry points exist to avoid. The in-process C++
        # figure is ~25M updates/sec; this will be orders of magnitude lower and that
        # difference IS the cost of the boundary, not of the book.
        "updates_per_sec": round(len(req.updates) / elapsed) if elapsed > 0 else None,
        "throughput_note": (
            "Measured across the Python/C++ boundary, one crossing per update. "
            "The in-process book figure is in benchmarks/results-baseline.json."
        ),
        "best_bid": {"price_ticks": best_bid[0], "qty": best_bid[1]} if best_bid else None,
        "best_ask": {"price_ticks": best_ask[0], "qty": best_ask[1]} if best_ask else None,
        "spread_ticks": book.spread_ticks(),
        "mid_ticks": book.mid_ticks(),
        "micro_price_ticks": book.micro_price_ticks(),
        "imbalance": book.imbalance(),
        "is_crossed": book.is_crossed(),
        "bids": [{"price_ticks": l.price, "qty": l.qty} for l in book.depth(0, req.depth_levels)],
        "asks": [{"price_ticks": l.price, "qty": l.qty} for l in book.depth(1, req.depth_levels)],
        "simulated": True,
    }


# ----------------------------------------------------------------- feed quality
class FeedTick(BaseModel):
    seq: int
    ts_ns: int
    price: float


class FeedRequest(BaseModel):
    ticks: list[FeedTick] = Field(..., min_length=1, max_length=200_000)
    price_min: float = 0.0
    price_max: float = 1e18
    max_z: float = 10.0
    min_samples: int = 30


@router.post("/feed/validate")
def validate_feed(req: FeedRequest) -> dict:
    v = conn.FeedValidator(price_min=req.price_min, price_max=req.price_max,
                           max_z=req.max_z, min_samples=req.min_samples)
    t0 = time.perf_counter()
    verdicts = v.check_batch([(t.seq, t.ts_ns, t.price) for t in req.ticks])
    elapsed = time.perf_counter() - t0
    return {
        "impl": conn.impl_name(),
        "ticks": len(req.ticks),
        "elapsed_us": round(elapsed * 1e6, 1),
        # One boundary crossing for the WHOLE batch (check_batch loops in C++), so
        # this figure is much closer to the in-process one than the /book endpoint's.
        "ticks_per_sec": round(len(req.ticks) / elapsed) if elapsed > 0 else None,
        "verdicts": dict(verdicts),
        "counters": dict(v.counters()),
        "reject_rate": v.reject_rate(),
        "needs_recovery": v.needs_recovery(),
        "last_price": v.last_price(),
    }


# ----------------------------------------------------------------- pre-trade risk
class LimitsModel(BaseModel):
    max_order_qty: float = 10_000
    max_order_notional: float = 1_000_000
    collar_bps: float = 500
    max_long_position: float = 50_000
    max_short_position: float = 20_000
    contract_size: float = 1.0


class PreTradeRequest(BaseModel):
    side: int = Field(..., ge=0, le=1, description="0 = buy, 1 = sell")
    qty: float
    price: float
    reference_price: float
    reference_ok: bool = True
    current_position: float = 0.0
    limits: LimitsModel = Field(default_factory=LimitsModel)
    kill_switch: bool = False


@router.post("/pretrade",
             dependencies=[Depends(require(Permission.READ_RISK))])
def pretrade(req: PreTradeRequest) -> dict:
    if not conn.HAVE_CPP:
        raise HTTPException(
            status_code=503,
            detail=("The pre-trade gate has no Python fallback by design. A risk "
                    "control that silently degrades to an unverified second "
                    "implementation is worse than one that refuses to start. "
                    "Build with ./scripts/build_bindings.sh"),
        )
    import mkt_connectivity as mc

    gate = conn.pretrade_gate(1)
    lim = mc.SymbolLimits()
    lim.known = True
    lim.max_order_qty = req.limits.max_order_qty
    lim.max_order_notional = req.limits.max_order_notional
    lim.collar_bps = req.limits.collar_bps
    lim.max_long_position = req.limits.max_long_position
    lim.max_short_position = req.limits.max_short_position
    lim.contract_size = req.limits.contract_size
    gate.set_limits(0, lim)
    gate.set_position(0, req.current_position)
    if req.kill_switch:
        gate.engage_kill_switch()

    t0 = time.perf_counter()
    decision = gate.check(0, req.side, req.qty, req.price, req.reference_price, req.reference_ok)
    elapsed = time.perf_counter() - t0
    tm.observe_pretrade(mc.decision_name(decision))
    tm.KILL_SWITCH.set(1 if gate.kill_switch_engaged() else 0)

    return {
        "impl": conn.impl_name(),
        "decision": mc.decision_name(decision),
        "accepted": mc.decision_name(decision) == "accept",
        "elapsed_us": round(elapsed * 1e6, 3),
        # This timing is the PYTHON-SIDE call, not the gate. It is dominated by the
        # interpreter/C++ boundary crossing, and this endpoint additionally builds a
        # fresh gate and limit table per request. The gate itself measures ~11ns
        # (89M checks/sec) in cpp/src/mkt_bench.cpp. Quoting the number below as the
        # risk check's cost would overstate it by roughly three orders of magnitude.
        "elapsed_note": (
            "Python-side call including the FFI boundary and per-request gate "
            "construction -- NOT the cost of the check itself. See "
            "benchmarks/results-baseline.json -> connectivity.pretrade_checks "
            "for the measured in-process figure."
        ),
        "resulting_position": req.current_position + (req.qty if req.side == 0 else -req.qty),
        "notional": req.qty * req.price * req.limits.contract_size,
        "deviation_bps": (
            abs(req.price - req.reference_price) / req.reference_price * 1e4
            if req.reference_ok and req.reference_price > 0 else None
        ),
        "simulated": True,
    }


# ----------------------------------------------------------------- FIX
@router.get("/fix/parse")
def parse_fix(raw: str = Query(..., description="FIX message; '|' is accepted for SOH"),
              validate: bool = True) -> dict:
    if not conn.HAVE_CPP:
        raise HTTPException(status_code=503, detail="FIX parsing requires ./scripts/build_bindings.sh")
    wire = raw.replace("|", "\x01")
    result = conn.parse_fix(wire, validate)
    return {
        "impl": conn.impl_name(),
        "validated": validate,
        "pretty": conn.pretty_fix(wire),
        **result,
        "note": ("validate=true checks BodyLength and CheckSum. validate=false is "
                 "only legitimate when replaying a log whose framing is trusted."),
    }
