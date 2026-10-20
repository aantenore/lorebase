"""Schema inference from uploaded CSV files or a Postgres source (design 9.1, extraction tier 0).

Nothing is committed: the proposal is returned for review, and a modeler applies it (edited)
through a schema change set on a branch. CPU-bound profiling and inference run in a worker
thread so the event loop stays free.

Postgres sources are named by secret reference only, and only references listed in
`keeper.schema_inference.allowed_sources` are accepted: callers never send connection strings,
and errors never echo the resolved value.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from lorebase_core.infer import InferenceOptions, SchemaProposal, TableProfile, infer_schema
from lorebase_core.model import SECRET_REFERENCE_PREFIX, Domain
from lorebase_keeper.errors import PAYLOAD_TOO_LARGE, UNAVAILABLE, VALIDATION_FAILED, ProblemError
from lorebase_keeper.services.config import SchemaInferenceSettings
from lorebase_platform.secrets import SecretResolutionError, resolve_secret
from lorebase_store.db import create_engine
from lorebase_store.profiling import (
    CsvProfileError,
    PostgresProfileError,
    profile_bytes,
    profile_postgres,
    table_name,
)
from lorebase_store.settings import (
    CsvProfilingSettings,
    DatabaseSettings,
    PostgresProfilingSettings,
)

logger = logging.getLogger("lorebase.keeper.schema_inference")


@dataclass(frozen=True)
class UploadedFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class PostgresSource:
    dsn: str
    schema: str
    tables: tuple[str, ...]


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
        raise invalid("/body/files", "upload at least one CSV file")
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


def _profile_files(files: Sequence[UploadedFile], csv: CsvProfilingSettings) -> list[TableProfile]:
    profiles = []
    for index, upload in enumerate(files):
        try:
            profiles.append(profile_bytes(upload.data, upload.name, csv))
        except CsvProfileError as error:
            raise invalid(f"/body/files/{index}", str(error)) from None
    return profiles


async def infer_from_files(
    domain_id: str,
    files: Sequence[UploadedFile],
    settings: SchemaInferenceSettings,
    csv: CsvProfilingSettings,
    options: InferenceOptions,
) -> SchemaProposal:
    """Profile uploaded CSV/TSV files and propose a schema."""
    domain = domain_of(domain_id)
    check_upload_sizes(files, settings)

    def run() -> SchemaProposal:
        return infer_schema(_profile_files(files, csv), domain, options)

    return await run_in_threadpool(run)


def _resolve_source(source: PostgresSource, settings: SchemaInferenceSettings) -> str:
    if not source.dsn.startswith(SECRET_REFERENCE_PREFIX):
        raise invalid("/body/postgres/dsn", "must be a secret reference such as secret://env/NAME")
    if source.dsn not in settings.allowed_sources:
        raise invalid(
            "/body/postgres/dsn",
            "is not an allowed source (keeper.schema_inference.allowed_sources)",
        )
    try:
        return resolve_secret(source.dsn)
    except SecretResolutionError:
        raise invalid("/body/postgres/dsn", "cannot be resolved on this server") from None


async def infer_from_postgres(
    domain_id: str,
    source: PostgresSource,
    settings: SchemaInferenceSettings,
    database: DatabaseSettings,
    postgres: PostgresProfilingSettings,
    options: InferenceOptions,
) -> SchemaProposal:
    """Profile tables of an allowed Postgres source (read-only) and propose a schema."""
    domain = domain_of(domain_id)
    url = _resolve_source(source, settings)
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
    return await run_in_threadpool(infer_schema, profiles, domain, options)
