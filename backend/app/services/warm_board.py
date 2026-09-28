"""Keeps the end-of-day store current.

This used to be a cache warmer: it re-fetched everything every 4 minutes to
stay just inside a 5-minute in-process TTL, because the request path went to
the network and an expired entry meant a ~15s screen.

The data now lives in SQLite (see core.marketdata.store), so this is a
REFRESHER, not a warmer. The difference matters in three ways:

  * The request path no longer depends on it. If this task dies, screens keep
    rendering from the database -- they just stop getting newer data, which
    the Market Data screen reports rather than hides.
  * A restart costs nothing. The store is on the same docker volume as the
    OMS database, so a redeploy starts warm instead of paying ~20s for the
    Treasury curve on the first visitor.
  * The interval is set by the DATA, not by a cache TTL. These are end-of-day
    series that change once a day; polling them every 4 minutes was 288
    re-downloads a day of a file that moves once. Thirty minutes is prompt
    enough to pick up a publication and far politer to free public endpoints.

Crypto is not here. Those quotes are live and only meaningful at the moment
they are read, so they have their own short-interval refresher and are never
persisted as EOD -- see services.crypto_quotes.

Disable with WARM_BOARD_SECONDS=0 (reads then fall back to fetching on demand
the first time a series is missing, and persisting what they fetch).
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

# EOD data changes once a day; this only has to be prompt, not tight.
INTERVAL = int(os.getenv("WARM_BOARD_SECONDS", "1800"))


def _refresh_fred() -> None:
    """FRED: every series in the instrument master, plus anything held.

    The universe is the superset the board renders; held symbols are added
    because a position can sit in a series the board does not carry, and the
    risk screens read exactly the held set. Series are refreshed one at a time
    -- forty concurrent connections to a free public service to save a few
    seconds of BACKGROUND work would be rude for no user-visible gain.
    """
    from ..core.marketdata import universe as U
    from ..core.marketdata.traditional import refresh_fred_series
    from ..core.marketdata import series_map
    from ..api.oms import oms

    sids = {i.series_id for i in U.UNIVERSE if i.source == "fred"}
    try:
        smap = series_map()
        held = {p.symbol.upper() for p in oms().store.list_positions()}
        sids |= {smap[s] for s in held if s in smap}
    except Exception as e:                       # noqa: BLE001
        log.warning("held-symbol lookup failed: %s: %s", type(e).__name__, e)
    sids.add("SP500")                            # benchmark, always needed

    written = ok = 0
    for sid in sorted(sids):
        try:
            written += refresh_fred_series(sid)
            ok += 1
        except Exception as e:                   # noqa: BLE001
            log.warning("fred refresh failed (%s): %s: %s", sid, type(e).__name__, e)
    log.info("fred refreshed: %s/%s series, %s row(s) new or revised",
             ok, len(sids), written)


def _refresh_treasury(first: bool) -> None:
    """US Treasury par curve. `first` also pulls the prior year, so the factor
    model has two years of curve on a fresh database."""
    from ..core.marketdata.traditional import refresh_treasury
    try:
        n = refresh_treasury(backfill_prior_year=first)
        log.info("treasury refreshed: %s row(s) new or revised", n)
    except Exception as e:                       # noqa: BLE001
        log.warning("treasury refresh failed: %s: %s", type(e).__name__, e)


def _refresh_ecb() -> None:
    from ..core.marketdata.traditional import refresh_ecb
    try:
        n = refresh_ecb()
        log.info("ecb refreshed: %s row(s) new or revised", n)
    except Exception as e:                       # noqa: BLE001
        log.warning("ecb refresh failed: %s: %s", type(e).__name__, e)


def _cycle(first: bool = False) -> None:
    """One refresh pass. Never raises: a failed refresh must degrade to
    slightly older data, never to a dead API.

    The three sources are independent and live on different hosts, so they run
    concurrently. Sequentially the FIRST pass costs their sum, and on an empty
    database the Treasury leg alone is ~20s -- dead time straight after a
    deploy, during which Market Data and Rates & Fixed Income are exactly as
    slow as they were before any of this work. Concurrent, that window is the
    slowest leg instead of the total. Later passes are cheap either way.

    Three workers against three different public hosts is one connection each,
    which is the polite version of this, not the greedy one.
    """
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(lambda f: f(), (
            _refresh_fred,
            lambda: _refresh_treasury(first),
            _refresh_ecb)))
    try:
        from ..core.marketdata.store import eod
        log.info("refresh pass complete: %s", eod().coverage())
    except Exception:                            # noqa: BLE001
        log.info("refresh pass complete")


async def run() -> None:
    if INTERVAL <= 0:
        log.info("EOD refresher disabled")
        return
    first = True
    while True:
        # to_thread: the fetches are blocking urllib calls, and running them
        # on the event loop would stall every concurrent request for as long
        # as the pass takes.
        await asyncio.to_thread(_cycle, first)
        first = False
        await asyncio.sleep(INTERVAL)
