// GARCH(1,1) maximum-likelihood estimation.
//
// This is the one genuinely CPU-bound path left in the Python risk stack, and
// it is CPU-bound for a structural reason rather than an accidental one:
//
//     sigma2_t = omega + alpha * r2_{t-1} + beta * sigma2_{t-1}
//
// Each variance depends on the PREVIOUS variance, so the recursion is strictly
// sequential and numpy cannot vectorise it. Fitting by grid-refined MLE runs
// that recursion once per grid node -- 15 alpha x 17 beta = 255 evaluations
// over the whole sample, roughly 191,000 interpreter-level iterations per
// call, which measured at 63ms in Python and dominated every endpoint that
// reported conditional volatility.
//
// The grid itself is embarrassingly parallel: the nodes are independent, so
// they are split across threads while each individual recursion stays serial.
// Determinism is preserved because the reduction picks the minimum by (value,
// alpha, beta) rather than by completion order -- a thread race must not be
// able to change which parameters are reported.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <thread>
#include <vector>

namespace quant {

struct GarchResult {
  double omega{};
  double alpha{};
  double beta{};
  double persistence{};
  double long_run_var{};
  double current_var{};
  double log_likelihood{};
  bool converged{false};
  int evaluations{};
};

namespace detail {

// Negative log-likelihood of one (alpha, beta) node. Variance targeting fixes
// omega from the sample variance, which removes a parameter from the search
// and guarantees the fitted long-run variance matches the data.
inline double garch_nll(const double* r, std::size_t n, double uncond,
                        double a, double b) {
  const double w = uncond * (1.0 - a - b);
  if (w <= 0.0) return std::numeric_limits<double>::max();
  double s2 = uncond;
  double ll = 0.0;
  for (std::size_t i = 0; i < n; ++i) {
    const double x2 = r[i] * r[i];
    s2 = w + a * x2 + b * s2;
    if (s2 <= 0.0 || !std::isfinite(s2)) return std::numeric_limits<double>::max();
    ll += std::log(s2) + x2 / s2;
  }
  return 0.5 * ll;
}

struct Node {
  double nll{std::numeric_limits<double>::max()};
  double a{}, b{};
};

// Deterministic tie-break: identical likelihoods must resolve to the same
// parameters regardless of which thread finished first.
inline bool better(const Node& x, const Node& y) {
  if (x.nll != y.nll) return x.nll < y.nll;
  if (x.a != y.a) return x.a < y.a;
  return x.b < y.b;
}

}  // namespace detail

// `n_threads = 0` uses all available cores.
inline GarchResult garch11(const double* returns, std::size_t n,
                           int n_alpha = 15, int n_beta = 17,
                           double a_lo = 0.01, double a_hi = 0.30,
                           double b_lo = 0.50, double b_hi = 0.98,
                           int n_threads = 0) {
  GarchResult out;
  if (n < 100) return out;

  // Work on the demeaned series: GARCH models the variance process, so a
  // non-zero mean would leak into the squared-return term as bias.
  std::vector<double> r(returns, returns + n);
  double mean = 0.0;
  for (double v : r) mean += v;
  mean /= static_cast<double>(n);
  for (double& v : r) v -= mean;

  double uncond = 0.0;
  for (double v : r) uncond += v * v;
  uncond /= static_cast<double>(n - 1);
  if (uncond <= 0.0) return out;

  const int total = n_alpha * n_beta;
  unsigned hw = std::thread::hardware_concurrency();
  if (hw == 0) hw = 1;
  int threads = n_threads > 0 ? n_threads : static_cast<int>(hw);
  threads = std::max(1, std::min(threads, total));

  std::vector<detail::Node> best(static_cast<std::size_t>(threads));

  auto worker = [&](int tid) {
    detail::Node local;
    for (int idx = tid; idx < total; idx += threads) {
      const int ia = idx / n_beta;
      const int ib = idx % n_beta;
      const double a = a_lo + (n_alpha > 1 ? (a_hi - a_lo) * ia / (n_alpha - 1) : 0.0);
      const double b = b_lo + (n_beta > 1 ? (b_hi - b_lo) * ib / (n_beta - 1) : 0.0);
      if (a + b >= 0.999) continue;   // stationarity guard
      const double v = detail::garch_nll(r.data(), n, uncond, a, b);
      detail::Node cand{v, a, b};
      if (detail::better(cand, local)) local = cand;
    }
    best[static_cast<std::size_t>(tid)] = local;
  };

  if (threads == 1) {
    worker(0);
  } else {
    std::vector<std::thread> pool;
    pool.reserve(static_cast<std::size_t>(threads));
    for (int t = 0; t < threads; ++t) pool.emplace_back(worker, t);
    for (auto& th : pool) th.join();
  }

  detail::Node win;
  for (const auto& nd : best)
    if (detail::better(nd, win)) win = nd;
  if (win.nll >= std::numeric_limits<double>::max()) return out;

  const double w = uncond * (1.0 - win.a - win.b);
  double s2 = uncond;
  for (std::size_t i = 0; i < n; ++i) s2 = w + win.a * r[i] * r[i] + win.b * s2;

  out.omega = w;
  out.alpha = win.a;
  out.beta = win.b;
  out.persistence = win.a + win.b;
  out.long_run_var = uncond;
  out.current_var = s2;
  out.log_likelihood = -win.nll;
  out.converged = true;
  out.evaluations = total;
  return out;
}

}  // namespace quant
