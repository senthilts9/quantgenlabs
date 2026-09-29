# Deployment

## What has actually been run

Being precise about this matters more than the instructions themselves.

| Artefact | Status |
|---|---|
| `docker-compose.yml` | **Not run here.** Docker is not installed in the development environment. Syntax validated; CI runs `docker compose config` and builds both images. |
| `deploy/Dockerfile.api` | **Not built here**, built in CI — which also asserts `mkt_connectivity` imports and a FIX session comes up *inside the image*. |
| `deploy/Dockerfile.web` | **Not built here**, built in CI. |
| `deploy/k8s/*` | **Written, never applied.** No cluster available. Schema-validated only. |
| The app itself | Run continuously against a live backend, Postgres, and a real browser. |

Manifests that have never touched an API server are not "deployed", and saying so
is cheaper than someone finding out during a rollout.

## Quickstart (Docker Compose)

```bash
cp .env.example .env          # set GRAFANA_ADMIN_PASSWORD at minimum
docker compose up --build
```

- Console → <http://localhost:5174>
- API → <http://localhost:8000/api/health>
- Prometheus → <http://localhost:9090> · Grafana → <http://localhost:3000>

Everything optional is off by default, so this works with nothing configured: the
OMS uses its SQLite book, auth is not enforced, tracing is off.

## Configuration

Every switch below is **off unless set**. That is deliberate — a clone with no
configuration should run, not fail on a missing variable.

| Variable | Effect if unset |
|---|---|
| `QGL_DATABASE_URL` | OMS uses SQLite. Set it to run on PostgreSQL. |
| `QGL_REQUIRE_AUTH` | Auth not enforced; guards still run, actor is `anonymous`. |
| `QGL_USERS` | **No accounts exist.** Must be set *with* `QGL_REQUIRE_AUTH`. |
| `QGL_JWT_SECRET` | Dev: ephemeral per-process key (tokens die on restart). **Production: startup error.** |
| `QGL_TRACING` | Tracing off. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Traces go to stdout instead of a collector. |
| `QGL_DATA_DIR` | Resolves to `/app/data` in a container, `backend/data` locally. |
| `ANTHROPIC_API_KEY` | Copilot returns "offline". |

### Two variables that must be set together

`QGL_REQUIRE_AUTH=true` with no `QGL_USERS` locks every guarded endpoint with no
way in — there are no default accounts, on purpose. Set both or neither.

## Enabling PostgreSQL

The crypto desk **stays on SQLite** either way; this is the OMS store only.

```bash
# 1. Uncomment the `postgres` service and `pg-data` volume in docker-compose.yml
# 2. Uncomment QGL_DATABASE_URL under the api service
# 3. Set POSTGRES_PASSWORD in .env  (compose refuses to start without it)
docker compose up -d postgres
QGL_DATABASE_URL=postgresql://qgl:$POSTGRES_PASSWORD@localhost:5432/quantgenlabs \
  backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
docker compose up -d --build
```

Migrations are verified up **and down** on both SQLite and PostgreSQL in the test
suite — a migration only ever run forward is one you find out about during an
incident.

## Enabling auth

```bash
QGL_JWT_SECRET="$(openssl rand -base64 48)"        # ≥32 chars or the app refuses
QGL_USERS="trader1:<pw>:trader;risk1:<pw>:risk;admin1:<pw>:admin"
QGL_REQUIRE_AUTH=true
```

Roles are **separations of duty, not a hierarchy**: a trader cannot set limits, a
risk manager cannot submit orders, and admin is deliberately not a superset of
trader. `GET /api/auth/matrix` publishes the matrix as data.

## Kubernetes

See [`deploy/k8s/README.md`](../deploy/k8s/README.md). Two constraints worth
knowing before scaling:

1. **The desk holds FIX sessions in process memory.** Sequence numbers only mean
   something within one connection, so a pod that dies takes its sessions with
   it. `minReplicas: 2` keeps the API available through a rollout; it does not
   make sessions survive one.
2. **With SQLite, each replica has its own OMS book.** Scaling past one pod is
   only meaningful once `QGL_DATABASE_URL` points them all at one database.

Scale-down is deliberately slow (5-minute stabilisation) because shedding a pod
mid-session costs more than the capacity saves.

## Health and observability

- `GET /api/health` — liveness and readiness; reports whether the C++ engines loaded.
- `GET /metrics` — Prometheus. Scrape annotations are set on the API pods.
- `X-Trace-Id` response header when tracing is on — paste straight into a viewer.
- `GET /api/connectivity/status` — which implementation is serving each component.

A `startupProbe` runs before liveness because first boot loads the C++ extensions
and warms the board; without it a slow start reads as a liveness failure and the
pod is killed in a loop.
