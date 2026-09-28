"""Connectivity API tests.

Two properties get particular attention, because they are the ones that would let
the platform publish something untrue:

  * /performance must serve what the benchmark harness wrote, and must 404 rather
    than synthesise a plausible default when no results exist.
  * Anything touching order flow must be labelled simulated.
"""
from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.connectivity import HAVE_CPP

client = TestClient(app)
needs_cpp = pytest.mark.skipif(not HAVE_CPP, reason="C++ extension not built")


def test_status_reports_which_implementation():
    r = client.get("/api/connectivity/status")
    assert r.status_code == 200
    d = r.json()
    assert d["impl"] in ("cpp", "python")
    assert d["cpp_extension_built"] is HAVE_CPP
    assert d["simulated"] is True
    # Components with no fallback must say so rather than claiming to work.
    if not HAVE_CPP:
        assert d["components"]["matching_engine"] == "unavailable"
        assert d["components"]["pretrade_gate"] == "unavailable"


# ------------------------------------------------------------------ performance
def test_performance_serves_measured_results_or_404s():
    r = client.get("/api/connectivity/performance")
    if r.status_code == 404:
        # Acceptable: no results on disk. It must NOT invent them.
        assert "will not synthesise" in r.json()["detail"]
        return
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    env = next(iter(d["runs"].values()))["environment"]
    # The fields that make a number falsifiable must all be present.
    for k in ("cpu", "physical_cores", "logical_cores", "compiler", "flags",
              "simd_compiled", "simd_available_on_cpu"):
        assert k in env, f"missing environment field: {k}"


def test_performance_never_claims_an_unmeasured_eight_core_figure():
    r = client.get("/api/connectivity/performance")
    if r.status_code == 404:
        pytest.skip("no benchmark results on disk")
    d = r.json()
    for run in d["runs"].values():
        mc = run["monte_carlo"]
        if not mc["eight_physical_core_measured"]:
            # It must say so explicitly, and name the real core count.
            assert "eight_physical_core_note" in mc
            assert "NOT MEASURED" in mc["eight_physical_core_note"]
        # No scaling row may exceed the machine's logical core count.
        assert all(row["threads"] <= run["environment"]["logical_cores"]
                   for row in mc["scaling"])


def test_performance_discloses_timesteps_and_smt():
    r = client.get("/api/connectivity/performance")
    if r.status_code == 404:
        pytest.skip("no benchmark results on disk")
    d = r.json()
    assert d["summary"]["timesteps_per_path"] == 1
    for run in d["runs"].values():
        mc = run["monte_carlo"]
        assert "timestep_note" in mc and "252" in mc["timestep_note"]
        phys = run["environment"]["physical_cores"]
        for row in mc["scaling"]:
            assert row["oversubscribed_smt"] == (row["threads"] > phys)


# ------------------------------------------------------------------------ book
def test_book_applies_updates_and_counts_rejects():
    r = client.post("/api/connectivity/book", json={
        "floor_ticks": 1, "n_ticks": 50_000, "depth_levels": 3,
        "updates": [
            {"side": 0, "price_ticks": 9990, "qty": 400},
            {"side": 0, "price_ticks": 9985, "qty": 600},
            {"side": 1, "price_ticks": 10010, "qty": 300},
            {"side": 1, "price_ticks": 999_999, "qty": 10},   # out of range
        ],
    })
    assert r.status_code == 200
    d = r.json()
    assert d["applied"] == 3 and d["rejected"] == 1
    assert d["best_bid"] == {"price_ticks": 9990, "qty": 400}
    assert d["best_ask"] == {"price_ticks": 10010, "qty": 300}
    assert d["spread_ticks"] == 20
    assert d["is_crossed"] is False
    assert [b["price_ticks"] for b in d["bids"]] == [9990, 9985]
    assert d["simulated"] is True


def test_book_empty_reports_none_not_zero():
    r = client.post("/api/connectivity/book", json={"updates": []})
    d = r.json()
    assert d["best_bid"] is None and d["best_ask"] is None
    assert d["mid_ticks"] == 0.0


def test_book_detects_crossed_state():
    r = client.post("/api/connectivity/book", json={"updates": [
        {"side": 0, "price_ticks": 10000, "qty": 100},
        {"side": 1, "price_ticks": 9995, "qty": 100},
    ]})
    assert r.json()["is_crossed"] is True


# ------------------------------------------------------------------------ feed
def test_feed_validate_classifies_faults():
    ticks = [{"seq": i, "ts_ns": i * 10**9, "price": 100.0} for i in range(1, 51)]
    ticks += [
        {"seq": 50, "ts_ns": 51 * 10**9, "price": 100.0},     # duplicate
        {"seq": 80, "ts_ns": 52 * 10**9, "price": 100.0},     # gap
        {"seq": 81, "ts_ns": 53 * 10**9, "price": 9999.0},    # out of band
    ]
    r = client.post("/api/connectivity/feed/validate",
                    json={"ticks": ticks, "price_min": 50, "price_max": 200})
    assert r.status_code == 200
    d = r.json()
    assert d["verdicts"]["accepted"] == 50
    assert d["verdicts"]["duplicate"] == 1
    assert d["verdicts"]["sequence_gap"] == 1
    assert d["verdicts"]["price_out_of_band"] == 1
    assert d["needs_recovery"] is True
    assert 0.0 < d["reject_rate"] < 1.0


def test_feed_rejects_oversized_batch():
    r = client.post("/api/connectivity/feed/validate", json={"ticks": []})
    assert r.status_code == 422        # min_length=1


# -------------------------------------------------------------------- pretrade
@needs_cpp
@pytest.mark.parametrize("body,expected", [
    (dict(side=0, qty=500, price=100.10, reference_price=100.0), "accept"),
    (dict(side=0, qty=50_000, price=100.10, reference_price=100.0), "max_order_qty"),
    (dict(side=0, qty=100, price=130.0, reference_price=100.0), "price_collar"),
    (dict(side=1, qty=100, price=70.0, reference_price=100.0), "price_collar"),
    (dict(side=0, qty=100, price=100.1, reference_price=0.0, reference_ok=False),
     "no_reference_price"),
    (dict(side=0, qty=1, price=100.1, reference_price=100.0, kill_switch=True),
     "kill_switch_engaged"),
])
def test_pretrade_decisions(body, expected):
    r = client.post("/api/connectivity/pretrade", json=body)
    assert r.status_code == 200
    assert r.json()["decision"] == expected
    assert r.json()["simulated"] is True


@needs_cpp
def test_pretrade_position_limit_uses_resulting_position():
    base = dict(side=0, price=100.0, reference_price=100.0,
                limits=dict(max_order_qty=1e9, max_order_notional=1e12,
                            collar_bps=1000, max_long_position=1000,
                            max_short_position=1000, contract_size=1.0))
    r = client.post("/api/connectivity/pretrade", json={**base, "qty": 100, "current_position": 900})
    assert r.json()["decision"] == "accept"          # -> exactly 1000
    r = client.post("/api/connectivity/pretrade", json={**base, "qty": 101, "current_position": 900})
    assert r.json()["decision"] == "position_limit"  # -> 1001


@needs_cpp
def test_pretrade_timing_is_labelled_as_boundary_cost():
    """The elapsed figure must not be readable as the gate's own cost."""
    r = client.post("/api/connectivity/pretrade",
                    json=dict(side=0, qty=1, price=100.0, reference_price=100.0))
    note = r.json()["elapsed_note"]
    assert "NOT the cost of the check itself" in note


# ------------------------------------------------------------------------- FIX
@needs_cpp
def test_fix_parse_reference_message():
    raw = ("8=FIX.4.2|9=65|35=A|49=SERVER|56=CLIENT|34=177|"
           "52=20090107-18:15:16|98=0|108=30|10=062|")
    r = client.get("/api/connectivity/fix/parse", params={"raw": raw})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["msg_type"] == "A"
    assert d["seq_num"] == 177
    assert d["fields"]["49"] == "SERVER"
    assert "|" in d["pretty"]


@needs_cpp
def test_fix_parse_detects_corruption_and_can_skip_validation():
    bad = ("8=FIX.4.2|9=65|35=A|49=XERVER|56=CLIENT|34=177|"
           "52=20090107-18:15:16|98=0|108=30|10=062|")
    r = client.get("/api/connectivity/fix/parse", params={"raw": bad})
    assert r.json()["ok"] is False
    assert r.json()["error"] == "checksum_mismatch"
    r = client.get("/api/connectivity/fix/parse", params={"raw": bad, "validate": False})
    assert r.json()["ok"] is True
    assert r.json()["fields"]["49"] == "XERVER"
