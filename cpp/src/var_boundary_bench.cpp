// Isolates ONE question: is the var_historical 0.6x loss algorithmic or linguistic?
//
// Current engine sorts the whole window (O(n log n)). numpy's percentile uses
// np.partition -> introselect (O(n) average). If that is the cause, swapping
// std::sort for std::nth_element should move the number.
//
// Both variants are compiled here side by side and cross-checked against each
// other before either is timed -- a faster wrong answer is worth nothing.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <stdexcept>
#include <vector>

using clk = std::chrono::steady_clock;

struct VaR { double var, es; };

// ---- variant A: verbatim copy of quant_engine.hpp:143 (full sort) ----------
inline VaR historical_var_sort(std::vector<double> returns, double confidence) {
    if (returns.size() < 2) throw std::invalid_argument("need >=2 returns");
    std::sort(returns.begin(), returns.end());
    const double a = 1.0 - confidence;
    const double pos = a * static_cast<double>(returns.size() - 1);
    const std::size_t lo = static_cast<std::size_t>(std::floor(pos));
    const double frac = pos - static_cast<double>(lo);
    const double qv = lo + 1 < returns.size()
                          ? returns[lo] + frac * (returns[lo + 1] - returns[lo])
                          : returns[lo];
    const double var = -qv;
    double tail = 0.0; std::size_t k = 0;
    for (double x : returns) if (x <= -var) { tail += x; ++k; }
    return {var, k ? -tail / static_cast<double>(k) : var};
}

// ---- variant B: selection instead of sort ---------------------------------
// We need exactly two order statistics (lo and lo+1) plus a tail scan. A full
// sort computes n of them and throws n-2 away.
//
// nth_element puts the lo-th order statistic in place and partitions around
// it, so everything at [lo+1, end) is >= it -- the (lo+1)-th order statistic
// is then just the min of that suffix. Both steps are O(n).
//
// The ES tail scan is order-independent (it is a predicate over every element),
// so it is unaffected by the array being only partially ordered.
inline VaR historical_var_select(std::vector<double> returns, double confidence) {
    if (returns.size() < 2) throw std::invalid_argument("need >=2 returns");
    const double a = 1.0 - confidence;
    const double pos = a * static_cast<double>(returns.size() - 1);
    const std::size_t lo = static_cast<std::size_t>(std::floor(pos));
    const double frac = pos - static_cast<double>(lo);

    std::nth_element(returns.begin(), returns.begin() + static_cast<long>(lo),
                     returns.end());
    const double x_lo = returns[lo];
    double qv = x_lo;
    if (lo + 1 < returns.size()) {
        const double x_hi = *std::min_element(
            returns.begin() + static_cast<long>(lo) + 1, returns.end());
        qv = x_lo + frac * (x_hi - x_lo);
    }
    const double var = -qv;
    double tail = 0.0; std::size_t k = 0;
    for (double x : returns) if (x <= -var) { tail += x; ++k; }
    return {var, k ? -tail / static_cast<double>(k) : var};
}

static std::vector<double> load(const char* path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { std::fprintf(stderr, "cannot open %s\n", path); std::exit(1); }
    f.seekg(0, std::ios::end);
    const std::size_t n = static_cast<std::size_t>(f.tellg()) / sizeof(double);
    f.seekg(0, std::ios::beg);
    std::vector<double> v(n);
    f.read(reinterpret_cast<char*>(v.data()), static_cast<long>(n * sizeof(double)));
    return v;
}

template <typename F>
static double best_of(F fn, const std::vector<double>& data, int reps, double& out) {
    double best = 1e18;
    for (int i = 0; i < reps; ++i) {
        const auto t0 = clk::now();
        const VaR v = fn(data, 0.99);          // by-value copy is INSIDE the timing,
        const auto t1 = clk::now();            // exactly as the engine is called
        asm volatile("" : : "r,m"(v.var) : "memory");
        out = v.var;
        best = std::min(best, std::chrono::duration<double, std::micro>(t1 - t0).count());
    }
    return best;
}

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "usage: var_bench <file.bin> [reps]\n"); return 1; }
    const std::vector<double> data = load(argv[1]);
    const int reps = argc > 2 ? std::atoi(argv[2]) : 200;

    // Cross-check BEFORE timing: identical VaR and ES, or the comparison is void.
    const VaR a = historical_var_sort(data, 0.99);
    const VaR b = historical_var_select(data, 0.99);
    const double dv = std::fabs(a.var - b.var), de = std::fabs(a.es - b.es);
    std::printf("n=%zu  sort: VaR=%.12f ES=%.12f\n", data.size(), a.var, a.es);
    std::printf("        select: VaR=%.12f ES=%.12f   |dVaR|=%.3g |dES|=%.3g %s\n",
                b.var, b.es, dv, de, (dv < 1e-15 && de < 1e-15) ? "IDENTICAL" : "*** MISMATCH ***");
    if (!(dv < 1e-15 && de < 1e-15)) return 2;

    double sink = 0;
    for (int i = 0; i < 20; ++i) { historical_var_sort(data, 0.99); }   // warm up
    const double t_sort = best_of(historical_var_sort, data, reps, sink);
    const double t_sel  = best_of(historical_var_select, data, reps, sink);
    std::printf("        sort   %10.2f us   (best of %d)\n", t_sort, reps);
    std::printf("        select %10.2f us   -> %.2fx vs sort\n", t_sel, t_sort / t_sel);
    return 0;
}
