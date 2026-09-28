"""Turning a finished parent order into rows, and back out again.

`Store` knows about tables and `ParentOrder` knows about slices, and neither
should have to know about the other: the store would grow imports from the
execution stack, and the algorithms would grow a schema they have to keep in
step. This module is the seam between them, and it is the only place that has
to change when either side moves.

The translation is not mechanical in one respect. A fill arrives attached to a
*child*, but the question asked afterwards is almost always about a *slice* --
"did the third slice, the one scheduled through the thin patch, pay for it?"
So every fill is stamped with the slice index its child was cut from, taken
from the child's own `client_order_id` rather than from its position in a list.
That id is what the venue echoed back, which makes it the one link between our
records and theirs that survives a reordering, a retry, or a gap.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..core.types import now_ms

#: Children are named `<parent>-cNN` by `ParentOrder.child_request`.
_CHILD_SUFFIX = re.compile(r"-c(\d+)$")


def slice_index_of(child, fallback: int) -> int:
    """The slice a child was cut from, by id, falling back to send order.

    The fallback matters on a venue that rejected a child outright: there is
    still a record to keep, and losing it because an id was not echoed would
    drop precisely the slice worth looking at.
    """
    match = _CHILD_SUFFIX.search(child.request.client_order_id or "")
    return int(match.group(1)) if match else fallback


def schedule_span_ms(parent) -> int:
    """How long the schedule actually spanned, first slice to last.

    Derived rather than remembered: `ParentOrder` keeps the schedule, not the
    duration it was asked for, and the span is the more honest number anyway --
    it is what the order did, not what it intended.
    """
    if len(parent.slices) < 2:
        return 0
    return int(parent.slices[-1].scheduled_ms - parent.slices[0].scheduled_ms)


def run_records(parent, tca=None, venue: str = "paper",
                benchmark_prints: int = 0,
                computed_ms: Optional[int] = None,
                ) -> Tuple[Dict[str, Any], List[Dict[str, Any]],
                           List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """`(parent, children, fills, tca)` records for `Store.save_algo_run`.

    Decimals are passed through untouched. Stringifying here would throw away
    the one property the store is built to preserve, and `Store._dec` is the
    boundary that decides how money becomes text -- doing it twice means two
    places to get it wrong.
    """
    if parent.arrival_price is None:
        # Storing 0 would make every shortfall against this run read as a
        # catastrophic loss rather than as a missing benchmark. Refuse instead:
        # a run whose arrival price was never captured cannot be measured, and
        # a TCA table that cannot say so is not worth keeping.
        raise ValueError(
            "parent %s has no arrival price - nothing to measure execution "
            "against, so the run would be stored unmeasurable"
            % parent.request.client_order_id)

    parent_id = parent.request.client_order_id
    by_index = {s.index: s for s in parent.slices}

    children: List[Dict[str, Any]] = []
    fills: List[Dict[str, Any]] = []
    for position, child in enumerate(parent.children):
        index = slice_index_of(child, position)
        sched = by_index.get(index)
        children.append({
            "slice_index": index,
            # A child with no matching slice is a bug worth seeing in the data
            # rather than a crash here, so fall back to the child's own record.
            "scheduled_ms": sched.scheduled_ms if sched else child.created_ms,
            "scheduled_size": sched.size if sched else child.request.size,
            "weight": sched.weight if sched else Decimal(0),
            "client_id": child.request.client_order_id,
            "status": child.status.value,
            "filled_size": child.filled_size,
            "avg_price": child.avg_price,
        })
        for fill in child.fills:
            fills.append({
                "slice_index": index,
                "ts_ms": fill.timestamp_ms,
                "size": fill.size,
                "price": fill.price,
                "fee": fill.fee,
                "liquidity": fill.liquidity,
            })

    completed = max((f["ts_ms"] for f in fills), default=None)
    parent_record = {
        "parent_id": parent_id,
        "created_ms": parent.arrival_ms,
        "venue": venue,
        "market": parent.request.market,
        "side": parent.request.side.value,
        "algo": parent.algo_name,
        "total_size": parent.total_size,
        "slice_count": len(parent.slices),
        "duration_ms": schedule_span_ms(parent),
        "arrival_price": parent.arrival_price,
        "status": parent.status.value,
        "filled_size": parent.filled_size,
        "avg_price": parent.avg_price,
        "completed_ms": completed if parent.is_complete else None,
    }

    tca_record = None
    if tca is not None and tca.filled_size > 0:
        tca_record = {
            "computed_ms": computed_ms if computed_ms is not None else now_ms(),
            "arrival_price": tca.arrival_price or parent.arrival_price,
            "avg_price": tca.avg_price,
            "implementation_shortfall_bps": tca.implementation_shortfall_bps,
            "vwap_slippage_bps": tca.vwap_slippage_bps,
            "fee_bps": tca.fee_bps,
            "total_cost_bps": tca.total_cost_bps,
            # Kept as a percentage, matching `TCAResult.to_dict`, so the two
            # renderings of the same run cannot disagree by a factor of 100.
            "participation_rate_pct": (None if tca.participation_rate is None
                                       else tca.participation_rate * 100),
            # How many real market prints the benchmark rested on. A VWAP
            # computed from four trades is arithmetic, not a benchmark, and
            # without this column nothing downstream can tell the difference.
            "benchmark_prints": benchmark_prints,
        }

    return parent_record, children, fills, tca_record


def save_run(store, parent, tca=None, venue: str = "paper",
             benchmark_prints: int = 0, keep: int = 500) -> str:
    """Persist one run and trim the table. Returns the parent id.

    Pruning here rather than on a timer keeps the bound honest: the table is
    written by whatever calls this, so this is the only moment we can be sure
    a check happens at all.
    """
    records = run_records(parent, tca, venue=venue,
                          benchmark_prints=benchmark_prints)
    store.save_algo_run(*records)
    if keep:
        store.prune_algo_runs(keep=keep)
    return records[0]["parent_id"]


def format_parent_row(row: Dict[str, Any]) -> str:
    """One stored run as a console line."""
    def bps(value):
        return "%+8.2f" % Decimal(value) if value not in (None, "") else "     n/a"

    return ("  %-26s %-6s %-14s %-4s %12s %10s %s %s"
            % (row["parent_id"][:26], row["algo"], row["market"], row["side"],
               row["filled_size"], row["status"][:10],
               bps(row.get("implementation_shortfall_bps")),
               bps(row.get("total_cost_bps"))))


def format_stats(stats: Dict[str, Any]) -> List[str]:
    """Aggregates across stored runs, as console lines."""
    lines = ["%d run(s), %d fill(s), %s filled in total"
             % (stats["runs"], stats["fills"], stats["total_filled"])]
    for entry in stats["by_algo"]:
        lines.append(
            "  %-6s %3d run(s)   mean shortfall %s   mean all-in %s"
            % (entry["algo"], entry["runs"],
               "%+.3f bps" % entry["avg_shortfall_bps"]
               if entry["avg_shortfall_bps"] is not None else "n/a",
               "%+.3f bps" % entry["avg_total_cost_bps"]
               if entry["avg_total_cost_bps"] is not None else "n/a"))
    return lines
