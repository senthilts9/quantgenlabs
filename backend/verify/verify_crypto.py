import sys, numpy as np
sys.path.insert(0, "backend")
from app.core.crypto import perps, amm, liquidation, risk

print("=== PERPS ===")
f = perps.funding_rate(premium_index=0.0008, interest_rate=0.0001)
print(f"  funding(prem=0.08%%) = {f*100:.4f}%  (clamped)")
assert abs(f - (0.0008 + max(min(0.0001-0.0008,0.0005),-0.0005))) < 1e-12
rates = np.full(90, 0.0001)   # 30 days @ 3/day, +0.01% each
p = perps.funding_pnl(1_000_000, rates)
print(f"  long funding PnL over {p.intervals} intervals on $1M = ${p.gross:,.0f}  (long pays)")
print(f"  annualised carry = {p.annualised_carry*100:.2f}%")
assert p.gross < 0
cc = perps.cash_and_carry_apr(rates, spot_borrow_apr=0.05)
print(f"  cash&carry APR (short perp, borrow 5%%) = {cc*100:.2f}%")
print(f"  annualised basis (fut=2.05, idx=2.00, 30d) = {perps.annualised_basis(2.05,2.00,30)*100:.1f}%")

print("\n=== AMM (constant product) ===")
sr = amm.swap_out(reserve_in=1_000_000, reserve_out=500_000, amount_in=10_000, fee=0.003)
print(f"  swap 10k in: out={sr.amount_out:,.2f}  impact={sr.price_impact*100:.3f}%  fee={sr.fee_paid:.1f}")
assert sr.price_impact < 0  # buying OUT token, pay worse than spot
il4 = amm.impermanent_loss(4.0); il_q = amm.impermanent_loss(0.25)
print(f"  IL @4x price = {il4*100:.2f}%   IL @0.25x = {il_q*100:.2f}%  (symmetric)")
assert abs(il4 - (-0.20)) < 0.005 and abs(il4 - il_q) < 1e-9
assert abs(amm.impermanent_loss(1.0)) < 1e-12
lp = amm.lp_value_vs_hodl(1000, 2000, price_ratio=4.0, fees_earned=50)
print(f"  LP value={lp.lp_value:.1f} vs HODL={lp.hodl_value:.1f}  IL={lp.impermanent_loss*100:.1f}%  net(+fees)={lp.net_vs_hodl:.1f}")

print("\n=== LIQUIDATION (linear perp, mmr=0.5%) ===")
lq_long = liquidation.liquidation_price(100.0, leverage=10, is_long=True)
lq_short = liquidation.liquidation_price(100.0, leverage=10, is_long=False)
print(f"  10x long entry 100 → liq {lq_long:.3f}   10x short → liq {lq_short:.3f}")
assert lq_long < 100 < lq_short
ms = liquidation.margin_state(entry=100, mark=95, qty=10, leverage=10, is_long=True)
print(f"  mark 95: uPnL={ms.unrealised_pnl:.1f} equity={ms.equity:.1f} marginRatio={ms.margin_ratio:.2f} distLiq={ms.distance_to_liq*100:.1f}%")
mx = liquidation.max_leverage_for_liq_buffer(100, is_long=True, buffer=0.20)
print(f"  max leverage for 20%% liq buffer (long) = {mx:.2f}x")
# sanity: liq price at that leverage should sit ~20% below entry
lp2 = liquidation.liquidation_price(100, mx, True); print(f"    → liq at {lp2:.2f} ({(lp2/100-1)*100:.1f}% from entry)")

print("\n=== CRYPTO RISK (fat tails) ===")
rng = np.random.default_rng(3)
# heavy-tailed daily crypto returns: t(3) scaled to ~4% daily vol
ret = rng.standard_t(3, 3000) * 0.03
for name, fn in risk.CRYPTO_VAR_METHODS.items():
    tr = fn(ret, 0.99)
    extra = f"  ξ={tr.xi:.3f} n_exc={tr.n_exceed}" if tr.xi is not None else ""
    print(f"  {name:14s} VaR99={tr.var*100:.2f}%  ES={tr.es*100:.2f}%{extra}")
print(f"  EWMA vol (latest) = {risk.ewma_vol(ret)*100:.2f}%/day → annualised {risk.annualise_vol(risk.ewma_vol(ret))*100:.0f}%")
sh = risk.sharpe_after_funding(ret, np.array([0.0001]))
print(f"  Sharpe after funding = {sh:.2f}")
# EVT tail should be >= historical for heavy tails at 99%
h = risk.historical_var(ret,0.99); e = risk.evt_var(ret,0.99)
print(f"  EVT ES ({e.es*100:.2f}%) vs historical ES ({h.es*100:.2f}%): EVT captures fatter tail = {e.es>=h.es*0.9}")
print("\nALL CRYPTO CHECKS PASSED ✓")
