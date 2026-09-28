"""SQLite persistence for the OMS.

Why SQLite: single-node deployment, modest volume, and the workflow needs
ACID (an order and its fill must land together or not at all). Postgres would
be another container and another failure mode for no benefit at this size --
if throughput ever demands it, only this file changes.

Concurrency: FastAPI runs sync handlers in a threadpool, so connections are
opened with check_same_thread=False and every write goes through one lock.
WAL mode lets reads proceed during writes, which is what keeps the blotter
responsive while an algo is filling.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from .models import Fill, Order, OrderStatus, OrderType, Position, Side

DEFAULT_DB = os.getenv("OMS_DB_PATH", "/app/data/oms.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    order_type    TEXT NOT NULL,
    limit_price   REAL,
    status        TEXT NOT NULL,
    filled_qty    REAL NOT NULL DEFAULT 0,
    avg_price     REAL NOT NULL DEFAULT 0,
    parent_id     TEXT,
    portfolio_id  TEXT NOT NULL DEFAULT 'DEFAULT',
    venue         TEXT,
    algo          TEXT,
    arrival_mid   REAL,
    reject_reason TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_symbol  ON orders(symbol);
CREATE INDEX IF NOT EXISTS ix_orders_parent  ON orders(parent_id);
CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created_at DESC);

CREATE TABLE IF NOT EXISTS fills (
    id       TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    symbol   TEXT NOT NULL,
    side     TEXT NOT NULL,
    qty      REAL NOT NULL,
    price    REAL NOT NULL,
    venue    TEXT,
    fee      REAL NOT NULL DEFAULT 0,
    ts       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS ix_fills_ts    ON fills(ts DESC);

CREATE TABLE IF NOT EXISTS risk_limits (
    id      INTEGER PRIMARY KEY CHECK (id = 1),   -- single config row
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS limit_breaches (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    side      TEXT NOT NULL,
    qty       REAL NOT NULL,
    gate      TEXT NOT NULL,
    detail    TEXT NOT NULL,
    observed  REAL,
    limit_val REAL,
    order_id  TEXT
);
CREATE INDEX IF NOT EXISTS ix_breaches_ts ON limit_breaches(ts DESC);

CREATE TABLE IF NOT EXISTS llm_invocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    model         TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    latency_ms    REAL NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT,
    prompt_chars  INTEGER NOT NULL DEFAULT 0,
    reply_chars   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_llm_ts ON llm_invocations(ts DESC);

-- One row per (route, venue): what the model predicted, what the router
-- asked for, and what came back. This is the training set for the SOR fill
-- model. `outcome_source` is NOT NULL on purpose -- a row whose provenance is
-- unknown must never be trainable, so there is nowhere to put an empty value.
CREATE TABLE IF NOT EXISTS route_observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    route_id       TEXT NOT NULL,
    symbol         TEXT,
    side           TEXT NOT NULL,
    venue          TEXT NOT NULL,
    data_source    TEXT NOT NULL,     -- feature provenance: synthetic | ccxt:...
    outcome_source TEXT NOT NULL,     -- label provenance: fade-sim | live
    model_version  INTEGER,
    predicted_p    REAL,
    size_ratio     REAL NOT NULL,
    latency_ms     REAL NOT NULL,
    spread_bps     REAL NOT NULL,
    volatility     REAL NOT NULL,
    requested_qty  REAL NOT NULL,
    filled_qty     REAL NOT NULL,
    fill_rate      REAL NOT NULL,
    label_filled   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_obs_ts    ON route_observations(ts DESC);
CREATE INDEX IF NOT EXISTS ix_obs_route ON route_observations(route_id);
CREATE INDEX IF NOT EXISTS ix_obs_src   ON route_observations(outcome_source);

-- Versioned model artifacts. Append-only: a superseded model is archived, not
-- deleted, because "which model produced this routing decision" has to stay
-- answerable after the model has been replaced.
CREATE TABLE IF NOT EXISTS model_registry (
    version       INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'sor_fill',
    status        TEXT NOT NULL DEFAULT 'challenger',  -- champion|challenger|archived
    sample_source TEXT NOT NULL,
    n_train       INTEGER NOT NULL DEFAULT 0,
    n_test        INTEGER NOT NULL DEFAULT 0,
    params        TEXT NOT NULL,      -- JSON {w, b, mu, sd}
    metrics       TEXT NOT NULL,      -- JSON, as reported by ml.training_metrics
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS ix_reg_status ON model_registry(kind, status);

CREATE TABLE IF NOT EXISTS portfolios (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    base_ccy   TEXT NOT NULL DEFAULT 'USD',
    mandate    TEXT,
    created_at TEXT NOT NULL
);

-- Composite key: the same instrument can be held independently by several
-- portfolios, and each keeps its own average cost and realised P&L. A single
-- symbol PK would silently merge two desks' books into one position.
CREATE TABLE IF NOT EXISTS positions (
    portfolio_id TEXT NOT NULL DEFAULT 'DEFAULT',
    symbol       TEXT NOT NULL,
    qty          REAL NOT NULL DEFAULT 0,
    avg_price    REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees         REAL NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, symbol)
);
"""


def _col(r: sqlite3.Row, name: str, default=None):
    """Tolerant column read -- a DB created before a migration will not have
    the newer columns, and a KeyError there would break every read path."""
    try:
        return r[name]
    except (IndexError, KeyError):
        return default


def _order_from_row(r: sqlite3.Row) -> Order:
    return Order(
        id=r["id"], symbol=r["symbol"], side=Side(r["side"]), qty=r["qty"],
        order_type=OrderType(r["order_type"]), limit_price=r["limit_price"],
        status=OrderStatus(r["status"]), filled_qty=r["filled_qty"],
        avg_price=r["avg_price"], parent_id=r["parent_id"], venue=r["venue"],
        algo=r["algo"], arrival_mid=r["arrival_mid"],
        reject_reason=r["reject_reason"], created_at=r["created_at"],
        updated_at=r["updated_at"],
        portfolio_id=_col(r, "portfolio_id", "DEFAULT") or "DEFAULT")


def _fill_from_row(r: sqlite3.Row) -> Fill:
    return Fill(id=r["id"], order_id=r["order_id"], symbol=r["symbol"],
                side=Side(r["side"]), qty=r["qty"], price=r["price"],
                venue=r["venue"], fee=r["fee"], ts=r["ts"])


def _position_from_row(r: sqlite3.Row) -> Position:
    return Position(symbol=r["symbol"], qty=r["qty"], avg_price=r["avg_price"],
                    realized_pnl=r["realized_pnl"], fees=r["fees"],
                    updated_at=r["updated_at"],
                    portfolio_id=_col(r, "portfolio_id", "DEFAULT") or "DEFAULT")


class OMSStore:
    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL: readers don't block on the writer, so the blotter stays live
        # while an execution algo is inserting fills.
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring an existing database up to the current schema.

        Runs on every boot and is idempotent. Deployed instances already hold
        real orders and fills, so the multi-portfolio change has to be a
        migration rather than a fresh CREATE -- dropping the table would throw
        away the audit trail the OMS exists to keep.
        """
        cur = self._conn.execute("PRAGMA table_info(orders)")
        cols = {r[1] for r in cur.fetchall()}
        if cols and "portfolio_id" not in cols:
            self._conn.execute(
                "ALTER TABLE orders ADD COLUMN portfolio_id TEXT NOT NULL "
                "DEFAULT 'DEFAULT'")
        # Index created here, NOT in _SCHEMA: on an existing database the
        # schema script runs before this migration, so indexing portfolio_id
        # up there fails with "no such column" before the ALTER can add it.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_orders_pf ON orders(portfolio_id)")

        cur = self._conn.execute("PRAGMA table_info(positions)")
        pcols = [r[1] for r in cur.fetchall()]
        if pcols and "portfolio_id" not in pcols:
            # positions' primary key changes from (symbol) to
            # (portfolio_id, symbol); SQLite cannot ALTER a PK, so rebuild.
            self._conn.executescript("""
                ALTER TABLE positions RENAME TO positions_old;
                CREATE TABLE positions (
                    portfolio_id TEXT NOT NULL DEFAULT 'DEFAULT',
                    symbol       TEXT NOT NULL,
                    qty          REAL NOT NULL DEFAULT 0,
                    avg_price    REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    fees         REAL NOT NULL DEFAULT 0,
                    updated_at   TEXT NOT NULL,
                    PRIMARY KEY (portfolio_id, symbol)
                );
                INSERT INTO positions
                    (portfolio_id, symbol, qty, avg_price, realized_pnl, fees, updated_at)
                SELECT 'DEFAULT', symbol, qty, avg_price, realized_pnl, fees, updated_at
                FROM positions_old;
                DROP TABLE positions_old;
            """)

        self._conn.execute(
            "INSERT OR IGNORE INTO portfolios (id,name,base_ccy,mandate,created_at) "
            "VALUES ('DEFAULT','Default Book','USD','general',?)",
            (__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(timespec="milliseconds"),))

    # ---- portfolios -----------------------------------------------------
    def upsert_portfolio(self, pid: str, name: str, base_ccy: str = "USD",
                         mandate: str | None = None) -> None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
        with self._lock:
            self._conn.execute(
                """INSERT INTO portfolios (id,name,base_ccy,mandate,created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, base_ccy=excluded.base_ccy,
                     mandate=excluded.mandate""",
                (pid.upper(), name, base_ccy, mandate, now))
            self._conn.commit()

    def list_portfolios(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM portfolios ORDER BY id")]

    def delete_portfolio(self, pid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM portfolios WHERE id=? AND id!='DEFAULT'",
                               (pid.upper(),))
            self._conn.commit()

    # ---- writes ---------------------------------------------------------
    def insert_order(self, o: Order) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO orders (id,symbol,side,qty,order_type,limit_price,
                   status,filled_qty,avg_price,parent_id,portfolio_id,venue,algo,
                   arrival_mid,reject_reason,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (o.id, o.symbol, o.side.value, o.qty, o.order_type.value,
                 o.limit_price, o.status.value, o.filled_qty, o.avg_price,
                 o.parent_id, o.portfolio_id, o.venue, o.algo, o.arrival_mid,
                 o.reject_reason, o.created_at, o.updated_at))
            self._conn.commit()

    def update_order(self, o: Order) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE orders SET status=?, filled_qty=?, avg_price=?,
                   venue=?, reject_reason=?, updated_at=? WHERE id=?""",
                (o.status.value, o.filled_qty, o.avg_price, o.venue,
                 o.reject_reason, o.updated_at, o.id))
            self._conn.commit()

    def record_fill(self, fill: Fill, order: Order, position: Position) -> None:
        """Fill + order state + position in ONE transaction.

        These three must move together: a fill that landed without its
        position update would silently corrupt P&L, and that class of bug is
        exactly what an audit trail is supposed to make impossible.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    """INSERT INTO fills (id,order_id,symbol,side,qty,price,venue,fee,ts)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (fill.id, fill.order_id, fill.symbol, fill.side.value,
                     fill.qty, fill.price, fill.venue, fill.fee, fill.ts))
                self._conn.execute(
                    """UPDATE orders SET status=?, filled_qty=?, avg_price=?,
                       venue=?, updated_at=? WHERE id=?""",
                    (order.status.value, order.filled_qty, order.avg_price,
                     order.venue, order.updated_at, order.id))
                self._conn.execute(
                    """INSERT INTO positions (portfolio_id,symbol,qty,avg_price,realized_pnl,fees,updated_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(portfolio_id,symbol) DO UPDATE SET
                         qty=excluded.qty, avg_price=excluded.avg_price,
                         realized_pnl=excluded.realized_pnl, fees=excluded.fees,
                         updated_at=excluded.updated_at""",
                    (position.portfolio_id, position.symbol, position.qty,
                     position.avg_price, position.realized_pnl, position.fees,
                     position.updated_at))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---- reads ----------------------------------------------------------
    def get_order(self, order_id: str) -> Order | None:
        cur = self._conn.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        r = cur.fetchone()
        return _order_from_row(r) if r else None

    def list_orders(self, limit: int = 200, symbol: str | None = None,
                    status: str | None = None,
                    portfolio_id: str | None = None) -> list[Order]:
        q = "SELECT * FROM orders"
        clauses, args = [], []
        if symbol:
            clauses.append("symbol=?"); args.append(symbol)
        if status:
            clauses.append("status=?"); args.append(status)
        if portfolio_id:
            clauses.append("portfolio_id=?"); args.append(portfolio_id)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [_order_from_row(r) for r in self._conn.execute(q, args)]

    def list_fills(self, limit: int = 200, order_id: str | None = None) -> list[Fill]:
        if order_id:
            cur = self._conn.execute(
                "SELECT * FROM fills WHERE order_id=? ORDER BY ts DESC LIMIT ?",
                (order_id, limit))
        else:
            cur = self._conn.execute(
                "SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))
        return [_fill_from_row(r) for r in cur]

    def get_position(self, symbol: str,
                     portfolio_id: str = "DEFAULT") -> Position | None:
        cur = self._conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND portfolio_id=?",
            (symbol, portfolio_id))
        r = cur.fetchone()
        return _position_from_row(r) if r else None

    def list_positions(self, include_flat: bool = False,
                       portfolio_id: str | None = None) -> list[Position]:
        """portfolio_id=None means ALL portfolios (firm-wide view)."""
        q, args = "SELECT * FROM positions", []
        where = []
        if not include_flat:
            where.append("qty != 0")
        if portfolio_id:
            where.append("portfolio_id=?"); args.append(portfolio_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY portfolio_id, symbol"
        return [_position_from_row(r) for r in self._conn.execute(q, args)]

    # ---- risk limits ----------------------------------------------------
    def load_limits(self) -> dict | None:
        cur = self._conn.execute("SELECT payload FROM risk_limits WHERE id=1")
        r = cur.fetchone()
        return json.loads(r["payload"]) if r else None

    def save_limits(self, payload: dict) -> None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
        with self._lock:
            self._conn.execute(
                """INSERT INTO risk_limits (id,payload,updated_at) VALUES (1,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (json.dumps(payload), now))
            self._conn.commit()

    def record_breach(self, *, symbol: str, side: str, qty: float, gate: str,
                      detail: str, observed: float | None,
                      limit_val: float | None, order_id: str | None = None) -> None:
        """Append-only: a rejected order still leaves a permanent record.

        Rejections are exactly what a reviewer asks to see -- 'show me a time
        the control fired' -- so they must outlive the request that caused them.
        """
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
        with self._lock:
            self._conn.execute(
                """INSERT INTO limit_breaches
                   (ts,symbol,side,qty,gate,detail,observed,limit_val,order_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, symbol, side, qty, gate, detail, observed, limit_val, order_id))
            self._conn.commit()

    def list_breaches(self, limit: int = 100) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM limit_breaches ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur]

    def realized_pnl_today(self) -> float:
        """Realised P&L booked since UTC midnight, for the daily loss gate."""
        import datetime
        start = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat(
            timespec="milliseconds")
        cur = self._conn.execute(
            "SELECT SUM(realized_pnl) AS p FROM positions WHERE updated_at >= ?",
            (start,))
        r = cur.fetchone()
        return float(r["p"] or 0.0)

    # ---- LLM telemetry --------------------------------------------------
    def record_llm(self, **kw) -> None:
        """Append one model invocation. Failures are recorded too -- an error
        rate you cannot see is an error rate you cannot manage."""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
        with self._lock:
            self._conn.execute(
                """INSERT INTO llm_invocations
                   (ts,model,endpoint,latency_ms,input_tokens,output_tokens,
                    cost_usd,ok,error,prompt_chars,reply_chars)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (now, kw.get("model", "?"), kw.get("endpoint", "?"),
                 kw.get("latency_ms", 0.0), kw.get("input_tokens", 0),
                 kw.get("output_tokens", 0), kw.get("cost_usd", 0.0),
                 1 if kw.get("ok", True) else 0, kw.get("error"),
                 kw.get("prompt_chars", 0), kw.get("reply_chars", 0)))
            self._conn.commit()

    def list_llm(self, limit: int = 200) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM llm_invocations ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur]

    # ---- SOR training data ----------------------------------------------
    def record_route_observations(self, rows: list[dict]) -> int:
        """Append routing outcomes. One transaction for the whole route.

        All-or-nothing per route: a half-written route would look like the
        router simply never touched the missing venues, which is a different
        and wrong fact about the decision.
        """
        if not rows:
            return 0
        cols = ("ts", "route_id", "symbol", "side", "venue", "data_source",
                "outcome_source", "model_version", "predicted_p", "size_ratio",
                "latency_ms", "spread_bps", "volatility", "requested_qty",
                "filled_qty", "fill_rate", "label_filled")
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.executemany(
                    f"INSERT INTO route_observations ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [tuple(r.get(c) for c in cols) for r in rows])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return len(rows)

    def list_route_observations(self, limit: int = 5000,
                                outcome_source: str | None = None) -> list[dict]:
        q = "SELECT * FROM route_observations"
        args: list = []
        if outcome_source:
            q += " WHERE outcome_source=?"
            args.append(outcome_source)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args)]

    def route_observation_stats(self) -> dict:
        """Counts by provenance — the numbers that decide whether a retrain is
        even worth running, and on what."""
        cur = self._conn.execute(
            """SELECT outcome_source, COUNT(*) AS n, AVG(label_filled) AS pos_rate,
                      AVG(fill_rate) AS mean_fill_rate
               FROM route_observations GROUP BY outcome_source""")
        by_src = {r["outcome_source"]: {
            "n": r["n"],
            "positive_rate": round(r["pos_rate"], 4) if r["pos_rate"] is not None else None,
            "mean_fill_rate": round(r["mean_fill_rate"], 4)
            if r["mean_fill_rate"] is not None else None} for r in cur}
        return {"total": sum(v["n"] for v in by_src.values()), "by_outcome_source": by_src}

    # ---- model registry --------------------------------------------------
    def save_model(self, *, params: dict, metrics: dict, sample_source: str,
                   n_train: int, n_test: int, kind: str = "sor_fill",
                   status: str = "challenger", notes: str | None = None) -> int:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO model_registry
                   (created_at,kind,status,sample_source,n_train,n_test,params,metrics,notes)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, kind, status, sample_source, n_train, n_test,
                 json.dumps(params), json.dumps(metrics), notes))
            self._conn.commit()
            return int(cur.lastrowid)

    def get_model(self, version: int) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM model_registry WHERE version=?", (version,))
        r = cur.fetchone()
        return dict(r) if r else None

    def champion_model(self, kind: str = "sor_fill") -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM model_registry WHERE kind=? AND status='champion' "
            "ORDER BY version DESC LIMIT 1", (kind,))
        r = cur.fetchone()
        return dict(r) if r else None

    def list_models(self, kind: str = "sor_fill", limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM model_registry WHERE kind=? ORDER BY version DESC LIMIT ?",
            (kind, limit))]

    def promote_model(self, version: int, kind: str = "sor_fill") -> None:
        """Make `version` champion and archive the incumbent, atomically.

        Two champions at once would mean routing decisions silently split
        between models, so the demotion and the promotion share a transaction.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    "UPDATE model_registry SET status='archived' "
                    "WHERE kind=? AND status='champion'", (kind,))
                self._conn.execute(
                    "UPDATE model_registry SET status='champion' WHERE version=?",
                    (version,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def reset(self) -> None:
        """Wipe all OMS state — demo convenience, never call in production.

        Deliberately leaves `model_registry` and `route_observations` alone:
        the demo reset clears the blotter, and throwing away accumulated
        training data plus model lineage as a side effect of that would be a
        nasty surprise.
        """
        with self._lock:
            self._conn.executescript(
                "DELETE FROM fills; DELETE FROM orders; DELETE FROM positions;"
                " DELETE FROM limit_breaches;")
            self._conn.commit()
