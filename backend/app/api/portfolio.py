"""Factor & Sensitivity + Stress & Scenarios — over the live OMS book."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Query

from ..core import portfolio as pf
from ..core.marketdata import fred_series, series_map

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

# OMS symbol -> FRED series carrying its history, from the instrument master.
# This was a three-entry literal that silently disagreed with the OMS's own
# price map and the stress-test factor map; all three now read one source, so
# adding an instrument makes it tradable, markable, risk-mapped and
# stress-mapped in one place. Symbols outside the master are reported as
# unmapped rather than assigned a fabricated series.
BENCHMARK = "SP500"


#: Firm-wide analytics default to the traditional book. Digital assets trade
#: 24/7 on separate venues under separate custody; folding them into a factor
#: model whose history is end-of-day FRED data produces a number that looks
#: firm-wide and is not. `desk=all` is available for anyone who wants the
#: blended view deliberately.
DESK_PATTERN = "^(traditional|digital|all)$"


def _book(limit_days: int, desk: str = "traditional"):
    """Signed exposures from the OMS + aligned FRED history, scoped to a desk."""
    from ..core.marketdata.universe import is_crypto
    from .oms import _marks, current_limits, oms

    engine = oms()
    marks = _marks()
    positions = engine.store.list_positions()
    smap = series_map()
    exposures, history, dates, unmapped = {}, {}, {}, []
    wanted: dict[str, str] = {}                 # symbol -> FRED series id
    for p in positions:
        if desk != "all" and is_crypto(p.symbol) != (desk == "digital"):
            continue
        sid = smap.get(p.symbol.upper())
        if not sid:
            if p.symbol not in unmapped:
                unmapped.append(p.symbol)
            continue
        mark = marks.get(p.symbol) or p.avg_price
        # SIGNED and ACCUMULATED. Signed so shorts reduce rather than add to
        # directional risk; accumulated because the same instrument can now be
        # held by several books. This was a plain assignment, which was correct
        # only while one symbol meant one position -- once multi-portfolio
        # landed, a symbol held in three books kept whichever row was read last
        # and silently dropped the other two from firm VaR. Netting across
        # books is the right firm-level treatment: a long in one book genuinely
        # does offset a short in another.
        exposures[p.symbol] = exposures.get(p.symbol, 0.0) + p.qty * mark
        wanted.setdefault(p.symbol, sid)
    wanted.setdefault(BENCHMARK, smap.get(BENCHMARK, BENCHMARK))

    # Fetch every series CONCURRENTLY. These were pulled one at a time in the
    # loop above, so a 17-instrument book made 17 sequential HTTPS round-trips
    # to FRED -- roughly 30 seconds on a cold cache, which is what made the
    # Factor & Sensitivity and Market Risk screens sit blank. The calls are
    # independent and the underlying fetch is already cached, so the only
    # thing serialising them was the loop. Worker count is capped: opening a
    # connection per symbol to a free public service is not a fix.
    def _pull(item: tuple[str, str]) -> tuple[str, dict]:
        sym, sid = item
        return sym, fred_series(sid, limit_days)

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(wanted)))) as pool:
        for sym, s in pool.map(_pull, wanted.items()):
            pts = (s or {}).get("points") or []
            if pts:
                history[sym] = [pt["value"] for pt in pts]
                dates[sym] = [pt["date"] for pt in pts]

    return exposures, history, dates, unmapped, engine, current_limits(engine)


@router.get("/factors")
def factors(desk: str = Query("traditional", pattern=DESK_PATTERN),
            days: int = Query(260, ge=60, le=2000),
            confidence: float = Query(0.99, gt=0.5, lt=1)) -> dict:
    try:
        exposures, history, dates, unmapped, _, _ = _book(days, desk)
        if not exposures:
            return {"empty": True, "unmapped_symbols": unmapped,
                    "message": "no positions with mapped history — trade on the "
                               "Blotter screen first"}
        res = pf.analyse(exposures, history, BENCHMARK, confidence, dates=dates)
        out = pf.public(res)
        out["unmapped_symbols"] = unmapped
        return out
    except Exception as e:
        raise HTTPException(502, f"factor analysis failed: {type(e).__name__}: {e}")


@router.get("/stress")
def stress(desk: str = Query("traditional", pattern=DESK_PATTERN),
           days: int = Query(260, ge=60, le=2000)) -> dict:
    try:
        exposures, history, dates, unmapped, _, limits = _book(days, desk)
        if not exposures:
            return {"empty": True, "unmapped_symbols": unmapped,
                    "message": "no positions with mapped history"}
        res = pf.analyse(exposures, history, BENCHMARK, 0.99, dates=dates)
        betas = {p["symbol"]: p["beta"] for p in res.get("positions", [])
                 if p.get("beta") is not None}
        out = pf.run_all(exposures, history, dates, limits.daily_loss_limit, betas)
        out["exposures"] = {k: round(v, 2) for k, v in exposures.items()}
        out["gross_exposure"] = res.get("gross_exposure")
        out["net_exposure"] = res.get("net_exposure")
        out["var_historical"] = res.get("var_historical")
        out["daily_loss_limit"] = limits.daily_loss_limit
        out["unmapped_symbols"] = unmapped
        return out
    except Exception as e:
        raise HTTPException(502, f"stress test failed: {type(e).__name__}: {e}")


@router.get("/optimise")
def optimise(desk: str = Query("traditional", pattern=DESK_PATTERN),
             days: int = Query(260, ge=60, le=2000),
             rf: float = Query(0.04, ge=-0.05, le=0.25)) -> dict:
    """Min-variance / risk-parity / max-Sharpe on the live book's covariance."""
    import numpy as np

    from ..core.portfolio import optimise_mod
    from ..core.portfolio.risk import align_on_dates

    try:
        exposures, history, dates, unmapped, _, _ = _book(days, desk)
        syms = [s for s in exposures if s in history and s in dates]
        if len(syms) < 2:
            return {"empty": True, "symbols": syms, "unmapped_symbols": unmapped,
                    "message": ("portfolio optimisation needs at least 2 positions "
                                "with price history — add another instrument on the "
                                "Blotter screen")}
        L, common = align_on_dates(history, dates, syms)
        if L.shape[0] < 31:
            return {"empty": True,
                    "message": f"only {L.shape[0]} overlapping dates; need 31+"}
        R = np.diff(L, axis=0) / L[:-1]
        cur = np.array([exposures[s] for s in syms], dtype=float)
        out = optimise_mod.optimise(syms, R, rf, cur)
        out["alignment"] = f"date-aligned on {len(common)} common trading days"
        out["current_exposures"] = {s: round(exposures[s], 2) for s in syms}
        out["unmapped_symbols"] = unmapped
        return out
    except Exception as e:
        raise HTTPException(502, f"optimisation failed: {type(e).__name__}: {e}")


@router.get("/frm")
def frm_measures(desk: str = Query("traditional", pattern=DESK_PATTERN),
                 days: int = Query(500, ge=100, le=2000),
                 rf: float = Query(0.04, ge=-0.05, le=0.25),
                 confidence: float = Query(0.95, gt=0.5, lt=1)) -> dict:
    """FRM Part I & II measure suite over the live book's return series."""
    import numpy as np

    from ..core.portfolio import frm
    from ..core.portfolio.risk import align_on_dates

    try:
        exposures, history, dates, unmapped, _, limits = _book(days, desk)
        syms = [s for s in exposures if s in history and s in dates]
        if not syms:
            return {"empty": True, "unmapped_symbols": unmapped,
                    "message": "no positions with mapped history"}

        bench = BENCHMARK if BENCHMARK in history else syms[0]
        allsyms = syms + ([bench] if bench not in syms else [])
        L, common = align_on_dates(history, dates, allsyms)
        if L.shape[0] < 101:
            return {"empty": True,
                    "message": f"only {L.shape[0]} overlapping dates; need 101+"}
        R = np.diff(L, axis=0) / L[:-1]
        w = np.array([exposures[s] for s in syms], float)
        gross = float(np.abs(w).sum()) or 1.0
        # Portfolio return = exposure-weighted, normalised so the series is a
        # RETURN not a currency P&L -- Sharpe on currency P&L is meaningless.
        port = R[:, :len(syms)] @ (w / gross)
        bench_r = R[:, allsyms.index(bench)]

        out = {
            "as_of_days": L.shape[0], "symbols": syms, "benchmark": bench,
            "alignment": f"date-aligned on {len(common)} common trading days",
            "moments": frm.moments(port),
            "performance": frm.performance(port, bench_r, rf),
            "ewma": frm.ewma_vol(port),
            "garch": frm.garch11(port),
            "liquidity_adjusted_var": None,
            "credit_example": frm.expected_loss(0.02, 0.45, gross),
            "unmapped_symbols": unmapped,
        }
        if len(syms) >= 2:
            out["coherence"] = frm.coherence_check(
                R[:, 0] * w[0], R[:, 1] * w[1], confidence)
        # LVaR needs a VaR to adjust; reuse the parametric one from risk.analyse
        base = analyse(days, confidence) if False else None
        from ..core.portfolio import risk as PR
        res = PR.analyse(exposures, history, bench, confidence, dates=dates)
        if "var_parametric" in res:
            out["liquidity_adjusted_var"] = frm.liquidity_adjusted_var(
                res["var_parametric"], gross, spread_pct=0.05,
                spread_vol_pct=0.02, confidence=0.99)
            out["var_parametric"] = res["var_parametric"]
            out["var_historical"] = res["var_historical"]
        return out
    except Exception as e:
        raise HTTPException(502, f"FRM suite failed: {type(e).__name__}: {e}")


@router.get("/var-suite")
def var_suite(desk: str = Query("traditional", pattern=DESK_PATTERN),
              days: int = Query(500, ge=100, le=2000),
              confidence: float = Query(0.99, gt=0.5, lt=1)) -> dict:
    """Cross-method VaR/ES on the live book, plus a Basel backtest.

    Every method runs on the SAME return series so the numbers are directly
    comparable -- the spread between them is the model risk, and that is the
    point of showing them side by side rather than picking one.
    """
    import numpy as np

    from ..core import risk as CR
    from ..core.portfolio import frm
    from ..core.portfolio.risk import align_on_dates

    try:
        exposures, history, dates, unmapped, _, _ = _book(days, desk)
        syms = [s for s in exposures if s in history and s in dates]
        if not syms:
            return {"empty": True, "message": "no positions with mapped history",
                    "unmapped_symbols": unmapped}
        L, common = align_on_dates(history, dates, syms)
        if L.shape[0] < 101:
            return {"empty": True,
                    "message": f"only {L.shape[0]} overlapping dates; need 101+"}
        R = np.diff(L, axis=0) / L[:-1]
        w = np.array([exposures[s] for s in syms], float)
        gross = float(np.abs(w).sum()) or 1.0
        pnl_ret = R @ (w / gross)          # portfolio return series

        rows = []
        for name, fn in sorted(CR.VAR_METHODS.items()):
            try:
                res = fn(pnl_ret, confidence)
                res95 = fn(pnl_ret, 0.95)
                rows.append({
                    "method": name,
                    "var_pct": round(res.var * 100, 4),
                    "es_pct": round(res.es * 100, 4) if res.es else None,
                    "var95_pct": round(res95.var * 100, 4),
                    "var_currency": round(res.var * gross, 2),
                    "es_currency": round(res.es * gross, 2) if res.es else None,
                })
            except Exception as e:
                rows.append({"method": name, "error": f"{type(e).__name__}"})

        # Basel backtest: rolling VaR forecast vs realised P&L
        backtest = None
        if len(pnl_ret) >= 150:
            win = 100
            fc = np.array([
                -float(np.quantile(pnl_ret[i - win:i], 1 - confidence))
                for i in range(win, len(pnl_ret))])
            realised = pnl_ret[win:]
            from dataclasses import asdict
            # BacktestResult is slots=True -- asdict, never __dict__.
            bt = asdict(CR.backtest_var(realised, fc, confidence))
            backtest = {k: (round(v, 6) if isinstance(v, float) else v)
                        for k, v in bt.items()}

        return {
            "symbols": syms, "gross_exposure": round(gross, 2),
            "confidence": confidence,
            "n_observations": int(len(pnl_ret)),
            "alignment": f"date-aligned on {len(common)} common trading days",
            "methods": rows,
            "backtest": backtest,
            "moments": frm.moments(pnl_ret),
            "ewma": {k: v for k, v in frm.ewma_vol(pnl_ret).items()
                     if k != "path_annual_pct"},
            "garch": frm.garch11(pnl_ret) if len(pnl_ret) >= 100 else None,
            "unmapped_symbols": unmapped,
        }
    except Exception as e:
        raise HTTPException(502, f"VaR suite failed: {type(e).__name__}: {e}")


@router.get("/greeks")
def greeks(desk: str = Query("traditional", pattern=DESK_PATTERN),
           rate: float = Query(0.045, ge=0, le=0.25)) -> dict:
    """First- and second-order Greeks aggregated across the live book.

    Linear positions report delta 1.0 and gamma exactly 0 — a result, not a
    gap. Options (symbols like SPX-P-7500-20270115) carry real convexity and
    are priced through the same engine selector as the pricing screen.
    """
    from ..core.marketdata import universe as U
    from ..core.portfolio import greeks as G
    from .oms import _marks, oms, underlying_vol

    engine = oms()
    # Scope the POSITION LIST, not only the beta calculation below. Passing
    # `desk` to _book() alone filtered the factor regression while this list
    # still enumerated every position on the book, so the screen reported
    # crypto deltas under a traditional-desk heading.
    positions = [
        {"symbol": p.symbol, "qty": p.qty, "portfolio_id": p.portfolio_id,
         "avg_price": p.avg_price}
        for p in engine.store.list_positions()
        if desk == "all" or U.is_crypto(p.symbol) == (desk == "digital")
    ]
    if not positions:
        return {"empty": True, "message": "no positions on the book"}

    marks = _marks()
    specs, unds = {}, set()
    for p in positions:
        s = U.parse_option(p["symbol"])
        if s:
            specs[p["symbol"]] = s
            unds.add(s["underlying"])
        else:
            unds.add(p["symbol"])

    # Underlying marks are needed for option positions whose UNDERLYING is not
    # itself held -- otherwise a naked option would silently be dropped for a
    # missing mark.
    extra = [u for u in unds if u not in marks]
    if extra:
        marks = {**marks, **_marks(extra)}

    vols = {u: underlying_vol(u) for u in unds}
    betas = {}
    try:
        exposures, history, dates, _, _, _ = _book(260, desk)
        res = pf.analyse(exposures, history, BENCHMARK, 0.99, dates=dates)
        for row in res.get("positions", []):
            if row.get("beta") is not None:
                betas[row["symbol"]] = row["beta"]
    except Exception:
        pass                       # beta-adjusted delta degrades, raw does not

    out = G.book_greeks(positions, marks, vols, betas, specs, rate)
    out["ladder"] = G.delta_ladder(positions, marks, vols, specs, rate=rate)
    out["vol_source"] = ("realised vol from price history, used as an implied-vol "
                         "proxy — there is no options feed here, so option marks "
                         "are model prices rather than quotes")
    out["vols_used_pct"] = {k: round(v * 100, 2) for k, v in sorted(vols.items())}
    return out
