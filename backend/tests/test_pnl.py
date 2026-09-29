"""Real-time P&L projection.

The attribution tests are the important half, because an attribution that does
not add up is worse than none: it looks authoritative and is not. Each of the
three sanity cases below caught a real error while this was being written.
"""
from __future__ import annotations

import time

import pytest

from app.core.pnl import PnLProjection


@pytest.fixture
def p():
    return PnLProjection()


def baseline(p, qty=100, price=100.0, mark=110.0, symbol="X"):
    """Open a position, set a first mark, and consume the baseline interval."""
    p.apply_fill(symbol=symbol, side="buy", qty=qty, price=price, exec_id="E0")
    p.apply_mark(symbol, mark)
    p.attribute()
    return p


def only(attrs, symbol="X"):
    return next(a for a in attrs if a.symbol == symbol)


# ------------------------------------------------------------- position math
def test_reducing_does_not_move_the_average(p):
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0)
    p.apply_fill(symbol="X", side="sell", qty=40, price=130.0)
    pos = p.position("X")
    assert pos.net_qty == 60
    assert pos.avg_cost == 100.0          # unchanged, not blended with 130
    assert pos.realized == pytest.approx(1200.0)


def test_increasing_moves_the_average(p):
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0)
    p.apply_fill(symbol="X", side="buy", qty=100, price=120.0)
    assert p.position("X").avg_cost == pytest.approx(110.0)


def test_crossing_zero_opens_at_the_new_price(p):
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0)
    p.apply_fill(symbol="X", side="sell", qty=150, price=120.0)
    pos = p.position("X")
    assert pos.net_qty == -50
    assert pos.avg_cost == 120.0          # the SHORT's basis, not 100
    assert pos.realized == pytest.approx(2000.0)


def test_short_profits_when_covering_below(p):
    p.apply_fill(symbol="X", side="sell", qty=100, price=100.0)
    p.apply_fill(symbol="X", side="buy", qty=100, price=90.0)
    assert p.position("X").realized == pytest.approx(1000.0)
    assert p.position("X").net_qty == 0


def test_redelivered_fill_is_not_applied_twice(p):
    """The bus is at-least-once. A fill applied twice doubles the position and
    nothing downstream notices until someone reconciles against the broker."""
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0, exec_id="E1")
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0, exec_id="E1")
    assert p.position("X").net_qty == 100
    assert p.duplicates_ignored == 1


# ------------------------------------------------------------ unrealised
def test_unrealised_is_none_without_a_mark(p):
    """A flat number and an unknown number are different things; returning 0.0
    would let a caller sum the unknown into a total."""
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0)
    assert p.position("X").unrealized() is None
    p.apply_mark("X", 105.0)
    assert p.position("X").unrealized() == pytest.approx(500.0)


def test_flat_position_is_zero_not_unknown(p):
    p.apply_fill(symbol="X", side="buy", qty=10, price=100.0)
    p.apply_fill(symbol="X", side="sell", qty=10, price=100.0)
    assert p.position("X").unrealized() == 0.0


def test_stale_marks_are_excluded_from_totals_and_reported(p):
    """An unrealised total that silently includes unvalued positions is worse
    than one that says it is partial."""
    proj = PnLProjection(stale_after_s=1.0)
    proj.apply_fill(symbol="X", side="buy", qty=100, price=100.0)
    proj.apply_mark("X", 110.0, ts=time.time() - 60)     # a minute old
    t = proj.totals()
    assert t["positions_unvalued"] == 1
    assert t["fully_valued"] is False
    assert "stale" in t["note"]
    assert t["unrealized"] == 0.0                        # excluded, not guessed


def test_fresh_marks_are_valued(p):
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0)
    p.apply_mark("X", 110.0)
    t = p.totals()
    assert t["fully_valued"] is True
    assert t["unrealized"] == pytest.approx(1000.0)
    assert t["total"] == pytest.approx(1000.0)


# ----------------------------------------------------------- attribution
def test_first_interval_is_a_baseline_not_an_unexplained_residual(p):
    """With no PRIOR mark there is no price move to attribute — you cannot
    measure a move without knowing where it started."""
    p.apply_fill(symbol="X", side="buy", qty=100, price=100.0)
    p.apply_mark("X", 110.0)
    a = only(p.attribute())
    assert a.baseline is True
    assert a.explained is True


def test_pure_market_move_is_entirely_price_effect(p):
    baseline(p)
    p.apply_mark("X", 120.0)
    a = only(p.attribute())
    assert a.price_effect == pytest.approx(1000.0)     # 100 held * +10
    assert a.trading_effect == pytest.approx(0.0)
    assert a.total_change == pytest.approx(1000.0)


def test_selling_AT_the_mark_attributes_to_zero(p):
    """Sanity check 1. You realise precisely what you were already carrying, so
    nothing changed. Counting the realised gain without subtracting the
    unrealised it came from double-counts it."""
    baseline(p)
    p.apply_fill(symbol="X", side="sell", qty=40, price=110.0, exec_id="E1")
    a = only(p.attribute())
    assert a.total_change == pytest.approx(0.0)
    assert a.trading_effect == pytest.approx(0.0)


def test_buying_AT_the_mark_attributes_to_zero(p):
    """Sanity check 2. This additionally requires valuing the previous period
    with the average IN EFFECT THEN — using the new blended average makes a
    neutral purchase look like a gain."""
    baseline(p)
    p.apply_fill(symbol="X", side="buy", qty=50, price=110.0, exec_id="E2")
    a = only(p.attribute())
    assert a.total_change == pytest.approx(0.0)
    assert a.trading_effect == pytest.approx(0.0)


def test_trading_better_than_the_mark_is_a_trading_gain(p):
    baseline(p)
    p.apply_fill(symbol="X", side="sell", qty=40, price=115.0, exec_id="E3")
    a = only(p.attribute())
    assert a.trading_effect == pytest.approx(200.0)    # 5 better * 40
    assert a.price_effect == pytest.approx(0.0)


def test_buying_below_the_mark_is_a_trading_gain(p):
    baseline(p)
    p.apply_fill(symbol="X", side="buy", qty=50, price=105.0, exec_id="E4")
    a = only(p.attribute())
    assert a.trading_effect == pytest.approx(250.0)    # 5 cheaper * 50


def test_fees_are_always_negative_and_reduce_the_total(p):
    baseline(p)
    p.apply_mark("X", 115.0)
    p.apply_fill(symbol="X", side="sell", qty=40, price=115.0, fee=5.0, exec_id="E5")
    a = only(p.attribute())
    assert a.fee_effect == pytest.approx(-5.0)
    assert a.total_change == pytest.approx(495.0)      # 500 price, -5 fee


@pytest.mark.parametrize("action", [
    lambda p: p.apply_mark("X", 120.0),
    lambda p: p.apply_fill(symbol="X", side="sell", qty=40, price=115.0, exec_id="A"),
    lambda p: p.apply_fill(symbol="X", side="buy", qty=50, price=105.0, exec_id="B"),
    lambda p: (p.apply_mark("X", 118.0),
               p.apply_fill(symbol="X", side="sell", qty=30, price=119.0, fee=2.0, exec_id="C")),
    lambda p: (p.apply_mark("X", 95.0),
               p.apply_fill(symbol="X", side="sell", qty=150, price=94.0, exec_id="D")),
])
def test_components_always_sum_to_the_change(p, action):
    """The property that makes attribution worth showing at all."""
    baseline(p)
    action(p)
    a = only(p.attribute())
    assert a.price_effect + a.trading_effect + a.fee_effect == pytest.approx(a.total_change)
    assert a.explained is True
    assert abs(a.residual) < 1e-9


# --------------------------------------------------------- reconciliation
def test_reconcile_reports_each_break_not_a_boolean(p):
    """"They differ" is not actionable; "AAPL is 100 here and 150 there" is."""
    p.apply_fill(symbol="AAPL", side="buy", qty=100, price=10.0)
    p.apply_fill(symbol="MSFT", side="buy", qty=50, price=20.0)
    r = p.reconcile({("DEFAULT", "AAPL"): 150.0, ("DEFAULT", "MSFT"): 50.0})
    assert r["clean"] is False
    assert len(r["breaks"]) == 1
    b = r["breaks"][0]
    assert b["symbol"] == "AAPL"
    assert b["projection"] == 100.0 and b["authoritative"] == 150.0
    assert b["difference"] == -50.0
    assert "STORE is authoritative" in r["note"]


def test_reconcile_clean_when_they_agree(p):
    p.apply_fill(symbol="AAPL", side="buy", qty=100, price=10.0)
    r = p.reconcile({("DEFAULT", "AAPL"): 100.0})
    assert r["clean"] is True and r["breaks"] == []


def test_reconcile_catches_a_position_missing_entirely(p):
    p.apply_fill(symbol="AAPL", side="buy", qty=100, price=10.0)
    r = p.reconcile({("DEFAULT", "AAPL"): 100.0, ("DEFAULT", "GOOG"): 25.0})
    assert [b["symbol"] for b in r["breaks"]] == ["GOOG"]


# --------------------------------------------------------------- rebuild
def test_rebuild_from_the_log_reproduces_the_running_total(p):
    """The property that makes the number defensible: if a rebuild disagrees
    with the running total, the running total was wrong."""
    events = []
    for i, (side, qty, px) in enumerate([("buy", 100, 10.0), ("buy", 50, 12.0),
                                         ("sell", 30, 15.0), ("sell", 20, 9.0)]):
        events.append({"event": "trade.executed", "symbol": "AAPL", "side": side,
                       "qty": qty, "price": px, "exec_id": f"E{i}"})
        p.apply_fill(symbol="AAPL", side=side, qty=qty, price=px, exec_id=f"E{i}")
    events.append({"event": "market.tick", "symbol": "AAPL", "price": 14.0})
    p.apply_mark("AAPL", 14.0)

    running = p.totals()

    fresh = PnLProjection()
    assert fresh.rebuild_from(events) == 5
    rebuilt = fresh.totals()

    assert rebuilt["realized"] == pytest.approx(running["realized"])
    assert rebuilt["unrealized"] == pytest.approx(running["unrealized"])
    assert fresh.position("AAPL").net_qty == p.position("AAPL").net_qty


def test_rebuild_discards_prior_state(p):
    p.apply_fill(symbol="OLD", side="buy", qty=99, price=1.0)
    p.rebuild_from([{"event": "trade.executed", "symbol": "NEW", "side": "buy",
                     "qty": 1, "price": 1.0, "exec_id": "N1"}])
    assert p.position("OLD") is None
    assert p.position("NEW").net_qty == 1


def test_unknown_event_types_are_ignored_not_fatal(p):
    """A projection that dies on an event it has not been taught about takes the
    whole consumer group down with it."""
    p.apply_event({"event": "something.new", "whatever": 1})
    p.apply_event({})
    assert p.stats()["trades_applied"] == 0
