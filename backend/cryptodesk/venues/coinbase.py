"""Coinbase adapters - and an honest account of what Coinbase can and cannot do.

Two separate things wear the Coinbase name here, and conflating them is the
trap this module exists to prevent:

`CoinbasePublicVenue` - the Exchange public REST feed. Real book, real prices,
no key, read-only. Safe and useful.

`CoinbaseSandboxVenue` - the Advanced Trade sandbox at
`api-sandbox.coinbase.com`. Per Coinbase's own documentation it needs **no
authentication** and **all responses are static and pre-defined**. There is no
matching engine. An order "filled" here was filled by a fixture file.

That matters for this project specifically. Coinbase offers no venue where you
can place a genuinely-matched order without real funds: production Advanced
Trade keys move real money, and the sandbox does not match at all. So the
sandbox's honest use is conformance - does our request serialise correctly, do
we parse the response shape, do we handle the documented error codes - and for
anything involving fills, this package routes to Binance testnet, Paradex
testnet or the local paper engine instead.

`is_mock = True` on the sandbox means `assert_real_execution()` raises, so no
PnL or TCA path can consume its output by accident.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from ..config import Settings
from ..core.errors import Code, Rejection, Stage
from ..core.instruments import Instrument, InstrumentRegistry, MarketState, SettlementKind
from ..core.types import (
    ZERO, AccountState, Balance, OrderBook, OrderRequest, OrderStatus, Quote,
    TimeInForce, dec, now_ms,
)
from ..marketdata.http import DEFAULT_CLIENT, HttpClient, HttpError, TransportError
from .base import Fill, OrderAck, VenueAdapter, VenueError, VenueUnsupported


class CoinbasePublicVenue(VenueAdapter):
    """Read-only Coinbase Exchange public data. No key, no account, no risk."""

    name = "coinbase_public"
    supports_trading = False
    has_matching_engine = False

    def __init__(self, settings: Optional[Settings] = None,
                 registry: Optional[InstrumentRegistry] = None,
                 client: Optional[HttpClient] = None,
                 store=None):
        super().__init__(settings, registry, store=store)
        self.client = client or DEFAULT_CLIENT
        self.base_url = self.settings.resolve(self.name)

    def _product(self, market: str) -> str:
        # Strip a perp suffix so a PERP symbol can be marked against spot depth.
        return market.upper().replace("-PERP", "")

    def fetch_book(self, market: str, depth: int = 25) -> OrderBook:
        raw = self.client.get(
            "%s/products/%s/book" % (self.base_url, self._product(market)),
            params={"level": 1 if depth <= 1 else 2},
        )
        bids = [(lv[0], lv[1]) for lv in raw.get("bids", [])[:depth]]
        asks = [(lv[0], lv[1]) for lv in raw.get("asks", [])[:depth]]
        return OrderBook.from_levels(market, bids, asks, source=self.name)

    def fetch_quote(self, market: str) -> Quote:
        raw = self.client.get(
            "%s/products/%s/ticker" % (self.base_url, self._product(market))
        )
        if not isinstance(raw, dict) or raw.get("price") is None:
            raise VenueError("coinbase: no price for %s" % market)
        return Quote(market, dec(raw["price"]), now_ms(), self.name)

    def fetch_instruments(self) -> List[Instrument]:
        """Translate Coinbase product metadata into instrument specs.

        Coinbase publishes increments as decimal strings ("0.01"), which map
        straight onto tick and step. `trading_disabled` and `status` both have
        to be checked - a product can be listed, untradeable, and still return
        a book.
        """
        raw = self.client.get("%s/products" % self.base_url)
        out: List[Instrument] = []
        for p in raw if isinstance(raw, list) else []:
            try:
                disabled = p.get("trading_disabled") or p.get("status") != "online"
                out.append(Instrument(
                    symbol=p["id"],
                    base=p.get("base_currency", ""),
                    quote=p.get("quote_currency", ""),
                    kind=SettlementKind.SPOT,
                    state=MarketState.PAUSED if disabled else MarketState.OPEN,
                    tick_size=dec(p.get("quote_increment", "0.01")),
                    step_size=dec(p.get("base_increment", "0.00000001")),
                    min_size=dec(p.get("base_min_size") or p.get("base_increment", "0.00000001")),
                    max_size=dec(p.get("base_max_size") or "1000000"),
                    min_notional=dec(p.get("min_market_funds", "1")),
                    max_leverage=Decimal(1),
                    maker_fee_bps=Decimal("40"), taker_fee_bps=Decimal("60"),
                ))
            except (KeyError, TypeError, ValueError):
                continue  # one malformed product must not lose the whole list
        return out


class CoinbaseSandboxVenue(VenueAdapter):
    """Coinbase Advanced Trade sandbox. Mocked responses - conformance only."""

    name = "coinbase_sandbox"
    supports_trading = True       # it accepts order requests...
    has_matching_engine = False   # ...but nothing is matched
    is_mock = True
    requires_signature = False

    def __init__(self, settings: Optional[Settings] = None,
                 registry: Optional[InstrumentRegistry] = None,
                 client: Optional[HttpClient] = None,
                 store=None):
        super().__init__(settings, registry, store=store)
        self.client = client or DEFAULT_CLIENT
        self.base_url = self.settings.resolve(self.name)
        from ..validation.engine import for_venue
        self.validator = for_venue("coinbase_sandbox", self.registry)

    def fetch_account(self) -> AccountState:
        raw = self.client.get("%s/accounts" % self.base_url)
        state = AccountState(account_id="coinbase-sandbox")
        for acct in (raw.get("accounts") or []) if isinstance(raw, dict) else []:
            bal = acct.get("available_balance") or {}
            currency = acct.get("currency") or bal.get("currency") or "?"
            value = dec(bal.get("value", "0"))
            state.balances[currency] = Balance(currency, value, value)
        return state

    def place_order(self, request: OrderRequest) -> OrderAck:
        """Submit an order and parse the canned response.

        Runs the same local validation as any other venue first. That is the
        actual value of this path: it proves the validator and the serialiser
        agree with what Coinbase's schema accepts, without a matching engine
        being involved either way.
        """
        report = self.validator.check(request, **self.replay_kwargs())
        if not report.ok:
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED, rejections=list(report.rejections),
                report=report, venue=self.name,
            )

        self.record_submission(request)
        body = self._serialise(request)
        try:
            raw = self.client.post("%s/orders" % self.base_url, body=body)
        except HttpError as exc:
            return self._ack_from_error(request, exc)
        except TransportError as exc:
            # No answer at all: the order may or may not exist. UNKNOWN forces
            # reconciliation rather than a retry that could double-fill.
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.UNKNOWN, venue=self.name,
                raw={"error": str(exc)},
            )

        success = bool(raw.get("success")) if isinstance(raw, dict) else False
        resp = (raw.get("success_response") or {}) if isinstance(raw, dict) else {}
        if not success:
            err = (raw.get("error_response") or {}) if isinstance(raw, dict) else {}
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED, venue=self.name, raw=raw,
                rejections=[Rejection(
                    Code.MARKET_NOT_TRADEABLE, Stage.INSTRUMENT,
                    err.get("message") or "sandbox rejected order",
                    {"venue_error": err},
                )],
            )
        return OrderAck(
            client_order_id=request.client_order_id,
            order_id=resp.get("order_id"),
            status=OrderStatus.NEW, venue=self.name, raw=raw,
        )

    def _serialise(self, request: OrderRequest) -> Dict:
        """Build the Advanced Trade order payload.

        The `order_configuration` union is the fiddly part: the key name
        encodes both order type and time-in-force, and sending the wrong member
        is rejected with a generic schema error that says nothing useful.
        """
        size = str(request.size)
        if request.order_type.value == "MARKET":
            config: Dict[str, Dict] = {"market_market_ioc": {"base_size": size}}
        elif request.time_in_force is TimeInForce.IOC:
            config = {"limit_limit_gtc": {
                "base_size": size, "limit_price": str(request.price),
                "post_only": False}}
        elif request.time_in_force is TimeInForce.GTD and request.expires_at_ms:
            config = {"limit_limit_gtd": {
                "base_size": size, "limit_price": str(request.price),
                "end_time": request.expires_at_ms, "post_only": request.post_only}}
        else:
            config = {"limit_limit_gtc": {
                "base_size": size, "limit_price": str(request.price),
                "post_only": request.post_only}}
        return {
            "client_order_id": request.client_order_id,
            "product_id": request.market,
            "side": request.side.value,
            "order_configuration": config,
        }

    def _ack_from_error(self, request: OrderRequest, exc: HttpError) -> OrderAck:
        payload = exc.json() or {}
        message = payload.get("message") or payload.get("error") or exc.body[:200]
        return OrderAck(
            client_order_id=request.client_order_id,
            status=OrderStatus.REJECTED, venue=self.name,
            raw={"status": exc.status, "body": exc.body[:500]},
            rejections=[Rejection(
                Code.MARKET_NOT_TRADEABLE, Stage.INSTRUMENT,
                "sandbox HTTP %d: %s" % (exc.status, message),
                {"http_status": exc.status},
            )],
        )

    def force_error(self, scenario: str, market: str = "BTC-USD") -> Dict:
        """Trigger a documented sandbox error via the `X-Sandbox` header.

        This is the one capability the sandbox has that nothing else does:
        deterministic, on-demand error responses. It lets the OMS's error
        handling be tested against real venue error *shapes* rather than
        hand-written fixtures that drift from reality.
        """
        try:
            return self.client.get(
                "%s/accounts" % self.base_url, headers={"X-Sandbox": scenario}
            )
        except HttpError as exc:
            return {"status": exc.status, "body": exc.json() or exc.body[:500]}

    def fetch_book(self, market: str, depth: int = 25) -> OrderBook:
        raise VenueUnsupported(
            "the Coinbase sandbox exposes no market data - use "
            "CoinbasePublicVenue for real Coinbase prices"
        )
