"""Durable state, on SQLite.

The question persistence answers here is not "where does the data go" but
**which invariants does a process restart violate?** Four of them do, and two
cause silent *wrong behaviour* rather than mere data loss:

* **The kill switch latches.** Its own docstring says a switch that re-arms
  itself "will let the same failure through repeatedly". A restart re-armed
  it. The safety property the class advertises was defeated by its storage.
* **A high-water mark only rises.** Losing it resets the vault to 1.0, so a
  fund down 20% would charge performance fees all the way back up - the exact
  double-charge the high-water mark exists to prevent. A money bug.
* **`client_order_id` is unique.** Forgetting it after a restart means a
  strategy that regenerates the same id double-sends.
* **Every order sent is tracked.** Forgetting one leaves an order live at the
  venue that nothing is watching, and `reconcile()` cannot report what it has
  no record of having sent.

Two decisions carry the weight:

**Write-ahead.** The blotter is written *before* the order goes to the venue,
not after. A crash between send and acknowledgement is the whole reason the
`UNKNOWN` state exists; if the write came second, that crash would leave no
record at all, which is strictly worse than UNKNOWN. Writing first costs one
`fsync` on the order path and turns an untraceable order into a recoverable
one.

**`synchronous=FULL`.** WAL with the usual `NORMAL` survives a process crash
but can lose the most recent commits to an OS or power failure. For an order
blotter the thing lost is the record of an order that exists at the venue, so
the durability is worth the fsync.

SQLite rather than JSON because a partially-written JSON file is corrupt,
whereas an interrupted SQLite transaction simply did not happen.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, Sequence

SCHEMA_VERSION = 4

# --- v2: algorithmic execution ----------------------------------------------
# Every monetary column is TEXT holding a Decimal's exact string form, never
# REAL. SQLite's REAL is an IEEE-754 double, and a working order sliced into
# eight children is eight opportunities for a representation error to become a
# position break. `_dec` below refuses floats outright so the rule is enforced
# at the boundary rather than trusted.
#
# Children and fills cascade from the parent: a parent order and the slices it
# produced are one fact, and a delete that left orphan fills would corrupt the
# TCA denominator rather than merely lose a row.
_EXEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS algo_parents (
    parent_id     TEXT PRIMARY KEY,
    created_ms    INTEGER NOT NULL,
    venue         TEXT NOT NULL,
    market        TEXT NOT NULL,
    side          TEXT NOT NULL,
    algo          TEXT NOT NULL,
    total_size    TEXT NOT NULL,
    slice_count   INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    arrival_price TEXT NOT NULL,
    status        TEXT NOT NULL,
    filled_size   TEXT NOT NULL DEFAULT '0',
    avg_price     TEXT,
    completed_ms  INTEGER,
    -- Who placed it. Nullable because runs predating wallet sign-in have no
    -- answer, and inventing one ("system") would be worse than admitting it:
    -- an audit trail that records *what* happened without *who* did it is a
    -- log, and a log that fabricates the actor is a misleading one.
    actor         TEXT,
    -- 'mainnet' | 'testnet' | 'simulated'. The single most consequential fact
    -- about a fill and the one a blotter most easily loses: real money and a
    -- rehearsal look identical once they are both a row with a price. Stored
    -- rather than inferred at read time, because the guard state that produced
    -- the fill is not recoverable afterwards.
    network       TEXT
);
CREATE INDEX IF NOT EXISTS idx_parents_market  ON algo_parents(market);
CREATE INDEX IF NOT EXISTS idx_parents_created ON algo_parents(created_ms DESC);

CREATE TABLE IF NOT EXISTS algo_children (
    parent_id      TEXT NOT NULL
                   REFERENCES algo_parents(parent_id) ON DELETE CASCADE,
    slice_index    INTEGER NOT NULL,
    scheduled_ms   INTEGER NOT NULL,
    scheduled_size TEXT NOT NULL,
    weight         TEXT NOT NULL,
    client_id      TEXT,
    status         TEXT NOT NULL,
    filled_size    TEXT NOT NULL DEFAULT '0',
    avg_price      TEXT,
    PRIMARY KEY (parent_id, slice_index)
);

CREATE TABLE IF NOT EXISTS algo_fills (
    fill_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   TEXT NOT NULL
                REFERENCES algo_parents(parent_id) ON DELETE CASCADE,
    slice_index INTEGER NOT NULL,
    ts_ms       INTEGER NOT NULL,
    size        TEXT NOT NULL,
    price       TEXT NOT NULL,
    fee         TEXT NOT NULL,
    liquidity   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_parent ON algo_fills(parent_id);

CREATE TABLE IF NOT EXISTS tca_results (
    parent_id                    TEXT PRIMARY KEY
                                 REFERENCES algo_parents(parent_id) ON DELETE CASCADE,
    computed_ms                  INTEGER NOT NULL,
    arrival_price                TEXT NOT NULL,
    avg_price                    TEXT NOT NULL,
    implementation_shortfall_bps TEXT,
    vwap_slippage_bps            TEXT,
    fee_bps                      TEXT,
    total_cost_bps               TEXT,
    participation_rate_pct       TEXT,
    benchmark_prints             INTEGER NOT NULL DEFAULT 0
);

-- On-chain observations. Kept because an oracle read is evidence about a
-- moment that cannot be re-fetched: `latestRoundData` at block N is gone once
-- the chain moves on, and a divergence alert nobody can reconstruct afterwards
-- is not an audit trail.
CREATE TABLE IF NOT EXISTS chain_observations (
    obs_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_ms INTEGER NOT NULL,
    chain       TEXT NOT NULL,
    block       INTEGER NOT NULL,
    market      TEXT NOT NULL,
    source      TEXT NOT NULL,
    price       TEXT NOT NULL,
    cex_price   TEXT,
    divergence_bps TEXT
);
CREATE INDEX IF NOT EXISTS idx_chain_obs ON chain_observations(market, observed_ms DESC);
"""

# --- v3: the actor on every write -------------------------------------------
# Additive and nullable, so an existing blotter migrates without rewriting a
# row. `ADD COLUMN` is the one DDL form SQLite cannot express idempotently -
# there is no `IF NOT EXISTS` for a column - so `_migrate` tolerates the
# duplicate-column error specifically rather than this script guarding itself.
_ACTOR_SCHEMA = """
ALTER TABLE algo_parents ADD COLUMN actor TEXT;
"""

# --- v4: which network the fill actually happened on -------------------------
# Backfillable without guessing: a venue whose `testnet_rest` is None has no
# non-mainnet code path, so any stored fill against it was mainnet by
# construction. See `tools/backfill_network.py`.
_NETWORK_SCHEMA = """
ALTER TABLE algo_parents ADD COLUMN network TEXT;
"""

#: Applied in ascending order for any file behind `SCHEMA_VERSION`. Every
#: statement is `IF NOT EXISTS` (or tolerated as a duplicate), so applying one
#: to a fresh file (which the base schema already provisioned) is a no-op
#: rather than an error.
_MIGRATIONS = {2: _EXEC_SCHEMA, 3: _ACTOR_SCHEMA, 4: _NETWORK_SCHEMA}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The blotter. `payload` is the serialised TrackedOrder; the promoted columns
-- exist so reconciliation can query without deserialising every row.
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    order_id        TEXT,
    market          TEXT NOT NULL,
    status          TEXT NOT NULL,
    venue           TEXT NOT NULL,
    created_ms      INTEGER NOT NULL,
    updated_ms      INTEGER NOT NULL,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_venue  ON orders(venue);

-- Replay protection, with expiry so the table does not grow without bound.
CREATE TABLE IF NOT EXISTS replay (
    kind      TEXT NOT NULL,   -- 'nonce' | 'coid'
    value     TEXT NOT NULL,
    expiry_ms INTEGER NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE INDEX IF NOT EXISTS idx_replay_expiry ON replay(expiry_ms);

-- Small singletons: kill switch, daily PnL, vault scalars.
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Vault share ledger. Relational rather than a JSON blob because losing track
-- of who owns what is the one failure with no recovery path.
CREATE TABLE IF NOT EXISTS vault_holders (
    vault     TEXT NOT NULL,
    address   TEXT NOT NULL,
    shares    TEXT NOT NULL,
    deposited TEXT NOT NULL,
    withdrawn TEXT NOT NULL,
    PRIMARY KEY (vault, address)
);
""" + _EXEC_SCHEMA


class StoreError(Exception):
    pass


class Store:
    """Durable state for the OMS, replay guard, risk engine and vault.

    Thread-safe via a single lock around a single connection. A connection pool
    would scale further, but the write volume here is one row per order and the
    lock makes the write-ahead ordering trivially correct.
    """

    def __init__(self, path: Optional[Path] = None, durable: bool = True):
        self.path = Path(path) if path else Path.home() / ".cryptodesk" / "state.db"
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # FULL over NORMAL: see the module docstring. `durable=False` is for
            # tests, where the fsync cost dominates and nothing is at stake.
            self._conn.execute(
                "PRAGMA synchronous=%s" % ("FULL" if durable else "OFF"))
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._check_version()

    def _check_version(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            return
        found = int(row["value"])
        if found == SCHEMA_VERSION:
            return
        if found > SCHEMA_VERSION:
            # Refuse rather than guess. Silently reading a future schema is how
            # a blotter ends up half-interpreted.
            raise StoreError(
                "state file %s is schema v%d, this build expects v%d - migrate "
                "or point at a different file" % (self.path, found, SCHEMA_VERSION)
            )
        # Behind: walk the ladder. Refusing here instead would have made every
        # existing blotter unreadable the moment a table was added, which is a
        # worse failure than the one the version check exists to prevent.
        for version in range(found + 1, SCHEMA_VERSION + 1):
            script = _MIGRATIONS.get(version)
            if script is None:
                raise StoreError(
                    "no migration to schema v%d for %s" % (version, self.path))
            try:
                self._conn.executescript(script)
            except sqlite3.OperationalError as exc:
                # `ALTER TABLE ... ADD COLUMN` cannot say IF NOT EXISTS, so a
                # column the base schema already provisioned raises here. That
                # specific case is the migration being a no-op, which is what
                # the ladder wants; anything else is a real failure.
                if "duplicate column name" not in str(exc).lower():
                    raise
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- orders ------------------------------------------------------------

    def save_order(self, record: Dict[str, Any]) -> None:
        """Upsert one order. Called *before* the send and again on every ack."""
        required = ("client_order_id", "market", "status", "venue",
                    "created_ms", "updated_ms")
        for k in required:
            if k not in record:
                raise StoreError("order record missing %r" % k)
        with self._lock:
            self._conn.execute(
                """INSERT INTO orders
                       (client_order_id, order_id, market, status, venue,
                        created_ms, updated_ms, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(client_order_id) DO UPDATE SET
                       order_id=excluded.order_id,
                       status=excluded.status,
                       updated_ms=excluded.updated_ms,
                       payload=excluded.payload""",
                (record["client_order_id"], record.get("order_id"),
                 record["market"], record["status"], record["venue"],
                 int(record["created_ms"]), int(record["updated_ms"]),
                 json.dumps(record, default=str)),
            )

    def load_orders(self, venue: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if venue:
                rows = self._conn.execute(
                    "SELECT payload FROM orders WHERE venue=? ORDER BY created_ms",
                    (venue,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT payload FROM orders ORDER BY created_ms").fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                rec = json.loads(r["payload"])
            except ValueError:
                continue  # one corrupt row must not lose the blotter
            out[rec["client_order_id"]] = rec
        return out

    def delete_order(self, client_order_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM orders WHERE client_order_id=?",
                               (client_order_id,))

    def order_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]

    # -- replay ------------------------------------------------------------

    def save_replay(self, nonce: Optional[int], client_order_id: Optional[str],
                    expiry_ms: int) -> None:
        with self._lock:
            if nonce is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO replay(kind, value, expiry_ms) "
                    "VALUES('nonce', ?, ?)", (str(nonce), int(expiry_ms)))
            if client_order_id:
                self._conn.execute(
                    "INSERT OR REPLACE INTO replay(kind, value, expiry_ms) "
                    "VALUES('coid', ?, ?)", (client_order_id, int(expiry_ms)))

    def load_replay(self, now_ms: int) -> Tuple[Set[int], Set[str]]:
        """Live nonces and client order ids, pruning anything expired.

        Pruning on read rather than on a timer keeps the table bounded without
        a background thread, and a restart is exactly when the prune is most
        useful - everything written before the last outage is likely stale.
        """
        with self._lock:
            self._conn.execute("DELETE FROM replay WHERE expiry_ms <= ?",
                               (int(now_ms),))
            rows = self._conn.execute("SELECT kind, value FROM replay").fetchall()
        nonces: Set[int] = set()
        coids: Set[str] = set()
        for r in rows:
            if r["kind"] == "nonce":
                try:
                    nonces.add(int(r["value"]))
                except ValueError:
                    continue
            else:
                coids.add(r["value"])
        return nonces, coids

    # -- key/value ---------------------------------------------------------

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?",
                                     (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))

    # -- vault ledger ------------------------------------------------------

    def save_holder(self, vault: str, address: str, shares: Decimal,
                    deposited: Decimal, withdrawn: Decimal) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO vault_holders(vault, address, shares,
                                             deposited, withdrawn)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(vault, address) DO UPDATE SET
                       shares=excluded.shares,
                       deposited=excluded.deposited,
                       withdrawn=excluded.withdrawn""",
                (vault, address, str(shares), str(deposited), str(withdrawn)))

    def load_holders(self, vault: str) -> Dict[str, Dict[str, Decimal]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, shares, deposited, withdrawn "
                "FROM vault_holders WHERE vault=?", (vault,)).fetchall()
        return {
            r["address"]: {
                "shares": Decimal(r["shares"]),
                "deposited": Decimal(r["deposited"]),
                "withdrawn": Decimal(r["withdrawn"]),
            }
            for r in rows
        }

    # -- introspection -----------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            def count(table: str) -> int:
                return self._conn.execute(
                    "SELECT COUNT(*) c FROM %s" % table).fetchone()["c"]
            return {
                "path": str(self.path),
                "schema_version": SCHEMA_VERSION,
                "orders": count("orders"),
                "replay_entries": count("replay"),
                "kv_entries": count("kv"),
                "vault_holders": count("vault_holders"),
                "algo_parents": count("algo_parents"),
                "algo_fills": count("algo_fills"),
                "chain_observations": count("chain_observations"),
            }

    def __repr__(self) -> str:
        return "<Store %s>" % self.path

    # -- algorithmic execution ---------------------------------------------

    @staticmethod
    def _dec(value: Any) -> str:
        """Exact text for a Decimal. Floats are refused, not coerced.

        A float here would be a money bug that no test catches until the
        position breaks: `Decimal(0.1)` is 0.1000000000000000055511151231...,
        and once that is written it is indistinguishable from a real quantity.
        Accepting `int` is safe - it is exact - and `str` passes through so
        callers that already serialised are not forced to round-trip.
        """
        if isinstance(value, float):
            raise StoreError(
                "refusing to store float %r as money; pass Decimal or str" % value)
        if value is None:
            return "0"
        return str(value)

    def save_algo_run(self, parent: Dict[str, Any],
                      children: Sequence[Dict[str, Any]],
                      fills: Sequence[Dict[str, Any]],
                      tca: Optional[Dict[str, Any]] = None) -> None:
        """Persist one parent order, its children, its fills and its TCA.

        **One transaction.** A parent that exists without the slices it was cut
        into is not a partial record, it is a wrong one: reconciliation would
        read a 3 BTC parent with two of six children and report four missing
        orders at the venue. `BEGIN IMMEDIATE` takes the write lock up front so
        a concurrent run blocks rather than interleaving.
        """
        d = self._dec
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO algo_parents
                           (parent_id, created_ms, venue, market, side, algo,
                            total_size, slice_count, duration_ms, arrival_price,
                            status, filled_size, avg_price, completed_ms,
                            actor, network)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(parent_id) DO UPDATE SET
                           status=excluded.status,
                           filled_size=excluded.filled_size,
                           avg_price=excluded.avg_price,
                           completed_ms=excluded.completed_ms""",
                    (parent["parent_id"], int(parent["created_ms"]),
                     parent["venue"], parent["market"], parent["side"],
                     parent["algo"], d(parent["total_size"]),
                     int(parent["slice_count"]), int(parent["duration_ms"]),
                     d(parent["arrival_price"]), parent["status"],
                     d(parent.get("filled_size")),
                     None if parent.get("avg_price") is None else d(parent["avg_price"]),
                     parent.get("completed_ms"),
                     # Deliberately not in the ON CONFLICT update list: the
                     # actor is who *placed* the order, and re-running a parent
                     # to record its fills must not reattribute it. Network is
                     # excluded for the same reason — a fill cannot change the
                     # network it happened on.
                     parent.get("actor"), parent.get("network")))

                # Replace rather than accumulate: re-running a parent must not
                # leave the previous attempt's children behind.
                self._conn.execute(
                    "DELETE FROM algo_children WHERE parent_id=?", (parent["parent_id"],))
                self._conn.execute(
                    "DELETE FROM algo_fills WHERE parent_id=?", (parent["parent_id"],))

                self._conn.executemany(
                    """INSERT INTO algo_children
                           (parent_id, slice_index, scheduled_ms, scheduled_size,
                            weight, client_id, status, filled_size, avg_price)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [(parent["parent_id"], int(c["slice_index"]),
                      int(c["scheduled_ms"]), d(c["scheduled_size"]),
                      d(c["weight"]), c.get("client_id"), c["status"],
                      d(c.get("filled_size")),
                      None if c.get("avg_price") is None else d(c["avg_price"]))
                     for c in children])

                self._conn.executemany(
                    """INSERT INTO algo_fills
                           (parent_id, slice_index, ts_ms, size, price, fee, liquidity)
                       VALUES (?,?,?,?,?,?,?)""",
                    [(parent["parent_id"], int(f["slice_index"]), int(f["ts_ms"]),
                      d(f["size"]), d(f["price"]), d(f.get("fee")),
                      f.get("liquidity") or "unknown") for f in fills])

                if tca is not None:
                    self._conn.execute(
                        """INSERT INTO tca_results
                               (parent_id, computed_ms, arrival_price, avg_price,
                                implementation_shortfall_bps, vwap_slippage_bps,
                                fee_bps, total_cost_bps, participation_rate_pct,
                                benchmark_prints)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(parent_id) DO UPDATE SET
                               computed_ms=excluded.computed_ms,
                               implementation_shortfall_bps=excluded.implementation_shortfall_bps,
                               vwap_slippage_bps=excluded.vwap_slippage_bps,
                               fee_bps=excluded.fee_bps,
                               total_cost_bps=excluded.total_cost_bps,
                               participation_rate_pct=excluded.participation_rate_pct,
                               benchmark_prints=excluded.benchmark_prints""",
                        (parent["parent_id"], int(tca["computed_ms"]),
                         d(tca["arrival_price"]), d(tca["avg_price"]),
                         _opt(d, tca.get("implementation_shortfall_bps")),
                         _opt(d, tca.get("vwap_slippage_bps")),
                         _opt(d, tca.get("fee_bps")),
                         _opt(d, tca.get("total_cost_bps")),
                         _opt(d, tca.get("participation_rate_pct")),
                         int(tca.get("benchmark_prints") or 0)))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_algo_parents(self, limit: int = 50,
                          market: Optional[str] = None) -> list:
        """Most recent parents first, each with its TCA joined on."""
        sql = ("""SELECT p.*, t.implementation_shortfall_bps, t.vwap_slippage_bps,
                         t.fee_bps, t.total_cost_bps, t.participation_rate_pct,
                         t.benchmark_prints,
                         (SELECT COUNT(*) FROM algo_fills f
                           WHERE f.parent_id = p.parent_id) AS fill_count
                    FROM algo_parents p
                    LEFT JOIN tca_results t ON t.parent_id = p.parent_id""")
        args: list = []
        if market:
            sql += " WHERE p.market = ?"
            args.append(market)
        sql += " ORDER BY p.created_ms DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def get_algo_run(self, parent_id: str) -> Optional[Dict[str, Any]]:
        """One parent with its children and fills, or None."""
        with self._lock:
            p = self._conn.execute(
                "SELECT * FROM algo_parents WHERE parent_id=?", (parent_id,)).fetchone()
            if p is None:
                return None
            children = self._conn.execute(
                "SELECT * FROM algo_children WHERE parent_id=? ORDER BY slice_index",
                (parent_id,)).fetchall()
            fills = self._conn.execute(
                "SELECT * FROM algo_fills WHERE parent_id=? ORDER BY fill_id",
                (parent_id,)).fetchall()
            tca = self._conn.execute(
                "SELECT * FROM tca_results WHERE parent_id=?", (parent_id,)).fetchone()
        return {"parent": dict(p), "children": [dict(c) for c in children],
                "fills": [dict(f) for f in fills],
                "tca": dict(tca) if tca else None}

    def algo_stats(self) -> Dict[str, Any]:
        """Aggregates across stored runs — the thing a single run cannot show.

        Execution quality is a distribution, not a number. One TWAP beating
        arrival by 0.4 bps is noise; ninety of them with a median of 0.4 bps is
        a result, and that claim is only available once runs are kept.
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS runs,
                          COALESCE(SUM(CAST(filled_size AS REAL)), 0) AS total_filled
                     FROM algo_parents""").fetchone()
            by_algo = self._conn.execute(
                """SELECT p.algo,
                          COUNT(*) AS runs,
                          AVG(CAST(t.implementation_shortfall_bps AS REAL)) AS avg_is_bps,
                          AVG(CAST(t.total_cost_bps AS REAL)) AS avg_cost_bps
                     FROM algo_parents p
                     JOIN tca_results t ON t.parent_id = p.parent_id
                    WHERE t.implementation_shortfall_bps IS NOT NULL
                    GROUP BY p.algo""").fetchall()
            fills = self._conn.execute(
                "SELECT COUNT(*) AS n FROM algo_fills").fetchone()
        return {
            "runs": row["runs"], "fills": fills["n"],
            "total_filled": round(row["total_filled"], 8),
            # CAST to REAL only for the AVERAGE. The stored values stay exact;
            # a mean of a bps figure is a statistic, not a ledger entry.
            "by_algo": [{"algo": r["algo"], "runs": r["runs"],
                         "avg_shortfall_bps": (round(r["avg_is_bps"], 3)
                                               if r["avg_is_bps"] is not None else None),
                         "avg_total_cost_bps": (round(r["avg_cost_bps"], 3)
                                                if r["avg_cost_bps"] is not None else None)}
                        for r in by_algo],
        }

    def prune_algo_runs(self, keep: int = 500) -> int:
        """Keep only the most recent `keep` parents. Returns rows deleted.

        This table is written by a publicly reachable endpoint, so it grows at
        whatever rate strangers call it. Unbounded growth on a shared volume is
        a real outage, not a tidiness concern - and the oldest runs are the
        least interesting, since the aggregate statistics people actually read
        are dominated by the recent ones. Children and fills follow via
        `ON DELETE CASCADE`.
        """
        with self._lock:
            cur = self._conn.execute(
                """DELETE FROM algo_parents WHERE parent_id IN (
                       SELECT parent_id FROM algo_parents
                       ORDER BY created_ms DESC LIMIT -1 OFFSET ?)""", (int(keep),))
            return cur.rowcount or 0

    def prune_chain_observations(self, keep: int = 2000) -> int:
        with self._lock:
            cur = self._conn.execute(
                """DELETE FROM chain_observations WHERE obs_id IN (
                       SELECT obs_id FROM chain_observations
                       ORDER BY observed_ms DESC LIMIT -1 OFFSET ?)""", (int(keep),))
            return cur.rowcount or 0

    # -- on-chain observations ---------------------------------------------

    def record_chain_observation(self, obs: Dict[str, Any]) -> int:
        """Append one oracle read. Returns its row id."""
        d = self._dec
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO chain_observations
                       (observed_ms, chain, block, market, source, price,
                        cex_price, divergence_bps)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (int(obs["observed_ms"]), obs["chain"], int(obs["block"]),
                 obs["market"], obs["source"], d(obs["price"]),
                 _opt(d, obs.get("cex_price")), _opt(d, obs.get("divergence_bps"))))
            return int(cur.lastrowid)

    def list_chain_observations(self, limit: int = 100,
                                market: Optional[str] = None) -> list:
        sql = "SELECT * FROM chain_observations"
        args: list = []
        if market:
            sql += " WHERE market = ?"
            args.append(market)
        sql += " ORDER BY observed_ms DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]


def memory_store() -> Store:
    """An in-memory store for tests. Durability is meaningless here by design."""
    return Store(Path(":memory:"), durable=False)

def _opt(fn, value):
    """Apply `fn`, but keep None as None — a missing benchmark is not zero."""
    return None if value is None else fn(value)
