"""Execution schedules: turning a parent order into timed child slices.

WHY THIS EXISTS SEPARATELY from `cryptodesk/execution/algos.py`. That module is
the crypto desk's, is vendored, and is bound to its own `Instrument` and
`OrderRequest` types. The scheduling *idea* is the same and the prior art is
acknowledged; the domain types are not shared, and reaching into a vendored
module to reuse arithmetic would couple two desks that should be independent.

THE ONE THING EVERY SCHEDULE MUST DO: sum to the parent, exactly. Rounding each
slice independently leaves a residual that either orphans quantity (the parent
never completes and an algo waits forever for a fill that cannot come) or
over-fills it. So the last slice always absorbs the remainder, and there is a
test asserting the sum is exact for awkward sizes.

WHAT EACH ALGO IS ACTUALLY FOR — they are not interchangeable:

  TWAP spreads evenly over time. It is the right choice when you have no view on
  the volume profile, and the wrong one when you do: trading a fixed rate through
  a thin period pushes the market more than the same quantity in a liquid one.

  VWAP varies SIZE by expected volume while keeping the timing even. The point is
  to be a constant *fraction* of the market rather than a constant quantity, so
  the order is hidden by the volume around it. It follows an expected profile,
  which is its weakness: a day that does not match the profile leaves you ahead
  of or behind schedule with no feedback.

  POV (participation of volume) is the feedback version: size each slice from the
  volume ACTUALLY observed, not from a forecast. It cannot be scheduled in
  advance for that reason — the sizes are decided as the market prints — so it is
  represented here as a rate, and the runner sizes each slice when it fires.

The intraday profile is a U: heavy at the open, light at midday, heavy into the
close. That shape is well documented in equity markets and is used here as a
DEFAULT to be replaced by a fitted one — a profile is an assumption, and using a
generic one on an instrument that does not follow it is how a VWAP algo quietly
underperforms its own benchmark.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class Algo(str, Enum):
    TWAP = "twap"
    VWAP = "vwap"
    POV = "pov"


class ScheduleError(ValueError):
    """The schedule cannot be built as requested."""


# A 13-bucket U-shaped intraday volume profile (half-hour buckets over a 6.5-hour
# US equity session). Deliberately a DEFAULT, not a fact: replace it with a
# fitted profile per instrument.
DEFAULT_VOLUME_PROFILE: tuple[float, ...] = (
    0.135, 0.095, 0.075, 0.060, 0.052, 0.048, 0.045,
    0.048, 0.052, 0.060, 0.075, 0.100, 0.155,
)


@dataclass(frozen=True)
class Slice:
    index: int
    offset_s: float        # seconds from the parent's start
    qty: float
    weight: float          # fraction of the parent, for reporting


@dataclass(frozen=True)
class Schedule:
    algo: Algo
    slices: tuple[Slice, ...]
    total_qty: float
    duration_s: float

    def __len__(self) -> int:
        return len(self.slices)

    @property
    def scheduled_qty(self) -> float:
        return sum(s.qty for s in self.slices)


def _resample(profile: tuple[float, ...], n: int) -> list[float]:
    """Map a fixed-bucket profile onto n slices by proportional overlap.

    Not nearest-neighbour: with 13 buckets and 5 slices, picking the nearest
    bucket would drop eight of them entirely and the shape would be whatever the
    sampling happened to hit. Overlap-weighting preserves the shape at any n.
    """
    buckets = len(profile)
    out: list[float] = []
    for i in range(n):
        lo = i * buckets / n
        hi = (i + 1) * buckets / n
        acc = 0.0
        for b in range(int(math.floor(lo)), min(buckets, int(math.ceil(hi)))):
            overlap = min(hi, b + 1) - max(lo, b)
            if overlap > 0:
                acc += profile[b] * overlap
        out.append(acc)
    total = sum(out)
    return [w / total for w in out] if total > 0 else [1.0 / n] * n


def weights_for(algo: Algo, n: int,
                profile: tuple[float, ...] | None = None) -> list[float]:
    if n < 1:
        raise ScheduleError("need at least one slice")
    if algo is Algo.TWAP:
        return [1.0 / n] * n
    if algo is Algo.VWAP:
        return _resample(profile or DEFAULT_VOLUME_PROFILE, n)
    if algo is Algo.POV:
        # POV sizes from observed volume at fire time, so an up-front weighting
        # is a placeholder. Returning an even split here rather than raising
        # keeps the schedule shape inspectable before the run starts.
        return [1.0 / n] * n
    raise ScheduleError(f"unknown algo {algo}")


def build(algo: Algo, total_qty: float, slices: int, duration_s: float, *,
          profile: tuple[float, ...] | None = None,
          min_slice_qty: float = 1.0,
          round_to: float = 1.0) -> Schedule:
    """Build a schedule whose slices sum EXACTLY to total_qty.

    `min_slice_qty` and `round_to` are venue constraints: a child below the
    minimum is rejected, and one that is not a multiple of the lot size is
    rejected or silently rounded by the venue — which is worse, because then the
    algo's own accounting disagrees with what was actually sent.
    """
    if total_qty <= 0:
        raise ScheduleError(f"parent quantity must be positive, got {total_qty}")
    if slices < 1:
        raise ScheduleError(f"need at least one slice, got {slices}")
    if duration_s < 0:
        raise ScheduleError("duration must not be negative")

    # Clamp the slice count so no child falls below the venue minimum. Asking for
    # 100 slices of a 50-lot parent is not an error to reject -- it is a request
    # to be satisfied as closely as the venue permits.
    max_slices = max(1, int(total_qty // min_slice_qty)) if min_slice_qty > 0 else slices
    n = max(1, min(slices, max_slices))

    w = weights_for(algo, n, profile)
    step = duration_s / n if n else 0.0

    out: list[Slice] = []
    allocated = 0.0
    for i in range(n):
        if i == n - 1:
            # The last slice absorbs the remainder so the schedule sums exactly.
            # Independently rounding every slice orphans quantity, and an algo
            # waiting on a fill that can never come is a stuck parent.
            qty = total_qty - allocated
        else:
            qty = _round_to(total_qty * w[i], round_to)
            if qty < min_slice_qty:
                qty = min_slice_qty
            # Never allocate more than is left for the remaining slices.
            remaining_slices = n - i - 1
            headroom = total_qty - allocated - remaining_slices * min_slice_qty
            qty = min(qty, max(min_slice_qty, headroom))
        allocated += qty
        out.append(Slice(index=i, offset_s=i * step, qty=qty, weight=w[i]))

    # Rounding can leave the final slice non-positive; fold it back rather than
    # emitting a zero-size child the venue would reject.
    while len(out) > 1 and out[-1].qty <= 0:
        tail = out.pop()
        prev = out[-1]
        out[-1] = Slice(prev.index, prev.offset_s, prev.qty + tail.qty, prev.weight)

    if out[-1].qty <= 0:
        raise ScheduleError("schedule collapsed to nothing after rounding")

    return Schedule(algo=algo, slices=tuple(out), total_qty=total_qty,
                    duration_s=duration_s)


def _round_to(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(x / step) * step


def pov_slice_qty(observed_volume: float, rate: float, *,
                  remaining: float, min_slice_qty: float = 1.0,
                  round_to: float = 1.0) -> float:
    """Size one POV slice from volume actually observed since the last slice.

    Capped at `remaining` so a burst of market volume cannot make the algo
    overshoot the parent — participating at 10% of a print larger than the whole
    order would otherwise send more than was asked for.
    """
    if rate <= 0 or observed_volume <= 0 or remaining <= 0:
        return 0.0
    qty = _round_to(observed_volume * rate, round_to)
    qty = min(qty, remaining)
    if qty < min_slice_qty:
        # Too small to send. Returning 0 makes the runner wait and accumulate
        # rather than sending a child the venue will reject.
        return 0.0 if remaining >= min_slice_qty else remaining
    return qty
