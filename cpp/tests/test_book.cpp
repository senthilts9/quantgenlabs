// test_book.cpp — L2 ladder tests.
//
// These target the specific failure modes the design exists to prevent, not
// just the happy path: best-of-book repair when top clears, rejection of
// out-of-range prices, exact integer price identity, crossed-book detection,
// and allocation-free depth snapshots.
#include "microtest.hpp"
#include "mkt/book/ladder.hpp"

#include <array>

using namespace mkt::book;

// A 90.00–110.00 book on a 0.01 tick.
static Ladder make_book() { return Ladder(9000, 2001); }

TEST(book_empty_has_no_best) {
    auto b = make_book();
    Ticks px; Qty q;
    REQUIRE(!b.best(Side::Bid, px, q));
    REQUIRE(!b.best(Side::Ask, px, q));
    REQUIRE(!b.two_sided());
    REQUIRE(!b.is_crossed());
    REQUIRE(b.levels(Side::Bid) == 0);
}

TEST(book_best_tracks_improvement) {
    auto b = make_book();
    Ticks px; Qty q;

    b.upsert(Side::Bid, 9990, 100);
    REQUIRE(b.best(Side::Bid, px, q)); REQUIRE(px == 9990); REQUIRE(q == 100);

    b.upsert(Side::Bid, 9985, 200);            // worse price, best unchanged
    REQUIRE(b.best(Side::Bid, px, q)); REQUIRE(px == 9990);

    b.upsert(Side::Bid, 9995, 50);             // better price, best moves up
    REQUIRE(b.best(Side::Bid, px, q)); REQUIRE(px == 9995); REQUIRE(q == 50);

    b.upsert(Side::Ask, 10005, 75);
    b.upsert(Side::Ask, 10010, 300);
    REQUIRE(b.best(Side::Ask, px, q)); REQUIRE(px == 10005);  // ask best is LOWEST
    REQUIRE(b.spread_ticks() == 10);
    CHECK_CLOSE(b.mid_ticks(), 10000.0, 1e-12);
}

// The lazy-repair path: clearing top of book must fall through to the next
// populated rung, skipping the empty ticks between them.
TEST(book_repairs_best_when_top_clears) {
    auto b = make_book();
    Ticks px; Qty q;
    b.upsert(Side::Bid, 9995, 50);
    b.upsert(Side::Bid, 9990, 100);
    b.upsert(Side::Bid, 9950, 400);            // 40 ticks below, gap between

    b.upsert(Side::Bid, 9995, 0);              // top clears
    REQUIRE(b.best(Side::Bid, px, q)); REQUIRE(px == 9990);

    b.upsert(Side::Bid, 9990, 0);              // next clears -> must skip the gap
    REQUIRE(b.best(Side::Bid, px, q)); REQUIRE(px == 9950); REQUIRE(q == 400);

    b.upsert(Side::Bid, 9950, 0);              // book empties entirely
    REQUIRE(!b.best(Side::Bid, px, q));
    REQUIRE(b.levels(Side::Bid) == 0);
    REQUIRE(b.total_qty(Side::Bid) == 0);
}

TEST(book_rejects_out_of_range_without_corrupting) {
    auto b = make_book();
    b.upsert(Side::Bid, 9990, 100);
    REQUIRE(!b.upsert(Side::Bid, 8999, 100));   // below floor
    REQUIRE(!b.upsert(Side::Ask, 11001, 100));  // above ceiling
    REQUIRE(!b.upsert(Side::Bid, 9990, -5));    // negative qty
    REQUIRE(b.rejects() == 3);
    Ticks px; Qty q;                            // untouched by the rejects
    REQUIRE(b.best(Side::Bid, px, q)); REQUIRE(px == 9990); REQUIRE(q == 100);
    REQUIRE(b.total_qty(Side::Bid) == 100);
}

TEST(book_level_and_total_accounting) {
    auto b = make_book();
    b.upsert(Side::Bid, 9990, 100);
    b.upsert(Side::Bid, 9980, 200);
    REQUIRE(b.levels(Side::Bid) == 2);
    REQUIRE(b.total_qty(Side::Bid) == 300);
    b.upsert(Side::Bid, 9990, 150);            // resize, not a new level
    REQUIRE(b.levels(Side::Bid) == 2);
    REQUIRE(b.total_qty(Side::Bid) == 350);
    b.upsert(Side::Bid, 9980, 0);              // remove
    REQUIRE(b.levels(Side::Bid) == 1);
    REQUIRE(b.total_qty(Side::Bid) == 150);
}

TEST(book_add_is_incremental_and_clamps) {
    auto b = make_book();
    b.add(Side::Ask, 10010, 100);
    b.add(Side::Ask, 10010, 50);
    REQUIRE(b.qty_at(Side::Ask, 10010) == 150);
    b.add(Side::Ask, 10010, -200);             // over-decrement -> clamp at 0
    REQUIRE(b.qty_at(Side::Ask, 10010) == 0);
    REQUIRE(b.underflows() == 1);
    REQUIRE(b.levels(Side::Ask) == 0);
}

TEST(book_depth_snapshot_is_ordered_and_bounded) {
    auto b = make_book();
    for (int i = 0; i < 5; ++i) b.upsert(Side::Bid, 9990 - i * 5, 100 + i);
    std::array<Level, 3> out{};                 // caller storage: no allocation
    const std::size_t n = b.depth(Side::Bid, out);
    REQUIRE(n == 3);
    REQUIRE(out[0].price == 9990);              // best first
    REQUIRE(out[1].price == 9985);
    REQUIRE(out[2].price == 9980);
    REQUIRE(out[0].qty == 100);

    std::array<Level, 8> wide{};
    REQUIRE(b.depth(Side::Bid, wide) == 5);     // only what exists

    std::array<Level, 4> asks{};
    b.upsert(Side::Ask, 10020, 10);
    b.upsert(Side::Ask, 10005, 20);
    REQUIRE(b.depth(Side::Ask, asks) == 2);
    REQUIRE(asks[0].price == 10005);            // ask depth ascends
    REQUIRE(asks[1].price == 10020);
}

TEST(book_consume_walks_levels_and_reports_vwap) {
    auto b = make_book();
    b.upsert(Side::Ask, 10000, 100);
    b.upsert(Side::Ask, 10010, 100);
    double vwap = 0.0;
    const Qty got = b.consume(Side::Ask, 150, &vwap);
    REQUIRE(got == 150);
    // 100 @ 10000 + 50 @ 10010 -> (1000000 + 500500)/150
    CHECK_CLOSE(vwap, (100.0 * 10000 + 50.0 * 10010) / 150.0, 1e-9);
    Ticks px; Qty q;
    REQUIRE(b.best(Side::Ask, px, q)); REQUIRE(px == 10010); REQUIRE(q == 50);
}

TEST(book_consume_stops_when_side_dry) {
    auto b = make_book();
    b.upsert(Side::Bid, 9990, 40);
    double vwap = 0.0;
    REQUIRE(b.consume(Side::Bid, 100, &vwap) == 40);   // partial, not a hang
    CHECK_CLOSE(vwap, 9990.0, 1e-9);
    Ticks px; Qty q;
    REQUIRE(!b.best(Side::Bid, px, q));
    REQUIRE(b.consume(Side::Bid, 10) == 0);            // empty side fills nothing
}

TEST(book_detects_crossed_state) {
    auto b = make_book();
    b.upsert(Side::Bid, 10000, 100);
    b.upsert(Side::Ask, 10005, 100);
    REQUIRE(!b.is_crossed());
    b.upsert(Side::Ask, 9995, 100);            // ask below bid: dropped message
    REQUIRE(b.is_crossed());
}

TEST(book_micro_price_skews_toward_heavy_side) {
    auto b = make_book();
    b.upsert(Side::Bid, 10000, 900);           // heavy bid
    b.upsert(Side::Ask, 10001, 100);           // light ask
    const double mp = b.micro_price_ticks();
    REQUIRE(mp > b.mid_ticks());               // fair value pulled up toward ask
    REQUIRE(mp < 10001.0);
    CHECK_CLOSE(b.imbalance(), 0.8, 1e-12);    // (900-100)/1000
    CHECK_CLOSE(mp, (10000.0 * 100 + 10001.0 * 900) / 1000.0, 1e-12);
}

// Exact tick identity is the whole reason prices are integers here: two
// updates to "the same price" must land on one level, always.
TEST(book_prices_are_exactly_identified) {
    auto b = make_book();
    b.upsert(Side::Bid, 10000, 100);
    b.upsert(Side::Bid, 10000, 250);
    REQUIRE(b.levels(Side::Bid) == 1);
    REQUIRE(b.qty_at(Side::Bid, 10000) == 250);
    REQUIRE(b.total_qty(Side::Bid) == 250);
}

TEST(book_clear_resets_all_state) {
    auto b = make_book();
    b.upsert(Side::Bid, 9990, 100);
    b.upsert(Side::Ask, 10010, 100);
    b.clear();
    Ticks px; Qty q;
    REQUIRE(!b.best(Side::Bid, px, q));
    REQUIRE(!b.best(Side::Ask, px, q));
    REQUIRE(b.total_qty(Side::Bid) == 0);
    REQUIRE(b.total_qty(Side::Ask) == 0);
    REQUIRE(b.levels(Side::Ask) == 0);
    b.upsert(Side::Bid, 9995, 10);             // usable after clear
    REQUIRE(b.best(Side::Bid, px, q)); REQUIRE(px == 9995);
}

int main() {
    std::printf("=== L2 order book tests ===\n");
    return microtest::run_all();
}
