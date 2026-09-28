"""Pure-Python equivalents for the two connectivity components where a fallback
is honest: the L2 book and the market-data quality gate.

These exist so the API keeps working when the C++ extension is not built. They
implement the SAME semantics, verified by the same tests running against both, and
they are deliberately straightforward rather than clever -- a fallback whose job is
correctness should not be trying to be fast.

They are slower by a wide margin and that is expected: the C++ versions exist
because these operations sit in the hot path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum


class Side(IntEnum):
    BID = 0
    ASK = 1


def _opposite(s: Side) -> Side:
    return Side.ASK if s == Side.BID else Side.BID


@dataclass
class Level:
    price: int
    qty: int
    orders: int = 1


class PyLadder:
    """Mirror of mkt::book::Ladder. Same integer-tick keying, same rejection
    behaviour, same cached-best semantics."""

    def __init__(self, floor_ticks: int, n_ticks: int) -> None:
        self._floor = floor_ticks
        self._n = n_ticks
        self._qty = {Side.BID: {}, Side.ASK: {}}
        self._best = {Side.BID: None, Side.ASK: None}
        self._total = {Side.BID: 0, Side.ASK: 0}
        self._rejects = 0
        self._underflows = 0

    # -- geometry --
    def floor_ticks(self) -> int: return self._floor
    def ceil_ticks(self) -> int: return self._floor + self._n - 1
    def in_range(self, px: int) -> bool: return self._floor <= px <= self.ceil_ticks()

    # -- mutation --
    def upsert(self, side, price: int, qty: int, orders: int = 1) -> bool:
        side = Side(side)
        if not self.in_range(price) or qty < 0:
            self._rejects += 1
            return False
        book = self._qty[side]
        prev = book.get(price, 0)
        self._total[side] += qty - prev
        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty
        self._recompute_best(side)
        return True

    def add(self, side, price: int, delta: int, orders: int = 1) -> bool:
        side = Side(side)
        if not self.in_range(price):
            self._rejects += 1
            return False
        nxt = self._qty[side].get(price, 0) + delta
        if nxt < 0:
            nxt = 0
            self._underflows += 1
        return self.upsert(side, price, nxt, orders)

    def clear(self) -> None:
        self._qty = {Side.BID: {}, Side.ASK: {}}
        self._best = {Side.BID: None, Side.ASK: None}
        self._total = {Side.BID: 0, Side.ASK: 0}

    def _recompute_best(self, side: Side) -> None:
        book = self._qty[side]
        if not book:
            self._best[side] = None
        else:
            self._best[side] = max(book) if side == Side.BID else min(book)

    # -- reads --
    def best(self, side):
        side = Side(side)
        p = self._best[side]
        return None if p is None else (p, self._qty[side][p])

    def qty_at(self, side, price: int) -> int:
        return self._qty[Side(side)].get(price, 0)

    def total_qty(self, side) -> int: return self._total[Side(side)]
    def levels(self, side) -> int: return len(self._qty[Side(side)])
    def rejects(self) -> int: return self._rejects

    def _two_sided(self) -> bool:
        return self._best[Side.BID] is not None and self._best[Side.ASK] is not None

    def spread_ticks(self) -> int:
        return (self._best[Side.ASK] - self._best[Side.BID]) if self._two_sided() else 0

    def mid_ticks(self) -> float:
        if not self._two_sided():
            return 0.0
        return 0.5 * (self._best[Side.BID] + self._best[Side.ASK])

    def micro_price_ticks(self) -> float:
        if not self._two_sided():
            return 0.0
        bp, ap = self._best[Side.BID], self._best[Side.ASK]
        bq, aq = self._qty[Side.BID][bp], self._qty[Side.ASK][ap]
        tot = bq + aq
        if tot <= 0:
            return self.mid_ticks()
        # Weight each side by the OPPOSING size: a heavy bid pulls fair value up.
        return (bp * aq + ap * bq) / tot

    def imbalance(self) -> float:
        if not self._two_sided():
            return 0.0
        bq = self._qty[Side.BID][self._best[Side.BID]]
        aq = self._qty[Side.ASK][self._best[Side.ASK]]
        tot = bq + aq
        return (bq - aq) / tot if tot else 0.0

    def is_crossed(self) -> bool:
        return self._two_sided() and self._best[Side.BID] >= self._best[Side.ASK]

    def depth(self, side, levels: int = 10):
        side = Side(side)
        ps = sorted(self._qty[side], reverse=(side == Side.BID))[:levels]
        return [Level(p, self._qty[side][p]) for p in ps]


class PyFeedValidator:
    """Mirror of mkt::feed::Validator, including the three-way sequence split and
    the warm-up rule that keeps a cold validator from rejecting the first price."""

    def __init__(self, price_min=0.0, price_max=1e18, max_z=10.0, min_samples=30,
                 lam=0.97, max_age_ns=5_000_000_000, stale_after_ns=10_000_000_000):
        self.price_min, self.price_max = price_min, price_max
        self.max_z, self.min_samples, self.lam = max_z, min_samples, lam
        self.max_age_ns, self.stale_after_ns = max_age_ns, stale_after_ns
        self.reset()

    def reset(self) -> None:
        self._have_seq = self._have_ts = False
        self._gap_pending = False
        self._last_seq = 0
        self._last_ts = 0
        self._last_price = 0.0
        self._mean = self._var = 0.0
        self._samples = 0
        self.c = dict(accepted=0, gaps=0, gap_messages_lost=0, duplicates=0,
                      reordered=0, stale_timestamp=0, too_old=0, outliers=0,
                      out_of_band=0, bad_values=0)

    def check(self, seq: int, exch_ns: int, recv_ns: int, price: float, size: float = 1.0) -> str:
        # Sequencing describes the TRANSPORT and advances independently of whether
        # the payload is then refused -- see the C++ header for why.
        if self._have_seq:
            if seq == self._last_seq:
                self.c["duplicates"] += 1; return "duplicate"
            if seq < self._last_seq:
                self.c["reordered"] += 1; return "reordered"
            if seq > self._last_seq + 1:
                self.c["gaps"] += 1
                self.c["gap_messages_lost"] += seq - self._last_seq - 1
                self._last_seq = seq
                self._gap_pending = True
                return "sequence_gap"
        self._last_seq = seq
        self._have_seq = True

        if not math.isfinite(price) or price <= 0 or not math.isfinite(size) or size < 0:
            self.c["bad_values"] += 1; return "bad_value"
        if self._have_ts and exch_ns < self._last_ts:
            self.c["stale_timestamp"] += 1; return "stale_timestamp"
        if recv_ns > 0 and exch_ns > 0 and (recv_ns - exch_ns) > self.max_age_ns:
            self.c["too_old"] += 1; return "too_old"
        if price < self.price_min or price > self.price_max:
            self.c["out_of_band"] += 1; return "price_out_of_band"
        if self._samples >= self.min_samples and self._last_price > 0:
            r = math.log(price / self._last_price)
            sd = math.sqrt(max(self._var, 0.0))
            if sd > 1e-12 and abs(r - self._mean) / sd > self.max_z:
                self.c["outliers"] += 1; return "price_outlier"

        if self._last_price > 0:
            r = math.log(price / self._last_price)
            d = r - self._mean
            self._mean = self.lam * self._mean + (1 - self.lam) * r
            self._var = self.lam * self._var + (1 - self.lam) * d * d
        self._last_price = price
        self._last_ts = exch_ns
        self._have_ts = True
        self._samples += 1
        self.c["accepted"] += 1
        return "accepted"

    # Returns EVERY verdict key, zero-filled, matching the C++ binding exactly.
    # A dict whose keys depend on what happened forces callers to use .get() with a
    # default and makes `is None` mean two different things across the two
    # implementations -- a parity test caught precisely that divergence.
    _VERDICTS = ("accepted", "sequence_gap", "duplicate", "reordered",
                 "stale_timestamp", "too_old", "price_outlier",
                 "price_out_of_band", "bad_value")

    def check_batch(self, ticks):
        out = {k: 0 for k in self._VERDICTS}
        for seq, ns, px in ticks:
            out[self.check(seq, ns, ns, px, 1.0)] += 1
        return out

    def needs_recovery(self) -> bool: return self._gap_pending
    def recovered(self, resume_seq: int) -> None:
        self._gap_pending = False; self._last_seq = resume_seq; self._have_seq = True
    def is_stale(self, now_ns: int) -> bool:
        return True if not self._have_ts else (now_ns - self._last_ts) > self.stale_after_ns
    def last_price(self) -> float: return self._last_price
    def counters(self) -> dict:
        d = dict(self.c)
        d["total_rejected"] = sum(v for k, v in self.c.items()
                                  if k not in ("accepted", "gap_messages_lost"))
        d["total_seen"] = d["total_rejected"] + self.c["accepted"]
        return d
    def reject_rate(self) -> float:
        c = self.counters()
        return c["total_rejected"] / c["total_seen"] if c["total_seen"] else 0.0
