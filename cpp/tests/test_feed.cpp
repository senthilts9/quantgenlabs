// test_feed.cpp — market-data quality gate tests.
//
// The cases that matter are the ones that distinguish similar-looking faults:
// a gap is not a duplicate is not a reorder, and a cold validator must not
// reject a legitimate first price for deviating from an empty distribution.
#include "microtest.hpp"
#include "mkt/feed/validator.hpp"

using namespace mkt::feed;

static constexpr std::int64_t S = 1'000'000'000LL;  // one second in ns

static Tick mk(std::uint64_t seq, double px, std::int64_t ns, double sz = 1.0) {
    return Tick{seq, ns, ns, px, sz};
}

// Feed `n` benign ticks so the statistical checks are armed.
static void warm(Validator& v, std::uint64_t n, double px = 100.0) {
    for (std::uint64_t i = 1; i <= n; ++i) {
        // Alternate tiny moves so variance is non-zero but small.
        const double p = px * (1.0 + ((i % 2) ? 1e-4 : -1e-4));
        v.check(mk(i, p, static_cast<std::int64_t>(i) * S));
    }
}

TEST(feed_accepts_clean_stream) {
    Validator v;
    for (std::uint64_t i = 1; i <= 20; ++i)
        REQUIRE(accepted(v.check(mk(i, 100.0 + 0.01 * static_cast<double>(i), static_cast<std::int64_t>(i) * S))));
    REQUIRE(v.counters().accepted == 20);
    REQUIRE(v.counters().total_rejected() == 0);
    CHECK_CLOSE(v.reject_rate(), 0.0, 1e-12);
    REQUIRE(v.last_seq() == 20);
}

// The three-way split: same seq / lower seq / higher seq are three faults.
TEST(feed_distinguishes_gap_duplicate_reorder) {
    Validator v;
    REQUIRE(accepted(v.check(mk(1, 100.0, S))));
    REQUIRE(accepted(v.check(mk(2, 100.0, 2 * S))));

    REQUIRE(v.check(mk(2, 100.0, 3 * S)) == Verdict::Duplicate);
    REQUIRE(v.check(mk(1, 100.0, 3 * S)) == Verdict::Reordered);
    REQUIRE(v.check(mk(9, 100.0, 4 * S)) == Verdict::SequenceGap);

    REQUIRE(v.counters().duplicates == 1);
    REQUIRE(v.counters().reordered == 1);
    REQUIRE(v.counters().gaps == 1);
    REQUIRE(v.counters().gap_messages_lost == 6);   // 3..8 inclusive
}

// After a gap the validator must resynchronise, not reject forever.
TEST(feed_resynchronises_after_gap) {
    Validator v;
    v.check(mk(1, 100.0, S));
    REQUIRE(v.check(mk(50, 100.0, 2 * S)) == Verdict::SequenceGap);
    REQUIRE(v.needs_recovery());
    REQUIRE(accepted(v.check(mk(51, 100.0, 3 * S))));   // continues from 50
    REQUIRE(v.needs_recovery());                        // still flagged
    v.recovered(51);
    REQUIRE(!v.needs_recovery());
    REQUIRE(accepted(v.check(mk(52, 100.0, 4 * S))));
}

TEST(feed_rejects_backwards_exchange_clock) {
    Validator v;
    REQUIRE(accepted(v.check(mk(1, 100.0, 10 * S))));
    REQUIRE(v.check(mk(2, 100.0, 9 * S)) == Verdict::StaleTimestamp);
    REQUIRE(v.counters().stale_timestamp == 1);
}

TEST(feed_rejects_excessively_delayed_tick) {
    Config c; c.max_age_ns = 2 * S;
    Validator v(c);
    Tick t = mk(1, 100.0, 10 * S);
    t.recv_ns = t.exch_ns + 5 * S;                  // 5s behind, tolerance 2s
    REQUIRE(v.check(t) == Verdict::TooOld);
    REQUIRE(v.counters().too_old == 1);
}

TEST(feed_enforces_hard_price_band) {
    Config c; c.price_min = 50.0; c.price_max = 150.0;
    Validator v(c);
    REQUIRE(accepted(v.check(mk(1, 100.0, S))));
    REQUIRE(v.check(mk(2, 200.0, 2 * S)) == Verdict::PriceOutOfBand);
    REQUIRE(v.check(mk(3, 10.0,  3 * S)) == Verdict::PriceOutOfBand);
    REQUIRE(v.counters().out_of_band == 2);
}

TEST(feed_rejects_nonfinite_and_nonpositive) {
    Validator v;
    REQUIRE(v.check(mk(1, 0.0, S))  == Verdict::BadValue);
    REQUIRE(v.check(mk(2, -1.0, S)) == Verdict::BadValue);
    REQUIRE(v.check(mk(3, std::nan(""), S)) == Verdict::BadValue);
    Tick neg = mk(4, 100.0, S); neg.size = -5.0;
    REQUIRE(v.check(neg) == Verdict::BadValue);
    REQUIRE(v.counters().bad_values == 4);
}

// A cold validator has no distribution; the first real price must be accepted.
TEST(feed_outlier_check_is_disarmed_until_warm) {
    Config c; c.min_samples = 30; c.max_z = 3.0;
    Validator v(c);
    // A violent move as the second tick — statistically absurd, but there is no
    // history to judge it against, so it must not be rejected as an outlier.
    REQUIRE(accepted(v.check(mk(1, 100.0, S))));
    REQUIRE(accepted(v.check(mk(2, 180.0, 2 * S))));
    REQUIRE(v.counters().outliers == 0);
}

TEST(feed_catches_outlier_once_warm) {
    Config c; c.min_samples = 30; c.max_z = 8.0;
    Validator v(c);
    warm(v, 200);                                   // ~1bp moves
    const double last = v.last_price();
    // A 40% jump against a 1bp-scale distribution is thousands of sigmas.
    REQUIRE(v.check(mk(201, last * 1.40, 300 * S)) == Verdict::PriceOutlier);
    REQUIRE(v.counters().outliers == 1);
    // A move in line with the distribution still passes.
    REQUIRE(accepted(v.check(mk(202, last * 1.0001, 301 * S))));
}

// Zero variance (a book that never moved) must not make every move infinite-z.
TEST(feed_flat_history_does_not_reject_everything) {
    Config c; c.min_samples = 10; c.max_z = 3.0;
    Validator v(c);
    for (std::uint64_t i = 1; i <= 50; ++i)
        REQUIRE(accepted(v.check(mk(i, 100.0, static_cast<std::int64_t>(i) * S))));
    REQUIRE(accepted(v.check(mk(51, 100.5, 51 * S))));
    REQUIRE(v.counters().outliers == 0);
}

TEST(feed_staleness_is_about_absence_of_ticks) {
    Config c; c.stale_after_ns = 10 * S;
    Validator v(c);
    REQUIRE(v.is_stale(0));                         // no price at all yet
    v.check(mk(1, 100.0, 100 * S));
    REQUIRE(!v.is_stale(105 * S));                  // 5s old, tolerance 10s
    REQUIRE(v.is_stale(120 * S));                   // 20s old
}

TEST(feed_reject_rate_and_reset) {
    Validator v;
    v.check(mk(1, 100.0, S));
    v.check(mk(1, 100.0, 2 * S));                   // duplicate
    REQUIRE(v.counters().total_seen() == 2);
    CHECK_CLOSE(v.reject_rate(), 0.5, 1e-12);
    v.reset();
    REQUIRE(v.counters().total_seen() == 0);
    REQUIRE(v.samples() == 0);
    REQUIRE(v.is_stale(0));
    REQUIRE(accepted(v.check(mk(7, 100.0, S))));    // any seq is fine when cold
}

int main() {
    std::printf("=== market-data quality gate tests ===\n");
    return microtest::run_all();
}
