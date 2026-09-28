// parallel_bench.cpp — determinism proof + scaling benchmark.
//
//   g++ -std=c++20 -O3 -Wall -Wextra -pthread -Icpp/include cpp/src/parallel_bench.cpp -o build/parallel_bench
//   ./build/parallel_bench
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <vector>

#include "quant/quant_parallel.hpp"

using namespace quant;
using clk = std::chrono::steady_clock;
static double ms_since(clk::time_point t) {
    return std::chrono::duration<double, std::milli>(clk::now() - t).count();
}

int main() {
    const double S = 100, K = 100, T = 0.5, r = 0.045, q = 0.01, sig = 0.22;

    std::printf("=== DETERMINISM: identical price for any thread count ===\n");
    double ref = 0.0;
    bool ok = true;
    for (unsigned nt : {1u, 2u, 4u, 8u, 16u}) {
        const MCResult m = monte_carlo_parallel(S, K, T, r, q, sig,
                                                20'000'000, true, 7, nt);
        std::printf("  %2u threads -> price = %.12f  (paths=%llu)\n", nt, m.price,
                    static_cast<unsigned long long>(m.paths));
        if (nt == 1) ref = m.price;
        else if (std::fabs(m.price - ref) > 0.0) ok = false;
    }
    std::printf("  bitwise-identical across thread counts: %s\n",
                ok ? "YES ✓" : "NO ✗");

    std::printf("\n=== MONTE CARLO SCALING (50M paths) ===\n");
    for (unsigned nt : {1u, 2u, 4u, 8u}) {
        const auto t0 = clk::now();
        const MCResult m = monte_carlo_parallel(S, K, T, r, q, sig,
                                                50'000'000, true, 7, nt);
        const double e = ms_since(t0);
        std::printf("  %2u thr  %8.1f ms  %7.1fM paths/s  price=%.4f\n", nt, e,
                    m.paths / (e / 1000.0) / 1e6, m.price);
    }

    std::printf("\n=== BOOK PRICING (50,000 options) ===\n");
    std::vector<OptionSpec> book;
    book.reserve(50'000);
    for (int i = 0; i < 50'000; ++i)
        book.push_back({S, 80.0 + (i % 40), T, r, q, 0.15 + (i % 20) * 0.01,
                        (i % 2) == 0, false});
    for (unsigned nt : {1u, 4u}) {
        const auto t0 = clk::now();
        const auto px = price_book(book, Model::Binomial, 256, 100000, nt);
        std::printf("  %2u thr  binomial x50k  %8.1f ms  (sample px[0]=%.4f)\n",
                    nt, ms_since(t0), px[0]);
    }

    std::printf("\n=== SCENARIO VaR — full revaluation (500 opts x 20,000 scenarios) ===\n");
    std::vector<OptionSpec> pf(book.begin(), book.begin() + 500);
    std::vector<Scenario> scen;
    scen.reserve(20'000);
    std::mt19937_64 g(11);
    std::normal_distribution<double> N(0, 1);
    for (int i = 0; i < 20'000; ++i)
        scen.push_back({0.03 * N(g), 0.02 * N(g), 0.0005 * N(g)});
    for (unsigned nt : {1u, 4u}) {
        const auto t0 = clk::now();
        const ScenarioVaR v = scenario_var(pf, scen, 0.99, nt);
        std::printf("  %2u thr  %8.1f ms  VaR99=%.2f ES=%.2f base=%.1f  (%llu revals)\n",
                    nt, ms_since(t0), v.var, v.es, v.base_value,
                    static_cast<unsigned long long>(v.scenarios) * pf.size());
    }

    // This note used to assert "sandbox has 1 physical core, so wall-clock won't
    // drop with more threads" and to project "~6-8x on an 8-core box". Both were
    // wrong to leave in: the first is false on any multi-core host (and the timings
    // printed above visibly contradict it), and the second is an extrapolation
    // presented next to real measurements, which is exactly how a projection gets
    // read as a result. Neither claim is made any more -- the numbers above are
    // whatever this host actually did, and cpp/src/mkt_bench.cpp is the harness that
    // records the environment needed to interpret them.
    std::printf("\nNote: timings above are from THIS host and include SMT effects if the\n"
                "thread count exceeds its physical core count -- run mkt_bench for the\n"
                "environment capture (physical vs logical cores, compiler, SIMD level).\n"
                "The determinism proof is independent of all that: identical price at\n"
                "every thread count, which is the property a risk desk signs off on.\n");
    return 0;
}
