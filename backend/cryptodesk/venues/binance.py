"""Binance Spot testnet.

The most practical venue in this package for end-to-end testing: a real
matching engine, free API keys from testnet.binance.vision with no mainnet
account or deposit required, and an order lifecycle that behaves like the
production one. Balances are reset periodically, which is a feature - it makes
every test run start from a known state.

Authentication is HMAC-SHA256 over the query string, session-level rather than
per-order, so there is no client-side order signing here. The secret is pulled
from the vault for the duration of one signature and not retained.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from decimal import Decimal
from typing import Dict, List, Optional

from ..config import Settings
from ..core.errors import Code, Rejection, Stage
from ..core.instruments import Instrument, InstrumentRegistry, MarketState, SettlementKind
from ..core.types import (
    ZERO, AccountState, Balance, OrderBook, OrderRequest, OrderStatus, Quote,
    Side, TimeInForce, dec, now_ms,
)
from ..marketdata.http import DEFAULT_CLIENT, HttpClient, HttpError, TransportError
from ..vault.base import KeyVault, VaultError
from .base import Fill, NotAuthenticated, OrderAck, VenueAdapter, VenueError

#: Binance status -> our lifecycle. PENDING_CANCEL and EXPIRED_IN_MATCH exist
#: on the wire and must map somewhere, or an unknown status silently becomes
#: None and the OMS loses the order.
_STATUS = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "PENDING_CANCEL": OrderStatus.PENDING_CANCEL,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}


class BinanceTestnetVenue(VenueAdapter):
    """Binance Spot testnet with a real matching engine."""

    name = "binance"
    supports_trading = True
    has_matching_engine = True
    requires_signature = False  # session HMAC, not per-order signing

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[InstrumentRegistry] = None,
        client: Optional[HttpClient] = None,
        store=None,
        vault: Optional[KeyVault] = None,
        api_key_name: str = "binance-testnet-key",
        api_secret_name: str = "binance-testnet-secret",
        recv_window_ms: int = 5000,
    ):
        super().__init__(settings, registry, store=store)
        self.client = client or DEFAULT_CLIENT
        self.base_url = self.settings.resolve(self.name)
        self.vault = vault
        self.api_key_name = api_key_name
        self.api_secret_name = api_secret_name
        # Bounds how long a signed request stays valid at the venue - Binance's
        # own replay protection. Too high re-opens the replay window; too low
        # and ordinary network jitter rejects good orders.
        self.recv_window_ms = recv_window_ms
        from ..validation.engine import for_venue
        self.validator = for_venue("binance", self.registry)

    # -- auth --------------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        if self.vault is None:
            return False
        try:
            return bool(self.vault.reveal(self.api_key_name, reason="auth-check"))
        except VaultError:
            return False

    def _api_key(self) -> str:
        if self.vault is None:
            raise NotAuthenticated(
                "no vault configured; store a testnet key with "
                "`cryptodesk keys set binance-testnet-key`"
            )
        try:
            return self.vault.reveal(self.api_key_name, reason="binance-request")
        except VaultError as exc:
            raise NotAuthenticated("binance API key unavailable: %s" % exc)

    def _sign(self, params: Dict[str, object]) -> str:
        """HMAC-SHA256 over the urlencoded query string.

        Parameter order is part of the signature: Binance verifies the exact
        string it received, so the query must be built once and both signed and
        sent. Re-encoding the dict afterwards can reorder keys and invalidate
        an otherwise-correct signature.
        """
        query = urllib.parse.urlencode(params)
        if self.vault is None:
            raise NotAuthenticated("no vault configured for binance secret")
        try:
            sig = self.vault.use(
                self.api_secret_name,
                lambda secret: hmac.new(
                    secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
                ).hexdigest(),
                reason="binance-sign",
            )
        except VaultError as exc:
            raise NotAuthenticated("binance API secret unavailable: %s" % exc)
        return "%s&signature=%s" % (query, sig)

    def _signed_request(self, method: str, path: str,
                        params: Optional[Dict] = None) -> object:
        p = dict(params or {})
        p["timestamp"] = now_ms()
        p["recvWindow"] = self.recv_window_ms
        query = self._sign(p)
        return self.client.request(
            method, "%s%s?%s" % (self.base_url, path, query),
            headers={"X-MBX-APIKEY": self._api_key()},
        )

    # -- symbols -----------------------------------------------------------

    def _symbol(self, market: str) -> str:
        sym = market.upper().replace("-", "").replace("_", "")
        return sym.replace("USD", "USDT") if sym.endswith("USD") else sym

    # -- market data (public, no key) --------------------------------------

    def fetch_book(self, market: str, depth: int = 20) -> OrderBook:
        legal = [5, 10, 20, 50, 100, 500, 1000, 5000]
        limit = next((d for d in legal if d >= depth), 100)
        raw = self.client.get("%s/api/v3/depth" % self.base_url,
                              params={"symbol": self._symbol(market), "limit": limit})
        return OrderBook.from_levels(market, raw.get("bids", []),
                                     raw.get("asks", []), source=self.name)

    def fetch_quote(self, market: str) -> Quote:
        raw = self.client.get("%s/api/v3/ticker/price" % self.base_url,
                              params={"symbol": self._symbol(market)})
        return Quote(market, dec(raw["price"]), now_ms(), self.name)

    def fetch_instruments(self) -> List[Instrument]:
        """Build specs from `exchangeInfo` filters.

        Binance publishes constraints as a list of typed filter objects rather
        than named fields, so tick, step and min-notional each have to be
        located by `filterType`. Missing filters fall back to permissive
        defaults - a too-loose local spec is caught by the venue, whereas a
        too-tight one silently blocks valid orders.
        """
        raw = self.client.get("%s/api/v3/exchangeInfo" % self.base_url)
        out: List[Instrument] = []
        for s in (raw.get("symbols") or []) if isinstance(raw, dict) else []:
            filters = {f.get("filterType"): f for f in s.get("filters", [])}
            price_f = filters.get("PRICE_FILTER", {})
            lot_f = filters.get("LOT_SIZE", {})
            notional_f = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
            percent_f = filters.get("PERCENT_PRICE_BY_SIDE", {})

            # Binance expresses its price band as a multiplier, not bps.
            band_bps = Decimal("1000")
            up = percent_f.get("askMultiplierUp") or percent_f.get("multiplierUp")
            if up:
                try:
                    band_bps = (dec(up) - Decimal(1)) * Decimal(10000)
                except Exception:
                    pass

            try:
                out.append(Instrument(
                    symbol=s["symbol"],
                    base=s.get("baseAsset", ""),
                    quote=s.get("quoteAsset", ""),
                    kind=SettlementKind.SPOT,
                    state=(MarketState.OPEN if s.get("status") == "TRADING"
                           else MarketState.PAUSED),
                    tick_size=dec(price_f.get("tickSize", "0.01")),
                    step_size=dec(lot_f.get("stepSize", "0.00000001")),
                    min_size=dec(lot_f.get("minQty", "0.00000001")),
                    max_size=dec(lot_f.get("maxQty", "1000000")),
                    min_notional=dec(notional_f.get("minNotional", "1")),
                    max_notional=dec(notional_f.get("maxNotional", "9000000")),
                    max_leverage=Decimal(1),
                    price_band_bps=band_bps,
                    maker_fee_bps=Decimal("10"), taker_fee_bps=Decimal("10"),
                    supported_order_types=frozenset(
                        s.get("orderTypes", ["LIMIT", "MARKET"])
                    ),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # -- account and trading ----------------------------------------------

    def fetch_account(self) -> AccountState:
        raw = self._signed_request("GET", "/api/v3/account")
        state = AccountState(account_id="binance-testnet")
        total = ZERO
        for b in (raw.get("balances") or []) if isinstance(raw, dict) else []:
            free, locked = dec(b.get("free", "0")), dec(b.get("locked", "0"))
            if free == ZERO and locked == ZERO:
                continue
            state.balances[b["asset"]] = Balance(b["asset"], free + locked, free)
            if b["asset"] in ("USDT", "USDC", "BUSD"):
                total += free
        state.free_collateral = total
        state.total_collateral = total
        return state

    def place_order(self, request: OrderRequest) -> OrderAck:
        report = self.validator.check(request, **self.replay_kwargs())
        if not report.ok:
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED, rejections=list(report.rejections),
                report=report, venue=self.name,
            )

        self.record_submission(request)
        params: Dict[str, object] = {
            "symbol": self._symbol(request.market),
            "side": request.side.value,
            "type": "MARKET" if request.order_type.value == "MARKET" else "LIMIT",
            "quantity": str(request.size),
            "newClientOrderId": request.client_order_id[:36],
        }
        if params["type"] == "LIMIT":
            params["price"] = str(request.price)
            # LIMIT_MAKER is Binance's post-only: rejected outright if it would
            # cross, which is exactly the post-only contract.
            if request.post_only:
                params["type"] = "LIMIT_MAKER"
                params.pop("timeInForce", None)
            else:
                params["timeInForce"] = {
                    TimeInForce.IOC: "IOC", TimeInForce.FOK: "FOK",
                }.get(request.time_in_force, "GTC")

        try:
            raw = self._signed_request("POST", "/api/v3/order", params)
        except HttpError as exc:
            return self._ack_from_error(request, exc)
        except TransportError as exc:
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.UNKNOWN, venue=self.name,
                raw={"error": str(exc)},
            )
        return self._ack_from_response(request, raw)

    def _ack_from_response(self, request: OrderRequest, raw: Dict) -> OrderAck:
        fills = []
        for f in (raw.get("fills") or []):
            fills.append(Fill(
                order_id=str(raw.get("orderId", "")), market=request.market,
                side=request.side.value, size=dec(f.get("qty", "0")),
                price=dec(f.get("price", "0")), fee=dec(f.get("commission", "0")),
                liquidity="taker", trade_id=str(f.get("tradeId", "")),
            ))
        filled = dec(raw.get("executedQty", "0"))
        quote_qty = dec(raw.get("cummulativeQuoteQty", "0"))
        return OrderAck(
            client_order_id=raw.get("clientOrderId") or request.client_order_id,
            order_id=str(raw.get("orderId", "")),
            status=_STATUS.get(raw.get("status", ""), OrderStatus.UNKNOWN),
            filled_size=filled,
            avg_price=(quote_qty / filled) if filled > ZERO else None,
            fills=fills, venue=self.name, raw=raw,
        )

    def _ack_from_error(self, request: OrderRequest, exc: HttpError) -> OrderAck:
        """Map Binance error codes onto our rejection taxonomy.

        Worth doing properly: -2010 covers both insufficient balance and a
        rejected post-only, and the caller's correct response differs - top up
        versus reprice. The message text is the only discriminator Binance
        gives.
        """
        payload = exc.json() or {}
        venue_code = payload.get("code")
        msg = payload.get("msg", exc.body[:200])
        code = Code.MARKET_NOT_TRADEABLE
        stage = Stage.INSTRUMENT
        if venue_code == -2010:
            if "immediately match" in msg.lower():
                code, stage = Code.POST_ONLY_WOULD_CROSS, Stage.MARKET
            else:
                code, stage = Code.INSUFFICIENT_BALANCE, Stage.ACCOUNT
        elif venue_code == -1013:
            code, stage = Code.BELOW_MIN_NOTIONAL, Stage.INSTRUMENT
        elif venue_code == -1021:
            code, stage = Code.SIGNATURE_EXPIRED, Stage.AUTH
        elif venue_code in (-1022, -2014, -2015):
            code, stage = Code.SIGNATURE_INVALID, Stage.AUTH
        elif venue_code == -1003:
            code, stage = Code.RATE_LIMITED, Stage.THROTTLE
        return OrderAck(
            client_order_id=request.client_order_id,
            status=OrderStatus.REJECTED, venue=self.name,
            raw={"status": exc.status, "code": venue_code, "msg": msg},
            rejections=[Rejection(code, stage, "binance %s: %s" % (venue_code, msg),
                                  {"venue_code": venue_code})],
        )

    def cancel_order(self, order_id: str, market: Optional[str] = None) -> OrderAck:
        if not market:
            raise VenueError("binance requires a symbol to cancel an order")
        try:
            raw = self._signed_request("DELETE", "/api/v3/order", {
                "symbol": self._symbol(market), "orderId": order_id,
            })
        except HttpError as exc:
            payload = exc.json() or {}
            # -2011 is "unknown order": already filled or already cancelled.
            status = (OrderStatus.UNKNOWN if payload.get("code") == -2011
                      else OrderStatus.REJECTED)
            return OrderAck(client_order_id="", order_id=order_id, status=status,
                            venue=self.name, raw=payload)
        return OrderAck(
            client_order_id=raw.get("origClientOrderId", ""),
            order_id=str(raw.get("orderId", order_id)),
            status=_STATUS.get(raw.get("status", ""), OrderStatus.CANCELLED),
            venue=self.name, raw=raw,
        )

    def fetch_open_orders(self) -> Dict[str, OrderRequest]:
        from ..core.types import OrderType
        raw = self._signed_request("GET", "/api/v3/openOrders")
        out: Dict[str, OrderRequest] = {}
        for o in raw if isinstance(raw, list) else []:
            try:
                out[str(o["orderId"])] = OrderRequest(
                    market=o["symbol"], side=Side(o["side"]),
                    order_type=OrderType.LIMIT if o.get("price") else OrderType.MARKET,
                    size=dec(o.get("origQty", "0")),
                    price=dec(o["price"]) if o.get("price") else None,
                    client_order_id=o.get("clientOrderId", ""),
                )
            except (KeyError, ValueError):
                continue
        return out
