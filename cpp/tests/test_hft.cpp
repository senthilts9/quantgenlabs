// test_hft.cpp — microstructure signals, market making, adverse selection.
//
// SIGN CONVENTIONS GET THE MOST ATTENTION HERE. A microstructure signal with an
// inverted sign is worse than no signal: it trades confidently in the wrong
// direction and looks like bad luck for a long time. Every directional property
// below is asserted in both directions for that reason.
#include "microtest.hpp"
#include "mkt/hft/market_maker.hpp"
#include "mkt/hft/markout.hpp"
#include "mkt/hft/signals.hpp"

using namespace mkt::hft;
using mkt::book::Ladder;
using mkt::book::Side;

static Ladder two_sided(int bid_qty, int ask_qty, int bid_px = 9990, int ask_px = 10010) {
    Ladder b(1, 50000);
    b.upsert(Side::Bid, bid_px, bid_qty);
    b.upsert(Side::Ask, ask_px, ask_qty);
    return b;
}

// ------------------------------------------------------------------ EWMA
TEST(ewma_tracks_mean_and_variance) {
    Ewma e(0.9);
    for (int i = 0; i < 200; ++i) e.update(5.0);
    CHECK_CLOSE(e.mean(), 5.0, 1e-6);
    CHECK_CLOSE(e.stdev(), 0.0, 1e-6);
    REQUIRE(e.warm());
}

TEST(ewma_is_cold_until_enough_samples) {
    Ewma e;
    e.update(1.0);
    REQUIRE(!e.warm(30));
    for (int i = 0; i < 40; ++i) e.update(1.0);
    REQUIRE(e.warm(30));
}

TEST(ewma_adapts_to_a_regime_change) {
    Ewma e(0.9);
    for (int i = 0; i < 100; ++i) e.update(1.0);
    for (int i = 0; i < 100; ++i) e.update(10.0);
    REQUIRE(e.mean() > 9.0);            // followed the new level
}

// -------------------------------------------------------------- imbalance
// POSITIVE imbalance = more bid than ask = buying pressure. Inverting this is
// the single most damaging sign error in the file.
TEST(imbalance_is_positive_when_the_bid_is_heavier) {
    FeatureEngine f;
    auto b = two_sided(900, 100);
    f.on_book(b);
    REQUIRE(f.features().imbalance > 0.5);
}

TEST(imbalance_is_negative_when_the_ask_is_heavier) {
    FeatureEngine f;
    auto b = two_sided(100, 900);
    f.on_book(b);
    REQUIRE(f.features().imbalance < -0.5);
}

TEST(imbalance_is_zero_when_balanced) {
    FeatureEngine f;
    auto b = two_sided(500, 500);
    f.on_book(b);
    CHECK_CLOSE(f.features().imbalance, 0.0, 1e-9);
}

// Micro price sits nearer the side with LESS size -- that side is about to be
// consumed, so fair value is closer to it.
TEST(micro_price_leans_toward_the_thinner_side) {
    FeatureEngine f;
    auto heavy_bid = two_sided(900, 100);
    f.on_book(heavy_bid);
    REQUIRE(f.features().micro_minus_mid > 0);      // leans toward the ask

    FeatureEngine g;
    auto heavy_ask = two_sided(100, 900);
    g.on_book(heavy_ask);
    REQUIRE(g.features().micro_minus_mid < 0);
}

TEST(depth_imbalance_reads_beyond_the_touch) {
    // A spoofed touch: one lot on the bid, but real size behind it.
    Ladder b(1, 50000);
    b.upsert(Side::Bid, 9990, 1);
    for (int i = 1; i < 5; ++i) b.upsert(Side::Bid, 9990 - i, 1000);
    b.upsert(Side::Ask, 10010, 1);
    for (int i = 1; i < 5; ++i) b.upsert(Side::Ask, 10010 + i, 10);

    FeatureEngine f;
    f.on_book(b);
    CHECK_CLOSE(f.features().imbalance, 0.0, 1e-9);   // touch says balanced
    REQUIRE(f.features().depth_imbalance > 0.8);      // depth says heavily bid
}

TEST(no_features_without_a_two_sided_book) {
    Ladder b(1, 50000);
    b.upsert(Side::Bid, 9990, 100);
    FeatureEngine f;
    f.on_book(b);
    REQUIRE(!f.features().two_sided);
    CHECK_CLOSE(f.fair_value_edge(), 0.0, 1e-12);
}

// ------------------------------------------------------------- trade flow
TEST(flow_imbalance_is_positive_when_buyers_are_aggressing) {
    FeatureEngine f;
    auto b = two_sided(500, 500);
    f.on_book(b);
    for (int i = 0; i < 50; ++i) f.on_trade(100, /*aggressor_bought=*/true);
    REQUIRE(f.features().flow_imbalance > 0);
}

TEST(flow_imbalance_is_negative_when_sellers_are_aggressing) {
    FeatureEngine f;
    auto b = two_sided(500, 500);
    f.on_book(b);
    for (int i = 0; i < 50; ++i) f.on_trade(100, false);
    REQUIRE(f.features().flow_imbalance < 0);
}

TEST(flow_imbalance_is_bounded_so_instruments_are_comparable) {
    FeatureEngine f;
    auto b = two_sided(500, 500);
    f.on_book(b);
    for (int i = 0; i < 200; ++i) f.on_trade(1'000'000, true);
    REQUIRE(f.features().flow_imbalance <= 1.0);
    REQUIRE(f.features().flow_imbalance >= -1.0);
}

TEST(composite_edge_agrees_in_sign_with_its_inputs) {
    FeatureEngine bull;
    auto heavy_bid = two_sided(900, 100);
    bull.on_book(heavy_bid);
    for (int i = 0; i < 40; ++i) bull.on_trade(100, true);
    REQUIRE(bull.fair_value_edge() > 0);

    FeatureEngine bear;
    auto heavy_ask = two_sided(100, 900);
    bear.on_book(heavy_ask);
    for (int i = 0; i < 40; ++i) bear.on_trade(100, false);
    REQUIRE(bear.fair_value_edge() < 0);
}

TEST(edge_is_clamped_to_unit_range) {
    FeatureEngine f;
    auto b = two_sided(1'000'000, 1);
    f.on_book(b);
    for (int i = 0; i < 500; ++i) f.on_trade(1'000'000, true);
    REQUIRE(f.fair_value_edge() <= 1.0);
}

// ---------------------------------------------------------- queue position
TEST(queue_position_starts_behind_what_is_resting) {
    QueueTracker q;
    q.join(5000);
    REQUIRE(q.ahead() == 5000);
    REQUIRE(!q.at_front());
}

TEST(trades_consume_the_queue_ahead) {
    QueueTracker q;
    q.join(1000);
    q.on_trade(400);
    REQUIRE(q.ahead() == 600);
    q.on_trade(600);
    REQUIRE(q.at_front());
    q.on_trade(999);                 // cannot go negative
    REQUIRE(q.ahead() == 0);
}

// A cancel of unknown position must be assumed BEHIND us. Assuming it was ahead
// claims a better queue position than we have -- optimistic in exactly the
// direction that loses money.
TEST(unknown_cancels_do_not_improve_the_claimed_position) {
    QueueTracker q;
    q.join(1000);
    q.on_cancel(500);                        // position unknown
    REQUIRE(q.ahead() == 1000);              // unchanged, pessimistically
    q.on_cancel(500, /*known_ahead=*/true, 500);
    REQUIRE(q.ahead() == 500);               // known ahead: now it counts
}

TEST(fill_probability_reflects_queue_position) {
    QueueTracker front, back;
    front.join(0);
    back.join(10'000);
    CHECK_CLOSE(front.fill_probability(100), 1.0, 1e-9);
    REQUIRE(back.fill_probability(100) < 0.02);
    // Two orders at the same price are not the same asset.
    REQUIRE(front.fill_probability(100) > back.fill_probability(100));
}

TEST(fill_probability_is_zero_before_joining) {
    QueueTracker q;
    CHECK_CLOSE(q.fill_probability(1e9), 0.0, 1e-12);
}

// ------------------------------------------------------------ market maker
TEST(quotes_straddle_the_mid_when_flat) {
    MarketMaker m;
    const auto q = m.quote(10000.0, 2.0);
    REQUIRE(q.bid_ticks < 10000.0);
    REQUIRE(q.ask_ticks > 10000.0);
    CHECK_CLOSE(q.reservation, 10000.0, 1e-9);     // no inventory, no skew
    CHECK_CLOSE(q.inventory_skew, 0.0, 1e-9);
    REQUIRE(q.quote_bid && q.quote_ask);
}

// The sign that matters most: LONG inventory must push the reservation price
// DOWN so the maker sheds it. Inverting this accumulates into losses.
TEST(long_inventory_skews_quotes_down) {
    MarketMaker m;
    m.on_fill(+500);
    const auto q = m.quote(10000.0, 2.0);
    REQUIRE(q.reservation < 10000.0);
    REQUIRE(q.inventory_skew < 0);
    REQUIRE(q.ask_ticks < 10000.0 + q.half_spread);   // ask pulled in, easier to hit
}

TEST(short_inventory_skews_quotes_up) {
    MarketMaker m;
    m.on_fill(-500);
    const auto q = m.quote(10000.0, 2.0);
    REQUIRE(q.reservation > 10000.0);
    REQUIRE(q.inventory_skew > 0);
}

TEST(skew_scales_with_inventory_size) {
    MarketMaker a, b;
    a.on_fill(100);
    b.on_fill(1000);
    REQUIRE(std::abs(b.quote(10000.0, 2.0).inventory_skew) >
            std::abs(a.quote(10000.0, 2.0).inventory_skew));
}

TEST(spread_widens_with_volatility) {
    MarketMaker m;
    const double calm = m.quote(10000.0, 1.0).half_spread;
    const double wild = m.quote(10000.0, 10.0).half_spread;
    REQUIRE(wild > calm);
}

TEST(spread_widens_with_risk_aversion) {
    MakerParams lo; lo.gamma = 0.05;
    MakerParams hi; hi.gamma = 0.5;
    MarketMaker a(lo), b(hi);
    REQUIRE(b.quote(10000.0, 5.0).half_spread > a.quote(10000.0, 5.0).half_spread);
}

TEST(higher_flow_intensity_tightens_the_spread) {
    MakerParams thin; thin.kappa = 0.2;
    MakerParams thick; thick.kappa = 20.0;
    MarketMaker a(thin), b(thick);
    REQUIRE(b.quote(10000.0, 2.0).half_spread < a.quote(10000.0, 2.0).half_spread);
}

// At the horizon there is no time left to be hurt by a position, so the maker
// stops skewing and quotes symmetrically.
TEST(inventory_skew_vanishes_at_the_horizon) {
    MakerParams p; p.time_to_horizon = 0.0;
    MarketMaker m(p);
    m.on_fill(1000);
    CHECK_CLOSE(m.quote(10000.0, 5.0).inventory_skew, 0.0, 1e-9);
}

TEST(spread_is_clamped_to_sane_bounds) {
    MakerParams p; p.min_half_spread_ticks = 2.0; p.max_half_spread_ticks = 5.0;
    MarketMaker m(p);
    REQUIRE(m.quote(10000.0, 0.0).half_spread >= 2.0);
    REQUIRE(m.quote(10000.0, 1000.0).half_spread <= 5.0);
}

// At the limit, quoting both sides turns a position you cannot hold into a
// larger one.
TEST(inventory_limit_suppresses_the_side_that_would_add) {
    MakerParams p; p.max_inventory = 100.0;
    MarketMaker m(p);
    m.on_fill(150);
    auto q = m.quote(10000.0, 2.0);
    REQUIRE(!q.quote_bid);
    REQUIRE(q.quote_ask);                       // can still reduce
    REQUIRE(std::string(q.suppressed_reason).find("long") != std::string::npos);

    m.set_inventory(-150);
    q = m.quote(10000.0, 2.0);
    REQUIRE(q.quote_bid);
    REQUIRE(!q.quote_ask);
}

TEST(edge_tilts_fair_value_when_enabled) {
    MakerParams p; p.edge_ticks = 5.0;
    MarketMaker m(p);
    const double up = m.quote(10000.0, 2.0, +1.0).reservation;
    const double flat = m.quote(10000.0, 2.0, 0.0).reservation;
    const double down = m.quote(10000.0, 2.0, -1.0).reservation;
    REQUIRE(up > flat);
    REQUIRE(down < flat);
    CHECK_CLOSE(up - flat, 5.0, 1e-9);
}

TEST(edge_is_ignored_when_weight_is_zero) {
    MarketMaker m;                              // edge_ticks defaults to 0
    CHECK_CLOSE(m.quote(10000.0, 2.0, 1.0).reservation,
                m.quote(10000.0, 2.0, -1.0).reservation, 1e-12);
}

TEST(degenerate_parameters_produce_a_quote_not_an_infinity) {
    MakerParams p; p.gamma = 0.0; p.kappa = 0.0;
    MarketMaker m(p);
    const auto q = m.quote(10000.0, 2.0);
    REQUIRE(std::isfinite(q.bid_ticks));
    REQUIRE(std::isfinite(q.ask_ticks));
    REQUIRE(q.ask_ticks > q.bid_ticks);
}

// --------------------------------------------------------------- markout
// POSITIVE is always good, both sides, so markouts aggregate across a book.
TEST(a_buy_followed_by_a_rise_is_a_positive_markout) {
    MarkoutTracker t;
    t.on_fill(0, 100.0, /*is_buy=*/true);
    t.on_mid(2'000'000, 101.0);                 // past the 1ms horizon
    REQUIRE(t.at(0).n == 1);
    REQUIRE(t.at(0).mean_bps() > 0);
}

TEST(a_sell_followed_by_a_fall_is_also_a_positive_markout) {
    MarkoutTracker t;
    t.on_fill(0, 100.0, /*is_buy=*/false);
    t.on_mid(2'000'000, 99.0);
    REQUIRE(t.at(0).mean_bps() > 0);
}

TEST(a_buy_followed_by_a_fall_is_adverse_selection) {
    MarkoutTracker t;
    t.on_fill(0, 100.0, true);
    t.on_mid(2'000'000, 99.0);
    REQUIRE(t.at(0).mean_bps() < 0);
    CHECK_CLOSE(t.at(0).mean_bps(), -100.0, 1e-6);   // 1% = 100bps
}

// A fill measured before its horizon is measured against a mid that has not had
// time to move, biasing every markout toward zero.
TEST(fills_are_not_measured_before_their_horizon) {
    MarkoutTracker t;
    t.on_fill(0, 100.0, true);
    t.on_mid(500'000, 101.0);                   // 0.5ms, before the 1ms horizon
    REQUIRE(t.at(0).n == 0);
    REQUIRE(t.pending() == 1);
    t.on_mid(2'000'000, 101.0);
    REQUIRE(t.at(0).n == 1);
}

TEST(longer_horizons_mature_later) {
    MarkoutTracker t;
    t.on_fill(0, 100.0, true);
    t.on_mid(2'000'000, 101.0);                 // 1ms only
    REQUIRE(t.at(0).n == 1);
    REQUIRE(t.at(3).n == 0);                    // 1s not yet
    t.on_mid(2'000'000'000, 102.0);
    REQUIRE(t.at(3).n == 1);
}

TEST(pending_fills_are_retired_once_fully_matured) {
    MarkoutTracker t;
    for (int i = 0; i < 10; ++i) t.on_fill(0, 100.0, true);
    REQUIRE(t.pending() == 10);
    t.on_mid(20'000'000'000LL, 100.0);          // past the longest horizon
    REQUIRE(t.pending() == 0);
}

// A markout of -0.2bps on six fills is noise; on sixty thousand it is a strategy
// being picked off. Reporting the mean without this invites acting on the first.
TEST(significance_requires_both_a_t_stat_and_a_sample) {
    MarkoutTracker few;
    for (int i = 0; i < 5; ++i) {
        few.on_fill(static_cast<std::int64_t>(i) * 100'000'000LL, 100.0, true);
        few.on_mid(static_cast<std::int64_t>(i) * 100'000'000LL + 2'000'000, 99.0);
    }
    REQUIRE(!few.at(0).significant());           // too few to call

    // Noisy but consistently adverse, which is what real pickoff looks like:
    // individual fills vary, the mean is negative.
    MarkoutTracker many;
    for (int i = 0; i < 200; ++i) {
        const std::int64_t t0 = static_cast<std::int64_t>(i) * 100'000'000LL;
        many.on_fill(t0, 100.0, true);
        const double jitter = ((i % 7) - 3) * 0.02;   // +/- 6bps of noise
        many.on_mid(t0 + 2'000'000, 99.9 + jitter);
    }
    REQUIRE(many.at(0).n >= 30);
    REQUIRE(many.at(0).mean_bps() < 0);
    REQUIRE(many.at(0).significant());
    REQUIRE(many.adversely_selected());
}

// The degenerate case the t_stat guard used to get backwards.
TEST(perfectly_consistent_adverse_markout_is_maximally_significant) {
    MarkoutTracker t;
    for (int i = 0; i < 100; ++i) {
        const std::int64_t t0 = static_cast<std::int64_t>(i) * 100'000'000LL;
        t.on_fill(t0, 100.0, true);
        t.on_mid(t0 + 2'000'000, 99.0);     // identical -100bps every time
    }
    CHECK_CLOSE(t.at(0).stdev_bps(), 0.0, 1e-6);
    REQUIRE(t.at(0).t_stat() < -1000.0);    // not zero
    REQUIRE(t.at(0).significant());
    REQUIRE(t.adversely_selected());
}


TEST(profitable_flow_is_not_flagged_as_adverse) {
    MarkoutTracker t;
    for (int i = 0; i < 200; ++i) {
        const std::int64_t t0 = static_cast<std::int64_t>(i) * 100'000'000LL;
        t.on_fill(t0, 100.0, true);
        t.on_mid(t0 + 2'000'000, 100.1);        // market moved our way
    }
    REQUIRE(t.at(0).mean_bps() > 0);
    REQUIRE(!t.adversely_selected());
}

TEST(markout_records_the_range_not_just_the_mean) {
    MarkoutTracker t;
    t.on_fill(0, 100.0, true);
    t.on_mid(2'000'000, 101.0);
    t.on_fill(10'000'000, 100.0, true);
    t.on_mid(12'000'000, 99.0);
    REQUIRE(t.at(0).n == 2);
    REQUIRE(t.at(0).max_bps > 0);
    REQUIRE(t.at(0).min_bps < 0);
}

int main() {
    std::printf("=== HFT signals, market making, adverse selection ===\n");
    return microtest::run_all();
}
