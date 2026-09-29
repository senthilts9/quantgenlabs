"""Relational schema for the transactional store.

This is the PostgreSQL side of the OMS. The domain dataclasses in core/oms/models
stay free of persistence concerns; these are the tables they map onto.

SCOPE — WHAT USES THIS AND WHAT DOES NOT.

  * The OMS / transactional store (orders, fills, positions, portfolios, audit)
    can use PostgreSQL, selected by QGL_DATABASE_URL.
  * THE CRYPTO DESK STAYS ON SQLITE and is not touched by any of this. Its store
    is self-contained, its workload is single-node, and migrating a working,
    tested module to gain nothing it needs would be churn with a regression risk
    attached.
  * SQLite remains the DEFAULT for the OMS too. With QGL_DATABASE_URL unset
    nothing here is on the path at all, so a clone still runs with no database
    to configure.

What PostgreSQL buys the OMS specifically, and SQLite cannot: concurrent writers
without a global write lock, server-side constraint enforcement shared across
connections, and room for a fills table that grows without bound.

DESIGN CHOICES WORTH KNOWING:

  * NUMERIC, NEVER FLOAT, for quantities and prices. A float price is a rounding
    error waiting to be reconciled: 0.1 + 0.2 != 0.3, and a blotter that disagrees
    with the broker by a cent per fill is worse than useless. Postgres NUMERIC is
    exact decimal arithmetic. The C++ hot path uses integer ticks for the same
    reason; this is that discipline carried into storage.

  * TIMESTAMPTZ, never naive timestamps. A trading system spans venues in
    different zones; a timestamp without one is ambiguous exactly when it matters
    (the overlap around a DST change).

  * Money amounts carry their currency. A `notional` column with no currency
    beside it is how a USD book and a EUR book get summed.

  * Fills are append-only and reference their order; there is no UPDATE path for
    a fill. An execution that happened cannot un-happen, and correcting one is a
    new booking, not an edit. That is what makes the audit trail reconstructable.

  * Positions are DERIVED and stored, with the derivation available as a query.
    Storing them is a cache for the blotter; the fills remain the truth, so a
    disagreement between the two is detectable rather than invisible.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Quantities and prices. 28 digits is generous enough for a crypto quantity with
# 18 decimals and an equity notional in the billions, in the same type.
Qty = Numeric(28, 10)
Px = Numeric(28, 10)
Money = Numeric(28, 6)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_ccy: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    orders: Mapped[list["OrderRow"]] = relationship(back_populates="portfolio")

    __table_args__ = (
        CheckConstraint("length(base_ccy) = 3", name="ck_portfolio_ccy_len"),
    )


class Instrument(Base):
    """Security master, minimal but present: an order referencing an unknown
    symbol is a data-quality failure that should be catchable at write time."""
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(24), nullable=False, default="equity")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    tick_size: Mapped[float] = mapped_column(Px, nullable=False, default=0.01)
    lot_size: Mapped[float] = mapped_column(Qty, nullable=False, default=1)
    contract_size: Mapped[float] = mapped_column(Qty, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("tick_size > 0", name="ck_instrument_tick_positive"),
        CheckConstraint("lot_size > 0", name="ck_instrument_lot_positive"),
    )


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("portfolios.id", ondelete="RESTRICT"),
        nullable=False, default="DEFAULT")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    qty: Mapped[float] = mapped_column(Qty, nullable=False)
    order_type: Mapped[str] = mapped_column(String(12), nullable=False, default="market")
    limit_price: Mapped[float | None] = mapped_column(Px)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    filled_qty: Mapped[float] = mapped_column(Qty, nullable=False, default=0)
    avg_price: Mapped[float] = mapped_column(Px, nullable=False, default=0)
    parent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("orders.id"))
    venue: Mapped[str | None] = mapped_column(String(32))
    algo: Mapped[str | None] = mapped_column(String(32))
    arrival_mid: Mapped[float | None] = mapped_column(Px)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="orders")
    fills: Mapped[list["FillRow"]] = relationship(back_populates="order")

    __table_args__ = (
        # The database enforces the invariants rather than trusting every writer:
        # an application bug should be rejected at the boundary, not persisted.
        CheckConstraint("qty > 0", name="ck_order_qty_positive"),
        CheckConstraint("filled_qty >= 0", name="ck_order_filled_nonneg"),
        CheckConstraint("filled_qty <= qty", name="ck_order_filled_le_qty"),
        CheckConstraint("side in ('buy','sell')", name="ck_order_side"),
        CheckConstraint("status in ('new','partially_filled','filled','cancelled','rejected')",
                        name="ck_order_status"),
        # A limit order without a price is not an order.
        CheckConstraint("order_type <> 'limit' OR limit_price IS NOT NULL",
                        name="ck_order_limit_has_price"),
        Index("ix_orders_symbol_created", "symbol", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_portfolio", "portfolio_id"),
        Index("ix_orders_parent", "parent_id"),
    )


class FillRow(Base):
    """Append-only. There is no update path: an execution that happened cannot
    un-happen, and a correction is a new booking rather than an edit."""
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    qty: Mapped[float] = mapped_column(Qty, nullable=False)
    price: Mapped[float] = mapped_column(Px, nullable=False)
    fee: Mapped[float] = mapped_column(Money, nullable=False, default=0)
    fee_ccy: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    venue: Mapped[str | None] = mapped_column(String(32))
    exec_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    order: Mapped[OrderRow] = relationship(back_populates="fills")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_fill_qty_positive"),
        CheckConstraint("price > 0", name="ck_fill_price_positive"),
        CheckConstraint("side in ('buy','sell')", name="ck_fill_side"),
        # A venue's exec id is unique per venue; this is what makes replaying a
        # drop-copy idempotent instead of double-booking every fill.
        UniqueConstraint("venue", "exec_id", name="uq_fill_venue_exec"),
        Index("ix_fills_order", "order_id"),
        Index("ix_fills_symbol_created", "symbol", "created_at"),
    )


class PositionRow(Base):
    """Derived from fills and stored for blotter reads. The fills remain the
    truth, so a disagreement is detectable -- see repository.recompute_position."""
    __tablename__ = "positions"

    portfolio_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    qty: Mapped[float] = mapped_column(Qty, nullable=False, default=0)       # signed
    avg_cost: Mapped[float] = mapped_column(Px, nullable=False, default=0)
    realized_pnl: Mapped[float] = mapped_column(Money, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("avg_cost >= 0", name="ck_position_avg_cost_nonneg"),
        Index("ix_positions_symbol", "symbol"),
    )


class AuditEvent(Base):
    """Immutable event log. Append-only by construction: no update path exists on
    this table, which is what "full audit trail" has to mean to be worth having."""
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_audit_kind_created", "kind", "created_at"),
        Index("ix_audit_entity", "entity_id"),
    )
