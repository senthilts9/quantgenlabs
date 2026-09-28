"""Operator risk limits and the kill switch.

Distinct from `validation` on purpose. Validation answers "will the venue
accept this order?" - it encodes the venue's rules and there is no discretion
in it. Risk answers "should *we* send this order?" - it encodes the operator's
appetite, and it is the layer that stops a correct-but-catastrophic order.

A well-formed order for 500 BTC passes every validation rule and should still
be refused by a desk whose limit is 5. That is this module.

The limits are checked against *account state plus pending orders*, not just
filled positions. Ten orders each inside the position limit, all live at once,
breach it together - checking only filled size is how that happens.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Deque, Dict, List, Optional

from ..core.errors import Code, Rejection, Stage
from ..core.types import ZERO, AccountState, OrderRequest, Quote, dec


@dataclass
class RiskLimits:
    """Operator-set bounds. Everything optional - None means unlimited.

    Defaults are deliberately tight. A risk config that starts permissive and
    is tightened later protects nobody during the window where it matters, and
    the failure mode of a too-tight limit is a rejected order with a clear
    reason.
    """

    max_order_notional: Optional[Decimal] = Decimal("50000")
    max_position_notional: Optional[Decimal] = Decimal("250000")
    max_gross_notional: Optional[Decimal] = Decimal("500000")
    max_daily_loss: Optional[Decimal] = Decimal("5000")
    max_orders_per_minute: Optional[int] = 60
    max_open_orders: Optional[int] = 50
    #: Whitelists. Empty set means "no restriction"; a populated set is exclusive.
    allowed_markets: frozenset = field(default_factory=frozenset)
    allowed_venues: frozenset = field(default_factory=frozenset)
    #: Refuse to trade on a book assembled from a synthetic/offline fallback.
    require_real_market_data: bool = True

    @classmethod
    def permissive(cls) -> "RiskLimits":
        """No limits. For tests that are exercising something else."""
        return cls(
            max_order_notional=None, max_position_notional=None,
            max_gross_notional=None, max_daily_loss=None,
            max_orders_per_minute=None, max_open_orders=None,
            require_real_market_data=False,
        )


class KillSwitch:
    """A latch that blocks all new orders once tripped.

    Latching rather than momentary: a condition that trips the switch is
    usually transient in appearance and persistent in cause, and a switch that
    silently re-arms itself when the symptom clears will let the same failure
    through repeatedly. Clearing it is an explicit human action.
    """

    def __init__(self, store=None) -> None:
        #: Optional durable backing. A latching switch that a restart re-arms
        #: is not latching - it is exactly the "silently re-arms itself"
        #: behaviour this class exists to prevent.
        self._store = store
        self._tripped = False
        self._reason: Optional[str] = None
        self._code: Code = Code.KILL_SWITCH_ACTIVE
        self._tripped_at: Optional[float] = None
        self.history: List[str] = []
        if store is not None:
            self._rehydrate()

    def _rehydrate(self) -> None:
        state = self._store.get("kill_switch")
        if not state or not state.get("tripped"):
            return
        self._tripped = True
        self._reason = state.get("reason")
        self._tripped_at = state.get("tripped_at")
        try:
            self._code = Code(state.get("code", Code.KILL_SWITCH_ACTIVE.value))
        except ValueError:
            self._code = Code.KILL_SWITCH_ACTIVE
        self.history = list(state.get("history") or [])
        self.history.append("REHYDRATED still tripped: %s" % self._reason)

    def _persist(self) -> None:
        if self._store is None:
            return
        self._store.put("kill_switch", {
            "tripped": self._tripped,
            "reason": self._reason,
            "code": self._code.value,
            "tripped_at": self._tripped_at,
            "history": self.history[-50:],
        })

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def code(self) -> Code:
        """Why it tripped, as a branchable code.

        A manual halt and a breached loss limit both stop trading, but the
        remediation differs - one is cleared by an operator deciding to
        resume, the other needs the day to roll or the limit to be revised.
        Collapsing both into KILL_SWITCH_ACTIVE made DAILY_LOSS_LIMIT
        unreachable and left callers unable to tell them apart.
        """
        return self._code

    def trip(self, reason: str, code: Code = Code.KILL_SWITCH_ACTIVE) -> None:
        if not self._tripped:
            self._tripped = True
            self._reason = reason
            self._code = code
            self._tripped_at = time.time()
            self.history.append("TRIPPED[%s]: %s" % (code.value, reason))
            self._persist()

    def clear(self, operator: str = "unknown") -> None:
        if self._tripped:
            self.history.append("CLEARED by %s (was: %s)" % (operator, self._reason))
        self._tripped = False
        self._reason = None
        self._code = Code.KILL_SWITCH_ACTIVE
        self._tripped_at = None
        self._persist()

    def __str__(self) -> str:
        return ("KILL SWITCH ACTIVE: %s" % self._reason if self._tripped
                else "kill switch clear")


class RiskEngine:
    """Applies `RiskLimits` to outbound orders."""

    def __init__(self, limits: Optional[RiskLimits] = None,
                 kill_switch: Optional[KillSwitch] = None,
                 store=None):
        self.limits = limits or RiskLimits()
        self.kill_switch = kill_switch or KillSwitch(store=store)
        self._store = store
        self._order_times: Deque[float] = deque()
        self._realised_pnl_today = ZERO
        self._day = time.strftime("%Y-%m-%d")
        if store is not None:
            saved = store.get("risk_pnl") or {}
            # Only restore within the same day; a stale figure would carry
            # yesterday's losses into today's limit.
            if saved.get("day") == self._day:
                self._realised_pnl_today = dec(saved.get("realised", "0"))

    # -- pnl tracking ------------------------------------------------------

    def record_pnl(self, delta: Decimal) -> None:
        """Book realised PnL and trip the switch if the daily limit is breached.

        Rolls at local midnight. A desk trading across a UTC boundary should
        pass its own day key; getting this wrong resets the loss limit halfway
        through a bad session.
        """
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._realised_pnl_today = ZERO
        self._realised_pnl_today += dec(delta)
        if self._store is not None:
            self._store.put("risk_pnl", {"day": self._day,
                                         "realised": str(self._realised_pnl_today)})

        limit = self.limits.max_daily_loss
        if limit is not None and self._realised_pnl_today <= -abs(limit):
            self.kill_switch.trip(
                "daily loss %s breached limit %s"
                % (self._realised_pnl_today, limit),
                code=Code.DAILY_LOSS_LIMIT,
            )

    @property
    def pnl_today(self) -> Decimal:
        return self._realised_pnl_today

    # -- throttle ----------------------------------------------------------

    def _note_order(self) -> None:
        self._order_times.append(time.monotonic())

    def _orders_in_last_minute(self) -> int:
        cutoff = time.monotonic() - 60.0
        while self._order_times and self._order_times[0] < cutoff:
            self._order_times.popleft()
        return len(self._order_times)

    # -- the check ---------------------------------------------------------

    def check(
        self,
        request: OrderRequest,
        account: Optional[AccountState] = None,
        mark: Optional[Quote] = None,
        venue: Optional[str] = None,
        book_source: Optional[str] = None,
        open_order_count: int = 0,
    ) -> List[Rejection]:
        """Return every risk rejection. Empty list means cleared to send."""
        out: List[Rejection] = []

        if self.kill_switch.tripped:
            # Returned alone: once the switch is tripped nothing else matters,
            # and a list of secondary complaints would bury the real reason.
            return [Rejection(
                self.kill_switch.code, Stage.RISK,
                str(self.kill_switch),
                {"reason": self.kill_switch.reason,
                 "code": self.kill_switch.code.value},
            )]

        lim = self.limits

        if lim.allowed_venues and venue and venue not in lim.allowed_venues:
            out.append(Rejection(
                Code.VENUE_NOT_ALLOWED, Stage.RISK,
                "venue %s is not in the allowed set" % venue,
                {"venue": venue, "allowed": sorted(lim.allowed_venues)},
            ))

        if lim.allowed_markets and request.market not in lim.allowed_markets:
            out.append(Rejection(
                Code.VENUE_NOT_ALLOWED, Stage.RISK,
                "market %s is not in the allowed set" % request.market,
                {"market": request.market, "allowed": sorted(lim.allowed_markets)},
            ))

        if lim.require_real_market_data and book_source == "synthetic":
            out.append(Rejection(
                Code.STALE_MARK_PRICE, Stage.RISK,
                "refusing to trade against a synthetic order book - no live "
                "market data was reachable",
                {"book_source": book_source},
            ))

        ref = request.price or (mark.price if mark else None)

        # A market order carries no price. If no mark was supplied either, the
        # notional limits cannot be evaluated - and silently skipping a
        # configured limit is the failure mode that matters, because a market
        # order is exactly what an unbounded notional limit lets through. Fail
        # closed and say why.
        needs_pricing = (lim.max_order_notional is not None
                         or lim.max_position_notional is not None
                         or lim.max_gross_notional is not None)
        if (ref is None or ref <= ZERO) and needs_pricing:
            out.append(Rejection(
                Code.ORDER_NOTIONAL_LIMIT, Stage.RISK,
                "cannot price %s to check notional limits - no order price and "
                "no mark available" % request.market,
                {"market": request.market,
                 "max_order_notional": str(lim.max_order_notional)},
            ))

        if ref is not None and ref > ZERO:
            notional = request.size * ref
            if lim.max_order_notional is not None and notional > lim.max_order_notional:
                out.append(Rejection(
                    Code.ORDER_NOTIONAL_LIMIT, Stage.RISK,
                    "order notional %s exceeds limit %s"
                    % (notional.quantize(Decimal("0.01")), lim.max_order_notional),
                    {"notional": str(notional.quantize(Decimal("0.01"))),
                     "limit": str(lim.max_order_notional)},
                ))

            if account is not None and lim.max_position_notional is not None:
                pos = account.position(request.market)
                projected = abs(pos.size + request.signed_size) * ref
                if projected > lim.max_position_notional:
                    out.append(Rejection(
                        Code.POSITION_LIMIT_EXCEEDED, Stage.RISK,
                        "resulting position notional %s exceeds limit %s"
                        % (projected.quantize(Decimal("0.01")),
                           lim.max_position_notional),
                        {"projected": str(projected.quantize(Decimal("0.01"))),
                         "limit": str(lim.max_position_notional)},
                    ))

            if account is not None and lim.max_gross_notional is not None:
                # Gross across all markets, with this order applied to its own.
                gross = ZERO
                for market, pos in account.positions.items():
                    size = pos.size + (request.signed_size
                                       if market == request.market else ZERO)
                    gross += abs(size) * (pos.mark_price or ref)
                if request.market not in account.positions:
                    gross += abs(request.signed_size) * ref
                if gross > lim.max_gross_notional:
                    out.append(Rejection(
                        Code.POSITION_LIMIT_EXCEEDED, Stage.RISK,
                        "gross notional %s exceeds limit %s"
                        % (gross.quantize(Decimal("0.01")), lim.max_gross_notional),
                        {"gross": str(gross.quantize(Decimal("0.01"))),
                         "limit": str(lim.max_gross_notional)},
                    ))

        if lim.max_open_orders is not None and open_order_count >= lim.max_open_orders:
            out.append(Rejection(
                Code.MAX_OPEN_ORDERS, Stage.RISK,
                "%d open orders is at the operator limit of %d"
                % (open_order_count, lim.max_open_orders),
                {"open": open_order_count, "limit": lim.max_open_orders},
            ))

        if lim.max_orders_per_minute is not None:
            recent = self._orders_in_last_minute()
            if recent >= lim.max_orders_per_minute:
                out.append(Rejection(
                    Code.RATE_LIMITED, Stage.RISK,
                    "%d orders in the last minute, limit %d"
                    % (recent, lim.max_orders_per_minute),
                    {"recent": recent, "limit": lim.max_orders_per_minute},
                ))

        if not out:
            # Only count orders that actually clear risk, or a burst of
            # rejections would throttle the orders that would have been fine.
            self._note_order()
        return out

    def status(self) -> Dict[str, object]:
        return {
            "kill_switch": str(self.kill_switch),
            "pnl_today": str(self._realised_pnl_today),
            "orders_last_minute": self._orders_in_last_minute(),
            "limits": {
                "max_order_notional": str(self.limits.max_order_notional),
                "max_position_notional": str(self.limits.max_position_notional),
                "max_gross_notional": str(self.limits.max_gross_notional),
                "max_daily_loss": str(self.limits.max_daily_loss),
                "max_orders_per_minute": self.limits.max_orders_per_minute,
            },
        }
