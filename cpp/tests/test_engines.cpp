// test_engines.cpp — invariants for the C++ engines.
// Build: g++ -std=c++20 -O2 -Wall -Wextra -pthread -Icpp/include -Icpp/tests cpp/tests/test_engines.cpp -o build/tests && ./build/tests
#include "microtest.hpp"
#include "quant/quant_engine.hpp"
#include "quant/quant_parallel.hpp"
#include "quant/lsm.hpp"
#include "xrpl/crypto_engine.hpp"

using namespace quant;

TEST(put_call_parity) {
    const double S = 100, K = 95, T = 0.75, r = 0.04, q = 0.01, sig = 0.25;
    const double c = black_scholes(S, K, T, r, q, sig, true).price;
    const double p = black_scholes(S, K, T, r, q, sig, false).price;
    CHECK_CLOSE(c - p, S * std::exp(-q * T) - K * std::exp(-r * T), 1e-9);
}

TEST(bsm_binomial_mc_agree) {
    const double S = 100, K = 100, T = 0.5, r = 0.045, q = 0.01, sig = 0.22;
    const double bs = black_scholes(S, K, T, r, q, sig, true).price;
    const double bn = binomial(S, K, T, r, q, sig, 800, true, false);
    const MCResult mc = monte_carlo(S, K, T, r, q, sig, 2'000'000, true);
    CHECK_CLOSE(bs, bn, 0.02);
    REQUIRE(mc.ci_lo <= bs && bs <= mc.ci_hi);   // BSM inside MC 95% CI
}

TEST(american_ge_european_put) {
    const double S = 100, K = 110, T = 1.0, r = 0.06, q = 0.0, sig = 0.3;
    const double eu = binomial(S, K, T, r, q, sig, 500, false, false);
    const double am = binomial(S, K, T, r, q, sig, 500, false, true);
    REQUIRE(am >= eu - 1e-9);
}

TEST(lsm_matches_binomial_american) {
    const double K = 40, r = 0.06, q = 0.0, T = 1.0;
    for (double S : {36.0, 40.0, 44.0}) {
        const double lsm = longstaff_schwartz(S, K, T, r, q, 0.20, 120'000, 50, false).price;
        const double tree = binomial(S, K, T, r, q, 0.20, 1500, false, true);
        CHECK_CLOSE(lsm, tree, 0.05);            // within MC error of the tree
    }
}

TEST(parallel_mc_deterministic) {
    double ref = monte_carlo_parallel(100, 100, 0.5, 0.045, 0.01, 0.22,
                                      10'000'000, true, 7, 1).price;
    for (unsigned nt : {2u, 4u, 8u}) {
        const double x = monte_carlo_parallel(100, 100, 0.5, 0.045, 0.01, 0.22,
                                              10'000'000, true, 7, nt).price;
        REQUIRE(x == ref);                        // bitwise identical
    }
}

TEST(parallel_lsm_deterministic) {
    double ref = longstaff_schwartz(36, 40, 1.0, 0.06, 0.0, 0.20, 120'000, 50, false, 7, 1).price;
    for (unsigned nt : {2u, 4u, 8u}) {
        const double x = longstaff_schwartz(36, 40, 1.0, 0.06, 0.0, 0.20,
                                            120'000, 50, false, 7, nt).price;
        REQUIRE(x == ref);
    }
}

TEST(crypto_il_symmetric_and_nonpositive) {
    using namespace xrpl::crypto;
    CHECK_CLOSE(impermanent_loss(4.0), impermanent_loss(0.25), 1e-9);
    REQUIRE(impermanent_loss(4.0) <= 0.0);
    CHECK_CLOSE(impermanent_loss(1.0), 0.0, 1e-12);
}

TEST(crypto_liquidation_ordering) {
    using namespace xrpl::crypto;
    const double lo = liquidation_price(100, 10, true);
    const double hi = liquidation_price(100, 10, false);
    REQUIRE(lo < 100.0 && 100.0 < hi);
}

TEST(crypto_max_leverage_buffer_inverts) {
    using namespace xrpl::crypto;
    const double lev = max_leverage_for_liq_buffer(true, 0.20);
    const double liq = liquidation_price(100, lev, true);
    CHECK_CLOSE(liq, 80.0, 1e-6);                 // 20% buffer ⇒ liq at 80
}

int main() {
    std::printf("=== C++ engine tests ===\n");
    return microtest::run_all();
}
