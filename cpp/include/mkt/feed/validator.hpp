// validator.hpp — per-instrument market-data quality gate.
//
// Every tick from a venue passes through here before it is allowed to touch a
// book, a price, or a risk number. The premise is that feeds lie: they drop
// messages, replay them, deliver them out of order, stamp them with clocks that
// disagree with yours, and occasionally quote a price three standard deviations
// from reality because someone fat-fingered a test order into production.
//
// A pricing engine that trusts its input produces confidently wrong numbers, so
// the checks live here — upstream of everything — and each one is COUNTED as
// well as acted on. The counters are the point: "we rejected 4,112 ticks today"
// is an operational signal, whereas silently dropping them is how a desk ends
// up marking a book against a stale price for an hour.
//
// SEQUENCE HANDLING is the subtle part. Three distinct things can happen when
// the sequence number is not exactly `expected`:
//   * seq  > expected  -> GAP. Messages were lost. The book is now untrustworthy
//                         and must be recovered (snapshot/resend), not patched.
//   * seq  < expected   -> either a DUPLICATE (already seen) or a late REORDER.
//                         Both are dropped, but they are different diagnoses:
//                         duplicates mean the transport is retransmitting,
//                         reorders mean it is not preserving order.
//   * seq == expected   -> in sequence; advance.
// Conflating these three is the most common bug in a hand-rolled feed handler,
// because all three "look like" a bad sequence number.
//
// OUTLIER DETECTION uses an EWMA mean and variance rather than a rolling window,
// for two reasons: it is O(1) in time and space (no ring buffer to size or
// scan), and it adapts to regime changes instead of being anchored by an
// arbitrary lookback. The trade-off is that it needs a warm-up period, so the
// check is disabled until `min_samples` ticks have been seen — a cold validator
// must not reject the very first real price for deviating from a mean of zero.
//
// Not thread-safe: one validator per instrument per feed thread, same ownership
// model as the book it guards.
#pragma once

#include <cmath>
#include <cstdint>

namespace mkt::feed {

// Why a tick was accepted or refused. Ordered so that `>= Rejected` is a single
// comparison on the hot path.
enum class Verdict : std::uint8_t {
    Accepted = 0,
    // --- everything below is a rejection ---
    Rejected        = 1,
    SequenceGap     = 2,   // messages lost — book must be recovered
    Duplicate       = 3,   // seq already processed
    Reordered       = 4,   // arrived after a later seq
    StaleTimestamp  = 5,   // exchange clock went backwards
    TooOld          = 6,   // age exceeds tolerance
    PriceOutlier    = 7,   // robust z-score breach
    PriceOutOfBand  = 8,   // outside the instrument's hard price band
    BadValue        = 9,   // non-finite or non-positive price / negative size
};

constexpr bool accepted(Verdict v) noexcept { return v == Verdict::Accepted; }

constexpr const char* to_string(Verdict v) noexcept {
    switch (v) {
        case Verdict::Accepted:       return "accepted";
        case Verdict::Rejected:       return "rejected";
        case Verdict::SequenceGap:    return "sequence_gap";
        case Verdict::Duplicate:      return "duplicate";
        case Verdict::Reordered:      return "reordered";
        case Verdict::StaleTimestamp: return "stale_timestamp";
        case Verdict::TooOld:         return "too_old";
        case Verdict::PriceOutlier:   return "price_outlier";
        case Verdict::PriceOutOfBand: return "price_out_of_band";
        case Verdict::BadValue:       return "bad_value";
    }
    return "unknown";
}

// A normalized tick. Timestamps are integer nanoseconds since the epoch: a
// double cannot represent nanosecond epoch time without losing precision
// (2^53 ns is only ~104 days), which is why every timestamp in this stack is
// an int64 and normalization to that form happens in the feed handler.
struct Tick {
    std::uint64_t seq        = 0;
    std::int64_t  exch_ns    = 0;   // venue-stamped time
    std::int64_t  recv_ns    = 0;   // our receipt time
    double        price      = 0.0;
    double        size       = 0.0;
};

struct Config {
    // Hard band: prices outside this are refused regardless of statistics. This
    // is the instrument's contractual limit (price collar / circuit band), not a
    // statistical judgement.
    double price_min = 0.0;
    double price_max = 1e18;

    // Robust z-score threshold for the statistical outlier check.
    double max_z = 10.0;

    // Ticks before the outlier check is armed. A cold validator has no
    // distribution to compare against, so it must not reject early prices.
    std::uint64_t min_samples = 30;

    // EWMA decay for the running mean/variance of log-returns. 0.97 ~ a few
    // hundred ticks of memory.
    double lambda = 0.97;

    // Max tolerated (recv_ns - exch_ns). Catches a feed that has fallen behind
    // or a venue clock that is badly skewed. Default 5s.
    std::int64_t max_age_ns = 5'000'000'000LL;

    // Age beyond which the LAST GOOD price is considered stale for pricing
    // purposes. Distinct from max_age_ns: that rejects an arriving tick, this
    // marks an instrument as having no current price. Default 10s.
    std::int64_t stale_after_ns = 10'000'000'000LL;
};

struct Counters {
    std::uint64_t accepted = 0;
    std::uint64_t gaps = 0, gap_messages_lost = 0;
    std::uint64_t duplicates = 0, reordered = 0;
    std::uint64_t stale_timestamp = 0, too_old = 0;
    std::uint64_t outliers = 0, out_of_band = 0, bad_values = 0;

    std::uint64_t total_rejected() const noexcept {
        return gaps + duplicates + reordered + stale_timestamp + too_old
             + outliers + out_of_band + bad_values;
    }
    std::uint64_t total_seen() const noexcept { return accepted + total_rejected(); }
};

class Validator {
  public:
    explicit Validator(Config cfg = {}) : cfg_(cfg) {}

    // The hot path. Branch order is deliberate: cheap integer checks first,
    // floating-point statistics last, so the common rejection cases never pay
    // for the expensive ones.
    Verdict check(const Tick& t) noexcept {
        // --- transport sequencing (first: cheapest, and independent) ---------
        // Sequencing describes the TRANSPORT, not the payload. A message that
        // arrives in order advances the stream even if its content is then
        // refused, because the venue did send it and the next message really is
        // the one after it. Gating the advance on content validity was a bug:
        // one out-of-band price would make every subsequent message look like a
        // gap, and the feed would never recover.
        if (have_seq_) {
            if (t.seq == last_seq_) { ++c_.duplicates; return Verdict::Duplicate; }
            if (t.seq <  last_seq_) { ++c_.reordered;  return Verdict::Reordered; }
            if (t.seq >  last_seq_ + 1) {
                ++c_.gaps;
                c_.gap_messages_lost += t.seq - last_seq_ - 1;
                // Advance so we resynchronise rather than rejecting forever, but
                // report the gap so the caller triggers recovery and stops
                // trusting the book. No content check runs on a gapped message:
                // the book is already known to be wrong.
                last_seq_ = t.seq;
                gap_pending_ = true;
                return Verdict::SequenceGap;
            }
        }
        last_seq_ = t.seq;
        have_seq_ = true;

        // --- value sanity (a precondition for every check below) -------------
        if (!std::isfinite(t.price) || t.price <= 0.0 || !std::isfinite(t.size) || t.size < 0.0) {
            ++c_.bad_values; return Verdict::BadValue;
        }

        // --- timestamps -----------------------------------------------------
        if (have_ts_ && t.exch_ns < last_exch_ns_) {
            ++c_.stale_timestamp; return Verdict::StaleTimestamp;
        }
        if (t.recv_ns > 0 && t.exch_ns > 0 && (t.recv_ns - t.exch_ns) > cfg_.max_age_ns) {
            ++c_.too_old; return Verdict::TooOld;
        }

        // --- hard price band ------------------------------------------------
        if (t.price < cfg_.price_min || t.price > cfg_.price_max) {
            ++c_.out_of_band; return Verdict::PriceOutOfBand;
        }

        // --- statistical outlier (only once warm) ---------------------------
        if (samples_ >= cfg_.min_samples && last_price_ > 0.0) {
            const double r = std::log(t.price / last_price_);
            const double sd = std::sqrt(var_ > 0.0 ? var_ : 0.0);
            // A zero-variance history (a book that has not moved) would make any
            // move infinitely many sigmas; require a real sd before judging.
            if (sd > 1e-12 && std::fabs(r - mean_) / sd > cfg_.max_z) {
                ++c_.outliers; return Verdict::PriceOutlier;
            }
        }

        accept(t);
        return Verdict::Accepted;
    }

    // True once a gap has been seen and not yet acknowledged. The book this
    // validator guards should be treated as unreliable until recovery completes.
    bool needs_recovery() const noexcept { return gap_pending_; }

    // Called after a snapshot/resend has rebuilt the book.
    void recovered(std::uint64_t resume_seq) noexcept {
        gap_pending_ = false;
        last_seq_ = resume_seq;
        have_seq_ = true;
    }

    // Is the last accepted price too old to price against? Separate from
    // rejecting an arriving tick — this is about the absence of new ones.
    bool is_stale(std::int64_t now_ns) const noexcept {
        if (!have_ts_) return true;                 // never had a price at all
        return (now_ns - last_exch_ns_) > cfg_.stale_after_ns;
    }

    double        last_price() const noexcept { return last_price_; }
    std::int64_t  last_exch_ns() const noexcept { return last_exch_ns_; }
    std::uint64_t last_seq() const noexcept { return last_seq_; }
    std::uint64_t samples() const noexcept { return samples_; }
    const Counters& counters() const noexcept { return c_; }
    const Config&   config()   const noexcept { return cfg_; }

    // Fraction of ticks refused — the headline data-quality number.
    double reject_rate() const noexcept {
        const std::uint64_t n = c_.total_seen();
        return n ? static_cast<double>(c_.total_rejected()) / static_cast<double>(n) : 0.0;
    }

    void reset() noexcept {
        have_seq_ = have_ts_ = false; gap_pending_ = false;
        last_seq_ = 0; last_exch_ns_ = 0; last_price_ = 0.0;
        mean_ = var_ = 0.0; samples_ = 0; c_ = {};
    }

  private:
    void accept(const Tick& t) noexcept {
        if (last_price_ > 0.0) {
            const double r = std::log(t.price / last_price_);
            const double l = cfg_.lambda;
            // EWMA mean and variance of log-returns. Variance is updated around
            // the PREVIOUS mean before the mean moves, which keeps the estimate
            // from collapsing when a large move arrives.
            const double d = r - mean_;
            mean_ = l * mean_ + (1.0 - l) * r;
            var_  = l * var_  + (1.0 - l) * d * d;
        }
        last_price_   = t.price;
        last_exch_ns_ = t.exch_ns;
        // last_seq_/have_seq_ are owned by the sequencing stage above, not here.
        have_ts_ = true;
        ++samples_;
        ++c_.accepted;
    }

    Config cfg_;
    Counters c_{};
    bool have_seq_ = false, have_ts_ = false, gap_pending_ = false;
    std::uint64_t last_seq_ = 0, samples_ = 0;
    std::int64_t  last_exch_ns_ = 0;
    double last_price_ = 0.0, mean_ = 0.0, var_ = 0.0;
};

}  // namespace mkt::feed
