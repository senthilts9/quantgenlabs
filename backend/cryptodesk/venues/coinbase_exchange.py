"""Coinbase Exchange sandbox — a real matching engine with fake funds.

**Three different things wear the Coinbase name in this package.** Confusing
them is the single most common mistake when wiring this venue:

======================  ==================  =========================
adapter                 matching engine     what it is for
======================  ==================  =========================
`coinbase_public`       n/a (read-only)     production market data
`coinbase_sandbox`      **none — fixtures** Advanced Trade request-shape
                                            conformance only
`coinbase_exchange`     **real**            this module: genuine order
                                            lifecycle, fake funds
======================  ==================  =========================

The Exchange sandbox hosts a subset of the production order books and supports
every exchange function except transfers. It is the only venue here that gives
a genuine order → ack → fill → position lifecycle without real money, and the
only one that publishes **L3 order-by-order depth** — which makes queue
position knowable rather than assumed. See `queue_ahead`.

Authentication has two traps that produce a signature which looks correct and
is rejected:

1. The API secret arrives base64-encoded. It must be **decoded to its 64 raw
   bytes** before being used as the HMAC key. Signing with the base64 *string*
   yields a well-formed, wrong signature.
2. The digest is **base64-encoded**, not hex — unlike most exchange APIs.

The prehash string is `timestamp + METHOD + requestPath + body`, where
`requestPath` includes the query string. Omitting the query is the third trap:
it only bites on the endpoints that take parameters, so it passes early
testing and fails later.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..core.errors import Code, Rejection, Stage
from ..core.instruments import (
    Instrument, InstrumentRegistry, MarketState, SettlementKind,
)
from ..core.types import (
    ZERO, AccountState, Balance, OrderBook, OrderRequest, OrderStatus, Quote,
    Side, TimeInForce, dec, now_ms,
)
from ..marketdata.http import DEFAULT_CLIENT, HttpClient, HttpError, TransportError
from ..vault.base import KeyVault, VaultError
from .base import Fill, NotAuthenticated, OrderAck, VenueAdapter, VenueError

#: Coinbase order status -> our lifecycle. `done` needs `done_reason` to
#: distinguish a fill from a cancel; collapsing them would report a cancelled
#: order as filled.
_STATUS = {
    "pending": OrderStatus.PENDING_NEW,
    "open": OrderStatus.NEW,
    "active": OrderStatus.NEW,
    "done": OrderStatus.FILLED,
    "rejected": OrderStatus.REJECTED,
}


class CoinbaseExchangeVenue(VenueAdapter):
    """Coinbase Exchange sandbox: real matching, real books, fake funds."""

    name = "coinbase_exchange"
    supports_trading = True
    has_matching_engine = True     # genuinely - see module docstring
    requires_signature = False     # session HMAC, not per-order signing
    is_mock = False

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[InstrumentRegistry] = None,
        client: Optional[HttpClient] = None,
        vault: Optional[KeyVault] = None,
        store=None,
        key_name: str = "coinbase-exchange-key",
        secret_name: str = "coinbase-exchange-secret",
        passphrase_name: str = "coinbase-exchange-passphrase",
    ):
        super().__init__(settings, registry, store=store)
        self.client = client or DEFAULT_CLIENT
        self.base_url = self.settings.resolve(self.name)
        self.vault = vault
        self.key_name = key_name
        self.secret_name = secret_name
        self.passphrase_name = passphrase_name
        #: Offset between our clock and the venue's, learned from /time.
        #: Coinbase rejects requests whose timestamp drifts too far, and a
        #: laptop that has slept is exactly where that bites.
        self._clock_skew_s: float = 0.0
        from ..validation.engine import for_venue
        self.validator = for_venue("coinbase", self.registry)

    # -- auth --------------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        if self.vault is None:
            return False
        try:
            return all(self.vault.reveal(n, reason="auth-check") for n in
                       (self.key_name, self.secret_name, self.passphrase_name))
        except VaultError:
            return False

    def sync_clock(self) -> float:
        """Learn our offset from the venue clock. Returns skew in seconds."""
        try:
            raw = self.client.get("%s/time" % self.base_url)
            server = float(raw["epoch"])
            self._clock_skew_s = server - time.time()
        except Exception:
            self._clock_skew_s = 0.0
        return self._clock_skew_s

    def _timestamp(self) -> str:
        return "%.0f" % (time.time() + self._clock_skew_s)

    def _sign(self, timestamp: str, method: str, request_path: str,
              body: str) -> str:
        """CB-ACCESS-SIGN over `timestamp + METHOD + requestPath + body`.

        The secret is base64-decoded to raw bytes first, and the digest is
        base64-encoded. Both are easy to get wrong in a way that still
        produces a plausible-looking header.
        """
        prehash = "%s%s%s%s" % (timestamp, method.upper(), request_path, body)

        def _hmac(secret_b64: str) -> str:
            key = base64.b64decode(secret_b64)
            digest = hmac.new(key, prehash.encode("utf-8"), hashlib.sha256).digest()
            return base64.b64encode(digest).decode("utf-8")

        if self.vault is None:
            raise NotAuthenticated("no vault configured for coinbase exchange")
        try:
            return self.vault.use(self.secret_name, _hmac, reason="cb-sign")
        except VaultError as exc:
            raise NotAuthenticated("coinbase secret unavailable: %s" % exc)

    def _reveal(self, name: str) -> str:
        if self.vault is None:
            raise NotAuthenticated(
                "no vault configured; store credentials with "
                "`cryptodesk keys set %s`" % name)
        try:
            return self.vault.reveal(name, reason="coinbase-request")
        except VaultError as exc:
            raise NotAuthenticated("%s unavailable: %s" % (name, exc))

    def _signed(self, method: str, path: str,
                params: Optional[Dict[str, Any]] = None,
                body: Optional[Dict[str, Any]] = None) -> Any:
        """Signed request. `path` must start with '/'."""
        request_path = path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                request_path = "%s?%s" % (path, urllib.parse.urlencode(clean))
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        ts = self._timestamp()
        headers = {
            "CB-ACCESS-KEY": self._reveal(self.key_name),
            "CB-ACCESS-SIGN": self._sign(ts, method, request_path, payload),
            "CB-ACCESS-TIMESTAMP": ts,
            "CB-ACCESS-PASSPHRASE": self._reveal(self.passphrase_name),
            "Content-Type": "application/json",
        }
        # The body is passed pre-serialised so the bytes signed are exactly the
        # bytes sent; re-encoding here could reorder keys and break the signature.
        return self.client.request(
            method, self.base_url + request_path, headers=headers,
            body=body if body else None,
        )

    # -- market data (public, no credentials) ------------------------------

    def fetch_instruments(self) -> List[Instrument]:
        raw = self.client.get("%s/products" % self.base_url)
        out: List[Instrument] = []
        for p in raw if isinstance(raw, list) else []:
            try:
                disabled = (p.get("trading_disabled") or p.get("status") != "online"
                            or p.get("cancel_only") or p.get("post_only"))
                out.append(Instrument(
                    symbol=p["id"],
                    base=p.get("base_currency", ""),
                    quote=p.get("quote_currency", ""),
                    kind=SettlementKind.SPOT,
                    state=(MarketState.POST_ONLY if p.get("post_only")
                           else MarketState.PAUSED if disabled
                           else MarketState.OPEN),
                    tick_size=dec(p.get("quote_increment", "0.01")),
                    step_size=dec(p.get("base_increment", "0.00000001")),
                    min_size=dec(p.get("base_min_size")
                                 or p.get("base_increment", "0.00000001")),
                    max_size=dec(p.get("base_max_size") or "1000000"),
                    min_notional=dec(p.get("min_market_funds", "1")),
                    max_leverage=Decimal(1),
                    price_band_bps=Decimal("1000"),
                    maker_fee_bps=Decimal("40"), taker_fee_bps=Decimal("60"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def fetch_book(self, market: str, depth: int = 25) -> OrderBook:
        level = 1 if depth <= 1 else 2
        raw = self.client.get("%s/products/%s/book" % (self.base_url, market),
                              params={"level": level})
        bids = [(lv[0], lv[1]) for lv in raw.get("bids", [])[:depth]]
        asks = [(lv[0], lv[1]) for lv in raw.get("asks", [])[:depth]]
        return OrderBook.from_levels(market, bids, asks, source=self.name)

    def fetch_quote(self, market: str) -> Quote:
        raw = self.client.get("%s/products/%s/ticker" % (self.base_url, market))
        if not isinstance(raw, dict) or raw.get("price") is None:
            raise VenueError("coinbase exchange: no price for %s" % market)
        return Quote(market, dec(raw["price"]), now_ms(), self.name)

    # -- L3: the part no other venue here offers ---------------------------

    def fetch_l3_book(self, market: str) -> Dict[str, List[Tuple[Decimal, Decimal, str]]]:
        """Full order-by-order depth: every resting order, in queue order.

        L2 aggregates each price level into one number, which is why most
        simulators cannot model queue position and fall back to assuming you
        are either first (optimistic) or last (pessimistic). L3 lists the
        individual orders in the sequence the engine will match them, so the
        question becomes answerable rather than assumed.
        """
        raw = self.client.get("%s/products/%s/book" % (self.base_url, market),
                              params={"level": 3})
        out: Dict[str, List[Tuple[Decimal, Decimal, str]]] = {}
        for side in ("bids", "asks"):
            out[side] = [
                (dec(lv[0]), dec(lv[1]), str(lv[2]))
                for lv in raw.get(side, []) if len(lv) >= 3
            ]
        return out

    def queue_ahead(self, market: str, side: Side, price: Decimal,
                    order_id: Optional[str] = None) -> Optional[Decimal]:
        """How much size must trade before an order at `price` fills.

        With `order_id`, counts only the orders genuinely ahead of yours in the
        queue. Without it, returns the whole resting size at that price — the
        amount a *newly placed* order would have to wait behind.

        This is the number that decides whether a passive strategy is viable:
        a 5 BTC queue ahead of you at a level that trades 0.1 BTC an hour means
        the order is decoration, not liquidity provision.
        """
        try:
            book = self.fetch_l3_book(market)
        except Exception:
            return None
        levels = book["bids"] if side is Side.BUY else book["asks"]
        target = dec(price)
        ahead = ZERO
        for lv_price, lv_size, lv_id in levels:
            if lv_price != target:
                continue
            if order_id is not None and lv_id == order_id:
                return ahead          # everything counted so far is ahead of us
            ahead += lv_size
        # order_id given but not found: it is not resting at that price.
        return None if order_id is not None else ahead

    # -- account and trading ----------------------------------------------

    def fetch_account(self) -> AccountState:
        raw = self._signed("GET", "/accounts")
        state = AccountState(account_id="coinbase-exchange-sandbox")
        total = ZERO
        for a in raw if isinstance(raw, list) else []:
            available = dec(a.get("available", "0"))
            hold = dec(a.get("hold", "0"))
            if available == ZERO and hold == ZERO:
                continue
            currency = a.get("currency", "?")
            state.balances[currency] = Balance(currency, available + hold, available)
            if currency in ("USD", "USDC"):
                total += available
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

        body: Dict[str, Any] = {
            "product_id": request.market,
            "side": request.side.value.lower(),
            "client_oid": request.client_order_id[:36],
        }
        if request.order_type.value == "MARKET":
            body["type"] = "market"
            body["size"] = str(request.size)
        else:
            body["type"] = "limit"
            body["price"] = str(request.price)
            body["size"] = str(request.size)
            # Coinbase spells post-only as a flag, and IOC/FOK as time_in_force.
            if request.post_only:
                body["post_only"] = True
            tif = {TimeInForce.IOC: "IOC", TimeInForce.FOK: "FOK"}.get(
                request.time_in_force)
            if tif:
                body["time_in_force"] = tif

        self.record_submission(request)
        try:
            raw = self._signed("POST", "/orders", body=body)
        except HttpError as exc:
            return self._ack_from_error(request, exc)
        except TransportError as exc:
            # No answer: the order may or may not exist. UNKNOWN forces
            # reconciliation rather than a retry that could double-fill.
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.UNKNOWN, venue=self.name,
                raw={"error": str(exc)},
            )
        return self._ack_from_response(request, raw)

    def _ack_from_response(self, request: OrderRequest, raw: Dict) -> OrderAck:
        status = _STATUS.get(raw.get("status", ""), OrderStatus.UNKNOWN)
        # `done` alone does not mean filled - a cancelled order is also done.
        if raw.get("status") == "done":
            reason = raw.get("done_reason", "")
            if reason == "canceled":
                status = OrderStatus.CANCELLED
            elif dec(raw.get("filled_size", "0")) <= ZERO:
                status = OrderStatus.CANCELLED
        filled = dec(raw.get("filled_size", "0"))
        spent = dec(raw.get("executed_value", "0"))
        return OrderAck(
            client_order_id=request.client_order_id,
            order_id=str(raw.get("id", "")),
            status=status,
            filled_size=filled,
            avg_price=(spent / filled) if filled > ZERO else None,
            venue=self.name, raw=raw,
        )

    def _ack_from_error(self, request: OrderRequest, exc: HttpError) -> OrderAck:
        """Map Coinbase's message strings onto the rejection taxonomy.

        Coinbase returns a flat `{"message": "..."}` with the HTTP status
        carrying most of the meaning, so the text is the only discriminator
        available. Matching on substrings is fragile, which is why anything
        unrecognised keeps its message rather than being forced into a code.
        """
        payload = exc.json() or {}
        message = str(payload.get("message", exc.body[:200]))
        low = message.lower()

        code, stage = Code.MARKET_NOT_TRADEABLE, Stage.INSTRUMENT
        if "insufficient funds" in low:
            code, stage = Code.INSUFFICIENT_BALANCE, Stage.ACCOUNT
        elif "post only" in low or "post-only" in low:
            code, stage = Code.POST_ONLY_WOULD_CROSS, Stage.MARKET
        elif "size is too small" in low or "min" in low and "size" in low:
            code, stage = Code.BELOW_MIN_SIZE, Stage.INSTRUMENT
        elif "price" in low and "increment" in low:
            code, stage = Code.PRICE_NOT_MULTIPLE_OF_TICK, Stage.INSTRUMENT
        elif "size" in low and "increment" in low:
            code, stage = Code.SIZE_NOT_MULTIPLE_OF_STEP, Stage.INSTRUMENT
        elif exc.status == 401 or "invalid signature" in low or "invalid api key" in low:
            code, stage = Code.SIGNATURE_INVALID, Stage.AUTH
        elif exc.status == 429 or "rate limit" in low:
            code, stage = Code.RATE_LIMITED, Stage.THROTTLE
        elif "product not found" in low:
            code, stage = Code.MARKET_NOT_FOUND, Stage.SCHEMA

        return OrderAck(
            client_order_id=request.client_order_id,
            status=OrderStatus.REJECTED, venue=self.name,
            raw={"status": exc.status, "message": message},
            rejections=[Rejection(code, stage,
                                  "coinbase exchange %d: %s" % (exc.status, message),
                                  {"http_status": exc.status})],
        )

    def cancel_order(self, order_id: str, market: Optional[str] = None) -> OrderAck:
        try:
            self._signed("DELETE", "/orders/%s" % order_id)
        except HttpError as exc:
            payload = exc.json() or {}
            # 404 means already filled or already cancelled, not "we failed".
            status = (OrderStatus.UNKNOWN if exc.status == 404
                      else OrderStatus.REJECTED)
            return OrderAck(client_order_id="", order_id=order_id, status=status,
                            venue=self.name, raw=payload)
        return OrderAck(client_order_id="", order_id=order_id,
                        status=OrderStatus.CANCELLED, venue=self.name)

    def fetch_open_orders(self) -> Dict[str, OrderRequest]:
        from ..core.types import OrderType
        raw = self._signed("GET", "/orders", params={"status": "open"})
        out: Dict[str, OrderRequest] = {}
        for o in raw if isinstance(raw, list) else []:
            try:
                out[str(o["id"])] = OrderRequest(
                    market=o["product_id"], side=Side(o["side"].upper()),
                    order_type=(OrderType.LIMIT if o.get("type") == "limit"
                                else OrderType.MARKET),
                    size=dec(o.get("size", "0")),
                    price=dec(o["price"]) if o.get("price") else None,
                    post_only=bool(o.get("post_only")),
                    client_order_id=o.get("client_oid", ""),
                )
            except (KeyError, ValueError):
                continue
        return out

    def fetch_fills(self, market: Optional[str] = None) -> List[Fill]:
        """Executions, which is what TCA is computed from.

        `liquidity` maps Coinbase's M/T flag onto maker/taker, because the fee
        and the execution quality read very differently depending on which one
        you were.
        """
        params = {"product_id": market} if market else {"limit": 100}
        raw = self._signed("GET", "/fills", params=params)
        out: List[Fill] = []
        for f in raw if isinstance(raw, list) else []:
            try:
                out.append(Fill(
                    order_id=str(f.get("order_id", "")),
                    market=f.get("product_id", ""),
                    side=str(f.get("side", "buy")).upper(),
                    size=dec(f.get("size", "0")),
                    price=dec(f.get("price", "0")),
                    fee=dec(f.get("fee", "0")),
                    liquidity=("maker" if f.get("liquidity") == "M" else "taker"),
                    trade_id=str(f.get("trade_id", "")),
                ))
            except (KeyError, ValueError):
                continue
        return out
