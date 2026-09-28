"""FRM Part I & II API.

Where a measure can be computed on the desk's OWN book and real history, it
is -- the performance, beta and regression endpoints read the live OMS
positions and FRED series rather than accepting hand-fed numbers. The
remaining endpoints are parameterised calculators, because a swap valuation
or an insurance combined ratio has no counterpart in this book.
"""
from __future__ import annotations

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.frm import foundations as F
from ..core.frm import products as P
from ..core.frm import quant as Q
from ..core.frm import valuation as V

router = APIRouter(prefix="/api/frm", tags=["frm"])


def _book_returns(days: int = 500):
    """Portfolio return series and benchmark, from the live book."""
    from .portfolio import _book
    from ..core.portfolio.risk import align_on_dates

    exposures, history, dates, unmapped, _, _ = _book(days)
    if not exposures:
        return None, None, {"empty": True, "message": "no positions on the book",
                            "unmapped": unmapped}
    syms = [s for s in exposures if s in history and s in dates]
    if not syms:
        return None, None, {"empty": True, "message": "no mapped history",
                            "unmapped": unmapped}
    bench = "SP500"
    allsyms = syms + ([bench] if bench in history and bench not in syms else [])
    L, common = align_on_dates(history, dates, allsyms)
    if L.shape[0] < 40:
        return None, None, {"empty": True,
                            "message": f"only {L.shape[0]} overlapping dates; need 40+"}
    R = np.diff(L, axis=0) / L[:-1]
    w = np.array([exposures[s] for s in syms], float)
    gross = float(np.abs(w).sum())
    if gross == 0:
        return None, None, {"empty": True, "message": "book is flat"}
    # Weighted portfolio return, normalised by gross so it is a RETURN series
    # and not a currency P&L series -- Sharpe on currency P&L is not a ratio.
    port = (R[:, :len(syms)] @ w) / gross
    bench_r = R[:, allsyms.index(bench)] if bench in allsyms else None
    return port, bench_r, {"symbols": syms, "n": int(len(port)),
                           "dates": [common[1], common[-1]], "gross": gross}


@router.get("/catalogue")
def catalogue() -> dict:
    """Every implemented measure, so the coverage is inspectable."""
    return {
        "part_i": {
            "foundations": ["CAPM / Security Market Line", "beta with R²",
                            "Arbitrage Pricing Theory", "Fama-French 3-factor",
                            "Sharpe", "Sortino (with MAR)", "Treynor",
                            "Jensen's alpha", "Information Ratio",
                            "tracking error", "Calmar", "max drawdown"],
            "quantitative": ["Bayes' rule", "binomial", "Poisson", "uniform",
                             "t-test", "chi-squared (variance)", "F-test",
                             "confidence intervals", "OLS + TSS/ESS/RSS",
                             "R² and adjusted R²", "VIF (multicollinearity)",
                             "Breusch-Pagan (heteroskedasticity)",
                             "Spearman's rho", "Kendall's tau", "AR(1)",
                             "covariance stationarity", "trend models",
                             "seasonal dummies", "Monte Carlo standard error"],
            "products": ["minimum-variance hedge ratio", "hedge effectiveness",
                         "beta hedging with index futures",
                         "duration-based hedge ratio", "cost-of-carry forwards",
                         "basis / contango / backwardation", "forward rates",
                         "FRA payoff", "cheapest-to-deliver", "put-call parity",
                         "option no-arbitrage bounds", "interest rate parity",
                         "purchasing power parity", "swap valuation",
                         "currency swap valuation", "CPR / SMM prepayment",
                         "insurance loss/expense/combined/operating ratios", "NAV"],
            "valuation": ["expected loss", "unexpected loss",
                          "Vasicek conditional PD / Basel IRB capital",
                          "operational risk: BIA / Standardized / SMA",
                          "loss distribution approach (Poisson × lognormal)",
                          "delta hedging", "gamma re-hedge error",
                          "portfolio insurance"],
        },
        "part_ii": {
            "market_risk": ["historical / parametric / Cornish-Fisher / Monte Carlo VaR",
                            "expected shortfall", "component VaR (Euler)",
                            "EWMA (RiskMetrics λ=0.94)", "GARCH(1,1)",
                            "coherence / subadditivity check",
                            "liquidity-adjusted VaR", "Kupiec POF",
                            "Christoffersen independence & CC", "Basel traffic light",
                            "EVT peaks-over-threshold"],
            "endpoints": ["/api/portfolio/var-suite", "/api/portfolio/frm",
                          "/api/portfolio/stress", "/api/risk/var/backtest"],
        },
        "note": "measures marked live read the desk's own positions and FRED "
                "history; the rest are parameterised calculators",
    }


@router.get("/performance")
def performance(days: int = Query(500, ge=60, le=2000),
                rf_annual: float = Query(0.04, ge=0, le=0.25),
                mar_annual: float = Query(0.0, ge=-1, le=1)) -> dict:
    """Full performance suite on the LIVE book against the S&P 500."""
    port, bench, meta = _book_returns(days)
    if port is None:
        return meta
    res = F.performance_suite(port, bench, rf_annual, mar_annual)
    res["book"] = meta
    return res


@router.get("/beta")
def book_beta(days: int = Query(500, ge=60, le=2000)) -> dict:
    """Book beta vs the benchmark, with the R² that qualifies it."""
    port, bench, meta = _book_returns(days)
    if port is None:
        return meta
    if bench is None:
        raise HTTPException(400, "benchmark history unavailable")
    return {**F.beta(port, bench), "book": meta}


class CapmReq(BaseModel):
    beta: float
    rf_annual: float = 0.04
    market_return_annual: float = 0.09


@router.post("/capm")
def capm(req: CapmReq) -> dict:
    return F.capm(req.beta, req.rf_annual, req.market_return_annual)


class AptReq(BaseModel):
    rf_annual: float = 0.04
    betas: dict[str, float]
    premia: dict[str, float]


@router.post("/apt")
def apt(req: AptReq) -> dict:
    return F.apt(req.rf_annual, req.betas, req.premia)


@router.get("/regression")
def regression(days: int = Query(500, ge=60, le=2000)) -> dict:
    """Regress the book on the benchmark, with full OLS diagnostics."""
    port, bench, meta = _book_returns(days)
    if port is None:
        return meta
    if bench is None:
        raise HTTPException(400, "benchmark history unavailable")
    res = Q.ols(port, bench.reshape(-1, 1), ["benchmark"])
    res["book"] = meta
    return res


@router.get("/timeseries")
def timeseries(days: int = Query(500, ge=60, le=2000)) -> dict:
    """AR(1) and rank correlation on the book's own return series."""
    port, bench, meta = _book_returns(days)
    if port is None:
        return meta
    out = {"ar1_returns": Q.ar1(port), "book": meta}
    # AR(1) on the cumulative level as well: returns are near-white-noise by
    # construction, the LEVEL is where a unit root actually shows up.
    out["ar1_level"] = Q.ar1(np.cumprod(1.0 + port))
    if bench is not None:
        out["rank_correlation_vs_benchmark"] = Q.rank_correlations(port, bench)
    return out


class BayesReq(BaseModel):
    prior: float = Field(ge=0, le=1)
    sensitivity: float = Field(ge=0, le=1)
    false_positive: float = Field(ge=0, le=1)


@router.post("/bayes")
def bayes(req: BayesReq) -> dict:
    return Q.bayes(req.prior, req.sensitivity, req.false_positive)


@router.get("/distributions")
def distributions(n: int = 10, p: float = 0.2, lam: float = 3.0,
                  x: int = 2, a: float = 0.0, b: float = 1.0) -> dict:
    return {"binomial": Q.binomial(n, p, x), "poisson": Q.poisson(lam, x),
            "uniform": Q.uniform(a, b, a, (a + b) / 2)}


@router.get("/hypothesis-tests")
def hypothesis_tests(days: int = Query(500, ge=60, le=2000),
                     mu0: float = 0.0) -> dict:
    """Tests run on the book's real returns, not textbook samples."""
    port, bench, meta = _book_returns(days)
    if port is None:
        return meta
    out = {"t_test_mean_return": Q.t_test_mean(port, mu0),
           "chi_squared_variance": Q.chi_squared_variance(
               port, float(np.var(port, ddof=1)) * 1.5),
           "book": meta}
    if bench is not None:
        out["f_test_vs_benchmark"] = Q.f_test_variances(port, bench)
    return out


# ---- products: parameterised calculators --------------------------------
class HedgeReq(BaseModel):
    sigma_spot: float
    sigma_futures: float
    correlation: float
    position_size: float = 0.0
    contract_size: float = 1.0


@router.post("/hedge/minimum-variance")
def mv_hedge(req: HedgeReq) -> dict:
    return P.minimum_variance_hedge(req.sigma_spot, req.sigma_futures,
                                    req.correlation, req.position_size,
                                    req.contract_size)


class BetaHedgeReq(BaseModel):
    portfolio_value: float
    current_beta: float
    target_beta: float = 0.0
    futures_price: float
    contract_multiplier: float = 250.0


@router.post("/hedge/beta")
def beta_hedge(req: BetaHedgeReq) -> dict:
    return P.beta_hedge(req.portfolio_value, req.current_beta, req.target_beta,
                        req.futures_price, req.contract_multiplier)


class DurHedgeReq(BaseModel):
    portfolio_value: float
    portfolio_duration: float
    futures_price: float
    futures_duration: float
    contract_multiplier: float = 1000.0


@router.post("/hedge/duration")
def dur_hedge(req: DurHedgeReq) -> dict:
    return P.duration_hedge(req.portfolio_value, req.portfolio_duration,
                            req.futures_price, req.futures_duration,
                            req.contract_multiplier)


class ForwardReq(BaseModel):
    spot: float
    rate: float = 0.04
    years: float = 1.0
    income_pv: float = 0.0
    yield_rate: float = 0.0
    storage_pv: float = 0.0
    storage_rate: float = 0.0
    convenience_yield: float = 0.0
    continuous: bool = False


@router.post("/forward-price")
def forward_price(req: ForwardReq) -> dict:
    return P.forward_price(**req.model_dump())


class ParityReq(BaseModel):
    call: float | None = None
    put: float | None = None
    spot: float
    strike: float
    rate: float = 0.04
    years: float = 1.0
    dividend_yield: float = 0.0


@router.post("/put-call-parity")
def put_call_parity(req: ParityReq) -> dict:
    return {**P.put_call_parity(**req.model_dump()),
            "bounds": P.option_bounds(req.spot, req.strike, req.rate,
                                      req.years, req.dividend_yield)}


class FxParityReq(BaseModel):
    spot: float
    rate_domestic: float
    rate_foreign: float
    years: float = 1.0
    inflation_domestic: float | None = None
    inflation_foreign: float | None = None


@router.post("/fx-parity")
def fx_parity(req: FxParityReq) -> dict:
    return P.fx_parity(**req.model_dump())


class PrepayReq(BaseModel):
    cpr: float = Field(ge=0, lt=1)
    months: int = 12
    balance: float = 1_000_000.0


@router.post("/prepayment")
def prepayment(req: PrepayReq) -> dict:
    return P.prepayment(req.cpr, req.months, req.balance)


class InsuranceReq(BaseModel):
    premiums: float
    losses: float
    expenses: float
    dividends: float = 0.0
    investment_income: float = 0.0


@router.post("/insurance-ratios")
def insurance_ratios(req: InsuranceReq) -> dict:
    return P.insurance_ratios(**req.model_dump())


# ---- valuation ----------------------------------------------------------
class CreditReq(BaseModel):
    ead: float = 1_000_000.0
    pd: float = Field(0.02, gt=0, lt=1)
    lgd: float = Field(0.45, ge=0, le=1)
    lgd_volatility: float = 0.25
    correlation: float = Field(0.15, ge=0, lt=1)
    confidence: float = Field(0.999, gt=0.5, lt=1)
    maturity: float = 1.0


@router.post("/credit")
def credit(req: CreditReq) -> dict:
    """EL, UL and Basel IRB capital for one exposure."""
    return {"loss": V.expected_loss(req.ead, req.pd, req.lgd, req.lgd_volatility),
            "basel_irb": V.vasicek_capital(req.pd, req.lgd, req.ead,
                                           req.correlation, req.confidence,
                                           req.maturity)}


class OpRiskReq(BaseModel):
    gross_income: list[float] | None = None
    business_lines: dict[str, float] | None = None
    business_indicator: float | None = None
    loss_component: float | None = None
    frequency_lambda: float = 4.0
    severity_mean_log: float = 11.0
    severity_sigma_log: float = 1.4
    simulations: int = Field(50_000, ge=1000, le=200_000)


@router.post("/operational-risk")
def operational_risk(req: OpRiskReq) -> dict:
    """Regulatory capital approaches plus a loss-distribution simulation."""
    return {
        "regulatory": V.operational_capital(
            req.gross_income, req.business_lines,
            req.business_indicator, req.loss_component),
        "loss_distribution": V.loss_distribution(
            req.frequency_lambda, req.severity_mean_log,
            req.severity_sigma_log, req.simulations),
    }


class DeltaHedgeReq(BaseModel):
    option_delta: float
    contracts: int
    contract_size: int = 100
    option_gamma: float = 0.0
    spot: float = 0.0
    move_pct: float = 1.0


@router.post("/delta-hedge")
def delta_hedge(req: DeltaHedgeReq) -> dict:
    return V.delta_hedge(**req.model_dump())
