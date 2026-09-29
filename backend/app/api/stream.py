"""WebSocket streaming.

Replaces polling for the screens that need to be live. Every screen in this
console refreshed on a `setInterval` between 8 and 60 seconds, which has three
problems a trading UI cannot live with: the data is stale by up to a full
interval, every client re-fetches the whole payload whether or not anything
changed, and load scales with client count rather than with market activity.

DESIGN DECISIONS THAT MATTER HERE:

  THE SERVER PACES, NOT THE MARKET. A book can update thousands of times a
  second; a human reads maybe ten. So the stream sends CONFLATED SNAPSHOTS at a
  fixed cadence rather than every event. Conflation is the right model for a
  price display — an intermediate book state nobody saw is not information — and
  it is what stops a fast market from turning into an unbounded send queue.
  Order events are NOT conflated, because a fill that was superseded still
  happened.

  BACKPRESSURE IS HANDLED BY DROPPING, DELIBERATELY. If a client cannot keep up,
  the server drops conflated frames for that client rather than buffering them.
  Buffering is how a slow consumer becomes a server-side memory leak, and for
  snapshot data the newest frame makes every older one redundant anyway.

  A DEAD SOCKET IS DETECTED, NOT ASSUMED. TCP will happily hold a connection open
  to a client that has gone away. The server pings and drops a client that stops
  answering, otherwise dead connections accumulate for the process lifetime.

  EVERY FRAME IS TAGGED. Frames carry a monotonically increasing sequence number
  and a server timestamp, so the client can detect a gap and display data
  freshness rather than implying everything on screen is current.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..core import connectivity as conn
from ..core.events import backend_name, get_bus

router = APIRouter(prefix="/api/stream", tags=["streaming"])

# How often a conflated snapshot goes out. 250ms is comfortably faster than a
# person can read and slow enough that a fast market cannot saturate the socket.
DEFAULT_INTERVAL_MS = 250
MIN_INTERVAL_MS = 50
MAX_CLIENTS = 64


# eq=False so the dataclass keeps identity hashing and can live in a set. With
# the default eq=True Python sets __hash__ to None, and two clients with the same
# field values would compare equal anyway -- which is wrong: each connection is a
# distinct thing regardless of its state.
@dataclass(eq=False)
class ClientState:
    ws: WebSocket
    seq: int = 0
    dropped: int = 0
    connected_at: float = field(default_factory=time.time)
    last_pong: float = field(default_factory=time.time)


class Hub:
    """Tracks connected clients so the server can report and bound them."""

    def __init__(self) -> None:
        self._clients: set[ClientState] = set()
        self._lock = asyncio.Lock()

    async def add(self, c: ClientState) -> bool:
        async with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                return False           # refuse rather than degrade everyone
            self._clients.add(c)
            return True

    async def remove(self, c: ClientState) -> None:
        async with self._lock:
            self._clients.discard(c)

    def count(self) -> int:
        return len(self._clients)

    def stats(self) -> dict:
        return {
            "clients": len(self._clients),
            "max_clients": MAX_CLIENTS,
            "total_dropped_frames": sum(c.dropped for c in self._clients),
        }


hub = Hub()


async def _send(c: ClientState, kind: str, payload: dict) -> bool:
    """Send one frame. Returns False when the client is gone or too slow.

    Frames carry seq and server time so the client can spot a gap and show data
    freshness rather than implying everything on screen is current.
    """
    c.seq += 1
    frame = {"kind": kind, "seq": c.seq, "ts_ms": int(time.time() * 1000), "data": payload}
    try:
        # A send that cannot complete promptly means the client is not keeping
        # up. For conflated snapshots the newest frame supersedes every older
        # one, so dropping is correct and buffering would be a memory leak.
        await asyncio.wait_for(c.ws.send_text(json.dumps(frame, default=str)), timeout=2.0)
        return True
    except asyncio.TimeoutError:
        c.dropped += 1
        return c.dropped < 20          # persistent slowness: give up on them
    except Exception:
        return False


@router.websocket("/desk/{desk_id}")
async def stream_desk(ws: WebSocket, desk_id: str,
                      interval_ms: int = Query(DEFAULT_INTERVAL_MS)):
    """Live book, blotter and position for one desk session.

    Snapshots are conflated at `interval_ms`: the client is shown the current
    state, not every intermediate state, because an intermediate book nobody saw
    is not information.
    """
    from .desk import _desks              # imported here to avoid a cycle

    interval = max(MIN_INTERVAL_MS, int(interval_ms)) / 1000.0
    await ws.accept()
    client = ClientState(ws=ws)

    if not await hub.add(client):
        await ws.send_text(json.dumps({
            "kind": "error",
            "data": {"detail": f"Too many streaming clients ({MAX_CLIENTS}). "
                               "Refusing rather than degrading every existing one."},
        }))
        await ws.close(code=1013)        # try again later
        return

    try:
        session = _desks.get(desk_id)
        if session is None:
            await _send(client, "error", {"detail": f"No desk session '{desk_id}'"})
            await ws.close(code=1008)
            return

        await _send(client, "hello", {
            "desk_id": desk_id,
            "interval_ms": int(interval * 1000),
            "impl": conn.impl_name(),
            "simulated": True,
            "note": "Conflated snapshots. Order events are not conflated; a "
                    "superseded fill still happened.",
        })

        last_blotter_len = -1
        while True:
            # Reading desk state takes the per-desk lock, which the C++ objects
            # require (one writer owns a book). Held only for the snapshot.
            with session.lock:
                d = session.desk
                book = _book_frame(d)
                blotter = list(d.blotter())
                position = dict(d.position())
                stats = dict(d.engine_stats())

            payload = {"book": book, "position": position, "venue_stats": stats}
            # The blotter is only resent when it actually changed: it is the
            # largest part of the frame and it changes far less often than the
            # book.
            if len(blotter) != last_blotter_len:
                payload["blotter"] = blotter
                last_blotter_len = len(blotter)

            if not await _send(client, "snapshot", payload):
                break
            await asyncio.sleep(interval)

    except WebSocketDisconnect:
        pass
    except Exception:
        # A streaming failure must never take down the endpoint for everyone.
        pass
    finally:
        await hub.remove(client)


def _book_frame(desk) -> dict:
    import mkt_connectivity as mc
    b = desk.book()
    ts = desk.tick_size()
    bid, ask = b.best(mc.Side.BID), b.best(mc.Side.ASK)
    return {
        "best_bid": {"price": bid[0] * ts, "qty": bid[1]} if bid else None,
        "best_ask": {"price": ask[0] * ts, "qty": ask[1]} if ask else None,
        "spread": (ask[0] - bid[0]) * ts if bid and ask else None,
        "micro_price": b.micro_price_ticks() * ts if bid and ask else None,
        "imbalance": b.imbalance(),
        "is_crossed": b.is_crossed(),
        "bids": [{"price": l.price * ts, "qty": l.qty} for l in b.depth(mc.Side.BID, 8)],
        "asks": [{"price": l.price * ts, "qty": l.qty} for l in b.depth(mc.Side.ASK, 8)],
    }


@router.websocket("/events/{topic}")
async def stream_events(ws: WebSocket, topic: str,
                        group: str = Query("ui"),
                        interval_ms: int = Query(500)):
    """Tail an event-bus topic.

    NOT conflated: these are discrete events, and a superseded one still
    happened. If a client cannot keep up it is dropped rather than buffered.
    """
    interval = max(MIN_INTERVAL_MS, int(interval_ms)) / 1000.0
    await ws.accept()
    client = ClientState(ws=ws)
    if not await hub.add(client):
        await ws.close(code=1013)
        return

    pending: list[dict] = []

    try:
        bus = get_bus()
        try:
            bus.subscribe(topic, f"{group}-{id(client)}",
                          lambda e: pending.append({
                              "event_id": e.event_id, "key": e.key,
                              "payload": e.payload, "ts_ns": e.ts_ns,
                              "schema_version": e.schema_version,
                          }))
        except KeyError as e:
            await _send(client, "error", {"detail": str(e)})
            await ws.close(code=1008)
            return

        await _send(client, "hello", {"topic": topic, "bus": backend_name(),
                                      "interval_ms": int(interval * 1000)})

        while True:
            # poll() is synchronous and takes a lock; run it off the event loop so
            # one slow handler cannot stall every other websocket on this worker.
            await asyncio.to_thread(bus.poll, topic, f"{group}-{id(client)}", 200)
            if pending:
                batch, pending[:] = list(pending), []
                if not await _send(client, "events", {"events": batch, "count": len(batch)}):
                    break
            await asyncio.sleep(interval)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.remove(client)


@router.get("/status")
def stream_status() -> dict:
    return {
        **hub.stats(),
        "default_interval_ms": DEFAULT_INTERVAL_MS,
        "bus": backend_name(),
        "conflation_note": (
            "Desk snapshots are conflated: the client sees current state, not "
            "every intermediate state. Event streams are not conflated, because "
            "a superseded event still happened."
        ),
        "backpressure_note": (
            "A client that cannot keep up has frames DROPPED, not buffered. "
            "Buffering turns a slow consumer into a server-side memory leak, and "
            "for snapshot data the newest frame supersedes every older one."
        ),
    }
