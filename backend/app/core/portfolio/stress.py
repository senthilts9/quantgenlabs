"""Stress testing: historical replay, hypothetical shocks, reverse stress.

Three kinds, because each answers a question the others cannot:

  1. HISTORICAL REPLAY -- take the worst days that actually happened in the
     sample and re-run today's book through them. No distributional
     assumption, and every scenario is defensible because it occurred.

  2. HYPOTHETICAL SHOCKS -- named forward-looking scenarios (equity crash,
     rate spike, vol explosion). These cover moves the sample never contained,
     which is exactly what a stress test is for: history is not an upper bound.

  3. REVERSE STRESS -- invert the question. Instead of "what do I lose in a
     -20% crash", ask "what move breaks me?" Regulators ask for this and most
     risk systems still do not produce it. It is far more actionable: a PM can
     judge whether a 12% adverse move is plausible this quarter much better
     than they can interpret an abstract VaR number.

Historical replay uses the ACTUAL joint move on each date -- not each asset's
worst day glued together, which would fabricate a scenario with impossible
correlations and overstate the loss.
"""
from __future__ import annotations

import numpy as np

from .risk import align_on_dates, returns_from_levels

# Forward-looking shocks in RETURN space per risk factor. Sized to be severe
# but not absurd -- roughly 1987/2008/2020-scale moves for equities, and a
# 1994/2022-scale repricing for rates.
#
# EVERY scenario must quote EVERY factor. A factor missing from a scenario
# resolves to a 0.0 shock, so an FX or commodity book would report a serene
# zero loss in an equity crash and nothing would flag it. The zeros below are
# therefore deliberate assertions ("this factor genuinely does not move here"),
# not omissions.
#
# FX is expressed as USD APPRECIATION and each instrument carries a signed
# exposure to it (see marketdata.universe.factor_sign), which is why a single
# number here can drive EUR/USD and USD/JPY in opposite directions correctly.
_FACTORS = ("EQUITY", "VOL", "RATES", "CREDIT", "FX", "COMMODITY", "CRYPTO")

HYPOTHETICAL = [
    # Risk-off: equities down, vol up, flight to quality bids Treasuries
    # (yields down), credit gaps wider, dollar bid, crypto sells off hardest.
    ("Equity crash -20%",        {"EQUITY": -0.20, "VOL": 1.50, "RATES": -0.15,
                                  "CREDIT": 1.20, "FX": 0.05, "COMMODITY": -0.25,
                                  "CRYPTO": -0.35}),
    ("Equity selloff -10%",      {"EQUITY": -0.10, "VOL": 0.60, "RATES": -0.07,
                                  "CREDIT": 0.50, "FX": 0.02, "COMMODITY": -0.12,
                                  "CRYPTO": -0.18}),
    ("Vol spike (VIX +100%)",    {"EQUITY": -0.07, "VOL": 1.00, "RATES": -0.03,
                                  "CREDIT": 0.35, "FX": 0.015, "COMMODITY": -0.06,
                                  "CRYPTO": -0.12}),
    # 2022-style: yields up AND equities down, correlation flips positive.
    ("Rate shock +100bp",        {"EQUITY": -0.04, "VOL": 0.25, "RATES": 0.22,
                                  "CREDIT": 0.15, "FX": 0.02, "COMMODITY": -0.03,
                                  "CRYPTO": -0.10}),
    ("Stagflation: rates+, eq-", {"EQUITY": -0.12, "VOL": 0.70, "RATES": 0.18,
                                  "CREDIT": 0.60, "FX": -0.03, "COMMODITY": 0.20,
                                  "CRYPTO": -0.15}),
    # Dollar squeeze: the scenario that hurts a book hedged only for equity
    # risk, because the FX leg moves without equities leading it.
    ("USD squeeze (DXY +8%)",    {"EQUITY": -0.05, "VOL": 0.40, "RATES": 0.05,
                                  "CREDIT": 0.30, "FX": 0.08, "COMMODITY": -0.10,
                                  "CRYPTO": -0.10}),
    # Crypto-idiosyncratic: contagion into traditional assets stays limited,
    # which is what makes it a different test from an equity crash.
    ("Crypto crash -45%",        {"EQUITY": -0.02, "VOL": 0.20, "RATES": 0.0,
                                  "CREDIT": 0.05, "FX": 0.0, "COMMODITY": -0.01,
                                  "CRYPTO": -0.45}),
    ("Melt-up +8%",              {"EQUITY": 0.08, "VOL": -0.30, "RATES": 0.04,
                                  "CREDIT": -0.15, "FX": -0.02, "COMMODITY": 0.05,
                                  "CRYPTO": 0.15}),
]


def _factor_map() -> dict[str, str]:
    """Instrument -> risk factor, from the instrument master.

    Was a four-entry literal covering only SP500 and VIX. Anything unmapped is
    still treated as equity-like AND flagged, rather than silently assigned
    zero risk -- an unmapped position showing no stress loss is the dangerous
    failure mode, because it looks like a hedged book rather than a blind spot.
    """
    from ..marketdata.universe import factor_map
    return factor_map()


def _factor_signs() -> dict[str, float]:
    from ..marketdata.universe import factor_signs
    return factor_signs()


def historical_scenarios(exposures: dict[str, float],
                         return_series: dict[str, list[float]],
                         dates: dict[str, list[str]] | None = None,
                         top_n: int = 8) -> dict:
    """Replay the worst historical days for THIS book."""
    symbols = [s for s in exposures if s in return_series]
    if not symbols:
        return {"scenarios": [], "error": "no history for any position"}

    # Same date-alignment requirement as risk.analyse: a "worst day" built
    # from positionally-mismatched series is a day that never happened.
    label_dates = None
    if dates and all(s in dates for s in symbols):
        L, common = align_on_dates(return_series, dates, symbols)
        if L.shape[0] < 31:
            return {"scenarios": [],
                    "error": f"insufficient overlapping history ({L.shape[0]} common dates)"}
        R = np.diff(L, axis=0) / L[:-1]
        label_dates = common[1:]                 # returns lose the first row
        n = R.shape[0]
    else:
        series = [returns_from_levels(return_series[s]) for s in symbols]
        n = min((len(r) for r in series), default=0)
        if n < 30:
            return {"scenarios": [], "error": f"insufficient history ({n} obs)"}
        R = np.column_stack([r[-n:] for r in series])

    w = np.array([exposures[s] for s in symbols], dtype=float)
    pnl = R @ w
    order = np.argsort(pnl)                      # worst first

    worst = []
    for idx in order[:top_n]:
        worst.append({
            "date": label_dates[idx] if label_dates else f"obs {int(idx)}",
            "pnl": round(float(pnl[idx]), 2),
            "moves": {s: round(float(R[idx, i] * 100), 3)
                      for i, s in enumerate(symbols)},
        })
    best = [{
        "date": label_dates[i] if label_dates else f"obs {int(i)}",
        "pnl": round(float(pnl[i]), 2),
    } for i in order[-3:][::-1]]

    return {
        "n_observations": int(n),
        "worst_days": worst,
        "best_days": best,
        "mean_daily_pnl": round(float(pnl.mean()), 2),
        "daily_pnl_vol": round(float(pnl.std(ddof=1)), 2),
        "note": "each scenario is the ACTUAL joint move on that date, so the "
                "cross-asset correlations are ones the market really produced",
    }


def hypothetical_scenarios(exposures: dict[str, float],
                           betas: dict[str, float] | None = None) -> dict:
    """Apply named forward-looking shocks through each position's factor.

    Beta scales the factor move onto the instrument where it is known; absent
    a beta the exposure moves 1:1 with its factor, which is conservative for
    equity-like names and explicitly reported.
    """
    betas = betas or {}
    fmap, signs = _factor_map(), _factor_signs()
    unmapped = [s for s in exposures if s.upper() not in fmap]
    rows = []
    for name, shocks in HYPOTHETICAL:
        total, breakdown, by_factor = 0.0, {}, {}
        for sym, exp in exposures.items():
            key = sym.upper()
            factor = fmap.get(key, "EQUITY")
            # Beta is estimated against the EQUITY benchmark, so it may only
            # scale the EQUITY shock. Applying it to a commodity or FX shock
            # is a category error, and not a harmless one: crude's equity beta
            # is often negative, which turned a -25% commodity shock into a
            # PROFIT on a long WTI position during an equity crash. Every
            # other factor is a direct exposure and moves 1:1 with its own
            # shock, carrying only the quote-convention sign.
            beta = betas.get(sym) if factor == "EQUITY" else None
            move = (shocks.get(factor, 0.0) * signs.get(key, 1.0)
                    * (beta if beta is not None else 1.0))
            pnl = exp * move
            total += pnl
            breakdown[sym] = round(pnl, 2)
            by_factor[factor] = round(by_factor.get(factor, 0.0) + pnl, 2)
        rows.append({"scenario": name, "pnl": round(total, 2),
                     "shocks_pct": {k: round(v * 100, 1) for k, v in shocks.items()},
                     "by_position": breakdown,
                     "by_factor": by_factor})
    return {
        "scenarios": sorted(rows, key=lambda r: r["pnl"]),
        "factors": list(_FACTORS),
        "unmapped_symbols": unmapped,
        "note": ("positions with no explicit factor mapping are treated as "
                 "equity-like; they are listed so the assumption is visible"
                 if unmapped else "all positions mapped to a risk factor"),
    }


def reverse_stress(exposures: dict[str, float], loss_limit: float,
                   betas: dict[str, float] | None = None) -> dict:
    """What uniform adverse move exactly exhausts the loss limit?

    Solves loss_limit = |sum_i exposure_i * beta_i * k| for k, the market move
    in percent. Answers "how far can the market go before I breach", which a
    PM can weigh against their own view far more easily than an abstract VaR.
    """
    betas = betas or {}
    if loss_limit <= 0:
        return {"error": "loss limit must be positive"}

    # Directional sensitivity: a market-neutral book has near-zero net delta
    # and is genuinely hard to break with a uniform move, which this shows.
    net_delta = sum(exp * (betas.get(sym) or 1.0) for sym, exp in exposures.items())
    gross_delta = sum(abs(exp) * abs(betas.get(sym) or 1.0)
                      for sym, exp in exposures.items())

    if abs(net_delta) < 1e-9:
        return {
            "loss_limit": loss_limit,
            "net_delta": round(net_delta, 2),
            "gross_delta": round(gross_delta, 2),
            "breach_move_pct": None,
            "interpretation": "book is directionally neutral — a uniform market "
                              "move does not breach the limit; the risk here is "
                              "basis/correlation, which a uniform shock cannot express",
        }

    k = loss_limit / abs(net_delta)
    direction = "down" if net_delta > 0 else "up"
    return {
        "loss_limit": loss_limit,
        "net_delta": round(net_delta, 2),
        "gross_delta": round(gross_delta, 2),
        "breach_move_pct": round(k * 100, 3),
        "direction": direction,
        "interpretation": (f"a {k * 100:.2f}% move {direction} exhausts the "
                           f"{loss_limit:,.0f} loss limit"),
        # Plausibility anchor: comparing the breach move to a normal daily
        # sigma tells you whether this is a routine day or a tail event.
        "hedge_note": ("reduce net delta or add a short hedge to widen this "
                       "buffer" if k < 0.05 else
                       "buffer is wide relative to typical daily moves"),
    }


def run_all(exposures: dict[str, float], return_series: dict[str, list[float]],
            dates: dict[str, list[str]] | None = None,
            loss_limit: float = 50_000.0,
            betas: dict[str, float] | None = None) -> dict:
    return {
        "historical": historical_scenarios(exposures, return_series, dates),
        "hypothetical": hypothetical_scenarios(exposures, betas),
        "reverse": reverse_stress(exposures, loss_limit, betas),
    }
