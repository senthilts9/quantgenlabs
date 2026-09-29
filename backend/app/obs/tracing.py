"""Distributed tracing with OpenTelemetry.

WHY THIS IS SEPARATE FROM THE PROMETHEUS METRICS ALREADY HERE. Metrics answer
"how many, how fast, how often" across all requests. A trace answers "what
happened to THIS order" — which spans it touched, in what order, and where the
time went. When a fill is missing, an aggregate p99 tells you nothing and a trace
tells you exactly which hop dropped it. The two are complements, not alternatives,
which is why both are wired rather than one replacing the other.

CORRELATION IDS COME FROM THE ORDER, NOT FROM A NEW UUID. The platform already
mints ClOrdIDs, and they already appear in the FIX wire log and the blotter. Using
them as the correlation key means a trace can be found from an id a human already
has in front of them, instead of requiring them to first map an order to some
separate trace id that exists only in the telemetry system.

OFF BY DEFAULT, and exporting to the console unless an OTLP endpoint is
configured. Tracing that silently tries to reach a collector that is not there
produces connection errors on every request, which is worse than no tracing:
people disable the whole thing to stop the noise.

SAMPLING. Trading traffic is bursty, and tracing every message during a burst is
how telemetry becomes the bottleneck it was installed to diagnose. The ratio is
configurable and defaults to 1.0 only because this is a simulation; a real deck
would sample far lower and always-sample errors.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger(__name__)

_enabled = False
_tracer = None


def enabled() -> bool:
    return _enabled


def _want_tracing() -> bool:
    return os.getenv("QGL_TRACING", "").lower() in ("1", "true", "yes", "on")


def setup(app=None, service_name: str = "quantgenlabs-api") -> bool:
    """Wire tracing. Returns whether it was actually enabled.

    Import failures degrade to disabled rather than crashing the app: telemetry
    that can take the service down is a liability, not an asset.
    """
    global _enabled, _tracer
    if not _want_tracing():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (BatchSpanProcessor,
                                                    ConsoleSpanExporter,
                                                    SimpleSpanProcessor)
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as e:
        log.warning("tracing requested but OpenTelemetry is not installed: %s", e)
        return False

    ratio = float(os.getenv("QGL_TRACE_SAMPLE_RATIO", "1.0"))
    resource = Resource.create({
        "service.name": service_name,
        "service.version": os.getenv("QGL_VERSION", "3.0.0"),
        "deployment.environment": os.getenv("QGL_ENV", "development"),
    })
    provider = TracerProvider(
        resource=resource,
        # ParentBased means a span that is part of an already-sampled trace is
        # kept, so a sampled request does not lose half its spans downstream.
        sampler=ParentBased(root=TraceIdRatioBased(ratio)),
    )

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import \
                OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            log.info("tracing -> OTLP %s", endpoint)
        except ImportError:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            log.warning("OTLP exporter unavailable; falling back to console")
    else:
        # Console, not a default collector address: pointing at a collector that
        # is not there produces an error per request and people disable tracing
        # entirely to stop the noise.
        #
        # SimpleSpanProcessor rather than Batch for the console exporter. Batching
        # runs a background thread that flushes on its own schedule, and at
        # shutdown that thread writes to a stdout the interpreter has already
        # closed -- "ValueError: I/O operation on closed file" on every exit.
        # Batching exists to amortise NETWORK round trips; writing to a local
        # stream has none to amortise, so it buys nothing here and costs that.
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        log.info("tracing -> console (set OTEL_EXPORTER_OTLP_ENDPOINT for a collector)")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(
                app,
                # The metrics mount and health check are high-frequency and carry
                # no diagnostic value; tracing them buries the spans that matter.
                excluded_urls="/metrics,/api/health,/docs,/openapi.json",
            )
        except ImportError as e:
            log.warning("FastAPI instrumentation unavailable: %s", e)

    _enabled = True
    return True


@contextmanager
def span(name: str, **attributes):
    """Create a span, or do nothing when tracing is off.

    A no-op context manager rather than a conditional at every call site: code
    that has to ask "is tracing on?" before it can be instrumented ends up
    instrumented in the easy places only.
    """
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v)
        yield s


def set_correlation(cl_ord_id: str | None = None, order_id: str | None = None,
                    symbol: str | None = None, actor: str | None = None) -> None:
    """Tag the current span with the identifiers a human already has.

    A trace found by ClOrdID is a trace someone can actually find: the id is in
    the blotter, in the FIX wire log and in the audit trail already.
    """
    if not _enabled:
        return
    try:
        from opentelemetry import trace
        s = trace.get_current_span()
        if s is None or not s.is_recording():
            return
        for key, val in (("qgl.cl_ord_id", cl_ord_id), ("qgl.order_id", order_id),
                         ("qgl.symbol", symbol), ("qgl.actor", actor)):
            if val:
                s.set_attribute(key, val)
    except Exception:       # telemetry must never break the request path
        pass


def shutdown() -> None:
    """Flush and stop the provider. Called on application shutdown so spans in
    flight are exported rather than lost with the process."""
    global _enabled, _tracer
    if not _enabled:
        return
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        pass
    _enabled = False
    _tracer = None


def current_trace_id() -> str | None:
    """Hex trace id for the active span, so it can be returned in a response
    header and pasted straight into a trace viewer."""
    if not _enabled:
        return None
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return None
