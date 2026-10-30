"""Schema inference from uploaded files or a Postgres source (design 9.1, extraction tier 0).

Uploads are read by suffix (`keeper.schema_inference.file_formats`): CSV or TSV text, JSON
documents holding an array of objects, JSON Lines, or Parquet files when pyarrow is installed
(the `lorebase-keeper[parquet]` extra; without it a Parquet upload is refused with a 422).

Nothing is committed: the proposal is returned for review, and a modeler applies it (edited)
through a schema change set on a branch. CPU-bound profiling and inference run in a worker
thread so the event loop stays free.

MySQL, MariaDB and SQL Server sources are profiled the same way (a bounded sample per table
read through `lorebase_store.profiling.sql_databases`, then the record profiler); their drivers
are the optional extras `lorebase-keeper[mysql]` and `lorebase-keeper[mssql]`.

A preview (`preview_files`) reads the same uploads under the same limits and returns their first
rows as records instead of a proposal; nothing is stored.

Database sources are named by secret reference only, and only references listed for the Domain
in `keeper.schema_inference.domain_sources` (or, for Domains not listed there, in
`keeper.schema_inference.allowed_sources`) are accepted: callers never send connection strings,
and errors never echo the resolved value nor the sources of other Domains.
"""

from __future__ import annotations

import logging
from base64 import b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from lorebase_core.infer import InferenceOptions, SchemaProposal, TableProfile, infer_schema
from lorebase_core.model import SECRET_REFERENCE_PREFIX, Domain
from lorebase_core.validate import SchemaRules
from lorebase_keeper.errors import PAYLOAD_TOO_LARGE, UNAVAILABLE, VALIDATION_FAILED, ProblemError
from lorebase_keeper.services.config import SchemaInferenceSettings, UploadFormat
from lorebase_platform.secrets import SecretResolutionError, resolve_secret
from lorebase_store.db import create_engine
from lorebase_store.profiling import (
    CsvProfileError,
    ParquetUnavailableError,
    PostgresProfileError,
    RecordProfileError,
    SqlDialect,
    SqlDriverError,
    SqlProfileError,
    profile_bytes,
    profile_json_bytes,
    profile_jsonl_bytes,
    profile_parquet_bytes,
    profile_postgres,
    profile_sql_database,
    table_name,
)
from lorebase_store.settings import (
    DatabaseSettings,
    PostgresProfilingSettings,
    ProfilingSettings,
)

logger = logging.getLogger("lorebase.keeper.schema_inference")

APPLICATION_NAME = "lorebase-keeper"
"""The installing package: named in driver hints and reported to the databases read."""
PARQUET_EXTRA = f"{APPLICATION_NAME}[parquet]"


@dataclass(frozen=True)
class UploadedFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class PostgresSource:
    dsn: str
    schema: str
    tables: tuple[str, ...]


@dataclass(frozen=True)
class SqlSource:
    """A MySQL, MariaDB or SQL Server source; `schema` defaults to the connection's own."""

    dsn: str
    schema: str | None
    tables: tuple[str, ...]


@dataclass(frozen=True)
class FilePreview:
    """The first rows of an uploaded file, as JSON-ready records."""

    name: str
    columns: tuple[str, ...]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


def invalid(pointer: str, message: str) -> ProblemError:
    """422 naming one invalid request member."""
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": "invalid"}]},
    )


def domain_of(domain_id: str) -> Domain:
    try:
        return Domain(id=domain_id)
    except ValidationError:
        raise invalid("/path/domain_id", "is not a Domain id") from None


def merged_options(
    settings: SchemaInferenceSettings, overrides: Mapping[str, Any] | None, pointer: str
) -> InferenceOptions:
    """The configured options with a request's overrides on top."""
    if not overrides:
        return settings.options
    try:
        return InferenceOptions.model_validate({**settings.options.model_dump(), **overrides})
    except ValidationError as error:
        first = error.errors()[0]
        location = "/".join(str(part) for part in first["loc"])
        raise invalid(f"{pointer}/{location}", str(first["msg"])) from None


def check_upload_sizes(files: Sequence[UploadedFile], settings: SchemaInferenceSettings) -> None:
    """Refuse requests over the file count, size or suffix limits."""
    if not files:
        raise invalid("/body/files", "upload at least one file")
    if len(files) > settings.max_files:
        raise invalid("/body/files", f"at most {settings.max_files} files are accepted")
    names: set[str] = set()
    for index, upload in enumerate(files):
        if not upload.name.lower().endswith(settings.file_suffixes):
            raise invalid(
                f"/body/files/{index}",
                f"file names must end with one of {', '.join(settings.file_suffixes)}",
            )
        name = table_name(upload.name)
        if name in names:
            raise invalid(f"/body/files/{index}", f"two files are named {name!r}")
        names.add(name)
        if len(upload.data) > settings.max_file_bytes:
            raise ProblemError(
                PAYLOAD_TOO_LARGE, f"{upload.name} exceeds {settings.max_file_bytes} bytes."
            )
    if sum(len(f.data) for f in files) > settings.max_total_bytes:
        raise ProblemError(
            PAYLOAD_TOO_LARGE, f"The files exceed {settings.max_total_bytes} bytes in total."
        )


def check_records_path(records_path: str | None, pointer: str = "/body/records_path") -> None:
    """Refuse an empty dotted path or one with an empty key."""
    if records_path is not None and not all(records_path.split(".")):
        raise invalid(pointer, "must be dotted keys such as data.items")


def _profile_file(
    upload: UploadedFile,
    upload_format: UploadFormat,
    profiling: ProfilingSettings,
    records_path: str | None = None,
) -> TableProfile:
    match upload_format:
        case "json":
            return profile_json_bytes(upload.data, upload.name, profiling.records, records_path)
        case "jsonl":
            return profile_jsonl_bytes(upload.data, upload.name, profiling.records)
        case "parquet":
            return profile_parquet_bytes(upload.data, upload.name, profiling.records)
        case _:
            return profile_bytes(upload.data, upload.name, profiling.csv)


def _profile_files(
    files: Sequence[UploadedFile],
    settings: SchemaInferenceSettings,
    profiling: ProfilingSettings,
    records_path: str | None = None,
) -> list[TableProfile]:
    profiles = []
    for index, upload in enumerate(files):
        try:
            profiles.append(
                _profile_file(upload, settings.format_of(upload.name), profiling, records_path)
            )
        except ParquetUnavailableError as error:
            raise invalid(
                f"/body/files/{index}", f"{error}; this server needs {PARQUET_EXTRA}"
            ) from None
        except (CsvProfileError, RecordProfileError) as error:
            raise invalid(f"/body/files/{index}", str(error)) from None
    return profiles


async def infer_from_files(
    domain_id: str,
    files: Sequence[UploadedFile],
    settings: SchemaInferenceSettings,
    profiling: ProfilingSettings,
    options: InferenceOptions,
    rules: SchemaRules | None = None,
    records_path: str | None = None,
) -> SchemaProposal:
    """Profile uploaded files and propose a schema (validated with `rules`). `records_path`
    (dotted keys) locates the records array of JSON documents holding them under a key."""
    domain = domain_of(domain_id)
    check_upload_sizes(files, settings)
    check_records_path(records_path)

    def run() -> SchemaProposal:
        profiles = _profile_files(files, settings, profiling, records_path)
        return infer_schema(profiles, domain, options, rules)

    return await run_in_threadpool(run)


def _json_value(value: Any, limit: int) -> Any:
    """A profiled value as JSON: dates as ISO text, decimals as text (exact), bytes as base64;
    text is cut to `limit` characters."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "..."
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, bytes | bytearray | memoryview):
        return b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, limit) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item, limit) for item in value]
    return str(value)


def preview_rows(rows: int | None, settings: SchemaInferenceSettings) -> int:
    """The number of preview rows: the request's, or the configured default, within the cap."""
    count = settings.preview_rows if rows is None else rows
    if not 1 <= count <= settings.preview_max_rows:
        raise invalid("/query/rows", f"must be between 1 and {settings.preview_max_rows}")
    return count


async def preview_files(
    files: Sequence[UploadedFile],
    settings: SchemaInferenceSettings,
    profiling: ProfilingSettings,
    rows: int,
    records_path: str | None = None,
) -> list[FilePreview]:
    """The first `rows` rows of each uploaded file as records, under the limits of an
    inference request; nothing is stored. CSV and TSV values stay text."""
    check_upload_sizes(files, settings)
    check_records_path(records_path)
    head: dict[str, Any] = {"sample_rows": rows, "sampling": "head"}
    bounded = profiling.model_copy(
        update={
            "csv": profiling.csv.model_copy(update=head),
            "records": profiling.records.model_copy(update=head),
        }
    )

    def run() -> list[FilePreview]:
        previews = []
        profiles = _profile_files(files, settings, bounded, records_path)
        for upload, profile in zip(files, profiles, strict=True):
            names = tuple(column.name for column in profile.columns)
            values = [list(column.values) for column in profile.columns]
            kept = len(values[0]) if values else 0
            records = [
                {
                    name: _json_value(column[index], settings.preview_max_cell_chars)
                    for name, column in zip(names, values, strict=True)
                }
                for index in range(kept)
            ]
            total = profile.row_count if profile.row_count is not None else kept
            previews.append(FilePreview(upload.name, names, records, total, total > kept))
        return previews

    return await run_in_threadpool(run)


def _resolve_source(
    domain: Domain,
    source: PostgresSource | SqlSource,
    settings: SchemaInferenceSettings,
    key: str = "postgres",
) -> str:
    pointer = f"/body/{key}/dsn"
    if not source.dsn.startswith(SECRET_REFERENCE_PREFIX):
        raise invalid(pointer, "must be a secret reference such as secret://env/NAME")
    if source.dsn not in settings.sources_for(domain.id):
        raise invalid(pointer, "is not an allowed source for this Domain")
    try:
        return resolve_secret(source.dsn)
    except SecretResolutionError:
        raise invalid(pointer, "cannot be resolved on this server") from None


async def infer_from_postgres(
    domain_id: str,
    source: PostgresSource,
    settings: SchemaInferenceSettings,
    database: DatabaseSettings,
    postgres: PostgresProfilingSettings,
    options: InferenceOptions,
    rules: SchemaRules | None = None,
) -> SchemaProposal:
    """Profile tables of an allowed Postgres source (read-only) and propose a schema
    (validated with `rules`)."""
    domain = domain_of(domain_id)
    url = _resolve_source(domain, source, settings)
    try:
        engine = create_engine(database, url=url, pooled=False)
    except Exception:  # the message of a URL parse error may contain the URL
        raise invalid("/body/postgres/dsn", "does not hold a Postgres URL") from None
    try:
        profiles = await profile_postgres(
            engine, postgres, schema=source.schema, tables=source.tables or None
        )
    except PostgresProfileError as error:
        raise invalid("/body/postgres", str(error)) from None
    except (SQLAlchemyError, OSError) as error:
        logger.warning(
            "schema inference source unavailable",
            extra={"source": source.dsn, "error": type(error).__name__},
        )
        raise ProblemError(UNAVAILABLE, "The source database cannot be read.") from None
    finally:
        await engine.dispose()
    return await run_in_threadpool(infer_schema, profiles, domain, options, rules)


async def infer_from_sql(
    domain_id: str,
    dialect: SqlDialect,
    source: SqlSource,
    settings: SchemaInferenceSettings,
    profiling: ProfilingSettings,
    options: InferenceOptions,
    rules: SchemaRules | None = None,
) -> SchemaProposal:
    """Profile tables of an allowed MySQL, MariaDB or SQL Server source (bounded sample per
    table, read-only where the server allows it) and propose a schema (validated with
    `rules`)."""
    domain = domain_of(domain_id)
    url = _resolve_source(domain, source, settings, dialect)
    try:
        profiles = await profile_sql_database(
            dialect,
            url,
            profiling.sql,
            profiling.records,
            application=APPLICATION_NAME,
            schema=source.schema,
            tables=source.tables or None,
            allowed_schemas=settings.allowed_schemas.get(source.dsn),
        )
    except SqlDriverError as error:
        # The server's own set-up problem: logged, not described to the caller.
        logger.warning(
            "schema inference driver unavailable",
            extra={"source": source.dsn, "detail": str(error)},
        )
        raise ProblemError(UNAVAILABLE, "The source database cannot be read.") from None
    except SqlProfileError as error:
        raise invalid(f"/body/{dialect}", str(error)) from None
    except Exception as error:  # driver, network and catalog failures all come from the worker
        logger.warning(
            "schema inference source unavailable",
            extra={"source": source.dsn, "error": type(error).__name__},
        )
        raise ProblemError(UNAVAILABLE, "The source database cannot be read.") from None
    return await run_in_threadpool(infer_schema, profiles, domain, options, rules)
