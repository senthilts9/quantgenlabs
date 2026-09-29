"""Event bus and P&L API.

The rebuild test is the important one. A projection that cannot be reproduced
from the log is a number nobody can defend, and that check caught a real defect
the first time it ran: marks were being applied to the projection without being
published, so a rebuild disagreed by the entire unrealised P&L.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.connectivity import HAVE_CPP
from app.main import app

client = TestClient(app)
needs_cpp = pytest.mark.skipif(not HAVE_CPP, reason="desk needs the C++ extension")


@pytest.fixture
def desk():
    r = client.post("/api/desk/session", json={"mid": 100.0, "levels": 4})
    if r.status_code == 503:
        pytest.skip("C++ extension not built")
    did = r.json()["desk_id"]
    yield did
    client.delete(f"/api/desk/{did}")


# ------------------------------------------------------------------ bus
def test_status_reports_backend_and_durability_limits():
    d = client.get("/api/events/status").json()
    assert d["backend"] in ("in-process", "kafka")
    # The limits must be stated, not discovered after a restart.
    if d["backend"] == "in-process":
        assert "NOT durable across a restart" in d["durability_note"]
    assert "published" in d["publisher"] and "failed" in d["publisher"]


def test_topics_publish_their_partitioning_rationale():
    d = client.get("/api/events/topics").json()
    names = {t["name"] for t in d["topics"]}
    assert {"orders.lifecycle", "trades.executed", "audit.events"} <= names
    by = {t["name"]: t for t in d["topics"]}
    # Ordering guarantees depend on the key, so the key is published.
    assert by["orders.lifecycle"]["key_field"] == "order_id"
    assert by["trades.executed"]["key_field"] == "order_id"
    assert by["audit.events"]["retention"] == "compact"
    assert "order WITHIN a partition" in d["design_note"]


def test_unknown_topic_is_404_on_replay_and_dlq():
    assert client.get("/api/events/replay/nope.nope").status_code == 404
    assert client.get("/api/events/dlq/nope.nope").status_code == 404


# ------------------------------------------------------- events actually flow
@needs_cpp
def test_trading_publishes_lifecycle_and_fill_events(desk):
    """The bus is architecture only if the order flow actually uses it."""
    before = client.get("/api/events/status").json()["publisher"]["published"]
    client.post(f"/api/desk/{desk}/order",
                json={"side": "1", "qty": 200, "price": 100.02})
    after = client.get("/api/events/status").json()["publisher"]["published"]
    assert after > before

    rp = client.get("/api/events/replay/trades.executed").json()
    assert rp["count"] >= 1
    p = rp["records"][-1]["payload"]
    assert p["event"] == "trade.executed"
    assert p["symbol"] and p["qty"] > 0 and p["price"] > 0


@needs_cpp
def test_fills_for_one_order_share_a_partition(desk):
    """Keyed on order id so a fill can never be processed before the order it
    fills, or out of order against another fill on the same order."""
    r = client.post(f"/api/desk/{desk}/order",
                    json={"side": "1", "qty": 400, "price": 100.05}).json()
    cl = r["this_order"]["cl_ord_id"]
    rec = client.get("/api/events/replay/trades.executed").json()["records"]
    mine = [x for x in rec if x["key"] == cl]
    assert len(mine) >= 1
    assert len({x["partition"] for x in mine}) == 1


@needs_cpp
def test_replay_does_not_disturb_consumers(desk):
    client.post(f"/api/desk/{desk}/order", json={"side": "1", "qty": 50, "price": 100.02})
    a = client.get("/api/events/replay/trades.executed").json()
    b = client.get("/api/events/replay/trades.executed").json()
    assert a["count"] == b["count"]        # reading twice changes nothing
    assert "does not move any consumer group" in a["note"]


def test_replay_from_an_offset():
    d = client.get("/api/events/replay/trades.executed",
                   params={"from_offset": 999999}).json()
    assert d["count"] == 0                 # past the end is empty, not an error


# ------------------------------------------------------------------- P&L
@needs_cpp
def test_pnl_reports_positions_and_totals(desk):
    client.post(f"/api/desk/{desk}/order", json={"side": "1", "qty": 200, "price": 100.02})
    d = client.get(f"/api/pnl/{desk}").json()
    assert d["simulated"] is True
    assert d["totals"]["positions_valued"] >= 1
    pos = d["positions"][0]
    assert pos["net_qty"] > 0
    assert pos["avg_cost"] > 0
    assert pos["mark"] is not None
    assert pos["stale"] is False


@needs_cpp
def test_buying_through_the_spread_shows_an_immediate_mark_loss(desk):
    """Crossing the spread costs the half-spread immediately. A P&L that showed
    zero here would not be marking to market."""
    client.post(f"/api/desk/{desk}/order", json={"side": "1", "qty": 200, "price": 100.05})
    d = client.get(f"/api/pnl/{desk}").json()
    assert d["totals"]["unrealized"] < 0


@needs_cpp
def test_attribution_components_sum_to_the_change(desk):
    client.post(f"/api/desk/{desk}/order", json={"side": "1", "qty": 200, "price": 100.02})
    client.get(f"/api/pnl/{desk}/attribution")          # consume the baseline
    client.post(f"/api/desk/{desk}/order", json={"side": "2", "qty": 50, "price": 99.98})
    rows = client.get(f"/api/pnl/{desk}/attribution").json()["attribution"]
    for a in rows:
        if a["baseline"]:
            continue
        total = a["price_effect"] + a["trading_effect"] + a["fee_effect"]
        assert total == pytest.approx(a["total_change"], abs=1e-9)
        assert a["explained"] is True


@needs_cpp
def test_first_attribution_is_a_baseline(desk):
    client.post(f"/api/desk/{desk}/order", json={"side": "1", "qty": 100, "price": 100.02})
    rows = client.get(f"/api/pnl/{desk}/attribution").json()["attribution"]
    assert rows and rows[0]["baseline"] is True


@needs_cpp
def test_rebuild_from_the_log_reproduces_the_running_total(desk):
    """The property that makes the projection defensible.

    This caught a real defect on its first run: marks were applied to the
    projection without being published, so a rebuild disagreed by the entire
    unrealised P&L. Marks are now published before being applied.
    """
    client.post(f"/api/desk/{desk}/order", json={"side": "1", "qty": 300, "price": 100.02})
    client.post(f"/api/desk/{desk}/order", json={"side": "2", "qty": 100, "price": 99.98})
    before = client.get(f"/api/pnl/{desk}").json()["totals"]

    rb = client.post(f"/api/pnl/{desk}/rebuild").json()
    assert rb["events_applied"] > 0
    assert rb["clean"] is True, rb["drift"]
    assert rb["after"]["total"] == pytest.approx(before["total"], abs=1e-9)
    assert "reproduced" in rb["note"]


@needs_cpp
def test_reconcile_compares_projection_against_the_desk(desk):
    client.post(f"/api/desk/{desk}/order", json={"side": "1", "qty": 150, "price": 100.02})
    r = client.get(f"/api/pnl/{desk}/reconcile").json()
    assert "breaks" in r and "clean" in r
    if not r["clean"]:
        # A break must name what differs; "they differ" is not actionable.
        b = r["breaks"][0]
        assert {"symbol", "projection", "authoritative", "difference"} <= set(b)


def test_pnl_for_unknown_desk_is_404():
    assert client.get("/api/pnl/nope").status_code == 404
    assert client.get("/api/pnl/nope/attribution").status_code == 404
    assert client.post("/api/pnl/nope/rebuild").status_code == 404
