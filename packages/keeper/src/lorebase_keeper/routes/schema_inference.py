"""Schema inference (design 9.1, 11.1): `POST /domains/{d}/schema:infer`, permission
`schema.write`.

The body is either `multipart/form-data` with files in `files` (CSV or TSV text, JSON documents,
JSON Lines or Parquet, read by suffix; optionally an `options` field holding a JSON object of
`InferenceOptions` overrides and a `records_path` field, the dotted keys of the records array
of JSON documents that hold it under a key), or JSON naming an allowed database source:
`{"postgres": {"dsn": "secret://env/ERP_DSN", "schema": "public", "tables": [...]}, "options":
{...}}`, with `mysql` (MySQL and MariaDB) or `mssql` (SQL Server) in place of `postgres`. The
answer is the proposal: the schema (JSON and YAML), the evidence of every element and the
issues to settle; with `Accept: text/yaml` or `format=yaml`, only the YAML document. Nothing is
committed.

`POST /domains/{d}/schema:preview` takes the same uploads and returns their first rows as
records instead of a proposal (permission, size limits and nothing stored as for `schema:infer`),
so a client can show a Parquet file it cannot read itself.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.datastructures import UploadFile

from lorebase_core.infer import SchemaProposal
from lorebase_core.model import dump_schema
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.deps import KeeperSettingsDep, SettingsDep
from lorebase_keeper.errors import BAD_REQUEST, Problem, ProblemError
from lorebase_keeper.services import schema_inference as service
from lorebase_store.profiling import SqlDialect
from lorebase_store.settings import database_settings, schema_settings, store_settings

router = APIRouter(tags=["schema"])

YAML_MEDIA_TYPES = ("text/yaml", "application/yaml", "application/x-yaml")
MULTIPART = "multipart/form-data"
#: Form fields besides the files: `options` and `records_path`.
_MAX_FORM_FIELDS = 2
SQL_KEYS: tuple[SqlDialect, ...] = ("mysql", "mssql")

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks schema.write in the Domain."},
    413: {"model": Problem, "description": "A file or the upload is too large."},
    422: {"model": Problem, "description": "Invalid files, source or options."},
    503: {"model": Problem, "description": "The source database cannot be read."},
}

_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            MULTIPART: {
                "schema": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                            "description": "CSV, TSV, JSON, JSON Lines or Parquet files, read "
                            "by suffix; the file name is the table name.",
                        },
                        "options": {
                            "type": "string",
                            "description": "JSON object of InferenceOptions overrides.",
                        },
                        "records_path": {
                            "type": "string",
                            "description": "Dotted keys of the records array of JSON documents "
                            "that hold it under a key, e.g. data.items.",
                        },
                    },
                    "required": ["files"],
                }
            },
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "postgres": {
                            "type": "object",
                            "properties": {
                                "dsn": {"type": "string", "example": "secret://env/ERP_DSN"},
                                "schema": {"type": "string", "default": "public"},
                                "tables": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["dsn"],
                        },
                        "mysql": {
                            "type": "object",
                            "description": "A MySQL or MariaDB source, in place of postgres.",
                            "properties": {
                                "dsn": {"type": "string", "example": "secret://env/ERP_DSN"},
                                "schema": {"type": "string"},
                                "tables": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["dsn"],
                        },
                        "mssql": {
                            "type": "object",
                            "description": "A SQL Server source, in place of postgres.",
                            "properties": {
                                "dsn": {"type": "string", "example": "secret://env/ERP_DSN"},
                                "schema": {"type": "string"},
                                "tables": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["dsn"],
                        },
                        "options": {"type": "object", "description": "InferenceOptions overrides."},
                    },
                },
            },
        },
    }
}

_PREVIEW_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            MULTIPART: {
                "schema": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                            "description": "CSV, TSV, JSON, JSON Lines or Parquet files, read "
                            "by suffix.",
                        },
                        "records_path": {
                            "type": "string",
                            "description": "Dotted keys of the records array of JSON documents "
                            "that hold it under a key, e.g. data.items.",
                        },
                    },
                    "required": ["files"],
                }
            }
        },
    }
}


class PostgresSourceIn(BaseModel):
    """A Postgres source named by an allowed secret reference."""

    dsn: str = Field(description="Secret reference, e.g. secret://env/ERP_DSN; never a URL.")
    schema_: str = Field(default="public", alias="schema", min_length=1, max_length=63)
    tables: list[str] = Field(
        default_factory=list, description="Tables to profile; empty: every table of the schema."
    )


class SqlSourceIn(BaseModel):
    """A MySQL, MariaDB or SQL Server source named by an allowed secret reference."""

    dsn: str = Field(description="Secret reference, e.g. secret://env/ERP_DSN; never a URL.")
    schema_: str | None = Field(
        default=None,
        alias="schema",
        min_length=1,
        max_length=128,
        description="Schema (SQL Server) or database (MySQL); default: the connection's own.",
    )
    tables: list[str] = Field(
        default_factory=list, description="Tables to profile; empty: every table."
    )


class InferFromSource(BaseModel):
    """JSON body: one database source (`postgres`, `mysql` or `mssql`) and optional option
    overrides."""

    postgres: PostgresSourceIn | None = None
    mysql: SqlSourceIn | None = None
    mssql: SqlSourceIn | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_source(self) -> InferFromSource:
        named = [key for key in ("postgres", "mysql", "mssql") if getattr(self, key) is not None]
        if len(named) != 1:
            raise ValueError("name exactly one of postgres, mysql and mssql")
        return self


class FilePreviewOut(BaseModel):
    """The first rows of one uploaded file."""

    name: str
    columns: list[str]
    rows: list[dict[str, Any]] = Field(
        description="Records; dates and decimals as text, nested values as JSON."
    )
    row_count: int = Field(description="Rows in the file (those read, for CSV and JSON Lines).")
    truncated: bool = Field(description="Whether the file has more rows than `rows` returned.")


class PreviewOut(BaseModel):
    """First rows of uploaded files; nothing is stored."""

    domain: str
    files: list[FilePreviewOut]


class ProposalOut(BaseModel):
    """A proposed schema; never committed."""

    domain: str
    status: Literal["proposed"] = "proposed"
    schema_: dict[str, Any] = Field(alias="schema")
    yaml: str = Field(description="The proposed schema as a YAML document.")
    evidence: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    issue_count: int
    tables: list[dict[str, Any]]


def _wants_yaml(request: Request, format_: str | None) -> bool:
    if format_ is not None:
        return format_ == "yaml"
    accept = request.headers.get("accept", "")
    return any(media in accept for media in YAML_MEDIA_TYPES)


def _options_field(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raise service.invalid("/body/options", "must be a JSON object")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise service.invalid("/body/options", "must be a JSON object") from None
    if not isinstance(parsed, dict):
        raise service.invalid("/body/options", "must be a JSON object")
    return parsed


def _records_path_field(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise service.invalid("/body/records_path", "must be dotted keys such as data.items")
    return raw


async def _uploads(
    request: Request, max_files: int, max_part_size: int
) -> tuple[list[service.UploadedFile], dict[str, Any], str | None]:
    try:
        form = await request.form(
            max_files=max_files, max_fields=_MAX_FORM_FIELDS, max_part_size=max_part_size
        )
    except Exception as error:  # the multipart parser reports limits and malformed bodies
        raise ProblemError(BAD_REQUEST, f"Unreadable multipart body: {error}") from None
    try:
        files = []
        for item in form.getlist("files"):
            if not isinstance(item, UploadFile):
                raise service.invalid("/body/files", "must be files")
            files.append(service.UploadedFile(item.filename or "upload.csv", await item.read()))
        return (
            files,
            _options_field(form.get("options")),
            _records_path_field(form.get("records_path")),
        )
    finally:
        await form.close()


async def _json_source(request: Request) -> InferFromSource:
    try:
        return InferFromSource.model_validate(await request.json())
    except ValueError as error:
        if isinstance(error, ValidationError):
            first = error.errors()[0]
            pointer = "/body" + "".join(f"/{part}" for part in first["loc"])
            raise service.invalid(pointer, str(first["msg"])) from None
        raise service.invalid("/body", "is not valid JSON") from None


def _render(proposal: SchemaProposal, domain_id: str, yaml_only: bool, limit: int) -> Response:
    text = dump_schema(proposal.schema)
    if yaml_only:
        return Response(text, media_type="text/yaml")
    document = proposal.document()
    body = ProposalOut(
        domain=domain_id,
        schema=document["schema"],
        yaml=text,
        evidence=document["evidence"],
        issues=document["issues"][:limit],
        issue_count=len(proposal.issues),
        tables=document["tables"],
    )
    return Response(body.model_dump_json(by_alias=True), media_type="application/json")


@router.post(
    "/domains/{domain_id}/schema:infer",
    summary="Propose a schema from tables or data files",
    responses={**_ERRORS, 200: {"model": ProposalOut, "content": {"text/yaml": {}}}},
    response_model=ProposalOut,
    openapi_extra=_BODY,
    dependencies=[Depends(require(Permission.SCHEMA_WRITE))],
)
async def infer(
    domain_id: str,
    request: Request,
    settings: SettingsDep,
    keeper: KeeperSettingsDep,
    format_: Annotated[Literal["json", "yaml"] | None, Query(alias="format")] = None,
) -> Response:
    """Profile the uploaded files (CSV, TSV, JSON, JSON Lines, Parquet) or the tables of a
    Postgres, MySQL, MariaDB or SQL Server source and propose Kinds, typed Attributes, keys and
    Relations with evidence. Deterministic and model-free; nothing is written."""
    config = keeper.schema_inference
    profiling = store_settings(settings).profiling
    rules = schema_settings(settings).rules()
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(MULTIPART):
        # One file over the limit still parses, so the answer names the limit (422); the
        # parser refuses bodies beyond that (400).
        files, overrides, records_path = await _uploads(
            request, config.max_files + 1, config.max_file_bytes
        )
        options = service.merged_options(config, overrides, "/body/options")
        proposal = await service.infer_from_files(
            domain_id, files, config, profiling, options, rules, records_path
        )
        source = ",".join(sorted({config.format_of(upload.name) for upload in files}))
    else:
        body = await _json_source(request)
        options = service.merged_options(config, body.options, "/body/options")
        sql_key = next((key for key in SQL_KEYS if getattr(body, key) is not None), None)
        if sql_key is not None:
            named: SqlSourceIn = getattr(body, sql_key)
            proposal = await service.infer_from_sql(
                domain_id,
                sql_key,
                service.SqlSource(named.dsn, named.schema_, tuple(named.tables)),
                config,
                profiling,
                options,
                rules,
            )
            source = sql_key
        else:
            assert body.postgres is not None  # the model names exactly one source
            proposal = await service.infer_from_postgres(
                domain_id,
                service.PostgresSource(
                    body.postgres.dsn, body.postgres.schema_, tuple(body.postgres.tables)
                ),
                config,
                database_settings(settings),
                profiling.postgres,
                options,
                rules,
            )
            source = "postgres"
    annotate(
        request,
        action="schema.infer",
        target=domain_id,
        source=source,
        tables=len(proposal.tables),
        kinds=len(proposal.schema.kinds),
        relations=len(proposal.schema.relations),
        issues=len(proposal.issues),
    )
    return _render(proposal, domain_id, _wants_yaml(request, format_), config.max_issues)


@router.post(
    "/domains/{domain_id}/schema:preview",
    summary="Read the first rows of uploaded data files",
    responses={**_ERRORS, 200: {"model": PreviewOut}},
    response_model=PreviewOut,
    openapi_extra=_PREVIEW_BODY,
    dependencies=[Depends(require(Permission.SCHEMA_WRITE))],
)
async def preview(
    domain_id: str,
    request: Request,
    settings: SettingsDep,
    keeper: KeeperSettingsDep,
    rows: Annotated[
        int | None, Query(description="Rows per file; default and cap are settings.")
    ] = None,
) -> PreviewOut:
    """Return the first rows of each uploaded file (CSV, TSV, JSON, JSON Lines, Parquet) as
    records, under the permission and size limits of `schema:infer`. Nothing is stored; a
    client uses it to show the rows of a format it cannot read itself."""
    config = keeper.schema_inference
    service.domain_of(domain_id)
    if not request.headers.get("content-type", "").startswith(MULTIPART):
        raise service.invalid("/body", "must be multipart/form-data with files")
    count = service.preview_rows(rows, config)
    files, _, records_path = await _uploads(request, config.max_files + 1, config.max_file_bytes)
    previews = await service.preview_files(
        files, config, store_settings(settings).profiling, count, records_path
    )
    annotate(
        request,
        action="schema.preview",
        target=domain_id,
        source=",".join(sorted({config.format_of(upload.name) for upload in files})),
        files=len(previews),
        rows=count,
    )
    return PreviewOut(
        domain=domain_id,
        files=[
            FilePreviewOut(
                name=item.name,
                columns=list(item.columns),
                rows=item.rows,
                row_count=item.row_count,
                truncated=item.truncated,
            )
            for item in previews
        ],
    )
