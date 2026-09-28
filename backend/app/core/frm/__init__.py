"""FRM Part I & II measures.

Organised by exam part so a reader can find a formula where they expect it:

  foundations -- CAPM/SML, APT, Fama-French, beta, Sharpe/Sortino/Treynor/
                 Jensen/Information Ratio, tracking error.
  quant       -- Bayes, binomial/Poisson/uniform, t/chi-squared/F tests, OLS
                 with VIF and Breusch-Pagan, Spearman/Kendall, AR(1), trend
                 and seasonality, Monte Carlo standard error.
  products    -- minimum-variance and duration hedge ratios, beta hedging,
                 cost-of-carry forwards, basis, FRAs, put-call parity, option
                 bounds, IRP/PPP, swap valuation, CPR/SMM, insurance ratios.
  valuation   -- EL/UL, Vasicek-Basel IRB capital, operational risk (BIA,
                 Standardized, SMA), loss distribution approach, delta/gamma
                 hedging, portfolio insurance.

Part II market-risk measures (EWMA, GARCH, coherence, liquidity-adjusted VaR)
live in `core.portfolio.frm`, next to the VaR engines they qualify.
"""
from . import foundations, products, quant, valuation

__all__ = ["foundations", "quant", "products", "valuation"]
