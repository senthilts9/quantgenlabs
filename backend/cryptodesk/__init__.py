"""cryptodesk - a testnet-only crypto trading stack.

Public market data, DeFi pricing, signed-order DEX and CEX testnet adapters,
a vault-backed key layer, and a pre-trade validation engine modelled on what a
signed-order perps venue enforces.

Nothing in this package can reach mainnet unless two environment variables are
set deliberately - see `cryptodesk.config`.
"""
__version__ = "0.1.0"

from .config import MAINNET_ACK, MainnetBlocked, Settings, describe_guard

__all__ = ["Settings", "MainnetBlocked", "MAINNET_ACK", "describe_guard", "__version__"]
