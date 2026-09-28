"""C++ / Python parity for the connectivity components that have both.

The point of a fallback is that a caller cannot tell which one is loaded. The only
way to make that claim credible is to run the SAME assertions against both, which
is what this file does: every test is parameterised over the two implementations
and skipped for C++ only if the extension is not built.

If these ever diverge, the fallback is lying and the API is returning numbers whose
meaning depends on a build flag.
"""
from __future__ import annotations

import math
import pytest

from app.core.connectivity import HAVE_CPP
from app.core.connectivity.fallback import PyLadder, PyFeedValidator

if HAVE_CPP:
    import mkt_connectivity as _mkt


def _ladders():
    out = [("python", lambda: PyLadder(1, 50000))]
    if HAVE_CPP:
        out.append(("cpp", lambda: _mkt.Ladder(1, 50000)))
    return out


def _validators():
    out = [("python", lambda **kw: PyFeedValidator(**kw))]
    if HAVE_CPP:
        def mk(**kw):
            cfg = _mkt.FeedConfig()
            for k, v in kw.items():
                setattr(cfg, "lambda_" if k == "lam" else k, v)
            return _mkt.FeedValidator(cfg)
        out.append(("cpp", mk))
    return out


LADDERS = _ladders()
VALIDATORS = _validators()
BID, ASK = 0, 1


# --------------------------------------------------------------------- ladder
@pytest.mark.parametrize("name,make", LADDERS)
def test_best_tracks_improvement(name, make):
    b = make()
    b.upsert(BID, 9990, 100)
    assert b.best(BID) == (9990, 100)
    b.upsert(BID, 9985, 200)                    # worse price
    assert b.best(BID) == (9990, 100)
    b.upsert(BID, 9995, 50)                     # better price
    assert b.best(BID) == (9995, 50)
    b.upsert(ASK, 10005, 75)
    assert b.best(ASK) == (10005, 75)
    assert b.spread_ticks() == 10


@pytest.mark.parametrize("name,make", LADDERS)
def test_best_repairs_when_top_clears(name, make):
    b = make()
    for px, q in [(9995, 50), (9990, 100), (9950, 400)]:
        b.upsert(BID, px, q)
    b.upsert(BID, 9995, 0)
    assert b.best(BID) == (9990, 100)
    b.upsert(BID, 9990, 0)                      # must skip the 40-tick gap
    assert b.best(BID) == (9950, 400)
    b.upsert(BID, 9950, 0)
    assert b.best(BID) is None
    assert b.levels(BID) == 0
    assert b.total_qty(BID) == 0


@pytest.mark.parametrize("name,make", LADDERS)
def test_rejects_out_of_range_without_corrupting(name, make):
    b = make()
    b.upsert(BID, 9990, 100)
    assert b.upsert(BID, 0, 100) is False       # below floor (floor is 1)
    assert b.upsert(ASK, 999999, 100) is False  # above ceiling
    assert b.upsert(BID, 9990, -5) is False     # negative qty
    assert b.rejects() == 3
    assert b.best(BID) == (9990, 100)           # untouched


@pytest.mark.parametrize("name,make", LADDERS)
def test_level_and_total_accounting(name, make):
    b = make()
    b.upsert(BID, 9990, 100)
    b.upsert(BID, 9980, 200)
    assert (b.levels(BID), b.total_qty(BID)) == (2, 300)
    b.upsert(BID, 9990, 150)                    # resize, not a new level
    assert (b.levels(BID), b.total_qty(BID)) == (2, 350)
    b.upsert(BID, 9980, 0)
    assert (b.levels(BID), b.total_qty(BID)) == (1, 150)


@pytest.mark.parametrize("name,make", LADDERS)
def test_add_is_incremental_and_clamps(name, make):
    b = make()
    b.add(ASK, 10010, 100)
    b.add(ASK, 10010, 50)
    assert b.qty_at(ASK, 10010) == 150
    b.add(ASK, 10010, -200)                     # over-decrement clamps at zero
    assert b.qty_at(ASK, 10010) == 0
    assert b.levels(ASK) == 0


@pytest.mark.parametrize("name,make", LADDERS)
def test_micro_price_and_imbalance(name, make):
    b = make()
    b.upsert(BID, 10000, 900)
    b.upsert(ASK, 10001, 100)
    assert b.imbalance() == pytest.approx(0.8)
    # Weighted by the OPPOSING size: heavy bid pulls fair value toward the ask.
    assert b.micro_price_ticks() == pytest.approx((10000 * 100 + 10001 * 900) / 1000)
    assert b.micro_price_ticks() > b.mid_ticks()


@pytest.mark.parametrize("name,make", LADDERS)
def test_crossed_book_detected(name, make):
    b = make()
    b.upsert(BID, 10000, 100)
    b.upsert(ASK, 10005, 100)
    assert b.is_crossed() is False
    b.upsert(ASK, 9995, 100)
    assert b.is_crossed() is True


@pytest.mark.parametrize("name,make", LADDERS)
def test_depth_is_ordered_and_bounded(name, make):
    b = make()
    for i in range(5):
        b.upsert(BID, 9990 - i * 5, 100 + i)
    d = b.depth(BID, 3)
    assert [lv.price for lv in d] == [9990, 9985, 9980]
    assert len(b.depth(BID, 8)) == 5            # only what exists
    b.upsert(ASK, 10020, 10)
    b.upsert(ASK, 10005, 20)
    assert [lv.price for lv in b.depth(ASK, 4)] == [10005, 10020]   # ascends


@pytest.mark.parametrize("name,make", LADDERS)
def test_empty_book_has_no_best(name, make):
    b = make()
    assert b.best(BID) is None                  # None, never a sentinel price
    assert b.best(ASK) is None
    assert b.mid_ticks() == 0.0
    assert b.is_crossed() is False


# ------------------------------------------------------------------ validator
@pytest.mark.parametrize("name,make", VALIDATORS)
def test_distinguishes_gap_duplicate_reorder(name, make):
    v = make()
    counts = v.check_batch([(1, 1_000_000_000, 100.0), (2, 2_000_000_000, 100.0)])
    assert counts["accepted"] == 2
    c = v.check_batch([
        (2, 3_000_000_000, 100.0),      # duplicate
        (1, 4_000_000_000, 100.0),      # reorder
        (9, 5_000_000_000, 100.0),      # gap
    ])
    assert c["duplicate"] == 1
    assert c["reordered"] == 1
    assert c["sequence_gap"] == 1
    assert v.counters()["gap_messages_lost"] == 6   # 3..8


@pytest.mark.parametrize("name,make", VALIDATORS)
def test_hard_price_band(name, make):
    v = make(price_min=50.0, price_max=150.0)
    assert v.check(1, 1_000_000_000, 1_000_000_000, 100.0, 1.0) in ("accepted", _accepted_enum())
    c = v.check_batch([(2, 2_000_000_000, 200.0), (3, 3_000_000_000, 10.0)])
    assert c["price_out_of_band"] == 2


@pytest.mark.parametrize("name,make", VALIDATORS)
def test_outlier_disarmed_until_warm(name, make):
    """A cold validator has no distribution, so a violent second tick must still
    be accepted -- rejecting it would mean rejecting the first real price."""
    v = make(min_samples=30, max_z=3.0)
    c = v.check_batch([(1, 1_000_000_000, 100.0), (2, 2_000_000_000, 180.0)])
    assert c["price_outlier"] == 0
    assert c["accepted"] == 2


@pytest.mark.parametrize("name,make", VALIDATORS)
def test_outlier_caught_once_warm(name, make):
    v = make(min_samples=30, max_z=8.0)
    ticks = [(i, i * 1_000_000_000, 100.0 * (1 + (1e-4 if i % 2 else -1e-4)))
             for i in range(1, 201)]
    v.check_batch(ticks)
    last = v.last_price()
    c = v.check_batch([(201, 300_000_000_000, last * 1.40)])
    assert c["price_outlier"] == 1


@pytest.mark.parametrize("name,make", VALIDATORS)
def test_flat_history_does_not_reject_everything(name, make):
    """Zero variance must not make every subsequent move infinitely many sigmas."""
    v = make(min_samples=10, max_z=3.0)
    c = v.check_batch([(i, i * 1_000_000_000, 100.0) for i in range(1, 51)])
    assert c["accepted"] == 50
    c2 = v.check_batch([(51, 51_000_000_000, 100.5)])
    assert c2["accepted"] == 1


@pytest.mark.parametrize("name,make", VALIDATORS)
def test_gap_sets_recovery_flag_and_resyncs(name, make):
    v = make()
    v.check_batch([(1, 1_000_000_000, 100.0)])
    v.check_batch([(50, 2_000_000_000, 100.0)])
    assert v.needs_recovery() is True
    c = v.check_batch([(51, 3_000_000_000, 100.0)])
    assert c["accepted"] == 1                   # resynchronised, not stuck
    v.recovered(51)
    assert v.needs_recovery() is False


@pytest.mark.parametrize("name,make", VALIDATORS)
def test_staleness_is_absence_of_ticks(name, make):
    v = make(stale_after_ns=10_000_000_000)
    assert v.is_stale(0) is True                # no price at all yet
    v.check(1, 100_000_000_000, 100_000_000_000, 100.0, 1.0)
    assert v.is_stale(105_000_000_000) is False
    assert v.is_stale(120_000_000_000) is True


@pytest.mark.parametrize("name,make", VALIDATORS)
def test_content_rejection_does_not_block_the_sequence(name, make):
    """The regression that bit the C++ version: a content rejection must not stop
    the sequence advancing, or one bad price makes every later message a gap."""
    v = make(price_min=50.0, price_max=150.0)
    c = v.check_batch([
        (1, 1_000_000_000, 100.0),      # accepted
        (2, 2_000_000_000, 900.0),      # rejected on content
        (3, 3_000_000_000, 100.0),      # must be ACCEPTED, not a gap
    ])
    assert c["accepted"] == 2
    assert c["price_out_of_band"] == 1
    assert c["sequence_gap"] == 0


def _accepted_enum():
    """The C++ binding returns a Verdict enum; the fallback returns a string."""
    return _mkt.Verdict.ACCEPTED if HAVE_CPP else "accepted"


# --------------------------------------------------------------- cross-check
@pytest.mark.skipif(not HAVE_CPP, reason="C++ extension not built")
def test_implementations_agree_on_a_random_book_walk():
    """Drive both with the same pseudo-random sequence and require identical
    top-of-book, depth and counters at every step."""
    import random
    rng = random.Random(20260928)
    py_b, cpp_b = PyLadder(1, 50000), _mkt.Ladder(1, 50000)
    for _ in range(4000):
        side = rng.choice([BID, ASK])
        px = rng.randint(9900, 10100)
        qty = rng.choice([0, 0, rng.randint(1, 1000)])
        assert py_b.upsert(side, px, qty) == cpp_b.upsert(side, px, qty)
        assert py_b.best(side) == cpp_b.best(side)
        assert py_b.total_qty(side) == cpp_b.total_qty(side)
        assert py_b.levels(side) == cpp_b.levels(side)
    assert py_b.spread_ticks() == cpp_b.spread_ticks()
    assert py_b.mid_ticks() == pytest.approx(cpp_b.mid_ticks())
    assert py_b.micro_price_ticks() == pytest.approx(cpp_b.micro_price_ticks())
    assert py_b.imbalance() == pytest.approx(cpp_b.imbalance())
    assert py_b.is_crossed() == cpp_b.is_crossed()
    assert [(l.price, l.qty) for l in py_b.depth(BID, 10)] == \
           [(l.price, l.qty) for l in cpp_b.depth(BID, 10)]


@pytest.mark.skipif(not HAVE_CPP, reason="C++ extension not built")
def test_validators_agree_on_a_random_tick_stream():
    import random
    rng = random.Random(1234)
    py_v = PyFeedValidator(price_min=50.0, price_max=200.0, min_samples=20)
    cfg = _mkt.FeedConfig()
    cfg.price_min, cfg.price_max, cfg.min_samples = 50.0, 200.0, 20
    cpp_v = _mkt.FeedValidator(cfg)

    ticks, seq = [], 0
    for _ in range(3000):
        r = rng.random()
        if r < 0.05:      seq += rng.randint(2, 5)      # gap
        elif r < 0.10:    pass                          # duplicate
        else:             seq += 1
        px = 300.0 if rng.random() < 0.02 else 100.0 * math.exp(rng.gauss(0, 0.0005))
        ticks.append((seq, (len(ticks) + 1) * 1_000_000_000, px))

    a = dict(py_v.check_batch(ticks))
    b = dict(cpp_v.check_batch(ticks))
    for k in set(a) | set(b):
        assert a.get(k, 0) == b.get(k, 0), f"{k}: python={a.get(k,0)} cpp={b.get(k,0)}"
    assert py_v.reject_rate() == pytest.approx(cpp_v.reject_rate())
    assert py_v.needs_recovery() == cpp_v.needs_recovery()
