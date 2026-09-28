"""Pytest + Hypothesis suite. Runs in CI:  pytest backend/tests -q

Each property from properties.py is exercised over a wide input space by
Hypothesis, which will shrink any counterexample to a minimal failing case.
"""
from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

import properties as pr

# reasonable market-input ranges
spot = st.floats(5, 500)
strike = st.floats(5, 500)
mat = st.floats(0.05, 3.0)
rate = st.floats(0.0, 0.10)
div = st.floats(0.0, 0.05)
vol = st.floats(0.05, 1.2)
conf = st.floats(0.90, 0.995)


# ---- pricing ----
@given(spot, strike, mat, rate, div, vol)
@settings(max_examples=200, deadline=None)
def test_put_call_parity(S, K, T, r, q, sig):
    pr.prop_put_call_parity(S, K, T, r, q, sig)


@given(spot, strike, mat, rate, div, vol, st.booleans())
@settings(max_examples=200, deadline=None)
def test_price_nonneg(S, K, T, r, q, sig, call):
    pr.prop_price_nonneg(S, K, T, r, q, sig, call)


@given(spot, strike, mat, rate, div, vol)
@settings(max_examples=150, deadline=None)
def test_call_monotonic_in_vol(S, K, T, r, q, sig):
    pr.prop_call_monotonic_in_vol(S, K, T, r, q, sig)


@given(spot, strike, mat, rate, div, vol)
@settings(max_examples=100, deadline=None)
def test_bsm_binomial_agree(S, K, T, r, q, sig):
    pr.prop_bsm_binomial_agree(S, K, T, r, q, sig)


@given(spot, strike, mat, rate, div, vol, st.booleans())
@settings(max_examples=100, deadline=None)
def test_american_ge_european(S, K, T, r, q, sig, call):
    pr.prop_american_ge_european(S, K, T, r, q, sig, call)


@given(spot, strike, mat, rate, div, vol, st.booleans())
@settings(max_examples=150, deadline=None)
def test_delta_bounds(S, K, T, r, q, sig, call):
    pr.prop_delta_bounds(S, K, T, r, q, sig, call)


# ---- risk ----
returns = st.lists(st.floats(-0.3, 0.3), min_size=100, max_size=1000)


@given(returns, conf)
@settings(max_examples=100, deadline=None)
def test_var_le_es(rets, c):
    pr.prop_var_le_es(rets, c)


@given(returns)
@settings(max_examples=100, deadline=None)
def test_higher_confidence_higher_var(rets):
    pr.prop_higher_confidence_higher_var(rets)


def test_component_var_sums():
    rng = np.random.default_rng(0)
    Rm = rng.standard_normal((800, 4)) * 0.01
    w = np.array([0.4, 0.3, 0.2, 0.1])
    pr.prop_component_var_sums(Rm, w, 0.99)


# ---- crypto ----
@given(st.floats(0.05, 20.0))
@settings(max_examples=200, deadline=None)
def test_il_nonpositive_symmetric(ratio):
    pr.prop_il_nonpositive_and_symmetric(ratio)


def test_il_zero_at_one():
    pr.prop_il_zero_at_one()


@given(st.floats(1.0, 1000.0), st.floats(1.5, 100.0))
@settings(max_examples=200, deadline=None)
def test_liq_ordering(entry, lev):
    pr.prop_liq_ordering(entry, lev)


@given(st.floats(-0.01, 0.01), st.floats(-0.001, 0.001))
@settings(max_examples=200, deadline=None)
def test_funding_clamped(premium, interest):
    pr.prop_funding_clamped(premium, interest)


def test_evt_captures_tail():
    rng = np.random.default_rng(3)
    rets = rng.standard_t(3, 3000) * 0.03
    pr.prop_evt_captures_tail(rets)
