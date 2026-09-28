// Pure-C++ kernel timing at nanosecond resolution. No Python, no pybind11,
// no HTTP -- just the compiled maths.
//
// THE TRAP THIS AVOIDS. black_scholes is inline, pure, side-effect-free and
// built -O3. A loop with constant arguments and a discarded result is deleted
// entirely by dead-code elimination; you measure ~1 ns and believe it. Two
// defences, both required:
//   1. inputs VARY per iteration, so nothing can be hoisted out of the loop
//   2. the result is SUNK through an asm barrier, so nothing can be dropped
//
// And the clock itself: two steady_clock::now() calls cost ~20-25 ns each. At
// a ~100 ns target that is 20%+ of the measurement, so the BATCH is timed once
// and divided -- never an individual call.
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>

#include "quant/quant_engine.hpp"

using clk = std::chrono::steady_clock;
using namespace quant;

// Force a value to be materialised; prevents the optimiser removing the work.
template <class T>
static inline void sink(const T& v) { asm volatile("" : : "r,m"(v) : "memory"); }

struct Row { const char* name; double ns; std::uint64_t n; };

static void report(const Row& r) {
    std::printf("  %-26s %10.2f ns/call   %12.2f M/s   n=%llu\n",
                r.name, r.ns, 1e3 / r.ns, (unsigned long long)r.n);
}

int main() {
    const double K = 100, T = 0.5, r = 0.045, q = 0.01, sig = 0.22;

    std::printf("=== PURE C++ KERNEL LATENCY (no Python, no binding) ===\n");
    std::printf("    inputs varied per iteration, result sunk, batch-timed\n\n");

    // ---- Black-Scholes + full Greeks ------------------------------------
    {
        const std::uint64_t N = 20'000'000;
        // Vary S across a realistic strip so nothing is loop-invariant.
        double acc = 0.0;
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            const double S = 80.0 + double(i & 0xFFFF) * 1e-3;
            const Price p = black_scholes(S, K, T, r, q, sig, true);
            acc += p.price + p.greeks.delta;
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        report({"black_scholes + greeks", std::chrono::duration<double, std::nano>(t1 - t0).count() / N, N});
    }

    // ---- norm_cdf alone (the erfc call inside BSM) -----------------------
    {
        const std::uint64_t N = 50'000'000;
        double acc = 0.0;
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            acc += norm_cdf(-3.0 + double(i & 0xFFFF) * 1e-4);
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        report({"norm_cdf (erfc)", std::chrono::duration<double, std::nano>(t1 - t0).count() / N, N});
    }

    // ---- Monte Carlo, per PATH -------------------------------------------
    {
        const std::uint64_t P = 40'000'000;
        const auto t0 = clk::now();
        const MCResult m = monte_carlo(100, K, T, r, q, sig, P, true);
        const auto t1 = clk::now();
        sink(m.price);
        report({"monte_carlo per path", std::chrono::duration<double, std::nano>(t1 - t0).count() / P, P});
    }

    // ---- CRR binomial: per call AND per tree step ------------------------
    for (int steps : {64, 512}) {
        const std::uint64_t N = steps == 64 ? 200'000ULL : 20'000ULL;
        double acc = 0.0;
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            const double S = 80.0 + double(i & 0xFF) * 1e-2;
            acc += binomial(S, K, T, r, q, sig, steps, true, false);
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        const double ns = std::chrono::duration<double, std::nano>(t1 - t0).count() / N;
        char buf[64];
        std::snprintf(buf, sizeof buf, "binomial(%d)", steps);
        report({buf, ns, N});
        // A tree is O(steps^2) nodes; per-node cost is the portable number.
        std::printf("  %-26s %10.2f ns/node  (%d^2/2 nodes)\n", "  -> per tree node",
                    ns / (double(steps) * double(steps) / 2.0), steps);
    }

    std::printf("\n  NOTE: macOS cannot pin frequency or isolate cores. Absolute\n");
    std::printf("        figures drift with thermal state -- ratios are stable,\n");
    std::printf("        absolutes are not. Quote these only from a pinned Linux run.\n");
    return 0;
}
