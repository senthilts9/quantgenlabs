// fix_venue.hpp — a simulated venue that speaks FIX.
//
// Composes the three pieces that already exist and are already tested, and adds
// only the translation between them:
//
//   mkt::fix::Session          session/sequence/heartbeat correctness
//   mkt::venue::MatchingEngine price-time priority matching
//   this file                  FIX <-> order-object translation, ExecutionReports
//
// That split is the point. The matching engine has no idea FIX exists, the session
// layer has no idea what an order is, and this file contains no matching logic and
// no sequence-number logic. Each was tested in isolation before being composed, so
// a failure here is a translation bug and nothing else.
//
// WHAT IT IS FOR. Section 7 of the specification requires FIX connectivity in a
// safe test environment, and rule 6 requires that it initially target simulated
// venues. This is that venue. It executes nothing real, moves no money, and
// touches no exchange: it is an in-process counterparty whose purpose is to make
// the complete lifecycle -- order out, ack back, fills back, cancel, reject --
// exercisable in a unit test and demonstrable in a UI.
//
// EXECUTIONREPORT IS THE WORKHORSE of FIX and the easiest message to get subtly
// wrong, because one message type carries every lifecycle transition, and the
// combination of fields must be internally consistent:
//   * ExecType (150) says what JUST HAPPENED (this event).
//   * OrdStatus (39) says what the ORDER IS NOW (cumulative state).
// Those two are routinely conflated. A partial fill is ExecType=F (Trade) with
// OrdStatus=1 (Partially filled); the final fill of the same order is ExecType=F
// with OrdStatus=2 (Filled). Meanwhile LeavesQty + CumQty must always equal the
// order quantity, and AvgPx must be the running average over all fills -- not the
// price of the latest one. Every one of those invariants is asserted in the tests.
#pragma once

#include "mkt/fix/session.hpp"
#include "mkt/venue/matching_engine.hpp"

#include <string>
#include <unordered_map>

namespace mkt::venue {

// FIX ExecType (150) values used here.
namespace exec_type {
constexpr std::string_view New            = "0";
constexpr std::string_view Cancelled      = "4";
constexpr std::string_view Replaced       = "5";
constexpr std::string_view Rejected       = "8";
constexpr std::string_view Trade          = "F";
}  // namespace exec_type

// FIX OrdStatus (39) values.
namespace ord_status {
constexpr std::string_view New             = "0";
constexpr std::string_view PartiallyFilled = "1";
constexpr std::string_view Filled          = "2";
constexpr std::string_view Cancelled       = "4";
constexpr std::string_view Rejected        = "8";
}  // namespace ord_status

class FixVenue {
  public:
    // SELF-MATCH PREVENTION DEFAULTS OFF HERE, and that is deliberate.
    //
    // MatchingEngine defaults it ON, which is correct for a venue with many
    // participants: a firm crossing its own orders creates wash trades. But this
    // venue carries ONE FIX session, so every order it receives belongs to the
    // same participant. With prevention on, no order from that session could ever
    // trade against another, and nothing would ever fill -- the client could post
    // a bid and an ask at the same price and watch them sit there.
    //
    // A single-session test venue exists precisely so one client can exercise both
    // sides of the book, so the default is off. Pass true to model a venue where
    // the client must not cross itself, and use seed_liquidity() to rest orders
    // under a different participant id for the realistic arrangement.
    FixVenue(fix::SessionConfig cfg, Ticks floor_ticks, std::size_t n_ticks,
             double tick_size = 0.01, bool prevent_self_match = false)
        : session_(std::move(cfg)),
          engine_(floor_ticks, n_ticks, prevent_self_match),
          tick_size_(tick_size) {
        // Fills are collected rather than reported immediately: a single inbound
        // order can generate several, and the ExecutionReports must carry the
        // running CumQty/AvgPx, which is only known after the whole match.
        engine_.on_fill([this](const Fill& f) { pending_fills_.push_back(f); });
    }

    // Rests liquidity under a participant id that is NOT the FIX client's, so a
    // test or demo can trade against "the market" rather than against itself. This
    // is how to exercise self-match prevention meaningfully, and how to give a UI
    // demo a book that already has depth in it.
    OrderResult seed_liquidity(Side side, double price, Qty qty, OwnerId owner = 99) {
        NewOrder o;
        o.id = ++next_internal_id_;
        o.owner = owner;
        o.side = side;
        o.price = to_ticks(price);
        o.qty = qty;
        o.type = OrdType::Limit;
        o.tif = TimeInForce::Day;
        pending_fills_.clear();
        const auto r = engine_.submit(o);
        pending_fills_.clear();   // seeded liquidity emits no ExecutionReports
        return r;
    }

    void listen(std::int64_t now_ns) { session_.listen(now_ns); }
    void connect(std::int64_t now_ns) { session_.connect(now_ns); }

    // Feed one complete inbound FIX message. Any ExecutionReports or rejects it
    // produces land in outbox().
    void on_wire(std::string_view raw, std::int64_t now_ns) {
        fix::Message m;
        const auto verdict = session_.on_message(raw, now_ns, m);
        if (verdict != fix::Inbound::Application) return;

        const auto mt = m.msg_type();
        if      (mt == fix::msg::NewOrderSingle)     handle_new(m, now_ns);
        else if (mt == fix::msg::OrderCancelRequest) handle_cancel(m, now_ns);
        else if (mt == fix::msg::OrderCancelReplace) handle_replace(m, now_ns);
    }

    bool on_timer(std::int64_t now_ns) { return session_.on_timer(now_ns); }

    std::vector<std::string>& outbox() noexcept { return session_.outbox(); }
    void clear_outbox() noexcept { session_.clear_outbox(); }

    const MatchingEngine& engine() const noexcept { return engine_; }
    const fix::Session& session() const noexcept { return session_; }
    fix::SessionState state() const noexcept { return session_.state(); }

    Ticks to_ticks(double price) const noexcept {
        // Round rather than truncate: 1.0999999999999999 must land on tick 110,
        // not 109. Truncation here is a classic off-by-one-tick bug.
        return static_cast<Ticks>(price / tick_size_ + (price >= 0 ? 0.5 : -0.5));
    }
    double to_price(Ticks t) const noexcept { return static_cast<double>(t) * tick_size_; }

  private:
    static constexpr OwnerId kClientOwner = 1;

    // Per-order cumulative state. FIX requires CumQty and AvgPx to be cumulative
    // over the order's whole life, so they cannot be derived from one fill.
    struct OrderBookkeeping {
        std::string cl_ord_id;
        std::string symbol;
        std::string side;          // FIX 54 as received
        Qty  order_qty = 0;
        Qty  cum_qty   = 0;
        double notional_ticks = 0.0;
        bool terminal = false;
    };

    void handle_new(const fix::Message& m, std::int64_t now_ns) {
        const std::string cl_ord_id(m.get_or(fix::tag::ClOrdID));
        const std::string symbol(m.get_or(fix::tag::Symbol));
        const std::string side(m.get_or(fix::tag::Side));
        const auto qty_opt = m.get_double(fix::tag::OrderQty);
        const auto ord_type = m.get_or(fix::tag::OrdType, "2");
        const auto tif_raw  = m.get_or(fix::tag::TimeInForce, "0");

        if (cl_ord_id.empty() || !qty_opt || *qty_opt <= 0.0 || (side != "1" && side != "2")) {
            send_order_reject(cl_ord_id, symbol, side, 0, "malformed NewOrderSingle", now_ns);
            return;
        }

        NewOrder o;
        o.id    = ++next_internal_id_;
        o.owner = kClientOwner;                        // one FIX session == one participant
        o.side  = (side == "1") ? Side::Bid : Side::Ask;
        o.qty   = static_cast<Qty>(*qty_opt);
        o.type  = (ord_type == "1") ? OrdType::Market : OrdType::Limit;
        o.tif   = (tif_raw == "3") ? TimeInForce::IOC
                : (tif_raw == "4") ? TimeInForce::FOK
                                   : TimeInForce::Day;

        if (o.type == OrdType::Limit) {
            const auto px = m.get_double(fix::tag::Price);
            if (!px || *px <= 0.0) {
                send_order_reject(cl_ord_id, symbol, side, o.qty, "limit order without a valid Price", now_ns);
                return;
            }
            o.price = to_ticks(*px);
        }

        pending_fills_.clear();
        const auto res = engine_.submit(o);

        OrderBookkeeping bk;
        bk.cl_ord_id = cl_ord_id; bk.symbol = symbol; bk.side = side;
        bk.order_qty = o.qty;

        if (res.state == OrderState::Rejected) {
            send_order_reject(cl_ord_id, symbol, side, o.qty, to_string(res.reject), now_ns);
            return;
        }

        by_internal_[o.id] = cl_ord_id;
        books_[cl_ord_id] = bk;
        const std::string venue_id = "V-" + std::to_string(o.id);
        venue_ids_[cl_ord_id] = venue_id;

        // An ack precedes any fill report. A client that receives a Trade for an
        // order it was never told the venue accepted cannot reconcile its blotter.
        emit_exec(cl_ord_id, exec_type::New, ord_status::New, 0, 0.0, now_ns);

        drain_fills(cl_ord_id, now_ns);

        // IOC/FOK/market remainders are cancelled by the venue, and the client must
        // be told, or it will wait forever for a fill that is not coming.
        auto& b = books_[cl_ord_id];
        if (!b.terminal && res.leaves > 0 && !engine_.is_resting(o.id)) {
            b.terminal = true;
            emit_exec(cl_ord_id, exec_type::Cancelled, ord_status::Cancelled, 0, 0.0, now_ns);
        }
    }

    void handle_cancel(const fix::Message& m, std::int64_t now_ns) {
        const std::string orig(m.get_or(fix::tag::OrigClOrdID));
        const std::string cl(m.get_or(fix::tag::ClOrdID));
        const std::uint64_t internal = internal_for(orig);
        if (internal == 0) { send_cancel_reject(cl, orig, "unknown order", now_ns); return; }

        const auto res = engine_.cancel(internal, kClientOwner);
        if (res.state != OrderState::Cancelled) {
            send_cancel_reject(cl, orig, to_string(res.reject), now_ns);
            return;
        }
        books_[orig].terminal = true;
        emit_exec(orig, exec_type::Cancelled, ord_status::Cancelled, 0, 0.0, now_ns, cl);
    }

    void handle_replace(const fix::Message& m, std::int64_t now_ns) {
        const std::string orig(m.get_or(fix::tag::OrigClOrdID));
        const std::string cl(m.get_or(fix::tag::ClOrdID));
        const std::uint64_t internal = internal_for(orig);
        if (internal == 0) { send_cancel_reject(cl, orig, "unknown order", now_ns); return; }

        const auto px  = m.get_double(fix::tag::Price);
        const auto qty = m.get_double(fix::tag::OrderQty);
        if (!px || !qty || *px <= 0.0 || *qty <= 0.0) {
            send_cancel_reject(cl, orig, "malformed replace", now_ns); return;
        }

        pending_fills_.clear();
        const auto res = engine_.amend(internal, kClientOwner, to_ticks(*px), static_cast<Qty>(*qty));
        if (res.state == OrderState::Rejected) {
            send_cancel_reject(cl, orig, to_string(res.reject), now_ns); return;
        }

        // The replacement carries the NEW ClOrdID, and its cumulative counters
        // carry over from the original -- a replaced order is the same order.
        OrderBookkeeping bk = books_[orig];
        bk.cl_ord_id = cl;
        bk.order_qty = static_cast<Qty>(*qty);
        bk.terminal = false;
        books_[cl] = bk;
        by_internal_[internal] = cl;
        venue_ids_[cl] = venue_ids_[orig];

        emit_exec(cl, exec_type::Replaced, ord_status::New, 0, 0.0, now_ns, orig);
        drain_fills(cl, now_ns);
    }

    // Converts the fills the engine produced into ExecutionReports, maintaining
    // CumQty/AvgPx across them.
    void drain_fills(const std::string& cl_ord_id, std::int64_t now_ns) {
        auto it = books_.find(cl_ord_id);
        if (it == books_.end()) return;
        OrderBookkeeping& b = it->second;

        for (const Fill& f : pending_fills_) {
            b.cum_qty        += f.qty;
            b.notional_ticks += static_cast<double>(f.qty) * static_cast<double>(f.price);
            const bool done = (b.cum_qty >= b.order_qty);
            if (done) b.terminal = true;
            // ExecType says what just happened (a trade); OrdStatus says what the
            // order now is (partially filled, or filled). Conflating the two is the
            // most common ExecutionReport error.
            emit_exec(cl_ord_id, exec_type::Trade,
                      done ? ord_status::Filled : ord_status::PartiallyFilled,
                      f.qty, to_price(f.price), now_ns);
        }
        pending_fills_.clear();
    }

    void emit_exec(const std::string& cl_ord_id, std::string_view etype,
                   std::string_view ostatus, Qty last_qty, double last_px,
                   std::int64_t now_ns, std::string_view orig_cl_ord_id = {}) {
        const auto it = books_.find(cl_ord_id);
        if (it == books_.end()) return;
        const OrderBookkeeping& b = it->second;
        const Qty leaves = (ostatus == ord_status::Cancelled || ostatus == ord_status::Filled)
                         ? 0 : (b.order_qty - b.cum_qty);
        const double avg = b.cum_qty > 0 ? (b.notional_ticks / static_cast<double>(b.cum_qty)) * tick_size_ : 0.0;

        session_.send_app(fix::msg::ExecutionReport, [&](fix::Builder& bld) {
            bld.add(fix::tag::OrderID, venue_ids_.count(cl_ord_id) ? venue_ids_[cl_ord_id] : "V-0")
               .add(fix::tag::ClOrdID, cl_ord_id);
            if (!orig_cl_ord_id.empty()) bld.add(fix::tag::OrigClOrdID, orig_cl_ord_id);
            bld.add(fix::tag::ExecID, "E-" + std::to_string(++exec_id_))
               .add(fix::tag::ExecType, etype)
               .add(fix::tag::OrdStatus, ostatus)
               .add(fix::tag::Symbol, b.symbol)
               .add(fix::tag::Side, b.side)
               .add_price(fix::tag::OrderQty, static_cast<double>(b.order_qty), 0);
            if (last_qty > 0) {
                bld.add_price(fix::tag::LastQty, static_cast<double>(last_qty), 0)
                   .add_price(fix::tag::LastPx, last_px, 2);
            }
            // LeavesQty + CumQty == OrderQty is an invariant a client reconciles
            // against, so both are always present.
            bld.add_price(fix::tag::LeavesQty, static_cast<double>(leaves), 0)
               .add_price(fix::tag::CumQty, static_cast<double>(b.cum_qty), 0)
               .add_price(fix::tag::AvgPx, avg, 2)
               .add(fix::tag::TransactTime, "20260928-00:00:00.000");
        }, now_ns);
    }

    void send_order_reject(const std::string& cl_ord_id, const std::string& symbol,
                           const std::string& side, Qty qty, std::string_view text,
                           std::int64_t now_ns) {
        session_.send_app(fix::msg::ExecutionReport, [&](fix::Builder& b) {
            b.add(fix::tag::OrderID, "NONE")
             .add(fix::tag::ClOrdID, cl_ord_id)
             .add(fix::tag::ExecID, "E-" + std::to_string(++exec_id_))
             .add(fix::tag::ExecType, exec_type::Rejected)
             .add(fix::tag::OrdStatus, ord_status::Rejected)
             .add(fix::tag::Symbol, symbol)
             .add(fix::tag::Side, side.empty() ? "1" : side)
             .add_price(fix::tag::OrderQty, static_cast<double>(qty), 0)
             .add_price(fix::tag::LeavesQty, 0.0, 0)
             .add_price(fix::tag::CumQty, 0.0, 0)
             .add(fix::tag::Text, text);
        }, now_ns);
        ++rejects_sent_;
    }

    // A cancel that cannot be honoured gets an OrderCancelReject (35=9), NOT an
    // ExecutionReject. Sending the wrong one leaves the client's state machine
    // waiting for a transition that never arrives.
    void send_cancel_reject(const std::string& cl_ord_id, const std::string& orig,
                            std::string_view text, std::int64_t now_ns) {
        session_.send_app(fix::msg::OrderCancelReject, [&](fix::Builder& b) {
            b.add(fix::tag::OrderID, venue_ids_.count(orig) ? venue_ids_[orig] : "NONE")
             .add(fix::tag::ClOrdID, cl_ord_id)
             .add(fix::tag::OrigClOrdID, orig)
             .add(fix::tag::OrdStatus, ord_status::Rejected)
             .add(fix::tag::CxlRejResponseTo, "1")
             .add(fix::tag::Text, text);
        }, now_ns);
        ++rejects_sent_;
    }

    std::uint64_t internal_for(const std::string& cl_ord_id) const {
        for (const auto& [internal, cl] : by_internal_)
            if (cl == cl_ord_id) return internal;
        return 0;
    }

    fix::Session session_;
    MatchingEngine engine_;
    double tick_size_;
    std::vector<Fill> pending_fills_;
    std::unordered_map<std::string, OrderBookkeeping> books_;
    std::unordered_map<std::string, std::string> venue_ids_;
    std::unordered_map<std::uint64_t, std::string> by_internal_;
    std::uint64_t next_internal_id_ = 0, exec_id_ = 0, rejects_sent_ = 0;
};

}  // namespace mkt::venue
