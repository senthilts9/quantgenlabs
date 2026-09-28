// message.hpp — FIX 4.x wire format: zero-copy parser and allocation-free builder.
//
// FIX is `tag=value<SOH>` repeated, SOH being 0x01. It is a deceptively simple
// format with three details that account for most real-world interop failures,
// all of which are handled explicitly here:
//
//   BODYLENGTH (tag 9) counts the bytes from the character AFTER the SOH that
//   terminates tag 9, up to and INCLUDING the SOH that terminates the last field
//   before the checksum. It is not the length of the whole message and it is not
//   the length of the body excluding delimiters. Getting this off by one is the
//   classic reason a counterparty silently drops your logon.
//
//   CHECKSUM (tag 10) is the unsigned sum of every byte up to and including the
//   SOH before `10=`, modulo 256, rendered as EXACTLY three ASCII digits with
//   leading zeros. "10=7" is invalid; "10=007" is correct.
//
//   FIELD ORDER is not free-form. 8 (BeginString), 9 (BodyLength) and 35
//   (MsgType) must be the first three fields in that order, and 10 (CheckSum)
//   must be last. A parser that accepts any order will happily consume a message
//   a real engine would reject.
//
// PERFORMANCE MODEL. Parsing does not allocate and does not copy: fields are
// stored as (tag, string_view) pairs pointing into the caller's buffer, so the
// cost of a parse is one linear pass over the bytes. Lookup is a linear scan
// over that array rather than a hash map, which is the faster choice at this
// scale -- a FIX message carries tens of fields, and a scan over a contiguous
// array of 16-byte entries beats hashing plus a pointer dereference until the
// field count is far larger than FIX ever produces. The buffer must outlive the
// Message; that is the price of zero-copy and it is stated rather than hidden.
//
// The builder writes into a fixed internal buffer sized for the largest
// practical FIX message, so encoding an order allocates nothing either. Body is
// assembled first and the header prepended once the length is known, because
// BodyLength cannot be known before the body exists.
#pragma once

#include <array>
#include <charconv>
#include <cstdint>
#include <cstring>
#include <optional>
#include <string_view>

namespace mkt::fix {

constexpr char SOH = '\x01';

using Tag = std::uint32_t;

// The tags this stack uses. Named rather than inlined as integers: `35` in a
// condition is unreadable, and a typo'd tag number is invisible in review.
namespace tag {
constexpr Tag BeginString        = 8;
constexpr Tag BodyLength         = 9;
constexpr Tag MsgType            = 35;
constexpr Tag SenderCompID       = 49;
constexpr Tag TargetCompID       = 56;
constexpr Tag MsgSeqNum          = 34;
constexpr Tag SendingTime        = 52;
constexpr Tag CheckSum           = 10;
constexpr Tag PossDupFlag        = 43;
constexpr Tag OrigSendingTime    = 122;

constexpr Tag HeartBtInt         = 108;
constexpr Tag EncryptMethod      = 98;
constexpr Tag TestReqID          = 112;
constexpr Tag BeginSeqNo         = 7;
constexpr Tag EndSeqNo           = 16;
constexpr Tag NewSeqNo           = 36;
constexpr Tag GapFillFlag        = 123;
constexpr Tag RefSeqNum          = 45;
constexpr Tag RefTagID           = 371;
constexpr Tag RefMsgType         = 372;
constexpr Tag SessionRejectReason= 373;
constexpr Tag Text               = 58;

constexpr Tag ClOrdID            = 11;
constexpr Tag OrigClOrdID        = 41;
constexpr Tag OrderID            = 37;
constexpr Tag ExecID             = 17;
constexpr Tag ExecType           = 150;
constexpr Tag OrdStatus          = 39;
constexpr Tag Symbol             = 55;
constexpr Tag Side               = 54;
constexpr Tag OrderQty           = 38;
constexpr Tag OrdType            = 40;
constexpr Tag Price              = 44;
constexpr Tag StopPx             = 99;
constexpr Tag TimeInForce        = 59;
constexpr Tag LastQty            = 32;
constexpr Tag LastPx             = 31;
constexpr Tag LeavesQty          = 151;
constexpr Tag CumQty             = 14;
constexpr Tag AvgPx              = 6;
constexpr Tag OrdRejReason       = 103;
constexpr Tag CxlRejReason       = 102;
constexpr Tag CxlRejResponseTo   = 434;
constexpr Tag TransactTime       = 60;
constexpr Tag Account            = 1;
}  // namespace tag

// MsgType values are short strings, not integers ("D", "8", "AE"), so they are
// compared as string_views.
namespace msg {
constexpr std::string_view Heartbeat            = "0";
constexpr std::string_view TestRequest          = "1";
constexpr std::string_view ResendRequest        = "2";
constexpr std::string_view Reject               = "3";
constexpr std::string_view SequenceReset        = "4";
constexpr std::string_view Logout               = "5";
constexpr std::string_view Logon                = "A";
constexpr std::string_view NewOrderSingle       = "D";
constexpr std::string_view ExecutionReport      = "8";
constexpr std::string_view OrderCancelRequest   = "F";
constexpr std::string_view OrderCancelReplace   = "G";
constexpr std::string_view OrderCancelReject    = "9";
}  // namespace msg

enum class ParseError : std::uint8_t {
    None = 0,
    Empty,
    NoSoh,              // a field without its terminating SOH
    BadTag,             // tag is not a positive integer
    NoEquals,           // field with no '=' separator
    TooManyFields,
    MissingBeginString, // tag 8 absent or not first
    MissingBodyLength,  // tag 9 absent or not second
    MissingMsgType,     // tag 35 absent or not third
    MissingCheckSum,    // tag 10 absent or not last
    BodyLengthMismatch,
    CheckSumMismatch,
};

constexpr const char* to_string(ParseError e) noexcept {
    switch (e) {
        case ParseError::None:               return "ok";
        case ParseError::Empty:              return "empty";
        case ParseError::NoSoh:              return "unterminated_field";
        case ParseError::BadTag:             return "bad_tag";
        case ParseError::NoEquals:           return "no_equals";
        case ParseError::TooManyFields:      return "too_many_fields";
        case ParseError::MissingBeginString: return "missing_begin_string";
        case ParseError::MissingBodyLength:  return "missing_body_length";
        case ParseError::MissingMsgType:     return "missing_msg_type";
        case ParseError::MissingCheckSum:    return "missing_checksum";
        case ParseError::BodyLengthMismatch: return "body_length_mismatch";
        case ParseError::CheckSumMismatch:   return "checksum_mismatch";
    }
    return "unknown";
}

struct Field {
    Tag tag = 0;
    std::string_view value;
};

// Computes the FIX checksum over [begin, end): unsigned byte sum mod 256.
inline std::uint8_t checksum(const char* begin, const char* end) noexcept {
    std::uint32_t sum = 0;
    for (const char* p = begin; p != end; ++p)
        sum += static_cast<std::uint8_t>(*p);
    return static_cast<std::uint8_t>(sum & 0xFFu);
}

class Message {
  public:
    // FIX messages carry tens of fields; 128 is generous headroom and keeps the
    // whole field table on the stack.
    static constexpr std::size_t kMaxFields = 128;

    // Parses `raw` in place. `raw` MUST outlive this Message: every value is a
    // view into it. `validate` off skips the BodyLength/CheckSum arithmetic,
    // which is useful when replaying a log whose framing is already trusted.
    ParseError parse(std::string_view raw, bool validate = true) noexcept {
        n_ = 0;
        err_ = ParseError::None;
        raw_ = raw;
        if (raw.empty()) return err_ = ParseError::Empty;

        const char* const base = raw.data();
        std::size_t pos = 0;
        while (pos < raw.size()) {
            const std::size_t eq = raw.find('=', pos);
            if (eq == std::string_view::npos) return err_ = ParseError::NoEquals;
            const std::size_t soh = raw.find(SOH, eq + 1);
            if (soh == std::string_view::npos) return err_ = ParseError::NoSoh;

            Tag t = 0;
            const auto tr = std::from_chars(base + pos, base + eq, t);
            if (tr.ec != std::errc{} || tr.ptr != base + eq || t == 0)
                return err_ = ParseError::BadTag;

            if (n_ >= kMaxFields) return err_ = ParseError::TooManyFields;
            fields_[n_++] = Field{t, raw.substr(eq + 1, soh - eq - 1)};

            // Remember where the checksum field starts: both the BodyLength and
            // CheckSum computations are defined relative to it.
            if (t == tag::CheckSum) {
                checksum_field_pos_ = pos;
                // 10= must be last; anything after it is not part of this message.
                pos = soh + 1;
                break;
            }
            pos = soh + 1;
        }

        // --- structural requirements ----------------------------------------
        if (n_ < 4) return err_ = ParseError::MissingCheckSum;
        if (fields_[0].tag != tag::BeginString) return err_ = ParseError::MissingBeginString;
        if (fields_[1].tag != tag::BodyLength)  return err_ = ParseError::MissingBodyLength;
        if (fields_[2].tag != tag::MsgType)     return err_ = ParseError::MissingMsgType;
        if (fields_[n_ - 1].tag != tag::CheckSum) return err_ = ParseError::MissingCheckSum;

        if (!validate) return err_ = ParseError::None;

        // --- BodyLength ------------------------------------------------------
        // Body starts after the SOH terminating tag 9 and ends at the SOH before
        // `10=`, inclusive -- which is exactly checksum_field_pos_.
        const std::string_view bl_val = fields_[1].value;
        std::uint32_t declared = 0;
        if (std::from_chars(bl_val.data(), bl_val.data() + bl_val.size(), declared).ec != std::errc{})
            return err_ = ParseError::BodyLengthMismatch;
        const std::size_t body_begin = body_start_offset();
        if (body_begin == std::string_view::npos || checksum_field_pos_ < body_begin)
            return err_ = ParseError::BodyLengthMismatch;
        if (declared != checksum_field_pos_ - body_begin)
            return err_ = ParseError::BodyLengthMismatch;

        // --- CheckSum --------------------------------------------------------
        const std::string_view cs_val = fields_[n_ - 1].value;
        if (cs_val.size() != 3) return err_ = ParseError::CheckSumMismatch;
        std::uint32_t declared_cs = 0;
        if (std::from_chars(cs_val.data(), cs_val.data() + cs_val.size(), declared_cs).ec != std::errc{})
            return err_ = ParseError::CheckSumMismatch;
        if (checksum(base, base + checksum_field_pos_) != static_cast<std::uint8_t>(declared_cs))
            return err_ = ParseError::CheckSumMismatch;

        return err_ = ParseError::None;
    }

    bool ok() const noexcept { return err_ == ParseError::None; }
    ParseError error() const noexcept { return err_; }
    std::size_t field_count() const noexcept { return n_; }
    std::string_view raw() const noexcept { return raw_; }

    // Linear scan: faster than hashing at FIX field counts. Returns the FIRST
    // occurrence, which is correct for header/body fields; repeating groups need
    // positional iteration, not lookup by tag.
    std::optional<std::string_view> get(Tag t) const noexcept {
        for (std::size_t i = 0; i < n_; ++i)
            if (fields_[i].tag == t) return fields_[i].value;
        return std::nullopt;
    }

    std::string_view get_or(Tag t, std::string_view fallback = {}) const noexcept {
        const auto v = get(t);
        return v ? *v : fallback;
    }

    bool has(Tag t) const noexcept { return get(t).has_value(); }

    // Integer accessor that reports failure rather than returning a silent 0 --
    // a missing OrderQty and an OrderQty of zero are different problems.
    template <class T>
    std::optional<T> get_int(Tag t) const noexcept {
        const auto v = get(t);
        if (!v || v->empty()) return std::nullopt;
        T out{};
        const auto r = std::from_chars(v->data(), v->data() + v->size(), out);
        if (r.ec != std::errc{} || r.ptr != v->data() + v->size()) return std::nullopt;
        return out;
    }

    // FIX prices are decimal strings. from_chars for double is not available in
    // libstdc++/libc++ of every vintage, so this parses explicitly -- which also
    // avoids locale dependence, a real hazard in a global trading system where
    // strtod under a comma-decimal locale silently truncates "1.5" to 1.
    std::optional<double> get_double(Tag t) const noexcept {
        const auto v = get(t);
        if (!v || v->empty()) return std::nullopt;
        return parse_decimal(*v);
    }

    static std::optional<double> parse_decimal(std::string_view s) noexcept {
        std::size_t i = 0;
        bool neg = false;
        if (s[i] == '+' || s[i] == '-') { neg = (s[i] == '-'); ++i; }
        if (i >= s.size()) return std::nullopt;
        double whole = 0.0;
        bool any = false;
        for (; i < s.size() && s[i] >= '0' && s[i] <= '9'; ++i) {
            whole = whole * 10.0 + static_cast<double>(s[i] - '0');
            any = true;
        }
        double frac = 0.0, scale = 1.0;
        if (i < s.size() && s[i] == '.') {
            ++i;
            for (; i < s.size() && s[i] >= '0' && s[i] <= '9'; ++i) {
                frac = frac * 10.0 + static_cast<double>(s[i] - '0');
                scale *= 10.0;
                any = true;
            }
        }
        if (!any || i != s.size()) return std::nullopt;   // trailing garbage
        const double val = whole + frac / scale;
        return neg ? -val : val;
    }

    std::string_view msg_type() const noexcept { return get_or(tag::MsgType); }
    std::optional<std::uint64_t> seq_num() const noexcept { return get_int<std::uint64_t>(tag::MsgSeqNum); }
    bool is_poss_dup() const noexcept { return get_or(tag::PossDupFlag) == "Y"; }

    const Field& field(std::size_t i) const noexcept { return fields_[i]; }

  private:
    // Offset of the first body byte: just past the SOH that terminates tag 9.
    std::size_t body_start_offset() const noexcept {
        const char* p = fields_[1].value.data() + fields_[1].value.size();
        if (p < raw_.data() || p >= raw_.data() + raw_.size()) return std::string_view::npos;
        return static_cast<std::size_t>(p - raw_.data()) + 1;   // +1 skips the SOH
    }

    std::array<Field, kMaxFields> fields_{};
    std::size_t n_ = 0;
    std::string_view raw_;
    std::size_t checksum_field_pos_ = 0;
    ParseError err_ = ParseError::Empty;
};

// Allocation-free encoder. Body is assembled first because BodyLength cannot be
// computed until the body exists, then the header is written and the two are
// joined in one final pass.
class Builder {
  public:
    static constexpr std::size_t kCapacity = 4096;

    explicit Builder(std::string_view begin_string = "FIX.4.4") : begin_(begin_string) {}

    void reset() noexcept { body_len_ = 0; out_len_ = 0; overflow_ = false; }

    Builder& add(Tag t, std::string_view value) noexcept {
        char num[12];
        const auto r = std::to_chars(num, num + sizeof num, t);
        write(std::string_view(num, static_cast<std::size_t>(r.ptr - num)));
        write("=");
        write(value);
        write(std::string_view(&SOH, 1));
        return *this;
    }

    template <class T>
    Builder& add_int(Tag t, T v) noexcept {
        char num[24];
        const auto r = std::to_chars(num, num + sizeof num, v);
        return add(t, std::string_view(num, static_cast<std::size_t>(r.ptr - num)));
    }

    // Fixed-decimal formatting with an explicit precision. FIX prices are
    // decimal strings and a counterparty that expects 2dp will reject
    // "1.1000000000000001", so the precision is a parameter, never a default
    // from the streaming operators.
    Builder& add_price(Tag t, double v, int dp = 5) noexcept {
        char buf[48];
        const std::size_t n = format_fixed(buf, sizeof buf, v, dp);
        return add(t, std::string_view(buf, n));
    }

    // Emits 8=..|9=..|<body>|10=..| into the internal buffer and returns a view
    // of it. The view is invalidated by the next reset()/finish().
    std::string_view finish() noexcept {
        out_len_ = 0;
        // Header: 8=<begin><SOH>9=<bodylen><SOH>
        char lenbuf[12];
        const auto lr = std::to_chars(lenbuf, lenbuf + sizeof lenbuf, body_len_);
        append(out_, out_len_, "8=");
        append(out_, out_len_, begin_);
        append(out_, out_len_, std::string_view(&SOH, 1));
        append(out_, out_len_, "9=");
        append(out_, out_len_, std::string_view(lenbuf, static_cast<std::size_t>(lr.ptr - lenbuf)));
        append(out_, out_len_, std::string_view(&SOH, 1));
        append(out_, out_len_, std::string_view(body_, body_len_));

        // CheckSum over everything written so far, three digits, zero-padded.
        const std::uint8_t cs = checksum(out_, out_ + out_len_);
        char csbuf[4] = {
            static_cast<char>('0' + (cs / 100) % 10),
            static_cast<char>('0' + (cs / 10) % 10),
            static_cast<char>('0' + cs % 10),
            '\0'};
        append(out_, out_len_, "10=");
        append(out_, out_len_, std::string_view(csbuf, 3));
        append(out_, out_len_, std::string_view(&SOH, 1));

        return overflow_ ? std::string_view{} : std::string_view(out_, out_len_);
    }

    bool overflowed() const noexcept { return overflow_; }

    // Renders SOH as '|' for logs and test failure messages -- a raw FIX message
    // in a terminal is unreadable because SOH is a control character.
    static std::size_t to_pipe(std::string_view fix, char* out, std::size_t cap) noexcept {
        const std::size_t n = fix.size() < cap ? fix.size() : cap;
        for (std::size_t i = 0; i < n; ++i) out[i] = (fix[i] == SOH) ? '|' : fix[i];
        return n;
    }

  private:
    void write(std::string_view s) noexcept { append(body_, body_len_, s); }

    void append(char* dst, std::size_t& len, std::string_view s) noexcept {
        if (len + s.size() > kCapacity) { overflow_ = true; return; }
        std::memcpy(dst + len, s.data(), s.size());
        len += s.size();
    }

    static std::size_t format_fixed(char* buf, std::size_t cap, double v, int dp) noexcept {
        if (dp < 0) dp = 0;
        if (dp > 9) dp = 9;
        bool neg = v < 0.0;
        if (neg) v = -v;
        double pow10 = 1.0;
        for (int i = 0; i < dp; ++i) pow10 *= 10.0;
        // Round half away from zero on the scaled integer, then split.
        const std::uint64_t scaled = static_cast<std::uint64_t>(v * pow10 + 0.5);
        const std::uint64_t ip = dp ? scaled / static_cast<std::uint64_t>(pow10) : scaled;
        const std::uint64_t fp = dp ? scaled % static_cast<std::uint64_t>(pow10) : 0;

        std::size_t n = 0;
        if (neg && n < cap) buf[n++] = '-';
        char tmp[24];
        const auto r = std::to_chars(tmp, tmp + sizeof tmp, ip);
        const std::size_t iplen = static_cast<std::size_t>(r.ptr - tmp);
        for (std::size_t i = 0; i < iplen && n < cap; ++i) buf[n++] = tmp[i];
        if (dp > 0) {
            if (n < cap) buf[n++] = '.';
            char fbuf[24];
            const auto fr = std::to_chars(fbuf, fbuf + sizeof fbuf, fp);
            const std::size_t flen = static_cast<std::size_t>(fr.ptr - fbuf);
            for (int pad = 0; pad < dp - static_cast<int>(flen) && n < cap; ++pad) buf[n++] = '0';
            for (std::size_t i = 0; i < flen && n < cap; ++i) buf[n++] = fbuf[i];
        }
        return n;
    }

    std::string_view begin_;
    char body_[kCapacity]{};
    char out_[kCapacity]{};
    std::size_t body_len_ = 0, out_len_ = 0;
    bool overflow_ = false;
};

}  // namespace mkt::fix
