// market_maker.hpp — Avellaneda-Stoikov quoting with inventory control.
//
// THIS IS THE STRATEGY THE PLATFORM DID NOT HAVE. Everything before it was
// plumbing: a book, a FIX engine, a matching engine, a 209ns tick-to-order path.
// None of that decides what to trade. A market maker does: it quotes both sides
// continuously and earns the spread, and its entire problem is that the quotes
// it wants to leave up are exactly the ones informed traders want to hit.
//
// THE MODEL (Avellaneda & Stoikov, 2008). Two quantities:
//
//   reservation price  r = s - q * gamma * sigma^2 * (T-t)
//   optimal half-spread  = (gamma * sigma^2 * (T-t) + (2/gamma) * ln(1 + gamma/kappa)) / 2
//
// where s is the mid, q is signed inventory, gamma is risk aversion, sigma is
// volatility, kappa is order-flow intensity and (T-t) is time to horizon.
//
// WHAT THE RESERVATION PRICE ACTUALLY DOES, since this is the part that matters
// and the part that is easy to get backwards: it SKEWS QUOTES AWAY FROM
// INVENTORY. Long inventory pushes r BELOW the mid, which lowers both quotes,
// which makes the maker more likely to be hit on the bid... no — it makes the
// ASK more attractive to buyers and the BID less attractive to sellers, so the
// maker sheds inventory. The sign is q NEGATIVE in the formula for exactly that
// reason, and inverting it produces a strategy that accumulates inventory into a
// losing position and looks like bad luck for a long time.
//
// WHY THE SPREAD WIDENS WITH VOLATILITY AND RISK AVERSION: both terms increase
// the cost of holding inventory you did not choose. A maker quoting a constant
// spread through a volatility spike is offering a free option to anyone who knows
// the market is about to move.
//
// WHAT THIS MODEL DOES NOT CAPTURE, stated because using it without knowing is
// how people lose money with it:
//   * It assumes a symmetric, uninformed order flow. Real flow is not: some of
//     it knows something. Adverse selection is handled separately (markout.hpp),
//     not by this formula.
//   * kappa is a fitted constant here. In reality flow intensity varies by time
//     of day, by regime and with the spread itself.
//   * It has no queue position. A quote at the front of the queue and one behind
//     10,000 lots are the same to this model and very different in practice --
//     which is why QueueTracker exists beside it.
#pragma once

#include "mkt/hft/signals.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace mkt::hft {

struct MakerParams {
    // Risk aversion. Higher = wider quotes and harder inventory skew. The single
    // most important knob: it is the price the maker puts on holding risk.
    double gamma = 0.1;
    // Order-flow intensity. Higher kappa = flow is more sensitive to price, so
    // the optimal spread is TIGHTER (you do not need to pay as much for fills).
    double kappa = 1.5;
    // Time to horizon, normalised to [0,1]. As it goes to zero the inventory
    // term vanishes: at the end of the session there is no time left to be hurt
    // by a position, so the maker stops skewing and quotes symmetrically.
    double time_to_horizon = 1.0;
    // Hard bounds, applied after the model. A model producing a negative or
    // absurd spread must not be allowed to quote it.
    double min_half_spread_ticks = 0.5;
    double max_half_spread_ticks = 100.0;
    // Inventory limit. At or beyond it the maker quotes ONE SIDE ONLY -- the one
    // that reduces the position. Continuing to quote both sides at a limit is
    // how a maker turns a position it cannot hold into a larger one.
    double max_inventory = 1000.0;
    // Weight on the microstructure edge when adjusting fair value, in ticks per
    // unit of edge. Zero disables it and leaves pure Avellaneda-Stoikov.
    double edge_ticks = 0.0;
};

struct Quote {
    double bid_ticks = 0.0;
    double ask_ticks = 0.0;
    double reservation = 0.0;
    double half_spread = 0.0;
    double inventory_skew = 0.0;    // reservation - mid; negative when long
    bool   quote_bid = true;
    bool   quote_ask = true;
    const char* suppressed_reason = "";
};

class MarketMaker {
  public:
    explicit MarketMaker(MakerParams p = {}) : p_(p) {}

    MakerParams& params() noexcept { return p_; }
    const MakerParams& params() const noexcept { return p_; }

    void on_fill(double signed_qty) noexcept { inventory_ += signed_qty; }
    void set_inventory(double q) noexcept { inventory_ = q; }
    double inventory() const noexcept { return inventory_; }

    // `mid` and `sigma` are both in TICKS. Mixing ticks and prices here silently
    // rescales the inventory term by the tick size, which is the kind of error
    // that produces quotes that look plausible and are wrong by a factor.
    Quote quote(double mid, double sigma_ticks, double edge = 0.0) const noexcept {
        Quote q;
        const double t = std::clamp(p_.time_to_horizon, 0.0, 1.0);
        const double var = sigma_ticks * sigma_ticks;

        // Reservation price. q NEGATIVE: long inventory pushes fair value DOWN so
        // the maker sheds it. Inverting this sign accumulates into losses.
        q.reservation = mid - inventory_ * p_.gamma * var * t;

        // Optional tilt from microstructure. Separate from the inventory skew
        // because they answer different questions: one is "where do I want to
        // hold risk", the other is "where is the price going".
        if (p_.edge_ticks != 0.0) q.reservation += edge * p_.edge_ticks;

        // Optimal spread. The ln term is the compensation for flow intensity;
        // guard kappa and gamma against zero so a misconfiguration produces a
        // wide quote rather than an infinity.
        const double g = std::max(p_.gamma, 1e-9);
        const double k = std::max(p_.kappa, 1e-9);
        const double total = g * var * t + (2.0 / g) * std::log1p(g / k);
        q.half_spread = std::clamp(total / 2.0, p_.min_half_spread_ticks,
                                   p_.max_half_spread_ticks);

        q.bid_ticks = q.reservation - q.half_spread;
        q.ask_ticks = q.reservation + q.half_spread;
        q.inventory_skew = q.reservation - mid;

        // At the inventory limit, quote only the side that reduces the position.
        if (inventory_ >= p_.max_inventory) {
            q.quote_bid = false;
            q.suppressed_reason = "long inventory limit: bid suppressed so the "
                                  "position can only shrink";
        } else if (inventory_ <= -p_.max_inventory) {
            q.quote_ask = false;
            q.suppressed_reason = "short inventory limit: ask suppressed so the "
                                  "position can only shrink";
        }
        return q;
    }

    void reset() noexcept { inventory_ = 0.0; }

  private:
    MakerParams p_;
    double inventory_ = 0.0;
};

}  // namespace mkt::hft
