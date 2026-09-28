// crypto_ns_bench.cpp — per-tick crypto risk latency, pure C++.
//
// These are the calls a 24/7 crypto risk system makes on EVERY tick: where is
// liquidation, how much margin is left, what does this swap cost through the
// AMM curve, what is funding accruing. On a venue that never closes and can
// gap 5 sigma, the cost of asking is the cost of knowing.
//
// Same discipline as ns_bench.cpp: inputs vary per iteration so nothing can be
// hoisted, results are sunk through an asm barrier so dead-code elimination
// cannot delete the work and hand back a fake ~1 ns, and the BATCH is timed
// then divided -- two clock reads cost ~20-25 ns each, which would be a third
// of the measurement on calls this small.
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>

#include "xrpl/crypto_engine.hpp"

using clk = std::chrono::steady_clock;
using namespace xrpl::crypto;

template <class T>
static inline void sink(const T& v) { asm volatile("" : : "r,m"(v) : "memory"); }

static void row(const char* name, double ns, std::uint64_t n, const char* note) {
    std::printf("  %-30s %8.2f ns   %9.2f M/s   n=%-10llu %s\n",
                name, ns, 1e3 / ns, (unsigned long long)n, note);
}

int main() {
    std::printf("=== CRYPTO PER-TICK RISK LATENCY (pure C++, no Python) ===\n");
    std::printf("    inputs varied per iteration, results sunk, batch-timed\n\n");

    const std::uint64_t N = 20'000'000;

    // --- liquidation price: the number a desk alerts on ------------------
    {
        double acc = 0;
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            const double entry = 1.30 + double(i & 0xFFFF) * 1e-6;
            acc += liquidation_price(entry, 10.0, true, 0.005);
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        row("liquidation_price", std::chrono::duration<double, std::nano>(t1 - t0).count() / N,
            N, "long 10x, 50bp MMR");
    }

    // --- full margin state: equity, ratio, distance-to-liquidation --------
    {
        double acc = 0;
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            const double mark = 1.30 + double(i & 0xFFFF) * 1e-6;
            const MarginState m = margin_state(1.3050, mark, 40000.0, 10.0, true, 0.005);
            acc += m.margin_ratio + m.distance_to_liq;
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        row("margin_state (full)", std::chrono::duration<double, std::nano>(t1 - t0).count() / N,
            N, "equity + ratio + dist-to-liq");
    }

    // --- AMM swap through the constant-product curve ----------------------
    {
        double acc = 0;
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            const double in = 100.0 + double(i & 0xFFFF) * 1e-3;
            const SwapResult s = swap_out(50'000.0, 65'000.0, in, 30.0);
            acc += s.amount_out;
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        row("swap_out (AMM x*y=k)", std::chrono::duration<double, std::nano>(t1 - t0).count() / N,
            N, "slippage through the curve");
    }

    // --- impermanent loss: the LP's actual risk ---------------------------
    {
        double acc = 0;
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            acc += impermanent_loss(0.5 + double(i & 0xFFFF) * 1e-5);
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        row("impermanent_loss", std::chrono::duration<double, std::nano>(t1 - t0).count() / N,
            N, "closed form, no simulation");
    }

    // --- funding carry: dominant PnL term on a basis book -----------------
    {
        double acc = 0;
        std::vector<double> rates{1e-4, 1.1e-4, 0.9e-4};   // 3 intervals = 1 day
        const auto t0 = clk::now();
        for (std::uint64_t i = 0; i < N; ++i) {
            rates[0] = 1e-4 + double(i & 0xFFFF) * 1e-11;
            acc += funding_pnl(55'275.0, rates);
            sink(acc);
        }
        const auto t1 = clk::now();
        sink(acc);
        row("funding_pnl", std::chrono::duration<double, std::nano>(t1 - t0).count() / N,
            N, "3 intervals = one day");
    }

    std::printf("\n  A tick that updates liquidation distance, margin ratio, AMM\n");
    std::printf("  swap cost and funding carry costs the SUM of the above.\n\n");
    std::printf("  NOTE: absolutes drift with thermal and CPU state. Quote these\n");
    std::printf("        only from a pinned run; see deploy/bench-pinned.sh.\n");
    return 0;
}
