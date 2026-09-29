"""Execution schedules and the algo runner.

Two properties get the most attention because getting either wrong is silent:

  * A schedule must sum EXACTLY to the parent. Rounding each slice independently
    orphans quantity, and an algo waiting on a fill that can never come is a
    stuck parent nobody notices until end of day.
  * A parent with quantity left must NOT report COMPLETED. Reporting "done"
    having traded 75% is the failure the PARTIAL state exists to prevent.
"""
from __future__ import annotations

import pytest

from app.core.execution import (Algo, AlgoRunner, DEFAULT_VOLUME_PROFILE,
                                ParentState, ScheduleError, build,
                                pov_slice_qty, weights_for)


# ----------------------------------------------------------------- weights
def test_twap_weights_are_uniform():
    w = weights_for(Algo.TWAP, 5)
    assert w == [0.2] * 5
    assert sum(w) == pytest.approx(1.0)


@pytest.mark.parametrize("n", [1, 2, 5, 7, 13, 30, 100])
def test_weights_always_sum_to_one(n):
    for algo in (Algo.TWAP, Algo.VWAP, Algo.POV):
        assert sum(weights_for(algo, n)) == pytest.approx(1.0)


def test_vwap_follows_the_u_shape():
    """Heavy at the open and close, light at midday. If this flattens, the algo
    has stopped tracking its own benchmark."""
    w = weights_for(Algo.VWAP, 13)
    mid = len(w) // 2
    assert w[0] > w[mid] * 2      # open is at least twice midday
    assert w[-1] > w[mid] * 2     # so is the close
    assert w[0] == max(w[: mid + 1])


def test_vwap_shape_survives_resampling_to_any_slice_count():
    """Nearest-neighbour sampling would drop buckets entirely and the shape would
    be whatever the sampling happened to hit."""
    for n in (3, 5, 20, 50):
        w = weights_for(Algo.VWAP, n)
        mid = len(w) // 2
        assert w[0] > w[mid], f"shape lost at n={n}"
        assert w[-1] > w[mid], f"shape lost at n={n}"


def test_zero_slices_refused():
    with pytest.raises(ScheduleError):
        weights_for(Algo.TWAP, 0)


# ---------------------------------------------------------------- schedule
@pytest.mark.parametrize("algo", [Algo.TWAP, Algo.VWAP])
@pytest.mark.parametrize("total,slices", [
    (1000, 10), (1000, 7), (37, 5), (1, 1), (999_999, 13), (100.5, 4),
])
def test_schedule_sums_exactly_to_the_parent(algo, total, slices):
    s = build(algo, total, slices, 60)
    assert s.scheduled_qty == pytest.approx(total, abs=1e-9)


def test_slices_are_evenly_spaced_in_time_even_for_vwap():
    """VWAP varies SIZE by expected volume, not WHEN the buckets occur."""
    s = build(Algo.VWAP, 1000, 5, 100)
    offsets = [x.offset_s for x in s.slices]
    assert offsets == [0.0, 20.0, 40.0, 60.0, 80.0]


def test_venue_minimum_clamps_the_slice_count():
    """Asking for 100 slices of a 50-lot parent is a request to satisfy as
    closely as the venue permits, not an error to reject."""
    s = build(Algo.TWAP, 37, 10, 60, min_slice_qty=5)
    assert len(s) <= 7
    assert all(x.qty >= 5 for x in s.slices[:-1])
    assert s.scheduled_qty == pytest.approx(37)


def test_lot_rounding_is_respected_with_the_residual_on_the_last_slice():
    s = build(Algo.TWAP, 1000, 3, 60, round_to=100)
    assert all(x.qty % 100 == 0 for x in s.slices[:-1])
    assert s.scheduled_qty == pytest.approx(1000)


def test_single_slice_is_the_whole_parent():
    s = build(Algo.TWAP, 500, 1, 0)
    assert len(s) == 1
    assert s.slices[0].qty == 500


@pytest.mark.parametrize("total,slices,duration", [
    (0, 5, 60), (-10, 5, 60), (100, 0, 60), (100, 5, -1),
])
def test_invalid_schedules_are_refused(total, slices, duration):
    with pytest.raises(ScheduleError):
        build(Algo.TWAP, total, slices, duration)


def test_custom_profile_is_used():
    flat = (1.0, 1.0, 1.0, 1.0)
    s = build(Algo.VWAP, 400, 4, 40, profile=flat)
    assert [x.qty for x in s.slices] == [100, 100, 100, 100]


# --------------------------------------------------------------------- POV
def test_pov_sizes_from_observed_volume():
    assert pov_slice_qty(10_000, 0.1, remaining=5_000) == 1_000


def test_pov_never_overshoots_the_parent():
    """Participating at 10% of a print larger than the whole order would
    otherwise send more than was asked for."""
    assert pov_slice_qty(1_000_000, 0.1, remaining=250) == 250


def test_pov_waits_rather_than_sending_a_sub_minimum_child():
    assert pov_slice_qty(10, 0.1, remaining=5_000, min_slice_qty=100) == 0.0


def test_pov_sends_the_last_scrap_rather_than_stranding_it():
    # Below the minimum, but it is all that is left -- stranding it would leave
    # the parent permanently incomplete.
    assert pov_slice_qty(10_000, 0.5, remaining=3, min_slice_qty=100) == 3


def test_pov_with_no_volume_sends_nothing():
    assert pov_slice_qty(0, 0.1, remaining=1000) == 0.0


# ------------------------------------------------------------------ runner
class Venue:
    """Stub venue with a configurable fill ratio."""

    def __init__(self, fill_ratio=1.0, px=100.0):
        self.fill_ratio, self.px, self.sent = fill_ratio, px, []

    def __call__(self, symbol, side, qty, limit_px):
        self.sent.append((symbol, side, qty, limit_px))
        filled = qty * self.fill_ratio
        return (f"C{len(self.sent)}", filled, limit_px or self.px,
                "filled" if filled >= qty else "partially_filled")


def drive(runner, parent_id, clock, steps, step_s=10.0, **kw):
    for i in range(steps):
        clock[0] = i * step_s
        runner.step(parent_id, reference_px=kw.get("reference_px", 100.0),
                    observed_volume=kw.get("observed_volume", 0.0))


def test_full_fill_completes_the_parent():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=1000, algo=Algo.TWAP,
                 slices=5, duration_s=40, arrival_mid=100.0)
    drive(r, p.parent_id, clock, 5)
    p = r.get(p.parent_id)
    assert p.state is ParentState.COMPLETED
    assert p.filled_qty == pytest.approx(1000)
    assert p.leaves_qty == 0
    assert len(p.children) == 5


def test_partial_fills_leave_the_parent_PARTIAL_not_completed():
    """Reporting 'done' having traded half is the failure this state prevents."""
    v, clock = Venue(fill_ratio=0.5), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=1000, algo=Algo.TWAP,
                 slices=4, duration_s=30, arrival_mid=100.0)
    drive(r, p.parent_id, clock, 5)
    p = r.get(p.parent_id)
    assert p.state is ParentState.PARTIAL
    assert p.filled_qty == pytest.approx(500)
    assert p.leaves_qty == pytest.approx(500)
    assert "unfilled" in p.note


def test_slices_fire_only_when_due():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=100, algo=Algo.TWAP,
                 slices=5, duration_s=100)
    r.step(p.parent_id, reference_px=100.0)
    assert len(v.sent) == 1                  # only slice 0 is due at t=0
    clock[0] = 45.0
    r.step(p.parent_id, reference_px=100.0)
    assert len(v.sent) == 3                  # 20s and 40s slices both caught up


def test_a_delayed_tick_catches_up_rather_than_falling_behind():
    """A runner that sends at most one slice per call can never recover from a
    stall, and drifts further behind schedule with every tick."""
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=1000, algo=Algo.TWAP,
                 slices=10, duration_s=100)
    clock[0] = 100.0                          # one very late tick
    r.step(p.parent_id, reference_px=100.0)
    assert r.get(p.parent_id).state is ParentState.COMPLETED


def test_never_sends_more_than_the_parent():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=100, algo=Algo.TWAP,
                 slices=5, duration_s=40)
    drive(r, p.parent_id, clock, 6)
    assert sum(s[2] for s in v.sent) == pytest.approx(100)


# ------------------------------------------------------------------- TCA
def test_shortfall_is_positive_when_a_buyer_pays_above_arrival():
    v, clock = Venue(px=101.0), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=100, algo=Algo.TWAP,
                 slices=1, duration_s=0, arrival_mid=100.0)
    r.step(p.parent_id, reference_px=101.0)
    assert r.get(p.parent_id).shortfall_bps() == pytest.approx(100.0)


def test_shortfall_is_positive_when_a_seller_receives_below_arrival():
    """Signed so POSITIVE IS ALWAYS BAD: a metric that flips sign between sides
    cannot be aggregated across a book."""
    v, clock = Venue(px=99.0), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="2", total_qty=100, algo=Algo.TWAP,
                 slices=1, duration_s=0, arrival_mid=100.0)
    r.step(p.parent_id, reference_px=99.0)
    assert r.get(p.parent_id).shortfall_bps() == pytest.approx(100.0)


def test_shortfall_is_negative_when_execution_beats_arrival():
    v, clock = Venue(px=99.0), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=100, algo=Algo.TWAP,
                 slices=1, duration_s=0, arrival_mid=100.0)
    r.step(p.parent_id, reference_px=99.0)
    assert r.get(p.parent_id).shortfall_bps() == pytest.approx(-100.0)


def test_shortfall_is_none_without_fills_or_a_benchmark():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=100, algo=Algo.TWAP,
                 slices=1, duration_s=0, arrival_mid=None)
    assert p.shortfall_bps() is None


def test_avg_price_is_quantity_weighted_across_children():
    class Stepped:
        def __init__(self): self.n = 0
        def __call__(self, s, side, qty, px):
            self.n += 1
            return (f"C{self.n}", qty, 100.0 if self.n == 1 else 110.0, "filled")

    clock = [0.0]
    r = AlgoRunner(Stepped(), now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=300, algo=Algo.TWAP,
                 slices=2, duration_s=10, arrival_mid=100.0)
    drive(r, p.parent_id, clock, 3, step_s=10)
    # 150 @ 100 and 150 @ 110 -> 105, not the simple mean of the prices by luck
    assert r.get(p.parent_id).avg_px == pytest.approx(105.0)


# --------------------------------------------------------------- control
def test_pause_stops_firing_and_resume_continues():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=1000, algo=Algo.TWAP,
                 slices=5, duration_s=40)
    r.step(p.parent_id, reference_px=100.0)
    r.pause(p.parent_id)
    clock[0] = 40.0
    r.step(p.parent_id, reference_px=100.0)
    assert len(v.sent) == 1                  # nothing fired while paused
    r.resume(p.parent_id)
    r.step(p.parent_id, reference_px=100.0)
    assert len(v.sent) > 1


def test_cancel_leaves_the_remainder_explicitly_unfilled():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=1000, algo=Algo.TWAP,
                 slices=5, duration_s=40)
    r.step(p.parent_id, reference_px=100.0)
    r.cancel(p.parent_id)
    clock[0] = 40.0
    r.step(p.parent_id, reference_px=100.0)
    p = r.get(p.parent_id)
    assert p.state is ParentState.CANCELLED
    assert p.leaves_qty > 0
    assert "NOT filled" in p.note


def test_terminal_parents_do_not_fire_again():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=100, algo=Algo.TWAP,
                 slices=1, duration_s=0)
    r.step(p.parent_id, reference_px=100.0)
    before = len(v.sent)
    for _ in range(3):
        assert r.step(p.parent_id, reference_px=100.0)["fired"] == 0
    assert len(v.sent) == before


def test_unknown_parent_raises():
    r = AlgoRunner(Venue())
    with pytest.raises(KeyError):
        r.step("nope")
    with pytest.raises(KeyError):
        r.cancel("nope")


# ------------------------------------------------------------ limit price
def test_limit_offset_makes_a_buyer_more_aggressive():
    """A positive offset for a buyer means willing to pay MORE. That direction is
    easy to invert, so it is pinned."""
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="1", total_qty=100, algo=Algo.TWAP,
                 slices=1, duration_s=0, limit_offset_bps=10)
    r.step(p.parent_id, reference_px=100.0)
    assert v.sent[0][3] == pytest.approx(100.1)


def test_limit_offset_makes_a_seller_more_aggressive_downward():
    v, clock = Venue(), [0.0]
    r = AlgoRunner(v, now=lambda: clock[0])
    p = r.create(symbol="AAPL", side="2", total_qty=100, algo=Algo.TWAP,
                 slices=1, duration_s=0, limit_offset_bps=10)
    r.step(p.parent_id, reference_px=100.0)
    assert v.sent[0][3] == pytest.approx(99.9)


def test_clamped_slice_count_is_reported_not_silent():
    v = Venue()
    r = AlgoRunner(v)
    p = r.create(symbol="AAPL", side="1", total_qty=37, algo=Algo.TWAP,
                 slices=20, duration_s=60, min_slice_qty=5)
    assert len(p.schedule) < 20
    assert "minimum" in p.note
