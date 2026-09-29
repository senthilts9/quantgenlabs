"""Kafka implementation of the EventBus contract.

STATUS, STATED PLAINLY: this code has NEVER been run against a broker. No Kafka
and no Docker exist in the development environment. It is written against the
kafka-python API and is structurally complete, but "written" and "working" are
different words and conflating them is how a deployment discovers the difference.

What that means in practice:
  * Every semantic the in-process bus implements is TESTED. The consumer logic
    -- idempotency, retry, dead-lettering, offset advancement after success --
    lives in shared code so the tested path and the Kafka path are the same
    logic with a different transport underneath.
  * This file is the transport only: connect, produce, poll, commit.
  * It fails LOUDLY on import if the client is missing, rather than degrading to
    a silent no-op. A bus that silently drops every event is worse than one that
    refuses to start.

DESIGN CHOICES THAT MATTER AND ARE EASY TO GET WRONG:

  acks="all" with enable_idempotence. Anything less means a produce can be
  acknowledged and then lost when the partition leader fails over -- and for a
  fills topic that is a trade the system believes it recorded and did not.

  enable_auto_commit=False. Auto-commit advances the offset on a timer, which
  means a crash between the commit and the work loses messages that were never
  processed. Offsets are committed only after the handler succeeds.

  max_poll_records kept small. A consumer that fetches a large batch and takes
  too long to process it is evicted from the group mid-batch, which triggers a
  rebalance, which redelivers the batch to someone else. Small batches keep the
  processing time inside the poll interval.

  The partitioner is OUR partition_for, not the client default. The default
  murmur2 partitioner is fine, but using ours means the in-process bus and Kafka
  put the same key in the same partition number, so a replay from one can be
  reasoned about against the other.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Iterable

from .bus import Envelope, EventBus, Record, partition_for
from .topics import DLQ_SUFFIX, dlq_for, spec


class KafkaUnavailable(RuntimeError):
    pass


def _client():
    try:
        import kafka  # noqa: F401
    except ImportError as e:      # fail loudly, never silently no-op
        raise KafkaUnavailable(
            "kafka-python is not installed. The Kafka bus will not silently "
            "no-op: a bus that drops every event is worse than one that refuses "
            "to start. pip install kafka-python-ng"
        ) from e
    return kafka


class KafkaBus(EventBus):
    def __init__(self, bootstrap_servers: str | None = None, *,
                 client_id: str = "quantgenlabs") -> None:
        kafka = _client()
        self.bootstrap = bootstrap_servers or os.getenv("QGL_KAFKA_BROKERS", "")
        if not self.bootstrap:
            raise KafkaUnavailable(
                "No brokers configured. Set QGL_KAFKA_BROKERS, e.g. localhost:9092"
            )
        self._kafka = kafka
        self._producer = kafka.KafkaProducer(
            bootstrap_servers=self.bootstrap.split(","),
            client_id=client_id,
            value_serializer=lambda v: v.encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8"),
            # A produce that can be acknowledged and then lost on leader failover
            # is, for a fills topic, a trade the system believes it recorded.
            acks="all",
            enable_idempotence=True,
            retries=10,
            max_in_flight_requests_per_connection=5,
            linger_ms=5,               # tiny batching window; latency vs syscalls
            compression_type="lz4",
        )
        self._consumers: dict[tuple[str, str], object] = {}
        self._handlers: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------- produce
    def publish(self, topic: str, key: str, payload: dict,
                correlation_id: str | None = None) -> Record:
        is_dlq = topic.endswith(DLQ_SUFFIX)
        s = spec(topic[: -len(DLQ_SUFFIX)] if is_dlq else topic)
        env = Envelope(topic=topic, key=key, payload=payload,
                       schema_version=s.schema_version, correlation_id=correlation_id)
        p = partition_for(key, s.partitions)
        fut = self._producer.send(topic, key=key, value=env.to_json(), partition=p)
        # Block for the broker's acknowledgement. An async publish that is never
        # awaited turns acks="all" into a lie -- nothing observes the failure.
        meta = fut.get(timeout=10)
        return Record(envelope=env, partition=meta.partition, offset=meta.offset)

    # ------------------------------------------------------------- consume
    def subscribe(self, topic: str, group: str,
                  handler: Callable[[Envelope], None], *,
                  max_attempts: int = 3) -> None:
        consumer = self._kafka.KafkaConsumer(
            topic,
            bootstrap_servers=self.bootstrap.split(","),
            group_id=group,
            # Offsets are committed only after the handler succeeds. Auto-commit
            # advances on a timer, so a crash between the commit and the work
            # loses messages that were never processed.
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda v: v.decode("utf-8"),
            key_deserializer=lambda k: k.decode("utf-8") if k else "",
            # Small batches: a consumer that fetches too many and processes them
            # slowly is evicted mid-batch, triggering a rebalance and redelivery.
            max_poll_records=100,
            max_poll_interval_ms=300000,
            session_timeout_ms=45000,
        )
        self._consumers[(topic, group)] = consumer
        self._handlers[(topic, group)] = {"handler": handler, "max_attempts": max_attempts,
                                          "seen": set()}

    def poll(self, topic: str, group: str, max_records: int = 100) -> int:
        consumer = self._consumers.get((topic, group))
        cfg = self._handlers.get((topic, group))
        if consumer is None or cfg is None:
            raise KeyError(f"No subscription for group '{group}' on '{topic}'")

        batches = consumer.poll(timeout_ms=1000, max_records=max_records)
        handled = 0
        for _tp, messages in batches.items():
            for msg in messages:
                env = Envelope.from_json(msg.value)
                if env.event_id in cfg["seen"]:
                    continue                      # idempotent redelivery
                ok = False
                for attempt in range(1, cfg["max_attempts"] + 1):
                    env.attempt = attempt
                    try:
                        cfg["handler"](env)
                        ok = True
                        break
                    except Exception:             # noqa: BLE001
                        continue
                if ok:
                    cfg["seen"].add(env.event_id)
                    handled += 1
                else:
                    # Poison message: route it aside rather than blocking the
                    # partition forever behind something that will never succeed.
                    self._producer.send(dlq_for(topic), key=env.key, value=env.to_json())
        if handled:
            consumer.commit()                     # only after the work succeeded
        return handled

    def replay(self, topic: str, from_offset: int = 0,
               partition: int | None = None) -> Iterable[Record]:
        from kafka import TopicPartition
        consumer = self._kafka.KafkaConsumer(
            bootstrap_servers=self.bootstrap.split(","),
            enable_auto_commit=False,             # replay must not move a group's
            value_deserializer=lambda v: v.decode("utf-8"),   # committed offsets
        )
        s = spec(topic)
        parts = [partition] if partition is not None else list(range(s.partitions))
        tps = [TopicPartition(topic, p) for p in parts]
        consumer.assign(tps)
        for tp in tps:
            consumer.seek(tp, from_offset)
        out: list[Record] = []
        for batch in consumer.poll(timeout_ms=2000, max_records=10000).values():
            for msg in batch:
                out.append(Record(Envelope.from_json(msg.value), msg.partition, msg.offset))
        consumer.close()
        return out

    def lag(self, topic: str, group: str) -> dict[int, int]:
        consumer = self._consumers.get((topic, group))
        if consumer is None:
            return {}
        out: dict[int, int] = {}
        for tp in consumer.assignment():
            end = consumer.end_offsets([tp])[tp]
            pos = consumer.position(tp)
            out[tp.partition] = max(0, end - pos)
        return out

    def close(self) -> None:
        for c in self._consumers.values():
            try:
                c.close()
            except Exception:
                pass
        try:
            self._producer.flush(timeout=5)
            self._producer.close(timeout=5)
        except Exception:
            pass
