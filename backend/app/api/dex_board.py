"""DEX board — the venue's public, unauthenticated read surface.

**Why this is a separate router from `cryptodesk.py`.** That module is venue-
agnostic: every endpoint takes `?venue=` and dispatches through the adapter
registry, which is the right shape for anything all venues implement — a book,
a quote, a funding rate. This venue publishes a great deal that the others do not
(a listed option surface with greeks, strategy vaults, an insurance fund), and
bending those into a venue-agnostic signature would mean inventing an
abstraction over one implementation. They live here instead, named for the
venue that serves them, and the screen says so.

**Everything here is public.** No key, no signature, no onboarding. That is
what makes it safe to proxy from a host with `allow_origins=["*"]` — the same
reasoning that lets `/book` and `/markets` exist there and keeps `/submit` out.
The venue's authenticated surface (balances, positions, fills, referrals) is
deliberately not proxied: this console holds no account on that venue, and an
endpoint that would need one is an endpoint that would need a key on the host.

**Why it proxies rather than letting the browser call the venue directly.**
Three reasons, in order of how much they bite: the venue sets no permissive
CORS header for arbitrary origins; a per-viewer fetch of the 3,045-row market
list is rude to a free public API and slow for the viewer; and a proxy is the
only place a cache can live. The cache is what makes a 30-second poll cost one
upstream request rather than one per open tab.

**Two TTLs, because the two payloads age differently.** Instrument definitions
— tick size, strike, expiry, margin fractions — change when the venue lists or
delists something, which is rare. Prices, greeks and funding change every
block. One TTL for both would either serve stale marks or refetch 1.4 MB of
unchanging contract metadata every ten seconds.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/cryptodesk/dex", tags=["crypto-desk-dex"])

# --- defensive import, matching cryptodesk.py -------------------------------
# A missing vendored package degrades these screens; it does not stop the
# console from booting.
try:
    from cryptodesk.marketdata.http import HttpClient, HttpError
    AVAILABLE = True
    IMPORT_ERROR = None
except Exception as exc:                                    # pragma: no cover
    AVAILABLE = False
    IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)

#: Upstream source of every number on these screens.
#:
#: **This stays accurate.** The screens are branded neutrally ("DEX venue") so
#: the console does not read as one venue's front end, but the `venue` field on
#: every response names where the data actually came from. Market data that
#: cannot say which venue priced it is not evidence, and a portfolio piece
#: whose stated discipline is provenance cannot quietly drop the source.
#: Change the display copy freely; these two are the wire, not the branding.
SOURCE_VENUE = "paradex"

API = "https://api.prod.paradex.trade/v1"

#: Contract definitions. Rare changes, large payload — cache hard.
SPECS_TTL_S = 300.0
#: Marks, greeks, funding, open interest. Cache only long enough to collapse
#: concurrent viewers onto one upstream request.
QUOTES_TTL_S = 10.0
#: Vault performance is computed by the venue on a slow cadence; polling it
#: faster than this returns the same numbers.
VAULTS_TTL_S = 60.0

_client: Optional[Any] = None
_client_lock = threading.Lock()


def _http():
    """One client for the process, so its rate limiter actually limits.

    A per-request client would carry a fresh empty token bucket every time,
    which is a rate limiter that never says no.
    """
    global _client
    if not AVAILABLE:
        raise HTTPException(
            503, "dex board unavailable: %s. The vendored cryptodesk "
                 "package failed to import." % IMPORT_ERROR)
    with _client_lock:
        if _client is None:
            _client = HttpClient(timeout=15.0, max_retries=2,
                                 default_rate=4.0, default_burst=8.0)
        return _client


# ------------------------------------------------------------------- cache ---

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, fetch):
    """Fetch through a TTL cache, serving stale data rather than an error.

    The stale fallback is the important half. These screens poll, so a single
    upstream blip would otherwise replace a board of live numbers with an error
    panel for one tick and then restore it — a flicker that reads as the desk
    being broken rather than the venue being briefly slow. Serving the last
    good payload with its age attached lets the screen show what it has and
    say how old it is, which is what a trading screen should do with a stalled
    feed anyway.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1], 0.0

    try:
        fresh = fetch()
    except Exception as exc:
        if hit:
            return hit[1], now - hit[0]
        # Nothing cached and the venue is unreachable: there is no answer to
        # give, so say which call failed rather than returning an empty board
        # that looks like a market with nothing listed.
        raise HTTPException(502, "venue %s unavailable: %s: %s"
                            % (key, type(exc).__name__, exc))
    with _cache_lock:
        _cache[key] = (now, fresh)
    return fresh, 0.0


def _get(path: str, params: Optional[Dict[str, str]] = None) -> Any:
    try:
        return _http().request("GET", API + path, params=params)
    except HttpError as exc:
        raise HTTPException(502, "venue GET %s: %s" % (path, exc))


def _specs() -> Dict[str, dict]:
    """Every listed contract, keyed by symbol."""
    def fetch():
        raw = _get("/markets")
        return {m["symbol"]: m for m in (raw.get("results") or [])
                if m.get("symbol")}
    return _cached("markets", SPECS_TTL_S, fetch)[0]


def _summaries() -> tuple:
    """Marks, greeks and funding for every contract, keyed by symbol."""
    def fetch():
        raw = _get("/markets/summary", {"market": "ALL"})
        return {m["symbol"]: m for m in (raw.get("results") or [])
                if m.get("symbol")}
    return _cached("summary", QUOTES_TTL_S, fetch)


# ------------------------------------------------------------- conversion ---

def _f(x: Any) -> Optional[float]:
    """The venue sends money as strings and absent values as `""`, not `null`.

    `float("")` raises and `float(None)` raises, but the caller wants a number
    or a blank — never a zero. An unquoted option has no bid; rendering that as
    0.00 claims someone is bidding zero for it.
    """
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _market_row(spec: dict, summ: dict) -> dict:
    """One tradable contract, specs joined to its live quote."""
    mark = _f(summ.get("mark_price"))
    oi = _f(summ.get("open_interest"))
    bid, ask = _f(summ.get("bid")), _f(summ.get("ask"))
    # Open interest is quoted in base units. A board that sorts by it has to
    # compare a BTC number against a HYPE number, so it is marked to USD here
    # rather than in the screen, where the mark would have to be re-looked-up.
    oi_usd = oi * mark if (oi is not None and mark is not None) else None
    spread_bps = None
    if bid and ask and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 else None
    return {
        "symbol": spec.get("symbol"),
        "kind": spec.get("asset_kind"),
        "base": spec.get("base_currency"),
        "quote": spec.get("quote_currency"),
        "settlement": spec.get("settlement_currency"),
        "mark": mark,
        "underlying": _f(summ.get("underlying_price")),
        "last": _f(summ.get("last_traded_price")),
        "bid": bid, "ask": ask,
        "bid_size": _f(summ.get("bid_size")), "ask_size": _f(summ.get("ask_size")),
        "spread_bps": spread_bps,
        # A rate, not a percentage: 0.000081 per period. The screen decides how
        # to annualise it, because the period differs per contract.
        "funding_rate": _f(summ.get("funding_rate")),
        "funding_period_hours": _f(spec.get("funding_period_hours")),
        "change_24h_pct": (lambda c: c * 100.0 if c is not None else None)(
            _f(summ.get("price_change_rate_24h"))),
        "volume_24h": _f(summ.get("volume_24h")),
        "total_volume": _f(summ.get("total_volume")),
        "open_interest": oi,
        "open_interest_usd": oi_usd,
        "tick_size": _f(spec.get("price_tick_size")),
        "step_size": _f(spec.get("order_size_increment")),
        "min_notional": _f(spec.get("min_notional")),
        "position_limit": _f(spec.get("position_limit")),
        "market_kind": spec.get("market_kind"),
        "expiry_ms": spec.get("expiry_at") or None,
        "tags": spec.get("tags") or [],
    }


def _option_row(spec: dict, summ: dict) -> dict:
    """One listed option: its contract terms, its mark, and its greeks.

    Greeks come from the venue's own risk model rather than being recomputed
    here. That is the honest choice for a board: these are the numbers the venue
    margins the position against, so a locally-derived delta that disagreed
    would not be a correction, it would be a second opinion mislabelled as the
    venue's.
    """
    g = summ.get("greeks") or {}
    return {
        "symbol": spec.get("symbol"),
        "base": spec.get("base_currency"),
        "option_type": spec.get("option_type"),      # CALL | PUT
        "strike": _f(spec.get("strike_price")),
        "expiry_ms": spec.get("expiry_at"),
        "mark": _f(summ.get("mark_price")),
        "mark_iv": _f(summ.get("mark_iv")),
        "underlying": _f(summ.get("underlying_price")),
        "bid": _f(summ.get("bid")), "ask": _f(summ.get("ask")),
        "bid_size": _f(summ.get("bid_size")), "ask_size": _f(summ.get("ask_size")),
        "last": _f(summ.get("last_traded_price")),
        "volume_24h": _f(summ.get("volume_24h")),
        "open_interest": _f(summ.get("open_interest")),
        "delta": _f(g.get("delta")), "gamma": _f(g.get("gamma")),
        "vega": _f(g.get("vega")), "theta": _f(g.get("theta")),
        "rho": _f(g.get("rho")),
        # Second-order vol greeks. This venue publishes them and almost nothing
        # else does; a vol desk reads vanna and volga to see how the hedge
        # moves when spot and vol move together.
        "vanna": _f(g.get("vanna")), "volga": _f(g.get("volga")),
    }


# -------------------------------------------------------------- endpoints ---

@router.get("/health")
def health() -> dict:
    """Whether this router can reach the venue, and how fresh its cache is."""
    if not AVAILABLE:
        return {"available": False, "error": IMPORT_ERROR}
    try:
        state = _get("/system/state")
    except HTTPException as exc:
        return {"available": True, "reachable": False, "detail": exc.detail}
    with _cache_lock:
        ages = {k: round(time.time() - v[0], 1) for k, v in _cache.items()}
    return {"available": True, "reachable": True,
            "venue_status": state.get("status"), "api": API,
            "cache_age_s": ages, "authenticated": False}


@router.get("/markets")
def markets(kind: str = Query("PERP", pattern="^(PERP|SPOT|OPTION|ALL)$"),
            base: Optional[str] = None,
            sort: str = Query("open_interest_usd"),
            limit: int = Query(60, ge=1, le=400)) -> dict:
    """The tradable universe, specs joined to live quotes.

    Defaults to perps because that is the venue's liquid surface: 63 perps
    against 2,980 listed options, nearly all of which have never traded. A
    board defaulting to everything would open on a screen of empty option
    rows and bury the markets someone is actually quoting.
    """
    specs = _specs()
    summ, stale_s = _summaries()

    rows: List[dict] = []
    for sym, spec in specs.items():
        if kind != "ALL" and spec.get("asset_kind") != kind:
            continue
        if base and (spec.get("base_currency") or "").upper() != base.upper():
            continue
        rows.append(_market_row(spec, summ.get(sym, {})))

    # None sorts last on every key: a contract with no open interest and no
    # volume belongs at the bottom of the board, not the top by accident.
    rows.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0.0)))

    counts: Dict[str, int] = {}
    for s in specs.values():
        k = s.get("asset_kind") or "UNKNOWN"
        counts[k] = counts.get(k, 0) + 1

    shown = rows[:limit]
    return {
        "venue": SOURCE_VENUE, "network": "mainnet", "kind": kind,
        "listed": counts, "matched": len(rows), "stale_s": round(stale_s, 1),
        "aggregate": {
            "open_interest_usd": sum(r["open_interest_usd"] or 0.0 for r in rows),
            "volume_24h": sum(r["volume_24h"] or 0.0 for r in rows),
        },
        "rows": shown,
    }


@router.get("/options/expiries")
def option_expiries(base: str = Query("BTC")) -> dict:
    """Listed expiries for one underlying, with how much is listed on each."""
    specs = _specs()
    summ, _ = _summaries()

    buckets: Dict[int, dict] = {}
    for sym, spec in specs.items():
        if spec.get("asset_kind") != "OPTION":
            continue
        if (spec.get("base_currency") or "").upper() != base.upper():
            continue
        exp = spec.get("expiry_at")
        if not exp:
            continue
        b = buckets.setdefault(exp, {
            "expiry_ms": exp, "contracts": 0, "calls": 0, "puts": 0,
            "open_interest": 0.0, "volume_24h": 0.0, "strikes": set(),
        })
        b["contracts"] += 1
        b["calls" if spec.get("option_type") == "CALL" else "puts"] += 1
        strike = _f(spec.get("strike_price"))
        if strike is not None:
            b["strikes"].add(strike)
        s = summ.get(sym, {})
        b["open_interest"] += _f(s.get("open_interest")) or 0.0
        b["volume_24h"] += _f(s.get("volume_24h")) or 0.0

    now_ms = time.time() * 1000.0
    out = []
    for b in sorted(buckets.values(), key=lambda x: x["expiry_ms"]):
        strikes = b.pop("strikes")
        b["strike_count"] = len(strikes)
        b["strike_low"] = min(strikes) if strikes else None
        b["strike_high"] = max(strikes) if strikes else None
        # Days to expiry is what a vol trader reads; the epoch is what the API
        # speaks. Both are returned so the screen need not do date maths.
        b["days_to_expiry"] = round((b["expiry_ms"] - now_ms) / 86_400_000.0, 2)
        out.append(b)
    return {"venue": SOURCE_VENUE, "base": base.upper(), "expiries": out}


@router.get("/options/chain")
def option_chain(base: str = Query("BTC"),
                 expiry_ms: Optional[int] = None,
                 limit: int = Query(80, ge=1, le=400)) -> dict:
    """One expiry's option chain, calls and puts paired by strike.

    Pairing is what makes it a chain rather than a list. A vol desk reads
    across a strike — call mark against put mark, call IV against put IV —
    because that comparison is where put-call parity violations and skew show
    up. Two separate tables sorted by strike make the reader do the join.
    """
    specs = _specs()
    summ, stale_s = _summaries()

    listed = [s for s in specs.values()
              if s.get("asset_kind") == "OPTION"
              and (s.get("base_currency") or "").upper() == base.upper()]
    if not listed:
        raise HTTPException(404, "no listed options for %r on this venue" % base)

    if expiry_ms is None:
        # Default to the nearest expiry with at least a day left, not simply
        # the nearest. A chain inside its last hours has no optionality left to
        # show: every strike has collapsed to delta 1 or delta 0 and vega is
        # ~1e-26, so the board opens on a settlement exercise rather than a vol
        # surface. Falling back to the nearest keeps the screen working on a
        # day when everything listed is expiring.
        now_ms = time.time() * 1000.0
        future = sorted({s["expiry_at"] for s in listed if s.get("expiry_at")
                         and s["expiry_at"] > now_ms})
        if not future:
            raise HTTPException(404, "every listed %s expiry has passed" % base)
        with_time = [e for e in future if e - now_ms > 86_400_000.0]
        expiry_ms = with_time[0] if with_time else future[0]

    by_strike: Dict[float, dict] = {}
    for spec in listed:
        if spec.get("expiry_at") != expiry_ms:
            continue
        strike = _f(spec.get("strike_price"))
        if strike is None:
            continue
        row = _option_row(spec, summ.get(spec["symbol"], {}))
        leg = "call" if spec.get("option_type") == "CALL" else "put"
        by_strike.setdefault(strike, {"strike": strike, "call": None, "put": None})[leg] = row

    if not by_strike:
        raise HTTPException(404, "no %s options listed for expiry %s"
                            % (base, expiry_ms))

    chain = [by_strike[k] for k in sorted(by_strike)]

    # The forward is what the chain is struck around, so it anchors the screen:
    # it decides which rows are in the money and where to scroll on open.
    underlying = None
    for r in chain:
        for leg in ("call", "put"):
            if r[leg] and r[leg].get("underlying") is not None:
                underlying = r[leg]["underlying"]
                break
        if underlying is not None:
            break

    atm = None
    if underlying is not None:
        atm = min(chain, key=lambda r: abs(r["strike"] - underlying))["strike"]

    return {
        "venue": SOURCE_VENUE, "base": base.upper(), "expiry_ms": expiry_ms,
        "days_to_expiry": round((expiry_ms - time.time() * 1000.0) / 86_400_000.0, 2),
        "underlying": underlying, "atm_strike": atm,
        "strike_count": len(chain), "stale_s": round(stale_s, 1),
        "chain": chain[:limit],
    }


@router.get("/vaults")
def vaults(limit: int = Query(25, ge=1, le=100),
           sort: str = Query("tvl")) -> dict:
    """Strategy vaults, definitions joined to their performance.

    Two upstream calls because the venue splits them: `/vaults` is what a vault
    *is* (owner, strategy, fee split) and `/vaults/summary` is how it has
    *done* (TVL, ROI, P&L). Neither is useful alone — a return with no strategy
    behind it is a number, and a strategy with no return is a pitch.
    """
    def fetch():
        defs = _get("/vaults").get("results") or []
        perf = {v.get("address"): v
                for v in (_get("/vaults/summary").get("results") or [])}
        return defs, perf

    (defs, perf), stale_s = _cached("vaults", VAULTS_TTL_S, fetch)

    rows = []
    for v in defs:
        p = perf.get(v.get("address"), {})
        rows.append({
            "address": v.get("address"),
            "name": v.get("name"), "kind": v.get("kind"),
            "description": v.get("description") or None,
            "strategy": v.get("strategy_description") or None,
            "curator": v.get("curator_name") or None,
            "owner": v.get("owner_account"),
            "status": v.get("status"),
            "profit_share_pct": _f(v.get("profit_share")),
            "lockup_days": _f(v.get("lockup_period_days")),
            "created_ms": v.get("created_at"),
            "tvl": _f(p.get("tvl")),
            "net_deposits": _f(p.get("net_deposits")),
            "owner_equity": _f(p.get("owner_equity")),
            "vtoken_price": _f(p.get("vtoken_price")),
            "vtoken_supply": _f(p.get("vtoken_supply")),
            # ROI arrives as a rate; percent is what the column header says.
            "roi_total_pct": (lambda x: x * 100.0 if x is not None else None)(
                _f(p.get("total_roi"))),
            "roi_24h_pct": (lambda x: x * 100.0 if x is not None else None)(
                _f(p.get("roi_24h"))),
            "pnl_total": _f(p.get("total_pnl")),
            "pnl_24h": _f(p.get("pnl_24h")),
            "max_drawdown_pct": (lambda x: x * 100.0 if x is not None else None)(
                _f(p.get("max_drawdown"))),
            "volume_24h": _f(p.get("volume_24h")),
        })

    rows.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0.0)))
    active = [r for r in rows if (r.get("tvl") or 0) > 0]
    return {
        "venue": SOURCE_VENUE, "total": len(rows), "funded": len(active),
        "stale_s": round(stale_s, 1),
        "aggregate_tvl": sum(r["tvl"] or 0.0 for r in rows),
        "rows": rows[:limit],
    }


@router.get("/funding")
def funding(market: str = Query("BTC-USD-PERP"),
            limit: int = Query(60, ge=1, le=200)) -> dict:
    """Realised funding history for one perp, newest first.

    A single funding print says what the market is paying now; the series says
    whether that is a regime or a spike, which is the only version of the
    question a carry decision actually turns on.
    """
    raw = _get("/funding/data",
               {"market": market.upper(), "page_size": str(limit)})
    points = []
    for p in (raw.get("results") or []):
        rate = _f(p.get("funding_rate"))
        points.append({
            "market": p.get("market"),
            "created_ms": p.get("created_at"),
            "funding_rate": rate,
            "funding_premium": _f(p.get("funding_premium")),
            "funding_index": _f(p.get("funding_index")),
        })

    rates = [p["funding_rate"] for p in points if p["funding_rate"] is not None]
    spec = _specs().get(market.upper(), {})
    period_h = _f(spec.get("funding_period_hours")) or 8.0
    periods_per_year = (365.0 * 24.0) / period_h if period_h else None

    mean = sum(rates) / len(rates) if rates else None
    return {
        "venue": SOURCE_VENUE, "market": market.upper(),
        "period_hours": period_h, "observations": len(points),
        "mean_rate": mean,
        # Annualising the *mean* of the window, not the latest print: one
        # elevated period extrapolated over a year is a headline, not a carry
        # estimate.
        "mean_apr_pct": (mean * periods_per_year * 100.0
                         if (mean is not None and periods_per_year) else None),
        "max_rate": max(rates) if rates else None,
        "min_rate": min(rates) if rates else None,
        "positive_share_pct": (100.0 * sum(1 for r in rates if r > 0) / len(rates)
                               if rates else None),
        "points": points,
    }


@router.get("/trades")
def trades(market: str = Query("BTC-USD-PERP"),
           limit: int = Query(50, ge=1, le=200)) -> dict:
    """The public tape for one market, newest first.

    Prints, not quotes. The book says what is on offer; the tape says what
    someone actually paid, which is the only one of the two that is evidence
    a market trades at all.
    """
    raw = _get("/trades", {"market": market.upper(), "page_size": str(limit)})
    rows = []
    for t in (raw.get("results") or []):
        rows.append({
            "id": t.get("id"),
            "market": t.get("market"),
            "side": t.get("side"),
            "size": _f(t.get("size")),
            "price": _f(t.get("price")),
            "created_ms": t.get("created_at"),
            "trade_type": t.get("trade_type"),
        })
    notional = sum((r["size"] or 0.0) * (r["price"] or 0.0) for r in rows)
    buys = sum(1 for r in rows if (r["side"] or "").upper().startswith("B"))
    return {
        "venue": SOURCE_VENUE, "market": market.upper(),
        "count": len(rows), "window_notional": notional,
        "buy_share_pct": (100.0 * buys / len(rows)) if rows else None,
        "rows": rows,
    }


@router.get("/insurance")
def insurance() -> dict:
    """The venue's insurance fund — the backstop behind every open position.

    Worth a tile on a risk desk: it is the balance that absorbs a liquidation
    that closes through bankruptcy, and the number between a bad fill and
    socialised losses for everyone else on the book.
    """
    raw, stale_s = _cached("insurance", VAULTS_TTL_S, lambda: _get("/insurance"))
    return {
        "venue": SOURCE_VENUE,
        "account": raw.get("account"),
        "account_value": _f(raw.get("account_value")),
        "settlement_asset": raw.get("settlement_asset"),
        "updated_ms": raw.get("updated_at"),
        "stale_s": round(stale_s, 1),
    }
