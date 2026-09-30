"""Reference data API: security master, calendars, corporate actions, recon, batch.

READ ENDPOINTS ARE READ-ONLY AND SHARED. The security master and corporate-action
book are process-wide singletons loaded from fixtures -- they are reference data,
the same for every caller, and rebuilding them per request would be the only thing
here slow enough to notice.

THE DAILY CYCLE IS PER-SESSION AND MUTABLE, because running it records that it
ran. That record is what makes a second run refuse rather than double-apply, so it
cannot be shared across callers who are each experimenting: one caller's SOD would
block another's. Sessions are capped and the cap is enforced, so the endpoint
cannot be made to allocate without bound.

AUTHENTICATION MATCHES THE REST OF THE READ SURFACE, WHICH IS TO SAY THERE IS
NONE. The platform's convention is that only order-submitting POSTs carry a
`require(Permission...)` guard -- /api/desk/order, /api/algo/start and the
cancels. No GET anywhere is guarded, and neither is /api/connectivity/status or
/api/events/topics. An earlier draft of this module guarded all seventeen routes;
with QGL_REQUIRE_AUTH=true (which the production configmap sets) that made
Reference Data the ONE screen returning 401 while every other screen worked,
because the frontend sends no Authorization header anywhere.

What is served here is a fixture security master, computed calendars and public
corporate-action records -- no prices, no positions, no money. The batch cycle
mutates only in-process session state that is explicitly labelled simulated, and
a guard on those endpoints alone would break the panel while protecting nothing.
Guarding them is the right call once the frontend carries a token; that is a
platform-wide change, not a change to this module.

EVERY DATE IS A REQUIRED PARAMETER. There is no as-of default of today anywhere in
this module. A ticker resolved against whatever today happens to be is the wrong
answer delivered confidently, and the point of the layer underneath is that it
refuses to do that.
"""
from __future__ import annotations

import threading
import uuid
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.refdata import (AmbiguousSymbol, DailyCycle, DailyCycleError,
                            Holding, IdType, Phase, Position, SymbologyError,
                            crypto_calendar, reconcile_cash,
                            reconcile_positions, reconcile_trades,
                            us_equity_calendar, valid_cusip, valid_isin)
from ..core.refdata.fixtures import demo_corporate_actions, demo_security_master

router = APIRouter(prefix="/api/refdata", tags=["reference data"])

SECURITY_MASTER = demo_security_master()
CA_BOOK = demo_corporate_actions()
CALENDARS = {"XNYS": us_equity_calendar(), "CRYPTO": crypto_calendar()}
# XNAS observes the same holidays as XNYS for equities; aliased rather than
# duplicated so the two cannot drift apart.
CALENDARS["XNAS"] = CALENDARS["XNYS"]

MAX_CYCLES = 16
_cycles: dict[str, DailyCycle] = {}
_cycles_lock = threading.RLock()


def _calendar(mic: str):
    cal = CALENDARS.get(mic.upper())
    if cal is None:
        raise HTTPException(404, f"No calendar for '{mic}'. Available: "
                                 f"{sorted(set(CALENDARS))}.")
    return cal


def _cycle(session_id: str | None) -> tuple[str, DailyCycle]:
    with _cycles_lock:
        if session_id and session_id in _cycles:
            return session_id, _cycles[session_id]
        if len(_cycles) >= MAX_CYCLES:
            raise HTTPException(429, f"{MAX_CYCLES} batch sessions already open.")
        sid = session_id or uuid.uuid4().hex[:12]
        _cycles[sid] = DailyCycle(CALENDARS["XNYS"], CA_BOOK)
        return sid, _cycles[sid]


# ---------------------------------------------------------------- symbology
@router.get("/instruments")
def list_instruments(asset_class: str | None = None):
    out = []
    for i in SECURITY_MASTER.all_instruments():
        if asset_class and i.asset_class.value != asset_class:
            continue
        out.append({
            "instrument_id": i.instrument_id, "name": i.name,
            "asset_class": i.asset_class.value, "currency": i.currency,
            "exchange": i.exchange, "tick_size": i.tick_size,
            "listed_from": i.listed_from.isoformat(),
            "listed_to": None if i.listed_to.year == 9999 else i.listed_to.isoformat(),
            "aliases": [{"type": a.id_type.value, "value": a.value,
                         "valid_from": a.valid_from.isoformat(),
                         "valid_to": None if a.valid_to.year == 9999 else a.valid_to.isoformat()}
                        for a in i.aliases],
        })
    return {"count": len(out), "instruments": out,
            "note": "Fixture security master. Identifiers are public and pass "
                    "their own check digits; nothing here is a price."}


@router.get("/resolve")
def resolve(symbol: str = Query(..., min_length=1),
            on: date = Query(..., description="As-of date. Required, not defaulted: "
                                              "a ticker is only unique within a window."),
            id_type: str | None = None):
    try:
        it = IdType(id_type) if id_type else None
    except ValueError:
        raise HTTPException(422, f"Unknown id_type '{id_type}'. "
                                 f"Valid: {[t.value for t in IdType]}.")
    try:
        inst = SECURITY_MASTER.resolve(symbol, on, it)
    except AmbiguousSymbol as e:
        raise HTTPException(409, str(e))
    except SymbologyError as e:
        raise HTTPException(404, str(e))
    return {
        "as_of": on.isoformat(),
        "instrument_id": inst.instrument_id, "name": inst.name,
        "asset_class": inst.asset_class.value, "exchange": inst.exchange,
        "currency": inst.currency, "tick_size": inst.tick_size,
        "tradable_on": inst.tradable_on(on),
        "identifiers": SECURITY_MASTER.cross_reference(symbol, on, it),
    }


@router.get("/lineage/{instrument_id}")
def lineage(instrument_id: str, id_type: str = "ticker"):
    try:
        it = IdType(id_type)
        hist = SECURITY_MASTER.history(instrument_id, it)
    except ValueError:
        raise HTTPException(422, f"Unknown id_type '{id_type}'.")
    except SymbologyError as e:
        raise HTTPException(404, str(e))
    return {
        "instrument_id": instrument_id, "id_type": it.value,
        "history": [{"value": a.value, "valid_from": a.valid_from.isoformat(),
                     "valid_to": None if a.valid_to.year == 9999 else a.valid_to.isoformat()}
                    for a in hist],
        "note": "Windows are half-open [from, to): an identifier that changes on a "
                "date does not resolve to both values on that date.",
    }


@router.get("/validate")
def validate(isin: str | None = None, cusip: str | None = None):
    if not isin and not cusip:
        raise HTTPException(422, "Pass isin or cusip.")
    out: dict = {}
    if isin:
        out["isin"] = {"value": isin, "valid": valid_isin(isin)}
    if cusip:
        out["cusip"] = {"value": cusip, "valid": valid_cusip(cusip)}
    out["note"] = ("Check digit only. A valid check digit means the string is not "
                   "mistyped; it does not mean the security exists.")
    return out


# ----------------------------------------------------------------- calendar
@router.get("/calendar/{mic}")
def calendar_info(mic: str, on: date = Query(...)):
    cal = _calendar(mic)
    trading = cal.is_trading_day(on)
    close = cal.close_time_on(on)
    return {
        "mic": cal.mic, "timezone": cal.timezone, "date": on.isoformat(),
        "is_trading_day": trading,
        "is_half_day": cal.is_half_day(on),
        "open_time": cal.session.open_time.isoformat() if trading else None,
        "close_time": close.isoformat() if close else None,
        "previous_trading_day": cal.previous_trading_day(on).isoformat(),
        "next_trading_day": cal.next_trading_day(on).isoformat(),
        "settlement_t_plus_2": cal.settlement_date(on, 2).isoformat() if trading else None,
        "note": "T+2 is two TRADING days forward on this calendar, not two "
                "calendar days. A half-day is open with an early close, not a "
                "holiday.",
    }


@router.get("/calendar/{mic}/session")
def session_state(mic: str, at: datetime = Query(..., description="Exchange-LOCAL time.")):
    cal = _calendar(mic)
    st = cal.state_at(at)
    return {"mic": cal.mic, "timezone": cal.timezone, "at": at.isoformat(),
            "state": st.value, "is_open": st.value == "open",
            "note": "Interpreted as exchange-local time, not UTC."}


@router.get("/calendar/{mic}/days")
def trading_days(mic: str, start: date = Query(...), end: date = Query(...)):
    cal = _calendar(mic)
    if (end - start).days > 400:
        raise HTTPException(422, "Range capped at 400 days.")
    days = cal.trading_days_between(start, end)
    return {"mic": cal.mic, "start": start.isoformat(), "end": end.isoformat(),
            "trading_days": len(days),
            "calendar_days": (end - start).days + 1,
            "half_days": [d.isoformat() for d in days if cal.is_half_day(d)],
            "holidays_in_range": sorted(h.isoformat() for h in cal.holidays
                                        if start <= h <= end),
            "days": [d.isoformat() for d in days]}


# -------------------------------------------------------- corporate actions
@router.get("/corporate-actions")
def corporate_actions(instrument_id: str | None = None,
                      start: date | None = None, end: date | None = None):
    if instrument_id:
        actions = CA_BOOK.for_instrument(instrument_id, start, end)
    else:
        actions = [a for i in SECURITY_MASTER.all_instruments()
                   for a in CA_BOOK.for_instrument(i.instrument_id, start, end)]
    return {
        "count": len(actions),
        "actions": [{
            "instrument_id": a.instrument_id, "type": a.action_type.value,
            "ex_date": a.ex_date.isoformat(),
            "pay_date": a.pay_date.isoformat() if a.pay_date else None,
            "ratio": a.ratio, "cash_amount": a.cash_amount,
            "currency": a.currency, "note": a.note,
            "adjusts_quantity": a.adjusts_quantity,
            "adjusts_price": a.adjusts_price,
        } for a in sorted(actions, key=lambda x: (x.ex_date, x.instrument_id))],
        "note": "Split ratios are NEW shares per OLD share: a 4-for-1 is 4.0. "
                "Records marked ILLUSTRATIVE are fixtures, not issuer records.",
    }


@router.get("/adjustment-factor")
def adjustment_factor(instrument_id: str, from_date: date = Query(...),
                      to_date: date | None = None):
    f = CA_BOOK.cumulative_price_factor(instrument_id, from_date, to_date)
    return {
        "instrument_id": instrument_id, "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat() if to_date else None,
        "price_factor": f,
        "quantity_factor": (1.0 / f) if f else None,
        "note": "Multiply a price observed on from_date by price_factor to state "
                "it in to_date terms. Without this a split shows as a real return "
                "on the ex-date, and every volatility, VaR and beta over that "
                "window is wrong.",
    }


class AdjustPositionRequest(BaseModel):
    instrument_id: str
    qty: float
    avg_cost: float
    ex_date: date


@router.post("/adjust-position")
def adjust_position(req: AdjustPositionRequest):
    actions = [a for a in CA_BOOK.for_instrument(req.instrument_id)
               if a.ex_date == req.ex_date]
    if not actions:
        raise HTTPException(404, f"No corporate action for {req.instrument_id} "
                                 f"with ex-date {req.ex_date}.")
    adj = CA_BOOK.adjust_position(instrument_id=req.instrument_id, qty=req.qty,
                                  avg_cost=req.avg_cost, action=actions[0])
    return {
        "instrument_id": adj.instrument_id, "ex_date": adj.ex_date.isoformat(),
        "action": adj.action_type.value,
        "qty_before": adj.qty_before, "qty_after": adj.qty_after,
        "cost_before": adj.cost_before, "cost_after": adj.cost_after,
        "book_value_before": adj.qty_before * adj.cost_before,
        "book_value_after": adj.qty_after * adj.cost_after,
        "cash_entitlement": adj.cash,
        "market_value_preserved": adj.market_value_preserved,
        "explanation": adj.explanation,
    }


# ----------------------------------------------------------- reconciliation
class PositionReconRequest(BaseModel):
    ours: dict[str, float]
    theirs: dict[str, float]
    as_of: date | None = None


@router.post("/reconcile/positions")
def recon_positions(req: PositionReconRequest):
    return reconcile_positions(req.ours, req.theirs, as_of=req.as_of).to_dict()


@router.post("/reconcile/cash")
def recon_cash(req: PositionReconRequest):
    return reconcile_cash(req.ours, req.theirs, as_of=req.as_of).to_dict()


class TradeRow(BaseModel):
    key: str
    qty: float
    price: float = 0.0
    trade_date: date | None = None


class TradeReconRequest(BaseModel):
    ours: list[TradeRow]
    theirs: list[TradeRow]
    as_of: date | None = None
    price_tolerance: float = 0.005


@router.post("/reconcile/trades")
def recon_trades(req: TradeReconRequest):
    return reconcile_trades(
        [Holding(r.key, r.qty, r.price, r.trade_date) for r in req.ours],
        [Holding(r.key, r.qty, r.price, r.trade_date) for r in req.theirs],
        as_of=req.as_of, price_tolerance=req.price_tolerance).to_dict()


# -------------------------------------------------------------- batch cycle
class PositionRow(BaseModel):
    instrument_id: str
    qty: float
    avg_cost: float
    mark: float = 0.0


class CycleRequest(BaseModel):
    business_date: date
    positions: list[PositionRow] = Field(default_factory=list)
    marks: dict[str, float] = Field(default_factory=dict)
    external_positions: dict[str, float] | None = None
    session_id: str | None = None
    require_full_marks: bool = True


@router.post("/batch/sod")
def run_sod(req: CycleRequest):
    sid, cyc = _cycle(req.session_id)
    try:
        res = cyc.run_sod(req.business_date,
                          [Position(p.instrument_id, p.qty, p.avg_cost, p.mark)
                           for p in req.positions],
                          external_positions=req.external_positions)
    except DailyCycleError as e:
        raise HTTPException(409, str(e))
    return {"session_id": sid, **res.to_dict()}


@router.post("/batch/eod")
def run_eod(req: CycleRequest):
    sid, cyc = _cycle(req.session_id)
    try:
        res = cyc.run_eod(req.business_date,
                          [Position(p.instrument_id, p.qty, p.avg_cost, p.mark)
                           for p in req.positions],
                          req.marks,
                          external_positions=req.external_positions,
                          require_full_marks=req.require_full_marks)
    except DailyCycleError as e:
        raise HTTPException(409, str(e))
    return {"session_id": sid, **res.to_dict()}


@router.get("/batch/history")
def batch_history(session_id: str):
    with _cycles_lock:
        cyc = _cycles.get(session_id)
    if cyc is None:
        raise HTTPException(404, f"No batch session '{session_id}'.")
    return {"session_id": session_id, "runs": cyc.history(),
            "note": "A phase already run for a business date is refused rather "
                    "than repeated; reverse it explicitly to re-run."}


@router.delete("/batch/{session_id}/{business_date}/{phase}")
def reverse_run(session_id: str, business_date: date, phase: str):
    with _cycles_lock:
        cyc = _cycles.get(session_id)
    if cyc is None:
        raise HTTPException(404, f"No batch session '{session_id}'.")
    try:
        ph = Phase(phase)
    except ValueError:
        raise HTTPException(422, f"Unknown phase '{phase}'. Use sod or eod.")
    reversed_ = cyc.reverse(business_date, ph)
    if not reversed_:
        raise HTTPException(404, f"{phase} has not run for {business_date}.")
    return {"reversed": True, "business_date": business_date.isoformat(),
            "phase": ph.value,
            "note": "The run record is cleared, so the phase can run again. "
                    "Reversing is deliberate and separate from running, because "
                    "a repeated cycle re-applies its adjustments."}
