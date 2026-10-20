"""Profilers of structured sources for schema inference (``lorebase_core.infer``).

They read CSV and TSV files (``delimited``) and Postgres tables (``postgres``) into
``TableProfile`` objects. Limits come from the ``store.profiling`` settings. Parquet is not
supported yet: it needs ``pyarrow``, which is not a dependency.
"""

from lorebase_store.profiling.delimited import (
    CsvProfileError,
    profile_bytes,
    profile_file,
    profile_text,
    table_name,
)
from lorebase_store.profiling.postgres import PostgresProfileError, profile_postgres, type_spec

__all__ = [
    "CsvProfileError",
    "PostgresProfileError",
    "profile_bytes",
    "profile_file",
    "profile_postgres",
    "profile_text",
    "table_name",
    "type_spec",
]
