"""Desk-state context for the Claude copilot.

The copilot is only useful if it answers about YOUR book, not textbook
generalities. This assembles a compact, structured snapshot of the desk —
positions, funding, live risk numbers — that gets injected into the system
prompt so every answer is grounded in what was last computed.

Kept deterministic and side-effect free so it can be unit-tested and so the
exact context sent to the model is auditable (a governance requirement for
anything touching a trading desk).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np

from app.core.crypto import liquidation, perps, risk


@dataclass
class Position:
    symbol: str
    side: str            # "long" | "short"
    qty: float           # base units
    entry: float
    mark: float
    leverage: float
    maintenance_margin_rate: float = 0.005


@dataclass
class DeskContext:
    positions: list[Position] = field(default_factory=list)
    funding_rates: dict[str, float] = field(default_factory=dict)  # per-interval
    returns_window: dict[str, list[float]] = field(default_factory=dict)

    def snapshot(self, confidence: float = 0.99) -> dict:
        rows = []
        gross_notional = 0.0
        worst_dist = None
        for p in self.positions:
            ms = liquidation.margin_state(
                p.entry, p.mark, p.qty, p.leverage,
                p.side == "long", p.maintenance_margin_rate)
            notional = abs(p.qty) * p.mark
            gross_notional += notional
            fr = self.funding_rates.get(p.symbol, 0.0)
            carry = perps.annualised_funding(fr)
            row = {
                "symbol": p.symbol, "side": p.side, "notional": round(notional, 2),
                "leverage": p.leverage, "uPnL": round(ms.unrealised_pnl, 2),
                "liq_price": round(ms.liquidation_price, 6),
                "distance_to_liq_pct": round(ms.distance_to_liq * 100, 2),
                "margin_ratio": round(ms.margin_ratio, 3),
                "funding_rate_interval_pct": round(fr * 100, 4),
                "annualised_carry_pct": round(carry * 100, 2),
            }
            r = self.returns_window.get(p.symbol)
            if r and len(r) >= 30:
                tr = risk.evt_var(np.asarray(r), confidence)
                row["var99_pct"] = round(tr.var * 100, 2)
                row["es99_pct"] = round(tr.es * 100, 2)
                if tr.xi is not None:
                    row["tail_index_xi"] = round(tr.xi, 3)
            rows.append(row)
            d = ms.distance_to_liq
            worst_dist = d if worst_dist is None else min(worst_dist, abs(d))
        return {
            "as_of": "live",
            "gross_notional": round(gross_notional, 2),
            "position_count": len(rows),
            "closest_liq_distance_pct": round((worst_dist or 0) * 100, 2),
            "positions": rows,
            "conventions": "linear perps, 24/7 (365d annualisation), "
                           "funding 3×/day, EVT-POT tail risk at 99%.",
        }

    def as_prompt_block(self) -> str:
        return json.dumps(self.snapshot(), indent=2)
