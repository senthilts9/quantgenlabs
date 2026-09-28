"""One live crypto quote cache, refreshed off the request path.

Polling the venues for a pair costs the better part of a second, and the
platform was doing it twice on two independent timers: `market_board` polled
all five pairs behind a 15s TTL, and `_marks()` polled the held ones again
behind its own 20s TTL. Same venues, same quotes, two caches.

Measured against the deployed box, that fan-out was ~3.9s of a ~4.0s
`/api/marketdata/board` and ~3.7s of a ~3.9s `/api/oms/blotter`. Because both
TTLs were far shorter than the time a person spends reading a screen, the
cache almost never hit for an actual user: nearly every click paid the full
multi-venue round-trip, which is what made "almost all the screens" slow.

The fix is not to cache harder -- it is to take the fetch off the request
path entirely. A background refresher polls on a short interval and readers
get the last observed quote plus its age.

Three properties are deliberate:

  STILL LIVE, NOT SYNTHETIC. This is the same ccxt fan-out as before, just
  performed by a timer instead of by whoever happened to click. `live_or_synth`
  returns an invented book when venues are unreachable; that is suppressed
  here exactly as it was in the two caches this replaces. An unavailable quote
  is reported as unavailable -- never fabricated.

  AGE IS REPORTED, NOT ASSUMED. Every read carries `age_s`. A quote that has
  stopped being refreshed (dead refresher, venues down) goes `stale` past
  STALE_AFTER rather than passing itself off as current, because a mark is
  what P&L, VaR and limit utilisation are computed from.

  COLD START STILL BLOCKS, ONCE. Before the first refresh lands, a reader
  fetches synchronously rather than reporting a live pair as unavailable.
  That path is single-flight: N concurrent cold readers cost one fan-out, not
  N of them.

Disable the refresher with CRYPTO_QUOTE_SECONDS=0 (readers then fall back to
fetching on demand, which is the old behaviour minus the duplicate cache).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Comfortably inside every consumer's freshness expectation, and slow enough
# to stay polite to free public exchange endpoints: five pairs every ten
# seconds is a fraction of what the per-request fan-out was generating under
# any real traffic.
INTERVAL = float(os.getenv("CRYPTO_QUOTE_SECONDS", "10"))

# A quote older than this is not treated as a live mark. Only reachable if
# the refresher has stopped or the venues have been unreachable for a while --
# at a 10s interval, normal operation is an order of magnitude inside it.
STALE_AFTER = float(os.getenv("CRYPTO_QUOTE_STALE_S", "120"))

# Longest a REQUEST will wait on cold pairs before giving up on them. Only the
# window between process start and the first prime landing can hit this, and a
# screen that renders in 3s with a pair marked unavailable beats a screen that
# hangs for 30s because one venue is wedged.
#
# This is a budget for the whole request, not per pair -- see `deadline()`.
# Applying it per pair is the easy mistake: the board reads five pairs in turn,
# so a per-pair bound let a cold request take 5 x COLD_WAIT (measured: 11.4s
# against a 3s "limit").
COLD_WAIT = float(os.getenv("CRYPTO_QUOTE_COLD_WAIT_S", "1.5"))


@dataclass(frozen=True)
class Quote:
    """One consolidated mid, or the absence of one, with the time it was taken."""
    pair: str
    ts: float
    mid: float | None
    venues: int = 0
    venue_names: tuple[str, ...] = ()
    note: str | None = None

    @property
    def age_s(self) -> float:
        return time.time() - self.ts

    @property
    def stale(self) -> bool:
        return self.age_s > STALE_AFTER

    @property
    def ok(self) -> bool:
        """Usable as a mark: a real quote, recently observed."""
        return self.mid is not None and not self.stale


_CACHE: dict[str, Quote] = {}
_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def pairs() -> list[str]:
    """Every ccxt-sourced pair in the instrument master."""
    from ..core.marketdata import universe as U
    return [i.series_id for i in U.UNIVERSE if i.source == "ccxt"]


def _lock(pair: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(pair, threading.Lock())


def _fetch(pair: str) -> Quote:
    """One multi-venue fan-out. Never raises, never synthesises."""
    from ..core.sor import ccxt_feed
    try:
        venues, mid, source = ccxt_feed.live_or_synth(pair)
        if source != "ccxt-live" or not mid:
            return Quote(pair, time.time(), None,
                         note="no live venue quote; synthetic fallback suppressed")
        names = tuple(str(getattr(v, "venue", getattr(v, "name", "?")))
                      for v in venues)[:6]
        return Quote(pair, time.time(), float(mid), len(venues), names)
    except Exception as e:                       # noqa: BLE001
        return Quote(pair, time.time(), None,
                     note=f"{type(e).__name__}: {e}"[:120])


def refresh(pair: str) -> Quote:
    """Fetch and store one pair. Called by the background refresher."""
    with _lock(pair):
        q = _fetch(pair)
        _CACHE[pair] = q
        return q


def deadline() -> float:
    """One cold-fetch budget for a whole request.

    A caller reading several pairs should take this ONCE and pass it to every
    `get()`, so the pairs share the budget instead of each getting their own.
    """
    return time.monotonic() + COLD_WAIT


def get(pair: str, until: float | None = None) -> Quote:
    """The last observed quote for `pair`.

    Returns immediately once the pair has been fetched at least once, which
    after the startup prime is always. Only a genuinely cold cache blocks, and
    that wait is bounded: `ccxt_feed` polls four venues with rate limiting and
    a retry, so a slow or unreachable venue can take tens of seconds, and no
    screen should be held hostage to that. Past the budget the pair is
    reported unavailable for this request, and the in-flight fetch is left to
    finish and populate the cache for the next one.

    `until` is a `deadline()` shared across the pairs of one request; omitted,
    the pair gets a full COLD_WAIT to itself.
    """
    hit = _CACHE.get(pair)
    if hit is not None:
        return hit
    budget = COLD_WAIT if until is None else until - time.monotonic()
    lk = _lock(pair)
    if budget <= 0 or not lk.acquire(timeout=budget):
        return Quote(pair, time.time(), None,
                     note="quote not yet available; fetch in flight")
    try:
        # Re-check under the lock: while we waited, the caller that held it
        # may have populated the entry we were about to duplicate.
        hit = _CACHE.get(pair)
        if hit is not None:
            return hit
        q = _fetch(pair)
        _CACHE[pair] = q
        return q
    finally:
        lk.release()


def mid(pair: str, until: float | None = None) -> float | None:
    """Marking price for `pair`, or None if there isn't a trustworthy one."""
    q = get(pair, until=until)
    return q.mid if q.ok else None


def refresh_all() -> None:
    """Every pair, concurrently. Startup priming and tests only -- NOT the
    steady-state path; see `run()` for why this must not be on a timer."""
    ps = pairs()
    if not ps:
        return
    with ThreadPoolExecutor(max_workers=min(len(ps), 5)) as ex:
        list(ex.map(refresh, ps))
    live = sum(1 for p in ps if (_CACHE.get(p) or Quote(p, 0, None)).ok)
    log.info("crypto quotes primed: %s/%s live", live, len(ps))


PRIME_TIMEOUT = float(os.getenv("CRYPTO_PRIME_TIMEOUT_S", "20"))


async def prime(timeout: float | None = None) -> bool:
    """Populate every pair once, before the app starts serving.

    Without this, the first requests after a process start each burn the full
    COLD_WAIT budget waiting on a cache the background refresher has not filled
    yet -- measured at 3.1s for Market Data and 5.7s for Market Risk on a
    restart, which is exactly the "slow screens" complaint, just relocated from
    the steady state to the minute after a deploy.

    Startup is the correct place to pay it: it happens once, no user is waiting
    on a request, and afterwards no reader ever meets a cold cache. Bounded, so
    unreachable venues delay the boot by `timeout` rather than hanging it -- a
    console that starts with crypto marked unavailable is strictly better than
    one that never starts.
    """
    timeout = PRIME_TIMEOUT if timeout is None else timeout
    try:
        await asyncio.wait_for(asyncio.to_thread(refresh_all), timeout)
        return True
    except asyncio.TimeoutError:
        log.warning("crypto prime timed out after %.0fs; serving unavailable "
                    "until the refresher catches up", timeout)
    except Exception as e:                       # noqa: BLE001
        log.warning("crypto prime failed: %s: %s", type(e).__name__, e)
    return False


async def run() -> None:
    """Refresh one pair per tick, round-robin.

    Refreshing all five at once is the obvious implementation and it was
    measurably wrong. `load_venues` already fans out one thread per venue, so
    a full pass put 5 x 4 = 20 concurrent book fetches in flight, and the JSON
    parsing of twenty order books holds the GIL long enough to stall requests
    that touch none of it: a blotter with a 1.2ms median measured 2.4s every
    time the burst landed. Taking the fetch off the request path is pointless
    if the fetch then periodically freezes the request path anyway.

    One pair per tick keeps at most one fan-out (4 connections) in flight --
    no more than a single old request used to cause -- while still giving each
    pair a full refresh every INTERVAL. It is also better manners toward free
    public venue endpoints than a periodic thundering herd.
    """
    if INTERVAL <= 0:
        log.info("crypto quote refresher disabled")
        return
    # `prime()` has already run from the startup hook by the time this task is
    # scheduled, so the round-robin starts against a populated cache.
    i = 0
    while True:
        ps = pairs()
        if not ps:
            await asyncio.sleep(INTERVAL)
            continue
        pair = ps[i % len(ps)]
        i += 1
        try:
            # to_thread: these are blocking network calls, and running them on
            # the event loop would stall every concurrent request for as long
            # as the fan-out takes -- the exact problem this module exists to
            # remove from the request path.
            await asyncio.to_thread(refresh, pair)
        except Exception as e:                   # noqa: BLE001
            log.warning("crypto quote refresh failed (%s): %s: %s",
                        pair, type(e).__name__, e)
        # Spread the pairs evenly across the interval rather than bursting.
        await asyncio.sleep(INTERVAL / len(ps))
