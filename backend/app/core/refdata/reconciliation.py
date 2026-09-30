"""Reconciliation: positions, cash and trades against an external record.

"THEY DIFFER" IS NOT A RECONCILIATION RESULT. It is the start of one. A break
report that says two numbers disagree leaves someone to work out *why*, and the
why is almost always one of a small number of causes that can be told apart
mechanically. Classifying them is the entire value:

  MISSING_HERE   — they have it, we do not. Usually a fill we never received, or
                   a drop-copy gap. The dangerous one: we are short a position we
                   actually hold.
  MISSING_THERE  — we have it, they do not. Usually a trade we booked that never
                   reached them, or one booked twice on our side.
  QUANTITY       — both have it, sizes differ. Often a partial fill applied once
                   on one side and twice on the other.
  PRICE          — sizes agree, prices do not. Usually a fee or commission booked
                   into the price on one side only.
  TIMING         — a trade that exists on both sides but on different DATES,
                   which is not a break at all: it is a cutoff difference and it
                   resolves itself tomorrow. Flagging it as a real break is how a
                   recon report gets ignored.
  SIGN           — same magnitude, opposite direction. A long/short convention
                   mismatch, and the most expensive to miss because the net error
                   is twice the position.

TOLERANCE IS PER FIELD AND ABSOLUTE, NOT RELATIVE. A 0.01% tolerance on a
10,000,000-share position permits a 1,000-share break. Quantity is compared to a
tiny absolute epsilon because share counts are integers in all but name; prices
get a tolerance sized to the instrument's tick.

EVERY BREAK IS REPORTED. There is no "minor" bucket that gets hidden by default —
a recon that suppresses small breaks is one where a systematic small error goes
unnoticed until it accumulates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class BreakType(str, Enum):
    MISSING_HERE = "missing_here"
    MISSING_THERE = "missing_there"
    QUANTITY = "quantity"
    PRICE = "price"
    TIMING = "timing"
    SIGN = "sign"


class Severity(str, Enum):
    CRITICAL = "critical"    # position or cash is wrong now
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"            # expected, e.g. a cutoff difference


@dataclass(frozen=True)
class Holding:
    key: str                 # instrument id or account
    qty: float
    price: float = 0.0
    trade_date: date | None = None


@dataclass
class Break:
    break_type: BreakType
    key: str
    severity: Severity
    ours: float | None
    theirs: float | None
    difference: float
    explanation: str
    suggested_action: str = ""

    def to_dict(self) -> dict:
        return {
            "break_type": self.break_type.value,
            "key": self.key,
            "severity": self.severity.value,
            "ours": self.ours,
            "theirs": self.theirs,
            "difference": self.difference,
            "explanation": self.explanation,
            "suggested_action": self.suggested_action,
        }


@dataclass
class ReconResult:
    matched: int = 0
    breaks: list[Break] = field(default_factory=list)
    as_of: date | None = None

    @property
    def clean(self) -> bool:
        # INFO-level differences are expected and do not make a recon dirty --
        # otherwise every cutoff difference makes the report red and people stop
        # reading it.
        return not any(b.severity is not Severity.INFO for b in self.breaks)

    def by_severity(self, s: Severity) -> list[Break]:
        return [b for b in self.breaks if b.severity is s]

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "matched": self.matched,
            "break_count": len(self.breaks),
            "clean": self.clean,
            "breaks": [b.to_dict() for b in self.breaks],
            "by_severity": {s.value: len(self.by_severity(s)) for s in Severity},
            "note": ("Positions agree." if self.clean else
                     "Breaks found. The external record is authoritative for "
                     "settled positions; ours is authoritative for intraday "
                     "activity they have not seen yet."),
        }


QTY_EPSILON = 1e-6          # share counts are integers in all but name
DEFAULT_PRICE_TOLERANCE = 0.005


def reconcile_positions(ours: dict[str, float], theirs: dict[str, float], *,
                        as_of: date | None = None,
                        qty_epsilon: float = QTY_EPSILON) -> ReconResult:
    """Position-level reconciliation with classified breaks."""
    res = ReconResult(as_of=as_of)
    for key in sorted(set(ours) | set(theirs)):
        raw_a, raw_b = ours.get(key), theirs.get(key)
        # A key absent on one side and an explicit 0.0 on the other are the same
        # fact -- flat. Brokers routinely report a closed position as 0.0 rather
        # than dropping the row, so this is the normal end state of every
        # position that was ever flattened, not an edge case. Normalising here
        # keeps the comparisons below arithmetic rather than three-valued.
        a = 0.0 if raw_a is None else raw_a
        b = 0.0 if raw_b is None else raw_b
        a_flat, b_flat = abs(a) < qty_epsilon, abs(b) < qty_epsilon

        if a_flat and b_flat:
            res.matched += 1
            continue

        if a_flat:
            res.breaks.append(Break(
                BreakType.MISSING_HERE, key, Severity.CRITICAL, raw_a, raw_b, -b,
                "They hold a position we do not. Most often a fill we never "
                "received or a drop-copy gap -- we are short a position we "
                "actually hold, so market risk is understated.",
                "Check the drop-copy for missing executions before adjusting."))
            continue
        if b_flat:
            res.breaks.append(Break(
                BreakType.MISSING_THERE, key, Severity.HIGH, raw_a, raw_b, a,
                "We hold a position they do not. Usually a trade booked here "
                "that never reached them, or one booked twice on our side.",
                "Confirm the trade reached the venue before assuming they are wrong."))
            continue

        diff = a - b
        if abs(diff) < qty_epsilon:
            res.matched += 1
            continue

        # Same magnitude, opposite sign: a convention mismatch, and the net error
        # is twice the position rather than the difference it appears to be.
        if abs(a + b) < qty_epsilon:
            res.breaks.append(Break(
                BreakType.SIGN, key, Severity.CRITICAL, a, b, diff,
                f"Equal magnitude, opposite direction ({a} vs {b}). A long/short "
                f"convention mismatch: the true error is {abs(diff)}, twice the "
                f"position, not the difference it looks like.",
                "Check the sign convention on the feed before netting."))
            continue

        res.breaks.append(Break(
            BreakType.QUANTITY, key, Severity.HIGH, a, b, diff,
            f"Quantities differ by {diff}. Often a partial fill applied once on "
            f"one side and twice on the other.",
            "Compare the execution list for this instrument, not just the total."))
    return res


def reconcile_cash(ours: dict[str, float], theirs: dict[str, float], *,
                   as_of: date | None = None,
                   tolerance: float = 0.005) -> ReconResult:
    """Cash reconciliation, by currency.

    Tolerance is a HALF-CENT, not a percentage: a percentage tolerance on a large
    balance permits a real amount of money, and cash is the one place where the
    numbers should agree exactly.
    """
    res = ReconResult(as_of=as_of)
    for ccy in sorted(set(ours) | set(theirs)):
        a, b = ours.get(ccy, 0.0), theirs.get(ccy, 0.0)
        diff = a - b
        if abs(diff) <= tolerance:
            res.matched += 1
            continue
        res.breaks.append(Break(
            BreakType.QUANTITY, ccy, Severity.CRITICAL, a, b, diff,
            f"Cash differs by {diff:.2f} {ccy}. Common causes: a dividend booked "
            f"on the ex-date instead of the pay-date, fees applied on one side "
            f"only, or an FX rate difference on a conversion.",
            "Check for entitlements booked on the wrong date before adjusting."))
    return res


def reconcile_trades(ours: list[Holding], theirs: list[Holding], *,
                     as_of: date | None = None,
                     price_tolerance: float = DEFAULT_PRICE_TOLERANCE,
                     allow_timing: bool = True) -> ReconResult:
    """Trade-level reconciliation, which can see causes a position recon cannot.

    Two errors that net to zero at position level are invisible there and visible
    here, which is why both exist rather than one.
    """
    res = ReconResult(as_of=as_of)
    ours_by = {h.key: h for h in ours}
    theirs_by = {h.key: h for h in theirs}

    for key in sorted(set(ours_by) | set(theirs_by)):
        a, b = ours_by.get(key), theirs_by.get(key)
        if a is None:
            res.breaks.append(Break(
                BreakType.MISSING_HERE, key, Severity.CRITICAL, None, b.qty, -b.qty,
                "Trade present on their side only.",
                "Check for a missed execution report."))
            continue
        if b is None:
            res.breaks.append(Break(
                BreakType.MISSING_THERE, key, Severity.HIGH, a.qty, None, a.qty,
                "Trade present on our side only.",
                "Confirm it reached the venue."))
            continue

        # Dates differing with everything else matching is a cutoff difference,
        # not a break. Reporting it as one is how a recon report loses its
        # audience.
        if (allow_timing and a.trade_date and b.trade_date
                and a.trade_date != b.trade_date
                and abs(a.qty - b.qty) < QTY_EPSILON):
            res.breaks.append(Break(
                BreakType.TIMING, key, Severity.INFO, a.qty, b.qty, 0.0,
                f"Same trade, different booking date ({a.trade_date} vs "
                f"{b.trade_date}). A cutoff difference, not an error -- it "
                f"resolves itself on the next cycle.",
                "No action; confirm it clears tomorrow."))
            continue

        if abs(a.qty - b.qty) >= QTY_EPSILON:
            res.breaks.append(Break(
                BreakType.QUANTITY, key, Severity.HIGH, a.qty, b.qty, a.qty - b.qty,
                f"Trade quantities differ by {a.qty - b.qty}.",
                "Check for a partial fill applied twice."))
            continue

        if abs(a.price - b.price) > price_tolerance:
            res.breaks.append(Break(
                BreakType.PRICE, key, Severity.MEDIUM, a.price, b.price,
                a.price - b.price,
                f"Prices differ by {a.price - b.price:.4f}. Usually a fee or "
                f"commission folded into the price on one side only.",
                "Compare gross vs net price conventions."))
            continue

        res.matched += 1
    return res
