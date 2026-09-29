"""Order/fill/position repository.

The P&L accounting is the part worth reading carefully, because it is where
blotters usually go wrong.

AVERAGE COST AND REALISED P&L. A fill that INCREASES a position changes the
average cost; a fill that REDUCES one does not — it realises P&L against the
existing average and leaves that average alone. Recomputing the average on a
reducing fill is the classic bug: sell half your position and the remaining
half's cost basis silently moves, so the next sale reports the wrong profit.

A fill that CROSSES through zero (sell 150 when long 100) is two events, and is
handled as such: realise against the 100 held, then open a 50 short at the fill
price. Treating it as one event leaves the new short carrying the old long's cost
basis, which is not merely wrong but wrong in the direction that flatters the P&L.

IDEMPOTENCY. record_fill is keyed on (venue, exec_id) at the database level. A
drop-copy replayed after a reconnect — which is normal, not exceptional — must not
double-book. The unique constraint means the second attempt is rejected by the
database rather than by a check that races.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AuditEvent, FillRow, Instrument, OrderRow, Portfolio, PositionRow

D = Decimal


def _d(x) -> Decimal:
    """Everything monetary crosses into Decimal at the boundary. Mixing float and
    Decimal in one expression raises, which is a feature: it surfaces the place a
    float leaked in rather than silently degrading precision."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


@dataclass(frozen=True)
class FillResult:
    fill_id: str
    duplicate: bool           # True when (venue, exec_id) was already booked
    position_qty: Decimal
    avg_cost: Decimal
    realized_delta: Decimal


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    # ---------------------------------------------------------------- reference
    def ensure_portfolio(self, pid: str = "DEFAULT", name: str | None = None,
                         base_ccy: str = "USD") -> Portfolio:
        p = self.s.get(Portfolio, pid)
        if p is None:
            p = Portfolio(id=pid, name=name or pid, base_ccy=base_ccy)
            self.s.add(p)
            self.s.flush()
        return p

    def upsert_instrument(self, symbol: str, **kw) -> Instrument:
        i = self.s.get(Instrument, symbol)
        if i is None:
            i = Instrument(symbol=symbol, **kw)
            self.s.add(i)
        else:
            for k, v in kw.items():
                setattr(i, k, v)
        self.s.flush()
        return i

    # ------------------------------------------------------------------- orders
    def create_order(self, *, id: str, symbol: str, side: str, qty, order_type: str = "market",
                     limit_price=None, portfolio_id: str = "DEFAULT", venue: str | None = None,
                     algo: str | None = None, arrival_mid=None,
                     parent_id: str | None = None) -> OrderRow:
        self.ensure_portfolio(portfolio_id)
        o = OrderRow(
            id=id, symbol=symbol, side=side, qty=_d(qty), order_type=order_type,
            limit_price=None if limit_price is None else _d(limit_price),
            portfolio_id=portfolio_id, venue=venue, algo=algo,
            arrival_mid=None if arrival_mid is None else _d(arrival_mid),
            parent_id=parent_id,
        )
        self.s.add(o)
        self.s.flush()
        self.audit("order.created", o.id, {"symbol": symbol, "side": side, "qty": str(qty)})
        return o

    def get_order(self, order_id: str) -> OrderRow | None:
        return self.s.get(OrderRow, order_id)

    def list_orders(self, *, limit: int = 200, symbol: str | None = None,
                    status: str | None = None, portfolio_id: str | None = None) -> list[OrderRow]:
        q = select(OrderRow).order_by(OrderRow.created_at.desc()).limit(limit)
        if symbol:
            q = q.where(OrderRow.symbol == symbol)
        if status:
            q = q.where(OrderRow.status == status)
        if portfolio_id:
            q = q.where(OrderRow.portfolio_id == portfolio_id)
        return list(self.s.scalars(q))

    def cancel_order(self, order_id: str, reason: str | None = None) -> OrderRow | None:
        o = self.s.get(OrderRow, order_id)
        if o is None or o.status in ("filled", "cancelled", "rejected"):
            return None          # terminal orders are not cancellable
        o.status = "cancelled"
        if reason:
            o.reject_reason = reason
        self.s.flush()
        self.audit("order.cancelled", order_id, {"reason": reason})
        return o

    # -------------------------------------------------------------------- fills
    def record_fill(self, *, fill_id: str, order_id: str, qty, price, fee=0,
                    fee_ccy: str = "USD", venue: str | None = None,
                    exec_id: str | None = None) -> FillResult:
        """Books a fill, advances the order, and updates the position — in one
        transaction, because an order and its fill landing separately is exactly
        the inconsistency the relational store exists to prevent."""
        o = self.s.get(OrderRow, order_id)
        if o is None:
            raise ValueError(f"unknown order {order_id}")

        q, px = _d(qty), _d(price)
        if q <= 0:
            raise ValueError("fill qty must be positive")
        if px <= 0:
            raise ValueError("fill price must be positive")

        # Idempotency is enforced by the database, not by a check that races with
        # a concurrent writer.
        if exec_id is not None and venue is not None:
            existing = self.s.scalar(
                select(FillRow).where(FillRow.venue == venue, FillRow.exec_id == exec_id))
            if existing is not None:
                pos = self._position(o.portfolio_id, o.symbol)
                return FillResult(existing.id, True, _d(pos.qty), _d(pos.avg_cost), D(0))

        remaining = _d(o.qty) - _d(o.filled_qty)
        if q > remaining:
            raise ValueError(f"fill {q} exceeds leaves {remaining} on order {order_id}")

        f = FillRow(id=fill_id, order_id=order_id, symbol=o.symbol, side=o.side,
                    qty=q, price=px, fee=_d(fee), fee_ccy=fee_ccy,
                    venue=venue, exec_id=exec_id)
        self.s.add(f)

        # Order: weighted average of its own fills.
        prev_filled = _d(o.filled_qty)
        prev_avg = _d(o.avg_price)
        new_filled = prev_filled + q
        o.avg_price = ((prev_avg * prev_filled) + (px * q)) / new_filled
        o.filled_qty = new_filled
        o.status = "filled" if new_filled >= _d(o.qty) else "partially_filled"

        realized = self._apply_to_position(o.portfolio_id, o.symbol, o.side, q, px, _d(fee))

        try:
            self.s.flush()
        except IntegrityError:
            self.s.rollback()
            raise

        pos = self._position(o.portfolio_id, o.symbol)
        self.audit("fill.booked", order_id,
                   {"fill_id": fill_id, "qty": str(q), "price": str(px)})
        return FillResult(fill_id, False, _d(pos.qty), _d(pos.avg_cost), realized)

    def list_fills(self, *, order_id: str | None = None, symbol: str | None = None,
                   limit: int = 500) -> list[FillRow]:
        q = select(FillRow).order_by(FillRow.created_at.desc()).limit(limit)
        if order_id:
            q = q.where(FillRow.order_id == order_id)
        if symbol:
            q = q.where(FillRow.symbol == symbol)
        return list(self.s.scalars(q))

    # ---------------------------------------------------------------- positions
    def _position(self, portfolio_id: str, symbol: str) -> PositionRow:
        p = self.s.get(PositionRow, (portfolio_id, symbol))
        if p is None:
            p = PositionRow(portfolio_id=portfolio_id, symbol=symbol,
                            qty=D(0), avg_cost=D(0), realized_pnl=D(0))
            self.s.add(p)
            self.s.flush()
        return p

    def _apply_to_position(self, portfolio_id: str, symbol: str, side: str,
                           qty: Decimal, price: Decimal, fee: Decimal) -> Decimal:
        """Returns realised P&L from this fill. See the module docstring for why
        increasing and reducing fills are treated differently."""
        pos = self._position(portfolio_id, symbol)
        signed = qty if side == "buy" else -qty
        old_q, old_avg = _d(pos.qty), _d(pos.avg_cost)
        realized = D(0)

        if old_q == 0 or (old_q > 0) == (signed > 0):
            # Opening or increasing: the average moves.
            new_q = old_q + signed
            total = (old_avg * abs(old_q)) + (price * qty)
            pos.avg_cost = total / abs(new_q) if new_q != 0 else D(0)
            pos.qty = new_q
        else:
            closing = min(abs(signed), abs(old_q))
            # Long closed by a sell profits when price > avg; short closed by a
            # buy profits when price < avg. One expression, signed by direction.
            direction = D(1) if old_q > 0 else D(-1)
            realized = (price - old_avg) * closing * direction
            new_q = old_q + signed
            if (new_q > 0) != (old_q > 0) and new_q != 0:
                # Crossed through zero: the remainder opens a NEW position at this
                # fill's price. Carrying the old average across the flip would let
                # a closed long's cost basis follow you into a short.
                pos.avg_cost = price
            elif new_q == 0:
                pos.avg_cost = D(0)          # flat has no cost basis
            # else: reducing but not flipping -- average is deliberately unchanged.
            pos.qty = new_q

        pos.realized_pnl = _d(pos.realized_pnl) + realized - fee
        self.s.flush()
        return realized

    def get_position(self, symbol: str, portfolio_id: str = "DEFAULT") -> PositionRow | None:
        return self.s.get(PositionRow, (portfolio_id, symbol))

    def list_positions(self, *, portfolio_id: str | None = None,
                       include_flat: bool = False) -> list[PositionRow]:
        q = select(PositionRow)
        if portfolio_id:
            q = q.where(PositionRow.portfolio_id == portfolio_id)
        if not include_flat:
            q = q.where(PositionRow.qty != 0)
        return list(self.s.scalars(q.order_by(PositionRow.symbol)))

    def recompute_position(self, symbol: str, portfolio_id: str = "DEFAULT") -> Decimal:
        """Net quantity derived from the FILLS, ignoring the stored position.

        The stored row is a cache for blotter reads; the fills are the truth. This
        is how a divergence between the two becomes detectable instead of
        invisible, which is what makes the stored copy safe to keep at all.
        """
        rows = self.s.execute(
            select(FillRow.side, func.sum(FillRow.qty))
            .join(OrderRow, FillRow.order_id == OrderRow.id)
            .where(FillRow.symbol == symbol, OrderRow.portfolio_id == portfolio_id)
            .group_by(FillRow.side)
        ).all()
        net = D(0)
        for side, total in rows:
            net += _d(total) if side == "buy" else -_d(total)
        return net

    # -------------------------------------------------------------------- audit
    def audit(self, kind: str, entity_id: str | None, payload: dict,
              actor: str = "system") -> None:
        self.s.add(AuditEvent(kind=kind, entity_id=entity_id, actor=actor,
                              payload=json.dumps(payload, default=str)))

    def list_audit(self, *, entity_id: str | None = None, kind: str | None = None,
                   limit: int = 200) -> list[AuditEvent]:
        q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
        if entity_id:
            q = q.where(AuditEvent.entity_id == entity_id)
        if kind:
            q = q.where(AuditEvent.kind == kind)
        return list(self.s.scalars(q))
