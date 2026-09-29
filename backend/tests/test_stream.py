"""WebSocket streaming tests.

The behaviours worth pinning are the ones that keep a streaming endpoint from
becoming an outage: frames are sequenced so a client can detect a gap, snapshots
are conflated so a fast market cannot saturate a socket, discrete events are NOT
conflated because a superseded event still happened, and a client count limit is
enforced by refusing rather than by degrading everyone already connected.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.connectivity import HAVE_CPP
from app.main import app

client = TestClient(app)


def test_status_documents_the_conflation_and_backpressure_policy():
    d = client.get("/api/stream/status").json()
    assert d["max_clients"] >= 1
    assert "conflated" in d["conflation_note"]
    # The policy must be stated, because "we drop frames" is surprising unless
    # the reason is visible.
    assert "DROPPED, not buffered" in d["backpressure_note"]
    assert d["bus"] in ("in-process", "kafka")


def test_unknown_desk_is_refused_not_streamed():
    with client.websocket_connect("/api/stream/desk/nonexistent") as ws:
        frame = json.loads(ws.receive_text())
        assert frame["kind"] == "error"
        assert "No desk session" in frame["data"]["detail"]


@pytest.mark.skipif(not HAVE_CPP, reason="desk needs the C++ extension")
def test_desk_stream_sends_sequenced_snapshots():
    r = client.post("/api/desk/session", json={"mid": 100.0, "levels": 3})
    desk_id = r.json()["desk_id"]
    try:
        with client.websocket_connect(f"/api/stream/desk/{desk_id}?interval_ms=50") as ws:
            hello = json.loads(ws.receive_text())
            assert hello["kind"] == "hello"
            assert hello["data"]["desk_id"] == desk_id
            assert hello["data"]["simulated"] is True

            seqs, kinds = [], []
            for _ in range(3):
                f = json.loads(ws.receive_text())
                seqs.append(f["seq"])
                kinds.append(f["kind"])
                assert f["ts_ms"] > 0
            assert kinds == ["snapshot"] * 3
            # Monotonic and gapless, so a client can detect a dropped frame.
            assert seqs == sorted(seqs)
            assert seqs == list(range(seqs[0], seqs[0] + 3))
    finally:
        client.delete(f"/api/desk/{desk_id}")


@pytest.mark.skipif(not HAVE_CPP, reason="desk needs the C++ extension")
def test_desk_snapshot_carries_book_and_position():
    r = client.post("/api/desk/session", json={"mid": 100.0, "levels": 4})
    desk_id = r.json()["desk_id"]
    try:
        with client.websocket_connect(f"/api/stream/desk/{desk_id}?interval_ms=50") as ws:
            json.loads(ws.receive_text())                 # hello
            snap = json.loads(ws.receive_text())["data"]
            assert snap["book"]["best_bid"] and snap["book"]["best_ask"]
            assert snap["book"]["is_crossed"] is False
            assert "position" in snap and "venue_stats" in snap
            # The blotter rides the first frame and is then only resent on change,
            # because it is the largest part and changes least often.
            assert "blotter" in snap
    finally:
        client.delete(f"/api/desk/{desk_id}")


@pytest.mark.skipif(not HAVE_CPP, reason="desk needs the C++ extension")
def test_blotter_is_resent_only_when_it_changes():
    r = client.post("/api/desk/session", json={"mid": 100.0, "levels": 3})
    desk_id = r.json()["desk_id"]
    try:
        with client.websocket_connect(f"/api/stream/desk/{desk_id}?interval_ms=50") as ws:
            json.loads(ws.receive_text())                 # hello
            first = json.loads(ws.receive_text())["data"]
            assert "blotter" in first                     # initial send
            second = json.loads(ws.receive_text())["data"]
            assert "blotter" not in second                # unchanged -> omitted
    finally:
        client.delete(f"/api/desk/{desk_id}")


def test_event_stream_rejects_an_undeclared_topic():
    """Topics are declared, not implicit; a typo must not open a stream that
    silently never delivers anything."""
    with client.websocket_connect("/api/stream/events/orders.lifcycle") as ws:
        frame = json.loads(ws.receive_text())
        assert frame["kind"] == "error"
        assert "Unknown topic" in frame["data"]["detail"]


def test_event_stream_delivers_published_events():
    from app.core.events import get_bus

    with client.websocket_connect("/api/stream/events/orders.lifecycle?interval_ms=50") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["kind"] == "hello"
        assert hello["data"]["topic"] == "orders.lifecycle"

        bus = get_bus()
        for i in range(3):
            bus.publish("orders.lifecycle", f"ORD-{i}", {"status": "new", "n": i})

        # Events are NOT conflated: all three must arrive.
        received = []
        for _ in range(6):
            frame = json.loads(ws.receive_text())
            if frame["kind"] == "events":
                received.extend(frame["data"]["events"])
            if len(received) >= 3:
                break
        assert len(received) >= 3
        assert {e["payload"]["n"] for e in received} >= {0, 1, 2}
        assert all(e["event_id"] for e in received)
