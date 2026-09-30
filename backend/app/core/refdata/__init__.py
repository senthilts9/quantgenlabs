"""Reference data: the identifiers, calendars and adjustments everything else assumes.

This layer is boring and load-bearing. Every other subsystem in the platform
already depends on the answers it gives -- what instrument is this, was the market
open, is the quantity still the quantity -- and before this package those answers
were implicit in whatever the caller happened to pass. Making them explicit is the
point:

  symbology          a ticker is not an identifier, and resolution is as-of a date
  calendar           a trading day is not a calendar day, and T+2 is not +2 days
  corporate_actions  adjust positions and price history together, or neither
  reconciliation     "they differ" is not a result; the classified cause is
  daily              the order of SOD/EOD steps is the correctness

Nothing here reaches an external vendor. The security master and calendars are
loaded in-process from computed rules and explicit fixtures, which is why the US
holiday set is derived rather than tabulated -- a hardcoded table expires and the
expiry is silent.
"""
from .calendar import (Session, SessionState, TradingCalendar, crypto_calendar,
                       us_equity_calendar, us_equity_half_days,
                       us_equity_holidays)
from .corporate_actions import (ActionType, CorporateAction, CorporateActionBook,
                                CorporateActionError, PositionAdjustment)
from .daily import (AlreadyRun, CycleResult, DailyCycle, DailyCycleError, Phase,
                    Position, StepResult)
from .reconciliation import (Break, BreakType, Holding, ReconResult, Severity,
                             reconcile_cash, reconcile_positions,
                             reconcile_trades)
from .symbology import (OPEN_ENDED, Alias, AmbiguousSymbol, AssetClass,
                        IdType, Instrument, SecurityMaster, SymbologyError,
                        valid_cusip, valid_isin)

__all__ = [
    "Alias", "AlreadyRun", "AmbiguousSymbol", "ActionType", "AssetClass",
    "Break", "BreakType", "CorporateAction", "CorporateActionBook",
    "CorporateActionError", "CycleResult", "DailyCycle", "DailyCycleError",
    "Holding", "IdType", "Instrument", "OPEN_ENDED", "Phase", "Position",
    "PositionAdjustment", "ReconResult", "SecurityMaster", "Session",
    "SessionState", "Severity", "StepResult", "SymbologyError",
    "TradingCalendar", "crypto_calendar", "reconcile_cash",
    "reconcile_positions", "reconcile_trades", "us_equity_calendar",
    "us_equity_half_days", "us_equity_holidays", "valid_cusip", "valid_isin",
]
