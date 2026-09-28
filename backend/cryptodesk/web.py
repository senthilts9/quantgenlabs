"""A local trading desk UI, on the standard library.

This exists to answer a question that keeps getting asked the wrong way round:
*retail trades through a UI, institutions through code - so which is this?*

Both. And that framing is the misleading part. The difference between a retail
app and a desk is not the presence of a screen, it is **what sits behind the
button**. A retail exchange app runs the same tick, notional, margin and band
checks - it just does not show them to you, and when one fires you get a red
toast saying "order failed".

So this serves the *same* `OrderManager`, the *same* 45 validation rules and
the *same* risk limits as `cryptodesk trade`, and renders the outcome twice:

  * the **retail view** - one line, accepted or rejected, as an app would show;
  * the **desk view** - every stage, every rule that fired, every rejection
    code with the machine-readable detail an execution system branches on.

Same order, same pipeline, two levels of disclosure. That is the actual
distinction, and it is much easier to see than to describe.

Deliberately local-only:

* binds `127.0.0.1`, so nothing outside this machine can reach it. There is no
  authentication because there is no remote attack surface to authenticate
  against - adding a login to a loopback socket would be theatre.
* **dry run by default.** Sending is a separate, explicit toggle, and the
  mainnet guard applies exactly as it does everywhere else.
* no CDN, no framework, no build step - consistent with a package whose whole
  premise is that it runs on a stock Python install.
"""
from __future__ import annotations

import json
import threading
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .config import Settings, describe_guard
from .core import InstrumentRegistry, OrderRequest, OrderType, Quote, Side
from .core.types import OrderBook, TimeInForce
from .execution import OrderManager
from .risk.limits import RiskEngine, RiskLimits
from .validation import for_venue as validator_for
from .venues import VENUES
from .venues.base import VenueError

STATIC = Path(__file__).parent / "static"


class Desk:
    """Shared, thread-safe state behind the HTTP handlers.

    Venues and their instrument registries are cached: refreshing metadata from
    a live exchange costs a round trip and returns over a thousand instruments,
    which is not something to repeat on every keystroke of an order ticket.
    """

    def __init__(self, settings: Optional[Settings] = None, store=None):
        self.settings = settings or Settings.from_env()
        self.store = store
        self._lock = threading.RLock()
        self._venues: Dict[str, Any] = {}
        self._registries: Dict[str, InstrumentRegistry] = {}
        self._oms: Dict[str, OrderManager] = {}
        self.risk = RiskEngine(RiskLimits(
            max_order_notional=Decimal("50000"),
            require_real_market_data=False,
        ), store=store)

    def venue(self, name: str):
        with self._lock:
            if name in self._venues:
                return self._venues[name], self._registries[name]
            if name not in VENUES:
                raise VenueError("unknown venue %r" % name)

            reg = InstrumentRegistry()
            if name == "paper":
                venue = VENUES[name](reg, starting_collateral=Decimal("100000"),
                                     store=self.store)
            else:
                venue = VENUES[name](registry=reg, store=self.store)

            # Live metadata where the venue publishes it; local defaults are a
            # fallback, never a substitute worth hiding.
            try:
                reg.refresh(venue)
            except Exception:
                pass

            if name == "paper":
                self._seed_paper(venue, reg)

            self._venues[name] = venue
            self._registries[name] = reg
            self._oms[name] = OrderManager(venue, self.risk, store=self.store)
            return venue, reg

    def _seed_paper(self, venue, reg) -> None:
        """Give the local engine real depth, so slippage is not a fiction."""
        for market in ("BTC-USD-PERP", "ETH-USD-PERP"):
            try:
                book = venue.seed_from_feed(market.replace("-PERP", ""), depth=25)
                venue.seed(OrderBook(market, book.bids, book.asks,
                                     book.timestamp_ms, book.source))
            except Exception:
                continue

    def oms(self, name: str) -> OrderManager:
        self.venue(name)
        return self._oms[name]

    # -- API ---------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        rows = []
        for name, cls in sorted(VENUES.items()):
            try:
                caps = cls().capabilities()
            except Exception as exc:
                rows.append({"venue": name, "error": str(exc)})
                continue
            rows.append({
                "venue": name,
                "trades": caps["supports_trading"],
                "matching": ("mock" if caps["is_mock"]
                             else ("real" if caps["has_matching_engine"] else "none")),
                "signed": caps["requires_signature"],
            })
        return {"guard": describe_guard(self.settings),
                "mainnet": self.settings.is_mainnet, "venues": rows}

    def markets(self, venue_name: str) -> Dict[str, Any]:
        _, reg = self.venue(venue_name)
        out = []
        for sym in reg.symbols()[:400]:
            inst = reg.require(sym)
            out.append({
                "symbol": sym, "kind": inst.kind.value,
                "tick": str(inst.tick_size), "step": str(inst.step_size),
                "min_notional": str(inst.min_notional),
                "band_bps": str(inst.price_band_bps),
                "max_leverage": str(inst.max_leverage.quantize(Decimal("1"))),
            })
        return {"markets": out}

    def book(self, venue_name: str, market: str) -> Dict[str, Any]:
        venue, _ = self.venue(venue_name)
        book = venue.fetch_book(market, depth=8)
        try:
            quote = venue.fetch_quote(market)
        except Exception:
            quote = Quote(market, book.mid or Decimal(0), source="book-mid")
        return {
            "market": market, "source": book.source,
            "bids": [{"price": str(l.price), "size": str(l.size)} for l in book.bids],
            "asks": [{"price": str(l.price), "size": str(l.size)} for l in book.asks],
            "mid": str(book.mid) if book.mid else None,
            "mark": str(quote.price),
            "spread_bps": (str(book.spread_bps.quantize(Decimal("0.01")))
                           if book.spread_bps is not None else None),
        }

    def _build(self, payload: Dict[str, Any]) -> OrderRequest:
        def dec_or_none(key):
            raw = (payload.get(key) or "").strip()
            if not raw:
                return None
            try:
                return Decimal(raw)
            except (InvalidOperation, ValueError):
                raise ValueError("%s is not a number: %r" % (key, raw))

        size = dec_or_none("size")
        if size is None:
            raise ValueError("size is required")
        return OrderRequest(
            market=payload["market"],
            side=Side(payload.get("side", "BUY").upper()),
            order_type=OrderType(payload.get("type", "LIMIT").upper()),
            size=size,
            price=dec_or_none("price"),
            time_in_force=TimeInForce(payload.get("tif", "GTC").upper()),
            post_only=bool(payload.get("post_only")),
            reduce_only=bool(payload.get("reduce_only")),
            max_slippage_bps=dec_or_none("max_slippage_bps"),
            nonce=int(payload.get("nonce") or 0) or None,
            signature="0xui-dry-run",
        )

    def validate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Run the real pipeline and report it at both levels of disclosure."""
        venue_name = payload.get("venue", "paper")
        venue, reg = self.venue(venue_name)
        request = self._build(payload)

        book = mark = None
        try:
            book = venue.fetch_book(request.market, depth=10)
            mark = venue.fetch_quote(request.market)
        except Exception:
            pass

        account = None
        try:
            account = venue.fetch_account()
        except Exception:
            pass

        validator = validator_for(venue_name, reg)
        repaired, report, changes = validator.validate_and_repair(
            request, book=book, mark=mark, account=account,
            **venue.replay_kwargs()
        )
        risk_rejections = self.risk.check(
            repaired, account=account, mark=mark, venue=venue_name,
            book_source=book.source if book else None,
        )

        accepted = report.ok and not risk_rejections
        # The retail line: what an exchange app would actually surface.
        if accepted:
            retail = "Order accepted"
        elif risk_rejections:
            retail = "Order failed. Please try again."
        else:
            retail = "Order failed. Please check your order details."

        return {
            "retail": {"accepted": accepted, "message": retail},
            "desk": {
                "repairs": changes,
                "validation": report.to_dict(),
                "risk": [r.to_dict() for r in risk_rejections],
                "order": repaired.to_dict(),
                "stages_run": [s.value for s in validator.stages],
                "rules_evaluated": sum(
                    len(v) for v in validator.rules_by_stage.values()),
            },
        }

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        venue_name = payload.get("venue", "paper")
        venue, reg = self.venue(venue_name)
        if venue.is_mock:
            raise VenueError(
                "this venue has no matching engine - a fill here would be a "
                "fixture, not an execution")

        request = self._build(payload)
        book = mark = None
        try:
            book = venue.fetch_book(request.market, depth=10)
            mark = venue.fetch_quote(request.market)
        except Exception:
            pass

        tracked = self.oms(venue_name).submit(request, mark=mark, book=book)
        return {
            "status": tracked.status.value,
            "client_order_id": tracked.client_order_id,
            "order_id": tracked.order_id,
            "filled": str(tracked.filled_size),
            "avg_price": str(tracked.avg_price) if tracked.avg_price else None,
            "rejections": [r.to_dict() for r in tracked.rejections],
            "blotter": self.oms(venue_name).blotter(),
        }


class Handler(BaseHTTPRequestHandler):
    desk: Desk = None            # injected by `serve`
    server_version = "cryptodesk"

    def log_message(self, fmt, *args):
        pass                      # the default logger writes to stderr per request

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This page loads no remote anything; say so rather than relying on it.
        self.send_header("Content-Security-Policy",
                         "default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json")

    def _error(self, exc: Exception, code: int = 400) -> None:
        from .vault.base import redact
        self._json({"error": redact(str(exc)),
                    "type": type(exc).__name__}, code)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path in ("/", "/index.html"):
                html = (STATIC / "desk.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if parsed.path == "/api/state":
                return self._json(self.desk.state())
            if parsed.path == "/api/markets":
                return self._json(self.desk.markets(query.get("venue", "paper")))
            if parsed.path == "/api/book":
                return self._json(self.desk.book(query.get("venue", "paper"),
                                                 query.get("market", "BTC-USD-PERP")))
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._body()
            if parsed.path == "/api/validate":
                return self._json(self.desk.validate(payload))
            if parsed.path == "/api/submit":
                return self._json(self.desk.submit(payload))
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._error(exc)


def serve(host: str = "127.0.0.1", port: int = 8787,
          settings: Optional[Settings] = None, store=None) -> None:
    """Run the desk UI. Loopback only."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Binding publicly would expose an unauthenticated order-entry surface.
        raise ValueError(
            "refusing to bind %r: this UI has no authentication because it is "
            "loopback-only. Put it behind a reverse proxy with real auth if "
            "you genuinely need remote access." % host
        )
    Handler.desk = Desk(settings, store=store)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(describe_guard(Handler.desk.settings))
    print("desk UI on http://%s:%d  (dry run by default, Ctrl-C to stop)"
          % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
