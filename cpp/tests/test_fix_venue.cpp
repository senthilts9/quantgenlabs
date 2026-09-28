// test_fix_venue.cpp — end-to-end FIX lifecycle against the simulated venue.
//
// A real client Session talks to a real FixVenue over in-process "wire" (byte
// strings), so every assertion here is made against messages that passed full FIX
// validation -- BodyLength and CheckSum included. Nothing is inspected through a
// side channel.
//
// The ExecutionReport invariants are the focus, because one message type carries
// every lifecycle transition and the field combinations must be consistent:
//   * ExecType (150) = what just happened;  OrdStatus (39) = what the order now is.
//   * LeavesQty + CumQty == OrderQty, always.
//   * AvgPx is the running average over all fills, not the last fill's price.
#include "microtest.hpp"
#include "mkt/venue/fix_venue.hpp"

#include <string>
#include <vector>

using namespace mkt;
using mkt::book::Side;

struct Wire {
    fix::Session client;
    venue::FixVenue venue;
    std::vector<fix::Message> client_inbox;
    std::vector<std::string>  client_raw;   // owns the bytes the views point into
    std::int64_t now = 1'000'000'000LL;

    Wire()
        : client(make_cfg("CLIENT", "SIMVENUE")),
          // Ladder spans 0.01-500.00 on a 0.01 tick, so every price these tests use is
          // representable. A ladder that does not cover the test prices makes the
          // venue correctly reject them as out-of-range, which looks like a venue bug.
          venue(make_cfg("SIMVENUE", "CLIENT"), 1, 50000, 0.01) {
        venue.listen(now);
        client.connect(now);
        pump();          // client Logon -> venue
        pump_back();     // venue Logon echo -> client
    }

    static fix::SessionConfig make_cfg(const char* me, const char* you) {
        fix::SessionConfig c;
        c.sender_comp_id = me; c.target_comp_id = you; c.heartbeat_secs = 30;
        return c;
    }

    void pump() {                        // client -> venue
        auto batch = client.outbox();
        client.clear_outbox();
        for (const auto& w : batch) venue.on_wire(w, now);
    }

    void pump_back() {                   // venue -> client
        auto batch = venue.outbox();
        venue.clear_outbox();
        for (const auto& w : batch) {
            client_raw.push_back(w);
            fix::Message m;
            // Full validation: if the venue emitted a malformed message this fails.
            if (m.parse(client_raw.back(), /*validate=*/true) != fix::ParseError::None) continue;
            if (client.on_message(client_raw.back(), now, m) == fix::Inbound::Application)
                client_inbox.push_back(m);
        }
    }

    void send_order(const char* cl_ord_id, const char* side, double qty, double px,
                    const char* ord_type = "2", const char* tif = "0") {
        client.send_app(fix::msg::NewOrderSingle, [&](fix::Builder& b) {
            b.add(fix::tag::ClOrdID, cl_ord_id)
             .add(fix::tag::Symbol, "AAPL")
             .add(fix::tag::Side, side)
             .add_price(fix::tag::OrderQty, qty, 0)
             .add(fix::tag::OrdType, ord_type)
             .add(fix::tag::TimeInForce, tif);
            if (std::string(ord_type) != "1") b.add_price(fix::tag::Price, px, 2);
        }, now);
        pump(); pump_back();
    }

    void cancel(const char* new_id, const char* orig_id) {
        client.send_app(fix::msg::OrderCancelRequest, [&](fix::Builder& b) {
            b.add(fix::tag::ClOrdID, new_id)
             .add(fix::tag::OrigClOrdID, orig_id)
             .add(fix::tag::Symbol, "AAPL")
             .add(fix::tag::Side, "1");
        }, now);
        pump(); pump_back();
    }

    void replace(const char* new_id, const char* orig_id, double qty, double px) {
        client.send_app(fix::msg::OrderCancelReplace, [&](fix::Builder& b) {
            b.add(fix::tag::ClOrdID, new_id)
             .add(fix::tag::OrigClOrdID, orig_id)
             .add(fix::tag::Symbol, "AAPL")
             .add(fix::tag::Side, "1")
             .add_price(fix::tag::OrderQty, qty, 0)
             .add_price(fix::tag::Price, px, 2);
        }, now);
        pump(); pump_back();
    }

    // Reports for one ClOrdID, in arrival order.
    std::vector<const fix::Message*> reports_for(const char* cl) {
        std::vector<const fix::Message*> out;
        for (const auto& m : client_inbox)
            if (m.get_or(fix::tag::ClOrdID) == cl) out.push_back(&m);
        return out;
    }
};

// Asserts the invariant every client reconciles against.
static void check_qty_invariant(const fix::Message& m) {
    const auto ordq = m.get_double(fix::tag::OrderQty);
    const auto lv   = m.get_double(fix::tag::LeavesQty);
    const auto cum  = m.get_double(fix::tag::CumQty);
    REQUIRE(ordq.has_value()); REQUIRE(lv.has_value()); REQUIRE(cum.has_value());
    const auto status = m.get_or(fix::tag::OrdStatus);
    if (status == venue::ord_status::Cancelled || status == venue::ord_status::Rejected) {
        CHECK_CLOSE(*lv, 0.0, 1e-9);                       // terminal: nothing left
    } else {
        CHECK_CLOSE(*lv + *cum, *ordq, 1e-9);
    }
}

TEST(venue_session_comes_up) {
    Wire w;
    REQUIRE(w.client.state() == fix::SessionState::Active);
    REQUIRE(w.venue.state() == fix::SessionState::Active);
}

TEST(venue_acks_a_resting_order_before_anything_else) {
    Wire w;
    w.send_order("C1", "1", 100, 187.25);
    const auto rs = w.reports_for("C1");
    REQUIRE_R(rs.size() == 1);
    REQUIRE(rs[0]->get_or(fix::tag::ExecType)  == venue::exec_type::New);
    REQUIRE(rs[0]->get_or(fix::tag::OrdStatus) == venue::ord_status::New);
    CHECK_CLOSE(rs[0]->get_double(fix::tag::LeavesQty).value(), 100.0, 1e-9);
    CHECK_CLOSE(rs[0]->get_double(fix::tag::CumQty).value(), 0.0, 1e-9);
    check_qty_invariant(*rs[0]);
    REQUIRE(!rs[0]->get_or(fix::tag::OrderID).empty());
    // The book really has it.
    book::Ticks px; book::Qty q;
    REQUIRE(w.venue.engine().best(Side::Bid, px, q));
    REQUIRE(px == 18725);
    REQUIRE(q == 100);
}

// ExecType=F with OrdStatus=2 on a complete fill: the two fields mean different
// things and both must be right.
TEST(venue_full_fill_reports_trade_and_filled) {
    Wire w;
    w.send_order("SELL1", "2", 100, 187.25);        // resting ask
    w.send_order("BUY1",  "1", 100, 187.25);        // crosses it

    const auto rs = w.reports_for("BUY1");
    REQUIRE_R(rs.size() == 2);                      // ack, then trade
    REQUIRE(rs[0]->get_or(fix::tag::ExecType) == venue::exec_type::New);
    REQUIRE(rs[1]->get_or(fix::tag::ExecType)  == venue::exec_type::Trade);
    REQUIRE(rs[1]->get_or(fix::tag::OrdStatus) == venue::ord_status::Filled);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::LastQty).value(), 100.0, 1e-9);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::LastPx).value(), 187.25, 1e-9);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::CumQty).value(), 100.0, 1e-9);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::LeavesQty).value(), 0.0, 1e-9);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::AvgPx).value(), 187.25, 1e-9);
    check_qty_invariant(*rs[1]);
}

// The distinction that is most often wrong: a partial fill is ExecType=F with
// OrdStatus=1, and only the LAST fill flips OrdStatus to 2.
TEST(venue_partial_fills_report_status_1_then_2) {
    Wire w;
    w.send_order("A1", "2", 40, 100.00);            // two resting asks at
    w.send_order("A2", "2", 60, 100.00);            // the same price
    w.send_order("B1", "1", 100, 100.00);           // sweeps both

    const auto rs = w.reports_for("B1");
    REQUIRE_R(rs.size() == 3);                      // ack + 2 trades
    REQUIRE(rs[1]->get_or(fix::tag::ExecType)  == venue::exec_type::Trade);
    REQUIRE(rs[1]->get_or(fix::tag::OrdStatus) == venue::ord_status::PartiallyFilled);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::CumQty).value(), 40.0, 1e-9);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::LeavesQty).value(), 60.0, 1e-9);

    REQUIRE(rs[2]->get_or(fix::tag::ExecType)  == venue::exec_type::Trade);
    REQUIRE(rs[2]->get_or(fix::tag::OrdStatus) == venue::ord_status::Filled);
    CHECK_CLOSE(rs[2]->get_double(fix::tag::CumQty).value(), 100.0, 1e-9);
    CHECK_CLOSE(rs[2]->get_double(fix::tag::LeavesQty).value(), 0.0, 1e-9);
    for (const auto* m : rs) check_qty_invariant(*m);
}

// AvgPx must be the running average, not the last print.
TEST(venue_avgpx_is_cumulative_not_last_price) {
    Wire w;
    w.send_order("A1", "2", 50, 100.00);
    w.send_order("A2", "2", 50, 101.00);
    w.send_order("B1", "1", 100, 101.00);           // fills 50@100 then 50@101

    const auto rs = w.reports_for("B1");
    REQUIRE_R(rs.size() == 3);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::AvgPx).value(), 100.00, 1e-9);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::LastPx).value(), 100.00, 1e-9);
    // After the second fill: avg = (50*100 + 50*101)/100 = 100.50, last = 101.00
    CHECK_CLOSE(rs[2]->get_double(fix::tag::AvgPx).value(), 100.50, 1e-9);
    CHECK_CLOSE(rs[2]->get_double(fix::tag::LastPx).value(), 101.00, 1e-9);
}

TEST(venue_partial_then_rests_remainder_on_book) {
    Wire w;
    w.send_order("A1", "2", 60, 100.00);
    w.send_order("B1", "1", 100, 100.00);           // 60 fills, 40 rests
    const auto rs = w.reports_for("B1");
    REQUIRE_R(rs.size() == 2);
    REQUIRE(rs[1]->get_or(fix::tag::OrdStatus) == venue::ord_status::PartiallyFilled);
    CHECK_CLOSE(rs[1]->get_double(fix::tag::LeavesQty).value(), 40.0, 1e-9);
    book::Ticks px; book::Qty q;
    REQUIRE(w.venue.engine().best(Side::Bid, px, q));
    REQUIRE(q == 40);                               // remainder is resting
}

// An IOC remainder must be reported cancelled, or the client waits forever.
TEST(venue_ioc_remainder_is_reported_cancelled) {
    Wire w;
    w.send_order("A1", "2", 60, 100.00);
    w.send_order("B1", "1", 100, 100.00, "2", "3"); // TIF=3 IOC
    const auto rs = w.reports_for("B1");
    REQUIRE_R(rs.size() == 3);                      // ack + trade + cancel
    REQUIRE(rs[2]->get_or(fix::tag::ExecType)  == venue::exec_type::Cancelled);
    REQUIRE(rs[2]->get_or(fix::tag::OrdStatus) == venue::ord_status::Cancelled);
    CHECK_CLOSE(rs[2]->get_double(fix::tag::LeavesQty).value(), 0.0, 1e-9);
    book::Ticks px; book::Qty q;
    REQUIRE(!w.venue.engine().best(Side::Bid, px, q));   // nothing left resting
}

TEST(venue_fok_that_cannot_fill_is_rejected_with_no_trade) {
    Wire w;
    w.send_order("A1", "2", 60, 100.00);
    w.send_order("B1", "1", 100, 100.00, "2", "4"); // TIF=4 FOK
    const auto rs = w.reports_for("B1");
    REQUIRE_R(rs.size() == 1);
    REQUIRE(rs[0]->get_or(fix::tag::ExecType)  == venue::exec_type::Rejected);
    REQUIRE(rs[0]->get_or(fix::tag::OrdStatus) == venue::ord_status::Rejected);
    REQUIRE(rs[0]->get_or(fix::tag::Text).find("fok") != std::string_view::npos);
    REQUIRE(w.venue.engine().leaves(1) == 60);      // resting order untouched
}

TEST(venue_market_order_sweeps_and_reports) {
    Wire w;
    w.send_order("A1", "2", 50, 100.00);
    w.send_order("A2", "2", 50, 100.50);
    w.send_order("B1", "1", 80, 0.0, "1");          // OrdType=1 Market
    const auto rs = w.reports_for("B1");
    REQUIRE_R(rs.size() >= 3);
    CHECK_CLOSE(rs.back()->get_double(fix::tag::CumQty).value(), 80.0, 1e-9);
    // True average is 100.1875, but AvgPx goes on the wire at 2dp, so the value a
    // client can read back is 100.19. The tolerance is half a cent because that is
    // the actual precision of the field, not because the arithmetic is approximate.
    CHECK_CLOSE(rs.back()->get_double(fix::tag::AvgPx).value(),
                (50.0 * 100.00 + 30.0 * 100.50) / 80.0, 0.005);
}

TEST(venue_rejects_malformed_orders) {
    Wire w;
    w.send_order("BAD1", "1", 0, 100.00);           // zero qty
    const auto r1 = w.reports_for("BAD1");
    REQUIRE_R(r1.size() == 1);
    REQUIRE(r1[0]->get_or(fix::tag::ExecType) == venue::exec_type::Rejected);

    // Limit order with no Price: send it by hand so the helper cannot add one.
    w.client.send_app(fix::msg::NewOrderSingle, [](fix::Builder& b) {
        b.add(fix::tag::ClOrdID, "BAD2").add(fix::tag::Symbol, "AAPL")
         .add(fix::tag::Side, "1").add_price(fix::tag::OrderQty, 10, 0)
         .add(fix::tag::OrdType, "2");
    }, w.now);
    w.pump(); w.pump_back();
    const auto r2 = w.reports_for("BAD2");
    REQUIRE_R(r2.size() == 1);
    REQUIRE(r2[0]->get_or(fix::tag::ExecType) == venue::exec_type::Rejected);
    REQUIRE(r2[0]->get_or(fix::tag::Text).find("Price") != std::string_view::npos);
}

TEST(venue_cancel_removes_order_and_confirms) {
    Wire w;
    w.send_order("C1", "1", 100, 99.00);
    w.cancel("C1-CXL", "C1");
    const auto rs = w.reports_for("C1");
    REQUIRE_R(rs.size() == 2);                      // ack, then cancel confirm
    REQUIRE(rs[1]->get_or(fix::tag::ExecType)  == venue::exec_type::Cancelled);
    REQUIRE(rs[1]->get_or(fix::tag::OrdStatus) == venue::ord_status::Cancelled);
    REQUIRE(rs[1]->get_or(fix::tag::OrigClOrdID) == "C1-CXL");
    book::Ticks px; book::Qty q;
    REQUIRE(!w.venue.engine().best(Side::Bid, px, q));
}

// An unhonourable cancel gets 35=9 OrderCancelReject, not an ExecutionReport --
// the wrong message leaves the client waiting for a transition that never comes.
TEST(venue_unknown_cancel_gets_cancel_reject_not_exec_report) {
    Wire w;
    w.cancel("X-CXL", "NEVER-EXISTED");
    bool found = false;
    for (const auto& m : w.client_inbox)
        if (m.msg_type() == fix::msg::OrderCancelReject) {
            found = true;
            REQUIRE(m.get_or(fix::tag::OrigClOrdID) == "NEVER-EXISTED");
            REQUIRE(m.get_or(fix::tag::CxlRejResponseTo) == "1");
        }
    REQUIRE(found);
}

TEST(venue_replace_repriced_into_market_fills) {
    Wire w;
    w.send_order("S1", "2", 100, 100.00);           // resting ask
    w.send_order("B1", "1", 100,  99.00);           // rests below, no cross
    REQUIRE_R(w.reports_for("B1").size() == 1);
    w.replace("B2", "B1", 100, 100.00);             // reprice up into the ask
    const auto rs = w.reports_for("B2");
    REQUIRE_R(rs.size() >= 2);
    REQUIRE(rs[0]->get_or(fix::tag::ExecType) == venue::exec_type::Replaced);
    REQUIRE(rs.back()->get_or(fix::tag::OrdStatus) == venue::ord_status::Filled);
    CHECK_CLOSE(rs.back()->get_double(fix::tag::AvgPx).value(), 100.00, 1e-9);
}

// Price conversion must round, not truncate: 1.0999999999999999 belongs on tick
// 110, and truncation is a classic off-by-one-tick bug.
TEST(venue_price_to_tick_conversion_rounds) {
    Wire w;
    REQUIRE(w.venue.to_ticks(187.25) == 18725);
    REQUIRE(w.venue.to_ticks(1.10) == 110);
    REQUIRE(w.venue.to_ticks(1.0999999999999999) == 110);
    REQUIRE(w.venue.to_ticks(100.004) == 10000);
    REQUIRE(w.venue.to_ticks(100.006) == 10001);
    CHECK_CLOSE(w.venue.to_price(18725), 187.25, 1e-9);
}

// Every message the venue emits must pass full FIX validation, including the
// sequence numbering, across a busy session.
TEST(venue_all_emitted_messages_are_valid_and_in_sequence) {
    Wire w;
    w.send_order("A1", "2", 50, 100.00);
    w.send_order("A2", "2", 50, 100.50);
    w.send_order("B1", "1", 80, 100.50);
    w.cancel("A2-CXL", "A2");
    w.send_order("BAD", "1", 0, 1.0);
    REQUIRE(w.client_raw.size() > 5);

    // client_raw[0] is the venue's own Logon, which is seq 1 -- so the expected
    // run starts at 1, not 2.
    std::uint64_t expect = 1;
    for (const auto& raw : w.client_raw) {
        fix::Message m;
        REQUIRE(m.parse(raw, /*validate=*/true) == fix::ParseError::None);
        REQUIRE(m.get_or(fix::tag::SenderCompID) == "SIMVENUE");
        REQUIRE(m.get_or(fix::tag::TargetCompID) == "CLIENT");
        REQUIRE(m.seq_num().value() == expect++);   // strictly sequential, no gaps
    }
    REQUIRE(w.client.expected_in_seq() == expect);
}

// seed_liquidity() rests orders under a different participant, which is what makes
// self-match prevention testable: with it on, the client cannot cross itself but
// CAN trade against seeded market liquidity.
TEST(venue_seeded_liquidity_trades_under_self_match_prevention) {
    fix::Session client(Wire::make_cfg("CLIENT", "SIMVENUE"));
    venue::FixVenue v(Wire::make_cfg("SIMVENUE", "CLIENT"), 1, 50000, 0.01,
                      /*prevent_self_match=*/true);
    std::int64_t now = 1'000'000'000LL;
    v.listen(now); client.connect(now);
    { auto b = client.outbox(); client.clear_outbox(); for (const auto& w : b) v.on_wire(w, now); }
    { auto b = v.outbox(); v.clear_outbox();
      for (const auto& w : b) { fix::Message m; client.on_message(w, now, m); } }
    REQUIRE_R(client.state() == fix::SessionState::Active);

    // Market liquidity from another participant.
    const auto seeded = v.seed_liquidity(Side::Ask, 100.00, 500);
    REQUIRE(seeded.state == venue::OrderState::New);
    book::Ticks px; book::Qty q;
    REQUIRE_R(v.engine().best(Side::Ask, px, q));
    REQUIRE(px == 10000); REQUIRE(q == 500);

    std::vector<std::string> raw;
    std::vector<fix::Message> inbox;
    auto trade = [&](const char* id, const char* side, double qty, double price) {
        client.send_app(fix::msg::NewOrderSingle, [&](fix::Builder& b) {
            b.add(fix::tag::ClOrdID, id).add(fix::tag::Symbol, "AAPL").add(fix::tag::Side, side)
             .add_price(fix::tag::OrderQty, qty, 0).add(fix::tag::OrdType, "2")
             .add_price(fix::tag::Price, price, 2);
        }, now);
        { auto b = client.outbox(); client.clear_outbox(); for (const auto& w : b) v.on_wire(w, now); }
        { auto b = v.outbox(); v.clear_outbox();
          for (const auto& w : b) { raw.push_back(w); fix::Message m;
            if (m.parse(raw.back(), true) != fix::ParseError::None) continue;
            if (client.on_message(raw.back(), now, m) == fix::Inbound::Application) inbox.push_back(m); } }
    };

    // Against seeded liquidity: fills.
    trade("T1", "1", 100, 100.00);
    bool filled = false;
    for (const auto& m : inbox)
        if (m.get_or(fix::tag::ClOrdID) == "T1" && m.get_or(fix::tag::ExecType) == venue::exec_type::Trade)
            filled = true;
    REQUIRE(filled);

    // Against its own resting order: blocked, no trade.
    inbox.clear();
    trade("T2", "2", 50, 99.00);      // client rests an ask below its own bid? no -- rest first
    inbox.clear();
    trade("T3", "1", 50, 99.00);      // client bids at its own ask price
    for (const auto& m : inbox)
        if (m.get_or(fix::tag::ClOrdID) == "T3")
            REQUIRE(m.get_or(fix::tag::ExecType) != venue::exec_type::Trade);
    REQUIRE(v.engine().stats().self_match_blocks > 0);
}

int main() {
    std::printf("=== FIX simulated-venue lifecycle tests ===\n");
    return microtest::run_all();
}
