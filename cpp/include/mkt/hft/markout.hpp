// markout.hpp — adverse selection, measured.
//
// THE QUESTION A MARKET MAKER MUST ANSWER ABOUT ITS OWN FILLS: did the market
// move against me immediately after I traded? A maker earns the spread on
// uninformed flow and loses far more than the spread to informed flow, so the
// P&L of a quoting strategy is almost entirely a question of which kind it is
// getting — and the spread capture alone tells you nothing about that.
//
// MARKOUT is the standard measure: take the mid at some horizon after the fill
// and compare it to the fill price, signed by the side you traded.
//
//   markout(h) = (mid_at_h - fill_price) * side_sign
//
// where side_sign is +1 for a buy and -1 for a sell, so POSITIVE IS ALWAYS GOOD
// regardless of direction. A buy that is followed by a rise and a sell that is
// followed by a fall are both good fills, and a measure that flips sign between
// them cannot be aggregated across a book.
//
// HOW TO READ IT, because the interpretation is the whole value:
//
//   Markout POSITIVE and growing with horizon  -> the flow is uninformed. You
//     bought and the price went up; you are being paid for liquidity.
//
//   Markout NEGATIVE, worsening with horizon   -> ADVERSE SELECTION. You are
//     being picked off: the counterparty knew something. A maker can have a
//     positive spread capture and still lose money here, which is precisely why
//     spread capture is not a sufficient metric.
//
//   Markout near zero at short horizons, negative later -> slower information.
//     Often the more dangerous case, because it survives a naive short-horizon
//     check.
//
// MULTIPLE HORIZONS, not one. Adverse selection at 100ms and at 60s are different
// phenomena with different causes: the first is usually latency, the second is
// usually genuine information. A single horizon conflates them and makes the fix
// unidentifiable.
//
// PENDING FILLS ARE HELD UNTIL THEIR HORIZON MATURES. A fill measured before its
// horizon has elapsed is measured against a mid that has not had time to move,
// which biases every markout toward zero and hides exactly the effect being
// looked for.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <vector>

namespace mkt::hft {

// Horizons in nanoseconds. Spanning three orders of magnitude on purpose: they
// separate latency-driven pickoff from information-driven pickoff.
inline constexpr std::array<std::int64_t, 5> kHorizonsNs = {
    1'000'000LL,        // 1ms   — pure latency
    10'000'000LL,       // 10ms
    100'000'000LL,      // 100ms
    1'000'000'000LL,    // 1s
    10'000'000'000LL,   // 10s   — information
};

struct MarkoutStats {
    std::int64_t horizon_ns = 0;
    std::size_t  n = 0;
    double sum_bps = 0.0;
    double sum_sq = 0.0;
    double min_bps = 0.0;
    double max_bps = 0.0;

    double mean_bps() const noexcept { return n ? sum_bps / static_cast<double>(n) : 0.0; }
    double stdev_bps() const noexcept {
        if (n < 2) return 0.0;
        const double m = mean_bps();
        const double v = sum_sq / static_cast<double>(n) - m * m;
        return v > 0 ? std::sqrt(v) : 0.0;
    }
    // Mean divided by standard error. A markout of -0.2bps on six fills is noise;
    // the same number on sixty thousand is a strategy that is being picked off.
    // Reporting the mean without this invites acting on the first.
    //
    // ZERO VARIANCE IS THE STRONGEST SIGNAL, NOT THE WEAKEST. Every fill marking
    // out identically against you is maximal evidence, not absent evidence.
    // Returning 0 there -- which the obvious `if (s <= 0) return 0` guard does --
    // reports a perfectly consistent loss as "not significant" and hides exactly
    // the worst case. A test caught this.
    double t_stat() const noexcept {
        if (n < 2) return 0.0;
        const double m = mean_bps();
        const double s = stdev_bps();
        if (s <= 0.0) {
            // Degenerate: no dispersion. Infinite t in the limit, so return a
            // large finite value carrying the mean's sign rather than zero.
            return m == 0.0 ? 0.0 : (m > 0 ? 1e9 : -1e9);
        }
        return m / (s / std::sqrt(static_cast<double>(n)));
    }
    bool significant(double threshold = 2.0) const noexcept {
        return n >= 30 && std::abs(t_stat()) >= threshold;
    }
};

class MarkoutTracker {
  public:
    struct Fill {
        std::int64_t ts_ns;
        double price;
        double side_sign;      // +1 buy, -1 sell
        std::array<bool, kHorizonsNs.size()> done{};
    };

    MarkoutTracker() {
        for (std::size_t i = 0; i < kHorizonsNs.size(); ++i)
            stats_[i].horizon_ns = kHorizonsNs[i];
    }

    void on_fill(std::int64_t ts_ns, double price, bool is_buy) {
        pending_.push_back(Fill{ts_ns, price, is_buy ? 1.0 : -1.0, {}});
    }

    // Call as the mid updates. Matures any pending fill whose horizon has
    // elapsed. Measuring before the horizon biases every markout toward zero.
    void on_mid(std::int64_t ts_ns, double mid) {
        for (auto& f : pending_) {
            for (std::size_t i = 0; i < kHorizonsNs.size(); ++i) {
                if (f.done[i]) continue;
                if (ts_ns - f.ts_ns < kHorizonsNs[i]) continue;
                if (f.price > 0) {
                    const double bps = ((mid - f.price) * f.side_sign / f.price) * 10'000.0;
                    auto& s = stats_[i];
                    if (s.n == 0) { s.min_bps = s.max_bps = bps; }
                    else { s.min_bps = std::min(s.min_bps, bps);
                           s.max_bps = std::max(s.max_bps, bps); }
                    s.sum_bps += bps;
                    s.sum_sq += bps * bps;
                    ++s.n;
                }
                f.done[i] = true;
            }
        }
        // Retire fills whose longest horizon has matured, so memory is bounded.
        pending_.erase(
            std::remove_if(pending_.begin(), pending_.end(),
                           [](const Fill& f) { return f.done.back(); }),
            pending_.end());
    }

    const MarkoutStats& at(std::size_t i) const noexcept { return stats_[i]; }
    std::size_t horizons() const noexcept { return kHorizonsNs.size(); }
    std::size_t pending() const noexcept { return pending_.size(); }

    // True when the SHORT horizons are significantly negative -- the signature
    // of latency-driven pickoff specifically, as opposed to slower information.
    bool adversely_selected(double t_threshold = 2.0) const noexcept {
        for (std::size_t i = 0; i < 3 && i < stats_.size(); ++i)
            if (stats_[i].significant(t_threshold) && stats_[i].mean_bps() < 0)
                return true;
        return false;
    }

    void reset() {
        pending_.clear();
        for (std::size_t i = 0; i < stats_.size(); ++i) {
            stats_[i] = MarkoutStats{};
            stats_[i].horizon_ns = kHorizonsNs[i];
        }
    }

  private:
    std::vector<Fill> pending_;
    std::array<MarkoutStats, kHorizonsNs.size()> stats_{};
};

}  // namespace mkt::hft
