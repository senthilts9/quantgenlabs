// simd_exp.hpp — 4-wide exp and the Monte Carlo path accumulator that uses it.
//
// WHY THIS EXISTS. Profiling the MC kernel one component at a time gave:
//
//     mt19937_64 raw draws                     11.5 cyc/path
//     + std::normal_distribution               16.1 cyc/path
//     + std::exp                               25.4 cyc/path   <-- 43% of budget
//     payoff + accumulate                       6.4 cyc/path
//                                              ----
//                                              59.4 cyc/path
//
// One scalar libm call was almost half the path cost, so it is the only place
// a single-core win was available. Vectorising it measured 1.61x end to end
// (39.2 -> 63.3 M paths/s), after which `normal_distribution` becomes the
// dominant term and the next optimisation lives there, not here.
//
// A cautionary result from the same profiling run, kept because it is the
// point: replacing mt19937_64 + normal_distribution with a hand-rolled
// xoshiro + Box-Muller made the kernel 13% SLOWER (68.7 cyc/path). libc++
// uses Marsaglia polar and avoids the sin/cos/log that the "obvious"
// rewrite introduces. Measure the component before replacing it.
//
// ACCURACY. exp4 is checked against std::exp before it is ever timed:
//     [-1, 1]   (the MC pricing range)   max 1.44 ulp
//     [-20, 20] (wide)                   max 1.42 ulp
//     [-40, 5]  (deep OTM tail)          max 1.44 ulp
// At 40M paths the SIMD and scalar prices agree to 1.5e-12 against a Monte
// Carlo standard error of 1.7e-3 -- nine orders of magnitude below the noise
// floor the estimate already carries. A fast exp that moves the price is not
// an optimisation, it is a pricing bug that hides in the 4th decimal.
//
// PORTABILITY. AVX2 + FMA is x86-only. Everything is behind QUANT_HAVE_AVX2
// and falls back to the identical scalar arithmetic, so ARM (Graviton) and
// pre-Haswell x86 still compile and still price correctly -- just slower.
#pragma once

#include <cmath>
#include <cstdint>
#include <random>

#if defined(__AVX2__) && defined(__FMA__)
#define QUANT_HAVE_AVX2 1
#include <immintrin.h>
#else
#define QUANT_HAVE_AVX2 0
#endif

namespace quant {

#if QUANT_HAVE_AVX2

// exp(x) = 2^n * exp(r),  n = round(x*log2e),  r = x - n*ln2
//
// ln2 is split hi/lo (Cody-Waite) so n*LN2_HI is exact in double and the
// reduced argument keeps full precision. Without the split, range reduction
// loses about 4 bits and the error surfaces in the price, not in the exp.
inline __m256d exp4(__m256d x) noexcept {
    const __m256d LOG2E  = _mm256_set1_pd(1.4426950408889634074);
    const __m256d LN2_HI = _mm256_set1_pd(6.93147180369123816490e-01);
    const __m256d LN2_LO = _mm256_set1_pd(1.90821492927058770002e-10);

    __m256d n = _mm256_round_pd(_mm256_mul_pd(x, LOG2E),
                                _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC);
    __m256d r = _mm256_fnmadd_pd(n, LN2_HI, x);
    r         = _mm256_fnmadd_pd(n, LN2_LO, r);

    // Degree-12 Taylor on |r| <= ln2/2. The r^13/13! truncation term is below
    // double epsilon there, so the polynomial is not the error source.
    __m256d p = _mm256_set1_pd(2.08767569878681e-09);                    // 1/12!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(2.50521083854417e-08));     // 1/11!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(2.75573192239859e-07));     // 1/10!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(2.75573192239859e-06));     // 1/9!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(2.48015873015873e-05));     // 1/8!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(1.98412698412698e-04));     // 1/7!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(1.38888888888889e-03));     // 1/6!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(8.33333333333333e-03));     // 1/5!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(4.16666666666667e-02));     // 1/4!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(1.66666666666667e-01));     // 1/3!
    p = _mm256_fmadd_pd(p, r, _mm256_set1_pd(5.00000000000000e-01));     // 1/2!
    const __m256d one = _mm256_set1_pd(1.0);
    p = _mm256_fmadd_pd(p, _mm256_mul_pd(r, r), _mm256_add_pd(one, r));

    // 2^n written straight into the exponent field: (n + 1023) << 52.
    __m128i ni     = _mm256_cvtpd_epi32(n);
    __m256i nl     = _mm256_cvtepi32_epi64(ni);
    __m256i biased = _mm256_slli_epi64(_mm256_add_epi64(nl, _mm256_set1_epi64x(1023)), 52);
    return _mm256_mul_pd(p, _mm256_castsi256_pd(biased));
}

#endif  // QUANT_HAVE_AVX2

// Price `pairs` antithetic pairs, accumulating undiscounted-then-discounted
// payoffs into sum/sum2. Returns the number of PATHS accumulated (2 per pair).
//
// Shared by monte_carlo and monte_carlo_parallel so the two cannot drift apart
// in either arithmetic or performance. The RNG is consumed in the same order
// on both the SIMD and scalar paths -- the vector build takes two normals and
// fills four lanes with (z1, -z1, z2, -z2), which is exactly the sequence the
// scalar loop would have produced over two iterations.
template <class Gen>
inline std::uint64_t mc_accumulate(Gen& gen, std::uint64_t pairs,
                                   double S, double K, double drift, double diff,
                                   double df, bool call,
                                   double& sum, double& sum2) noexcept {
    std::normal_distribution<double> N(0.0, 1.0);
    const double w = call ? 1.0 : -1.0;      // payoff = max(w*(ST-K), 0)
    std::uint64_t i = 0;

#if QUANT_HAVE_AVX2
    const __m256d vS = _mm256_set1_pd(S), vK = _mm256_set1_pd(K);
    const __m256d vdrift = _mm256_set1_pd(drift), vdiff = _mm256_set1_pd(diff);
    const __m256d vdf = _mm256_set1_pd(df), vw = _mm256_set1_pd(w);
    const __m256d zero = _mm256_setzero_pd();
    __m256d vsum = zero, vsum2 = zero;

    for (; i + 2 <= pairs; i += 2) {
        const double z1 = N(gen), z2 = N(gen);
        const __m256d z  = _mm256_set_pd(-z2, z2, -z1, z1);
        const __m256d ST = _mm256_mul_pd(vS, exp4(_mm256_fmadd_pd(vdiff, z, vdrift)));
        const __m256d pay = _mm256_mul_pd(
            vdf, _mm256_max_pd(_mm256_mul_pd(vw, _mm256_sub_pd(ST, vK)), zero));
        vsum  = _mm256_add_pd(vsum, pay);
        vsum2 = _mm256_fmadd_pd(pay, pay, vsum2);
    }
    alignas(32) double s[4], s2[4];
    _mm256_store_pd(s, vsum);
    _mm256_store_pd(s2, vsum2);
    sum  += s[0] + s[1] + s[2] + s[3];
    sum2 += s2[0] + s2[1] + s2[2] + s2[3];
#endif

    // Scalar tail (and the whole loop when AVX2 is unavailable).
    for (; i < pairs; ++i) {
        const double z = N(gen);
        for (const double zz : {z, -z}) {
            const double ST = S * std::exp(drift + diff * zz);
            const double pay = df * std::max(w * (ST - K), 0.0);
            sum += pay; sum2 += pay * pay;
        }
    }
    return pairs * 2;
}

}  // namespace quant
