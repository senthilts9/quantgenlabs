"""Algorithmic execution API.

The metric assertions here exist because two gaps only surfaced by deploying and
reading /metrics: algo children bypassed the order counter entirely (the desk
looked idle while an algo was working it), and /run drove slices without counting
them. Neither was visible from the API responses, which is why the tests now
check the instrumentation and not just the payloads.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.connectivity import HAVE_CPP
from app.main import app

client = TestClient(app)
needs_cpp = pytest.mark.skipif(not HAVE_CPP, reason="venue needs the C++ extension")


@pytest.fixture
def desk():
    r = client.post("/api/desk/session",
                    json={"mid": 100.0, "levels": 10, "level_qty": 500})
    if r.status_code == 503:
        pytest.skip("C++ extension not built")
    did = r.json()["desk_id"]
    yield did
    client.delete(f"/api/desk/{did}")


def metric_value(name: str) -> float:
    """Sum of every series for a metric, from the live /metrics endpoint."""
    text = client.get("/metrics/").text
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name) and not line.startswith("#"):
            try:
                total += float(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                pass
    return total


# ----------------------------------------------------------------- preview
def test_preview_builds_a_schedule_without_sending(desk):
    """A trader should see what an algo intends to do before committing to it."""
    before = metric_value("qgl_orders_submitted_total")
    r = client.post("/api/algo/preview",
                    json={"algo": "vwap", "total_qty": 10_000, "slices": 9,
                          "duration_s": 300})
    assert r.status_code == 200
    d = r.json()
    assert d["scheduled_slices"] == 9
    assert d["exact"] is True
    assert sum(s["qty"] for s in d["slices"]) == pytest.approx(10_000)
    assert metric_value("qgl_orders_submitted_total") == before   # nothing sent


def test_preview_shows_the_vwap_u_shape():
    d = client.post("/api/algo/preview",
                    json={"algo": "vwap", "total_qty": 13_000, "slices": 13,
                          "duration_s": 300}).json()
    q = [s["qty"] for s in d["slices"]]
    assert q[0] > q[6] * 2 and q[-1] > q[6] * 2


def test_preview_reports_a_clamped_slice_count(desk):
    d = client.post("/api/algo/preview",
                    json={"algo": "twap", "total_qty": 37, "slices": 20,
                          "duration_s": 60, "min_slice_qty": 5}).json()
    assert d["clamped"] is True
    assert d["scheduled_slices"] < 20
    assert "minimum" in d["note"]


@pytest.mark.parametrize("body", [
    {"algo": "twap", "total_qty": 0, "slices": 5, "duration_s": 60},
    {"algo": "twap", "total_qty": -5, "slices": 5, "duration_s": 60},
    {"algo": "nope", "total_qty": 100, "slices": 5, "duration_s": 60},
    {"algo": "twap", "total_qty": 100, "slices": 0, "duration_s": 60},
])
def test_invalid_previews_are_refused(body):
    assert client.post("/api/algo/preview", json=body).status_code == 422


# ------------------------------------------------------------------- start
@needs_cpp
def test_start_sets_the_arrival_benchmark_from_the_mid(desk):
    r = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 500,
                          "slices": 5, "duration_s": 0})
    assert r.status_code == 200
    d = r.json()
    assert d["arrival_mid"] is not None
    assert d["state"] == "pending"
    assert d["simulated"] is True
    assert "cost of the decision" in d["arrival_note"]


@needs_cpp
def test_pov_without_a_rate_is_refused(desk):
    """POV has no basis on which to size a slice without a participation rate."""
    r = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "pov", "side": "1", "total_qty": 500,
                          "slices": 5, "duration_s": 0, "pov_rate": 0})
    assert r.status_code == 422
    assert "participation rate" in r.json()["detail"]


# --------------------------------------------------------------- execution
@needs_cpp
@pytest.mark.parametrize("algo", ["twap", "vwap"])
def test_run_executes_children_and_measures_shortfall(desk, algo):
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": algo, "side": "1", "total_qty": 1200,
                          "slices": 8, "duration_s": 0}).json()
    run = client.post(f"/api/algo/{desk}/{s['parent_id']}/run").json()
    p = run["parent"]
    assert p["state"] in ("completed", "partial")
    assert p["slices_sent"] == 8
    assert len(p["children"]) == 8
    assert p["filled_qty"] > 0
    assert p["shortfall_bps"] is not None


@needs_cpp
def test_partial_execution_is_reported_as_partial_not_completed(desk):
    """An aggressive IOC child only takes the top of book, so a parent larger
    than the available depth cannot complete. Reporting 'completed' there would
    claim a fill that did not happen."""
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 1_000_000,
                          "slices": 4, "duration_s": 0}).json()
    p = client.post(f"/api/algo/{desk}/{s['parent_id']}/run").json()["parent"]
    assert p["state"] == "partial"
    assert p["leaves_qty"] > 0
    assert "unfilled" in p["note"]


@needs_cpp
def test_children_never_exceed_the_parent(desk):
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 400,
                          "slices": 4, "duration_s": 0}).json()
    p = client.post(f"/api/algo/{desk}/{s['parent_id']}/run").json()["parent"]
    assert sum(c["requested"] for c in p["children"]) <= 400 + 1e-9


@needs_cpp
def test_step_is_idempotent_once_terminal(desk):
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 200,
                          "slices": 2, "duration_s": 0}).json()
    client.post(f"/api/algo/{desk}/{s['parent_id']}/run")
    for _ in range(3):
        r = client.post(f"/api/algo/{desk}/{s['parent_id']}/step").json()
        assert r["fired"] == 0


# ----------------------------------------------------------------- control
@needs_cpp
def test_pause_then_cancel_leaves_the_remainder_unfilled(desk):
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 500,
                          "slices": 5, "duration_s": 600}).json()
    pid = s["parent_id"]
    client.post(f"/api/algo/{desk}/{pid}/step")
    assert client.post(f"/api/algo/{desk}/{pid}/pause").json()["state"] == "paused"
    fired = client.post(f"/api/algo/{desk}/{pid}/step").json()["fired"]
    assert fired == 0
    assert client.post(f"/api/algo/{desk}/{pid}/resume").json()["state"] == "working"
    c = client.post(f"/api/algo/{desk}/{pid}/cancel").json()
    assert c["state"] == "cancelled"
    assert c["leaves_qty"] > 0
    assert "NOT filled" in c["note"]


@needs_cpp
def test_unknown_parent_is_404(desk):
    for path in ("step", "pause", "resume", "cancel"):
        assert client.post(f"/api/algo/{desk}/P-nope/{path}").status_code == 404
    assert client.get(f"/api/algo/{desk}/P-nope").status_code == 404


def test_unknown_desk_is_404():
    assert client.post("/api/algo/nodesk/start",
                       json={"algo": "twap", "side": "1", "total_qty": 10,
                             "slices": 1, "duration_s": 0}).status_code == 404


# --------------------------------------------------------------------- TCA
@needs_cpp
def test_tca_aggregates_shortfall_across_parents(desk):
    for algo in ("twap", "vwap"):
        s = client.post(f"/api/algo/{desk}/start",
                        json={"algo": algo, "side": "1", "total_qty": 600,
                              "slices": 4, "duration_s": 0}).json()
        client.post(f"/api/algo/{desk}/{s['parent_id']}/run")
    t = client.get(f"/api/algo/{desk}").json()["tca"]
    assert t["orders_measured"] >= 2
    assert t["avg_shortfall_bps"] is not None
    assert "POSITIVE IS ALWAYS BAD" in t["note"]


@needs_cpp
def test_a_buyer_crossing_the_spread_shows_positive_shortfall(desk):
    """Crossing costs the half-spread. A shortfall of zero here would mean the
    benchmark was re-baselined to the fill price, which flatters every algo."""
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 300,
                          "slices": 3, "duration_s": 0}).json()
    p = client.post(f"/api/algo/{desk}/{s['parent_id']}/run").json()["parent"]
    assert p["shortfall_bps"] > 0


# ------------------------------------------------------------ instrumentation
@needs_cpp
def test_algo_children_increment_the_order_counter(desk):
    """Children are orders. Counting them only at the HTTP endpoint made every
    algo-generated order invisible to the order-flow panels, so the desk looked
    idle while an algo was working it. Found by deploying and reading /metrics."""
    before = metric_value("qgl_orders_submitted_total")
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 400,
                          "slices": 4, "duration_s": 0}).json()
    client.post(f"/api/algo/{desk}/{s['parent_id']}/run")
    assert metric_value("qgl_orders_submitted_total") >= before + 4


@needs_cpp
def test_run_counts_slices_not_just_step(desk):
    """/step counted slices and /run did not, so driving a parent to completion
    left the slice panel flat while children were genuinely being sent."""
    before = metric_value("qgl_algo_slices_total")
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 500,
                          "slices": 5, "duration_s": 0}).json()
    client.post(f"/api/algo/{desk}/{s['parent_id']}/run")
    assert metric_value("qgl_algo_slices_total") >= before + 5


@needs_cpp
def test_shortfall_is_recorded_as_a_histogram(desk):
    before = metric_value("qgl_algo_shortfall_bps_count")
    s = client.post(f"/api/algo/{desk}/start",
                    json={"algo": "twap", "side": "1", "total_qty": 300,
                          "slices": 3, "duration_s": 0}).json()
    client.post(f"/api/algo/{desk}/{s['parent_id']}/run")
    assert metric_value("qgl_algo_shortfall_bps_count") > before
