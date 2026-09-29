"""Event backbone.

QGL_KAFKA_BROKERS selects the transport. Unset, the platform uses the in-process
bus -- which implements the same semantics (partitioning, ordering, consumer
groups, at-least-once, idempotency, DLQ, replay) but is not durable across a
restart and not visible to another process.

get_bus() is the single entry point so no caller has to know which is in use.
"""
from __future__ import annotations

import os
import threading

from .bus import Envelope, EventBus, InProcessBus, Record, partition_for
from .topics import TOPICS, Retention, TopicSpec, dlq_for, spec

_bus: EventBus | None = None
_lock = threading.Lock()


def backend_name() -> str:
    return "kafka" if os.getenv("QGL_KAFKA_BROKERS") else "in-process"


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        with _lock:
            if _bus is None:
                if os.getenv("QGL_KAFKA_BROKERS"):
                    from .kafka_bus import KafkaBus
                    _bus = KafkaBus()
                else:
                    _bus = InProcessBus()
    return _bus


def reset_bus() -> None:
    """Drop the process-wide bus. For tests, and for switching transport."""
    global _bus
    with _lock:
        if _bus is not None:
            _bus.close()
        _bus = None


__all__ = ["Envelope", "EventBus", "InProcessBus", "Record", "TOPICS", "Retention",
           "TopicSpec", "spec", "dlq_for", "partition_for", "get_bus", "reset_bus",
           "backend_name"]
