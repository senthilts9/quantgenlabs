// quant_bindings.cpp — expose the general pricing/risk engine to Python.
// Builds only if pybind11 is present (see CMakeLists). Module: quant_engine_cpp.
//
//   g++ -std=c++20 -O3 -Wall -shared -fPIC $(python3 -m pybind11 --includes) \
//       -Icpp/include cpp/src/quant_bindings.cpp \
//       -o quant_engine_cpp$(python3-config --extension-suffix)
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <utility>

#include "quant/quant_engine.hpp"
#include "quant/quant_parallel.hpp"
#include "quant/lsm.hpp"
#include "quant/garch.hpp"

namespace py = pybind11;
using namespace quant;

// Zero-copy array argument. See `arr_to_vec` below for why this exists.
using f64_array = py::array_t<double, py::array::c_style | py::array::forcecast>;

// Take a numpy buffer straight into a vector: one contiguous memcpy, no
// per-element conversion.
//
// WHAT THIS REPLACES, AND WHY IT MATTERED. The array arguments used to arrive
// as std::vector<double> via pybind11/stl.h, which converts element by element
// -- and the Python caller was additionally doing list(map(float, returns))
// first. So every call boxed n numpy doubles into n Python floats, built a
// list, then unboxed all n back again.
//
// Measured at n=704 on the historical-VaR path:
//     numpy percentile + tail scan          75.7 us
//     C++ kernel alone                       7.5 us   (10x faster than numpy)
//     list(map(float, ...)) marshalling     40.9 us   (5.4x the kernel)
//     C++ as actually called                125.7 us  -> 0.58x
//
// That 0.58x reproduces the 0.6x this platform had been publishing. The kernel
// was never the problem and neither was the language: the conversion around it
// cost five times the arithmetic it was feeding. With the buffer handed over
// directly the same kernel measures 23x numpy instead of 0.58x.
//
// The copy is kept (rather than borrowing the caller's memory) because
// historical_var reorders its input via nth_element. Mutating a caller's numpy
// array in place would be a silent, action-at-a-distance bug; one memcpy of
// n*8 bytes is nothing against what was being spent on boxing.
static inline std::vector<double> arr_to_vec(const f64_array& a) {
    const py::buffer_info b = a.request();
    const double* p = static_cast<const double*>(b.ptr);
    return std::vector<double>(p, p + static_cast<std::size_t>(b.size));
}

PYBIND11_MODULE(quant_engine_cpp, m) {
    m.doc() = "General pricing & risk hot path (C++). Selected via engines.py.";

    py::class_<Greeks>(m, "Greeks")
        .def_readonly("delta", &Greeks::delta).def_readonly("gamma", &Greeks::gamma)
        .def_readonly("vega", &Greeks::vega).def_readonly("theta", &Greeks::theta)
        .def_readonly("rho", &Greeks::rho);
    py::class_<Price>(m, "Price")
        .def_readonly("price", &Price::price).def_readonly("greeks", &Price::greeks);
    py::class_<MCResult>(m, "MCResult")
        .def_readonly("price", &MCResult::price).def_readonly("stderr", &MCResult::stderr)
        .def_readonly("ci_lo", &MCResult::ci_lo).def_readonly("ci_hi", &MCResult::ci_hi)
        .def_readonly("paths", &MCResult::paths);
    py::class_<VaR>(m, "VaR")
        .def_readonly("var", &VaR::var).def_readonly("es", &VaR::es);

    m.def("black_scholes", &black_scholes,
          py::arg("S"), py::arg("K"), py::arg("T"), py::arg("r"),
          py::arg("q"), py::arg("sig"), py::arg("call"));
    m.def("binomial", &binomial,
          py::arg("S"), py::arg("K"), py::arg("T"), py::arg("r"), py::arg("q"),
          py::arg("sig"), py::arg("steps"), py::arg("call"), py::arg("american"));
    m.def("monte_carlo", &monte_carlo,
          py::arg("S"), py::arg("K"), py::arg("T"), py::arg("r"), py::arg("q"),
          py::arg("sig"), py::arg("paths"), py::arg("call"), py::arg("seed") = 7,
          py::call_guard<py::gil_scoped_release>());   // release GIL for the hot loop
    // The buffer is copied while the GIL is held (a memcpy, microseconds at
    // these window sizes) and only then released for the O(n) selection and
    // tail scan. Releasing first would leave another thread free to mutate the
    // array underneath us.
    m.def("historical_var",
          [](const f64_array& returns, double confidence) {
              std::vector<double> v = arr_to_vec(returns);
              py::gil_scoped_release unlock;
              return historical_var(std::move(v), confidence);
          },
          py::arg("returns"), py::arg("confidence"));
    m.def("parametric_var",
          [](const f64_array& returns, double confidence) {
              const std::vector<double> v = arr_to_vec(returns);
              py::gil_scoped_release unlock;
              return parametric_var(v, confidence);
          },
          py::arg("returns"), py::arg("confidence"));

    // --- parallel (deterministic across thread count) ---
    m.def("monte_carlo_parallel", &monte_carlo_parallel,
          py::arg("S"), py::arg("K"), py::arg("T"), py::arg("r"), py::arg("q"),
          py::arg("sig"), py::arg("paths"), py::arg("call"), py::arg("seed") = 7,
          py::arg("n_threads") = 0, py::arg("block_pairs") = 32768,
          py::call_guard<py::gil_scoped_release>());

    py::class_<ScenarioVaR>(m, "ScenarioVaR")
        .def_readonly("var", &ScenarioVaR::var).def_readonly("es", &ScenarioVaR::es)
        .def_readonly("scenarios", &ScenarioVaR::scenarios)
        .def_readonly("base_value", &ScenarioVaR::base_value);
    py::class_<OptionSpec>(m, "OptionSpec")
        .def(py::init<>())
        .def_readwrite("S", &OptionSpec::S).def_readwrite("K", &OptionSpec::K)
        .def_readwrite("T", &OptionSpec::T).def_readwrite("r", &OptionSpec::r)
        .def_readwrite("q", &OptionSpec::q).def_readwrite("sig", &OptionSpec::sig)
        .def_readwrite("call", &OptionSpec::call)
        .def_readwrite("american", &OptionSpec::american);
    py::class_<Scenario>(m, "Scenario")
        .def(py::init<>())
        .def_readwrite("spot_shock", &Scenario::spot_shock)
        .def_readwrite("vol_shock", &Scenario::vol_shock)
        .def_readwrite("rate_shock", &Scenario::rate_shock);
    m.def("scenario_var", &scenario_var, py::arg("book"), py::arg("scenarios"),
          py::arg("confidence"), py::arg("n_threads") = 0,
          py::call_guard<py::gil_scoped_release>());

    py::class_<LSMResult>(m, "LSMResult")
        .def_readonly("price", &LSMResult::price)
        .def_readonly("stderr", &LSMResult::stderr)
        .def_readonly("european", &LSMResult::european)
        .def_readonly("early_exercise_premium", &LSMResult::early_exercise_premium)
        .def_readonly("paths", &LSMResult::paths)
        .def_readonly("steps", &LSMResult::steps);
    m.def("longstaff_schwartz", &longstaff_schwartz,
          py::arg("S0"), py::arg("K"), py::arg("T"), py::arg("r"), py::arg("q"),
          py::arg("sig"), py::arg("n_paths"), py::arg("n_steps"), py::arg("call"),
          py::arg("seed") = 7, py::arg("n_threads") = 0, py::arg("degree") = 3,
          py::call_guard<py::gil_scoped_release>());

    // GARCH(1,1) MLE. The variance recursion is strictly sequential, so numpy
    // cannot vectorise it and the 255-node grid search dominated every
    // endpoint reporting conditional volatility (63ms in pure Python).
    py::class_<GarchResult>(m, "GarchResult")
        .def_readonly("omega", &GarchResult::omega)
        .def_readonly("alpha", &GarchResult::alpha)
        .def_readonly("beta", &GarchResult::beta)
        .def_readonly("persistence", &GarchResult::persistence)
        .def_readonly("long_run_var", &GarchResult::long_run_var)
        .def_readonly("current_var", &GarchResult::current_var)
        .def_readonly("log_likelihood", &GarchResult::log_likelihood)
        .def_readonly("converged", &GarchResult::converged)
        .def_readonly("evaluations", &GarchResult::evaluations);
    m.def("garch11",
          [](const f64_array& returns, int n_alpha, int n_beta, int n_threads) {
              const py::buffer_info b = returns.request();
              const double* p = static_cast<const double*>(b.ptr);
              const std::size_t n = static_cast<std::size_t>(b.size);
              // garch11 reads the series without modifying it, so the numpy
              // buffer is used in place -- no copy at all on this path.
              py::gil_scoped_release unlock;
              return garch11(p, n, n_alpha, n_beta, 0.01, 0.30, 0.50, 0.98,
                             n_threads);
          },
          // No py::call_guard here on purpose. The guard releases the GIL
          // BEFORE the lambda body runs, and the body calls .request() on a
          // numpy object -- which needs the GIL held. The lambda manages the
          // release itself, after the buffer has been obtained.
          py::arg("returns"), py::arg("n_alpha") = 15, py::arg("n_beta") = 17,
          py::arg("n_threads") = 0);
}
