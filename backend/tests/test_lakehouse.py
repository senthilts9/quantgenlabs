"""Medallion pipeline tests, run against REAL Delta tables.

delta-rs writes the same `_delta_log` a Databricks cluster reads, so these
exercise the actual format rather than a stand-in: ACID commits, schema
enforcement, time travel and the commit history are all genuinely present.

The cases that matter are the ones that make a pipeline safe to re-run and safe
to trust: idempotent ingestion, rejects kept with reasons rather than dropped, and
gold rebuilt from silver rather than drifting.
"""
from __future__ import annotations

import pytest

from app.core.lakehouse import HAVE_DELTA, Medallion, availability

pytestmark = pytest.mark.skipif(not HAVE_DELTA, reason="deltalake/pyarrow not installed")


@pytest.fixture
def lake(tmp_path):
    return Medallion(tmp_path / "lake")


def ticks(n=10, symbol="AAPL", start_ts=0):
    return [{"symbol": symbol, "ts": start_ts + i, "price": 100.0 + i * 0.01,
             "size": 100 + i} for i in range(n)]


def test_engine_needs_no_spark_or_databricks():
    a = availability()
    assert a["available"] is True
    assert "no Spark" in a["engine"]


# ------------------------------------------------------------------ bronze
def test_bronze_stores_records_verbatim(lake):
    r = lake.ingest_bronze("ticks", ticks(5), source="simfeed", batch_id="B1")
    assert r.rows_written == 5
    rows = lake.read("bronze", "ticks")
    assert len(rows) == 5
    assert all(set(x) >= {"_source", "_batch_id", "_ingested_at", "_raw"} for x in rows)
    assert rows[0]["_source"] == "simfeed"


def test_bronze_keeps_records_that_will_fail_validation(lake):
    """The layer people skip. When a normalisation bug is found in silver, bronze
    is the only thing that lets it be reprocessed rather than re-requested."""
    bad = [{"symbol": "", "ts": 1, "price": "junk", "size": 1}]
    r = lake.ingest_bronze("ticks", bad, source="simfeed", batch_id="B1")
    assert r.rows_written == 1          # kept, not rejected at ingest
    assert r.rows_rejected == 0


def test_bronze_ingest_is_idempotent(lake):
    """A pipeline that must not be run twice will be run twice at 3am."""
    lake.ingest_bronze("ticks", ticks(10), source="simfeed", batch_id="B1")
    again = lake.ingest_bronze("ticks", ticks(10), source="simfeed", batch_id="B1")
    assert again.skipped is True
    assert again.rows_written == 0
    assert len(lake.read("bronze", "ticks")) == 10      # not 20


def test_bronze_appends_a_different_batch(lake):
    lake.ingest_bronze("ticks", ticks(5, start_ts=0), source="f", batch_id="B1")
    r = lake.ingest_bronze("ticks", ticks(5, start_ts=5), source="f", batch_id="B2")
    assert r.skipped is False
    assert len(lake.read("bronze", "ticks")) == 10


def test_empty_batch_is_not_an_error(lake):
    r = lake.ingest_bronze("ticks", [], source="f", batch_id="B0")
    assert r.rows_written == 0
    assert "empty" in r.note


# ------------------------------------------------------------------ silver
def test_silver_validates_types_and_dedupes(lake):
    rows = ticks(10)
    rows.append(dict(rows[0]))                                   # duplicate key
    rows.append({"symbol": "", "ts": 99, "price": 1.0, "size": 1})     # missing
    rows.append({"symbol": "X", "ts": 98, "price": "junk", "size": 1}) # non-numeric
    lake.ingest_bronze("ticks", rows, source="f", batch_id="B1")

    r = lake.build_silver("ticks", key_fields=["symbol", "ts"],
                          required=["symbol", "ts", "price"], numeric=["price", "size"])
    assert r.rows_in == 13
    assert r.rows_written == 10
    assert r.rows_rejected == 3


def test_silver_keeps_rejects_with_a_reason(lake):
    """"How much did we reject and why" must be a query, not archaeology."""
    rows = ticks(3)
    rows.append(dict(rows[0]))
    rows.append({"symbol": "", "ts": 50, "price": 1.0, "size": 1})
    rows.append({"symbol": "X", "ts": 51, "price": "junk", "size": 1})
    lake.ingest_bronze("ticks", rows, source="f", batch_id="B1")
    lake.build_silver("ticks", key_fields=["symbol", "ts"],
                      required=["symbol", "ts", "price"], numeric=["price", "size"])

    reasons = [x["_reason"] for x in lake.read("silver", "ticks_rejected")]
    assert len(reasons) == 3
    assert any("duplicate" in r for r in reasons)
    assert any("missing" in r for r in reasons)
    assert any("non-numeric" in r for r in reasons)


def test_silver_coerces_numeric_strings(lake):
    lake.ingest_bronze("ticks", [{"symbol": "A", "ts": 1, "price": "100.5", "size": "10"}],
                       source="f", batch_id="B1")
    lake.build_silver("ticks", key_fields=["symbol", "ts"],
                      required=["symbol", "ts", "price"], numeric=["price", "size"])
    row = lake.read("silver", "ticks")[0]
    assert isinstance(row["price"], float) and row["price"] == 100.5


def test_silver_rebuild_is_deterministic_not_cumulative(lake):
    """Silver is a pure function of bronze, so rebuilding must not duplicate."""
    lake.ingest_bronze("ticks", ticks(10), source="f", batch_id="B1")
    a = lake.build_silver("ticks", key_fields=["symbol", "ts"],
                          required=["symbol"], numeric=["price"])
    b = lake.build_silver("ticks", key_fields=["symbol", "ts"],
                          required=["symbol"], numeric=["price"])
    assert a.rows_written == b.rows_written == 10
    assert len(lake.read("silver", "ticks")) == 10       # not 20


def test_silver_without_bronze_fails_clearly(lake):
    with pytest.raises(RuntimeError, match="No bronze table"):
        lake.build_silver("nope", key_fields=["a"], required=["a"])


# -------------------------------------------------------------------- gold
def test_gold_aggregates_per_group(lake):
    rows = ticks(10, "AAPL") + ticks(10, "MSFT")
    lake.ingest_bronze("ticks", rows, source="f", batch_id="B1")
    lake.build_silver("ticks", key_fields=["symbol", "ts"],
                      required=["symbol"], numeric=["price", "size"])
    r = lake.build_gold("ticks", group_by=["symbol"],
                        aggregates={"price": "mean", "size": "sum"})
    assert r.rows_written == 2
    by = {x["symbol"]: x for x in lake.read("gold", "ticks_agg")}
    assert by["AAPL"]["row_count"] == 10
    assert by["AAPL"]["size_sum"] == sum(100 + i for i in range(10))


def test_gold_vwap_is_size_weighted_not_a_mean_of_prices(lake):
    """A plain mean of prices is a different and usually wrong number."""
    rows = [{"symbol": "A", "ts": 1, "price": 100.0, "size": 1},
            {"symbol": "A", "ts": 2, "price": 200.0, "size": 99}]
    lake.ingest_bronze("ticks", rows, source="f", batch_id="B1")
    lake.build_silver("ticks", key_fields=["symbol", "ts"],
                      required=["symbol"], numeric=["price", "size"])
    lake.build_gold("ticks", group_by=["symbol"], aggregates={"price": "vwap"})
    vwap = lake.read("gold", "ticks_agg")[0]["price_vwap"]
    assert vwap == pytest.approx((100 * 1 + 200 * 99) / 100)
    assert vwap != pytest.approx(150.0)        # the naive mean


def test_gold_rebuild_does_not_drift(lake):
    lake.ingest_bronze("ticks", ticks(10), source="f", batch_id="B1")
    lake.build_silver("ticks", key_fields=["symbol", "ts"],
                      required=["symbol"], numeric=["price", "size"])
    lake.build_gold("ticks", group_by=["symbol"], aggregates={"size": "sum"})
    first = lake.read("gold", "ticks_agg")[0]["size_sum"]
    lake.build_gold("ticks", group_by=["symbol"], aggregates={"size": "sum"})
    assert lake.read("gold", "ticks_agg")[0]["size_sum"] == first


def test_unsupported_aggregate_is_refused(lake):
    lake.ingest_bronze("ticks", ticks(3), source="f", batch_id="B1")
    lake.build_silver("ticks", key_fields=["symbol", "ts"],
                      required=["symbol"], numeric=["price"])
    with pytest.raises(ValueError, match="unsupported aggregate"):
        lake.build_gold("ticks", group_by=["symbol"], aggregates={"price": "median"})


# ------------------------------------------------------------ delta format
def test_time_travel_reads_an_earlier_version(lake):
    """The reason the format was chosen: "what did this look like on Tuesday"
    has to be answerable."""
    lake.ingest_bronze("ticks", ticks(5, start_ts=0), source="f", batch_id="B1")
    assert lake.versions("bronze", "ticks") == 0
    lake.ingest_bronze("ticks", ticks(5, start_ts=5), source="f", batch_id="B2")
    assert lake.versions("bronze", "ticks") == 1

    assert len(lake.read("bronze", "ticks")) == 10              # current
    assert len(lake.read("bronze", "ticks", version=0)) == 5    # as it was


def test_commit_history_is_available(lake):
    lake.ingest_bronze("ticks", ticks(3), source="f", batch_id="B1")
    lake.ingest_bronze("ticks", ticks(3, start_ts=3), source="f", batch_id="B2")
    h = lake.history("bronze", "ticks")
    assert len(h) >= 2
    assert all("operation" in e for e in h)


def test_layers_are_listed(lake):
    lake.ingest_bronze("ticks", ticks(5), source="f", batch_id="B1")
    lake.build_silver("ticks", key_fields=["symbol", "ts"],
                      required=["symbol"], numeric=["price"])
    lake.build_gold("ticks", group_by=["symbol"], aggregates={"price": "mean"})
    t = lake.tables()
    assert "ticks" in t["bronze"]
    assert "ticks" in t["silver"]
    assert "ticks_agg" in t["gold"]
