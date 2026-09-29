"""Bronze / silver / gold on Delta Lake.

RUNS LOCALLY, FOR REAL. This uses delta-rs (`deltalake`), a Rust implementation of
the Delta protocol with Python bindings — no Spark, no JVM, no Databricks
workspace. The tables it writes are ordinary Delta tables: the same `_delta_log` a
Databricks cluster reads. So the pipeline below is executed and tested here rather
than written against an environment nobody has.

WHAT DATABRICKS WOULD ADD, stated so the gap is not mistaken for parity: a
cluster, Unity Catalog governance, and Spark's distributed execution for volumes
one machine cannot hold. None of those change the table format or the pipeline
shape, which is why this is a real first step rather than a mock of one.

THE THREE LAYERS, and why each exists:

  BRONZE — raw, append-only, exactly as received, including records that will
  later fail validation. This is the layer people are tempted to skip, and
  skipping it is the mistake: when a normalisation bug is found in silver, bronze
  is the only thing that allows reprocessing instead of re-requesting from a
  vendor who may no longer have it. Nothing is dropped and nothing is corrected.

  SILVER — validated, typed, deduplicated. Bad records are not deleted; they are
  written aside WITH THE REASON, so "how much did we reject and why" is a query
  rather than an archaeology exercise.

  GOLD — aggregated, analytics-ready. Rebuilt from silver rather than updated in
  place, because an aggregate that has drifted from its source is worse than one
  that is a few minutes stale.

IDEMPOTENCY. Every stage can re-run over the same input without duplicating rows:
bronze skips a batch_id it has already ingested, silver dedupes on the business
key and is a pure function of bronze. A pipeline that must not be run twice is a
pipeline that will be run twice at 3am by someone who does not know that.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_UNAVAILABLE_REASON = ""
try:
    import pyarrow as pa
    import pyarrow.compute as pc
    from deltalake import DeltaTable, write_deltalake
    HAVE_DELTA = True
except ImportError as e:            # pragma: no cover - environment dependent
    HAVE_DELTA = False
    _UNAVAILABLE_REASON = str(e)


def availability() -> dict:
    return {
        "available": HAVE_DELTA,
        "reason": _UNAVAILABLE_REASON or None,
        "engine": "delta-rs (no Spark/JVM/Databricks required)" if HAVE_DELTA else None,
    }


def default_root() -> Path:
    from ..paths import data_dir
    return Path(os.getenv("QGL_LAKE_ROOT", str(data_dir() / "lake")))


@dataclass
class StageResult:
    layer: str
    table: str
    rows_in: int
    rows_written: int
    rows_rejected: int
    version: int
    skipped: bool = False
    note: str = ""


class Medallion:
    def __init__(self, root: Path | str | None = None) -> None:
        if not HAVE_DELTA:
            raise RuntimeError(
                f"deltalake is not installed ({_UNAVAILABLE_REASON}). "
                "pip install deltalake — no Spark or Databricks required."
            )
        self.root = Path(root) if root else default_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, layer: str, table: str) -> str:
        return str(self.root / layer / table)

    def _version(self, layer: str, table: str) -> int:
        try:
            return DeltaTable(self.path(layer, table)).version()
        except Exception:
            return -1

    def _batch_already_ingested(self, table: str, batch_id: str) -> bool:
        try:
            dt = DeltaTable(self.path("bronze", table))
            tbl = dt.to_pyarrow_table(columns=["_batch_id"])
            if tbl.num_rows == 0:
                return False
            return pc.any(pc.equal(tbl.column("_batch_id"), batch_id)).as_py() is True
        except Exception:
            return False

    # ------------------------------------------------------------- bronze
    def ingest_bronze(self, table: str, records: Iterable[dict], *,
                      source: str, batch_id: str) -> StageResult:
        """Append raw records verbatim. Nothing dropped, nothing corrected.

        Re-running with the same batch_id is a no-op.
        """
        rows = list(records)
        if self._batch_already_ingested(table, batch_id):
            return StageResult("bronze", table, len(rows), 0, 0,
                               self._version("bronze", table), skipped=True,
                               note=f"batch_id '{batch_id}' already ingested")

        ingested_at = datetime.now(timezone.utc).isoformat()
        payload = [{
            "_source": source,
            "_batch_id": batch_id,
            "_ingested_at": ingested_at,
            # The raw record as JSON. Storing it whole means a vendor schema
            # change does not break ingestion: bronze accepts what arrives and
            # silver decides what it means.
            "_raw": json.dumps(r, default=str, separators=(",", ":")),
        } for r in rows]

        if not payload:
            return StageResult("bronze", table, 0, 0, 0,
                               self._version("bronze", table), note="empty batch")

        write_deltalake(self.path("bronze", table), pa.Table.from_pylist(payload),
                        mode="append")
        return StageResult("bronze", table, len(rows), len(rows), 0,
                           self._version("bronze", table))

    # ------------------------------------------------------------- silver
    def build_silver(self, table: str, *, key_fields: list[str],
                     required: list[str],
                     numeric: list[str] | None = None) -> StageResult:
        """Validate, type and deduplicate bronze into silver.

        Rejects are KEPT with a reason rather than dropped, so the rejection rate
        is a query. Overwrite rather than append: silver is a deterministic
        function of bronze, so rebuilding is always correct and appending would
        duplicate on every run.
        """
        numeric = numeric or []
        try:
            bronze = DeltaTable(self.path("bronze", table)).to_pyarrow_table()
        except Exception as e:
            raise RuntimeError(f"No bronze table for '{table}': {e}") from e

        raw_rows = bronze.column("_raw").to_pylist()
        meta = list(zip(bronze.column("_source").to_pylist(),
                        bronze.column("_batch_id").to_pylist(),
                        bronze.column("_ingested_at").to_pylist()))

        good: list[dict] = []
        bad: list[dict] = []
        seen: set[tuple] = set()

        for raw, (src, batch, at) in zip(raw_rows, meta):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                bad.append({"_raw": raw, "_reason": f"unparseable: {e}", "_source": src})
                continue

            missing = [f for f in required if rec.get(f) in (None, "")]
            if missing:
                bad.append({"_raw": raw, "_reason": f"missing {missing}", "_source": src})
                continue

            try:
                for f in numeric:
                    if rec.get(f) is not None:
                        rec[f] = float(rec[f])
            except (TypeError, ValueError) as e:
                bad.append({"_raw": raw, "_reason": f"non-numeric: {e}", "_source": src})
                continue

            k = tuple(rec.get(f) for f in key_fields)
            if k in seen:
                # Deduplication is silver's job: bronze accepts a redelivered
                # batch verbatim, and the duplicate is resolved here.
                bad.append({"_raw": raw, "_reason": "duplicate key", "_source": src})
                continue
            seen.add(k)

            rec["_source"] = src
            rec["_batch_id"] = batch
            rec["_ingested_at"] = at
            good.append(rec)

        if good:
            write_deltalake(self.path("silver", table), pa.Table.from_pylist(good),
                            mode="overwrite", schema_mode="overwrite")
        if bad:
            write_deltalake(self.path("silver", f"{table}_rejected"),
                            pa.Table.from_pylist(bad),
                            mode="overwrite", schema_mode="overwrite")

        return StageResult("silver", table, len(raw_rows), len(good), len(bad),
                           self._version("silver", table))

    # --------------------------------------------------------------- gold
    def build_gold(self, table: str, *, group_by: list[str],
                   aggregates: dict[str, str],
                   target: str | None = None) -> StageResult:
        """Aggregate silver into an analytics-ready table, rebuilt each run."""
        target = target or f"{table}_agg"
        try:
            silver = DeltaTable(self.path("silver", table)).to_pyarrow_table()
        except Exception as e:
            raise RuntimeError(f"No silver table for '{table}': {e}") from e

        rows = silver.to_pylist()
        buckets: dict[tuple, list[dict]] = {}
        for r in rows:
            buckets.setdefault(tuple(r.get(g) for g in group_by), []).append(r)

        out: list[dict] = []
        for key, group in buckets.items():
            rec = dict(zip(group_by, key))
            rec["row_count"] = len(group)
            for field, how in aggregates.items():
                vals = [r[field] for r in group if r.get(field) is not None]
                if how == "vwap":
                    # Volume-weighted average needs a `size` column; a plain mean
                    # of prices is a different and usually wrong number.
                    num = sum(r[field] * r.get("size", 0) for r in group
                              if r.get(field) is not None)
                    den = sum(r.get("size", 0) for r in group)
                    rec[f"{field}_vwap"] = float(num / den) if den else None
                elif not vals:
                    rec[f"{field}_{how}"] = None
                elif how == "sum":
                    rec[f"{field}_sum"] = float(sum(vals))
                elif how == "mean":
                    rec[f"{field}_mean"] = float(sum(vals)) / len(vals)
                elif how == "min":
                    rec[f"{field}_min"] = float(min(vals))
                elif how == "max":
                    rec[f"{field}_max"] = float(max(vals))
                elif how == "count":
                    rec[f"{field}_count"] = len(vals)
                else:
                    raise ValueError(f"unsupported aggregate '{how}'")
            out.append(rec)

        if out:
            write_deltalake(self.path("gold", target), pa.Table.from_pylist(out),
                            mode="overwrite", schema_mode="overwrite")
        return StageResult("gold", target, len(rows), len(out), 0,
                           self._version("gold", target))

    # ------------------------------------------------------- time travel
    def read(self, layer: str, table: str, version: int | None = None) -> list[dict]:
        dt = DeltaTable(self.path(layer, table))
        if version is not None:
            dt.load_as_version(version)
        return dt.to_pyarrow_table().to_pylist()

    def history(self, layer: str, table: str, limit: int = 20) -> list[dict]:
        """Delta's commit log — what makes "what did this look like on Tuesday"
        answerable, and the reason the format was chosen."""
        return DeltaTable(self.path(layer, table)).history(limit)

    def versions(self, layer: str, table: str) -> int:
        return self._version(layer, table)

    def tables(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for layer in ("bronze", "silver", "gold"):
            d = self.root / layer
            out[layer] = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []
        return out
