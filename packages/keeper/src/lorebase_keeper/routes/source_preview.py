"""Preview of the expression mappings of a candidate Source (design 5.9, 11.1).

| Route | Permission |
|-------|------------|
| `POST /domains/{d}/sources:preview` | `schema.read` on the branch |

The body is a schema change set, as for `POST /schema:validate` (`yaml`, or `patch` and
`remove`; an empty `patch` previews the schema as saved), `source`, the api name of the Source in
the schema it produces, and `rows`, sample rows of the Source as the caller read them (column
name to value). The schema is validated like a schema change (`schema.expression_limits`
included), then every Attribute mapped by `expression` is evaluated on each row, as Scribe
evaluates it when it maps rows, and the value before the mapping's transforms is returned.

Rows come from the caller and nothing else is read: no Record, no connection, no secret. At
most `read.source_preview_max_rows` rows are accepted.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field, JsonValue

from lorebase_core.canonical import hex
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.runtime import ReadRuntimeDep
from lorebase_keeper.routes.derivations import PreviewErrorOut
from lorebase_keeper.routes.schema import ChangeSetBody
from lorebase_keeper.services import schema as schema_service
from lorebase_keeper.services import source_preview as service
from lorebase_keeper.services.derivation_preview import CandidateInvalidError
from lorebase_keeper.services.errors import violations_problem
from lorebase_keeper.services.reads import resolve_readable

router = APIRouter(tags=["sources"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks schema.read for the branch."},
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
        "At most `read.source_preview_max_rows`.",
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


@router.post(
    "/domains/{domain_id}/sources:preview",
    summary="Preview the expression mappings of a candidate Source",
    response_model=SourcePreviewOut,
    responses=_ERRORS,
)
async def preview_source(
    domain_id: str,
    payload: Annotated[SourcePreviewRequest, Body()],
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    runtime: ReadRuntimeDep,
) -> SourcePreviewOut:
    """Evaluate the `expression` mappings of a Source, as edited and not yet saved, on sample
    rows. Nothing is read from the Source and nothing is written."""
    maximum = runtime.read.source_preview_max_rows
    if len(payload.rows) > maximum:
        raise invalid("/rows", f"must not exceed {maximum}", "limit_exceeded")
    annotate(request, domain_id=domain_id, permission=Permission.SCHEMA_READ.value)
    target = await resolve_readable(
        store,
        auth.authorizer,
        principal,
        (Permission.SCHEMA_READ,),
        domain_id,
        branch=payload.branch,
        at=payload.at,
        max_depth=keeper.reads.reachability_max_depth,
    )
    current = await schema_service.read(store, domain_id, target)
    try:
        candidate = service.candidate_of(
            current, payload.to_input(), payload.source, store.runtime.schema.rules()
        )
    except CandidateInvalidError as error:
        raise violations_problem(error.issues, limit=keeper.schema_changes.max_issues) from None
    cells = service.evaluate(
        candidate,
        payload.rows,
        limits=store.runtime.schema.expression_limits,
        now=store.clock(),
    )
    kind = candidate.schema.kinds[candidate.source.target_kind]
    return SourcePreviewOut(
        domain=domain_id,
        branch=target.ref,
        revision=hex(target.revision),
        source=payload.source,
        target_kind=kind.api_name,
        attributes=list(service.expression_attributes(candidate)),
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
