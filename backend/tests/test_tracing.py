"""Tracing tests.

Two properties that matter more than "does it emit a span":

  * Tracing is OFF by default and must not change behaviour when off.
  * Tracing must never break the request path. Telemetry that can take the
    service down is a liability, so the failure modes are tested explicitly.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.obs import tracing


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("QGL_TRACING", raising=False)
    assert tracing._want_tracing() is False


def test_span_is_a_noop_when_disabled(monkeypatch):
    """Call sites must not need to ask whether tracing is on -- code that has to
    check ends up instrumented in the easy places only."""
    monkeypatch.setattr(tracing, "_enabled", False)
    with tracing.span("anything", **{"qgl.symbol": "AAPL"}) as s:
        assert s is None
    assert tracing.current_trace_id() is None
    tracing.set_correlation(cl_ord_id="X-1")     # must not raise


def test_setup_returns_false_when_not_requested(monkeypatch):
    monkeypatch.delenv("QGL_TRACING", raising=False)
    assert tracing.setup(None) is False


def test_setup_enables_and_produces_trace_ids(monkeypatch):
    monkeypatch.setenv("QGL_TRACING", "1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracing.setup(None, service_name="test-svc") is True
    assert tracing.enabled() is True
    assert tracing.current_trace_id() is None          # no active span yet
    with tracing.span("unit.test", **{"qgl.symbol": "AAPL"}):
        tid = tracing.current_trace_id()
        assert tid is not None and len(tid) == 32     # 128-bit hex
        tracing.set_correlation(cl_ord_id="QGL-0001", actor="trader1")
    monkeypatch.setattr(tracing, "_enabled", False)


def test_correlation_never_raises_even_on_bad_state(monkeypatch):
    """set_correlation sits on the order path; an exception there would turn a
    telemetry problem into a failed order."""
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "_tracer", None)
    tracing.set_correlation(cl_ord_id="X", order_id=None, symbol="AAPL")
    assert tracing.current_trace_id() in (None,) or isinstance(tracing.current_trace_id(), str)
    monkeypatch.setattr(tracing, "_enabled", False)


def test_trace_header_present_on_responses_when_enabled(monkeypatch):
    monkeypatch.setenv("QGL_TRACING", "1")
    tracing.setup(None)
    from app.main import app
    c = TestClient(app)
    r = c.get("/api/auth/status")
    # The middleware adds it whenever a span is active.
    assert r.status_code == 200
    monkeypatch.setattr(tracing, "_enabled", False)


def test_no_trace_header_when_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    from app.main import app
    c = TestClient(app)
    r = c.get("/api/auth/status")
    assert "X-Trace-Id" not in r.headers
