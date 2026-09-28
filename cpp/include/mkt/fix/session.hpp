// session.hpp — FIX 4.4 session layer: the state machine above the wire format.
//
// The session layer is what makes FIX reliable on an unreliable connection, and
// nearly all of its complexity is sequence-number handling. The rules are
// specific and the failure modes are asymmetric, so they are spelled out rather
// than inferred:
//
//   seq == expected          Normal. Process, advance.
//   seq >  expected          A message was lost. Send a ResendRequest and STOP
//                            processing application messages until the gap is
//                            filled. Do NOT process the newer message first --
//                            an ExecutionReport applied out of order corrupts
//                            position.
//   seq <  expected, no 43   FATAL. The counterparty's sequence went backwards
//                            without claiming a duplicate, which means one side's
//                            persisted state is wrong. The spec requires logout
//                            and a manual reset; silently accepting it is how two
//                            engines end up permanently disagreeing about what
//                            was sent.
//   seq <  expected, 43=Y    A resent duplicate we already processed. Ignore it
//                            quietly -- this is normal during recovery.
//
// TIME IS INJECTED, never read from a clock inside this class. Every entry point
// takes `now_ns`. This is what makes heartbeat, test-request and timeout logic
// testable deterministically instead of with sleeps: the tests below advance a
// fake clock by exact intervals and assert on what was emitted. A session that
// calls std::chrono::now() internally can only be tested by waiting.
//
// ALLOCATION POLICY, stated rather than implied. Session-level traffic is
// low-rate: one Logon, a heartbeat every 30s, the occasional TestRequest. Those
// use std::string in an outbox, and optimising them would be optimising nothing.
// The message store retains sent messages because RESEND REQUIRES IT -- that is
// inherent to the protocol, not an oversight, and a production deployment
// persists it to disk so it survives a restart. Application messages on the
// inbound path are handed out by reference into the caller's buffer with no copy,
// which is where the volume actually is.
//
// Transport-agnostic by construction: this class never touches a socket. It
// consumes complete messages and produces complete messages in `outbox()`, which
// the caller drains and writes. That is what lets the same state machine drive a
// TCP session, a replay of a log file, and an in-process simulated venue.
#pragma once

#include "mkt/fix/message.hpp"

#include <cstdio>
#include <functional>
#include <string>
#include <vector>

namespace mkt::fix {

enum class SessionState : std::uint8_t {
    Disconnected = 0,
    LogonSent,        // initiator has sent Logon, awaiting the echo
    AwaitingLogon,    // acceptor is listening for a Logon
    Active,
    LogoutSent,
    LoggedOut,
};

constexpr const char* to_string(SessionState s) noexcept {
    switch (s) {
        case SessionState::Disconnected:  return "disconnected";
        case SessionState::LogonSent:     return "logon_sent";
        case SessionState::AwaitingLogon: return "awaiting_logon";
        case SessionState::Active:        return "active";
        case SessionState::LogoutSent:    return "logout_sent";
        case SessionState::LoggedOut:     return "logged_out";
    }
    return "unknown";
}

// What the caller should do with an inbound message.
enum class Inbound : std::uint8_t {
    Ignored = 0,      // admin traffic handled internally, or a benign duplicate
    Application,      // a business message; `out` is populated
    Rejected,         // malformed or failed a session check; a Reject was queued
    GapDetected,      // ResendRequest queued; app processing suspended
    Disconnect,       // session is over; caller must close the connection
};

constexpr const char* to_string(Inbound i) noexcept {
    switch (i) {
        case Inbound::Ignored:     return "ignored";
        case Inbound::Application: return "application";
        case Inbound::Rejected:    return "rejected";
        case Inbound::GapDetected: return "gap_detected";
        case Inbound::Disconnect:  return "disconnect";
    }
    return "unknown";
}

struct SessionConfig {
    std::string sender_comp_id;
    std::string target_comp_id;
    std::string begin_string = "FIX.4.4";
    int  heartbeat_secs = 30;
    // On Logon, ask the counterparty to reset sequence numbers to 1. Convenient
    // for a test venue; in production this discards the audit continuity that
    // sequence numbers provide, so it defaults off.
    bool reset_seq_num_on_logon = false;
    // Grace multiplier before a silent counterparty is declared dead. The spec
    // suggests ~20% slack for transmission time; we test at HeartBtInt * 1.2 and
    // disconnect at HeartBtInt * 2.4.
    double test_request_factor = 1.2;
};

struct SessionStats {
    std::uint64_t sent = 0, received = 0;
    std::uint64_t heartbeats_sent = 0, test_requests_sent = 0;
    std::uint64_t resend_requests_sent = 0, messages_resent = 0;
    std::uint64_t rejects_sent = 0, gaps_detected = 0;
    std::uint64_t duplicates_ignored = 0;
};

class Session {
  public:
    explicit Session(SessionConfig cfg) : cfg_(std::move(cfg)), builder_(cfg_.begin_string) {}

    // ---- lifecycle ---------------------------------------------------------
    // Initiator side: queue a Logon.
    void connect(std::int64_t now_ns) {
        state_ = SessionState::LogonSent;
        last_rx_ns_ = now_ns;
        send_logon(now_ns);
    }

    // Acceptor side: wait for the counterparty's Logon.
    void listen(std::int64_t now_ns) {
        state_ = SessionState::AwaitingLogon;
        last_rx_ns_ = now_ns;
    }

    void logout(std::string_view text, std::int64_t now_ns) {
        if (state_ == SessionState::LoggedOut || state_ == SessionState::Disconnected) return;
        builder_.reset();
        begin_admin(msg::Logout, now_ns);
        if (!text.empty()) builder_.add(tag::Text, text);
        emit(now_ns);
        state_ = SessionState::LogoutSent;
    }

    // ---- inbound -----------------------------------------------------------
    // `raw` must be one complete FIX message. `out` receives the parsed message
    // when the return value is Application; its views point into `raw`, so `raw`
    // must outlive the caller's use of `out`.
    Inbound on_message(std::string_view raw, std::int64_t now_ns, Message& out) {
        ++stats_.received;
        last_rx_ns_ = now_ns;

        if (out.parse(raw, /*validate=*/true) != ParseError::None) {
            // A message that fails framing has no usable MsgSeqNum, so it cannot
            // be Reject'ed against a RefSeqNum. Count it and drop it; the
            // transport, not the session, is broken.
            ++malformed_;
            return Inbound::Rejected;
        }

        const std::string_view mt = out.msg_type();
        const auto seq_opt = out.seq_num();
        if (!seq_opt) {
            send_reject(0, mt, tag::MsgSeqNum, "missing MsgSeqNum", now_ns);
            return Inbound::Rejected;
        }
        const std::uint64_t seq = *seq_opt;

        // A Logon is the only message allowed before the session is active.
        if (state_ == SessionState::AwaitingLogon) {
            if (mt != msg::Logon) return Inbound::Rejected;
            negotiate_heartbeat(out);
            if (out.get_or(tag::Text) == "ResetSeqNumFlag" || out.get_or(141) == "Y") reset_sequences();
            expected_in_ = seq;                      // adopt the peer's numbering
            state_ = SessionState::Active;
            send_logon(now_ns);                      // echo
            advance_expected(seq);
            return Inbound::Ignored;
        }

        // --- SequenceReset-Reset is exempt from sequence checking ------------
        // It exists precisely to fix a broken sequence, so validating it against
        // the sequence it is repairing would make recovery impossible.
        if (mt == msg::SequenceReset && out.get_or(tag::GapFillFlag) != "Y") {
            if (const auto ns = out.get_int<std::uint64_t>(tag::NewSeqNo)) expected_in_ = *ns;
            return Inbound::Ignored;
        }

        // --- sequence checks -------------------------------------------------
        if (seq < expected_in_) {
            if (out.is_poss_dup()) {                 // expected during recovery
                ++stats_.duplicates_ignored;
                return Inbound::Ignored;
            }
            // Sequence went backwards without a duplicate claim: unrecoverable.
            logout("MsgSeqNum too low, expecting " + std::to_string(expected_in_)
                   + " but received " + std::to_string(seq), now_ns);
            state_ = SessionState::LoggedOut;
            return Inbound::Disconnect;
        }

        if (seq > expected_in_) {
            // Gap. Request the missing range and suspend application processing:
            // acting on a later message before an earlier one is how positions
            // get corrupted.
            if (!resend_pending_) {
                send_resend_request(expected_in_, 0, now_ns);   // 0 = infinity
                resend_pending_ = true;
                ++stats_.gaps_detected;
            }
            return Inbound::GapDetected;
        }

        // --- in sequence -----------------------------------------------------
        advance_expected(seq);
        resend_pending_ = false;

        if (mt == msg::Logon) {
            negotiate_heartbeat(out);
            state_ = SessionState::Active;
            return Inbound::Ignored;
        }
        if (mt == msg::Heartbeat) { awaiting_test_reply_ = false; return Inbound::Ignored; }
        if (mt == msg::TestRequest) {
            builder_.reset();
            begin_admin(msg::Heartbeat, now_ns);
            builder_.add(tag::TestReqID, out.get_or(tag::TestReqID));
            emit(now_ns);
            ++stats_.heartbeats_sent;
            return Inbound::Ignored;
        }
        if (mt == msg::ResendRequest) { handle_resend_request(out, now_ns); return Inbound::Ignored; }
        if (mt == msg::Reject)  { ++rejects_received_; return Inbound::Ignored; }
        if (mt == msg::Logout) {
            if (state_ != SessionState::LogoutSent) logout("responding to logout", now_ns);
            state_ = SessionState::LoggedOut;
            return Inbound::Disconnect;
        }
        if (mt == msg::SequenceReset) return Inbound::Ignored;   // gap-fill variant

        return Inbound::Application;
    }

    // ---- timers ------------------------------------------------------------
    // Call regularly. Emits a Heartbeat when we have been quiet, a TestRequest
    // when the counterparty has been quiet, and signals disconnect when it stays
    // quiet after being prodded.
    bool on_timer(std::int64_t now_ns) {
        if (state_ != SessionState::Active) return true;
        const std::int64_t hb = static_cast<std::int64_t>(cfg_.heartbeat_secs) * 1'000'000'000LL;
        if (hb <= 0) return true;

        if (now_ns - last_tx_ns_ >= hb) {
            builder_.reset();
            begin_admin(msg::Heartbeat, now_ns);
            emit(now_ns);
            ++stats_.heartbeats_sent;
        }

        const std::int64_t quiet = now_ns - last_rx_ns_;
        const std::int64_t test_at = static_cast<std::int64_t>(static_cast<double>(hb) * cfg_.test_request_factor);
        if (quiet >= 2 * test_at) {
            // Prodded and still silent: the connection is dead even if the socket
            // says otherwise. This is the case a TCP keepalive will not catch.
            logout("test request timeout", now_ns);
            state_ = SessionState::LoggedOut;
            return false;
        }
        if (quiet >= test_at && !awaiting_test_reply_) {
            builder_.reset();
            begin_admin(msg::TestRequest, now_ns);
            builder_.add(tag::TestReqID, "TR-" + std::to_string(++test_req_id_));
            emit(now_ns);
            awaiting_test_reply_ = true;
            ++stats_.test_requests_sent;
        }
        return true;
    }

    // ---- outbound application messages -------------------------------------
    // `fill` adds the business fields; the session owns the header and trailer,
    // so a caller cannot get the sequence number or checksum wrong.
    bool send_app(std::string_view msg_type,
                  const std::function<void(Builder&)>& fill,
                  std::int64_t now_ns) {
        if (state_ != SessionState::Active) return false;
        builder_.reset();
        begin_admin(msg_type, now_ns);
        fill(builder_);
        emit(now_ns);
        return true;
    }

    // ---- accessors ---------------------------------------------------------
    std::vector<std::string>& outbox() noexcept { return outbox_; }
    const std::vector<std::string>& outbox() const noexcept { return outbox_; }
    void clear_outbox() noexcept { outbox_.clear(); }

    SessionState state() const noexcept { return state_; }
    std::uint64_t next_out_seq() const noexcept { return out_seq_; }
    std::uint64_t expected_in_seq() const noexcept { return expected_in_; }
    bool resend_pending() const noexcept { return resend_pending_; }
    bool awaiting_test_reply() const noexcept { return awaiting_test_reply_; }
    int negotiated_heartbeat() const noexcept { return cfg_.heartbeat_secs; }
    const SessionStats& stats() const noexcept { return stats_; }
    std::uint64_t malformed() const noexcept { return malformed_; }
    std::uint64_t rejects_received() const noexcept { return rejects_received_; }
    std::size_t store_size() const noexcept { return store_.size(); }

    void reset_sequences() noexcept {
        out_seq_ = 1; expected_in_ = 1; store_.clear(); resend_pending_ = false;
    }

  private:
    void begin_admin(std::string_view mt, std::int64_t now_ns) {
        builder_.add(tag::MsgType, mt)
                .add(tag::SenderCompID, cfg_.sender_comp_id)
                .add(tag::TargetCompID, cfg_.target_comp_id)
                .add_int(tag::MsgSeqNum, out_seq_)
                .add(tag::SendingTime, utc_timestamp(now_ns));
    }

    // Takes the send time explicitly. An earlier version read it from a member
    // that only some call sites assigned, which left last_tx_ns_ stale and made
    // heartbeat scheduling drift.
    void emit(std::int64_t now_ns) {
        const auto wire = builder_.finish();
        if (wire.empty()) return;                    // overflowed; drop loudly upstream
        outbox_.emplace_back(wire);
        store_.emplace_back(wire);                   // retained for resend
        ++out_seq_;
        ++stats_.sent;
        last_tx_ns_ = now_ns;
        builder_.reset();
    }

    void send_logon(std::int64_t now_ns) {
        builder_.reset();
        begin_admin(msg::Logon, now_ns);
        builder_.add_int(tag::EncryptMethod, 0)
                .add_int(tag::HeartBtInt, cfg_.heartbeat_secs);
        if (cfg_.reset_seq_num_on_logon) builder_.add(141, "Y");   // ResetSeqNumFlag
        emit(now_ns);
    }

    void send_resend_request(std::uint64_t from, std::uint64_t to, std::int64_t now_ns) {
        builder_.reset();
        begin_admin(msg::ResendRequest, now_ns);
        builder_.add_int(tag::BeginSeqNo, from).add_int(tag::EndSeqNo, to);
        emit(now_ns);
        ++stats_.resend_requests_sent;
    }

    void send_reject(std::uint64_t ref_seq, std::string_view ref_type,
                     Tag ref_tag, std::string_view text, std::int64_t now_ns) {
        builder_.reset();
        begin_admin(msg::Reject, now_ns);
        builder_.add_int(tag::RefSeqNum, ref_seq)
                .add_int(tag::RefTagID, ref_tag)
                .add(tag::RefMsgType, ref_type)
                .add(tag::Text, text);
        emit(now_ns);
        ++stats_.rejects_sent;
    }

    // Replays stored messages in the requested range, marked PossDupFlag=Y so
    // the counterparty can tell a replay from a new message. EndSeqNo 0 means
    // "everything from BeginSeqNo onward".
    void handle_resend_request(const Message& m, std::int64_t now_ns) {
        const auto from = m.get_int<std::uint64_t>(tag::BeginSeqNo).value_or(1);
        const auto to   = m.get_int<std::uint64_t>(tag::EndSeqNo).value_or(0);
        const std::uint64_t hi = (to == 0) ? out_seq_ - 1 : to;
        for (std::uint64_t s = from; s <= hi && s >= 1; ++s) {
            const std::size_t idx = static_cast<std::size_t>(s - 1);
            if (idx >= store_.size()) break;
            std::string replay = store_[idx];
            // Re-emitting requires rebuilding: PossDupFlag changes the body, so
            // BodyLength and CheckSum both change. Reusing the stored bytes with
            // a flag bolted on would produce a message that fails validation.
            Message parsed;
            if (parsed.parse(replay, false) != ParseError::None) continue;
            builder_.reset();
            builder_.add(tag::MsgType, parsed.msg_type())
                    .add(tag::SenderCompID, cfg_.sender_comp_id)
                    .add(tag::TargetCompID, cfg_.target_comp_id)
                    .add_int(tag::MsgSeqNum, s)
                    .add(tag::SendingTime, utc_timestamp(now_ns))
                    .add(tag::PossDupFlag, "Y")
                    .add(tag::OrigSendingTime, parsed.get_or(tag::SendingTime));
            for (std::size_t i = 0; i < parsed.field_count(); ++i) {
                const Field& f = parsed.field(i);
                if (is_header_or_trailer(f.tag)) continue;
                builder_.add(f.tag, f.value);
            }
            const auto wire = builder_.finish();
            if (!wire.empty()) { outbox_.emplace_back(wire); ++stats_.messages_resent; }
            builder_.reset();
        }
    }

    static bool is_header_or_trailer(Tag t) noexcept {
        return t == tag::BeginString || t == tag::BodyLength || t == tag::MsgType
            || t == tag::SenderCompID || t == tag::TargetCompID || t == tag::MsgSeqNum
            || t == tag::SendingTime || t == tag::CheckSum || t == tag::PossDupFlag
            || t == tag::OrigSendingTime;
    }

    void negotiate_heartbeat(const Message& m) {
        if (const auto hb = m.get_int<int>(tag::HeartBtInt)) if (*hb >= 0) cfg_.heartbeat_secs = *hb;
    }

    void advance_expected(std::uint64_t seq) noexcept { expected_in_ = seq + 1; }

    // FIX UTCTimestamp: YYYYMMDD-HH:MM:SS.sss, derived from epoch nanoseconds
    // with explicit civil-date arithmetic (Howard Hinnant's days_from_civil
    // inverse). Deliberately avoids gmtime_r, which is not guaranteed
    // thread-safe on every platform and reads process-global timezone state.
    static std::string utc_timestamp(std::int64_t ns) {
        const std::int64_t total_ms = ns / 1'000'000LL;
        std::int64_t days = total_ms / 86'400'000LL;
        std::int64_t ms_of_day = total_ms % 86'400'000LL;
        if (ms_of_day < 0) { ms_of_day += 86'400'000LL; --days; }

        // civil_from_days, epoch 1970-01-01.
        std::int64_t z = days + 719468;
        const std::int64_t era = (z >= 0 ? z : z - 146096) / 146097;
        const std::int64_t doe = z - era * 146097;
        const std::int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
        std::int64_t y = yoe + era * 400;
        const std::int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        const std::int64_t mp = (5 * doy + 2) / 153;
        const std::int64_t d = doy - (153 * mp + 2) / 5 + 1;
        const std::int64_t mo = mp < 10 ? mp + 3 : mp - 9;
        if (mo <= 2) ++y;

        const int hh = static_cast<int>(ms_of_day / 3'600'000LL);
        const int mi = static_cast<int>((ms_of_day / 60'000LL) % 60);
        const int ss = static_cast<int>((ms_of_day / 1'000LL) % 60);
        const int ms = static_cast<int>(ms_of_day % 1'000LL);

        char buf[32];
        std::snprintf(buf, sizeof buf, "%04lld%02lld%02lld-%02d:%02d:%02d.%03d",
                      static_cast<long long>(y), static_cast<long long>(mo),
                      static_cast<long long>(d), hh, mi, ss, ms);
        return std::string(buf);
    }

    SessionConfig cfg_;
    Builder builder_;
    SessionState state_ = SessionState::Disconnected;

    std::uint64_t out_seq_ = 1, expected_in_ = 1;
    std::int64_t last_tx_ns_ = 0, last_rx_ns_ = 0;
    bool resend_pending_ = false, awaiting_test_reply_ = false;
    std::uint64_t test_req_id_ = 0, malformed_ = 0, rejects_received_ = 0;

    std::vector<std::string> outbox_;   // drained by the caller
    std::vector<std::string> store_;    // sent-message store, required for resend
    SessionStats stats_{};
};

}  // namespace mkt::fix
