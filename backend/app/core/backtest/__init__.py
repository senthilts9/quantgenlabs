"""Event-driven basis backtester + tearsheet + live data adapters.

Data sources:
  * simulate()            offline, calibrated synthetic series
  * load_csv()            any CSV in the MarketData schema
  * xrpl_feed.XRPLFeed    real on-chain spot + AMM (public rippled JSON-RPC)
  * cefi_feed.perp_source real perp mark + funding (Binance USD-M) for the
                          derivatives leg XRPL lacks
"""
from . import cefi_feed, xrpl_feed
from .data import MarketData, load_csv, simulate
from .engine import BasisConfig, BacktestResult, run_basis_backtest
from .metrics import max_drawdown, print_tearsheet, tearsheet

__all__ = ["MarketData", "load_csv", "simulate", "BasisConfig", "BacktestResult",
           "run_basis_backtest", "tearsheet", "print_tearsheet", "max_drawdown",
           "xrpl_feed", "cefi_feed"]
