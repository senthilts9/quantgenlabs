// quant_parallel.hpp — parallelism applied where it actually pays off.
//
//   * monte_carlo_parallel  — parallel over PATHS (deterministic per thread count)
//   * price_book            — parallel over INSTRUMENTS (any pricer: BSM/binomial/MC)
//   * scenario_var          — parallel over SCENARIOS (full-revaluation VaR/ES)
//
// A single BSM call or one binomial tree stays serial (threading them only
// adds overhead). The batch dimension is what we parallelise.
#pragma once

#include <algorithm>
#include <cstdint>
#include <random>
#include <vector>

#include "quant/parallel.hpp"
#include "quant/quant_engine.hpp"

namespace quant {

// ------------------- parallel Monte Carlo (over paths) ------------------
// Deterministic: block b is seeded by splitmix64(seed + b) and partials are
// summed in block order, so the price is identical for any n_threads.
inline MCResult monte_carlo_parallel(double S, double K, double T, double r,
                                     double q, double sig, std::uint64_t paths,
                                     bool call, std::uint64_t seed = 7,
                                     unsigned n_threads = 0,
                                     std::uint64_t block_pairs = 32768) {
    const double drift = (r - q - 0.5 * sig * sig) * T;
    const double diff = sig * std::sqrt(T);
    const double df = std::exp(-r * T);
    const std::uint64_t pairs = paths / 2;

    struct Acc { double sum = 0.0, sum2 = 0.0; std::uint64_t n = 0; };

    // Block boundaries come from block_pairs, never from n_threads, so the
    // vectorised inner loop cannot disturb the cross-thread determinism this
    // function exists to provide: block b always sees the same pair range and
    // the same RNG stream regardless of how many threads run it.
    auto block_fn = [&](std::uint64_t b, std::uint64_t lo, std::uint64_t hi) -> Acc {
        std::mt19937_64 gen(par::splitmix64(seed + b));
        Acc a;
        a.n = mc_accumulate(gen, hi - lo, S, K, drift, diff, df, call,
                            a.sum, a.sum2);
        return a;
    };
    auto combine = [](const Acc& x, const Acc& y) {
        return Acc{x.sum + y.sum, x.sum2 + y.sum2, x.n + y.n};
    };

    const Acc a = par::block_reduce<Acc>(pairs, block_pairs, block_fn, combine,
                                         Acc{}, n_threads);
    const double mean = a.sum / a.n;
    const double var = std::max(a.sum2 / a.n - mean * mean, 0.0) / a.n;
    const double se = std::sqrt(var);
    return MCResult{mean, se, mean - 1.96 * se, mean + 1.96 * se, a.n};
}

// ---------------------- parallel book pricing ---------------------------
enum class Model { BlackScholes, Binomial, MonteCarlo };

struct OptionSpec {
    double S, K, T, r, q, sig;
    bool call = true;
    bool american = false;
};

// Price a whole book in parallel over instruments. Results indexed by
// position ⇒ order-independent ⇒ trivially reproducible.
inline std::vector<double> price_book(const std::vector<OptionSpec>& book,
                                      Model model, int steps = 256,
                                      std::uint64_t mc_paths = 100000,
                                      unsigned n_threads = 0) {
    std::vector<double> out(book.size(), 0.0);
    par::parallel_for(book.size(), [&](std::size_t i) {
        const OptionSpec& o = book[i];
        switch (model) {
            case Model::BlackScholes:
                out[i] = black_scholes(o.S, o.K, o.T, o.r, o.q, o.sig, o.call).price;
                break;
            case Model::Binomial:
                out[i] = binomial(o.S, o.K, o.T, o.r, o.q, o.sig, steps, o.call, o.american);
                break;
            case Model::MonteCarlo:
                out[i] = monte_carlo(o.S, o.K, o.T, o.r, o.q, o.sig, mc_paths,
                                     o.call, 7 + i).price;   // per-instrument seed
                break;
        }
    }, n_threads);
    return out;
}

// -------------- parallel scenario VaR (full revaluation) ----------------
// The institutional hot path: reprice the ENTIRE book under many market
// scenarios, build the P&L distribution, take VaR/ES. Parallel over
// scenarios; each scenario's P&L lands in its own slot ⇒ reproducible.
struct Scenario { double spot_shock; double vol_shock; double rate_shock; };

struct ScenarioVaR { double var; double es; std::uint64_t scenarios; double base_value; };

inline ScenarioVaR scenario_var(const std::vector<OptionSpec>& book,
                                const std::vector<Scenario>& scenarios,
                                double confidence, unsigned n_threads = 0) {
    // base value (serial, cheap)
    double base = 0.0;
    for (const auto& o : book)
        base += black_scholes(o.S, o.K, o.T, o.r, o.q, o.sig, o.call).price;

    std::vector<double> pnl(scenarios.size(), 0.0);
    par::parallel_for(scenarios.size(), [&](std::size_t s) {
        const Scenario& sc = scenarios[s];
        double v = 0.0;
        for (const auto& o : book) {
            const double S2 = o.S * (1.0 + sc.spot_shock);
            const double sig2 = std::max(o.sig + sc.vol_shock, 1e-6);
            const double r2 = o.r + sc.rate_shock;
            v += black_scholes(S2, o.K, o.T, r2, o.q, sig2, o.call).price;
        }
        pnl[s] = v - base;                     // scenario P&L
    }, n_threads);

    std::sort(pnl.begin(), pnl.end());          // losses at the low end
    const double a = 1.0 - confidence;
    const std::size_t idx = static_cast<std::size_t>(a * (pnl.size() - 1));
    const double var = -pnl[idx];
    double tail = 0.0; std::size_t k = 0;
    for (double x : pnl) if (x <= -var) { tail += x; ++k; }
    const double es = k ? -tail / static_cast<double>(k) : var;
    return ScenarioVaR{var, es, static_cast<std::uint64_t>(scenarios.size()), base};
}

}  // namespace quant
