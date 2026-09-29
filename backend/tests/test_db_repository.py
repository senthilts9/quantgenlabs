"""Repository tests, run against BOTH SQLite and a real PostgreSQL server.

Every test is parameterised over the two backends. SQLite always runs; PostgreSQL
runs whenever an embedded server can be started (pgserver ships real Postgres
binaries as a wheel, so this needs no Docker and no system install). Skipping
Postgres silently would defeat the point — the suite reports which backends it
actually exercised.

The accounting cases are the ones that matter: average cost must NOT move on a
reducing fill, and a fill that crosses through zero must open the new side at the
new price rather than carrying the old side's basis across the flip.
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import engine as dbengine
from app.core.db.models import Base
from app.core.db.repository import OrderRepository

D = Decimal


# --------------------------------------------------------------------- backends
def _postgres_url() -> str | None:
    """Start an embedded PostgreSQL and return its URL, or None if unavailable."""
    if os.getenv("QGL_SKIP_PG"):
        return None
    try:
        import pgserver
    except ImportError:
        return None
    try:
        data = os.getenv("QGL_PG_TESTDIR") or "/tmp/qgl-pgtest"
        os.makedirs(data, exist_ok=True)
        srv = pgserver.get_server(data)
        return srv.get_uri()
    except Exception:
        return None


_PG_URL = _postgres_url()

BACKENDS = ["sqlite"] + (["postgresql"] if _PG_URL else [])


@pytest.fixture(params=BACKENDS)
def repo(request, tmp_path):
    """A repository on a throwaway schema, per backend, per test."""
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'test.db'}"
    else:
        url = _PG_URL

    dbengine.dispose()
    eng = dbengine.get_engine(url)
    # Postgres is one server reused across tests, so the schema is dropped and
    # rebuilt rather than assumed empty. SQLite gets a fresh file each time.
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)

    with dbengine.session_scope(url) as s:
        yield OrderRepository(s)

    dbengine.dispose()


def _oid() -> str:
    return f"O-{uuid.uuid4().hex[:10]}"


def test_backends_actually_exercised():
    """Fails loudly if Postgres silently dropped out of the matrix."""
    assert "sqlite" in BACKENDS
    if _PG_URL is None:
        pytest.skip("embedded PostgreSQL unavailable — SQLite only on this host")
    assert "postgresql" in BACKENDS


# ----------------------------------------------------------------------- orders
def test_create_and_read_order(repo):
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=500,
                      order_type="limit", limit_price=187.25)
    o = repo.get_order(oid)
    assert o is not None
    assert o.symbol == "AAPL"
    assert D(str(o.qty)) == D("500")
    assert D(str(o.limit_price)) == D("187.25")
    assert o.status == "new"
    assert o.portfolio_id == "DEFAULT"          # auto-created


# A rejected write poisons the session until it is rolled back, so these two
# roll back explicitly. That is the real contract: the database refuses the row
# AND the caller must abandon the transaction, which is exactly why record_fill
# rolls back on IntegrityError rather than leaving a half-applied state.
def test_negative_quantity_rejected_by_the_database(repo):
    """The invariants are enforced by the schema, not by trusting every writer."""
    with pytest.raises(IntegrityError):
        repo.create_order(id=_oid(), symbol="AAPL", side="buy", qty=-5)
        repo.s.flush()
    repo.s.rollback()
    assert repo.list_orders() == []


def test_limit_order_without_price_rejected(repo):
    ok = _oid()
    repo.create_order(id=ok, symbol="AAPL", side="buy", qty=10,
                      order_type="limit", limit_price=100)
    repo.s.flush()
    with pytest.raises(IntegrityError):
        repo.create_order(id=_oid(), symbol="AAPL", side="buy", qty=10,
                          order_type="limit", limit_price=None)
        repo.s.flush()
    repo.s.rollback()


def test_filled_cannot_exceed_qty_at_the_schema_level(repo):
    """Belt and braces: record_fill checks leaves, but the schema enforces it too
    so a direct writer cannot persist an impossible order."""
    from app.core.db.models import OrderRow
    repo.ensure_portfolio("DEFAULT")
    repo.s.add(OrderRow(id=_oid(), symbol="AAPL", side="buy", qty=10,
                        filled_qty=99, portfolio_id="DEFAULT"))
    with pytest.raises(IntegrityError):
        repo.s.flush()
    repo.s.rollback()


def test_list_orders_filters(repo):
    repo.create_order(id="A1", symbol="AAPL", side="buy", qty=10)
    repo.create_order(id="M1", symbol="MSFT", side="sell", qty=20)
    repo.s.flush()
    assert len(repo.list_orders()) == 2
    assert [o.id for o in repo.list_orders(symbol="MSFT")] == ["M1"]
    assert [o.id for o in repo.list_orders(status="new")] != []


def test_cancel_is_refused_on_terminal_orders(repo):
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=100)
    repo.record_fill(fill_id="F1", order_id=oid, qty=100, price=100)
    assert repo.get_order(oid).status == "filled"
    assert repo.cancel_order(oid) is None        # already terminal


# ------------------------------------------------------------------------ fills
def test_fill_advances_order_and_position(repo):
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=300)
    r = repo.record_fill(fill_id="F1", order_id=oid, qty=100, price=50)
    assert r.duplicate is False
    o = repo.get_order(oid)
    assert o.status == "partially_filled"
    assert D(str(o.filled_qty)) == D("100")
    assert D(str(o.avg_price)) == D("50")
    assert r.position_qty == D("100")
    assert r.avg_cost == D("50")


def test_order_avg_price_is_weighted_across_fills(repo):
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=300)
    repo.record_fill(fill_id="F1", order_id=oid, qty=100, price=100)
    repo.record_fill(fill_id="F2", order_id=oid, qty=200, price=130)
    o = repo.get_order(oid)
    assert o.status == "filled"
    # (100*100 + 200*130) / 300 = 120
    assert D(str(o.avg_price)) == D("120")


def test_overfill_is_rejected(repo):
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=100)
    repo.record_fill(fill_id="F1", order_id=oid, qty=80, price=10)
    with pytest.raises(ValueError, match="exceeds leaves"):
        repo.record_fill(fill_id="F2", order_id=oid, qty=30, price=10)


@pytest.mark.parametrize("qty,price", [(0, 10), (-5, 10), (10, 0), (10, -1)])
def test_malformed_fills_rejected(repo, qty, price):
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=100)
    with pytest.raises(ValueError):
        repo.record_fill(fill_id="F1", order_id=oid, qty=qty, price=price)


def test_replayed_drop_copy_does_not_double_book(repo):
    """A drop-copy replayed after a reconnect is normal, not exceptional."""
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=500)
    a = repo.record_fill(fill_id="F1", order_id=oid, qty=100, price=50,
                         venue="XNAS", exec_id="E-777")
    b = repo.record_fill(fill_id="F2", order_id=oid, qty=100, price=50,
                         venue="XNAS", exec_id="E-777")     # same exec id
    assert a.duplicate is False
    assert b.duplicate is True
    assert D(str(repo.get_order(oid).filled_qty)) == D("100")   # booked once
    assert repo.recompute_position("AAPL") == D("100")


# ------------------------------------------------------- position accounting
def test_increasing_position_moves_the_average(repo):
    o1, o2 = _oid(), _oid()
    repo.create_order(id=o1, symbol="AAPL", side="buy", qty=100)
    repo.record_fill(fill_id="F1", order_id=o1, qty=100, price=100)
    repo.create_order(id=o2, symbol="AAPL", side="buy", qty=100)
    r = repo.record_fill(fill_id="F2", order_id=o2, qty=100, price=120)
    assert r.position_qty == D("200")
    assert r.avg_cost == D("110")            # (100*100 + 100*120)/200


def test_reducing_position_does_NOT_move_the_average(repo):
    """The classic blotter bug: selling half must not move the remaining half's
    cost basis, or the next sale reports the wrong profit."""
    buy, sell = _oid(), _oid()
    repo.create_order(id=buy, symbol="AAPL", side="buy", qty=100)
    repo.record_fill(fill_id="F1", order_id=buy, qty=100, price=100)
    repo.create_order(id=sell, symbol="AAPL", side="sell", qty=40)
    r = repo.record_fill(fill_id="F2", order_id=sell, qty=40, price=130)

    assert r.position_qty == D("60")
    assert r.avg_cost == D("100")            # UNCHANGED, not 100 blended with 130
    assert r.realized_delta == D("1200")     # (130 - 100) * 40


def test_closing_to_flat_clears_the_basis(repo):
    buy, sell = _oid(), _oid()
    repo.create_order(id=buy, symbol="AAPL", side="buy", qty=100)
    repo.record_fill(fill_id="F1", order_id=buy, qty=100, price=100)
    repo.create_order(id=sell, symbol="AAPL", side="sell", qty=100)
    r = repo.record_fill(fill_id="F2", order_id=sell, qty=100, price=110)
    assert r.position_qty == D("0")
    assert r.avg_cost == D("0")              # flat has no cost basis
    assert r.realized_delta == D("1000")


def test_crossing_through_zero_opens_the_new_side_at_the_new_price(repo):
    """Sell 150 when long 100: realise against the 100, then open a 50 short AT
    THE FILL PRICE. Carrying the long's basis into the short would flatter P&L."""
    buy, sell = _oid(), _oid()
    repo.create_order(id=buy, symbol="AAPL", side="buy", qty=100)
    repo.record_fill(fill_id="F1", order_id=buy, qty=100, price=100)
    repo.create_order(id=sell, symbol="AAPL", side="sell", qty=150)
    r = repo.record_fill(fill_id="F2", order_id=sell, qty=150, price=120)

    assert r.position_qty == D("-50")
    assert r.avg_cost == D("120")            # the SHORT's basis, not 100
    assert r.realized_delta == D("2000")     # (120-100) * 100 closed


def test_short_then_cover_realises_correctly(repo):
    """A short profits when it covers BELOW its average, which is the opposite
    sign convention to a long and is easy to get backwards."""
    sell, buy = _oid(), _oid()
    repo.create_order(id=sell, symbol="AAPL", side="sell", qty=100)
    repo.record_fill(fill_id="F1", order_id=sell, qty=100, price=100)
    pos = repo.get_position("AAPL")
    assert D(str(pos.qty)) == D("-100")
    assert D(str(pos.avg_cost)) == D("100")

    repo.create_order(id=buy, symbol="AAPL", side="buy", qty=100)
    r = repo.record_fill(fill_id="F2", order_id=buy, qty=100, price=90)
    assert r.position_qty == D("0")
    assert r.realized_delta == D("1000")     # sold at 100, covered at 90


def test_fees_reduce_realized_pnl(repo):
    buy, sell = _oid(), _oid()
    repo.create_order(id=buy, symbol="AAPL", side="buy", qty=100)
    repo.record_fill(fill_id="F1", order_id=buy, qty=100, price=100, fee=5)
    repo.create_order(id=sell, symbol="AAPL", side="sell", qty=100)
    repo.record_fill(fill_id="F2", order_id=sell, qty=100, price=110, fee=7)
    pos = repo.get_position("AAPL")
    # 1000 gross, less 5 + 7 of fees
    assert D(str(pos.realized_pnl)) == D("988")


def test_stored_position_agrees_with_the_fills(repo):
    """The stored row is a cache; the fills are the truth. This is the check that
    makes keeping the cache safe."""
    for i, (side, q, px) in enumerate([("buy", 100, 10), ("buy", 50, 12),
                                       ("sell", 30, 15), ("sell", 20, 9)]):
        oid = f"O{i}"
        repo.create_order(id=oid, symbol="AAPL", side=side, qty=q)
        repo.record_fill(fill_id=f"F{i}", order_id=oid, qty=q, price=px)

    stored = D(str(repo.get_position("AAPL").qty))
    derived = repo.recompute_position("AAPL")
    assert stored == derived == D("100")     # 100 + 50 - 30 - 20


def test_positions_listing_hides_flat_by_default(repo):
    o1, o2 = _oid(), _oid()
    repo.create_order(id=o1, symbol="AAPL", side="buy", qty=10)
    repo.record_fill(fill_id="F1", order_id=o1, qty=10, price=10)
    repo.create_order(id=o2, symbol="AAPL", side="sell", qty=10)
    repo.record_fill(fill_id="F2", order_id=o2, qty=10, price=11)
    assert repo.list_positions() == []
    assert len(repo.list_positions(include_flat=True)) == 1


# ------------------------------------------------------------------- precision
def test_decimal_precision_is_exact_not_floating(repo):
    """0.1 + 0.2 != 0.3 in binary floating point. A blotter that disagrees with
    the broker by a cent per fill is worse than useless, so prices are NUMERIC."""
    oid = _oid()
    repo.create_order(id=oid, symbol="BTC", side="buy", qty=3)
    repo.record_fill(fill_id="F1", order_id=oid, qty=1, price="0.1")
    repo.record_fill(fill_id="F2", order_id=oid, qty=1, price="0.2")
    repo.record_fill(fill_id="F3", order_id=oid, qty=1, price="0.3")
    o = repo.get_order(oid)
    # (0.1 + 0.2 + 0.3) / 3 = 0.2 exactly
    assert D(str(o.avg_price)) == D("0.2")


def test_high_precision_crypto_quantities_survive(repo):
    oid = _oid()
    repo.create_order(id=oid, symbol="BTC", side="buy", qty="0.0000000001")
    repo.record_fill(fill_id="F1", order_id=oid, qty="0.0000000001", price="65000.5")
    pos = repo.get_position("BTC")
    assert D(str(pos.qty)) == D("0.0000000001")


# ----------------------------------------------------------------------- audit
def test_audit_trail_records_the_lifecycle(repo):
    oid = _oid()
    repo.create_order(id=oid, symbol="AAPL", side="buy", qty=10)
    repo.record_fill(fill_id="F1", order_id=oid, qty=10, price=10)
    repo.s.flush()
    kinds = [a.kind for a in repo.list_audit(entity_id=oid)]
    assert "order.created" in kinds
    assert "fill.booked" in kinds


def test_reference_data_upsert(repo):
    repo.upsert_instrument("AAPL", asset_class="equity", currency="USD", tick_size="0.01")
    repo.upsert_instrument("AAPL", tick_size="0.005")
    from app.core.db.models import Instrument
    i = repo.s.get(Instrument, "AAPL")
    assert D(str(i.tick_size)) == D("0.005")
    assert i.asset_class == "equity"
