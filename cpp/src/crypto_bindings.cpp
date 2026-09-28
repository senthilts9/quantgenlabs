// crypto_bindings.cpp — expose the C++ crypto engine to Python via pybind11.
//
// Matches the parent repo's `qgl_xrpl_ext` pattern: builds ONLY if pybind11 is
// found (see CMake), never fatal otherwise — Python falls back to the pure
// xquant.crypto modules. This gives the compiled hot path a drop-in Python
// call site so a research notebook and the live engine share one implementation.
//
// Build (standalone, if you have pybind11):
//   g++ -std=c++20 -O3 -Wall -shared -fPIC $(python3 -m pybind11 --includes) \
//       -Icpp/include cpp/src/crypto_bindings.cpp \
//       -o qgl_xrpl_crypto$(python3-config --extension-suffix)
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "xrpl/crypto_engine.hpp"

namespace py = pybind11;
using namespace xrpl::crypto;

PYBIND11_MODULE(qgl_xrpl_crypto, m) {
    m.doc() = "Low-latency crypto quant/risk engine (C++ hot path).";

    // perps
    m.def("funding_rate", &funding_rate, py::arg("premium_index"),
          py::arg("interest_rate") = 0.0001, py::arg("clamp") = kDefaultClamp);
    m.def("basis", &basis);
    m.def("annualised_funding", &annualised_funding, py::arg("rate_per_interval"),
          py::arg("intervals_per_day") = kFundingIntervalsPerDay);
    m.def("funding_pnl", &funding_pnl);

    // liquidation
    py::class_<MarginState>(m, "MarginState")
        .def_readonly("liquidation_price", &MarginState::liquidation_price)
        .def_readonly("bankruptcy_price", &MarginState::bankruptcy_price)
        .def_readonly("margin_ratio", &MarginState::margin_ratio)
        .def_readonly("distance_to_liq", &MarginState::distance_to_liq)
        .def_readonly("unrealised_pnl", &MarginState::unrealised_pnl)
        .def_readonly("equity", &MarginState::equity);
    m.def("liquidation_price", &liquidation_price, py::arg("entry"),
          py::arg("leverage"), py::arg("is_long"), py::arg("mmr") = 0.005);
    m.def("margin_state", &margin_state, py::arg("entry"), py::arg("mark"),
          py::arg("qty"), py::arg("leverage"), py::arg("is_long"),
          py::arg("mmr") = 0.005);
    m.def("max_leverage_for_liq_buffer", &max_leverage_for_liq_buffer,
          py::arg("is_long"), py::arg("buffer"), py::arg("mmr") = 0.005);

    // amm
    py::class_<SwapResult>(m, "SwapResult")
        .def_readonly("amount_out", &SwapResult::amount_out)
        .def_readonly("execution_price", &SwapResult::execution_price)
        .def_readonly("spot_price_before", &SwapResult::spot_price_before)
        .def_readonly("price_impact", &SwapResult::price_impact)
        .def_readonly("fee_paid", &SwapResult::fee_paid);
    m.def("swap_out", &swap_out, py::arg("reserve_in"), py::arg("reserve_out"),
          py::arg("amount_in"), py::arg("fee") = 0.003);
    m.def("impermanent_loss", &impermanent_loss);

    // risk
    py::class_<VaRResult>(m, "VaRResult")
        .def_readonly("var", &VaRResult::var)
        .def_readonly("es", &VaRResult::es);
    m.def("historical_var", &historical_var, py::arg("returns"),
          py::arg("confidence") = 0.99);
    m.def("ewma_vol", &ewma_vol, py::arg("returns"), py::arg("lambda") = 0.94);
    m.def("annualise_vol", &annualise_vol, py::arg("sigma_per_bar"),
          py::arg("bars_per_day") = 1.0);
}
