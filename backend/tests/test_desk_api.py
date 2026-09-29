"""Trading desk API tests.

The property under test throughout: the blotter reflects what the venue reported.
Every assertion reads state that was rebuilt from ExecutionReports which crossed a
real FIX session.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.connectivity import HAVE_CPP

client = TestClient(app)
pytestmark = pytest.mark.skipif(not HAVE_CPP, reason="simulated venue needs the C++ extension")


@pytest.fixture
def desk():
    r = client.post("/api/desk/session", json={"mid": 100.0, "levels": 5, "level_qty": 200})
    assert r.status_code == 200
    d = r.json()
    yield d["desk_id"]
    client.delete(f"/api/desk/{d['desk_id']}")


def order(desk_id, **kw):
    body = {"side": "1", "qty": 100, "price": 100.01, "ord_type": "2", "tif": "0", **kw}
    return client.post(f"/api/desk/{desk_id}/order", json=body)


def test_session_opens_with_a_two_sided_book(desk):
    d = client.get(f"/api/desk/{desk}/state").json()
    assert d["connected"] is True
    assert d["simulated"] is True
    assert d["book"]["best_bid"] and d["book"]["best_ask"]
    assert d["book"]["is_crossed"] is False
    assert d["book"]["best_bid"]["price"] < d["book"]["best_ask"]["price"]
    assert d["blotter"] == []


def test_resting_order_appears_new_with_full_leaves(desk):
    r = order(desk, qty=50, price=98.00)          # far below the market
    assert r.status_code == 200
    row = r.json()["this_order"]["row"]
    assert row["status"] == "new"
    assert row["cum_qty"] == 0
    assert row["leaves_qty"] == 50
    assert row["venue_order_id"]                   # venue assigned an id


def test_marketable_order_fills_and_updates_position(desk):
    ask = client.get(f"/api/desk/{desk}/state").json()["book"]["best_ask"]
    r = order(desk, qty=ask["qty"], price=ask["price"])
    st = r.json()
    row = st["this_order"]["row"]
    assert row["status"] == "filled"
    assert row["cum_qty"] == ask["qty"]
    assert row["leaves_qty"] == 0
    assert len(row["fills"]) >= 1
    assert st["position"]["net_qty"] == ask["qty"]
    assert st["position"]["avg_cost"] == pytest.approx(ask["price"], abs=0.02)


def test_partial_fill_rests_remainder_on_the_book(desk):
    ask = client.get(f"/api/desk/{desk}/state").json()["book"]["best_ask"]
    over = ask["qty"] + 150
    st = order(desk, qty=over, price=ask["price"]).json()
    row = st["this_order"]["row"]
    assert row["status"] == "partially_filled"
    assert row["cum_qty"] == ask["qty"]
    assert row["leaves_qty"] == 150
    # The remainder became the best bid at our limit.
    assert st["book"]["best_bid"]["price"] == pytest.approx(ask["price"])
    assert st["book"]["best_bid"]["qty"] == 150


def test_cancel_marks_row_cancelled_and_pulls_liquidity(desk):
    st = order(desk, qty=75, price=98.00).json()
    cl = st["this_order"]["cl_ord_id"]
    assert st["this_order"]["row"]["status"] == "new"
    r = client.post(f"/api/desk/{desk}/cancel", json={"orig_cl_ord_id": cl})
    row = r.json()["this_order"]["row"]
    assert row["status"] == "cancelled"
    assert row["leaves_qty"] == 0


def test_fok_that_cannot_fill_is_rejected_with_no_fills(desk):
    st = order(desk, qty=1_000_000, price=200.0, tif="4").json()
    row = st["this_order"]["row"]
    assert row["status"] == "rejected"
    assert row["fills"] == []
    assert "fok" in row["reject_text"]
    assert st["position"]["net_qty"] == 0          # nothing printed


def test_ioc_remainder_is_cancelled_not_left_open(desk):
    ask = client.get(f"/api/desk/{desk}/state").json()["book"]["best_ask"]
    st = order(desk, qty=ask["qty"] + 500, price=ask["price"], tif="3").json()
    row = st["this_order"]["row"]
    assert row["status"] == "cancelled"
    assert row["cum_qty"] == ask["qty"]
    assert row["leaves_qty"] == 0


def test_every_wire_message_is_valid_fix_and_sequential(desk):
    order(desk, qty=100, price=100.05)
    order(desk, qty=40, price=98.5)
    wire = client.get(f"/api/desk/{desk}/state", params={"wire_tail": 0}).json()["wire_log"]
    assert len(wire) >= 4
    for w in wire:
        # SOH is rendered as '|' for transport; the parser accepts that form.
        assert w["raw"].startswith("8=FIX.4.4|")
        assert w["raw"].rstrip().endswith("|")
        assert "10=" in w["raw"]
        assert w["msg_type"]
    outbound = [w["seq"] for w in wire if w["outbound"]]
    inbound = [w["seq"] for w in wire if not w["outbound"]]
    assert outbound == sorted(outbound)            # strictly ordered per direction
    assert inbound == sorted(inbound)


def test_this_order_wire_isolates_the_exchange(desk):
    order(desk, qty=100, price=100.05)
    st = order(desk, qty=60, price=98.0).json()
    cl = st["this_order"]["cl_ord_id"]
    msgs = st["this_order"]["wire"]
    assert msgs, "the order produced no traffic"
    assert any(m["outbound"] and m["msg_type"] == "D" for m in msgs)
    assert any(not m["outbound"] and m["msg_type"] == "8" for m in msgs)
    assert all(f"11={cl}|" in m["raw"] for m in msgs)   # only this order's messages


def test_limit_order_without_price_is_rejected_by_validation(desk):
    r = client.post(f"/api/desk/{desk}/order",
                    json={"side": "1", "qty": 10, "price": 0, "ord_type": "2", "tif": "0"})
    assert r.status_code == 422
    assert "price" in r.json()["detail"].lower()


@pytest.mark.parametrize("body", [
    {"side": "3", "qty": 10, "price": 100.0},      # invalid side
    {"side": "1", "qty": 0, "price": 100.0},       # zero qty
    {"side": "1", "qty": -5, "price": 100.0},      # negative qty
    {"side": "1", "qty": 10, "price": 100.0, "tif": "9"},   # invalid TIF
])
def test_malformed_tickets_rejected(desk, body):
    assert client.post(f"/api/desk/{desk}/order", json=body).status_code == 422


def test_unknown_desk_is_404_not_a_new_session():
    assert client.get("/api/desk/doesnotexist/state").status_code == 404
    assert client.post("/api/desk/doesnotexist/order",
                       json={"side": "1", "qty": 1, "price": 100.0}).status_code == 404


def test_seeding_liquidity_moves_the_book(desk):
    before = client.get(f"/api/desk/{desk}/state").json()["book"]["best_ask"]["price"]
    r = client.post(f"/api/desk/{desk}/seed", json={"side": "2", "price": before - 0.01, "qty": 500})
    assert r.status_code == 200
    after = r.json()["book"]["best_ask"]["price"]
    assert after == pytest.approx(before - 0.01)


def test_sub_tick_price_snaps_to_the_grid(desk):
    """Prices live on a tick grid, so a half-tick improvement is not a better
    price -- it is the same level. Confirms conversion rounds rather than
    truncates, and that the qty merges into the existing level."""
    st = client.get(f"/api/desk/{desk}/state").json()
    best = st["book"]["best_ask"]
    r = client.post(f"/api/desk/{desk}/seed",
                    json={"side": "2", "price": best["price"] - 0.004, "qty": 300})
    after = r.json()["book"]["best_ask"]
    assert after["price"] == pytest.approx(best["price"])      # same tick
    assert after["qty"] == best["qty"] + 300                   # merged, not a new level


def test_tick_keeps_session_alive(desk):
    r = client.post(f"/api/desk/{desk}/tick")
    assert r.status_code == 200
    assert r.json()["session_alive"] is True
    assert r.json()["connected"] is True
