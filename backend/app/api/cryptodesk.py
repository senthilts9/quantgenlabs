"""Crypto Desk API — market data, funding, and pre-trade validation.

**There is deliberately no order-submission endpoint here, and adding one
would be a mistake.** This service runs on a publicly reachable host with
`allow_origins=["*"]` and no authentication. The standalone desk UI that backs
this module refuses to bind to anything but loopback for exactly that reason:
an order-entry surface with no caller identity is not something to expose,
and the testnet-only guard limits the blast radius without removing it.

What is exposed is everything that reads or computes without sending:

* venue capabilities, so the screen can say which venues actually match orders
  and which return fixtures;
* instrument specifications pulled from each venue's own metadata;
* order books and live funding rates;
* the **validation pipeline** — 45 rules across six stages — run against a
  hypothetical order. Nothing is transmitted; the caller gets back both what a
  retail app would show and what a desk would see.

Everything is imported defensively, matching `app.core.engines`: if the
vendored package is missing or fails to load, the router still mounts and
reports itself unavailable rather than taking the whole API down at boot.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .cryptodesk_auth import require_actor

router = APIRouter(prefix="/api/cryptodesk", tags=["crypto-desk"])

# --- defensive import -------------------------------------------------------
# A missing vendored package degrades this one screen, it does not stop the
# console from booting.
try:
    from cryptodesk import __version__ as CD_VERSION, describe_guard
    from cryptodesk.config import Settings
    from cryptodesk.core import (
        InstrumentRegistry, OrderRequest, OrderType, Quote, Side,
    )
    from cryptodesk.core.types import TimeInForce
    from cryptodesk.validation import for_venue as validator_for
    from cryptodesk.venues import VENUES
    AVAILABLE = True
    IMPORT_ERROR = None
except Exception as exc:                                    # pragma: no cover
    AVAILABLE = False
    IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)
    CD_VERSION = None

#: Venues worth surfacing. The mocked sandbox is excluded: it returns fixtures,
#: and a screen that showed its "fills" beside real ones would be misleading.
PUBLIC_VENUES = ("paper", "binance", "paradex", "hyperliquid")

#: Instrument metadata is a ~1300-row fetch on some venues. Cache it.
_CACHE: Dict[str, Any] = {}
_CACHE_TTL_S = 300.0


def _require() -> None:
    if not AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="crypto desk unavailable: %s" % IMPORT_ERROR)


def _venue(name: str):
    """Construct (and cache) a venue plus its instrument registry."""
    _require()
    if name not in PUBLIC_VENUES:
        raise HTTPException(400, "unknown venue %r; known: %s"
                            % (name, ", ".join(PUBLIC_VENUES)))
    hit = _CACHE.get(name)
    if hit and time.time() - hit["at"] < _CACHE_TTL_S:
        return hit["venue"], hit["registry"]

    registry = InstrumentRegistry()
    try:
        if name == "paper":
            venue = VENUES[name](registry, starting_collateral=Decimal("100000"))
            _seed_paper(venue)
        else:
            venue = VENUES[name](registry=registry)
            registry.refresh(venue)
    except Exception as exc:
        raise HTTPException(502, "venue %s unreachable: %s"
                            % (name, type(exc).__name__))

    _CACHE[name] = {"venue": venue, "registry": registry, "at": time.time()}
    return venue, registry


def _seed_paper(venue) -> None:
    """Give the local engine live depth so its slippage is not a fiction."""
    from cryptodesk.core.types import OrderBook
    for market in ("BTC-USD-PERP", "ETH-USD-PERP"):
        try:
            book = venue.seed_from_feed(market.replace("-PERP", ""), depth=25)
            venue.seed(OrderBook(market, book.bids, book.asks,
                                 book.timestamp_ms, book.source))
        except Exception:
            continue


# ---------------------------------------------------------------------------

@router.get("/health")
def health() -> dict:
    """Availability, network guard, and an explicit statement of what is not here."""
    if not AVAILABLE:
        return {"available": False, "error": IMPORT_ERROR}
    settings = Settings.from_env()
    return {
        "available": True,
        "version": CD_VERSION,
        "guard": describe_guard(settings),
        "network": settings.network,
        "mainnet_unlocked": settings.mainnet_unlocked,
        "order_submission": False,
        "order_submission_note": (
            "Deliberately absent. This host is public with open CORS and no "
            "authentication; order entry without caller identity is not "
            "something to expose. Validation runs here, sending does not."
        ),
    }


@router.get("/venues")
def venues() -> dict:
    """Which venues match orders for real, and which return fixtures."""
    _require()
    out = []
    for name in PUBLIC_VENUES:
        try:
            caps = VENUES[name]().capabilities()
        except Exception as exc:
            out.append({"venue": name, "error": type(exc).__name__})
            continue
        out.append({
            "venue": name,
            "trades": caps["supports_trading"],
            "matching": ("mock" if caps["is_mock"]
                         else ("real" if caps["has_matching_engine"] else "none")),
            "signed_orders": caps["requires_signature"],
            "notes": caps["notes"],
        })
    return {"venues": out}


@router.get("/markets")
def markets(venue: str = Query("paradex"), limit: int = Query(60, le=400)) -> dict:
    """Instrument specifications, as the venue itself publishes them.

    These are the constraints the validator enforces - tick, step, minimum
    notional, price band, leverage - read from venue metadata rather than
    hardcoded, so the screen agrees with the exchange instead of approximating.
    """
    _, registry = _venue(venue)
    rows = []
    for symbol in registry.symbols()[:limit]:
        inst = registry.require(symbol)
        rows.append({
            "symbol": symbol,
            "kind": inst.kind.value,
            "tick_size": str(inst.tick_size),
            "step_size": str(inst.step_size),
            "min_size": str(inst.min_size),
            "max_size": str(inst.max_size),
            "min_notional": str(inst.min_notional),
            "price_band_bps": str(inst.price_band_bps),
            "max_leverage": str(inst.max_leverage.quantize(Decimal("1"))),
            "initial_margin": str(inst.im_fraction),
            "maintenance_margin": str(inst.mm_fraction),
            "funding_period_hours": inst.funding_period_hours,
            # An order ticket that prices fees and caps slippage off constants
            # compiled into the browser is quoting a different venue than the
            # one it sends to. These travel so the estimate is the venue's.
            "maker_fee_bps": str(inst.maker_fee_bps),
            "taker_fee_bps": str(inst.taker_fee_bps),
            "max_slippage_bps": (None if inst.max_slippage_bps is None
                                 else str(inst.max_slippage_bps)),
            "max_position_size": (None if inst.max_position_size is None
                                  else str(inst.max_position_size)),
            # Venues differ on this and the difference is not cosmetic:
            # hyperliquid publishes no stop types, so a ticket offering
            # STOP_MARKET there collects a trigger price and then fails
            # validation on the last step. Sent so the screen can say no first.
            "supported_order_types": sorted(inst.supported_order_types),
        })
    return {"venue": venue, "count": len(rows), "markets": rows}


@router.get("/book")
def book(venue: str = Query("paper"),
         market: str = Query("BTC-USD-PERP"),
         depth: int = Query(8, le=25)) -> dict:
    v, _ = _venue(venue)
    try:
        ob = v.fetch_book(market, depth=depth)
    except Exception as exc:
        raise HTTPException(502, "book unavailable: %s" % type(exc).__name__)
    try:
        quote = v.fetch_quote(market)
    except Exception:
        quote = Quote(market, ob.mid or Decimal(0), source="book-mid")
    return {
        "market": market, "venue": venue, "source": ob.source,
        "bids": [{"price": str(l.price), "size": str(l.size)} for l in ob.bids],
        "asks": [{"price": str(l.price), "size": str(l.size)} for l in ob.asks],
        "mid": str(ob.mid) if ob.mid else None,
        "mark": str(quote.price),
        "spread_bps": (str(ob.spread_bps.quantize(Decimal("0.01")))
                       if ob.spread_bps is not None else None),
        "crossed": ob.is_crossed,
    }


@router.get("/funding")
def funding(market: str = Query("BTC-USD-PERP")) -> dict:
    """Live funding rates, annualised, across the perp venues.

    The period is reported alongside the rate because the same number means
    very different money without it: an identical per-period rate is eight
    times the annual cost when it settles hourly rather than eight-hourly.
    """
    _require()
    rows = []
    for name in ("paradex", "hyperliquid"):
        try:
            v, _ = _venue(name)
            rate = v.fetch_funding_rate(market)
        except Exception as exc:
            rows.append({"venue": name, "error": type(exc).__name__})
            continue
        if rate is None:
            rows.append({"venue": name, "error": "no rate published"})
            continue
        per_day = (rate.rate * (Decimal(24) / Decimal(rate.period_hours))
                   * Decimal(10000))
        rows.append({
            "venue": name,
            "rate": str(rate.rate),
            "period_hours": rate.period_hours,
            "apr_pct": str((rate.apr * 100).quantize(Decimal("0.01"))),
            "long_cost_per_10k_per_day": str((-per_day).quantize(Decimal("0.01"))),
            "direction": "longs pay shorts" if rate.rate > 0 else "shorts pay longs",
        })
    return {"market": market, "rates": rows}


# ---------------------------------------------------------------------------

class LifecycleRequest(BaseModel):
    venue: str = "paper"
    market: str = "BTC-USD-PERP"
    side: str = "BUY"
    size: str = "3.0"
    algo: str = "TWAP"
    slices: int = 6


#: Runs are persisted to the API container's data volume, not its filesystem.
#: A blotter that a `docker compose up` erases is a demo, not a record.
_DB_PATH = os.environ.get("CRYPTODESK_DB_PATH", "/app/data/cryptodesk.db")

#: Retention. This endpoint is publicly reachable, so the table grows at
#: whatever rate strangers call it; the oldest runs are also the least
#: interesting, since the aggregates people read are dominated by recent ones.
_KEEP_RUNS = int(os.environ.get("CRYPTODESK_KEEP_RUNS", "500"))

_STORE = None
_STORE_LOCK = threading.Lock()


def _store():
    """One Store for the process. Its own lock makes it thread-safe."""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                from cryptodesk.store import Store
                _STORE = Store(Path(_DB_PATH))
    return _STORE


def _new_parent_id() -> str:
    """Sortable by construction: time prefix, random suffix."""
    return "%013d-%s" % (int(time.time() * 1000), uuid.uuid4().hex[:8])


@router.get("/chain")
def chain(market: str = Query("BTC-USD"),
          chain: str = Query("sepolia", pattern="^(sepolia|ethereum|base-sepolia)$")) -> dict:
    """Live on-chain state, and the mark cross-check that depends on it.

    Reads a Chainlink aggregator with `eth_call` against a public RPC and
    compares it to the exchange mid. Two independent sources that do not share
    a failure mode: a stale socket at the venue does not make the chain agree,
    and a pushed print on one book does not move a decentralised feed.

    The block number is returned because it is what makes the read evidence
    rather than an assertion - `latestRoundData()` answers differently the
    moment the chain advances, so a divergence nobody can reconstruct later is
    not an audit trail.
    """
    _require()
    from cryptodesk.marketdata.feeds import best_effort_book
    from cryptodesk.risk.oracle import cross_check

    cex = None
    cex_source = None
    try:
        book = best_effort_book(market, depth=5)
        cex, cex_source = book.mid, book.source
    except Exception:
        pass

    try:
        check = cross_check(market, cex, chain=chain)
    except Exception as exc:
        # Surface the failure. A control that reports "ok" when it could not
        # run is worse than no control.
        raise HTTPException(502, "chain read failed: %s: %s"
                            % (type(exc).__name__, exc))
    if check is None:
        raise HTTPException(404, "no on-chain aggregator configured for %r" % market)

    out = check.to_dict()
    out["cex_source"] = cex_source
    try:
        st = _store()
        out["observation_id"] = st.record_chain_observation({
            "observed_ms": check.observed_ms, "chain": check.chain,
            "block": check.block, "market": check.market,
            "source": check.oracle_source, "price": check.oracle_price,
            "cex_price": check.cex_price, "divergence_bps": check.divergence_bps})
        st.prune_chain_observations()
    except Exception as exc:
        out["observation_id"] = None
        out["persist_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


@router.get("/chain/history")
def chain_history(market: Optional[str] = None, limit: int = Query(100, le=500)) -> dict:
    """Stored oracle observations, newest first."""
    _require()
    return {"observations": _store().list_chain_observations(limit=limit, market=market)}


@router.get("/runs")
def runs(market: Optional[str] = None, limit: int = Query(50, le=200)) -> dict:
    """Every stored parent order, newest first, with its TCA joined on."""
    _require()
    return {"runs": _store().list_algo_parents(limit=limit, market=market)}


@router.get("/runs/{parent_id}")
def run_detail(parent_id: str) -> dict:
    """One parent with its children and fills, read back from the database."""
    _require()
    row = _store().get_algo_run(parent_id)
    if row is None:
        raise HTTPException(404, "no run %r" % parent_id)
    return row


@router.get("/stats")
def stats() -> dict:
    """Aggregates across stored runs.

    Execution quality is a distribution, not a number: one TWAP beating
    arrival by 0.4 bps is noise, ninety with a median of 0.4 bps is a result.
    That claim only exists because the runs are kept.
    """
    _require()
    out = _store().algo_stats()
    out["db_path"] = _DB_PATH
    out["retained_runs"] = _KEEP_RUNS
    return out


@router.post("/lifecycle")
def lifecycle(req: LifecycleRequest,
              actor: str = Depends(require_actor)) -> dict:
    """Walk one parent order through the entire trade lifecycle, and keep it.

    Each stage is returned as its own block so the screen can render the chain
    rather than a single verdict: on-chain mark validation, market data,
    pre-trade risk, the child schedule, the fills those children produced, the
    resulting position, TCA against arrival, reconciliation, and the row ids
    proving the run was persisted.

    Everything executes against the **local simulator**. Nothing is sent to any
    exchange, which is why this endpoint can exist on a public host at all -
    the same reason `/validate` does and no `/submit` does. The market data,
    the oracle read and the block number are real.
    """
    _require()
    from cryptodesk.core import InstrumentRegistry, OrderRequest, OrderType, Side
    from cryptodesk.core.types import OrderBook, now_ms
    from cryptodesk.execution import TWAP, VWAP, AlgoRunner, MarketTrade, analyse
    from cryptodesk.execution.oms import OrderManager
    from cryptodesk.marketdata.feeds import best_effort_book
    from cryptodesk.marketdata.http import HttpClient
    from cryptodesk.risk.limits import RiskEngine, RiskLimits
    from cryptodesk.risk.oracle import cross_check
    from cryptodesk.venues import PaperVenue

    market = req.market
    spot = market.replace("-PERP", "")
    try:
        side = Side(req.side.upper())
        size = Decimal(req.size)
    except (ValueError, InvalidOperation):
        raise HTTPException(400, "bad side or size")
    if size <= 0:
        raise HTTPException(400, "size must be positive")

    t0 = now_ms()
    seed = best_effort_book(spot, depth=25)
    arrival = seed.mid
    if arrival is None:
        raise HTTPException(502, "no market data for %s" % market)

    reg = InstrumentRegistry()
    inst = reg.get(market)
    if inst is None:
        raise HTTPException(400, "unknown market %r" % market)
    venue = PaperVenue(reg, starting_collateral=Decimal("50000000"))
    venue.seed(OrderBook(market, list(seed.bids), list(seed.asks),
                         seed.timestamp_ms, seed.source))

    # --- 1. market data -------------------------------------------------
    stage_market = {
        "source": seed.source,
        "bid": str(seed.best_bid), "ask": str(seed.best_ask),
        "mid": str(arrival),
        "spread_bps": (str(seed.spread_bps.quantize(Decimal("0.01")))
                       if seed.spread_bps is not None else None),
        "depth_levels": len(seed.bids) + len(seed.asks),
    }

    # --- 2. on-chain mark validation ------------------------------------
    # This gates the risk stage below rather than decorating the screen. If
    # the venue mark disagrees with an independent on-chain oracle, every
    # number downstream - notional, margin, liquidation price, P&L - is
    # computed off a price one of the two sources has wrong.
    stage_chain = {"available": False, "gating": True}
    oracle_ok = None
    try:
        check = cross_check(spot, arrival)
        if check is None:
            stage_chain = {"available": False, "gating": True,
                           "note": "no on-chain aggregator for %s; mark is "
                                   "unverified rather than verified" % spot}
        else:
            stage_chain = {"available": True, "gating": True, **check.to_dict()}
            oracle_ok = check.ok
            try:
                st = _store()
                stage_chain["observation_id"] = st.record_chain_observation({
                    "observed_ms": check.observed_ms, "chain": check.chain,
                    "block": check.block, "market": check.market,
                    "source": check.oracle_source, "price": check.oracle_price,
                    "cex_price": check.cex_price,
                    "divergence_bps": check.divergence_bps})
            except Exception:
                stage_chain["observation_id"] = None
    except Exception as exc:
        stage_chain = {"available": False, "gating": True,
                       "error": "%s: %s" % (type(exc).__name__, exc),
                       "note": "RPC unreachable - the mark is unverified. The "
                               "control did not run, which is not the same as "
                               "the control passing."}

    # --- 3. pre-trade risk ----------------------------------------------
    # The mark must be supplied: a MARKET order carries no price, and the risk
    # engine fails closed when it cannot value an order against a configured
    # notional limit.
    risk = RiskEngine(RiskLimits(max_order_notional=Decimal("100000000"),
                                 max_position_notional=Decimal("100000000"),
                                 max_gross_notional=Decimal("100000000"),
                                 require_real_market_data=False))
    parent_req = OrderRequest(market, side, OrderType.MARKET, size)
    mark_quote = Quote(market, arrival, seed.timestamp_ms, seed.source)
    rejections = risk.check(parent_req, mark=mark_quote, venue=req.venue,
                            book_source=seed.source)
    checks = [r.to_dict() for r in rejections]
    if oracle_ok is False:
        checks.append({"code": "ORACLE_DIVERGENCE", "stage": "MARKET",
                       "message": stage_chain.get("reason") or "on-chain cross-check failed",
                       "severity": "reject"})
    stage_risk = {
        "cleared": not checks,
        "checks": checks,
        "notional": str((size * arrival).quantize(Decimal("0.01"))),
        "mark_verified_on_chain": bool(oracle_ok),
    }

    # --- 4. schedule the parent into children ---------------------------
    algo = VWAP() if req.algo.upper() == "VWAP" else TWAP()
    oms = OrderManager(venue, RiskEngine(RiskLimits.permissive()))
    runner = AlgoRunner(oms, algo)
    parent = runner.create(parent_req, inst, arrival,
                           slices=max(1, min(req.slices, 24)),
                           duration_ms=300000, arrival_price=arrival,
                           start_ms=t0)
    stage_schedule = {
        "algo": algo.name,
        "slices": [{"index": s.index, "size": str(s.size),
                    "weight": str(s.weight.quantize(Decimal("0.0001")))}
                   for s in parent.slices],
        "sums_to_parent": parent.scheduled_size == parent.total_size,
    }

    # --- 5. execute: ack and fill ---------------------------------------
    runner.run(parent)
    fills = [f for c in parent.children for f in c.fills]
    stage_execution = {
        "children": [c.summary() for c in parent.children],
        "fills": [{"size": str(f.size), "price": str(f.price),
                   "fee": str(f.fee), "liquidity": f.liquidity}
                  for f in fills],
        "filled_size": str(parent.filled_size),
        "avg_price": str(parent.avg_price) if parent.avg_price else None,
        "status": parent.status.value,
    }

    # --- 6. position and P&L --------------------------------------------
    account = venue.fetch_account()
    pos = account.position(market)
    stage_position = {
        "size": str(pos.size), "entry_price": str(pos.entry_price),
        "mark_price": str(pos.mark_price),
        "unrealised_pnl": str(pos.unrealised_pnl.quantize(Decimal("0.01"))),
        "free_collateral": str(account.free_collateral.quantize(Decimal("0.01"))),
        "margin_ratio": (str(venue.margin_ratio().quantize(Decimal("0.001")))
                         if venue.margin_ratio() else None),
        "liquidation_price": None,
    }
    liq = pos.liquidation_price(account.equity, inst.mm_fraction)
    if liq is not None:
        stage_position["liquidation_price"] = str(liq.quantize(Decimal("0.01")))

    # --- 7. TCA ----------------------------------------------------------
    trades = []
    try:
        raw = HttpClient().get(
            "https://api.exchange.coinbase.com/products/%s/trades" % spot)
        trades = [MarketTrade(Decimal(t["price"]), Decimal(t["size"]),
                              t0 + i * 10) for i, t in enumerate(raw[:300])]
    except Exception:
        pass
    tca = analyse(market, side, fills, arrival_price=arrival,
                  trades=trades or None, start_ms=t0, end_ms=t0 + 300000)
    stage_tca = tca.to_dict()
    stage_tca["benchmark_prints"] = len(trades)

    # --- 8. reconciliation ------------------------------------------------
    stage_recon = {k: v for k, v in oms.reconcile().items() if v}
    stage_recon["clean"] = not stage_recon
    stage_recon["blotter_rows"] = len(oms.blotter())

    # --- 9. persist --------------------------------------------------------
    # One transaction for the parent, its children, its fills and its TCA. A
    # parent stored without its slices would make reconciliation report orders
    # live at a venue that never received them.
    parent_id = _new_parent_id()
    stage_persist = {"parent_id": parent_id, "stored": False}
    try:
        st = _store()
        child_rows, fill_rows = [], []
        for idx, slc in enumerate(parent.slices):
            child = parent.children[idx] if idx < len(parent.children) else None
            child_rows.append({
                "slice_index": slc.index,
                "scheduled_ms": getattr(slc, "scheduled_ms", t0),
                "scheduled_size": slc.size, "weight": slc.weight,
                "client_id": getattr(child, "client_order_id", None) if child else None,
                "status": (child.status.value if child and hasattr(child.status, "value")
                           else "UNFILLED"),
                "filled_size": getattr(child, "filled_size", Decimal(0)) if child else Decimal(0),
                "avg_price": getattr(child, "avg_price", None) if child else None})
            if child:
                for f in child.fills:
                    fill_rows.append({
                        "slice_index": slc.index,
                        "ts_ms": getattr(f, "timestamp_ms", t0),
                        "size": f.size, "price": f.price, "fee": f.fee,
                        "liquidity": f.liquidity})
        st.save_algo_run(
            {"parent_id": parent_id, "created_ms": t0, "venue": req.venue,
             "market": market, "side": side.value, "algo": algo.name,
             "total_size": size, "slice_count": len(parent.slices),
             "duration_ms": 300000, "arrival_price": arrival,
             "status": parent.status.value, "filled_size": parent.filled_size,
             "avg_price": parent.avg_price, "completed_ms": now_ms(),
             # The address that signed in. This is the whole reason the write
             # path is gated: a run stored without it is a row, not an audit
             # entry.
             "actor": actor,
             # Always 'simulated' here: /lifecycle executes against PaperVenue
             # regardless of what venue label the request carried. Recording
             # the request's venue as the network would be the exact confusion
             # this column exists to prevent.
             "network": "simulated"},
            child_rows, fill_rows,
            {"computed_ms": now_ms(), "arrival_price": arrival,
             "avg_price": parent.avg_price or Decimal(0),
             "implementation_shortfall_bps": tca.implementation_shortfall_bps,
             "vwap_slippage_bps": tca.vwap_slippage_bps,
             "fee_bps": tca.fee_bps, "total_cost_bps": tca.total_cost_bps,
             # `TCAResult` exposes `participation_rate` as a *fraction*; only
             # the serialised dict carries the `_pct` name. Reading the dict's
             # key off the object raised AttributeError inside the persist
             # block, which is caught below - so every run executed, filled,
             # and was then silently dropped, leaving `/runs` permanently
             # empty while the endpoint reported a clean fill.
             "participation_rate_pct": (
                 None if tca.participation_rate is None
                 else tca.participation_rate * 100),
             "benchmark_prints": len(trades)})
        pruned = st.prune_algo_runs(_KEEP_RUNS)
        stage_persist.update({
            "stored": True, "children_rows": len(child_rows),
            "fill_rows": len(fill_rows), "pruned": pruned,
            "readback": "/api/cryptodesk/runs/%s" % parent_id})
    except Exception as exc:
        stage_persist["error"] = "%s: %s" % (type(exc).__name__, exc)

    return {
        "parent_id": parent_id,
        "market": market, "side": side.value, "size": str(size),
        "arrival_price": str(arrival),
        "stages": {
            "market_data": stage_market,
            "chain": stage_chain,
            "pre_trade_risk": stage_risk,
            "schedule": stage_schedule,
            "execution": stage_execution,
            "position": stage_position,
            "tca": stage_tca,
            "reconciliation": stage_recon,
            "persistence": stage_persist,
        },
        "actor": actor,
        "simulated": True,
        "sent_to_exchange": False,
    }


class ValidateRequest(BaseModel):
    venue: str = "paper"
    market: str = "BTC-USD-PERP"
    side: str = "BUY"
    type: str = "LIMIT"
    size: str = "0.01"
    price: Optional[str] = None
    post_only: bool = False
    reduce_only: bool = False
    # Rules already exist for all three - trigger presence, the slippage cap
    # walked against real depth, and the leverage/initial-margin check. They
    # were unreachable from here because the request could not express them,
    # so a ticket offering leverage and a slippage cap was having neither
    # validated. An unvalidated field on an order ticket is worse than an
    # absent one: it looks checked.
    trigger_price: Optional[str] = None
    max_slippage_bps: Optional[str] = None
    leverage: Optional[str] = None


@router.post("/validate")
def validate(req: ValidateRequest) -> dict:
    """Run a hypothetical order through the full pre-trade pipeline.

    **Nothing is transmitted to any venue.** The order is constructed, checked
    against the venue's own published constraints and live market data, and
    discarded.

    The response is deliberately two-layered. `retail` is the single line an
    exchange app would surface; `desk` is every rule that fired, with the
    machine-readable codes an execution system branches on. Same order, same
    engine — the difference between the two *is* the point of the screen.
    """
    _require()
    v, registry = _venue(req.venue)

    def dec_or_none(raw: Optional[str]) -> Optional[Decimal]:
        if raw is None or not str(raw).strip():
            return None
        try:
            return Decimal(str(raw).strip())
        except (InvalidOperation, ValueError):
            raise HTTPException(400, "not a number: %r" % raw)

    size = dec_or_none(req.size)
    if size is None:
        raise HTTPException(400, "size is required")
    try:
        order = OrderRequest(
            market=req.market, side=Side(req.side.upper()),
            order_type=OrderType(req.type.upper()), size=size,
            price=dec_or_none(req.price),
            trigger_price=dec_or_none(req.trigger_price),
            max_slippage_bps=dec_or_none(req.max_slippage_bps),
            leverage=dec_or_none(req.leverage),
            time_in_force=TimeInForce.GTC,
            post_only=req.post_only, reduce_only=req.reduce_only,
            signature="0xvalidation-only",
        )
    except ValueError as exc:
        # An unknown order type reaches here as a ValueError from the enum, and
        # the bare message ("'STOP' is not a valid OrderType") does not tell a
        # caller what is valid. List them.
        raise HTTPException(400, "%s; order types: %s"
                            % (exc, ", ".join(t.value for t in OrderType)))

    book_snapshot = mark = account = None
    try:
        book_snapshot = v.fetch_book(req.market, depth=10)
        mark = v.fetch_quote(req.market)
    except Exception:
        pass
    try:
        account = v.fetch_account()
    except Exception:
        pass

    validator = validator_for(req.venue, registry)
    repaired, report, changes = validator.validate_and_repair(
        order, book=book_snapshot, mark=mark, account=account)

    accepted = report.ok
    return {
        "retail": {
            "accepted": accepted,
            "message": ("Order accepted" if accepted
                        else "Order failed. Please check your order details."),
        },
        "desk": {
            "repairs": changes,
            "rejections": [r.to_dict() for r in report.rejections],
            "warnings": report.warnings,
            "order": repaired.to_dict(),
            "stages": [s.value for s in validator.stages],
            "rules_evaluated": sum(len(r) for r in
                                   validator.rules_by_stage.values()),
            "market_source": book_snapshot.source if book_snapshot else None,
        },
        "submitted": False,
    }


# ---------------------------------------------------------------------------
# Crypto market risk: open public data, in-house engines.
#
# The return series comes from the vendored desk package (stdlib, public
# venue candles, no key). Every number computed from it comes from the
# in-house engines already in this codebase -- `core.risk` for the TradFi
# measures and `core.crypto.risk` for the fat-tailed ones. Nothing here
# reimplements a estimator that already exists.
#
# Two choices below are load-bearing and easy to get silently wrong:
#
# **Spot history backs perp risk.** Hyperliquid publishes a book, not
# candles; Coinbase publishes candles for the spot pair. `BTC-USD-PERP` risk
# is therefore computed from `BTC-USD` spot returns. Over a 1-day horizon the
# perp/spot basis is second-order next to a 2.3% daily sigma, but the
# substitution is declared in the response rather than hidden.
#
# **The backtest is out-of-sample.** Each day's VaR forecast is fitted only
# to data strictly before that day. An in-sample backtest -- fitting to the
# whole series and then counting breaches of it -- is the single most common
# way to publish a VaR model that looks well-calibrated and is not.
# ---------------------------------------------------------------------------

_RISK_CACHE: Dict[str, Any] = {}
_RISK_TTL_S = 300.0
_RISK_LOCK = threading.Lock()

#: Warm-up before the first out-of-sample forecast. Below this a quantile at
#: 99% is being read off too few points to mean anything.
_MIN_WARMUP = 120


def _spot_for(market: str) -> str:
    """Candle symbol for a market. Perps borrow their spot pair's history."""
    m = market.upper()
    for suffix in ("-PERP", "-PERPETUAL", "-SWAP"):
        if m.endswith(suffix):
            return m[: -len(suffix)]
    return m


def _series_cached(market: str, granularity: int):
    """Return series with a short TTL - candles do not move intraday."""
    key = "%s:%d" % (market, granularity)
    now = time.time()
    with _RISK_LOCK:
        hit = _RISK_CACHE.get(key)
        if hit and now - hit[0] < _RISK_TTL_S:
            return hit[1]
    from cryptodesk.marketdata.history import return_series
    series = return_series(market, granularity_s=granularity, kind="simple")
    with _RISK_LOCK:
        _RISK_CACHE[key] = (now, series)
    return series


@router.get("/risk")
def crypto_risk(
    market: str = Query("BTC-USD-PERP"),
    granularity: int = Query(86400, description="candle seconds: 3600 or 86400"),
    confidence: float = Query(0.99, gt=0.5, lt=1.0),
    notional: float = Query(1_000_000.0, gt=0),
    backtest: bool = Query(True),
) -> dict:
    """VaR/ES for one crypto market across every estimator in the house.

    Public candles in, in-house numpy/C++ engines out. Returns the TradFi
    measures (historical, parametric, Cornish-Fisher, Monte Carlo) beside the
    crypto-native ones (EVT peaks-over-threshold, EWMA-scaled historical), so
    the gap between a normal assumption and a fitted tail is visible rather
    than asserted.
    """
    _require()

    import numpy as np

    from ..core import risk as R
    from ..core.crypto import risk as CR
    from ..obs import timed

    spot = _spot_for(market)
    try:
        series = _series_cached(spot, granularity)
    except Exception as exc:
        raise HTTPException(502, "candle fetch failed for %s: %s: %s"
                            % (spot, type(exc).__name__, exc))

    r = np.asarray(series.returns, float)
    if r.size < _MIN_WARMUP:
        raise HTTPException(
            422, "only %d returns for %s; %d needed before a %.0f%% quantile "
                 "is meaningful" % (r.size, spot, _MIN_WARMUP, confidence * 100))

    bars_per_day = 86400.0 / granularity
    sigma = float(r.std(ddof=1))
    ewma_sigma = CR.ewma_vol(r)
    n = r.size
    mu = float(r.mean())
    skew = float(np.sum((r - mu) ** 3) / n / sigma ** 3)
    ex_kurt = float(np.sum((r - mu) ** 4) / n / sigma ** 4 - 3.0)

    methods = []

    def record(name, family, var, es, extra=None):
        row = {
            "method": name, "family": family,
            "var_pct": round(var * 100, 4), "es_pct": round(es * 100, 4),
            "var_currency": round(var * notional, 2),
            "es_currency": round(es * notional, 2),
        }
        if extra:
            row.update(extra)
        methods.append(row)

    with timed("crypto_var", "numpy"):
        for name, fn in sorted(R.VAR_METHODS.items()):
            res = fn(r, confidence)
            # Cornish-Fisher reports no separate ES - the estimator adjusts a
            # quantile, it does not integrate the tail beyond it. Say so
            # rather than printing VaR twice and implying they were both
            # measured.
            es = None if name == "cornish_fisher" else res.es
            record(name, "tradfi", res.var, res.es,
                   {"es_measured": es is not None})

        hist = CR.historical_var(r, confidence)
        evt = CR.evt_var(r, confidence)
        ewma = CR.ewma_scaled_var(r, confidence)
        record(evt.method, "crypto", evt.var, evt.es, {
            "es_measured": True,
            # xi > 0 is a heavy (Frechet) tail: the fitted distribution has
            # infinite higher moments, which is the formal statement of what
            # the excess kurtosis above is hinting at.
            "gpd_xi": None if evt.xi is None else round(evt.xi, 5),
            "gpd_beta": None if evt.beta is None else round(evt.beta, 8),
            "threshold_pct": None if evt.threshold is None else round(evt.threshold * 100, 4),
            "n_exceedances": evt.n_exceed,
        })
        record(ewma.method, "crypto", ewma.var, ewma.es, {"es_measured": True})

    backtests = []
    if backtest:
        # Out-of-sample: forecast day t from returns strictly before t.
        with timed("crypto_var_backtest", "numpy"):
            warmup = max(_MIN_WARMUP, int(n * 0.4))
            if n - warmup >= 30:
                realised = r[warmup:]
                for name, fn in (("historical", CR.historical_var),
                                 ("ewma_scaled", CR.ewma_scaled_var),
                                 ("evt_pot", CR.evt_var)):
                    forecasts = np.empty(n - warmup, float)
                    for i in range(warmup, n):
                        try:
                            forecasts[i - warmup] = fn(r[:i], confidence).var
                        except Exception:
                            forecasts[i - warmup] = np.nan
                    ok = ~np.isnan(forecasts)
                    if ok.sum() < 30:
                        continue
                    bt = R.backtest_var(realised[ok], forecasts[ok], confidence)
                    backtests.append({
                        "method": name,
                        "observations": bt.observations,
                        "exceptions": bt.exceptions,
                        "expected": round(bt.expected, 2),
                        "kupiec_stat": round(bt.kupiec_stat, 4),
                        "kupiec_p": round(bt.kupiec_p, 4),
                        "christoffersen_ind_p": round(bt.christoffersen_ind_p, 4),
                        "christoffersen_cc_p": round(bt.christoffersen_cc_p, 4),
                        "basel_zone": bt.basel_zone,
                        # A p below 0.05 rejects the model at 95%. Spelling out
                        # the verdict keeps the screen from having to encode
                        # the statistics.
                        "coverage_verdict": ("REJECTED" if bt.kupiec_p < 0.05
                                             else "NOT REJECTED"),
                    })

    return {
        "market": market.upper(),
        "history_symbol": spot,
        "history_note": (None if spot == market.upper() else
                         "perp risk computed from %s spot candles; no perp "
                         "candle feed is configured" % spot),
        "confidence": confidence,
        "notional": notional,
        "horizon": "1 bar (%s)" % ("1d" if granularity == 86400 else "%dh" % (granularity // 3600)),
        "series": series.to_dict(),
        "volatility": {
            "sigma_per_bar": round(sigma, 6),
            "ewma_sigma_per_bar": round(ewma_sigma, 6),
            # 365, not 252: crypto does not close at the weekend.
            "annualised_pct": round(CR.annualise_vol(sigma, bars_per_day) * 100, 2),
            "ewma_annualised_pct": round(CR.annualise_vol(ewma_sigma, bars_per_day) * 100, 2),
            "regime": ("ELEVATED" if ewma_sigma > sigma * 1.15 else
                       "CALM" if ewma_sigma < sigma * 0.85 else "TYPICAL"),
        },
        "moments": {"mean_per_bar": round(mu, 8), "skew": round(skew, 4),
                    "excess_kurtosis": round(ex_kurt, 4),
                    "fat_tailed": bool(ex_kurt > 1.0)},
        "methods": methods,
        "backtests": backtests,
        "engine": {"cpp_available": _cpp_available()},
    }


def _cpp_available() -> bool:
    """Whether the pybind11 fast path is compiled into this image."""
    try:
        from ..core.engines import HAVE_CPP
        return bool(HAVE_CPP)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Perpetual board: mark, index, basis, funding, open interest.
#
# Sourced from the venue's **public, unauthenticated read-only** info endpoint,
# which is the same class of access `CoinbasePublicFeed` already uses for the
# desk's spot prices. The mainnet guard is untouched: it protects the order
# path, and is enforced at URL resolution before a key is loaded or anything is
# signed. Nothing here signs, and there is no order path to protect.
#
# Defaulting to mainnet is a correctness decision, not a convenience one.
# Testnet BTC open interest is ~$3.8m against ~$2.8bn on mainnet, and testnet
# ETH funding annualises to -440% off a near-empty book. A screen captioned
# "Open Interest" showing the former is not a conservative version of the
# truth, it is a different market. The network travels with every row so a
# consumer can label it and cannot confuse the two.
# ---------------------------------------------------------------------------

_PERPS_CACHE: Dict[str, Any] = {}
_PERPS_TTL_S = 20.0


@router.get("/perps")
def perps(markets: Optional[str] = Query(
              None, description="comma-separated, e.g. BTC-USD-PERP,ETH-USD-PERP"),
          network: str = Query("mainnet", pattern="^(mainnet|testnet)$"),
          limit: int = Query(12, ge=1, le=100)) -> dict:
    """Perp statistics for the board, newest state, one upstream request."""
    _require()

    from cryptodesk.marketdata.perps import fetch_perp_stats

    wanted = [m.strip() for m in markets.split(",") if m.strip()] if markets else None
    key = "%s:%s" % (network, markets or "*")
    now = time.time()
    with _RISK_LOCK:
        hit = _PERPS_CACHE.get(key)
        if hit and now - hit[0] < _PERPS_TTL_S:
            rows = hit[1]
        else:
            rows = None
    if rows is None:
        try:
            rows = fetch_perp_stats(wanted, network=network)
        except Exception as exc:
            raise HTTPException(502, "perp feed failed (%s): %s: %s"
                                % (network, type(exc).__name__, exc))
        with _RISK_LOCK:
            _PERPS_CACHE[key] = (now, rows)

    shown = rows[:limit]
    total_oi = sum((r.open_interest_usd for r in rows), Decimal(0))
    total_vol = sum((r.day_notional_volume for r in rows), Decimal(0))
    return {
        "network": network,
        "source": "hyperliquid:info (public, read-only)",
        "contracts_total": len(rows),
        "contracts_shown": len(shown),
        "aggregate": {
            "open_interest_usd": str(total_oi.quantize(Decimal("0.01"))),
            "day_notional_volume": str(total_vol.quantize(Decimal("0.01"))),
        },
        "rows": [r.to_dict() for r in shown],
    }


# ---------------------------------------------------------------------------
# Custody: wallet balances and ERC-4626 vault state, read from chain.
#
# Read-only by construction. `cryptodesk.chain` never broadcasts — deposits,
# transfers and approvals return *unsigned* calldata there, and no signing
# path is reachable from this router. A library that could both construct and
# send makes an address-handling bug irreversible, so a Transfers screen here
# can show intent and status but must never originate a transaction.
#
# Block number is returned with every read for the same reason the oracle
# cross-check returns one: a balance without the block it was taken at cannot
# be reproduced, and an unreproducible balance is an assertion, not evidence.
# ---------------------------------------------------------------------------

_ADDRESS_RE = __import__("re").compile(r"^0x[0-9a-fA-F]{40}$")
_CHAINS = ("sepolia", "base-sepolia", "ethereum")


def _check_address(address: str) -> str:
    if not _ADDRESS_RE.match(address or ""):
        raise HTTPException(422, "not a 20-byte hex address: %r" % address)
    return address


@router.get("/wallet")
def wallet(address: str = Query(..., description="0x-prefixed 20-byte address"),
           chain: str = Query("sepolia")) -> dict:
    """Native and ERC-20 balances for one address, at a named block."""
    _require()
    if chain not in _CHAINS:
        raise HTTPException(422, "chain must be one of %s" % ", ".join(_CHAINS))
    _check_address(address)

    from cryptodesk.chain import Wallet
    from cryptodesk.marketdata.defi import JsonRpc

    try:
        snap = Wallet(address, JsonRpc(chain), chain=chain).snapshot()
    except Exception as exc:
        raise HTTPException(502, "chain read failed: %s: %s"
                            % (type(exc).__name__, exc))
    return {
        "address": snap.address, "chain": snap.chain, "block": snap.block,
        "native": str(snap.native), "native_symbol": snap.native_symbol,
        "tokens": [{
            "symbol": t.symbol, "address": t.address,
            # `amount` is the scaled Decimal; `raw` is the on-chain integer.
            # Both travel: the raw value is what the chain actually holds, and
            # it is the only one that round-trips without a decimals lookup.
            "amount": str(t.amount), "raw": str(t.raw), "decimals": t.decimals,
            # Never zero-filled: an unpriced token and a worthless one are
            # different facts.
            "usd_value": None if t.usd_value is None else str(t.usd_value),
        } for t in snap.tokens],
    }


@router.get("/vault")
def vault(address: str = Query(..., description="ERC-4626 vault address"),
          chain: str = Query("sepolia"),
          holder: Optional[str] = Query(None)) -> dict:
    """ERC-4626 vault state: share price, assets, and round-trip cost.

    `round_trip_cost_bps` is deposit-then-immediately-redeem. It is the
    hurdle a yield has to clear before the position is profitable at all,
    and it is invisible on a screen that only reports APY.
    """
    _require()
    if chain not in _CHAINS:
        raise HTTPException(422, "chain must be one of %s" % ", ".join(_CHAINS))
    _check_address(address)
    if holder:
        _check_address(holder)

    from cryptodesk.chain import ERC4626Vault
    from cryptodesk.marketdata.defi import JsonRpc

    try:
        v = ERC4626Vault(address, JsonRpc(chain), chain=chain)
        if not v.is_erc4626():
            raise HTTPException(
                422, "%s does not implement ERC-4626 on %s" % (address, chain))
        return v.report(holder=holder)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "vault read failed: %s: %s"
                            % (type(exc).__name__, exc))
