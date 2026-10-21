"""Profilers of structured sources for schema inference (``lorebase_core.infer``).

They read CSV and TSV files (``delimited``), Postgres tables (``postgres``), rows read as
records (``records``: JSON, JSON Lines, Parquet and API Sources, whose readers live in Scribe)
and uploaded JSON, JSON Lines and Parquet files (``record_files``) into ``TableProfile``
objects. Limits come from the ``store.profiling`` settings.
"""

from lorebase_store.profiling.delimited import (
    CsvProfileError,
    profile_bytes,
    profile_file,
    profile_text,
    table_name,
)
from lorebase_store.profiling.postgres import PostgresProfileError, profile_postgres, type_spec
from lorebase_store.profiling.record_files import (
    ParquetUnavailableError,
    profile_json_bytes,
    profile_jsonl_bytes,
    profile_parquet_bytes,
)
from lorebase_store.profiling.records import (
    RecordProfileError,
    RecordProfiler,
    RowSampler,
    profile_records,
)

__all__ = [
    "CsvProfileError",
    "ParquetUnavailableError",
    "PostgresProfileError",
    "RecordProfileError",
    "RecordProfiler",
    "RowSampler",
    "profile_bytes",
    "profile_file",
    "profile_json_bytes",
    "profile_jsonl_bytes",
    "profile_parquet_bytes",
    "profile_postgres",
    "profile_records",
    "profile_text",
    "table_name",
    "type_spec",
]
