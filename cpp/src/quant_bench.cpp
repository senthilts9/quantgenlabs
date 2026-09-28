// quant_bench.cpp — correctness cross-check + Monte Carlo benchmark.
//
//   g++ -std=c++20 -O3 -Wall -Wextra -Icpp/include cpp/src/quant_bench.cpp -o build/quant_bench
//   ./build/quant_bench
#include <chrono>
#include <cstdint>
#include <cstdio>

#include "quant/quant_engine.hpp"

using namespace quant;
using clk = std::chrono::steady_clock;

int main() {
    const double S = 100, K = 100, T = 0.5, r = 0.045, q = 0.01, sig = 0.22;

    std::printf("=== PRICING CROSS-CHECK (ATM call) ===\n");
    const Price bs = black_scholes(S, K, T, r, q, sig, true);
    const double bn = binomial(S, K, T, r, q, sig, 512, true, false);
    const MCResult mc = monte_carlo(S, K, T, r, q, sig, 2'000'000, true);
    std::printf("  Black-Scholes = %.5f\n", bs.price);
    std::printf("  Binomial(512) = %.5f  (diff %.5f)\n", bn, std::fabs(bn - bs.price));
    std::printf("  Monte Carlo   = %.5f  CI[%.4f, %.4f]\n", mc.price, mc.ci_lo, mc.ci_hi);
    std::printf("  Greeks: delta=%.4f gamma=%.4f vega=%.4f theta=%.4f rho=%.4f\n",
                bs.greeks.delta, bs.greeks.gamma, bs.greeks.vega,
                bs.greeks.theta, bs.greeks.rho);

    std::printf("\n=== MONTE CARLO BENCHMARK (single thread, -O3) ===\n");
    for (std::uint64_t paths : {1'000'000ULL, 10'000'000ULL, 50'000'000ULL}) {
        const auto t0 = clk::now();
        const MCResult m = monte_carlo(S, K, T, r, q, sig, paths, true);
        const auto t1 = clk::now();
        const double ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
        const double per_sec = m.paths / (ms / 1000.0);
        std::printf("  %10llu paths  price=%.4f  %8.2f ms  %6.1fM paths/sec\n",
                    static_cast<unsigned long long>(m.paths), m.price, ms,
                    per_sec / 1e6);
    }

    std::printf("\n=== VaR BENCHMARK (historical, 1e6-point window) ===\n");
    std::vector<double> rets;
    rets.reserve(1'000'000);
    std::mt19937_64 g(1);
    std::student_t_distribution<double> t(5.0);
    for (int i = 0; i < 1'000'000; ++i) rets.push_back(t(g) * 0.01);
    const auto v0 = clk::now();
    const VaR v = historical_var(rets, 0.99);
    const auto v1 = clk::now();
    std::printf("  VaR99=%.3f%% ES=%.3f%%  in %.2f ms\n", v.var * 100, v.es * 100,
                std::chrono::duration<double, std::milli>(v1 - v0).count());
    return 0;
}
