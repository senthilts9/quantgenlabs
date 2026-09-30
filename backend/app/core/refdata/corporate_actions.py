"""Corporate actions and the adjustments they force.

THE MISTAKE THIS EXISTS TO PREVENT. A 2-for-1 split doubles the share count and
halves the price. If positions are adjusted but the price HISTORY is not, the
return series shows a -50% move on the ex-date that never happened — and every
volatility, VaR, beta and correlation computed over that window is wrong. The
position looks right, the risk numbers look plausible, and nothing errors.

Equally: if history is adjusted but the position is not, the portfolio doubles in
value overnight.

Both halves or neither. That is the whole point of doing this in one place.

THREE DATES, AND THEY ARE NOT THE SAME DAY:

  ANNOUNCEMENT — when it became known. Matters for compliance and for knowing
    what a strategy could have seen; nothing adjusts.
  EX-DATE      — the first day the security trades WITHOUT the entitlement. This
    is the day prices and positions adjust. Buying on the ex-date does NOT
    entitle you to the dividend.
  PAY-DATE     — when cash actually arrives, usually weeks later. Cash moves
    here, not on the ex-date, which is why a dividend creates a receivable in
    between and why cash reconciliation breaks if you book it on the wrong one.

POINT-IN-TIME VS ADJUSTED, and why both must exist. An adjusted series is right
for computing returns and wrong for answering "what did this trade at on that
day" — a backtest that uses today's adjusted prices as if they were observable
then is using information from the future. Both are kept, and which one a caller
wants is an explicit choice rather than a default.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class ActionType(str, Enum):
    SPLIT = "split"                  # incl. reverse splits (ratio < 1)
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SPINOFF = "spinoff"
    MERGER = "merger"
    RIGHTS_ISSUE = "rights_issue"
    SYMBOL_CHANGE = "symbol_change"
    DELISTING = "delisting"


class CorporateActionError(ValueError):
    pass


@dataclass(frozen=True)
class CorporateAction:
    instrument_id: str
    action_type: ActionType
    ex_date: date
    # Split/stock-dividend ratio: NEW shares per OLD share. A 2-for-1 is 2.0; a
    # 1-for-10 reverse split is 0.1. Stating the direction matters because the
    # convention is quoted both ways in the wild and inverting it is silent.
    ratio: float = 1.0
    cash_amount: float = 0.0         # per share, in `currency`
    currency: str = "USD"
    pay_date: date | None = None
    announcement_date: date | None = None
    # For spinoffs and mergers: what the holder receives.
    target_instrument_id: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.action_type in (ActionType.SPLIT, ActionType.STOCK_DIVIDEND):
            if self.ratio <= 0:
                raise CorporateActionError(
                    f"split ratio must be positive, got {self.ratio}")
            if self.ratio == 1.0:
                raise CorporateActionError(
                    "a 1:1 split is a no-op; recording it would apply a factor of "
                    "1.0 to history and positions for no reason")
        if self.action_type is ActionType.CASH_DIVIDEND and self.cash_amount <= 0:
            raise CorporateActionError("cash dividend must be positive")
        if self.pay_date and self.pay_date < self.ex_date:
            raise CorporateActionError(
                f"pay date {self.pay_date} precedes ex-date {self.ex_date}: cash "
                f"cannot arrive before the entitlement is fixed")

    @property
    def adjusts_quantity(self) -> bool:
        return self.action_type in (ActionType.SPLIT, ActionType.STOCK_DIVIDEND)

    @property
    def adjusts_price(self) -> bool:
        return self.action_type in (ActionType.SPLIT, ActionType.STOCK_DIVIDEND,
                                    ActionType.CASH_DIVIDEND)


@dataclass
class PositionAdjustment:
    instrument_id: str
    ex_date: date
    action_type: ActionType
    qty_before: float
    qty_after: float
    cost_before: float
    cost_after: float
    cash: float = 0.0                # entitlement, receivable until pay-date
    explanation: str = ""

    @property
    def market_value_preserved(self) -> bool:
        """A split must not change what the position is worth. This is the
        invariant that catches an inverted ratio immediately."""
        return abs(self.qty_before * self.cost_before -
                   self.qty_after * self.cost_after) < 1e-6


class CorporateActionBook:
    def __init__(self) -> None:
        self._by_instrument: dict[str, list[CorporateAction]] = {}

    def add(self, ca: CorporateAction) -> CorporateAction:
        lst = self._by_instrument.setdefault(ca.instrument_id, [])
        # Two quantity-adjusting actions on the same ex-date compound, and the
        # order they are applied in changes the answer. Refuse rather than pick.
        if ca.adjusts_quantity:
            for existing in lst:
                if existing.ex_date == ca.ex_date and existing.adjusts_quantity:
                    raise CorporateActionError(
                        f"{ca.instrument_id} already has a quantity-adjusting "
                        f"action on {ca.ex_date}; two compound and the order "
                        f"changes the result, so this must be booked as one "
                        f"combined ratio")
        lst.append(ca)
        lst.sort(key=lambda a: a.ex_date)
        return ca

    def for_instrument(self, instrument_id: str,
                       start: date | None = None,
                       end: date | None = None) -> list[CorporateAction]:
        out = self._by_instrument.get(instrument_id, [])
        if start:
            out = [a for a in out if a.ex_date >= start]
        if end:
            out = [a for a in out if a.ex_date <= end]
        return out

    def on_date(self, d: date) -> list[CorporateAction]:
        return [a for lst in self._by_instrument.values() for a in lst if a.ex_date == d]

    # ------------------------------------------------------- adjustments
    def adjust_position(self, *, instrument_id: str, qty: float, avg_cost: float,
                        action: CorporateAction) -> PositionAdjustment:
        """Apply one action to a holding.

        Market value is preserved for splits: the position is worth the same
        immediately before and after, because nothing of value happened. Any
        implementation where that does not hold has an inverted ratio.
        """
        if action.instrument_id != instrument_id:
            raise CorporateActionError(
                f"action is for {action.instrument_id}, not {instrument_id}")

        if action.adjusts_quantity:
            new_qty = qty * action.ratio
            # Cost divides by the same ratio the quantity multiplies by. Using
            # the ratio in the same direction for both doubles the book value.
            new_cost = avg_cost / action.ratio if action.ratio else avg_cost
            return PositionAdjustment(
                instrument_id, action.ex_date, action.action_type,
                qty, new_qty, avg_cost, new_cost,
                explanation=(
                    f"{action.ratio}:1 {action.action_type.value} — quantity x{action.ratio}, "
                    f"cost basis /{action.ratio}. Market value unchanged, because "
                    f"a split creates nothing."),
            )

        if action.action_type is ActionType.CASH_DIVIDEND:
            entitlement = qty * action.cash_amount
            # Quantity and cost basis are NOT changed by a cash dividend. The
            # holder receives cash; the shares are the same shares. Reducing the
            # cost basis here is a common error that understates future gains.
            return PositionAdjustment(
                instrument_id, action.ex_date, action.action_type,
                qty, qty, avg_cost, avg_cost, cash=entitlement,
                explanation=(
                    f"{action.cash_amount} {action.currency}/share on {qty} shares "
                    f"= {entitlement:.2f}, receivable until "
                    f"{action.pay_date or 'pay date'}. Quantity and cost basis "
                    f"unchanged — the holder receives cash, not a different "
                    f"position."),
            )

        return PositionAdjustment(
            instrument_id, action.ex_date, action.action_type,
            qty, qty, avg_cost, avg_cost,
            explanation=f"{action.action_type.value} requires manual handling; "
                        f"no automatic adjustment applied.",
        )

    def cumulative_price_factor(self, instrument_id: str, from_date: date,
                                to_date: date | None = None) -> float:
        """Factor to convert a price observed on `from_date` into `to_date` terms.

        This is what makes a return series continuous across a split. Without it
        a 2-for-1 shows as a -50% return on the ex-date, and every volatility,
        VaR, beta and correlation over that window is wrong — silently, because
        the number is plausible.
        """
        factor = 1.0
        for a in self._by_instrument.get(instrument_id, []):
            if a.ex_date <= from_date:
                continue
            if to_date and a.ex_date > to_date:
                continue
            if a.adjusts_quantity:
                factor /= a.ratio
        return factor

    def adjust_price_series(self, instrument_id: str,
                            series: list[tuple[date, float]]) -> list[tuple[date, float]]:
        """Back-adjust a price series so returns are continuous.

        The ADJUSTED series is right for returns and WRONG for "what did it trade
        at that day" — a backtest that treats adjusted prices as observable at
        the time is using information from the future. Both forms must exist and
        the caller picks; this returns the adjusted one explicitly.
        """
        if not series:
            return []
        latest = max(d for d, _ in series)
        return [(d, px * self.cumulative_price_factor(instrument_id, d, latest))
                for d, px in series]

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_instrument.values())
