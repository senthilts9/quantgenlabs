import sys, math, numpy as np
sys.path.insert(0, "backend")
from app.core import pricing as P
from app.core import risk as R

print("=== PRICING CROSS-CHECK (ATM call, S=K=100, T=0.5, r=4.5%, q=1%, vol=22%) ===")
opt = P.Option(spot=100, strike=100, maturity=0.5, rate=0.045, dividend=0.01,
               vol=0.22, kind=P.OptionType.CALL)
bs = P.bsm(opt); bn = P.crr(opt, 512); mc = P.mc_price(opt, 200_000)
print(f"  Black-Scholes : {bs.price:.5f}")
print(f"  Binomial(512) : {bn.price:.5f}   diff={abs(bn.price-bs.price):.5f}")
print(f"  Monte Carlo   : {mc.price:.5f}   CI={mc.ci95[0]:.4f}..{mc.ci95[1]:.4f}  se={mc.stderr:.5f}")
assert abs(bn.price-bs.price) < 0.02, "binomial disagrees"
assert mc.ci95[0] <= bs.price <= mc.ci95[1], "BSM outside MC 95% CI"
print("  Greeks:", {k: round(getattr(bs.greeks,k),4) for k in ('delta','gamma','vega','theta','rho')})

print("\n=== PUT-CALL PARITY  (C - P == S e^-qT - K e^-rT) ===")
put = P.Option(spot=100, strike=100, maturity=0.5, rate=0.045, dividend=0.01,
               vol=0.22, kind=P.OptionType.PUT)
lhs = bs.price - P.bsm(put).price
rhs = 100*math.exp(-0.01*0.5) - 100*math.exp(-0.045*0.5)
print(f"  C-P={lhs:.6f}  parity={rhs:.6f}  diff={abs(lhs-rhs):.2e}")
assert abs(lhs-rhs) < 1e-8, "parity violated"

print("\n=== AMERICAN >= EUROPEAN PUT (early exercise premium) ===")
ae = P.Option(spot=100, strike=110, maturity=1.0, rate=0.06, dividend=0.0,
              vol=0.3, kind=P.OptionType.PUT, exercise=P.Exercise.AMERICAN)
ee = P.Option(spot=100, strike=110, maturity=1.0, rate=0.06, dividend=0.0,
              vol=0.3, kind=P.OptionType.PUT, exercise=P.Exercise.EUROPEAN)
pa, pe = P.crr(ae,512).price, P.crr(ee,512).price
print(f"  American put={pa:.4f}  European put={pe:.4f}  premium={pa-pe:.4f}")
assert pa >= pe - 1e-6, "American < European"

print("\n=== VaR / ES (sim: t-dist fat tails, 2000 days) ===")
rng = np.random.default_rng(1)
rets = rng.standard_t(5, 2000) * 0.01
for name, fn in R.VAR_METHODS.items():
    v = fn(rets, 0.99)
    print(f"  {name:14s} VaR99={v.var*100:.3f}%  ES={v.es*100:.3f}%")

print("\n=== VaR BACKTEST (well-calibrated forecast should be GREEN) ===")
sigma = rets.std()
vf = np.full_like(rets, 2.326*sigma)     # 99% normal VaR forecast
bt = R.backtest_var(rets, vf, 0.99)
print(f"  obs={bt.observations} exceptions={bt.exceptions} expected={bt.expected:.1f}")
print(f"  Kupiec p={bt.kupiec_p:.3f}  Christoffersen-CC p={bt.christoffersen_cc_p:.3f}  zone={bt.basel_zone}")

print("\n=== COMPONENT VaR (sums to portfolio VaR) ===")
Rm = rng.standard_normal((1000,3))*0.01
w = np.array([0.5,0.3,0.2])
cv = R.component_var(Rm, w, 0.99)
print(f"  components={[round(x,5) for x in cv['component_var']]}")
print(f"  sum={sum(cv['component_var']):.6f}  portfolio_var={cv['portfolio_var']:.6f}")
assert abs(sum(cv['component_var'])-cv['portfolio_var'])<1e-9, "components don't sum"
print("\nALL CHECKS PASSED ✓")
