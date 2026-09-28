"""Cross-checking a venue's mark against an independent on-chain oracle.

Every P&L number on this desk descends from one mark. If that mark comes from
a single exchange feed, then a stale socket, a thin book or a deliberately
pushed print becomes an unrealised P&L that nobody can distinguish from a real
one -- and on a perpetual venue it becomes a liquidation price, which is the
point at which a bad mark stops being a reporting problem and starts spending
money.

So the mark is checked against a **second source that does not share a failure
mode with the first**: a Chainlink aggregator, read on-chain at a known block.
It is not merely a different HTTP endpoint -- it is a different set of
reporters, a different transport, and a value that is already committed to a
block by the time we read it.

Two things are measured, because they fail differently:

**Divergence.** The two prices disagree. Measured in bps against the oracle,
signed, so the direction is legible: positive means the venue is quoting above
the chain.

**Staleness.** The oracle agrees, but it is agreeing with an old world. A
Chainlink feed updates on a deviation threshold *or* a heartbeat, so a quiet
market legitimately leaves the last round an hour old. That is not a fault;
marking a book to it during a fast move is. Age is measured from the round's
own `updatedAt`, never from when our HTTP request happened to return.

The thresholds are measured, not guessed. Against Sepolia feeds, observed
divergence from Coinbase mid sat between 7 and 24 bps with rounds 6-25 minutes
old -- almost entirely the feed's own update lag rather than disagreement
about price. `DEFAULT_TOLERANCE_BPS` sits well above that band so ordinary lag
does not cry wolf, and far below the move that would accompany a real break.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..core.types import now_ms

#: 1%. Observed testnet lag is 7-24 bps; a genuine feed break or a manipulated
#: print is far larger. A tighter bound would reject honest orders all day,
#: which is how a control ends up switched off.
DEFAULT_TOLERANCE_BPS = Decimal("100")

#: One hour, matching the heartbeat these aggregators publish under. A feed
#: quiet for longer has stopped doing its job rather than found a quiet market.
DEFAULT_MAX_AGE_S = 3600

BPS = Decimal(10000)


@dataclass(frozen=True)
class OracleCheck:
    """The result of one cross-check. Immutable: it is a record of a moment.

    `block` is the point of the whole thing. An oracle read without the block
    it was taken at cannot be reproduced -- `latestRoundData()` returns
    something different the moment the chain advances, so an alert that cites
    only a price is an assertion, while one that cites a block is evidence.
    """

    market: str
    chain: str
    block: int
    oracle_price: Decimal
    oracle_source: str
    oracle_age_s: Optional[int]
    cex_price: Optional[Decimal]
    divergence_bps: Optional[Decimal]
    tolerance_bps: Decimal
    max_age_s: int
    observed_ms: int

    @property
    def diverged(self) -> bool:
        if self.divergence_bps is None:
            return False
        return abs(self.divergence_bps) > self.tolerance_bps

    @property
    def stale(self) -> bool:
        if self.oracle_age_s is None:
            return False
        return self.oracle_age_s > self.max_age_s

    @property
    def ok(self) -> bool:
        return not (self.diverged or self.stale)

    def reason(self) -> Optional[str]:
        """Why the mark is not trustworthy, or None if it is."""
        if self.stale:
            return ("oracle round is %ds old (limit %ds) - the feed has stopped "
                    "updating, so agreement with it proves nothing"
                    % (self.oracle_age_s, self.max_age_s))
        if self.diverged:
            return ("venue mark is %s bps from the on-chain oracle (limit %s) - "
                    "one of the two sources is wrong and this order would be "
                    "priced off it"
                    % (self.divergence_bps.quantize(Decimal("0.01")),
                       self.tolerance_bps))
        return None

    def to_dict(self) -> dict:
        q = lambda d: None if d is None else str(d.quantize(Decimal("0.00000001")))
        return {
            "market": self.market,
            "chain": self.chain,
            "block": self.block,
            "oracle_price": q(self.oracle_price),
            "oracle_source": self.oracle_source,
            "oracle_age_s": self.oracle_age_s,
            "cex_price": q(self.cex_price),
            "divergence_bps": (None if self.divergence_bps is None
                               else str(self.divergence_bps.quantize(Decimal("0.01")))),
            "tolerance_bps": str(self.tolerance_bps),
            "max_age_s": self.max_age_s,
            "observed_ms": self.observed_ms,
            "diverged": self.diverged,
            "stale": self.stale,
            "ok": self.ok,
            "reason": self.reason(),
        }


def cross_check(market: str,
                cex_price: Optional[Decimal],
                chain: str = "sepolia",
                tolerance_bps: Decimal = DEFAULT_TOLERANCE_BPS,
                max_age_s: int = DEFAULT_MAX_AGE_S,
                rpc=None,
                oracle=None) -> Optional[OracleCheck]:
    """Read the on-chain oracle for `market` and compare it to `cex_price`.

    Returns None when no aggregator is configured for the market -- absence of
    a second source is not a breach, it is simply an unverified mark, and the
    caller must be able to tell those apart.

    Raises whatever the RPC raises. A cross-check that swallows transport
    errors and reports `ok` would be worse than no cross-check at all: it would
    report the control as passing precisely when the control was not running.
    """
    from ..marketdata.defi import ChainlinkOracle, JsonRpc

    rpc = rpc or JsonRpc(chain)
    oracle = oracle or ChainlinkOracle(rpc, chain=chain)

    quote = oracle.read(market)
    if quote is None:
        return None

    block = rpc.block_number()
    observed = now_ms()
    age_s = None
    if quote.timestamp_ms:
        age_s = max(0, int((observed - quote.timestamp_ms) / 1000))

    divergence = None
    if cex_price is not None and quote.price > 0:
        # Signed, and against the ORACLE as denominator. The oracle is the
        # reference being checked against, so a 10 bps reading means the same
        # thing regardless of which venue is being validated.
        divergence = (cex_price - quote.price) / quote.price * BPS

    return OracleCheck(
        market=market.upper(), chain=chain, block=block,
        oracle_price=quote.price, oracle_source=quote.source,
        oracle_age_s=age_s, cex_price=cex_price,
        divergence_bps=divergence, tolerance_bps=tolerance_bps,
        max_age_s=max_age_s, observed_ms=observed)
