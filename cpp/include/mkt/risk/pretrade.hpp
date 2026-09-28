// pretrade.hpp — pre-trade risk gate: the last thing an order passes before the wire.
//
// This sits on the order path, so every check is O(1), allocation-free, and
// ordered cheapest-and-most-decisive first. It is the control that stops the
// expensive mistakes: a mistyped quantity, a limit price through the market, an
// algorithm that has started looping, a position that breaches a mandate.
//
// CHECK ORDER IS A DESIGN DECISION, not an accident of how it was written:
//   1. Kill switch      — one atomic load. When a desk hits the switch, nothing
//                         else should be able to let an order through, so it is
//                         first and it is unconditional.
//   2. Well-formedness  — zero/negative/non-finite quantities and prices.
//   3. Static limits    — max order qty, max notional. Pure arithmetic on the
//                         order itself, no market data needed, so they work even
//                         when the feed is down.
//   4. Price collar     — needs a reference price, so it comes after the checks
//                         that do not.
//   5. Position limits  — needs current state, the most expensive lookup.
//
// THE STALE-REFERENCE DECISION. A price collar compares a limit price against a
// reference (last trade or mid). If market data is stale, there is no trustworthy
// reference — and the correct behaviour is to REJECT, not to wave the order
// through unchecked. Skipping the collar when the reference is bad would disable
// the fat-finger protection at exactly the moment it is most likely to be needed
// (a disconnected feed and a panicking trader). This is configurable because some
// desks accept the opposite trade-off, but the default is to refuse.
//
// "FAT FINGER" is implemented as two independent checks, because the two common
// typos are different mistakes. An extra digit in QUANTITY is caught by
// max_order_qty and max_notional. An extra digit in PRICE is caught by the
// collar, which is expressed in basis points from the reference so it scales
// across instruments instead of needing a per-symbol absolute tolerance.
//
// State (positions, per-symbol limits) is held in symbol-indexed vectors rather
// than a hash map: symbol ids come from the security master as small dense
// integers, so an index is both O(1) and cache-resident, and the entire limit
// table for a few thousand instruments fits comfortably in L2.
#pragma once

#include <atomic>
#include <cmath>
#include <cstdint>
#include <vector>

namespace mkt::risk {

using SymbolId = std::uint32_t;

enum class Side : std::uint8_t { Buy = 0, Sell = 1 };

enum class Decision : std::uint8_t {
    Accept = 0,
    KillSwitchEngaged,
    SymbolHalted,
    UnknownSymbol,
    BadQuantity,          // zero, negative, or non-finite
    BadPrice,
    MaxOrderQty,          // single-order quantity ceiling
    MaxNotional,          // single-order value ceiling
    PriceCollar,          // limit price too far from reference
    NoReferencePrice,     // collar could not be evaluated -> refused
    PositionLimit,        // resulting position breaches the mandate
    MaxOpenOrders,
    MessageRateLimit,     // runaway algorithm
};

constexpr bool accepted(Decision d) noexcept { return d == Decision::Accept; }

constexpr const char* to_string(Decision d) noexcept {
    switch (d) {
        case Decision::Accept:            return "accept";
        case Decision::KillSwitchEngaged: return "kill_switch_engaged";
        case Decision::SymbolHalted:      return "symbol_halted";
        case Decision::UnknownSymbol:     return "unknown_symbol";
        case Decision::BadQuantity:       return "bad_quantity";
        case Decision::BadPrice:          return "bad_price";
        case Decision::MaxOrderQty:       return "max_order_qty";
        case Decision::MaxNotional:       return "max_notional";
        case Decision::PriceCollar:       return "price_collar";
        case Decision::NoReferencePrice:  return "no_reference_price";
        case Decision::PositionLimit:     return "position_limit";
        case Decision::MaxOpenOrders:     return "max_open_orders";
        case Decision::MessageRateLimit:  return "message_rate_limit";
    }
    return "unknown";
}

enum class OrdType : std::uint8_t { Market = 0, Limit = 1 };

struct OrderRequest {
    SymbolId symbol = 0;
    Side     side   = Side::Buy;
    double   qty    = 0.0;
    double   price  = 0.0;       // ignored for Market
    OrdType  type   = OrdType::Limit;
};

// Per-instrument limits. Defaults are permissive-but-finite: a limit that is
// accidentally left unset should still be a limit, not infinity.
struct SymbolLimits {
    bool   known             = false;
    bool   halted            = false;
    double max_order_qty     = 1e9;
    double max_order_notional= 1e12;
    double collar_bps        = 1000.0;   // 10% from reference
    double max_long_position = 1e12;
    double max_short_position= 1e12;     // magnitude of a permitted short
    double contract_size     = 1.0;      // notional = qty * price * contract_size
};

// What the gate knows about the market for a symbol at decision time.
struct MarketContext {
    double reference_price = 0.0;  // last trade or mid
    bool   reference_ok    = false;// false when stale/absent
};

struct GateConfig {
    // Refuse when the collar cannot be evaluated. See the header note: waving
    // orders through on a dead feed disables fat-finger protection exactly when
    // it matters most.
    bool reject_without_reference = true;
    // Global ceiling on in-flight orders per symbol.
    std::uint32_t max_open_orders_per_symbol = 1000;
    // Token-bucket message rate limit, orders per second, 0 = disabled.
    double max_orders_per_sec = 0.0;
    double burst_allowance    = 50.0;
};

struct GateStats {
    std::uint64_t checked = 0, accepted = 0;
    std::uint64_t kill_switch = 0, halted = 0, unknown = 0;
    std::uint64_t bad_qty = 0, bad_price = 0;
    std::uint64_t max_qty = 0, max_notional = 0;
    std::uint64_t collar = 0, no_reference = 0;
    std::uint64_t position = 0, open_orders = 0, rate_limited = 0;

    std::uint64_t rejected() const noexcept { return checked - accepted; }
};

class PreTradeGate {
  public:
    explicit PreTradeGate(std::size_t n_symbols, GateConfig cfg = {})
        : cfg_(cfg),
          limits_(n_symbols),
          position_(n_symbols, 0.0),
          open_orders_(n_symbols, 0u),
          tokens_(cfg.burst_allowance) {}

    // ---- configuration -----------------------------------------------------
    void set_limits(SymbolId s, const SymbolLimits& l) {
        if (s < limits_.size()) limits_[s] = l;
    }
    const SymbolLimits& limits(SymbolId s) const { return limits_[s]; }

    void set_position(SymbolId s, double qty) { if (s < position_.size()) position_[s] = qty; }
    double position(SymbolId s) const { return s < position_.size() ? position_[s] : 0.0; }

    void halt(SymbolId s, bool on) { if (s < limits_.size()) limits_[s].halted = on; }

    // The kill switch is atomic and relaxed-ordered: it is read on the order
    // path and written by a human or a supervisor thread. Relaxed is sufficient
    // because there is no other state whose visibility must be ordered against
    // it -- the only requirement is that the flip is seen promptly, and an
    // atomic store guarantees that without a fence on every order check.
    void engage_kill_switch() noexcept { kill_.store(true, std::memory_order_relaxed); }
    void release_kill_switch() noexcept { kill_.store(false, std::memory_order_relaxed); }
    bool kill_switch_engaged() const noexcept { return kill_.load(std::memory_order_relaxed); }

    // ---- the hot path ------------------------------------------------------
    Decision check(const OrderRequest& o, const MarketContext& mkt, double now_sec = 0.0) noexcept {
        ++st_.checked;

        if (kill_.load(std::memory_order_relaxed)) { ++st_.kill_switch; return Decision::KillSwitchEngaged; }

        if (o.symbol >= limits_.size() || !limits_[o.symbol].known) { ++st_.unknown; return Decision::UnknownSymbol; }
        const SymbolLimits& L = limits_[o.symbol];
        if (L.halted) { ++st_.halted; return Decision::SymbolHalted; }

        if (!std::isfinite(o.qty) || o.qty <= 0.0) { ++st_.bad_qty; return Decision::BadQuantity; }
        if (o.type == OrdType::Limit && (!std::isfinite(o.price) || o.price <= 0.0)) {
            ++st_.bad_price; return Decision::BadPrice;
        }

        if (o.qty > L.max_order_qty) { ++st_.max_qty; return Decision::MaxOrderQty; }

        // Notional needs a price. For a market order there is none, so the
        // reference is used -- and if there is no usable reference, the notional
        // ceiling cannot be enforced either.
        const bool have_ref = mkt.reference_ok && std::isfinite(mkt.reference_price) && mkt.reference_price > 0.0;
        const double px_for_notional = (o.type == OrdType::Limit) ? o.price
                                     : (have_ref ? mkt.reference_price : 0.0);
        if (px_for_notional > 0.0) {
            const double notional = o.qty * px_for_notional * L.contract_size;
            if (notional > L.max_order_notional) { ++st_.max_notional; return Decision::MaxNotional; }
        } else if (cfg_.reject_without_reference) {
            ++st_.no_reference; return Decision::NoReferencePrice;
        }

        // Price collar, in basis points from the reference.
        if (o.type == OrdType::Limit) {
            if (!have_ref) {
                if (cfg_.reject_without_reference) { ++st_.no_reference; return Decision::NoReferencePrice; }
            } else {
                const double dev_bps = 1e4 * std::fabs(o.price - mkt.reference_price) / mkt.reference_price;
                if (dev_bps > L.collar_bps) { ++st_.collar; return Decision::PriceCollar; }
            }
        } else if (!have_ref && cfg_.reject_without_reference) {
            // A market order with no reference price is a blind order.
            ++st_.no_reference; return Decision::NoReferencePrice;
        }

        // Position limit on the RESULTING position, which is the number that
        // breaches a mandate -- checking the current position would let every
        // individual order pass while the book walks past the limit.
        const double signed_qty = (o.side == Side::Buy) ? o.qty : -o.qty;
        const double resulting  = position_[o.symbol] + signed_qty;
        if (resulting > L.max_long_position)   { ++st_.position; return Decision::PositionLimit; }
        if (-resulting > L.max_short_position) { ++st_.position; return Decision::PositionLimit; }

        if (open_orders_[o.symbol] >= cfg_.max_open_orders_per_symbol) {
            ++st_.open_orders; return Decision::MaxOpenOrders;
        }

        // Token bucket, evaluated last so a rate-limited order is still reported
        // against the most specific reason it would have failed for.
        if (cfg_.max_orders_per_sec > 0.0) {
            refill(now_sec);
            if (tokens_ < 1.0) { ++st_.rate_limited; return Decision::MessageRateLimit; }
            tokens_ -= 1.0;
        }

        ++st_.accepted;
        return Decision::Accept;
    }

    // ---- lifecycle callbacks ----------------------------------------------
    // The gate only knows what it is told. These are how the OMS keeps it honest.
    void on_order_sent(SymbolId s) noexcept { if (s < open_orders_.size()) ++open_orders_[s]; }
    void on_order_closed(SymbolId s) noexcept {
        if (s < open_orders_.size() && open_orders_[s] > 0) --open_orders_[s];
    }
    void on_fill(SymbolId s, Side side, double qty) noexcept {
        if (s < position_.size()) position_[s] += (side == Side::Buy ? qty : -qty);
    }

    std::uint32_t open_orders(SymbolId s) const noexcept {
        return s < open_orders_.size() ? open_orders_[s] : 0u;
    }
    const GateStats& stats() const noexcept { return st_; }
    void reset_stats() noexcept { st_ = {}; }

  private:
    void refill(double now_sec) noexcept {
        if (last_refill_ == 0.0) { last_refill_ = now_sec; return; }
        const double dt = now_sec - last_refill_;
        if (dt <= 0.0) return;
        last_refill_ = now_sec;
        tokens_ += dt * cfg_.max_orders_per_sec;
        if (tokens_ > cfg_.burst_allowance) tokens_ = cfg_.burst_allowance;
    }

    GateConfig cfg_;
    std::vector<SymbolLimits> limits_;
    std::vector<double> position_;
    std::vector<std::uint32_t> open_orders_;
    std::atomic<bool> kill_{false};
    double tokens_ = 0.0, last_refill_ = 0.0;
    GateStats st_{};
};

}  // namespace mkt::risk
