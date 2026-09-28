// parallel.hpp — reusable parallel primitives with DETERMINISTIC results.
//
// The one property that makes threaded quant code trustworthy: the answer
// must not depend on the thread count. We guarantee that two ways:
//   * per-block RNG seeding derived only from (base_seed, block_index), so
//     which thread runs a block is irrelevant;
//   * partial results written to a fixed slot per block and reduced in
//     block-index order, so floating-point summation order is fixed
//     (fp addition isn't associative — order matters for bitwise parity).
//
// Result: run on 1 thread or 64, you get the identical number. That's the
// difference between a demo and something a risk desk will sign off on.
//
// Header-only, C++20, std::thread only (no OpenMP dependency).
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <thread>
#include <vector>

namespace quant::par {

// SplitMix64 — fast, high-quality seeding for per-block RNG substreams.
inline std::uint64_t splitmix64(std::uint64_t x) noexcept {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

inline unsigned hardware_threads(unsigned requested = 0) noexcept {
    if (requested) return requested;
    const unsigned hc = std::thread::hardware_concurrency();
    return hc ? hc : 1u;
}

// Dynamic parallel-for over [0, n): calls body(i). Work is grabbed via an
// atomic counter (load-balances uneven work). body MUST write only to its
// own slot i to stay deterministic; do the reduction afterwards in order.
template <class Body>
void parallel_for(std::size_t n, Body body, unsigned n_threads = 0) {
    const unsigned nt = hardware_threads(n_threads);
    if (nt <= 1 || n <= 1) {
        for (std::size_t i = 0; i < n; ++i) body(i);
        return;
    }
    std::atomic<std::size_t> next{0};
    std::vector<std::thread> pool;
    pool.reserve(nt);
    for (unsigned t = 0; t < nt; ++t)
        pool.emplace_back([&] {
            for (std::size_t i = next.fetch_add(1, std::memory_order_relaxed);
                 i < n; i = next.fetch_add(1, std::memory_order_relaxed))
                body(i);
        });
    for (auto& th : pool) th.join();
}

// Deterministic block-parallel reduction. Splits `n_items` into blocks of
// `block`, runs `block_fn(block_index, lo, hi) -> T` per block in parallel,
// then folds the per-block results with `combine` in BLOCK-INDEX ORDER.
template <class T, class BlockFn, class Combine>
T block_reduce(std::uint64_t n_items, std::uint64_t block, BlockFn block_fn,
               Combine combine, T identity, unsigned n_threads = 0) {
    const std::uint64_t n_blocks = (n_items + block - 1) / block;
    std::vector<T> partial(static_cast<std::size_t>(n_blocks), identity);
    parallel_for(static_cast<std::size_t>(n_blocks), [&](std::size_t b) {
        const std::uint64_t lo = static_cast<std::uint64_t>(b) * block;
        const std::uint64_t hi = std::min(lo + block, n_items);
        partial[b] = block_fn(static_cast<std::uint64_t>(b), lo, hi);
    }, n_threads);
    T acc = identity;                          // fold in fixed order
    for (const T& p : partial) acc = combine(acc, p);
    return acc;
}

}  // namespace quant::par
