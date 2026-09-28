"""Market data adapters.

Traditional (equities indices, rates, FX) comes from keyless public
endpoints that work from cloud IPs; crypto books come from CCXT under
core/sor/ccxt_feed.py.
"""
from . import universe
from .board import market_board, marks
from .universe import UNIVERSE, Instrument, factor_map, resolve, series_map
from .traditional import (ecb_fx, fred_latest, fred_series, snapshot,
                          treasury_curve, treasury_history)

__all__ = ["treasury_curve", "fred_series", "fred_latest", "ecb_fx", "snapshot",
           "treasury_history", "universe", "UNIVERSE", "Instrument", "resolve",
           "factor_map", "series_map", "market_board", "marks"]
