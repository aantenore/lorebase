"""Read API (design 8, 11.1): Records, Links, query, traversal, neighborhoods, search and
resolution of words to schema and values.

Every read takes `branch` (default: the Domain's default branch) and `at`, a Revision id or
tag, served from the Revision tree (key reads and listings ordered by key) or from a snapshot
of the Revision (everything else, `reading.snapshots`). Responses carry the Revision they were
served from in `revision` and in the `Lorebase-Revision` header, and `served_from`
(`materialized` or `tree`). GET reads carry an `ETag` bound to that Revision, answer
`If-None-Match` with 304 and are cached per Revision (`reading.caching`); at a branch head
the 304 is decided from the head Revision alone (`unchanged_head`), before the read point is
resolved. POST reads carry an `ETag` too.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.query import Query as DslQuery
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading import graph, record_history, records, resolve, search
from lorebase_keeper.reading.caching import CACHE_CONTROL, JSON, respond
from lorebase_keeper.reading.cursors import fingerprint
from lorebase_keeper.reading.points import (
    REVISION_HEADER,
    ReadPoint,
    read_guard,
    resolve_point,
    unchanged_head,
)
from lorebase_keeper.reading.problems import invalid, request_problem
from lorebase_keeper.reading.record_history import RecordHistoryOut, RecordLineageOut
from lorebase_keeper.reading.requests import (
    AT,
    NeighborhoodRequest,
    QueryRequest,
    ResolveRequest,
    SearchRequest,
    TraverseRequest,
)
from lorebase_keeper.reading.runtime import ReadRuntime, ReadRuntimeDep, ReadSessionDep
from lorebase_keeper.reading.views import (
    LinkPage,
    NeighborhoodResponse,
    QueryResponse,
    RecordPage,
    RecordResponse,
    ResolveResponse,
    SearchResponse,
    TraverseResponse,
    served,
)
from lorebase_keeper.services.reads import ReadGuard

router = APIRouter(tags=["read"])

BASE = "/domains/{domain_id}"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {
        "model": Problem,
        "description": "The caller lacks data.read for the branch or Revision read.",
    },
    404: {"model": Problem, "description": "Domain, branch, Revision, Kind or Record not found."},
    422: {"model": ValidationProblem, "description": "Invalid request or query."},
}

DataRead = Annotated[ReadGuard, Depends(read_guard())]
Branch = Annotated[
    str | None, Query(description="Branch to read; default: the Domain's default branch.")
]
At = Annotated[
    str | None,
    Query(description=AT),
]
Limit = Annotated[int | None, Query(ge=1, description="Page size.")]
Cursor = Annotated[str | None, Query(max_length=4096, description="Opaque page cursor.")]


_NOT_MODIFIED: dict[int | str, dict[str, Any]] = {
    304: {"description": "Not modified: If-None-Match names the current ETag."}
}


def _mark(response: Response, point: ReadPoint, body: BaseModel) -> None:
    """Headers of a POST read: Revision, and an ETag of the Revision and the request body."""
    digest = fingerprint(body.model_dump(mode="json"), point.variant)
    response.headers[REVISION_HEADER] = point.revision_hex
    response.headers["ETag"] = f'"{point.revision_hex}.{digest}"'
    response.headers["Cache-Control"] = CACHE_CONTROL


async def _answer(
    request: Request, runtime: ReadRuntime, point: ReadPoint, produce: Any, session: AsyncSession
) -> Response:
    return await respond(
        request,
        runtime.cache,
        domain_id=point.domain.id,
        revision=point.revision_hex,
        produce=produce,
        time_travel=not point.materialized,
        headers={REVISION_HEADER: point.revision_hex},
        variant=point.variant,
        disclose=point.disclosure(session, runtime).send,
    )


async def _sent(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, body: BaseModel, out: BaseModel
) -> Response:
    """A POST read's response: `out` with its vault references resolved, and the headers of
    `_mark`."""
    content = await point.disclosure(session, runtime).model(out)
    response = Response(content, media_type=JSON)
    _mark(response, point, body)
    return response


def _limit(runtime: ReadRuntime, limit: int | None) -> int:
    bounds = runtime.read.records
    effective = bounds.default_limit if limit is None else limit
    if effective > bounds.max_limit:
        raise invalid("/query/limit", f"must not exceed {bounds.max_limit}", "limit_exceeded")
    return effective


def _shape(query: DslQuery) -> str:
    return fingerprint("query", query.model_dump(mode="json", exclude={"cursor", "limit"}))


# Records ----------------------------------------------------------------------------------


@router.get(
    f"{BASE}/kinds/{{kind}}/records",
    summary="List Records of a Kind",
    response_model=RecordPage,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def list_records(
    domain_id: str,
    kind: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    at: At = None,
    where: Annotated[
        str | None, Query(description="DSL predicate as JSON (design 8.2).", max_length=65536)
    ] = None,
    order_by: Annotated[
        str | None, Query(description="Comma-separated `name[:asc|desc]` terms.")
    ] = None,
    select: Annotated[str | None, Query(description="Comma-separated field names.")] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """Records of a Kind filtered with the DSL, ordered and paginated by keyset."""
    if at is None and (
        unchanged := await unchanged_head(session, runtime, access, request, branch=branch)
    ):
        return unchanged
    point = await resolve_point(session, runtime, access, branch=branch, at=at)
    records.require_kind(point, kind)
    document: dict[str, Any] = {"kind": kind, "limit": _limit(runtime, limit), "cursor": cursor}
    if where:
        try:
            document["where"] = json.loads(where)
        except ValueError:
            raise invalid("/query/where", "is not valid JSON") from None
    if order_by:
        terms = []
        for term in order_by.split(","):
            name, _, direction = term.strip().partition(":")
            terms.append({"attr": name, "dir": direction or "asc"})
        document["order_by"] = terms
    if select:
        document["select"] = [name.strip() for name in select.split(",") if name.strip()]
    try:
        query = DslQuery.model_validate(document)
    except ValidationError as error:
        raise request_problem(error, "/query") from None
    plan = records.plan_query(query, point, runtime, "/query")

    async def produce() -> RecordPage:
        page = await records.run_query(
            session, runtime, point, plan, shape=_shape(query), cursor_pointer="/query/cursor"
        )
        return RecordPage(**served(point), items=page.items, next_cursor=page.next_cursor)

    return await _answer(request, runtime, point, produce, session)


@router.get(
    f"{BASE}/kinds/{{kind}}/records/{{key}}/links/{{relation}}",
    summary="List the Links of a Record along a Relation",
    response_model=LinkPage,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def list_record_links(
    domain_id: str,
    kind: str,
    key: str,
    relation: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    at: At = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """Links of the Record along `relation`, a forward or inverse api name, by far key."""
    if at is None and (
        unchanged := await unchanged_head(session, runtime, access, request, branch=branch)
    ):
        return unchanged
    point = await resolve_point(session, runtime, access, branch=branch, at=at)
    found = records.require_kind(point, kind)
    effective = _limit(runtime, limit)

    async def produce() -> LinkPage:
        page = await records.record_links(
            session,
            runtime,
            point,
            found,
            key,
            relation,
            limit=effective,
            cursor=cursor,
            shape=fingerprint("record-links", kind, key, relation),
        )
        return LinkPage(**served(point), items=page.items, next_cursor=page.next_cursor)

    return await _answer(request, runtime, point, produce, session)


@router.get(
    f"{BASE}/kinds/{{kind}}/records/{{key}}",
    summary="Get a Record by key",
    response_model=RecordResponse,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def get_record(
    domain_id: str,
    kind: str,
    key: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    at: At = None,
    links: Annotated[bool, Query(description="Add Link counts per Relation.")] = False,
    lineage: Annotated[bool, Query(description="Add the provenance of each value.")] = False,
) -> Response:
    """One Record, optionally with its Link counts and the lineage of its values."""
    if at is None and (
        unchanged := await unchanged_head(session, runtime, access, request, branch=branch)
    ):
        return unchanged
    point = await resolve_point(session, runtime, access, branch=branch, at=at)
    found = records.require_kind(point, kind)

    async def produce() -> RecordResponse:
        record, _ = await records.get_record(
            session, runtime, point, found, key, links=links, lineage=lineage
        )
        return RecordResponse(**served(point), record=record)

    return await _answer(request, runtime, point, produce, session)


@router.get(
    f"{BASE}/kinds/{{kind}}/records/{{key}}/history",
    summary="History of a Record",
    response_model=RecordHistoryOut,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def get_record_history(
    domain_id: str,
    kind: str,
    key: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    at: At = None,
    values: Annotated[
        bool, Query(description="Add the values before and after each change.")
    ] = False,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """Revisions that changed the Record on the branch, newest first, with the Attributes each
    changed and their provenance, from the branch history; with `at`, the changes up to that
    Revision."""
    if at is None and (
        unchanged := await unchanged_head(session, runtime, access, request, branch=branch)
    ):
        return unchanged
    point = await resolve_point(session, runtime, access, branch=branch, at=at)
    found = records.require_kind(point, kind)
    bounds = runtime.read.history
    effective = bounds.default_limit if limit is None else limit
    if effective > bounds.max_limit:
        raise invalid("/limit", f"must not exceed {bounds.max_limit}", "limit_exceeded")

    async def produce() -> RecordHistoryOut:
        return await record_history.record_history(
            session,
            runtime,
            point,
            found,
            key,
            limit=effective,
            cursor=cursor,
            values=values,
            shape=fingerprint("record-history", kind, key, values),
        )

    return await _answer(request, runtime, point, produce, session)


@router.get(
    f"{BASE}/kinds/{{kind}}/records/{{key}}/lineage",
    summary="Lineage of a Record",
    response_model=RecordLineageOut,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def get_record_lineage(
    domain_id: str,
    kind: str,
    key: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    at: At = None,
) -> Response:
    """Where each value of the Record came from: provenance, Revision, Source, Operation,
    extraction evidence and Proposal. At the branch head, or at `at` (from a snapshot)."""
    if at is None and (
        unchanged := await unchanged_head(session, runtime, access, request, branch=branch)
    ):
        return unchanged
    point = await resolve_point(session, runtime, access, branch=branch, at=at)
    found = records.require_kind(point, kind)

    async def produce() -> RecordLineageOut:
        return await record_history.record_lineage(
            session,
            runtime,
            point,
            found,
            key,
        )

    return await _answer(request, runtime, point, produce, session)


@router.get(
    f"{BASE}/relations/{{relation}}/links",
    summary="List the Links of a Relation",
    response_model=LinkPage,
    responses={**_ERRORS, **_NOT_MODIFIED},
)
async def list_relation_links(
    domain_id: str,
    relation: str,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    request: Request,
    branch: Branch = None,
    at: At = None,
    from_key: Annotated[str | None, Query(description="Only Links from this key.")] = None,
    to_key: Annotated[str | None, Query(description="Only Links to this key.")] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """Links of a Relation (either api name) ordered by `(from_key, to_key)`."""
    if at is None and (
        unchanged := await unchanged_head(session, runtime, access, request, branch=branch)
    ):
        return unchanged
    point = await resolve_point(session, runtime, access, branch=branch, at=at)
    effective = _limit(runtime, limit)

    async def produce() -> LinkPage:
        page = await records.relation_links(
            session,
            runtime,
            point,
            relation,
            from_key=from_key,
            to_key=to_key,
            limit=effective,
            cursor=cursor,
            shape=fingerprint("relation-links", relation, from_key, to_key),
        )
        return LinkPage(**served(point), items=page.items, next_cursor=page.next_cursor)

    return await _answer(request, runtime, point, produce, session)


# Query, traversal, search ---------------------------------------------------------------


@router.post(
    f"{BASE}/query",
    summary="Query Records with the DSL",
    responses=_ERRORS,
    response_model=QueryResponse,
)
async def query_records(
    domain_id: str,
    body: QueryRequest,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
) -> Response:
    """Run a DSL query (design 8.2): filters, Relation predicates, includes, aggregates."""
    point = await resolve_point(session, runtime, access, branch=body.branch, at=body.at)
    query = DslQuery.model_validate(body.model_dump(by_alias=True, exclude={"branch", "at"}))
    plan = records.plan_query(query, point, runtime, "/body")
    page = await records.run_query(
        session, runtime, point, plan, shape=_shape(query), cursor_pointer="/body/cursor"
    )
    out = QueryResponse(
        **served(point), items=page.items, next_cursor=page.next_cursor, aggregate=page.aggregate
    )
    return await _sent(session, runtime, point, body, out)


@router.post(
    f"{BASE}/traverse",
    summary="Traverse Relations from a Record",
    responses=_ERRORS,
    response_model=TraverseResponse,
)
async def traverse(
    domain_id: str,
    body: TraverseRequest,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
) -> Response:
    """Follow a path of Relations, with a filter and a cap per hop (design 8.3)."""
    point = await resolve_point(session, runtime, access, branch=body.branch, at=body.at)
    result = await graph.traverse(session, runtime, point, body)
    return await _sent(session, runtime, point, body, result)


@router.post(
    f"{BASE}/neighborhood",
    summary="Neighborhood of seed Records",
    responses=_ERRORS,
    response_model=NeighborhoodResponse,
)
async def neighborhood(
    domain_id: str,
    body: NeighborhoodRequest,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
) -> Response:
    """Breadth-first expansion from seed Records within depth, node and edge caps."""
    point = await resolve_point(session, runtime, access, branch=body.branch, at=body.at)
    result = await graph.neighborhood(session, runtime, point, body)
    return await _sent(session, runtime, point, body, result)


@router.post(
    f"{BASE}/search", summary="Search Records", responses=_ERRORS, response_model=SearchResponse
)
async def search_records(
    domain_id: str,
    body: SearchRequest,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
) -> Response:
    """Lexical, semantic (caller-supplied vector) or hybrid search (design 8.4)."""
    point = await resolve_point(session, runtime, access, branch=body.branch, at=body.at)
    result = await search.search(session, runtime, point, body)
    return await _sent(session, runtime, point, body, result)


@router.post(
    f"{BASE}/resolve",
    summary="Resolve words to schema elements and values",
    responses=_ERRORS,
    response_model=ResolveResponse,
)
async def resolve_words(
    domain_id: str,
    body: ResolveRequest,
    access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
) -> Response:
    """Ground the words of a text in the schema and the indexed values (design 8.9): ranked
    candidate Kinds, Attributes and values, each with the words it matched and, for values,
    a ready-to-use predicate. No query is run and nothing is interpreted beyond matching."""
    point = await resolve_point(session, runtime, access, branch=body.branch, at=body.at)
    result = await resolve.resolve(session, runtime, point, body)
    return await _sent(session, runtime, point, body, result)
