import sys; sys.path.insert(0, "backend")
from app.core import sor

print("=== SMART ORDER ROUTING ===")
venues, mid = sor.synth_market(sor.Side.BUY, mid=100.0, seed=7)
order = 2500.0
r = sor.route(order, venues, sor.Side.BUY, mid)
s = r.summary()
print(f"  SOR slippage={s['slippage_bps']:.2f}bps  vs best-single={s['benchmarks_bps']['best_single_venue']:.2f}bps"
      f"  vs pro-rata={s['benchmarks_bps']['pro_rata']:.1f}bps")
print(f"  improvement vs best single venue: {s['improvement_vs_best_single_bps']:.2f} bps")
assert s['slippage_bps'] <= s['benchmarks_bps']['best_single_venue'] + 1e-6
assert s['slippage_bps'] <= s['benchmarks_bps']['pro_rata'] + 1e-6

m = sor.train_fill_model()
print("  ML fill model:", sor.training_metrics(m))
fp = sor.fill_probabilities(m, venues, order)
r2 = sor.route(order, venues, sor.Side.BUY, mid, fill_prob=fp)
print("  SOR+ML explanation:", sor.explain.explain(r2, fp)["explanation"][:160], "...")
print("\nSOR CHECKS PASSED ✓")
