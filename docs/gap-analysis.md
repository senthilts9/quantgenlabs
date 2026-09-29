# Gap analysis against the institutional-platform specification

Assessed 2026-09-28, before and after the market-connectivity work. The
specification has 22 sections; this records honestly which are covered, which are
partial, and which are absent.

## Method

The "before" column is what existed when the codebase was first put under version
control (commit `0360850`). Presence was established by grepping for the relevant
technology across `backend/app`, `backend/cryptodesk`, `cpp`, `frontend/src`,
`observability` and `docker-compose.yml` — not by reading the README, which
overstated some things.

## Status by specification section

| § | Area | Before | Now | Notes |
|---|---|---|---|---|
| 1 | Event-driven architecture, DDD, clean layering | Partial | Partial | `api`/`core`/`services`/`obs` separation was already good. Still request/response, not event-sourced. |
| 2 | Market-data platform | Partial | **Good** | Feed validation, gap/duplicate/reorder/stale/outlier detection and an L2 book added in C++. Corporate actions, symbology and calendars still absent. |
| 3 | Kafka / streaming backbone | **Absent** | **Good** | Topic catalogue with justified partition keys, retention and schema versions; EventBus interface; KafkaBus (written, never run — no broker here); InProcessBus implementing the same semantics and fully tested (ordering, consumer groups, at-least-once, idempotency, DLQ, replay). WebSocket streaming to the UI. |
| 4 | Databricks / Delta Lake / medallion | **Absent** | **Good** | Bronze/silver/gold on delta-rs — **real Delta tables, genuinely executed and tested**, no Spark/JVM/Databricks needed. Idempotent ingest, rejects kept with reasons, time travel. Databricks itself would add a cluster and Unity Catalog, not a different format. |
| 5 | PostgreSQL operational store | **Absent** | **Good** | SQLAlchemy schema + repository + Alembic, opt-in via QGL_DATABASE_URL. 53 repository tests run against BOTH SQLite and a real PostgreSQL 16.2 (pgserver wheel, no Docker needed); migrations verified up AND down on both. Crypto desk deliberately stays on SQLite. |
| 6 | C++ low-latency core | Partial | **Good** | Book, feed handler, FIX engine, pre-trade risk, matching engine, plus a lock-free SPSC ring and a **latency-distribution harness (P50/P90/P99/P99.9)** — the tail figures §6 asked for and earlier throughput work had not answered. |
| 7 | FIX protocol / connectivity | **Absent** | **Good** | Wire format + session layer + simulated venue, 48 tests. Logon/Heartbeat/TestRequest/Resend/NewOrderSingle/ExecutionReport/Cancel/CancelReplace/Reject/Logout. |
| 8 | Complete trade lifecycle | Partial | **Good** | End to end and runnable (`lifecycle_demo`). Allocation, confirmation, settlement and reconciliation still absent. |
| 9 | OMS / EMS / SOR | **Good** | **Good** | Already strong: marginal-cost routing, ML fill probability, TCA. Unchanged by this work. |
| 10 | Pricing engine | **Good** | **Good** | BSM, Black-76, binomial, MC, LSM American, GARCH, curves, PCA. Heston/SABR/local-vol absent. |
| 11 | Risk engine | **Good** | **Good** | VaR/ES (historical, parametric, MC), EVT tails, stress, Greeks, limits. CVA/PFE absent. |
| 12 | Real-time P&L | Partial | Partial | Position and realised P&L now derived from booked fills with correct increase/reduce/cross-zero accounting. No streaming attribution service. |
| 13 | **Crypto trading — must be retained** | **Good** | **Good** | `backend/cryptodesk` (12.9k LOC) and `frontend/src/cryptodesk` (9k LOC) **untouched**. Verified by `verify_crypto.py` still passing. |
| 14 | Portfolio management | **Good** | **Good** | Markowitz, efficient frontier, risk parity, attribution, stress. |
| 15 | Enterprise UI/UX | Partial | **Good** | Command palette (⌘K, fuzzy), light/dark theme on CSS tokens, virtualised sortable/filterable table with CSV export, Trading Desk with ladder + ticket + blotter + FIX monitor, and **WebSocket streaming with reconnect, gap detection and a data-freshness indicator**. Still missing: resizable panels, saved workspaces. |
| 16 | Observability / SRE | Partial | **Good** | Prometheus + Grafana, plus OpenTelemetry tracing correlated on ClOrdID and an X-Trace-Id response header. Opt-in via QGL_TRACING. |
| 17 | Security / RBAC | **Absent** | **Good** | JWT + scrypt + four roles as separations of duty (trader cannot set limits, risk cannot trade, admin is not a superset). Opt-in via QGL_REQUIRE_AUTH; the ENABLED path is tested. Crypto desk keeps its wallet-signature auth. |
| 18 | Testing | Partial | **Good** | 155 C++ (was 9), 275 Python (was 48), 16 frontend units (was 0), 5 property, 3 verifiers. Load/soak/failure-recovery still absent. |
| 19 | Benchmark / performance lab | Partial | **Good** | Throughput harness *and* a latency-distribution harness that measures the clock's own cost first and flags any result sitting at that floor. Surfaced in a Performance Lab screen that cannot display a number the harness did not produce. |
| 20 | Institutional features | Partial | Partial | Kill switch, fat-finger, collars, position limits, halts, rate limiting added. Security master, reconciliation, EOD/SOD, model governance absent. |
| 21 | Repository structure | Partial | Partial | `cpp/`, `backend/`, `frontend/`, `docs/`, `benchmarks/`, `observability/`. Not restructured into the `/services` + `/apps` layout the spec sketches. |
| 22 | Incremental execution | — | **Done** | Version control established first; each increment built, tested and committed separately. |

## Scorecard

|  | Before | After C++ core | Now |
|---|---|---|---|
| Good | 5 | 11 | 18 |
| Partial | 9 | 8 | 4 |
| Absent | 7 | 5 | **0** |

No section is entirely absent. Two carry an explicit caveat rather than a clean
bill:

- **Kafka (§3).** The topic design, the bus contract and all the consumer
  semantics are real and tested against the in-process implementation. The
  `KafkaBus` transport itself has **never been run against a broker** — none
  exists in this environment — and that is stated at the top of the file rather
  than left to be discovered.
- **Databricks (§4).** The medallion pipeline genuinely runs on real Delta tables
  via delta-rs. What a Databricks workspace would add is a cluster, Unity Catalog
  and distributed execution — none of which change the table format or the
  pipeline shape.

## The five remaining absences, and what each actually requires

**Kafka (§3).** The honest blocker is not code, it is that there is no broker to
test against — Docker is not installed on the dev host. Writing producers and
consumers that have never connected to a broker would produce code with the shape
of correctness and none of the substance. The design work that *can* be done
without one is topic partitioning strategy and schema versioning, and that is where
this should start.

**Databricks / Delta Lake (§4).** Same constraint, larger. A medallion pipeline is
testable locally against `delta-rs` or a Spark local session, which is the sensible
first step rather than targeting a Databricks workspace that does not exist.

**PostgreSQL (§5).** This one is genuinely straightforward and is the highest-value
remaining item: the domain models exist, so it is SQLAlchemy models plus Alembic
migrations plus a repository interface with the existing SQLite path as one
implementation. No external dependency beyond a local Postgres.

**Auth / RBAC (§17).** Straightforward and self-contained: JWT issuance, a role
enum (Trader / Risk / Admin), and dependency-injected guards on the FastAPI
routers. Worth doing before anything is deployed anywhere real.

**OpenTelemetry (§16).** Mostly mechanical given Prometheus is already wired: add
the SDK, instrument the FastAPI app and the SOR path, and propagate correlation IDs
from the order id that already exists.

## Two things the specification asked for that were deliberately not done

**An 8-core Monte Carlo figure.** The objective was ~34M paths/sec single-thread
scaling to ~250M on 8 cores. The single-thread target is met and exceeded (39.4M
baseline, 70.9M with AVX2). The 8-core figure is **not reported**, because this
host has 2 physical cores. Multiplying a measured per-core number by 8 would
produce something indistinguishable from a measurement and it would not be one.
The harness emits `eight_physical_core_measured: false` and populates the field
automatically when run on suitable hardware. See
[`benchmark-methodology.md`](benchmark-methodology.md).

**A repository restructure into `/services` and `/apps`.** The spec sketches a
layout, but the existing `api`/`core`/`services` separation inside `backend/app`
already achieves the separation of concerns the layout is *for*, and the crypto
module is already self-contained. Moving 39k LOC of working, tested code between
directories carries real regression risk and delivers no behavioural improvement,
which trades badly. Recorded here as a deliberate decision rather than an
oversight — spec rule 15 says working functionality should not be disturbed merely
because a new architecture is introduced.
