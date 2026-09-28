"""Market-connectivity core — the C++ layer, with an honest Python fallback.

Follows the same contract as `core/engines.py`: import defensively, expose
`HAVE_CPP`, and never make a caller branch on whether the extension was built.

WHAT THE FALLBACK DOES AND DOES NOT DO. Where a pure-Python equivalent is honest
and cheap, it is provided (the book, the quality gate). Where it would not be —
the matching engine and the FIX session layer are stateful protocol machines whose
whole value is that they are the tested C++ implementation — the fallback raises
rather than offering a second, untested implementation that could silently disagree
with the first. Two implementations of a matching engine is two answers to "what
filled", which is worse than one answer and a clear error.

Build the extension with:

    ./scripts/build_bindings.sh
"""
from __future__ import annotations

try:
    import mkt_connectivity as _mkt          # pybind11 module (optional)
    HAVE_CPP = True
except Exception:                            # not built -> Python fallbacks
    _mkt = None
    HAVE_CPP = False

from .fallback import PyLadder, PyFeedValidator

__all__ = ["HAVE_CPP", "impl_name", "Ladder", "FeedValidator", "matching_engine",
           "pretrade_gate", "parse_fix", "pretty_fix"]


def impl_name() -> str:
    """'cpp' or 'python' — surfaced in API responses so a reader of a number
    always knows which implementation produced it."""
    return "cpp" if HAVE_CPP else "python"


def Ladder(floor_ticks: int, n_ticks: int):
    if HAVE_CPP:
        return _mkt.Ladder(floor_ticks, n_ticks)
    return PyLadder(floor_ticks, n_ticks)


def FeedValidator(**kwargs):
    if HAVE_CPP:
        cfg = _mkt.FeedConfig()
        for k, v in kwargs.items():
            setattr(cfg, "lambda_" if k == "lam" else k, v)
        return _mkt.FeedValidator(cfg)
    return PyFeedValidator(**kwargs)


def matching_engine(floor_ticks: int, n_ticks: int, prevent_self_match: bool = True):
    if not HAVE_CPP:
        raise RuntimeError(
            "The matching engine has no Python fallback, deliberately. It is a "
            "stateful protocol machine whose value is that it is the tested C++ "
            "implementation; a second implementation would be a second answer to "
            "'what filled'. Build it with ./scripts/build_bindings.sh"
        )
    return _mkt.MatchingEngine(floor_ticks, n_ticks, prevent_self_match)


def pretrade_gate(n_symbols: int):
    if not HAVE_CPP:
        raise RuntimeError(
            "The pre-trade gate has no Python fallback, deliberately. A risk "
            "control that silently switches to an unverified second implementation "
            "is worse than one that refuses to start. "
            "Build it with ./scripts/build_bindings.sh"
        )
    return _mkt.PreTradeGate(n_symbols)


def parse_fix(raw: str, validate: bool = True) -> dict:
    if not HAVE_CPP:
        raise RuntimeError("FIX parsing requires the C++ extension: ./scripts/build_bindings.sh")
    return _mkt.parse_fix(raw, validate)


def pretty_fix(raw: str) -> str:
    if HAVE_CPP:
        return _mkt.pretty_fix(raw)
    return raw.replace("\x01", "|")


# Re-export the enums when available so callers can write Side.BID etc.
if HAVE_CPP:
    Side = _mkt.Side
    OrderSide = _mkt.OrderSide
    OrdType = _mkt.OrdType
    Decision = _mkt.Decision
    Verdict = _mkt.Verdict
    TimeInForce = _mkt.TimeInForce
    OrderState = _mkt.OrderState
    SymbolLimits = _mkt.SymbolLimits
    decision_name = _mkt.decision_name
    verdict_name = _mkt.verdict_name
    __all__ += ["Side", "OrderSide", "OrdType", "Decision", "Verdict",
                "TimeInForce", "OrderState", "SymbolLimits",
                "decision_name", "verdict_name"]
