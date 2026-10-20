"""Neighborhood traversal over the materialized head (design 8.3).

`neighborhood` walks breadth first from seed Records along Links, in one SQL statement: a
recursive CTE whose recursive step joins each frontier Record laterally to its Links (stored
Links through the two `mat_links` indexes, foreign-key backed Links through the Records and
their per-Attribute index), at most `fanout` Links per Record and hop. The walk never
re-enters a Record already on its own path (`CYCLE`), and it is consumed lazily: the outer
`LIMIT` on Links walked stops the recursion, so caps bound the work, not only the result.
The same statement hydrates the Records reached, nearest first up to the node cap, and returns
the Links walked between them: one round trip.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Integer, LargeBinary, Text, column, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Relation, Schema
from lorebase_store.indexes import predicate_sql, sql_literal, values_text
from lorebase_store.reads import (
    MAT_RECORDS,
    RECORD_NAMES,
    Direction,
    LinkRef,
    escape_colons,
    foreign_key_relations,
    stored_relation_ids,
)
from lorebase_store.repositories.materialized import RecordRow
from lorebase_store.runtime import StoreRuntime, default_runtime

_STEP_COLUMNS = (
    "kind_id, key, relation_id, from_kind, from_key, to_kind, to_key, link_values, link_hash"
)


@dataclass(frozen=True, slots=True)
class Neighbor:
    depth: int
    record: RecordRow


@dataclass(frozen=True, slots=True)
class Neighborhood:
    """Records reached (nearest first) and the Links walked between them.

    `truncated` is true when the Link or node cap cut the walk; the per-Record fan-out cap
    is not reported.
    """

    records: list[Neighbor]
    links: list[LinkRef]
    truncated: bool


class TraversalLimitError(ValueError):
    """A requested depth, seed count or cap exceeds the configured maximum."""


def _within(name: str, value: int, maximum: int) -> int:
    if value < 1:
        raise TraversalLimitError(f"{name} must be positive")
    if value > maximum:
        raise TraversalLimitError(f"{name} {value} exceeds the maximum {maximum}")
    return value


def _branches(
    stored_out: bool,
    stored_in: bool,
    foreign_out: bool,
    foreign_in: Sequence[Relation],
    domain_id: str,
    branch: str,
) -> list[str]:
    """Lateral sources of one hop from the frontier Record `w`."""
    links = (
        "JOIN mat_links l ON l.domain_id = :domain AND l.branch = :branch "
        "AND l.relation_id = r.relation_id"
    )
    parts: list[str] = []
    if stored_out:
        parts.append(
            "SELECT r.to_kind, l.to_key, l.relation_id, r.from_kind, l.from_key, r.to_kind, "
            f'l.to_key, l."values", l.object_hash FROM out_rels r {links} '
            "AND l.from_key = w.key WHERE r.from_kind = w.kind_id LIMIT :fanout"
        )
    if stored_in:
        parts.append(
            "SELECT r.from_kind, l.from_key, l.relation_id, r.from_kind, l.from_key, r.to_kind, "
            f'l.to_key, l."values", l.object_hash FROM in_rels r {links} '
            "AND l.to_key = w.key WHERE r.to_kind = w.kind_id LIMIT :fanout"
        )
    if foreign_out:
        parts.append(
            'SELECT f.to_kind, m."values" ->> f.attribute, f.relation_id, f.from_kind, m.key, '
            "f.to_kind, m.\"values\" ->> f.attribute, CAST('{}' AS jsonb), CAST(NULL AS bytea) "
            "FROM fk_rels f JOIN mat_records m ON m.domain_id = :domain AND m.branch = :branch "
            "AND m.kind_id = w.kind_id AND m.key = w.key "
            'WHERE f.from_kind = w.kind_id AND m."values" ->> f.attribute IS NOT NULL '
            "LIMIT :fanout"
        )
    for relation in foreign_in:
        assert relation.foreign_key_attribute is not None
        from_kind = sql_literal(relation.from_kind)
        to_kind = sql_literal(relation.to_kind)
        parts.append(
            f"SELECT CAST({from_kind} AS text), key, CAST({sql_literal(relation.id)} AS text), "
            f"CAST({from_kind} AS text), key, CAST({to_kind} AS text), w.key, "
            "CAST('{}' AS jsonb), CAST(NULL AS bytea) FROM mat_records "
            f"WHERE w.kind_id = {to_kind} "
            f"AND {escape_colons(predicate_sql(domain_id, branch, relation.from_kind))} "
            f"AND {values_text(relation.foreign_key_attribute)} = w.key LIMIT :fanout"
        )
    return parts


def neighborhood_sql(
    schema: Schema,
    domain_id: str,
    branch: str,
    direction: Direction,
    relation_ids: Sequence[str] | None,
    follow: Mapping[str, Direction] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The neighborhood statement and its schema-derived parameters.

    `follow` (Relation id to direction), when given, replaces `direction` and
    `relation_ids`: each Relation is followed in its own direction only.

    Other parameters: `:domain`, `:branch`, `:seed_kinds`, `:seed_keys`, `:depth`,
    `:fanout`, `:max_steps` (Links walked plus seeds) and `:max_nodes`.
    """
    if follow is None:
        wanted = None if relation_ids is None else set(relation_ids)
        follow = {
            r.id: direction for r in schema.relations.values() if wanted is None or r.id in wanted
        }
    relations = [r for r in sorted(schema.relations.values(), key=lambda r: r.id) if r.id in follow]
    outgoing = [r for r in relations if follow[r.id].outgoing]
    incoming = [r for r in relations if follow[r.id].incoming]
    out_rels = stored_relation_ids(outgoing)
    in_rels = stored_relation_ids(incoming)
    foreign_out = foreign_key_relations(outgoing)
    by_id = {r.id: r for r in relations}
    params: dict[str, Any] = {
        "out_ids": out_rels,
        "out_from": [by_id[r].from_kind for r in out_rels],
        "out_to": [by_id[r].to_kind for r in out_rels],
        "in_ids": in_rels,
        "in_from": [by_id[r].from_kind for r in in_rels],
        "in_to": [by_id[r].to_kind for r in in_rels],
        "fk_ids": [r.id for r in foreign_out],
        "fk_from": [r.from_kind for r in foreign_out],
        "fk_to": [r.to_kind for r in foreign_out],
        "fk_attributes": [r.foreign_key_attribute for r in foreign_out],
    }
    parts = _branches(
        bool(out_rels),
        bool(in_rels),
        bool(foreign_out),
        foreign_key_relations(incoming),
        domain_id,
        branch,
    )
    step = (
        "SELECT w.depth + 1, n.* FROM walk w CROSS JOIN LATERAL (SELECT * FROM ("
        + " UNION ALL ".join(f"({part})" for part in parts)
        + ") AS hop LIMIT :fanout) AS n WHERE w.depth < :depth"
        if parts
        else "SELECT w.depth + 1, w.kind_id, w.key, CAST(NULL AS text), CAST(NULL AS text), "
        "CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS jsonb), "
        "CAST(NULL AS bytea) FROM walk w WHERE false"
    )
    record_columns = ", ".join(f'm."{c}"' for c in RECORD_NAMES)
    sql = f"""
    WITH RECURSIVE
    out_rels(relation_id, from_kind, to_kind) AS (
        SELECT * FROM unnest(CAST(:out_ids AS text[]), CAST(:out_from AS text[]),
                             CAST(:out_to AS text[]))),
    in_rels(relation_id, from_kind, to_kind) AS (
        SELECT * FROM unnest(CAST(:in_ids AS text[]), CAST(:in_from AS text[]),
                             CAST(:in_to AS text[]))),
    fk_rels(relation_id, from_kind, to_kind, attribute) AS (
        SELECT * FROM unnest(CAST(:fk_ids AS text[]), CAST(:fk_from AS text[]),
                             CAST(:fk_to AS text[]), CAST(:fk_attributes AS text[]))),
    walk(depth, {_STEP_COLUMNS}) AS (
        SELECT 0, s.kind_id, s.key, CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS text),
               CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS jsonb), CAST(NULL AS bytea)
        FROM unnest(CAST(:seed_kinds AS text[]), CAST(:seed_keys AS text[])) AS s(kind_id, key)
        UNION ALL
        {step}
    ) CYCLE kind_id, key SET is_cycle USING path,
    walked AS MATERIALIZED (SELECT depth, {_STEP_COLUMNS} FROM walk LIMIT :max_steps),
    nodes AS MATERIALIZED (
        SELECT kind_id, key, min(depth) AS depth FROM walked GROUP BY kind_id, key
        ORDER BY min(depth), kind_id, key LIMIT :max_nodes + 1),
    links AS (
        SELECT DISTINCT ON (relation_id, from_key, to_key) relation_id, from_key, to_key,
               link_values, link_hash
        FROM walked w
        WHERE relation_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM nodes n WHERE n.kind_id = w.from_kind AND n.key = w.from_key)
          AND EXISTS (SELECT 1 FROM nodes n WHERE n.kind_id = w.to_kind AND n.key = w.to_key))
    SELECT 'record' AS item, n.depth, {record_columns},
           NULL AS relation_id, NULL AS from_key, NULL AS to_key,
           CAST(NULL AS jsonb) AS link_values, CAST(NULL AS bytea) AS link_hash
    FROM nodes n
    JOIN mat_records m ON m.domain_id = :domain AND m.branch = :branch
                      AND m.kind_id = n.kind_id AND m.key = n.key
    UNION ALL
    SELECT 'link', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           relation_id, from_key, to_key, link_values, link_hash
    FROM links
    UNION ALL
    SELECT 'steps', (SELECT count(*) FROM walked), NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL, NULL, NULL, NULL, NULL
    """
    return sql, params


_RESULT_COLUMNS: tuple[ColumnElement[Any], ...] = (
    column("item", Text),
    column("depth", Integer),
    *(MAT_RECORDS.c[name] for name in RECORD_NAMES),
    column("relation_id", Text),
    column("from_key", Text),
    column("to_key", Text),
    column("link_values", JSONB),
    column("link_hash", LargeBinary),
)


async def neighborhood(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    schema: Schema,
    seeds: Sequence[tuple[str, str]],
    *,
    depth: int = 2,
    direction: Direction = Direction.BOTH,
    relation_ids: Sequence[str] | None = None,
    follow: Mapping[str, Direction] | None = None,
    fanout: int | None = None,
    max_nodes: int | None = None,
    max_links: int | None = None,
    runtime: StoreRuntime | None = None,
) -> Neighborhood:
    """Records within `depth` hops of `seeds` (`(kind_id, key)` pairs) and the Links between.

    Links of every Relation in `relation_ids` (default: all) are followed in `direction`;
    `follow` (Relation id to direction) replaces both. Caps default to and may not exceed
    `store.traverse`. Seeds that do not exist are absent from the result.
    """
    caps = (runtime or default_runtime()).settings.traverse
    seeds = list(dict.fromkeys(seeds))
    _within("seed count", len(seeds), caps.max_seeds)
    nodes_cap = _within("max_nodes", max_nodes or caps.max_nodes, caps.max_nodes)
    links_cap = _within("max_links", max_links or caps.max_links, caps.max_links)
    sql, params = neighborhood_sql(schema, domain_id, branch, direction, relation_ids, follow)
    params.update(
        domain=domain_id,
        branch=branch,
        seed_kinds=[kind for kind, _ in seeds],
        seed_keys=[key for _, key in seeds],
        depth=_within("depth", depth, caps.max_depth),
        fanout=_within("fanout", fanout or caps.max_fanout, caps.max_fanout),
        max_steps=links_cap + len(seeds),
        max_nodes=nodes_cap,
    )
    statement = text(sql).columns(*_RESULT_COLUMNS)
    records: list[Neighbor] = []
    links: list[LinkRef] = []
    steps = 0
    for row in (await session.execute(statement, params)).mappings():
        if row["item"] == "record":
            record = RecordRow(**{name: row[name] for name in RECORD_NAMES})
            records.append(Neighbor(row["depth"], record))
        elif row["item"] == "link":
            links.append(
                LinkRef(
                    row["relation_id"],
                    row["from_key"],
                    row["to_key"],
                    row["link_values"],
                    row["link_hash"],
                )
            )
        else:
            steps = row["depth"]
    records.sort(key=lambda n: (n.depth, n.record.kind_id, n.record.key))
    truncated = steps >= params["max_steps"] or len(records) > nodes_cap
    if len(records) > nodes_cap:
        records = records[:nodes_cap]
    kept = {(n.record.kind_id, n.record.key) for n in records}
    links = [link for link in links if _between(schema, link, kept)]
    links.sort(key=lambda link: (link.relation_id, link.from_key, link.to_key))
    return Neighborhood(records, links, truncated)


def _between(schema: Schema, link: LinkRef, kept: set[tuple[str, str]]) -> bool:
    relation = schema.relations[link.relation_id]
    return (relation.from_kind, link.from_key) in kept and (relation.to_kind, link.to_key) in kept
