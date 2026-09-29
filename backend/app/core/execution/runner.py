"""Algo runner: works a parent order slice by slice against the venue.

WHAT MAKES THIS AN ALGO AND NOT A LOOP. Three things, and each is the difference
between a schedule and an execution:

  IT REACTS TO WHAT COMES BACK. A slice that does not fill leaves quantity behind,
  and the remainder has to go somewhere — either caught up in later slices or left
  unfilled at the end. Ignoring that is how an algo reports "done" having traded
  75% of the parent.

  IT CAN BE STOPPED. A working parent is a live commitment; pause, resume and
  cancel are not conveniences. Cancel leaves the unexecuted remainder explicitly
  unfilled rather than pretending the parent completed.

  IT MEASURES ITSELF AGAINST THE DECISION, not against its own fills. The
  benchmark is the ARRIVAL PRICE — the mid at the moment the parent was accepted.
  Everything after that is a cost of the decision to trade. Measuring against the
  average of your own fills is circular and always flatters the algo.

STATE IS DERIVED FROM CHILDREN, never tracked alongside them. There is no
`self.filled` counter: a second source of truth for the same number is a bug
waiting for the two to disagree, and in an execution algo that disagreement is
silent until someone reconciles.

THE CLOCK IS INJECTED. `now()` is a parameter so a 30-minute schedule is testable
in milliseconds. An algo that can only be tested by waiting is an algo that is not
tested.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .schedule import Algo, Schedule, Slice, build, pov_slice_qty


class ParentState(str, Enum):
    PENDING = "pending"          # scheduled, not started
    WORKING = "working"
    PAUSED = "paused"
    COMPLETED = "completed"      # every slice sent AND fully filled
    PARTIAL = "partial"          # schedule exhausted, quantity left unfilled
    CANCELLED = "cancelled"


@dataclass
class ChildResult:
    slice_index: int
    cl_ord_id: str
    requested_qty: float
    filled_qty: float
    avg_px: float
    status: str
    sent_at: float

    @property
    def unfilled(self) -> float:
        return max(0.0, self.requested_qty - self.filled_qty)


@dataclass
class ParentOrder:
    parent_id: str
    symbol: str
    side: str                    # FIX 54: "1" buy, "2" sell
    total_qty: float
    algo: Algo
    schedule: Schedule
    arrival_mid: float | None    # the benchmark; set when the parent is accepted
    started_at: float
    state: ParentState = ParentState.PENDING
    children: list[ChildResult] = field(default_factory=list)
    next_slice: int = 0
    pov_rate: float = 0.0
    limit_offset_bps: float = 0.0
    note: str = ""

    # ---- everything below is DERIVED from children, never tracked ----
    @property
    def filled_qty(self) -> float:
        return sum(c.filled_qty for c in self.children)

    @property
    def leaves_qty(self) -> float:
        return max(0.0, self.total_qty - self.filled_qty)

    @property
    def avg_px(self) -> float:
        f = self.filled_qty
        if f <= 0:
            return 0.0
        return sum(c.filled_qty * c.avg_px for c in self.children) / f

    @property
    def slices_sent(self) -> int:
        return self.next_slice

    @property
    def participation(self) -> float:
        return self.filled_qty / self.total_qty if self.total_qty else 0.0

    def shortfall_bps(self) -> float | None:
        """Implementation shortfall against the arrival mid, in basis points.

        Signed so POSITIVE IS ALWAYS BAD regardless of side: a buyer paying above
        arrival and a seller receiving below it are the same kind of failure, and
        a metric that flips sign between them cannot be aggregated.
        """
        if self.arrival_mid is None or self.arrival_mid <= 0 or self.filled_qty <= 0:
            return None
        diff = self.avg_px - self.arrival_mid
        if self.side == "2":            # selling: receiving less is the cost
            diff = -diff
        return (diff / self.arrival_mid) * 10_000

    def to_dict(self) -> dict:
        return {
            "parent_id": self.parent_id,
            "symbol": self.symbol,
            "side": self.side,
            "algo": self.algo.value,
            "state": self.state.value,
            "total_qty": self.total_qty,
            "filled_qty": self.filled_qty,
            "leaves_qty": self.leaves_qty,
            "avg_px": self.avg_px,
            "arrival_mid": self.arrival_mid,
            "shortfall_bps": self.shortfall_bps(),
            "participation": self.participation,
            "slices_total": len(self.schedule),
            "slices_sent": self.next_slice,
            "children": [
                {"slice": c.slice_index, "cl_ord_id": c.cl_ord_id,
                 "requested": c.requested_qty, "filled": c.filled_qty,
                 "avg_px": c.avg_px, "status": c.status, "unfilled": c.unfilled}
                for c in self.children
            ],
            "note": self.note,
            "simulated": True,
        }


class AlgoRunner:
    """Drives parents against a send function.

    The send function is injected rather than the runner reaching for a desk:
    it makes the runner testable against a stub, and it keeps the execution logic
    independent of whether the other side is the simulated venue, a FIX session
    or a replay.
    """

    def __init__(self, send, *, now=time.monotonic) -> None:
        # send(symbol, side, qty, limit_price) -> (cl_ord_id, filled_qty, avg_px, status)
        self._send = send
        self._now = now
        self._parents: dict[str, ParentOrder] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- create
    def create(self, *, symbol: str, side: str, total_qty: float, algo: Algo,
               slices: int = 10, duration_s: float = 60.0,
               arrival_mid: float | None = None, pov_rate: float = 0.0,
               min_slice_qty: float = 1.0, round_to: float = 1.0,
               limit_offset_bps: float = 0.0,
               profile: tuple[float, ...] | None = None) -> ParentOrder:
        sched = build(algo, total_qty, slices, duration_s,
                      profile=profile, min_slice_qty=min_slice_qty,
                      round_to=round_to)
        p = ParentOrder(
            parent_id=f"P-{uuid.uuid4().hex[:10]}",
            symbol=symbol, side=side, total_qty=total_qty, algo=algo,
            schedule=sched, arrival_mid=arrival_mid, started_at=self._now(),
            pov_rate=pov_rate, limit_offset_bps=limit_offset_bps,
        )
        if len(sched) < slices:
            # The venue minimum bit. Say so rather than silently delivering a
            # different schedule than was asked for.
            p.note = (f"Requested {slices} slices, scheduled {len(sched)}: any more "
                      f"would put a child below the {min_slice_qty} minimum.")
        with self._lock:
            self._parents[p.parent_id] = p
        return p

    # -------------------------------------------------------------- drive
    def step(self, parent_id: str, *, observed_volume: float = 0.0,
             reference_px: float | None = None,
             limit_anchor: float | None = None) -> dict:
        """Fire every slice now due. Returns what happened this step.

        Called on a timer. Firing *every* due slice rather than one keeps a
        delayed tick from leaving the schedule permanently behind: a runner that
        sends at most one slice per call can never catch up after a stall.

        TWO PRICES, NOT ONE, and conflating them is why the first version filled
        nothing. `reference_px` is the MID: it is the arrival benchmark, because
        implementation shortfall must be measured against the price that existed
        when the decision was made. `limit_anchor` is where the child is actually
        priced -- for an aggressive IOC child that is the FAR TOUCH, since a buy
        limit at the mid never crosses the ask and every child cancels unfilled.
        Defaults to reference_px when not given, which is the passive behaviour.
        """
        with self._lock:
            p = self._parents.get(parent_id)
            if p is None:
                raise KeyError(f"unknown parent '{parent_id}'")
            if p.state in (ParentState.COMPLETED, ParentState.CANCELLED,
                           ParentState.PARTIAL):
                return {"fired": 0, "state": p.state.value, "reason": "terminal"}
            if p.state is ParentState.PAUSED:
                return {"fired": 0, "state": p.state.value, "reason": "paused"}

            if p.state is ParentState.PENDING:
                p.state = ParentState.WORKING
                if p.arrival_mid is None and reference_px:
                    # The benchmark is set once, at acceptance. Re-baselining it
                    # later would make every algo look like it performed well.
                    p.arrival_mid = reference_px

            elapsed = self._now() - p.started_at
            fired: list[int] = []

            while p.next_slice < len(p.schedule):
                sl: Slice = p.schedule.slices[p.next_slice]
                if sl.offset_s > elapsed:
                    break                       # not due yet

                qty = self._slice_qty(p, sl, observed_volume)
                if qty <= 0:
                    # POV with no volume to participate in: wait rather than
                    # sending a child sized from nothing.
                    break

                limit_px = self._limit_for(p, limit_anchor if limit_anchor is not None
                                           else reference_px)
                cl, filled, avg, status = self._send(p.symbol, p.side, qty, limit_px)
                p.children.append(ChildResult(
                    slice_index=sl.index, cl_ord_id=cl, requested_qty=qty,
                    filled_qty=filled, avg_px=avg, status=status,
                    sent_at=self._now()))
                fired.append(sl.index)
                p.next_slice += 1

                if p.leaves_qty <= 0:
                    break                       # parent complete; stop early

            self._settle(p)
            return {"fired": len(fired), "slices": fired, "state": p.state.value,
                    "filled_qty": p.filled_qty, "leaves_qty": p.leaves_qty}

    def _slice_qty(self, p: ParentOrder, sl: Slice, observed_volume: float) -> float:
        if p.algo is Algo.POV:
            return pov_slice_qty(observed_volume, p.pov_rate,
                                 remaining=p.leaves_qty)
        # Never send more than is left: earlier slices may have over-filled
        # relative to plan, or a cancel may have reduced the parent.
        return min(sl.qty, p.leaves_qty)

    def _limit_for(self, p: ParentOrder, reference_px: float | None) -> float:
        """A limit offset from the reference, in bps.

        Zero means "at the reference", which crosses the spread and pays it.
        A positive offset for a buyer means willing to pay MORE — more aggressive,
        higher fill probability, worse price. That direction is easy to invert,
        so it is stated here.
        """
        if reference_px is None or reference_px <= 0:
            return 0.0
        if p.limit_offset_bps == 0:
            return reference_px
        sign = 1.0 if p.side == "1" else -1.0
        return reference_px * (1.0 + sign * p.limit_offset_bps / 10_000.0)

    def _settle(self, p: ParentOrder) -> None:
        if p.leaves_qty <= 1e-9:
            p.state = ParentState.COMPLETED
        elif p.next_slice >= len(p.schedule):
            # Schedule exhausted with quantity left. PARTIAL, not COMPLETED --
            # reporting "done" having traded 75% of the parent is the failure
            # this state exists to prevent.
            p.state = ParentState.PARTIAL
            unfilled = sum(c.unfilled for c in p.children)
            p.note = (f"Schedule exhausted with {p.leaves_qty:.4g} unfilled "
                      f"({unfilled:.4g} of it from partially-filled children).")

    # ------------------------------------------------------------ control
    def pause(self, parent_id: str) -> ParentOrder:
        with self._lock:
            p = self._require(parent_id)
            if p.state is ParentState.WORKING:
                p.state = ParentState.PAUSED
            return p

    def resume(self, parent_id: str) -> ParentOrder:
        with self._lock:
            p = self._require(parent_id)
            if p.state is ParentState.PAUSED:
                p.state = ParentState.WORKING
            return p

    def cancel(self, parent_id: str) -> ParentOrder:
        with self._lock:
            p = self._require(parent_id)
            if p.state not in (ParentState.COMPLETED, ParentState.CANCELLED):
                p.state = ParentState.CANCELLED
                p.note = (f"Cancelled with {p.leaves_qty:.4g} unexecuted. The "
                          "remainder is explicitly NOT filled.")
            return p

    def get(self, parent_id: str) -> ParentOrder:
        with self._lock:
            return self._require(parent_id)

    def list_parents(self) -> list[ParentOrder]:
        with self._lock:
            return list(self._parents.values())

    def active(self) -> list[ParentOrder]:
        with self._lock:
            return [p for p in self._parents.values()
                    if p.state in (ParentState.PENDING, ParentState.WORKING,
                                   ParentState.PAUSED)]

    def _require(self, parent_id: str) -> ParentOrder:
        p = self._parents.get(parent_id)
        if p is None:
            raise KeyError(f"unknown parent '{parent_id}'")
        return p
