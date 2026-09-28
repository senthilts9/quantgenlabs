"""Paradex - Starknet perpetuals DEX, testnet.

The reference implementation in this package for a *signed-order* venue, and
the one whose validation model the whole `validation` package is shaped after.

Three things make it structurally different from a centralised exchange:

1. **Onboarding.** An account must be registered with the venue before it can
   trade, by signing an onboarding message. There is no "create an account"
   form; the key is the account.
2. **JWT session auth.** A short-lived JWT is obtained by signing a typed-data
   auth request with the Starknet key. It expires, and the client re-mints it.
3. **Per-order signatures.** Every order carries a SNIP-12 typed-data
   signature over its own economic terms - market, side, type, size, price,
   timestamp. The venue verifies it, and so does the settlement layer. An
   order is therefore self-authenticating: the exchange cannot alter its terms
   without invalidating the signature, which is the property a centralised
   venue cannot offer.

Public market data needs none of that and works with no credentials, so the
instrument specs and order books below are usable immediately. Trading
requires `starknet-py` and a funded testnet account; without them this adapter
fails loudly at construction rather than at order time.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from ..config import Settings
from ..core.errors import Code, Rejection, Stage
from ..core.instruments import Instrument, InstrumentRegistry, MarketState, SettlementKind
from ..core.types import (
    ZERO, AccountState, Balance, FundingRate, OrderBook, OrderRequest,
    OrderStatus, Position, Quote, Side, TimeInForce, dec, now_ms,
)
from ..marketdata.http import DEFAULT_CLIENT, HttpClient, HttpError, TransportError
from ..vault.base import KeyVault, Signer
from ..vault.signers import paradex_order_typed_data
from .base import Fill, NotAuthenticated, OrderAck, VenueAdapter, VenueError

_STATUS = {
    "NEW": OrderStatus.NEW,
    "OPEN": OrderStatus.NEW,
    "UNTRIGGERED": OrderStatus.NEW,
    "CLOSED": OrderStatus.FILLED,
}


class ParadexVenue(VenueAdapter):
    """Paradex testnet adapter."""

    name = "paradex"
    supports_trading = True
    has_matching_engine = True
    requires_signature = True

    #: JWT lifetime is short; re-mint with this much margin to spare so a
    #: token does not expire between the check and the request that uses it.
    JWT_REFRESH_MARGIN_MS = 60_000

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[InstrumentRegistry] = None,
        client: Optional[HttpClient] = None,
        vault: Optional[KeyVault] = None,
        signer: Optional[Signer] = None,
        account_address: Optional[str] = None,
        store=None,
    ):
        super().__init__(settings, registry, store=store)
        self.client = client or DEFAULT_CLIENT
        self.base_url = self.settings.resolve(self.name)
        self.vault = vault
        self.signer = signer
        self.account_address = account_address
        self._jwt: Optional[str] = None
        self._jwt_expires_ms: int = 0
        self._config: Optional[Dict] = None
        from ..validation.engine import for_venue
        self.validator = for_venue("paradex", self.registry)

    # -- system config -----------------------------------------------------

    def system_config(self) -> Dict:
        """Venue config, including the Starknet chain id used in signatures.

        Cached: it is static per deployment, and the chain id is needed on
        every order. Fetching it per order would add a round trip to the
        latency-critical path for a value that never changes.
        """
        if self._config is None:
            self._config = self.client.get("%s/system/config" % self.base_url)
        return self._config or {}

    @property
    def chain_id(self) -> str:
        cfg = self.system_config()
        return (cfg.get("starknet_chain_id")
                or cfg.get("l2_chain_id")
                or "PRIVATE_SN_POTC_SEPOLIA")

    # -- public market data (no credentials) -------------------------------

    #: `asset_kind` values this adapter models. Options are deliberately
    #: excluded: testnet lists ~4000 of them, and an option's risk is driven by
    #: strike, expiry and greeks, none of which the linear validation model
    #: here represents. Labelling them PERPETUAL would silently margin them
    #: wrong, which is worse than not listing them.
    SUPPORTED_ASSET_KINDS = {"PERP": SettlementKind.PERPETUAL,
                             "SPOT": SettlementKind.SPOT}

    def fetch_instruments(self, include_options: bool = False) -> List[Instrument]:
        """Instrument specs from `/markets`, using the venue's own numbers.

        Paradex publishes the constraints this package would otherwise guess:

        * `price_bands_width` is a fraction ("0.05" = 5%), not bps - it is the
          real fat-finger band, so the validator agrees with the venue instead
          of approximating it.
        * `delta1_cross_margin_params.imf_base` is the initial margin fraction;
          max leverage is its reciprocal. There is no `max_leverage` field, and
          defaulting to 20 understated BTC's real 50x.
        * `position_limit` is a size in *base units*, not a notional, and is
          separate from `max_order_size` - BTC caps the position at 60 coins
          while permitting a single 100-coin order.
        * fees live under `fee_config.api_fee`, as fractions.
        """
        raw = self.client.get("%s/markets" % self.base_url)
        out: List[Instrument] = []
        for m in (raw.get("results") or []) if isinstance(raw, dict) else []:
            try:
                kind_raw = (m.get("asset_kind") or "PERP").upper()
                kind = self.SUPPORTED_ASSET_KINDS.get(kind_raw)
                if kind is None and not include_options:
                    continue
                if kind is None:
                    kind = SettlementKind.PERPETUAL

                symbol = m["symbol"]
                step = dec(m.get("order_size_increment", "0.001"))

                # price_bands_width is a fraction of price; convert to bps.
                band = m.get("price_bands_width")
                band_bps = (dec(band) * Decimal(10000)) if band else Decimal("500")

                # imf_base -> initial margin fraction -> leverage.
                margin = m.get("delta1_cross_margin_params") or {}
                imf = dec(margin["imf_base"]) if margin.get("imf_base") else None
                max_lev = (Decimal(1) / imf) if imf and imf > ZERO else Decimal("20")
                mmf_factor = (dec(margin["mmf_factor"])
                              if margin.get("mmf_factor") else Decimal("0.5"))

                fees = ((m.get("fee_config") or {}).get("api_fee") or {})
                maker = ((fees.get("maker_fee") or {}).get("fee")) or "0"
                taker = ((fees.get("taker_fee") or {}).get("fee")) or "0.0002"

                slip = m.get("max_slippage")
                mode = (m.get("trading_mode") or "STANDARD").upper()

                out.append(Instrument(
                    symbol=symbol,
                    base=m.get("base_currency") or symbol.split("-")[0],
                    quote=m.get("quote_currency", "USD"),
                    kind=kind,
                    state=(MarketState.OPEN if mode == "STANDARD"
                           else MarketState.REDUCE_ONLY),
                    tick_size=dec(m.get("price_tick_size", "0.1")),
                    step_size=step,
                    min_size=step,  # one increment is the smallest legal order
                    max_size=dec(m.get("max_order_size", "1000")),
                    min_notional=dec(m.get("min_notional", "10")),
                    max_notional=Decimal("50000000"),
                    max_position_size=(dec(m["position_limit"])
                                       if m.get("position_limit") else None),
                    max_slippage_bps=((dec(slip) * Decimal(10000))
                                      if slip else None),
                    max_open_orders=m.get("max_open_orders"),
                    funding_period_hours=int(m.get("funding_period_hours") or 8),
                    max_funding_rate=(dec(m["max_funding_rate"])
                                      if m.get("max_funding_rate") else None),
                    max_leverage=max_lev,
                    initial_margin_fraction=imf,
                    maintenance_margin_fraction=((imf * mmf_factor) if imf
                                                 else Decimal("0.03")),
                    price_band_bps=band_bps,
                    maker_fee_bps=dec(maker) * Decimal(10000),
                    taker_fee_bps=dec(taker) * Decimal(10000),
                    supported_order_types=frozenset(
                        m.get("order_types") or ["LIMIT", "MARKET"]
                    ),
                ))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue  # one malformed market must not lose the whole list
        return out

    def fetch_book(self, market: str, depth: int = 20) -> OrderBook:
        raw = self.client.get("%s/orderbook/%s" % (self.base_url, market),
                              params={"depth": depth})
        return OrderBook.from_levels(market, raw.get("bids", []),
                                     raw.get("asks", []), source=self.name)

    def fetch_quote(self, market: str) -> Quote:
        """Prefer the venue's published mark price over a local mid.

        On a perp the mark is index-anchored and is what margin and liquidation
        are computed against; the book mid is not. Using the mid here would
        make the validator's price bands disagree with the venue's.
        """
        raw = self.client.get("%s/markets/summary" % self.base_url,
                              params={"market": market})
        rows = (raw.get("results") or []) if isinstance(raw, dict) else []
        for row in rows:
            price = row.get("mark_price") or row.get("last_traded_price")
            if price:
                return Quote(market, dec(price), now_ms(), "%s:mark" % self.name)
        book = self.fetch_book(market, depth=1)
        if book.mid is None:
            raise VenueError("paradex: no mark or mid for %s" % market)
        return Quote(market, book.mid, book.timestamp_ms, "%s:mid" % self.name)

    def fetch_funding_rate(self, market: str) -> Optional[FundingRate]:
        """Live funding rate from `/markets/summary`.

        The period comes from the instrument spec rather than the summary,
        because the same numeric rate means a very different annualised cost
        depending on whether it settles every 8 hours or every hour.
        """
        raw = self.client.get("%s/markets/summary" % self.base_url,
                              params={"market": market})
        for row in (raw.get("results") or []) if isinstance(raw, dict) else []:
            value = row.get("funding_rate")
            if value is None:
                continue
            inst = self.registry.get(market)
            period = inst.funding_period_hours if inst else 8
            return FundingRate(market=market, rate=dec(value),
                               period_hours=period, source="paradex")
        return None

    # -- auth --------------------------------------------------------------

    def _require_signer(self) -> Signer:
        if self.signer is None:
            raise NotAuthenticated(
                "Paradex trading needs a Starknet signer. Install `starknet-py`, "
                "store the L2 key in the vault, and pass account_address."
            )
        if getattr(self.signer, "is_test", False):
            # The test signer produces an HMAC, not a Stark signature. It would
            # be rejected remotely; failing here says why.
            raise NotAuthenticated(
                "DeterministicTestSigner cannot sign real Paradex orders - it "
                "produces test digests, not Stark-curve signatures."
            )
        return self.signer

    def authenticate(self, force: bool = False) -> str:
        """Mint a JWT by signing the auth typed data.

        Re-mints when the current token is inside its refresh margin. Returning
        an expiring token would surface as a 401 mid-order, which reads like a
        credential problem rather than a clock one.
        """
        if (not force and self._jwt
                and now_ms() < self._jwt_expires_ms - self.JWT_REFRESH_MARGIN_MS):
            return self._jwt

        signer = self._require_signer()
        timestamp = now_ms() // 1000
        expiration = timestamp + 24 * 60 * 60
        typed = {
            "domain": {"name": "Paradex", "chainId": self.chain_id, "version": "1"},
            "types": {
                "StarkNetDomain": [
                    {"name": "name", "type": "felt"},
                    {"name": "chainId", "type": "felt"},
                    {"name": "version", "type": "felt"},
                ],
                "Request": [
                    {"name": "method", "type": "felt"},
                    {"name": "path", "type": "felt"},
                    {"name": "body", "type": "felt"},
                    {"name": "timestamp", "type": "felt"},
                    {"name": "expiration", "type": "felt"},
                ],
            },
            "message": {
                "method": "POST", "path": "/v1/auth", "body": "",
                "timestamp": str(timestamp), "expiration": str(expiration),
            },
        }
        sig = signer.sign_typed_data(typed["domain"], typed["types"], typed["message"])
        try:
            raw = self.client.post("%s/auth" % self.base_url, headers={
                "PARADEX-STARKNET-ACCOUNT": self.account_address or signer.address(),
                "PARADEX-STARKNET-SIGNATURE": sig.value,
                "PARADEX-TIMESTAMP": str(timestamp),
                "PARADEX-SIGNATURE-EXPIRATION": str(expiration),
            })
        except HttpError as exc:
            raise NotAuthenticated("paradex auth failed: HTTP %d %s"
                                   % (exc.status, exc.body[:200]))
        token = raw.get("jwt_token") if isinstance(raw, dict) else None
        if not token:
            raise NotAuthenticated("paradex auth returned no jwt_token")
        self._jwt = token
        self._jwt_expires_ms = expiration * 1000
        return token

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer %s" % self.authenticate()}

    def onboard(self) -> Dict:
        """Register the account with the venue. Required once before trading."""
        signer = self._require_signer()
        typed = {
            "domain": {"name": "Paradex", "chainId": self.chain_id, "version": "1"},
            "types": {
                "StarkNetDomain": [
                    {"name": "name", "type": "felt"},
                    {"name": "chainId", "type": "felt"},
                    {"name": "version", "type": "felt"},
                ],
                "Constant": [{"name": "action", "type": "felt"}],
            },
            "message": {"action": "Onboarding"},
        }
        sig = signer.sign_typed_data(typed["domain"], typed["types"], typed["message"])
        return self.client.post("%s/onboarding" % self.base_url,
                                body={"public_key": signer.address()},
                                headers={
                                    "PARADEX-STARKNET-ACCOUNT":
                                        self.account_address or signer.address(),
                                    "PARADEX-STARKNET-SIGNATURE": sig.value,
                                })

    # -- account and trading ----------------------------------------------

    def fetch_account(self) -> AccountState:
        raw = self.client.get("%s/account" % self.base_url,
                              headers=self._auth_headers())
        state = AccountState(
            account_id=self.account_address or "paradex",
            free_collateral=dec(raw.get("free_collateral", "0")),
            total_collateral=dec(raw.get("account_value", "0")),
        )
        try:
            positions = self.client.get("%s/positions" % self.base_url,
                                        headers=self._auth_headers())
            for p in (positions.get("results") or []):
                size = dec(p.get("size", "0"))
                if size == ZERO:
                    continue
                state.positions[p["market"]] = Position(
                    market=p["market"], size=size,
                    entry_price=dec(p.get("average_entry_price", "0")),
                    mark_price=dec(p.get("mark_price", "0")),
                )
        except (HttpError, TransportError, KeyError, ValueError):
            pass  # positions are enrichment; an account with none is valid
        return state

    def place_order(self, request: OrderRequest) -> OrderAck:
        """Sign and submit an order.

        The signature is minted over the *same* economic terms that go in the
        body, built from one typed-data structure, so the signed message and
        the transmitted order cannot diverge. Building them separately is the
        classic source of "signature invalid" against a body that looks right.
        """
        signer = self._require_signer()

        book = None
        mark = None
        try:
            book = self.fetch_book(request.market, depth=10)
            mark = self.fetch_quote(request.market)
        except (HttpError, TransportError, VenueError):
            pass  # validate on what we have; the venue re-checks anyway

        timestamp = now_ms()
        typed = paradex_order_typed_data(request, self.chain_id, timestamp)
        sig = signer.sign_typed_data(typed["domain"], typed["types"], typed["message"])
        request.signature = sig.value
        request.signature_timestamp_ms = timestamp
        request.account = self.account_address or signer.address()

        report = self.validator.check(
            request, book=book, mark=mark,
            require_signature=True,
            **self.replay_kwargs()
        )
        if not report.ok:
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED, rejections=list(report.rejections),
                report=report, venue=self.name,
            )

        self.record_submission(request)
        body = {
            "market": request.market,
            "side": request.side.value,
            "type": request.order_type.value,
            "size": str(request.size),
            "client_id": request.client_order_id,
            "signature": sig.value,
            "signature_timestamp": timestamp,
            "instruction": ("POST_ONLY" if request.post_only
                            else request.time_in_force.value),
        }
        if request.price is not None:
            body["price"] = str(request.price)
        if request.reduce_only:
            body["flags"] = ["REDUCE_ONLY"]

        try:
            raw = self.client.post("%s/orders" % self.base_url, body=body,
                                   headers=self._auth_headers())
        except HttpError as exc:
            return self._ack_from_error(request, exc)
        except TransportError as exc:
            return OrderAck(
                client_order_id=request.client_order_id,
                status=OrderStatus.UNKNOWN, venue=self.name,
                raw={"error": str(exc)},
            )
        return OrderAck(
            client_order_id=request.client_order_id,
            order_id=str(raw.get("id", "")),
            status=_STATUS.get(raw.get("status", ""), OrderStatus.NEW),
            filled_size=dec(raw.get("size_filled", "0") or "0"),
            venue=self.name, raw=raw,
        )

    def _ack_from_error(self, request: OrderRequest, exc: HttpError) -> OrderAck:
        payload = exc.json() or {}
        venue_code = payload.get("error") or payload.get("code")
        message = payload.get("message", exc.body[:200])
        mapping = {
            "INVALID_ORDER_SIGNATURE": (Code.SIGNATURE_INVALID, Stage.AUTH),
            "ORDER_ID_NOT_FOUND": (Code.MARKET_NOT_FOUND, Stage.SCHEMA),
            "INSUFFICIENT_FREE_COLLATERAL": (Code.INSUFFICIENT_MARGIN, Stage.ACCOUNT),
            "INVALID_PRICE_PRECISION": (Code.PRICE_NOT_MULTIPLE_OF_TICK, Stage.INSTRUMENT),
            "INVALID_SIZE_PRECISION": (Code.SIZE_NOT_MULTIPLE_OF_STEP, Stage.INSTRUMENT),
            "MARKET_NOT_FOUND": (Code.MARKET_NOT_FOUND, Stage.SCHEMA),
            "ORDER_WOULD_CROSS": (Code.POST_ONLY_WOULD_CROSS, Stage.MARKET),
            "REDUCE_ONLY_ORDER_WOULD_INCREASE_POSITION":
                (Code.REDUCE_ONLY_WOULD_INCREASE, Stage.ACCOUNT),
            "GEO_IP_BLOCK": (Code.VENUE_NOT_ALLOWED, Stage.RISK),
        }
        code, stage = mapping.get(str(venue_code),
                                  (Code.MARKET_NOT_TRADEABLE, Stage.INSTRUMENT))
        return OrderAck(
            client_order_id=request.client_order_id,
            status=OrderStatus.REJECTED, venue=self.name,
            raw={"status": exc.status, "error": venue_code, "message": message},
            rejections=[Rejection(code, stage, "paradex %s: %s" % (venue_code, message),
                                  {"venue_error": venue_code})],
        )

    def cancel_order(self, order_id: str, market: Optional[str] = None) -> OrderAck:
        try:
            self.client.request("DELETE", "%s/orders/%s" % (self.base_url, order_id),
                                headers=self._auth_headers())
        except HttpError as exc:
            return OrderAck(client_order_id="", order_id=order_id,
                            status=OrderStatus.UNKNOWN, venue=self.name,
                            raw={"status": exc.status, "body": exc.body[:200]})
        return OrderAck(client_order_id="", order_id=order_id,
                        status=OrderStatus.CANCELLED, venue=self.name)

    def fetch_open_orders(self) -> Dict[str, OrderRequest]:
        from ..core.types import OrderType
        raw = self.client.get("%s/orders" % self.base_url,
                              headers=self._auth_headers())
        out: Dict[str, OrderRequest] = {}
        for o in (raw.get("results") or []) if isinstance(raw, dict) else []:
            try:
                out[str(o["id"])] = OrderRequest(
                    market=o["market"], side=Side(o["side"]),
                    order_type=OrderType(o.get("type", "LIMIT")),
                    size=dec(o.get("size", "0")),
                    price=dec(o["price"]) if o.get("price") else None,
                    client_order_id=o.get("client_id", ""),
                )
            except (KeyError, ValueError):
                continue
        return out
