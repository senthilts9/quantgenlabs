"""Start-of-day and end-of-day processing.

THE ORDER OF THESE STEPS IS THE CORRECTNESS. Every step here is individually
obvious and the sequence is not, and getting the sequence wrong produces numbers
that are wrong by a plausible amount:

  Apply corporate actions BEFORE marking. A 2-for-1 split on AAPL leaves 200
  shares at a 100 mark. Mark first and you value 100 shares at 100: the position
  is understated by exactly half and nothing errors, because both numbers are
  numbers that AAPL has really had.

  Mark BEFORE computing P&L, obviously, but also mark before reconciling: a recon
  against yesterday's marks reports price breaks that are only a stale mark.

  Reconcile BEFORE rolling. Rolling forward is what makes today's close into
  tomorrow's open, and rolling an unreconciled position carries the break into a
  new day where its cause is a day further away.

  Roll LAST, and only once.

IDEMPOTENCY IS NOT OPTIONAL. An EOD run that is triggered twice — a retry after a
timeout, an operator who did not see the first one finish — must not apply the
split twice or book the dividend twice. Every run is keyed by (business_date,
phase) and a repeat is refused rather than silently redone. That refusal is the
feature: a double-applied 2-for-1 gives a 4x position, which looks like a trading
error and gets investigated as one.

BUSINESS DATE IS NOT wall-clock date. A cycle for Friday can run at 02:00 on
Saturday, and everything it stamps must say Friday. Every date here is passed in
explicitly; nothing calls date.today().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .calendar import TradingCalendar
from .corporate_actions import CorporateActionBook, PositionAdjustment
from .reconciliation import ReconResult, Severity, reconcile_positions


class Phase(str, Enum):
    SOD = "sod"
    EOD = "eod"


class DailyCycleError(RuntimeError):
    pass


class AlreadyRun(DailyCycleError):
    """A phase was run twice for one business date. Refused, not repeated."""


@dataclass
class Position:
    instrument_id: str
    qty: float
    avg_cost: float
    mark: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.mark

    @property
    def unrealised(self) -> float:
        return self.qty * (self.mark - self.avg_cost)

    def to_dict(self) -> dict:
        return {"instrument_id": self.instrument_id, "qty": self.qty,
                "avg_cost": self.avg_cost, "mark": self.mark,
                "market_value": self.market_value, "unrealised": self.unrealised}


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok, "detail": self.detail, **self.data}


@dataclass
class CycleResult:
    business_date: date
    phase: Phase
    steps: list[StepResult] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    adjustments: list[PositionAdjustment] = field(default_factory=list)
    recon: ReconResult | None = None
    cash_entitlements: float = 0.0

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def total_market_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def total_unrealised(self) -> float:
        return sum(p.unrealised for p in self.positions)

    def to_dict(self) -> dict:
        return {
            "business_date": self.business_date.isoformat(),
            "phase": self.phase.value,
            "ok": self.ok,
            "steps": [s.to_dict() for s in self.steps],
            "positions": [p.to_dict() for p in self.positions],
            "adjustments": [
                {"instrument_id": a.instrument_id, "action": a.action_type.value,
                 "qty_before": a.qty_before, "qty_after": a.qty_after,
                 "cost_before": a.cost_before, "cost_after": a.cost_after,
                 "cash": a.cash, "market_value_preserved": a.market_value_preserved,
                 "explanation": a.explanation}
                for a in self.adjustments],
            "recon": self.recon.to_dict() if self.recon else None,
            "cash_entitlements": self.cash_entitlements,
            "total_market_value": self.total_market_value,
            "total_unrealised": self.total_unrealised,
            "simulated": True,
            "note": "Simulated batch cycle. No money moves and no external "
                    "system is updated.",
        }


class DailyCycle:
    """Runs SOD and EOD for a book of positions.

    Holds the run log so a repeat can be refused. In a deployed system that log
    lives in the database, for the same reason it lives here: the answer to "has
    today's EOD run" must survive the process that ran it.
    """

    def __init__(self, calendar: TradingCalendar,
                 ca_book: CorporateActionBook | None = None) -> None:
        self.calendar = calendar
        self.ca_book = ca_book or CorporateActionBook()
        self._runs: dict[tuple[date, Phase], CycleResult] = {}

    # ---------------------------------------------------------------- guards
    def has_run(self, business_date: date, phase: Phase) -> bool:
        return (business_date, phase) in self._runs

    def result_for(self, business_date: date, phase: Phase) -> CycleResult | None:
        return self._runs.get((business_date, phase))

    def _claim(self, business_date: date, phase: Phase) -> None:
        if self.has_run(business_date, phase):
            raise AlreadyRun(
                f"{phase.value.upper()} has already run for {business_date}. "
                f"Refusing to run it again: re-applying a split or a dividend "
                f"would change positions by a plausible-looking amount. Use the "
                f"stored result, or reverse the run explicitly first.")
        if not self.calendar.is_trading_day(business_date):
            raise DailyCycleError(
                f"{business_date} is not a trading day on {self.calendar.mic}. A "
                f"cycle on a closed day would mark positions against prices that "
                f"do not exist.")

    # ------------------------------------------------------------------- SOD
    def run_sod(self, business_date: date, positions: list[Position], *,
                external_positions: dict[str, float] | None = None) -> CycleResult:
        """Start of day: apply the day's corporate actions, then reconcile.

        Corporate actions are applied on their EX-DATE, which is the first day the
        security trades without the entitlement -- so this belongs at SOD, before
        any order is accepted against a quantity that is about to change.
        """
        self._claim(business_date, Phase.SOD)
        res = CycleResult(business_date, Phase.SOD,
                          positions=[Position(p.instrument_id, p.qty, p.avg_cost, p.mark)
                                     for p in positions])

        actions = self.ca_book.on_date(business_date)
        by_id = {p.instrument_id: p for p in res.positions}
        applied = 0
        for a in actions:
            pos = by_id.get(a.instrument_id)
            if pos is None:
                continue          # no holding, nothing to adjust
            adj = self.ca_book.adjust_position(
                instrument_id=pos.instrument_id, qty=pos.qty,
                avg_cost=pos.avg_cost, action=a)
            if a.adjusts_quantity and not adj.market_value_preserved:
                res.steps.append(StepResult(
                    "corporate_actions", False,
                    f"{a.instrument_id}: market value not preserved by a "
                    f"{a.ratio}:1 {a.action_type.value}. The ratio is inverted -- "
                    f"refusing to apply it.",
                    {"instrument_id": a.instrument_id}))
                return self._store(res)
            pos.qty, pos.avg_cost = adj.qty_after, adj.cost_after
            # A split changes the price the same way it changes the quantity, so
            # the mark has to move too -- otherwise the position doubles in value
            # for the rest of the day.
            if a.adjusts_quantity and pos.mark and a.ratio:
                pos.mark /= a.ratio
            res.adjustments.append(adj)
            res.cash_entitlements += adj.cash
            applied += 1

        res.steps.append(StepResult(
            "corporate_actions", True,
            f"{applied} of {len(actions)} action(s) on {business_date} applied to "
            f"held positions." if actions else
            f"No corporate actions with an ex-date of {business_date}.",
            {"applied": applied, "entitlements": res.cash_entitlements}))

        if external_positions is not None:
            res.recon = reconcile_positions(
                {p.instrument_id: p.qty for p in res.positions},
                external_positions, as_of=business_date)
            crit = len(res.recon.by_severity(Severity.CRITICAL))
            res.steps.append(StepResult(
                "reconciliation", res.recon.clean,
                f"{res.recon.matched} matched, {len(res.recon.breaks)} break(s), "
                f"{crit} critical.",
                {"break_count": len(res.recon.breaks), "critical": crit}))
        else:
            res.steps.append(StepResult(
                "reconciliation", True,
                "Skipped: no external record supplied. A skipped recon is "
                "reported, not omitted -- a cycle with no recon is not a clean "
                "cycle, it is an unchecked one."))

        return self._store(res)

    # ------------------------------------------------------------------- EOD
    def run_eod(self, business_date: date, positions: list[Position],
                marks: dict[str, float], *,
                external_positions: dict[str, float] | None = None,
                require_full_marks: bool = True) -> CycleResult:
        """End of day: mark, value, reconcile, then roll.

        A missing mark is an error by default. Carrying yesterday's mark forward
        silently is how a position in a halted name keeps reporting a stale P&L
        for weeks -- the number moves with the quantity, so it looks alive.
        """
        self._claim(business_date, Phase.EOD)
        res = CycleResult(business_date, Phase.EOD,
                          positions=[Position(p.instrument_id, p.qty, p.avg_cost, p.mark)
                                     for p in positions])

        missing = [p.instrument_id for p in res.positions
                   if p.instrument_id not in marks]
        if missing and require_full_marks:
            res.steps.append(StepResult(
                "marking", False,
                f"No closing mark for {', '.join(sorted(missing))}. Refusing to "
                f"carry the previous mark forward: a stale mark reports a P&L "
                f"that looks alive because it moves with the quantity.",
                {"missing": sorted(missing)}))
            return self._store(res)

        for p in res.positions:
            if p.instrument_id in marks:
                p.mark = marks[p.instrument_id]
        res.steps.append(StepResult(
            "marking", True,
            f"{len(res.positions) - len(missing)} of {len(res.positions)} "
            f"position(s) marked." +
            (f" Stale marks retained for {', '.join(sorted(missing))} because "
             f"require_full_marks was disabled." if missing else ""),
            {"marked": len(res.positions) - len(missing), "stale": sorted(missing)}))

        res.steps.append(StepResult(
            "valuation", True,
            f"Market value {res.total_market_value:,.2f}, unrealised "
            f"{res.total_unrealised:,.2f}.",
            {"market_value": res.total_market_value,
             "unrealised": res.total_unrealised}))

        if external_positions is not None:
            res.recon = reconcile_positions(
                {p.instrument_id: p.qty for p in res.positions},
                external_positions, as_of=business_date)
            crit = len(res.recon.by_severity(Severity.CRITICAL))
            res.steps.append(StepResult(
                "reconciliation", res.recon.clean,
                f"{res.recon.matched} matched, {len(res.recon.breaks)} break(s), "
                f"{crit} critical.",
                {"break_count": len(res.recon.breaks), "critical": crit}))
        else:
            res.steps.append(StepResult(
                "reconciliation", True,
                "Skipped: no external record supplied."))

        nxt = self.calendar.next_trading_day(business_date)
        res.steps.append(StepResult(
            "roll", True,
            f"Next business date {nxt}" +
            (f" (early close {self.calendar.close_time_on(nxt)})"
             if self.calendar.is_half_day(nxt) else "") + ".",
            {"next_business_date": nxt.isoformat(),
             "next_is_half_day": self.calendar.is_half_day(nxt),
             "settlement_date": self.calendar.settlement_date(business_date).isoformat()}))

        return self._store(res)

    def _store(self, res: CycleResult) -> CycleResult:
        # Stored whether it succeeded or not. A failed run must still block a
        # blind retry -- a retry that reruns the successful steps before hitting
        # the same failure double-applies them.
        self._runs[(res.business_date, res.phase)] = res
        return res

    def reverse(self, business_date: date, phase: Phase) -> bool:
        """Explicitly un-claim a run so it can be repeated.

        Exists so the idempotency guard is recoverable rather than a dead end, and
        is separate from running so that repeating a cycle is always a deliberate
        act with a name attached.
        """
        return self._runs.pop((business_date, phase), None) is not None

    def history(self) -> list[dict]:
        return [{"business_date": d.isoformat(), "phase": p.value,
                 "ok": r.ok, "steps": len(r.steps)}
                for (d, p), r in sorted(self._runs.items(), key=lambda kv: (kv[0][0], kv[0][1].value))]
