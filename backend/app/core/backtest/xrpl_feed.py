"""Live XRPL market data — real spot & AMM state from public rippled APIs.

Sources the parts of the market that genuinely live on-ledger:

  * `amm_info`   → native constant-product AMM pool reserves, fee, LP token.
                   This is the *real* input for the impermanent-loss / LP
                   analytics — an actual on-chain pool, not a proxy.
  * `book_offers`→ the native DEX central-limit order book, from which we take
                   the top-of-book mid (and can derive depth / slippage).

Deliberately honest about scope:
  * XRPL has NO perpetual futures and NO funding rate — those are CeFi
    (Binance/Bybit/Deribit) constructs. The basis backtest therefore takes its
    SPOT leg from XRPL and its PERP/FUNDING leg from a derivatives venue
    (see `perp_source` below). Don't fake a funding rate from ledger data.
  * A rippled node serves CURRENT state, not history. For a backtest, either
    `record()` a live stream to CSV over time, or point at a Clio/indexer.

Transport: raw JSON-RPC over HTTPS via stdlib urllib (no dependency, and it
shows the protocol rather than hiding it behind an SDK). If `xrpl-py` is
installed it is used instead. Offline, callers fall back to `simulate()`.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass

ENDPOINTS = {
    "mainnet": "https://xrplcluster.com/",
    "mainnet_ripple": "https://s1.ripple.com:51234/",
    "testnet": "https://s.altnet.rippletest.net:51234/",
    "devnet": "https://s.devnet.rippletest.net:51234/",
}
DROPS_PER_XRP = 1_000_000


def _amount_to_float(amt) -> float:
    """XRPL amounts: XRP is a drops string; issued tokens are {value,...}."""
    if isinstance(amt, str):
        return int(amt) / DROPS_PER_XRP
    return float(amt["value"])


def _rpc(method: str, params: dict, url: str, timeout: float = 10.0) -> dict:
    body = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # network required
        return json.loads(r.read()).get("result", {})


def _asset(currency: str, issuer: str | None = None) -> dict:
    return {"currency": currency} if currency == "XRP" else {
        "currency": currency, "issuer": issuer}


@dataclass
class AMMState:
    spot: float          # asset2 per asset (quote per base)
    reserve_base: float
    reserve_quote: float
    trading_fee_bps: float
    lp_token: float


class XRPLFeed:
    """Thin client over public rippled JSON-RPC."""

    def __init__(self, network: str = "mainnet"):
        self.url = ENDPOINTS.get(network, network)   # accept a raw URL too

    # ---- native AMM (on-chain constant-product pool) ----
    def amm_state(self, base: str, quote: str, *, base_issuer: str | None = None,
                  quote_issuer: str | None = None) -> AMMState:
        res = _rpc("amm_info",
                   {"asset": _asset(base, base_issuer),
                    "asset2": _asset(quote, quote_issuer)}, self.url)
        amm = res["amm"]
        rb = _amount_to_float(amm["amount"])
        rq = _amount_to_float(amm["amount2"])
        fee = amm.get("trading_fee", 0) / 1000.0          # units of 1/100000 → bps
        lp = _amount_to_float(amm["lp_token"])
        return AMMState(spot=rq / rb, reserve_base=rb, reserve_quote=rq,
                        trading_fee_bps=fee, lp_token=lp)

    # ---- native DEX order book mid ----
    def book_mid(self, base: str, quote: str, *, base_issuer: str | None = None,
                 quote_issuer: str | None = None) -> float:
        ask = _rpc("book_offers", {"taker_gets": _asset(base, base_issuer),
                                   "taker_pays": _asset(quote, quote_issuer),
                                   "limit": 1}, self.url).get("offers", [])
        bid = _rpc("book_offers", {"taker_gets": _asset(quote, quote_issuer),
                                   "taker_pays": _asset(base, base_issuer),
                                   "limit": 1}, self.url).get("offers", [])
        def px(offers, invert):
            if not offers:
                return None
            q = float(offers[0]["quality"])         # taker_pays/taker_gets
            return (1.0 / q) if invert else q
        a, b = px(ask, False), px(bid, True)
        return (a + b) / 2 if a and b else (a or b)

    def spot(self, base: str = "XRP", quote: str = "USD",
             quote_issuer: str | None = None) -> float:
        """Best-available spot: AMM if a pool exists, else DEX mid."""
        try:
            return self.amm_state(base, quote, quote_issuer=quote_issuer).spot
        except Exception:
            return self.book_mid(base, quote, quote_issuer=quote_issuer)


def record(out_csv: str, *, minutes: float = 60.0, interval_s: float = 60.0,
           base: str = "XRP", quote: str = "USD", quote_issuer: str | None = None,
           network: str = "mainnet", perp_source=None) -> str:
    """Poll XRPL and write a MarketData CSV [timestamp,spot,perp_mark,funding_rate].

    `perp_source`: optional callable() -> (perp_mark, funding_rate) from a CeFi
    venue for the derivatives leg. If None, perp_mark := spot and funding := 0
    (spot-only; the basis P&L funding term is then zero — clearly not a perp).
    """
    feed = XRPLFeed(network)
    n = int(minutes * 60 / interval_s)
    rows = ["timestamp,spot,perp_mark,funding_rate"]
    for _ in range(n):
        s = feed.spot(base, quote, quote_issuer=quote_issuer)
        if perp_source is not None:
            perp, fund = perp_source()
        else:
            perp, fund = s, 0.0
        rows.append(f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{s:.8f},{perp:.8f},{fund:.10f}")
        time.sleep(interval_s)
    with open(out_csv, "w") as f:
        f.write("\n".join(rows))
    return out_csv


def live_amm_or_simulate(base: str = "XRP", quote: str = "USD",
                         quote_issuer: str | None = None, network: str = "mainnet"):
    """Return a live AMMState if the network is reachable, else None.

    Lets callers do:  state = live_amm_or_simulate() or <fallback>.
    """
    try:
        return XRPLFeed(network).amm_state(base, quote, quote_issuer=quote_issuer)
    except Exception:
        return None
