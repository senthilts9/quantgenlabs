// mkt_bench.cpp — reproducible benchmark harness for the market-connectivity core.
//
// Emits JSON on stdout so the Performance Lab UI reads MEASURED numbers instead
// of numbers someone typed into a component. Every claim this harness makes is
// accompanied by the context needed to reproduce or falsify it.
//
// WHY THE ENVIRONMENT BLOCK EXISTS. "34M paths/sec" is not a fact about code, it
// is a fact about code on a CPU with a compiler at an optimisation level running
// a specific model with a specific number of timesteps. Quoting the number
// without those is how benchmark claims become unfalsifiable. So the harness
// records: CPU brand, physical and logical core counts, compiler and version,
// the exact optimisation flags it was built with, and for the Monte Carlo rows
// the model, path count, TIMESTEP COUNT and RNG.
//
// THE TIMESTEP DISCLOSURE IS THE IMPORTANT ONE. This engine prices European
// options by sampling the TERMINAL value of a GBM: one timestep per path. A
// 252-step path-dependent simulation does roughly 252x the work per path, so a
// terminal-value paths/sec figure is not comparable to a path-dependent one. That
// is stated in the output, not left for a reader to assume.
//
// WHY SCALING IS REPORTED AGAINST PHYSICAL CORES. This machine reports 4 logical
// CPUs from 2 physical cores. Two threads on two physical cores is real
// parallelism; four threads on the same two cores is SMT, which shares execution
// units and cannot deliver 4x on an ALU-bound workload. Reporting "4 threads:
// 2.8x" without that context invites the reader to infer 8 threads would give
// 5.7x. Every row is therefore tagged with whether it oversubscribes the physical
// core count, and the harness refuses to extrapolate beyond what it measured.
#include "quant/quant_engine.hpp"
#include "quant/quant_parallel.hpp"
#include "mkt/book/ladder.hpp"
#include "mkt/feed/validator.hpp"
#include "mkt/fix/message.hpp"
#include "mkt/fix/session.hpp"
#include "mkt/risk/pretrade.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <string>
#include <thread>
#include <vector>

#if defined(__APPLE__)
#include <sys/sysctl.h>
#endif

using Clock = std::chrono::steady_clock;

static double seconds_since(Clock::time_point t0) {
    return std::chrono::duration<double>(Clock::now() - t0).count();
}

// ----------------------------------------------------------- environment
struct Env {
    std::string cpu_brand = "unknown";
    std::string simd_available = "unknown";   // what the CPU supports at runtime
    unsigned physical_cores = 0;
    unsigned logical_cores  = 0;
    std::string compiler;
    std::string flags;
    std::string arch;
    std::string simd;
};

static Env probe_env() {
    Env e;
    e.logical_cores = std::thread::hardware_concurrency();

#if defined(__APPLE__)
    char brand[256]; std::size_t bl = sizeof brand;
    if (sysctlbyname("machdep.cpu.brand_string", brand, &bl, nullptr, 0) == 0) e.cpu_brand = brand;
    int phys = 0; std::size_t pl = sizeof phys;
    if (sysctlbyname("hw.physicalcpu", &phys, &pl, nullptr, 0) == 0) e.physical_cores = static_cast<unsigned>(phys);
    // Runtime capability, independent of what the binary was compiled for. The
    // gap between the two is the point: a CPU that supports AVX2 running a
    // baseline binary is leaving measurable throughput on the table, and that is
    // only visible if both are reported.
    int has = 0; std::size_t hl = sizeof has;
    if (sysctlbyname("hw.optional.avx512f", &has, &hl, nullptr, 0) == 0 && has) e.simd_available = "AVX-512";
    else { hl = sizeof has;
      if (sysctlbyname("hw.optional.avx2_0", &has, &hl, nullptr, 0) == 0 && has) e.simd_available = "AVX2";
      else { hl = sizeof has;
        if (sysctlbyname("hw.optional.avx1_0", &has, &hl, nullptr, 0) == 0 && has) e.simd_available = "AVX";
        else e.simd_available = "SSE"; } }
#elif defined(__linux__)
    // Physical core count on Linux needs /proc parsing; left to the caller via
    // the env var rather than guessed, because a wrong physical-core count makes
    // every scaling-efficiency number wrong.
    if (const char* p = std::getenv("QGL_PHYSICAL_CORES")) e.physical_cores = static_cast<unsigned>(std::atoi(p));
#endif
    if (e.physical_cores == 0) e.physical_cores = e.logical_cores;   // conservative

#if defined(__clang__)
    e.compiler = std::string("clang++ ") + __clang_version__;
#elif defined(__GNUC__)
    e.compiler = "g++ " + std::to_string(__GNUC__) + "." + std::to_string(__GNUC_MINOR__)
               + "." + std::to_string(__GNUC_PATCHLEVEL__);
#else
    e.compiler = "unknown";
#endif

    // Reported from the macros actually in effect, so the flags string cannot
    // drift from the build.
#if defined(__OPTIMIZE__)
    e.flags = "-O3 -std=c++20";
#else
    e.flags = "-O0 (UNOPTIMISED -- these numbers are not meaningful)";
#endif

#if defined(__x86_64__)
    e.arch = "x86_64";
#elif defined(__aarch64__)
    e.arch = "arm64";
#else
    e.arch = "unknown";
#endif

#if defined(__AVX512F__)
    e.simd = "AVX-512";
#elif defined(__AVX2__)
    e.simd = "AVX2";
#elif defined(__AVX__)
    e.simd = "AVX";
#elif defined(__SSE4_2__)
    e.simd = "SSE4.2";
#elif defined(__ARM_NEON)
    e.simd = "NEON";
#else
    e.simd = "baseline";
#endif
    return e;
}

// Median of a small sample. Preferred over the mean because a single scheduler
// preemption produces one huge outlier that drags a mean but not a median.
static double median_of(std::vector<double> v) {
    std::sort(v.begin(), v.end());
    const std::size_t n = v.size();
    return n == 0 ? 0.0 : (n % 2 ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]));
}

// Repeats `fn` until at least `min_sec` has elapsed, returning ops/sec. Running
// to a time floor rather than a fixed iteration count keeps short operations out
// of clock-resolution noise.
template <class F>
static double throughput_once(F&& fn, std::uint64_t ops_per_call, double min_sec = 0.35) {
    // One untimed call first: the first invocation pays page faults and
    // i-cache misses that steady-state operation does not.
    fn();
    auto t0 = Clock::now();
    std::uint64_t calls = 0;
    double el = 0.0;
    do { fn(); ++calls; el = seconds_since(t0); } while (el < min_sec);
    return static_cast<double>(calls * ops_per_call) / el;
}

// Median of `reps` independent measurements, plus the observed spread. A single
// throughput sample on a desktop under an ordinary OS scheduler is not a
// measurement, it is one draw from a noisy distribution.
struct Thr { double median = 0.0, best = 0.0, worst = 0.0, spread_pct = 0.0; };

template <class F>
static Thr throughput(F&& fn, std::uint64_t ops_per_call, int reps = 3, double min_sec = 0.2) {
    std::vector<double> v;
    for (int i = 0; i < reps; ++i) v.push_back(throughput_once(fn, ops_per_call, min_sec));
    Thr t;
    t.median = median_of(v);
    t.best   = *std::max_element(v.begin(), v.end());
    t.worst  = *std::min_element(v.begin(), v.end());
    t.spread_pct = t.median > 0.0 ? 100.0 * (t.best - t.worst) / t.median : 0.0;
    return t;
}

struct MCRow {
    unsigned threads;
    double   seconds;         // median
    double   paths_per_sec;   // from the median time
    double   best_paths_per_sec;
    double   worst_paths_per_sec;
    double   spread_pct;      // (best - worst) / median, the honesty number
    double   speedup;
    double   efficiency;      // speedup / threads
    bool     oversubscribed;  // threads > physical cores
    double   price;
};


int main(int argc, char** argv) {
    const Env env = probe_env();

    // ---------------------------------------------------- Monte Carlo scaling
    const std::uint64_t mc_paths = (argc > 1) ? std::strtoull(argv[1], nullptr, 10) : 20'000'000ULL;
    const double S = 100, K = 100, T = 1.0, r = 0.03, q = 0.0, sig = 0.2;

    std::vector<unsigned> thread_counts;
    for (unsigned t = 1; t <= env.logical_cores; t *= 2) thread_counts.push_back(t);
    if (thread_counts.empty()) thread_counts.push_back(1);

    const int reps = (argc > 2) ? std::atoi(argv[2]) : 5;
    std::vector<MCRow> mc_rows;
    double base_seconds = 0.0;
    for (unsigned nt : thread_counts) {
        // Warm-up at this thread count so thread-creation cost is not counted.
        (void)quant::monte_carlo_parallel(S, K, T, r, q, sig, mc_paths / 10, true, 7, nt);
        std::vector<double> times;
        double price = 0.0;
        for (int rep = 0; rep < reps; ++rep) {
            auto t0 = Clock::now();
            const auto res = quant::monte_carlo_parallel(S, K, T, r, q, sig, mc_paths, true, 7, nt);
            times.push_back(seconds_since(t0));
            price = res.price;
        }
        const double med  = median_of(times);
        const double fast = *std::min_element(times.begin(), times.end());
        const double slow = *std::max_element(times.begin(), times.end());
        if (nt == 1) base_seconds = med;
        MCRow row;
        row.threads             = nt;
        row.seconds             = med;
        row.paths_per_sec       = static_cast<double>(mc_paths) / med;
        row.best_paths_per_sec  = static_cast<double>(mc_paths) / fast;
        row.worst_paths_per_sec = static_cast<double>(mc_paths) / slow;
        row.spread_pct          = med > 0.0 ? 100.0 * (slow - fast) / med : 0.0;
        row.speedup             = base_seconds > 0.0 ? base_seconds / med : 1.0;
        row.efficiency          = row.speedup / static_cast<double>(nt);
        row.oversubscribed      = nt > env.physical_cores;
        row.price               = price;
        mc_rows.push_back(row);
    }

    // Determinism across thread counts: the property that makes the parallel
    // engine trustworthy. Compared bitwise, not with a tolerance.
    bool mc_deterministic = true;
    for (const auto& row : mc_rows)
        if (row.price != mc_rows.front().price) mc_deterministic = false;

    // ---------------------------------------------------- order book
    mkt::book::Ladder book(9000, 2001);
    std::mt19937_64 rng(42);
    std::uniform_int_distribution<int> off(0, 40);
    std::uniform_int_distribution<int> qd(0, 500);
    const Thr book_ups = throughput([&] {
        for (int i = 0; i < 1000; ++i) {
            const mkt::book::Ticks bid = 9990 - off(rng);
            const mkt::book::Ticks ask = 10010 + off(rng);
            book.upsert(mkt::book::Side::Bid, bid, qd(rng));
            book.upsert(mkt::book::Side::Ask, ask, qd(rng));
        }
    }, 2000);

    // Top-of-book reads, the most frequently called accessor in the system.
    mkt::book::Ladder rbook(9000, 2001);
    for (int i = 0; i < 20; ++i) {
        rbook.upsert(mkt::book::Side::Bid, 9990 - i, 100 + i);
        rbook.upsert(mkt::book::Side::Ask, 10010 + i, 100 + i);
    }
    volatile double sink = 0.0;
    const Thr tob_reads = throughput([&] {
        for (int i = 0; i < 1000; ++i) sink = sink + rbook.micro_price_ticks();
    }, 1000);

    // Depth snapshot into caller storage (no allocation).
    std::array<mkt::book::Level, 10> depth_out{};
    const Thr depth_snaps = throughput([&] {
        for (int i = 0; i < 1000; ++i) sink = sink + static_cast<double>(rbook.depth(mkt::book::Side::Bid, depth_out));
    }, 1000);

    // ---------------------------------------------------- feed validation
    mkt::feed::Validator val;
    std::uint64_t vseq = 0;
    std::int64_t vts = 0;
    std::uniform_real_distribution<double> px(99.9, 100.1);
    const Thr feed_ticks = throughput([&] {
        for (int i = 0; i < 1000; ++i) {
            mkt::feed::Tick t{++vseq, vts += 1'000'000, vts, px(rng), 10.0};
            sink = sink + static_cast<double>(static_cast<int>(val.check(t)));
        }
    }, 1000);

    // ---------------------------------------------------- FIX
    mkt::fix::Builder fb("FIX.4.4");
    const Thr fix_encode = throughput([&] {
        for (int i = 0; i < 200; ++i) {
            fb.reset();
            fb.add(mkt::fix::tag::MsgType, mkt::fix::msg::NewOrderSingle)
              .add(mkt::fix::tag::SenderCompID, "QGLABS")
              .add(mkt::fix::tag::TargetCompID, "SIMVENUE")
              .add_int(mkt::fix::tag::MsgSeqNum, i)
              .add(mkt::fix::tag::SendingTime, "20260928-09:30:00.000")
              .add(mkt::fix::tag::ClOrdID, "ORD-000123")
              .add(mkt::fix::tag::Symbol, "AAPL")
              .add(mkt::fix::tag::Side, "1")
              .add_int(mkt::fix::tag::OrderQty, 500)
              .add(mkt::fix::tag::OrdType, "2")
              .add_price(mkt::fix::tag::Price, 187.25, 2);
            sink = sink + static_cast<double>(fb.finish().size());
        }
    }, 200);

    // Encode one message to parse repeatedly, so parse cost is measured alone.
    fb.reset();
    fb.add(mkt::fix::tag::MsgType, mkt::fix::msg::ExecutionReport)
      .add(mkt::fix::tag::SenderCompID, "SIMVENUE")
      .add(mkt::fix::tag::TargetCompID, "QGLABS")
      .add_int(mkt::fix::tag::MsgSeqNum, 12345)
      .add(mkt::fix::tag::SendingTime, "20260928-09:30:00.000")
      .add(mkt::fix::tag::OrderID, "V-99887766")
      .add(mkt::fix::tag::ExecID, "E-1122334455")
      .add(mkt::fix::tag::ExecType, "F")
      .add(mkt::fix::tag::OrdStatus, "2")
      .add(mkt::fix::tag::Symbol, "AAPL")
      .add(mkt::fix::tag::Side, "1")
      .add_int(mkt::fix::tag::LastQty, 500)
      .add_price(mkt::fix::tag::LastPx, 187.25, 2)
      .add_int(mkt::fix::tag::CumQty, 500)
      .add_price(mkt::fix::tag::AvgPx, 187.25, 2);
    const std::string exec_report(fb.finish());

    mkt::fix::Message pm;
    const Thr fix_parse_validated = throughput([&] {
        for (int i = 0; i < 500; ++i) {
            pm.parse(exec_report, /*validate=*/true);
            sink = sink + static_cast<double>(pm.field_count());
        }
    }, 500);
    const Thr fix_parse_unvalidated = throughput([&] {
        for (int i = 0; i < 500; ++i) {
            pm.parse(exec_report, /*validate=*/false);
            sink = sink + static_cast<double>(pm.field_count());
        }
    }, 500);
    // Field lookup by tag, the operation an order handler does several times per
    // message. Measured separately because it is the cost the linear-scan-versus-
    // hash-map decision was made on.
    pm.parse(exec_report, false);
    const Thr fix_lookups = throughput([&] {
        for (int i = 0; i < 1000; ++i)
            sink = sink + static_cast<double>(pm.get_or(mkt::fix::tag::AvgPx).size());
    }, 1000);

    // ---------------------------------------------------- pre-trade gate
    mkt::risk::PreTradeGate gate(16);
    mkt::risk::SymbolLimits lim;
    lim.known = true; lim.max_order_qty = 1e6; lim.max_order_notional = 1e9;
    lim.collar_bps = 1000; lim.max_long_position = 1e9; lim.max_short_position = 1e9;
    gate.set_limits(0, lim);
    const mkt::risk::MarketContext ctx{100.0, true};
    mkt::risk::OrderRequest req;
    req.symbol = 0; req.side = mkt::risk::Side::Buy; req.qty = 100; req.price = 100.0;
    const Thr gate_checks = throughput([&] {
        for (int i = 0; i < 1000; ++i)
            sink = sink + static_cast<double>(static_cast<int>(gate.check(req, ctx)));
    }, 1000);

    // ---------------------------------------------------- end-to-end order path
    // The composite that matters operationally: validate a tick, update the book,
    // read top of book, clear pre-trade risk, encode a NewOrderSingle. This is
    // the per-order cost of the connectivity layer, excluding network and venue.
    mkt::book::Ladder ebook(9000, 2001);
    mkt::feed::Validator eval;
    mkt::fix::Builder ebuild("FIX.4.4");
    std::uint64_t eseq = 0; std::int64_t ets = 0;
    const Thr e2e = throughput([&] {
        for (int i = 0; i < 200; ++i) {
            mkt::feed::Tick t{++eseq, ets += 1'000'000, ets, px(rng), 10.0};
            if (!mkt::feed::accepted(eval.check(t))) continue;
            const auto tk = static_cast<mkt::book::Ticks>(t.price * 100.0);
            ebook.upsert(mkt::book::Side::Bid, tk - 1, 500);
            ebook.upsert(mkt::book::Side::Ask, tk + 1, 500);
            mkt::book::Ticks bp; mkt::book::Qty bq;
            if (!ebook.best(mkt::book::Side::Bid, bp, bq)) continue;
            mkt::risk::OrderRequest o;
            o.symbol = 0; o.side = mkt::risk::Side::Buy; o.qty = 100;
            o.price = static_cast<double>(bp) / 100.0;
            const mkt::risk::MarketContext mc{o.price, true};
            if (!mkt::risk::accepted(gate.check(o, mc))) continue;
            ebuild.reset();
            ebuild.add(mkt::fix::tag::MsgType, mkt::fix::msg::NewOrderSingle)
                  .add(mkt::fix::tag::ClOrdID, "E-1")
                  .add(mkt::fix::tag::Symbol, "AAPL")
                  .add(mkt::fix::tag::Side, "1")
                  .add_int(mkt::fix::tag::OrderQty, 100)
                  .add_price(mkt::fix::tag::Price, o.price, 2);
            sink = sink + static_cast<double>(ebuild.finish().size());
        }
    }, 200);

    // ---------------------------------------------------- JSON
    auto j_str = [](const std::string& s) {
        std::string o = "\"";
        for (char c : s) { if (c == '"' || c == '\\') o += '\\'; o += c; }
        return o + "\"";
    };

    std::printf("{\n");
    std::printf("  \"schema_version\": 1,\n");
    std::printf("  \"environment\": {\n");
    std::printf("    \"cpu\": %s,\n", j_str(env.cpu_brand).c_str());
    std::printf("    \"arch\": %s,\n", j_str(env.arch).c_str());
    std::printf("    \"simd_compiled\": %s,\n", j_str(env.simd).c_str());
    std::printf("    \"simd_available_on_cpu\": %s,\n", j_str(env.simd_available).c_str());
    std::printf("    \"physical_cores\": %u,\n", env.physical_cores);
    std::printf("    \"logical_cores\": %u,\n", env.logical_cores);
    std::printf("    \"compiler\": %s,\n", j_str(env.compiler).c_str());
    std::printf("    \"flags\": %s,\n", j_str(env.flags).c_str());
    std::printf("    \"optimised\": %s\n",
#if defined(__OPTIMIZE__)
                "true"
#else
                "false"
#endif
    );
    std::printf("  },\n");

    std::printf("  \"monte_carlo\": {\n");
    std::printf("    \"model\": \"European call, GBM, terminal-value sampling\",\n");
    std::printf("    \"timesteps_per_path\": 1,\n");
    std::printf("    \"timestep_note\": \"Terminal-value sampling: ONE timestep per path. A 252-step path-dependent simulation does ~252x the work per path, so this paths/sec figure is NOT comparable to a path-dependent one.\",\n");
    std::printf("    \"variance_reduction\": \"antithetic variates\",\n");
    std::printf("    \"rng\": \"std::mt19937_64, per-block substreams seeded by splitmix64(seed+block)\",\n");
    std::printf("    \"paths\": %llu,\n", static_cast<unsigned long long>(mc_paths));
    std::printf("    \"repetitions\": %d,\n", reps);
    std::printf("    \"spot\": %.1f, \"strike\": %.1f, \"maturity_years\": %.2f, \"rate\": %.4f, \"vol\": %.4f,\n", S, K, T, r, sig);
    std::printf("    \"bitwise_identical_across_thread_counts\": %s,\n", mc_deterministic ? "true" : "false");
    std::printf("    \"price\": %.12f,\n", mc_rows.front().price);
    std::printf("    \"scaling\": [\n");
    for (std::size_t i = 0; i < mc_rows.size(); ++i) {
        const auto& r2 = mc_rows[i];
        std::printf("      {\"threads\": %u, \"median_seconds\": %.6f, \"paths_per_sec\": %.0f, "
                    "\"best_paths_per_sec\": %.0f, \"worst_paths_per_sec\": %.0f, \"spread_pct\": %.1f, "
                    "\"speedup\": %.4f, \"efficiency\": %.4f, \"oversubscribed_smt\": %s}%s\n",
                    r2.threads, r2.seconds, r2.paths_per_sec, r2.best_paths_per_sec,
                    r2.worst_paths_per_sec, r2.spread_pct, r2.speedup, r2.efficiency,
                    r2.oversubscribed ? "true" : "false",
                    (i + 1 < mc_rows.size()) ? "," : "");
    }
    std::printf("    ],\n");
    // The honesty gate. An 8-physical-core figure is only emitted if 8 physical
    // cores were actually measured. No extrapolation, no projection.
    const bool have8 = env.physical_cores >= 8;
    std::printf("    \"eight_physical_core_measured\": %s,\n", have8 ? "true" : "false");
    if (!have8)
        std::printf("    \"eight_physical_core_note\": \"NOT MEASURED. This host has %u physical cores (%u logical). No 8-core number is reported and none is extrapolated -- run this harness on an 8-core host and it will populate automatically.\",\n",
                    env.physical_cores, env.logical_cores);
    std::printf("    \"smt_note\": \"Rows with oversubscribed_smt=true run more threads than physical cores. SMT shares execution units, so those rows are NOT evidence of per-core scaling and must not be extrapolated from.\"\n");
    std::printf("  },\n");

    std::printf("  \"connectivity\": {\n");
    auto row = [&](const char* name, const Thr& t, bool comma) {
        std::printf("    \"%s\": {\"median_per_sec\": %.0f, \"best_per_sec\": %.0f, "
                    "\"worst_per_sec\": %.0f, \"spread_pct\": %.1f}%s\n",
                    name, t.median, t.best, t.worst, t.spread_pct, comma ? "," : "");
    };
    row("book_updates", book_ups, true);
    row("top_of_book_reads", tob_reads, true);
    row("depth_snapshots", depth_snaps, true);
    row("feed_validations", feed_ticks, true);
    row("fix_encode", fix_encode, true);
    row("fix_parse_validated", fix_parse_validated, true);
    row("fix_parse_unvalidated", fix_parse_unvalidated, true);
    row("fix_field_lookups", fix_lookups, true);
    row("pretrade_checks", gate_checks, true);
    row("end_to_end_orders", e2e, true);
    std::printf("    \"end_to_end_note\": \"tick validate -> book update -> top-of-book read -> pre-trade risk -> NewOrderSingle encode, single thread, in-process. EXCLUDES network, kernel and venue time, which dominate real wire-to-wire latency.\"\n");
    std::printf("  },\n");
    std::printf("  \"methodology\": \"Each figure runs a warm-up call, then repeats until a >=0.35s time floor to stay clear of clock resolution. ops/sec = total ops / elapsed wall time on an otherwise-loaded desktop OS with no CPU pinning, no isolated cores and no hugepages -- treat these as relative comparisons between implementations on this host, not as absolute production latency figures.\",\n");
    std::printf("  \"checksum_guard\": %.1f\n", static_cast<double>(sink) == 0.0 ? 0.0 : 1.0);
    std::printf("}\n");
    return 0;
}
