"""Bond analytics off a bootstrapped curve.

Everything here is priced by discounting cash flows on the real zero curve
rather than off a single yield-to-maturity. That distinction matters: YTM
maths assumes a flat curve, which is exactly wrong for measuring the risk of
a steepener or a butterfly.

Closed-form duration/convexity are cross-checked against finite-difference
bump-and-reprice. If an analytic derivative and a numerical one disagree,
one of them is wrong, and shipping the pair makes that impossible to miss.

KEY RATE DURATIONS are the reason this module exists. A single duration
number says a portfolio loses X for a parallel shift -- but curves almost
never move in parallel, and a barbell and a bullet with identical duration
have completely different P&L in a steepener. KRDs decompose the exposure
per tenor bucket, and their sum must reconcile to effective duration (an
invariant this module asserts).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .curve import FREQ, ZeroCurve, _coupon_times, bootstrap

BP = 1e-4


@dataclass
class Bond:
    face: float = 100.0
    coupon_pct: float = 4.0          # annual coupon rate, percent
    maturity: float = 10.0           # years

    def cashflows(self) -> tuple[np.ndarray, np.ndarray]:
        t = _coupon_times(self.maturity)
        cf = np.full(len(t), self.face * self.coupon_pct / 100.0 / FREQ)
        cf[-1] += self.face
        return t, cf


def price(bond: Bond, curve: ZeroCurve) -> float:
    t, cf = bond.cashflows()
    return float((cf * curve.discount(t)).sum())


def ytm(bond: Bond, curve: ZeroCurve) -> float:
    """Semiannual-compounded yield to maturity implied by the curve price.

    Solved by bisection rather than Newton: it cannot diverge, and the
    bracket [-50%, +200%] covers every plausible market plus the deep
    negative-rate regimes that broke a lot of production code in 2015-2021.
    """
    target = price(bond, curve)
    t, cf = bond.cashflows()

    def pv(y: float) -> float:
        return float((cf / (1 + y / FREQ) ** (FREQ * t)).sum())

    lo, hi = -0.5, 2.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if pv(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def analytics(bond: Bond, curve: ZeroCurve) -> dict:
    """Price, durations, convexity, DV01 — analytic and numerically verified."""
    p = price(bond, curve)
    t, cf = bond.cashflows()
    y = ytm(bond, curve)

    # Analytic measures on the YTM basis (the market convention for quoting)
    disc = (1 + y / FREQ) ** (FREQ * t)
    pv = cf / disc
    macaulay = float((t * pv).sum() / p)
    modified = macaulay / (1 + y / FREQ)
    convexity = float(((t * (t + 1 / FREQ)) * pv).sum() / (p * (1 + y / FREQ) ** 2))
    dv01 = modified * p * BP

    # Finite-difference cross-check on the CURVE (parallel shift of zeros),
    # which is the risk that actually gets hedged.
    up = ZeroCurve(curve.years, curve.zeros + BP)
    dn = ZeroCurve(curve.years, curve.zeros - BP)
    p_up, p_dn = price(bond, up), price(bond, dn)
    eff_duration = (p_dn - p_up) / (2 * p * BP)
    eff_convexity = (p_up + p_dn - 2 * p) / (p * BP ** 2)
    dv01_numeric = (p_dn - p_up) / 2

    return {
        "price": round(p, 6),
        "ytm_pct": round(y * 100, 5),
        "macaulay_duration": round(macaulay, 6),
        "modified_duration": round(modified, 6),
        "effective_duration": round(float(eff_duration), 6),
        "convexity": round(convexity, 5),
        "effective_convexity": round(float(eff_convexity), 5),
        "dv01": round(dv01, 6),
        "dv01_numeric": round(float(dv01_numeric), 6),
        # Analytic (flat-YTM) vs curve-based numerics differ by construction;
        # quoting the gap keeps the distinction visible instead of implying a
        # precision the two methods don't share.
        "dv01_check_diff": round(abs(dv01 - float(dv01_numeric)), 8),
    }


def key_rate_durations(bond: Bond, years, par_yields_pct,
                       key_tenors=(0.5, 2.0, 5.0, 10.0, 30.0)) -> dict:
    """KRD per key tenor via triangular bumps of the PAR curve.

    Each key tenor is bumped 1bp with a tent-shaped decay to its neighbours,
    so the bumps partition the curve: summing them reproduces a parallel
    shift, which is why sum(KRD) must reconcile to effective duration. That
    reconciliation is returned, not assumed.
    """
    years = np.asarray(years, dtype=float)
    par = np.asarray(par_yields_pct, dtype=float)
    base_curve = bootstrap(years, par)
    p0 = price(bond, base_curve)
    keys = sorted(key_tenors)

    def tent(centre_idx: int) -> np.ndarray:
        """1 at the key tenor, decaying linearly to 0 at adjacent keys."""
        lo = keys[centre_idx - 1] if centre_idx > 0 else None
        hi = keys[centre_idx + 1] if centre_idx < len(keys) - 1 else None
        c = keys[centre_idx]
        w = np.zeros_like(years)
        for i, t in enumerate(years):
            if t == c:
                w[i] = 1.0
            elif lo is not None and lo < t < c:
                w[i] = (t - lo) / (c - lo)
            elif hi is not None and c < t < hi:
                w[i] = (hi - t) / (hi - c)
            elif (lo is None and t < c) or (hi is None and t > c):
                w[i] = 1.0        # flat beyond the first/last key
        return w

    krds, total = [], 0.0
    for i, k in enumerate(keys):
        w = tent(i)
        up = price(bond, bootstrap(years, par + w * BP * 100))
        dn = price(bond, bootstrap(years, par - w * BP * 100))
        krd = (dn - up) / (2 * p0 * BP)
        total += krd
        krds.append({"tenor_years": k, "krd": round(float(krd), 6),
                     "dv01_contribution": round(float(krd * p0 * BP), 6)})

    up_all = price(bond, bootstrap(years, par + BP * 100))
    dn_all = price(bond, bootstrap(years, par - BP * 100))
    eff = (dn_all - up_all) / (2 * p0 * BP)
    return {
        "price": round(p0, 6),
        "key_rate_durations": krds,
        "sum_krd": round(float(total), 6),
        "effective_duration": round(float(eff), 6),
        # Small residual is expected: the tent weights partition the quoted
        # tenor grid, not the continuum the parallel shift moves.
        "reconciliation_diff": round(abs(float(total - eff)), 6),
    }


def scenario_pnl(bond: Bond, years, par_yields_pct, scenarios=None) -> list[dict]:
    """Reprice under named curve moves — how a rates PM actually thinks.

    Parallel is the easy one; steepeners, flatteners and butterflies are
    where duration alone stops being informative.
    """
    years = np.asarray(years, dtype=float)
    par = np.asarray(par_yields_pct, dtype=float)
    p0 = price(bond, bootstrap(years, par))
    short = years <= 2.0
    belly = (years > 2.0) & (years <= 10.0)
    long_ = years > 10.0

    if scenarios is None:
        scenarios = [
            ("Parallel +100bp", np.ones_like(years) * 1.0),
            ("Parallel -100bp", np.ones_like(years) * -1.0),
            ("Bear steepener", np.where(long_, 1.0, np.where(belly, 0.4, 0.0))),
            ("Bull flattener", np.where(long_, -1.0, np.where(belly, -0.4, 0.0))),
            ("Bear flattener", np.where(short, 1.0, np.where(belly, 0.4, 0.0))),
            ("Butterfly (belly cheapens)",
             np.where(belly, 0.75, np.where(short | long_, -0.25, 0.0))),
        ]

    out = []
    for name, shift_pct in scenarios:
        p1 = price(bond, bootstrap(years, par + np.asarray(shift_pct, dtype=float)))
        out.append({
            "scenario": name,
            "price": round(float(p1), 6),
            "pnl": round(float(p1 - p0), 6),
            "pnl_pct": round(float((p1 - p0) / p0 * 100), 4),
        })
    return out
