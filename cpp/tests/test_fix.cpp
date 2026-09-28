// test_fix.cpp — FIX wire-format tests.
//
// The important test here is `fix_parses_reference_logon`: a round-trip against
// our own Builder would prove only that the parser and builder share a bug. That
// case uses the canonical Logon example from the FIX 4.2 documentation, whose
// BodyLength (65) and CheckSum (062) were computed by someone else. If our
// arithmetic disagrees with it, ours is wrong.
#include "microtest.hpp"
#include "mkt/fix/message.hpp"

#include <string>

using namespace mkt::fix;

// Builds a FIX string from a '|'-delimited literal, so test data stays readable.
static std::string fx(std::string_view pipe) {
    std::string s(pipe);
    for (char& c : s) if (c == '|') c = SOH;
    return s;
}

// ---------------------------------------------------------------- ground truth
TEST(fix_parses_reference_logon) {
    // Canonical FIX 4.2 Logon example. BodyLength and CheckSum are external
    // ground truth -- not produced by this codebase.
    const std::string raw = fx("8=FIX.4.2|9=65|35=A|49=SERVER|56=CLIENT|34=177|"
                               "52=20090107-18:15:16|98=0|108=30|10=062|");
    Message m;
    const auto e = m.parse(raw, /*validate=*/true);
    REQUIRE(e == ParseError::None);
    REQUIRE(m.ok());
    REQUIRE(m.msg_type() == msg::Logon);
    REQUIRE(m.get_or(tag::SenderCompID) == "SERVER");
    REQUIRE(m.get_or(tag::TargetCompID) == "CLIENT");
    REQUIRE(m.seq_num().value() == 177u);
    REQUIRE(m.get_int<int>(tag::HeartBtInt).value() == 30);
    REQUIRE(m.field_count() == 10);   // 8,9,35,49,56,34,52,98,108,10
}

// A one-byte corruption must be caught by the checksum, not silently accepted.
TEST(fix_detects_corruption_via_checksum) {
    std::string raw = fx("8=FIX.4.2|9=65|35=A|49=SERVER|56=CLIENT|34=177|"
                         "52=20090107-18:15:16|98=0|108=30|10=062|");
    raw[raw.find("SERVER")] = 'X';              // same length, so BodyLength still fits
    Message m;
    REQUIRE(m.parse(raw, true) == ParseError::CheckSumMismatch);
    // With validation off the same bytes parse: framing is trusted, so this is
    // the replay-a-trusted-log path.
    REQUIRE(m.parse(raw, false) == ParseError::None);
    REQUIRE(m.get_or(tag::SenderCompID) == "XERVER");
}

TEST(fix_detects_wrong_body_length) {
    const std::string raw = fx("8=FIX.4.2|9=64|35=A|49=SERVER|56=CLIENT|34=177|"
                               "52=20090107-18:15:16|98=0|108=30|10=062|");
    Message m;
    REQUIRE(m.parse(raw, true) == ParseError::BodyLengthMismatch);
}

// ---------------------------------------------------------------- structure
TEST(fix_enforces_mandatory_field_order) {
    Message m;
    // 9 before 8
    REQUIRE(m.parse(fx("9=12|8=FIX.4.4|35=A|10=000|"), false) == ParseError::MissingBeginString);
    // 35 before 9
    REQUIRE(m.parse(fx("8=FIX.4.4|35=A|9=12|10=000|"), false) == ParseError::MissingBodyLength);
    // no 35 at all
    REQUIRE(m.parse(fx("8=FIX.4.4|9=12|49=A|10=000|"), false) == ParseError::MissingMsgType);
    // no checksum
    REQUIRE(m.parse(fx("8=FIX.4.4|9=12|35=A|49=X|"), false) == ParseError::MissingCheckSum);
}

TEST(fix_rejects_malformed_fields) {
    Message m;
    REQUIRE(m.parse("", false) == ParseError::Empty);
    REQUIRE(m.parse(fx("8=FIX.4.4|9=5|35=A|10=000"), false) == ParseError::NoSoh);  // no final SOH
    REQUIRE(m.parse(fx("8=FIX.4.4|9=5|XX=A|10=000|"), false) == ParseError::BadTag);
    REQUIRE(m.parse(fx("8=FIX.4.4|9=5|0=A|10=000|"), false) == ParseError::BadTag); // tag 0 invalid
    REQUIRE(m.parse("8=FIX.4.4", false) == ParseError::NoSoh);
}

// ---------------------------------------------------------------- accessors
TEST(fix_typed_accessors_distinguish_missing_from_zero) {
    const std::string raw = fx("8=FIX.4.4|9=10|35=D|38=0|44=1.25|10=000|");
    Message m;
    m.parse(raw, false);
    REQUIRE(m.get_int<int>(tag::OrderQty).has_value());
    REQUIRE(m.get_int<int>(tag::OrderQty).value() == 0);        // present AND zero
    REQUIRE(!m.get_int<int>(tag::LastQty).has_value());         // absent
    CHECK_CLOSE(m.get_double(tag::Price).value(), 1.25, 1e-12);
    REQUIRE(!m.get_double(tag::StopPx).has_value());
    REQUIRE(m.has(tag::Price));
    REQUIRE(!m.has(tag::StopPx));
}

TEST(fix_rejects_non_numeric_in_typed_accessors) {
    const std::string raw = fx("8=FIX.4.4|9=10|35=D|38=12x|44=1.2.3|59=|10=000|");
    Message m;
    m.parse(raw, false);
    REQUIRE(!m.get_int<int>(tag::OrderQty).has_value());   // trailing garbage
    REQUIRE(!m.get_double(tag::Price).has_value());        // two decimal points
    REQUIRE(!m.get_int<int>(tag::TimeInForce).has_value());// empty value
}

TEST(fix_decimal_parsing_edge_cases) {
    CHECK_CLOSE(Message::parse_decimal("0").value(), 0.0, 1e-12);
    CHECK_CLOSE(Message::parse_decimal("-1.5").value(), -1.5, 1e-12);
    CHECK_CLOSE(Message::parse_decimal("+2.25").value(), 2.25, 1e-12);
    CHECK_CLOSE(Message::parse_decimal("100.").value(), 100.0, 1e-12);
    CHECK_CLOSE(Message::parse_decimal(".5").value(), 0.5, 1e-12);
    REQUIRE(!Message::parse_decimal("abc").has_value());
    REQUIRE(!Message::parse_decimal("1e5").has_value());   // FIX is plain decimal
    REQUIRE(!Message::parse_decimal("-").has_value());
}

// ---------------------------------------------------------------- builder
// The builder's output must satisfy the parser's full validation, which means
// its BodyLength and CheckSum arithmetic has to be right, not merely
// self-consistent -- the reference-message test above pins the parser to spec.
TEST(fix_builder_output_passes_full_validation) {
    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::NewOrderSingle)
     .add(tag::SenderCompID, "QGLABS")
     .add(tag::TargetCompID, "SIMVENUE")
     .add_int(tag::MsgSeqNum, 42)
     .add(tag::SendingTime, "20260928-09:30:00.000")
     .add(tag::ClOrdID, "ORD-1")
     .add(tag::Symbol, "AAPL")
     .add(tag::Side, "1")
     .add_int(tag::OrderQty, 500)
     .add(tag::OrdType, "2")
     .add_price(tag::Price, 187.25, 2);
    const auto wire = b.finish();
    REQUIRE(!b.overflowed());
    REQUIRE(!wire.empty());

    Message m;
    REQUIRE(m.parse(wire, /*validate=*/true) == ParseError::None);
    REQUIRE(m.msg_type() == msg::NewOrderSingle);
    REQUIRE(m.seq_num().value() == 42u);
    REQUIRE(m.get_or(tag::Symbol) == "AAPL");
    REQUIRE(m.get_int<int>(tag::OrderQty).value() == 500);
    CHECK_CLOSE(m.get_double(tag::Price).value(), 187.25, 1e-12);
    REQUIRE(m.get_or(tag::Price) == "187.25");      // exact wire text, 2dp
}

TEST(fix_builder_starts_with_8_and_9_and_ends_with_10) {
    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::Heartbeat).add_int(tag::MsgSeqNum, 1);
    const auto wire = b.finish();
    REQUIRE(wire.substr(0, 9) == "8=FIX.4.4");
    REQUIRE(wire[9] == SOH);
    REQUIRE(wire.substr(10, 2) == "9=");
    REQUIRE(wire.size() >= 7);
    const auto tail = wire.substr(wire.size() - 7);
    REQUIRE(tail.size() == 7);                      // 10=NNN<SOH> is exactly 7 bytes
    REQUIRE(tail.substr(0, 3) == "10=");            // checksum is the last field
    REQUIRE(tail[6] == SOH);                        // and terminated
}

// Checksum must always be three digits. A small sum is the case that catches a
// naive integer-to-string conversion emitting "7" instead of "007".
TEST(fix_checksum_is_always_three_digits) {
    for (int qty = 1; qty <= 300; ++qty) {
        Builder b("FIX.4.4");
        b.add(tag::MsgType, msg::NewOrderSingle).add_int(tag::OrderQty, qty);
        const auto wire = b.finish();
        Message m;
        REQUIRE(m.parse(wire, true) == ParseError::None);
        const auto cs = m.get_or(tag::CheckSum);
        REQUIRE(cs.size() == 3);
        for (char c : cs) REQUIRE(c >= '0' && c <= '9');
    }
}

TEST(fix_price_formatting_respects_precision) {
    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::NewOrderSingle)
     .add_price(tag::Price, 1.1, 2)
     .add_price(tag::StopPx, 0.5, 5)
     .add_price(tag::LastPx, -2.345, 2)
     .add_price(tag::AvgPx, 100.0, 0);
    Message m;
    REQUIRE(m.parse(b.finish(), true) == ParseError::None);
    REQUIRE(m.get_or(tag::Price)  == "1.10");       // padded, not "1.1"
    REQUIRE(m.get_or(tag::StopPx) == "0.50000");
    REQUIRE(m.get_or(tag::LastPx) == "-2.35");      // rounded half away from zero
    REQUIRE(m.get_or(tag::AvgPx)  == "100");        // 0dp emits no point
}

TEST(fix_builder_reset_reuses_buffer) {
    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::Heartbeat);
    const std::string first(b.finish());
    b.reset();
    b.add(tag::MsgType, msg::Logout).add(tag::Text, "done");
    const auto second = b.finish();
    REQUIRE(first != std::string(second));
    Message m;
    REQUIRE(m.parse(second, true) == ParseError::None);
    REQUIRE(m.msg_type() == msg::Logout);
    REQUIRE(m.get_or(tag::Text) == "done");
}

TEST(fix_poss_dup_detected) {
    Builder b("FIX.4.4");
    b.add(tag::MsgType, msg::ExecutionReport).add_int(tag::MsgSeqNum, 9).add(tag::PossDupFlag, "Y");
    Message m;
    m.parse(b.finish(), true);
    REQUIRE(m.is_poss_dup());
    Builder c("FIX.4.4");
    c.add(tag::MsgType, msg::ExecutionReport).add_int(tag::MsgSeqNum, 10);
    Message m2;
    m2.parse(c.finish(), true);
    REQUIRE(!m2.is_poss_dup());
}

int main() {
    std::printf("=== FIX wire-format tests ===\n");
    return microtest::run_all();
}
