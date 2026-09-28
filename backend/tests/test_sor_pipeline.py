"""SOR training pipeline — capture, quote fade, and the model registry.

These tests guard the properties that make the pipeline trustworthy rather
than merely runnable. The three that matter most:

  * the features LOGGED are byte-for-byte the features PREDICTED on
    (train/serve skew is silent and it flatters offline metrics);
  * the fade simulator produces both classes (a label that is always 1 makes
    every downstream metric meaningless);
  * exactly one champion exists at a time (two would mean routing decisions
    split between models with no record of which did what).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import sor                                  # noqa: E402
from app.core.oms.store import OMSStore                   # noqa: E402


@pytest.fixture
def store():
    return OMSStore(":memory:")


def _simulate_routes(store, model, n=200, seed=3):
    """Drive n routes through decide -> fade -> capture and persist them."""
    rng = np.random.default_rng(seed)
    for i in range(n):
        venues, mid = sor.synth_market(sor.Side.BUY, mid=100.0,
                                       seed=int(rng.integers(1, 10 ** 6)))
        qty = float(rng.uniform(500, 9000))
        vol = float(rng.uniform(0.2, 1.8))
        fp = sor.fill_probabilities(model, venues, qty, volatility=vol)
        res = sor.route(qty, venues, sor.Side.BUY, mid, fill_prob=fp)
        realised = sor.fade.simulate_fills(res.allocation, venues, volatility=vol,
                                           seed=int(rng.integers(1, 10 ** 6)))
        obs = sor.capture.observe_route(
            route_id=f"r{i}", side="buy", symbol="TEST", venues=venues,
            allocation=res.allocation,
            fills={k: v.filled for k, v in realised.items()},
            order_qty=qty, volatility=vol, data_source="synthetic",
            outcome_source="fade-sim", predicted=fp, model_version=model.version)
        store.record_route_observations([o.to_row() for o in obs])


# ---- feature capture ----------------------------------------------------
def test_capture_features_match_model_features():
    """One feature vocabulary. If these drift, every logged row silently
    becomes untrainable against the served model."""
    assert sor.capture.FEATURES == sor.ml.FEATURES


def test_logged_features_reproduce_the_served_prediction():
    """The anti-skew property: re-running the model on what was LOGGED must
    return exactly what was SERVED at decision time."""
    model = sor.train_fill_model()
    venues, mid = sor.synth_market(sor.Side.BUY, mid=100.0, seed=7)
    qty, vol = 4000.0, 0.9

    served = sor.fill_probabilities(model, venues, qty, volatility=vol)
    res = sor.route(qty, venues, sor.Side.BUY, mid, fill_prob=served)
    realised = sor.fade.simulate_fills(res.allocation, venues, volatility=vol, seed=1)
    obs = sor.capture.observe_route(
        route_id="r", side="buy", symbol="T", venues=venues,
        allocation=res.allocation,
        fills={k: v.filled for k, v in realised.items()},
        order_qty=qty, volatility=vol, data_source="synthetic",
        outcome_source="fade-sim", predicted=served)

    assert obs, "a filled route must produce observations"
    for o in obs:
        assert model.predict(o.features()) == pytest.approx(served[o.venue])


def test_unallocated_venues_are_not_logged():
    """A venue the router skipped has no outcome. Logging it as a zero would
    teach the model a fact about the router, not about the venue."""
    venues, mid = sor.synth_market(sor.Side.BUY, mid=100.0, seed=7)
    alloc = {venues[0].venue: 100.0}          # only one venue used
    obs = sor.capture.observe_route(
        route_id="r", side="buy", symbol="T", venues=venues, allocation=alloc,
        fills={venues[0].venue: 100.0}, order_qty=100.0, volatility=0.8,
        data_source="synthetic", outcome_source="fade-sim")
    assert [o.venue for o in obs] == [venues[0].venue]


@pytest.mark.parametrize("filled,expected", [(100.0, 1), (99.5, 1), (98.0, 0), (0.0, 0)])
def test_label_threshold(filled, expected):
    o = sor.capture.RouteObservation(
        route_id="r", venue="V", side="buy", symbol="T", data_source="synthetic",
        outcome_source="fade-sim", model_version=None, predicted_p=None,
        size_ratio=0.1, latency_ms=5, spread_bps=10, volatility=0.8,
        requested_qty=100.0, filled_qty=filled)
    assert o.label_filled == expected


# ---- quote fade ---------------------------------------------------------
@pytest.mark.parametrize("lat_lo,lat_hi", [(1, 50), (10, 200)])
def test_fade_decreases_with_latency(lat_lo, lat_hi):
    lo = sor.fade.reachable_fraction(latency_ms=lat_lo, volatility=0.8, depth_ratio=0.2)
    hi = sor.fade.reachable_fraction(latency_ms=lat_hi, volatility=0.8, depth_ratio=0.2)
    assert hi < lo


def test_fade_decreases_with_depth_consumption():
    small = sor.fade.reachable_fraction(latency_ms=5, volatility=0.8, depth_ratio=0.05)
    large = sor.fade.reachable_fraction(latency_ms=5, volatility=0.8, depth_ratio=1.5)
    assert large < small


@pytest.mark.parametrize("lat", [0, 5, 100, 5000])
@pytest.mark.parametrize("ratio", [0.0, 0.5, 10.0])
def test_fade_is_a_valid_fraction(lat, ratio):
    f = sor.fade.reachable_fraction(latency_ms=lat, volatility=1.0, depth_ratio=ratio)
    assert 0.0 <= f <= 1.0


def test_fade_never_overfills():
    venues, _ = sor.synth_market(sor.Side.BUY, mid=100.0, seed=7)
    alloc = {v.venue: 500.0 for v in venues}
    for vf in sor.fade.simulate_fills(alloc, venues, seed=5).values():
        assert 0.0 <= vf.filled <= vf.requested


def test_fade_produces_both_classes(store):
    """The reason this module exists. Without fade every label is 1 and the
    captured set cannot train anything."""
    model, _ = sor.registry.get_or_bootstrap(store)
    _simulate_routes(store, model, n=150)
    rows = store.list_route_observations(20000)
    labels = {r["label_filled"] for r in rows}
    assert labels == {0, 1}, f"degenerate labels: {labels}"


# ---- registry -----------------------------------------------------------
def test_bootstrap_registers_synthetic_champion(store):
    model, metrics = sor.registry.get_or_bootstrap(store)
    assert model.version == 1
    assert model.sample_source == sor.ml.SYNTHETIC_SOURCE
    assert store.champion_model()["version"] == 1
    # A second call must not mint a second model.
    again, _ = sor.registry.get_or_bootstrap(store)
    assert again.version == 1
    assert len(store.list_models()) == 1


def test_model_round_trips_through_the_registry(store):
    model, metrics = sor.registry.get_or_bootstrap(store)
    loaded, loaded_metrics = sor.registry.load(store)
    feats = {"size_ratio": 0.4, "latency_ms": 8.0, "spread_bps": 12.0,
             "volatility": 0.9}
    assert loaded.predict(feats) == pytest.approx(model.predict(feats), abs=1e-12)
    assert loaded_metrics["auc"] == metrics["auc"]


def test_promotion_leaves_exactly_one_champion(store):
    sor.registry.get_or_bootstrap(store)
    _simulate_routes(store, sor.registry.load(store)[0], n=200)
    rows = store.list_route_observations(20000)
    challenger = sor.train_from_observations(rows)
    v = sor.registry.save(store, challenger, sor.training_metrics(challenger),
                          n_train=len(rows), n_test=0)
    assert store.get_model(v)["status"] == "challenger"

    store.promote_model(v)
    champions = [m for m in store.list_models() if m["status"] == "champion"]
    assert [c["version"] for c in champions] == [v]
    assert store.get_model(1)["status"] == "archived"


def test_feature_order_mismatch_is_rejected():
    """Deserialising weights against a reordered feature list would map
    coefficients to the wrong inputs and still return plausible probabilities
    — worse than failing."""
    model = sor.train_fill_model()
    params = model.to_params()
    params["features"] = list(reversed(params["features"]))
    with pytest.raises(ValueError, match="feature set"):
        sor.FillModel.from_params(params)


def test_reset_preserves_training_data_and_lineage(store):
    """Clearing the demo blotter must not discard accumulated training rows or
    the model history that references them."""
    model, _ = sor.registry.get_or_bootstrap(store)
    _simulate_routes(store, model, n=20)
    before = store.route_observation_stats()["total"]
    assert before > 0
    store.reset()
    assert store.route_observation_stats()["total"] == before
    assert store.champion_model() is not None


# ---- training guards ----------------------------------------------------
def test_training_refuses_too_few_rows():
    with pytest.raises(ValueError, match="labelled rows"):
        sor.train_from_observations([])


def test_training_refuses_a_single_class():
    """Every-venue-filled fits an intercept that just encodes the base rate,
    and reports flawless accuracy while knowing nothing."""
    rows = [{"size_ratio": 0.1 * i, "latency_ms": 5.0, "spread_bps": 10.0,
             "volatility": 0.8, "label_filled": 1,
             "outcome_source": "fade-sim", "data_source": "synthetic"}
            for i in range(sor.MIN_TRAINING_ROWS + 10)]
    with pytest.raises(ValueError, match="single class"):
        sor.train_from_observations(rows)


def test_captured_model_beats_the_base_rate(store):
    """Brier skill > 0 on held-out rows. This says the plumbing carries
    signal end-to-end; it says nothing about real fills, because the labels
    come from the fade simulator."""
    model, _ = sor.registry.get_or_bootstrap(store)
    _simulate_routes(store, model, n=300)
    rows = store.list_route_observations(20000)
    trained = sor.train_from_observations(rows)
    m = sor.training_metrics(trained)
    assert m["brier_skill_score"] > 0
    assert "fade-sim" in m["sample_source"]


def test_metrics_refuse_a_registry_model_without_its_holdout(store):
    """A deserialised captured-data model has no eval set. Scoring it against
    the synthetic generator would report a number describing neither."""
    sor.registry.get_or_bootstrap(store)
    _simulate_routes(store, sor.registry.load(store)[0], n=200)
    rows = store.list_route_observations(20000)
    trained = sor.train_from_observations(rows)
    v = sor.registry.save(store, trained, sor.training_metrics(trained),
                          n_train=len(rows), n_test=0)
    loaded, _ = sor.registry.load(store, v)
    with pytest.raises(ValueError, match="held-out"):
        sor.training_metrics(loaded)


# ---- persistence --------------------------------------------------------
def test_observations_persist_with_provenance(store):
    model, _ = sor.registry.get_or_bootstrap(store)
    _simulate_routes(store, model, n=30)
    stats = store.route_observation_stats()
    assert stats["total"] > 0
    assert set(stats["by_outcome_source"]) == {"fade-sim"}
    for r in store.list_route_observations(10):
        assert r["outcome_source"] == "fade-sim"
        assert r["model_version"] == model.version
        assert 0.0 <= r["fill_rate"] <= 1.0
