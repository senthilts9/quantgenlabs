// test_fix_session.cpp — FIX session-layer tests.
//
// Every test drives an explicit fake clock, so heartbeat, test-request and
// timeout behaviour is asserted at exact intervals with no sleeping. Two
// Sessions are wired to each other in-process for the handshake tests, which is
// the same arrangement the simulated venue uses -- a session that can only be
// tested against a real counterparty cannot be tested in CI.
#include "microtest.hpp"
#include "mkt/fix/session.hpp"

using namespace mkt::fix;

static constexpr std::int64_t SEC = 1'000'000'000LL;

static SessionConfig cfg(const char* me, const char* you, int hb = 30) {
    SessionConfig c;
    c.sender_comp_id = me;
    c.target_comp_id = you;
    c.heartbeat_secs = hb;
    return c;
}

// Delivers everything in `from`'s outbox to `to`, returning how many messages
// `to` classified as application traffic.
static int pump(Session& from, Session& to, std::int64_t now) {
    int app = 0;
    auto batch = from.outbox();      // copy: `to` may enqueue replies into its own
    from.clear_outbox();
    for (const auto& wire : batch) {
        Message m;
        if (to.on_message(wire, now, m) == Inbound::Application) ++app;
    }
    return app;
}

// ------------------------------------------------------------------ handshake
TEST(session_logon_handshake_completes) {
    Session initiator(cfg("CLIENT", "VENUE"));
    Session acceptor(cfg("VENUE", "CLIENT"));
    std::int64_t now = 1'000 * SEC;

    acceptor.listen(now);
    initiator.connect(now);
    REQUIRE(initiator.state() == SessionState::LogonSent);
    REQUIRE(initiator.outbox().size() == 1);

    pump(initiator, acceptor, now);                 // Logon -> acceptor
    REQUIRE(acceptor.state() == SessionState::Active);
    pump(acceptor, initiator, now);                 // Logon echo -> initiator
    REQUIRE(initiator.state() == SessionState::Active);

    REQUIRE(initiator.next_out_seq() == 2);         // one Logon sent
    REQUIRE(acceptor.next_out_seq() == 2);
    REQUIRE(initiator.expected_in_seq() == 2);
    REQUIRE(acceptor.expected_in_seq() == 2);
}

TEST(session_negotiates_heartbeat_from_logon) {
    Session initiator(cfg("CLIENT", "VENUE", 30));
    Session acceptor(cfg("VENUE", "CLIENT", 99));   // will adopt the peer's 30
    std::int64_t now = 0;
    acceptor.listen(now);
    initiator.connect(now);
    pump(initiator, acceptor, now);
    REQUIRE(acceptor.negotiated_heartbeat() == 30);
}

TEST(session_rejects_non_logon_before_logon) {
    Session acceptor(cfg("VENUE", "CLIENT"));
    acceptor.listen(0);
    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::NewOrderSingle).add_int(tag::MsgSeqNum, 1)
     .add(tag::SenderCompID, "CLIENT").add(tag::TargetCompID, "VENUE");
    Message m;
    REQUIRE(acceptor.on_message(b.finish(), 0, m) == Inbound::Rejected);
    REQUIRE(acceptor.state() == SessionState::AwaitingLogon);
}

// ------------------------------------------------------------------ app flow
TEST(session_delivers_application_messages_in_order) {
    Session cl(cfg("CLIENT", "VENUE")), vn(cfg("VENUE", "CLIENT"));
    std::int64_t now = 0;
    vn.listen(now); cl.connect(now);
    pump(cl, vn, now); pump(vn, cl, now);

    for (int i = 1; i <= 3; ++i) {
        const bool ok = cl.send_app(msg::NewOrderSingle, [&](Builder& b) {
            b.add(tag::ClOrdID, "ORD-" + std::to_string(i))
             .add(tag::Symbol, "AAPL").add(tag::Side, "1")
             .add_int(tag::OrderQty, 100 * i).add(tag::OrdType, "1");
        }, now);
        REQUIRE(ok);
    }
    REQUIRE(pump(cl, vn, now) == 3);
    REQUIRE(vn.expected_in_seq() == 5);             // logon + 3 orders
}

TEST(session_refuses_app_send_when_not_active) {
    Session cl(cfg("CLIENT", "VENUE"));
    REQUIRE(!cl.send_app(msg::NewOrderSingle, [](Builder&) {}, 0));
    cl.connect(0);
    REQUIRE(!cl.send_app(msg::NewOrderSingle, [](Builder&) {}, 0));   // LogonSent
}

// ------------------------------------------------------------------ sequencing
// A gap must suspend application processing, not reorder it.
TEST(session_gap_triggers_resend_and_suspends_app) {
    Session vn(cfg("VENUE", "CLIENT"));
    std::int64_t now = 0;
    vn.listen(now);
    { Builder b("FIX.4.4");
      b.add(tag::MsgType, msg::Logon).add(tag::SenderCompID, "CLIENT")
       .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 1).add_int(tag::HeartBtInt, 30);
      Message m; vn.on_message(b.finish(), now, m); }
    vn.clear_outbox();
    REQUIRE(vn.expected_in_seq() == 2);

    // seq 5 arrives when 2 was expected.
    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::NewOrderSingle).add(tag::SenderCompID, "CLIENT")
     .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 5).add(tag::ClOrdID, "X");
    Message m;
    REQUIRE(vn.on_message(b.finish(), now, m) == Inbound::GapDetected);
    REQUIRE(vn.resend_pending());
    REQUIRE(vn.stats().gaps_detected == 1);
    REQUIRE(vn.stats().resend_requests_sent == 1);
    REQUIRE(vn.expected_in_seq() == 2);             // NOT advanced past the gap

    // A ResendRequest was queued asking from seq 2.
    Message rr;
    REQUIRE(rr.parse(vn.outbox().at(0), true) == ParseError::None);
    REQUIRE(rr.msg_type() == msg::ResendRequest);
    REQUIRE(rr.get_int<std::uint64_t>(tag::BeginSeqNo).value() == 2u);
}

// seq < expected with no PossDupFlag is unrecoverable and must force logout.
TEST(session_low_seq_without_possdup_is_fatal) {
    Session vn(cfg("VENUE", "CLIENT"));
    std::int64_t now = 0;
    vn.listen(now);
    { Builder b("FIX.4.4");
      b.add(tag::MsgType, msg::Logon).add(tag::SenderCompID, "CLIENT")
       .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 10).add_int(tag::HeartBtInt, 30);
      Message m; vn.on_message(b.finish(), now, m); }
    REQUIRE(vn.expected_in_seq() == 11);
    vn.clear_outbox();

    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::NewOrderSingle).add(tag::SenderCompID, "CLIENT")
     .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 4).add(tag::ClOrdID, "X");
    Message m;
    REQUIRE(vn.on_message(b.finish(), now, m) == Inbound::Disconnect);
    REQUIRE(vn.state() == SessionState::LoggedOut);
    Message lo;
    REQUIRE(lo.parse(vn.outbox().at(0), true) == ParseError::None);
    REQUIRE(lo.msg_type() == msg::Logout);
    REQUIRE(lo.get_or(tag::Text).find("too low") != std::string_view::npos);
}

// The same low sequence WITH PossDupFlag=Y is a normal resend: ignore quietly.
TEST(session_low_seq_with_possdup_is_ignored) {
    Session vn(cfg("VENUE", "CLIENT"));
    vn.listen(0);
    { Builder b("FIX.4.4");
      b.add(tag::MsgType, msg::Logon).add(tag::SenderCompID, "CLIENT")
       .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 10).add_int(tag::HeartBtInt, 30);
      Message m; vn.on_message(b.finish(), 0, m); }
    vn.clear_outbox();

    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::ExecutionReport).add(tag::SenderCompID, "CLIENT")
     .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 4).add(tag::PossDupFlag, "Y");
    Message m;
    REQUIRE(vn.on_message(b.finish(), 0, m) == Inbound::Ignored);
    REQUIRE(vn.state() == SessionState::Active);
    REQUIRE(vn.stats().duplicates_ignored == 1);
    REQUIRE(vn.outbox().empty());
}

// SequenceReset-Reset must bypass the very check it exists to repair.
TEST(session_sequence_reset_bypasses_seq_check) {
    Session vn(cfg("VENUE", "CLIENT"));
    vn.listen(0);
    { Builder b("FIX.4.4");
      b.add(tag::MsgType, msg::Logon).add(tag::SenderCompID, "CLIENT")
       .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 50).add_int(tag::HeartBtInt, 30);
      Message m; vn.on_message(b.finish(), 0, m); }
    REQUIRE(vn.expected_in_seq() == 51);

    Builder b("FIX.4.4");                            // low seq, but a reset
    b.add(tag::MsgType, msg::SequenceReset).add(tag::SenderCompID, "CLIENT")
     .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 1).add_int(tag::NewSeqNo, 1);
    Message m;
    REQUIRE(vn.on_message(b.finish(), 0, m) == Inbound::Ignored);
    REQUIRE(vn.state() == SessionState::Active);     // not a fatal low-seq
    REQUIRE(vn.expected_in_seq() == 1);
}

// ------------------------------------------------------------------ resend
TEST(session_resends_with_possdup_and_valid_framing) {
    Session cl(cfg("CLIENT", "VENUE")), vn(cfg("VENUE", "CLIENT"));
    std::int64_t now = 0;
    vn.listen(now); cl.connect(now);
    pump(cl, vn, now); pump(vn, cl, now);

    for (int i = 1; i <= 3; ++i)
        cl.send_app(msg::NewOrderSingle, [&](Builder& b) {
            b.add(tag::ClOrdID, "ORD-" + std::to_string(i)).add(tag::Symbol, "MSFT")
             .add_int(tag::OrderQty, 10 * i);
        }, now);
    cl.clear_outbox();                               // pretend the wire dropped them

    // Venue asks for 2..4 (the three orders).
    Builder rr("FIX.4.4");
    rr.add(tag::MsgType, msg::ResendRequest).add(tag::SenderCompID, "VENUE")
      .add(tag::TargetCompID, "CLIENT").add_int(tag::MsgSeqNum, 2)
      .add_int(tag::BeginSeqNo, 2).add_int(tag::EndSeqNo, 4);
    Message m;
    REQUIRE(cl.on_message(rr.finish(), now, m) == Inbound::Ignored);
    REQUIRE(cl.stats().messages_resent == 3);
    REQUIRE(cl.outbox().size() == 3);

    // Each replay must be independently valid: PossDupFlag changes the body, so
    // BodyLength and CheckSum must both have been recomputed.
    std::uint64_t expect_seq = 2;
    for (const auto& wire : cl.outbox()) {
        Message r;
        REQUIRE(r.parse(wire, /*validate=*/true) == ParseError::None);
        REQUIRE(r.is_poss_dup());
        REQUIRE(r.seq_num().value() == expect_seq++);
        REQUIRE(r.msg_type() == msg::NewOrderSingle);
        REQUIRE(r.has(tag::OrigSendingTime));
        REQUIRE(r.get_or(tag::Symbol) == "MSFT");    // business fields preserved
    }
}

TEST(session_resend_to_zero_means_everything_onward) {
    Session cl(cfg("CLIENT", "VENUE")), vn(cfg("VENUE", "CLIENT"));
    vn.listen(0); cl.connect(0);
    pump(cl, vn, 0); pump(vn, cl, 0);
    cl.send_app(msg::NewOrderSingle, [](Builder& b) { b.add(tag::ClOrdID, "A"); }, 0);
    cl.send_app(msg::NewOrderSingle, [](Builder& b) { b.add(tag::ClOrdID, "B"); }, 0);
    cl.clear_outbox();

    Builder rr("FIX.4.4");
    rr.add(tag::MsgType, msg::ResendRequest).add(tag::SenderCompID, "VENUE")
      .add(tag::TargetCompID, "CLIENT").add_int(tag::MsgSeqNum, 2)
      .add_int(tag::BeginSeqNo, 1).add_int(tag::EndSeqNo, 0);   // 0 = infinity
    Message m;
    cl.on_message(rr.finish(), 0, m);
    REQUIRE(cl.stats().messages_resent == 3);        // logon + 2 orders
}

// ------------------------------------------------------------------ timers
TEST(session_emits_heartbeat_after_quiet_interval) {
    Session cl(cfg("CLIENT", "VENUE", 30)), vn(cfg("VENUE", "CLIENT", 30));
    std::int64_t now = 0;
    vn.listen(now); cl.connect(now);
    pump(cl, vn, now); pump(vn, cl, now);
    cl.clear_outbox();

    cl.on_timer(now + 10 * SEC);                     // quiet 10s of 30
    REQUIRE(cl.outbox().empty());

    cl.on_timer(now + 31 * SEC);                     // past the interval
    REQUIRE(cl.outbox().size() == 1);
    Message hb;
    REQUIRE(hb.parse(cl.outbox().at(0), true) == ParseError::None);
    REQUIRE(hb.msg_type() == msg::Heartbeat);
    REQUIRE(cl.stats().heartbeats_sent == 1);
}

TEST(session_test_request_then_timeout_disconnect) {
    Session cl(cfg("CLIENT", "VENUE", 10));
    Session vn(cfg("VENUE", "CLIENT", 10));
    std::int64_t now = 0;
    vn.listen(now); cl.connect(now);
    pump(cl, vn, now); pump(vn, cl, now);
    cl.clear_outbox();

    // Counterparty silent for 12s > 10 * 1.2 -> prod it.
    REQUIRE(cl.on_timer(now + 13 * SEC));
    REQUIRE(cl.awaiting_test_reply());
    REQUIRE(cl.stats().test_requests_sent == 1);
    bool found_tr = false;
    for (const auto& w : cl.outbox()) { Message m; m.parse(w, true); if (m.msg_type() == msg::TestRequest) found_tr = true; }
    REQUIRE(found_tr);

    // Still silent past 2 * 12s -> declare the connection dead.
    REQUIRE(!cl.on_timer(now + 25 * SEC));
    REQUIRE(cl.state() == SessionState::LoggedOut);
}

TEST(session_test_request_is_answered_with_matching_id) {
    Session vn(cfg("VENUE", "CLIENT"));
    vn.listen(0);
    { Builder b("FIX.4.4");
      b.add(tag::MsgType, msg::Logon).add(tag::SenderCompID, "CLIENT")
       .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 1).add_int(tag::HeartBtInt, 30);
      Message m; vn.on_message(b.finish(), 0, m); }
    vn.clear_outbox();

    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::TestRequest).add(tag::SenderCompID, "CLIENT")
     .add(tag::TargetCompID, "VENUE").add_int(tag::MsgSeqNum, 2).add(tag::TestReqID, "PING-77");
    Message m;
    REQUIRE(vn.on_message(b.finish(), 0, m) == Inbound::Ignored);
    Message hb;
    REQUIRE(hb.parse(vn.outbox().at(0), true) == ParseError::None);
    REQUIRE(hb.msg_type() == msg::Heartbeat);
    REQUIRE(hb.get_or(tag::TestReqID) == "PING-77");   // must echo the id
}

TEST(session_heartbeat_reply_clears_awaiting_flag) {
    Session cl(cfg("CLIENT", "VENUE", 10)), vn(cfg("VENUE", "CLIENT", 10));
    std::int64_t now = 0;
    vn.listen(now); cl.connect(now);
    pump(cl, vn, now); pump(vn, cl, now);
    cl.on_timer(now + 13 * SEC);
    REQUIRE(cl.awaiting_test_reply());
    pump(cl, vn, now + 13 * SEC);                    // venue answers the TestRequest
    pump(vn, cl, now + 13 * SEC);
    REQUIRE(!cl.awaiting_test_reply());
}

// ------------------------------------------------------------------ teardown
TEST(session_logout_is_acknowledged_both_ways) {
    Session cl(cfg("CLIENT", "VENUE")), vn(cfg("VENUE", "CLIENT"));
    std::int64_t now = 0;
    vn.listen(now); cl.connect(now);
    pump(cl, vn, now); pump(vn, cl, now);
    cl.clear_outbox(); vn.clear_outbox();

    cl.logout("done for the day", now);
    REQUIRE(cl.state() == SessionState::LogoutSent);
    Message m;
    const auto batch = cl.outbox();
    REQUIRE(vn.on_message(batch.at(0), now, m) == Inbound::Disconnect);
    REQUIRE(vn.state() == SessionState::LoggedOut);
}

TEST(session_malformed_message_counted_not_crashed) {
    Session vn(cfg("VENUE", "CLIENT"));
    vn.listen(0);
    Message m;
    REQUIRE(vn.on_message("garbage-not-fix", 0, m) == Inbound::Rejected);
    REQUIRE(vn.on_message("", 0, m) == Inbound::Rejected);
    REQUIRE(vn.malformed() == 2);
    REQUIRE(vn.state() == SessionState::AwaitingLogon);
}

TEST(session_reset_sequences_clears_store) {
    Session cl(cfg("CLIENT", "VENUE"));
    cl.connect(0);
    REQUIRE(cl.store_size() == 1);
    cl.reset_sequences();
    REQUIRE(cl.next_out_seq() == 1);
    REQUIRE(cl.expected_in_seq() == 1);
    REQUIRE(cl.store_size() == 0);
}

int main() {
    std::printf("=== FIX session-layer tests ===\n");
    return microtest::run_all();
}
