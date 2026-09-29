// desk.hpp — a client desk wired to the simulated venue.
//
// Pairs a client-side fix::Session with a FixVenue and pumps messages between
// them, keeping the blotter a real OMS would keep: one row per ClOrdID, updated
// from the ExecutionReports that come back rather than from what we hoped would
// happen. That distinction is the whole point of a blotter -- an OMS that
// updates its own state optimistically at send time and reconciles later is how
// a desk ends up disagreeing with its broker about what it owns.
//
// WHY THIS EXISTS as a C++ component rather than as orchestration in Python.
// Two reasons. It is testable here with no interpreter and no HTTP, so the
// lifecycle logic is pinned by the same zero-dependency harness as everything
// else. And it keeps FIX ENCODING inside C++: the session layer owns sequence
// numbers, BodyLength and CheckSum, and a caller that could assemble its own FIX
// could produce a message the session never sequenced. Python asks for "a limit
// buy of 500" and gets back reports; it never touches the wire format.
//
// The wire log keeps every message in both directions, because the most useful
// thing a FIX monitor can show is the actual bytes -- SOH rendered as '|' -- next
// to the blotter row they moved.
#pragma once

#include "mkt/fix/session.hpp"
#include "mkt/venue/fix_venue.hpp"

#include <string>
#include <unordered_map>
#include <vector>

namespace mkt::venue {

// One line of the FIX monitor.
struct WireEntry {
    bool        outbound = true;     // desk -> venue
    std::string raw;                 // exact bytes, SOH intact
    std::string msg_type;
    std::uint64_t seq = 0;
};

// One execution as the desk recorded it.
struct DeskFill {
    std::string exec_id;
    double qty = 0.0;
    double price = 0.0;
    std::string exec_type;           // FIX 150
    std::string ord_status;          // FIX 39
};

// One blotter row. Mirrors what an OMS holds per client order.
struct BlotterRow {
    std::string cl_ord_id;
    std::string orig_cl_ord_id;      // set once replaced
    std::string venue_order_id;
    std::string symbol;
    std::string side;                // FIX 54: "1" buy, "2" sell
    std::string ord_type;            // FIX 40
    std::string tif;                 // FIX 59
    double order_qty = 0.0;
    double limit_price = 0.0;
    // --- everything below is derived ONLY from ExecutionReports ---
    std::string status = "pending";  // pending until the venue says otherwise
    double cum_qty = 0.0;
    double leaves_qty = 0.0;
    double avg_px = 0.0;
    std::string reject_text;
    std::vector<DeskFill> fills;
};

class SimulatedDesk {
  public:
    SimulatedDesk(Ticks floor_ticks = 1, std::size_t n_ticks = 50'000,
                  double tick_size = 0.01,
                  std::string sender = "QGLABS", std::string target = "SIMVENUE")
        : venue_(make_cfg(target, sender), floor_ticks, n_ticks, tick_size),
          client_(make_cfg(sender, target)),
          tick_size_(tick_size) {}

    // Brings the session up. Must be called before any order.
    void start(std::int64_t now_ns) {
        venue_.listen(now_ns);
        client_.connect(now_ns);
        pump(now_ns);
    }

    bool connected() const noexcept {
        return client_.state() == fix::SessionState::Active
            && venue_.state() == fix::SessionState::Active;
    }

    // Rests liquidity as another participant, so the desk trades against a
    // market rather than against itself.
    void seed_liquidity(book::Side side, double price, Qty qty) {
        venue_.seed_liquidity(side, price, qty, /*owner=*/99);
    }

    // Sends a NewOrderSingle and pumps until quiet. Returns the ClOrdID so the
    // caller can find the row. `side` is FIX 54 ("1"/"2"), `ord_type` FIX 40
    // ("1" market, "2" limit), `tif` FIX 59 ("0" day, "3" IOC, "4" FOK).
    std::string submit(const std::string& cl_ord_id, const std::string& symbol,
                       const std::string& side, double qty, double price,
                       const std::string& ord_type, const std::string& tif,
                       std::int64_t now_ns) {
        BlotterRow row;
        row.cl_ord_id = cl_ord_id; row.symbol = symbol; row.side = side;
        row.ord_type = ord_type; row.tif = tif;
        row.order_qty = qty; row.limit_price = price;
        row.leaves_qty = qty;
        index_[cl_ord_id] = blotter_.size();
        blotter_.push_back(row);

        client_.send_app(fix::msg::NewOrderSingle, [&](fix::Builder& b) {
            b.add(fix::tag::ClOrdID, cl_ord_id)
             .add(fix::tag::Symbol, symbol)
             .add(fix::tag::Side, side)
             .add_price(fix::tag::OrderQty, qty, 0)
             .add(fix::tag::OrdType, ord_type)
             .add(fix::tag::TimeInForce, tif);
            if (ord_type != "1") b.add_price(fix::tag::Price, price, 2);
        }, now_ns);
        pump(now_ns);
        return cl_ord_id;
    }

    void cancel(const std::string& cl_ord_id, const std::string& orig_cl_ord_id,
                std::int64_t now_ns) {
        const auto it = index_.find(orig_cl_ord_id);
        const std::string sym = it != index_.end() ? blotter_[it->second].symbol : "";
        const std::string sd  = it != index_.end() ? blotter_[it->second].side : "1";
        client_.send_app(fix::msg::OrderCancelRequest, [&](fix::Builder& b) {
            b.add(fix::tag::ClOrdID, cl_ord_id)
             .add(fix::tag::OrigClOrdID, orig_cl_ord_id)
             .add(fix::tag::Symbol, sym)
             .add(fix::tag::Side, sd);
        }, now_ns);
        pump(now_ns);
    }

    // Drives both sides' timers; returns false when the session died.
    bool tick(std::int64_t now_ns) {
        const bool a = client_.on_timer(now_ns);
        const bool b = venue_.on_timer(now_ns);
        pump(now_ns);
        return a && b;
    }

    // ---- reads --------------------------------------------------------------
    const std::vector<BlotterRow>& blotter() const noexcept { return blotter_; }
    const std::vector<WireEntry>&  wire_log() const noexcept { return wire_; }
    const MatchingEngine& engine() const noexcept { return venue_.engine(); }
    double tick_size() const noexcept { return tick_size_; }

    const BlotterRow* row(const std::string& cl_ord_id) const {
        const auto it = index_.find(cl_ord_id);
        return it == index_.end() ? nullptr : &blotter_[it->second];
    }

    // Net position and average cost across every fill the venue reported.
    // Derived from the blotter, not tracked separately, so it cannot drift from
    // the executions it is supposed to summarise.
    void position(double& net_qty, double& avg_cost) const {
        double buy_q = 0, buy_n = 0, sell_q = 0, sell_n = 0;
        for (const auto& r : blotter_)
            for (const auto& f : r.fills) {
                if (r.side == "1") { buy_q += f.qty;  buy_n += f.qty * f.price; }
                else               { sell_q += f.qty; sell_n += f.qty * f.price; }
            }
        net_qty = buy_q - sell_q;
        const double q = buy_q + sell_q;
        avg_cost = q > 0 ? (buy_n + sell_n) / q : 0.0;
    }

    std::size_t wire_count() const noexcept { return wire_.size(); }
    fix::SessionState client_state() const noexcept { return client_.state(); }

  private:
    static fix::SessionConfig make_cfg(std::string me, std::string you) {
        fix::SessionConfig c;
        c.sender_comp_id = std::move(me);
        c.target_comp_id = std::move(you);
        c.heartbeat_secs = 30;
        return c;
    }

    // Moves messages both ways until neither side has anything left to say.
    // Bounded, because a bug that makes two sessions answer each other forever
    // should surface as a stuck order rather than as a hung process.
    void pump(std::int64_t now_ns) {
        for (int guard = 0; guard < 64; ++guard) {
            bool moved = false;

            auto out = client_.outbox();
            client_.clear_outbox();
            for (const auto& w : out) {
                log(true, w);
                venue_.on_wire(w, now_ns);
                moved = true;
            }

            auto in = venue_.outbox();
            venue_.clear_outbox();
            for (const auto& w : in) {
                log(false, w);
                fix::Message m;
                if (client_.on_message(w, now_ns, m) == fix::Inbound::Application)
                    apply(m);
                moved = true;
            }
            if (!moved) break;
        }
    }

    void log(bool outbound, const std::string& raw) {
        fix::Message m;
        WireEntry e;
        e.outbound = outbound;
        e.raw = raw;
        if (m.parse(raw, false) == fix::ParseError::None) {
            e.msg_type = std::string(m.msg_type());
            e.seq = m.seq_num().value_or(0);
        }
        wire_.push_back(std::move(e));
    }

    // The blotter is updated ONLY from what the venue reported.
    void apply(const fix::Message& m) {
        const auto mt = m.msg_type();
        if (mt == fix::msg::OrderCancelReject) {
            const std::string orig(m.get_or(fix::tag::OrigClOrdID));
            if (auto* r = mutable_row(orig)) r->reject_text = std::string(m.get_or(fix::tag::Text));
            return;
        }
        if (mt != fix::msg::ExecutionReport) return;

        const std::string cl(m.get_or(fix::tag::ClOrdID));
        auto* r = mutable_row(cl);
        if (!r) return;

        r->venue_order_id = std::string(m.get_or(fix::tag::OrderID));
        if (m.has(fix::tag::OrigClOrdID)) r->orig_cl_ord_id = std::string(m.get_or(fix::tag::OrigClOrdID));
        r->status     = std::string(status_name(m.get_or(fix::tag::OrdStatus)));
        r->cum_qty    = m.get_double(fix::tag::CumQty).value_or(r->cum_qty);
        r->leaves_qty = m.get_double(fix::tag::LeavesQty).value_or(r->leaves_qty);
        r->avg_px     = m.get_double(fix::tag::AvgPx).value_or(r->avg_px);
        if (m.has(fix::tag::Text)) r->reject_text = std::string(m.get_or(fix::tag::Text));

        // A Trade carries LastQty/LastPx; an ack or a cancel does not.
        if (m.get_or(fix::tag::ExecType) == exec_type::Trade) {
            DeskFill f;
            f.exec_id    = std::string(m.get_or(fix::tag::ExecID));
            f.qty        = m.get_double(fix::tag::LastQty).value_or(0.0);
            f.price      = m.get_double(fix::tag::LastPx).value_or(0.0);
            f.exec_type  = std::string(m.get_or(fix::tag::ExecType));
            f.ord_status = std::string(m.get_or(fix::tag::OrdStatus));
            r->fills.push_back(std::move(f));
        }
    }

    BlotterRow* mutable_row(const std::string& cl) {
        const auto it = index_.find(cl);
        return it == index_.end() ? nullptr : &blotter_[it->second];
    }

    static std::string_view status_name(std::string_view fix39) {
        if (fix39 == ord_status::New)             return "new";
        if (fix39 == ord_status::PartiallyFilled) return "partially_filled";
        if (fix39 == ord_status::Filled)          return "filled";
        if (fix39 == ord_status::Cancelled)       return "cancelled";
        if (fix39 == ord_status::Rejected)        return "rejected";
        return "unknown";
    }

    FixVenue    venue_;
    fix::Session client_;
    double      tick_size_;
    std::vector<BlotterRow> blotter_;
    std::vector<WireEntry>  wire_;
    std::unordered_map<std::string, std::size_t> index_;
};

}  // namespace mkt::venue
