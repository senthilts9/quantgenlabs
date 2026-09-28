"""Pre-trade risk gates.

Every order passes through here BEFORE it is accepted. That ordering is the
whole point: a limit checked after the fill is a report, not a control.

Gates, in the order a desk would want them evaluated (cheapest and most
absolute first, so a halted desk never burns a market-data lookup):

  1. kill switch          -- global halt, rejects everything
  2. restricted symbol    -- instrument-level block (compliance/hard-to-borrow)
  3. max order notional   -- single-order fat-finger cap
  4. price collar         -- limit price too far from reference; catches the
                             "typed 70 instead of 700" class of error
  5. max position notional-- POST-trade position, per symbol
  6. max gross exposure   -- POST-trade book-level
  7. daily loss limit     -- realised P&L stop for the session

5-7 are evaluated against projected post-trade state, not current state:
checking "am I within the limit now" lets an order through that breaches on
fill, which is the bug that makes limits theatre.

Every decision -- pass or fail -- is returned with the numbers that produced
it, so a rejection is explainable to a trader without reading the code.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .models import Position, Side


@dataclass
class RiskLimits:
    """Desk limits. All notionals in base currency."""
    kill_switch: bool = False
    restricted_symbols: list[str] = field(default_factory=list)
    max_order_notional: float = 500_000.0
    max_position_notional: float = 2_000_000.0
    max_gross_exposure: float = 5_000_000.0
    daily_loss_limit: float = 50_000.0        # positive number, realised loss
    price_collar_pct: float = 5.0             # |limit - ref| / ref, percent

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RiskLimits":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str
    observed: float | None = None
    limit: float | None = None


@dataclass
class RiskDecision:
    approved: bool
    checks: list[GateResult]

    @property
    def reason(self) -> str | None:
        """First failing gate — what the trader actually needs to be told."""
        for c in self.checks:
            if not c.passed:
                return f"{c.gate}: {c.detail}"
        return None

    def to_dict(self) -> dict:
        return {"approved": self.approved, "reason": self.reason,
                "checks": [asdict(c) for c in self.checks]}


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def evaluate(limits: RiskLimits, *, symbol: str, side: Side, qty: float,
             reference_price: float, positions: list[Position],
             marks: dict[str, float] | None = None,
             realized_pnl_today: float = 0.0,
             limit_price: float | None = None) -> RiskDecision:
    """Run every gate and return the full record.

    Deliberately evaluates ALL gates rather than short-circuiting on the
    first failure: a trader resubmitting against a partially-known limit set
    would otherwise discover breaches one painful round-trip at a time.
    """
    symbol = symbol.upper()
    marks = {k.upper(): v for k, v in (marks or {}).items()}
    checks: list[GateResult] = []

    order_notional = abs(qty) * reference_price
    signed = qty * side.sign

    # 1. kill switch
    checks.append(GateResult(
        "kill_switch", not limits.kill_switch,
        "desk halted — all new orders blocked" if limits.kill_switch
        else "desk active"))

    # 2. restricted instrument
    restricted = symbol in {s.upper() for s in limits.restricted_symbols}
    checks.append(GateResult(
        "restricted_symbol", not restricted,
        f"{symbol} is on the restricted list" if restricted
        else f"{symbol} is tradable"))

    # 3. single-order notional
    ok = order_notional <= limits.max_order_notional
    checks.append(GateResult(
        "max_order_notional", ok,
        f"order notional {_fmt(order_notional)} "
        f"{'within' if ok else 'exceeds'} cap {_fmt(limits.max_order_notional)}",
        round(order_notional, 2), limits.max_order_notional))

    # 4. price collar (limit orders only — a market order has no price to check)
    if limit_price and reference_price:
        dev = abs(limit_price - reference_price) / reference_price * 100
        ok = dev <= limits.price_collar_pct
        checks.append(GateResult(
            "price_collar", ok,
            f"limit {limit_price:,.4f} is {dev:.2f}% from reference "
            f"{reference_price:,.4f} ({'within' if ok else 'outside'} "
            f"{limits.price_collar_pct:.2f}%)",
            round(dev, 4), limits.price_collar_pct))
    else:
        checks.append(GateResult("price_collar", True,
                                 "market order — no limit price to collar"))

    # 5. projected position notional for this symbol
    by_symbol = {p.symbol: p for p in positions}
    current = by_symbol.get(symbol)
    projected_qty = (current.qty if current else 0.0) + signed
    projected_notional = abs(projected_qty) * reference_price
    ok = projected_notional <= limits.max_position_notional
    checks.append(GateResult(
        "max_position_notional", ok,
        f"post-trade {symbol} notional {_fmt(projected_notional)} "
        f"{'within' if ok else 'exceeds'} cap {_fmt(limits.max_position_notional)}",
        round(projected_notional, 2), limits.max_position_notional))

    # 6. projected gross exposure across the book
    gross = 0.0
    for p in positions:
        q = projected_qty if p.symbol == symbol else p.qty
        gross += abs(q) * (marks.get(p.symbol) or p.avg_price or reference_price)
    if symbol not in by_symbol:
        gross += projected_notional
    ok = gross <= limits.max_gross_exposure
    checks.append(GateResult(
        "max_gross_exposure", ok,
        f"post-trade gross {_fmt(gross)} "
        f"{'within' if ok else 'exceeds'} cap {_fmt(limits.max_gross_exposure)}",
        round(gross, 2), limits.max_gross_exposure))

    # 7. daily realised loss limit
    loss = -min(0.0, realized_pnl_today)
    ok = loss <= limits.daily_loss_limit
    checks.append(GateResult(
        "daily_loss_limit", ok,
        f"realised loss {_fmt(loss)} "
        f"{'within' if ok else 'exceeds'} limit {_fmt(limits.daily_loss_limit)}",
        round(loss, 2), limits.daily_loss_limit))

    return RiskDecision(approved=all(c.passed for c in checks), checks=checks)


def utilisation(limits: RiskLimits, positions: list[Position],
                marks: dict[str, float] | None = None,
                realized_pnl_today: float = 0.0) -> list[dict]:
    """Current usage of each book-level limit, for the alerts panel.

    Utilisation is what turns a limit from a binary gate into something a
    desk can manage — you want to see 80% before you see a rejection.
    """
    marks = {k.upper(): v for k, v in (marks or {}).items()}
    gross = sum(abs(p.qty) * (marks.get(p.symbol) or p.avg_price)
                for p in positions)
    largest = max((abs(p.qty) * (marks.get(p.symbol) or p.avg_price)
                   for p in positions), default=0.0)
    loss = -min(0.0, realized_pnl_today)

    rows = [
        ("Gross exposure", gross, limits.max_gross_exposure),
        ("Largest position", largest, limits.max_position_notional),
        ("Daily realised loss", loss, limits.daily_loss_limit),
    ]
    out = []
    for name, used, cap in rows:
        pct = (used / cap * 100) if cap else 0.0
        out.append({
            # `or 0.0` normalises negative zero: -min(0.0, 0.0) is -0.0, which
            # renders as "-0" and reads like a bug in the alerts panel.
            "limit": name, "used": round(used, 2) or 0.0,
            "cap": round(cap, 2),
            "utilisation_pct": round(pct, 2) or 0.0,
            # Bands mirror how a desk escalates: watch, then warn, then stop.
            "status": "breach" if pct >= 100 else
                      "warning" if pct >= 80 else
                      "watch" if pct >= 60 else "ok",
        })
    return out
