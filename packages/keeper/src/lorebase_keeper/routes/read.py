"""Read API (design 8, 11.1): Records, Links, query, traversal, neighborhoods and search.

Every read takes `branch` (default: the Domain's default branch); Record and Link reads also
take `at`, a Revision id or tag, served from the Revision tree. Responses carry the Revision
they were served from in `revision` and in the `Lorebase-Revision` header, and `served_from`
(`materialized` or `tree`). Record reads carry an `ETag` and answer `If-None-Match` with 304.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import ValidationError

from lorebase_core.query import Query as DslQuery
from lorebase_core.query import QueryError, QueryPlan, resolve_query
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading import graph, records
from lorebase_keeper.reading.cursors import fingerprint
from lorebase_keeper.reading.points import REVISION_HEADER, ReadPoint, resolve_point
from lorebase_keeper.reading.problems import invalid, query_problem, request_problem
from lorebase_keeper.reading.requests import (
    NeighborhoodRequest,
    TraverseRequest,
)
from lorebase_keeper.reading.runtime import ReadRuntime, ReadRuntimeDep, ReadSessionDep
from lorebase_keeper.reading.views import (
    LinkPage,
    NeighborhoodResponse,
    RecordPage,
    RecordResponse,
    TraverseResponse,
    record_etag,
    served,
)

router = APIRouter(tags=["read"])

BASE = "/domains/{domain_id}"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks data.read in the Domain."},
    404: {"model": Problem, "description": "Domain, branch, Revision, Kind or Record not found."},
    422: {"model": ValidationProblem, "description": "Invalid request or query."},
}

DataRead = Annotated[Access, Depends(require(Permission.DATA_READ))]
KindRead = Annotated[Access, Depends(require(Permission.DATA_READ, kind_param="kind"))]
Branch = Annotated[
    str | None, Query(description="Branch to read; default: the Domain's default branch.")
]
At = Annotated[
    str | None,
    Query(description="Revision id or tag to read at (time travel, served from the tree)."),
]
Limit = Annotated[int | None, Query(ge=1, description="Page size.")]
Cursor = Annotated[str | None, Query(max_length=4096, description="Opaque page cursor.")]


def _mark(response: Response, point: ReadPoint) -> None:
    response.headers[REVISION_HEADER] = point.revision_hex


def _limit(runtime: ReadRuntime, limit: int | None) -> int:
    bounds = runtime.read.records
    effective = bounds.default_limit if limit is None else limit
    if effective > bounds.max_limit:
        raise invalid("/query/limit", f"must not exceed {bounds.max_limit}", "limit_exceeded")
    return effective


def _plan(query: DslQuery, point: ReadPoint, runtime: ReadRuntime, prefix: str) -> QueryPlan:
    try:
        return resolve_query(query, point.schema, runtime.read.query_limits())
    except QueryError as error:
        raise query_problem(error, prefix) from None


def _shape(query: DslQuery) -> str:
    return fingerprint("query", query.model_dump(mode="json", exclude={"cursor", "limit"}))


# Records ----------------------------------------------------------------------------------


@router.get(f"{BASE}/kinds/{{kind}}/records", summary="List Records of a Kind", responses=_ERRORS)
async def list_records(
    domain_id: str,
    kind: str,
    _access: KindRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    response: Response,
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
) -> RecordPage:
    """Records of a Kind filtered with the DSL, ordered and paginated by keyset."""
    point = await resolve_point(session, runtime, domain_id, branch=branch, at=at)
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
    plan = _plan(query, point, runtime, "/query")
    page = await records.run_query(
        session, runtime, point, plan, shape=_shape(query), cursor_pointer="/query/cursor"
    )
    _mark(response, point)
    return RecordPage(**served(point), items=page.items, next_cursor=page.next_cursor)


@router.get(
    f"{BASE}/kinds/{{kind}}/records/{{key}}/links/{{relation}}",
    summary="List the Links of a Record along a Relation",
    responses=_ERRORS,
)
async def list_record_links(
    domain_id: str,
    kind: str,
    key: str,
    relation: str,
    _access: KindRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    response: Response,
    branch: Branch = None,
    at: At = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> LinkPage:
    """Links of the Record along `relation`, a forward or inverse api name, by far key."""
    point = await resolve_point(session, runtime, domain_id, branch=branch, at=at)
    found = records.require_kind(point, kind)
    page = await records.record_links(
        session,
        runtime,
        point,
        found,
        key,
        relation,
        limit=_limit(runtime, limit),
        cursor=cursor,
        shape=fingerprint("record-links", kind, key, relation),
    )
    _mark(response, point)
    return LinkPage(**served(point), items=page.items, next_cursor=page.next_cursor)


@router.get(
    f"{BASE}/kinds/{{kind}}/records/{{key}}",
    summary="Get a Record by key",
    responses={**_ERRORS, 304: {"description": "Not modified (If-None-Match)."}},
)
async def get_record(
    domain_id: str,
    kind: str,
    key: str,
    _access: KindRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    response: Response,
    branch: Branch = None,
    at: At = None,
    links: Annotated[bool, Query(description="Add Link counts per Relation.")] = False,
    lineage: Annotated[bool, Query(description="Add the provenance of each value.")] = False,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Any:
    """One Record, optionally with its Link counts and the lineage of its values."""
    point = await resolve_point(session, runtime, domain_id, branch=branch, at=at)
    found = records.require_kind(point, kind)
    record, fetched = await records.get_record(
        session, runtime, point, found, key, links=links, lineage=lineage
    )
    etag = record_etag(fetched.object_hash, fetched.row.get("derived"))
    headers = {REVISION_HEADER: point.revision_hex, "ETag": etag}
    if if_none_match is not None and etag in {t.strip() for t in if_none_match.split(",")}:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return RecordResponse(**served(point), record=record)


@router.get(
    f"{BASE}/relations/{{relation}}/links",
    summary="List the Links of a Relation",
    responses=_ERRORS,
)
async def list_relation_links(
    domain_id: str,
    relation: str,
    _access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    response: Response,
    branch: Branch = None,
    at: At = None,
    from_key: Annotated[str | None, Query(description="Only Links from this key.")] = None,
    to_key: Annotated[str | None, Query(description="Only Links to this key.")] = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> LinkPage:
    """Links of a Relation (either api name) ordered by `(from_key, to_key)`."""
    point = await resolve_point(session, runtime, domain_id, branch=branch, at=at)
    page = await records.relation_links(
        session,
        runtime,
        point,
        relation,
        from_key=from_key,
        to_key=to_key,
        limit=_limit(runtime, limit),
        cursor=cursor,
        shape=fingerprint("relation-links", relation, from_key, to_key),
    )
    _mark(response, point)
    return LinkPage(**served(point), items=page.items, next_cursor=page.next_cursor)


# Traversal ---------------------------------------------------------------


@router.post(f"{BASE}/traverse", summary="Traverse Relations from a Record", responses=_ERRORS)
async def traverse(
    domain_id: str,
    body: TraverseRequest,
    _access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    response: Response,
) -> TraverseResponse:
    """Follow a path of Relations, with a filter and a cap per hop (design 8.3)."""
    point = await resolve_point(session, runtime, domain_id, branch=body.branch, at=None)
    result = await graph.traverse(session, runtime, point, body)
    _mark(response, point)
    return result


@router.post(f"{BASE}/neighborhood", summary="Neighborhood of seed Records", responses=_ERRORS)
async def neighborhood(
    domain_id: str,
    body: NeighborhoodRequest,
    _access: DataRead,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    response: Response,
) -> NeighborhoodResponse:
    """Breadth-first expansion from seed Records within depth, node and edge caps."""
    point = await resolve_point(session, runtime, domain_id, branch=body.branch, at=None)
    result = await graph.neighborhood(session, runtime, point, body)
    _mark(response, point)
    return result
