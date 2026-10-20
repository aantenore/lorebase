"""Schema inference (design 9.1, 11.1): `POST /domains/{d}/schema:infer`, permission
`schema.write`.

The body is either `multipart/form-data` with CSV or TSV files in `files` (and optionally an
`options` field holding a JSON object of `InferenceOptions` overrides), or JSON naming an allowed
Postgres source: `{"postgres": {"dsn": "secret://env/ERP_DSN", "schema": "public", "tables":
[...]}, "options": {...}}`. The answer is the proposal: the schema (JSON and YAML), the evidence
of every element and the issues to settle; with `Accept: text/yaml` or `format=yaml`, only the
YAML document. Nothing is committed.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile

from lorebase_core.infer import SchemaProposal
from lorebase_core.model import dump_schema
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.deps import KeeperSettingsDep, SettingsDep
from lorebase_keeper.errors import BAD_REQUEST, Problem, ProblemError
from lorebase_keeper.services import schema_inference as service
from lorebase_store.settings import database_settings, store_settings

router = APIRouter(tags=["schema"])

YAML_MEDIA_TYPES = ("text/yaml", "application/yaml", "application/x-yaml")
MULTIPART = "multipart/form-data"
#: Form fields besides the files: `options`.
_MAX_FORM_FIELDS = 1

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
                            "description": "CSV or TSV files; the file name is the table name.",
                        },
                        "options": {
                            "type": "string",
                            "description": "JSON object of InferenceOptions overrides.",
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
                        "options": {"type": "object", "description": "InferenceOptions overrides."},
                    },
                    "required": ["postgres"],
                }
            },
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


class InferFromSource(BaseModel):
    """JSON body: a Postgres source and optional option overrides."""

    postgres: PostgresSourceIn
    options: dict[str, Any] = Field(default_factory=dict)


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


async def _uploads(
    request: Request, max_files: int, max_part_size: int
) -> tuple[list[service.UploadedFile], dict[str, Any]]:
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
        return files, _options_field(form.get("options"))
    finally:
        await form.close()


async def _json_source(request: Request) -> InferFromSource:
    try:
        return InferFromSource.model_validate(await request.json())
    except ValueError as error:
        if isinstance(error, ValidationError):
            first = error.errors()[0]
            pointer = "/body/" + "/".join(str(p) for p in first["loc"])
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
    summary="Propose a schema from tables or CSV files",
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
    """Profile the uploaded files or the Postgres tables and propose Kinds, typed Attributes,
    keys and Relations with evidence. Deterministic and model-free; nothing is written."""
    config = keeper.schema_inference
    profiling = store_settings(settings).profiling
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(MULTIPART):
        # One file over the limit still parses, so the answer names the limit (422); the
        # parser refuses bodies beyond that (400).
        files, overrides = await _uploads(request, config.max_files + 1, config.max_file_bytes)
        options = service.merged_options(config, overrides, "/body/options")
        proposal = await service.infer_from_files(domain_id, files, config, profiling.csv, options)
        source = "csv"
    else:
        body = await _json_source(request)
        options = service.merged_options(config, body.options, "/body/options")
        proposal = await service.infer_from_postgres(
            domain_id,
            service.PostgresSource(
                body.postgres.dsn, body.postgres.schema_, tuple(body.postgres.tables)
            ),
            config,
            database_settings(settings),
            profiling.postgres,
            options,
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
