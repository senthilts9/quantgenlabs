"""Replay protection state.

A signed order is a bearer instrument until it expires. Bounding its lifetime
(`signature_ttl_ms`) narrows the window; remembering its nonce closes it -
within the valid window a captured order is still resubmittable unless the
venue refuses a nonce it has already seen.

The validator has always had the rule. What was missing was somewhere to keep
the state: every venue adapter built a fresh `ValidationContext` with an empty
`seen_nonces`, so the rule could never fire outside a test. This class is that
state, owned by the adapter and passed into every validation.

**Bounded by time, not count.** A nonce older than the signature TTL cannot be
replayed successfully anyway - the freshness rule rejects it first - so it can
be forgotten. An unbounded set would grow for the life of the process, which on
a long-running desk is a leak that only shows up in production.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Set, Tuple

from .types import now_ms


class ReplayGuard:
    """Time-windowed memory of nonces and client order ids."""

    def __init__(self, ttl_ms: int = 300_000, max_entries: int = 200_000,
                 store=None):
        #: Retention window. Should comfortably exceed the validator's
        #: signature TTL plus any clock-skew tolerance, or a nonce could be
        #: forgotten while its signature is still considered fresh.
        self.ttl_ms = ttl_ms
        #: Hard ceiling, so a burst cannot exhaust memory before pruning.
        self.max_entries = max_entries
        self._nonces: Deque[Tuple[int, int]] = deque()       # (expiry_ms, nonce)
        self._nonce_set: Set[int] = set()
        self._coids: Deque[Tuple[int, str]] = deque()        # (expiry_ms, coid)
        self._coid_set: Set[str] = set()
        #: Optional durable backing. Without it a restart empties the guard and
        #: a client_order_id that was already sent becomes reusable.
        self._store = store
        if store is not None:
            self._rehydrate()

    def _rehydrate(self) -> None:
        """Reload surviving entries from the store, expiry-pruned."""
        nonces, coids = self._store.load_replay(now_ms())
        expiry = now_ms() + self.ttl_ms
        for n in nonces:
            if n not in self._nonce_set:
                self._nonces.append((expiry, n))
                self._nonce_set.add(n)
        for c in coids:
            if c not in self._coid_set:
                self._coids.append((expiry, c))
                self._coid_set.add(c)

    def _prune(self, now: Optional[int] = None) -> None:
        now = now if now is not None else now_ms()
        while self._nonces and self._nonces[0][0] <= now:
            self._nonce_set.discard(self._nonces.popleft()[1])
        while self._coids and self._coids[0][0] <= now:
            self._coid_set.discard(self._coids.popleft()[1])
        # Ceiling enforcement drops the oldest first, which is also the least
        # likely to still be replayable.
        while len(self._nonces) > self.max_entries:
            self._nonce_set.discard(self._nonces.popleft()[1])
        while len(self._coids) > self.max_entries:
            self._coid_set.discard(self._coids.popleft()[1])

    def record(self, nonce: Optional[int] = None,
               client_order_id: Optional[str] = None,
               now: Optional[int] = None) -> None:
        """Remember a nonce and/or client order id as used."""
        now = now if now is not None else now_ms()
        self._prune(now)
        expiry = now + self.ttl_ms
        if nonce is not None and nonce not in self._nonce_set:
            self._nonces.append((expiry, nonce))
            self._nonce_set.add(nonce)
        if client_order_id and client_order_id not in self._coid_set:
            self._coids.append((expiry, client_order_id))
            self._coid_set.add(client_order_id)
        if self._store is not None:
            self._store.save_replay(nonce, client_order_id, expiry)

    @property
    def nonces(self) -> Set[int]:
        """Live nonce set, pruned. Handed to `ValidationContext`."""
        self._prune()
        return self._nonce_set

    @property
    def client_order_ids(self) -> Set[str]:
        self._prune()
        return self._coid_set

    def seen_nonce(self, nonce: int) -> bool:
        return nonce in self.nonces

    def seen_client_order_id(self, coid: str) -> bool:
        return coid in self.client_order_ids

    def clear(self) -> None:
        self._nonces.clear()
        self._nonce_set.clear()
        self._coids.clear()
        self._coid_set.clear()

    def __len__(self) -> int:
        self._prune()
        return len(self._nonce_set) + len(self._coid_set)

    def __repr__(self) -> str:
        return "<ReplayGuard nonces=%d coids=%d ttl=%dms>" % (
            len(self.nonces), len(self.client_order_ids), self.ttl_ms)
