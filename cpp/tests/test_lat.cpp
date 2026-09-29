// test_lat.cpp — SPSC ring and latency histogram.
//
// The ring is tested under REAL concurrency, not just single-threaded. A
// lock-free structure that passes only sequential tests is untested: the bugs
// live in the interleavings, and the memory-ordering pairing this relies on
// cannot fail without two threads running at once.
#include "microtest.hpp"
#include "mkt/lat/histogram.hpp"
#include "mkt/lat/spsc_ring.hpp"

#include <atomic>
#include <thread>
#include <vector>

using namespace mkt::lat;

// ------------------------------------------------------------------- ring
TEST(ring_push_pop_round_trip) {
    SpscRing<int> r(8);
    int v = 0;
    REQUIRE(!r.pop(v));                 // empty
    REQUIRE(r.push(42));
    REQUIRE(r.pop(v));
    REQUIRE(v == 42);
    REQUIRE(!r.pop(v));                 // empty again
}

TEST(ring_capacity_rounds_up_to_power_of_two) {
    REQUIRE(SpscRing<int>(5).capacity() == 8);
    REQUIRE(SpscRing<int>(8).capacity() == 8);
    REQUIRE(SpscRing<int>(9).capacity() == 16);
    REQUIRE(SpscRing<int>(1).capacity() == 2);
    REQUIRE(SpscRing<int>(0).capacity() == 2);
}

// Full means full. A queue that grows under load turns backpressure into an
// out-of-memory kill, so push() must refuse rather than expand.
TEST(ring_is_bounded_and_refuses_when_full) {
    SpscRing<int> r(4);                 // one slot is reserved to distinguish
    int pushed = 0;                     // full from empty, so usable = cap - 1
    while (r.push(pushed)) ++pushed;
    REQUIRE(pushed == 3);
    REQUIRE(!r.push(999));              // still refuses, repeatedly
    REQUIRE(!r.push(999));
    int v;
    REQUIRE(r.pop(v));                  // make room
    REQUIRE(r.push(999));               // now it fits
}

TEST(ring_preserves_order) {
    SpscRing<int> r(64);
    for (int i = 0; i < 32; ++i) REQUIRE(r.push(i));
    for (int i = 0; i < 32; ++i) {
        int v = -1;
        REQUIRE(r.pop(v));
        REQUIRE(v == i);                // strict FIFO
    }
}

TEST(ring_wraps_around_many_times) {
    SpscRing<int> r(8);
    int expect = 0;
    for (int round = 0; round < 1000; ++round) {
        for (int i = 0; i < 5; ++i) REQUIRE(r.push(round * 5 + i));
        for (int i = 0; i < 5; ++i) {
            int v = -1;
            REQUIRE(r.pop(v));
            REQUIRE(v == expect++);
        }
    }
    REQUIRE(r.empty_approx());
}

// The test that matters: two real threads, and every item must arrive exactly
// once and in order. Ordering is what the release/acquire pairing guarantees;
// without it the consumer can read a slot the producer has not written.
TEST(ring_concurrent_transfer_is_lossless_and_ordered) {
    constexpr std::uint64_t N = 200000;
    SpscRing<std::uint64_t> r(1024);
    std::atomic<bool> failed{false};
    std::uint64_t received = 0;

    std::thread consumer([&] {
        std::uint64_t expect = 0, v = 0;
        while (expect < N) {
            if (r.pop(v)) {
                if (v != expect) { failed.store(true); return; }
                ++expect;
            }
        }
        received = expect;
    });

    for (std::uint64_t i = 0; i < N; ++i)
        while (!r.push(i)) { /* spin until the consumer drains */ }

    consumer.join();
    REQUIRE(!failed.load());
    REQUIRE(received == N);
}

TEST(ring_survives_a_slow_consumer) {
    // The producer must block on a full ring rather than dropping or growing.
    constexpr std::uint64_t N = 20000;
    SpscRing<std::uint64_t> r(16);      // deliberately tiny
    std::atomic<bool> bad{false};

    std::thread consumer([&] {
        std::uint64_t expect = 0, v = 0;
        while (expect < N) {
            if (r.pop(v)) {
                if (v != expect++) { bad.store(true); return; }
                // Occasionally stall, forcing the producer to back off.
                if (expect % 512 == 0) std::this_thread::yield();
            }
        }
    });
    for (std::uint64_t i = 0; i < N; ++i) while (!r.push(i)) {}
    consumer.join();
    REQUIRE(!bad.load());
}

TEST(ring_carries_structs_not_just_scalars) {
    struct Tick { std::uint64_t seq; double px; std::int64_t qty; };
    SpscRing<Tick> r(32);
    REQUIRE(r.push(Tick{7, 100.25, 500}));
    Tick t{};
    REQUIRE(r.pop(t));
    REQUIRE(t.seq == 7);
    CHECK_CLOSE(t.px, 100.25, 1e-12);
    REQUIRE(t.qty == 500);
}

TEST(ring_size_approx_tracks_depth) {
    SpscRing<int> r(64);
    REQUIRE(r.size_approx() == 0);
    for (int i = 0; i < 10; ++i) r.push(i);
    REQUIRE(r.size_approx() == 10);
    int v;
    for (int i = 0; i < 4; ++i) r.pop(v);
    REQUIRE(r.size_approx() == 6);
}

// The false-shared variant must be functionally identical -- it exists only so
// the benchmark can price the difference, and a broken one would make that
// comparison meaningless.
TEST(false_shared_variant_is_functionally_equivalent) {
    SpscRingFalseShared<int> r(16);
    for (int i = 0; i < 10; ++i) REQUIRE(r.push(i));
    for (int i = 0; i < 10; ++i) {
        int v = -1;
        REQUIRE(r.pop(v));
        REQUIRE(v == i);
    }
}

// -------------------------------------------------------------- histogram
TEST(histogram_percentiles_are_order_statistics) {
    Histogram h;
    for (std::uint64_t i = 1; i <= 1000; ++i) h.record(i);
    const auto s = h.compute();
    REQUIRE(s.n == 1000);
    CHECK_CLOSE(s.min, 1.0, 1e-9);
    CHECK_CLOSE(s.max, 1000.0, 1e-9);
    CHECK_CLOSE(s.p50, 501.0, 1.0);
    CHECK_CLOSE(s.p99, 991.0, 2.0);
    CHECK_CLOSE(s.mean, 500.5, 0.01);
}

// The whole reason this file exists: a mean cannot see a tail.
TEST(histogram_exposes_a_tail_the_mean_hides) {
    Histogram h;
    for (int i = 0; i < 999; ++i) h.record(200);      // 200ns typical
    h.record(50000);                                  // one 50us outlier
    const auto s = h.compute();
    // The mean barely moves -- it looks like noise.
    REQUIRE(s.mean < 250.0);
    CHECK_CLOSE(s.p50, 200.0, 1e-9);
    // The tail shows it plainly.
    CHECK_CLOSE(s.max, 50000.0, 1e-9);
    REQUIRE(s.p999 > 1000.0);
    REQUIRE(s.p999_over_p50 > 100.0);                 // the ratio screams
}

TEST(histogram_handles_empty_and_single) {
    Histogram h;
    REQUIRE(h.compute().n == 0);
    h.record(5);
    const auto s = h.compute();
    REQUIRE(s.n == 1);
    CHECK_CLOSE(s.p50, 5.0, 1e-9);
    CHECK_CLOSE(s.p9999, 5.0, 1e-9);
    CHECK_CLOSE(s.stddev, 0.0, 1e-9);
}

TEST(histogram_drop_warmup_removes_leading_samples) {
    Histogram h;
    for (std::uint64_t i = 0; i < 100; ++i) h.record(i);
    h.drop_warmup(10);
    REQUIRE(h.size() == 90);
    REQUIRE(h.samples().front() == 10);
    h.drop_warmup(1000);                 // more than exist
    REQUIRE(h.size() == 0);
}

TEST(clock_overhead_is_measured_not_assumed) {
    const auto s = clock_overhead(20000);
    REQUIRE(s.n > 0);
    REQUIRE(s.p50 >= 0.0);
    // A timer that appears to take zero nanoseconds would mean the resolution is
    // too coarse to measure anything at this scale; that must be visible.
    REQUIRE(s.max >= s.p50);
}

int main() {
    std::printf("=== SPSC ring + latency histogram tests ===\n");
    return microtest::run_all();
}
