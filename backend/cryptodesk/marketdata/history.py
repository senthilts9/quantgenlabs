"""Historical candles, and the return series a risk engine can consume.

A VaR number is a statement about a distribution, and the distribution comes
from this file. Everything downstream -- EVT tail fits, EWMA scaling, Basel
backtests -- inherits whatever errors are made here, and inherits them
silently, because a wrong VaR looks exactly like a right one.

So four things are checked rather than assumed:

**Ordering.** Coinbase returns candles *newest first*. Feeding that straight
into a return series inverts the sign of every return. The series is sorted
ascending by timestamp here, once, and the invariant is asserted.

**Gaps.** An exchange outage leaves a missing day. Differencing across it
produces one return that spans 72 hours sitting in a series scaled to 24, and
it lands in the tail -- precisely the observations EVT fits to. Gaps are
detected and reported rather than silently absorbed.

**Duplicates.** Repeated timestamps produce a zero return that dilutes the
volatility estimate downward.

**Simple vs log returns.** The engine documents its inputs as *simple*
returns, and P&L is `notional x simple_return`. Log returns would understate
a large loss when converted to currency (-10% log is -9.5% simple), which is
the wrong direction for a risk measure. Simple is the default; log is
available for volatility modelling, where it is the better choice, and the
two are never mixed in one series.

**The in-progress bar.** A venue returns the current period as a candle whose
`close` is simply the last trade -- four hours into a daily bar, that "close"
is a 4-hour price. Differencing it produces a 4-hour return sitting in a
series every downstream estimator scales to 24. It is systematically too
small, and it does the most damage where it is weighted most: an EWMA puts
its largest weight on the newest observation, so the conditional volatility
that drives a VaR is biased low by exactly the observation that is not real
yet. It is dropped by default.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

from .http import HttpClient

#: Granularities the venue accepts, in seconds. Anything else is rejected by
#: the API with a 400, so it is rejected here with a message that says why.
VALID_GRANULARITY = (60, 300, 900, 3600, 21600, 86400)

CANDLE_URL = "https://api.exchange.coinbase.com/products/%s/candles"


class HistoryError(Exception):
    """The series cannot be trusted for risk work."""


@dataclass(frozen=True)
class Candle:
    """One bar. Prices stay Decimal; this package does not do float money."""

    time_s: int
    low: Decimal
    high: Decimal
    open: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class ReturnSeries:
    """A return series and everything needed to judge whether to trust it.

    `gaps` is not diagnostic decoration. A caller computing a 1-day VaR from a
    series containing 3-day returns is producing a number that means nothing,
    and it must be able to find that out without re-deriving it.
    """

    market: str
    granularity_s: int
    returns: List[float]
    closes: List[Decimal]
    start_s: int
    end_s: int
    gaps: List[Tuple[int, int]]      # (timestamp, actual gap in seconds)
    duplicates: int
    kind: str                        # "simple" or "log"
    #: Whether the venue's in-progress bar was discarded. 1 in normal
    #: operation; 0 only when the newest period happens to have just closed.
    incomplete_dropped: int = 0

    @property
    def n(self) -> int:
        return len(self.returns)

    @property
    def clean(self) -> bool:
        return not self.gaps and not self.duplicates

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "granularity_s": self.granularity_s,
            "n_returns": self.n,
            "kind": self.kind,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "gaps": [{"after_ts": t, "gap_s": g} for t, g in self.gaps],
            "duplicates": self.duplicates,
            "incomplete_dropped": self.incomplete_dropped,
            "clean": self.clean,
        }


def drop_incomplete_bar(candles: Sequence[Candle], granularity_s: int,
                        now_s: Optional[int] = None) -> Tuple[List[Candle], int]:
    """Discard the newest bar when its period has not closed yet.

    `now_s` is injectable so this is testable without waiting for a clock.
    """
    ordered = sorted(candles, key=lambda c: c.time_s)
    if not ordered:
        return [], 0
    now = int(time.time()) if now_s is None else int(now_s)
    if ordered[-1].time_s + granularity_s > now:
        return ordered[:-1], 1
    return ordered, 0


def fetch_candles(market: str, granularity_s: int = 86400,
                  client: Optional[HttpClient] = None) -> List[Candle]:
    """Public OHLCV for `market`, oldest first.

    No key, no entitlement: this is the venue's public endpoint. Raises rather
    than returning a short series -- a risk caller that silently receives 3
    observations will happily fit a tail to them.
    """
    if granularity_s not in VALID_GRANULARITY:
        raise HistoryError(
            "granularity %ds is not one the venue publishes; use one of %s"
            % (granularity_s, ", ".join(str(g) for g in VALID_GRANULARITY)))

    client = client or HttpClient()
    raw = client.get(CANDLE_URL % market.upper(),
                     params={"granularity": str(granularity_s)})
    if not isinstance(raw, list) or not raw:
        raise HistoryError("no candles returned for %s" % market)

    out: List[Candle] = []
    for row in raw:
        # [ time, low, high, open, close, volume ]
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise HistoryError("unexpected candle shape for %s: %r" % (market, row))
        out.append(Candle(
            time_s=int(row[0]),
            low=Decimal(str(row[1])), high=Decimal(str(row[2])),
            open=Decimal(str(row[3])), close=Decimal(str(row[4])),
            volume=Decimal(str(row[5]))))

    # The venue returns newest-first. Sorting here rather than reversing means
    # the invariant holds even if that ever changes.
    out.sort(key=lambda c: c.time_s)
    return out


def build_returns(candles: Sequence[Candle], market: str,
                  granularity_s: int = 86400, kind: str = "simple",
                  incomplete_dropped: int = 0) -> ReturnSeries:
    """Turn candles into a return series, reporting what is wrong with it."""
    if kind not in ("simple", "log"):
        raise HistoryError("kind must be 'simple' or 'log', got %r" % kind)
    if len(candles) < 3:
        raise HistoryError(
            "%d candle(s) is not a series; refusing to derive returns" % len(candles))

    ordered = sorted(candles, key=lambda c: c.time_s)

    closes: List[Decimal] = []
    times: List[int] = []
    duplicates = 0
    for c in ordered:
        if times and c.time_s == times[-1]:
            # A repeated bar contributes a zero return that drags the variance
            # estimate down. Drop it rather than average it in.
            duplicates += 1
            continue
        if c.close <= 0:
            raise HistoryError(
                "non-positive close %s at %d for %s - a return is undefined "
                "across it" % (c.close, c.time_s, market))
        times.append(c.time_s)
        closes.append(c.close)

    gaps: List[Tuple[int, int]] = []
    returns: List[float] = []
    for i in range(1, len(closes)):
        step = times[i] - times[i - 1]
        if step != granularity_s:
            gaps.append((times[i - 1], step))
        prev, cur = closes[i - 1], closes[i]
        if kind == "simple":
            returns.append(float(cur / prev - 1))
        else:
            # Decimal has no ln(); the conversion is explicit and happens once.
            import math
            returns.append(math.log(float(cur) / float(prev)))

    return ReturnSeries(
        market=market.upper(), granularity_s=granularity_s, returns=returns,
        closes=closes, start_s=times[0], end_s=times[-1],
        gaps=gaps, duplicates=duplicates, kind=kind,
        incomplete_dropped=incomplete_dropped)


def return_series(market: str, granularity_s: int = 86400,
                  kind: str = "simple", client: Optional[HttpClient] = None,
                  drop_incomplete: bool = True,
                  now_s: Optional[int] = None) -> ReturnSeries:
    """Fetch and build in one call, excluding the in-progress bar."""
    candles = fetch_candles(market, granularity_s, client)
    dropped = 0
    if drop_incomplete:
        candles, dropped = drop_incomplete_bar(candles, granularity_s, now_s)
    return build_returns(
        candles, market=market, granularity_s=granularity_s, kind=kind,
        incomplete_dropped=dropped)
