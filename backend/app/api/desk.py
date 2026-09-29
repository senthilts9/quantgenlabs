"""Trading desk API — an order ticket wired to the C++ simulated venue.

Each browser session gets its own `SimulatedDesk`: a client FIX session paired
with a matching-engine venue, kept in memory. Orders go out as real FIX,
ExecutionReports come back, and the blotter is rebuilt from those reports rather
than from what the desk hoped would happen.

STATE IS PER-SESSION AND IN-MEMORY, and that is a deliberate limit rather than an
oversight. A desk holds a live FIX session with sequence numbers that only mean
something within one connection, so it cannot be recreated per request or shared
across workers. In-memory means it is lost on restart and will not survive more
than one uvicorn worker -- which is correct for a simulation and is exactly what
the PostgreSQL work would change for anything real.

The C++ objects are not thread-safe (one writer owns a book, by design), so every
mutation is serialised under a per-desk lock.

Nothing here reaches a real exchange and no money moves. Every response says so.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import connectivity as conn
from ..core.auth import Permission, require
from ..core.events import publishers
from ..core.pnl import PnLProjection
from ..obs import tracing

router = APIRouter(prefix="/api/desk", tags=["trading desk"])

# Sessions are capped so a long-running public demo cannot be made to allocate
# unbounded matching engines by anyone who can hit the endpoint.
MAX_DESKS = 32
NS = 1_000_000_000


@dataclass
class DeskSession:
    desk: object
    # RLock, not Lock. The publish helper needs desk state and is called from
    # inside the critical section that produced it; with a non-reentrant Lock
    # that self-deadlocks the request thread and the endpoint hangs forever
    # rather than failing. Re-entrancy is the honest fix here because the lock
    # guards one thread's access to non-thread-safe C++ objects, not a resource
    # being contended between the nested calls.
    lock: threading.RLock = field(default_factory=threading.RLock)
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    seq: int = 0                      # ClOrdID counter for this desk
    # Fed from the same events this desk publishes, so the projection is a
    # genuine consumer rather than a second copy of the desk's own state.
    pnl: PnLProjection = field(default_factory=PnLProjection)
    published_fills: set = field(default_factory=set)


_desks: dict[str, DeskSession] = {}
_registry_lock = threading.Lock()

# A fixed epoch so timestamps in the wire log are stable and comparable within a
# session; the desk's clock is injected, never read from the wall.
_EPOCH_NS = 1_759_000_000 * NS


def _now_ns(s: DeskSession) -> int:
    return _EPOCH_NS + int((time.time() - s.created) * NS)


def _require_cpp() -> None:
    if not conn.HAVE_CPP:
        raise HTTPException(
            status_code=503,
            detail=("The simulated venue is a C++ component with no Python fallback: "
                    "a second implementation of a matching engine means a second "
                    "answer to 'what filled'. Build with ./scripts/build_bindings.sh"),
        )


def _get(desk_id: str) -> DeskSession:
    with _registry_lock:
        s = _desks.get(desk_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"No desk session '{desk_id}'. POST /api/desk/session first.")
    s.last_used = time.time()
    return s


def _book_snapshot(desk, levels: int = 8) -> dict:
    import mkt_connectivity as mc
    b = desk.book()
    ts = desk.tick_size()
    bid, ask = b.best(mc.Side.BID), b.best(mc.Side.ASK)
    return {
        "tick_size": ts,
        "best_bid": {"price": bid[0] * ts, "ticks": bid[0], "qty": bid[1]} if bid else None,
        "best_ask": {"price": ask[0] * ts, "ticks": ask[0], "qty": ask[1]} if ask else None,
        "spread": b.spread_ticks() * ts if bid and ask else None,
        "mid": b.mid_ticks() * ts if bid and ask else None,
        "micro_price": b.micro_price_ticks() * ts if bid and ask else None,
        "imbalance": b.imbalance(),
        "is_crossed": b.is_crossed(),
        "bids": [{"price": l.price * ts, "qty": l.qty} for l in b.depth(mc.Side.BID, levels)],
        "asks": [{"price": l.price * ts, "qty": l.qty} for l in b.depth(mc.Side.ASK, levels)],
    }


def _publish_order_events(s: DeskSession, desk_id: str, cl_ord_id: str,
                          symbol: str) -> None:
    """Emit the facts this order produced, and feed the P&L projection.

    Wrapped so a bus failure cannot reach the order path: the trade already
    happened, and failing it because the event log was unreachable would be
    strictly worse than losing the event.
    """
    try:
        with s.lock:
            row = next((r for r in s.desk.blotter() if r["cl_ord_id"] == cl_ord_id), None)
            if row is None:
                return
            fills = list(row["fills"])
            sym = row["symbol"] or symbol
            side = row["side"]
            status, cum, leaves, avg = (row["status"], row["cum_qty"],
                                        row["leaves_qty"], row["avg_px"])
            reject = row.get("reject_text", "")

        publishers.order_state_changed(
            cl_ord_id=cl_ord_id, symbol=sym, status=status, cum_qty=cum,
            leaves_qty=leaves, avg_px=avg, reject_text=reject, desk_id=desk_id)

        for f in fills:
            exec_id = f.get("exec_id") or f"{cl_ord_id}-{f['qty']}-{f['price']}"
            # Only publish a fill once even though this runs after every action
            # on the order: the blotter carries the full fill history each time.
            if exec_id in s.published_fills:
                continue
            s.published_fills.add(exec_id)
            fix_side = "buy" if side == "1" else "sell"
            publishers.trade_executed(
                cl_ord_id=cl_ord_id, exec_id=exec_id, symbol=sym,
                side=fix_side, qty=f["qty"], price=f["price"], desk_id=desk_id)
            s.pnl.apply_fill(symbol=sym, side=fix_side, qty=f["qty"],
                             price=f["price"], exec_id=exec_id)

        if status == "rejected" and reject:
            publishers.risk_breached(symbol=sym, decision=reject, side=side,
                                     qty=cum + leaves, price=avg)

        # Mark from the venue's top of book so unrealised has something fresh.
        #
        # The mark is PUBLISHED before it is applied, not just applied. Feeding
        # the projection something that is not in the log breaks the property the
        # projection is built on -- that it can be rebuilt from the log and get
        # the same answer. The rebuild check caught exactly this: marks applied
        # directly made a rebuild disagree by the whole unrealised P&L.
        with s.lock:
            import mkt_connectivity as mc
            b = s.desk.book()
            ts = s.desk.tick_size()
            bid, ask = b.best(mc.Side.BID), b.best(mc.Side.ASK)
        if bid and ask:
            mid = ((bid[0] + ask[0]) / 2.0) * ts
            publishers.market_tick(symbol=sym, seq=0, price=mid, size=0.0,
                                   verdict="accepted")
            s.pnl.apply_mark(sym, mid)
    except Exception:            # noqa: BLE001 - must never break the order path
        pass


def _state(desk_id: str, s: DeskSession, wire_tail: int = 40) -> dict:
    d = s.desk
    return {
        "desk_id": desk_id,
        "impl": conn.impl_name(),
        "connected": d.connected(),
        "book": _book_snapshot(d),
        "blotter": list(d.blotter()),
        "position": dict(d.position()),
        "venue_stats": dict(d.engine_stats()),
        "pnl": s.pnl.totals(),
        "wire_log": list(d.wire_log(wire_tail)),
        "simulated": True,
        "note": "Simulated venue. No real exchange, no money.",
    }


# ------------------------------------------------------------------ session
class SessionRequest(BaseModel):
    # Seeded so the book is not empty on arrival: a desk staring at an empty
    # ladder cannot demonstrate anything.
    seed_liquidity: bool = True
    mid: float = Field(100.0, gt=0, le=100_000)
    levels: int = Field(6, ge=1, le=25)
    level_qty: int = Field(250, ge=1, le=1_000_000)
    tick_size: float = Field(0.01, gt=0, le=1000)


@router.post("/session",
             dependencies=[Depends(require(Permission.SUBMIT_ORDER))])
def create_session(req: SessionRequest) -> dict:
    _require_cpp()
    import mkt_connectivity as mc

    with _registry_lock:
        if len(_desks) >= MAX_DESKS:
            # Evict the least recently used rather than refusing: a demo that
            # stops working after N visitors is worse than one that forgets the
            # oldest idle session.
            oldest = min(_desks.items(), key=lambda kv: kv[1].last_used)[0]
            _desks.pop(oldest, None)

    desk_id = uuid.uuid4().hex[:12]
    # The ladder must span the prices the desk will actually quote. Sized from
    # the requested mid rather than fixed, so a 3000-dollar instrument is not
    # silently rejected as out-of-range.
    n_ticks = int(req.mid * 3 / req.tick_size)
    desk = mc.SimulatedDesk(1, max(n_ticks, 1000), req.tick_size, "QGLABS", "SIMVENUE")
    s = DeskSession(desk=desk)
    desk.start(_now_ns(s))

    if req.seed_liquidity:
        for i in range(req.levels):
            off = (i + 1) * req.tick_size
            qty = req.level_qty + i * 40
            desk.seed_liquidity(mc.Side.BID, round(req.mid - off, 10), qty)
            desk.seed_liquidity(mc.Side.ASK, round(req.mid + off, 10), qty)

    with _registry_lock:
        _desks[desk_id] = s
    return _state(desk_id, s)


@router.get("/{desk_id}/state")
def get_state(desk_id: str, wire_tail: int = 40) -> dict:
    s = _get(desk_id)
    with s.lock:
        return _state(desk_id, s, wire_tail)


@router.delete("/{desk_id}")
def drop_session(desk_id: str) -> dict:
    with _registry_lock:
        existed = _desks.pop(desk_id, None) is not None
    return {"dropped": existed}


# -------------------------------------------------------------------- orders
class OrderRequest(BaseModel):
    side: str = Field(..., pattern="^[12]$", description="FIX 54: 1 buy, 2 sell")
    qty: float = Field(..., gt=0, le=1e7)
    price: float = Field(0.0, ge=0, le=1e7)
    ord_type: str = Field("2", pattern="^[12]$", description="FIX 40: 1 market, 2 limit")
    tif: str = Field("0", pattern="^[034]$", description="FIX 59: 0 day, 3 IOC, 4 FOK")
    symbol: str = Field("AAPL", min_length=1, max_length=20)
    cl_ord_id: str | None = None


@router.post("/{desk_id}/order",
             dependencies=[Depends(require(Permission.SUBMIT_ORDER))])
def submit_order(desk_id: str, req: OrderRequest) -> dict:
    _require_cpp()
    s = _get(desk_id)
    if req.ord_type == "2" and req.price <= 0:
        raise HTTPException(status_code=422, detail="A limit order needs a price above zero.")

    with s.lock:
        s.seq += 1
        cl = req.cl_ord_id or f"QGL-{s.seq:04d}"
        before = len(s.desk.wire_log(0))
        # ClOrdID is the correlation key: it is already in the blotter, the FIX
        # wire log and the audit trail, so a trace found by it is a trace someone
        # can actually find.
        tracing.set_correlation(cl_ord_id=cl, symbol=req.symbol)
        with tracing.span("desk.submit_order", **{
            "qgl.cl_ord_id": cl, "qgl.symbol": req.symbol, "qgl.side": req.side,
            "qgl.qty": float(req.qty), "qgl.ord_type": req.ord_type, "qgl.tif": req.tif,
        }):
            s.desk.submit(cl, req.symbol, req.side, req.qty, req.price,
                          req.ord_type, req.tif, _now_ns(s))

        # Events are published AFTER the state change is committed. Publishing
        # "filled" before the fill is recorded is a lie the rest of the system
        # would act on, and the window is long enough to matter.
        _publish_order_events(s, desk_id, cl, req.symbol)
        st = _state(desk_id, s)
        # The messages THIS order produced, so the UI can show the exchange that
        # just happened rather than the whole session history.
        st["this_order"] = {
            "cl_ord_id": cl,
            "row": next((r for r in st["blotter"] if r["cl_ord_id"] == cl), None),
            "wire": list(s.desk.wire_log(0))[before:],
        }
        return st


class CancelRequest(BaseModel):
    orig_cl_ord_id: str = Field(..., min_length=1, max_length=64)


@router.post("/{desk_id}/cancel",
             dependencies=[Depends(require(Permission.CANCEL_ORDER))])
def cancel_order(desk_id: str, req: CancelRequest) -> dict:
    _require_cpp()
    s = _get(desk_id)
    with s.lock:
        s.seq += 1
        before = len(s.desk.wire_log(0))
        s.desk.cancel(f"{req.orig_cl_ord_id}-C{s.seq}", req.orig_cl_ord_id, _now_ns(s))
        _publish_order_events(s, desk_id, req.orig_cl_ord_id, "")
        st = _state(desk_id, s)
        st["this_order"] = {
            "cl_ord_id": req.orig_cl_ord_id,
            "row": next((r for r in st["blotter"] if r["cl_ord_id"] == req.orig_cl_ord_id), None),
            "wire": list(s.desk.wire_log(0))[before:],
        }
        return st


class SeedRequest(BaseModel):
    side: str = Field(..., pattern="^[12]$")
    price: float = Field(..., gt=0, le=1e7)
    qty: int = Field(..., gt=0, le=10_000_000)


@router.post("/{desk_id}/seed")
def seed(desk_id: str, req: SeedRequest) -> dict:
    """Rest liquidity as another participant, so the desk trades against a
    market instead of against itself."""
    _require_cpp()
    import mkt_connectivity as mc
    s = _get(desk_id)
    with s.lock:
        s.desk.seed_liquidity(mc.Side.BID if req.side == "1" else mc.Side.ASK, req.price, req.qty)
        return _state(desk_id, s)


@router.post("/{desk_id}/tick")
def tick(desk_id: str) -> dict:
    """Advance session timers: emits heartbeats and test requests when quiet."""
    s = _get(desk_id)
    with s.lock:
        alive = s.desk.tick(_now_ns(s))
        st = _state(desk_id, s)
        st["session_alive"] = alive
        return st
