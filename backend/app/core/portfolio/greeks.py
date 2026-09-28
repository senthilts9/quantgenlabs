"""Portfolio Greeks — first- and second-order risk aggregated over the book.

Greeks were only ever available for a single hypothetical contract on the
pricing screen. A desk needs them for the BOOK: the question is never "what is
this option's delta" but "how much do I make or lose if the market gaps 2%,
and how fast does that answer change".

Two things here are deliberate:

  LINEAR POSITIONS HAVE GREEKS TOO, and reporting them as blank is wrong. A
  long index future has delta 1.0 per unit and gamma exactly zero. That zero
  is a RESULT, not missing data -- it says the book has no convexity, so a
  delta hedge holds until the position changes. Blank would imply unknown.

  DELTA IS REPORTED THREE WAYS. Raw delta (per unit), cash delta (what a 1%
  move costs), and BETA-ADJUSTED cash delta (cash delta scaled by each
  instrument's beta to the index). Summing raw cash delta across an equity
  index, a gold position and a yen position implies those all move together,
  which they do not. Beta-adjusted delta is the number an equity desk actually
  runs on, and the gap between the two measures the diversification being
  assumed.

Option Greeks route through the same engine selector as the pricing screen, so
they are computed in C++ when the extension is loaded, and appear in the
profiler under the same `timed` seam.
"""
from __future__ import annotations

import math

import numpy as np

TRADING_DAYS = 252


def _linear_greeks(qty: float, mark: float) -> dict:
    """Greeks of a linear instrument: delta 1 per unit, everything else zero.

    Zero gamma/vega/theta is a statement about the instrument, not an absence
    of data -- a future or a spot position has no convexity and no vol
    sensitivity by construction.
    """
    return {"delta": 1.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0,
            "cash_delta": qty * mark, "cash_gamma": 0.0,
            "position_delta": qty, "convex": False}


def _option_greeks(spec: dict, qty: float, spot: float, vol: float,
                   rate: float = 0.045) -> dict | None:
    """Per-contract Greeks from the pricing engine, scaled by position size."""
    from ..engines import price_black_scholes
    from ..pricing import Exercise, Option, OptionType

    T = max(spec["years_to_expiry"], 1e-6)
    opt = Option(spot=spot, strike=spec["strike"], maturity=T, rate=rate,
                 dividend=0.0, vol=max(vol, 1e-4),
                 kind=OptionType.CALL if spec["is_call"] else OptionType.PUT,
                 exercise=Exercise.EUROPEAN)
    d = price_black_scholes(opt)
    g = d.get("greeks") or {}
    if not g:
        return None
    mult = spec.get("multiplier", 1.0)
    scale = qty * mult
    delta = float(g.get("delta", 0.0))
    gamma = float(g.get("gamma", 0.0))
    return {
        "delta": delta, "gamma": gamma,
        "vega": float(g.get("vega", 0.0)), "theta": float(g.get("theta", 0.0)),
        "rho": float(g.get("rho", 0.0)),
        # Cash delta: value change for a 1% move in the underlying.
        "cash_delta": delta * scale * spot,
        # Cash gamma: how much cash delta itself moves on a 1% move. This is
        # the number that says whether a delta hedge survives the day.
        "cash_gamma": gamma * scale * spot * spot * 0.01,
        "position_delta": delta * scale,
        "option_price": d["price"], "impl": d.get("impl"),
        "convex": True,
    }


def book_greeks(positions: list[dict], marks: dict[str, float],
                vols: dict[str, float], betas: dict[str, float] | None = None,
                option_specs: dict[str, dict] | None = None,
                rate: float = 0.045) -> dict:
    """Aggregate Greeks across every position on the book.

    `positions` are OMS rows; `vols` are annualised decimals per underlying;
    `betas` map instrument -> beta vs the equity benchmark.
    """
    betas = betas or {}
    option_specs = option_specs or {}
    rows, missing = [], []
    tot = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0,
           "cash_delta": 0.0, "cash_gamma": 0.0, "beta_adj_cash_delta": 0.0}

    for p in positions:
        sym, qty = p["symbol"], float(p.get("qty") or 0.0)
        if qty == 0:
            continue
        spec = option_specs.get(sym)
        underlying = spec["underlying"] if spec else sym
        mark = marks.get(underlying) or marks.get(sym) or p.get("mark") or p.get("avg_price")
        if not mark:
            missing.append(sym)
            continue

        if spec:
            g = _option_greeks(spec, qty, float(mark),
                               vols.get(underlying, 0.20), rate)
            if g is None:
                missing.append(sym)
                continue
        else:
            g = _linear_greeks(qty, float(mark))

        beta = betas.get(underlying, betas.get(sym))
        # No beta means the instrument was never regressed on the benchmark.
        # Assuming 1.0 would silently claim it moves with equities -- gold and
        # yen emphatically do not -- so it is excluded from the beta-adjusted
        # total and listed, rather than defaulted.
        beta_adj = g["cash_delta"] * beta if beta is not None else None

        rows.append({
            "symbol": sym, "portfolio_id": p.get("portfolio_id", "DEFAULT"),
            "qty": qty, "underlying": underlying, "mark": round(float(mark), 6),
            "is_option": bool(spec),
            "delta": round(g["delta"], 6), "gamma": round(g["gamma"], 8),
            "vega": round(g["vega"], 6), "theta": round(g["theta"], 8),
            "rho": round(g["rho"], 6),
            "position_delta": round(g["position_delta"], 4),
            "cash_delta": round(g["cash_delta"], 2),
            "cash_gamma": round(g["cash_gamma"], 4),
            "beta": beta, "beta_adj_cash_delta": round(beta_adj, 2) if beta_adj is not None else None,
            "convex": g["convex"],
        })
        for k in ("delta", "gamma", "vega", "theta", "rho"):
            tot[k] += g[k] * qty
        tot["cash_delta"] += g["cash_delta"]
        tot["cash_gamma"] += g["cash_gamma"]
        if beta_adj is not None:
            tot["beta_adj_cash_delta"] += beta_adj

    unbeta = [r["symbol"] for r in rows if r["beta_adj_cash_delta"] is None]
    n_opt = sum(1 for r in rows if r["is_option"])
    gross_cash_delta = sum(abs(r["cash_delta"]) for r in rows)

    # Bounds are asserted per CONTRACT, and only delta is bounded.
    #
    #   delta = dV/dS is dimensionless and lies in [-1, 1] for a vanilla: the
    #     option cannot outrun its own underlying.
    #   gamma = d2V/dS2 carries units of 1/price and has NO such bound. On an
    #     index near 7,700 a typical gamma is ~0.0006; on a $2 underlying the
    #     same contract's gamma is hundreds of times larger. Comparing it to 1
    #     is a units error, not a risk check.
    #   CASH gamma is a currency amount (how many dollars of delta are gained
    #     per 1% move), so it is unbounded by construction and routinely runs
    #     to thousands.
    #
    # Portfolio-level gamma may legitimately be negative -- that is what being
    # short options means -- so the sign check applies to long vanillas only.
    violations = []
    for r in rows:
        if not (-1.0 <= r["delta"] <= 1.0):
            violations.append(f"{r['symbol']}: delta {r['delta']} outside [-1, 1]")
        if r["is_option"] and r["gamma"] < 0:
            violations.append(f"{r['symbol']}: per-contract gamma {r['gamma']} < 0")

    return {
        "positions": rows,
        "totals": {k: round(v, 4) for k, v in tot.items()},
        # Value change for a 1% move in every underlying at once.
        "pnl_1pct_move": round(tot["cash_delta"] * 0.01, 2),
        "pnl_1pct_beta_adjusted": round(tot["beta_adj_cash_delta"] * 0.01, 2),
        "gross_cash_delta": round(gross_cash_delta, 2),
        "net_over_gross_delta": round(abs(tot["cash_delta"]) / gross_cash_delta, 4)
                                if gross_cash_delta else None,
        "option_positions": n_opt,
        "linear_positions": len(rows) - n_opt,
        "positions_without_beta": unbeta or None,
        "missing_marks": missing or None,
        "bounds_check": {
            "delta_in_range": not any("delta" in v for v in violations),
            "gamma_sign_ok": not any("gamma" in v for v in violations),
            "violations": violations or None,
            "note": ("delta is dimensionless and bounded to [-1, 1]; gamma is a "
                     "SECOND derivative with units of 1/price and has no such "
                     "bound — on an index near 7,700 it is ~0.0006, and cash "
                     "gamma is a currency amount that runs to thousands. "
                     "Portfolio gamma may be negative: that is short optionality."),
        },
        "convexity_note": (
            "book is entirely linear — gamma and vega are exactly zero, so a "
            "delta hedge holds until positions change. That is a property of "
            "the instruments held, not missing data."
            if n_opt == 0 else
            f"{n_opt} option position(s) carry convexity: cash gamma "
            f"{tot['cash_gamma']:,.0f} means portfolio delta itself moves that "
            f"much on a 1% underlying move, so the hedge must be re-struck."),
        "beta_note": (
            "beta-adjusted delta scales each position by its sensitivity to the "
            "equity benchmark. Raw cash delta sums a gold, a yen and an index "
            "position as though they move together; they do not, and the gap "
            "between the two totals is the diversification being assumed."),
    }


def delta_ladder(positions: list[dict], marks: dict[str, float],
                 vols: dict[str, float], option_specs: dict[str, dict] | None = None,
                 shocks_pct: tuple[float, ...] = (-10, -5, -2, -1, 0, 1, 2, 5, 10),
                 rate: float = 0.045) -> dict:
    """Re-price the book across a ladder of underlying shocks.

    A single delta is a tangent line. The ladder is the curve, and for a book
    with options the two diverge exactly where it matters -- in the tail. It
    is a full revaluation at each node, not a delta/gamma Taylor expansion,
    so it stays correct for large moves where that expansion breaks down.
    """
    option_specs = option_specs or {}
    base = None
    rungs = []
    for s in shocks_pct:
        shocked = {k: v * (1 + s / 100.0) for k, v in marks.items()}
        total = 0.0
        for p in positions:
            sym, qty = p["symbol"], float(p.get("qty") or 0.0)
            if qty == 0:
                continue
            spec = option_specs.get(sym)
            und = spec["underlying"] if spec else sym
            m = shocked.get(und) or shocked.get(sym)
            if not m:
                continue
            if spec:
                g = _option_greeks(spec, qty, float(m), vols.get(und, 0.20), rate)
                total += (g["option_price"] * qty * spec.get("multiplier", 1.0)) if g else 0.0
            else:
                total += qty * float(m)
        if s == 0:
            base = total
        rungs.append({"shock_pct": s, "book_value": round(total, 2)})

    for r in rungs:
        r["pnl"] = round(r["book_value"] - (base or 0.0), 2)

    # Curvature test. A second difference only measures curvature on EQUALLY
    # SPACED nodes, and this ladder is deliberately not equally spaced (it is
    # denser near the money). Applied naively it reported a perfectly linear
    # book as convex. Instead, fit the best straight line through the ladder
    # and measure the largest residual: for a linear book every point lies on
    # it exactly, whatever the spacing.
    xs = np.array([r["shock_pct"] for r in rungs], float)
    ys = np.array([r["pnl"] for r in rungs], float)
    slope, intercept = np.polyfit(xs, ys, 1) if len(xs) > 1 else (0.0, 0.0)
    resid = np.abs(ys - (slope * xs + intercept))
    max_resid = float(resid.max()) if len(resid) else 0.0
    scale = max(float(np.abs(ys).max()), 1.0)
    # Relative tolerance: the ladder is rounded to cents, so an absolute
    # threshold would flag rounding noise on a large book as convexity.
    curvature = max_resid / scale
    linear = curvature < 1e-6

    return {
        "base_value": round(base or 0.0, 2),
        "ladder": rungs,
        "linear_slope_per_1pct": round(float(slope), 2),
        "max_residual_vs_line": round(max_resid, 4),
        "relative_curvature": round(curvature, 10),
        "is_linear": bool(linear),
        "note": ("P&L is exactly linear in the shock — no convexity in the book, "
                 "so delta fully describes it and a hedge holds at any size of move"
                 if linear else
                 "P&L curves away from the straight line: delta alone misstates "
                 "the tail, which is precisely what gamma measures"),
    }
