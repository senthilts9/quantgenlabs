// lsm_bench.cpp — validate LSM against a binomial American tree + determinism.
//   g++ -std=c++20 -O3 -Wall -Wextra -pthread -Icpp/include cpp/src/lsm_bench.cpp -o build/lsm_bench
#include <cstdint>
#include <cstdio>

#include "quant/lsm.hpp"
#include "quant/quant_engine.hpp"

using namespace quant;

int main() {
    // Longstaff-Schwartz (2001) Table 1 setup: American PUT, K=40, r=0.06, T=1.
    const double K = 40, r = 0.06, q = 0.0, T = 1.0;

    std::printf("=== LSM AMERICAN PUT vs BINOMIAL (K=40, r=6%%, T=1) ===\n");
    std::printf("  %-6s %-10s %-10s %-10s %-10s\n",
                "S", "sig", "LSM", "Binomial", "Euro(BS)");
    for (double S : {36.0, 40.0, 44.0}) {
        for (double sig : {0.20, 0.40}) {
            const LSMResult l = longstaff_schwartz(S, K, T, r, q, sig,
                                                   200'000, 50, false, 7, 0, 3);
            const double bino = binomial(S, K, T, r, q, sig, 2000, false, true);
            const double euro = black_scholes(S, K, T, r, q, sig, false).price;
            std::printf("  %-6.0f %-10.2f %-10.4f %-10.4f %-10.4f\n",
                        S, sig, l.price, bino, euro);
        }
    }

    std::printf("\n=== EARLY-EXERCISE PREMIUM (LSM american - european) ===\n");
    const LSMResult l = longstaff_schwartz(36, K, T, r, q, 0.20, 200'000, 50, false);
    std::printf("  S=36 sig=0.20: american=%.4f european=%.4f premium=%.4f (>0 expected)\n",
                l.price, l.european, l.early_exercise_premium);

    std::printf("\n=== DETERMINISM: identical LSM price for any thread count ===\n");
    double ref = 0.0; bool ok = true;
    for (unsigned nt : {1u, 2u, 4u, 8u}) {
        const LSMResult r2 = longstaff_schwartz(36, K, T, r, q, 0.20,
                                                200'000, 50, false, 7, nt, 3);
        std::printf("  %2u threads -> %.12f\n", nt, r2.price);
        if (nt == 1) ref = r2.price; else if (r2.price != ref) ok = false;
    }
    std::printf("  bitwise-identical across thread counts: %s\n", ok ? "YES ✓" : "NO ✗");
    return 0;
}
