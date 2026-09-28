// crypto_sim.cpp — standalone demo + cross-language parity check.
//
// Runs the same scenarios as python/verify_crypto.py and prints them, so you
// can eyeball that the compiled hot path matches the research library to the
// last decimal. No deps.
//
//   g++ -std=c++20 -O2 -Wall -Wextra -Icpp/include cpp/src/crypto_sim.cpp -o build/crypto_sim
//   ./build/crypto_sim
#include <cstdio>
#include <random>
#include <vector>

#include "xrpl/crypto_engine.hpp"

using namespace xrpl::crypto;

int main() {
    std::printf("=== PERPS ===\n");
    const double f = funding_rate(0.0008, 0.0001);
    std::printf("  funding(prem=0.08%%) = %.4f%%  (clamped)\n", f * 100.0);
    std::vector<double> rates(90, 0.0001);
    std::printf("  long funding PnL on $1M over 90 intervals = $%.0f\n",
                funding_pnl(1'000'000.0, rates));
    std::printf("  annualised carry = %.2f%%\n", annualised_funding(0.0001) * 100.0);

    std::printf("\n=== AMM ===\n");
    const SwapResult sr = swap_out(1'000'000.0, 500'000.0, 10'000.0, 0.003);
    std::printf("  swap 10k in: out=%.2f  impact=%.3f%%  fee=%.1f\n",
                sr.amount_out, sr.price_impact * 100.0, sr.fee_paid);
    std::printf("  IL @4x = %.2f%%   IL @0.25x = %.2f%%  (symmetric)\n",
                impermanent_loss(4.0) * 100.0, impermanent_loss(0.25) * 100.0);

    std::printf("\n=== LIQUIDATION (mmr=0.5%%) ===\n");
    std::printf("  10x long liq = %.3f   10x short liq = %.3f\n",
                liquidation_price(100.0, 10.0, true),
                liquidation_price(100.0, 10.0, false));
    const MarginState ms = margin_state(100.0, 95.0, 10.0, 10.0, true);
    std::printf("  mark 95: uPnL=%.1f equity=%.1f marginRatio=%.2f distLiq=%.1f%%\n",
                ms.unrealised_pnl, ms.equity, ms.margin_ratio,
                ms.distance_to_liq * 100.0);
    const double mx = max_leverage_for_liq_buffer(true, 0.20);
    std::printf("  max leverage for 20%% buffer = %.2fx  -> liq at %.2f\n",
                mx, liquidation_price(100.0, mx, true));

    std::printf("\n=== RISK (fat-tailed t(3), same seed idea as Python) ===\n");
    std::mt19937_64 gen(3);
    std::student_t_distribution<double> t(3.0);
    std::vector<double> ret;
    ret.reserve(3000);
    for (int i = 0; i < 3000; ++i) ret.push_back(t(gen) * 0.03);
    const VaRResult vr = historical_var(ret, 0.99);
    std::printf("  historical VaR99=%.2f%%  ES=%.2f%%\n", vr.var * 100.0, vr.es * 100.0);
    const double ev = ewma_vol(ret);
    std::printf("  EWMA vol = %.2f%%/day -> annualised %.0f%%\n",
                ev * 100.0, annualise_vol(ev) * 100.0);

    std::printf("\nC++ engine ran clean. Compare against python/verify_crypto.py.\n");
    return 0;
}
