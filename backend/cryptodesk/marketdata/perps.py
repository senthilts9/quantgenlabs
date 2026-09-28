"""Perpetual-contract statistics: mark, index, basis, funding, open interest.

**Why this lives in `marketdata/` and not on the venue adapter.**

The mainnet guard exists to stop a *working order path* reaching production —
it is enforced at URL resolution, before a key is loaded or anything is
signed. A venue adapter is order-capable, so it stays behind that guard and
resolves to testnet.

But Hyperliquid's `/info` endpoint is public, unauthenticated and read-only:
no key, no signature, no order. Reading it is the same class of operation as
`CoinbasePublicFeed` already performing against mainnet Coinbase to produce
the desk's spot prices. Treating it as a *feed* rather than as venue access
keeps the guard exactly as strong as it was while letting the desk show the
market that actually exists.

That distinction is not cosmetic. On testnet, BTC perp open interest is
roughly 49 BTC; on mainnet it is around 36,000. A screen captioned "Open
Interest" showing the former is not a conservative version of the truth, it
is a different number entirely — and every basis and funding figure beside it
is drawn from a book with almost nothing in it.

Which network a row came from therefore travels *with* the row. A consumer
can label it, and cannot silently mistake one for the other.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Sequence

from ..core.types import ZERO, dec
from .http import HttpClient

#: Public, unauthenticated, read-only. Order entry never touches these.
MAINNET_INFO = "https://api.hyperliquid.xyz/info"
TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"


class PerpDataError(Exception):
    """The venue's payload was not the shape this parser understands."""


def _opt_dec(value) -> Optional[Decimal]:
    """Decimal or None. The venue omits fields on halted contracts, where
    `Decimal(None)` raises and `Decimal(0)` would quietly lie."""
    if value is None or value == "":
        return None
    try:
        return dec(value)
    except Exception:
        return None


@dataclass(frozen=True)
class PerpStats:
    """One perpetual contract's published state, on a named network.

    `basis_bps` is signed against the index: positive means the perp trades
    above spot, which is exactly the condition under which longs pay shorts.
    Fixing the convention here means no consumer re-derives which direction of
    a basis is expensive for which side.
    """

    market: str
    coin: str
    network: str
    mark: Decimal
    index: Decimal
    basis_bps: Decimal
    funding_rate: Optional[Decimal]
    funding_period_hours: int
    open_interest_base: Decimal
    open_interest_usd: Decimal
    day_notional_volume: Decimal
    prev_day_price: Optional[Decimal]
    change_24h_pct: Optional[Decimal]
    max_leverage: Optional[int]

    @property
    def funding_apr_pct(self) -> Optional[Decimal]:
        """Annualised funding. 24/7 market: 365 days, no weekend gap."""
        if self.funding_rate is None:
            return None
        periods = Decimal(24 * 365) / Decimal(self.funding_period_hours)
        return self.funding_rate * periods * Decimal(100)

    def to_dict(self) -> dict:
        def q(d, places="0.00000001"):
            return None if d is None else str(d.quantize(Decimal(places)))
        return {
            "market": self.market, "coin": self.coin, "network": self.network,
            "mark": q(self.mark), "index": q(self.index),
            "basis_bps": q(self.basis_bps, "0.01"),
            "funding_rate": q(self.funding_rate, "0.000000000001"),
            "funding_period_hours": self.funding_period_hours,
            "funding_apr_pct": q(self.funding_apr_pct, "0.01"),
            "open_interest_base": q(self.open_interest_base, "0.0001"),
            "open_interest_usd": q(self.open_interest_usd, "0.01"),
            "day_notional_volume": q(self.day_notional_volume, "0.01"),
            "change_24h_pct": q(self.change_24h_pct, "0.01"),
            "max_leverage": self.max_leverage,
        }


def parse_meta_ctxs(raw, network: str,
                    markets: Optional[Sequence[str]] = None) -> List[PerpStats]:
    """Turn a `metaAndAssetCtxs` payload into stats rows.

    Split out from fetching so it is testable against a fixture without a
    network call, which is the only way the sign conventions below get pinned.
    """
    if not isinstance(raw, list) or len(raw) < 2:
        raise PerpDataError("unexpected metaAndAssetCtxs payload")
    meta, ctxs = raw[0], raw[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        raise PerpDataError("metaAndAssetCtxs halves are not (meta, ctxs)")

    wanted = None
    if markets:
        wanted = {m.upper().split("-")[0] for m in markets}

    out: List[PerpStats] = []
    for i, asset in enumerate(meta.get("universe") or []):
        coin = asset.get("name")
        if not coin or (wanted is not None and coin not in wanted):
            continue
        if i >= len(ctxs) or not isinstance(ctxs[i], dict):
            continue
        c = ctxs[i]
        mark, index = _opt_dec(c.get("markPx")), _opt_dec(c.get("oraclePx"))
        if mark is None or index is None or index <= ZERO:
            # Halted or delisted. Skipping keeps a null out of a board where
            # every other cell is a price.
            continue
        oi_base = _opt_dec(c.get("openInterest")) or ZERO
        prev = _opt_dec(c.get("prevDayPx"))
        out.append(PerpStats(
            market="%s-USD-PERP" % coin, coin=coin, network=network,
            mark=mark, index=index,
            basis_bps=(mark - index) / index * Decimal(10000),
            funding_rate=_opt_dec(c.get("funding")),
            funding_period_hours=1,          # this venue settles hourly
            open_interest_base=oi_base,
            # Published in base units; a desk reads exposure in notional.
            # Derived once here so no consumer repeats it against the index.
            open_interest_usd=oi_base * mark,
            day_notional_volume=_opt_dec(c.get("dayNtlVlm")) or ZERO,
            prev_day_price=prev,
            change_24h_pct=((mark - prev) / prev * Decimal(100)
                            if prev and prev > ZERO else None),
            max_leverage=int(asset.get("maxLeverage") or 0) or None,
        ))
    out.sort(key=lambda s: s.open_interest_usd, reverse=True)
    return out


def fetch_perp_stats(markets: Optional[Sequence[str]] = None,
                     network: str = "mainnet",
                     client: Optional[HttpClient] = None) -> List[PerpStats]:
    """Every perp's state in **one** request.

    `metaAndAssetCtxs` returns the whole universe together, so a ten-market
    board costs one call rather than ten. Fetching per market would be the
    difference between a screen that refreshes and one that rate-limits itself
    off the venue.
    """
    if network not in ("mainnet", "testnet"):
        raise PerpDataError("network must be 'mainnet' or 'testnet'")
    url = MAINNET_INFO if network == "mainnet" else TESTNET_INFO
    client = client or HttpClient()
    raw = client.post(url, body={"type": "metaAndAssetCtxs"})
    return parse_meta_ctxs(raw, network, markets)
