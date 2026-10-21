"""Read primitives on the materialized head of a branch (design 8).

Each primitive is one SQL statement shaped for an index scan, with stable text so the asyncpg
driver reuses one prepared statement per shape. The caller runs them in its own transaction
after `scope_to_domain` (RLS). For a response that must be consistent with the Revision it
reports, run the reads of one request in a `REPEATABLE READ` transaction.

- `head_revision`: the branch head, one primary-key read; the value for `ETag` and `304`.
- `head_schema`: the schema at the head through the per-process `SchemaCache`.
- `get_record`, `list_records`, `hydrate_records`: primary-key lookups and keyset pages.
- `links_of_record`: Links in either direction, stored or foreign-key backed.
- `links_to_record`: one page of the Links pointing at a Record along one Relation.
- `query_by_attribute`: comparisons served by the per-Attribute partial indexes.
- `search_records`: full-text search over the GIN index, ranking a bounded candidate set.

Incoming foreign-key Links, attribute queries and search compare with functions Postgres does
not mark leakproof, so under row-level security plain SQL cannot use their indexes. With
`store.reads.use_definer_functions` (the default) they run through the security definer
functions of revision 0005 (`lb_links_in`, `lb_query_by_attribute`, `lb_search`), which take
the Domain as an argument: only these primitives call them, with the Domain their caller has
already authorized (store schema notes). Otherwise the same statements run as plain SQL.

Every primitive that reads rows takes the branch as a name or as its `BranchLayout`
(`lorebase_store.layout`); a name costs one more primary-key read to find the layout. On an
overlay branch the statements read its resolved rows (overlay rows over the base rows as of
the fork point) and the definer functions are their `_overlay` variants (revision `0010`);
on a full branch they are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Any

from sqlalchemy import LargeBinary, Table, TextClause, TextualSelect, column, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Relation, RelationBacking, Schema
from lorebase_store.indexes import IndexSpec, plan_indexes, predicate_sql, sql_literal, values_text
from lorebase_store.layout import BranchLayout, escape_colons, resolve_layout
from lorebase_store.models import metadata
from lorebase_store.repositories.materialized import RecordRow
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.telemetry import record_definer_call

MAT_RECORDS: Table = metadata.tables["mat_records"]
MAT_LINKS: Table = metadata.tables["mat_links"]

RECORD_NAMES = (
    "kind_id",
    "key",
    "values",
    "title",
    "object_hash",
    "lineage",
    "updated_revision",
    "updated_at",
)
RECORD_COLUMNS = ", ".join(f'm."{name}"' for name in RECORD_NAMES)


def _records(sql: str) -> TextualSelect:
    """A statement returning Record columns, typed so JSON comes back decoded."""
    return text(sql).columns(*(MAT_RECORDS.c[name] for name in RECORD_NAMES))


class Direction(StrEnum):
    """Which Links of a Record to follow: outgoing (`from`), incoming (`to`) or both."""

    OUT = "out"
    IN = "in"
    BOTH = "both"

    @property
    def outgoing(self) -> bool:
        return self is not Direction.IN

    @property
    def incoming(self) -> bool:
        return self is not Direction.OUT


class Comparison(StrEnum):
    EQ = "eq"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"


_OPERATORS = {
    Comparison.EQ: "=",
    Comparison.LT: "<",
    Comparison.LTE: "<=",
    Comparison.GT: ">",
    Comparison.GTE: ">=",
}


@dataclass(frozen=True, slots=True)
class HeadSchema:
    revision: bytes
    schema: Schema


@dataclass(frozen=True, slots=True)
class LinkRef:
    """A Link at the head; foreign-key backed Links have no values and no object hash."""

    relation_id: str
    from_key: str
    to_key: str
    values: dict[str, Any]
    object_hash: bytes | None


@dataclass(frozen=True, slots=True)
class SearchHit:
    kind_id: str
    key: str
    title: str | None
    score: float


def _runtime(runtime: StoreRuntime | None) -> StoreRuntime:
    return runtime or default_runtime()


def _definer(runtime: StoreRuntime | None) -> bool:
    """Whether reads go through the security definer functions (`store.reads`)."""
    return _runtime(runtime).settings.reads.use_definer_functions


def _function(name: str, layout: BranchLayout) -> str:
    """A security definer read function, or its overlay variant on an overlay branch, for a
    statement about to call it (the call is counted)."""
    function = f"{name}_overlay" if layout.overlay else name
    record_definer_call(function)
    return function


def _cap(limit: int, maximum: int) -> int:
    if limit < 1:
        raise ValueError("limit must be positive")
    return min(limit, maximum)


# Heads and schemas ------------------------------------------------------------------------------

_HEAD = text("SELECT head FROM refs WHERE domain_id = :domain AND name = :branch")
_HEAD_SCHEMA = text(
    """
    SELECT r.head, s.compiled
    FROM refs r
    JOIN mat_schema s ON s.domain_id = r.domain_id AND s.branch = r.name
    WHERE r.domain_id = :domain AND r.name = :branch
    """
).columns(column("head", LargeBinary), column("compiled", JSONB))


async def head_revision(session: AsyncSession, domain_id: str, branch: str) -> bytes | None:
    """Head Revision id of a branch (one primary-key read), `None` when it does not exist.

    The id is a strong validator: an `ETag` derived from it (plus the caller's mask profile)
    answers `If-None-Match` with `304` before any other read.
    """
    params = {"domain": domain_id, "branch": branch}
    found: bytes | None = (await session.execute(_HEAD, params)).scalar_one_or_none()
    return found


async def head_schema(
    session: AsyncSession, domain_id: str, branch: str, *, runtime: StoreRuntime | None = None
) -> HeadSchema | None:
    """Head Revision and schema of a branch; the schema comes from the cache when it can."""
    cache = _runtime(runtime).schema_cache
    head = await head_revision(session, domain_id, branch)
    if head is None:
        return None
    cached = cache.get(domain_id, head)
    if cached is not None:
        return HeadSchema(head, cached)
    row = (
        await session.execute(_HEAD_SCHEMA, {"domain": domain_id, "branch": branch})
    ).one_or_none()
    if row is None:
        return None
    schema = Schema.model_validate(row.compiled)
    cache.put(domain_id, row.head, schema)
    return HeadSchema(row.head, schema)


# Records ----------------------------------------------------------------------------------------

_SCOPE = "m.domain_id = :domain AND m.branch = :branch"


@dataclass(frozen=True, slots=True)
class _RecordStatements:
    get: TextualSelect
    page: TextualSelect
    page_after: TextualSelect
    hydrate: TextualSelect


@lru_cache(maxsize=256)
def _record_statements(records: str) -> _RecordStatements:
    """Key and page reads over the FROM item `records` (aliased `m`)."""
    return _RecordStatements(
        get=_records(
            f"SELECT {RECORD_COLUMNS} FROM {records} "
            f"WHERE {_SCOPE} AND m.kind_id = :kind AND m.key = :key"
        ),
        page=_records(
            f"SELECT {RECORD_COLUMNS} FROM {records} "
            f"WHERE {_SCOPE} AND m.kind_id = :kind ORDER BY m.key LIMIT :limit"
        ),
        page_after=_records(
            f"SELECT {RECORD_COLUMNS} FROM {records} "
            f"WHERE {_SCOPE} AND m.kind_id = :kind AND m.key > :after ORDER BY m.key LIMIT :limit"
        ),
        hydrate=_records(
            f"""
    SELECT {RECORD_COLUMNS}
    FROM unnest(CAST(:kinds AS text[]), CAST(:keys AS text[])) AS i(kind_id, key)
    JOIN {records} ON {_SCOPE} AND m.kind_id = i.kind_id AND m.key = i.key
    """
        ),
    )


def _statements(layout: BranchLayout) -> _RecordStatements:
    if layout.overlay:
        return _overlay_statements(layout)
    return _record_statements(layout.records_from("m"))


@lru_cache(maxsize=256)
def _overlay_statements(layout: BranchLayout) -> _RecordStatements:
    """Key and page reads of an overlay: pages are taken from each half (overlay rows, base
    rows) in key order and merged, so a page reads at most twice its size; hydration looks
    each identity up laterally (LIMIT 1 keeps the lookup from being turned into a join with
    every row), so its keys reach both halves as index conditions."""
    kind = " AND m.kind_id = :kind"
    page = " ORDER BY m.key LIMIT :limit"
    return _RecordStatements(
        get=_record_statements(layout.records_from("m")).get,
        page=_records(
            f"SELECT {RECORD_COLUMNS} FROM {layout.records_arms(kind + page)} "
            "ORDER BY m.key LIMIT :limit"
        ),
        page_after=_records(
            f"SELECT {RECORD_COLUMNS} FROM "
            f"{layout.records_arms(kind + ' AND m.key > :after' + page)} "
            "ORDER BY m.key LIMIT :limit"
        ),
        hydrate=_records(
            f"""
    SELECT {RECORD_COLUMNS}
    FROM unnest(CAST(:kinds AS text[]), CAST(:keys AS text[])) AS i(kind_id, key)
    CROSS JOIN LATERAL (
        SELECT {RECORD_COLUMNS} FROM {layout.records_from("m")}
        WHERE {_SCOPE} AND m.kind_id = i.kind_id AND m.key = i.key LIMIT 1) AS m
    """
        ),
    )


async def get_record(
    session: AsyncSession, domain_id: str, branch: str | BranchLayout, kind_id: str, key: str
) -> RecordRow | None:
    layout = await resolve_layout(session, domain_id, branch)
    params = {"domain": domain_id, "branch": layout.branch, "kind": kind_id, "key": key}
    found = (await session.execute(_statements(layout).get, params)).mappings().one_or_none()
    return None if found is None else RecordRow(**found)


async def list_records(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    kind_id: str,
    *,
    limit: int,
    after: str | None = None,
    runtime: StoreRuntime | None = None,
) -> list[RecordRow]:
    """A page of a Kind's Records in key order, starting after the key `after`."""
    layout = await resolve_layout(session, domain_id, branch)
    params: dict[str, Any] = {
        "domain": domain_id,
        "branch": layout.branch,
        "kind": kind_id,
        "limit": _cap(limit, _runtime(runtime).settings.reads.max_page_rows),
    }
    statement = _statements(layout).page
    if after is not None:
        statement, params["after"] = _statements(layout).page_after, after
    return [RecordRow(**row) for row in (await session.execute(statement, params)).mappings()]


async def hydrate_records(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    identities: Iterable[tuple[str, str]],
    *,
    runtime: StoreRuntime | None = None,
) -> dict[tuple[str, str], RecordRow]:
    """Records by `(kind_id, key)` in one statement per `reads.max_hydrate_keys` identities."""
    unique = list(dict.fromkeys(identities))
    batch = _runtime(runtime).settings.reads.max_hydrate_keys
    found: dict[tuple[str, str], RecordRow] = {}
    if not unique:
        return found
    layout = await resolve_layout(session, domain_id, branch)
    statement = _statements(layout).hydrate
    for start in range(0, len(unique), batch):
        chunk = unique[start : start + batch]
        params = {
            "domain": domain_id,
            "branch": layout.branch,
            "kinds": [kind for kind, _ in chunk],
            "keys": [key for _, key in chunk],
        }
        for row in (await session.execute(statement, params)).mappings():
            record = RecordRow(**row)
            found[(record.kind_id, record.key)] = record
    return found


# Links ------------------------------------------------------------------------------------------

_LINK_SELECT = 'relation_id, from_key, to_key, "values", object_hash'


def _relations(
    schema: Schema, kind_id: str, direction: Direction, relation_ids: Sequence[str] | None
) -> tuple[list[Relation], list[Relation]]:
    """Relations leaving and entering `kind_id`, restricted to `relation_ids` when given."""
    wanted = None if relation_ids is None else set(relation_ids)
    outgoing: list[Relation] = []
    incoming: list[Relation] = []
    for relation in sorted(schema.relations.values(), key=lambda r: r.id):
        if wanted is not None and relation.id not in wanted:
            continue
        if direction.outgoing and relation.from_kind == kind_id:
            outgoing.append(relation)
        if direction.incoming and relation.to_kind == kind_id:
            incoming.append(relation)
    return outgoing, incoming


def stored_relation_ids(relations: Iterable[Relation]) -> list[str]:
    return [r.id for r in relations if r.backing is RelationBacking.EXPLICIT]


def foreign_key_relations(relations: Iterable[Relation]) -> list[Relation]:
    return [
        r
        for r in relations
        if r.backing is RelationBacking.FOREIGN_KEY and r.foreign_key_attribute is not None
    ]


def foreign_key_inbound_sql(
    relation: Relation,
    domain_id: str,
    branch: str,
    *,
    target: str,
    limit: str,
    records: str = "mat_records",
) -> str:
    """Links of a foreign-key Relation that point at `target` (an SQL expression), read from
    the FROM item `records` (`BranchLayout.records_from()`).

    The literal predicate and expression match the Relation's per-Attribute index.
    """
    assert relation.foreign_key_attribute is not None
    predicate = escape_colons(predicate_sql(domain_id, branch, relation.from_kind))
    return (
        f"SELECT CAST({sql_literal(relation.id)} AS text) AS relation_id, key AS from_key, "
        f"{values_text(relation.foreign_key_attribute)} AS to_key, "
        f"CAST('{{}}' AS jsonb) AS \"values\", CAST(NULL AS bytea) AS object_hash "
        f"FROM {records} WHERE {predicate} "
        f"AND {values_text(relation.foreign_key_attribute)} = {target} LIMIT {limit}"
    )


async def links_of_record(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    schema: Schema,
    kind_id: str,
    key: str,
    *,
    direction: Direction = Direction.BOTH,
    relation_ids: Sequence[str] | None = None,
    limit: int,
    runtime: StoreRuntime | None = None,
) -> list[LinkRef]:
    """Links of one Record ordered by Relation, from key and to key, at most `limit`."""
    outgoing, incoming = _relations(schema, kind_id, direction, relation_ids)
    layout = await resolve_layout(session, domain_id, branch)
    links_from, records_from = layout.links_from(), layout.records_from("m")
    params: dict[str, Any] = {
        "domain": domain_id,
        "branch": layout.branch,
        "kind": kind_id,
        "key": key,
        "limit": _cap(limit, _runtime(runtime).settings.reads.max_page_rows),
    }
    parts: list[str] = []
    if stored_out := stored_relation_ids(outgoing):
        params["out_rels"] = stored_out
        parts.append(
            f"SELECT {_LINK_SELECT} FROM {links_from} WHERE domain_id = :domain "
            "AND branch = :branch AND relation_id = ANY(CAST(:out_rels AS text[])) "
            "AND from_key = :key LIMIT :limit"
        )
    if stored_in := stored_relation_ids(incoming):
        params["in_rels"] = stored_in
        parts.append(
            f"SELECT {_LINK_SELECT} FROM {links_from} WHERE domain_id = :domain "
            "AND branch = :branch AND relation_id = ANY(CAST(:in_rels AS text[])) "
            "AND to_key = :key LIMIT :limit"
        )
    if foreign_out := foreign_key_relations(outgoing):
        params["fk_rels"] = [r.id for r in foreign_out]
        params["fk_attributes"] = [r.foreign_key_attribute for r in foreign_out]
        parts.append(
            'SELECT r.relation_id, m.key AS from_key, m."values" ->> r.attribute AS to_key, '
            "CAST('{}' AS jsonb) AS \"values\", CAST(NULL AS bytea) AS object_hash "
            f"FROM {records_from} CROSS JOIN unnest(CAST(:fk_rels AS text[]), "
            "CAST(:fk_attributes AS text[])) AS r(relation_id, attribute) "
            f"WHERE {_SCOPE} AND m.kind_id = :kind AND m.key = :key "
            'AND m."values" ->> r.attribute IS NOT NULL'
        )
    definer = _definer(runtime)
    for index, relation in enumerate(foreign_key_relations(incoming)):
        if not definer:
            parts.append(
                foreign_key_inbound_sql(
                    relation,
                    domain_id,
                    layout.branch,
                    target=":key",
                    limit=":limit",
                    records=layout.records_from(),
                )
            )
            continue
        params[f"fk_in_{index}"] = relation.id
        params[f"fk_in_kind_{index}"] = relation.from_kind
        params[f"fk_in_attribute_{index}"] = relation.foreign_key_attribute
        parts.append(
            f"SELECT {_LINK_SELECT} FROM {_function('lb_links_in', layout)}(:domain, :branch, "
            f":fk_in_{index}, :key, CAST(:limit AS integer), NULL, :fk_in_kind_{index}, "
            f":fk_in_attribute_{index})"
        )
    if not parts:
        return []
    union = " UNION ALL ".join(f"({part})" for part in parts)
    statement = _links(
        f"SELECT * FROM ({union}) AS links ORDER BY relation_id, from_key, to_key LIMIT :limit"
    )
    return [LinkRef(**row) for row in (await session.execute(statement, params)).mappings()]


def _links(sql: str) -> TextualSelect:
    """A statement returning Link columns, typed so JSON comes back decoded."""
    return text(sql).columns(
        MAT_LINKS.c.relation_id,
        MAT_LINKS.c.from_key,
        MAT_LINKS.c.to_key,
        MAT_LINKS.c["values"],
        MAT_LINKS.c.object_hash,
    )


_LINKS_IN = {
    function: _links(
        f"SELECT * FROM {function}(:domain, :branch, :relation, :to_key, "
        "CAST(:limit AS integer), :after, :from_kind, :attribute)"
    )
    for function in ("lb_links_in", "lb_links_in_overlay")
}


def _plain_links_in(layout: BranchLayout, relation: Relation, *, after: bool) -> str:
    """`lb_links_in` as plain SQL (parameters `:relation`, `:to_key`, `:limit`, `:after`)."""
    if relation.backing is not RelationBacking.FOREIGN_KEY:
        return (
            f"SELECT {_LINK_SELECT} FROM {layout.links_from()} WHERE domain_id = :domain "
            "AND branch = :branch AND relation_id = :relation AND to_key = :to_key"
            f"{' AND from_key > :after' if after else ''} ORDER BY from_key LIMIT :limit"
        )
    assert relation.foreign_key_attribute is not None
    predicate = escape_colons(predicate_sql(layout.domain_id, layout.branch, relation.from_kind))
    return (
        "SELECT CAST(:relation AS text) AS relation_id, key AS from_key, "
        "CAST(:to_key AS text) AS to_key, CAST('{}' AS jsonb) AS \"values\", "
        f"CAST(NULL AS bytea) AS object_hash FROM {layout.records_from()} WHERE {predicate} "
        f"AND {values_text(relation.foreign_key_attribute)} = :to_key"
        f"{' AND key > :after' if after else ''} ORDER BY key LIMIT :limit"
    )


async def links_to_record(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    relation: Relation,
    to_key: str,
    *,
    limit: int,
    after: str | None = None,
    runtime: StoreRuntime | None = None,
) -> list[LinkRef]:
    """Links of `relation` pointing at the Record `to_key`, in from-key order after `after`.

    Stored Links are read through the `(domain_id, branch, relation_id, to_key)` index,
    foreign-key backed ones through the per-Attribute index of the from Kind.
    """
    foreign = relation.backing is RelationBacking.FOREIGN_KEY
    if foreign and relation.foreign_key_attribute is None:
        return []
    layout = await resolve_layout(session, domain_id, branch)
    params: dict[str, Any] = {
        "domain": domain_id,
        "branch": layout.branch,
        "relation": relation.id,
        "to_key": to_key,
        "limit": _cap(limit, _runtime(runtime).settings.reads.max_page_rows),
    }
    if _definer(runtime):
        params.update(
            after=after,
            from_kind=relation.from_kind if foreign else None,
            attribute=relation.foreign_key_attribute if foreign else None,
        )
        statement = _LINKS_IN[_function("lb_links_in", layout)]
    else:
        if after is not None:
            params["after"] = after
        statement = _links(_plain_links_in(layout, relation, after=after is not None))
    return [LinkRef(**row) for row in (await session.execute(statement, params)).mappings()]


# Attribute queries ------------------------------------------------------------------------------

AttributeValue = str | int | float | bool | Decimal


def _value_text(value: AttributeValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def index_spec(schema: Schema, kind_id: str, attribute_id: str) -> IndexSpec | None:
    """The per-Attribute index of an Attribute, `None` when it has none."""
    return plan_indexes(schema).by_attribute().get((kind_id, attribute_id))


async def query_by_attribute(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    spec: IndexSpec,
    comparison: Comparison,
    value: AttributeValue,
    *,
    limit: int,
    after: tuple[AttributeValue, str] | None = None,
    runtime: StoreRuntime | None = None,
) -> list[RecordRow]:
    """Records of `spec`'s Kind whose Attribute compares with `value`, through its index.

    Equality pages in key order (`after` = `(value, key)` of the last row); ranges page in
    `(value, key)` order and need an ordered index (`spec.ordered`). `value` is canonical.
    """
    if comparison is not Comparison.EQ and not spec.ordered:
        raise ValueError(f"the index of {spec.attribute_id} supports equality only")
    layout = await resolve_layout(session, domain_id, branch)
    expression = spec.expression
    params: dict[str, Any] = {
        "value": _value_text(value),
        "limit": _cap(limit, _runtime(runtime).settings.reads.max_page_rows),
    }
    if _definer(runtime):
        ranged = comparison is not Comparison.EQ
        params.update(
            domain=domain_id,
            branch=layout.branch,
            kind=spec.kind_id,
            attribute=spec.attribute_id,
            type=spec.value_type,
            op=comparison.value,
            after_value=_value_text(after[0]) if after is not None and ranged else None,
            after_key=None if after is None else after[1],
        )
        statement = _QUERY_BY_ATTRIBUTE[_function("lb_query_by_attribute", layout)]
        found = (await session.execute(statement, params)).mappings()
        return [RecordRow(**row) for row in found]
    where = [
        f"m.kind_id = {sql_literal(spec.kind_id)}" if layout.overlay else "",
        f"{expression} {_OPERATORS[comparison]} {spec.value_sql(':value')}",
    ]
    if not layout.overlay:
        where[0] = escape_colons(spec.predicate_sql(domain_id, layout.branch))
    if comparison is Comparison.EQ:
        order = "m.key"
        if after is not None:
            where.append("m.key > :after_key")
            params["after_key"] = after[1]
    else:
        order = f"{expression}, m.key"
        if after is not None:
            where.append(f"({expression}, m.key) > ({spec.value_sql(':after_value')}, :after_key)")
            params["after_value"], params["after_key"] = _value_text(after[0]), after[1]
    if layout.overlay:
        # Each half (overlay rows, base rows through the base's partial index) is ordered and
        # cut to the page before the halves are merged.
        tail = f" AND {' AND '.join(where)} ORDER BY {order} LIMIT :limit"
        source = layout.records_arms(tail)
        statement = _records(f"SELECT {RECORD_COLUMNS} FROM {source} ORDER BY {order} LIMIT :limit")
    else:
        statement = _records(
            f"SELECT {RECORD_COLUMNS} FROM {layout.records_from('m')} "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT :limit"
        )
    return [RecordRow(**row) for row in (await session.execute(statement, params)).mappings()]


_QUERY_BY_ATTRIBUTE = {
    function: _records(
        f"SELECT * FROM {function}(:domain, :branch, :kind, :attribute, :type, :op, "
        ":value, CAST(:limit AS integer), :after_value, :after_key)"
    )
    for function in ("lb_query_by_attribute", "lb_query_by_attribute_overlay")
}


# Full-text search -------------------------------------------------------------------------------

_QUERY = "websearch_to_tsquery(CAST(:config AS regconfig), :query)"


@dataclass(frozen=True, slots=True)
class TitleSimilarity:
    """Typo-tolerant title matches by trigram similarity (`pg_trgm`, trigram index on titles).

    Titles at least `threshold` similar to the query match too, and `weight` times the
    similarity is added to the rank.
    """

    threshold: float
    weight: float


def search_sql(*, by_kind: bool, titles: bool = False, records: str = "mat_records m") -> str:
    """Full-text search: the GIN index finds at most `:candidates` matches of the branch, and
    only those are ranked (`ts_rank_cd` reads each candidate's vector).

    Parameters: `:domain`, `:branch`, `:config`, `:query` (web search syntax), `:candidates`,
    `:limit`, `:kinds` (text array) when `by_kind`, and `:title_weight` when `titles` (the
    threshold is the transaction's `pg_trgm.similarity_threshold`). `records` is the FROM item
    of the rows, aliased `m` (`BranchLayout.records_from("m")`).
    """
    kinds = "AND m.kind_id = ANY(CAST(:kinds AS text[])) " if by_kind else ""
    match = f"m.search @@ {_QUERY}"
    score = f"ts_rank_cd(c.search, {_QUERY})"
    if titles:
        match = f"({match} OR m.title % :query)"
        score += " + CAST(:title_weight AS real) * similarity(coalesce(c.title, ''), :query)"
    return (
        f"SELECT c.kind_id, c.key, c.title, CAST({score} AS real) AS score "
        f"FROM (SELECT m.kind_id, m.key, m.title, m.search FROM {records} "
        f"WHERE {_SCOPE} {kinds}AND {match} LIMIT :candidates) AS c "
        f"ORDER BY score DESC, c.kind_id, c.key LIMIT :limit"
    )


@lru_cache(maxsize=256)
def _search(records: str, by_kind: bool, titles: bool) -> TextClause:
    return text(search_sql(by_kind=by_kind, titles=titles, records=records))


_SEARCH_DEFINER = {
    function: text(
        f"SELECT * FROM {function}(:domain, :branch, CAST(:kinds AS text[]), "
        "CAST(:config AS regconfig), :query, CAST(:candidates AS integer), "
        "CAST(:limit AS integer), CAST(:threshold AS real), CAST(:title_weight AS real))"
    )
    for function in ("lb_search", "lb_search_overlay")
}
_SIMILARITY_THRESHOLD = text(
    "SELECT set_config('pg_trgm.similarity_threshold', "
    "CAST(CAST(:threshold AS real) AS text), true)"
)


async def search_records(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    query: str,
    *,
    limit: int,
    kind_ids: Sequence[str] | None = None,
    titles: TitleSimilarity | None = None,
    runtime: StoreRuntime | None = None,
) -> list[SearchHit]:
    """Best `limit` lexical matches of `query` (web search syntax) on the branch head.

    `kind_ids` restricts the Kinds searched (`None`: every Kind); `titles` adds typo-tolerant
    title matches (the plain SQL path sets `pg_trgm.similarity_threshold` for the
    transaction, the function only for its own call).
    """
    settings = _runtime(runtime).settings
    layout = await resolve_layout(session, domain_id, branch)
    params: dict[str, Any] = {
        "domain": domain_id,
        "branch": layout.branch,
        "config": settings.materialize.text_search_config,
        "query": query,
        "candidates": settings.reads.search_candidates,
        "limit": _cap(limit, settings.reads.max_search_rows),
    }
    kinds = None if kind_ids is None else list(kind_ids)
    if _definer(runtime):
        params.update(
            kinds=kinds,
            threshold=None if titles is None else titles.threshold,
            title_weight=0.0 if titles is None else titles.weight,
        )
        statement = _SEARCH_DEFINER[_function("lb_search", layout)]
    else:
        if titles is not None:
            await session.execute(_SIMILARITY_THRESHOLD, {"threshold": titles.threshold})
            params["title_weight"] = titles.weight
        if kinds is not None:
            params["kinds"] = kinds
        statement = _search(layout.records_from("m"), kinds is not None, titles is not None)
    rows = (await session.execute(statement, params)).all()
    return [SearchHit(kind_id, key, title, float(score)) for kind_id, key, title, score in rows]


# Title matching (entity resolution) -------------------------------------------------------------

_SIMILAR_TITLES = text(
    "SELECT m.kind_id, m.key, m.title, "
    "CAST(GREATEST(CASE WHEN lower(m.title) = lower(:query) THEN 1 ELSE 0 END, "
    "similarity(m.title, :query)) AS real) AS score "
    f"FROM mat_records m WHERE {_SCOPE} AND m.kind_id = ANY(CAST(:kinds AS text[])) "
    "AND m.title IS NOT NULL "
    "AND (lower(m.title) = lower(:query) OR similarity(m.title, :query) >= :threshold) "
    "ORDER BY score DESC, m.kind_id, m.key LIMIT :limit"
)


async def similar_titles(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    query: str,
    *,
    kind_ids: Sequence[str],
    threshold: float,
    limit: int,
    runtime: StoreRuntime | None = None,
) -> list[SearchHit]:
    """Records of `kind_ids` whose title equals `query` ignoring case (score 1) or is at least
    `threshold` similar to it by trigrams (`pg_trgm`), best first.

    Meant for entity resolution in batch jobs: under row-level security the trigram index
    cannot serve the condition, so the statement scans the titles of the named Kinds.
    """
    if not kind_ids or not query.strip():
        return []
    settings = _runtime(runtime).settings
    rows = (
        await session.execute(
            _SIMILAR_TITLES,
            {
                "domain": domain_id,
                "branch": branch,
                "kinds": list(kind_ids),
                "query": query,
                "threshold": threshold,
                "limit": _cap(limit, settings.reads.max_search_rows),
            },
        )
    ).all()
    return [SearchHit(kind_id, key, title, float(score)) for kind_id, key, title, score in rows]
