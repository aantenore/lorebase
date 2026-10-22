"""Validation of a batch of data changes against a Schema and existing data (invariant I1).

Existing data is read through an ``IndexView`` the caller provides (for example the
materialized head of a branch). The batch is evaluated as a whole: a Record created in the
batch can be the endpoint of a Link created in the same batch, and a unique value released by
one Record can be taken by another.

Deleting a Record handles its Links by the Relation's ``on_delete``: ``cascade`` yields Link
deletions as derived changes, ``restrict`` yields a violation. ``ref`` attributes pointing at
a deleted Record always restrict the deletion.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue

from lorebase_core.model.attribute import Attribute
from lorebase_core.model.kind import Kind
from lorebase_core.model.record import Link, LinkIdentity, Record, RecordIdentity
from lorebase_core.model.relation import OnDelete, Relation, RelationBacking
from lorebase_core.model.schema import Schema
from lorebase_core.types import AttributeType, CanonicalValue
from lorebase_core.validate.changes import (
    DataChange,
    LinkDelete,
    LinkUpsert,
    RecordDelete,
    RecordUpsert,
)
from lorebase_core.validate.data_rules import validate_link, validate_record
from lorebase_core.validate.issues import Issue, IssueCode, PathPart

#: Number of example Links or Records listed in grouped issues.
_SAMPLE_SIZE = 10


class IndexView(Protocol):
    """Read access to the existing data a batch is validated against."""

    def record_exists(self, kind_id: str, key: str) -> bool:
        """Whether the Record exists."""
        ...

    def find_unique(self, kind_id: str, attribute_id: str, value: CanonicalValue) -> str | None:
        """Key of the Record of ``kind_id`` whose ``attribute_id`` equals ``value``, if any."""
        ...

    def keys_referencing(self, kind_id: str, attribute_id: str, key: str) -> Iterable[str]:
        """Keys of Records of ``kind_id`` whose ``ref`` attribute holds ``key`` (or contains it)."""
        ...

    def link_exists(self, relation_id: str, from_key: str, to_key: str) -> bool:
        """Whether the Link exists."""
        ...

    def count_links(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> int:
        """Number of Links of ``relation_id`` with the given endpoint(s)."""
        ...

    def links_of(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> Iterable[tuple[str, str]]:
        """``(from_key, to_key)`` of the Links of ``relation_id`` with the given endpoint(s)."""
        ...


@dataclass
class ChangesetReport:
    """Outcome of a batch validation: issues plus changes derived from the batch."""

    issues: list[Issue] = field(default_factory=list)
    #: Link deletions caused by ``on_delete = cascade``.
    derived_changes: list[DataChange] = field(default_factory=list)


@dataclass
class _Batch:
    schema: Schema
    index: IndexView
    report: ChangesetReport = field(default_factory=ChangesetReport)
    #: Final state of each Record and Link touched by the batch (``None`` when deleted).
    records: dict[RecordIdentity, Record | None] = field(default_factory=dict)
    links: dict[LinkIdentity, Link | None] = field(default_factory=dict)
    record_paths: dict[RecordIdentity, tuple[PathPart, ...]] = field(default_factory=dict)
    link_paths: dict[LinkIdentity, tuple[PathPart, ...]] = field(default_factory=dict)

    def add(self, issue: Issue) -> None:
        self.report.issues.append(issue)

    # State -------------------------------------------------------------------------

    def record_exists(self, kind_id: str, key: str) -> bool:
        identity = RecordIdentity(kind_id, key)
        if identity in self.records:
            return self.records[identity] is not None
        return self.index.record_exists(kind_id, key)

    def link_exists_before(self, identity: LinkIdentity) -> bool:
        return self.index.link_exists(*identity)

    # Steps ---------------------------------------------------------------------------

    def collect(self, changes: Sequence[DataChange]) -> None:
        seen: set[RecordIdentity | LinkIdentity] = set()
        for index, change in enumerate(changes):
            path: tuple[PathPart, ...] = ("changes", index)
            identity = change.identity
            if identity in seen:
                self.add(
                    Issue(
                        code=IssueCode.CHANGESET_DUPLICATE_CHANGE,
                        path=path,
                        message=f"{identity} is changed more than once in the change set",
                        details={"identity": list(identity)},
                    )
                )
                continue
            seen.add(identity)
            if isinstance(change, RecordUpsert):
                self.report.issues.extend(validate_record(self.schema, change.record, path=path))
                self.records[change.identity] = change.record
                self.record_paths[change.identity] = path
            elif isinstance(change, RecordDelete):
                self.collect_record_delete(change, path)
            elif isinstance(change, LinkUpsert):
                self.report.issues.extend(validate_link(self.schema, change.link, path=path))
                self.links[change.identity] = change.link
                self.link_paths[change.identity] = path
            else:
                self.collect_link_delete(change, path)

    def collect_record_delete(self, change: RecordDelete, path: tuple[PathPart, ...]) -> None:
        if change.kind not in self.schema.kinds:
            self.add(
                Issue(
                    code=IssueCode.RECORD_KIND_UNKNOWN,
                    path=(*path, "kind"),
                    message=f"unknown Kind {change.kind}",
                    kind=change.kind,
                    key=change.key,
                )
            )
            return
        if not self.index.record_exists(change.kind, change.key):
            self.add(
                Issue(
                    code=IssueCode.RECORD_NOT_FOUND,
                    path=path,
                    message=f"Record {change.key!r} does not exist",
                    kind=change.kind,
                    key=change.key,
                )
            )
            return
        self.records[change.identity] = None
        self.record_paths[change.identity] = path

    def collect_link_delete(self, change: LinkDelete, path: tuple[PathPart, ...]) -> None:
        if change.relation not in self.schema.relations:
            self.add(
                Issue(
                    code=IssueCode.LINK_RELATION_UNKNOWN,
                    path=(*path, "relation"),
                    message=f"unknown Relation {change.relation}",
                    relation=change.relation,
                )
            )
            return
        if not self.index.link_exists(*change.identity):
            self.add(
                Issue(
                    code=IssueCode.LINK_NOT_FOUND,
                    path=path,
                    message=f"Link {change.from_key!r} -> {change.to_key!r} does not exist",
                    relation=change.relation,
                )
            )
            return
        self.links[change.identity] = None
        self.link_paths[change.identity] = path

    def apply_deletes(self) -> None:
        deleted = [identity for identity, record in self.records.items() if record is None]
        for identity in deleted:
            path = self.record_paths[identity]
            for relation in self.schema.relations_of(identity.kind):
                if relation.backing is RelationBacking.EXPLICIT:
                    self.delete_links_of(relation, identity, path)
            self.check_references_to(identity, path)

    def delete_links_of(
        self, relation: Relation, identity: RecordIdentity, path: tuple[PathPart, ...]
    ) -> None:
        existing: set[tuple[str, str]] = set()
        if relation.from_kind == identity.kind:
            existing.update(self.index.links_of(relation.id, from_key=identity.key))
        if relation.to_kind == identity.kind:
            existing.update(self.index.links_of(relation.id, to_key=identity.key))
        # Links the batch also writes are checked as Link changes (endpoint missing).
        remaining = sorted(
            pair for pair in existing if LinkIdentity(relation.id, *pair) not in self.links
        )
        if not remaining:
            return
        if relation.on_delete is OnDelete.CASCADE:
            for from_key, to_key in remaining:
                link_identity = LinkIdentity(relation.id, from_key, to_key)
                self.links[link_identity] = None
                self.link_paths[link_identity] = path
                self.report.derived_changes.append(
                    LinkDelete(relation=relation.id, from_key=from_key, to_key=to_key)
                )
            return
        self.add(
            Issue(
                code=IssueCode.RECORD_DELETE_RESTRICTED,
                path=path,
                message=(
                    f"{len(remaining)} Link(s) of {relation.api_name!r} restrict deleting "
                    f"Record {identity.key!r}"
                ),
                kind=identity.kind,
                relation=relation.id,
                key=identity.key,
                details={
                    "count": len(remaining),
                    "links": [list(pair) for pair in remaining[:_SAMPLE_SIZE]],
                },
            )
        )

    def reference_attributes(self, target_kind: str) -> list[tuple[Kind, Attribute]]:
        return [
            (kind, attribute)
            for kind in self.schema.kinds.values()
            for attribute in kind.attributes.values()
            if attribute.type.scalar.name is AttributeType.REF
            and attribute.type.scalar.kind_ref == target_kind
        ]

    def check_references_to(self, identity: RecordIdentity, path: tuple[PathPart, ...]) -> None:
        for kind, attribute in self.reference_attributes(identity.kind):
            referencing = set(self.index.keys_referencing(kind.id, attribute.id, identity.key))
            for record_identity, record in self.records.items():
                if record_identity.kind == kind.id and record is not None:
                    if _holds_key(record.values.get(attribute.id), identity.key):
                        referencing.add(record_identity.key)
                    else:
                        referencing.discard(record_identity.key)
            referencing = {
                key
                for key in referencing
                if RecordIdentity(kind.id, key) not in self.records
                or self.records[RecordIdentity(kind.id, key)] is not None
            }
            if referencing:
                keys = sorted(referencing)
                self.add(
                    Issue(
                        code=IssueCode.RECORD_DELETE_RESTRICTED,
                        path=path,
                        message=(
                            f"{len(keys)} {kind.api_name} Record(s) reference {identity.key!r} "
                            f"through {attribute.api_name!r}"
                        ),
                        kind=identity.kind,
                        key=identity.key,
                        attribute=attribute.id,
                        details={"count": len(keys), "keys": list(keys[:_SAMPLE_SIZE])},
                    )
                )

    def check_link_endpoints(self) -> None:
        for identity, link in self.links.items():
            if link is None:
                continue
            relation = self.schema.relations.get(identity.relation)
            if relation is None or relation.backing is RelationBacking.FOREIGN_KEY:
                continue
            for end, kind_id, key in (
                ("from", relation.from_kind, identity.from_key),
                ("to", relation.to_kind, identity.to_key),
            ):
                if not self.record_exists(kind_id, key):
                    self.add(
                        Issue(
                            code=IssueCode.LINK_ENDPOINT_MISSING,
                            path=(*self.link_paths[identity], end),
                            message=f"{end} Record {key!r} of {relation.api_name!r} does not exist",
                            kind=kind_id,
                            relation=relation.id,
                            key=key,
                        )
                    )

    def check_reference_values(self) -> None:
        for identity, record in self.records.items():
            kind = self.schema.kinds.get(identity.kind)
            if record is None or kind is None:
                continue
            for attribute_id, value in record.values.items():
                attribute = kind.attributes.get(attribute_id)
                if attribute is None or attribute.type.scalar.name is not AttributeType.REF:
                    continue
                target = attribute.type.scalar.kind_ref
                assert target is not None
                for key in _keys_in(value):
                    if not self.record_exists(target, key):
                        self.add(
                            Issue(
                                code=IssueCode.VALUE_REFERENCE_MISSING,
                                path=(*self.record_paths[identity], "values", attribute_id),
                                message=f"{attribute.api_name!r} references missing Record {key!r}",
                                kind=identity.kind,
                                key=identity.key,
                                attribute=attribute_id,
                                details={"target_kind": target, "target_key": key},
                            )
                        )

    def check_uniqueness(self) -> None:
        claimed: dict[tuple[str, str, str], str] = {}
        for identity, record in sorted(self.records.items()):
            kind = self.schema.kinds.get(identity.kind)
            if record is None or kind is None:
                continue
            for attribute in kind.attributes.values():
                value = record.values.get(attribute.id)
                if not attribute.unique or value is None:
                    continue
                owner = self.unique_owner(identity, attribute, value, claimed)
                if owner is None:
                    continue
                # For a sensitive Attribute the owner is not named: a writer without the
                # clearance would learn which Record holds a value it guessed.
                sensitive = attribute.is_sensitive
                self.add(
                    Issue(
                        code=IssueCode.VALUE_UNIQUE,
                        path=(*self.record_paths[identity], "values", attribute.id),
                        message=(
                            f"{attribute.api_name!r} value is already used by another Record"
                            if sensitive
                            else f"{attribute.api_name!r} value {value!r} is used by {owner!r}"
                        ),
                        kind=identity.kind,
                        key=identity.key,
                        attribute=attribute.id,
                        details={} if sensitive else {"other_key": owner},
                    )
                )

    def unique_owner(
        self,
        identity: RecordIdentity,
        attribute: Attribute,
        value: JsonValue,
        claimed: dict[tuple[str, str, str], str],
    ) -> str | None:
        """Another Record holding ``value`` after the batch, if any."""
        claim = (identity.kind, attribute.id, repr(value))
        if claim in claimed:
            return claimed[claim]
        claimed[claim] = identity.key
        owner = self.index.find_unique(identity.kind, attribute.id, value)
        if owner is None or owner == identity.key:
            return None
        if RecordIdentity(identity.kind, owner) in self.records:
            # The existing owner is changed by the batch: if it keeps the value, its own
            # claim collides with this one; otherwise the value is released.
            return None
        return owner

    def link_changes(self, relation_id: str) -> tuple[list[LinkIdentity], list[LinkIdentity]]:
        """New Links and removed existing Links of a Relation in the batch."""
        added: list[LinkIdentity] = []
        removed: list[LinkIdentity] = []
        for identity, link in self.links.items():
            if identity.relation != relation_id:
                continue
            exists = self.link_exists_before(identity)
            if link is not None and not exists:
                added.append(identity)
            elif link is None and exists:
                removed.append(identity)
        return added, removed

    def final_link_count(
        self,
        relation: Relation,
        added: Counter[str],
        removed: Counter[str],
        *,
        from_key: str | None = None,
        to_key: str | None = None,
    ) -> int:
        """Links of `relation` from `from_key` (or to `to_key`) once the batch applies; `added`
        and `removed` count the batch's new and removed Links per key of that side."""
        key = from_key if from_key is not None else to_key
        assert key is not None
        existing = self.index.count_links(relation.id, from_key=from_key, to_key=to_key)
        return existing - removed[key] + added[key]

    def check_cardinality(self) -> None:
        for relation in self.schema.relations.values():
            if relation.backing is RelationBacking.FOREIGN_KEY:
                continue
            if not (relation.single_per_from or relation.single_per_to):
                continue
            added, removed = self.link_changes(relation.id)
            sides = []
            if relation.single_per_from:
                sides.append("from")
            if relation.single_per_to:
                sides.append("to")
            for side in sides:
                groups: dict[str, list[LinkIdentity]] = defaultdict(list)
                for identity in added:
                    groups[identity.from_key if side == "from" else identity.to_key].append(
                        identity
                    )
                removed_by = Counter(
                    identity.from_key if side == "from" else identity.to_key for identity in removed
                )
                added_by = Counter({key: len(competing) for key, competing in groups.items()})
                for key, competing in sorted(groups.items()):
                    count = self.final_link_count(
                        relation,
                        added_by,
                        removed_by,
                        from_key=key if side == "from" else None,
                        to_key=key if side == "to" else None,
                    )
                    if count <= 1:
                        continue
                    self.add(
                        Issue(
                            code=IssueCode.LINK_CARDINALITY,
                            path=self.link_paths[competing[0]],
                            message=(
                                f"{relation.api_name!r} is {relation.cardinality}: {side} Record "
                                f"{key!r} would have {count} Links"
                            ),
                            relation=relation.id,
                            key=key,
                            details={
                                "side": side,
                                "count": count,
                                "links": [[i.from_key, i.to_key] for i in competing[:_SAMPLE_SIZE]],
                            },
                        )
                    )

    def check_required_relations(self) -> None:
        for relation in self.schema.relations.values():
            if not relation.required_from:
                continue
            if relation.backing is RelationBacking.FOREIGN_KEY:
                self.check_required_foreign_key(relation)
                continue
            added, removed = self.link_changes(relation.id)
            candidates: set[str] = {
                identity.key
                for identity, record in self.records.items()
                if identity.kind == relation.from_kind
                and record is not None
                and not self.index.record_exists(identity.kind, identity.key)
            }
            candidates.update(
                identity.from_key
                for identity in removed
                if self.record_exists(relation.from_kind, identity.from_key)
            )
            added_by = Counter(identity.from_key for identity in added)
            removed_by = Counter(identity.from_key for identity in removed)
            for key in sorted(candidates):
                if self.final_link_count(relation, added_by, removed_by, from_key=key) > 0:
                    continue
                identity = RecordIdentity(relation.from_kind, key)
                path = self.record_paths.get(identity) or next(
                    self.link_paths[i] for i in removed if i.from_key == key
                )
                self.add(
                    Issue(
                        code=IssueCode.RECORD_REQUIRED_RELATION,
                        path=path,
                        message=f"Record {key!r} needs at least one {relation.api_name!r} Link",
                        kind=relation.from_kind,
                        relation=relation.id,
                        key=key,
                    )
                )

    def check_required_foreign_key(self, relation: Relation) -> None:
        attribute_id = relation.foreign_key_attribute
        for identity, record in self.records.items():
            if identity.kind != relation.from_kind or record is None or attribute_id is None:
                continue
            if record.values.get(attribute_id) is None:
                self.add(
                    Issue(
                        code=IssueCode.RECORD_REQUIRED_RELATION,
                        path=(*self.record_paths[identity], "values", attribute_id),
                        message=f"Record {identity.key!r} needs a {relation.api_name!r} reference",
                        kind=identity.kind,
                        relation=relation.id,
                        key=identity.key,
                        attribute=attribute_id,
                    )
                )


def _keys_in(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _holds_key(value: JsonValue, key: str) -> bool:
    return key in _keys_in(value)


def evaluate_changeset(
    schema: Schema, index_view: IndexView, changes: Sequence[DataChange]
) -> ChangesetReport:
    """Validate a batch and compute its derived changes (cascaded Link deletions)."""
    batch = _Batch(schema, index_view)
    batch.collect(changes)
    batch.apply_deletes()
    batch.check_link_endpoints()
    batch.check_reference_values()
    batch.check_uniqueness()
    batch.check_cardinality()
    batch.check_required_relations()
    return batch.report


def validate_changeset(
    schema: Schema, index_view: IndexView, changes: Sequence[DataChange]
) -> list[Issue]:
    """Issues of a batch of changes; see ``evaluate_changeset`` for derived changes."""
    return evaluate_changeset(schema, index_view, changes).issues
