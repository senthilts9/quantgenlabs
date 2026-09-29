"""Lakehouse: bronze/silver/gold on Delta Lake via delta-rs.

No Spark, no JVM, no Databricks workspace required — the tables written here are
ordinary Delta tables that a Databricks cluster would read unchanged.
"""
from .medallion import (HAVE_DELTA, Medallion, StageResult, availability,
                        default_root)

__all__ = ["HAVE_DELTA", "Medallion", "StageResult", "availability", "default_root"]
