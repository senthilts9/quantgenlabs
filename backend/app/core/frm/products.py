"""FRM Part I — Financial Markets and Products.

Hedging, forwards and futures pricing, FRAs, parity relations, option bounds,
swaps, MBS prepayment, insurance ratios.

The hedging block is the part worth reading. A hedge ratio is not "sell the
same notional": it is the ratio that MINIMISES VARIANCE of the combined
position, h = rho * (sigma_S / sigma_F). Hedging one-for-one when the hedge
instrument is more volatile than the exposure over-hedges and can add risk --
the position ends up short basis rather than flat.

Cost-of-carry appears once, in `forward_price`, with each carry component as
an argument rather than as five near-identical functions. Income, yield,
storage, lease rate and convenience yield all enter the same relation; only
their sign differs, and writing them separately is how they end up with
inconsistent conventions.
"""
from __future__ import annotations

import math

import numpy as np


# ---- hedging ------------------------------------------------------------
def minimum_variance_hedge(sigma_spot: float, sigma_futures: float,
                           correlation: float, position_size: float = 0.0,
                           contract_size: float = 1.0) -> dict:
    """h* = rho * sigma_S / sigma_F, and the contract count it implies.

    Hedge effectiveness is rho^2 -- the proportion of variance removed. A
    correlation of 0.7 sounds strong and removes less than half the variance,
    which is the number that actually matters to the residual risk.
    """
    if sigma_futures <= 0:
        return {"error": "futures volatility must be positive"}
    if not -1 <= correlation <= 1:
        return {"error": "correlation must lie in [-1, 1]"}
    h = correlation * sigma_spot / sigma_futures
    out = {"hedge_ratio": round(h, 6),
           "hedge_effectiveness_r2": round(correlation ** 2, 6),
           "variance_reduction_pct": round(correlation ** 2 * 100, 2),
           "residual_risk_pct_of_unhedged": round(
               math.sqrt(max(0.0, 1 - correlation ** 2)) * 100, 2)}
    if position_size and contract_size:
        n = h * position_size / contract_size
        out.update({"contracts_exact": round(n, 4),
                    "contracts_rounded": int(round(n)),
                    "note": "negative = short the futures to hedge a long exposure"})
    return out


def beta_hedge(portfolio_value: float, current_beta: float, target_beta: float,
               futures_price: float, contract_multiplier: float = 250.0) -> dict:
    """N = (target beta - current beta) * V / (F * multiplier).

    Setting target_beta = 0 gives the full systematic hedge. It removes MARKET
    risk only: the residual is the idiosyncratic component, which is exactly
    the risk a stock-index future cannot touch.
    """
    denom = futures_price * contract_multiplier
    if denom <= 0:
        return {"error": "futures price and multiplier must be positive"}
    n = (target_beta - current_beta) * portfolio_value / denom
    return {"contracts_exact": round(n, 4), "contracts_rounded": int(round(n)),
            "direction": "sell" if n < 0 else "buy",
            "current_beta": current_beta, "target_beta": target_beta,
            "notional_per_contract": round(denom, 2),
            "hedged_notional": round(abs(n) * denom, 2),
            "note": "removes systematic risk only; idiosyncratic risk remains"}


def duration_hedge(portfolio_value: float, portfolio_duration: float,
                   futures_price: float, futures_duration: float,
                   contract_multiplier: float = 1000.0) -> dict:
    """N = -(V_P * D_P) / (V_F * D_F). Immunises against a small PARALLEL shift.

    The qualifier matters: this neutralises the first-order response to a
    level move and does nothing for a twist or a butterfly. A duration-hedged
    book can still lose materially when the curve reshapes, which is why key
    rate durations exist alongside it.
    """
    vf = futures_price * contract_multiplier
    if vf <= 0 or futures_duration <= 0:
        return {"error": "futures price, multiplier and duration must be positive"}
    n = -(portfolio_value * portfolio_duration) / (vf * futures_duration)
    return {"contracts_exact": round(n, 4), "contracts_rounded": int(round(n)),
            "direction": "sell" if n < 0 else "buy",
            "portfolio_dv01": round(portfolio_value * portfolio_duration * 1e-4, 2),
            "futures_dv01_per_contract": round(vf * futures_duration * 1e-4, 2),
            "note": "immunises a small PARALLEL shift only — no protection "
                    "against curve twist or butterfly"}


# ---- forwards and futures ----------------------------------------------
def forward_price(spot: float, rate: float, years: float,
                  income_pv: float = 0.0, yield_rate: float = 0.0,
                  storage_pv: float = 0.0, storage_rate: float = 0.0,
                  convenience_yield: float = 0.0,
                  continuous: bool = False) -> dict:
    """Cost of carry: F = (S + PV(storage) - PV(income)) * carry.

    One relation, every variant. Income and convenience yield REDUCE the
    forward (holding the asset pays you); storage and financing RAISE it.
    Getting a sign wrong here is the standard way a carry trade looks
    profitable on paper and is not.
    """
    if spot <= 0 or years < 0:
        return {"error": "spot must be positive and horizon non-negative"}
    base = spot + storage_pv - income_pv
    net = rate + storage_rate - yield_rate - convenience_yield
    if continuous:
        f = base * math.exp(net * years)
    else:
        f = base * (1 + net) ** years
    basis = f - spot
    return {
        "forward_price": round(f, 8),
        "spot": spot,
        "net_carry_rate_pct": round(net * 100, 5),
        "compounding": "continuous" if continuous else "discrete",
        "basis": round(basis, 8),
        "market_shape": "contango" if f > spot else "backwardation" if f < spot else "flat",
        "interpretation": (
            "contango — forward above spot; carry costs exceed benefits of holding"
            if f > spot else
            "backwardation — forward below spot; a benefit to holding the physical "
            "(convenience yield or scarcity) exceeds carry costs"),
    }


def basis_analysis(spot: float, futures: float, days_to_expiry: int = 0) -> dict:
    """Basis = spot - futures. It must converge to zero at expiry.

    Basis risk is what remains after a hedge: the hedge is only as good as the
    stability of this number, and a "perfect" hedge on a mismatched underlying
    simply exchanges price risk for basis risk.
    """
    b = spot - futures
    return {"basis": round(b, 8),
            "basis_pct_of_spot": round(b / spot * 100, 5) if spot else None,
            "market_shape": "backwardation" if b > 0 else "contango" if b < 0 else "flat",
            "days_to_expiry": days_to_expiry,
            "annualised_basis_pct": round(b / spot * 365 / days_to_expiry * 100, 4)
                                    if spot and days_to_expiry > 0 else None,
            "note": "basis converges to zero at delivery; until then it is the "
                    "residual risk a hedger retains"}


def forward_rate(spot_rates: dict[float, float], t1: float, t2: float,
                 continuous: bool = True) -> dict:
    """Implied forward between t1 and t2 from the spot curve."""
    if t2 <= t1:
        return {"error": "t2 must exceed t1"}
    r1, r2 = spot_rates.get(t1), spot_rates.get(t2)
    if r1 is None or r2 is None:
        return {"error": f"spot rates needed at both {t1} and {t2}"}
    if continuous:
        f = (r2 * t2 - r1 * t1) / (t2 - t1)
    else:
        f = (((1 + r2) ** t2 / (1 + r1) ** t1) ** (1 / (t2 - t1))) - 1
    return {"forward_rate_pct": round(f * 100, 6), "t1": t1, "t2": t2,
            "spot_t1_pct": round(r1 * 100, 6), "spot_t2_pct": round(r2 * 100, 6),
            "compounding": "continuous" if continuous else "discrete",
            "curve_shape": "upward" if r2 > r1 else "inverted" if r2 < r1 else "flat"}


def fra_payoff(notional: float, fixed_rate: float, floating_rate: float,
               t1: float, t2: float, discount_rate: float | None = None) -> dict:
    """FRA cash flow at T2 = L * (R - RK) * (T2 - T1) to the fixed-rate PAYER.

    The settlement is discounted back to T1 because that is when an FRA
    actually settles, even though the rate applies over T1..T2.
    """
    tau = t2 - t1
    if tau <= 0:
        return {"error": "t2 must exceed t1"}
    cf = notional * (floating_rate - fixed_rate) * tau
    dr = discount_rate if discount_rate is not None else floating_rate
    pv = cf / (1 + dr * tau)
    return {"cash_flow_at_t2": round(cf, 4),
            "settlement_at_t1": round(pv, 4),
            "accrual_years": round(tau, 6),
            "to_party": "fixed-rate payer" if cf >= 0 else "fixed-rate receiver",
            "note": "positive means the floating rate set above the contract rate, "
                    "so the fixed payer gains"}


def cheapest_to_deliver(bonds: list[dict], quoted_futures_price: float) -> dict:
    """CTD minimises (quoted bond price - futures price x conversion factor).

    The short chooses, so the delivery option always favours them. Pricing a
    bond future off any bond other than the CTD systematically misprices it.
    """
    rows = []
    for b in bonds:
        qbp, cf = float(b["quoted_price"]), float(b["conversion_factor"])
        cost = qbp - quoted_futures_price * cf
        rows.append({"bond": b.get("name", "?"), "quoted_price": qbp,
                     "conversion_factor": cf,
                     "cash_received": round(quoted_futures_price * cf, 6),
                     "delivery_cost": round(cost, 6)})
    if not rows:
        return {"error": "no bonds supplied"}
    rows.sort(key=lambda r: r["delivery_cost"])
    return {"cheapest_to_deliver": rows[0]["bond"],
            "delivery_cost": rows[0]["delivery_cost"],
            "candidates": rows,
            "note": "the SHORT selects delivery, so the cheapest bond is "
                    "delivered and the future prices off it"}


# ---- parity relations ---------------------------------------------------
def put_call_parity(call: float | None, put: float | None, spot: float,
                    strike: float, rate: float, years: float,
                    dividend_yield: float = 0.0) -> dict:
    """c + PV(X) = p + S. Any one leg is implied by the other three.

    Parity is an ARBITRAGE identity, not a model: it holds regardless of how
    the option is priced. A violation is therefore either a data error or a
    trade, and checking it is the cheapest sanity test on an option book.
    """
    pv_x = strike * math.exp(-rate * years)
    s_adj = spot * math.exp(-dividend_yield * years)
    out = {"pv_strike": round(pv_x, 8), "dividend_adjusted_spot": round(s_adj, 8)}
    if call is not None and put is None:
        out["implied_put"] = round(call + pv_x - s_adj, 8)
    elif put is not None and call is None:
        out["implied_call"] = round(put + s_adj - pv_x, 8)
    elif call is not None and put is not None:
        lhs, rhs = call + pv_x, put + s_adj
        out.update({"lhs_call_plus_pv_strike": round(lhs, 8),
                    "rhs_put_plus_spot": round(rhs, 8),
                    "violation": round(lhs - rhs, 8),
                    "arbitrage_present": bool(abs(lhs - rhs) > 1e-6),
                    "action": ("no arbitrage" if abs(lhs - rhs) < 1e-6 else
                               "sell the expensive side, buy the cheap side, "
                               "finance at the risk-free rate")})
    else:
        out["error"] = "supply at least one of call or put"
    return out


def option_bounds(spot: float, strike: float, rate: float, years: float,
                  dividend_yield: float = 0.0) -> dict:
    """No-arbitrage bounds. A quote outside these is a free lunch, not a view."""
    pv_x = strike * math.exp(-rate * years)
    s_adj = spot * math.exp(-dividend_yield * years)
    return {
        "european_call": {"lower": round(max(s_adj - pv_x, 0.0), 8), "upper": round(spot, 8)},
        "european_put": {"lower": round(max(pv_x - s_adj, 0.0), 8), "upper": round(pv_x, 8)},
        "american_call": {"lower": round(max(s_adj - pv_x, spot - strike, 0.0), 8),
                          "upper": round(spot, 8)},
        "american_put": {"lower": round(max(strike - spot, 0.0), 8),
                         "upper": round(strike, 8)},
        "early_exercise": (
            "An American call on a NON-dividend-paying stock is never optimally "
            "exercised early — the lower bound already exceeds intrinsic value, so "
            "selling the option beats exercising it. American puts CAN be optimal "
            "to exercise early when deep in the money, because the strike received "
            "can then earn interest."),
    }


def fx_parity(spot: float, rate_domestic: float, rate_foreign: float,
              years: float = 1.0, inflation_domestic: float | None = None,
              inflation_foreign: float | None = None) -> dict:
    """Covered interest rate parity, and PPP when inflation is supplied.

    Quoted as DOMESTIC per FOREIGN. The currency with the HIGHER interest rate
    trades at a forward DISCOUNT -- if it did not, borrowing the low-rate
    currency and lending the high-rate one would be riskless profit.
    """
    if spot <= 0:
        return {"error": "spot must be positive"}
    fwd = spot * ((1 + rate_domestic) / (1 + rate_foreign)) ** years
    out = {"spot": spot, "forward": round(fwd, 8),
           "forward_premium_pct": round((fwd / spot - 1) * 100, 5),
           "rate_domestic_pct": round(rate_domestic * 100, 4),
           "rate_foreign_pct": round(rate_foreign * 100, 4),
           "interpretation": (
               "foreign currency at a forward premium (its rate is lower)"
               if fwd > spot else
               "foreign currency at a forward discount (its rate is higher)")}
    if inflation_domestic is not None and inflation_foreign is not None:
        exp_chg = inflation_domestic - inflation_foreign
        out["ppp"] = {
            "expected_spot_change_pct": round(exp_chg * 100, 5),
            "ppp_implied_spot": round(spot * (1 + exp_chg) ** years, 8),
            "note": "PPP is a long-run relation; it fails badly over horizons "
                    "shorter than several years",
        }
    return out


# ---- swaps --------------------------------------------------------------
def swap_value(notional: float, fixed_rate: float, discount_rates: dict[float, float],
               floating_rate_next: float, next_payment_time: float,
               pay_fixed: bool = True) -> dict:
    """Bond method: V = B_float - B_fixed for a fixed payer.

    The floating leg prices to par at each reset, so its value collapses to
    the next known coupon plus notional discounted to today -- which is why
    a swap is worth zero at inception and only accrues value as rates move.
    """
    times = sorted(discount_rates)
    if not times:
        return {"error": "discount curve required"}
    b_fixed = 0.0
    prev = 0.0
    for t in times:
        df = math.exp(-discount_rates[t] * t)
        b_fixed += notional * fixed_rate * (t - prev) * df
        prev = t
    b_fixed += notional * math.exp(-discount_rates[times[-1]] * times[-1])

    r_next = discount_rates.get(next_payment_time, discount_rates[times[0]])
    df_next = math.exp(-r_next * next_payment_time)
    b_float = (notional + notional * floating_rate_next * next_payment_time) * df_next

    v = (b_float - b_fixed) if pay_fixed else (b_fixed - b_float)
    return {"value": round(v, 4),
            "fixed_leg_value": round(b_fixed, 4),
            "floating_leg_value": round(b_float, 4),
            "position": "pay fixed / receive floating" if pay_fixed
                        else "receive fixed / pay floating",
            "in_the_money": bool(v > 0),
            "credit_risk_note": ("this side has positive value, so it carries "
                                 "counterparty credit exposure; the other side "
                                 "carries none" if v > 0 else
                                 "this side is out of the money and carries no "
                                 "current counterparty exposure")}


def currency_swap_value(notional_domestic: float, notional_foreign: float,
                        bond_domestic: float, bond_foreign: float,
                        spot: float) -> dict:
    """V = B_domestic - S0 * B_foreign, both legs valued as bonds."""
    v = bond_domestic - spot * bond_foreign
    return {"value_domestic_ccy": round(v, 4),
            "domestic_leg": round(bond_domestic, 4),
            "foreign_leg_in_domestic": round(spot * bond_foreign, 4),
            "spot": spot,
            "notional_domestic": notional_domestic,
            "notional_foreign": notional_foreign,
            "note": "unlike an interest rate swap, principals ARE exchanged, so "
                    "a currency swap carries far larger credit exposure"}


# ---- MBS ----------------------------------------------------------------
def prepayment(cpr: float, months: int = 12, balance: float = 1_000_000.0) -> dict:
    """SMM = 1 - (1 - CPR)^(1/12), and the balance path it implies.

    CPR is ANNUAL, SMM is monthly; the conversion is geometric, not CPR/12.
    Using the arithmetic version overstates monthly prepayment and therefore
    understates the life of the pool.
    """
    if not 0 <= cpr < 1:
        return {"error": "CPR must be in [0, 1)"}
    smm = 1 - (1 - cpr) ** (1 / 12)
    path, bal = [], balance
    for m in range(1, months + 1):
        prepaid = bal * smm
        bal -= prepaid
        path.append({"month": m, "prepaid": round(prepaid, 2),
                     "ending_balance": round(bal, 2)})
    return {"cpr": cpr, "smm": round(smm, 8),
            "naive_cpr_over_12": round(cpr / 12, 8),
            "conversion_error_bp": round((cpr / 12 - smm) * 1e4, 2),
            "balance_after_period": round(bal, 2),
            "fraction_remaining": round(bal / balance, 6),
            "path": path[:12],
            "note": "prepayments accelerate when rates fall and borrowers "
                    "refinance — this is the negative convexity in an MBS"}


# ---- insurance ----------------------------------------------------------
def insurance_ratios(premiums: float, losses: float, expenses: float,
                     dividends: float = 0.0,
                     investment_income: float = 0.0) -> dict:
    """Loss, expense, combined, combined-after-dividends and operating ratios.

    A combined ratio above 100% means the underwriting book loses money. The
    OPERATING ratio subtracts investment income, and it is the one that says
    whether the insurer as a whole is profitable -- many insurers underwrite
    at a deliberate loss and make it back on the float.
    """
    if premiums <= 0:
        return {"error": "premiums must be positive"}
    loss_r = losses / premiums
    exp_r = expenses / premiums
    comb = loss_r + exp_r
    comb_div = comb + dividends / premiums
    op = comb_div - investment_income / premiums
    return {
        "loss_ratio_pct": round(loss_r * 100, 4),
        "expense_ratio_pct": round(exp_r * 100, 4),
        "combined_ratio_pct": round(comb * 100, 4),
        "combined_after_dividends_pct": round(comb_div * 100, 4),
        "operating_ratio_pct": round(op * 100, 4),
        "underwriting_profitable": bool(comb_div < 1),
        "overall_profitable": bool(op < 1),
        "interpretation": (
            f"underwriting {'profit' if comb_div < 1 else 'loss'} of "
            f"{abs(1 - comb_div) * 100:.2f}% of premiums; "
            f"after investment income the insurer is "
            f"{'profitable' if op < 1 else 'unprofitable'}"),
    }


def nav(assets: float, liabilities: float, shares: float) -> dict:
    """NAV = (assets - liabilities) / shares outstanding."""
    if shares <= 0:
        return {"error": "shares outstanding must be positive"}
    n = (assets - liabilities) / shares
    return {"nav_per_share": round(n, 6), "net_assets": round(assets - liabilities, 2),
            "shares": shares,
            "note": "open-end funds transact AT NAV; closed-end funds trade at a "
                    "premium or discount to it"}
