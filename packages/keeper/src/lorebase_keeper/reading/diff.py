"""Diff of two Revisions and expansion of element changes (design 6.3, 6.7).

`compute_diff` loads, level by level and in batched queries, only the tree nodes that differ
between the two trees (equal subtrees are never read), then runs the core `tree_diff` over
them in memory. The result is the ordered list of `(partition, key, old, new)` hashes, capped
by `read.diff.max_changes`; partition filters are applied before any node is read.

`expand` turns hash-level changes into element changes: Records and Links attribute by
attribute, schema elements property by property, with api names taken from the newer schema
(the older one for removed elements).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import node_hash
from lorebase_core.model import Attribute, Schema
from lorebase_core.tree import (
    BranchNode,
    DiffEntry,
    MissingNodeError,
    PartitionRef,
    RootNode,
    Tree,
    decode_node,
    encode_node,
    tree_diff,
)
from lorebase_keeper.errors import VALIDATION_FAILED, ProblemError
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import Masking, relation_kinds
from lorebase_store.materialize.rows import PartitionType, parse_partition, split_link_key
from lorebase_store.nodestore import NodeRepository
from lorebase_store.objectstore import PgObjectStore


class ChangeType(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


class ValueChange(BaseModel):
    """One changed Attribute (Records, Links) or property (schema elements)."""

    name: str
    old: JsonValue = None
    new: JsonValue = None


class ChangeOut(BaseModel):
    """One changed element."""

    element: PartitionType
    change: ChangeType
    partition: str
    kind: str | None = None
    relation: str | None = None
    key: str | None = Field(default=None, description="Record key or schema element id.")
    from_key: str | None = None
    to_key: str | None = None
    element_type: str | None = Field(default=None, description="Schema object type.")
    api_name: str | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    changes: list[ValueChange] | None = None
    provenance: dict[str, str] | None = Field(
        default=None, description="Provenance entry per changed Attribute (changesets)."
    )


class PartitionSummary(BaseModel):
    partition: str
    element: PartitionType
    name: str
    added: int = 0
    removed: int = 0
    modified: int = 0


@dataclass(frozen=True, slots=True)
class HashChange:
    partition: str
    key: str
    old_hash: bytes | None
    new_hash: bytes | None
    provenance: Mapping[str, str] | None = None


class _Nodes:
    """Read-only in-memory `NodeStore` over prefetched node bodies."""

    def __init__(self) -> None:
        self.bodies: dict[bytes, bytes] = {}

    def get(self, node_hash: bytes) -> bytes | None:
        return self.bodies.get(node_hash)

    def put(self, node_hash: bytes, body: bytes) -> None:
        self.bodies[node_hash] = body

    def has(self, node_hash: bytes) -> bool:
        return node_hash in self.bodies


def too_large(message: str) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        message,
        extensions={
            "errors": [{"pointer": "/query/kinds", "message": message, "code": "diff_too_large"}]
        },
    )


async def compute_diff(
    session: AsyncSession,
    runtime: ReadRuntime,
    domain_id: str,
    root_a: bytes,
    root_b: bytes,
    *,
    partitions: set[str] | None = None,
) -> list[DiffEntry]:
    """Changes from tree `root_a` to tree `root_b`, in partition then key-hash order."""
    settings = runtime.store_settings
    cap = runtime.read.diff.max_changes
    nodes = NodeRepository(
        session,
        domain_id,
        writes=settings.writes,
        cache=runtime.store.node_cache,
        batch=settings.nodes.prefetch_batch,
    )
    tree_a = await nodes.load_tree(root_a)
    tree_b = await nodes.load_tree(root_b)
    selected = sorted(
        p
        for p in tree_a.partitions.keys() | tree_b.partitions.keys()
        if partitions is None or p in partitions
    )
    lower_bound = sum(
        abs(_count(tree_a, p) - _count(tree_b, p))
        for p in selected
        if _root(tree_a, p) != _root(tree_b, p)
    )
    if lower_bound > cap:
        raise too_large(
            f"The diff holds at least {lower_bound} changes, more than the limit of {cap}; "
            "narrow it with kinds or relations."
        )
    store = _Nodes()
    frontier = [
        (_root(tree_a, p), _root(tree_b, p))
        for p in selected
        if _root(tree_a, p) != _root(tree_b, p)
    ]
    while frontier:
        wanted = {h for pair in frontier for h in pair if h is not None and h not in store.bodies}
        store.bodies.update(await nodes.get_many(wanted))
        following: list[tuple[bytes | None, bytes | None]] = []
        for a, b in frontier:
            if a == b:
                continue
            children_a = _children(store, a)
            children_b = _children(store, b)
            if children_a is None and children_b is None:
                continue
            for index in sorted((children_a or {}).keys() | (children_b or {}).keys()):
                child_a = None if children_a is None else children_a.get(index)
                child_b = None if children_b is None else children_b.get(index)
                if child_a != child_b:
                    following.append((child_a, child_b))
        frontier = following
    root_a = _write_root(store, tree_a, selected)
    root_b = _write_root(store, tree_b, selected)
    entries: list[DiffEntry] = []
    try:
        for entry in tree_diff(store, root_a, root_b):
            entries.append(entry)
            if len(entries) > cap:
                raise too_large(
                    f"The diff holds more than {cap} changes; narrow it with kinds or relations."
                )
    except MissingNodeError as error:  # pragma: no cover - prefetch covers every read
        raise RuntimeError(f"tree node {error} was not prefetched") from error
    return entries


def _root(tree: Tree, partition: str) -> bytes | None:
    ref = tree.partitions.get(partition)
    return None if ref is None else ref.root


def _count(tree: Tree, partition: str) -> int:
    ref = tree.partitions.get(partition)
    return 0 if ref is None else ref.count


def _children(store: _Nodes, digest: bytes | None) -> dict[int, bytes] | None:
    """Children of a branch node; None for leaves and absent sides."""
    if digest is None:
        return None
    body = store.bodies.get(digest)
    if body is None:
        raise MissingNodeError(digest.hex())
    node = decode_node(body)
    return dict(node.children) if isinstance(node, BranchNode) else None


def _write_root(store: _Nodes, tree: Tree, partitions: Sequence[str]) -> bytes:
    """A root node holding only the selected partitions of `tree`."""
    kept: list[tuple[str, PartitionRef]] = [
        (p, tree.partitions[p]) for p in partitions if p in tree.partitions
    ]
    kept.sort(key=lambda item: item[0].encode("utf-8"))
    body = encode_node(RootNode(tuple(kept)))
    digest = node_hash(body)
    store.bodies[digest] = body
    return digest


# Expansion --------------------------------------------------------------------------------


def partition_name(partition: str, schemas: Sequence[Schema]) -> tuple[PartitionType, str]:
    """Type of a partition and the api name of its Kind or Relation (id when unknown)."""
    parsed = parse_partition(partition)
    for schema in schemas:
        if parsed.type is PartitionType.KIND and parsed.element_id in schema.kinds:
            return parsed.type, schema.kinds[parsed.element_id].api_name
        if parsed.type is PartitionType.RELATION and parsed.element_id in schema.relations:
            return parsed.type, schema.relations[parsed.element_id].api_name
    return parsed.type, "schema" if parsed.type is PartitionType.SCHEMA else parsed.element_id


def summarize(
    changes: Iterable[HashChange | DiffEntry], schemas: Sequence[Schema]
) -> list[PartitionSummary]:
    """Added, removed and modified elements per partition."""
    summary: dict[str, PartitionSummary] = {}
    for change in changes:
        old, new = _hashes(change)
        item = summary.get(change.partition)
        if item is None:
            element, name = partition_name(change.partition, schemas)
            item = summary[change.partition] = PartitionSummary(
                partition=change.partition, element=element, name=name
            )
        if old is None:
            item.added += 1
        elif new is None:
            item.removed += 1
        else:
            item.modified += 1
    return list(summary.values())


def _hashes(change: HashChange | DiffEntry) -> tuple[bytes | None, bytes | None]:
    if isinstance(change, HashChange):
        return change.old_hash, change.new_hash
    return change.hash_a, change.hash_b


def as_hash_change(entry: DiffEntry) -> HashChange:
    return HashChange(entry.partition, entry.key, entry.hash_a, entry.hash_b)


async def expand(
    session: AsyncSession,
    runtime: ReadRuntime,
    domain_id: str,
    changes: Sequence[HashChange],
    *,
    old_schema: Schema,
    new_schema: Schema,
    masking: Masking,
) -> list[ChangeOut]:
    """Element-level changes, attribute by attribute (see module doc), with the values the
    caller is not cleared for masked."""
    objects = PgObjectStore(
        session,
        domain_id,
        writes=runtime.store_settings.writes,
        batch=runtime.store_settings.materialize.read_batch_rows,
    )
    hashes = {h for c in changes for h in (c.old_hash, c.new_hash) if h is not None}
    bodies = {digest: stored.body for digest, stored in (await objects.get_many(hashes)).items()}
    schemas = (new_schema, old_schema)
    return [_expand(change, bodies, schemas, masking) for change in changes]


def _expand(
    change: HashChange,
    bodies: Mapping[bytes, Mapping[str, Any]],
    schemas: Sequence[Schema],
    masking: Masking,
) -> ChangeOut:
    old = None if change.old_hash is None else bodies.get(change.old_hash, {})
    new = None if change.new_hash is None else bodies.get(change.new_hash, {})
    kind = (
        ChangeType.ADDED
        if old is None
        else ChangeType.REMOVED
        if new is None
        else ChangeType.MODIFIED
    )
    element, name = partition_name(change.partition, schemas)
    out = ChangeOut(
        element=element,
        change=kind,
        partition=change.partition,
        old_hash=None if change.old_hash is None else change.old_hash.hex(),
        new_hash=None if change.new_hash is None else change.new_hash.hex(),
    )
    parsed = parse_partition(change.partition)
    if element is PartitionType.SCHEMA:
        body = new if new is not None else old or {}
        out.key = change.key
        out.api_name = body.get("api_name") or body.get("id")
        out.element_type = _schema_type(change.key, schemas)
        out.changes = _value_changes(old or {}, new or {}, lambda key: key)
        out.provenance = None if change.provenance is None else dict(change.provenance)
        return out
    names = _attribute_names(parsed.type, parsed.element_id, schemas)
    if element is PartitionType.KIND:
        out.kind = name
        out.key = change.key
    else:
        out.relation = name
        out.from_key, out.to_key = split_link_key(change.key)
    attributes, kinds = _attributes(parsed.type, parsed.element_id, schemas)
    out.changes = [
        ValueChange(
            name=value.name,
            old=masking.value(attributes.get(value.name), kinds, value.old),
            new=masking.value(attributes.get(value.name), kinds, value.new),
        )
        for value in _value_changes(
            (old or {}).get("values", {}), (new or {}).get("values", {}), lambda a: a
        )
    ]
    for value in out.changes:
        value.name = names.get(value.name, value.name)
    if change.provenance is not None:
        out.provenance = {names.get(a, a): p for a, p in sorted(change.provenance.items())}
    return out


def _attribute_names(
    partition_type: PartitionType, element_id: str, schemas: Sequence[Schema]
) -> dict[str, str]:
    names: dict[str, str] = {}
    for schema in reversed(schemas):  # older first, newer names win
        owner = (
            schema.kinds.get(element_id)
            if partition_type is PartitionType.KIND
            else schema.relations.get(element_id)
        )
        if owner is not None:
            names.update({a.id: a.api_name for a in owner.attributes.values()})
    return names


def _attributes(
    partition_type: PartitionType, element_id: str, schemas: Sequence[Schema]
) -> tuple[dict[str, Attribute], tuple[str, ...]]:
    """Attributes of a Kind or Relation by id (newer schemas win) and the api names of the
    Kinds holding their values, for masking."""
    found: dict[str, Attribute] = {}
    kinds: tuple[str, ...] = ()
    for schema in reversed(schemas):  # older first, newer definitions win
        if partition_type is PartitionType.KIND:
            kind = schema.kinds.get(element_id)
            kinds = (element_id,)
            if kind is not None:
                found.update(kind.attributes)
        else:
            relation = schema.relations.get(element_id)
            if relation is not None:
                found.update(relation.attributes)
                kinds = relation_kinds(schema, relation)
    return found, kinds


def _schema_type(element_id: str, schemas: Sequence[Schema]) -> str:
    for schema in schemas:
        if element_id == schema.domain.id:
            return "domain"
        for collection in ("kinds", "relations", "traits", "operations", "derivations", "sources"):
            if element_id in getattr(schema, collection):
                return collection.removesuffix("s")
    return "element"


def _value_changes(old: Mapping[str, Any], new: Mapping[str, Any], name: Any) -> list[ValueChange]:
    return [
        ValueChange(name=name(key), old=old.get(key), new=new.get(key))
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    ]
