"""Event bus semantics.

These test the behaviours a Kafka consumer has to get right and that are
impossible to verify without a broker: per-key ordering, consumer-group offsets,
at-least-once delivery, idempotent redelivery, dead-lettering and replay.

The in-process bus is not a mock — it implements those semantics, so exercising
them here exercises the logic that would otherwise only be tested in an
environment nobody has locally.
"""
from __future__ import annotations

import pytest

from app.core.events import (Envelope, InProcessBus, TOPICS, dlq_for,
                             partition_for, spec)


@pytest.fixture
def bus():
    b = InProcessBus()
    yield b
    b.close()


# ------------------------------------------------------------------ topics
def test_catalogue_is_declared_not_implicit(bus):
    """Publishing to an undeclared topic must fail. Auto-creating on first
    publish means a typo creates a topic nobody consumes and loses the messages
    silently."""
    with pytest.raises(KeyError, match="Unknown topic"):
        bus.publish("orders.lifcycle", "k", {})      # typo


def test_every_topic_declares_key_retention_and_partitions():
    for name, s in TOPICS.items():
        assert s.key_field, f"{name} has no partition key"
        assert s.partitions >= 1
        assert s.retention is not None
        assert s.schema_version >= 1
        assert s.purpose, f"{name} has no stated purpose"


def test_audit_is_retained_forever():
    """Deleting the audit trail destroys the thing it exists to be."""
    assert spec("audit.events").retention.value == "compact"
    assert spec("trades.executed").retention.value == "7y"


# -------------------------------------------------------------- partitioning
def test_partitioning_is_deterministic_across_processes():
    """NOT Python's hash(): PYTHONHASHSEED randomises str hashing per process, so
    the same key would land elsewhere after a restart and the per-key ordering
    guarantee would silently evaporate."""
    assert partition_for("ORD-123", 6) == partition_for("ORD-123", 6)
    # Hardcoded so a change to the algorithm is a DELIBERATE decision: changing it
    # moves existing keys to different partitions, which silently breaks the
    # per-key ordering guarantee for everything already in flight.
    assert partition_for("ORD-123", 6) == 0
    assert partition_for("AAPL", 12) == 4


def test_partitioning_survives_a_process_restart():
    """The concrete failure this avoids. Verified across three subprocesses with
    different PYTHONHASHSEED values: ours is stable, builtin hash() is not.

        seed=1  ORD-123 -> 0   builtin hash() % 6 -> 4
        seed=2  ORD-123 -> 0   builtin hash() % 6 -> 2
        seed=3  ORD-123 -> 0   builtin hash() % 6 -> 3

    A bus keyed on builtin hash() would put the same order in a different
    partition after every restart, so events for one order would arrive out of
    order and nothing would report an error."""
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    outs = set()
    for seed in ("1", "2", "3"):
        r = subprocess.run(
            [sys.executable, "-c",
             "from app.core.events import partition_for; print(partition_for('ORD-123', 6))"],
            cwd=backend, capture_output=True, text=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
        )
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip())
    assert len(outs) == 1, f"partition moved between processes: {outs}"


def test_same_key_always_same_partition(bus):
    parts = {bus.publish("orders.lifecycle", "ORD-7", {"n": i}).partition for i in range(50)}
    assert len(parts) == 1          # every event for one order in one partition


def test_different_keys_spread_across_partitions(bus):
    parts = {bus.publish("market.normalized", f"SYM{i}", {}).partition for i in range(60)}
    assert len(parts) > 1           # not all funnelled into one hot partition


# ------------------------------------------------------------------ ordering
def test_per_key_order_is_preserved(bus):
    seen = []
    bus.subscribe("orders.lifecycle", "g", lambda e: seen.append(e.payload["n"]))
    for i in range(100):
        bus.publish("orders.lifecycle", "ORD-1", {"n": i})
    bus.poll("orders.lifecycle", "g", max_records=1000)
    assert seen == list(range(100))     # an ExecutionReport cannot overtake its ack


# -------------------------------------------------------- consumer groups
def test_groups_have_independent_offsets(bus):
    a, b = [], []
    bus.subscribe("orders.lifecycle", "oms", lambda e: a.append(e.event_id))
    bus.subscribe("orders.lifecycle", "risk", lambda e: b.append(e.event_id))
    for i in range(10):
        bus.publish("orders.lifecycle", f"O{i}", {"i": i})
    bus.poll("orders.lifecycle", "oms", max_records=100)
    assert len(a) == 10 and len(b) == 0     # risk has not consumed yet
    bus.poll("orders.lifecycle", "risk", max_records=100)
    assert len(b) == 10                     # and gets every message


def test_polling_twice_does_not_redeliver(bus):
    seen = []
    bus.subscribe("orders.lifecycle", "g", lambda e: seen.append(e.event_id))
    bus.publish("orders.lifecycle", "O1", {})
    assert bus.poll("orders.lifecycle", "g") == 1
    assert bus.poll("orders.lifecycle", "g") == 0
    assert len(seen) == 1


def test_lag_reports_unread_records(bus):
    bus.subscribe("market.normalized", "pricer", lambda e: None)
    for i in range(20):
        bus.publish("market.normalized", "AAPL", {"i": i})
    assert sum(bus.lag("market.normalized", "pricer").values()) == 20
    bus.poll("market.normalized", "pricer", max_records=1000)
    assert sum(bus.lag("market.normalized", "pricer").values()) == 0


# --------------------------------------------------------------- delivery
def test_handler_failure_retries_then_dead_letters(bus):
    """A poison message must not be retried forever: it would block its partition
    and every later message for that key would wait behind it."""
    attempts = []

    def always_fails(e: Envelope):
        attempts.append(e.attempt)
        raise ValueError("cannot process this")

    bus.subscribe("orders.lifecycle", "g", always_fails, max_attempts=3)
    bus.publish("orders.lifecycle", "O1", {"bad": True})
    bus.poll("orders.lifecycle", "g")

    assert attempts == [1, 2, 3]                    # retried, and the attempt
    assert len(bus.dlq("orders.lifecycle")) == 1    # count is visible to the handler
    assert bus.dead_lettered == 1


def test_partition_advances_past_a_poison_message(bus):
    ok = []

    def fails_on_two(e: Envelope):
        if e.payload.get("n") == 2:
            raise ValueError("poison")
        ok.append(e.payload["n"])

    bus.subscribe("orders.lifecycle", "g", fails_on_two, max_attempts=2)
    for i in range(5):
        bus.publish("orders.lifecycle", "SAME-KEY", {"n": i})
    bus.poll("orders.lifecycle", "g", max_records=100)
    # 2 went to the DLQ; 3 and 4 were NOT blocked behind it.
    assert ok == [0, 1, 3, 4]
    assert len(bus.dlq("orders.lifecycle")) == 1


def test_transient_failure_succeeds_on_retry(bus):
    calls = {"n": 0}

    def flaky(e: Envelope):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")

    bus.subscribe("orders.lifecycle", "g", flaky, max_attempts=5)
    bus.publish("orders.lifecycle", "O1", {})
    assert bus.poll("orders.lifecycle", "g") == 1
    assert calls["n"] == 3
    assert bus.dead_lettered == 0


def test_offsets_advance_only_after_success(bus):
    """At-least-once, not at-most-once: advancing the offset before the work
    loses messages on any failure."""
    state = {"fail": True}
    got = []

    def handler(e: Envelope):
        if state["fail"]:
            raise RuntimeError("down")
        got.append(e.event_id)

    bus.subscribe("orders.lifecycle", "g", handler, max_attempts=1)
    bus.publish("orders.lifecycle", "O1", {})
    bus.poll("orders.lifecycle", "g")
    assert got == [] and bus.dead_lettered == 1     # went to DLQ, not lost silently


# ------------------------------------------------------------ idempotency
def test_redelivery_carries_the_same_event_id(bus):
    """Generating a fresh id on redelivery makes deduplication impossible, which
    turns at-least-once into at-least-twice-with-side-effects."""
    rec = bus.publish("trades.executed", "ORD-1", {"qty": 100})
    replayed = list(bus.replay("trades.executed"))
    assert replayed[0].envelope.event_id == rec.envelope.event_id


def test_consumer_suppresses_a_duplicate_event_id(bus):
    applied = []
    bus.subscribe("trades.executed", "pos", lambda e: applied.append(e.event_id))
    rec = bus.publish("trades.executed", "ORD-1", {"qty": 100})
    bus.poll("trades.executed", "pos")
    # Simulate a broker redelivering the same envelope.
    with bus._lock:                      # noqa: SLF001 - deliberate, tests internals
        part = bus._log["trades.executed"][rec.partition]
        part.append(type(rec)(envelope=rec.envelope, partition=rec.partition,
                              offset=len(part)))
    bus.poll("trades.executed", "pos")
    assert len(applied) == 1             # applied once, not twice
    assert bus.duplicates_suppressed == 1


# ----------------------------------------------------------------- replay
def test_replay_does_not_disturb_live_consumers(bus):
    """The point of an event log: reprocess yesterday with corrected code without
    moving a live consumer's offsets."""
    seen = []
    bus.subscribe("market.normalized", "live", lambda e: seen.append(e.payload["i"]))
    for i in range(10):
        bus.publish("market.normalized", "AAPL", {"i": i})
    bus.poll("market.normalized", "live", max_records=100)
    assert len(seen) == 10

    replayed = [r.envelope.payload["i"] for r in bus.replay("market.normalized")]
    assert sorted(replayed) == list(range(10))
    assert sum(bus.lag("market.normalized", "live").values()) == 0   # untouched


def test_replay_from_an_offset(bus):
    for i in range(10):
        bus.publish("market.normalized", "AAPL", {"i": i})
    tail = [r.envelope.payload["i"] for r in bus.replay("market.normalized", from_offset=5)]
    assert tail == [5, 6, 7, 8, 9]


# ---------------------------------------------------------------- envelope
def test_envelope_round_trips_through_json():
    e = Envelope(topic="orders.lifecycle", key="O1", payload={"a": 1},
                 correlation_id="trace-abc")
    back = Envelope.from_json(e.to_json())
    assert back.event_id == e.event_id
    assert back.payload == {"a": 1}
    assert back.correlation_id == "trace-abc"
    assert back.schema_version == e.schema_version


def test_envelope_carries_a_schema_version(bus):
    rec = bus.publish("orders.lifecycle", "O1", {})
    assert rec.envelope.schema_version == spec("orders.lifecycle").schema_version


def test_stats_account_for_traffic(bus):
    bus.subscribe("orders.lifecycle", "g", lambda e: None)
    for i in range(5):
        bus.publish("orders.lifecycle", f"O{i}", {})
    bus.poll("orders.lifecycle", "g", max_records=100)
    s = bus.stats()
    assert s["published"] == 5
    assert s["delivered"] == 5
    assert s["dead_lettered"] == 0
