"""In-memory repository and a two-Kind schema for merge tests."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue

from lorebase_core.canonical import ObjectType
from lorebase_core.merge import (
    InMemoryObjectStore,
    MergeAttempt,
    MergedTree,
    MergeInput,
    MergeOutcome,
    Resolution,
    load_schema,
    merge,
    put_schema,
)
from lorebase_core.model import (
    Attribute,
    Cardinality,
    Domain,
    Kind,
    Link,
    OnDelete,
    Record,
    Relation,
    Schema,
    Trait,
)
from lorebase_core.revision import InMemoryRevisionGraph
from lorebase_core.tree import (
    PARTITION_SCHEMA,
    InMemoryNodeStore,
    TreeChange,
    kind_partition,
    link_key,
    relation_partition,
    tree_apply,
    tree_entries,
    tree_get,
)
from lorebase_core.types import TypeSpec


def ident(prefix: str, number: int) -> str:
    """Deterministic, well-formed prefixed id."""
    return f"{prefix}_01JC0M{number:020d}"


S = TypeSpec.of("string")
INTEGER = TypeSpec.of("integer")

SUPPLIER = ident("knd", 1)
PART = ident("knd", 2)
CODE = ident("att", 11)
NAME = ident("att", 12)
COUNTRY = ident("att", 13)
RATING = ident("att", 14)
VAT = ident("att", 15)
NUMBER = ident("att", 21)
PART_NAME = ident("att", 22)
QTY = ident("att", 23)
SUPPLIED_BY = ident("rel", 31)
NAMED = ident("trt", 41)
LABEL = ident("att", 42)


def attribute(id_: str, api_name: str, spec: TypeSpec = S, **extra: Any) -> Attribute:
    return Attribute(id=id_, api_name=api_name, description=api_name, type=spec, **extra)


def base_schema() -> Schema:
    """``Supplier`` and ``Part`` with ``supplied_by`` (Part -> Supplier, many_to_one)."""
    supplier = Kind(
        id=SUPPLIER,
        api_name="Supplier",
        primary_key=(CODE,),
        title_attribute=NAME,
        attributes={
            CODE: attribute(CODE, "code", required=True),
            NAME: attribute(NAME, "name", required=True),
            COUNTRY: attribute(COUNTRY, "country"),
            RATING: attribute(RATING, "rating", INTEGER),
            VAT: attribute(VAT, "vat", unique=True),
        },
    )
    part = Kind(
        id=PART,
        api_name="Part",
        primary_key=(NUMBER,),
        title_attribute=PART_NAME,
        attributes={
            NUMBER: attribute(NUMBER, "number", required=True),
            PART_NAME: attribute(PART_NAME, "name", required=True),
            QTY: attribute(QTY, "qty", INTEGER),
        },
    )
    relation = Relation(
        id=SUPPLIED_BY,
        api_name="supplied_by",
        inverse_api_name="supplies",
        from_kind=PART,
        to_kind=SUPPLIER,
        cardinality=Cardinality.MANY_TO_ONE,
        on_delete=OnDelete.CASCADE,
    )
    return Schema(
        domain=Domain(id="test"),
        kinds={SUPPLIER: supplier, PART: part},
        relations={SUPPLIED_BY: relation},
    )


def named_trait(required: bool = False) -> Trait:
    from lorebase_core.model import TraitAttribute

    return Trait(
        id=NAMED,
        api_name="Named",
        attributes={LABEL: TraitAttribute(id=LABEL, api_name="label", type=S, required=required)},
    )


# Schema edits ------------------------------------------------------------------------


def replace_kind(schema: Schema, kind_id: str, **changes: Any) -> Schema:
    kind = schema.kinds[kind_id].model_copy(update=changes)
    return schema.model_copy(update={"kinds": {**schema.kinds, kind_id: kind}})


def replace_attribute(schema: Schema, kind_id: str, attribute_id: str, **changes: Any) -> Schema:
    kind = schema.kinds[kind_id]
    updated = kind.attributes[attribute_id].model_copy(update=changes)
    return replace_kind(schema, kind_id, attributes={**kind.attributes, attribute_id: updated})


def add_attribute(schema: Schema, kind_id: str, new: Attribute) -> Schema:
    kind = schema.kinds[kind_id]
    return replace_kind(schema, kind_id, attributes={**kind.attributes, new.id: new})


def remove_attribute(schema: Schema, kind_id: str, attribute_id: str) -> Schema:
    kind = schema.kinds[kind_id]
    attributes = {k: v for k, v in kind.attributes.items() if k != attribute_id}
    return replace_kind(schema, kind_id, attributes=attributes)


def replace_relation(schema: Schema, relation_id: str, **changes: Any) -> Schema:
    relation = schema.relations[relation_id].model_copy(update=changes)
    return schema.model_copy(update={"relations": {**schema.relations, relation_id: relation}})


def with_elements(schema: Schema, **collections: dict[str, Any]) -> Schema:
    return schema.model_copy(update=collections)


# Data ----------------------------------------------------------------------------------


_SUPPLIER_NAMES = {"country": COUNTRY, "rating": RATING, "vat": VAT}
_PART_NAMES = {"qty": QTY}


def supplier(code: str, name: str | None = None, **values: JsonValue) -> Record:
    """A Supplier; keyword values are named by api name (``vat``, ``country``, ``rating``)."""
    named = {_SUPPLIER_NAMES.get(k, k): v for k, v in values.items()}
    return Record(
        kind=SUPPLIER, key=code, values={CODE: code, NAME: name or f"Supplier {code}", **named}
    )


def part(number: str, name: str | None = None, **values: JsonValue) -> Record:
    """A Part; keyword values are named by api name (``qty``)."""
    named = {_PART_NAMES.get(k, k): v for k, v in values.items()}
    return Record(
        kind=PART, key=number, values={NUMBER: number, PART_NAME: name or f"Part {number}", **named}
    )


def supplied_by(part_number: str, supplier_code: str) -> Link:
    return Link(relation=SUPPLIED_BY, from_key=part_number, to_key=supplier_code)


def base_records() -> list[Record | Link]:
    return [
        supplier("S-1", vat="IT001", country="IT", rating="3"),
        supplier("S-2", vat="IT002", country="IT"),
        supplier("S-3", vat="IT003"),
        part("P-1", qty="10"),
        part("P-2"),
        part("P-3"),
        supplied_by("P-1", "S-1"),
        supplied_by("P-2", "S-2"),
    ]


# Repository -----------------------------------------------------------------------------


def location(item: Record | Link) -> tuple[str, str]:
    if isinstance(item, Record):
        return kind_partition(item.kind), item.key
    return relation_partition(item.relation), link_key(item.from_key, item.to_key)


@dataclass
class Edit:
    """Changes of one commit: an optional new schema, upserts and deletions."""

    schema: Schema | None = None
    put: list[Record | Link] = field(default_factory=list)
    delete: list[tuple[str, str]] = field(default_factory=list)

    def upsert(self, *items: Record | Link) -> Edit:
        self.put.extend(items)
        return self

    def remove(self, *items: Record | Link | tuple[str, str]) -> Edit:
        for item in items:
            self.delete.append(item if isinstance(item, tuple) else location(item))
        return self


class Repo:
    """Node store, object store and Revision graph in memory."""

    def __init__(self) -> None:
        self.store = InMemoryNodeStore()
        self.objects = InMemoryObjectStore()
        self.graph = InMemoryRevisionGraph()
        self.roots: dict[bytes, bytes] = {}

    # Writing ----------------------------------------------------------------------------

    def write(self, root: bytes | None, edit: Edit) -> bytes:
        changes: list[TreeChange] = []
        if edit.schema is not None:
            entries = put_schema(self.objects, edit.schema)
            keys = {e.key for e in entries}
            changes.extend(entries)
            if root is not None:
                changes.extend(
                    TreeChange(PARTITION_SCHEMA, key, None)
                    for key, _ in tree_entries(self.store, root, PARTITION_SCHEMA)
                    if key not in keys
                )
        for item in edit.put:
            object_type = ObjectType.RECORD if isinstance(item, Record) else ObjectType.LINK
            changes.append(
                TreeChange(*location(item), self.objects.put(object_type, item.content()))
            )
        changes.extend(TreeChange(p, k, None) for p, k in edit.delete)
        return tree_apply(self.store, root, changes)

    def commit(self, label: str, edit: Edit, *parents: bytes) -> bytes:
        """Write a Revision with ``parents``; its tree is the first parent's plus ``edit``."""
        root = self.write(self.roots[parents[0]] if parents else None, edit)
        return self.add(label, root, *parents)

    def add(self, label: str, root: bytes, *parents: bytes) -> bytes:
        revision = hashlib.sha256(label.encode()).digest()
        self.graph.add(revision, parents)
        self.roots[revision] = root
        return revision

    # Reading ------------------------------------------------------------------------------

    def schema(self, root: bytes) -> Schema:
        return load_schema(self.store, self.objects, root)

    def values(self, root: bytes, kind_id: str, key: str) -> dict[str, JsonValue] | None:
        digest = tree_get(self.store, root, kind_partition(kind_id), key)
        if digest is None:
            return None
        payload = self.objects.get(digest)
        assert isinstance(payload, dict)
        values = payload["values"]
        assert isinstance(values, dict)
        return values

    def has_link(self, root: bytes, relation_id: str, start: str, end: str) -> bool:
        partition = relation_partition(relation_id)
        return tree_get(self.store, root, partition, link_key(start, end)) is not None

    def keys(self, root: bytes, partition: str) -> set[str]:
        return {key for key, _ in tree_entries(self.store, root, partition)}

    # Merging ------------------------------------------------------------------------------

    def input(self, ours: bytes, theirs: bytes, **options: Any) -> MergeInput:
        return MergeInput(
            store=self.store,
            objects=self.objects,
            graph=self.graph,
            revision_root=self.roots.__getitem__,
            ours=ours,
            theirs=theirs,
            **options,
        )

    def merge(self, ours: bytes, theirs: bytes, **options: Any) -> MergeOutcome:
        return merge(self.input(ours, theirs, **options))


@dataclass
class Fork:
    """A base Revision and two divergent children."""

    repo: Repo
    base: bytes
    ours: bytes
    theirs: bytes

    def merge(self, **options: Any) -> MergeOutcome:
        return self.repo.merge(self.ours, self.theirs, **options)

    def reverse(self, **options: Any) -> MergeOutcome:
        return self.repo.merge(self.theirs, self.ours, **options)


def fork(
    ours: Edit,
    theirs: Edit,
    *,
    schema: Schema | None = None,
    data: Iterable[Record | Link] | None = None,
) -> Fork:
    repo = Repo()
    base = repo.commit(
        "base",
        Edit(schema=schema or base_schema(), put=list(base_records() if data is None else data)),
    )
    return Fork(repo, base, repo.commit("ours", ours, base), repo.commit("theirs", theirs, base))


def merged(outcome: MergeOutcome) -> MergedTree:
    assert isinstance(outcome, MergedTree), _describe(outcome)
    return outcome


def attempt(outcome: MergeOutcome) -> MergeAttempt:
    assert isinstance(outcome, MergeAttempt), "expected conflicts, the merge was clean"
    return outcome


def _describe(outcome: MergeOutcome) -> str:
    if isinstance(outcome, MergeAttempt):
        return "; ".join(
            f"{c.kind.value} {c.partition} {c.key} {c.attribute}" for c in outcome.conflicts
        )
    return ""


def resolve(conflict_fingerprint: str, choice: str, **extra: Any) -> Resolution:
    return Resolution(fingerprint=conflict_fingerprint, choice=choice, **extra)  # type: ignore[arg-type]
