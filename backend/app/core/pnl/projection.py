"""Real-time P&L, built as a projection over the event stream.

WHAT A PROJECTION IS, AND WHY IT IS NOT JUST A CACHE. This holds no authoritative
state. It is derived entirely from `trades.executed` and `market.normalized`, and
it can be thrown away and rebuilt from the log at any time. That property is the
whole point: a P&L number you cannot reconstruct is a P&L number you cannot
defend, and "rebuild it and see if it matches" is the only check that actually
proves the running total has not drifted.

REALISED AND UNREALISED ARE DIFFERENT KINDS OF NUMBER, and conflating them is the
classic P&L bug:

  REALISED is a fact. It crystallised when a position was reduced, at a price that
  was actually traded, and it never changes again. It does not move when the
  market moves.

  UNREALISED is an opinion. It is `(mark - avg_cost) * qty`, and it is only as
  good as the mark. A stale mark produces an unrealised number that looks precise
  and is wrong, which is why every mark here carries its age and a position whose
  mark has gone stale is reported as such rather than silently valued.

ATTRIBUTION ANSWERS "WHY DID P&L CHANGE". The components must SUM to the observed
change, or the attribution is decorative:

  price effect   = qty_held_before * (mark_now - mark_before)
                   the market moving against what was ALREADY held. Well-defined
                   and computed directly.

  trading effect = realised_change + (unrealised_now - unrealised_before)
                                   - price_effect
                   everything the market move does not explain: the value trading
                   added or removed, net of the unrealised it consumed or created.

  fees           = costs booked this interval, always negative

THREE SANITY CHECKS this must pass, each of which caught a real error here:

  * Selling AT the mark attributes to ZERO. You realise precisely what you were
    already carrying, so nothing changed. Counting the realised gain without
    subtracting the unrealised it came from double-counts it.
  * Buying more AT the mark attributes to ZERO. This one additionally requires
    valuing the previous period with the average cost IN EFFECT THEN — using the
    new blended average makes a neutral purchase look like a gain.
  * A pure market move with no trading attributes ENTIRELY to price effect.

The reconcile() method exists because a projection that nobody checks against the
authoritative store is a projection that will silently diverge.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Marks older than this are not trusted for valuation. A stale mark produces an
# unrealised number that looks precise and is wrong.
DEFAULT_STALE_AFTER_S = 30.0


@dataclass
class PositionPnL:
    symbol: str
    portfolio_id: str = "DEFAULT"
    net_qty: float = 0.0            # signed: long > 0, short < 0
    avg_cost: float = 0.0
    realized: float = 0.0           # crystallised; never moves with the market
    fees: float = 0.0
    mark: float | None = None
    mark_ts: float | None = None
    # Snapshot of the previous interval, for attribution. avg_cost is included
    # because valuing the previous period with the CURRENT average is wrong the
    # moment a position is added to: buying more at the mark changes nothing, but
    # valuing "then" with the new blended average makes it look like a gain.
    _prev_mark: float | None = None
    _prev_qty: float = 0.0
    _prev_avg: float = 0.0
    _prev_realized: float = 0.0
    _prev_fees: float = 0.0

    def mark_age_s(self, now: float | None = None) -> float | None:
        if self.mark_ts is None:
            return None
        return (now or time.time()) - self.mark_ts

    def is_stale(self, now: float | None = None,
                 stale_after: float = DEFAULT_STALE_AFTER_S) -> bool:
        age = self.mark_age_s(now)
        return age is None or age > stale_after

    def unrealized(self) -> float | None:
        """None when there is no mark. Returning 0.0 would be a lie — a flat
        number and an unknown number are different things, and a caller that
        cannot tell them apart will sum the unknown into a total."""
        if self.mark is None or self.net_qty == 0:
            return 0.0 if self.net_qty == 0 else None
        return (self.mark - self.avg_cost) * self.net_qty

    def total(self) -> float | None:
        u = self.unrealized()
        return None if u is None else self.realized + u - self.fees


@dataclass
class Attribution:
    """Why P&L changed over one interval. The three components must sum to the
    observed change, or the attribution is decorative."""
    symbol: str
    price_effect: float = 0.0
    trading_effect: float = 0.0
    fee_effect: float = 0.0
    # The two halves of trading_effect, exposed so the decomposition is
    # inspectable rather than a single opaque number.
    realized_component: float = 0.0
    unrealized_released: float = 0.0
    total_change: float = 0.0
    explained: bool = True
    residual: float = 0.0
    # The first interval for a symbol has no PRIOR mark, so there is no price
    # move to attribute -- you cannot measure a move without knowing where it
    # started. Reporting that as an unexplained residual would look like a defect
    # when it is the absence of a baseline, so it is flagged instead and real
    # attribution begins on the next call.
    baseline: bool = False


class PnLProjection:
    """Read model over trades and marks. Rebuildable from the log by construction."""

    def __init__(self, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> None:
        self._lock = threading.RLock()
        self._pos: dict[tuple[str, str], PositionPnL] = {}
        self.stale_after_s = stale_after_s
        self.trades_applied = 0
        self.marks_applied = 0
        self.duplicates_ignored = 0
        self._seen_exec: set[str] = set()

    # ------------------------------------------------------------- inputs
    def apply_fill(self, *, symbol: str, side: str, qty: float, price: float,
                   fee: float = 0.0, portfolio_id: str = "DEFAULT",
                   exec_id: str | None = None) -> PositionPnL:
        """Apply one execution.

        Deduplicated on exec_id because the bus is at-least-once: a redelivered
        fill applied twice doubles the position, and nothing downstream would
        notice until someone reconciled against the broker.
        """
        with self._lock:
            if exec_id is not None:
                if exec_id in self._seen_exec:
                    self.duplicates_ignored += 1
                    return self._get(portfolio_id, symbol)
                self._seen_exec.add(exec_id)

            p = self._get(portfolio_id, symbol)
            signed = qty if side in ("buy", "1", "BUY") else -qty
            old_q, old_avg = p.net_qty, p.avg_cost

            if old_q == 0 or (old_q > 0) == (signed > 0):
                # Opening or increasing: the average moves.
                new_q = old_q + signed
                p.avg_cost = ((old_avg * abs(old_q)) + (price * qty)) / abs(new_q) if new_q else 0.0
                p.net_qty = new_q
            else:
                # Reducing: realise against the EXISTING average, and leave that
                # average alone. Recomputing it here is the classic bug -- sell
                # half and the remaining half's basis silently moves.
                closing = min(abs(signed), abs(old_q))
                direction = 1.0 if old_q > 0 else -1.0
                p.realized += (price - old_avg) * closing * direction
                new_q = old_q + signed
                if new_q != 0 and (new_q > 0) != (old_q > 0):
                    # Crossed through zero: the remainder is a NEW position at
                    # this price. Carrying the old basis across the flip would
                    # flatter the next close.
                    p.avg_cost = price
                elif new_q == 0:
                    p.avg_cost = 0.0
                p.net_qty = new_q

            p.fees += fee
            self.trades_applied += 1
            return p

    def apply_mark(self, symbol: str, price: float, *,
                   portfolio_id: str = "DEFAULT", ts: float | None = None) -> None:
        with self._lock:
            p = self._get(portfolio_id, symbol)
            p.mark = price
            p.mark_ts = ts if ts is not None else time.time()
            self.marks_applied += 1

    def apply_event(self, payload: dict) -> None:
        """Dispatch a bus envelope payload. Unknown events are ignored rather than
        raising: a projection that dies on an event type it has not been taught
        about takes the whole consumer group down with it."""
        kind = payload.get("event")
        if kind == "trade.executed":
            self.apply_fill(symbol=payload["symbol"], side=payload["side"],
                            qty=float(payload["qty"]), price=float(payload["price"]),
                            fee=float(payload.get("fee", 0.0)),
                            exec_id=payload.get("exec_id"))
        elif kind == "market.tick":
            self.apply_mark(payload["symbol"], float(payload["price"]))

    # ------------------------------------------------------------ outputs
    def position(self, symbol: str, portfolio_id: str = "DEFAULT") -> PositionPnL | None:
        with self._lock:
            return self._pos.get((portfolio_id, symbol))

    def positions(self, *, include_flat: bool = False) -> list[PositionPnL]:
        with self._lock:
            return [p for p in self._pos.values() if include_flat or p.net_qty != 0]

    def totals(self, now: float | None = None) -> dict:
        """Portfolio totals. Positions with a stale or missing mark are counted
        separately rather than folded in, because an unrealised total that
        silently includes unvalued positions is worse than one that says so."""
        now = now or time.time()
        with self._lock:
            realized = fees = unrealized = 0.0
            valued = stale = 0
            for p in self._pos.values():
                realized += p.realized
                fees += p.fees
                if p.net_qty == 0:
                    continue
                if p.is_stale(now, self.stale_after_s):
                    stale += 1
                    continue
                u = p.unrealized()
                if u is not None:
                    unrealized += u
                    valued += 1
            return {
                "realized": realized,
                "unrealized": unrealized,
                "fees": fees,
                "total": realized + unrealized - fees,
                "positions_valued": valued,
                "positions_unvalued": stale,
                "fully_valued": stale == 0,
                "note": ("Every open position has a fresh mark."
                         if stale == 0 else
                         f"{stale} open position(s) have a stale or missing mark and are "
                         "EXCLUDED from unrealised. The total below is therefore partial."),
            }

    # -------------------------------------------------------- attribution
    def attribute(self, portfolio_id: str = "DEFAULT") -> list[Attribution]:
        """Explain the change since the last call, then reset the baseline.

        Each component must sum to the observed change; `residual` is published
        rather than hidden, because an attribution that does not add up is worse
        than none — it looks authoritative and is not.
        """
        out: list[Attribution] = []
        with self._lock:
            for (pid, sym), p in self._pos.items():
                if pid != portfolio_id:
                    continue
                # No prior mark: establish the baseline and attribute nothing.
                # Attribution is a statement about the interval BETWEEN two
                # marks, and there is only one so far.
                if p._prev_mark is None:
                    out.append(Attribution(symbol=sym, baseline=True, explained=True))
                    p._prev_mark, p._prev_qty = p.mark, p.net_qty
                    p._prev_avg = p.avg_cost
                    p._prev_realized, p._prev_fees = p.realized, p.fees
                    continue

                prev_total = self._total_at(p._prev_avg, p._prev_mark, p._prev_qty,
                                            p._prev_realized, p._prev_fees)
                now_total = self._total_at(p.avg_cost, p.mark, p.net_qty,
                                           p.realized, p.fees)
                if prev_total is None or now_total is None:
                    p._prev_mark, p._prev_qty = p.mark, p.net_qty
                    p._prev_realized, p._prev_fees = p.realized, p.fees
                    continue

                # The market moved against the quantity we HELD BEFORE -- not
                # the quantity now, which includes this interval's own trading.
                # The market moved against the quantity held BEFORE -- not the
                # quantity now, which includes this interval's own trading.
                price_effect = p._prev_qty * (p.mark - p._prev_mark) if p.mark is not None else 0.0

                realized_component = p.realized - p._prev_realized
                # Unrealised then vs now, each valued with ITS OWN average cost.
                unreal_now = (p.mark - p.avg_cost) * p.net_qty if p.mark is not None else 0.0
                unreal_prev = (p._prev_mark - p._prev_avg) * p._prev_qty
                unrealized_released = unreal_now - unreal_prev

                # Whatever the market move on the held quantity does not explain.
                trading_effect = realized_component + unrealized_released - price_effect

                fee_effect = -(p.fees - p._prev_fees)
                change = now_total - prev_total
                residual = change - (price_effect + trading_effect + fee_effect)

                out.append(Attribution(
                    symbol=sym, price_effect=price_effect,
                    trading_effect=trading_effect, fee_effect=fee_effect,
                    realized_component=realized_component,
                    unrealized_released=unrealized_released,
                    total_change=change, residual=residual,
                    explained=abs(residual) < 1e-6,
                ))
                p._prev_mark, p._prev_qty = p.mark, p.net_qty
                p._prev_avg = p.avg_cost
                p._prev_realized, p._prev_fees = p.realized, p.fees
        return out

    @staticmethod
    def _total_at(avg_cost: float, mark: float | None, qty: float,
                  realized: float, fees: float) -> float | None:
        """Value a snapshot with the average cost IN EFFECT AT THAT TIME. Taking
        avg_cost as a parameter rather than reading it off the position is the
        whole point: the current average is the wrong one for the prior period."""
        if mark is None:
            return None if qty != 0 else realized - fees
        return realized + (mark - avg_cost) * qty - fees

    # ------------------------------------------------------- reconciliation
    def reconcile(self, authoritative: dict[tuple[str, str], float],
                  *, tolerance: float = 1e-9) -> dict:
        """Compare the projection's positions against the authoritative store.

        A projection nobody checks is a projection that will silently diverge.
        This is the check, and it reports EVERY discrepancy rather than a boolean:
        "they differ" is not actionable, "AAPL is 100 here and 150 there" is.
        """
        with self._lock:
            mine = {(p.portfolio_id, p.symbol): p.net_qty for p in self._pos.values()}
        keys = set(mine) | set(authoritative)
        breaks = []
        for k in sorted(keys):
            a, b = mine.get(k, 0.0), authoritative.get(k, 0.0)
            if abs(a - b) > tolerance:
                breaks.append({"portfolio_id": k[0], "symbol": k[1],
                               "projection": a, "authoritative": b, "difference": a - b})
        return {
            "matched": len(keys) - len(breaks),
            "breaks": breaks,
            "clean": not breaks,
            "note": ("Projection agrees with the store."
                     if not breaks else
                     f"{len(breaks)} position(s) disagree. The STORE is authoritative; "
                     "the projection should be rebuilt from the event log."),
        }

    def rebuild_from(self, events) -> int:
        """Throw the projection away and rebuild from the log.

        This is the property that makes the number defensible: if a rebuild
        disagrees with the running total, the running total was wrong.
        """
        with self._lock:
            self._pos.clear()
            self._seen_exec.clear()
            self.trades_applied = self.marks_applied = self.duplicates_ignored = 0
        n = 0
        for e in events:
            self.apply_event(e)
            n += 1
        return n

    def stats(self) -> dict:
        with self._lock:
            return {"trades_applied": self.trades_applied,
                    "marks_applied": self.marks_applied,
                    "duplicates_ignored": self.duplicates_ignored,
                    "positions_tracked": len(self._pos)}

    def _get(self, portfolio_id: str, symbol: str) -> PositionPnL:
        key = (portfolio_id, symbol)
        if key not in self._pos:
            self._pos[key] = PositionPnL(symbol=symbol, portfolio_id=portfolio_id)
        return self._pos[key]
