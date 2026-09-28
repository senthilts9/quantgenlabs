// ladder.hpp — L2 aggregated price ladder for a single instrument.
//
// This is the data structure a feed handler updates on every book message, so
// it is the one place in the stack where constant factors genuinely matter.
// Four decisions define it, and each one is a deliberate rejection of the
// obvious `std::map<double, Level>` implementation:
//
//   1. PRICES ARE INTEGER TICKS, NOT DOUBLES.
//      A book keyed by `double` is a latent correctness bug: 1.10 arrives from
//      one venue as 1.0999999999999999 and from another as 1.1000000000000001,
//      and they land on two different levels. Venues quote on a tick grid, so
//      the grid is the natural key. All arithmetic here is exact integer
//      arithmetic; conversion to/from display prices happens at the edges only.
//
//   2. ARRAY-INDEXED, NOT TREE-INDEXED.
//      A live instrument's price range is bounded over a session, so the whole
//      ladder fits in a flat array indexed by (tick - floor). That turns every
//      update into an O(1) store instead of an O(log n) tree walk with pointer
//      chasing, and it means the hot path performs ZERO allocation: the arrays
//      are sized once at construction and never grow.
//
//   3. STRUCTURE-OF-ARRAYS.
//      Quantity and order-count live in separate arrays. Walking depth to sum
//      liquidity touches only the qty array, so a cache line holds 8 adjacent
//      levels' quantities rather than 8 half-used level structs.
//
//   4. CACHED BEST WITH LAZY REPAIR.
//      Best bid/ask is maintained incrementally: improving the book is a
//      compare-and-store. Only when the current best level is fully consumed do
//      we scan inward for the next populated level — rare, and bounded by the
//      number of empty ticks crossed. The alternative (re-deriving best on
//      every read) would put a scan on the most frequently called accessor in
//      the whole system.
//
// Out-of-range prices are REJECTED, not clamped and not undefined behaviour.
// A feed can and does send garbage; a book that corrupts itself or reads out of
// bounds on a bad message is worse than one that drops it and says so. Every
// rejection is counted so the data-quality layer can alarm on it.
//
// Not thread-safe by design: one writer thread owns a book. Cross-thread
// publication is the ring buffer's job, not the book's — mixing the two would
// put a lock on the hot path to solve a problem the threading model doesn't
// have.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <vector>

namespace mkt::book {

using Ticks = std::int64_t;   // price, in integral ticks
using Qty   = std::int64_t;   // quantity, in integral lots

enum class Side : std::uint8_t { Bid = 0, Ask = 1 };

constexpr Side opposite(Side s) noexcept {
    return s == Side::Bid ? Side::Ask : Side::Bid;
}

// One rung of the ladder, as returned by a depth snapshot.
struct Level {
    Ticks price;
    Qty   qty;
    std::uint32_t orders;
};

class Ladder {
  public:
    // `floor_ticks` is the lowest representable price; `n_ticks` the number of
    // rungs. Both sides share the grid, so a book that can quote 90.00–110.00
    // on a 0.01 tick is floor=9000, n=2001.
    Ladder(Ticks floor_ticks, std::size_t n_ticks)
        : floor_(floor_ticks),
          n_(n_ticks),
          bid_qty_(n_ticks, 0),
          ask_qty_(n_ticks, 0),
          bid_orders_(n_ticks, 0),
          ask_orders_(n_ticks, 0) {}

    // ---- geometry ----------------------------------------------------------
    Ticks floor_ticks() const noexcept { return floor_; }
    Ticks ceil_ticks()  const noexcept { return floor_ + static_cast<Ticks>(n_) - 1; }
    std::size_t capacity() const noexcept { return n_; }

    bool in_range(Ticks px) const noexcept {
        return px >= floor_ && px <= ceil_ticks();
    }

    // ---- mutation ----------------------------------------------------------
    // L2 replace: set the absolute resting quantity at `px`. This is the
    // semantics of almost every exchange's incremental book feed ("level 12 is
    // now 400"), as opposed to a delta. Returns false iff the price was out of
    // range, in which case nothing is mutated and the reject counter advances.
    bool upsert(Side side, Ticks px, Qty qty, std::uint32_t orders = 1) noexcept {
        if (!in_range(px) || qty < 0) { ++rejects_; return false; }
        const std::size_t i = index(px);
        auto& q  = qty_array(side);
        auto& oc = order_array(side);

        total_ref(side) += qty - q[i];
        if (q[i] == 0 && qty > 0)      ++populated(side);
        else if (q[i] > 0 && qty == 0) --populated(side);

        q[i]  = qty;
        oc[i] = (qty > 0) ? orders : 0u;

        if (qty > 0) widen_best(side, i);
        else if (static_cast<std::int64_t>(i) == best_idx(side)) repair_best(side);
        return true;
    }

    // Incremental delta ("level 12 gained 100"). Clamps at zero rather than
    // going negative: a feed that over-decrements is a sequencing problem to be
    // reported, not a reason to hold a negative resting quantity.
    bool add(Side side, Ticks px, Qty delta, std::uint32_t orders = 1) noexcept {
        if (!in_range(px)) { ++rejects_; return false; }
        const Qty cur = qty_array(side)[index(px)];
        Qty next = cur + delta;
        if (next < 0) { next = 0; ++underflows_; }
        return upsert(side, px, next, orders);
    }

    // Consume `qty` from the given side, walking from the best price inward —
    // the book-side effect of a trade. Returns the quantity actually removed
    // (less than requested iff the side ran dry) and the volume-weighted
    // average tick paid, which is what a taker's slippage is measured against.
    Qty consume(Side side, Qty qty, double* vwap_ticks = nullptr) noexcept {
        Qty filled = 0;
        double notional = 0.0;
        while (filled < qty) {
            Ticks px; Qty avail;
            if (!best(side, px, avail)) break;
            const Qty take = (avail < qty - filled) ? avail : qty - filled;
            notional += static_cast<double>(take) * static_cast<double>(px);
            filled   += take;
            upsert(side, px, avail - take, qty_array(side)[index(px)] ? 1u : 0u);
        }
        if (vwap_ticks) *vwap_ticks = filled ? notional / static_cast<double>(filled) : 0.0;
        return filled;
    }

    void clear() noexcept {
        std::fill(bid_qty_.begin(), bid_qty_.end(), 0);
        std::fill(ask_qty_.begin(), ask_qty_.end(), 0);
        std::fill(bid_orders_.begin(), bid_orders_.end(), 0u);
        std::fill(ask_orders_.begin(), ask_orders_.end(), 0u);
        best_bid_ = best_ask_ = kNone;
        bid_total_ = ask_total_ = 0;
        bid_levels_ = ask_levels_ = 0;
    }

    // ---- top of book -------------------------------------------------------
    bool best(Side side, Ticks& px, Qty& qty) const noexcept {
        const std::int64_t i = best_idx(side);
        if (i == kNone) return false;
        px  = floor_ + i;
        qty = qty_array(side)[static_cast<std::size_t>(i)];
        return true;
    }

    bool has_bid() const noexcept { return best_bid_ != kNone; }
    bool has_ask() const noexcept { return best_ask_ != kNone; }
    bool has_side(Side s) const noexcept { return best_idx(s) != kNone; }
    bool two_sided() const noexcept { return has_bid() && has_ask(); }

    // Spread in ticks. Only meaningful on a two-sided book; callers must check.
    Ticks spread_ticks() const noexcept {
        return two_sided() ? (floor_ + best_ask_) - (floor_ + best_bid_) : 0;
    }

    // Mid in ticks, as a double because a one-tick spread has a half-tick mid.
    double mid_ticks() const noexcept {
        if (!two_sided()) return 0.0;
        return 0.5 * static_cast<double>((floor_ + best_bid_) + (floor_ + best_ask_));
    }

    // Size-weighted mid — the fair value a market maker actually skews around,
    // since a 10x1 book at 99/100 is not worth 99.5.
    double micro_price_ticks() const noexcept {
        if (!two_sided()) return 0.0;
        const Qty bq = bid_qty_[static_cast<std::size_t>(best_bid_)];
        const Qty aq = ask_qty_[static_cast<std::size_t>(best_ask_)];
        const Qty tot = bq + aq;
        if (tot <= 0) return mid_ticks();
        const double bp = static_cast<double>(floor_ + best_bid_);
        const double ap = static_cast<double>(floor_ + best_ask_);
        // Weight each side by the OPPOSING size: heavy bid pulls fair value up.
        return (bp * static_cast<double>(aq) + ap * static_cast<double>(bq))
             / static_cast<double>(tot);
    }

    // (bid_qty - ask_qty) / (bid_qty + ask_qty) at top of book, in [-1, 1].
    double imbalance() const noexcept {
        if (!two_sided()) return 0.0;
        const double bq = static_cast<double>(bid_qty_[static_cast<std::size_t>(best_bid_)]);
        const double aq = static_cast<double>(ask_qty_[static_cast<std::size_t>(best_ask_)]);
        const double tot = bq + aq;
        return tot > 0.0 ? (bq - aq) / tot : 0.0;
    }

    // A crossed or locked book (bid >= ask) is never a valid market state; it
    // means a dropped or reordered message. Surfaced, not silently tolerated.
    bool is_crossed() const noexcept {
        return two_sided() && best_bid_ >= best_ask_;
    }

    // ---- depth -------------------------------------------------------------
    // Fills up to `out.size()` levels from the best price inward, skipping
    // empty rungs. Writes into caller-provided storage so a snapshot on the hot
    // path allocates nothing. Returns the number of levels written.
    std::size_t depth(Side side, std::span<Level> out) const noexcept {
        const std::int64_t start = best_idx(side);
        if (start == kNone || out.empty()) return 0;
        const auto& q  = qty_array(side);
        const auto& oc = order_array(side);
        std::size_t w = 0;
        if (side == Side::Bid) {
            for (std::int64_t i = start; i >= 0 && w < out.size(); --i)
                if (q[static_cast<std::size_t>(i)] > 0)
                    out[w++] = {floor_ + i, q[static_cast<std::size_t>(i)],
                                oc[static_cast<std::size_t>(i)]};
        } else {
            const std::int64_t lim = static_cast<std::int64_t>(n_);
            for (std::int64_t i = start; i < lim && w < out.size(); ++i)
                if (q[static_cast<std::size_t>(i)] > 0)
                    out[w++] = {floor_ + i, q[static_cast<std::size_t>(i)],
                                oc[static_cast<std::size_t>(i)]};
        }
        return w;
    }

    Qty qty_at(Side side, Ticks px) const noexcept {
        return in_range(px) ? qty_array(side)[index(px)] : 0;
    }

    Qty  total_qty(Side side) const noexcept { return side == Side::Bid ? bid_total_ : ask_total_; }
    std::size_t levels(Side side) const noexcept { return side == Side::Bid ? bid_levels_ : ask_levels_; }

    // ---- data-quality counters --------------------------------------------
    std::uint64_t rejects()    const noexcept { return rejects_; }
    std::uint64_t underflows() const noexcept { return underflows_; }

  private:
    static constexpr std::int64_t kNone = -1;

    std::size_t index(Ticks px) const noexcept {
        return static_cast<std::size_t>(px - floor_);
    }

    std::int64_t best_idx(Side s) const noexcept { return s == Side::Bid ? best_bid_ : best_ask_; }

    std::vector<Qty>&       qty_array(Side s) noexcept { return s == Side::Bid ? bid_qty_ : ask_qty_; }
    const std::vector<Qty>& qty_array(Side s) const noexcept { return s == Side::Bid ? bid_qty_ : ask_qty_; }
    std::vector<std::uint32_t>&       order_array(Side s) noexcept { return s == Side::Bid ? bid_orders_ : ask_orders_; }
    const std::vector<std::uint32_t>& order_array(Side s) const noexcept { return s == Side::Bid ? bid_orders_ : ask_orders_; }
    Qty&         total_ref(Side s) noexcept { return s == Side::Bid ? bid_total_ : ask_total_; }
    std::size_t& populated(Side s) noexcept { return s == Side::Bid ? bid_levels_ : ask_levels_; }

    // A newly populated level can only ever improve the best, which is the
    // common case and stays branch-light.
    void widen_best(Side s, std::size_t i) noexcept {
        const std::int64_t ii = static_cast<std::int64_t>(i);
        if (s == Side::Bid) { if (best_bid_ == kNone || ii > best_bid_) best_bid_ = ii; }
        else                { if (best_ask_ == kNone || ii < best_ask_) best_ask_ = ii; }
    }

    // The best level emptied: walk inward for the next populated rung. Bounded
    // by the empty ticks crossed, and only reachable when top of book clears.
    void repair_best(Side s) noexcept {
        if (s == Side::Bid) {
            std::int64_t i = best_bid_;
            while (i >= 0 && bid_qty_[static_cast<std::size_t>(i)] == 0) --i;
            best_bid_ = (i >= 0) ? i : kNone;
        } else {
            const std::int64_t lim = static_cast<std::int64_t>(n_);
            std::int64_t i = best_ask_;
            while (i < lim && ask_qty_[static_cast<std::size_t>(i)] == 0) ++i;
            best_ask_ = (i < lim) ? i : kNone;
        }
    }

    Ticks       floor_;
    std::size_t n_;

    std::vector<Qty> bid_qty_, ask_qty_;
    std::vector<std::uint32_t> bid_orders_, ask_orders_;

    std::int64_t best_bid_ = kNone, best_ask_ = kNone;
    Qty bid_total_ = 0, ask_total_ = 0;
    std::size_t bid_levels_ = 0, ask_levels_ = 0;
    std::uint64_t rejects_ = 0, underflows_ = 0;
};

}  // namespace mkt::book
