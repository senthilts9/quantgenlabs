// tsc.hpp — timestamping cheap enough to measure the thing being measured.
//
// WHY THIS EXISTS. The latency harness reported a clock overhead of 43ns p50 on
// this host, and three of its twelve measurements sat AT that floor — top-of-book
// reads, depth snapshots and the pre-trade gate were all being measured by an
// instrument more expensive than the operation. Those results were flagged
// `at_timer_floor: true` and honestly not quoted, but flagging a measurement you
// cannot make is not the same as making it.
//
// RDTSC is the fix: a single instruction that reads the CPU's invariant cycle
// counter, typically 6-10ns versus the 40-60ns a clock_gettime round trip costs
// through the vDSO. That is the difference between being able to measure a 46ns
// operation and not.
//
// FOUR THINGS THAT MAKE RDTSC CORRECT, and getting any of them wrong produces
// numbers that look fine and are not:
//
// 1. INVARIANT TSC. Older CPUs varied the counter with frequency scaling and
//    stopped it in deep C-states, so a "cycle count" meant different wall times
//    at different moments. Every x86-64 since Nehalem sets CPUID.80000007:EDX[8]
//    to say the TSC is invariant; this checks it and refuses to be used if not.
//
// 2. SERIALISATION. Plain RDTSC can be reordered by the out-of-order engine, so
//    the timestamp may be taken before or after the code it brackets. RDTSCP
//    waits for prior instructions to retire, which is what you want at the END of
//    a measured region. At the START, LFENCE before RDTSC prevents later work
//    from moving above it. Using bare RDTSC for both is the classic error and it
//    makes short measurements systematically wrong in an unpredictable direction.
//
// 3. CALIBRATION AGAINST A KNOWN CLOCK. Cycles are not nanoseconds. The ratio is
//    measured at startup against the monotonic clock rather than read from a
//    nominal frequency, because the nominal figure is the base clock and the CPU
//    is rarely running at it.
//
// 4. NOT COMPARABLE ACROSS CORES unless the TSC is synchronised. On a single
//    socket it generally is; across sockets it may not be. A timestamp taken on
//    one core and subtracted from one taken on another can be NEGATIVE, which is
//    why any cross-core measurement here also pins the threads.
//
// FALLBACK IS AUTOMATIC AND HONEST. On a platform without a usable invariant TSC
// this degrades to steady_clock and says so, rather than silently returning
// numbers in the wrong unit.
#pragma once

#include <chrono>
#include <cstdint>
#include <string>

#if defined(__x86_64__) || defined(_M_X64)
#define MKT_HAVE_X86_TSC 1
#include <x86intrin.h>
#else
#define MKT_HAVE_X86_TSC 0
#endif

namespace mkt::hft {

#if MKT_HAVE_X86_TSC

// Start of a measured region: LFENCE first so nothing below it drifts above.
inline std::uint64_t tsc_begin() noexcept {
    _mm_lfence();
    const std::uint64_t t = __rdtsc();
    _mm_lfence();
    return t;
}

// End of a measured region: RDTSCP waits for prior instructions to retire, so
// the work being timed is genuinely finished.
inline std::uint64_t tsc_end() noexcept {
    unsigned aux;
    const std::uint64_t t = __rdtscp(&aux);
    _mm_lfence();
    return t;
}

// Unfenced, for a monotonic counter where ordering does not matter. Cheapest of
// the three; do NOT use it to bracket a short region.
inline std::uint64_t tsc_raw() noexcept { return __rdtsc(); }

inline bool has_invariant_tsc() noexcept {
    // CPUID.80000007:EDX[8] — invariant TSC. Without it the counter varies with
    // frequency scaling and stops in deep C-states, so it is not a clock.
    unsigned a, b, c, d;
    __asm__ __volatile__("cpuid" : "=a"(a), "=b"(b), "=c"(c), "=d"(d)
                         : "a"(0x80000007), "c"(0));
    return (d & (1u << 8)) != 0;
}

#else   // non-x86: no TSC, everything routes to steady_clock

inline std::uint64_t tsc_begin() noexcept {
    return static_cast<std::uint64_t>(std::chrono::steady_clock::now()
                                      .time_since_epoch().count());
}
inline std::uint64_t tsc_end() noexcept { return tsc_begin(); }
inline std::uint64_t tsc_raw() noexcept { return tsc_begin(); }
inline bool has_invariant_tsc() noexcept { return false; }

#endif

// Converts cycles to nanoseconds using a ratio MEASURED at startup, not a
// nominal frequency. The nominal figure is the base clock and the CPU is rarely
// running at it.
class TscClock {
  public:
    static TscClock& instance() {
        static TscClock c;
        return c;
    }

    bool usable() const noexcept { return usable_; }
    double ns_per_cycle() const noexcept { return ns_per_cycle_; }
    double ghz() const noexcept { return ns_per_cycle_ > 0 ? 1.0 / ns_per_cycle_ : 0.0; }

    double to_ns(std::uint64_t cycles) const noexcept {
        return static_cast<double>(cycles) * ns_per_cycle_;
    }

    const std::string& source() const noexcept { return source_; }

    // Recalibrate over a longer window. The default startup calibration is kept
    // short so process start is not delayed; a benchmark that cares can pay for
    // a tighter estimate.
    void calibrate(std::chrono::milliseconds window = std::chrono::milliseconds(50)) {
        using clk = std::chrono::steady_clock;
        const auto w0 = clk::now();
        const std::uint64_t c0 = tsc_begin();
        while (clk::now() - w0 < window) { /* spin: sleeping would not tick the TSC the same way */ }
        const std::uint64_t c1 = tsc_end();
        const auto w1 = clk::now();

        const double elapsed_ns =
            std::chrono::duration<double, std::nano>(w1 - w0).count();
        const std::uint64_t cycles = c1 - c0;
        if (cycles > 0 && elapsed_ns > 0) ns_per_cycle_ = elapsed_ns / static_cast<double>(cycles);
    }

  private:
    TscClock() {
#if MKT_HAVE_X86_TSC
        if (has_invariant_tsc()) {
            usable_ = true;
            source_ = "rdtsc (invariant)";
            calibrate(std::chrono::milliseconds(20));
            return;
        }
        source_ = "steady_clock (TSC present but NOT invariant -- it varies with "
                  "frequency scaling and stops in deep C-states, so it is not a clock)";
#else
        source_ = "steady_clock (no x86 TSC on this architecture)";
#endif
        usable_ = false;
        ns_per_cycle_ = 1.0;     // steady_clock already counts nanoseconds here
    }

    bool usable_ = false;
    double ns_per_cycle_ = 1.0;
    std::string source_;
};

// One timestamp, in whatever the best available source is. Returns CYCLES when
// the TSC is usable and nanoseconds otherwise, so callers must convert through
// TscClock::to_ns rather than assuming a unit.
inline std::uint64_t fast_ticks() noexcept {
    return TscClock::instance().usable()
               ? tsc_raw()
               : static_cast<std::uint64_t>(
                     std::chrono::steady_clock::now().time_since_epoch().count());
}

}  // namespace mkt::hft
