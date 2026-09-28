"""Risk engine — VaR/ES measures and model backtesting."""
from .backtest import BacktestResult, backtest_var
from .var_es import (VaRResult, component_var, cornish_fisher, historical,
                     monte_carlo, parametric)

VAR_METHODS = {
    "historical": historical,
    "parametric": parametric,
    "cornish_fisher": cornish_fisher,
    "monte_carlo": monte_carlo,
}

__all__ = ["VaRResult", "BacktestResult", "backtest_var", "component_var",
           "historical", "parametric", "cornish_fisher", "monte_carlo",
           "VAR_METHODS"]
