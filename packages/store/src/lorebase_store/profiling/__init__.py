"""Profilers of structured sources for schema inference (``lorebase_core.infer``).

They read CSV and TSV files (``delimited``), Postgres tables (``postgres``) and rows read as
records (``records``: JSON, JSON Lines, Parquet and API Sources, whose readers live in Scribe)
into ``TableProfile`` objects. Limits come from the ``store.profiling`` settings.
"""

from lorebase_store.profiling.delimited import (
    CsvProfileError,
    profile_bytes,
    profile_file,
    profile_text,
    table_name,
)
from lorebase_store.profiling.postgres import PostgresProfileError, profile_postgres, type_spec
from lorebase_store.profiling.records import (
    RecordProfileError,
    RecordProfiler,
    RowSampler,
    profile_records,
)

__all__ = [
    "CsvProfileError",
    "PostgresProfileError",
    "RecordProfileError",
    "RecordProfiler",
    "RowSampler",
    "profile_bytes",
    "profile_file",
    "profile_postgres",
    "profile_records",
    "profile_text",
    "table_name",
    "type_spec",
]
