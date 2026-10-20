"""Consistency under the merged schema (design 6.8 step 4, invariant I1).

The checks are scoped to what the merge changed: the merged states of the elements changed on
either side, read together with the target head through an ``IndexView``. Violations become
cross-cutting Conflicts grouped per Kind or Relation and Attribute. Dangling Links of
``cascade`` Relations are dropped and recorded as automatic resolutions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from pydantic import JsonValue, ValidationError

from lorebase_core.canonical import ObjectType, canonical_bytes
from lorebase_core.merge.conflicts import ConflictKind, ResolutionChoice, Side
from lorebase_core.merge.data_merge import DataMerge, Origin
from lorebase_core.merge.objects import data_object_type, partition_element
from lorebase_core.merge.state import Context, Payload, Stage
from lorebase_core.model import (
    Cardinality,
    Link,
    OnDelete,
    Record,
    Relation,
    RelationBacking,
    Schema,
)
from lorebase_core.tree import (
    LINK_KEY_SEPARATOR,
    kind_partition,
    link_key,
    relation_partition,
    tree_get,
)
from lorebase_core.types import AttributeType
from lorebase_core.validate import IndexView, Issue, IssueCode, validate_link, validate_record

__all__ = ["MergedView", "check_consistency", "relaxed_cardinality"]

_ISSUE_KINDS: dict[IssueCode, ConflictKind] = {
    IssueCode.VALUE_ATTRIBUTE_UNKNOWN: ConflictKind.ORPHANED_VALUE,
    IssueCode.VALUE_TYPE: ConflictKind.TYPE_COERCION,
    IssueCode.VALUE_NOT_CANONICAL: ConflictKind.TYPE_COERCION,
    IssueCode.VALUE_REQUIRED: ConflictKind.MISSING_VALUE,
    IssueCode.RECORD_KIND_UNKNOWN: ConflictKind.ORPHANED_DATA,
    IssueCode.LINK_RELATION_UNKNOWN: ConflictKind.ORPHANED_DATA,
    IssueCode.LINK_FOREIGN_KEY_BACKED: ConflictKind.ORPHANED_DATA,
    IssueCode.RECORD_TRAIT_CONFORMANCE: ConflictKind.TRAIT_CONFORMANCE,
}
"""Issue codes of single-element validation and their Conflict kinds; others are constraints."""

_CARDINALITIES = {
    (True, True): Cardinality.ONE_TO_ONE,
    (True, False): Cardinality.MANY_TO_ONE,
    (False, True): Cardinality.ONE_TO_MANY,
    (False, False): Cardinality.MANY_TO_MANY,
}


def relaxed_cardinality(cardinality: Cardinality, side: str) -> Cardinality:
    """``cardinality`` without the "at most one Link" rule on ``side`` (``from`` or ``to``)."""
    single_from = cardinality in (Cardinality.ONE_TO_ONE, Cardinality.MANY_TO_ONE)
    single_to = cardinality in (Cardinality.ONE_TO_ONE, Cardinality.ONE_TO_MANY)
    if side == "from":
        single_from = False
    else:
        single_to = False
    return _CARDINALITIES[(single_from, single_to)]


class MergedView:
    """Records and Links after the merge: the target head overlaid with the merge set."""

    def __init__(self, index: IndexView, merge: DataMerge) -> None:
        self.index = index
        self.merge = merge
        self._from: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self._to: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for (partition, _), state in merge.present():
            if data_object_type(partition) is ObjectType.LINK:
                self.add(state)

    def add(self, state: Payload) -> None:
        relation, start, end = str(state["relation"]), str(state["from"]), str(state["to"])
        self._from[relation][start].add(end)
        self._to[relation][end].add(start)

    def drop(self, partition: str, key: str) -> None:
        state = self.merge.states.get((partition, key))
        if state is not None:
            relation, start, end = str(state["relation"]), str(state["from"]), str(state["to"])
            self._from[relation][start].discard(end)
            self._to[relation][end].discard(start)
        self.merge.states[(partition, key)] = None

    def record_exists(self, kind_id: str, key: str) -> bool:
        location = (kind_partition(kind_id), key)
        if location in self.merge.states:
            return self.merge.states[location] is not None
        return self.index.record_exists(kind_id, key)

    def links(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> set[tuple[str, str]]:
        """``(from_key, to_key)`` of the Links with the given endpoint after the merge."""
        partition = relation_partition(relation_id)
        found = {
            pair
            for pair in self.index.links_of(relation_id, from_key=from_key, to_key=to_key)
            if (partition, link_key(*pair)) not in self.merge.states
        }
        if from_key is not None:
            found.update((from_key, end) for end in self._from[relation_id].get(from_key, ()))
        if to_key is not None:
            found.update((start, to_key) for start in self._to[relation_id].get(to_key, ()))
        return found


def check_consistency(ctx: Context, schema: Schema, merge: DataMerge, index: IndexView) -> None:
    """Run the consistency rules of design 6.8 step 4 on the merge set."""
    checker = _Checker(ctx, schema, merge, MergedView(index, merge))
    checker.dangling_links()
    checker.elements()
    checker.references()
    checker.uniqueness()
    checker.cardinality()
    checker.required_relations()


class _Checker:
    def __init__(self, ctx: Context, schema: Schema, merge: DataMerge, view: MergedView) -> None:
        self.ctx = ctx
        self.schema = schema
        self.merge = merge
        self.view = view

    def checked(self, location: tuple[str, str]) -> bool:
        return location[0] not in self.ctx.blocked and location not in self.ctx.open_keys

    def add(
        self,
        kind: ConflictKind,
        partition: str,
        attribute: str | None,
        member: str,
        detail: JsonValue,
        message: str,
        *,
        values_from: str | None = None,
    ) -> None:
        element = partition_element(partition)
        group = self.ctx.group(
            kind,
            partition,
            None,
            attribute,
            stage=Stage.CHECK,
            element=element,
            message=message,
        )
        if values_from is not None and element is not None and not group.members:
            group.values = self.ctx.property_values(element, values_from)
        group.members.setdefault(member, detail)

    # Dangling Links ----------------------------------------------------------------

    def dangling_links(self) -> None:
        cascaded: dict[str, list[str]] = defaultdict(list)
        candidates: list[tuple[Relation, str, str, str]] = []
        for (partition, key), state in list(self.merge.present()):
            if data_object_type(partition) is not ObjectType.LINK:
                continue
            relation = self.schema.relations.get(str(state["relation"]))
            if relation is not None and relation.backing is RelationBacking.EXPLICIT:
                candidates.append((relation, key, str(state["from"]), str(state["to"])))
        for (partition, key), merged in sorted(self.merge.states.items()):
            element = partition_element(partition)
            ours = self.merge.ours.get((partition, key))
            if merged is not None or ours is None or element is None:
                continue
            if data_object_type(partition) is ObjectType.LINK:
                continue
            for relation in self.schema.relations_of(element):
                if relation.backing is not RelationBacking.EXPLICIT:
                    continue
                pairs: set[tuple[str, str]] = set()
                if relation.from_kind == element:
                    pairs.update(self.view.index.links_of(relation.id, from_key=key))
                if relation.to_kind == element:
                    pairs.update(self.view.index.links_of(relation.id, to_key=key))
                link_partition = relation_partition(relation.id)
                for start, end in sorted(pairs):
                    location = (link_partition, link_key(start, end))
                    if location in self.merge.states:
                        continue
                    digest = tree_get(self.ctx.store, self.ctx.roots[Side.OURS], *location)
                    loaded = self.ctx.document(digest)
                    if loaded is None:
                        continue
                    self.merge.states[location] = loaded
                    self.merge.ours[location] = digest
                    self.merge.origins[location] = Origin()
                    self.view.add(loaded)
                    candidates.append((relation, location[1], start, end))
        for relation, key, start, end in candidates:
            partition = relation_partition(relation.id)
            missing = [
                [kind_id, endpoint]
                for kind_id, endpoint in ((relation.from_kind, start), (relation.to_kind, end))
                if not self.view.record_exists(kind_id, endpoint)
            ]
            if not missing:
                continue
            if relation.on_delete is OnDelete.CASCADE and self.ctx.policy.cascade_dangling:
                self.view.drop(partition, key)
                cascaded[partition].append(key)
            elif self.checked((partition, key)):
                self.add(
                    ConflictKind.DANGLING_LINK,
                    partition,
                    None,
                    key,
                    {"missing": missing},  # type: ignore[dict-item]
                    f"Links of {relation.api_name!r} point at deleted Records",
                    values_from="on_delete",
                )
        for partition, keys in sorted(cascaded.items()):
            element = partition_element(partition)
            conflict = self.ctx.conflict(
                ConflictKind.DANGLING_LINK,
                partition,
                None,
                None,
                self.ctx.property_values(element or "", "on_delete"),
                self.ctx.element_hashes(element),
                members=keys,
            )
            self.ctx.record_auto(conflict, ResolutionChoice.DROP, "policy:cascade_dangling")

    # Single elements -----------------------------------------------------------------

    def elements(self) -> None:
        for location, state in list(self.merge.present()):
            if not self.checked(location):
                continue
            partition, key = location
            for issue in self.validate(location, state):
                kind = _ISSUE_KINDS.get(issue.code, ConflictKind.CONSTRAINT)
                detail: dict[str, JsonValue] = {"code": issue.code.value, "message": issue.message}
                if kind is ConflictKind.TRAIT_CONFORMANCE:
                    detail["trait"] = issue.details.get("trait")
                self.add(kind, partition, issue.attribute, key, detail, issue.message)

    def validate(self, location: tuple[str, str], state: Payload) -> list[Issue]:
        try:
            if data_object_type(location[0]) is ObjectType.LINK:
                return validate_link(self.schema, Link.model_validate(state))
            return validate_record(self.schema, Record.model_validate(state))
        except ValidationError as exc:
            return [
                Issue(
                    code=IssueCode.VALUE_TYPE,
                    message=f"invalid element: {exc.errors()[0]['msg']}",
                    key=location[1],
                )
            ]

    # Reference Attributes ---------------------------------------------------------------

    def references(self) -> None:
        for location, state in list(self.merge.present()):
            partition, key = location
            element = partition_element(partition)
            kind = self.schema.kinds.get(element or "")
            if kind is None or not self.checked(location):
                continue
            values = state.get("values")
            if not isinstance(values, dict):
                continue
            for attribute in kind.attributes.values():
                spec = attribute.type.scalar
                if spec.name is not AttributeType.REF or spec.kind_ref is None:
                    continue
                target = spec.kind_ref
                missing = [
                    [target, ref]
                    for ref in _keys_in(values.get(attribute.id))
                    if not self.view.record_exists(target, ref)
                ]
                if missing:
                    self.add(
                        ConflictKind.DANGLING_LINK,
                        partition,
                        attribute.id,
                        key,
                        {"missing": missing},  # type: ignore[dict-item]
                        f"{attribute.api_name!r} references deleted Records",
                    )

    # Uniqueness ---------------------------------------------------------------------------

    def uniqueness(self) -> None:
        for kind in self.schema.kinds.values():
            unique = [a for a in kind.attributes.values() if a.unique]
            partition = kind_partition(kind.id)
            if not unique or partition in self.ctx.blocked:
                continue
            claims: dict[tuple[str, bytes], str] = {}
            for location, state in self.merge.present(partition):
                if location in self.ctx.open_keys:
                    continue
                key = location[1]
                values = state.get("values")
                if not isinstance(values, dict):
                    continue
                for attribute in unique:
                    value = values.get(attribute.id)
                    if value is None:
                        continue
                    claim = (attribute.id, canonical_bytes(value))
                    others = []
                    if claim in claims:
                        others.append(claims[claim])
                    else:
                        claims[claim] = key
                    owner = self.view.index.find_unique(kind.id, attribute.id, value)
                    if (
                        owner is not None
                        and owner != key
                        and (partition, owner) not in (self.merge.states)
                    ):
                        others.append(owner)
                    for other in others:
                        message = f"{attribute.api_name!r} value is used by several Records"
                        for member, peer in ((key, other), (other, key)):
                            self.add(
                                ConflictKind.UNIQUENESS,
                                partition,
                                attribute.id,
                                member,
                                {"value": value, "other": peer},
                                message,
                            )

    # Cardinality ----------------------------------------------------------------------------

    def cardinality(self) -> None:
        for relation in self.schema.relations.values():
            partition = relation_partition(relation.id)
            if relation.backing is not RelationBacking.EXPLICIT or partition in self.ctx.blocked:
                continue
            for side, single in (
                ("from", relation.single_per_from),
                ("to", relation.single_per_to),
            ):
                if not single:
                    continue
                endpoints = sorted(
                    {
                        str(state[side])
                        for location, state in self.merge.present(partition)
                        if location not in self.ctx.open_keys
                    }
                )
                for endpoint in endpoints:
                    pairs = (
                        self.view.links(relation.id, from_key=endpoint)
                        if side == "from"
                        else self.view.links(relation.id, to_key=endpoint)
                    )
                    if len(pairs) > 1:
                        self.add(
                            ConflictKind.CARDINALITY,
                            partition,
                            side,
                            endpoint,
                            {"links": [list(p) for p in sorted(pairs)]},
                            f"{relation.api_name!r} is {relation.cardinality}: competing Links",
                            values_from="cardinality",
                        )

    # Required relations -------------------------------------------------------------------------

    def required_relations(self) -> None:
        for relation in self.schema.relations.values():
            if not relation.required_from:
                continue
            partition = relation_partition(relation.id)
            records = kind_partition(relation.from_kind)
            if partition in self.ctx.blocked or records in self.ctx.blocked:
                continue
            missing: Iterable[str]
            if relation.backing is RelationBacking.FOREIGN_KEY:
                missing = self.missing_foreign_keys(relation, records)
            else:
                missing = self.missing_links(relation, partition, records)
            for key in missing:
                self.add(
                    ConflictKind.REQUIRED_RELATION,
                    partition,
                    None,
                    key,
                    None,
                    f"Records need at least one {relation.api_name!r} Link",
                    values_from="required_from",
                )

    def missing_links(self, relation: Relation, partition: str, records: str) -> list[str]:
        candidates = {
            location[1]
            for location, _ in self.merge.present(records)
            if location not in self.ctx.open_keys
        }
        for (link_partition, key), state in self.merge.states.items():
            if link_partition == partition and state is None:
                start = key.split(LINK_KEY_SEPARATOR, 1)[0]
                if self.view.record_exists(relation.from_kind, start):
                    candidates.add(start)
        return [k for k in sorted(candidates) if not self.view.links(relation.id, from_key=k)]

    def missing_foreign_keys(self, relation: Relation, records: str) -> list[str]:
        attribute = relation.foreign_key_attribute
        missing = []
        for location, state in self.merge.present(records):
            values = state.get("values")
            if attribute is None or location in self.ctx.open_keys or not isinstance(values, dict):
                continue
            if values.get(attribute) is None:
                missing.append(location[1])
        return missing


def _keys_in(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []
