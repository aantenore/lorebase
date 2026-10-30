"""Search over the materialized head (design 8.4).

- lexical: `search @@ websearch_to_tsquery(q)`, ranked by `ts_rank_cd`, with typo tolerance:
  each word of `q` the branch does not hold is corrected to its closest words in the branch's
  lexicon (`lorebase_store.reads.search_corrections`, `search.trigram_threshold` and
  `search.typo_*`) and searched as `(word | correction | ...)`. Hits of `q` as written come
  first; the others follow at `search.trigram_weight` times their rank. The response lists
  the corrections. When q as written finds fewer than `search.partial.min_results` Records,
  Records holding only some of its terms follow (`lorebase_store.reads.PartialMatching`:
  the terms joined by OR, negations and phrases kept, scored by `ts_rank_cd` plus
  `search.partial.term_bonus` per term held, corrections applied per term); each hit
  reports `match: all|partial`;
- semantic: cosine distance between the caller's `query_vector` and the stored embedding of
  each Record's object hash for the configured model and the fingerprint of its Kind's
  embedding template in the searched branch (Keeper never computes vectors);
- hybrid: reciprocal rank fusion of both candidate lists, computed in SQL:
  `w_lex / (k + rank_lex) + w_sem / (k + rank_sem)`.

Filters (`kinds`, `traits`, `lens`, `where`) apply before ranking and fusion. Snippets come
from `ts_headline` over the title and the searchable Attributes.

Filtered vector search (design 8.8): the filters stay in the statement of the semantic list,
before its `LIMIT`. With pgvector 0.8 or later and `search.semantic.iterative_scan: auto`, the
statement runs under `hnsw.iterative_scan = relaxed_order` (`lorebase_store.embeddings`) in a
materialized subquery that the outer query orders again by exact distance. Otherwise the
index subquery returns `search.candidates * search.semantic.overfetch_factor` nearest rows
(`hnsw.ef_search` is raised to that, up to pgvector's 1000, for the transaction) and the
outer query filters and trims them. A list shorter than `search.candidates` while
more Records match the filters (the scan bound was reached) is reported as
`semantic_truncated`.

Lexical search without `where` goes through the store primitive `search_records` (design
8.7), through the security definer function `lb_search_bounded`, so its index plans hold
under row-level security; snippets are computed for the returned page only. The word lexicon
bounds the matches of q (`lorebase_store.reads.CommonTerms`): within
`search.common_term_docs` every match is ranked; when every term is more common, only the
first `search.common_term_candidates` matches in index order are, and the response says
`truncated_candidates`. With `search.backend: bm25` the BM25 index of `pg_search` ranks it
instead (`lorebase_store.bm25`): every match is ranked by BM25, and the store keeps the
default ranking while the database has no valid BM25 index. Filtered, semantic and hybrid
searches are compiled here, with `ts_rank_cd`, whatever the backend.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pgvector.sqlalchemy import HALFVEC, VECTOR
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
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind
from lorebase_core.model.embedding import TemplateError, kind_template
from lorebase_core.query import QueryError, Target, condition_kinds, resolve_where
from lorebase_keeper.reading.compiler import Compiler, json_text
from lorebase_keeper.reading.lenses import resolve_lens, trait_kinds
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.problems import invalid, query_problem
from lorebase_keeper.reading.records import compiler_for
from lorebase_keeper.reading.requests import SearchMode, SearchRequest, SearchTuning
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import Masking, condition_fields, require_comparable
from lorebase_keeper.reading.snapshots import with_rows
from lorebase_keeper.reading.views import (
    SearchCorrection,
    SearchHit,
    SearchResponse,
    kind_name,
    served,
)
from lorebase_keeper.tracing.explain import explain
from lorebase_store.embeddings import (
    fingerprint_of,
    index_precision,
    iterative_scans,
    template_fingerprints,
    use_ef_search,
    use_iterative_scan,
)
from lorebase_store.models import metadata
from lorebase_store.reads import (
    CommonTerms,
    Correction,
    PartialMatching,
    PartialQuery,
    TypoTolerance,
    partial_terms,
    search_corrections,
    search_records,
)

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
    """The Kinds to search: those named by `kinds` and `traits`, else those of the lens, else
    every Kind the caller may read. A name in the request wins over the lens."""
    resolved = None if request.lens is None else resolve_lens(point, request.lens, "/body/lens")
    if request.kinds is None and request.traits is None:
        if resolved is not None and resolved.kinds is not None:
            return Target("search", resolved.kinds)
        # Default: every Kind the caller may read; Kinds named explicitly must be readable.
        kinds = tuple(
            sorted(
                (k for k in point.schema.kinds.values() if point.scope.allows(k.api_name)),
                key=lambda k: k.id,
            )
        )
        return Target("search", kinds)
    found: dict[str, Kind] = {}
    if request.kinds is not None:
        point.scope.precheck(request.kinds)
        for index, name in enumerate(request.kinds):
            kind = point.schema.kind_by_api_name(name)
            if kind is None:
                raise invalid(f"/body/kinds/{index}", f"unknown Kind {name!r}", "unknown_kind")
            found.setdefault(kind.id, kind)
        if not request.kinds and not request.traits:
            raise invalid("/body/kinds", "name at least one Kind", "missing")
    if request.traits is not None:
        for kind in trait_kinds(point, request.traits, "/body/traits"):
            found.setdefault(kind.id, kind)
    kinds_found = tuple(found.values())
    if len(kinds_found) == 1:
        return Target(kinds_found[0].api_name, kinds_found)
    return Target("search", kinds_found)


async def search(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, request: SearchRequest
) -> SearchResponse:
    runtime = tuned(runtime, request.tuning)
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

    point = await with_rows(session, runtime, point, "Search")
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
    partial = partial_matching(runtime)
    if mode == "lexical" and where is None:
        assert request.q is not None
        return await _lexical_page(
            session, runtime, compiler, point, target, request.q, limit, corrections, partial
        )

    def filters(rows: Any) -> list[ColumnElement[bool]]:
        found = [compiler.record_scope(rows, target)]
        if where is not None:
            found.append(compiler.condition(rows, where, target))
        return found

    r = compiler.records("r")
    base = filters(r)

    alternatives = None
    if request.q is not None and partial is not None and mode in ("lexical", "hybrid"):
        alternatives = await partial_terms(session, request.q, partial.stop_words)
    lists: list[tuple[Select[Any], float]] = []
    if mode in ("lexical", "hybrid"):
        assert request.q is not None
        lists.append(
            (
                _lexical(compiler, r, base, request.q, corrections, runtime, alternatives),
                settings.hybrid.lexical_weight,
            )
        )
    if mode in ("semantic", "hybrid"):
        assert request.query_vector is not None
        precision = await index_precision(session, dims)
        scan = await _semantic_scan(session, runtime)
        lists.append(
            (
                _semantic(
                    r,
                    [*base, _embedded_readable(r, point, target)],
                    request.query_vector,
                    runtime,
                    target,
                    point.domain.id,
                    _fingerprints(point, target),
                    half=precision == "half",
                    scan=scan,
                    filtered=where is not None,
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
            _corrected(
                compiler, request.q if alternatives is None else alternatives.text, corrections
            ),
            literal(settings.headline_options),
        )
    # A lexical list puts full matches first, then the hits of q as written; fusion ranks by
    # position already (its `full` is null for semantic-only hits, which DESC would put first).
    fused = len(lists) > 1
    exact_first = [] if fused else [ranked.c[c].desc() for c in ("full", "exact") if c in ranked.c]
    full = ranked.c.full if "full" in ranked.c else literal(None, Boolean)
    semantic_found: list[ColumnElement[Any]] = []
    if mode == "semantic":
        semantic_found = [over(func.count()).label("semantic_found")]
    elif mode == "hybrid":
        semantic_found = [over(func.count(ranked.c.semantic_rank)).label("semantic_found")]
    query = (
        select(
            ranked.c.kind_id,
            ranked.c.key,
            ranked.c.score,
            full.label("full"),
            final.c.title,
            snippet.label("snippet"),
            *semantic_found,
        )
        .select_from(ranked)
        .join(
            final,
            and_(
                final.c.domain_id == literal(point.domain.id),
                final.c.branch == literal(point.layout.branch),
                final.c.kind_id == ranked.c.kind_id,
                final.c.key == ranked.c.key,
            ),
        )
        .order_by(*exact_first, ranked.c.score.desc(), ranked.c.kind_id, ranked.c.key)
        .limit(limit)
    )
    await explain(session, query, point.domain.id, f"search:{mode}")
    rows = list((await session.execute(query)).mappings())
    items = [
        SearchHit(
            kind=kind_name(point.schema, row["kind_id"]),
            key=row["key"],
            title=point.masking.title(point.schema, row["kind_id"], row["title"]),
            snippet=row["snippet"],
            score=float(row["score"]),
            match=_match(row["full"]),
        )
        for row in rows
    ]
    truncated = False
    if semantic_found:
        found = int(rows[0]["semantic_found"]) if rows else 0
        if found < settings.candidates:
            truncated = await _semantic_cut_short(
                session, runtime, compiler, point, target, filters, found
            )
    return SearchResponse(
        **served(point),
        mode=mode,
        items=items,
        corrections=_applied(corrections),
        semantic_truncated=truncated,
    )


def tuned(runtime: ReadRuntime, tuning: SearchTuning | None) -> ReadRuntime:
    """`runtime` with the ranking knobs of `tuning` in place of the `search` settings. Knobs
    that bound the work may only make it smaller: `candidates` and `typo_candidates` lower,
    `trigram_threshold` higher; 422 otherwise."""
    if tuning is None:
        return runtime
    settings = runtime.search
    for name in ("candidates", "typo_candidates"):
        value, bound = getattr(tuning, name), getattr(settings, name)
        if value is not None and value > bound:
            raise invalid(f"/body/tuning/{name}", f"must not exceed {bound}", "limit_exceeded")
    threshold = tuning.trigram_threshold
    if threshold is not None and threshold < settings.trigram_threshold:
        raise invalid(
            "/body/tuning/trigram_threshold",
            f"must not be below {settings.trigram_threshold}",
            "limit_exceeded",
        )
    given: dict[str, Any] = tuning.model_dump(exclude_none=True)
    hybrid: dict[str, Any] = {
        k: given.pop(k) for k in ("lexical_weight", "semantic_weight", "rrf_k") if k in given
    }
    effective = copy.copy(runtime)
    effective.search = settings.model_copy(
        update={**given, "hybrid": settings.hybrid.model_copy(update=hybrid)}
    )
    return effective


def typo_tolerance(runtime: ReadRuntime) -> TypoTolerance:
    """Typo tolerance of lexical search from the `search` settings."""
    settings = runtime.search
    return TypoTolerance(
        threshold=settings.trigram_threshold,
        weight=settings.trigram_weight,
        candidates=settings.typo_candidates,
        min_length=settings.typo_min_length,
    )


def partial_matching(runtime: ReadRuntime) -> PartialMatching | None:
    """Partial matching of lexical search from `search.partial`; `None` when it is off."""
    settings = runtime.search.partial
    if settings.min_results < 1:
        return None
    return PartialMatching(
        min_results=settings.min_results,
        weight=settings.weight,
        term_bonus=settings.term_bonus,
        candidates=settings.candidates,
        stop_words=settings.stop_words,
    )


def common_terms(runtime: ReadRuntime) -> CommonTerms | None:
    """How many matches lexical search ranks, from `search.common_term_*`; `None` (the store's
    `store.reads.search_candidates`) when `common_term_docs` is 0."""
    settings = runtime.search
    if settings.common_term_docs < 1:
        return None
    return CommonTerms(docs=settings.common_term_docs, candidates=settings.common_term_candidates)


def _match(full: bool | None) -> Literal["all", "partial"] | None:
    if full is None:
        return None
    return "all" if full else "partial"


def _applied(corrections: list[Correction]) -> list[SearchCorrection]:
    return [
        SearchCorrection(word=c.word, replacements=list(c.replacements))
        for c in corrections
        if c.replacements
    ]


def _corrected(compiler: Compiler, q: str, corrections: list[Correction]) -> ColumnElement[Any]:
    """The tsquery of q with each corrected word widened to `(word | replacement | ...)`, as
    the store's search does (`lb_lexeme_query`, revision 0018)."""
    return _rewritten(compiler.tsquery(q), corrections)


def _rewritten(query: ColumnElement[Any], corrections: list[Correction]) -> ColumnElement[Any]:
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
    partial: PartialMatching | None,
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
        partial=partial,
        common=common_terms(runtime),
        backend=settings.backend,
        runtime=runtime.store,
    )
    snippets: dict[tuple[str, str], str | None] = {}
    alternatives = None
    if partial is not None and any(h.partial for h in hits):
        alternatives = await partial_terms(session, q, partial.stop_words)
    if hits:
        hit = point.layout.records("hit")
        headline = func.ts_headline(
            compiler.regconfig(),
            func.concat_ws(
                " ",
                _snippet_title(hit, target, point.masking),
                *_searchable_text(hit, target, point.masking),
            ),
            _corrected(compiler, q if alternatives is None else alternatives.text, corrections),
            literal(settings.headline_options),
        )
        query = select(hit.c.kind_id, hit.c.key, headline.label("snippet")).where(
            hit.c.domain_id == literal(point.domain.id),
            hit.c.branch == literal(point.layout.branch),
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
            match="partial" if h.partial else "all",
        )
        for h in hits
    ]
    return SearchResponse(
        **served(point),
        mode="lexical",
        items=items,
        corrections=_applied(corrections),
        truncated_candidates=hits.truncated,
    )


def _lexical(
    compiler: Compiler,
    r: Any,
    base: list[ColumnElement[bool]],
    q: str,
    corrections: list[Correction],
    runtime: ReadRuntime,
    alternatives: PartialQuery | None = None,
) -> Select[Any]:
    """Lexical candidates with `full` and `exact` columns: matches of q as written first, then
    those found only through a correction at `trigram_weight` times their rank. With
    `alternatives` (`search.partial`), fewer than `min_results` of them are followed by
    Records holding only some terms (`full` false)."""
    settings = runtime.search
    tsquery = compiler.tsquery(q)
    exact: ColumnElement[bool] = r.c.search.op("@@", return_type=Boolean)(tsquery)
    score: ColumnElement[Any] = func.ts_rank_cd(r.c.search, tsquery)
    match: ColumnElement[bool] = exact
    fixes = [c for c in corrections if c.replacements]
    if fixes:
        fixed = _corrected(compiler, q, corrections)
        match = or_(exact, r.c.search.op("@@", return_type=Boolean)(fixed))
        score = case(
            (exact, score),
            else_=literal(settings.trigram_weight, Float) * func.ts_rank_cd(r.c.search, fixed),
        )
    else:
        exact = true()
    flag = func.coalesce(exact, False)
    strict = (
        select(
            r.c.kind_id,
            r.c.key,
            cast(score, Float).label("score"),
            flag.label("exact"),
            true().label("full"),
        )
        .where(*base, match)
        .order_by(not_(flag), score.desc(), r.c.kind_id, r.c.key)
        .limit(settings.candidates)
    )
    if alternatives is None:
        return strict
    full = strict.cte("full_matches")
    enough = select(func.count()).select_from(full).scalar_subquery()
    partial = _partial(compiler, r, base, alternatives, fixes, runtime).where(
        not_(func.coalesce(match, False)),
        enough < literal(settings.partial.min_results),
    )
    lexical = union_all(select(*full.c), partial).subquery("lexical")
    return select(*lexical.c)


def _partial(
    compiler: Compiler,
    r: Any,
    base: list[ColumnElement[bool]],
    alternatives: PartialQuery,
    fixes: list[Correction],
    runtime: ReadRuntime,
) -> Select[Any]:
    """Records holding any term of q, scored `weight * (rank + term_bonus * terms held)`, as
    the store's `search_records` does with `PartialMatching`."""
    settings = runtime.search
    partial = settings.partial
    written = compiler.tsquery(alternatives.text)
    any_term = r.c.search.op("@@", return_type=Boolean)(written)
    rank: ColumnElement[Any] = func.ts_rank_cd(r.c.search, written)
    if fixes:
        fixed = _corrected(compiler, alternatives.text, fixes)
        rank = case(
            (any_term, rank),
            else_=literal(settings.trigram_weight, Float) * func.ts_rank_cd(r.c.search, fixed),
        )
        any_term = r.c.search.op("@@", return_type=Boolean)(fixed)
    terms = (
        func.unnest(literal(list(alternatives.terms), ARRAY(Text)))
        .table_valued("term")
        .render_derived(name="terms")
    )
    term_query = _rewritten(func.websearch_to_tsquery(compiler.regconfig(), terms.c.term), fixes)
    held = (
        select(func.count())
        .select_from(terms)
        .where(r.c.search.op("@@", return_type=Boolean)(term_query))
        .scalar_subquery()
    )
    score = literal(partial.weight, Float) * (
        rank + literal(partial.term_bonus, Float) * cast(held, Float)
    )
    return (
        select(
            r.c.kind_id,
            r.c.key,
            cast(score, Float).label("score"),
            true().label("exact"),
            false().label("full"),
        )
        .where(*base, any_term)
        .order_by(score.desc(), r.c.kind_id, r.c.key)
        .limit(min(partial.candidates, settings.candidates))
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


def _embedded_kinds(point: ReadPoint, target: Target) -> list[str]:
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
    return readable


def _fingerprints(point: ReadPoint, target: Target) -> list[str]:
    """Template fingerprints of the searched Kinds whose vectors the caller may rank by."""
    known = template_fingerprints(target.kinds)
    return sorted({known[k] for k in _embedded_kinds(point, target) if k in known})


def _embedded_readable(r: Any, point: ReadPoint, target: Target) -> ColumnElement[bool]:
    """The Records of `_embedded_kinds`."""
    readable = _embedded_kinds(point, target)
    return r.c.kind_id.in_(readable) if readable else false()


@dataclass(frozen=True, slots=True)
class SemanticScan:
    """How the vector list is fetched (design 8.8): the `overfetch` nearest vectors per wanted
    row of the searched Kinds, taken from `embeddings` alone, then joined to the Records. With
    `iterative`, the index scan goes on until it has them although the filter on the template
    fingerprints (one Kind's vectors are a small part of the model's) rejects most of what it
    visits (`lorebase_store.embeddings`)."""

    overfetch: int
    iterative: bool


async def _semantic_scan(session: AsyncSession, runtime: ReadRuntime) -> SemanticScan:
    """The fetch strategy of this transaction. An iterative scan is turned on for it with
    `SET LOCAL` when `search.semantic.iterative_scan` is `auto` and pgvector is 0.8 or later."""
    semantic = runtime.search.semantic
    if semantic.iterative_scan == "auto" and await iterative_scans(session):
        await use_iterative_scan(session, semantic.max_scan_tuples)
        return SemanticScan(semantic.overfetch_factor, True)
    # The index scan ends after `hnsw.ef_search` rows, whatever the LIMIT: raise it to what is
    # over-fetched (pgvector caps it at 1000).
    await use_ef_search(session, runtime.search.candidates * semantic.overfetch_factor)
    return SemanticScan(semantic.overfetch_factor, False)


def _embedding_match(r: Any, e: Any, runtime: ReadRuntime, target: Target) -> ColumnElement[bool]:
    """Joins Records to the vectors of the configured model stored for their object hash under
    the fingerprint of their Kind's embedding template in the searched branch's schema."""
    dims = runtime.store_settings.embeddings.dims
    fingerprint = fingerprint_of(r.c.kind_id, template_fingerprints(target.kinds))
    return and_(
        e.c.domain_id == r.c.domain_id,
        e.c.object_hash == r.c.object_hash,
        e.c.model == literal(runtime.search.semantic.model),
        e.c.fingerprint == fingerprint,
        e.c.dims == literal(dims, literal_execute=True),
    )


def _semantic(
    r: Any,
    base: list[ColumnElement[bool]],
    vector: list[float],
    runtime: ReadRuntime,
    target: Target,
    domain_id: str,
    fingerprints: list[str],
    *,
    half: bool = False,
    scan: SemanticScan,
    filtered: bool = False,
) -> Select[Any]:
    """Records ranked by the vector stored for their object hash under the fingerprint of
    their Kind's embedding template in the searched branch's schema: another branch's vector
    for the same content, rendered by a different template, is never used. With `half`, the
    distance is computed in half precision, as the HNSW index of the storage profile holds it
    (`lorebase_store.embeddings.index_precision`).

    The nearest vectors come first, from `embeddings` alone, so that the planner reads the
    HNSW index (joined to the Records, it estimates a few rows, sorts a hash join of both
    tables and the cost grows with the model's vectors). A fingerprint names one Kind (the
    template fingerprint includes the Kind id), so `fingerprints`, those of the searched Kinds,
    leave the vectors of other Kinds out of that list. The Records then give the branch and
    the filters of `base`; the list is as long as `search.candidates` unless fewer of the
    `candidates * overfetch` nearest vectors pass them (`_semantic_cut_short` says if more
    would).

    A request with Attribute conditions (`filtered`) under an iterative scan joins the Records
    first instead: the conditions must come before the limit for the list to fill, which no
    number of nearest vectors guarantees. That statement reads the HNSW index only when the
    planner finds it cheaper than sorting the model's vectors."""
    dims = runtime.store_settings.embeddings.dims
    candidates = runtime.search.candidates
    e = EMBEDDINGS.alias("e")
    kind = HALFVEC(dims) if half else VECTOR(dims)
    distance = cast(e.c.embedding, kind).op("<=>", return_type=Float)(literal(vector, kind))
    if filtered and scan.iterative:
        listed = (
            select(r.c.kind_id, r.c.key, (literal(1.0, Float) - distance).label("score"))
            .join(e, _embedding_match(r, e, runtime, target))
            .where(*base)
            .order_by(distance, r.c.kind_id, r.c.key)
            .limit(candidates)
        )
        # Relaxed-order scans return rows slightly out of order: the CTE is computed as written
        # and ordered again, by exact distance, where it is read.
        rows = listed.cte("nearest_records").prefix_with("MATERIALIZED")
        return select(rows).order_by(rows.c.score.desc(), rows.c.kind_id, rows.c.key)
    nearest = (
        select(e.c.domain_id, e.c.object_hash, e.c.fingerprint, distance.label("distance"))
        .where(
            e.c.domain_id == literal(domain_id),
            e.c.model == literal(runtime.search.semantic.model),
            e.c.dims == literal(dims, literal_execute=True),
            e.c.fingerprint.in_(fingerprints),
        )
        .order_by(distance)
        .limit(candidates * scan.overfetch)
        .cte("nearest_vectors")
        .prefix_with("MATERIALIZED")
    )
    fingerprint = fingerprint_of(r.c.kind_id, template_fingerprints(target.kinds))
    return (
        select(r.c.kind_id, r.c.key, (literal(1.0, Float) - nearest.c.distance).label("score"))
        .join(
            nearest,
            and_(
                nearest.c.domain_id == r.c.domain_id,
                nearest.c.object_hash == r.c.object_hash,
                nearest.c.fingerprint == fingerprint,
            ),
        )
        .where(*base)
        .order_by(nearest.c.distance, r.c.kind_id, r.c.key)
        .limit(candidates)
    )


async def _semantic_cut_short(
    session: AsyncSession,
    runtime: ReadRuntime,
    compiler: Compiler,
    point: ReadPoint,
    target: Target,
    filters: Callable[[Any], list[ColumnElement[bool]]],
    found: int,
) -> bool:
    """Whether a vector list of `found` rows (fewer than `search.candidates`) is short because
    the index scan stopped at its bound rather than because no more Records match: some more
    Record with a vector satisfies the same filters. Counts at most `search.candidates`
    matches, unordered, without the index."""
    r = compiler.records("r")
    e = EMBEDDINGS.alias("e")
    matches = (
        select(r.c.key)
        .join(e, _embedding_match(r, e, runtime, target))
        .where(*filters(r), _embedded_readable(r, point, target))
        .limit(runtime.search.candidates)
        .subquery("matches")
    )
    exact = (await session.execute(select(func.count()).select_from(matches))).scalar_one()
    return int(exact) > found


def _fused(lists: list[tuple[Select[Any], float]], runtime: ReadRuntime) -> Any:
    """Reciprocal rank fusion of the ranked lists (full outer join on the Record)."""
    k = runtime.search.hybrid.rrf_k
    ranked = []
    for index, (candidates, weight) in enumerate(lists):
        inner = candidates.subquery(f"c{index}")
        exact_first = [inner.c[c].desc() for c in ("full", "exact") if c in inner.c]
        position = over(
            func.row_number(),
            order_by=(*exact_first, inner.c.score.desc(), inner.c.kind_id, inner.c.key),
        )
        carried = [inner.c.full] if "full" in inner.c else []
        ranked.append(
            (
                select(inner.c.kind_id, inner.c.key, position.label("rank"), *carried).subquery(
                    f"l{index}"
                ),
                weight,
            )
        )
    (first, w_first), (second, w_second) = ranked
    full = [ranks.c.full for ranks, _ in ranked if "full" in ranks.c]
    score = func.coalesce(literal(w_first, Float) / (literal(k) + first.c["rank"]), 0.0) + (
        func.coalesce(literal(w_second, Float) / (literal(k) + second.c["rank"]), 0.0)
    )
    return (
        select(
            func.coalesce(first.c.kind_id, second.c.kind_id).label("kind_id"),
            func.coalesce(first.c.key, second.c.key).label("key"),
            cast(score, Float).label("score"),
            second.c["rank"].label("semantic_rank"),
            *([func.coalesce(*full).label("full")] if full else []),
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
