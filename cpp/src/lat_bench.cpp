// lat_bench.cpp — per-operation LATENCY DISTRIBUTION for the hot path.
//
// Companion to mkt_bench.cpp, which measures throughput. This measures the tail,
// because they answer different questions and only one of them decides whether a
// trading path is usable.
//
// Everything this prints is per-operation nanoseconds: p50/p90/p99/p99.9/p99.99,
// plus the p99/p50 ratio, which is the single most useful number here — it says
// how far the bad case is from the typical one, and a path with a ratio of 2 is a
// different kind of system from one with a ratio of 200 even if both share a
// median.
//
// THE CLOCK'S OWN COST IS MEASURED FIRST and printed alongside. On a general
// -purpose OS, reading a monotonic clock costs tens of nanoseconds — the same
// order as the operations being timed. A figure at or below that floor is not a
// measurement of the code, and the output says so rather than letting the number
// be quoted.
//
// WHAT THESE NUMBERS ARE NOT. In-process, single machine, general-purpose kernel,
// no CPU pinning, no isolated cores, no tickless mode, no hugepages, no
// kernel-bypass NIC. In a real low-latency deployment all of those are controlled
// and the p99.9 here is dominated by the SCHEDULER, not by the code under test.
// The honest reading is: the p50 tells you about the code, the tail tells you
// about this host.
#include "mkt/hft/affinity.hpp"
#include "mkt/hft/tsc.hpp"
#include "mkt/lat/histogram.hpp"
#include "mkt/lat/spsc_ring.hpp"
#include "mkt/book/ladder.hpp"
#include "mkt/feed/validator.hpp"
#include "mkt/fix/message.hpp"
#include "mkt/risk/pretrade.hpp"

#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <queue>
#include <condition_variable>
#include <random>
#include <string>
#include <thread>
#include <vector>

using namespace mkt;
using lat::Histogram;
using lat::now_ns;
using lat::Stats;

static std::vector<lat::Named> results;

// Defeats dead-code elimination. Not volatile: compound assignment to a volatile
// is deprecated in C++20, and it was never needed -- this global is PRINTED at
// the end, so its value is observable and the compiler cannot elide the work that
// produces it. A benchmark whose result is discarded can be optimised away
// entirely, and then it measures an empty loop.
double g_sink = 0;

// Timed with RDTSC where available. clock_gettime costs ~43ns on this host,
// which is MORE than several of the operations being measured -- three of the
// twelve results previously sat at that floor and could not be quoted. RDTSC
// with proper fencing costs ~13ns, which is what makes them measurable at all.
template <class Setup, class Op>
static void measure(const char* name, std::size_t iters, Setup setup, Op op,
                    const char* note = "") {
    Histogram h(iters);
    setup();
    // Untimed warm-up: the first iterations pay cold caches, page faults and an
    // untrained branch predictor. Including them measures start-up.
    for (std::size_t i = 0; i < iters / 20 + 1000; ++i) op(i);

    const auto& clk = mkt::hft::TscClock::instance();
    if (clk.usable()) {
        for (std::size_t i = 0; i < iters; ++i) {
            const std::uint64_t c0 = mkt::hft::tsc_begin();
            op(i);
            const std::uint64_t c1 = mkt::hft::tsc_end();
            h.record(static_cast<std::uint64_t>(clk.to_ns(c1 - c0)));
        }
    } else {
        for (std::size_t i = 0; i < iters; ++i) {
            const auto t0 = now_ns();
            op(i);
            const auto t1 = now_ns();
            h.record(t1 - t0);
        }
    }
    results.push_back({name, h.compute(), note});
}

// The TSC's own cost, measured the same way the operations are.
static Stats tsc_overhead(std::size_t n = 200000) {
    Histogram h(n);
    const auto& clk = mkt::hft::TscClock::instance();
    for (std::size_t i = 0; i < 2000; ++i) { (void)mkt::hft::tsc_begin(); }
    for (std::size_t i = 0; i < n; ++i) {
        const std::uint64_t a = mkt::hft::tsc_begin();
        const std::uint64_t b = mkt::hft::tsc_end();
        h.record(static_cast<std::uint64_t>(clk.to_ns(b - a)));
    }
    h.drop_warmup(n / 100);
    return h.compute();
}

int main(int argc, char** argv) {
    const std::size_t iters = (argc > 1) ? std::strtoull(argv[1], nullptr, 10) : 300000;

    // ------------------------------------------------- measurement floor
    const Stats clk = lat::clock_overhead(100000);
    const auto& tscclk = mkt::hft::TscClock::instance();
    const Stats tsc = tscclk.usable() ? tsc_overhead(100000) : clk;
    // Whichever timer is actually in use sets the floor results are judged against.
    const Stats floor = tscclk.usable() ? tsc : clk;
    const auto topo = mkt::hft::topology();
    const auto pin = mkt::hft::pin_current_thread(0);

    // ------------------------------------------------- order book
    {
        static book::Ladder b(1, 50000);
        static std::mt19937_64 rng(42);
        static std::uniform_int_distribution<int> off(0, 30);
        static std::uniform_int_distribution<int> qd(1, 900);
        measure("book.upsert", iters, [] {},
                [](std::size_t i) {
                    const auto side = (i & 1) ? book::Side::Ask : book::Side::Bid;
                    const book::Ticks px = (i & 1) ? 10010 + off(rng) : 9990 - off(rng);
                    b.upsert(side, px, qd(rng));
                },
                "one level update, the operation a feed handler performs most");
    }
    {
        static book::Ladder b(1, 50000);
        for (int i = 0; i < 40; ++i) {
            b.upsert(book::Side::Bid, 9990 - i, 100 + i);
            b.upsert(book::Side::Ask, 10010 + i, 100 + i);
        }
        measure("book.top_of_book", iters, [] {},
                [](std::size_t) { g_sink += b.micro_price_ticks(); },
                "the most frequently called read in the system");

        static std::array<book::Level, 10> depth{};
        measure("book.depth_10", iters, [] {},
                [](std::size_t) { g_sink += static_cast<double>(b.depth(book::Side::Bid, depth)); },
                "10-level snapshot into caller storage, no allocation");
    }

    // ------------------------------------------------- feed validation
    {
        static feed::Validator v;
        static std::mt19937_64 rng(7);
        static std::uniform_real_distribution<double> px(99.9, 100.1);
        static std::uint64_t seq = 0;
        static std::int64_t ts = 0;
        measure("feed.validate", iters, [] {},
                [](std::size_t) {
                    ts += 1000000;
                    (void)v.check(feed::Tick{++seq, ts, ts, px(rng), 10.0});
                },
                "sequence + timestamp + band + EWMA outlier check");
    }

    // ------------------------------------------------- pre-trade risk
    {
        static risk::PreTradeGate gate(4);
        risk::SymbolLimits lim;
        lim.known = true; lim.max_order_qty = 1e9; lim.max_order_notional = 1e12;
        lim.collar_bps = 1000; lim.max_long_position = 1e12; lim.max_short_position = 1e12;
        gate.set_limits(0, lim);
        static const risk::MarketContext ctx{100.0, true};
        static risk::OrderRequest req;
        req.symbol = 0; req.side = risk::Side::Buy; req.qty = 100; req.price = 100.0;
        measure("risk.pretrade_check", iters, [] {},
                [](std::size_t) { g_sink += static_cast<double>(gate.check(req, ctx)); },
                "kill switch -> limits -> collar -> position, full gate");
    }

    // ------------------------------------------------- FIX
    {
        static fix::Builder b("FIX.4.4");
        measure("fix.encode_nos", iters / 2, [] {},
                [](std::size_t i) {
                    b.reset();
                    b.add(fix::tag::MsgType, fix::msg::NewOrderSingle)
                     .add(fix::tag::SenderCompID, "QGLABS")
                     .add(fix::tag::TargetCompID, "SIMVENUE")
                     .add_int(fix::tag::MsgSeqNum, static_cast<int>(i))
                     .add(fix::tag::SendingTime, "20260929-09:30:00.000")
                     .add(fix::tag::ClOrdID, "ORD-000123")
                     .add(fix::tag::Symbol, "AAPL")
                     .add(fix::tag::Side, "1")
                     .add_int(fix::tag::OrderQty, 500)
                     .add(fix::tag::OrdType, "2")
                     .add_price(fix::tag::Price, 187.25, 2);
                    g_sink += static_cast<double>(b.finish().size());
                },
                "NewOrderSingle, 11 fields, BodyLength + CheckSum computed");

        fix::Builder eb("FIX.4.4");
        eb.add(fix::tag::MsgType, fix::msg::ExecutionReport)
          .add(fix::tag::SenderCompID, "SIMVENUE").add(fix::tag::TargetCompID, "QGLABS")
          .add_int(fix::tag::MsgSeqNum, 12345).add(fix::tag::SendingTime, "20260929-09:30:00.000")
          .add(fix::tag::OrderID, "V-99887766").add(fix::tag::ExecID, "E-1122334455")
          .add(fix::tag::ExecType, "F").add(fix::tag::OrdStatus, "2")
          .add(fix::tag::Symbol, "AAPL").add(fix::tag::Side, "1")
          .add_int(fix::tag::LastQty, 500).add_price(fix::tag::LastPx, 187.25, 2)
          .add_int(fix::tag::CumQty, 500).add_price(fix::tag::AvgPx, 187.25, 2);
        static const std::string wire(eb.finish());
        static fix::Message m;
        measure("fix.parse_validated", iters / 2, [] {},
                [](std::size_t) { m.parse(wire, true); g_sink += static_cast<double>(m.field_count()); },
                "full validation: BodyLength + CheckSum, what a real engine pays");
        measure("fix.parse_trusted", iters / 2, [] {},
                [](std::size_t) { m.parse(wire, false); g_sink += static_cast<double>(m.field_count()); },
                "framing trusted -- only legitimate replaying a trusted log");
    }

    // ------------------------------------------------- tick-to-order
    // The composite that matters: a tick arrives and an order leaves.
    {
        static book::Ladder b(1, 50000);
        static feed::Validator v;
        static fix::Builder bld("FIX.4.4");
        static risk::PreTradeGate gate(4);
        risk::SymbolLimits lim;
        lim.known = true; lim.max_order_qty = 1e9; lim.max_order_notional = 1e12;
        lim.collar_bps = 5000; lim.max_long_position = 1e12; lim.max_short_position = 1e12;
        gate.set_limits(0, lim);
        static std::mt19937_64 rng(11);
        static std::uniform_real_distribution<double> px(99.9, 100.1);
        static std::uint64_t seq = 0;
        static std::int64_t ts = 0;
        measure("tick_to_order", iters / 2, [] {},
                [](std::size_t) {
                    ts += 1000000;
                    const double p = px(rng);
                    if (!feed::accepted(v.check(feed::Tick{++seq, ts, ts, p, 10.0}))) return;
                    const auto tk = static_cast<book::Ticks>(p * 100.0);
                    b.upsert(book::Side::Bid, tk - 1, 500);
                    b.upsert(book::Side::Ask, tk + 1, 500);
                    book::Ticks bp; book::Qty bq;
                    if (!b.best(book::Side::Bid, bp, bq)) return;
                    risk::OrderRequest o;
                    o.symbol = 0; o.side = risk::Side::Buy; o.qty = 100;
                    o.price = static_cast<double>(bp) / 100.0;
                    if (!risk::accepted(gate.check(o, risk::MarketContext{o.price, true}))) return;
                    bld.reset();
                    bld.add(fix::tag::MsgType, fix::msg::NewOrderSingle)
                       .add(fix::tag::ClOrdID, "E-1").add(fix::tag::Symbol, "AAPL")
                       .add(fix::tag::Side, "1").add_int(fix::tag::OrderQty, 100)
                       .add_price(fix::tag::Price, o.price, 2);
                    g_sink += static_cast<double>(bld.finish().size());
                },
                "validate -> book -> top-of-book -> pre-trade -> encode. NO network, NO kernel, NO venue.");
    }

    // ------------------------------------------------- cross-thread handoff
    // Producer stamps a timestamp into the slot; consumer reads it and records
    // the delta. This is the latency a feed handler adds by handing off to a
    // strategy thread, and the reason a mutex is the wrong primitive here.
    struct Msg { std::uint64_t t_ns; std::uint64_t seq; };
    const std::size_t handoff_n = 50000;

    // MEASUREMENT NOTE, learned the hard way. The first version of this ran the
    // producer flat out and timestamped at push. That measures QUEUEING DELAY,
    // not handoff latency: the producer fills a 4096-slot ring faster than the
    // consumer drains it, so every message waits behind thousands of
    // predecessors. It reported ~29us for a lock-free ring and ~18ms for the
    // mutex queue -- both of which are real numbers about a saturated pipe and
    // neither of which is what "how long does a handoff take" means.
    //
    // Handoff latency is only defined when the queue is NOT backed up. So the
    // producer now waits for the ring to drain before sending the next message.
    // Each sample is then one message crossing an empty queue, which is the cost
    // the structure actually adds.
    auto run_spsc = [&](auto& ring, const char* name, const char* note) {
        Histogram h(handoff_n);
        std::atomic<bool> stop{false};
        std::atomic<std::size_t> consumed{0};
        std::thread consumer([&] {
            Msg m{};
            while (!stop.load(std::memory_order_relaxed)) {
                if (ring.pop(m)) {
                    h.record(now_ns() - m.t_ns);
                    consumed.fetch_add(1, std::memory_order_release);
                }
            }
        });
        for (std::size_t i = 0; i < handoff_n; ++i) {
            const std::size_t before = consumed.load(std::memory_order_acquire);
            while (!ring.push(Msg{now_ns(), i})) {}
            // Wait for THIS message to be taken before sending the next, so the
            // queue is empty at every push and no sample includes queueing.
            while (consumed.load(std::memory_order_acquire) == before) {}
        }
        stop.store(true, std::memory_order_relaxed);
        consumer.join();
        h.drop_warmup(handoff_n / 20);
        results.push_back({name, h.compute(), note});
    };

    { lat::SpscRing<Msg> ring(4096);
      run_spsc(ring, "handoff.spsc_lockfree",
               "one message across an EMPTY queue. Cache-line separated indices, acquire/release, cached opposite index."); }
    { lat::SpscRingFalseShared<Msg> ring(4096);
      run_spsc(ring, "handoff.spsc_false_shared",
               "IDENTICAL logic, indices packed into ONE cache line -- the measured cost of false sharing."); }

    // The blocking baseline, paced identically so the comparison is like for like.
    {
        Histogram h(handoff_n);
        std::queue<Msg> q;
        std::mutex mu;
        std::condition_variable cv;
        std::atomic<bool> stop{false};
        std::atomic<std::size_t> consumed{0};
        std::thread consumer([&] {
            while (!stop.load(std::memory_order_relaxed)) {
                std::unique_lock<std::mutex> lk(mu);
                cv.wait_for(lk, std::chrono::milliseconds(5),
                            [&] { return !q.empty() || stop.load(std::memory_order_relaxed); });
                while (!q.empty()) {
                    const Msg m = q.front(); q.pop();
                    lk.unlock();
                    h.record(now_ns() - m.t_ns);
                    consumed.fetch_add(1, std::memory_order_release);
                    lk.lock();
                }
            }
        });
        for (std::size_t i = 0; i < handoff_n; ++i) {
            const std::size_t before = consumed.load(std::memory_order_acquire);
            { std::lock_guard<std::mutex> lk(mu); q.push(Msg{now_ns(), i}); }
            cv.notify_one();
            while (consumed.load(std::memory_order_acquire) == before) {}
        }
        stop.store(true, std::memory_order_relaxed);
        cv.notify_all();
        consumer.join();
        h.drop_warmup(handoff_n / 20);
        results.push_back({"handoff.mutex_condvar", h.compute(),
                           "std::queue + mutex + condition_variable, paced identically. The wake-up is a syscall and the tail is a scheduling quantum."});
    }

    // ------------------------------------------------- JSON
    auto esc = [](const std::string& s) {
        std::string o = "\"";
        for (char c : s) { if (c == '"' || c == '\\') o += '\\'; o += c; }
        return o + "\"";
    };

    std::printf("{\n  \"schema_version\": 1,\n  \"kind\": \"latency_distribution\",\n");
    std::printf("  \"units\": \"nanoseconds per operation\",\n");
    std::printf("  \"iterations\": %zu,\n", iters);
    std::printf("  \"timer\": {\n");
    std::printf("    \"source\": %s,\n", esc(tscclk.source()).c_str());
    std::printf("    \"tsc_usable\": %s,\n", tscclk.usable() ? "true" : "false");
    std::printf("    \"calibrated_ghz\": %.4f,\n", tscclk.ghz());
    std::printf("    \"clock_gettime_overhead_ns\": {\"p50\": %.0f, \"p99\": %.0f},\n", clk.p50, clk.p99);
    std::printf("    \"tsc_overhead_ns\": {\"p50\": %.0f, \"p99\": %.0f},\n", tsc.p50, tsc.p99);
    std::printf("    \"floor_ns\": %.0f,\n", floor.p50);
    std::printf("    \"note\": \"RDTSC (LFENCE+RDTSC to open, RDTSCP to close) replaces clock_gettime because the latter costs more than several of the operations being measured. Any figure at or below floor_ns is NOT a measurement of the code -- it is the timer.\"\n");
    std::printf("  },\n");
    std::printf("  \"cpu_placement\": {\n");
    std::printf("    \"logical_cpus\": %u, \"physical_cores\": %u, \"numa_nodes\": %u,\n",
                topo.logical_cpus, topo.physical_cores, topo.numa_nodes);
    std::printf("    \"hard_pinning_available\": %s,\n", topo.hard_pinning ? "true" : "false");
    std::printf("    \"pin_result\": %s,\n", esc(mkt::hft::to_string(pin)).c_str());
    std::printf("    \"note\": %s\n", esc(topo.affinity_note).c_str());
    std::printf("  },\n");
    std::printf("  \"measurements\": [\n");
    for (std::size_t i = 0; i < results.size(); ++i) {
        const auto& r = results[i];
        const auto& s = r.stats;
        // Flag it here rather than leaving a reader to compare against the
        // floor themselves. An operation whose p50 sits within 25% of the
        // timer's own cost is not being measured -- the timer is.
        const bool at_floor = s.p50 <= floor.p50 * 1.25;
        std::printf("    {\"name\": %s, \"n\": %zu, \"min\": %.0f, \"p50\": %.0f, "
                    "\"p90\": %.0f, \"p99\": %.0f, \"p999\": %.0f, \"p9999\": %.0f, "
                    "\"max\": %.0f, \"mean\": %.1f, \"stddev\": %.1f, "
                    "\"p99_over_p50\": %.2f, \"p999_over_p50\": %.2f, "
                    "\"at_timer_floor\": %s, \"note\": %s}%s\n",
                    esc(r.name).c_str(), s.n, s.min, s.p50, s.p90, s.p99, s.p999,
                    s.p9999, s.max, s.mean, s.stddev, s.p99_over_p50, s.p999_over_p50,
                    at_floor ? "true" : "false",
                    esc(r.note).c_str(), (i + 1 < results.size()) ? "," : "");
    }
    std::printf("  ],\n");
    std::printf("  \"environment_note\": \"In-process, single machine, general-purpose kernel. NO CPU pinning, NO isolated cores, NO tickless mode, NO hugepages, NO kernel-bypass networking. A real low-latency deployment controls all of those. Read it this way: the p50 describes the code, the tail describes this host.\",\n");
    std::printf("  \"tail_note\": \"p99_over_p50 is the number to read first. A ratio near 1 is a predictable path; a large one means the median is not describing the system's behaviour.\",\n  \"sink_guard\": %.0f\n}\n", g_sink == 0.0 ? 0.0 : 1.0);
    return 0;
}
