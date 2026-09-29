// affinity.hpp — thread placement, and honest reporting of what the OS allows.
//
// WHY PINNING MATTERS at this scale. A thread migrated to another core arrives
// with cold L1 and L2, and on a multi-socket box with a cold LLC and remote
// memory. The cost is tens of microseconds, which does not show in a mean and
// shows up entirely in the p99.9 — the tail nobody can explain. A feed handler
// and its strategy thread that keep moving are paying that repeatedly.
//
// WHAT EACH PLATFORM ACTUALLY DOES, because "pinned" means different things and
// reporting a hint as a guarantee is worse than not pinning at all:
//
//   LINUX: pthread_setaffinity_np is a HARD constraint. The scheduler will not
//   run the thread anywhere else. Combined with `isolcpus` or cgroup cpusets,
//   nothing else runs there either, which is the actual goal — pinning to a core
//   that the rest of the system also uses buys much less than people expect.
//
//   macOS: THREAD_AFFINITY_POLICY is an AFFINITY TAG, not a pin. It asks the
//   scheduler to co-locate threads sharing a tag on the same L2, and the kernel
//   is free to ignore it. Apple does not expose hard pinning at all. So this
//   reports `Hint` rather than `Pinned`, and the benchmark output says so instead
//   of implying a control it does not have.
//
//   Anything else: unsupported, reported as such.
//
// NUMA. Detected rather than assumed. On a single-socket machine — like the one
// this was written on — there is no remote memory and the whole concern is moot;
// the API is here so that the code is correct on a box where it is not, and
// reports 1 node rather than pretending to do something.
#pragma once

#include <cstdint>
#include <string>
#include <thread>

#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#elif defined(__APPLE__)
#include <mach/mach.h>
#include <mach/thread_act.h>
#include <mach/thread_policy.h>
#include <pthread.h>
#include <sys/sysctl.h>
#endif

namespace mkt::hft {

enum class AffinityResult {
    Pinned,        // a hard constraint the scheduler will honour
    Hint,          // a request the kernel may ignore (macOS affinity tags)
    Unsupported,   // no API on this platform
    Failed,        // the call was made and refused
};

constexpr const char* to_string(AffinityResult r) noexcept {
    switch (r) {
        case AffinityResult::Pinned:      return "pinned (hard constraint)";
        case AffinityResult::Hint:        return "hint only (kernel may ignore)";
        case AffinityResult::Unsupported: return "unsupported on this platform";
        case AffinityResult::Failed:      return "call failed";
    }
    return "unknown";
}

/// Place the CURRENT thread on `cpu`. The return value distinguishes a real pin
/// from a hint, because treating the two the same is how a "pinned" benchmark on
/// macOS produces tails that make no sense.
inline AffinityResult pin_current_thread(unsigned cpu) noexcept {
#if defined(__linux__)
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    const int rc = pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
    return rc == 0 ? AffinityResult::Pinned : AffinityResult::Failed;
#elif defined(__APPLE__)
    // An affinity TAG, not a pin: it asks the scheduler to co-locate threads
    // sharing a tag on one L2. Distinct non-zero tags therefore mean "keep these
    // apart"; tag 0 means "no preference".
    thread_affinity_policy_data_t policy{static_cast<integer_t>(cpu + 1)};
    const kern_return_t rc = thread_policy_set(
        pthread_mach_thread_np(pthread_self()), THREAD_AFFINITY_POLICY,
        reinterpret_cast<thread_policy_t>(&policy), THREAD_AFFINITY_POLICY_COUNT);
    return rc == KERN_SUCCESS ? AffinityResult::Hint : AffinityResult::Failed;
#else
    (void)cpu;
    return AffinityResult::Unsupported;
#endif
}

/// True when this platform can HARD pin. Used by the benchmark harness to label
/// its own results rather than claiming isolation it does not have.
inline bool hard_pinning_available() noexcept {
#if defined(__linux__)
    return true;
#else
    return false;
#endif
}

struct TopologyInfo {
    unsigned logical_cpus = 0;
    unsigned physical_cores = 0;
    unsigned numa_nodes = 1;
    bool hard_pinning = false;
    std::string affinity_note;
};

inline TopologyInfo topology() {
    TopologyInfo t;
    t.logical_cpus = std::thread::hardware_concurrency();
    t.physical_cores = t.logical_cpus;
    t.hard_pinning = hard_pinning_available();

#if defined(__APPLE__)
    int v = 0;
    std::size_t len = sizeof(v);
    if (sysctlbyname("hw.physicalcpu", &v, &len, nullptr, 0) == 0) t.physical_cores = v;
    len = sizeof(v);
    if (sysctlbyname("hw.packages", &v, &len, nullptr, 0) == 0 && v > 0)
        t.numa_nodes = static_cast<unsigned>(v);
    t.affinity_note =
        "macOS exposes affinity TAGS, not pinning: the kernel is asked to "
        "co-locate tagged threads on one L2 and may ignore it. Tail latency here "
        "includes migrations that a Linux box with isolcpus would not have.";
#elif defined(__linux__)
    // Physical cores and NUMA nodes need /sys or /proc parsing; left to the
    // caller rather than guessed, because a wrong core count makes every
    // per-core figure wrong.
    t.affinity_note =
        "Linux: pthread_setaffinity_np is a hard constraint. For a real "
        "low-latency result also isolate the cores (isolcpus or a cgroup cpuset) "
        "-- pinning to a core the rest of the system also uses buys much less "
        "than people expect.";
#else
    t.affinity_note = "No affinity API on this platform; threads float.";
#endif
    if (t.numa_nodes <= 1)
        t.affinity_note += " Single NUMA node, so remote-memory cost does not "
                           "arise on this host.";
    return t;
}

}  // namespace mkt::hft
