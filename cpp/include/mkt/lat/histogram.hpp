// histogram.hpp — latency distribution, not latency average.
//
// THE POINT. Every performance figure this platform reported until now was
// THROUGHPUT: operations per second, as a median across repetitions. That is the
// right measure for a batch job and the wrong one for a trading path, because it
// hides the only number that decides whether a system is usable: the tail.
//
// A path that averages 200ns and has a p99.9 of 50us is not a 200ns path. It is a
// path that misses one message in a thousand by a margin that loses the trade,
// and a mean cannot see it — one 50us sample among a thousand 200ns samples moves
// the mean by 50ns, which looks like noise.
//
// WHY RECORDING EVERY SAMPLE, NOT A RUNNING SUMMARY. Percentiles are order
// statistics: p99.9 cannot be derived from a mean and a variance, and streaming
// approximations (t-digest, HdrHistogram's bucketing) trade accuracy for space we
// do not need. At a few million samples an array of uint64 is tens of megabytes,
// which is free on a desktop and exact.
//
// MEASUREMENT COST IS PART OF THE MEASUREMENT. Reading the clock is not free —
// on this platform it is tens of nanoseconds, which is the same order as the
// operations being timed. So the harness measures the CLOCK ITSELF first and
// reports that overhead alongside every result. A latency figure smaller than the
// timer's own cost is not a measurement, and this makes that visible rather than
// letting it be quoted.
//
// WHAT THIS DELIBERATELY DOES NOT CLAIM. These are in-process, single-machine
// numbers on a general-purpose OS with no CPU pinning, no isolated cores, no
// tickless kernel and no hugepages. A real low-latency deployment controls all
// four, and the p99.9 here is dominated by the scheduler rather than by the code.
// That is stated in the output, because a tail figure from a noisy host quoted
// without that context is misleading in the direction that flatters it.
#pragma once

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>

namespace mkt::lat {

using Clock = std::chrono::steady_clock;

inline std::uint64_t now_ns() noexcept {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            Clock::now().time_since_epoch()).count());
}

struct Stats {
    std::size_t n = 0;
    double min = 0, max = 0, mean = 0, stddev = 0;
    double p50 = 0, p90 = 0, p99 = 0, p999 = 0, p9999 = 0;
    // How far the tail is from the typical case. A ratio near 1 means a
    // predictable path; a large one means the median is not describing reality.
    double p99_over_p50 = 0, p999_over_p50 = 0;
};

class Histogram {
  public:
    explicit Histogram(std::size_t reserve = 1u << 20) { samples_.reserve(reserve); }

    // Hot: a single push_back into a pre-reserved vector, so recording a sample
    // does not itself allocate and pollute the measurement.
    inline void record(std::uint64_t ns) noexcept { samples_.push_back(ns); }

    void clear() noexcept { samples_.clear(); }
    std::size_t size() const noexcept { return samples_.size(); }
    const std::vector<std::uint64_t>& samples() const noexcept { return samples_; }

    // Drops the first `n` samples. The first iterations pay cold caches, page
    // faults and branch predictors that have never seen this loop; including them
    // measures start-up, not steady state.
    void drop_warmup(std::size_t n) {
        if (n >= samples_.size()) { samples_.clear(); return; }
        samples_.erase(samples_.begin(), samples_.begin() + static_cast<std::ptrdiff_t>(n));
    }

    Stats compute() const {
        Stats s;
        if (samples_.empty()) return s;
        std::vector<std::uint64_t> v = samples_;     // sort a copy: callers may
        std::sort(v.begin(), v.end());               // want to keep raw order
        s.n = v.size();
        s.min = static_cast<double>(v.front());
        s.max = static_cast<double>(v.back());

        long double sum = 0;
        for (auto x : v) sum += static_cast<long double>(x);
        s.mean = static_cast<double>(sum / static_cast<long double>(v.size()));

        long double var = 0;
        for (auto x : v) {
            const long double d = static_cast<long double>(x) - s.mean;
            var += d * d;
        }
        s.stddev = static_cast<double>(std::sqrt(var / static_cast<long double>(v.size())));

        s.p50   = pct(v, 0.50);
        s.p90   = pct(v, 0.90);
        s.p99   = pct(v, 0.99);
        s.p999  = pct(v, 0.999);
        s.p9999 = pct(v, 0.9999);
        s.p99_over_p50  = s.p50 > 0 ? s.p99 / s.p50 : 0;
        s.p999_over_p50 = s.p50 > 0 ? s.p999 / s.p50 : 0;
        return s;
    }

    // Nearest-rank on a sorted vector. Deliberately not interpolating: at p99.9
    // an interpolated value is a number that no operation actually took, and for
    // a tail figure the real observed sample is the honest answer.
    static double pct(const std::vector<std::uint64_t>& sorted, double q) {
        if (sorted.empty()) return 0;
        const double rank = q * static_cast<double>(sorted.size());
        std::size_t i = static_cast<std::size_t>(rank);
        if (i >= sorted.size()) i = sorted.size() - 1;
        return static_cast<double>(sorted[i]);
    }

  private:
    std::vector<std::uint64_t> samples_;
};

// The cost of the measurement apparatus itself. Reported with every result so a
// figure at or below this floor is visibly not a measurement of the code.
inline Stats clock_overhead(std::size_t n = 200000) {
    Histogram h(n);
    // Warm the clock path before timing it.
    for (std::size_t i = 0; i < 1000; ++i) (void)now_ns();
    for (std::size_t i = 0; i < n; ++i) {
        const auto a = now_ns();
        const auto b = now_ns();
        h.record(b - a);
    }
    h.drop_warmup(n / 100);
    return h.compute();
}

struct Named {
    std::string name;
    Stats stats;
    std::string note;
};

}  // namespace mkt::lat
