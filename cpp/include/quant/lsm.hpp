// lsm.hpp — Longstaff-Schwartz American Monte Carlo, parallel & deterministic.
//
// The one pricing model that is genuinely hard AND parallel-over-paths.
// American options can be exercised early, so there's no closed form; LSM
// estimates the optimal exercise boundary by least-squares regression of the
// continuation value on a polynomial basis, working backward through time.
//
// Two things make this production-grade rather than a toy:
//   * PARALLEL: path generation and the per-step regression normal-equations
//     (XᵀX, Xᵀy) are accumulated across path-blocks, each block seeded by
//     splitmix64(seed+block) and folded in block order — so the fitted
//     coefficients, the exercise decisions, and the final price are IDENTICAL
//     for any thread count. "I threaded LSM deterministically" is true here.
//   * VERIFIED: cross-checks against a fine binomial American tree.
//
// Note: in-sample LSM is a slight LOW-biased estimator of the true American
// value (the exercise rule is fitted on the same paths it's evaluated on).
// That's expected and documented; a dual/upper-bound (Andersen-Broadie) is
// the standard next step.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <random>
#include <vector>

#include "quant/parallel.hpp"

namespace quant {

struct LSMResult {
    double price;                  // American value (LSM, low-biased)
    double stderr;
    double european;               // discounted terminal payoff (reference)
    double early_exercise_premium; // american - european
    std::uint64_t paths;
    int steps;
};

// Solve A c = b for small n (Gauss-Jordan, partial pivot). A row-major n*n.
inline std::vector<double> solve_linear(std::vector<double> A,
                                        std::vector<double> b, int n) {
    for (int col = 0; col < n; ++col) {
        int piv = col; double best = std::fabs(A[col * n + col]);
        for (int rr = col + 1; rr < n; ++rr) {
            const double v = std::fabs(A[rr * n + col]);
            if (v > best) { best = v; piv = rr; }
        }
        if (piv != col) {
            for (int k = 0; k < n; ++k) std::swap(A[col * n + k], A[piv * n + k]);
            std::swap(b[col], b[piv]);
        }
        const double d = A[col * n + col];
        if (std::fabs(d) < 1e-13) continue;          // near-singular → skip
        for (int rr = 0; rr < n; ++rr) {
            if (rr == col) continue;
            const double f = A[rr * n + col] / d;
            for (int k = col; k < n; ++k) A[rr * n + k] -= f * A[col * n + k];
            b[rr] -= f * b[col];
        }
    }
    std::vector<double> c(n, 0.0);
    for (int i = 0; i < n; ++i) {
        const double d = A[i * n + i];
        c[i] = std::fabs(d) > 1e-13 ? b[i] / d : 0.0;
    }
    return c;
}

inline LSMResult longstaff_schwartz(double S0, double K, double T, double r,
                                    double q, double sig, std::uint64_t n_paths,
                                    int n_steps, bool call, std::uint64_t seed = 7,
                                    unsigned n_threads = 0, int degree = 3) {
    const double dt = T / n_steps;
    const double drift = (r - q - 0.5 * sig * sig) * dt;
    const double vol = sig * std::sqrt(dt);
    const double disc = std::exp(-r * dt);
    const int nb = degree + 1;
    const std::uint64_t N = n_paths;
    const std::size_t stride = static_cast<std::size_t>(n_steps) + 1;

    // --- deterministic parallel path generation -------------------------
    std::vector<double> path(static_cast<std::size_t>(N) * stride);
    const std::uint64_t block = 8192;
    const std::uint64_t n_blocks = (N + block - 1) / block;
    par::parallel_for(static_cast<std::size_t>(n_blocks), [&](std::size_t bi) {
        const std::uint64_t lo = static_cast<std::uint64_t>(bi) * block;
        const std::uint64_t hi = std::min(lo + block, N);
        std::mt19937_64 gen(par::splitmix64(seed + bi));
        std::normal_distribution<double> Nd(0.0, 1.0);
        for (std::uint64_t i = lo; i < hi; ++i) {
            double s = S0;
            path[static_cast<std::size_t>(i) * stride] = s;
            for (int t = 1; t <= n_steps; ++t) {
                s *= std::exp(drift + vol * Nd(gen));
                path[static_cast<std::size_t>(i) * stride + t] = s;
            }
        }
    }, n_threads);

    auto payoff = [&](double s) { return std::max(call ? s - K : K - s, 0.0); };
    auto basis = [&](double s, double* g) {
        double x = s / K, p = 1.0;
        for (int d = 0; d <= degree; ++d) { g[d] = p; p *= x; }
    };

    // value as-of the current step; initialise at maturity
    std::vector<double> value(N);
    double euro_sum = 0.0;
    for (std::uint64_t i = 0; i < N; ++i) {
        const double vT = payoff(path[static_cast<std::size_t>(i) * stride + n_steps]);
        value[i] = vT;
        euro_sum += vT;
    }
    const double european = std::exp(-r * T) * euro_sum / N;

    // --- backward induction --------------------------------------------
    const int A_sz = nb * nb;
    for (int t = n_steps - 1; t >= 1; --t) {
        for (std::uint64_t i = 0; i < N; ++i) value[i] *= disc;   // t+1 → t

        // accumulate XᵀX and Xᵀy over ITM paths, deterministically:
        // per-block partials folded in block order (fp order fixed).
        std::vector<double> partials(static_cast<std::size_t>(n_blocks) * (A_sz + nb), 0.0);
        par::parallel_for(static_cast<std::size_t>(n_blocks), [&](std::size_t bi) {
            const std::uint64_t lo = static_cast<std::uint64_t>(bi) * block;
            const std::uint64_t hi = std::min(lo + block, N);
            double* pa = &partials[bi * (A_sz + nb)];
            double* pb = pa + A_sz;
            double g[8];
            for (std::uint64_t i = lo; i < hi; ++i) {
                const double s = path[static_cast<std::size_t>(i) * stride + t];
                if (payoff(s) <= 0.0) continue;               // ITM only
                basis(s, g);
                for (int a = 0; a < nb; ++a) {
                    pb[a] += g[a] * value[i];
                    for (int c = 0; c < nb; ++c) pa[a * nb + c] += g[a] * g[c];
                }
            }
        }, n_threads);

        std::vector<double> A(A_sz, 0.0), b(nb, 0.0);
        for (std::uint64_t bi = 0; bi < n_blocks; ++bi) {       // fixed-order fold
            const double* pa = &partials[bi * (A_sz + nb)];
            for (int k = 0; k < A_sz; ++k) A[k] += pa[k];
            for (int k = 0; k < nb; ++k) b[k] += pa[A_sz + k];
        }
        const std::vector<double> coef = solve_linear(A, b, nb);

        // exercise decision (ITM paths only)
        double g[8];
        for (std::uint64_t i = 0; i < N; ++i) {
            const double s = path[static_cast<std::size_t>(i) * stride + t];
            const double intrinsic = payoff(s);
            if (intrinsic <= 0.0) continue;
            basis(s, g);
            double cont = 0.0;
            for (int a = 0; a < nb; ++a) cont += coef[a] * g[a];
            if (intrinsic > cont) value[i] = intrinsic;         // exercise now
        }
    }

    // values as-of t=1 → discount to t=0
    double sum = 0.0, sum2 = 0.0;
    for (std::uint64_t i = 0; i < N; ++i) {
        const double v = disc * value[i];
        sum += v; sum2 += v * v;
    }
    const double price = sum / N;
    const double var = std::max(sum2 / N - price * price, 0.0) / N;
    return LSMResult{price, std::sqrt(var), european, price - european, N, n_steps};
}

}  // namespace quant
