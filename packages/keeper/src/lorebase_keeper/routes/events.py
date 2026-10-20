"""Change feed of a branch (`agents.feed`): Server-Sent Events and the polling fallback.

Both are history reads: they need `data.read` on the branch through a grant not narrowed to a
Kind, like the branch log.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from lorebase_keeper.agents.feed import (
    EVENT_STREAM,
    ChangesFeedPage,
    changes_since,
    kind_filter,
    parse_sequence,
    stream,
)
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading.caching import respond
from lorebase_keeper.reading.points import (
    REVISION_HEADER,
    branch_schema,
    open_branch,
    open_domain,
    open_revision,
    read_guard,
)
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.runtime import ReadRuntimeDep, ReadSessionDep, begin_read
from lorebase_keeper.routes.agents import AgentSettingsDep
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.repositories import OutboxRepository

router = APIRouter(tags=["agents"])

BASE = "/domains/{domain_id}"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks data.read for the branch."},
    404: {"model": Problem, "description": "Domain, branch or Revision not found."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}

DataRead = Annotated[ReadGuard, Depends(read_guard())]
Kinds = Annotated[
    list[str] | None,
    Query(description="Only events changing these Kinds (repeated or comma-separated)."),
]


@router.get(
    f"{BASE}/branches/{{branch:path}}/events",
    summary="Stream the Revisions of a branch (Server-Sent Events)",
    response_class=StreamingResponse,
    responses={
        **_ERRORS,
        200: {
            "content": {EVENT_STREAM: {}},
            "description": "`event: revision` messages whose `data` is a RevisionEventOut and "
            "whose `id` is the event sequence; comment lines as heartbeats.",
        },
    },
)
async def branch_events(
    domain_id: str,
    branch: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    settings: AgentSettingsDep,
    request: Request,
    kinds: Kinds = None,
    last_event_id: Annotated[
        str | None,
        Query(description="Resume after this event id (for clients that cannot set headers)."),
    ] = None,
    last_event_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Revisions committed on the branch from now on, or after `Last-Event-ID`."""
    async with runtime.sessions() as session, session.begin():
        await begin_read(session, runtime)
        domain = await open_domain(session, access)
        ref = await open_branch(session, access, domain, branch)
        wanted = kind_filter(await branch_schema(session, runtime, domain.id, ref.name), kinds)
        start = parse_sequence(last_event_header or last_event_id)
        if start is None:
            start = await OutboxRepository(session).latest(domain.id)
    return StreamingResponse(
        stream(
            runtime,
            domain.id,
            ref.name,
            start,
            wanted,
            settings.feed,
            request.is_disconnected,
        ),
        media_type=EVENT_STREAM,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            REVISION_HEADER: ref.head.hex(),
        },
    )


@router.get(
    f"{BASE}/branches/{{branch:path}}/changes",
    summary="Revisions of a branch since a Revision",
    response_model=ChangesFeedPage,
    responses={
        **_ERRORS,
        304: {"description": "Not modified: If-None-Match names the current ETag."},
        409: {"model": Problem, "description": "`since` is not reachable from the head."},
    },
)
async def branch_changes(
    domain_id: str,
    branch: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    settings: AgentSettingsDep,
    request: Request,
    since: Annotated[
        str | None,
        Query(description="Revision id or tag; default: the latest Revisions."),
    ] = None,
    kinds: Kinds = None,
    limit: Annotated[int | None, Query(ge=1, description="Page size.")] = None,
) -> Response:
    """First-parent Revisions after `since`, oldest first; continue with `next_since`."""
    domain = await open_domain(session, access)
    ref = await open_branch(session, access, domain, branch)
    bounds = settings.feed.changes
    effective = bounds.default_limit if limit is None else limit
    if effective > bounds.max_limit:
        raise invalid("/query/limit", f"must not exceed {bounds.max_limit}", "limit_exceeded")
    since_row = None if since is None else await open_revision(session, access, domain, since)
    wanted = kind_filter(await branch_schema(session, runtime, domain.id, ref.name), kinds)

    async def produce() -> ChangesFeedPage:
        return await changes_since(
            session,
            runtime,
            domain.id,
            ref.name,
            ref.head,
            since_row,
            limit=effective,
            wanted=wanted,
            settings=settings.feed,
        )

    return await respond(
        request,
        runtime.cache,
        domain_id=domain.id,
        revision=ref.head.hex(),
        produce=produce,
        headers={REVISION_HEADER: ref.head.hex()},
        variant="" if since_row is None else since_row.id.hex(),
    )
