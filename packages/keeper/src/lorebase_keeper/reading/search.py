"""Search over the materialized head (design 8.4).

- lexical: `search @@ websearch_to_tsquery(q)` or trigram similarity of the title (`%`, with
  `pg_trgm.similarity_threshold` from settings), ranked by `ts_rank_cd` plus the weighted title
  similarity;
- semantic: cosine distance between the caller's `query_vector` and the stored embedding of
  each Record's object hash for the configured model (Keeper never computes vectors);
- hybrid: reciprocal rank fusion of both candidate lists, computed in SQL:
  `w_lex / (k + rank_lex) + w_sem / (k + rank_sem)`.

Filters (`kinds`, `where`) apply before ranking and fusion. Snippets come from `ts_headline`
over the title and the searchable Attributes.
"""

from __future__ import annotations

from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    Boolean,
    ColumnElement,
    Float,
    Select,
    Table,
    and_,
    cast,
    func,
    literal,
    or_,
    over,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind
from lorebase_core.query import QueryError, Target, resolve_where
from lorebase_keeper.reading.compiler import Compiler, json_text
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.problems import invalid, query_problem, tree_unsupported
from lorebase_keeper.reading.records import compiler_for
from lorebase_keeper.reading.requests import SearchMode, SearchRequest
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.views import SearchHit, SearchResponse, kind_name, served
from lorebase_store.models import metadata
from lorebase_store.repositories.materialized import MAT_RECORDS

EMBEDDINGS: Table = metadata.tables["embeddings"]


def search_mode(request: SearchRequest) -> SearchMode:
    """Requested mode, checked against the inputs it needs; 422 otherwise."""
    has_text, has_vector = request.q is not None, request.query_vector is not None
    mode = request.mode
    if mode is None:
        if has_text and has_vector:
            return "hybrid"
        if has_vector:
            return "semantic"
        if has_text:
            return "lexical"
        raise invalid("/body/q", "give q, query_vector or both", "missing")
    if mode in ("lexical", "hybrid") and not has_text:
        raise invalid("/body/q", f"{mode} search needs q", "missing")
    if mode in ("semantic", "hybrid") and not has_vector:
        raise invalid("/body/query_vector", f"{mode} search needs query_vector", "missing")
    return mode


def _target(point: ReadPoint, request: SearchRequest) -> Target:
    if request.kinds is None:
        kinds = tuple(sorted(point.schema.kinds.values(), key=lambda k: k.id))
        return Target("search", kinds)
    found: list[Kind] = []
    for index, name in enumerate(request.kinds):
        kind = point.schema.kind_by_api_name(name)
        if kind is None:
            raise invalid(f"/body/kinds/{index}", f"unknown Kind {name!r}", "unknown_kind")
        found.append(kind)
    if not found:
        raise invalid("/body/kinds", "name at least one Kind", "missing")
    return Target(found[0].api_name if len(found) == 1 else "search", tuple(found))


async def search(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, request: SearchRequest
) -> SearchResponse:
    if not point.materialized:
        raise tree_unsupported("Search")
    settings = runtime.search
    mode = search_mode(request)
    limit = request.limit or settings.results.default_limit
    if limit > settings.results.max_limit:
        raise invalid(
            "/body/limit", f"must not exceed {settings.results.max_limit}", "limit_exceeded"
        )
    dims = runtime.store_settings.embeddings.dims
    if request.query_vector is not None and len(request.query_vector) != dims:
        raise invalid(
            "/body/query_vector",
            f"has {len(request.query_vector)} dimensions; the configured model "
            f"{settings.semantic.model!r} has {dims}",
            "dimension_mismatch",
        )
    target = _target(point, request)
    if not target.kinds:
        return SearchResponse(**served(point), mode=mode, items=[])
    where = None
    if request.where is not None:
        if len(target.kinds) != 1:
            raise invalid("/body/where", "filters need exactly one Kind in kinds", "ambiguous")
        try:
            where = resolve_where(
                request.where, target, point.schema, runtime.read.query_limits(), "/where"
            )
        except QueryError as error:
            raise query_problem(error, "/body") from None

    compiler = compiler_for(point, runtime)
    r = compiler.records("r")
    base = [compiler.record_scope(r, target)]
    if where is not None:
        base.append(compiler.condition(r, where, target))

    lists: list[tuple[Select[Any], float]] = []
    if mode in ("lexical", "hybrid"):
        assert request.q is not None
        await session.execute(
            select(
                func.set_config(
                    "pg_trgm.similarity_threshold", str(settings.trigram_threshold), True
                )
            )
        )
        lists.append(
            (_lexical(compiler, r, base, request.q, runtime), settings.hybrid.lexical_weight)
        )
    if mode in ("semantic", "hybrid"):
        assert request.query_vector is not None
        lists.append(
            (
                _semantic(r, base, request.query_vector, runtime),
                settings.hybrid.semantic_weight,
            )
        )
    ranked = lists[0][0].subquery("ranked") if len(lists) == 1 else _fused(lists, runtime)

    final = MAT_RECORDS.alias("hit")
    snippet: ColumnElement[Any] = literal(None)
    if request.q is not None:
        snippet = func.ts_headline(
            compiler.regconfig(),
            func.concat_ws(" ", final.c.title, *_searchable_text(final, target)),
            compiler.tsquery(request.q),
            literal(settings.headline_options),
        )
    query = (
        select(
            ranked.c.kind_id, ranked.c.key, ranked.c.score, final.c.title, snippet.label("snippet")
        )
        .select_from(ranked)
        .join(
            final,
            and_(
                final.c.domain_id == literal(point.domain.id),
                final.c.branch == literal(point.branch),
                final.c.kind_id == ranked.c.kind_id,
                final.c.key == ranked.c.key,
            ),
        )
        .order_by(ranked.c.score.desc(), ranked.c.kind_id, ranked.c.key)
        .limit(limit)
    )
    rows = (await session.execute(query)).mappings()
    items = [
        SearchHit(
            kind=kind_name(point.schema, row["kind_id"]),
            key=row["key"],
            title=row["title"],
            snippet=row["snippet"],
            score=float(row["score"]),
        )
        for row in rows
    ]
    return SearchResponse(**served(point), mode=mode, items=items)


def _lexical(
    compiler: Compiler, r: Any, base: list[ColumnElement[bool]], q: str, runtime: ReadRuntime
) -> Select[Any]:
    settings = runtime.search
    tsquery = compiler.tsquery(q)
    score = func.ts_rank_cd(r.c.search, tsquery) + literal(settings.trigram_weight, Float) * (
        func.similarity(func.coalesce(r.c.title, ""), literal(q))
    )
    return (
        select(r.c.kind_id, r.c.key, cast(score, Float).label("score"))
        .where(
            *base,
            or_(
                r.c.search.op("@@", return_type=Boolean)(tsquery),
                r.c.title.op("%", return_type=Boolean)(literal(q)),
            ),
        )
        .order_by(score.desc(), r.c.kind_id, r.c.key)
        .limit(settings.candidates)
    )


def _semantic(
    r: Any, base: list[ColumnElement[bool]], vector: list[float], runtime: ReadRuntime
) -> Select[Any]:
    dims = runtime.store_settings.embeddings.dims
    e = EMBEDDINGS.alias("e")
    distance = cast(e.c.embedding, VECTOR(dims)).op("<=>", return_type=Float)(
        literal(vector, VECTOR(dims))
    )
    return (
        select(r.c.kind_id, r.c.key, (literal(1.0, Float) - distance).label("score"))
        .join(
            e,
            and_(
                e.c.domain_id == r.c.domain_id,
                e.c.object_hash == r.c.object_hash,
                e.c.model == literal(runtime.search.semantic.model),
                e.c.dims == literal(dims, literal_execute=True),
            ),
        )
        .where(*base)
        .order_by(distance, r.c.kind_id, r.c.key)
        .limit(runtime.search.candidates)
    )


def _fused(lists: list[tuple[Select[Any], float]], runtime: ReadRuntime) -> Any:
    """Reciprocal rank fusion of the ranked lists (full outer join on the Record)."""
    k = runtime.search.hybrid.rrf_k
    ranked = []
    for index, (candidates, weight) in enumerate(lists):
        inner = candidates.subquery(f"c{index}")
        position = over(
            func.row_number(),
            order_by=(inner.c.score.desc(), inner.c.kind_id, inner.c.key),
        )
        ranked.append(
            (
                select(inner.c.kind_id, inner.c.key, position.label("rank")).subquery(f"l{index}"),
                weight,
            )
        )
    (first, w_first), (second, w_second) = ranked
    score = func.coalesce(literal(w_first, Float) / (literal(k) + first.c["rank"]), 0.0) + (
        func.coalesce(literal(w_second, Float) / (literal(k) + second.c["rank"]), 0.0)
    )
    return (
        select(
            func.coalesce(first.c.kind_id, second.c.kind_id).label("kind_id"),
            func.coalesce(first.c.key, second.c.key).label("key"),
            cast(score, Float).label("score"),
        )
        .select_from(
            first.join(
                second,
                and_(first.c.kind_id == second.c.kind_id, first.c.key == second.c.key),
                full=True,
            )
        )
        .subquery("ranked")
    )


def _searchable_text(rows: Any, target: Target) -> list[ColumnElement[Any]]:
    attributes = sorted(
        {a.id for kind in target.kinds for a in kind.attributes.values() if a.searchable}
    )
    return [json_text(rows.c["values"], attribute) for attribute in attributes]
