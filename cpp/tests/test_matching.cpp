// test_matching.cpp — matching engine tests.
//
// Price-time priority is the property a matching engine is most easily wrong
// about, because an implementation that iterates a container with unspecified
// order still passes every "does it fill?" test while silently violating time
// priority. So the FIFO ordering is asserted directly, by identifying WHICH order
// filled, not just how much.
#include "microtest.hpp"
#include "mkt/venue/matching_engine.hpp"

#include <vector>

using namespace mkt::venue;
using mkt::book::Side;

struct Harness {
    MatchingEngine eng;
    std::vector<Fill> fills;
    explicit Harness(bool prevent_self = true) : eng(9000, 2001, prevent_self) {
        eng.on_fill([this](const Fill& f) { fills.push_back(f); });
    }
    OrderResult limit(OrderId id, OwnerId own, Side s, Ticks px, Qty q,
                      TimeInForce tif = TimeInForce::Day) {
        NewOrder o; o.id = id; o.owner = own; o.side = s; o.price = px; o.qty = q;
        o.type = OrdType::Limit; o.tif = tif;
        return eng.submit(o);
    }
    OrderResult market(OrderId id, OwnerId own, Side s, Qty q) {
        NewOrder o; o.id = id; o.owner = own; o.side = s; o.qty = q;
        o.type = OrdType::Market; o.tif = TimeInForce::IOC;
        return eng.submit(o);
    }
};

// ------------------------------------------------------------------ resting
TEST(match_resting_order_builds_book) {
    Harness h;
    const auto r = h.limit(1, 1, Side::Bid, 9990, 100);
    REQUIRE(r.state == OrderState::New);
    REQUIRE(r.filled == 0);
    REQUIRE(r.leaves == 100);
    Ticks px; Qty q;
    REQUIRE(h.eng.best(Side::Bid, px, q));
    REQUIRE(px == 9990); REQUIRE(q == 100);
    REQUIRE(h.eng.is_resting(1));
    REQUIRE(h.fills.empty());
}

TEST(match_non_crossing_orders_both_rest) {
    Harness h;
    h.limit(1, 1, Side::Bid, 9990, 100);
    h.limit(2, 2, Side::Ask, 10010, 100);
    REQUIRE(h.fills.empty());
    REQUIRE(h.eng.book().spread_ticks() == 20);
    REQUIRE(!h.eng.book().is_crossed());
}

// ------------------------------------------------------------------ price priority
TEST(match_better_price_fills_first) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10010, 100);   // worse for a buyer
    h.limit(2, 2, Side::Ask, 10000, 100);   // best ask
    const auto r = h.limit(3, 3, Side::Bid, 10010, 100);
    REQUIRE(r.state == OrderState::Filled);
    REQUIRE(h.fills.size() == 1);
    REQUIRE(h.fills[0].resting == 2);       // the BETTER price, not the earlier one
    REQUIRE(h.fills[0].price == 10000);
}

// Time priority: at the same price, the earlier order must fill first. This is
// the assertion a wrong implementation fails while still "working".
TEST(match_time_priority_at_equal_price) {
    Harness h;
    h.limit(10, 1, Side::Ask, 10000, 100);  // first in queue
    h.limit(11, 2, Side::Ask, 10000, 100);  // second
    h.limit(12, 3, Side::Ask, 10000, 100);  // third
    REQUIRE(h.eng.queue_position(10) == 0);
    REQUIRE(h.eng.queue_position(11) == 1);
    REQUIRE(h.eng.queue_position(12) == 2);

    h.limit(20, 4, Side::Bid, 10000, 250);  // takes 2.5 of the 3
    REQUIRE(h.fills.size() == 3);
    REQUIRE(h.fills[0].resting == 10);      // strict arrival order
    REQUIRE(h.fills[1].resting == 11);
    REQUIRE(h.fills[2].resting == 12);
    REQUIRE(h.fills[0].qty == 100);
    REQUIRE(h.fills[1].qty == 100);
    REQUIRE(h.fills[2].qty == 50);          // partial on the last
    REQUIRE(h.eng.leaves(12) == 50);
    REQUIRE(h.eng.queue_position(12) == 0); // now at the front
}

// The passive side sets the price: an aggressive buyer willing to pay more than
// the resting ask trades at the ASK, and keeps the difference.
TEST(match_passive_side_sets_price) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 100);
    const auto r = h.limit(2, 2, Side::Bid, 10050, 100);   // willing to pay 10050
    REQUIRE(r.state == OrderState::Filled);
    REQUIRE(h.fills[0].price == 10000);                    // pays only 10000
    CHECK_CLOSE(r.avg_price_ticks, 10000.0, 1e-9);
}

TEST(match_walks_multiple_levels_and_reports_vwap) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 100);
    h.limit(2, 1, Side::Ask, 10010, 100);
    h.limit(3, 1, Side::Ask, 10020, 100);
    const auto r = h.limit(9, 2, Side::Bid, 10020, 250);
    REQUIRE(r.state == OrderState::Filled);
    REQUIRE(r.filled == 250);
    REQUIRE(h.fills.size() == 3);
    CHECK_CLOSE(r.avg_price_ticks, (100.0*10000 + 100.0*10010 + 50.0*10020)/250.0, 1e-9);
    REQUIRE(h.eng.leaves(3) == 50);
}

TEST(match_partial_then_rests_remainder) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 60);
    const auto r = h.limit(2, 2, Side::Bid, 10000, 100);
    REQUIRE(r.state == OrderState::PartiallyFilled);
    REQUIRE(r.filled == 60);
    REQUIRE(r.leaves == 40);
    Ticks px; Qty q;
    REQUIRE(h.eng.best(Side::Bid, px, q));      // remainder joined the bid
    REQUIRE(px == 10000); REQUIRE(q == 40);
    REQUIRE(h.eng.is_resting(2));
}

TEST(match_stops_at_limit_price) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 100);
    h.limit(2, 1, Side::Ask, 10050, 100);
    const auto r = h.limit(3, 2, Side::Bid, 10000, 200);   // only willing up to 10000
    REQUIRE(r.filled == 100);
    REQUIRE(r.leaves == 100);
    REQUIRE(h.fills.size() == 1);
    REQUIRE(h.eng.leaves(2) == 100);                       // untouched
}

// ------------------------------------------------------------------ TIF
TEST(match_ioc_cancels_remainder_instead_of_resting) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 60);
    const auto r = h.limit(2, 2, Side::Bid, 10000, 100, TimeInForce::IOC);
    REQUIRE(r.filled == 60);
    REQUIRE(r.leaves == 40);
    REQUIRE(r.state == OrderState::PartiallyFilled);
    REQUIRE(!h.eng.is_resting(2));              // did NOT join the book
    Ticks px; Qty q;
    REQUIRE(!h.eng.best(Side::Bid, px, q));
}

TEST(match_ioc_with_no_liquidity_is_cancelled) {
    Harness h;
    const auto r = h.limit(1, 1, Side::Bid, 10000, 100, TimeInForce::IOC);
    REQUIRE(r.state == OrderState::Cancelled);
    REQUIRE(r.filled == 0);
    REQUIRE(!h.eng.is_resting(1));
}

// FOK must be decided before anything prints: a partial fill followed by a cancel
// would have already published trades that should never have existed.
TEST(match_fok_unfillable_prints_nothing) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 60);        // only 60 available
    const auto r = h.limit(2, 2, Side::Bid, 10000, 100, TimeInForce::FOK);
    REQUIRE(r.state == OrderState::Rejected);
    REQUIRE(r.reject == RejectReason::FokUnfillable);
    REQUIRE(h.fills.empty());                   // nothing printed
    REQUIRE(h.eng.leaves(1) == 60);             // resting order untouched
}

TEST(match_fok_fillable_fills_completely) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 60);
    h.limit(2, 1, Side::Ask, 10010, 60);
    const auto r = h.limit(3, 2, Side::Bid, 10010, 100, TimeInForce::FOK);
    REQUIRE(r.state == OrderState::Filled);
    REQUIRE(r.filled == 100);
    REQUIRE(h.fills.size() == 2);
}

TEST(match_market_order_sweeps_book) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 50);
    h.limit(2, 1, Side::Ask, 10010, 50);
    const auto r = h.market(3, 2, Side::Bid, 80);
    REQUIRE(r.filled == 80);
    REQUIRE(h.fills.size() == 2);
    CHECK_CLOSE(r.avg_price_ticks, (50.0*10000 + 30.0*10010)/80.0, 1e-9);
}

TEST(match_market_order_with_empty_side_rejected) {
    Harness h;
    const auto r = h.market(1, 1, Side::Bid, 100);
    REQUIRE(r.state == OrderState::Rejected);
    REQUIRE(r.reject == RejectReason::NoLiquidity);
}

// ------------------------------------------------------------------ validation
TEST(match_rejects_malformed_orders) {
    Harness h;
    REQUIRE(h.limit(1, 1, Side::Bid, 10000, 0).reject == RejectReason::BadQuantity);
    REQUIRE(h.limit(2, 1, Side::Bid, 10000, -5).reject == RejectReason::BadQuantity);
    REQUIRE(h.limit(3, 1, Side::Bid, 0, 100).reject == RejectReason::BadPrice);
    REQUIRE(h.limit(4, 1, Side::Bid, 50000, 100).reject == RejectReason::PriceOutOfRange);
    REQUIRE(h.eng.stats().orders_rejected == 4);
}

// ------------------------------------------------------------------ self-match
TEST(match_prevents_self_match) {
    Harness h(/*prevent_self=*/true);
    h.limit(1, 7, Side::Ask, 10000, 100);       // owner 7 resting
    const auto r = h.limit(2, 7, Side::Bid, 10000, 100);   // owner 7 aggressing
    REQUIRE(h.fills.empty());                   // no wash trade
    REQUIRE(h.eng.stats().self_match_blocks > 0);
    REQUIRE(r.filled == 0);
    REQUIRE(h.eng.leaves(1) == 100);

    // A different owner trades against it normally.
    const auto r2 = h.limit(3, 8, Side::Bid, 10000, 100);
    REQUIRE(r2.state == OrderState::Filled);
    REQUIRE(h.fills.size() == 1);
}

TEST(match_self_match_skips_own_and_fills_others_behind) {
    Harness h(true);
    h.limit(1, 7, Side::Ask, 10000, 100);       // ours, first in queue
    h.limit(2, 9, Side::Ask, 10000, 100);       // someone else's, behind it
    const auto r = h.limit(3, 7, Side::Bid, 10000, 150);
    // Must skip our own and fill the other party's, without deadlocking.
    REQUIRE(r.filled == 100);
    REQUIRE(h.fills.size() == 1);
    REQUIRE(h.fills[0].resting == 2);
    REQUIRE(h.eng.leaves(1) == 100);            // ours untouched
}

TEST(match_self_match_allowed_when_disabled) {
    Harness h(/*prevent_self=*/false);
    h.limit(1, 7, Side::Ask, 10000, 100);
    const auto r = h.limit(2, 7, Side::Bid, 10000, 100);
    REQUIRE(r.state == OrderState::Filled);
    REQUIRE(h.fills.size() == 1);
}

// ------------------------------------------------------------------ cancel
TEST(match_cancel_removes_liquidity) {
    Harness h;
    h.limit(1, 1, Side::Bid, 9990, 100);
    h.limit(2, 1, Side::Bid, 9990, 200);
    REQUIRE(h.eng.book().qty_at(Side::Bid, 9990) == 300);
    const auto r = h.eng.cancel(1, 1);
    REQUIRE(r.state == OrderState::Cancelled);
    REQUIRE(h.eng.book().qty_at(Side::Bid, 9990) == 200);
    REQUIRE(!h.eng.is_resting(1));
    REQUIRE(h.eng.queue_position(2) == 0);      // order 2 moved to the front
}

TEST(match_cancel_unknown_or_wrong_owner_rejected) {
    Harness h;
    h.limit(1, 1, Side::Bid, 9990, 100);
    REQUIRE(h.eng.cancel(999, 1).reject == RejectReason::UnknownOrder);
    REQUIRE(h.eng.cancel(1, 2).reject == RejectReason::UnknownOrder);   // not owner 2's
    REQUIRE(h.eng.is_resting(1));                                        // still there
    REQUIRE(h.eng.cancel(1, 1).state == OrderState::Cancelled);
    REQUIRE(h.eng.cancel(1, 1).reject == RejectReason::UnknownOrder);   // already gone
}

TEST(match_cancel_last_order_empties_side) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10010, 100);
    h.eng.cancel(1, 1);
    Ticks px; Qty q;
    REQUIRE(!h.eng.best(Side::Ask, px, q));
    REQUIRE(h.eng.book().levels(Side::Ask) == 0);
}

// ------------------------------------------------------------------ amend
TEST(match_amend_down_keeps_queue_priority) {
    Harness h;
    h.limit(1, 1, Side::Bid, 9990, 100);        // front of queue
    h.limit(2, 2, Side::Bid, 9990, 100);
    const auto r = h.eng.amend(1, 1, 9990, 60); // reduce size, same price
    REQUIRE(r.state == OrderState::New);
    REQUIRE(h.eng.leaves(1) == 60);
    REQUIRE(h.eng.queue_position(1) == 0);      // priority RETAINED
    REQUIRE(h.eng.book().qty_at(Side::Bid, 9990) == 160);
}

// A size increase must lose priority, or a participant could reserve a queue slot
// with a small order and inflate it once it reached the front.
TEST(match_amend_up_loses_queue_priority) {
    Harness h;
    h.limit(1, 1, Side::Bid, 9990, 100);
    h.limit(2, 2, Side::Bid, 9990, 100);
    h.eng.amend(1, 1, 9990, 300);               // increase
    REQUIRE(h.eng.leaves(1) == 300);
    REQUIRE(h.eng.queue_position(1) == 1);      // now BEHIND order 2
    REQUIRE(h.eng.queue_position(2) == 0);
    REQUIRE(h.eng.book().qty_at(Side::Bid, 9990) == 400);
}

TEST(match_amend_price_can_cross_the_book) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 100);
    h.limit(2, 2, Side::Bid, 9900, 100);        // rests, no cross
    REQUIRE(h.fills.empty());
    const auto r = h.eng.amend(2, 2, 10000, 100);   // reprice up into the ask
    REQUIRE(r.state == OrderState::Filled);
    REQUIRE(h.fills.size() == 1);
    REQUIRE(h.fills[0].price == 10000);
}

TEST(match_amend_unknown_or_bad_rejected) {
    Harness h;
    h.limit(1, 1, Side::Bid, 9990, 100);
    REQUIRE(h.eng.amend(999, 1, 9990, 50).reject == RejectReason::UnknownOrder);
    REQUIRE(h.eng.amend(1, 2, 9990, 50).reject == RejectReason::UnknownOrder);
    REQUIRE(h.eng.amend(1, 1, 9990, 0).reject == RejectReason::BadQuantity);
    REQUIRE(h.eng.amend(1, 1, 50000, 50).reject == RejectReason::PriceOutOfRange);
}

// ------------------------------------------------------------------ determinism
// The engine is a test oracle, so the same inbound sequence must always produce
// byte-identical fills. No clocks, no randomness, no threads.
TEST(match_is_deterministic_across_runs) {
    auto run = [] {
        Harness h;
        std::vector<std::string> log;
        h.eng.on_fill([&](const Fill& f) {
            log.push_back(std::to_string(f.exec_seq) + ":" + std::to_string(f.aggressor) + ":"
                        + std::to_string(f.resting) + ":" + std::to_string(f.price) + ":"
                        + std::to_string(f.qty));
        });
        for (int i = 0; i < 20; ++i)
            h.limit(static_cast<OrderId>(100 + i), static_cast<OwnerId>(i % 3),
                    (i % 2) ? Side::Ask : Side::Bid,
                    (i % 2) ? 10000 + (i % 5) : 10000 - (i % 5), 10 + i);
        return log;
    };
    REQUIRE(run() == run());
    REQUIRE(!run().empty());
}

TEST(match_stats_account_for_activity) {
    Harness h;
    h.limit(1, 1, Side::Ask, 10000, 100);
    h.limit(2, 2, Side::Bid, 10000, 60);
    h.eng.cancel(1, 1);
    REQUIRE(h.eng.stats().orders_accepted == 2);
    REQUIRE(h.eng.stats().fills == 1);
    REQUIRE(h.eng.stats().cancels == 1);
    REQUIRE(h.eng.stats().volume == 60);
    REQUIRE(h.eng.exec_count() == 1);
}

int main() {
    std::printf("=== matching engine tests ===\n");
    return microtest::run_all();
}
