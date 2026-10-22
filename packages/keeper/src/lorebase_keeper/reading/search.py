"""Search over the materialized head (design 8.4).

- lexical: `search @@ websearch_to_tsquery(q)`, ranked by `ts_rank_cd`, with typo tolerance:
  each word of `q` the branch does not hold is corrected to its closest words in the branch's
  lexicon (`lorebase_store.reads.search_corrections`, `search.trigram_threshold` and
  `search.typo_*`) and searched as `(word | correction | ...)`. Hits of `q` as written come
  first; the others follow at `search.trigram_weight` times their rank. The response lists
  the corrections;
- semantic: cosine distance between the caller's `query_vector` and the stored embedding of
  each Record's object hash for the configured model (Keeper never computes vectors);
- hybrid: reciprocal rank fusion of both candidate lists, computed in SQL:
  `w_lex / (k + rank_lex) + w_sem / (k + rank_sem)`.

Filters (`kinds`, `where`) apply before ranking and fusion. Snippets come from `ts_headline`
over the title and the searchable Attributes.

Lexical search without `where` goes through the store primitive `search_records` (design
8.7): at most `store.reads.search_candidates` matches are ranked, through the security
definer function `lb_search_corrected`, so its index plans hold under row-level security;
snippets are computed for the returned page only. Filtered, semantic and hybrid searches are
compiled here.
"""

from __future__ import annotations

from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    ARRAY,
    Boolean,
    ColumnElement,
    Float,
    Select,
    Table,
    Text,
    and_,
    case,
    cast,
    false,
    func,
    literal,
    not_,
    null,
    or_,
    over,
    select,
    true,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind
from lorebase_core.model.embedding import TemplateError, kind_template
from lorebase_core.query import QueryError, Target, condition_kinds, resolve_where
from lorebase_keeper.reading.compiler import Compiler, json_text
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.problems import invalid, query_problem, tree_unsupported
from lorebase_keeper.reading.records import compiler_for
from lorebase_keeper.reading.requests import SearchMode, SearchRequest
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import Masking, condition_fields, require_comparable
from lorebase_keeper.reading.views import (
    SearchCorrection,
    SearchHit,
    SearchResponse,
    kind_name,
    served,
)
from lorebase_store.models import metadata
from lorebase_store.reads import Correction, TypoTolerance, search_corrections, search_records

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
        # Default: every Kind the caller may read; Kinds named explicitly must be readable.
        kinds = tuple(
            sorted(
                (k for k in point.schema.kinds.values() if point.scope.allows(k.api_name)),
                key=lambda k: k.id,
            )
        )
        return Target("search", kinds)
    point.scope.require(request.kinds)
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
    if mode in ("lexical", "hybrid"):
        target = _indexed_readable(point, target)
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
        point.scope.require(condition_kinds(where))
        require_comparable(
            point.schema, point.masking, condition_fields(where, target.kinds), "/body/where"
        )

    compiler = compiler_for(point, runtime)
    corrections: list[Correction] = []
    if request.q is not None and mode in ("lexical", "hybrid"):
        corrections = await search_corrections(
            session,
            point.domain.id,
            point.layout,
            request.q,
            typo_tolerance(runtime),
            kind_ids=list(target.kind_ids),
            runtime=runtime.store,
        )
    if mode == "lexical" and where is None:
        assert request.q is not None
        return await _lexical_page(
            session, runtime, compiler, point, target, request.q, limit, corrections
        )
    r = compiler.records("r")
    base = [compiler.record_scope(r, target)]
    if where is not None:
        base.append(compiler.condition(r, where, target))

    lists: list[tuple[Select[Any], float]] = []
    if mode in ("lexical", "hybrid"):
        assert request.q is not None
        lists.append(
            (
                _lexical(compiler, r, base, request.q, corrections, runtime),
                settings.hybrid.lexical_weight,
            )
        )
    if mode in ("semantic", "hybrid"):
        assert request.query_vector is not None
        lists.append(
            (
                _semantic(
                    r, [*base, _embedded_readable(r, point, target)], request.query_vector, runtime
                ),
                settings.hybrid.semantic_weight,
            )
        )
    ranked = lists[0][0].subquery("ranked") if len(lists) == 1 else _fused(lists, runtime)

    final = point.layout.records("hit")
    snippet: ColumnElement[Any] = literal(None)
    if request.q is not None:
        snippet = func.ts_headline(
            compiler.regconfig(),
            func.concat_ws(
                " ",
                _snippet_title(final, target, point.masking),
                *_searchable_text(final, target, point.masking),
            ),
            _corrected(compiler, request.q, corrections),
            literal(settings.headline_options),
        )
    # A lexical list puts the hits of q as written first; fusion ranks by position already.
    exact_first = [ranked.c.exact.desc()] if "exact" in ranked.c else []
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
        .order_by(*exact_first, ranked.c.score.desc(), ranked.c.kind_id, ranked.c.key)
        .limit(limit)
    )
    rows = (await session.execute(query)).mappings()
    items = [
        SearchHit(
            kind=kind_name(point.schema, row["kind_id"]),
            key=row["key"],
            title=point.masking.title(point.schema, row["kind_id"], row["title"]),
            snippet=row["snippet"],
            score=float(row["score"]),
        )
        for row in rows
    ]
    return SearchResponse(
        **served(point), mode=mode, items=items, corrections=_applied(corrections)
    )


def typo_tolerance(runtime: ReadRuntime) -> TypoTolerance:
    """Typo tolerance of lexical search from the `search` settings."""
    settings = runtime.search
    return TypoTolerance(
        threshold=settings.trigram_threshold,
        weight=settings.trigram_weight,
        candidates=settings.typo_candidates,
        min_length=settings.typo_min_length,
    )


def _applied(corrections: list[Correction]) -> list[SearchCorrection]:
    return [
        SearchCorrection(word=c.word, replacements=list(c.replacements))
        for c in corrections
        if c.replacements
    ]


def _corrected(compiler: Compiler, q: str, corrections: list[Correction]) -> ColumnElement[Any]:
    """The tsquery of q with each corrected word widened to `(word | replacement | ...)`, as
    the store's search does (`lb_lexeme_query`, revision 0018)."""
    query = compiler.tsquery(q)
    for correction in corrections:
        if not correction.replacements:
            continue
        word = literal(correction.word, Text)
        alternatives = literal(list(correction.replacements), ARRAY(Text))
        query = func.ts_rewrite(
            query, func.lb_lexeme_query(word), func.lb_lexeme_query(word, alternatives)
        )
    return query


async def _lexical_page(
    session: AsyncSession,
    runtime: ReadRuntime,
    compiler: Compiler,
    point: ReadPoint,
    target: Target,
    q: str,
    limit: int,
    corrections: list[Correction],
) -> SearchResponse:
    """Lexical search without filters: the store primitive ranks a bounded candidate set
    (index plans under row-level security too), then snippets for the page by key."""
    settings = runtime.search
    hits = await search_records(
        session,
        point.domain.id,
        point.layout,
        q,
        limit=limit,
        kind_ids=list(target.kind_ids),
        corrections=corrections,
        correction_weight=settings.trigram_weight,
        runtime=runtime.store,
    )
    snippets: dict[tuple[str, str], str | None] = {}
    if hits:
        hit = point.layout.records("hit")
        headline = func.ts_headline(
            compiler.regconfig(),
            func.concat_ws(
                " ",
                _snippet_title(hit, target, point.masking),
                *_searchable_text(hit, target, point.masking),
            ),
            _corrected(compiler, q, corrections),
            literal(settings.headline_options),
        )
        query = select(hit.c.kind_id, hit.c.key, headline.label("snippet")).where(
            hit.c.domain_id == literal(point.domain.id),
            hit.c.branch == literal(point.branch),
            tuple_(hit.c.kind_id, hit.c.key).in_([(h.kind_id, h.key) for h in hits]),
        )
        for row in (await session.execute(query)).mappings():
            snippets[(row["kind_id"], row["key"])] = row["snippet"]
    items = [
        SearchHit(
            kind=kind_name(point.schema, h.kind_id),
            key=h.key,
            title=point.masking.title(point.schema, h.kind_id, h.title),
            snippet=snippets.get((h.kind_id, h.key)),
            score=h.score,
        )
        for h in hits
    ]
    return SearchResponse(
        **served(point), mode="lexical", items=items, corrections=_applied(corrections)
    )


def _lexical(
    compiler: Compiler,
    r: Any,
    base: list[ColumnElement[bool]],
    q: str,
    corrections: list[Correction],
    runtime: ReadRuntime,
) -> Select[Any]:
    """Lexical candidates with an `exact` column: matches of q as written first, then those
    found only through a correction at `trigram_weight` times their rank."""
    settings = runtime.search
    tsquery = compiler.tsquery(q)
    exact: ColumnElement[bool] = r.c.search.op("@@", return_type=Boolean)(tsquery)
    score: ColumnElement[Any] = func.ts_rank_cd(r.c.search, tsquery)
    match: ColumnElement[bool] = exact
    if any(c.replacements for c in corrections):
        fixed = _corrected(compiler, q, corrections)
        match = or_(exact, r.c.search.op("@@", return_type=Boolean)(fixed))
        score = case(
            (exact, score),
            else_=literal(settings.trigram_weight, Float) * func.ts_rank_cd(r.c.search, fixed),
        )
    else:
        exact = true()
    flag = func.coalesce(exact, False)
    return (
        select(r.c.kind_id, r.c.key, cast(score, Float).label("score"), flag.label("exact"))
        .where(*base, match)
        .order_by(not_(flag), score.desc(), r.c.kind_id, r.c.key)
        .limit(settings.candidates)
    )


def _indexed_readable(point: ReadPoint, target: Target) -> Target:
    """The Kinds whose full-text index the caller may search. The index of a branch holds the
    words of the Attributes its schema leaves untagged; when the default branch tagged one of
    them after the branch forked (a searchable or title Attribute), its words are still
    indexed there, so only callers cleared for it search that Kind."""
    readable = []
    for kind in target.kinds:
        stale = [
            a
            for a in kind.attributes.values()
            if (a.searchable or a.id == kind.title_attribute)
            and not a.is_sensitive
            and point.masking.hidden(a, (kind.id,))
        ]
        if not stale:
            readable.append(kind)
    return Target(target.name, tuple(readable), target.trait)


def _embedded_readable(r: Any, point: ReadPoint, target: Target) -> ColumnElement[bool]:
    """Kinds whose vectors embed only values the caller may read: a Kind that embeds a
    sensitive Attribute (`search.embedding_sensitive`) ranks by it only for cleared callers."""
    readable = []
    for kind in target.kinds:
        try:
            template = kind_template(kind)
        except TemplateError:
            template = None
        attributes = [] if template is None else template.attributes.values()
        if not any(
            point.masking.hidden(kind.attributes[a], (kind.id,))
            for a in attributes
            if a in kind.attributes
        ):
            readable.append(kind.id)
    return r.c.kind_id.in_(readable) if readable else false()


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
        exact_first = [inner.c.exact.desc()] if "exact" in inner.c else []
        position = over(
            func.row_number(),
            order_by=(*exact_first, inner.c.score.desc(), inner.c.kind_id, inner.c.key),
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


def _snippet_title(rows: Any, target: Target, masking: Masking) -> ColumnElement[Any]:
    """The stored title in snippets, except for Kinds whose title Attribute is sensitive (by
    the default branch's tags too): a branch forked before the tag may still store it."""
    hidden = [
        kind.id
        for kind in target.kinds
        if kind.title_attribute in kind.attributes
        and masking.sensitive(kind.attributes[kind.title_attribute])
    ]
    title: ColumnElement[Any] = rows.c.title
    if not hidden:
        return title
    return case((rows.c.kind_id.in_(hidden), null()), else_=title)


def _searchable_text(rows: Any, target: Target, masking: Masking) -> list[ColumnElement[Any]]:
    """Text of the snippets: searchable Attributes that are not sensitive (by the tags of the
    default branch too)."""
    attributes = sorted(
        {
            a.id
            for kind in target.kinds
            for a in kind.attributes.values()
            if a.searchable and not masking.sensitive(a)
        }
    )
    return [json_text(rows.c["values"], attribute) for attribute in attributes]
