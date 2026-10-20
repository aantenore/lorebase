"""Change feed of a branch: Revision events as Server-Sent Events, with a polling fallback.

Events are the `revision.committed` outbox events of the branch, described by
`lorebase_store.changefeed` (Revision metadata, changes per Kind and Relation, changed keys up
to `agents.feed.changed_keys_max`). The SSE `id` of an event is its outbox sequence, so a
client reconnecting with `Last-Event-ID` resumes after the last event it saw; without it the
stream starts with the next commit. The stream sends a comment line after `heartbeat_s` of
silence and closes after `max_connection_s`.

`GET .../changes?since=<revision>` reads the same events from the first-parent history of the
branch instead of the outbox: the Revisions after `since`, oldest first, a page at a time,
with `next_since` to continue. Use it to catch up after a gap or where streams are not
available.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.tree import kind_partition
from lorebase_keeper.agents.settings import FeedSettings
from lorebase_keeper.errors import CONFLICT, ProblemError
from lorebase_keeper.reading.points import branch_schema
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.runtime import ReadRuntime, begin_read
from lorebase_store.changefeed import EventRef, describe, first_parents, matches
from lorebase_store.commit.pipeline import COMMITTED_EVENT
from lorebase_store.repositories import OutboxRepository, RevisionRow, scope_to_domain

logger = logging.getLogger("lorebase.keeper.feed")

EVENT_STREAM = "text/event-stream"
SSE_EVENT = "revision"


class RevisionEventOut(BaseModel):
    """A Revision committed on a branch."""

    sequence: int | None = Field(
        default=None, description="Outbox sequence; the SSE id. Absent in `changes` pages."
    )
    type: str
    domain: str
    branch: str
    revision: str
    parents: list[str]
    generation: int
    author: str
    on_behalf_of: str | None = None
    message: str
    origin: dict[str, JsonValue]
    stats: dict[str, JsonValue]
    created_at: str
    kinds: dict[str, int] = Field(description="Changed Records per Kind api name.")
    relations: dict[str, int] = Field(description="Changed Links per Relation api name.")
    schema_changed: bool
    keys: dict[str, list[str]] = Field(description="Changed Record keys per Kind, capped.")
    keys_truncated: bool


class ChangesFeedPage(BaseModel):
    branch: str
    head: str
    since: str | None = None
    items: list[RevisionEventOut]
    next_since: str = Field(description="Pass as `since` to continue.")
    more: bool = Field(description="True when more Revisions follow `next_since`.")


@dataclass(frozen=True, slots=True)
class KindFilter:
    """Kinds an event must touch (None: every event); keys are listed for those Kinds only."""

    names: frozenset[str] | None = None
    partitions: frozenset[str] | None = None


def kind_filter(schema: Schema | None, values: Sequence[str] | None) -> KindFilter:
    """The filter of a `kinds` query parameter (repeated or comma-separated api names)."""
    if values is None:
        return KindFilter()
    names = [name.strip() for value in values for name in value.split(",") if name.strip()]
    partitions = set()
    for index, name in enumerate(names):
        kind = None if schema is None else schema.kind_by_api_name(name)
        if kind is None:
            raise invalid(f"/query/kinds/{index}", f"unknown Kind {name!r}", "unknown_kind")
        partitions.add(kind_partition(kind.id))
    return KindFilter(frozenset(names), frozenset(partitions))


def parse_sequence(value: str | None) -> int | None:
    """An event id given by the client (`Last-Event-ID`); 422 when it is not one."""
    if value is None or value == "":
        return None
    try:
        sequence = int(value)
    except ValueError:
        sequence = -1
    if sequence < 0:
        raise invalid("/headers/Last-Event-ID", "is not an event id", "invalid")
    return sequence


async def events_after(
    session: AsyncSession,
    runtime: ReadRuntime,
    domain_id: str,
    branch: str,
    after: int,
    wanted: KindFilter,
    settings: FeedSettings,
) -> tuple[int, list[dict[str, Any]]]:
    """Events of the branch after sequence `after` (one batch) and the last sequence read."""
    rows = await OutboxRepository(session).after(
        domain_id,
        after,
        event_types=(COMMITTED_EVENT,),
        branch=branch,
        limit=settings.batch_events,
    )
    if not rows:
        return after, []
    schema = await branch_schema(session, runtime, domain_id, branch)
    documents = await describe(
        session,
        domain_id,
        [EventRef(bytes.fromhex(str(r.payload["revision"])), branch, r.id) for r in rows],
        schemas={branch: schema},
        keys_max=settings.changed_keys_max,
        kind_partitions=wanted.partitions,
    )
    return rows[-1].id, [d for d in documents if matches(d, wanted.names)]


def sse(document: dict[str, Any]) -> bytes:
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return f"id: {document['sequence']}\nevent: {SSE_EVENT}\ndata: {data}\n\n".encode()


async def stream(
    runtime: ReadRuntime,
    domain_id: str,
    branch: str,
    start: int,
    wanted: KindFilter,
    settings: FeedSettings,
    disconnected: Any,
) -> AsyncIterator[bytes]:
    """The SSE body: events as they are committed, heartbeats, closed after the max time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.max_connection_s
    quiet_since = loop.time()
    cursor = start
    yield f"retry: {settings.retry_ms}\n\n".encode()
    while loop.time() < deadline:
        if await disconnected():
            return
        try:
            async with runtime.sessions() as session, session.begin():
                await begin_read(session, runtime)
                await scope_to_domain(session, domain_id)
                cursor, documents = await events_after(
                    session, runtime, domain_id, branch, cursor, wanted, settings
                )
        except Exception:
            logger.exception("change feed poll failed", extra={"domain": domain_id})
            return
        for document in documents:
            yield sse(document)
            quiet_since = loop.time()
        if loop.time() - quiet_since >= settings.heartbeat_s:
            yield b": keep-alive\n\n"
            quiet_since = loop.time()
        await asyncio.sleep(max(min(settings.poll_interval_s, deadline - loop.time()), 0))


async def changes_since(
    session: AsyncSession,
    runtime: ReadRuntime,
    domain_id: str,
    branch: str,
    head: bytes,
    since: RevisionRow | None,
    *,
    limit: int,
    wanted: KindFilter,
    settings: FeedSettings,
) -> ChangesFeedPage:
    """A page of the first-parent Revisions after `since` (see module doc)."""
    if since is None:
        segment = await first_parents(session, domain_id, head, floor=-1, max_steps=limit - 1)
        newer = [revision for revision, _ in reversed(segment)]
    else:
        segment = await first_parents(
            session, domain_id, head, floor=since.generation, max_steps=settings.max_scan
        )
        boundary, generation = segment[-1]
        if boundary != since.id:
            reason = (
                "is not on the first-parent history of the branch"
                if generation <= since.generation
                else f"is more than {settings.max_scan} Revisions behind the head"
            )
            raise ProblemError(
                CONFLICT,
                f"Revision {since.id.hex()} {reason}; read the current state and follow the "
                "branch from its head.",
                extensions={"code": "since_unreachable"},
            )
        newer = [revision for revision, _ in reversed(segment[:-1])]
    page = newer[:limit]
    schema = await branch_schema(session, runtime, domain_id, branch)
    documents = await describe(
        session,
        domain_id,
        [EventRef(revision, branch) for revision in page],
        schemas={branch: schema},
        keys_max=settings.changed_keys_max,
        kind_partitions=wanted.partitions,
    )
    fallback = head if since is None else since.id
    return ChangesFeedPage(
        branch=branch,
        head=head.hex(),
        since=None if since is None else since.id.hex(),
        items=[RevisionEventOut.model_validate(d) for d in documents if matches(d, wanted.names)],
        next_since=(page[-1] if page else fallback).hex(),
        more=len(newer) > limit,
    )
