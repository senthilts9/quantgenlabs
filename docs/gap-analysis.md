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
| 3 | Kafka / streaming backbone | **Absent** | **Absent** | Not started. No broker in the dev environment. |
| 4 | Databricks / Delta Lake / medallion | **Absent** | **Absent** | Not started. |
| 5 | PostgreSQL operational store | **Absent** | **Absent** | SQLite today. |
| 6 | C++ low-latency core | Partial | **Good** | Was pricing/risk only. Now adds book, feed handler, FIX engine, pre-trade risk, matching engine, benchmark harness. |
| 7 | FIX protocol / connectivity | **Absent** | **Good** | Wire format + session layer + simulated venue, 48 tests. Logon/Heartbeat/TestRequest/Resend/NewOrderSingle/ExecutionReport/Cancel/CancelReplace/Reject/Logout. |
| 8 | Complete trade lifecycle | Partial | **Good** | End to end and runnable (`lifecycle_demo`). Allocation, confirmation, settlement and reconciliation still absent. |
| 9 | OMS / EMS / SOR | **Good** | **Good** | Already strong: marginal-cost routing, ML fill probability, TCA. Unchanged by this work. |
| 10 | Pricing engine | **Good** | **Good** | BSM, Black-76, binomial, MC, LSM American, GARCH, curves, PCA. Heston/SABR/local-vol absent. |
| 11 | Risk engine | **Good** | **Good** | VaR/ES (historical, parametric, MC), EVT tails, stress, Greeks, limits. CVA/PFE absent. |
| 12 | Real-time P&L | Partial | Partial | Reconciled from ExecutionReports in the demo; no streaming attribution service. |
| 13 | **Crypto trading — must be retained** | **Good** | **Good** | `backend/cryptodesk` (12.9k LOC) and `frontend/src/cryptodesk` (9k LOC) **untouched**. Verified by `verify_crypto.py` still passing. |
| 14 | Portfolio management | **Good** | **Good** | Markowitz, efficient frontier, risk parity, attribution, stress. |
| 15 | Enterprise UI/UX | Partial | Partial | CryptoDesk console is dense and professional. No unified multi-asset workspace, command palette or virtualised tables. |
| 16 | Observability / SRE | Partial | Partial | Prometheus + Grafana + structured metrics. No OpenTelemetry traces, no correlation IDs. |
| 17 | Security / RBAC | **Absent** | **Absent** | No auth layer. Secrets handling is correct (`.gitignore`, no hardcoded defaults). |
| 18 | Testing | Partial | **Good** | 126 C++ tests (was 9), 48 Python, 5 property, 3 verifiers. Load/soak/failure-recovery tests absent. |
| 19 | Benchmark / performance lab | Partial | **Good** | Reproducible harness with full environment capture, repetitions and spread. Not yet surfaced in the UI. |
| 20 | Institutional features | Partial | Partial | Kill switch, fat-finger, collars, position limits, halts, rate limiting added. Security master, reconciliation, EOD/SOD, model governance absent. |
| 21 | Repository structure | Partial | Partial | `cpp/`, `backend/`, `frontend/`, `docs/`, `benchmarks/`, `observability/`. Not restructured into the `/services` + `/apps` layout the spec sketches. |
| 22 | Incremental execution | — | **Done** | Version control established first; each increment built, tested and committed separately. |

## Scorecard

|  | Before | Now |
|---|---|---|
| Good | 5 | 11 |
| Partial | 9 | 8 |
| Absent | 7 | 5 |

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
