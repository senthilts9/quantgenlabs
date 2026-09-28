"""Rejection taxonomy.

Every way an order can be refused before it reaches a matching engine gets a
stable machine-readable code. Codes matter more than messages: an execution
system decides *what to do next* from them. `POST_ONLY_WOULD_CROSS` means
reprice and retry; `INSUFFICIENT_MARGIN` means stop and alert; `RATE_LIMITED`
means back off. Free-text messages cannot be branched on safely.

Codes are grouped by the stage that produces them, which is also the order the
validation pipeline runs in - cheap structural checks before anything that
needs market data or account state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class Stage(str, Enum):
    """Which validation stage produced a rejection."""

    SCHEMA = "schema"          # malformed request, no market data needed
    INSTRUMENT = "instrument"  # against the market's own trading rules
    MARKET = "market"          # needs a live mark/book (price bands, crossing)
    ACCOUNT = "account"        # needs position/balance (margin, reduce-only)
    AUTH = "auth"              # signature, nonce, expiry
    THROTTLE = "throttle"      # rate and order-count limits
    RISK = "risk"              # operator-imposed limits and kill switches


class Code(str, Enum):
    """Stable rejection codes.

    String-valued so they serialise straight to JSON and survive a round trip
    through a queue or log without a lookup table.
    """

    # -- schema -----------------------------------------------------------
    MARKET_NOT_FOUND = "MARKET_NOT_FOUND"
    # INVALID_SIDE and INVALID_ORDER_TYPE are deliberately absent: Side(...)
    # and OrderType(...) raise at construction, so an OrderRequest carrying a
    # bad one cannot exist to be validated. Codes that no rule can emit are
    # worse than no codes - they imply a check that is not happening.
    INVALID_TIME_IN_FORCE = "INVALID_TIME_IN_FORCE"
    PRICE_NOT_POSITIVE = "PRICE_NOT_POSITIVE"
    SIZE_NOT_POSITIVE = "SIZE_NOT_POSITIVE"
    LIMIT_REQUIRES_PRICE = "LIMIT_REQUIRES_PRICE"
    MARKET_FORBIDS_PRICE = "MARKET_FORBIDS_PRICE"
    NON_FINITE_NUMBER = "NON_FINITE_NUMBER"
    PRECISION_OVERFLOW = "PRECISION_OVERFLOW"

    # -- instrument -------------------------------------------------------
    MARKET_NOT_TRADEABLE = "MARKET_NOT_TRADEABLE"
    MARKET_REDUCE_ONLY = "MARKET_REDUCE_ONLY"
    MARKET_POST_ONLY = "MARKET_POST_ONLY"
    PRICE_NOT_MULTIPLE_OF_TICK = "PRICE_NOT_MULTIPLE_OF_TICK"
    SIZE_NOT_MULTIPLE_OF_STEP = "SIZE_NOT_MULTIPLE_OF_STEP"
    BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
    ABOVE_MAX_SIZE = "ABOVE_MAX_SIZE"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    ABOVE_MAX_NOTIONAL = "ABOVE_MAX_NOTIONAL"
    UNSUPPORTED_ORDER_TYPE_FOR_MARKET = "UNSUPPORTED_ORDER_TYPE_FOR_MARKET"

    # -- market -----------------------------------------------------------
    PRICE_BAND_EXCEEDED = "PRICE_BAND_EXCEEDED"
    STALE_MARK_PRICE = "STALE_MARK_PRICE"
    NO_MARK_PRICE = "NO_MARK_PRICE"
    POST_ONLY_WOULD_CROSS = "POST_ONLY_WOULD_CROSS"
    MARKET_ORDER_SLIPPAGE_EXCEEDED = "MARKET_ORDER_SLIPPAGE_EXCEEDED"
    CROSSED_BOOK = "CROSSED_BOOK"
    EMPTY_BOOK = "EMPTY_BOOK"

    # -- account ----------------------------------------------------------
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    LEVERAGE_EXCEEDED = "LEVERAGE_EXCEEDED"
    POSITION_LIMIT_EXCEEDED = "POSITION_LIMIT_EXCEEDED"
    REDUCE_ONLY_NO_POSITION = "REDUCE_ONLY_NO_POSITION"
    REDUCE_ONLY_WOULD_INCREASE = "REDUCE_ONLY_WOULD_INCREASE"
    REDUCE_ONLY_WOULD_FLIP = "REDUCE_ONLY_WOULD_FLIP"
    SELF_TRADE_PREVENTED = "SELF_TRADE_PREVENTED"
    ACCOUNT_NOT_ONBOARDED = "ACCOUNT_NOT_ONBOARDED"
    ACCOUNT_LIQUIDATING = "ACCOUNT_LIQUIDATING"
    #: The account is already below its maintenance requirement. Distinct from
    #: INSUFFICIENT_MARGIN, which is about affording a *new* position - this
    #: says the existing book is underwater and only de-risking is allowed.
    BELOW_MAINTENANCE_MARGIN = "BELOW_MAINTENANCE_MARGIN"
    #: The order would leave the resulting position liquidatable by a move
    #: smaller than the operator's tolerance.
    LIQUIDATION_TOO_CLOSE = "LIQUIDATION_TOO_CLOSE"

    # -- auth -------------------------------------------------------------
    SIGNATURE_MISSING = "SIGNATURE_MISSING"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    SIGNATURE_EXPIRED = "SIGNATURE_EXPIRED"
    SIGNATURE_TIMESTAMP_IN_FUTURE = "SIGNATURE_TIMESTAMP_IN_FUTURE"
    NONCE_REUSED = "NONCE_REUSED"
    DUPLICATE_CLIENT_ORDER_ID = "DUPLICATE_CLIENT_ORDER_ID"
    JWT_EXPIRED = "JWT_EXPIRED"

    # -- throttle ---------------------------------------------------------
    RATE_LIMITED = "RATE_LIMITED"
    MAX_OPEN_ORDERS = "MAX_OPEN_ORDERS"

    # -- risk -------------------------------------------------------------
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    ORDER_NOTIONAL_LIMIT = "ORDER_NOTIONAL_LIMIT"
    VENUE_NOT_ALLOWED = "VENUE_NOT_ALLOWED"


#: Codes where the correct client response is "adjust and resend", as opposed
#: to "stop and escalate". The OMS uses this to decide on auto-retry.
RETRYABLE: frozenset = frozenset({
    Code.POST_ONLY_WOULD_CROSS,
    Code.RATE_LIMITED,
    Code.STALE_MARK_PRICE,
    Code.NONCE_REUSED,
    Code.JWT_EXPIRED,
})


@dataclass(frozen=True)
class Rejection:
    """A single reason an order was refused."""

    code: Code
    stage: Stage
    message: str
    #: Machine-usable context - the limit that was breached and the value seen.
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def __str__(self) -> str:
        return "[%s/%s] %s" % (self.stage.value, self.code.value, self.message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "stage": self.stage.value,
            "message": self.message,
            "detail": self.detail,
            "retryable": self.retryable,
        }


@dataclass
class ValidationReport:
    """Outcome of running an order through the validation pipeline.

    Collects *every* failure by default rather than stopping at the first.
    A caller fixing up an order wants the full list - discovering a bad tick
    size, fixing it, then discovering a bad step size costs a round trip each
    time. `strict=True` on the engine short-circuits when that cost does not
    apply (hot path, or a venue that only reports one reason anyway).
    """

    rejections: List[Rejection] = field(default_factory=list)
    #: Non-fatal observations - e.g. an order that passed but sits far from mid.
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejections

    @property
    def codes(self) -> List[Code]:
        return [r.code for r in self.rejections]

    def add(self, rejection: Rejection) -> None:
        self.rejections.append(rejection)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def raise_if_rejected(self) -> None:
        if self.rejections:
            raise OrderRejected(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "rejections": [r.to_dict() for r in self.rejections],
            "warnings": list(self.warnings),
        }

    def __str__(self) -> str:
        if self.ok:
            return "ACCEPTED" + (
                " (%d warning(s))" % len(self.warnings) if self.warnings else ""
            )
        return "REJECTED: " + "; ".join(str(r) for r in self.rejections)


class OrderRejected(Exception):
    """Raised by `ValidationReport.raise_if_rejected`."""

    def __init__(self, report: ValidationReport):
        super().__init__(str(report))
        self.report = report
