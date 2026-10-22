"""Health reports (design 14): how sound the data of a Domain, or of one Kind, is.

Computed on request from the materialized head of a branch and kept in the response cache under
the Revision they describe, so asking again costs nothing until the branch moves. Callers with
Kind-narrowed grants get a report limited to those Kinds. `format=markdown` renders the report
for pasting into a prompt.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from lorebase_keeper.agents.context import JSON, MARKDOWN
from lorebase_keeper.errors import CONFLICT, Problem, ProblemError, ValidationProblem
from lorebase_keeper.kindhealth import markdown, report
from lorebase_keeper.kindhealth.models import DomainHealthOut, KindHealthOut
from lorebase_keeper.reading.caching import cache_key, respond_with
from lorebase_keeper.reading.points import REVISION_HEADER, ReadPoint, read_guard, resolve_point
from lorebase_keeper.reading.records import require_kind
from lorebase_keeper.reading.runtime import ReadRuntimeDep, ReadSessionDep
from lorebase_keeper.reading.views import served
from lorebase_keeper.services.reads import ReadGuard

router = APIRouter(tags=["health"])

BASE = "/domains/{domain_id}"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks data.read for the branch."},
    404: {"model": Problem, "description": "Domain, branch or Kind not found."},
    409: {"model": Problem, "description": "The branch has no materialized head."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
    503: {"model": Problem, "description": "The scan exceeded its statement timeout."},
}

DataRead = Annotated[ReadGuard, Depends(read_guard())]
Branch = Annotated[
    str | None, Query(description="Branch to examine; default: the Domain's default branch.")
]
Format = Annotated[
    Literal["json", "markdown"], Query(description="`markdown` renders the report as text.")
]


def _materialized(point: ReadPoint) -> None:
    if not point.materialized:
        raise ProblemError(CONFLICT, "Health reports need the materialized head of the branch.")


@router.get(
    f"{BASE}/health",
    summary="Health of a Domain",
    responses={
        **_ERRORS,
        200: {
            "model": DomainHealthOut,
            "content": {JSON: {}, MARKDOWN: {}},
            "description": "The report in JSON, or markdown with `format=markdown`.",
        },
        304: {"description": "Not modified: If-None-Match names the current ETag."},
    },
)
async def domain_health(
    domain_id: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    format: Format = "json",
) -> Response:
    """Per Kind: Records, Links by Relation, constraint violations against the current schema,
    required-relation gaps, orphan Links, duplicates on unique and title Attributes, stale
    derived values and the last change; for the Kinds the caller may read."""
    point = await resolve_point(session, runtime, access, branch=branch, at=None)
    _materialized(point)

    async def produce() -> tuple[bytes, str]:
        found = await report.scan_branch(
            session,
            point,
            runtime.read.health,
            report.visible_kinds(point),
            report.visible_relations(point),
        )
        out = DomainHealthOut(
            **served(point),
            domain=point.domain.id,
            branch=point.branch,
            summary=found.summary,
            kinds=found.kinds,
            relations=found.relations,
        )
        if format == "markdown":
            return markdown.domain_markdown(out).encode(), MARKDOWN
        return out.model_dump_json().encode(), JSON

    return await respond_with(
        request,
        runtime.cache,
        cache_key(request, point.domain.id, point.revision_hex, point.variant),
        produce,
        headers={REVISION_HEADER: point.revision_hex},
    )


@router.get(
    f"{BASE}/kinds/{{kind}}/health",
    summary="Health of a Kind",
    responses={
        **_ERRORS,
        200: {
            "model": KindHealthOut,
            "content": {JSON: {}, MARKDOWN: {}},
            "description": "The report in JSON, or markdown with `format=markdown`.",
        },
        304: {"description": "Not modified: If-None-Match names the current ETag."},
    },
)
async def kind_health(
    domain_id: str,
    kind: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    format: Format = "json",
) -> Response:
    """The checks of the Domain report for one Kind, with the Relations it takes part in."""
    point = await resolve_point(session, runtime, access, branch=branch, at=None)
    found_kind = require_kind(point, kind)
    _materialized(point)

    async def produce() -> tuple[bytes, str]:
        found = await report.scan_branch(
            session,
            point,
            runtime.read.health,
            [found_kind],
            report.visible_relations(point, found_kind),
        )
        out = KindHealthOut(
            **served(point),
            domain=point.domain.id,
            branch=point.branch,
            summary=found.summary,
            health=found.kinds[0],
            relations=report.with_direction(found.relations, found_kind),
        )
        if format == "markdown":
            return markdown.kind_markdown(out).encode(), MARKDOWN
        return out.model_dump_json().encode(), JSON

    return await respond_with(
        request,
        runtime.cache,
        cache_key(request, point.domain.id, point.revision_hex, point.variant),
        produce,
        headers={REVISION_HEADER: point.revision_hex},
    )
