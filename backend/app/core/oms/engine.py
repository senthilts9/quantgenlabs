"""OMS engine — order lifecycle and average-cost position accounting.

The accounting here is the part worth reviewing closely. Three cases matter
and blotters get the third one wrong all the time:

  1. OPENING / ADDING  -- same-sign fill: weighted-average the entry price,
     realize nothing.
  2. REDUCING          -- opposite-sign fill smaller than the position:
     realize P&L on the closed quantity, leave the average entry alone
     (the remaining lot was bought at the same price it always was).
  3. FLIPPING          -- opposite-sign fill LARGER than the position: realize
     on the part that closed, then re-open the remainder at the fill price.
     Averaging across a flip is the classic bug -- it silently blends a long
     cost basis into a short one and every subsequent P&L number is wrong.

Realized P&L is signed by the direction of the position being closed, so a
profitable short (sell 100, cover 90) books +10, not -10.

All state changes go through the store in one transaction; nothing here
mutates the DB directly.
"""
from __future__ import annotations

from dataclasses import replace

from .models import (Fill, Order, OrderStatus, OrderType, Position, Side,
                     _now, new_id)
from .store import OMSStore

# Below this the position is treated as flat -- guards against float dust
# leaving a 1e-15 residual that renders as a phantom open position.
EPS = 1e-9


def apply_fill_to_position(pos: Position, side: Side, qty: float,
                           price: float, fee: float = 0.0) -> Position:
    """Pure function: position + fill -> new position. No I/O, unit-testable."""
    signed = qty * side.sign
    old_qty, old_avg = pos.qty, pos.avg_price
    realized = pos.realized_pnl
    new_qty = old_qty + signed

    if abs(old_qty) < EPS:                       # opening from flat
        new_avg = price
    elif (old_qty > 0) == (signed > 0):          # 1. adding to the same side
        new_avg = (old_qty * old_avg + signed * price) / new_qty
    else:                                        # reducing, closing or flipping
        closed = min(abs(signed), abs(old_qty))
        direction = 1.0 if old_qty > 0 else -1.0
        realized += (price - old_avg) * closed * direction
        if abs(new_qty) < EPS:                   # 2b. flat
            new_qty, new_avg = 0.0, 0.0
        elif (new_qty > 0) == (old_qty > 0):     # 2a. still same side
            new_avg = old_avg
        else:                                    # 3. flipped through zero
            new_avg = price

    # portfolio_id MUST be carried through: this returns a NEW Position, and
    # omitting the field silently defaults it to "DEFAULT", so every fill
    # lands in the wrong book while the order still looks correctly tagged.
    return Position(symbol=pos.symbol, portfolio_id=pos.portfolio_id,
                    qty=new_qty, avg_price=new_avg,
                    realized_pnl=realized, fees=pos.fees + fee,
                    updated_at=_now())


class OMS:
    """Order management: accept, fill, cancel, and report."""

    def __init__(self, store: OMSStore):
        self.store = store

    # ---- lifecycle ------------------------------------------------------
    def place(self, symbol: str, side: Side, qty: float,
              order_type: OrderType = OrderType.MARKET,
              limit_price: float | None = None, parent_id: str | None = None,
              venue: str | None = None, algo: str | None = None,
              arrival_mid: float | None = None,
              portfolio_id: str = "DEFAULT") -> Order:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if order_type is OrderType.LIMIT and not limit_price:
            raise ValueError("limit order requires limit_price")
        order = Order(id=new_id("ord"), symbol=symbol.upper(), side=side, qty=qty,
                      order_type=order_type, limit_price=limit_price,
                      parent_id=parent_id, venue=venue, algo=algo,
                      arrival_mid=arrival_mid,
                      portfolio_id=(portfolio_id or "DEFAULT").upper())
        self.store.insert_order(order)
        return order

    def reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        order.updated_at = _now()
        self.store.update_order(order)
        return order

    def cancel(self, order_id: str) -> Order:
        order = self.store.get_order(order_id)
        if order is None:
            raise KeyError(order_id)
        if order.status.is_terminal:
            raise ValueError(f"order {order_id} is {order.status.value}")
        order.status = OrderStatus.CANCELLED
        order.updated_at = _now()
        self.store.update_order(order)
        return order

    def fill(self, order_id: str, qty: float, price: float,
             venue: str | None = None, fee: float = 0.0) -> tuple[Order, Fill, Position]:
        """Apply an execution. Partial fills are the norm, not the exception."""
        order = self.store.get_order(order_id)
        if order is None:
            raise KeyError(order_id)
        if order.status.is_terminal:
            raise ValueError(f"cannot fill a {order.status.value} order")
        if qty <= 0:
            raise ValueError("fill qty must be positive")
        # Overfilling is a genuine integrity error, not something to clamp
        # silently -- a venue reporting more than we asked for means the
        # order state and the venue's disagree, and that needs to surface.
        if qty > order.leaves_qty + EPS:
            raise ValueError(
                f"fill {qty} exceeds leaves {order.leaves_qty} on {order_id}")

        # weighted-average the order's own execution price
        total = order.filled_qty + qty
        order.avg_price = ((order.avg_price * order.filled_qty + price * qty) / total
                           if total > 0 else 0.0)
        order.filled_qty = total
        order.status = (OrderStatus.FILLED if order.leaves_qty <= EPS
                        else OrderStatus.PARTIALLY_FILLED)
        order.venue = venue or order.venue
        order.updated_at = _now()

        fill = Fill(id=new_id("fill"), order_id=order.id, symbol=order.symbol,
                    side=order.side, qty=qty, price=price, venue=venue, fee=fee)

        # Position is looked up per PORTFOLIO: the same symbol held by two
        # books must keep two independent cost bases.
        pos = (self.store.get_position(order.symbol, order.portfolio_id)
               or Position(symbol=order.symbol, portfolio_id=order.portfolio_id))
        pos = apply_fill_to_position(pos, order.side, qty, price, fee)

        self.store.record_fill(fill, order, pos)
        return order, fill, pos

    def roll_up_parent(self, parent_id: str) -> Order:
        """Recompute a parent's fill state from its children.

        A parent order is an aggregate, not a separate execution. Only the
        children actually hit the market, so only their fills may move the
        position -- calling fill() on the parent as well would book the same
        quantity twice and silently double the position (a bug this method
        exists to make structurally impossible).
        """
        parent = self.store.get_order(parent_id)
        if parent is None:
            raise KeyError(parent_id)
        children = [o for o in self.store.list_orders(limit=500)
                    if o.parent_id == parent_id]
        filled = sum(c.filled_qty for c in children)
        notional = sum(c.filled_qty * c.avg_price for c in children)
        parent.filled_qty = filled
        parent.avg_price = (notional / filled) if filled > 0 else 0.0
        parent.status = (OrderStatus.FILLED if parent.leaves_qty <= EPS
                         else OrderStatus.PARTIALLY_FILLED if filled > 0
                         else parent.status)
        parent.updated_at = _now()
        self.store.update_order(parent)
        return parent

    # ---- reporting ------------------------------------------------------
    def blotter(self, marks: dict[str, float] | None = None,
                portfolio_id: str | None = None) -> dict:
        """Positions + P&L. `marks` supplies current prices per symbol; any
        symbol without a mark falls back to its own average price, which makes
        unrealised P&L exactly zero rather than fabricating a move."""
        marks = {k.upper(): v for k, v in (marks or {}).items()}
        rows, gross, net, unreal, real, fees = [], 0.0, 0.0, 0.0, 0.0, 0.0
        by_pf: dict[str, dict] = {}
        for p in self.store.list_positions(portfolio_id=portfolio_id):
            mark = marks.get(p.symbol) or p.avg_price
            u = p.unrealized_pnl(mark)
            notional = p.notional(mark)
            gross += notional
            net += notional * (1 if p.qty > 0 else -1)
            unreal += u
            real += p.realized_pnl
            fees += p.fees
            agg = by_pf.setdefault(p.portfolio_id, {
                "portfolio_id": p.portfolio_id, "positions": 0,
                "gross_notional": 0.0, "unrealized_pnl": 0.0,
                "realized_pnl": 0.0, "fees": 0.0})
            agg["positions"] += 1
            agg["gross_notional"] += notional
            agg["unrealized_pnl"] += u
            agg["realized_pnl"] += p.realized_pnl
            agg["fees"] += p.fees

            rows.append({
                "portfolio_id": p.portfolio_id,
                "symbol": p.symbol, "qty": round(p.qty, 8),
                "side": "long" if p.qty > 0 else "short",
                "avg_price": round(p.avg_price, 6),
                "mark": round(mark, 6),
                "marked": p.symbol in marks,
                "notional": round(notional, 2),
                "unrealized_pnl": round(u, 2),
                "realized_pnl": round(p.realized_pnl, 2),
                "fees": round(p.fees, 4),
                "total_pnl": round(u + p.realized_pnl - p.fees, 2),
                "updated_at": p.updated_at,
            })
        for a in by_pf.values():
            for k in ("gross_notional", "unrealized_pnl", "realized_pnl", "fees"):
                a[k] = round(a[k], 2)
            a["total_pnl"] = round(a["unrealized_pnl"] + a["realized_pnl"]
                                   - a["fees"], 2)
        return {
            "positions": rows,
            "portfolio_id": portfolio_id,
            "by_portfolio": sorted(by_pf.values(), key=lambda x: x["portfolio_id"]),
            "totals": {
                "position_count": len(rows),
                "gross_notional": round(gross, 2),
                "net_notional": round(net, 2),
                "unrealized_pnl": round(unreal, 2),
                "realized_pnl": round(real, 2),
                "fees": round(fees, 4),
                "total_pnl": round(unreal + real - fees, 2),
            },
        }
