"""Revisions, log and diff API (design 6.3, 6.4, 6.7, 11.1).

Revisions are named by id (hex), tag or branch (its head). Callers whose grants are narrowed
to branches read the branches they are granted and Revisions reachable from their heads
(`services.reads`).
`GET .../diff` pages through the changes between two Revisions (`format=ndjson` streams all of
them as JSON lines, after a summary line); changes are expanded to Attribute level for Records
and Links and to property level for schema elements.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.tree import kind_partition, relation_partition
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading.caching import respond
from lorebase_keeper.reading.cursors import fingerprint, invalid_cursor, issue, position_of
from lorebase_keeper.reading.diff import (
    ChangeOut,
    HashChange,
    PartitionSummary,
    as_hash_change,
    compute_diff,
    expand,
    summarize,
)
from lorebase_keeper.reading.history import (
    PartitionCount,
    RevisionOut,
    log,
    partition_counts,
    revision_out,
)
from lorebase_keeper.reading.points import (
    REVISION_HEADER,
    open_branch,
    open_domain,
    open_revision,
    read_guard,
    schema_at,
)
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.runtime import (
    ReadRuntime,
    ReadRuntimeDep,
    ReadSessionDep,
    begin_read,
)
from lorebase_keeper.reading.sensitivity import (
    Disclosure,
    Masking,
    default_schema,
    masking_for,
)
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.repositories import ChangesetRepository, RevisionRepository, RevisionRow
from lorebase_store.repositories.scope import scope_to_domain

router = APIRouter(tags=["revisions"])

BASE = "/domains/{domain_id}"
NDJSON = "application/x-ndjson"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {
        "model": Problem,
        "description": "The caller lacks data.read for the branch or Revision read.",
    },
    404: {"model": Problem, "description": "Domain, branch or Revision not found."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}

DataRead = Annotated[ReadGuard, Depends(read_guard())]
_NOT_MODIFIED: dict[int | str, dict[str, Any]] = {
    304: {"description": "Not modified: If-None-Match names the current ETag."}
}
Limit = Annotated[int | None, Query(ge=1, description="Page size.")]
Cursor = Annotated[str | None, Query(max_length=4096, description="Opaque page cursor.")]


class LogPage(BaseModel):
    branch: str
    head: str
    items: list[RevisionOut]
    next_cursor: str | None = None


class CountsResponse(BaseModel):
    revision: str
    partitions: list[PartitionCount]


class ChangesPage(BaseModel):
    """A page of element changes with the summary of all of them."""

    from_revision: str | None = Field(description="Older side; null for a root Revision.")
    to_revision: str
    total: int
    summary: list[PartitionSummary]
    items: list[ChangeOut]
    next_cursor: str | None = None


def _bounded(limit: int | None, default: int, maximum: int) -> int:
    effective = default if limit is None else limit
    if effective > maximum:
        raise invalid("/query/limit", f"must not exceed {maximum}", "limit_exceeded")
    return effective


@router.get(
    f"{BASE}/branches/{{branch:path}}/log",
    summary="History of a branch",
    response_model=LogPage,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def branch_log(
    domain_id: str,
    branch: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    all_parents: Annotated[
        bool, Query(alias="all", description="Every ancestor instead of the first-parent chain.")
    ] = False,
    origin_type: Annotated[str | None, Query(description="Only Revisions of this origin.")] = None,
    author: Annotated[str | None, Query(description="Only Revisions by this principal.")] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """Revisions of a branch, newest first (first parents by default)."""
    domain = await open_domain(session, access)
    ref = await open_branch(session, access, domain, branch)
    bounds = runtime.read.log
    effective = _bounded(limit, bounds.default_limit, bounds.max_limit)
    shape = fingerprint("log", branch, all_parents, origin_type, author)
    position = position_of(cursor, shape, "/query/cursor")
    after = None
    if position is not None:
        try:
            after = (int(position[0]), bytes.fromhex(position[1]))
        except (IndexError, TypeError, ValueError):
            raise invalid_cursor("/query/cursor") from None

    async def produce() -> LogPage:
        items, more = await log(
            session,
            domain.id,
            ref.head,
            first_parent=not all_parents,
            origin_type=origin_type,
            author=author,
            limit=effective,
            after=after,
        )
        next_cursor = issue(shape, [items[-1].generation, items[-1].id]) if more and items else None
        return LogPage(branch=ref.name, head=ref.head.hex(), items=items, next_cursor=next_cursor)

    return await _answer(request, runtime, domain.id, ref.head.hex(), produce)


@router.get(
    f"{BASE}/revisions/{{revision}}",
    summary="Get a Revision",
    response_model=RevisionOut,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def get_revision(
    domain_id: str,
    revision: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
) -> Response:
    """A Revision by id, tag or branch name."""
    domain = await open_domain(session, access)
    found = await open_revision(session, access, domain, revision)

    async def produce() -> RevisionOut:
        return revision_out(found)

    return await _answer(request, runtime, domain.id, found.id.hex(), produce)


@router.get(
    f"{BASE}/revisions/{{revision}}/counts",
    summary="Element counts per partition at a Revision",
    response_model=CountsResponse,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def revision_counts(
    domain_id: str,
    revision: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
) -> Response:
    """Schema elements, Records per Kind and Links per Relation at a Revision."""
    domain = await open_domain(session, access)
    found = await open_revision(session, access, domain, revision)

    async def produce() -> CountsResponse:
        schema = await schema_at(session, runtime, domain_id, found)
        return CountsResponse(
            revision=found.id.hex(),
            partitions=await partition_counts(session, runtime, domain_id, found, schema),
        )

    return await _answer(request, runtime, domain.id, found.id.hex(), produce)


@router.get(
    f"{BASE}/revisions/{{revision}}/changeset",
    summary="Changes of a Revision against its first parent",
    response_model=ChangesPage,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def revision_changeset(
    domain_id: str,
    revision: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """The stored changeset (design 6.3), with provenance per changed Attribute."""
    domain = await open_domain(session, access)
    found = await open_revision(session, access, domain, revision)
    parent = await _first_parent(session, domain_id, found)
    new_schema = await schema_at(session, runtime, domain_id, found)
    old_schema = (
        new_schema if parent is None else await schema_at(session, runtime, domain_id, parent)
    )
    masking = (await _masking(session, access, runtime, domain.id)).over(new_schema, old_schema)

    async def produce() -> ChangesPage:
        entries = await ChangesetRepository(
            session, domain_id, writes=runtime.store_settings.writes
        ).entries(found.id)
        changes = [
            HashChange(e.partition, e.key, e.old_hash, e.new_hash, dict(e.provenance) or None)
            for e in entries
        ]
        page = await _page(
            session,
            runtime,
            domain_id,
            changes,
            old_schema,
            new_schema,
            masking,
            limit=limit,
            cursor=cursor,
            shape=fingerprint("changeset", found.id.hex()),
        )
        return ChangesPage(
            from_revision=None if parent is None else parent.id.hex(),
            to_revision=found.id.hex(),
            **page,
        )

    disclosure = Disclosure(session, domain.id, (new_schema, old_schema), masking, runtime.vault)
    return await _answer(
        request, runtime, domain.id, found.id.hex(), produce, masking=masking, disclosure=disclosure
    )


@router.get(
    f"{BASE}/diff",
    summary="Diff two Revisions",
    response_model=ChangesPage,
    responses={**_ERRORS, **_NOT_MODIFIED, 200: {"content": {NDJSON: {}}}},
)
async def diff(
    domain_id: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    from_: Annotated[str, Query(alias="from", description="Revision id, tag or branch.")],
    to: Annotated[str, Query(description="Revision id, tag or branch.")],
    kinds: Annotated[
        list[str] | None, Query(description="Kind api names (repeated or comma-separated).")
    ] = None,
    relations: Annotated[
        list[str] | None, Query(description="Relation api names (repeated or comma-separated).")
    ] = None,
    schema: Annotated[
        bool | None,
        Query(description="Include schema changes; default true unless filters are given."),
    ] = None,
    format: Annotated[Literal["json", "ndjson"], Query()] = "json",
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """Element changes from `from` to `to`, with a per-partition summary."""
    domain = await open_domain(session, access)
    old = await open_revision(session, access, domain, from_)
    new = await open_revision(session, access, domain, to)
    old_schema = await schema_at(session, runtime, domain_id, old)
    new_schema = await schema_at(session, runtime, domain_id, new)
    masking = (await _masking(session, access, runtime, domain.id)).over(new_schema, old_schema)

    async def changes_between() -> tuple[list[HashChange], set[str] | None]:
        partitions = _partitions((new_schema, old_schema), _names(kinds), _names(relations), schema)
        entries = await compute_diff(
            session, runtime, domain_id, old.root, new.root, partitions=partitions
        )
        return [as_hash_change(e) for e in entries], partitions

    if format == "ndjson":
        changes, _ = await changes_between()
        return StreamingResponse(
            _stream(runtime, domain_id, changes, old_schema, new_schema, masking, old, new),
            media_type=NDJSON,
            headers={REVISION_HEADER: new.id.hex()},
        )

    async def produce() -> ChangesPage:
        changes, partitions = await changes_between()
        page = await _page(
            session,
            runtime,
            domain_id,
            changes,
            old_schema,
            new_schema,
            masking,
            limit=limit,
            cursor=cursor,
            shape=fingerprint("diff", old.id.hex(), new.id.hex(), sorted(partitions or [])),
        )
        return ChangesPage(from_revision=old.id.hex(), to_revision=new.id.hex(), **page)

    return await _answer(
        request,
        runtime,
        domain.id,
        f"{old.id.hex()}..{new.id.hex()}",
        produce,
        headers={REVISION_HEADER: new.id.hex()},
        masking=masking,
        disclosure=Disclosure(session, domain.id, (new_schema, old_schema), masking, runtime.vault),
    )


async def _masking(
    session: AsyncSession, access: ReadGuard, runtime: ReadRuntime, domain_id: str
) -> Masking:
    """Masking of history reads, which are not tied to one branch: Domain-wide and
    Kind-narrowed clearances count."""
    reference = await default_schema(session, domain_id, runtime.store)
    return await masking_for(
        access.authorizer, access.principal, domain_id, reference, runtime.sensitivity
    )


async def _answer(
    request: Request,
    runtime: ReadRuntime,
    domain_id: str,
    revision: str,
    produce: Any,
    *,
    headers: dict[str, str] | None = None,
    masking: Masking | None = None,
    disclosure: Disclosure | None = None,
) -> Response:
    return await respond(
        request,
        runtime.cache,
        domain_id=domain_id,
        revision=revision,
        produce=produce,
        headers=headers or {REVISION_HEADER: revision},
        variant="" if masking is None else masking.variant,
        disclose=None if disclosure is None else disclosure.send,
    )


def _names(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [name.strip() for value in values for name in value.split(",") if name.strip()]


def _partitions(
    schemas: Sequence[Schema],
    kinds: list[str] | None,
    relations: list[str] | None,
    include_schema: bool | None,
) -> set[str] | None:
    if kinds is None and relations is None:
        return None if include_schema in (None, True) else _all_data(schemas)
    selected: set[str] = {"schema"} if include_schema else set()
    for index, name in enumerate(kinds or []):
        kind = next((s.kind_by_api_name(name) for s in schemas if s.kind_by_api_name(name)), None)
        if kind is None:
            raise invalid(f"/query/kinds/{index}", f"unknown Kind {name!r}", "unknown_kind")
        selected.add(kind_partition(kind.id))
    for index, name in enumerate(relations or []):
        found = next(
            (s.relation_by_api_name(name) for s in schemas if s.relation_by_api_name(name)), None
        )
        if found is None:
            raise invalid(
                f"/query/relations/{index}", f"unknown Relation {name!r}", "unknown_relation"
            )
        selected.add(relation_partition(found[0].id))
    return selected


def _all_data(schemas: Sequence[Schema]) -> set[str]:
    selected: set[str] = set()
    for schema in schemas:
        selected.update(kind_partition(k) for k in schema.kinds)
        selected.update(relation_partition(r) for r in schema.relations)
    return selected


async def _first_parent(session: Any, domain_id: str, revision: RevisionRow) -> RevisionRow | None:
    if not revision.parents:
        return None
    return await RevisionRepository(session, domain_id).require(revision.parents[0])


async def _page(
    session: Any,
    runtime: ReadRuntime,
    domain_id: str,
    changes: list[HashChange],
    old_schema: Schema,
    new_schema: Schema,
    masking: Masking,
    *,
    limit: int | None,
    cursor: str | None,
    shape: str,
) -> dict[str, Any]:
    bounds = runtime.read.diff.page
    effective = _bounded(limit, bounds.default_limit, bounds.max_limit)
    position = position_of(cursor, shape, "/query/cursor")
    start = 0
    if position is not None:
        if len(position) != 1 or not isinstance(position[0], int) or position[0] < 0:
            raise invalid_cursor("/query/cursor")
        start = position[0]
    window = changes[start : start + effective]
    items = await expand(
        session,
        runtime,
        domain_id,
        window,
        old_schema=old_schema,
        new_schema=new_schema,
        masking=masking,
    )
    end = start + len(window)
    return {
        "total": len(changes),
        "summary": summarize(changes, (new_schema, old_schema)),
        "items": items,
        "next_cursor": issue(shape, [end]) if end < len(changes) else None,
    }


async def _stream(
    runtime: ReadRuntime,
    domain_id: str,
    changes: list[HashChange],
    old_schema: Schema,
    new_schema: Schema,
    masking: Masking,
    old: RevisionRow,
    new: RevisionRow,
) -> AsyncIterator[bytes]:
    """JSON lines: one summary line, then one line per change, expanded batch by batch. Each
    batch has its vault references resolved as a JSON answer would (`Disclosure`)."""
    summary = {
        "type": "summary",
        "from_revision": old.id.hex(),
        "to_revision": new.id.hex(),
        "total": len(changes),
        "summary": [
            s.model_dump(mode="json") for s in summarize(changes, (new_schema, old_schema))
        ],
    }
    yield (json.dumps(summary) + "\n").encode()
    batch = runtime.read.diff.page.max_limit
    async with runtime.sessions() as session, session.begin():
        await begin_read(session, runtime)
        await scope_to_domain(session, domain_id)
        disclosure = Disclosure(
            session, domain_id, (new_schema, old_schema), masking, runtime.vault
        )
        for start in range(0, len(changes), batch):
            items = await expand(
                session,
                runtime,
                domain_id,
                changes[start : start + batch],
                old_schema=old_schema,
                new_schema=new_schema,
                masking=masking,
            )
            lines = (
                json.dumps({"type": "change", **item.model_dump(mode="json", exclude_none=True)})
                for item in items
            )
            yield await disclosure.send(("\n".join(lines) + "\n").encode(), NDJSON)
