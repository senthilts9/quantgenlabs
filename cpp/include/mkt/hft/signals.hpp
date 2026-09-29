// signals.hpp — microstructure features, computed on the hot path.
//
// The platform had the low-latency PLUMBING and no strategy. A book, a FIX
// engine and a 209ns tick-to-order path are what an HFT system is built ON; they
// are not an HFT system, because nothing in them decides what to trade. These
// are the features a short-horizon strategy actually decides from.
//
// EVERY ONE IS O(1) AND ALLOCATION-FREE, updated incrementally as ticks arrive.
// A signal that needs a pass over history cannot run at tick rate, and a signal
// computed at a rate slower than the book changes is describing a book that no
// longer exists.
//
// WHAT THESE SIGNALS ARE AND ARE NOT. They are well-documented microstructure
// features with published predictive content at horizons of milliseconds to
// seconds. They are NOT alpha: a feature with predictive content and a strategy
// that makes money are separated by transaction costs, queue position, adverse
// selection and capacity, every one of which can consume the entire edge. The
// honest claim is that these are the right inputs, not that they are profitable.
//
// SIGNS ARE STATED EXPLICITLY throughout, because a microstructure signal with an
// inverted sign is worse than no signal: it is a strategy that trades
// confidently in the wrong direction and looks like bad luck for a long time.
#pragma once

#include "mkt/book/ladder.hpp"

#include <cmath>
#include <cstdint>

namespace mkt::hft {

using book::Ladder;
using book::Qty;
using book::Side;
using book::Ticks;

// Exponentially weighted mean/variance. O(1) in time and space, adapts to regime
// change, and needs no ring buffer to size or scan.
class Ewma {
  public:
    explicit Ewma(double lambda = 0.97) : lambda_(lambda) {}

    void update(double x) noexcept {
        if (!warm_) { mean_ = x; warm_ = true; ++n_; return; }
        const double d = x - mean_;
        mean_ += (1.0 - lambda_) * d;
        // Variance around the PREVIOUS mean, before it moves: updating around the
        // new mean makes the estimate collapse exactly when a large move arrives,
        // which is when it is needed.
        var_ = lambda_ * var_ + (1.0 - lambda_) * d * d;
        ++n_;
    }

    double mean() const noexcept { return mean_; }
    double var() const noexcept { return var_ > 0 ? var_ : 0.0; }
    double stdev() const noexcept { return std::sqrt(var()); }
    bool warm(std::uint64_t min_n = 30) const noexcept { return n_ >= min_n; }
    std::uint64_t count() const noexcept { return n_; }
    void reset() noexcept { mean_ = var_ = 0.0; n_ = 0; warm_ = false; }

  private:
    double lambda_, mean_ = 0.0, var_ = 0.0;
    std::uint64_t n_ = 0;
    bool warm_ = false;
};

struct Features {
    // Top-of-book size imbalance in [-1, 1]. POSITIVE = more bid than ask =
    // buying pressure = price more likely to tick UP.
    double imbalance = 0.0;
    // Size-weighted fair value. Sits nearer the side with LESS size, because
    // that side is the one about to be consumed.
    double micro_price = 0.0;
    // micro_price - mid, in ticks. Positive means fair value is above the
    // midpoint, i.e. the book is leaning bid.
    double micro_minus_mid = 0.0;
    // Depth imbalance over N levels, not just the touch. The touch can be
    // spoofed cheaply; depth is more expensive to fake.
    double depth_imbalance = 0.0;
    // Signed trade flow: buys minus sells, EWMA. Positive = aggressors lifting
    // offers. This is the most direct measure of pressure, because a trade is a
    // commitment and a quote is not.
    double flow_imbalance = 0.0;
    // Realised volatility of mid returns, EWMA, per tick.
    double volatility = 0.0;
    Ticks spread_ticks = 0;
    bool  two_sided = false;
    bool  warm = false;
};

// Rolls the features forward as the book and tape change. One instance per
// instrument per thread, same ownership model as the book it reads.
class FeatureEngine {
  public:
    explicit FeatureEngine(double lambda = 0.97, std::size_t depth_levels = 5)
        : flow_(lambda), vol_(lambda), depth_levels_(depth_levels) {}

    // Call after the book changes.
    void on_book(const Ladder& b) noexcept {
        Ticks bp, ap; Qty bq, aq;
        f_.two_sided = b.best(Side::Bid, bp, bq) && b.best(Side::Ask, ap, aq);
        if (!f_.two_sided) return;

        f_.spread_ticks = ap - bp;
        f_.imbalance = b.imbalance();
        f_.micro_price = b.micro_price_ticks();
        f_.micro_minus_mid = f_.micro_price - b.mid_ticks();

        // Depth beyond the touch. The touch is cheap to spoof -- a single small
        // order sets it -- so a signal that only reads level one is reading the
        // part of the book that is easiest to manipulate.
        std::array<book::Level, 16> bids{}, asks{};
        const std::size_t n = depth_levels_ < 16 ? depth_levels_ : 16;
        const std::size_t nb = b.depth(Side::Bid, std::span<book::Level>(bids.data(), n));
        const std::size_t na = b.depth(Side::Ask, std::span<book::Level>(asks.data(), n));
        double bsum = 0, asum = 0;
        for (std::size_t i = 0; i < nb; ++i) bsum += static_cast<double>(bids[i].qty);
        for (std::size_t i = 0; i < na; ++i) asum += static_cast<double>(asks[i].qty);
        const double tot = bsum + asum;
        f_.depth_imbalance = tot > 0 ? (bsum - asum) / tot : 0.0;

        const double mid = b.mid_ticks();
        if (last_mid_ > 0) vol_.update(std::log(mid / last_mid_));
        last_mid_ = mid;
        f_.volatility = vol_.stdev();
        f_.warm = vol_.warm();
    }

    // Call on every trade print. `aggressor_bought` is true when the buyer was
    // the aggressor (a trade at or above the ask).
    void on_trade(Qty qty, bool aggressor_bought) noexcept {
        flow_.update(aggressor_bought ? static_cast<double>(qty)
                                      : -static_cast<double>(qty));
        // Normalised so the signal is comparable across instruments of very
        // different typical size. Without this a 1000-lot name and a 10-lot name
        // produce numbers that cannot share a threshold.
        const double scale = std::abs(flow_.mean()) + flow_.stdev() + 1.0;
        f_.flow_imbalance = flow_.mean() / scale;
    }

    const Features& features() const noexcept { return f_; }

    // Composite short-horizon view in [-1, 1]. POSITIVE = expect the mid to tick
    // UP. Equal weights: fitting them on the data you then trade is how a
    // backtest becomes a curve fit, and the honest default is to not pretend to
    // know the weights.
    double fair_value_edge() const noexcept {
        if (!f_.two_sided) return 0.0;
        const double e = (f_.imbalance + f_.depth_imbalance + f_.flow_imbalance) / 3.0;
        return e > 1.0 ? 1.0 : (e < -1.0 ? -1.0 : e);
    }

    void reset() noexcept { flow_.reset(); vol_.reset(); last_mid_ = 0; f_ = {}; }

  private:
    Features f_{};
    Ewma flow_, vol_;
    double last_mid_ = 0.0;
    std::size_t depth_levels_;
};

// ---------------------------------------------------------------- queue position
//
// The number that decides whether a passive quote is worth having. Two orders at
// the same price are not the same asset: the one at the front fills on the next
// trade, the one behind 10,000 lots may never fill at all. A market-making
// strategy that ignores queue position is systematically wrong about its own
// fill probability, and it is wrong in the optimistic direction.
class QueueTracker {
  public:
    // Joining the back of a level: everything already resting is ahead.
    void join(Qty qty_ahead) noexcept {
        ahead_ = qty_ahead;
        joined_ = true;
    }

    // A trade consumed `qty` at this level. Only quantity ahead of us matters.
    void on_trade(Qty qty) noexcept {
        if (!joined_) return;
        ahead_ = (qty >= ahead_) ? 0 : ahead_ - qty;
    }

    // A cancel removed size from this level. We cannot see WHERE, so assume the
    // pessimistic case: the cancel was BEHIND us and our position is unchanged.
    // Assuming it was ahead would make the model claim a better queue position
    // than it has -- optimistic in exactly the direction that loses money.
    void on_cancel(Qty, bool known_ahead = false, Qty ahead_qty = 0) noexcept {
        if (!joined_ || !known_ahead) return;
        ahead_ = (ahead_qty >= ahead_) ? 0 : ahead_ - ahead_qty;
    }

    Qty ahead() const noexcept { return ahead_; }
    bool at_front() const noexcept { return joined_ && ahead_ == 0; }

    // Probability of filling given expected volume at this level. Not a model of
    // anything subtle: if the volume that trades exceeds what is ahead, we fill.
    double fill_probability(double expected_volume) const noexcept {
        if (!joined_) return 0.0;
        if (ahead_ == 0) return 1.0;
        if (expected_volume <= 0) return 0.0;
        const double p = expected_volume / static_cast<double>(ahead_);
        return p >= 1.0 ? 1.0 : p;
    }

    void reset() noexcept { ahead_ = 0; joined_ = false; }

  private:
    Qty ahead_ = 0;
    bool joined_ = false;
};

}  // namespace mkt::hft
