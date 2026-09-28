// crypto_engine.hpp — low-latency crypto quant/risk hot path.
//
// Header-only, dependency-free, C++20. Mirrors python/xquant/crypto/* so the
// same numbers are reproducible in the compiled path a real-time risk system
// runs per tick. The Python modules are the research/reference implementation;
// this is what you'd call inside the C++ tick loop (or via the pybind11
// bindings in crypto_bindings.cpp).
//
// Conventions match the Python side: linear (USDT-margined) perps, funding
// paid 3x/day, positive funding => longs pay shorts, 24/7 (365d) annualisation.
//
// Builds clean under: g++ -std=c++20 -O2 -Wall -Wextra
#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <numeric>
#include <optional>
#include <stdexcept>
#include <vector>

namespace xrpl::crypto {

// ----------------------------- perps ------------------------------------
inline constexpr int kFundingIntervalsPerDay = 3;      // 8h venues
inline constexpr double kDefaultClamp = 0.0005;        // ±0.05%/interval

// Exchange funding: F = premium + clamp(interest - premium, ±clamp).
inline double funding_rate(double premium_index,
                           double interest_rate = 0.0001,
                           double clamp = kDefaultClamp) noexcept {
    const double diff = std::clamp(interest_rate - premium_index, -clamp, clamp);
    return premium_index + diff;
}

inline double basis(double futures_price, double index_price) {
    if (index_price <= 0.0) throw std::invalid_argument("index_price>0");
    return futures_price / index_price - 1.0;
}

inline double annualised_funding(double rate_per_interval,
                                 int intervals_per_day = kFundingIntervalsPerDay) noexcept {
    return rate_per_interval * intervals_per_day * 365.0;
}

// Long's funding cash flow over a series (negative = paid out).
inline double funding_pnl(double position_notional,
                          const std::vector<double>& rates) noexcept {
    const double s = std::accumulate(rates.begin(), rates.end(), 0.0);
    return -position_notional * s;
}

// --------------------------- liquidation --------------------------------
struct MarginState {
    double liquidation_price;
    double bankruptcy_price;
    double margin_ratio;        // maintenance / equity; >=1 => liquidated
    double distance_to_liq;     // signed fraction from mark to liq
    double unrealised_pnl;
    double equity;
};

inline double liquidation_price(double entry, double leverage, bool is_long,
                                double mmr = 0.005) {
    if (leverage <= 0.0) throw std::invalid_argument("leverage>0");
    return is_long ? entry * (1.0 - 1.0 / leverage) / (1.0 - mmr)
                   : entry * (1.0 + 1.0 / leverage) / (1.0 + mmr);
}

inline double bankruptcy_price(double entry, double leverage, bool is_long) noexcept {
    return is_long ? entry * (1.0 - 1.0 / leverage)
                   : entry * (1.0 + 1.0 / leverage);
}

inline MarginState margin_state(double entry, double mark, double qty,
                                double leverage, bool is_long,
                                double mmr = 0.005) {
    const double side = is_long ? 1.0 : -1.0;
    const double q = std::abs(qty);
    const double initial_margin = q * entry / leverage;
    const double upnl = side * q * (mark - entry);
    const double equity = initial_margin + upnl;
    const double maint = mmr * q * mark;
    const double liq = liquidation_price(entry, leverage, is_long, mmr);
    return MarginState{
        liq, bankruptcy_price(entry, leverage, is_long),
        equity > 0.0 ? maint / equity : std::numeric_limits<double>::infinity(),
        (liq - mark) / mark, upnl, equity};
}

// Largest leverage keeping liq at least `buffer` away from entry.
inline double max_leverage_for_liq_buffer(bool is_long, double buffer,
                                          double mmr = 0.005) noexcept {
    if (is_long) {
        const double target = (1.0 - buffer) * (1.0 - mmr);
        return 1.0 / (1.0 - target);
    }
    const double target = (1.0 + buffer) * (1.0 + mmr);
    return 1.0 / (target - 1.0);
}

// ------------------------------- AMM ------------------------------------
struct SwapResult {
    double amount_out;
    double execution_price;
    double spot_price_before;
    double price_impact;        // fraction
    double fee_paid;
};

// Constant-product (x*y=k) swap with fee — Uniswap-v2 / XRPL-AMM math.
inline SwapResult swap_out(double reserve_in, double reserve_out,
                           double amount_in, double fee = 0.003) {
    if (amount_in <= 0.0) throw std::invalid_argument("amount_in>0");
    const double spot = reserve_out / reserve_in;
    const double ain_net = amount_in * (1.0 - fee);
    const double k = reserve_in * reserve_out;
    const double amount_out = reserve_out - k / (reserve_in + ain_net);
    const double exec = amount_out / amount_in;
    return SwapResult{amount_out, exec, spot, exec / spot - 1.0, amount_in * fee};
}

// Exact impermanent loss for a 50/50 pool: 2*sqrt(r)/(1+r) - 1, r = P_now/P_entry.
inline double impermanent_loss(double price_ratio) {
    if (price_ratio <= 0.0) throw std::invalid_argument("price_ratio>0");
    return 2.0 * std::sqrt(price_ratio) / (1.0 + price_ratio) - 1.0;
}

// ------------------------------ risk ------------------------------------
struct VaRResult { double var; double es; };

// Historical VaR/ES as positive losses at `confidence`. O(n log n).
// Hot-path note: for a fixed rolling window prefer a partial sort / heap;
// full sort kept here for clarity and exact parity with the Python.
inline VaRResult historical_var(std::vector<double> returns,
                                double confidence = 0.99) {
    if (returns.size() < 2) throw std::invalid_argument("need >=2 returns");
    std::sort(returns.begin(), returns.end());
    const double a = 1.0 - confidence;
    const double pos = a * static_cast<double>(returns.size() - 1);
    const std::size_t lo = static_cast<std::size_t>(std::floor(pos));
    const double frac = pos - static_cast<double>(lo);
    const double q = lo + 1 < returns.size()
                         ? returns[lo] + frac * (returns[lo + 1] - returns[lo])
                         : returns[lo];
    const double var = -q;
    double tail_sum = 0.0; std::size_t n = 0;
    for (double r : returns) if (r <= -var) { tail_sum += r; ++n; }
    const double es = n ? -tail_sum / static_cast<double>(n) : var;
    return VaRResult{var, es};
}

// RiskMetrics EWMA volatility (latest conditional sigma).
inline double ewma_vol(const std::vector<double>& returns, double lambda = 0.94) {
    const std::size_t n = returns.size();
    if (n == 0) return 0.0;
    const double mu = std::accumulate(returns.begin(), returns.end(), 0.0)
                      / static_cast<double>(n);
    double wsum = 0.0, acc = 0.0, w;
    for (std::size_t i = 0; i < n; ++i) {
        w = (1.0 - lambda) * std::pow(lambda, static_cast<double>(n - 1 - i));
        acc += w * (returns[i] - mu) * (returns[i] - mu);
        wsum += w;
    }
    return std::sqrt(acc / wsum);
}

inline double annualise_vol(double sigma_per_bar, double bars_per_day = 1.0) noexcept {
    return sigma_per_bar * std::sqrt(365.0 * bars_per_day);
}

}  // namespace xrpl::crypto
