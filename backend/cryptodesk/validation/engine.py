"""The validation pipeline.

Runs rules stage by stage over an order and returns a `ValidationReport`.

Two behaviours matter for correctness:

*Stage gating.* If the SCHEMA stage fails, later stages are skipped. A rule
that reads `request.price` after a rule established the price is missing would
either crash or, worse, produce a second misleading rejection. So a failed
stage stops the pipeline - but rules *within* a stage all run, so a caller
fixing up an order sees every tick, step and bound problem at once rather than
one per round trip.

*Autofix.* `repair()` returns a corrected copy of an order for the rejections
that have a mechanical fix - snap to tick, snap to step, step back from a
crossed post-only price. It deliberately refuses to fix anything involving
money or risk: it will not shrink a size to fit your margin, or clamp a
reduce-only order to your position. Those are decisions, not corrections.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from ..core.errors import Code, Stage, ValidationReport
from ..core.instruments import InstrumentRegistry
from ..core.types import OrderRequest, Side
from .rules import RULES_BY_STAGE, STAGE_ORDER, Rule, ValidationContext


class OrderValidator:
    """Runs a ruleset over orders.

    `strict=True` stops at the first failing *rule* rather than the first
    failing stage - matching venues that report a single reason per request.
    The default collects everything, which is what you want client-side.
    """

    def __init__(
        self,
        registry: Optional[InstrumentRegistry] = None,
        rules_by_stage: Optional[Dict[Stage, List[Rule]]] = None,
        stages: Optional[Iterable[Stage]] = None,
        strict: bool = False,
    ):
        self.registry = registry or InstrumentRegistry()
        self.rules_by_stage = rules_by_stage or RULES_BY_STAGE
        self.stages = list(stages) if stages is not None else list(STAGE_ORDER)
        self.strict = strict

    def validate(self, ctx: ValidationContext) -> ValidationReport:
        report = ValidationReport()
        for stage in self.stages:
            before = len(report.rejections)
            for rule in self.rules_by_stage.get(stage, []):
                rule(ctx, report)
                if self.strict and report.rejections:
                    return report
            # A stage that produced rejections has invalidated the assumptions
            # later stages rest on. Stop here rather than cascade.
            if len(report.rejections) > before:
                break
        return report

    def check(
        self,
        request: OrderRequest,
        book=None,
        mark=None,
        account=None,
        **kwargs,
    ) -> ValidationReport:
        """Convenience wrapper that builds the context for you."""
        ctx = ValidationContext(
            request=request, registry=self.registry,
            book=book, mark=mark, account=account, **kwargs
        )
        return self.validate(ctx)

    # -- autofix -----------------------------------------------------------

    #: Rejections `repair()` knows how to correct. Everything else is left
    #: alone deliberately - see the module docstring.
    REPAIRABLE = frozenset({
        Code.PRICE_NOT_MULTIPLE_OF_TICK,
        Code.SIZE_NOT_MULTIPLE_OF_STEP,
        Code.POST_ONLY_WOULD_CROSS,
    })

    def repair(
        self,
        request: OrderRequest,
        report: ValidationReport,
        book=None,
    ) -> Tuple[OrderRequest, List[str]]:
        """Return a mechanically-corrected copy plus a log of what changed.

        Never mutates the input: a caller comparing the original against the
        repaired version is how an operator sees what the system did on their
        behalf. Returns the original unchanged if nothing is repairable.
        """
        inst = self.registry.get(request.market)
        if inst is None:
            return request, []

        changes: List[str] = []
        price = request.price
        size = request.size

        for rej in report.rejections:
            if rej.code not in self.REPAIRABLE:
                continue

            if rej.code is Code.PRICE_NOT_MULTIPLE_OF_TICK:
                if rej.detail.get("field") == "price" and price is not None:
                    snapped = inst.round_price(price, request.side)
                    if snapped != price:
                        changes.append(
                            "price %s -> %s (tick %s)" % (price, snapped, inst.tick_size)
                        )
                        price = snapped

            elif rej.code is Code.SIZE_NOT_MULTIPLE_OF_STEP:
                snapped = inst.round_size(size)
                # Rounding down can reach zero on a sub-step size; that is a
                # sizing error, not a rounding one, so leave it for the caller.
                if snapped != size and snapped > 0:
                    changes.append(
                        "size %s -> %s (step %s)" % (size, snapped, inst.step_size)
                    )
                    size = snapped

            elif rej.code is Code.POST_ONLY_WOULD_CROSS and book is not None:
                if request.side is Side.BUY and book.best_ask is not None:
                    new_price = book.best_ask - inst.tick_size
                else:
                    new_price = (book.best_bid + inst.tick_size
                                 if book.best_bid is not None else price)
                if new_price is not None and new_price != price:
                    changes.append(
                        "post-only price %s -> %s (steps inside spread)"
                        % (price, new_price)
                    )
                    price = new_price

        if not changes:
            return request, []
        return replace(request, price=price, size=size), changes

    def validate_and_repair(
        self,
        request: OrderRequest,
        max_passes: int = 3,
        **kwargs,
    ) -> Tuple[OrderRequest, ValidationReport, List[str]]:
        """Validate, repair, revalidate - up to `max_passes`.

        Iterates because one repair can expose the next: snapping a size to the
        step grid changes the notional, which can newly breach the minimum. The
        loop is bounded because a repair that keeps re-triggering the same
        rejection is a bug in the ruleset, and spinning on it would hang the
        order path rather than surface the problem.
        """
        all_changes: List[str] = []
        current = request
        report = self.check(current, **kwargs)
        for _ in range(max_passes):
            if report.ok:
                break
            repaired, changes = self.repair(current, report, book=kwargs.get("book"))
            if not changes:
                break
            all_changes.extend(changes)
            current = repaired
            report = self.check(current, **kwargs)
        return current, report, all_changes


# ---------------------------------------------------------------------------
# Venue rulesets
# ---------------------------------------------------------------------------
# Venues enforce overlapping but non-identical subsets. Encoding the difference
# as a ruleset rather than runtime flags means a rule a venue does not enforce
# simply is not present - it cannot fire by accident, and the set of rules any
# given venue applies is readable in one place.

def _ruleset(drop: Iterable[Rule] = (), stages: Optional[List[Stage]] = None):
    dropped = set(drop)
    return {
        stage: [r for r in rules if r not in dropped]
        for stage, rules in RULES_BY_STAGE.items()
        if stages is None or stage in stages
    }


def for_venue(venue: str, registry: Optional[InstrumentRegistry] = None,
              **kwargs) -> OrderValidator:
    """Build a validator configured for a named venue."""
    from . import rules as R

    venue = venue.lower()
    reg = registry or InstrumentRegistry()

    if venue in ("paradex", "hyperliquid"):
        # Signed-order DEXes: the full pipeline, signatures mandatory.
        return OrderValidator(reg, RULES_BY_STAGE, **kwargs)

    if venue in ("binance", "binance_testnet", "coinbase", "coinbase_sandbox"):
        # Centralised venues authenticate the *session* (HMAC or JWT) rather
        # than each order, so per-order signature and nonce rules do not apply.
        return OrderValidator(
            reg,
            _ruleset(drop=[R.rule_signature_present, R.rule_signature_freshness,
                           R.rule_nonce_unused]),
            **kwargs
        )

    if venue == "paper":
        # Local engine: no auth surface at all, but every economic rule stays
        # on so paper results mean something.
        return OrderValidator(
            reg,
            _ruleset(stages=[Stage.SCHEMA, Stage.INSTRUMENT, Stage.MARKET,
                             Stage.ACCOUNT, Stage.THROTTLE]),
            stages=[Stage.SCHEMA, Stage.INSTRUMENT, Stage.MARKET,
                    Stage.ACCOUNT, Stage.THROTTLE],
            **kwargs
        )

    return OrderValidator(reg, **kwargs)


def offline(registry: Optional[InstrumentRegistry] = None) -> OrderValidator:
    """Structural checks only - no book, mark or account needed.

    Useful for validating a strategy's generated orders in CI, where no venue
    is reachable and no account exists.
    """
    return OrderValidator(
        registry or InstrumentRegistry(),
        stages=[Stage.SCHEMA, Stage.INSTRUMENT],
    )
