// mkt_bindings.cpp — expose the market-connectivity core to Python.
// Builds only if pybind11 is present (see CMakeLists). Module: mkt_connectivity.
//
//   c++ -std=c++20 -O3 -Wall -shared -fPIC -undefined dynamic_lookup \
//       $(python3 -m pybind11 --includes) -Icpp/include cpp/src/mkt_bindings.cpp \
//       -o mkt_connectivity$(python3-config --extension-suffix)
//
// WHAT IS AND IS NOT EXPOSED, and why.
//
// The hot path is NOT crossed per-tick from Python. Calling into C++ once per
// market-data message would put the interpreter in the middle of the loop this
// layer exists to keep fast, and the cost of the boundary would dominate the work
// -- the same lesson quant_bindings.cpp records in detail, where per-element
// marshalling made a 10x-faster kernel 0.58x slower end to end.
//
// So the boundary is drawn at COARSE operations: feed a batch of ticks, submit one
// order and get the resulting reports, read a book snapshot. Python orchestrates
// and observes; C++ does the per-message work in a loop that never returns to the
// interpreter mid-flight.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <string>
#include <vector>

#include "mkt/book/ladder.hpp"
#include "mkt/feed/validator.hpp"
#include "mkt/fix/message.hpp"
#include "mkt/risk/pretrade.hpp"
#include "mkt/venue/desk.hpp"
#include "mkt/venue/matching_engine.hpp"

namespace py = pybind11;

PYBIND11_MODULE(mkt_connectivity, m) {
    m.doc() = "QuantGenLabs market-connectivity core: L2 book, feed quality gate, "
              "FIX wire format, pre-trade risk, matching engine. Simulation only.";

    // ---------------------------------------------------------------- book
    // py::arithmetic() + implicitly_convertible so a plain int works too. The
    // pure-Python fallback uses an IntEnum and therefore accepts ints; if the C++
    // binding refused them, the two implementations would have different call
    // signatures and callers would have to branch on which one is loaded -- which
    // is precisely what the wrapper in core/connectivity exists to prevent.
    py::enum_<mkt::book::Side>(m, "Side", py::arithmetic())
        .value("BID", mkt::book::Side::Bid)
        .value("ASK", mkt::book::Side::Ask);
    py::implicitly_convertible<int, mkt::book::Side>();

    py::class_<mkt::book::Level>(m, "Level")
        .def_readonly("price", &mkt::book::Level::price)
        .def_readonly("qty", &mkt::book::Level::qty)
        .def_readonly("orders", &mkt::book::Level::orders)
        .def("__repr__", [](const mkt::book::Level& l) {
            return "<Level px=" + std::to_string(l.price) + " qty=" + std::to_string(l.qty) + ">";
        });

    py::class_<mkt::book::Ladder>(m, "Ladder")
        .def(py::init<mkt::book::Ticks, std::size_t>(), py::arg("floor_ticks"), py::arg("n_ticks"))
        .def("upsert", &mkt::book::Ladder::upsert,
             py::arg("side"), py::arg("price"), py::arg("qty"), py::arg("orders") = 1)
        .def("add", &mkt::book::Ladder::add,
             py::arg("side"), py::arg("price"), py::arg("delta"), py::arg("orders") = 1)
        .def("clear", &mkt::book::Ladder::clear)
        .def("qty_at", &mkt::book::Ladder::qty_at)
        .def("total_qty", &mkt::book::Ladder::total_qty)
        .def("levels", &mkt::book::Ladder::levels)
        .def("spread_ticks", &mkt::book::Ladder::spread_ticks)
        .def("mid_ticks", &mkt::book::Ladder::mid_ticks)
        .def("micro_price_ticks", &mkt::book::Ladder::micro_price_ticks)
        .def("imbalance", &mkt::book::Ladder::imbalance)
        .def("is_crossed", &mkt::book::Ladder::is_crossed)
        .def("rejects", &mkt::book::Ladder::rejects)
        // Returns None rather than a sentinel: an empty side and a zero price are
        // different things, and a sentinel invites the caller to conflate them.
        .def("best", [](const mkt::book::Ladder& b, mkt::book::Side s) -> py::object {
            mkt::book::Ticks px; mkt::book::Qty q;
            if (!b.best(s, px, q)) return py::none();
            return py::make_tuple(px, q);
        }, py::arg("side"))
        .def("depth", [](const mkt::book::Ladder& b, mkt::book::Side s, std::size_t n) {
            std::vector<mkt::book::Level> out(n);
            const std::size_t w = b.depth(s, std::span<mkt::book::Level>(out.data(), out.size()));
            out.resize(w);
            return out;
        }, py::arg("side"), py::arg("levels") = 10);

    // ---------------------------------------------------------------- feed
    py::enum_<mkt::feed::Verdict>(m, "Verdict")
        .value("ACCEPTED", mkt::feed::Verdict::Accepted)
        .value("SEQUENCE_GAP", mkt::feed::Verdict::SequenceGap)
        .value("DUPLICATE", mkt::feed::Verdict::Duplicate)
        .value("REORDERED", mkt::feed::Verdict::Reordered)
        .value("STALE_TIMESTAMP", mkt::feed::Verdict::StaleTimestamp)
        .value("TOO_OLD", mkt::feed::Verdict::TooOld)
        .value("PRICE_OUTLIER", mkt::feed::Verdict::PriceOutlier)
        .value("PRICE_OUT_OF_BAND", mkt::feed::Verdict::PriceOutOfBand)
        .value("BAD_VALUE", mkt::feed::Verdict::BadValue);

    py::class_<mkt::feed::Config>(m, "FeedConfig")
        .def(py::init<>())
        .def_readwrite("price_min", &mkt::feed::Config::price_min)
        .def_readwrite("price_max", &mkt::feed::Config::price_max)
        .def_readwrite("max_z", &mkt::feed::Config::max_z)
        .def_readwrite("min_samples", &mkt::feed::Config::min_samples)
        .def_readwrite("lambda_", &mkt::feed::Config::lambda)
        .def_readwrite("max_age_ns", &mkt::feed::Config::max_age_ns)
        .def_readwrite("stale_after_ns", &mkt::feed::Config::stale_after_ns);

    py::class_<mkt::feed::Validator>(m, "FeedValidator")
        .def(py::init<mkt::feed::Config>(), py::arg("config") = mkt::feed::Config{})
        .def("check", [](mkt::feed::Validator& v, std::uint64_t seq, std::int64_t exch_ns,
                         std::int64_t recv_ns, double price, double size) {
            return v.check(mkt::feed::Tick{seq, exch_ns, recv_ns, price, size});
        }, py::arg("seq"), py::arg("exch_ns"), py::arg("recv_ns"), py::arg("price"), py::arg("size") = 1.0)
        // THE BATCH ENTRY POINT. This is how a feed is actually meant to be driven
        // from Python: the whole batch is validated inside one C++ loop, and only
        // the verdict counts come back. Calling check() per tick from Python works
        // and is useful for tests, but it puts the interpreter in the hot loop.
        .def("check_batch", [](mkt::feed::Validator& v,
                               const std::vector<std::tuple<std::uint64_t, std::int64_t, double>>& ticks) {
            std::vector<std::uint64_t> counts(10, 0);
            for (const auto& [seq, ns, px] : ticks)
                ++counts[static_cast<std::size_t>(v.check(mkt::feed::Tick{seq, ns, ns, px, 1.0}))];
            py::dict out;
            out["accepted"]          = counts[0];
            out["sequence_gap"]      = counts[2];
            out["duplicate"]         = counts[3];
            out["reordered"]         = counts[4];
            out["stale_timestamp"]   = counts[5];
            out["too_old"]           = counts[6];
            out["price_outlier"]     = counts[7];
            out["price_out_of_band"] = counts[8];
            out["bad_value"]         = counts[9];
            return out;
        }, py::arg("ticks"), "Validate a batch of (seq, timestamp_ns, price) tuples in one C++ pass.")
        .def("needs_recovery", &mkt::feed::Validator::needs_recovery)
        .def("recovered", &mkt::feed::Validator::recovered)
        .def("is_stale", &mkt::feed::Validator::is_stale)
        .def("last_price", &mkt::feed::Validator::last_price)
        .def("reject_rate", &mkt::feed::Validator::reject_rate)
        .def("reset", &mkt::feed::Validator::reset)
        .def("counters", [](const mkt::feed::Validator& v) {
            const auto& c = v.counters();
            py::dict d;
            d["accepted"] = c.accepted; d["gaps"] = c.gaps;
            d["gap_messages_lost"] = c.gap_messages_lost;
            d["duplicates"] = c.duplicates; d["reordered"] = c.reordered;
            d["stale_timestamp"] = c.stale_timestamp; d["too_old"] = c.too_old;
            d["outliers"] = c.outliers; d["out_of_band"] = c.out_of_band;
            d["bad_values"] = c.bad_values;
            d["total_rejected"] = c.total_rejected(); d["total_seen"] = c.total_seen();
            return d;
        });

    m.def("verdict_name", [](mkt::feed::Verdict v) { return std::string(mkt::feed::to_string(v)); });

    // ---------------------------------------------------------------- pre-trade risk
    py::enum_<mkt::risk::Side>(m, "OrderSide", py::arithmetic())
        .value("BUY", mkt::risk::Side::Buy)
        .value("SELL", mkt::risk::Side::Sell);
    py::implicitly_convertible<int, mkt::risk::Side>();

    py::enum_<mkt::risk::OrdType>(m, "OrdType")
        .value("MARKET", mkt::risk::OrdType::Market)
        .value("LIMIT", mkt::risk::OrdType::Limit);

    py::enum_<mkt::risk::Decision>(m, "Decision")
        .value("ACCEPT", mkt::risk::Decision::Accept)
        .value("KILL_SWITCH_ENGAGED", mkt::risk::Decision::KillSwitchEngaged)
        .value("SYMBOL_HALTED", mkt::risk::Decision::SymbolHalted)
        .value("UNKNOWN_SYMBOL", mkt::risk::Decision::UnknownSymbol)
        .value("BAD_QUANTITY", mkt::risk::Decision::BadQuantity)
        .value("BAD_PRICE", mkt::risk::Decision::BadPrice)
        .value("MAX_ORDER_QTY", mkt::risk::Decision::MaxOrderQty)
        .value("MAX_NOTIONAL", mkt::risk::Decision::MaxNotional)
        .value("PRICE_COLLAR", mkt::risk::Decision::PriceCollar)
        .value("NO_REFERENCE_PRICE", mkt::risk::Decision::NoReferencePrice)
        .value("POSITION_LIMIT", mkt::risk::Decision::PositionLimit)
        .value("MAX_OPEN_ORDERS", mkt::risk::Decision::MaxOpenOrders)
        .value("MESSAGE_RATE_LIMIT", mkt::risk::Decision::MessageRateLimit);

    m.def("decision_name", [](mkt::risk::Decision d) { return std::string(mkt::risk::to_string(d)); });

    py::class_<mkt::risk::SymbolLimits>(m, "SymbolLimits")
        .def(py::init<>())
        .def_readwrite("known", &mkt::risk::SymbolLimits::known)
        .def_readwrite("halted", &mkt::risk::SymbolLimits::halted)
        .def_readwrite("max_order_qty", &mkt::risk::SymbolLimits::max_order_qty)
        .def_readwrite("max_order_notional", &mkt::risk::SymbolLimits::max_order_notional)
        .def_readwrite("collar_bps", &mkt::risk::SymbolLimits::collar_bps)
        .def_readwrite("max_long_position", &mkt::risk::SymbolLimits::max_long_position)
        .def_readwrite("max_short_position", &mkt::risk::SymbolLimits::max_short_position)
        .def_readwrite("contract_size", &mkt::risk::SymbolLimits::contract_size);

    py::class_<mkt::risk::PreTradeGate>(m, "PreTradeGate")
        .def(py::init<std::size_t>(), py::arg("n_symbols"))
        .def("set_limits", &mkt::risk::PreTradeGate::set_limits)
        .def("set_position", &mkt::risk::PreTradeGate::set_position)
        .def("position", &mkt::risk::PreTradeGate::position)
        .def("halt", &mkt::risk::PreTradeGate::halt)
        .def("engage_kill_switch", &mkt::risk::PreTradeGate::engage_kill_switch)
        .def("release_kill_switch", &mkt::risk::PreTradeGate::release_kill_switch)
        .def("kill_switch_engaged", &mkt::risk::PreTradeGate::kill_switch_engaged)
        .def("on_fill", &mkt::risk::PreTradeGate::on_fill)
        .def("on_order_sent", &mkt::risk::PreTradeGate::on_order_sent)
        .def("on_order_closed", &mkt::risk::PreTradeGate::on_order_closed)
        .def("check", [](mkt::risk::PreTradeGate& g, std::uint32_t symbol, mkt::risk::Side side,
                         double qty, double price, double reference_price, bool reference_ok,
                         mkt::risk::OrdType type) {
            mkt::risk::OrderRequest o;
            o.symbol = symbol; o.side = side; o.qty = qty; o.price = price; o.type = type;
            return g.check(o, mkt::risk::MarketContext{reference_price, reference_ok});
        }, py::arg("symbol"), py::arg("side"), py::arg("qty"), py::arg("price"),
           py::arg("reference_price"), py::arg("reference_ok") = true,
           py::arg("type") = mkt::risk::OrdType::Limit)
        .def("stats", [](const mkt::risk::PreTradeGate& g) {
            const auto& s = g.stats();
            py::dict d;
            d["checked"] = s.checked; d["accepted"] = s.accepted; d["rejected"] = s.rejected();
            d["kill_switch"] = s.kill_switch; d["max_qty"] = s.max_qty;
            d["max_notional"] = s.max_notional; d["collar"] = s.collar;
            d["position"] = s.position; d["no_reference"] = s.no_reference;
            d["rate_limited"] = s.rate_limited; d["halted"] = s.halted;
            return d;
        });

    // ---------------------------------------------------------------- FIX
    // Parsing only. Encoding is not exposed: a Python caller that can build
    // arbitrary FIX could construct a message the session layer never sequenced,
    // which is exactly the invariant the session layer exists to hold.
    m.def("parse_fix", [](const std::string& raw, bool validate) -> py::object {
        mkt::fix::Message msg;
        const auto err = msg.parse(raw, validate);
        py::dict d;
        d["ok"] = (err == mkt::fix::ParseError::None);
        d["error"] = std::string(mkt::fix::to_string(err));
        if (err != mkt::fix::ParseError::None) return d;
        py::dict fields;
        for (std::size_t i = 0; i < msg.field_count(); ++i) {
            const auto& f = msg.field(i);
            fields[py::int_(f.tag)] = std::string(f.value);
        }
        d["fields"] = fields;
        d["msg_type"] = std::string(msg.msg_type());
        d["seq_num"] = msg.seq_num() ? py::cast(*msg.seq_num()) : py::none();
        d["field_count"] = msg.field_count();
        return d;
    }, py::arg("raw"), py::arg("validate") = true,
       "Parse a FIX message. Returns {ok, error, fields{tag:value}, msg_type, seq_num}.");

    m.def("pretty_fix", [](const std::string& raw) {
        std::string out = raw;
        for (char& c : out) if (c == mkt::fix::SOH) c = '|';
        return out;
    }, py::arg("raw"), "Render SOH as '|' so a FIX message is readable in a log or UI.");

    // ---------------------------------------------------------------- matching
    py::enum_<mkt::venue::OrderState>(m, "OrderState")
        .value("NEW", mkt::venue::OrderState::New)
        .value("PARTIALLY_FILLED", mkt::venue::OrderState::PartiallyFilled)
        .value("FILLED", mkt::venue::OrderState::Filled)
        .value("CANCELLED", mkt::venue::OrderState::Cancelled)
        .value("REJECTED", mkt::venue::OrderState::Rejected);

    py::enum_<mkt::venue::TimeInForce>(m, "TimeInForce")
        .value("DAY", mkt::venue::TimeInForce::Day)
        .value("IOC", mkt::venue::TimeInForce::IOC)
        .value("FOK", mkt::venue::TimeInForce::FOK);

    py::class_<mkt::venue::MatchingEngine>(m, "MatchingEngine")
        .def(py::init<mkt::book::Ticks, std::size_t, bool>(),
             py::arg("floor_ticks"), py::arg("n_ticks"), py::arg("prevent_self_match") = true)
        // Returns the fills alongside the result, because a caller that has to ask
        // for them separately can forget to, and then the book and its own view of
        // position silently diverge.
        .def("submit", [](mkt::venue::MatchingEngine& e, std::uint64_t id, std::uint32_t owner,
                          mkt::book::Side side, mkt::book::Ticks price, std::int64_t qty,
                          mkt::venue::TimeInForce tif, bool market) {
            std::vector<py::dict> fills;
            e.on_fill([&](const mkt::venue::Fill& f) {
                py::dict d;
                d["exec_seq"] = f.exec_seq; d["aggressor"] = f.aggressor;
                d["resting"] = f.resting;   d["price"] = f.price; d["qty"] = f.qty;
                fills.push_back(d);
            });
            mkt::venue::NewOrder o;
            o.id = id; o.owner = owner; o.side = side; o.price = price; o.qty = qty;
            o.tif = tif;
            o.type = market ? mkt::venue::OrdType::Market : mkt::venue::OrdType::Limit;
            const auto r = e.submit(o);
            e.on_fill(nullptr);
            py::dict out;
            out["id"] = r.id;
            out["state"] = r.state;
            out["reject"] = std::string(mkt::venue::to_string(r.reject));
            out["filled"] = r.filled; out["leaves"] = r.leaves;
            out["avg_price_ticks"] = r.avg_price_ticks;
            out["fills"] = fills;
            return out;
        }, py::arg("id"), py::arg("owner"), py::arg("side"), py::arg("price"), py::arg("qty"),
           py::arg("tif") = mkt::venue::TimeInForce::Day, py::arg("market") = false)
        .def("cancel", [](mkt::venue::MatchingEngine& e, std::uint64_t id, std::uint32_t owner) {
            const auto r = e.cancel(id, owner);
            py::dict d;
            d["state"] = r.state;
            d["reject"] = std::string(mkt::venue::to_string(r.reject));
            return d;
        }, py::arg("id"), py::arg("owner"))
        .def("leaves", &mkt::venue::MatchingEngine::leaves)
        .def("is_resting", &mkt::venue::MatchingEngine::is_resting)
        .def("queue_position", &mkt::venue::MatchingEngine::queue_position)
        .def("book", &mkt::venue::MatchingEngine::book, py::return_value_policy::reference_internal)
        .def("stats", [](const mkt::venue::MatchingEngine& e) {
            const auto& s = e.stats();
            py::dict d;
            d["orders_accepted"] = s.orders_accepted; d["orders_rejected"] = s.orders_rejected;
            d["fills"] = s.fills; d["cancels"] = s.cancels; d["amends"] = s.amends;
            d["self_match_blocks"] = s.self_match_blocks; d["volume"] = s.volume;
            return d;
        });

    // ---------------------------------------------------------------- desk
    // The client side: a FIX session wired to the simulated venue, keeping a
    // blotter built ONLY from the ExecutionReports that came back.
    //
    // Python asks for "a limit buy of 500" and receives blotter rows and a wire
    // log. It never assembles FIX itself -- the session layer owns sequence
    // numbers, BodyLength and CheckSum, and a caller able to build arbitrary FIX
    // could produce a message the session never sequenced.
    py::class_<mkt::venue::SimulatedDesk>(m, "SimulatedDesk")
        .def(py::init<mkt::book::Ticks, std::size_t, double, std::string, std::string>(),
             py::arg("floor_ticks") = 1, py::arg("n_ticks") = 50000,
             py::arg("tick_size") = 0.01, py::arg("sender") = "QGLABS",
             py::arg("target") = "SIMVENUE")
        .def("start", &mkt::venue::SimulatedDesk::start, py::arg("now_ns"))
        .def("connected", &mkt::venue::SimulatedDesk::connected)
        .def("seed_liquidity", &mkt::venue::SimulatedDesk::seed_liquidity,
             py::arg("side"), py::arg("price"), py::arg("qty"))
        .def("submit", &mkt::venue::SimulatedDesk::submit,
             py::arg("cl_ord_id"), py::arg("symbol"), py::arg("side"), py::arg("qty"),
             py::arg("price"), py::arg("ord_type"), py::arg("tif"), py::arg("now_ns"))
        .def("cancel", &mkt::venue::SimulatedDesk::cancel,
             py::arg("cl_ord_id"), py::arg("orig_cl_ord_id"), py::arg("now_ns"))
        .def("tick", &mkt::venue::SimulatedDesk::tick, py::arg("now_ns"))
        .def("tick_size", &mkt::venue::SimulatedDesk::tick_size)
        .def("book", [](const mkt::venue::SimulatedDesk& d) {
            return &d.engine().book();
        }, py::return_value_policy::reference_internal)
        .def("engine_stats", [](const mkt::venue::SimulatedDesk& d) {
            const auto& s = d.engine().stats();
            py::dict o;
            o["orders_accepted"] = s.orders_accepted; o["orders_rejected"] = s.orders_rejected;
            o["fills"] = s.fills; o["cancels"] = s.cancels;
            o["self_match_blocks"] = s.self_match_blocks; o["volume"] = s.volume;
            return o;
        })
        .def("position", [](const mkt::venue::SimulatedDesk& d) {
            double net = 0, avg = 0;
            d.position(net, avg);
            py::dict o; o["net_qty"] = net; o["avg_cost"] = avg;
            return o;
        })
        .def("blotter", [](const mkt::venue::SimulatedDesk& d) {
            py::list rows;
            for (const auto& r : d.blotter()) {
                py::dict o;
                o["cl_ord_id"] = r.cl_ord_id;
                o["orig_cl_ord_id"] = r.orig_cl_ord_id;
                o["venue_order_id"] = r.venue_order_id;
                o["symbol"] = r.symbol;
                o["side"] = r.side;
                o["ord_type"] = r.ord_type;
                o["tif"] = r.tif;
                o["order_qty"] = r.order_qty;
                o["limit_price"] = r.limit_price;
                o["status"] = r.status;
                o["cum_qty"] = r.cum_qty;
                o["leaves_qty"] = r.leaves_qty;
                o["avg_px"] = r.avg_px;
                o["reject_text"] = r.reject_text;
                py::list fills;
                for (const auto& f : r.fills) {
                    py::dict fd;
                    fd["exec_id"] = f.exec_id; fd["qty"] = f.qty; fd["price"] = f.price;
                    fd["exec_type"] = f.exec_type; fd["ord_status"] = f.ord_status;
                    fills.append(fd);
                }
                o["fills"] = fills;
                rows.append(o);
            }
            return rows;
        })
        // Raw bytes come back with SOH rendered as '|': a FIX message containing
        // a 0x01 control character is not safely transportable through JSON.
        .def("wire_log", [](const mkt::venue::SimulatedDesk& d, std::size_t tail) {
            py::list out;
            const auto& w = d.wire_log();
            const std::size_t start = (tail && w.size() > tail) ? w.size() - tail : 0;
            for (std::size_t i = start; i < w.size(); ++i) {
                py::dict o;
                std::string pretty = w[i].raw;
                for (char& c : pretty) if (c == mkt::fix::SOH) c = '|';
                o["outbound"] = w[i].outbound;
                o["raw"] = pretty;
                o["msg_type"] = w[i].msg_type;
                o["seq"] = w[i].seq;
                out.append(o);
            }
            return out;
        }, py::arg("tail") = 0);
}
