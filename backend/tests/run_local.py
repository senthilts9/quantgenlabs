"""Dependency-free property runner (no pytest / hypothesis needed).

Runs each shared property over a random grid so the invariants are verified
anywhere Python + numpy exist. CI uses the full Hypothesis suite in
test_engines.py; this is the offline smoke test.

    python backend/tests/run_local.py
"""
from __future__ import annotations

import random
import traceback

import numpy as np

import properties as pr

rng = random.Random(42)
nrng = np.random.default_rng(42)
N = 300
passed = failed = 0


def run(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✓ {name}")
        passed += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {name}: {e}")
        traceback.print_exc()
        failed += 1


def rfloat(a, b):
    return a + (b - a) * rng.random()


def sample_pricing():
    for _ in range(N):
        S, K = rfloat(5, 500), rfloat(5, 500)
        T, r, q, sig = rfloat(0.05, 3), rfloat(0, 0.1), rfloat(0, 0.05), rfloat(0.05, 1.2)
        pr.prop_put_call_parity(S, K, T, r, q, sig)
        pr.prop_price_nonneg(S, K, T, r, q, sig, rng.random() > 0.5)
        pr.prop_call_monotonic_in_vol(S, K, T, r, q, sig)
        pr.prop_delta_bounds(S, K, T, r, q, sig, rng.random() > 0.5)


def sample_trees():
    for _ in range(60):
        S, K = rfloat(20, 200), rfloat(20, 200)
        T, r, q, sig = rfloat(0.1, 2), rfloat(0, 0.08), 0.0, rfloat(0.1, 0.8)
        pr.prop_bsm_binomial_agree(S, K, T, r, q, sig)
        pr.prop_american_ge_european(S, K, T, r, q, sig, rng.random() > 0.5)


def sample_risk():
    for _ in range(50):
        rets = nrng.standard_t(5, 500) * 0.01
        pr.prop_var_le_es(rets, rfloat(0.9, 0.99))
        pr.prop_higher_confidence_higher_var(rets)
    Rm = nrng.standard_normal((800, 4)) * 0.01
    pr.prop_component_var_sums(Rm, np.array([0.4, 0.3, 0.2, 0.1]), 0.99)


def sample_crypto():
    for _ in range(N):
        pr.prop_il_nonpositive_and_symmetric(rfloat(0.05, 20))
        pr.prop_liq_ordering(rfloat(1, 1000), rfloat(1.5, 100))
        pr.prop_funding_clamped(rfloat(-0.01, 0.01), rfloat(-0.001, 0.001))
    pr.prop_il_zero_at_one()
    pr.prop_evt_captures_tail(nrng.standard_t(3, 3000) * 0.03)


def sample_backtest():
    pr.prop_basis_is_delta_neutral()


if __name__ == "__main__":
    print("pricing properties:");   run("pricing grid", sample_pricing)
    print("tree/american properties:"); run("tree grid", sample_trees)
    print("risk properties:");     run("risk grid", sample_risk)
    print("crypto properties:");    run("crypto grid", sample_crypto)
    print("backtest properties:");  run("basis delta-neutral", sample_backtest)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
