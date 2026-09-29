"""Prometheus metrics for the trading path.

WHAT WAS MISSING. The platform exported six metrics, all about engine latency and
HTTP routes. Nothing about the thing it exists to do: no order counts, no fill
rates, no pre-trade rejections, no event-bus lag, no P&L, no FIX session state. A
dashboard built on that can tell you the pricing engine is healthy while the desk
has not filled an order in an hour.

LABEL CARDINALITY IS THE TRAP, and it is why the obvious labels are absent here.
Prometheus creates a separate time series per label combination, so labelling by
`cl_ord_id` or `desk_id` produces an unbounded series count that eventually kills
the server — the classic way monitoring takes down the thing it monitors. So:

  * Labelled by SYMBOL and SIDE, which are bounded by the instrument universe.
  * Labelled by REASON for rejections, bounded by the enum.
  * NEVER labelled by order id, execution id, desk id or user.

Anything unbounded that is genuinely needed belongs in a trace (where the
cardinality is the point) or a log, not a metric.

COUNTERS FOR EVENTS, GAUGES FOR STATE, HISTOGRAMS FOR DISTRIBUTIONS. A counter
that can go down is a bug — `rate()` on it produces nonsense. A gauge sampled at
scrape time cannot report a peak between scrapes, which is why latency is a
histogram and not a gauge holding "the last one".
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ----------------------------------------------------------------- orders
ORDERS_SUBMITTED = Counter(
    "qgl_orders_submitted_total",
    "Orders accepted by the desk and sent to a venue.",
    ("symbol", "side", "ord_type"),
)

ORDERS_TERMINAL = Counter(
    "qgl_orders_terminal_total",
    "Orders reaching a terminal state, by how they ended.",
    ("symbol", "status"),          # filled | cancelled | rejected
)

FILLS = Counter(
    "qgl_fills_total",
    "Individual executions received.",
    ("symbol", "side"),
)

FILLED_QTY = Counter(
    "qgl_filled_quantity_total",
    "Cumulative quantity filled. Separate from the fill COUNT because one fill "
    "of 10,000 and ten fills of 1,000 are different things operationally.",
    ("symbol", "side"),
)

# Buckets chosen for an in-process simulated venue: the whole round trip is
# sub-millisecond, so the default Prometheus buckets (starting at 5ms) would put
# every observation in the first bucket and report nothing.
ORDER_ROUND_TRIP = Histogram(
    "qgl_order_round_trip_seconds",
    "Submit to final ExecutionReport, in-process. EXCLUDES network and venue "
    "time, which dominate a real deployment.",
    ("symbol",),
    buckets=(0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1),
)

# --------------------------------------------------------- pre-trade risk
PRETRADE_DECISIONS = Counter(
    "qgl_pretrade_decisions_total",
    "Pre-trade gate outcomes by reason. The reason label is what makes this "
    "actionable: 'rejections are up' is not, 'price_collar rejections are up' is.",
    ("decision",),
)

KILL_SWITCH = Gauge(
    "qgl_kill_switch_engaged",
    "1 when the kill switch is engaged. A gauge, not a counter: it is a state.",
)

# ------------------------------------------------------------ market data
TICKS_VALIDATED = Counter(
    "qgl_market_ticks_total",
    "Ticks through the quality gate, by verdict.",
    ("verdict",),                  # accepted | sequence_gap | duplicate | ...
)

BOOK_UPDATES = Counter(
    "qgl_book_updates_total", "Order-book level updates applied.", ("symbol",),
)

BOOK_SPREAD = Gauge(
    "qgl_book_spread",
    "Current top-of-book spread in price units. A gauge because it is a "
    "point-in-time state, and it is per-symbol because an aggregate spread "
    "across instruments is not a number that means anything.",
    ("symbol",),
)

FEED_STALE = Gauge(
    "qgl_feed_stale",
    "1 when an instrument has no fresh mark. Distinct from 'no ticks arriving' "
    "-- a feed can be alive and an instrument still unmarked.",
    ("symbol",),
)

# ------------------------------------------------------------- event bus
BUS_PUBLISHED = Counter(
    "qgl_bus_published_total", "Events published, by topic.", ("topic",),
)

BUS_PUBLISH_FAILED = Counter(
    "qgl_bus_publish_failed_total",
    "Publishes that failed. Non-zero means the event log is incomplete while "
    "trading continued -- deliberate, but it must be visible.",
    ("topic",),
)

BUS_LAG = Gauge(
    "qgl_bus_consumer_lag",
    "Unread records per consumer group. The number an operator watches: a lag "
    "that grows monotonically means a consumer is falling behind permanently.",
    ("topic", "group"),
)

BUS_DEAD_LETTERED = Counter(
    "qgl_bus_dead_lettered_total",
    "Messages a consumer could not process after its retries.",
    ("topic",),
)

# ------------------------------------------------------------------- P&L
PNL = Gauge(
    "qgl_pnl",
    "Profit and loss by component. Realised and unrealised are separated "
    "because they are different kinds of number: one is a fact, the other is an "
    "opinion about a mark.",
    ("component",),                # realized | unrealized | fees | total
)

POSITION_QTY = Gauge(
    "qgl_position_quantity", "Net signed position.", ("symbol",),
)

POSITIONS_UNVALUED = Gauge(
    "qgl_positions_unvalued",
    "Open positions with a stale or missing mark, therefore EXCLUDED from "
    "unrealised P&L. Non-zero means the P&L total on the dashboard is partial.",
)

# ------------------------------------------------------------ FIX session
FIX_SESSION_UP = Gauge(
    "qgl_fix_session_up", "1 when the FIX session is active.", ("session",),
)

FIX_MESSAGES = Counter(
    "qgl_fix_messages_total",
    "FIX messages by direction and type. Bounded label: MsgType is an enum.",
    ("direction", "msg_type"),
)

FIX_SEQ_NUM = Gauge(
    "qgl_fix_seq_num",
    "Current sequence numbers. A gap between what we sent and what the "
    "counterparty expects is the first sign of a session problem.",
    ("session", "kind"),           # outbound | expected_inbound
)

# ------------------------------------------------------- execution algos
ALGO_PARENTS = Counter(
    "qgl_algo_parents_total", "Parent orders started, by algo.", ("algo", "symbol"),
)

ALGO_SLICES = Counter(
    "qgl_algo_slices_total", "Child slices sent, by algo.", ("algo", "symbol"),
)

ALGO_ACTIVE = Gauge(
    "qgl_algo_active", "Parent orders currently working.", ("algo",),
)

ALGO_SHORTFALL_BPS = Histogram(
    "qgl_algo_shortfall_bps",
    "Implementation shortfall against the arrival price, in basis points. The "
    "honest execution-quality measure: everything after the decision to trade "
    "is a cost of that decision.",
    ("algo",),
    buckets=(-50, -20, -10, -5, -2, -1, 0, 1, 2, 5, 10, 20, 50, 100),
)


def observe_pretrade(decision: str) -> None:
    PRETRADE_DECISIONS.labels(decision=decision).inc()


def observe_tick(verdict: str) -> None:
    TICKS_VALIDATED.labels(verdict=verdict).inc()


def observe_bus(topic: str, ok: bool) -> None:
    (BUS_PUBLISHED if ok else BUS_PUBLISH_FAILED).labels(topic=topic).inc()


def set_pnl(totals: dict) -> None:
    for k in ("realized", "unrealized", "fees", "total"):
        if isinstance(totals.get(k), (int, float)):
            PNL.labels(component=k).set(totals[k])
    if isinstance(totals.get("positions_unvalued"), int):
        POSITIONS_UNVALUED.set(totals["positions_unvalued"])
