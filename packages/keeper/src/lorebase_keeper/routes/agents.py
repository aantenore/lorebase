"""Agent-facing reads (design 11): context packs and Record explanations.

`POST /context` answers in one call with the seed Records, their neighborhood, the schema
fragment, recent changes and lineage, trimmed to a token budget (`agents.context`). It is
authorized like every read (`services.reads`, Kind-scoped grants included) and cached by
Revision like GET reads: the `ETag` names the Revision and a digest of the request, and a
matching `If-None-Match` answers 304.

`GET .../records/{key}/explain` tells why a Record exists and why its values are what they
are (`agents.explain`), as JSON or markdown, cached like every GET read.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response

from lorebase_keeper.agents.context import JSON, MARKDOWN, PackContext, build_pack
from lorebase_keeper.agents.explain import ExplainOut, explain_markdown, explain_record
from lorebase_keeper.agents.pack import ContextPackOut, ContextRequest, dumps
from lorebase_keeper.agents.settings import AgentSettings, agent_settings
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.ratelimit.enforce import charge_context_tokens, check_context_quota
from lorebase_keeper.reading.caching import CacheKey, cache_key, entity_tag, respond_with
from lorebase_keeper.reading.cursors import fingerprint
from lorebase_keeper.reading.points import REVISION_HEADER, open_revision, read_guard, resolve_point
from lorebase_keeper.reading.records import require_kind
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
    await check_context_quota(request, access.principal)
    point = await resolve_point(session, runtime, access, branch=body.branch, at=body.at)
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
        point.variant,
    )
    context = PackContext(point=point, etag=entity_tag(key), since=since, floor=floor)

    async def produce() -> tuple[bytes, str]:
        return await build_pack(session, runtime, context, body, settings.context)

    response = await respond_with(
        request,
        runtime.cache,
        key,
        produce,
        time_travel=not point.materialized,
        headers={REVISION_HEADER: point.revision_hex},
        disclose=point.disclosure(session, runtime).send,
    )
    await charge_context_tokens(
        request, access.principal, response, settings.context.chars_per_token
    )
    return response


@router.get(
    f"{BASE}/kinds/{{kind}}/records/{{key}}/explain",
    summary="Explain a Record",
    responses={
        **_ERRORS,
        200: {
            "model": ExplainOut,
            "content": {JSON: {}, MARKDOWN: {}},
            "description": "The explanation in JSON, or markdown with `format=markdown`.",
        },
        304: {"description": "Not modified: If-None-Match names the current ETag."},
    },
)
async def explain(
    domain_id: str,
    kind: str,
    key: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    settings: AgentSettingsDep,
    request: Request,
    branch: Annotated[
        str | None, Query(description="Branch to read; default: the Domain's default branch.")
    ] = None,
    at: Annotated[
        str | None,
        Query(description="Revision id or tag to read at (time travel, served from a snapshot)."),
    ] = None,
    format: Annotated[Literal["json", "markdown"], Query()] = "json",
) -> Response:
    """Creation, per-Attribute lineage, Links with their origin and Proposals of a Record."""
    point = await resolve_point(session, runtime, access, branch=branch, at=at)
    found = require_kind(point, kind)

    async def produce() -> tuple[bytes, str]:
        document = await explain_record(session, runtime, point, found, key, settings.explain)
        if format == "markdown":
            return explain_markdown(document).encode(), MARKDOWN
        return dumps(document).encode(), JSON

    return await respond_with(
        request,
        runtime.cache,
        cache_key(request, point.domain.id, point.revision_hex, point.variant),
        produce,
        time_travel=not point.materialized,
        headers={REVISION_HEADER: point.revision_hex},
        disclose=point.disclosure(session, runtime).send,
    )
