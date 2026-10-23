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
- `search_records`: full-text search over the GIN index, ranking a bounded candidate set
  (sized per query from the word lexicon with `CommonTerms`, and reported `truncated` when
  the cap cut the ranking); with `PartialMatching`, a query whose words do not all occur
  together falls back to Records that hold some of them (`partial_terms`).
- `search_corrections`: typo corrections of query words from the branch's word lexicon.

Incoming foreign-key Links, attribute queries and search compare with functions Postgres does
not mark leakproof, so under row-level security plain SQL cannot use their indexes. With
`store.reads.use_definer_functions` (the default) they run through the security definer
functions of revisions 0005, 0018 and 0035 (`lb_links_in`, `lb_query_by_attribute`,
`lb_search_bounded`, `lb_search_corrections`), which take the Domain as an argument: only
these primitives call them, with the Domain their caller has already authorized (store schema
notes). Otherwise the same statements run as plain SQL.

Every primitive that reads rows takes the branch as a name or as its `BranchLayout`
(`lorebase_store.layout`); a name costs one more primary-key read to find the layout. On an
overlay branch the statements read its resolved rows (overlay rows over the base rows as of
the fork point) and the definer functions are their `_overlay` variants (revision `0010`);
on a full branch they are unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
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


def lineage_sql(value: str, domain: str = "CAST(:domain AS text)") -> str:
    """`value` (a map column of `mat_records` or `mat_links`) as the map it stands for: with
    shared lineage maps (revision 0036) a column may hold a reference to the map instead."""
    return f"lb_lineage({domain}, {value})"


def record_columns(alias: str | None = "m") -> str:
    """The columns of `RECORD_NAMES` from `alias` (bare names without one), lineage resolved
    with the bound `:domain`."""
    prefix = "" if alias is None else f"{alias}."
    columns = [f'{prefix}"{name}"' for name in RECORD_NAMES]
    return ", ".join(
        f'{lineage_sql(column)} AS "{name}"' if name == "lineage" else column
        for name, column in zip(RECORD_NAMES, columns, strict=True)
    )


RECORD_COLUMNS = record_columns()


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
    """A search match; `exact` is false when it matched only through a typo correction,
    `partial` is true when it holds only some of the query's terms (`PartialMatching`)."""

    kind_id: str
    key: str
    title: str | None
    score: float
    exact: bool = True
    partial: bool = False


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
        "domain": domain_id,
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
        f"SELECT {record_columns(None)} FROM {function}(:domain, :branch, :kind, :attribute, "
        ":type, :op, "
        ":value, CAST(:limit AS integer), :after_value, :after_key)"
    )
    for function in ("lb_query_by_attribute", "lb_query_by_attribute_overlay")
}


# Full-text search -------------------------------------------------------------------------------

_QUERY = "websearch_to_tsquery(CAST(:config AS regconfig), :query)"


@dataclass(frozen=True, slots=True)
class TypoTolerance:
    """Typo-tolerant search through the words of the branch (`mat_lexicon`, revision 0018).

    A query word of at least `min_length` characters that the branch does not hold (nor its
    base, for an overlay) is corrected to its `candidates` closest words at least `threshold`
    similar by trigrams (`pg_trgm`). Search then matches the word or any of them; hits that
    match only through a correction rank after the others, at `weight` times their rank.
    """

    threshold: float
    weight: float
    candidates: int
    min_length: int


@dataclass(frozen=True, slots=True)
class Correction:
    """A query word (a lexeme of the text search configuration) and its replacements, best
    first."""

    word: str
    replacements: tuple[str, ...]


_WORDS = (
    "SELECT DISTINCT replace(replace(t.m[2], '''''', ''''), '\\\\', '\\') AS word "
    "FROM regexp_matches(CAST(" + _QUERY + " AS text), '(!?)''((?:[^'']|'''')*)''', 'g') "
    "AS t(m) WHERE t.m[1] = ''"
)
"""The lexemes of the query as full-text search reads it (tsquery output doubles quotes and
backslashes), but negated ones, as in `lb_search_corrections`."""

_CORRECTIONS_SQL = text(
    f"""
    SELECT w.word, c.word AS replacement
    FROM ({_WORDS}) AS w
    CROSS JOIN LATERAL (
        SELECT l.word, max(similarity(l.word, w.word)) AS similarity, sum(l.records) AS records
        FROM mat_lexicon l
        WHERE l.domain_id = :domain AND l.branch = ANY(CAST(:branches AS text[]))
          AND l.records > 0 AND l.word % w.word
          AND (CAST(:kinds AS text[]) IS NULL OR l.kind_id = ANY(CAST(:kinds AS text[])))
        GROUP BY l.word
        ORDER BY 2 DESC, 3 DESC, l.word
        LIMIT :candidates) AS c
    WHERE char_length(w.word) >= :min_length AND lb_lexicon_word(w.word)
      AND NOT EXISTS (
          SELECT 1 FROM mat_lexicon x
          WHERE x.domain_id = :domain AND x.branch = ANY(CAST(:branches AS text[]))
            AND x.word = w.word AND x.records > 0
            AND (CAST(:kinds AS text[]) IS NULL OR x.kind_id = ANY(CAST(:kinds AS text[]))))
    ORDER BY w.word, c.similarity DESC, c.records DESC, c.word
    """
)
_CORRECTIONS_DEFINER = text(
    "SELECT word, replacement FROM lb_search_corrections(:domain, :branch, "
    "CAST(:kinds AS text[]), CAST(:config AS regconfig), :query, CAST(:threshold AS real), "
    "CAST(:candidates AS integer), CAST(:min_length AS integer))"
)
_SIMILARITY_THRESHOLD = text(
    "SELECT set_config('pg_trgm.similarity_threshold', "
    "CAST(CAST(:threshold AS real) AS text), true)"
)


async def search_corrections(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    query: str,
    typos: TypoTolerance,
    *,
    kind_ids: Sequence[str] | None = None,
    runtime: StoreRuntime | None = None,
) -> list[Correction]:
    """Corrections of the words of `query` (web search syntax) the Records of `kind_ids`
    (`None`: every Kind) on the branch do not hold, from words of those Kinds, in word order;
    empty when every word is known (or too short, has a digit or is negated).

    The plain SQL path sets `pg_trgm.similarity_threshold` for the transaction, the function
    only for its own call.
    """
    if typos.candidates < 1:
        return []
    settings = _runtime(runtime).settings
    layout = await resolve_layout(session, domain_id, branch)
    params: dict[str, Any] = {
        "domain": domain_id,
        "config": settings.materialize.text_search_config,
        "query": query,
        "candidates": _cap(typos.candidates, settings.reads.max_search_rows),
        "min_length": typos.min_length,
        "kinds": None if kind_ids is None else list(kind_ids),
    }
    if _definer(runtime):
        record_definer_call("lb_search_corrections")
        params.update(branch=layout.branch, threshold=typos.threshold)
        statement = _CORRECTIONS_DEFINER
    else:
        await session.execute(_SIMILARITY_THRESHOLD, {"threshold": typos.threshold})
        params["branches"] = [layout.branch, *([layout.base] if layout.base else [])]
        statement = _CORRECTIONS_SQL
    found: dict[str, list[str]] = {}
    for word, replacement in (await session.execute(statement, params)).all():
        found.setdefault(word, []).append(replacement)
    return [Correction(word, tuple(replacements)) for word, replacements in found.items()]


def _corrected_query(corrections: int, query: str = ":query") -> str:
    """The query (web search syntax; `query` is its SQL expression) with the words of
    `corrections` replacements rewritten, as SQL text over `:word_<n>` and
    `:replacements_<n>` (`lb_lexeme_query`, as `lb_search_corrected` does)."""
    fixed = f"websearch_to_tsquery(CAST(:config AS regconfig), {query})"
    for n in range(corrections):
        fixed = (
            f"ts_rewrite({fixed}, lb_lexeme_query(:word_{n}), "
            f"lb_lexeme_query(:word_{n}, CAST(:replacements_{n} AS text[])))"
        )
    return fixed


def search_sql(*, by_kind: bool, corrections: int = 0, records: str = "mat_records m") -> str:
    """Full-text search: the GIN index finds at most `:candidates` matches of the branch, and
    only those are ranked (`ts_rank_cd` reads each candidate's vector).

    Parameters: `:domain`, `:branch`, `:config`, `:query` (web search syntax), `:candidates`,
    `:limit`, `:kinds` (text array) when `by_kind`. With `corrections`, `:word_<n>`,
    `:replacements_<n>` (text array) for each and `:weight`: up to `:candidates` more rows
    that match only the corrected query come after the exact matches, ranked at `:weight`
    times their rank. `records` is the FROM item of the rows, aliased `m`
    (`BranchLayout.records_from("m")`). Every row's `truncated` says whether the candidates
    of either kind filled `:candidates` (revision 0035, `lb_search_bounded`).
    """
    kinds = "AND m.kind_id = ANY(CAST(:kinds AS text[])) " if by_kind else ""

    def rows(exact: bool, match: str) -> str:
        return (
            f"SELECT m.kind_id, m.key, m.title, m.search, {str(exact).lower()} AS exact "
            f"FROM {records} WHERE {_SCOPE} {kinds}AND {match} LIMIT :candidates"
        )

    if not corrections:
        return (
            f"SELECT c.kind_id, c.key, c.title, "
            f"CAST(ts_rank_cd(c.search, {_QUERY}) AS real) AS score, c.exact, "
            "count(*) OVER () >= :candidates AS truncated "
            f"FROM ({rows(True, f'm.search @@ {_QUERY}')}) AS c "
            f"ORDER BY score DESC, c.kind_id, c.key LIMIT :limit"
        )
    fixed = _corrected_query(corrections)
    score = (
        f"CASE WHEN c.exact THEN ts_rank_cd(c.search, {_QUERY}) "
        f"ELSE CAST(:weight AS real) * ts_rank_cd(c.search, {fixed}) END"
    )
    return (
        f"SELECT c.kind_id, c.key, c.title, CAST({score} AS real) AS score, c.exact, "
        "(count(*) FILTER (WHERE c.exact) OVER () >= :candidates "
        "OR count(*) FILTER (WHERE NOT c.exact) OVER () >= :candidates) AS truncated "
        f"FROM (({rows(True, f'm.search @@ {_QUERY}')}) UNION ALL "
        f"({rows(False, f'm.search @@ {fixed} AND NOT m.search @@ {_QUERY}')})) AS c "
        f"ORDER BY c.exact DESC, score DESC, c.kind_id, c.key LIMIT :limit"
    )


@lru_cache(maxsize=256)
def _search(records: str, by_kind: bool, corrections: int) -> TextClause:
    return text(search_sql(by_kind=by_kind, corrections=corrections, records=records))


_SEARCH_DEFINER = {
    function: text(
        f"SELECT * FROM {function}(:domain, :branch, CAST(:kinds AS text[]), "
        "CAST(:config AS regconfig), :query, CAST(:candidates AS integer), "
        "CAST(:limit AS integer), CAST(:words AS text[]), CAST(:replacements AS text[]), "
        "CAST(:weight AS real))"
    )
    for function in ("lb_search_bounded", "lb_search_bounded_overlay")
}


class SearchHits(list[SearchHit]):
    """Hits of `search_records`, best first. `truncated`: a candidate cap cut the ranking, so
    the hits are the best of the first matches in index order, not of every match."""

    def __init__(self, hits: Iterable[SearchHit] = (), *, truncated: bool = False) -> None:
        super().__init__(hits)
        self.truncated = truncated


@dataclass(frozen=True, slots=True)
class CommonTerms:
    """How many matches search ranks, chosen per query from the word lexicon (`mat_lexicon`
    counts, the base's added on an overlay).

    The lexicon gives an upper bound of the matches of a query: a lexeme matches at most the
    Records counted for it, `a & b` at most the fewer of the two, `a | b` at most their sum
    (a negation or a word the lexicon does not hold, one with a digit, leaves the bound
    open). Within `docs`, every match is ranked (`docs` candidates: an exact ranking).
    Otherwise, when every term is very common, only the first `candidates` matches in index
    order are ranked and the result says it is truncated, so that callers narrow the query.
    """

    docs: int
    candidates: int


_TERM_COUNTS_SQL = """
SELECT q.query, w.word, (
    SELECT sum(l.records) FROM mat_lexicon l
    WHERE l.domain_id = :domain AND l.branch = ANY(CAST(:branches AS text[]))
      {kinds}AND l.word = w.word AND l.records > 0) AS records
FROM (SELECT CAST(websearch_to_tsquery(CAST(:config AS regconfig), :query) AS text) AS query) q
LEFT JOIN LATERAL (
    SELECT DISTINCT x.word FROM ({words} UNION ALL
                                 SELECT unnest(CAST(:extra AS text[]))) AS x) AS w ON true
"""
_TERM_COUNTS = {
    by_kind: text(
        _TERM_COUNTS_SQL.format(
            kinds="AND l.kind_id = ANY(CAST(:kinds AS text[])) " if by_kind else "",
            words=_WORDS.replace("CAST(" + _QUERY + " AS text)", "q.query"),
        )
    )
    for by_kind in (False, True)
}
"""The query as full-text search reads it (tsquery text), and the Records of the branch that
hold each of its lexemes but the negated ones, and each word of `:extra` (one row each)."""

_TSQUERY_TOKENS = re.compile(r"'((?:[^']|'')*)'(?::[*A-D]+)?|<\d+>|<->|[&|!()]")


def _lexicon_word(word: str) -> bool:
    """Whether the lexicon holds `word` when the branch does (`lb_lexicon_word`)."""
    return not any(c.isdigit() for c in word) and len(word) <= 64


@dataclass(frozen=True, slots=True)
class MatchBound:
    """Upper bounds of the Records a query matches (`None`: the lexicon cannot say)."""

    query: int | None
    """Of the query itself."""
    any_term: int | None
    """Of any of its positive lexemes, the alternatives of partial matching."""


def match_bound(tsquery: str, records: Callable[[str], int | None]) -> MatchBound:
    """The bounds of `tsquery` (its text form) from `records(lexeme)`, the Records holding a
    lexeme (`None` when unknown). Operators bind as tsquery prints them: `!`, then `<->` and
    `<N>`, then `&`, then `|`."""
    tokens: list[str | re.Match[str]] = [
        m if m.group(1) is not None else m.group(0) for m in _TSQUERY_TOKENS.finditer(tsquery)
    ]
    if not tokens:
        return MatchBound(0, 0)
    position = 0
    positive: list[int | None] = []

    def peek() -> str | re.Match[str] | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str | re.Match[str]:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def operand(negated: bool) -> int | None:
        token = take()
        if token == "!":
            operand(not negated)
            return None  # NOT x: open
        if token == "(":
            found = alternatives(negated)
            take()  # ")"
            return found
        assert isinstance(token, re.Match)
        count = records(token.group(1).replace("''", "'"))
        if not negated:
            positive.append(count)
        return count

    def conjunction(negated: bool, operator: str) -> int | None:
        inner = (lambda n: conjunction(n, "<")) if operator == "&" else operand
        bound = inner(negated)
        while isinstance(token := peek(), str) and token.startswith(operator):
            take()
            other = inner(negated)
            # x AND y: the smaller bound; an open side leaves the other.
            bound = other if bound is None else bound if other is None else min(bound, other)
        return bound

    def alternatives(negated: bool) -> int | None:
        bound = conjunction(negated, "&")
        while peek() == "|":
            take()
            other = conjunction(negated, "&")
            bound = None if bound is None or other is None else bound + other
        return bound

    bound = alternatives(False)
    any_term = None if None in positive else sum(c for c in positive if c is not None)
    return MatchBound(bound, any_term)


async def _match_bound(
    session: AsyncSession,
    layout: BranchLayout,
    query: str,
    fixes: Sequence[Correction],
    kind_ids: Sequence[str] | None,
    config: str,
) -> MatchBound:
    """`match_bound` of `query` with the lexicon's counts, each corrected word counting the
    Records of its replacements too (one round trip)."""
    extra = sorted({r for c in fixes for r in c.replacements})
    params: dict[str, Any] = {
        "domain": layout.domain_id,
        "branches": [layout.branch, *([layout.base] if layout.base else [])],
        "config": config,
        "query": query,
        "extra": extra,
    }
    if kind_ids is not None:
        params["kinds"] = list(kind_ids)
    rows = (await session.execute(_TERM_COUNTS[kind_ids is not None], params)).all()
    if not rows:
        return MatchBound(0, 0)
    tsquery = rows[0][0] or ""
    counts = {word: int(found or 0) for _, word, found in rows if word is not None}
    widened = {c.word: c.replacements for c in fixes}

    def records(word: str) -> int | None:
        if not _lexicon_word(word):
            return None
        return counts.get(word, 0) + sum(counts.get(r, 0) for r in widened.get(word, ()))

    return match_bound(tsquery, records)


def _candidates(wanted: int, ceiling: int) -> int:
    """The candidates argument for `wanted` matches: one more, so that the statement fills it
    (`truncated`) only when more matches exist than were wanted."""
    return max(1, min(wanted + 1, ceiling))


def _wanted(bound: int | None, common: CommonTerms) -> int:
    """Matches to rank: all of them when the bound is within `common.docs`, else a sample."""
    return common.docs if bound is not None and bound <= common.docs else common.candidates


@dataclass(frozen=True, slots=True)
class PartialMatching:
    """Ranked partial matching of multi-word queries.

    Search matches every term of the query first. When that finds fewer than `min_results`
    Records, the Records holding at least one term follow (`partial_terms`): at most
    `candidates` of them, best by `ts_rank_cd` of the terms joined by OR, are scored
    `weight * (rank + term_bonus * matched terms)` and ranked after every full match.
    `stop_words` names a text search configuration whose stop words are left out of the
    terms (`None`: only those of the search configuration, which drops them anyway).
    """

    min_results: int
    weight: float
    term_bonus: float
    candidates: int
    stop_words: str | None = None


@dataclass(frozen=True, slots=True)
class PartialQuery:
    """The positive terms of a query (words and quoted phrases) and its negated ones."""

    terms: tuple[str, ...]
    negated: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """The query matching any term, in web search syntax, each alternative carrying the
        negations of the original."""
        suffix = "".join(f" {n}" for n in self.negated)
        return " or ".join(f"{t}{suffix}" for t in self.terms)


_TOKENS = re.compile(r'(-?)"([^"]*)"?|([^\s"]+)')


def partial_query(query: str) -> PartialQuery | None:
    """The terms of `query` (web search syntax), or `None` when it has no positive term.
    Negations (`-word`, `-"a phrase"`) apply to every alternative and `or` is dropped; a term
    repeated in another case counts once."""
    terms: dict[str, str] = {}
    negated: list[str] = []
    for sign, phrase, word in _TOKENS.findall(query):
        if word:
            if word.lower() == "or" or word == "-":
                continue
            if word.startswith("-"):
                negated.append(word)
                continue
            term = word
        else:
            if not phrase.strip():
                continue
            term = f'"{phrase.strip()}"'
            if sign:
                negated.append(f"-{term}")
                continue
        terms.setdefault(term.lower(), term)
    if not terms:
        return None
    return PartialQuery(tuple(terms.values()), tuple(negated))


_CONTENT_TERMS = text(
    "SELECT t.term FROM unnest(CAST(:terms AS text[])) WITH ORDINALITY AS t(term, n) "
    "WHERE numnode(websearch_to_tsquery(CAST(:config AS regconfig), t.term)) > 0 ORDER BY t.n"
)


async def partial_terms(
    session: AsyncSession, query: str, stop_words: str | None
) -> PartialQuery | None:
    """The terms partial matching searches for `query`: `None` when it has fewer than two
    terms (a partial match would be a full one) or none is left once the stop words of the
    configuration `stop_words` are dropped (one round trip)."""
    found = partial_query(query)
    if found is None or len(found.terms) < 2:
        return None
    if stop_words is None:
        return found
    rows = await session.execute(_CONTENT_TERMS, {"terms": list(found.terms), "config": stop_words})
    kept = tuple(rows.scalars())
    return PartialQuery(kept, found.negated) if kept else None


def _matched_sql(records: str, corrections: int, *, overlay: bool) -> str:
    """How many of `:terms` (each in web search syntax, its corrected words widened as in
    `search_sql`) each Record of `:kinds`/`:keys` holds, read by primary key."""
    fixed = _corrected_query(corrections, "t.term")
    matched = (
        f"(SELECT count(*) FROM unnest(CAST(:terms AS text[])) AS t(term) "
        f"WHERE m.search @@ {fixed})"
    )
    identities = "unnest(CAST(:kinds AS text[]), CAST(:keys AS text[])) AS i(kind_id, key)"
    if overlay:
        # Each identity looked up laterally reaches both halves of the overlay by key.
        return (
            f"SELECT i.kind_id, i.key, m.matched FROM {identities} "
            f"CROSS JOIN LATERAL (SELECT {matched} AS matched FROM {records} "
            f"WHERE {_SCOPE} AND m.kind_id = i.kind_id AND m.key = i.key LIMIT 1) AS m"
        )
    return (
        f"SELECT i.kind_id, i.key, {matched} AS matched FROM {identities} "
        f"JOIN {records} ON {_SCOPE} AND m.kind_id = i.kind_id AND m.key = i.key"
    )


@lru_cache(maxsize=256)
def _matched(records: str, corrections: int, overlay: bool) -> TextClause:
    return text(_matched_sql(records, corrections, overlay=overlay))


async def search_records(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
    query: str,
    *,
    limit: int,
    kind_ids: Sequence[str] | None = None,
    corrections: Sequence[Correction] = (),
    correction_weight: float = 1.0,
    partial: PartialMatching | None = None,
    common: CommonTerms | None = None,
    runtime: StoreRuntime | None = None,
) -> SearchHits:
    """Best `limit` lexical matches of `query` (web search syntax) on the branch head.

    `kind_ids` restricts the Kinds searched (`None`: every Kind). `corrections`
    (`search_corrections`) widens each corrected word to its replacements: hits found only
    that way have `exact` false and come after the others, ranked at `correction_weight`
    times their rank. With `partial`, fewer than `partial.min_results` hits are followed by
    Records that hold only some of the terms (`partial` true; `PartialMatching`).

    At most `store.reads.search_candidates` matches are ranked, or with `common` as many as
    the word lexicon says the query needs (`CommonTerms`, one more round trip); the result
    is `truncated` when a statement filled its candidates.
    """
    settings = _runtime(runtime).settings
    layout = await resolve_layout(session, domain_id, branch)
    fixes = [c for c in corrections if c.replacements]
    cap = _cap(limit, settings.reads.max_search_rows)
    config = settings.materialize.text_search_config
    ceiling = settings.reads.definer_ceilings.candidates
    bound = None
    wanted = settings.reads.search_candidates
    if common is not None:
        bound = await _match_bound(session, layout, query, fixes, kind_ids, config)
        wanted = _wanted(bound.query, common)
    params: dict[str, Any] = {
        "domain": domain_id,
        "branch": layout.branch,
        "config": config,
        "candidates": _candidates(wanted, ceiling),
        "weight": correction_weight,
    }
    kinds = None if kind_ids is None else list(kind_ids)
    definer = _definer(runtime)
    if definer:
        params.update(
            kinds=kinds,
            words=[c.word for c in fixes for _ in c.replacements],
            replacements=[r for c in fixes for r in c.replacements],
        )
        statement = _SEARCH_DEFINER[_function("lb_search_bounded", layout)]
    else:
        for n, correction in enumerate(fixes):
            params[f"word_{n}"] = correction.word
            params[f"replacements_{n}"] = list(correction.replacements)
        if kinds is not None:
            params["kinds"] = kinds
        statement = _search(layout.records_from("m", probe=False), kinds is not None, len(fixes))
    rows = (await session.execute(statement, {**params, "query": query, "limit": cap})).all()
    hits = SearchHits(
        (
            SearchHit(kind_id, key, title, float(score), bool(exact))
            for kind_id, key, title, score, exact, _ in rows
        ),
        truncated=bool(rows and rows[0][5]),
    )
    if partial is None or len(hits) >= min(partial.min_results, cap):
        return hits
    alternatives = await partial_terms(session, query, partial.stop_words)
    if alternatives is None:
        return hits
    if definer:
        _function("lb_search_bounded", layout)
    found = {(h.kind_id, h.key) for h in hits}
    others: dict[str, Any] = {
        "query": alternatives.text,
        "limit": _cap(partial.candidates, settings.reads.max_search_rows),
    }
    if common is not None and bound is not None:
        others["candidates"] = _candidates(_wanted(bound.any_term, common), ceiling)
    found_rows = (await session.execute(statement, {**params, **others})).all()
    truncated = hits.truncated or bool(found_rows and found_rows[0][5])
    matches = [
        (kind_id, key, title, float(score), bool(exact))
        for kind_id, key, title, score, exact, _ in found_rows
        if (kind_id, key) not in found
    ]
    if not matches:
        return SearchHits(hits, truncated=truncated)
    matched = await _matched_terms(
        session,
        domain_id,
        layout,
        [(c[0], c[1]) for c in matches],
        alternatives,
        fixes,
        params["config"],
    )
    scored = [
        SearchHit(
            kind_id,
            key,
            title,
            partial.weight * (rank + partial.term_bonus * matched.get((kind_id, key), 0)),
            exact,
            partial=True,
        )
        for kind_id, key, title, rank, exact in matches
    ]
    scored.sort(key=lambda h: (-h.score, h.kind_id, h.key))
    return SearchHits([*hits, *scored[: cap - len(hits)]], truncated=truncated)


async def _matched_terms(
    session: AsyncSession,
    domain_id: str,
    layout: BranchLayout,
    identities: Sequence[tuple[str, str]],
    alternatives: PartialQuery,
    fixes: Sequence[Correction],
    config: str,
) -> dict[tuple[str, str], int]:
    """Terms of `alternatives` each Record holds (corrections applied), by primary key."""
    params: dict[str, Any] = {
        "domain": domain_id,
        "branch": layout.branch,
        "config": config,
        "kinds": [kind for kind, _ in identities],
        "keys": [key for _, key in identities],
        "terms": list(alternatives.terms),
    }
    for n, correction in enumerate(fixes):
        params[f"word_{n}"] = correction.word
        params[f"replacements_{n}"] = list(correction.replacements)
    statement = _matched(layout.records_from("m"), len(fixes), layout.overlay)
    rows = (await session.execute(statement, params)).all()
    return {(kind_id, key): int(count) for kind_id, key, count in rows}


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
