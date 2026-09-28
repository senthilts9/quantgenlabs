"""FRM Part I — Valuation and Risk Models.

Expected and unexpected loss, operational risk capital, the loss-distribution
approach, and delta/gamma hedging.

EL vs UL is the distinction the whole capital framework rests on. EXPECTED
loss is a cost of doing business: it is priced into the spread and provisioned
against. UNEXPECTED loss is the volatility around it, and that is what capital
exists to absorb. Reserving for EL and calling it risk management leaves the
firm undercapitalised for exactly the outcomes that matter.

UL is not EAD x PD x LGD scaled up. It requires the VARIANCE of both default
(Bernoulli, variance PD(1-PD)) and recovery, and the default-variance term
dominates for investment-grade exposures where PD is small.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats


# ---- credit -------------------------------------------------------------
def expected_loss(ead: float, pd: float, lgd: float,
                  lgd_volatility: float = 0.0) -> dict:
    """EL = EAD x PD x LGD, and UL from the variance of default and recovery.

        UL = EAD x sqrt( PD x sigma_LGD^2  +  LGD^2 x PD(1-PD) )

    The second term is default variance and is usually the larger one. For a
    1% PD with 45% LGD, UL is roughly nine times EL -- which is precisely why
    capital, not provisions, is what absorbs it.
    """
    if not 0 <= pd <= 1 or not 0 <= lgd <= 1:
        return {"error": "PD and LGD must be probabilities in [0, 1]"}
    el = ead * pd * lgd
    var_ul = pd * lgd_volatility ** 2 + lgd ** 2 * pd * (1 - pd)
    ul = ead * math.sqrt(max(var_ul, 0.0))
    return {
        "expected_loss": round(el, 4),
        "unexpected_loss": round(ul, 4),
        "ul_to_el_ratio": round(ul / el, 3) if el > 0 else None,
        "ead": ead, "pd": pd, "lgd": lgd, "lgd_volatility": lgd_volatility,
        "default_variance_term": round(lgd ** 2 * pd * (1 - pd), 10),
        "recovery_variance_term": round(pd * lgd_volatility ** 2, 10),
        "note": "EL is a cost priced into the spread and provisioned for; "
                "UL is the volatility around it and is what capital absorbs",
    }


def vasicek_capital(pd: float, lgd: float, ead: float, correlation: float = 0.15,
                    confidence: float = 0.999, maturity: float = 1.0) -> dict:
    """Basel IRB: conditional PD at a stressed systematic factor.

        PD_cond = N[ (N^-1(PD) + sqrt(R) N^-1(q)) / sqrt(1-R) ]

    Capital is charged on PD_cond MINUS PD, because EL is already provisioned.
    Charging the full conditional loss would double-count the expected part.
    """
    if not 0 < pd < 1:
        return {"error": "PD must be strictly between 0 and 1"}
    if not 0 <= correlation < 1:
        return {"error": "asset correlation must be in [0, 1)"}
    ppf, cdf = stats.norm.ppf, stats.norm.cdf
    pd_cond = float(cdf((ppf(pd) + math.sqrt(correlation) * ppf(confidence))
                        / math.sqrt(1 - correlation)))
    # Basel maturity adjustment
    b = (0.11852 - 0.05478 * math.log(pd)) ** 2
    ma = (1 + (maturity - 2.5) * b) / (1 - 1.5 * b)
    k = (pd_cond - pd) * lgd * ma
    return {
        "conditional_pd": round(pd_cond, 8),
        "unconditional_pd": pd,
        "stress_multiple": round(pd_cond / pd, 3),
        "capital_requirement_k": round(k, 8),
        "capital_amount": round(k * ead, 4),
        "expected_loss": round(pd * lgd * ead, 4),
        "maturity_adjustment": round(ma, 6),
        "asset_correlation": correlation, "confidence": confidence,
        "note": "capital covers PD_conditional MINUS PD; the expected part is "
                "already provisioned, so charging the full amount double-counts",
    }


# ---- operational risk ---------------------------------------------------
def operational_capital(gross_income: list[float] | None = None,
                        business_lines: dict[str, float] | None = None,
                        business_indicator: float | None = None,
                        loss_component: float | None = None) -> dict:
    """Basic Indicator, Standardized, and SMA operational capital.

    Only positive gross-income years count in the BIA average. Including a
    negative year would REDUCE required capital because the firm had a bad
    year, which is backwards.
    """
    out: dict = {}

    if gross_income:
        pos = [g for g in gross_income if g > 0]
        if pos:
            out["basic_indicator"] = {
                "alpha": 0.15,
                "average_positive_gross_income": round(float(np.mean(pos)), 2),
                "years_used": len(pos), "years_excluded": len(gross_income) - len(pos),
                "capital": round(0.15 * float(np.mean(pos)), 2),
                "note": "negative-income years are excluded, not averaged in",
            }

    if business_lines:
        # Basel II beta factors.
        betas = {"corporate_finance": 0.18, "trading_and_sales": 0.18,
                 "payment_and_settlement": 0.18, "commercial_banking": 0.15,
                 "agency_services": 0.15, "retail_banking": 0.12,
                 "asset_management": 0.12, "retail_brokerage": 0.12}
        rows, total = [], 0.0
        for line, gi in business_lines.items():
            beta = betas.get(line.lower().replace(" ", "_"), 0.15)
            cap = beta * gi
            total += cap
            rows.append({"line": line, "beta": beta, "gross_income": gi,
                         "capital": round(cap, 2)})
        out["standardized"] = {"by_line": rows, "capital": round(total, 2),
                               "note": "trading & sales carries the highest beta "
                                       "(18%) — it is judged the riskiest activity"}

    if business_indicator is not None:
        bi = business_indicator
        # Basel III SMA marginal buckets (EUR bn thresholds).
        if bi <= 1e9:
            bic = 0.12 * bi
        elif bi <= 30e9:
            bic = 0.12e9 + 0.15 * (bi - 1e9)
        else:
            bic = 0.12e9 + 0.15 * 29e9 + 0.18 * (bi - 30e9)
        ilm = 1.0
        if loss_component is not None and bic > 0:
            ilm = float(math.log(math.e - 1 + (loss_component / bic) ** 0.8))
        out["sma"] = {"business_indicator": bi,
                      "bi_component": round(bic, 2),
                      "loss_component": loss_component,
                      "internal_loss_multiplier": round(ilm, 6),
                      "capital": round(bic * ilm, 2),
                      "note": "ILM > 1 means the firm's own loss history is worse "
                              "than the standard assumption and it is charged more"}
    return out or {"error": "supply gross_income, business_lines or business_indicator"}


def loss_distribution(frequency_lambda: float, severity_mean_log: float,
                      severity_sigma_log: float, simulations: int = 50_000,
                      confidence: float = 0.999, seed: int = 11) -> dict:
    """Loss Distribution Approach: Poisson frequency x lognormal severity.

    Frequency and severity are modelled SEPARATELY and convolved by simulation
    because they are driven by different things -- how often control failures
    occur versus how bad each one is -- and the aggregate has no closed form.

    Lognormal severity is chosen for its right skew and fat tail: operational
    losses are dominated by a handful of extreme events, so the mean is a poor
    summary and the 99.9th percentile is the number capital is set against.
    """
    if frequency_lambda < 0 or severity_sigma_log <= 0:
        return {"error": "lambda must be non-negative and sigma positive"}
    rng = np.random.default_rng(seed)
    counts = rng.poisson(frequency_lambda, simulations)
    totals = np.zeros(simulations)
    nz = counts > 0
    for i in np.flatnonzero(nz):
        totals[i] = rng.lognormal(severity_mean_log, severity_sigma_log,
                                  counts[i]).sum()
    q = float(np.quantile(totals, confidence))
    tail = totals[totals >= q]
    mean = float(totals.mean())
    return {
        "simulations": simulations, "confidence": confidence,
        "expected_annual_loss": round(mean, 2),
        "median_annual_loss": round(float(np.median(totals)), 2),
        "operational_var": round(q, 2),
        "expected_shortfall": round(float(tail.mean()), 2) if len(tail) else None,
        "capital_charge": round(q - mean, 2),
        "max_simulated": round(float(totals.max()), 2),
        "zero_loss_years_pct": round(float((totals == 0).mean()) * 100, 2),
        "expected_frequency": frequency_lambda,
        "expected_severity": round(
            float(math.exp(severity_mean_log + severity_sigma_log ** 2 / 2)), 2),
        "note": "capital is set against the tail quantile MINUS expected loss; "
                "the mean is a poor summary of a right-skewed loss distribution",
    }


# ---- delta / gamma hedging ---------------------------------------------
def delta_hedge(option_delta: float, contracts: int, contract_size: int = 100,
                option_gamma: float = 0.0, spot: float = 0.0,
                move_pct: float = 1.0) -> dict:
    """Shares to hedge a written option book, and where the hedge fails.

    Delta hedging is a LOCAL approximation. Gamma measures how fast delta
    changes, so the re-hedge error over a finite move is roughly
    0.5 * gamma * (dS)^2 -- second order, invisible in the delta number, and
    the reason a delta-neutral book still loses money in a gap.
    """
    exposure = option_delta * contracts * contract_size
    out = {"option_delta": option_delta, "contracts": contracts,
           "position_delta": round(exposure, 4),
           "shares_to_trade": round(-exposure, 4),
           "action": "buy" if exposure < 0 else "sell",
           "note": "delta-neutral only for SMALL moves; it is a first-order hedge"}
    if option_gamma and spot > 0:
        ds = spot * move_pct / 100.0
        pos_gamma = option_gamma * contracts * contract_size
        err = 0.5 * pos_gamma * ds ** 2
        out.update({
            "position_gamma": round(pos_gamma, 6),
            "spot_move_modelled": round(ds, 6),
            "delta_after_move": round(exposure + pos_gamma * ds, 4),
            "unhedged_pnl_from_gamma": round(err, 4),
            "gamma_warning": (
                f"a {move_pct}% move changes position delta by "
                f"{pos_gamma * ds:,.2f} and leaves {err:,.2f} unhedged — "
                "short gamma loses on moves in EITHER direction"),
        })
    return out


def portfolio_insurance(portfolio_value: float, floor_value: float,
                        put_delta: float, index_level: float,
                        contract_multiplier: float = 250.0) -> dict:
    """Protective put sizing, and the synthetic-replication caveat.

    Replicating the put by trading the index dynamically requires SELLING into
    a falling market. In 1987 that mechanism ran into a market with no bids;
    the strategy is only as good as the liquidity assumed by its rebalancing.
    """
    if index_level <= 0 or portfolio_value <= 0:
        return {"error": "portfolio value and index level must be positive"}
    protect = max(0.0, portfolio_value - floor_value)
    contracts = portfolio_value / (index_level * contract_multiplier)
    return {"portfolio_value": portfolio_value, "floor_value": floor_value,
            "value_at_risk_below_floor": round(protect, 2),
            "protection_pct": round(floor_value / portfolio_value * 100, 3),
            "put_contracts_required": round(contracts, 4),
            "synthetic_shares_to_short": round(abs(put_delta) * portfolio_value
                                               / index_level, 2),
            "note": "synthetic replication sells into a falling market; that "
                    "feedback is what broke portfolio insurance in October 1987"}
