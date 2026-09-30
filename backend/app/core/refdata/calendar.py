"""Trading calendars, sessions and settlement dates.

A TRADING DAY IS NOT A CALENDAR DAY, and almost every date bug in a trading system
comes from code that assumed it was. Weekends, exchange holidays, half-days, and
the fact that two venues trading the same security observe different holidays —
all of it means "the next day" and "the next trading day" are different questions
with different answers, and only one of them is ever the one you want.

SETTLEMENT IS THE CLASSIC CASE. T+2 does not mean "two days later". It means two
TRADING days later on the SETTLEMENT calendar, which is not necessarily the
trading calendar: a trade on the Thursday before a Friday holiday settles on
Tuesday, not Saturday. Adding a timedelta produces a settlement date that falls on
a closed day, and the failure surfaces days later in an operations queue.

HALF-DAYS ARE NOT HOLIDAYS. The market is open, orders are live, and the close is
early. Treating them as closed loses a real trading session; treating them as
normal leaves algorithms working a schedule against a session that already ended,
which is how a VWAP order finishes 30% unfilled with no error anywhere.

TIMES ARE STORED AS EXCHANGE-LOCAL, and the caller converts. Storing UTC would
require re-deriving local time to answer "is it lunchtime in Tokyo", and the DST
transition would silently move the open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum


class SessionState(str, Enum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"          # orders accepted, no matching
    OPEN = "open"
    AUCTION_OPEN = "auction_open"
    AUCTION_CLOSE = "auction_close"
    POST_CLOSE = "post_close"
    HALTED = "halted"


@dataclass(frozen=True)
class Session:
    open_time: time
    close_time: time
    pre_open: time | None = None
    post_close: time | None = None


@dataclass
class TradingCalendar:
    """One exchange's calendar. Per exchange, never shared: two venues trading
    the same security observe different holidays, so a single global calendar is
    wrong for at least one of them."""

    mic: str                                    # ISO 10383 market identifier
    timezone: str = "America/New_York"
    session: Session = field(default_factory=lambda: Session(time(9, 30), time(16, 0),
                                                             time(4, 0), time(20, 0)))
    holidays: set[date] = field(default_factory=set)
    # Half-days map to their EARLY CLOSE. The market is open, so this cannot be
    # folded into the holiday set.
    half_days: dict[date, time] = field(default_factory=dict)
    weekend: tuple[int, ...] = (5, 6)           # Sat, Sun as weekday() indices

    # ------------------------------------------------------------ is open
    def is_trading_day(self, d: date) -> bool:
        if d.weekday() in self.weekend:
            return False
        return d not in self.holidays

    def close_time_on(self, d: date) -> time | None:
        if not self.is_trading_day(d):
            return None
        return self.half_days.get(d, self.session.close_time)

    def is_half_day(self, d: date) -> bool:
        return d in self.half_days and self.is_trading_day(d)

    def state_at(self, dt: datetime) -> SessionState:
        """Session state at an exchange-local datetime."""
        d, t = dt.date(), dt.time()
        if not self.is_trading_day(d):
            return SessionState.CLOSED
        close = self.close_time_on(d) or self.session.close_time
        if self.session.pre_open and self.session.pre_open <= t < self.session.open_time:
            return SessionState.PRE_OPEN
        if self.session.open_time <= t < close:
            return SessionState.OPEN
        if self.session.post_close and close <= t < self.session.post_close:
            return SessionState.POST_CLOSE
        return SessionState.CLOSED

    def is_open(self, dt: datetime) -> bool:
        return self.state_at(dt) is SessionState.OPEN

    # ------------------------------------------------------- date walking
    def next_trading_day(self, d: date, n: int = 1) -> date:
        """n trading days forward. NOT d + timedelta(n): that lands on weekends
        and holidays, and every downstream date derived from it is then wrong."""
        if n < 1:
            raise ValueError("n must be at least 1; use previous_trading_day to go back")
        cur, remaining = d, n
        # Bounded so a calendar with a pathological holiday set fails loudly
        # rather than spinning forever.
        for _ in range(n * 10 + 400):
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                remaining -= 1
                if remaining == 0:
                    return cur
        raise ValueError(f"no trading day found within a sane window after {d}")

    def previous_trading_day(self, d: date, n: int = 1) -> date:
        if n < 1:
            raise ValueError("n must be at least 1")
        cur, remaining = d, n
        for _ in range(n * 10 + 400):
            cur -= timedelta(days=1)
            if self.is_trading_day(cur):
                remaining -= 1
                if remaining == 0:
                    return cur
        raise ValueError(f"no trading day found within a sane window before {d}")

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """Inclusive of both ends where they are trading days."""
        if end < start:
            return []
        out, cur = [], start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def settlement_date(self, trade_date: date, offset: int = 2,
                        settlement_calendar: "TradingCalendar | None" = None) -> date:
        """T+n settlement.

        The settlement calendar may differ from the trading one — a security can
        trade on a day its settlement system is closed. Defaults to this calendar
        because that is the common case, but the parameter exists because the
        assumption is not always safe.
        """
        cal = settlement_calendar or self
        if offset == 0:
            return trade_date
        return cal.next_trading_day(trade_date, offset)


# ------------------------------------------------------------- US equities
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """nth given weekday of a month; n negative counts from the end."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    d -= timedelta(days=(d.weekday() - weekday) % 7)
    return d + timedelta(weeks=n + 1)


def _observed(d: date) -> date:
    """US rule: a holiday on Saturday is observed Friday, on Sunday the Monday.
    Omitting this puts several holidays a year on the wrong date."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm. Needed for Good Friday, which is the only
    US equity holiday that moves with the lunar calendar."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (2 * e + 2 * i - h - k + 32) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    day = (h + l - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def us_equity_holidays(year: int) -> set[date]:
    """NYSE/Nasdaq holidays for one year, computed rather than hardcoded so the
    calendar does not silently expire at the end of a hardcoded table."""
    h = {
        _observed(date(year, 1, 1)),                       # New Year's Day
        _nth_weekday(year, 1, 0, 3),                       # MLK, 3rd Monday Jan
        _nth_weekday(year, 2, 0, 3),                       # Presidents', 3rd Mon Feb
        _easter(year) - timedelta(days=2),                 # Good Friday
        _nth_weekday(year, 5, 0, -1),                      # Memorial, last Mon May
        _observed(date(year, 7, 4)),                       # Independence Day
        _nth_weekday(year, 9, 0, 1),                       # Labor, 1st Mon Sep
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving, 4th Thu
        _observed(date(year, 12, 25)),                     # Christmas
    }
    if year >= 2022:
        h.add(_observed(date(year, 6, 19)))                # Juneteenth, from 2022
    return h


def us_equity_half_days(year: int) -> dict[date, time]:
    """1pm closes: the day after Thanksgiving, and Christmas Eve when it falls on
    a weekday. The market is OPEN on these — treating them as holidays loses a
    real session."""
    out: dict[date, time] = {}
    out[_nth_weekday(year, 11, 3, 4) + timedelta(days=1)] = time(13, 0)
    xmas_eve = date(year, 12, 24)
    if xmas_eve.weekday() < 5:
        out[xmas_eve] = time(13, 0)
    july3 = date(year, 7, 3)
    if july3.weekday() < 5:
        out[july3] = time(13, 0)
    return out


def us_equity_calendar(years: range | None = None) -> TradingCalendar:
    years = years or range(2020, 2036)
    cal = TradingCalendar(mic="XNYS", timezone="America/New_York")
    for y in years:
        cal.holidays |= us_equity_holidays(y)
        cal.half_days.update(us_equity_half_days(y))
    # A half-day that is also a holiday is a holiday: Christmas Eve on a Friday
    # when the 25th falls on Saturday is observed closed, not a short session.
    for d in list(cal.half_days):
        if d in cal.holidays:
            del cal.half_days[d]
    return cal


def crypto_calendar() -> TradingCalendar:
    """24/7: no weekend, no holidays. Present so crypto instruments resolve
    through the same interface rather than needing a special case at every call
    site — a calendar that says 'always open' is still a calendar."""
    return TradingCalendar(
        mic="CRYPTO", timezone="UTC",
        session=Session(time(0, 0), time(23, 59, 59)),
        weekend=(),
    )
