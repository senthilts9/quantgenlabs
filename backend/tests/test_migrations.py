"""Migrations must apply AND roll back, on every backend they claim to support.

A migration that has only ever been run forward, on one engine, is a migration
you find out about during an incident. This applies it, checks the schema it
claims to create actually exists, then rolls it back and checks nothing is left
behind.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {"portfolios", "instruments", "orders", "fills", "positions", "audit_events"}


def _postgres_url() -> str | None:
    if os.getenv("QGL_SKIP_PG"):
        return None
    try:
        import pgserver
    except ImportError:
        return None
    try:
        d = os.getenv("QGL_PG_MIGDIR") or "/tmp/qgl-pgtest-mig"
        os.makedirs(d, exist_ok=True)
        return pgserver.get_server(d).get_uri()
    except Exception:
        return None


_PG = _postgres_url()


def _cfg(url: str) -> Config:
    os.environ["QGL_DATABASE_URL"] = url
    c = Config(str(BACKEND_DIR / "alembic.ini"))
    c.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return c


def _normalise(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1) if url.startswith("postgresql://") else url


def _round_trip(url: str) -> None:
    cfg = _cfg(url)
    command.upgrade(cfg, "head")
    eng = create_engine(_normalise(url))
    try:
        tables = set(inspect(eng).get_table_names())
        assert EXPECTED_TABLES <= tables, f"missing {EXPECTED_TABLES - tables}"
    finally:
        eng.dispose()

    command.downgrade(cfg, "base")
    eng = create_engine(_normalise(url))
    try:
        left = set(inspect(eng).get_table_names()) - {"alembic_version"}
        assert not left, f"downgrade left tables behind: {left}"
    finally:
        eng.dispose()


def test_migration_round_trip_sqlite():
    with tempfile.TemporaryDirectory() as td:
        _round_trip(f"sqlite:///{td}/m.db")


@pytest.mark.skipif(_PG is None, reason="embedded PostgreSQL unavailable")
def test_migration_round_trip_postgres():
    _round_trip(_PG)


@pytest.mark.skipif(_PG is None, reason="embedded PostgreSQL unavailable")
def test_check_constraints_actually_created():
    """The schema's invariants have to reach the database, not just the models.
    A CheckConstraint that autogenerate missed is a constraint that does not
    exist, and the application would never notice until bad data arrived."""
    cfg = _cfg(_PG)
    command.upgrade(cfg, "head")
    eng = create_engine(_normalise(_PG))
    try:
        names = {c["name"] for c in inspect(eng).get_check_constraints("orders")}
        for expected in ("ck_order_qty_positive", "ck_order_filled_le_qty",
                         "ck_order_side", "ck_order_status", "ck_order_limit_has_price"):
            assert expected in names, f"{expected} missing from {names}"
        fill_uqs = {u["name"] for u in inspect(eng).get_unique_constraints("fills")}
        assert "uq_fill_venue_exec" in fill_uqs   # idempotency guard must exist
    finally:
        eng.dispose()
        command.downgrade(cfg, "base")


def test_alembic_refuses_to_guess_a_database(monkeypatch):
    """Pointing a migration at the wrong database is not recoverable, so an unset
    URL must fail loudly rather than defaulting to something convenient."""
    monkeypatch.delenv("QGL_DATABASE_URL", raising=False)
    c = Config(str(BACKEND_DIR / "alembic.ini"))
    c.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    with pytest.raises(Exception, match="QGL_DATABASE_URL"):
        command.upgrade(c, "head")
