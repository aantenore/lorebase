"""Neighborhood traversal over the materialized head (design 8.3).

`neighborhood` walks breadth first from seed Records along Links, in one SQL statement: a
recursive CTE whose recursive step joins each frontier Record laterally to its Links (stored
Links through the two `mat_links` indexes, foreign-key backed Links through the Records and
their per-Attribute index), at most `fanout` Links per Record and hop. The walk never
re-enters a Record already on its own path (`CYCLE`), and it is consumed lazily: the outer
`LIMIT` on Links walked stops the recursion, so caps bound the work, not only the result.
The same statement hydrates the Records reached, nearest first up to the node cap, and returns
the Links walked between them: one round trip.

Inbound foreign-key hops compare a JSON value, which row-level security keeps from being an
index condition; with `store.reads.use_definer_functions` the statement runs inside the
security definer function `lb_neighborhood` (revision 0005), which builds the same statement
from the Domain, branch and Relations it is given. On an overlay branch the walk reads the
resolved rows (`lorebase_store.layout`) and the function is `lb_neighborhood_overlay`
(revision 0010).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Integer, LargeBinary, Text, column, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Relation, Schema
from lorebase_store.indexes import predicate_sql, sql_literal, values_text
from lorebase_store.layout import BranchLayout, escape_colons, resolve_layout
from lorebase_store.reads import (
    MAT_RECORDS,
    RECORD_NAMES,
    Direction,
    LinkRef,
    foreign_key_relations,
    stored_relation_ids,
)
from lorebase_store.repositories.materialized import RecordRow
from lorebase_store.runtime import StoreRuntime, default_runtime

_STEP_COLUMNS = (
    "via_kind, via_key, kind_id, key, relation_id, from_kind, from_key, to_kind, to_key, "
    "link_values, link_hash"
)
_NULL_LINK = (
    "CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS text), "
    "CAST(NULL AS text), CAST(NULL AS jsonb), CAST(NULL AS bytea)"
)


@dataclass(frozen=True, slots=True)
class Neighbor:
    depth: int
    record: RecordRow


@dataclass(frozen=True, slots=True)
class Neighborhood:
    """Records reached (nearest first) and the Links walked between them.

    `truncated` is true when a cap may have cut the walk: the Link or node cap, or a Record
    that had `fanout` Links followed in one hop.
    """

    records: list[Neighbor]
    links: list[LinkRef]
    truncated: bool


@dataclass(frozen=True, slots=True)
class Hops:
    """Relations one neighborhood follows: forward (out) and backward (in), each stored or
    foreign-key backed."""

    stored_out: tuple[Relation, ...]
    stored_in: tuple[Relation, ...]
    foreign_out: tuple[Relation, ...]
    foreign_in: tuple[Relation, ...]

    def as_json(self) -> str:
        """The `p_relations` argument of `lb_neighborhood`."""

        def rows(relations: Sequence[Relation], attribute: bool) -> list[list[str | None]]:
            return [
                [r.id, r.from_kind, r.to_kind, *([r.foreign_key_attribute] if attribute else [])]
                for r in relations
            ]

        return json.dumps(
            {
                "out": rows(self.stored_out, False),
                "in": rows(self.stored_in, False),
                "fk_out": rows(self.foreign_out, True),
                "fk_in": rows(self.foreign_in, True),
            }
        )


class TraversalLimitError(ValueError):
    """A requested depth, seed count or cap exceeds the configured maximum."""


def _within(name: str, value: int, maximum: int) -> int:
    if value < 1:
        raise TraversalLimitError(f"{name} must be positive")
    if value > maximum:
        raise TraversalLimitError(f"{name} {value} exceeds the maximum {maximum}")
    return value


def plan_hops(
    schema: Schema,
    direction: Direction = Direction.BOTH,
    relation_ids: Sequence[str] | None = None,
    *,
    follow: Mapping[str, Direction] | None = None,
) -> Hops:
    """The Relations to follow: `direction` over `relation_ids` (all when `None`), or each
    Relation of `follow` (Relation id to direction) in its own direction only when given."""
    relations = sorted(schema.relations.values(), key=lambda r: r.id)
    if follow is not None:
        outgoing = {rid for rid, way in follow.items() if way.outgoing}
        incoming = {rid for rid, way in follow.items() if way.incoming}
    else:
        wanted = {r.id for r in relations} if relation_ids is None else set(relation_ids)
        outgoing = wanted if direction.outgoing else set()
        incoming = wanted if direction.incoming else set()
    out = [r for r in relations if r.id in outgoing]
    into = [r for r in relations if r.id in incoming]
    stored = set(stored_relation_ids(relations))
    return Hops(
        stored_out=tuple(r for r in out if r.id in stored),
        stored_in=tuple(r for r in into if r.id in stored),
        foreign_out=tuple(foreign_key_relations(out)),
        foreign_in=tuple(foreign_key_relations(into)),
    )


def _branches(hops: Hops, layout: BranchLayout) -> list[str]:
    """Lateral sources of one hop from the frontier Record `w`."""

    def links(near: str) -> str:
        scope = (
            "l.domain_id = :domain AND l.branch = :branch AND l.relation_id = r.relation_id "
            f"AND l.{near} = w.key"
        )
        if layout.overlay:
            # Lateral, so that the Relation and the frontier key reach both halves of the
            # overlay's Links as index conditions (OFFSET 0 keeps it a subquery).
            return (
                f"CROSS JOIN LATERAL (SELECT * FROM {layout.links_from('l')} WHERE {scope} "
                "OFFSET 0) AS l"
            )
        return f"JOIN mat_links l ON {scope}"

    parts: list[str] = []
    if hops.stored_out:
        parts.append(
            "SELECT r.to_kind, l.to_key, l.relation_id, r.from_kind, l.from_key, r.to_kind, "
            f'l.to_key, l."values", l.object_hash FROM out_rels r {links("from_key")} '
            "WHERE r.from_kind = w.kind_id LIMIT :fanout"
        )
    if hops.stored_in:
        parts.append(
            "SELECT r.from_kind, l.from_key, l.relation_id, r.from_kind, l.from_key, r.to_kind, "
            f'l.to_key, l."values", l.object_hash FROM in_rels r {links("to_key")} '
            "WHERE r.to_kind = w.kind_id LIMIT :fanout"
        )
    if hops.foreign_out:
        parts.append(
            'SELECT f.to_kind, m."values" ->> f.attribute, f.relation_id, f.from_kind, m.key, '
            "f.to_kind, m.\"values\" ->> f.attribute, CAST('{}' AS jsonb), CAST(NULL AS bytea) "
            f"FROM fk_rels f JOIN {layout.records_from('m')} ON m.domain_id = :domain "
            "AND m.branch = :branch "
            "AND m.kind_id = w.kind_id AND m.key = w.key "
            'WHERE f.from_kind = w.kind_id AND m."values" ->> f.attribute IS NOT NULL '
            "LIMIT :fanout"
        )
    for relation in hops.foreign_in:
        assert relation.foreign_key_attribute is not None
        from_kind = sql_literal(relation.from_kind)
        to_kind = sql_literal(relation.to_kind)
        predicate = predicate_sql(layout.domain_id, layout.branch, relation.from_kind)
        parts.append(
            f"SELECT CAST({from_kind} AS text), key, CAST({sql_literal(relation.id)} AS text), "
            f"CAST({from_kind} AS text), key, CAST({to_kind} AS text), w.key, "
            f"CAST('{{}}' AS jsonb), CAST(NULL AS bytea) FROM {layout.records_from()} "
            f"WHERE w.kind_id = {to_kind} AND {escape_colons(predicate)} "
            f"AND {values_text(relation.foreign_key_attribute)} = w.key LIMIT :fanout"
        )
    return parts


def neighborhood_sql(
    schema: Schema,
    domain_id: str,
    branch: str | BranchLayout,
    direction: Direction,
    relation_ids: Sequence[str] | None,
    *,
    follow: Mapping[str, Direction] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The neighborhood statement and its schema-derived parameters (see `plan_hops`).

    `follow` (Relation id to direction), when given, replaces `direction` and
    `relation_ids`: each Relation is followed in its own direction only.

    Other parameters: `:domain`, `:branch`, `:seed_kinds`, `:seed_keys`, `:depth`,
    `:fanout`, `:max_steps` (Links walked plus seeds) and `:max_nodes`. `lb_neighborhood`
    builds the same statement. A branch given by name is read as a full branch.
    """
    layout = branch if isinstance(branch, BranchLayout) else BranchLayout(domain_id, branch)
    hops = plan_hops(schema, direction, relation_ids, follow=follow)
    params: dict[str, Any] = {
        "out_ids": [r.id for r in hops.stored_out],
        "out_from": [r.from_kind for r in hops.stored_out],
        "out_to": [r.to_kind for r in hops.stored_out],
        "in_ids": [r.id for r in hops.stored_in],
        "in_from": [r.from_kind for r in hops.stored_in],
        "in_to": [r.to_kind for r in hops.stored_in],
        "fk_ids": [r.id for r in hops.foreign_out],
        "fk_from": [r.from_kind for r in hops.foreign_out],
        "fk_to": [r.to_kind for r in hops.foreign_out],
        "fk_attributes": [r.foreign_key_attribute for r in hops.foreign_out],
    }
    parts = _branches(hops, layout)
    step = (
        "SELECT w.depth + 1, w.kind_id, w.key, n.* FROM walk w CROSS JOIN LATERAL "
        "(SELECT * FROM ("
        + " UNION ALL ".join(f"({part})" for part in parts)
        + ") AS hop LIMIT :fanout) AS n WHERE w.depth < :depth"
        if parts
        else f"SELECT w.depth + 1, w.kind_id, w.key, w.kind_id, w.key, {_NULL_LINK} "
        "FROM walk w WHERE false"
    )
    record_columns = ", ".join(f'm."{c}"' for c in RECORD_NAMES)
    found = (
        "m.domain_id = :domain AND m.branch = :branch AND m.kind_id = n.kind_id AND m.key = n.key"
    )
    hydrate = (
        # A lateral lookup per node: the join keys reach both halves of an overlay's rows.
        # (LIMIT 1 keeps it a per-node subquery instead of a join with every row).
        f"CROSS JOIN LATERAL (SELECT * FROM {layout.records_from('m')} WHERE {found} LIMIT 1) AS m"
        if layout.overlay
        else f"JOIN mat_records m ON {found}"
    )
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
        SELECT 0, CAST(NULL AS text), CAST(NULL AS text), s.kind_id, s.key, {_NULL_LINK}
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
    {hydrate}
    UNION ALL
    SELECT 'link', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           relation_id, from_key, to_key, link_values, link_hash
    FROM links
    UNION ALL
    SELECT 'steps', (SELECT count(*) FROM walked), NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL, NULL, NULL, NULL, NULL
    UNION ALL
    SELECT 'fanout', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL
    WHERE EXISTS (
        SELECT 1 FROM walked WHERE depth > 0 GROUP BY depth, via_kind, via_key
        HAVING count(DISTINCT (relation_id, from_key, to_key)) >= :fanout)
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
_DEFINER = {
    function: text(
        f"SELECT * FROM {function}(:domain, :branch, CAST(:seeds AS jsonb), "
        "CAST(:depth AS integer), CAST(:relations AS jsonb), CAST(:fanout AS integer), "
        "CAST(:max_steps AS integer), CAST(:max_nodes AS integer))"
    ).columns(*_RESULT_COLUMNS)
    for function in ("lb_neighborhood", "lb_neighborhood_overlay")
}


async def neighborhood(
    session: AsyncSession,
    domain_id: str,
    branch: str | BranchLayout,
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

    Follows `direction` over `relation_ids` (every Relation when `None`), or exactly the
    `(relation id, direction)` pairs of `follow`. Caps default to and may not exceed
    `store.traverse`. Seeds that do not exist are absent from the result.
    """
    settings = (runtime or default_runtime()).settings
    caps = settings.traverse
    layout = await resolve_layout(session, domain_id, branch)
    seeds = list(dict.fromkeys(seeds))
    _within("seed count", len(seeds), caps.max_seeds)
    nodes_cap = _within("max_nodes", max_nodes or caps.max_nodes, caps.max_nodes)
    links_cap = _within("max_links", max_links or caps.max_links, caps.max_links)
    bounds: dict[str, Any] = {
        "domain": domain_id,
        "branch": layout.branch,
        "depth": _within("depth", depth, caps.max_depth),
        "fanout": _within("fanout", fanout or caps.max_fanout, caps.max_fanout),
        "max_steps": links_cap + len(seeds),
        "max_nodes": nodes_cap,
    }
    if settings.reads.use_definer_functions:
        hops = plan_hops(schema, direction, relation_ids, follow=follow)
        params = {
            **bounds,
            "seeds": json.dumps([list(seed) for seed in seeds]),
            "relations": hops.as_json(),
        }
        statement = _DEFINER["lb_neighborhood_overlay" if layout.overlay else "lb_neighborhood"]
    else:
        sql, params = neighborhood_sql(
            schema, domain_id, layout, direction, relation_ids, follow=follow
        )
        params.update(
            bounds,
            seed_kinds=[kind for kind, _ in seeds],
            seed_keys=[key for _, key in seeds],
        )
        statement = text(sql).columns(*_RESULT_COLUMNS)
    records: list[Neighbor] = []
    links: list[LinkRef] = []
    steps = 0
    saturated = False
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
        elif row["item"] == "fanout":
            saturated = True
        else:
            steps = row["depth"]
    records.sort(key=lambda n: (n.depth, n.record.kind_id, n.record.key))
    truncated = saturated or steps >= bounds["max_steps"] or len(records) > nodes_cap
    if len(records) > nodes_cap:
        records = records[:nodes_cap]
    kept = {(n.record.kind_id, n.record.key) for n in records}
    links = [link for link in links if _between(schema, link, kept)]
    links.sort(key=lambda link: (link.relation_id, link.from_key, link.to_key))
    return Neighborhood(records, links, truncated)


def _between(schema: Schema, link: LinkRef, kept: set[tuple[str, str]]) -> bool:
    relation = schema.relations[link.relation_id]
    return (relation.from_kind, link.from_key) in kept and (relation.to_kind, link.to_key) in kept
