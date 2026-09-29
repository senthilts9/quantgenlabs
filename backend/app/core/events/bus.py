"""Event bus: the interface, plus a fully in-process implementation.

WHY AN INTERFACE AT ALL. The transport is the replaceable part. An in-process
bus, Kafka, or a log file replayed from disk are the same contract to a producer,
and pinning the domain code to a Kafka client would mean none of it can be tested
without a broker.

THE IN-PROCESS BUS IS NOT A MOCK. It implements the semantics that matter --
partitioning by key, per-partition ordering, consumer groups with offsets,
at-least-once delivery, idempotent consumption, dead-letter routing and replay
from an offset. Those are exactly the behaviours a Kafka consumer has to get
right, so testing against this tests the logic that would otherwise only be
exercised in an environment nobody has locally. What it does NOT provide is
durability across a restart, cross-process delivery, or real broker failure modes,
and it says so rather than pretending.

AT-LEAST-ONCE, NOT EXACTLY-ONCE. Exactly-once across a broker and an external
side effect is a distributed-transaction problem, and the usual "exactly-once"
setting only covers broker-internal read-process-write. So delivery is
at-least-once and consumers are expected to be IDEMPOTENT -- which is why every
envelope carries a stable event_id and the consumer wrapper here deduplicates on
it. That is the honest arrangement, and it is the one the fills table already
relies on with its UNIQUE(venue, exec_id).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from .topics import DLQ_SUFFIX, TOPICS, dlq_for, spec


@dataclass
class Envelope:
    """What actually crosses the bus.

    event_id is the idempotency key: a redelivery carries the SAME id, which is
    what lets a consumer recognise it. Generating a fresh id on redelivery would
    make deduplication impossible, which is the mistake that turns at-least-once
    into at-least-twice-with-side-effects.
    """
    topic: str
    key: str
    payload: dict
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = 1
    ts_ns: int = field(default_factory=lambda: time.time_ns())
    # Correlates a bus event with the trace and the audit row for the same action.
    correlation_id: str | None = None
    # Incremented by the broker on redelivery, so a consumer can tell a retry from
    # a first attempt and a supervisor can spot a poison message.
    attempt: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @staticmethod
    def from_json(raw: str) -> "Envelope":
        return Envelope(**json.loads(raw))


@dataclass
class Record:
    envelope: Envelope
    partition: int
    offset: int


class EventBus(ABC):
    @abstractmethod
    def publish(self, topic: str, key: str, payload: dict,
                correlation_id: str | None = None) -> Record: ...

    @abstractmethod
    def subscribe(self, topic: str, group: str,
                  handler: Callable[[Envelope], None], *,
                  max_attempts: int = 3) -> None: ...

    @abstractmethod
    def poll(self, topic: str, group: str, max_records: int = 100) -> int: ...

    @abstractmethod
    def replay(self, topic: str, from_offset: int = 0,
               partition: int | None = None) -> Iterable[Record]: ...

    @abstractmethod
    def lag(self, topic: str, group: str) -> dict[int, int]: ...

    @abstractmethod
    def close(self) -> None: ...


def partition_for(key: str, partitions: int) -> int:
    """Stable across processes and restarts.

    Deliberately NOT Python's hash(): PYTHONHASHSEED randomises str hashing per
    process, so the same key would land in a different partition after a restart
    and the per-key ordering guarantee would silently evaporate. This is a real
    bug people ship.
    """
    if partitions <= 1:
        return 0
    h = 0
    for ch in key.encode("utf-8"):
        h = (h * 31 + ch) & 0xFFFFFFFF          # deterministic, seed-independent
    return h % partitions


class InProcessBus(EventBus):
    """Thread-safe, ordered-per-partition, with consumer groups and a DLQ.

    Not durable across a restart and not visible to another process. Those are the
    two things a real broker adds; everything else about the contract is here.
    """

    def __init__(self, max_retained: int = 100_000) -> None:
        self._lock = threading.RLock()
        # topic -> partition -> list of Records (the log)
        self._log: dict[str, dict[int, list[Record]]] = defaultdict(lambda: defaultdict(list))
        # topic -> group -> partition -> next offset to read
        self._offsets: dict[str, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
        # topic -> group -> handler config
        self._subs: dict[str, dict[str, dict]] = defaultdict(dict)
        # group -> set of event ids already processed (idempotency)
        self._seen: dict[str, set[str]] = defaultdict(set)
        self._dlq: dict[str, list[Envelope]] = defaultdict(list)
        self._max_retained = max_retained
        self.published = 0
        self.delivered = 0
        self.dead_lettered = 0
        self.duplicates_suppressed = 0

    # ------------------------------------------------------------- produce
    def publish(self, topic: str, key: str, payload: dict,
                correlation_id: str | None = None) -> Record:
        # Unknown topics are refused rather than auto-created: a typo would
        # otherwise create a topic nobody consumes and lose the messages silently.
        is_dlq = topic.endswith(DLQ_SUFFIX)
        s = spec(topic[: -len(DLQ_SUFFIX)]) if is_dlq else spec(topic)
        env = Envelope(topic=topic, key=key, payload=payload,
                       schema_version=s.schema_version, correlation_id=correlation_id)
        p = partition_for(key, s.partitions)
        with self._lock:
            part = self._log[topic][p]
            rec = Record(envelope=env, partition=p, offset=len(part))
            part.append(rec)
            # Bounded retention so a long-running process cannot grow without
            # limit. A real broker ages by time or size; this is the same idea.
            if len(part) > self._max_retained:
                del part[: len(part) - self._max_retained]
            self.published += 1
            return rec

    # ------------------------------------------------------------- consume
    def subscribe(self, topic: str, group: str,
                  handler: Callable[[Envelope], None], *,
                  max_attempts: int = 3) -> None:
        spec(topic[: -len(DLQ_SUFFIX)] if topic.endswith(DLQ_SUFFIX) else topic)
        with self._lock:
            self._subs[topic][group] = {"handler": handler, "max_attempts": max_attempts}

    def poll(self, topic: str, group: str, max_records: int = 100) -> int:
        """Deliver up to max_records. Returns how many were handled.

        Offsets advance ONLY after the handler returns successfully, which is what
        makes delivery at-least-once: a crash mid-handler re-delivers rather than
        skipping. The alternative -- advancing first -- is at-most-once and loses
        messages on any failure.
        """
        with self._lock:
            cfg = self._subs.get(topic, {}).get(group)
            if cfg is None:
                raise KeyError(f"No subscription for group '{group}' on '{topic}'")
            handler = cfg["handler"]
            max_attempts = cfg["max_attempts"]
            partitions = sorted(self._log.get(topic, {}).keys())

        handled = 0
        for p in partitions:
            while handled < max_records:
                with self._lock:
                    off = self._offsets[topic][group].get(p, 0)
                    part = self._log[topic][p]
                    if off >= len(part):
                        break
                    rec = part[off]

                env = rec.envelope
                # Idempotency: a redelivery carries the same event_id, so a
                # consumer that already applied it must not apply it twice.
                with self._lock:
                    if env.event_id in self._seen[group]:
                        self._offsets[topic][group][p] = off + 1
                        self.duplicates_suppressed += 1
                        continue

                ok = False
                last_err: Exception | None = None
                for attempt in range(1, max_attempts + 1):
                    env.attempt = attempt
                    try:
                        handler(env)
                        ok = True
                        break
                    except Exception as e:      # noqa: BLE001 - the bus must not die
                        last_err = e

                with self._lock:
                    if ok:
                        self._seen[group].add(env.event_id)
                        self.delivered += 1
                        handled += 1
                    else:
                        # Poison message. Without a DLQ this would be retried
                        # forever and block its partition -- every later message
                        # for that key waiting behind one it will never pass.
                        self._dlq[dlq_for(topic)].append(env)
                        self.dead_lettered += 1
                    self._offsets[topic][group][p] = off + 1
                    if not ok and last_err is not None:
                        env.payload = dict(env.payload)
                        env.payload["_dlq_reason"] = f"{type(last_err).__name__}: {last_err}"
        return handled

    # -------------------------------------------------------------- replay
    def replay(self, topic: str, from_offset: int = 0,
               partition: int | None = None) -> Iterable[Record]:
        """Read the log directly without touching any consumer group's offsets.

        This is what makes an event log worth having: reprocessing yesterday with
        corrected code, without disturbing live consumers.
        """
        with self._lock:
            parts = [partition] if partition is not None else sorted(self._log.get(topic, {}))
            out: list[Record] = []
            for p in parts:
                out.extend(r for r in self._log.get(topic, {}).get(p, []) if r.offset >= from_offset)
        return out

    def lag(self, topic: str, group: str) -> dict[int, int]:
        """Unread records per partition -- the number an operator watches."""
        with self._lock:
            return {p: len(recs) - self._offsets[topic][group].get(p, 0)
                    for p, recs in self._log.get(topic, {}).items()}

    def dlq(self, topic: str) -> list[Envelope]:
        with self._lock:
            return list(self._dlq.get(dlq_for(topic), []))

    def topic_size(self, topic: str) -> int:
        with self._lock:
            return sum(len(r) for r in self._log.get(topic, {}).values())

    def stats(self) -> dict:
        with self._lock:
            return {
                "published": self.published,
                "delivered": self.delivered,
                "dead_lettered": self.dead_lettered,
                "duplicates_suppressed": self.duplicates_suppressed,
                "topics": {t: self.topic_size(t) for t in self._log},
            }

    def close(self) -> None:
        with self._lock:
            self._subs.clear()
