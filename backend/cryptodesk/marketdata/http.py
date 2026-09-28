"""Minimal HTTP client built on the standard library.

No `requests`, no `aiohttp`. The whole package runs on a stock Python 3.9
install, which matters because the first thing a trading system should not do
is fail to start on a machine that has not had `pip install` run on it.

What this adds over bare `urllib`:

* a token-bucket rate limiter per host, because public endpoints ban by IP and
  a retry storm is the fastest way to lose market data during volatility;
* retries with exponential backoff and jitter, on the status codes that are
  actually transient (429, 5xx) and never on 4xx, which will fail identically
  on every attempt;
* `Retry-After` honoured when the server sends it - guessing a backoff when
  the server has told you the answer is how a soft rate limit becomes a ban;
* a hard distinction between "request failed" and "request succeeded with an
  error body", so callers can tell a network blip from a rejected order.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

DEFAULT_TIMEOUT = 10.0
DEFAULT_UA = "cryptodesk/0.1 (+testnet research client)"

#: Transient by nature: worth retrying. 4xx other than 429 are not - the
#: request is malformed or unauthorised and will stay that way.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(Exception):
    """Non-2xx response. Carries the body, which is where venues put the
    rejection reason - discarding it would throw away the only useful part."""

    def __init__(self, status: int, url: str, body: str,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__("HTTP %d for %s: %s" % (status, url, body[:500]))
        self.status = status
        self.url = url
        self.body = body
        self.headers = headers or {}

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None


class TransportError(Exception):
    """The request never got an answer: DNS, TLS, timeout, connection reset."""


@dataclass
class RateLimiter:
    """Token bucket, one per host.

    `rate` tokens accrue per second up to `burst`. Blocking rather than
    raising: a market-data poller wants to be slowed down, not to lose the
    tick. Callers that cannot block should check `available()` first.
    """

    rate: float = 8.0
    burst: float = 16.0
    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default_factory=time.monotonic, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = self.burst

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def available(self) -> bool:
        with self._lock:
            self._refill()
            return self._tokens >= 1.0

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                deficit = (1.0 - self._tokens) / self.rate
            if time.monotonic() + deficit > deadline:
                return False
            time.sleep(min(deficit, 0.25))


class HttpClient:
    """Synchronous JSON-over-HTTP client with per-host rate limiting."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        user_agent: str = DEFAULT_UA,
        default_rate: float = 8.0,
        default_burst: float = 16.0,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.default_rate = default_rate
        self.default_burst = default_burst
        self._limiters: Dict[str, RateLimiter] = {}
        self._lock = threading.Lock()
        #: Observed latencies per host - cheap venue health signal.
        self.latency_ms: Dict[str, float] = {}

    def limiter_for(self, url: str) -> RateLimiter:
        host = urllib.parse.urlparse(url).netloc
        with self._lock:
            lim = self._limiters.get(host)
            if lim is None:
                lim = RateLimiter(self.default_rate, self.default_burst)
                self._limiters[host] = lim
            return lim

    def set_rate(self, url: str, rate: float, burst: Optional[float] = None) -> None:
        """Override the limit for a host - venues publish wildly different caps."""
        host = urllib.parse.urlparse(url).netloc
        with self._lock:
            self._limiters[host] = RateLimiter(rate, burst if burst is not None else rate * 2)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Perform a request and return parsed JSON.

        Raises HttpError for non-2xx (after retries) and TransportError if the
        request never completed. Returns the raw text when the response is not
        JSON, so a venue returning HTML on an outage is visible rather than
        masked by a parse error.
        """
        if params:
            # Drop None values so callers can pass optional params inline.
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = "%s?%s" % (url, urllib.parse.urlencode(clean))

        data: Optional[bytes] = None
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)

        limiter = self.limiter_for(url)
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            if not limiter.acquire(timeout=self.timeout):
                raise TransportError("rate limiter timeout for %s" % url)

            req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    payload = resp.read().decode("utf-8", errors="replace")
                    self._record_latency(url, started)
                    return self._parse(payload)

            except urllib.error.HTTPError as exc:
                payload = exc.read().decode("utf-8", errors="replace")
                self._record_latency(url, started)
                err = HttpError(exc.code, url, payload, dict(exc.headers or {}))
                if exc.code not in RETRY_STATUSES or attempt == self.max_retries:
                    raise err
                last_exc = err
                self._sleep_before_retry(attempt, err.headers.get("Retry-After"))

            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self._record_latency(url, started)
                last_exc = TransportError("%s: %s" % (type(exc).__name__, exc))
                if attempt == self.max_retries:
                    raise last_exc
                self._sleep_before_retry(attempt, None)

        raise last_exc or TransportError("request failed: %s" % url)

    def get(self, url: str, **kw) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Any:
        return self.request("POST", url, **kw)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _parse(payload: str) -> Any:
        try:
            return json.loads(payload)
        except ValueError:
            return payload

    def _record_latency(self, url: str, started: float) -> None:
        host = urllib.parse.urlparse(url).netloc
        self.latency_ms[host] = (time.perf_counter() - started) * 1000.0

    @staticmethod
    def _sleep_before_retry(attempt: int, retry_after: Optional[str]) -> None:
        """Honour Retry-After when present; otherwise exponential + jitter.

        The jitter is not decoration: without it, every client that got rate
        limited at the same moment retries at the same moment, and the venue
        sees the same spike again one backoff later.
        """
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 30.0))
                return
            except (TypeError, ValueError):
                pass
        time.sleep(min(0.25 * (2 ** attempt) + random.random() * 0.25, 10.0))


#: Module-level default. Venue adapters accept a client so tests can inject one.
DEFAULT_CLIENT = HttpClient()


def probe(url: str, client: Optional[HttpClient] = None) -> Tuple[bool, str]:
    """Cheap reachability check used by the CLI's `doctor` command."""
    c = client or DEFAULT_CLIENT
    try:
        c.get(url, timeout=5.0)
        return True, "ok (%.0fms)" % c.latency_ms.get(
            urllib.parse.urlparse(url).netloc, 0.0
        )
    except HttpError as exc:
        # A 4xx still proves the host is up and speaking HTTP.
        return exc.status < 500, "HTTP %d" % exc.status
    except TransportError as exc:
        return False, str(exc)
