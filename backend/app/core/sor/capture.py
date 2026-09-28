"""Feature capture at route time — the half of the fill model that was missing.

The model in `ml.py` was trained on a synthetic generator because nothing ever
recorded what it actually saw. This module is the fix: one definition of the
feature vector, used by BOTH the prediction path and the logging path, so a
model trained on captured rows sees exactly the inputs it will be served.

Two decisions worth stating, because they are the ones a reviewer challenges:

  * **Features are captured at DECISION time, not execution time.** The router
    needs P(fill) *before* it can allocate, so the only size feature available
    then is parent-order-size / venue-depth -- not the per-venue allocation,
    which is an output of the very decision being made. Logging the allocation
    ratio instead would train the model on information it will never have at
    serving time. That is train/serve skew, and it makes offline metrics look
    better than live behaviour. The allocation is logged separately as a
    diagnostic, never as a feature.

  * **`spread_bps` is a misnomer inherited from the model's feature list.** It
    is the price span from top-of-book to the worst captured level, not the
    bid-ask spread. It is kept as-is because renaming or recomputing it would
    silently change the inputs of every model already fitted against it. The
    name is wrong; the semantics are stable, which matters more.
"""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass

# Must stay in lockstep with ml.FEATURES -- asserted in ml.py at import time.
FEATURES = ("size_ratio", "latency_ms", "spread_bps", "volatility")


def _book_span_bps(v) -> float:
    """Top-of-book to worst-level price span in bps. See the note above on why
    this is called `spread_bps` despite not being a spread."""
    if len(v.levels) > 1 and v.levels[0][0]:
        return abs(v.levels[0][0] - v.levels[-1][0]) / v.levels[0][0] * 1e4
    return 1.0


def venue_features(v, order_qty: float, volatility: float = 0.8) -> dict:
    """The decision-time feature vector for one venue.

    `order_qty` is the PARENT order size, deliberately -- see module docstring.
    """
    depth = v.depth() or 1.0
    return {
        "size_ratio": order_qty / depth,
        "latency_ms": float(v.latency_ms),
        "spread_bps": _book_span_bps(v),
        "volatility": float(volatility),
    }


@dataclass
class RouteObservation:
    """One venue's decision + outcome, ready to become a training row.

    `label_filled` is the supervised target: did this venue deliver what the
    router asked of it. A venue the router deliberately allocated nothing to is
    NOT an observation -- there is no outcome to learn from, and including it
    as a zero would teach the model that unattractive venues never fill, which
    is a fact about the router, not about the venue.
    """
    route_id: str
    venue: str
    side: str
    symbol: str | None
    data_source: str            # where the FEATURES came from: synthetic | ccxt
    outcome_source: str         # where the LABEL came from: fade-sim | live
    model_version: int | None
    predicted_p: float | None
    size_ratio: float
    latency_ms: float
    spread_bps: float
    volatility: float
    requested_qty: float
    filled_qty: float
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="milliseconds")

    @property
    def fill_rate(self) -> float:
        return self.filled_qty / self.requested_qty if self.requested_qty else 0.0

    @property
    def label_filled(self) -> int:
        """1 if the venue substantially delivered. The 99% threshold rather
        than exact equality absorbs float noise in the size arithmetic; a
        venue 0.4% short has not meaningfully missed."""
        return 1 if self.fill_rate >= 0.99 else 0

    def features(self) -> dict:
        return {k: getattr(self, k) for k in FEATURES}

    def to_row(self) -> dict:
        d = asdict(self)
        d["fill_rate"] = round(self.fill_rate, 6)
        d["label_filled"] = self.label_filled
        return d


def observe_route(*, route_id: str, side: str, symbol: str | None,
                  venues, allocation: dict, fills: dict, order_qty: float,
                  volatility: float, data_source: str, outcome_source: str,
                  predicted: dict | None = None,
                  model_version: int | None = None) -> list[RouteObservation]:
    """Join the routing decision to its realised outcome, one row per venue.

    Only venues that were actually allocated size produce a row -- see the
    note on `label_filled` above.
    """
    out = []
    for v in venues:
        requested = float(allocation.get(v.venue, 0.0) or 0.0)
        if requested <= 0:
            continue
        f = venue_features(v, order_qty, volatility)
        out.append(RouteObservation(
            route_id=route_id, venue=v.venue, side=side, symbol=symbol,
            data_source=data_source, outcome_source=outcome_source,
            model_version=model_version,
            predicted_p=(predicted or {}).get(v.venue),
            requested_qty=requested,
            filled_qty=float(fills.get(v.venue, 0.0) or 0.0),
            **f))
    return out
