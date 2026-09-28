"""Durable end-of-day store.

Every market-data screen used to be a live call to somebody else's free public
endpoint, behind a 5-minute in-process cache. That has three problems, and all
three are why the console felt slow:

  * A process restart threw the whole cache away. The Treasury curve costs
    ~19s cold, so the first visitor after any deploy paid it -- and so did
    every screen that needed a curve.
  * A 5-minute TTL means the data expires 288 times a day for information
    that changes ONCE a day. Most of those refetches re-downloaded a file
    whose content had not moved.
  * An outage upstream became an outage here. The cache could only serve what
    it had recently fetched.

EOD data is the easy case for persistence: it is append-only, keyed by date,
and immutable once published (bar revisions, which are handled below). So it
belongs in the database, not in a dictionary that dies with the process.

**The read path never touches the network.** A screen reads rows from SQLite.
A background job refreshes from upstream and upserts. If the network is down,
if FRED rate-limits us, or if the container restarted ten seconds ago, screens
still render -- against data whose own `as_of` date is carried through, so
staleness stays visible rather than assumed away.

Design notes:

  UPSERT, NOT INSERT-IGNORE. Economic series get revised: FRED restates recent
  observations, and the Treasury republishes a day's curve when a tenor was
  missing. Ignoring conflicts would freeze the first value ever seen and
  silently diverge from the source. `ON CONFLICT DO UPDATE` keeps the latest
  published value for a date.

  (series_id, date) IS THE NATURAL KEY, so these are WITHOUT ROWID tables --
  the row IS the index, which for a keyed-lookup table this shape is both
  smaller and faster than carrying a separate rowid.

  THE FETCH IS FULL, THE WRITE IS INCREMENTAL. FRED's CSV endpoint takes no
  date range, so a refresh re-downloads the whole series; only genuinely new
  or revised rows are written, and `changed` reports how many. The Treasury
  file IS per-year, so the refresher pulls the current year and only reaches
  for prior years when backfilling.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# Same volume as the OMS database (docker-compose mounts `oms-data` at
# /app/data), so the store survives restarts and redeploys.
DEFAULT_DB = os.getenv("EOD_DB_PATH",
                       os.path.join(os.path.dirname(
                           os.getenv("OMS_DB_PATH", "/app/data/oms.db")), "eod.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eod_series (
    series_id TEXT NOT NULL,
    date      TEXT NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (series_id, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS eod_curve (
    date      TEXT NOT NULL,
    tenor     TEXT NOT NULL,
    years     REAL NOT NULL,
    yield_pct REAL NOT NULL,
    PRIMARY KEY (date, tenor)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS eod_fx (
    date TEXT NOT NULL,
    ccy  TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (date, ccy)
) WITHOUT ROWID;

-- One row per upstream source: when it was last refreshed and how it went.
-- This is what makes staleness answerable ("the curve is from Tuesday and we
-- last reached the Treasury an hour ago") instead of guessable.
CREATE TABLE IF NOT EXISTS eod_meta (
    source       TEXT PRIMARY KEY,
    last_date    TEXT,
    refreshed_at TEXT,
    rows         INTEGER NOT NULL DEFAULT 0,
    ok           INTEGER NOT NULL DEFAULT 1,
    note         TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EODStore:
    """SQLite-backed daily bars. One connection PER THREAD, one write lock, WAL.

    The connection is thread-local, and that is not a detail. FastAPI runs sync
    handlers in a threadpool, so several requests read this store at the same
    time while the background refresher writes to it. A single shared
    `sqlite3.Connection` is not safe for that even with
    check_same_thread=False -- concurrent statements on one connection trample
    each other's cursor state. Sharing one connection produced intermittent
    500s on exactly the endpoints that read the store: 19 failures in 60
    requests against production, while the one quant endpoint that touches no
    database (probability) never failed once.

    WAL plus a per-thread connection is the combination that actually works:
    readers never block each other or the writer, and `busy_timeout` makes a
    reader wait out a write rather than raising "database is locked". Writes
    still serialise on `self._lock` so the refresher cannot interleave two
    upserts.
    """

    def __init__(self, path: str = DEFAULT_DB):
        # An in-memory database is private to its connection, so a thread-local
        # pool of them would hand every thread a different empty database. The
        # shared-cache URI makes them all the SAME in-memory database, which is
        # what a caller asking for ":memory:" means.
        self.path = ("file:eod_shared?mode=memory&cache=shared"
                     if path == ":memory:" else path)
        self._uri = path == ":memory:"
        if not self._uri:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        # Held only for ":memory:", where the database lives exactly as long as
        # at least one connection to it does.
        self._keepalive = self._connect() if self._uri else None
        conn = self._conn
        conn.executescript(_SCHEMA)
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False,
                               timeout=30, uri=self._uri)
        conn.row_factory = sqlite3.Row
        if not self._uri:
            conn.execute("PRAGMA journal_mode=WAL")
        # Wait out a concurrent writer instead of raising "database is locked".
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self._connect()
        return c

    # ---- writes (refresher only) ----------------------------------------
    def upsert_series(self, series_id: str, points: list[dict]) -> int:
        """Insert or revise daily observations. Returns rows actually changed."""
        if not points:
            return 0
        rows = [(series_id, p["date"], float(p["value"])) for p in points
                if p.get("date") and p.get("value") is not None]
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT INTO eod_series (series_id, date, value) VALUES (?,?,?)
                   ON CONFLICT(series_id, date) DO UPDATE SET value=excluded.value
                   WHERE value IS NOT excluded.value""", rows)
            changed = self._conn.total_changes - before
            last = max((r[1] for r in rows), default=None)
            self._meta(f"fred:{series_id}", last, len(rows), True, None)
            self._conn.commit()
        return changed

    def upsert_curve(self, dated_points: list[tuple[str, list[dict]]]) -> int:
        """`dated_points` is [(date, [{tenor, years, yield_pct}, ...]), ...]."""
        rows = [(d, p["tenor"], float(p["years"]), float(p["yield_pct"]))
                for d, pts in dated_points for p in pts
                if p.get("tenor") and p.get("yield_pct") is not None]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT INTO eod_curve (date, tenor, years, yield_pct) VALUES (?,?,?,?)
                   ON CONFLICT(date, tenor) DO UPDATE SET
                       yield_pct=excluded.yield_pct, years=excluded.years
                   WHERE yield_pct IS NOT excluded.yield_pct""", rows)
            changed = self._conn.total_changes - before
            self._meta("treasury", max(r[0] for r in rows), len(rows), True, None)
            self._conn.commit()
        return changed

    def upsert_fx(self, date: str, rates: dict[str, float]) -> int:
        if not date or not rates:
            return 0
        rows = [(date, c, float(v)) for c, v in rates.items()]
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT INTO eod_fx (date, ccy, rate) VALUES (?,?,?)
                   ON CONFLICT(date, ccy) DO UPDATE SET rate=excluded.rate
                   WHERE rate IS NOT excluded.rate""", rows)
            changed = self._conn.total_changes - before
            self._meta("ecb", date, len(rows), True, None)
            self._conn.commit()
        return changed

    def mark_failed(self, source: str, note: str) -> None:
        """Record that a refresh failed WITHOUT touching last_date.

        The data already stored is still valid and still serves reads; what
        changed is our confidence that it is current, and that is what the
        Market Data screen should be able to show.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT last_date, rows FROM eod_meta WHERE source=?", (source,)).fetchone()
            self._meta(source, cur["last_date"] if cur else None,
                       cur["rows"] if cur else 0, False, note[:200])
            self._conn.commit()

    def _meta(self, source, last_date, rows, ok, note) -> None:
        """Caller holds the lock and commits."""
        self._conn.execute(
            """INSERT INTO eod_meta (source, last_date, refreshed_at, rows, ok, note)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                   last_date=COALESCE(excluded.last_date, eod_meta.last_date),
                   refreshed_at=excluded.refreshed_at, rows=excluded.rows,
                   ok=excluded.ok, note=excluded.note""",
            (source, last_date, _now(), rows, 1 if ok else 0, note))

    # ---- reads (request path) -------------------------------------------
    def series(self, series_id: str, limit: int = 260) -> list[dict]:
        """The most recent `limit` observations, oldest first.

        ORDER BY date DESC + LIMIT then reversed, so SQLite walks the tail of
        the index instead of scanning a whole series to discard the front.
        """
        cur = self._conn.execute(
            "SELECT date, value FROM eod_series WHERE series_id=? ORDER BY date DESC LIMIT ?",
            (series_id, int(limit)))
        return [{"date": r["date"], "value": r["value"]} for r in cur.fetchall()][::-1]

    def has_series(self, series_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM eod_series WHERE series_id=? LIMIT 1", (series_id,)).fetchone() is not None

    def curve_latest(self) -> tuple[str | None, list[dict]]:
        row = self._conn.execute("SELECT MAX(date) d FROM eod_curve").fetchone()
        d = row["d"] if row else None
        if not d:
            return None, []
        cur = self._conn.execute(
            "SELECT tenor, years, yield_pct FROM eod_curve WHERE date=? ORDER BY years", (d,))
        return d, [{"tenor": r["tenor"], "years": r["years"],
                    "yield_pct": r["yield_pct"]} for r in cur.fetchall()]

    def curve_tenor_history(self, tenor: str = "10 Yr", days: int = 60) -> list[dict]:
        cur = self._conn.execute(
            """SELECT date, yield_pct FROM eod_curve WHERE tenor=?
               ORDER BY date DESC LIMIT ?""", (tenor, int(days)))
        return [{"date": r["date"], "y10": r["yield_pct"]} for r in cur.fetchall()][::-1]

    def curve_matrix(self, days: int = 250) -> tuple[list[str], list[str], list[list[float]]]:
        """(tenors, dates, matrix) with any incomplete day dropped.

        Same contract as the live path: a day missing a tenor is dropped, not
        imputed -- interpolating a gap would manufacture a curve shape that was
        never quoted, and the PCA would then fit that artefact.
        """
        dates = [r["date"] for r in self._conn.execute(
            "SELECT DISTINCT date FROM eod_curve ORDER BY date DESC LIMIT ?", (int(days),))][::-1]
        if not dates:
            return [], [], []
        qs = ",".join("?" * len(dates))
        rows = self._conn.execute(
            f"""SELECT date, tenor, years, yield_pct FROM eod_curve
                WHERE date IN ({qs}) ORDER BY years""", dates).fetchall()
        by_date: dict[str, dict[str, float]] = {}
        order: dict[str, float] = {}
        for r in rows:
            by_date.setdefault(r["date"], {})[r["tenor"]] = r["yield_pct"]
            order[r["tenor"]] = r["years"]
        tenors = sorted(order, key=lambda t: order[t])
        out_d, matrix = [], []
        for d in dates:
            vals = by_date.get(d, {})
            if len(vals) == len(tenors):
                out_d.append(d)
                matrix.append([vals[t] for t in tenors])
        return tenors, out_d, matrix

    def fx_latest(self) -> tuple[str | None, dict[str, float]]:
        row = self._conn.execute("SELECT MAX(date) d FROM eod_fx").fetchone()
        d = row["d"] if row else None
        if not d:
            return None, {}
        cur = self._conn.execute("SELECT ccy, rate FROM eod_fx WHERE date=?", (d,))
        return d, {r["ccy"]: r["rate"] for r in cur.fetchall()}

    def meta(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT source, last_date, refreshed_at, rows, ok, note FROM eod_meta ORDER BY source")
        return [dict(r) for r in cur.fetchall()]

    def coverage(self) -> dict:
        """Row counts per table — what the store actually holds."""
        q = lambda s: self._conn.execute(s).fetchone()[0]      # noqa: E731
        return {
            "series_rows": q("SELECT COUNT(*) FROM eod_series"),
            "series_count": q("SELECT COUNT(DISTINCT series_id) FROM eod_series"),
            "curve_days": q("SELECT COUNT(DISTINCT date) FROM eod_curve"),
            "fx_days": q("SELECT COUNT(DISTINCT date) FROM eod_fx"),
        }


_STORE: EODStore | None = None
_STORE_LOCK = threading.Lock()


def eod() -> EODStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = EODStore()
    return _STORE
