// spsc_ring.hpp — single-producer / single-consumer lock-free ring buffer.
//
// The handoff a feed handler needs: one thread decoding the wire, one thread
// acting on it, and nothing between them that can block. A mutex here is not
// merely slower on average — it introduces an unbounded tail, because a producer
// descheduled while holding the lock stops the consumer for a whole scheduling
// quantum. That is the p99.9 nobody can explain.
//
// FOUR THINGS MAKE THIS CORRECT AND FAST, and all four are easy to omit:
//
// 1. CACHE-LINE SEPARATION OF THE INDICES. head_ and tail_ are written by
//    DIFFERENT threads. Sharing a cache line means every producer write
//    invalidates the consumer's copy and vice versa — false sharing, which turns
//    two independent atomics into a ping-pong over the interconnect and can cost
//    an order of magnitude. Each index gets its own line, and the benchmark in
//    lat_bench.cpp measures the difference rather than asserting it.
//
// 2. ACQUIRE/RELEASE, NOT SEQUENTIALLY CONSISTENT. seq_cst costs a full barrier
//    on x86 for a guarantee this pattern does not need. The producer must ensure
//    the SLOT WRITE is visible before the index that publishes it (release), and
//    the consumer must not read the slot before observing that index (acquire).
//    That pairing is exactly sufficient, and relaxed alone is not — the compiler
//    or CPU may reorder the slot write past the index update, and the consumer
//    reads a slot the producer has not filled.
//
// 3. CACHED OPPOSITE INDEX. The producer needs to know the consumer has made
//    room, but loading the consumer's atomic on every push is a guaranteed cache
//    miss on a line the other core owns. Caching it and only re-reading when the
//    cached value says "full" removes that miss from the common path entirely.
//
// 4. POWER-OF-TWO CAPACITY. Masking replaces a modulo. A hardware divide is tens
//    of cycles and this is the hottest arithmetic in the structure.
//
// BOUNDED, NEVER GROWING. A queue that grows under load converts backpressure
// into latency and then into an out-of-memory kill. Full means full: push()
// returns false, and the caller decides — drop, conflate, or count it. Deciding
// that at the call site is the whole point of surfacing it.
//
// ONE PRODUCER, ONE CONSUMER. Not enforced at compile time and not checked at
// runtime, because a check would cost more than the operation. Two producers
// corrupt it silently. That constraint is the price of the performance.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <new>
#include <vector>

namespace mkt::lat {

// std::hardware_destructive_interference_size is the right answer but is not
// available everywhere; 64 is correct for every x86-64 and arm64 target here.
#if defined(__cpp_lib_hardware_interference_size)
inline constexpr std::size_t kCacheLine = std::hardware_destructive_interference_size;
#else
inline constexpr std::size_t kCacheLine = 64;
#endif

template <class T>
class SpscRing {
  public:
    // `capacity` is rounded UP to a power of two so the index wrap is a mask.
    explicit SpscRing(std::size_t capacity)
        : mask_(round_up_pow2(capacity) - 1), buf_(round_up_pow2(capacity)) {}

    std::size_t capacity() const noexcept { return mask_ + 1; }

    // Producer side only. False when full — the caller decides what that means.
    bool push(const T& v) noexcept {
        const std::size_t head = head_.idx.load(std::memory_order_relaxed);
        const std::size_t next = (head + 1) & mask_;
        if (next == cached_tail_) {
            // Only now pay for the consumer's cache line.
            cached_tail_ = tail_.idx.load(std::memory_order_acquire);
            if (next == cached_tail_) return false;      // genuinely full
        }
        buf_[head] = v;
        // Release: the slot write above must be visible before this index is.
        head_.idx.store(next, std::memory_order_release);
        return true;
    }

    // Consumer side only. False when empty.
    bool pop(T& out) noexcept {
        const std::size_t tail = tail_.idx.load(std::memory_order_relaxed);
        if (tail == cached_head_) {
            cached_head_ = head_.idx.load(std::memory_order_acquire);
            if (tail == cached_head_) return false;      // genuinely empty
        }
        out = buf_[tail];
        tail_.idx.store((tail + 1) & mask_, std::memory_order_release);
        return true;
    }

    // Approximate: both indices move underneath this. Useful for a depth gauge,
    // never for a correctness decision.
    std::size_t size_approx() const noexcept {
        const std::size_t h = head_.idx.load(std::memory_order_acquire);
        const std::size_t t = tail_.idx.load(std::memory_order_acquire);
        return (h - t) & mask_;
    }

    bool empty_approx() const noexcept {
        return head_.idx.load(std::memory_order_acquire) ==
               tail_.idx.load(std::memory_order_acquire);
    }

  private:
    static std::size_t round_up_pow2(std::size_t n) noexcept {
        if (n < 2) return 2;
        --n;
        for (std::size_t s = 1; s < sizeof(std::size_t) * 8; s <<= 1) n |= n >> s;
        return n + 1;
    }

    // Each index alone on its own cache line. Without alignas these two sit in
    // one line and the two threads fight over it on every operation.
    struct alignas(kCacheLine) Index {
        std::atomic<std::size_t> idx{0};
        char pad[kCacheLine - sizeof(std::atomic<std::size_t>)]{};
    };

    const std::size_t mask_;
    std::vector<T> buf_;                 // allocated once, never resized
    alignas(kCacheLine) Index head_{};   // producer writes
    alignas(kCacheLine) Index tail_{};   // consumer writes
    // Each thread's private copy of the other's index; no sharing, so these live
    // beside the index their owner writes rather than on a contended line.
    alignas(kCacheLine) std::size_t cached_tail_{0};   // producer's view
    alignas(kCacheLine) std::size_t cached_head_{0};   // consumer's view
};

// The deliberately-wrong variant, kept ONLY so the benchmark can measure what
// false sharing actually costs on this machine. Identical logic, indices packed
// into one cache line. Never use it for anything.
template <class T>
class SpscRingFalseShared {
  public:
    explicit SpscRingFalseShared(std::size_t capacity)
        : mask_(round_up_pow2(capacity) - 1), buf_(round_up_pow2(capacity)) {}

    bool push(const T& v) noexcept {
        const std::size_t head = head_.load(std::memory_order_relaxed);
        const std::size_t next = (head + 1) & mask_;
        if (next == tail_.load(std::memory_order_acquire)) return false;
        buf_[head] = v;
        head_.store(next, std::memory_order_release);
        return true;
    }

    bool pop(T& out) noexcept {
        const std::size_t tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire)) return false;
        out = buf_[tail];
        tail_.store((tail + 1) & mask_, std::memory_order_release);
        return true;
    }

  private:
    static std::size_t round_up_pow2(std::size_t n) noexcept {
        if (n < 2) return 2;
        --n;
        for (std::size_t s = 1; s < sizeof(std::size_t) * 8; s <<= 1) n |= n >> s;
        return n + 1;
    }
    const std::size_t mask_;
    std::vector<T> buf_;
    // Adjacent on purpose: these two land in the same 64-byte line.
    std::atomic<std::size_t> head_{0};
    std::atomic<std::size_t> tail_{0};
};

}  // namespace mkt::lat
