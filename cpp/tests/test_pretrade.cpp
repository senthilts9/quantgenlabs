// test_pretrade.cpp — pre-trade risk gate tests.
//
// The cases that matter are the adversarial ones: the kill switch must beat every
// other consideration, position limits must be evaluated on the RESULTING
// position (or a sequence of individually-legal orders walks past the mandate),
// and a dead market-data feed must not silently disable the collar.
#include "microtest.hpp"
#include "mkt/risk/pretrade.hpp"

using namespace mkt::risk;

static SymbolLimits basic() {
    SymbolLimits l;
    l.known = true;
    l.max_order_qty      = 10'000;
    l.max_order_notional = 1'000'000;
    l.collar_bps         = 500;        // 5%
    l.max_long_position  = 50'000;
    l.max_short_position = 20'000;
    return l;
}

static MarketContext ref(double px) { return MarketContext{px, true}; }
static MarketContext no_ref()       { return MarketContext{0.0, false}; }

static OrderRequest ord(Side s, double qty, double px, OrdType t = OrdType::Limit) {
    OrderRequest o; o.symbol = 0; o.side = s; o.qty = qty; o.price = px; o.type = t;
    return o;
}

TEST(gate_accepts_a_normal_order) {
    PreTradeGate g(4);
    g.set_limits(0, basic());
    REQUIRE(accepted(g.check(ord(Side::Buy, 100, 100.0), ref(100.0))));
    REQUIRE(g.stats().accepted == 1);
    REQUIRE(g.stats().rejected() == 0);
}

// The kill switch must beat everything, including an otherwise-perfect order.
TEST(gate_kill_switch_overrides_everything) {
    PreTradeGate g(4);
    g.set_limits(0, basic());
    REQUIRE(accepted(g.check(ord(Side::Buy, 100, 100.0), ref(100.0))));
    g.engage_kill_switch();
    REQUIRE(g.kill_switch_engaged());
    REQUIRE(g.check(ord(Side::Buy, 100, 100.0), ref(100.0)) == Decision::KillSwitchEngaged);
    REQUIRE(g.check(ord(Side::Sell, 1, 100.0), ref(100.0)) == Decision::KillSwitchEngaged);
    g.release_kill_switch();
    REQUIRE(accepted(g.check(ord(Side::Buy, 100, 100.0), ref(100.0))));
    REQUIRE(g.stats().kill_switch == 2);
}

TEST(gate_rejects_unknown_and_halted_symbols) {
    PreTradeGate g(4);
    g.set_limits(0, basic());
    REQUIRE(g.check(ord(Side::Buy, 1, 100.0), ref(100.0)) == Decision::Accept);
    auto o = ord(Side::Buy, 1, 100.0); o.symbol = 1;          // never configured
    REQUIRE(g.check(o, ref(100.0)) == Decision::UnknownSymbol);
    o.symbol = 99;                                             // out of range
    REQUIRE(g.check(o, ref(100.0)) == Decision::UnknownSymbol);
    g.halt(0, true);
    REQUIRE(g.check(ord(Side::Buy, 1, 100.0), ref(100.0)) == Decision::SymbolHalted);
    g.halt(0, false);
    REQUIRE(accepted(g.check(ord(Side::Buy, 1, 100.0), ref(100.0))));
}

TEST(gate_rejects_malformed_quantities_and_prices) {
    PreTradeGate g(4);
    g.set_limits(0, basic());
    REQUIRE(g.check(ord(Side::Buy, 0, 100.0), ref(100.0)) == Decision::BadQuantity);
    REQUIRE(g.check(ord(Side::Buy, -5, 100.0), ref(100.0)) == Decision::BadQuantity);
    REQUIRE(g.check(ord(Side::Buy, std::nan(""), 100.0), ref(100.0)) == Decision::BadQuantity);
    REQUIRE(g.check(ord(Side::Buy, 10, 0.0), ref(100.0)) == Decision::BadPrice);
    REQUIRE(g.check(ord(Side::Buy, 10, -1.0), ref(100.0)) == Decision::BadPrice);
}

// Fat finger #1: an extra digit in QUANTITY.
TEST(gate_catches_fat_finger_quantity) {
    PreTradeGate g(4);
    g.set_limits(0, basic());
    REQUIRE(accepted(g.check(ord(Side::Buy, 1'000, 100.0), ref(100.0))));
    REQUIRE(g.check(ord(Side::Buy, 100'000, 100.0), ref(100.0)) == Decision::MaxOrderQty);
    REQUIRE(g.stats().max_qty == 1);
}

// Fat finger #2: an extra digit in PRICE, caught by the collar.
TEST(gate_catches_fat_finger_price_via_collar) {
    PreTradeGate g(4);
    g.set_limits(0, basic());                    // 500 bps = 5%
    REQUIRE(accepted(g.check(ord(Side::Buy, 10, 104.0), ref(100.0))));   // 4% away
    REQUIRE(g.check(ord(Side::Buy, 10, 106.0), ref(100.0)) == Decision::PriceCollar);
    REQUIRE(g.check(ord(Side::Buy, 10, 1000.0), ref(100.0)) == Decision::PriceCollar);
    REQUIRE(g.check(ord(Side::Sell, 10, 50.0), ref(100.0)) == Decision::PriceCollar); // both directions
    REQUIRE(g.stats().collar == 3);
}

TEST(gate_enforces_notional_ceiling) {
    PreTradeGate g(4);
    SymbolLimits l = basic();
    l.max_order_notional = 100'000;
    l.max_order_qty = 1e9;                       // isolate notional from qty
    g.set_limits(0, l);
    REQUIRE(accepted(g.check(ord(Side::Buy, 500, 100.0), ref(100.0))));      // 50k
    REQUIRE(g.check(ord(Side::Buy, 2'000, 100.0), ref(100.0)) == Decision::MaxNotional);
    REQUIRE(g.stats().max_notional == 1);
}

TEST(gate_notional_scales_with_contract_size) {
    PreTradeGate g(4);
    SymbolLimits l = basic();
    l.max_order_notional = 100'000;
    l.max_order_qty = 1e9;
    l.contract_size = 100.0;                     // an option/future multiplier
    g.set_limits(0, l);
    // 10 contracts * 100 price * 100 multiplier = 100,000 -> at the limit
    REQUIRE(accepted(g.check(ord(Side::Buy, 10, 100.0), ref(100.0))));
    REQUIRE(g.check(ord(Side::Buy, 11, 100.0), ref(100.0)) == Decision::MaxNotional);
}

// A dead feed must not silently disable fat-finger protection.
TEST(gate_refuses_when_reference_price_unusable_by_default) {
    PreTradeGate g(4);
    g.set_limits(0, basic());
    REQUIRE(g.check(ord(Side::Buy, 10, 100.0), no_ref()) == Decision::NoReferencePrice);
    REQUIRE(g.check(ord(Side::Buy, 10, 0.0, OrdType::Market), no_ref()) == Decision::NoReferencePrice);
    REQUIRE(g.stats().no_reference == 2);
}

// Desks that accept the opposite trade-off can configure it, explicitly.
TEST(gate_can_be_configured_to_pass_without_reference) {
    GateConfig c; c.reject_without_reference = false;
    PreTradeGate g(4, c);
    g.set_limits(0, basic());
    REQUIRE(accepted(g.check(ord(Side::Buy, 10, 100.0), no_ref())));
    // Static limits still apply with no market data at all.
    REQUIRE(g.check(ord(Side::Buy, 100'000, 100.0), no_ref()) == Decision::MaxOrderQty);
}

TEST(gate_market_order_uses_reference_for_notional) {
    PreTradeGate g(4);
    SymbolLimits l = basic();
    l.max_order_notional = 100'000;
    l.max_order_qty = 1e9;
    g.set_limits(0, l);
    REQUIRE(accepted(g.check(ord(Side::Buy, 500, 0.0, OrdType::Market), ref(100.0))));
    REQUIRE(g.check(ord(Side::Buy, 2'000, 0.0, OrdType::Market), ref(100.0)) == Decision::MaxNotional);
    // A market order carries no price, so the collar cannot and should not fire.
    REQUIRE(accepted(g.check(ord(Side::Buy, 1, 0.0, OrdType::Market), ref(100.0))));
}

// The core position-limit property: limits apply to the RESULTING position, so a
// sequence of individually-legal orders cannot walk the book past the mandate.
TEST(gate_position_limit_is_on_resulting_position) {
    PreTradeGate g(4);
    SymbolLimits l = basic();
    l.max_long_position = 1'000;
    g.set_limits(0, l);
    g.set_position(0, 900);
    REQUIRE(accepted(g.check(ord(Side::Buy, 100, 100.0), ref(100.0))));   // -> 1000, at limit
    REQUIRE(g.check(ord(Side::Buy, 101, 100.0), ref(100.0)) == Decision::PositionLimit);
    // Selling reduces exposure and stays legal.
    REQUIRE(accepted(g.check(ord(Side::Sell, 500, 100.0), ref(100.0))));
}

TEST(gate_short_limit_is_separate_from_long) {
    PreTradeGate g(4);
    SymbolLimits l = basic();
    l.max_long_position = 50'000;
    l.max_short_position = 1'000;
    g.set_limits(0, l);
    g.set_position(0, 0);
    REQUIRE(accepted(g.check(ord(Side::Sell, 1'000, 100.0), ref(100.0))));  // -> -1000
    REQUIRE(g.check(ord(Side::Sell, 1'001, 100.0), ref(100.0)) == Decision::PositionLimit);
    // 5,000 @ 100 = 500k notional, inside basic()'s 1M ceiling, so this
    // isolates the position check rather than tripping the notional one.
    REQUIRE(accepted(g.check(ord(Side::Buy, 5'000, 100.0), ref(100.0))));   // long side is roomier
}

TEST(gate_fills_move_the_position) {
    PreTradeGate g(4);
    SymbolLimits l = basic();
    l.max_long_position = 1'000;
    g.set_limits(0, l);
    REQUIRE(accepted(g.check(ord(Side::Buy, 600, 100.0), ref(100.0))));
    g.on_fill(0, Side::Buy, 600);
    CHECK_CLOSE(g.position(0), 600.0, 1e-12);
    REQUIRE(g.check(ord(Side::Buy, 500, 100.0), ref(100.0)) == Decision::PositionLimit);
    g.on_fill(0, Side::Sell, 600);
    CHECK_CLOSE(g.position(0), 0.0, 1e-12);
    REQUIRE(accepted(g.check(ord(Side::Buy, 500, 100.0), ref(100.0))));
}

TEST(gate_caps_open_orders) {
    GateConfig c; c.max_open_orders_per_symbol = 3;
    PreTradeGate g(4, c);
    g.set_limits(0, basic());
    for (int i = 0; i < 3; ++i) {
        REQUIRE(accepted(g.check(ord(Side::Buy, 1, 100.0), ref(100.0))));
        g.on_order_sent(0);
    }
    REQUIRE(g.open_orders(0) == 3);
    REQUIRE(g.check(ord(Side::Buy, 1, 100.0), ref(100.0)) == Decision::MaxOpenOrders);
    g.on_order_closed(0);
    REQUIRE(accepted(g.check(ord(Side::Buy, 1, 100.0), ref(100.0))));
}

// A runaway algorithm is a rate problem, not a size problem.
TEST(gate_rate_limits_runaway_algo) {
    GateConfig c;
    c.max_orders_per_sec = 10.0;
    c.burst_allowance    = 5.0;
    PreTradeGate g(4, c);
    g.set_limits(0, basic());
    double t = 100.0;
    // Burst of 5 is allowed, the 6th at the same instant is not.
    for (int i = 0; i < 5; ++i)
        REQUIRE(accepted(g.check(ord(Side::Buy, 1, 100.0), ref(100.0), t)));
    REQUIRE(g.check(ord(Side::Buy, 1, 100.0), ref(100.0), t) == Decision::MessageRateLimit);
    // Half a second later the bucket has refilled by 5.
    t += 0.5;
    REQUIRE(accepted(g.check(ord(Side::Buy, 1, 100.0), ref(100.0), t)));
    REQUIRE(g.stats().rate_limited == 1);
}

TEST(gate_stats_account_for_every_decision) {
    PreTradeGate g(4);
    g.set_limits(0, basic());
    g.check(ord(Side::Buy, 1, 100.0), ref(100.0));            // accept
    g.check(ord(Side::Buy, 1e9, 100.0), ref(100.0));          // max qty
    g.check(ord(Side::Buy, 1, 500.0), ref(100.0));            // collar
    g.engage_kill_switch();
    g.check(ord(Side::Buy, 1, 100.0), ref(100.0));            // kill switch
    REQUIRE(g.stats().checked == 4);
    REQUIRE(g.stats().accepted == 1);
    REQUIRE(g.stats().rejected() == 3);
    g.reset_stats();
    REQUIRE(g.stats().checked == 0);
}

int main() {
    std::printf("=== pre-trade risk gate tests ===\n");
    return microtest::run_all();
}
