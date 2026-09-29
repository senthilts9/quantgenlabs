"""Domain events emitted by the trading flow.

Until now the bus existed and nothing published to it — which made it
infrastructure rather than architecture. This is the seam where the order flow
becomes a stream of facts other components can consume without being called.

WHAT IS AN EVENT HERE. A fact that has already happened, named in the past tense,
carrying everything a consumer needs to act on it. Not a command, not a request,
and never a pointer that says "go and look it up": a consumer that has to call
back into the producer to understand an event is coupled to the producer's
availability, which is most of what the bus was supposed to remove.

EVENTS ARE PUBLISHED AFTER THE FACT IS TRUE, never before. An `order.filled`
published before the fill is recorded is a lie the rest of the system will act on,
and the window is exactly long enough to matter. So every publisher here is called
from the point where the state change has already been committed.

PUBLISHING MUST NOT BREAK THE TRADE. If the bus is unavailable, the order still
happened — dropping it because telemetry failed would be strictly worse than
losing the event. Every publish is therefore wrapped: failures increment a counter
and are visible in /api/events/status, but never propagate into the order path.
That is a deliberate trade, and it is the reason the authoritative state lives in
the store rather than in the event log.
"""
from __future__ import annotations

import logging
import threading

from . import get_bus
from ...obs import trading_metrics as tm

log = logging.getLogger(__name__)

_failures = 0
_published = 0
_lock = threading.Lock()


def publish_stats() -> dict:
    with _lock:
        return {"published": _published, "failed": _failures}


def _safe(topic: str, key: str, payload: dict, correlation_id: str | None = None) -> bool:
    """Publish, but never let a bus failure reach the caller.

    The order already happened. Failing it because the event log was unreachable
    would be strictly worse than losing the event, which is why the store and not
    the log is authoritative.
    """
    global _published, _failures
    try:
        get_bus().publish(topic, key, payload, correlation_id=correlation_id)
        with _lock:
            _published += 1
        tm.observe_bus(topic, ok=True)
        return True
    except Exception as e:                       # noqa: BLE001 - deliberate
        with _lock:
            _failures += 1
        tm.observe_bus(topic, ok=False)
        log.warning("event publish failed on %s: %s", topic, e)
        return False


# --------------------------------------------------------------- lifecycle
def order_submitted(*, cl_ord_id: str, symbol: str, side: str, qty: float,
                    price: float | None, ord_type: str, tif: str,
                    desk_id: str, correlation_id: str | None = None) -> bool:
    return _safe("orders.lifecycle", cl_ord_id, {
        "event": "order.submitted",
        "cl_ord_id": cl_ord_id, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "ord_type": ord_type, "tif": tif,
        "desk_id": desk_id,
    }, correlation_id)


def order_state_changed(*, cl_ord_id: str, symbol: str, status: str,
                        cum_qty: float, leaves_qty: float, avg_px: float,
                        reject_text: str = "", desk_id: str = "",
                        correlation_id: str | None = None) -> bool:
    return _safe("orders.lifecycle", cl_ord_id, {
        "event": "order.state_changed",
        "cl_ord_id": cl_ord_id, "symbol": symbol, "status": status,
        "cum_qty": cum_qty, "leaves_qty": leaves_qty, "avg_px": avg_px,
        "reject_text": reject_text, "desk_id": desk_id,
    }, correlation_id)


def trade_executed(*, cl_ord_id: str, exec_id: str, symbol: str, side: str,
                   qty: float, price: float, desk_id: str = "",
                   correlation_id: str | None = None) -> bool:
    """A fill. Keyed on the ORDER id, same as the lifecycle topic, so a fill can
    never be processed before the order it fills."""
    return _safe("trades.executed", cl_ord_id, {
        "event": "trade.executed",
        "cl_ord_id": cl_ord_id, "exec_id": exec_id, "symbol": symbol,
        "side": side, "qty": qty, "price": price, "desk_id": desk_id,
    }, correlation_id)


def position_updated(*, portfolio_id: str, symbol: str, net_qty: float,
                     avg_cost: float, realized_pnl: float,
                     correlation_id: str | None = None) -> bool:
    # Keyed portfolio+symbol: the unit whose running total must be applied in
    # order. Keying on symbol alone would interleave two portfolios' deltas.
    return _safe("positions.updated", f"{portfolio_id}:{symbol}", {
        "event": "position.updated",
        "portfolio_id": portfolio_id, "symbol": symbol,
        "net_qty": net_qty, "avg_cost": avg_cost, "realized_pnl": realized_pnl,
    }, correlation_id)


def risk_breached(*, symbol: str, decision: str, side: str, qty: float,
                  price: float, limit_detail: str = "",
                  correlation_id: str | None = None) -> bool:
    """A pre-trade refusal. Retained as long as the trades it relates to, because
    "why was this not sent" is a question asked months later."""
    return _safe("risk.breaches", symbol, {
        "event": "risk.breached",
        "symbol": symbol, "decision": decision, "side": side,
        "qty": qty, "price": price, "limit_detail": limit_detail,
    }, correlation_id)


def market_tick(*, symbol: str, seq: int, price: float, size: float,
                verdict: str, correlation_id: str | None = None) -> bool:
    topic = "market.normalized" if verdict == "accepted" else "market.quality"
    return _safe(topic, symbol, {
        "event": "market.tick" if verdict == "accepted" else "market.rejected",
        "symbol": symbol, "seq": seq, "price": price, "size": size,
        "verdict": verdict,
    }, correlation_id)


def audit(*, kind: str, entity_id: str, actor: str, detail: dict,
          correlation_id: str | None = None) -> bool:
    return _safe("audit.events", entity_id, {
        "event": kind, "entity_id": entity_id, "actor": actor, "detail": detail,
    }, correlation_id)
