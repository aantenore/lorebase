"""Preview of the expression mappings of a candidate Source (design 5.9, 11.1).

| Route | Permission |
|-------|------------|
| `POST /domains/{d}/sources:preview` | `schema.read` on the branch or the Source's target Kind |

The body is a schema change set, as for `POST /schema:validate` (`yaml`, or `patch` and
`remove`; an empty `patch` previews the schema as saved), `source`, the api name of the Source in
the schema it produces, and `rows`, sample rows of the Source as the caller read them (column
name to value). The schema is validated like a schema change (`schema.expression_limits`
included), then every Attribute mapped by `expression` is evaluated on each row, as Scribe
evaluates it when it maps rows, and the value before the mapping's transforms is returned.
Expressions are compiled once over every column the rows have: a column a row lacks is null.

Rows come from the caller and nothing else is read: no Record, no connection, no secret. At
most `read.source_preview_max_rows` rows of at most `read.source_preview_max_columns` columns
are accepted, and at most `read.source_preview_max_cells` expressions (rows times Attributes
mapped by an expression) are evaluated, off the event loop. A grant of `schema.read` narrowed
to Kinds is enough when it names the Source's target Kind (in the candidate and, when the Kind
exists there, in the schema read); otherwise the preview is refused (403) before the
candidate is validated.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field, JsonValue

from lorebase_core.model import Schema, Source
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading.points import ReadPoint, resolve_point
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.runtime import ReadRuntimeDep, ReadSessionDep
from lorebase_keeper.reading.settings import ReadSettings
from lorebase_keeper.routes.derivations import PreviewErrorOut
from lorebase_keeper.routes.schema import ChangeSetBody
from lorebase_keeper.services import source_preview as service
from lorebase_keeper.services.derivation_preview import CandidateInvalidError
from lorebase_keeper.services.errors import violations_problem
from lorebase_keeper.services.reads import ReadGuard

router = APIRouter(tags=["sources"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {
        "model": Problem,
        "description": "The caller lacks schema.read for the branch and for the Source's "
        "target Kind.",
    },
    404: {"model": Problem, "description": "Domain, branch or Revision not found."},
    422: {
        "model": ValidationProblem,
        "description": "Invalid request, or a candidate schema that fails validation (`issues` "
        "locate why).",
    },
}


class SourcePreviewRequest(ChangeSetBody):
    """A candidate schema change set, the Source to preview in it and sample rows."""

    source: str = Field(
        min_length=1, description="Api name of the Source in the schema the change set produces."
    )
    rows: list[dict[str, JsonValue]] = Field(
        min_length=1,
        description="Sample rows: column name to value (text for CSV, native values for JSON). "
        "At most `read.source_preview_max_rows`, each with at most "
        "`read.source_preview_max_columns` columns.",
    )
    branch: str | None = Field(default=None, description="Default: the default branch.")
    at: str | None = Field(default=None, description="Revision id, tag or branch name.")


class ExpressionCellOut(BaseModel):
    """The value of one expression mapping for one row, before the transforms."""

    value: JsonValue = Field(description="Null when there is none or `error` is set.")
    error: PreviewErrorOut | None = None


class SourcePreviewOut(BaseModel):
    """Values of the expression mappings of a candidate Source on sample rows."""

    domain: str
    branch: str | None
    revision: str = Field(description="Revision whose schema the change set was applied to.")
    source: str
    target_kind: str
    attributes: list[str] = Field(description="Attributes mapped by an expression, by api name.")
    rows: list[dict[str, ExpressionCellOut]] = Field(
        description="One entry per row sent: the cell of each Attribute in `attributes`."
    )


def schema_reader() -> Callable[..., Awaitable[ReadGuard]]:
    """Dependency: the authenticated reader of the Domain named by the path; `schema.read` is
    checked once the read point is resolved."""

    async def dependency(
        request: Request, principal: PrincipalDep, auth: AuthDep, keeper: KeeperSettingsDep
    ) -> ReadGuard:
        domain_id = str(request.path_params["domain_id"])
        annotate(request, domain_id=domain_id, permission=Permission.SCHEMA_READ.value)
        return ReadGuard(
            authorizer=auth.authorizer,
            principal=principal,
            permissions=(Permission.SCHEMA_READ,),
            domain_id=domain_id,
            max_depth=keeper.reads.reachability_max_depth,
        )

    return dependency


SchemaReader = Annotated[ReadGuard, Depends(schema_reader())]


def _bound_rows(rows: list[dict[str, JsonValue]], expressions: int, read: ReadSettings) -> None:
    """422 for more rows, columns or cells than the settings allow."""
    if len(rows) > read.source_preview_max_rows:
        raise invalid("/rows", f"must not exceed {read.source_preview_max_rows}", "limit_exceeded")
    for index, row in enumerate(rows):
        if len(row) > read.source_preview_max_columns:
            raise invalid(
                f"/rows/{index}",
                f"must not have more than {read.source_preview_max_columns} columns",
                "limit_exceeded",
            )
    if len(rows) * expressions > read.source_preview_max_cells:
        raise invalid(
            "/rows",
            f"{len(rows)} rows times {expressions} expression mappings exceed "
            f"{read.source_preview_max_cells} cells",
            "limit_exceeded",
        )


def _target_kind_check(point: ReadPoint) -> Callable[[Schema, Source | None], None]:
    """403 unless the caller may read the Source's target Kind: always with grants that are
    not narrowed to Kinds; with Kind-narrowed grants, when they name it in the candidate and,
    if the Kind exists there, in the schema read (a rename in the change set moves nothing)."""

    def check(candidate: Schema, source: Source | None) -> None:
        if point.scope.kinds is None:
            return
        kind = None if source is None else candidate.kinds.get(source.target_kind)
        if kind is None:
            raise point.scope.guard.denied()
        names = {kind.api_name}
        stored = point.schema.kinds.get(kind.id)
        if stored is not None:
            names.add(stored.api_name)
        if point.scope.invisible and not all(point.scope.allows(n) for n in names):
            # Like a Source that does not exist: nothing tells which Kind is missing.
            raise point.scope.guard.denied()
        point.scope.require(names)

    return check


@router.post(
    "/domains/{domain_id}/sources:preview",
    summary="Preview the expression mappings of a candidate Source",
    response_model=SourcePreviewOut,
    responses=_ERRORS,
)
async def preview_source(
    domain_id: str,
    payload: Annotated[SourcePreviewRequest, Body()],
    access: SchemaReader,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
) -> SourcePreviewOut:
    """Evaluate the `expression` mappings of a Source, as edited and not yet saved, on sample
    rows. Nothing is read from the Source and nothing is written."""
    _bound_rows(payload.rows, 0, runtime.read)
    point = await resolve_point(session, runtime, access, branch=payload.branch, at=payload.at)
    try:
        candidate = service.candidate_of(
            point.schema,
            payload.to_input(),
            payload.source,
            store.runtime.schema.rules(),
            authorize=_target_kind_check(point),
        )
    except CandidateInvalidError as error:
        raise violations_problem(error.issues, limit=keeper.schema_changes.max_issues) from None
    attributes = service.expression_attributes(candidate)
    _bound_rows(payload.rows, len(attributes), runtime.read)
    cells = await anyio.to_thread.run_sync(
        partial(
            service.evaluate,
            candidate,
            payload.rows,
            limits=store.runtime.schema.expression_limits,
            now=store.clock(),
        )
    )
    kind = candidate.schema.kinds[candidate.source.target_kind]
    return SourcePreviewOut(
        domain=domain_id,
        branch=point.branch,
        revision=point.revision_hex,
        source=payload.source,
        target_kind=kind.api_name,
        attributes=list(attributes),
        rows=[
            {
                attribute: ExpressionCellOut(
                    value=cell.value,
                    error=None
                    if cell.error_code is None
                    else PreviewErrorOut(code=cell.error_code, message=cell.error_message),
                )
                for attribute, cell in row.items()
            }
            for row in cells
        ],
    )
