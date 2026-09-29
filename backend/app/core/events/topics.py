"""Topic catalogue, partitioning strategy and schema versioning.

THIS FILE IS THE DESIGN, and it is the part that survives a change of broker. The
transport is replaceable; what topics exist, how they are keyed, and how their
payloads evolve is the architecture.

PARTITION KEY IS ORDERING. Kafka guarantees order within a partition and nothing
across partitions, so the key choice IS the ordering guarantee:

  * Order lifecycle events key on ORDER ID. Every event for one order lands in one
    partition, so an ExecutionReport can never overtake the ack it follows. Keying
    on symbol instead would be a bigger blast radius for no benefit; keying on
    nothing would let a fill be processed before the order it fills.
  * Market data keys on SYMBOL. A book is rebuilt per instrument, so per-symbol
    order is what a consumer needs, and per-symbol keying spreads load across
    partitions better than a single hot key.
  * Positions key on PORTFOLIO+SYMBOL, because that tuple is the unit whose
    running total must be applied in order.

RETENTION IS A CORRECTNESS PROPERTY, not a storage setting. Audit events are
compacted-forever because deleting them destroys the trail they exist to be.
Market data is time-bounded because a replay of last month's book has no consumer.

SCHEMA VERSIONING. Every envelope carries a schema version. The rule is that
consumers accept any version they know and REFUSE a version from the future
rather than guessing — a consumer that silently ignores fields it does not
understand will process a v2 event as though it were v1, and the difference
between those is usually the field that was added because v1 was wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Retention(str, Enum):
    COMPACT_FOREVER = "compact"      # keep the latest per key, indefinitely
    DAYS_7 = "7d"
    DAYS_30 = "30d"
    YEARS_7 = "7y"                   # regulatory retention


@dataclass(frozen=True)
class TopicSpec:
    name: str
    key_field: str                   # which envelope field supplies the partition key
    partitions: int
    retention: Retention
    schema_version: int
    purpose: str


# The catalogue. Partition counts are sized by expected key cardinality and
# consumer parallelism -- a topic with 1 partition cannot be consumed in parallel
# no matter how many consumers subscribe, and a topic with 100 partitions for 3
# keys wastes 97 of them.
TOPICS: dict[str, TopicSpec] = {
    t.name: t for t in [
        TopicSpec("market.raw", "symbol", 12, Retention.DAYS_7, 1,
                  "Untouched venue payloads. Kept so a normalisation bug can be "
                  "replayed against corrected code rather than re-requested."),
        TopicSpec("market.normalized", "symbol", 12, Retention.DAYS_7, 1,
                  "Post-validation ticks. What pricing and risk consume."),
        TopicSpec("market.quality", "symbol", 3, Retention.DAYS_30, 1,
                  "Gap, duplicate, reorder and outlier verdicts from the feed gate."),
        TopicSpec("orders.lifecycle", "order_id", 6, Retention.YEARS_7, 1,
                  "New/ack/reject/cancel/amend. Keyed on order id so no event for "
                  "an order can overtake another."),
        TopicSpec("trades.executed", "order_id", 6, Retention.YEARS_7, 1,
                  "Fills. Same key as the lifecycle so a fill cannot be processed "
                  "before the order it fills."),
        TopicSpec("positions.updated", "position_key", 6, Retention.DAYS_30, 1,
                  "Position deltas, keyed portfolio+symbol -- the unit whose "
                  "running total must be applied in order."),
        TopicSpec("risk.breaches", "symbol", 3, Retention.YEARS_7, 1,
                  "Pre-trade refusals and limit breaches. Retained as long as the "
                  "trades they relate to."),
        TopicSpec("pnl.realtime", "position_key", 3, Retention.DAYS_7, 1,
                  "Mark-to-market updates."),
        TopicSpec("audit.events", "entity_id", 3, Retention.COMPACT_FOREVER, 1,
                  "Immutable trail. Never deleted -- deleting it destroys the thing "
                  "it exists to be."),
    ]
}

# Where a message goes when a consumer cannot process it. Without this, a poison
# message is retried forever and blocks its partition: every later message for
# that key waits behind one it will never get past.
DLQ_SUFFIX = ".dlq"


def dlq_for(topic: str) -> str:
    return f"{topic}{DLQ_SUFFIX}"


def spec(topic: str) -> TopicSpec:
    if topic not in TOPICS:
        raise KeyError(
            f"Unknown topic '{topic}'. Topics are declared in topics.py rather "
            f"than created implicitly on first publish: a typo would otherwise "
            f"create a topic nobody consumes and lose the messages silently."
        )
    return TOPICS[topic]
