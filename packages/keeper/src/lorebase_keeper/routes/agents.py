"""Agent-facing reads (design 11): context packs.

`POST /context` answers in one call with the seed Records, their neighborhood, the schema
fragment, recent changes and lineage, trimmed to a token budget (`agents.context`). It is
authorized like every read (`services.reads`, Kind-scoped grants included) and cached by
Revision like GET reads: the `ETag` names the Revision and a digest of the request, and a
matching `If-None-Match` answers 304.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request, Response

from lorebase_keeper.agents.context import JSON, MARKDOWN, PackContext, build_pack
from lorebase_keeper.agents.pack import ContextPackOut, ContextRequest
from lorebase_keeper.agents.settings import AgentSettings, agent_settings
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading.caching import CacheKey, entity_tag, respond_with
from lorebase_keeper.reading.cursors import fingerprint
from lorebase_keeper.reading.points import REVISION_HEADER, open_revision, read_guard, resolve_point
from lorebase_keeper.reading.runtime import ReadRuntimeDep, ReadSessionDep
from lorebase_keeper.services.reads import ReadGuard
from lorebase_platform.settings import Settings

router = APIRouter(tags=["agents"])

BASE = "/domains/{domain_id}"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks data.read for what is read."},
    404: {"model": Problem, "description": "Domain, branch, Revision, Kind or Record not found."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}

DataRead = Annotated[ReadGuard, Depends(read_guard())]


def get_agent_settings(request: Request) -> AgentSettings:
    return agent_settings(cast(Settings, request.app.state.settings))


AgentSettingsDep = Annotated[AgentSettings, Depends(get_agent_settings)]


@router.post(
    f"{BASE}/context",
    summary="Context pack for an agent",
    responses={
        **_ERRORS,
        200: {
            "model": ContextPackOut,
            "content": {JSON: {}, MARKDOWN: {}},
            "description": "The pack in JSON, or markdown with `format: markdown`.",
        },
        304: {"description": "Not modified: If-None-Match names the current ETag."},
    },
)
async def context_pack(
    domain_id: str,
    body: ContextRequest,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    settings: AgentSettingsDep,
    request: Request,
) -> Response:
    """Seed Records, their neighborhood, schema, recent changes and lineage within a budget."""
    point = await resolve_point(session, runtime, access, branch=body.branch, at=None)
    since: str | None = None
    floor: int | None = None
    if body.since is not None and body.include_recent_changes:
        found = await open_revision(session, access, point.domain, body.since)
        since, floor = found.id.hex(), found.generation
    digest = fingerprint("context", body.model_dump(mode="json"), since)
    key = CacheKey(
        point.domain.id,
        point.revision_hex,
        request.url.path,
        (("body", digest),),
        point.scope.variant,
    )
    context = PackContext(point=point, etag=entity_tag(key), since=since, floor=floor)

    async def produce() -> tuple[bytes, str]:
        return await build_pack(session, runtime, context, body, settings.context)

    return await respond_with(
        request, runtime.cache, key, produce, headers={REVISION_HEADER: point.revision_hex}
    )
