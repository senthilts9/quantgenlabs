"""Smart Order Routing — cost-minimising multi-venue best execution.

Cross-asset: CLOB venues (equities/FX/futures/CEX) and AMM venues (on-chain
DEX) route through one marginal-cost optimiser, with an ML fill-probability
model discounting unreachable liquidity and a GenAI layer explaining routes.
"""
from .book import Side, VenueBook, VenueType, synth_market
from .ml import (MIN_TRAINING_ROWS, FillModel, fill_probabilities,
                 train_fill_model, train_from_observations, training_accuracy,
                 training_metrics)
from . import capture, ccxt_feed, explain, fade, registry
from .router import Fill, RoutingResult, route

__all__ = ["Side", "VenueType", "VenueBook", "synth_market", "route",
           "RoutingResult", "Fill", "FillModel", "train_fill_model",
           "fill_probabilities", "training_accuracy", "training_metrics",
           "train_from_observations", "MIN_TRAINING_ROWS",
           "capture", "fade", "registry", "explain", "ccxt_feed"]
