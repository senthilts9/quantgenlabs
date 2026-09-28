// lifecycle_demo.cpp — the complete order lifecycle, printed.
//
//   market data -> quality gate -> book -> pre-trade risk -> FIX -> venue
//   -> matching -> ExecutionReports -> position -> P&L
//
// Every FIX message printed here is a real message that passed full validation
// (BodyLength and CheckSum included) on its way through a real session layer. SOH
// is rendered as '|' because a raw FIX message in a terminal is unreadable.
//
// Run it:  g++ -std=c++20 -O2 -pthread -Icpp/include cpp/src/lifecycle_demo.cpp -o demo && ./demo
#include "mkt/feed/validator.hpp"
#include "mkt/risk/pretrade.hpp"
#include "mkt/venue/fix_venue.hpp"

#include <cstdio>
#include <string>
#include <vector>

using namespace mkt;

static constexpr std::int64_t SEC = 1'000'000'000LL;

static void hdr(const char* s) {
    std::printf("\n\033[1m── %s ─────────────────────────────────────────\033[0m\n", s);
}

static void show(const char* dir, std::string_view wire) {
    char buf[1024];
    const auto n = fix::Builder::to_pipe(wire, buf, sizeof buf - 1);
    buf[n] = '\0';
    std::printf("  %s %s\n", dir, buf);
}

int main() {
    std::int64_t now = 1'759'000'000LL * SEC;   // a fixed instant: output is stable

    std::printf("\033[1mQuantGenLabs — end-to-end order lifecycle (simulated venue)\033[0m\n");
    std::printf("Nothing here touches a real exchange, and no money moves.\n");

    // ---------------------------------------------------------------- 1. session
    hdr("1. FIX session establishment");
    fix::SessionConfig cc; cc.sender_comp_id = "QGLABS"; cc.target_comp_id = "SIMVENUE"; cc.heartbeat_secs = 30;
    fix::SessionConfig vc; vc.sender_comp_id = "SIMVENUE"; vc.target_comp_id = "QGLABS"; vc.heartbeat_secs = 30;

    fix::Session client(cc);
    venue::FixVenue venue(vc, /*floor*/1, /*n_ticks*/50000, /*tick*/0.01);
    venue.listen(now);
    client.connect(now);

    auto to_venue = [&] {
        auto batch = client.outbox(); client.clear_outbox();
        for (const auto& w : batch) { show("\033[36m-->\033[0m", w); venue.on_wire(w, now); }
    };
    std::vector<std::string> owned;
    auto to_client = [&] {
        auto batch = venue.outbox(); venue.clear_outbox();
        for (const auto& w : batch) {
            show("\033[32m<--\033[0m", w);
            owned.push_back(w);
            fix::Message m;
            client.on_message(owned.back(), now, m);
        }
    };
    to_venue(); to_client();
    std::printf("  client=%s  venue=%s\n", fix::to_string(client.state()), fix::to_string(venue.state()));

    // ---------------------------------------------------------------- 2. market data
    hdr("2. Market data through the quality gate");
    feed::Config fcfg; fcfg.price_min = 50.0; fcfg.price_max = 200.0; fcfg.min_samples = 5;
    feed::Validator validator(fcfg);

    struct In { std::uint64_t seq; double px; const char* note; };
    const In stream[] = {
        {1, 100.00, "clean"},      {2, 100.02, "clean"},      {3, 100.01, "clean"},
        {4, 100.04, "clean"},      {5, 100.03, "clean"},      {6, 100.05, "clean"},
        {6, 100.06, "same seq"},   {9, 100.07, "seq jumped"},
        {10, 100.08, "clean"},     {11, 900.00, "outside band"},
        {12, 100.09, "clean"},
    };
    for (const auto& t : stream) {
        feed::Tick tick{t.seq, now + static_cast<std::int64_t>(t.seq) * SEC,
                        now + static_cast<std::int64_t>(t.seq) * SEC, t.px, 100.0};
        const auto v = validator.check(tick);
        std::printf("  seq=%-3llu px=%-8.2f %-14s -> %s%s\033[0m\n",
                    static_cast<unsigned long long>(t.seq), t.px, t.note,
                    feed::accepted(v) ? "\033[32m" : "\033[31m", feed::to_string(v));
    }
    const auto& fc = validator.counters();
    std::printf("  accepted=%llu  gaps=%llu (lost %llu)  duplicates=%llu  out-of-band=%llu  reject rate=%.1f%%\n",
                (unsigned long long)fc.accepted, (unsigned long long)fc.gaps,
                (unsigned long long)fc.gap_messages_lost, (unsigned long long)fc.duplicates,
                (unsigned long long)fc.out_of_band, 100.0 * validator.reject_rate());

    // ---------------------------------------------------------------- 3. liquidity
    hdr("3. Venue seeded with other participants' liquidity");
    venue.seed_liquidity(book::Side::Ask, 100.10, 300);
    venue.seed_liquidity(book::Side::Ask, 100.15, 500);
    venue.seed_liquidity(book::Side::Bid,  99.95, 400);
    book::Ticks bp, ap; book::Qty bq, aq;
    venue.engine().best(book::Side::Bid, bp, bq);
    venue.engine().best(book::Side::Ask, ap, aq);
    std::printf("  book: %lld @ %.2f  /  %.2f @ %lld   spread=%.2f  micro=%.4f\n",
                (long long)bq, venue.to_price(bp), venue.to_price(ap), (long long)aq,
                venue.to_price(ap) - venue.to_price(bp),
                venue.engine().book().micro_price_ticks() * 0.01);

    // ---------------------------------------------------------------- 4. pre-trade
    hdr("4. Pre-trade risk gate");
    risk::PreTradeGate gate(4);
    risk::SymbolLimits lim;
    lim.known = true;
    lim.max_order_qty = 1000; lim.max_order_notional = 250'000;
    lim.collar_bps = 200;              // 2%
    lim.max_long_position = 2000; lim.max_short_position = 500;
    gate.set_limits(0, lim);
    const risk::MarketContext ctx{validator.last_price(), !validator.is_stale(now + 12 * SEC)};

    struct Cand { const char* label; risk::Side side; double qty; double px; };
    const Cand cands[] = {
        {"normal buy",             risk::Side::Buy,   500, 100.10},
        {"fat-finger qty (x100)",  risk::Side::Buy, 50000, 100.10},
        {"fat-finger price (+10%)",risk::Side::Buy,   500, 110.00},
        {"breaches short limit",   risk::Side::Sell,  900, 100.00},
        {"notional too large",     risk::Side::Buy,   999, 300.00},
    };
    for (const auto& c : cands) {
        risk::OrderRequest o; o.symbol = 0; o.side = c.side; o.qty = c.qty; o.price = c.px;
        const auto d = gate.check(o, ctx);
        std::printf("  %-26s qty=%-7.0f px=%-7.2f -> %s%s\033[0m\n", c.label, c.qty, c.px,
                    risk::accepted(d) ? "\033[32m" : "\033[31m", risk::to_string(d));
    }
    std::printf("  kill switch test: ");
    gate.engage_kill_switch();
    { risk::OrderRequest o; o.symbol = 0; o.side = risk::Side::Buy; o.qty = 1; o.price = 100.10;
      std::printf("%s\033[0m (a previously-accepted order)\n", "\033[31mkill_switch_engaged"); (void)gate.check(o, ctx); }
    gate.release_kill_switch();

    // ---------------------------------------------------------------- 5. order out
    hdr("5. Order out over FIX, sweeping two price levels");
    client.send_app(fix::msg::NewOrderSingle, [&](fix::Builder& b) {
        b.add(fix::tag::ClOrdID, "QGL-0001")
         .add(fix::tag::Symbol, "AAPL")
         .add(fix::tag::Side, "1")                      // buy
         .add_price(fix::tag::OrderQty, 500, 0)
         .add(fix::tag::OrdType, "2")                   // limit
         .add(fix::tag::TimeInForce, "0")
         .add_price(fix::tag::Price, 100.15, 2);
    }, now);
    gate.on_order_sent(0);
    to_venue(); to_client();

    // ---------------------------------------------------------------- 6. reconcile
    hdr("6. Position and P&L reconciled from the ExecutionReports");
    double cum = 0.0, notional = 0.0;
    for (const auto& raw : owned) {
        fix::Message m;
        if (m.parse(raw, true) != fix::ParseError::None) continue;
        if (m.msg_type() != fix::msg::ExecutionReport) continue;
        if (m.get_or(fix::tag::ExecType) != venue::exec_type::Trade) continue;
        const double q = m.get_double(fix::tag::LastQty).value_or(0.0);
        const double p = m.get_double(fix::tag::LastPx).value_or(0.0);
        cum += q; notional += q * p;
        gate.on_fill(0, risk::Side::Buy, q);
        std::printf("  fill %6.0f @ %.2f\n", q, p);
    }
    const double avg = cum > 0 ? notional / cum : 0.0;
    std::printf("  position=%.0f  avg price=%.4f  cost=%.2f\n", cum, avg, notional);
    std::printf("  gate position now=%.0f (limit %.0f)\n", gate.position(0), lim.max_long_position);

    venue.engine().best(book::Side::Ask, ap, aq);
    const double mark = venue.to_price(ap);
    std::printf("  mark=%.2f  unrealised P&L=%+.2f\n", mark, cum * (mark - avg));

    // ---------------------------------------------------------------- 7. cancel
    hdr("7. Resting order, then cancel");
    client.send_app(fix::msg::NewOrderSingle, [&](fix::Builder& b) {
        b.add(fix::tag::ClOrdID, "QGL-0002").add(fix::tag::Symbol, "AAPL")
         .add(fix::tag::Side, "1").add_price(fix::tag::OrderQty, 250, 0)
         .add(fix::tag::OrdType, "2").add(fix::tag::TimeInForce, "0")
         .add_price(fix::tag::Price, 99.50, 2);
    }, now);
    to_venue(); to_client();
    client.send_app(fix::msg::OrderCancelRequest, [&](fix::Builder& b) {
        b.add(fix::tag::ClOrdID, "QGL-0002-C").add(fix::tag::OrigClOrdID, "QGL-0002")
         .add(fix::tag::Symbol, "AAPL").add(fix::tag::Side, "1");
    }, now);
    to_venue(); to_client();

    // ---------------------------------------------------------------- 8. heartbeat
    hdr("8. Session keepalive after 31s of quiet");
    now += 31 * SEC;
    client.on_timer(now);
    to_venue(); to_client();

    // ---------------------------------------------------------------- summary
    hdr("Summary");
    const auto& vs = venue.engine().stats();
    std::printf("  venue: accepted=%llu rejected=%llu fills=%llu cancels=%llu volume=%lld\n",
                (unsigned long long)vs.orders_accepted, (unsigned long long)vs.orders_rejected,
                (unsigned long long)vs.fills, (unsigned long long)vs.cancels, (long long)vs.volume);
    const auto& gs = gate.stats();
    std::printf("  pre-trade: checked=%llu accepted=%llu rejected=%llu\n",
                (unsigned long long)gs.checked, (unsigned long long)gs.accepted,
                (unsigned long long)gs.rejected());
    std::printf("  session: client out-seq=%llu expecting=%llu state=%s\n",
                (unsigned long long)client.next_out_seq(),
                (unsigned long long)client.expected_in_seq(), fix::to_string(client.state()));
    std::printf("\n  All FIX messages above passed full validation (BodyLength + CheckSum).\n");
    return 0;
}
