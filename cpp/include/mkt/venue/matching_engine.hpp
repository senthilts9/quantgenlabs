// matching_engine.hpp — price-time priority matching engine.
//
// This is the counterparty side of the connectivity stack: it exists so the FIX
// gateway, OMS and execution algorithms can be tested end-to-end against
// something that behaves like a venue, in-process and deterministically, with no
// exchange connection and no money.
//
// PRICE-TIME PRIORITY is the rule nearly every equity and futures venue uses, and
// it has two parts that must both hold:
//   * better PRICE always fills first;
//   * at equal price, EARLIER arrival fills first.
// The second part is what makes the queue position at a price level valuable, and
// it is the part a naive implementation gets wrong by iterating a container whose
// order is unspecified. Here each price level owns an explicit FIFO of order ids,
// so time priority is a property of the data structure rather than an accident of
// iteration order.
//
// ORDERS LIVE IN A POOL, not in individually allocated nodes. The pool is a
// vector indexed by a dense internal handle; the per-level FIFO is an intrusive
// singly-linked list threaded through that pool via indices, not pointers. Two
// consequences: resting an order performs no allocation once the pool is warm, and
// the whole book is relocatable and trivially snapshot-able because it contains no
// pointers.
//
// AGGREGATE DEPTH IS DELEGATED to mkt::book::Ladder rather than recomputed. The
// Ladder already maintains per-level totals and cached best bid/ask under test, so
// the matching engine keeps it in lockstep and reads top-of-book from it. Keeping
// two independent copies of "what is the best bid" is how they come to disagree.
//
// SELF-MATCH PREVENTION is included because it is a regulatory requirement, not a
// nicety: a firm crossing its own orders creates wash trades. The engine refuses
// to match two orders with the same owner and reports it.
//
// DETERMINISM: no clocks, no randomness, no threads. Sequence numbers come from a
// counter, so a given sequence of inbound orders always produces byte-identical
// fills. That is what makes it usable as a test oracle.
#pragma once

#include "mkt/book/ladder.hpp"

#include <cstdint>
#include <functional>
#include <limits>
#include <string>
#include <unordered_map>
#include <vector>

namespace mkt::venue {

using Ticks = book::Ticks;
using Qty   = book::Qty;
using Side  = book::Side;

using OrderId = std::uint64_t;
using OwnerId = std::uint32_t;

enum class TimeInForce : std::uint8_t {
    Day = 0,   // rests until cancelled
    IOC,       // fill what you can now, cancel the remainder
    FOK,       // fill entirely and immediately, or cancel entirely
};

enum class OrdType : std::uint8_t { Limit = 0, Market = 1 };

enum class OrderState : std::uint8_t {
    New = 0, PartiallyFilled, Filled, Cancelled, Rejected,
};

enum class RejectReason : std::uint8_t {
    None = 0,
    BadQuantity,
    BadPrice,
    PriceOutOfRange,     // outside the ladder's representable band
    UnknownOrder,        // cancel/amend for an id the venue does not hold
    AlreadyTerminal,     // cancel/amend for a filled or cancelled order
    SelfMatch,
    FokUnfillable,
    NoLiquidity,         // market order with an empty opposing side
};

constexpr const char* to_string(RejectReason r) noexcept {
    switch (r) {
        case RejectReason::None:            return "none";
        case RejectReason::BadQuantity:     return "bad_quantity";
        case RejectReason::BadPrice:        return "bad_price";
        case RejectReason::PriceOutOfRange: return "price_out_of_range";
        case RejectReason::UnknownOrder:    return "unknown_order";
        case RejectReason::AlreadyTerminal: return "already_terminal";
        case RejectReason::SelfMatch:       return "self_match";
        case RejectReason::FokUnfillable:   return "fok_unfillable";
        case RejectReason::NoLiquidity:     return "no_liquidity";
    }
    return "unknown";
}

struct NewOrder {
    OrderId     id    = 0;        // client-assigned, must be unique
    OwnerId     owner = 0;
    Side        side  = Side::Bid;
    Ticks       price = 0;        // ignored for Market
    Qty         qty   = 0;
    OrdType     type  = OrdType::Limit;
    TimeInForce tif   = TimeInForce::Day;
};

// A single execution. Emitted for BOTH sides of every match, because each party
// needs its own fill record -- a venue that reports one side is unauditable.
struct Fill {
    std::uint64_t exec_seq = 0;
    OrderId aggressor = 0, resting = 0;
    OwnerId aggressor_owner = 0, resting_owner = 0;
    Side    aggressor_side = Side::Bid;
    Ticks   price = 0;            // resting order's price: the passive side sets it
    Qty     qty   = 0;
};

struct OrderResult {
    OrderId      id = 0;
    OrderState   state = OrderState::New;
    RejectReason reject = RejectReason::None;
    Qty          filled = 0;      // this request's cumulative fill
    Qty          leaves = 0;
    double       avg_price_ticks = 0.0;
    std::size_t  fill_count = 0;
};

struct VenueStats {
    std::uint64_t orders_accepted = 0, orders_rejected = 0;
    std::uint64_t fills = 0, cancels = 0, amends = 0;
    std::uint64_t self_match_blocks = 0;
    Qty           volume = 0;
};

class MatchingEngine {
  public:
    // Fill callback: invoked once per execution, in match order. Kept as a
    // std::function because the venue emits at most one per match and the cost is
    // dwarfed by the FIX encode that follows; the hot inner loop does not call it
    // per price level.
    using FillHandler = std::function<void(const Fill&)>;

    MatchingEngine(Ticks floor_ticks, std::size_t n_ticks, bool prevent_self_match = true)
        : book_(floor_ticks, n_ticks),
          levels_(n_ticks * 2, Level{}),
          n_ticks_(n_ticks),
          prevent_self_match_(prevent_self_match) {
        pool_.reserve(4096);
    }

    void on_fill(FillHandler h) { fill_handler_ = std::move(h); }

    // ---- inbound -----------------------------------------------------------
    OrderResult submit(const NewOrder& o) {
        OrderResult r;
        r.id = o.id;

        if (o.qty <= 0) return reject(r, RejectReason::BadQuantity);
        if (o.type == OrdType::Limit) {
            if (o.price <= 0) return reject(r, RejectReason::BadPrice);
            if (!book_.in_range(o.price)) return reject(r, RejectReason::PriceOutOfRange);
        }

        // A market order is modelled as a limit at the extreme of the ladder,
        // which is exactly what "take whatever is there" means on a bounded price
        // grid -- and it keeps one matching path rather than two.
        const Ticks limit = (o.type == OrdType::Market)
                          ? (o.side == Side::Bid ? book_.ceil_ticks() : book_.floor_ticks())
                          : o.price;

        if (o.type == OrdType::Market && !book_.has_side(book::opposite(o.side)))
            return reject(r, RejectReason::NoLiquidity);

        // FOK must be decided BEFORE any fill is emitted: it is all-or-nothing, so
        // filling partially and then cancelling would have already printed trades
        // that should never have happened.
        if (o.tif == TimeInForce::FOK && available_against(o, limit) < o.qty)
            return reject(r, RejectReason::FokUnfillable);

        Qty remaining = o.qty;
        double notional = 0.0;
        match(o, limit, remaining, notional, r);

        r.filled = o.qty - remaining;
        r.leaves = remaining;
        r.avg_price_ticks = r.filled > 0 ? notional / static_cast<double>(r.filled) : 0.0;

        if (remaining == 0) {
            r.state = OrderState::Filled;
        } else if (o.tif == TimeInForce::IOC || o.type == OrdType::Market) {
            // Nothing left to do at a price we are willing to pay: cancel the rest.
            r.state = r.filled > 0 ? OrderState::PartiallyFilled : OrderState::Cancelled;
        } else {
            rest(o, limit, remaining);
            r.state = r.filled > 0 ? OrderState::PartiallyFilled : OrderState::New;
        }

        ++st_.orders_accepted;
        return r;
    }

    OrderResult cancel(OrderId id, OwnerId owner) {
        OrderResult r; r.id = id;
        const auto it = index_.find(id);
        if (it == index_.end()) return reject(r, RejectReason::UnknownOrder);
        Entry& e = pool_[it->second];
        if (e.terminal || e.owner != owner) return reject(r, RejectReason::UnknownOrder);
        if (e.leaves <= 0) return reject(r, RejectReason::AlreadyTerminal);
        remove_from_level(it->second);
        e.terminal = true;
        r.state = OrderState::Cancelled;
        r.leaves = 0;
        ++st_.cancels;
        return r;
    }

    // Amend. A price change or a quantity INCREASE loses time priority, which is
    // the venue rule rather than an implementation shortcut: allowing a size
    // increase to keep queue position would let a participant reserve a place in
    // the queue with a small order and inflate it once it reached the front.
    // A quantity DECREASE keeps priority.
    OrderResult amend(OrderId id, OwnerId owner, Ticks new_price, Qty new_qty) {
        OrderResult r; r.id = id;
        const auto it = index_.find(id);
        if (it == index_.end()) return reject(r, RejectReason::UnknownOrder);
        Entry& e = pool_[it->second];
        if (e.terminal || e.owner != owner) return reject(r, RejectReason::UnknownOrder);
        if (new_qty <= 0) return reject(r, RejectReason::BadQuantity);
        if (!book_.in_range(new_price)) return reject(r, RejectReason::PriceOutOfRange);

        const bool keeps_priority = (new_price == e.price) && (new_qty <= e.leaves);
        if (keeps_priority) {
            const Qty delta = new_qty - e.leaves;
            e.leaves = new_qty;
            book_.add(e.side, e.price, delta, 1);
            ++st_.amends;
            r.state = OrderState::New;
            r.leaves = new_qty;
            return r;
        }

        // Cancel-replace: pull it, then resubmit so it re-crosses the book if the
        // new price is now aggressive.
        const Side side = e.side;
        const OwnerId own = e.owner;
        remove_from_level(it->second);
        e.terminal = true;
        ++st_.amends;

        NewOrder repl;
        repl.id = id; repl.owner = own; repl.side = side;
        repl.price = new_price; repl.qty = new_qty;
        repl.type = OrdType::Limit; repl.tif = TimeInForce::Day;
        index_.erase(id);                  // the replacement re-registers the id
        return submit(repl);
    }

    // ---- inspection --------------------------------------------------------
    const book::Ladder& book() const noexcept { return book_; }
    const VenueStats& stats() const noexcept { return st_; }

    bool best(Side s, Ticks& px, Qty& qty) const noexcept { return book_.best(s, px, qty); }

    // Queue position of a resting order at its level, 0 = front. O(queue length),
    // and deliberately not cached: it is a diagnostic for tests and TCA, not a
    // hot-path operation.
    std::size_t queue_position(OrderId id) const {
        const auto it = index_.find(id);
        if (it == index_.end()) return std::numeric_limits<std::size_t>::max();
        const Entry& e = pool_[it->second];
        if (e.terminal) return std::numeric_limits<std::size_t>::max();
        const Level& L = level_of(e.side, e.price);
        std::size_t pos = 0;
        for (std::uint32_t cur = L.head; cur != kNil; cur = pool_[cur].next, ++pos)
            if (cur == it->second) return pos;
        return std::numeric_limits<std::size_t>::max();
    }

    Qty leaves(OrderId id) const {
        const auto it = index_.find(id);
        if (it == index_.end()) return 0;
        const Entry& e = pool_[it->second];
        return e.terminal ? 0 : e.leaves;
    }

    bool is_resting(OrderId id) const { return leaves(id) > 0; }
    std::uint64_t exec_count() const noexcept { return exec_seq_; }

  private:
    static constexpr std::uint32_t kNil = 0xFFFFFFFFu;

    struct Entry {
        OrderId  id = 0;
        OwnerId  owner = 0;
        Side     side = Side::Bid;
        Ticks    price = 0;
        Qty      leaves = 0;
        std::uint32_t next = kNil;   // intrusive FIFO link, an index not a pointer
        bool     terminal = false;
    };

    struct Level { std::uint32_t head = kNil, tail = kNil; };

    OrderResult& reject(OrderResult& r, RejectReason why) {
        r.state = OrderState::Rejected;
        r.reject = why;
        ++st_.orders_rejected;
        if (why == RejectReason::SelfMatch) ++st_.self_match_blocks;
        return r;
    }

    std::size_t level_index(Side s, Ticks px) const noexcept {
        const std::size_t base = (s == Side::Bid) ? 0 : n_ticks_;
        return base + static_cast<std::size_t>(px - book_.floor_ticks());
    }
    Level& level_of(Side s, Ticks px) noexcept { return levels_[level_index(s, px)]; }
    const Level& level_of(Side s, Ticks px) const noexcept { return levels_[level_index(s, px)]; }

    // How much of `o` could fill right now, respecting self-match prevention.
    // Needed by FOK, which must know before printing anything.
    Qty available_against(const NewOrder& o, Ticks limit) const {
        const Side opp = book::opposite(o.side);
        Qty total = 0;
        Ticks px; Qty q;
        if (!book_.best(opp, px, q)) return 0;
        const Ticks stop = limit;
        for (Ticks p = px; crossable(o.side, p, stop); p += (o.side == Side::Bid ? 1 : -1)) {
            if (!book_.in_range(p)) break;
            const Level& L = level_of(opp, p);
            for (std::uint32_t cur = L.head; cur != kNil; cur = pool_[cur].next) {
                const Entry& e = pool_[cur];
                if (prevent_self_match_ && e.owner == o.owner) continue;
                total += e.leaves;
            }
            if (total >= o.qty) break;
        }
        return total;
    }

    // Would an order on `aggressor_side` with limit `stop` trade at price `p`?
    static bool crossable(Side aggressor_side, Ticks p, Ticks stop) noexcept {
        return aggressor_side == Side::Bid ? (p <= stop) : (p >= stop);
    }

    void match(const NewOrder& o, Ticks limit, Qty& remaining, double& notional, OrderResult& r) {
        const Side opp = book::opposite(o.side);
        while (remaining > 0) {
            Ticks px; Qty avail;
            if (!book_.best(opp, px, avail)) break;
            if (!crossable(o.side, px, limit)) break;

            Level& L = level_of(opp, px);
            bool progressed = false;
            std::uint32_t prev = kNil;
            std::uint32_t cur = L.head;
            while (cur != kNil && remaining > 0) {
                Entry& e = pool_[cur];
                if (prevent_self_match_ && e.owner == o.owner) {
                    ++st_.self_match_blocks;
                    prev = cur; cur = e.next;       // skip, do not trade
                    continue;
                }
                const Qty take = (e.leaves < remaining) ? e.leaves : remaining;
                e.leaves  -= take;
                remaining -= take;
                notional  += static_cast<double>(take) * static_cast<double>(px);
                book_.add(opp, px, -take, 1);
                st_.volume += take;
                ++st_.fills;
                ++r.fill_count;
                progressed = true;

                if (fill_handler_) {
                    Fill f;
                    f.exec_seq        = ++exec_seq_;
                    f.aggressor       = o.id;
                    f.resting         = e.id;
                    f.aggressor_owner = o.owner;
                    f.resting_owner   = e.owner;
                    f.aggressor_side  = o.side;
                    f.price           = px;          // passive side sets the price
                    f.qty             = take;
                    fill_handler_(f);
                }

                if (e.leaves == 0) {                 // fully filled: unlink
                    e.terminal = true;
                    const std::uint32_t next = e.next;
                    if (prev == kNil) L.head = next; else pool_[prev].next = next;
                    if (L.tail == cur) L.tail = prev;
                    cur = next;
                } else {
                    prev = cur; cur = e.next;
                }
            }
            // Every order at the best level was ours (self-match skipped): without
            // this the loop would spin forever on a level it can never consume.
            if (!progressed) break;
        }
    }

    void rest(const NewOrder& o, Ticks price, Qty qty) {
        const std::uint32_t h = static_cast<std::uint32_t>(pool_.size());
        Entry e;
        e.id = o.id; e.owner = o.owner; e.side = o.side;
        e.price = price; e.leaves = qty; e.next = kNil;
        pool_.push_back(e);
        index_[o.id] = h;

        Level& L = level_of(o.side, price);
        if (L.tail == kNil) { L.head = L.tail = h; }     // first at this level
        else { pool_[L.tail].next = h; L.tail = h; }     // FIFO: append = later
        book_.add(o.side, price, qty, 1);
    }

    void remove_from_level(std::uint32_t h) {
        Entry& e = pool_[h];
        Level& L = level_of(e.side, e.price);
        std::uint32_t prev = kNil;
        for (std::uint32_t cur = L.head; cur != kNil; prev = cur, cur = pool_[cur].next) {
            if (cur != h) continue;
            const std::uint32_t next = pool_[cur].next;
            if (prev == kNil) L.head = next; else pool_[prev].next = next;
            if (L.tail == cur) L.tail = prev;
            break;
        }
        if (e.leaves > 0) book_.add(e.side, e.price, -e.leaves, 1);
        e.leaves = 0;
    }

    book::Ladder book_;
    std::vector<Entry> pool_;
    std::vector<Level> levels_;
    std::unordered_map<OrderId, std::uint32_t> index_;
    std::size_t n_ticks_;
    bool prevent_self_match_;
    std::uint64_t exec_seq_ = 0;
    FillHandler fill_handler_;
    VenueStats st_{};
};

}  // namespace mkt::venue
