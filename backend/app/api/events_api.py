"""Event bus and P&L projection over HTTP.

The bus is the platform's architecture, not just infrastructure, so it is
inspectable: what topics exist, how they are keyed, what is flowing, what is
lagging, and what went to a dead-letter queue.

The P&L endpoints expose a PROJECTION — a read model derived entirely from the
event stream and rebuildable from it. Every response says which positions could
not be valued rather than silently excluding them from a total that then looks
complete.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..core.events import TOPICS, backend_name, get_bus, spec
from ..core.events.publishers import publish_stats

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/status")
def status() -> dict:
    bus = get_bus()
    stats = bus.stats() if hasattr(bus, "stats") else {}
    return {
        "backend": backend_name(),
        "publisher": publish_stats(),
        "bus": stats,
        "durability_note": (
            "In-process bus: same semantics as Kafka (partitioning, per-partition "
            "ordering, consumer groups, at-least-once, idempotency, DLQ, replay) "
            "but NOT durable across a restart and NOT visible to another process. "
            "Set QGL_KAFKA_BROKERS to use Kafka."
            if backend_name() == "in-process" else
            "Kafka transport. Note this path has not been exercised against a "
            "broker in development; see core/events/kafka_bus.py."
        ),
    }


@router.get("/topics")
def topics() -> dict:
    """The catalogue, with the reasoning. Partition key IS the ordering
    guarantee, so each choice is published rather than buried."""
    bus = get_bus()
    sizes = bus.stats().get("topics", {}) if hasattr(bus, "stats") else {}
    return {
        "topics": [
            {
                "name": t.name,
                "key_field": t.key_field,
                "partitions": t.partitions,
                "retention": t.retention.value,
                "schema_version": t.schema_version,
                "purpose": t.purpose,
                "records": sizes.get(t.name, 0),
            }
            for t in sorted(TOPICS.values(), key=lambda x: x.name)
        ],
        "design_note": (
            "Partition key determines ordering: Kafka guarantees order WITHIN a "
            "partition and nothing across them. Order lifecycle and fills key on "
            "order id so a fill cannot be processed before the order it fills; "
            "market data keys on symbol; positions key on portfolio+symbol."
        ),
    }


@router.get("/replay/{topic}")
def replay(topic: str, from_offset: int = Query(0, ge=0),
           limit: int = Query(200, ge=1, le=5000)) -> dict:
    """Read the log without disturbing any consumer group's offsets.

    This is what makes an event log worth having: reprocessing with corrected
    code, without affecting live consumers.
    """
    if topic not in TOPICS:
        raise HTTPException(status_code=404, detail=f"Unknown topic '{topic}'")
    records = list(get_bus().replay(topic, from_offset=from_offset))[:limit]
    return {
        "topic": topic,
        "from_offset": from_offset,
        "count": len(records),
        "records": [
            {"partition": r.partition, "offset": r.offset,
             "key": r.envelope.key, "event_id": r.envelope.event_id,
             "schema_version": r.envelope.schema_version,
             "ts_ns": r.envelope.ts_ns, "payload": r.envelope.payload}
            for r in records
        ],
        "note": "Replay does not move any consumer group's committed offsets.",
    }


@router.get("/dlq/{topic}")
def dead_letters(topic: str) -> dict:
    """Messages a consumer could not process after its retries.

    Without a DLQ a poison message is retried forever and blocks its partition —
    every later message for that key waits behind one it will never get past.
    """
    if topic not in TOPICS:
        raise HTTPException(status_code=404, detail=f"Unknown topic '{topic}'")
    bus = get_bus()
    msgs = bus.dlq(topic) if hasattr(bus, "dlq") else []
    return {
        "topic": topic,
        "count": len(msgs),
        "messages": [{"key": m.key, "event_id": m.event_id, "attempt": m.attempt,
                      "payload": m.payload} for m in msgs],
    }


# ------------------------------------------------------------------- P&L
pnl_router = APIRouter(prefix="/api/pnl", tags=["pnl"])


def _projection(desk_id: str):
    from .desk import _desks
    s = _desks.get(desk_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"No desk session '{desk_id}'")
    return s


@pnl_router.get("/{desk_id}")
def pnl(desk_id: str) -> dict:
    s = _projection(desk_id)
    with s.lock:
        totals = s.pnl.totals()
        positions = [
            {
                "symbol": p.symbol,
                "net_qty": p.net_qty,
                "avg_cost": p.avg_cost,
                "mark": p.mark,
                "mark_age_s": p.mark_age_s(),
                "stale": p.is_stale(stale_after=s.pnl.stale_after_s),
                "realized": p.realized,
                # None rather than 0.0 when unvalued: a flat number and an
                # unknown number are different things.
                "unrealized": p.unrealized(),
                "fees": p.fees,
                "total": p.total(),
            }
            for p in s.pnl.positions(include_flat=True)
        ]
        stats = s.pnl.stats()
    return {"desk_id": desk_id, "totals": totals, "positions": positions,
            "projection": stats, "simulated": True}


@pnl_router.get("/{desk_id}/attribution")
def attribution(desk_id: str) -> dict:
    """Why P&L changed since the last call.

    Components must sum to the observed change; `residual` and `explained` are
    published rather than hidden, because an attribution that does not add up is
    worse than none — it looks authoritative and is not.
    """
    s = _projection(desk_id)
    with s.lock:
        rows = s.pnl.attribute()
    return {
        "desk_id": desk_id,
        "attribution": [
            {"symbol": a.symbol, "price_effect": a.price_effect,
             "trading_effect": a.trading_effect, "fee_effect": a.fee_effect,
             "realized_component": a.realized_component,
             "unrealized_released": a.unrealized_released,
             "total_change": a.total_change, "residual": a.residual,
             "explained": a.explained, "baseline": a.baseline}
            for a in rows
        ],
        "method_note": (
            "price effect = qty held BEFORE x (mark now - mark before). "
            "trading effect = realised change + unrealised change - price effect. "
            "Selling or buying AT the mark must attribute to zero."
        ),
    }


@pnl_router.get("/{desk_id}/reconcile")
def reconcile(desk_id: str) -> dict:
    """Compare the projection against the desk's own position.

    A projection nobody checks is a projection that will silently diverge. Every
    discrepancy is named, because "they differ" is not actionable.
    """
    s = _projection(desk_id)
    with s.lock:
        desk_pos = dict(s.desk.position())
        # The desk tracks one instrument per session in this simulation.
        authoritative = {}
        for p in s.pnl.positions(include_flat=True):
            authoritative[("DEFAULT", p.symbol)] = desk_pos.get("net_qty", 0.0)
        result = s.pnl.reconcile(authoritative)
    return {"desk_id": desk_id, **result}


@pnl_router.post("/{desk_id}/rebuild")
def rebuild(desk_id: str) -> dict:
    """Discard the projection and rebuild it from the event log.

    If a rebuild disagrees with the running total, the running total was wrong.
    That check is the reason a projection is worth more than a cached number.
    """
    s = _projection(desk_id)
    bus = get_bus()

    # Filter by desk. The topics are shared across every session on this bus, so
    # replaying them wholesale rebuilds THIS desk's projection from OTHER desks'
    # trades. Caught by the rebuild test as soon as a second session existed --
    # which is also exactly how it would present in a multi-desk deployment.
    def mine(payload: dict) -> bool:
        return payload.get("desk_id") in (desk_id, None, "")

    events = [r.envelope.payload for r in bus.replay("trades.executed")
              if mine(r.envelope.payload)]
    # Marks carry no desk_id, so they are scoped to the symbols this desk traded.
    symbols = {e.get("symbol") for e in events}
    events += [r.envelope.payload for r in bus.replay("market.normalized")
               if r.envelope.payload.get("symbol") in symbols]
    with s.lock:
        before = s.pnl.totals()
        applied = s.pnl.rebuild_from(events)
        after = s.pnl.totals()
    drift = {k: after[k] - before[k] for k in ("realized", "unrealized", "fees", "total")
             if isinstance(after.get(k), (int, float)) and isinstance(before.get(k), (int, float))}
    return {
        "desk_id": desk_id, "events_applied": applied,
        "before": before, "after": after, "drift": drift,
        "clean": all(abs(v) < 1e-6 for v in drift.values()),
        "note": ("Rebuild reproduced the running total."
                 if all(abs(v) < 1e-6 for v in drift.values()) else
                 "Rebuild DISAGREES with the running total, which means the "
                 "running total had drifted. The rebuild is the correct value."),
    }
