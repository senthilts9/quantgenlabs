"""Property definitions — the invariants every engine must satisfy.

Each function encodes ONE mathematical property as an assertion over raw
inputs. Both the pytest+Hypothesis suite and the dependency-free local
runner call these, so there is a single source of truth for correctness and
no drift between "what CI checks" and "what runs offline".
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

# make `app` importable no matter where the runner is launched from
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import pricing as P
from app.core import risk as R
from app.core.crypto import amm, liquidation, perps
from app.core.crypto import risk as crisk

TOL = 1e-9


def _opt(S, K, T, r, q, sig, call=True, exercise=P.Exercise.EUROPEAN):
    return P.Option(spot=S, strike=K, maturity=T, rate=r, dividend=q, vol=sig,
                    kind=P.OptionType.CALL if call else P.OptionType.PUT,
                    exercise=exercise)


# ---------------- pricing properties ----------------
def prop_put_call_parity(S, K, T, r, q, sig):
    """C - P == S·e^{-qT} - K·e^{-rT}  (to numerical tolerance)."""
    c = P.bsm(_opt(S, K, T, r, q, sig, True)).price
    p = P.bsm(_opt(S, K, T, r, q, sig, False)).price
    lhs = c - p
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert abs(lhs - rhs) < 1e-8, f"parity broke: {lhs} vs {rhs}"


def prop_price_nonneg(S, K, T, r, q, sig, call):
    assert P.bsm(_opt(S, K, T, r, q, sig, call)).price >= -TOL


def prop_call_monotonic_in_vol(S, K, T, r, q, sig):
    """A call is non-decreasing in volatility (vega ≥ 0)."""
    lo = P.bsm(_opt(S, K, T, r, q, sig, True)).price
    hi = P.bsm(_opt(S, K, T, r, q, sig + 0.05, True)).price
    assert hi >= lo - 1e-9, f"vega negative: {lo}->{hi}"


def prop_bsm_binomial_agree(S, K, T, r, q, sig):
    a = P.bsm(_opt(S, K, T, r, q, sig, True)).price
    b = P.crr(_opt(S, K, T, r, q, sig, True), 600).price
    # A fixed 0.05 absolute tolerance breaks at the extreme corner of this
    # property's own input range (deep ITM/OTM x vol up to 120% x T up to 3y,
    # e.g. S=500,K=500,T=3,sig=1.2 prices at ~350 with a genuine ~0.1-0.15
    # discretization gap at N=600) -- that's CRR's well-known odd/even
    # "sawtooth" convergence, not a pricing bug in either bsm() or crr(): the
    # binomial price visibly converges toward BSM as N grows (verified by
    # hand up to N=10000). A tolerance that scales with price level is the
    # correct fix, not cranking up N (which just moves the corner, and the
    # ATM/high-vol/long-T corner above still isn't robust even at N=1500).
    tol = max(0.05, 0.001 * abs(a))
    assert abs(a - b) < tol, f"BSM vs binomial: {a} vs {b} (tol={tol})"


def prop_american_ge_european(S, K, T, r, q, sig, call):
    e = P.crr(_opt(S, K, T, r, q, sig, call, P.Exercise.EUROPEAN), 400).price
    am = P.crr(_opt(S, K, T, r, q, sig, call, P.Exercise.AMERICAN), 400).price
    assert am >= e - 1e-6, f"American {am} < European {e}"


def prop_delta_bounds(S, K, T, r, q, sig, call):
    d = P.bsm(_opt(S, K, T, r, q, sig, call)).greeks.delta
    assert (-1.0 - 1e-6) <= d <= (1.0 + 1e-6), f"delta out of range: {d}"


# ---------------- risk properties ----------------
def prop_var_le_es(returns, confidence):
    for fn in (R.historical, R.parametric):
        res = fn(np.asarray(returns, float), confidence)
        assert res.es >= res.var - 1e-9, f"{res.method}: ES {res.es} < VaR {res.var}"


def prop_component_var_sums(returns_matrix, weights, confidence):
    cv = R.component_var(np.asarray(returns_matrix, float),
                         np.asarray(weights, float), confidence)
    assert abs(sum(cv["component_var"]) - cv["portfolio_var"]) < 1e-9


def prop_higher_confidence_higher_var(returns):
    lo = R.historical(np.asarray(returns, float), 0.95).var
    hi = R.historical(np.asarray(returns, float), 0.99).var
    assert hi >= lo - 1e-9, f"VaR99 {hi} < VaR95 {lo}"


# ---------------- crypto properties ----------------
def prop_il_nonpositive_and_symmetric(ratio):
    il = amm.impermanent_loss(ratio)
    assert il <= 1e-12, f"IL positive: {il}"
    assert abs(il - amm.impermanent_loss(1.0 / ratio)) < 1e-9, "IL not symmetric in 1/r"


def prop_il_zero_at_one():
    assert abs(amm.impermanent_loss(1.0)) < 1e-12


def prop_liq_ordering(entry, lev):
    lo = liquidation.liquidation_price(entry, lev, is_long=True)
    hi = liquidation.liquidation_price(entry, lev, is_long=False)
    assert lo < entry < hi, f"liq ordering: {lo} < {entry} < {hi}"


def prop_funding_clamped(premium, interest, clamp=0.0005):
    f = perps.funding_rate(premium, interest, clamp)
    # funding = premium + clamp(interest-premium); bounded within premium±clamp
    assert premium - clamp - 1e-12 <= f <= premium + clamp + 1e-12


def prop_evt_captures_tail(returns):
    """On a fat-tailed sample EVT ES should not understate the empirical ES."""
    h = crisk.historical_var(np.asarray(returns, float), 0.99)
    e = crisk.evt_var(np.asarray(returns, float), 0.99)
    assert e.es >= h.es * 0.85, f"EVT ES {e.es} << historical {h.es}"


# ---------------- backtest properties ----------------
def prop_basis_is_delta_neutral():
    """Price P&L should be tiny vs funding — the trade is delta-neutral."""
    from app.core.backtest import BasisConfig, run_basis_backtest, simulate
    res = run_basis_backtest(simulate(days=180, seed=1), BasisConfig())
    a = res.attribution
    gross = abs(a["funding"]) + abs(a["fees"]) + abs(a["financing"]) + 1.0
    assert abs(a["price"]) / gross < 0.15, f"not delta-neutral: price={a['price']}"
    assert res.equity.iloc[-1] > 0, "equity went non-positive"
