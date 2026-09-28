"""Order management and vault-backed strategy execution."""
from .oms import LEGAL_TRANSITIONS, OrderManager, TrackedOrder
from .vault_strategy import VaultStrategyRunner

__all__ = ["LEGAL_TRANSITIONS", "OrderManager", "TrackedOrder", "VaultStrategyRunner"]

from .algos import (
    DEFAULT_VOLUME_PROFILE, AlgoError, AlgoRunner, ExecutionAlgo, ParentOrder,
    Slice, TWAP, VWAP, participation_cap,
)
from .tca import MarketTrade, TCAResult, analyse, analyse_parent, interval_vwap
from .persistence import run_records, save_run, slice_index_of

__all__ += [
    "DEFAULT_VOLUME_PROFILE", "AlgoError", "AlgoRunner", "ExecutionAlgo",
    "ParentOrder", "Slice", "TWAP", "VWAP", "participation_cap",
    "MarketTrade", "TCAResult", "analyse", "analyse_parent", "interval_vwap",
    "run_records", "save_run", "slice_index_of",
]
