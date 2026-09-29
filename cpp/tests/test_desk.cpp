// test_desk.cpp — client desk blotter tests.
//
// The property under test throughout: the blotter reflects what the VENUE
// reported, not what the desk hoped. Every assertion reads a row that was
// populated from ExecutionReports arriving over the wire.
#include "microtest.hpp"
#include "mkt/venue/desk.hpp"

using namespace mkt::venue;
using mkt::book::Side;

static constexpr std::int64_t SEC = 1'000'000'000LL;

static SimulatedDesk make_desk() {
    SimulatedDesk d(1, 50'000, 0.01);
    d.start(1000 * SEC);
    return d;
}

TEST(desk_session_comes_up) {
    auto d = make_desk();
    REQUIRE(d.connected());
    REQUIRE(d.wire_count() >= 2);          // logon out, logon echo back
}

TEST(desk_resting_order_shows_new_with_full_leaves) {
    auto d = make_desk();
    d.submit("O1", "AAPL", "1", 500, 99.00, "2", "0", 1001 * SEC);
    const auto* r = d.row("O1");
    REQUIRE_R(r != nullptr);
    REQUIRE(r->status == "new");
    CHECK_CLOSE(r->cum_qty, 0.0, 1e-9);
    CHECK_CLOSE(r->leaves_qty, 500.0, 1e-9);
    REQUIRE(r->fills.empty());
    REQUIRE(!r->venue_order_id.empty());   // venue assigned an OrderID
}

TEST(desk_full_fill_updates_blotter_from_reports) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 500);
    d.submit("O1", "AAPL", "1", 300, 100.00, "2", "0", 1001 * SEC);
    const auto* r = d.row("O1");
    REQUIRE_R(r != nullptr);
    REQUIRE(r->status == "filled");
    CHECK_CLOSE(r->cum_qty, 300.0, 1e-9);
    CHECK_CLOSE(r->leaves_qty, 0.0, 1e-9);
    CHECK_CLOSE(r->avg_px, 100.00, 1e-9);
    REQUIRE_R(r->fills.size() == 1);
    CHECK_CLOSE(r->fills[0].qty, 300.0, 1e-9);
    REQUIRE(r->fills[0].ord_status == "2");        // Filled
}

// Sweeping two levels must produce two fills and a cumulative average, with the
// intermediate report showing partially-filled.
TEST(desk_partial_fills_accumulate) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 100);
    d.seed_liquidity(Side::Ask, 101.00, 100);
    d.submit("O1", "AAPL", "1", 200, 101.00, "2", "0", 1001 * SEC);
    const auto* r = d.row("O1");
    REQUIRE_R(r != nullptr);
    REQUIRE(r->status == "filled");
    REQUIRE_R(r->fills.size() == 2);
    CHECK_CLOSE(r->fills[0].price, 100.00, 1e-9);
    CHECK_CLOSE(r->fills[1].price, 101.00, 1e-9);
    REQUIRE(r->fills[0].ord_status == "1");        // partially filled
    REQUIRE(r->fills[1].ord_status == "2");        // then filled
    CHECK_CLOSE(r->cum_qty, 200.0, 1e-9);
    CHECK_CLOSE(r->avg_px, 100.50, 0.005);         // 2dp on the wire
}

TEST(desk_partial_then_rests_shows_remaining_leaves) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 120);
    d.submit("O1", "AAPL", "1", 300, 100.00, "2", "0", 1001 * SEC);
    const auto* r = d.row("O1");
    REQUIRE_R(r != nullptr);
    REQUIRE(r->status == "partially_filled");
    CHECK_CLOSE(r->cum_qty, 120.0, 1e-9);
    CHECK_CLOSE(r->leaves_qty, 180.0, 1e-9);
}

TEST(desk_ioc_remainder_reported_cancelled) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 120);
    d.submit("O1", "AAPL", "1", 300, 100.00, "2", "3", 1001 * SEC);   // TIF=IOC
    const auto* r = d.row("O1");
    REQUIRE_R(r != nullptr);
    REQUIRE(r->status == "cancelled");             // not left hanging
    CHECK_CLOSE(r->cum_qty, 120.0, 1e-9);
    CHECK_CLOSE(r->leaves_qty, 0.0, 1e-9);
}

TEST(desk_fok_that_cannot_fill_is_rejected_with_no_fills) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 50);
    d.submit("O1", "AAPL", "1", 300, 100.00, "2", "4", 1001 * SEC);   // TIF=FOK
    const auto* r = d.row("O1");
    REQUIRE_R(r != nullptr);
    REQUIRE(r->status == "rejected");
    REQUIRE(r->fills.empty());
    REQUIRE(r->reject_text.find("fok") != std::string::npos);
}

TEST(desk_cancel_marks_row_cancelled) {
    auto d = make_desk();
    d.submit("O1", "AAPL", "1", 500, 99.00, "2", "0", 1001 * SEC);
    REQUIRE_R(d.row("O1") != nullptr);
    REQUIRE(d.row("O1")->status == "new");
    d.cancel("O1-C", "O1", 1002 * SEC);
    REQUIRE(d.row("O1")->status == "cancelled");
    CHECK_CLOSE(d.row("O1")->leaves_qty, 0.0, 1e-9);
}

TEST(desk_cancel_of_unknown_order_records_reject_text) {
    auto d = make_desk();
    d.submit("O1", "AAPL", "1", 10, 99.00, "2", "0", 1001 * SEC);
    d.cancel("X-C", "NOPE", 1002 * SEC);
    REQUIRE(d.row("O1")->status == "new");         // untouched
    REQUIRE(d.row("NOPE") == nullptr);
}

// Position is derived from the fills, so it cannot drift from the executions.
TEST(desk_position_is_derived_from_fills) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 1000);
    d.seed_liquidity(Side::Bid,  99.00, 1000);
    d.submit("B1", "AAPL", "1", 300, 100.00, "2", "0", 1001 * SEC);
    d.submit("S1", "AAPL", "2", 100,  99.00, "2", "0", 1002 * SEC);

    double net = 0, avg = 0;
    d.position(net, avg);
    CHECK_CLOSE(net, 200.0, 1e-9);                 // 300 bought, 100 sold
    CHECK_CLOSE(avg, (300.0 * 100.0 + 100.0 * 99.0) / 400.0, 0.01);
}

TEST(desk_wire_log_records_both_directions) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 500);
    d.submit("O1", "AAPL", "1", 100, 100.00, "2", "0", 1001 * SEC);

    bool saw_order = false, saw_exec = false;
    for (const auto& w : d.wire_log()) {
        if (w.outbound && w.msg_type == "D") saw_order = true;
        if (!w.outbound && w.msg_type == "8") saw_exec = true;
    }
    REQUIRE(saw_order);
    REQUIRE(saw_exec);
    // Every logged message must be a real, fully valid FIX message.
    for (const auto& w : d.wire_log()) {
        mkt::fix::Message m;
        REQUIRE(m.parse(w.raw, /*validate=*/true) == mkt::fix::ParseError::None);
    }
}

TEST(desk_market_order_sweeps_without_a_limit_price) {
    auto d = make_desk();
    d.seed_liquidity(Side::Ask, 100.00, 50);
    d.seed_liquidity(Side::Ask, 100.50, 50);
    d.submit("M1", "AAPL", "1", 80, 0.0, "1", "3", 1001 * SEC);   // market, IOC
    const auto* r = d.row("M1");
    REQUIRE_R(r != nullptr);
    CHECK_CLOSE(r->cum_qty, 80.0, 1e-9);
    REQUIRE(r->fills.size() == 2);
}

TEST(desk_book_reflects_resting_orders) {
    auto d = make_desk();
    d.seed_liquidity(Side::Bid, 99.50, 400);
    d.submit("O1", "AAPL", "1", 250, 99.00, "2", "0", 1001 * SEC);
    mkt::book::Ticks px; mkt::book::Qty q;
    REQUIRE_R(d.engine().best(Side::Bid, px, q));
    REQUIRE(px == 9950);                           // seeded liquidity is better
    REQUIRE(q == 400);
    REQUIRE(d.engine().book().qty_at(Side::Bid, 9900) == 250);  // our order rests
}

TEST(desk_heartbeat_keeps_session_alive) {
    auto d = make_desk();
    REQUIRE(d.tick(1031 * SEC));                   // 31s quiet -> heartbeats
    REQUIRE(d.connected());
    bool hb = false;
    for (const auto& w : d.wire_log()) if (w.msg_type == "0") hb = true;
    REQUIRE(hb);
}

int main() {
    std::printf("=== simulated desk blotter tests ===\n");
    return microtest::run_all();
}
