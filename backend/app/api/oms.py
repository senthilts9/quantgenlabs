"""OMS API — order entry, fills, blotter, positions.

Execution here is SIMULATED against a reference price: this endpoint never
routes to a real venue and holds no broker credentials. A live/paper adapter
(Alpaca paper, exchange testnet) plugs in at `simulate_fill` without changing
the order model or the accounting.
"""
from __future__ import annotations

import random
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..core import marketdata as MD
from ..core.marketdata import fred_latest
from ..core.oms import (OMS, OMSStore, OrderType, RiskLimits, Side,
                        evaluate, utilisation)

router = APIRouter(prefix="/api/oms", tags=["oms"])

_store: OMSStore | None = None
_oms: OMS | None = None


def oms() -> OMS:
    global _store, _oms
    if _oms is None:
        _store = OMSStore()
        _oms = OMS(_store)
    return _oms


def _dump(obj) -> dict:
    d = asdict(obj)
    return {k: (v.value if hasattr(v, "value") else v) for k, v in d.items()}


def _order_out(o) -> dict:
    d = _dump(o)
    d["leaves_qty"] = round(o.leaves_qty, 8)
    d["shortfall_bps"] = (round(o.shortfall_bps(), 3)
                          if o.shortfall_bps() is not None else None)
    return d


class PortfolioReq(BaseModel):
    id: str
    name: str
    base_ccy: str = "USD"
    mandate: str | None = None


@router.get("/portfolios")
def list_portfolios() -> dict:
    engine = oms()
    marks = _marks()
    books = engine.store.list_portfolios()
    b = engine.blotter(marks)
    by_pf = {x["portfolio_id"]: x for x in b.get("by_portfolio", [])}
    for bk in books:
        bk.update(by_pf.get(bk["id"], {
            "positions": 0, "gross_notional": 0.0, "unrealized_pnl": 0.0,
            "realized_pnl": 0.0, "fees": 0.0, "total_pnl": 0.0}))
    return {"portfolios": books, "firm_totals": b["totals"]}


@router.put("/portfolios")
def upsert_portfolio(req: PortfolioReq) -> dict:
    oms().store.upsert_portfolio(req.id, req.name, req.base_ccy, req.mandate)
    return list_portfolios()


@router.delete("/portfolios/{pid}")
def delete_portfolio(pid: str) -> dict:
    oms().store.delete_portfolio(pid)
    return list_portfolios()


class PlaceReq(BaseModel):
    symbol: str
    side: str = "buy"
    qty: float = Field(gt=0)
    order_type: str = "market"
    limit_price: float | None = None
    algo: str | None = "MANUAL"
    portfolio_id: str = "DEFAULT"
    reference_price: float | None = None   # arrival mid; also the sim fill anchor
    auto_fill: bool = True                 # simulate execution immediately


@router.post("/orders")
def place_order(req: PlaceReq) -> dict:
    engine = oms()
    try:
        side = Side(req.side.lower())
        otype = OrderType(req.order_type.lower())
    except ValueError as e:
        raise HTTPException(400, str(e))

    ref = req.reference_price or _reference_price(req.symbol)
    if ref is None:
        inst = MD.resolve(req.symbol)
        if inst is not None and not inst.tradable:
            # A yield is not a price. Rejecting this explicitly beats letting
            # someone book "100 units of the 10Y at 4.77" and then reasoning
            # about the resulting P&L as if it meant something.
            raise HTTPException(
                400, f"{inst.symbol} is quoted as {inst.quote_type} ({inst.unit}), "
                     f"not a price — it is a risk factor, not a tradable line. "
                     f"Rates enter the book through DV01 on a bond; see /api/rates.")
        raise HTTPException(
            400, f"no reference price for {req.symbol}; pass reference_price "
                 f"explicitly, or use a symbol from /api/marketdata/universe")

    # PRE-trade gate: evaluated before the order is accepted, against
    # projected post-trade state. A rejected order is still persisted (with
    # its reason) so the control leaves an audit trail.
    limits = current_limits(engine)
    marks = _marks()
    decision = evaluate(
        limits, symbol=req.symbol, side=side, qty=req.qty, reference_price=ref,
        positions=engine.store.list_positions(), marks=marks,
        realized_pnl_today=engine.store.realized_pnl_today(),
        limit_price=req.limit_price)

    try:
        order = engine.place(req.symbol, side, req.qty, otype, req.limit_price,
                             algo=req.algo, arrival_mid=ref,
                             portfolio_id=req.portfolio_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not decision.approved:
        engine.reject(order, decision.reason or "risk gate")
        for c in decision.checks:
            if not c.passed:
                engine.store.record_breach(
                    symbol=req.symbol.upper(), side=side.value, qty=req.qty,
                    gate=c.gate, detail=c.detail, observed=c.observed,
                    limit_val=c.limit, order_id=order.id)
        return {"order": _order_out(engine.store.get_order(order.id)),
                "fills": [], "risk": decision.to_dict()}

    if req.auto_fill:
        _simulate_execution(engine, order, ref)
        order = engine.store.get_order(order.id)

    return {"order": _order_out(order),
            "fills": [_dump(f) for f in engine.store.list_fills(order_id=order.id)],
            "risk": decision.to_dict()}


def current_limits(engine: OMS) -> RiskLimits:
    stored = engine.store.load_limits()
    return RiskLimits.from_dict(stored) if stored else RiskLimits()


def _marks(extra: list[str] | None = None) -> dict[str, float]:
    """Marks for the symbols actually on the book, keyed as the book stores them.

    Only held symbols are fetched rather than the whole instrument master:
    the universe is ~40 instruments and a blotter refresh has no business
    pulling marks for positions nobody holds.

    Keys are the STORED symbol, not the canonical one. Positions opened before
    the instrument master existed are recorded as "SP500" while the master
    calls it "SPX"; marking by canonical name only would leave those holdings
    valued at average cost with no unrealised P&L and no error anywhere.
    """
    engine = oms()
    held = {p.symbol for p in engine.store.list_positions()}
    held |= {s.upper() for s in (extra or [])}

    fred_ids: dict[str, list[str]] = {}
    crypto: list[tuple[str, object]] = []
    options: list[str] = []
    for sym in held:
        inst = MD.resolve(sym)
        if inst is None or not inst.tradable:
            continue                      # RATE/SPREAD have no price to mark
        if inst.asset_class == "OPTION":
            options.append(inst.symbol)
        elif inst.source == "fred":
            fred_ids.setdefault(inst.series_id, []).append(sym)
        else:
            crypto.append((sym, inst))

    out: dict[str, float] = {}
    dates: list[str] = []
    if fred_ids:
        for r in fred_latest(list(fred_ids)):
            for sym in fred_ids.get(r["series_id"], []):
                out[sym] = r["value"]
            if r.get("date"):
                dates.append(r["date"])
    # ONE cold-fetch budget for this whole call, shared by the crypto marks and
    # by the options priced off crypto underlyings. Marking a book must not be
    # able to wait a separate timeout per position: with a per-call budget, a
    # book holding four crypto lines plus options on them measured 8.3s against
    # a 3s bound on a cold process, because each leg started its own clock.
    from ..services import crypto_quotes
    until = crypto_quotes.deadline()
    for sym, inst in crypto:
        px = crypto_quotes.mid(inst.series_id, until=until)
        if px is not None:
            out[sym] = px
    # Options are marked at model value off the underlying. Priced last so the
    # underlying's own mark is already cached and is not re-fetched per strike.
    for sym in options:
        px = _option_price(sym, until=until)
        if px is not None:
            out[sym] = px
    # Oldest EOD mark date across the book. Reported rather than a single
    # "as-of", because marks are a MIXTURE: crypto is live and the FRED block
    # is end-of-day with a per-series lag. The oldest date is the honest
    # summary -- it is the point beyond which some position is not current.
    _MARK_DATES.clear()
    _MARK_DATES.extend(sorted(dates))
    return out


# Populated by _marks(); read by blotter() so the UI can date-stamp the book.
_MARK_DATES: list[str] = []


def _crypto_mid(pair: str, until: float | None = None) -> float | None:
    """Live consolidated mid, or None. Never the synthetic fallback.

    `live_or_synth` returns an invented book when venues are unreachable. That
    is fine for demonstrating routing mechanics, but marking a real position
    against a fabricated price is not -- it would flow straight into P&L,
    VaR and limit utilisation as though it were observed. A quote the
    refresher has stopped updating is refused for the same reason.

    This used to do the multi-venue fan-out inline behind a 20s TTL. The
    blotter, the limits screen and the portfolio list each call _marks() on
    every request, and 20s is shorter than anyone spends on a screen, so the
    cache rarely hit and a 60ms blotter measured 3.7s. The fetch now happens
    on a background refresher (services.crypto_quotes, shared with the board
    so the same venues are not polled twice) and this is a cache read. The
    Smart Order Routing screen still fetches uncached books when it routes.
    """
    from ..services import crypto_quotes
    return crypto_quotes.mid(pair, until=until)


def underlying_vol(symbol: str, default: float = 0.20) -> float:
    """Annualised REALISED vol of an underlying, as an implied-vol proxy.

    Stated plainly because it matters: there is no options market data feed
    here, so realised vol from the price history stands in for implied. Those
    differ — implied normally trades above realised, and the gap is the
    variance risk premium — so option marks are model prices, not quotes.
    Assuming a flat 20% for everything would be worse: it would price a 120%-vol
    crypto option and a 12%-vol index option identically.
    """
    try:
        from ..core.marketdata.board import _fred_row
        inst = MD.resolve(symbol)
        if inst is None or inst.source != "fred":
            return default
        row = _fred_row(inst, 260)
        v = row.get("vol_annual_pct")
        return float(v) / 100.0 if v else default
    except Exception:
        return default


def _option_price(symbol: str, until: float | None = None) -> float | None:
    """Model price for an option ticker, off the underlying mark and its vol."""
    spec = MD.universe.parse_option(symbol)
    if spec is None or spec["expired"]:
        return None
    spot = _reference_price(spec["underlying"], until=until)
    if not spot:
        return None
    from ..core.engines import price_black_scholes
    from ..core.pricing import Exercise, Option, OptionType
    opt = Option(spot=spot, strike=spec["strike"],
                 maturity=max(spec["years_to_expiry"], 1e-6),
                 rate=0.045, dividend=0.0,
                 vol=max(underlying_vol(spec["underlying"]), 1e-4),
                 kind=OptionType.CALL if spec["is_call"] else OptionType.PUT,
                 exercise=Exercise.EUROPEAN)
    return float(price_black_scholes(opt)["price"])


def _reference_price(symbol: str, until: float | None = None) -> float | None:
    """Mark for a tradable instrument; None if unknown or not price-quoted.

    Rather than invent a price for an arbitrary ticker, unmapped symbols
    require the caller to supply one, so no position is ever marked against a
    number that came from nowhere.
    """
    inst = MD.resolve(symbol)
    if inst is None or not inst.tradable:
        return None
    if inst.asset_class == "OPTION":
        return _option_price(inst.symbol)
    if inst.source == "fred":
        rows = fred_latest([inst.series_id])
        return rows[0]["value"] if rows else None
    return _crypto_mid(inst.series_id, until=until)


def _simulate_execution(engine: OMS, order, ref: float) -> None:
    """Slice into 1-3 child fills around the reference price.

    Spread/impact are modelled, not observed: each fill crosses a half-spread
    in the direction of the trade plus a small random component, so the
    resulting shortfall is realistic in shape but is NOT a claim about what a
    real venue would have done.
    """
    remaining = order.qty
    n = random.randint(1, 3)
    half_spread_bps = 1.5
    for i in range(n):
        qty = remaining if i == n - 1 else round(remaining / (n - i), 8)
        if qty <= 0:
            break
        drift_bps = half_spread_bps + random.uniform(0.0, 2.0)
        px = ref * (1 + drift_bps / 1e4 * order.side.sign)
        fee = abs(qty) * px * 0.0002          # 2 bps commission
        engine.fill(order.id, qty, round(px, 6), venue="SIM", fee=round(fee, 6))
        remaining -= qty


class FillReq(BaseModel):
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    venue: str | None = None
    fee: float = 0.0


@router.post("/orders/{order_id}/fill")
def fill_order(order_id: str, req: FillReq) -> dict:
    engine = oms()
    try:
        order, fill, pos = engine.fill(order_id, req.qty, req.price,
                                       req.venue, req.fee)
    except KeyError:
        raise HTTPException(404, f"order {order_id} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"order": _order_out(order), "fill": _dump(fill), "position": _dump(pos)}


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str) -> dict:
    try:
        return {"order": _order_out(oms().cancel(order_id))}
    except KeyError:
        raise HTTPException(404, f"order {order_id} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


def _in_desk(symbol: str, desk: str) -> bool:
    """True if `symbol` belongs on `desk`. One predicate for every route here,
    so orders, fills and the blotter cannot drift out of agreement."""
    if desk == "all":
        return True
    from ..core.marketdata.universe import is_crypto
    return is_crypto(symbol) == (desk == "digital")


@router.get("/orders")
def list_orders(limit: int = Query(100, le=500), symbol: str | None = None,
                status: str | None = None,
                desk: str = Query("traditional",
                                  pattern="^(traditional|digital|all)$")) -> list[dict]:
    rows = oms().store.list_orders(limit, symbol, status)
    return [_order_out(o) for o in rows if _in_desk(o.symbol, desk)]


@router.get("/fills")
def list_fills(limit: int = Query(100, le=500),
               desk: str = Query("traditional",
                                 pattern="^(traditional|digital|all)$")) -> list[dict]:
    rows = oms().store.list_fills(limit)
    return [_dump(f) for f in rows if _in_desk(f.symbol, desk)]


@router.get("/blotter")
def blotter(portfolio_id: str | None = None,
            desk: str = Query("traditional",
                              pattern="^(traditional|digital|all)$")) -> dict:
    """Positions, P&L and recent activity — the Holdings & Blotter screen.

    portfolio_id omitted = firm-wide view across every book.

    `desk` scopes the rows to one asset class. Digital assets are excluded by
    default: they settle on different venues under different custody, and a
    firm-wide P&L that silently blends a 24/7 crypto mark with an end-of-day
    Treasury mark is a number nobody can act on. `digital` returns only the
    crypto book; `all` returns everything, for reconciliation.
    """
    from ..core.marketdata.universe import is_crypto

    def keep(symbol: str) -> bool:
        if desk == "all":
            return True
        return is_crypto(symbol) if desk == "digital" else not is_crypto(symbol)

    engine = oms()
    marks = _marks()
    b = engine.blotter(marks, portfolio_id=portfolio_id)
    b["orders"] = [_order_out(o)
                   for o in engine.store.list_orders(50, portfolio_id=portfolio_id)]
    b["fills"] = [_dump(f) for f in engine.store.list_fills(50)]
    b["marks"] = marks

    # Filter every symbol-keyed collection, not just the obvious one: a
    # position list without its marks renders as blanks, and an order list
    # that still shows crypto makes the boundary look broken.
    def sym(row):
        return str(row.get("symbol") or row.get("instrument") or "")
    for key in ("positions", "orders", "fills"):
        if isinstance(b.get(key), list):
            b[key] = [r for r in b[key] if keep(sym(r))]
    if isinstance(b.get("marks"), dict):
        b["marks"] = {k: v for k, v in b["marks"].items() if keep(k)}
    b["desk"] = desk
    b["marks_as_of"] = (f"{_MARK_DATES[0]}"
                        + (f" … {_MARK_DATES[-1]}" if _MARK_DATES[0] != _MARK_DATES[-1] else "")
                        ) if _MARK_DATES else None
    b["marks_note"] = ("crypto marked live; other instruments end-of-day"
                       if any(MD.resolve(s) and MD.resolve(s).source == "ccxt"
                              for s in marks) else "end-of-day marks")
    b["portfolios"] = engine.store.list_portfolios()
    return b


# ---- risk limits --------------------------------------------------------
class LimitsReq(BaseModel):
    kill_switch: bool | None = None
    restricted_symbols: list[str] | None = None
    max_order_notional: float | None = None
    max_position_notional: float | None = None
    max_gross_exposure: float | None = None
    daily_loss_limit: float | None = None
    price_collar_pct: float | None = None


@router.get("/limits")
def get_limits() -> dict:
    """Limits, live utilisation, and the breach log — the Limits & Alerts screen."""
    engine = oms()
    limits = current_limits(engine)
    marks = _marks()
    positions = engine.store.list_positions()
    return {
        "limits": limits.to_dict(),
        "utilisation": utilisation(limits, positions, marks,
                                   engine.store.realized_pnl_today()),
        "breaches": engine.store.list_breaches(50),
        "realized_pnl_today": round(engine.store.realized_pnl_today(), 2),
    }


@router.put("/limits")
def update_limits(req: LimitsReq) -> dict:
    engine = oms()
    merged = current_limits(engine).to_dict()
    merged.update({k: v for k, v in req.model_dump().items() if v is not None})
    engine.store.save_limits(merged)
    return get_limits()


@router.post("/limits/check")
def check_order(req: PlaceReq) -> dict:
    """Dry-run the gate without placing anything — lets the UI warn before submit."""
    engine = oms()
    try:
        side = Side(req.side.lower())
    except ValueError as e:
        raise HTTPException(400, str(e))
    ref = req.reference_price or _reference_price(req.symbol)
    if ref is None:
        raise HTTPException(400, f"no reference price for {req.symbol}")
    return evaluate(current_limits(engine), symbol=req.symbol, side=side,
                    qty=req.qty, reference_price=ref,
                    positions=engine.store.list_positions(), marks=_marks(),
                    realized_pnl_today=engine.store.realized_pnl_today(),
                    limit_price=req.limit_price).to_dict()


@router.post("/reset")
def reset() -> dict:
    oms().store.reset()
    return {"status": "reset"}
