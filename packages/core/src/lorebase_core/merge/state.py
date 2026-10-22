"""Shared state of one merge run: sides, directives from resolutions, Conflict collection."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Final

from pydantic import JsonValue

from lorebase_core.canonical import ObjectType
from lorebase_core.merge.conflicts import (
    CHOICES,
    MACHINE_RESOLVABLE,
    AutoResolution,
    Conflict,
    ConflictKind,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    Side,
    conflict_fingerprint,
)
from lorebase_core.merge.objects import ObjectStore, SchemaLoader, load_object
from lorebase_core.merge.policies import (
    ChangeStamp,
    MergePolicy,
    PolicyDecision,
    PolicyName,
    PolicyRule,
    decide_authority,
    decide_newest,
)
from lorebase_core.model import Schema
from lorebase_core.tree import NodeStore, TreeParams
from lorebase_core.validate import SchemaRules

__all__ = [
    "MISSING",
    "SIDES",
    "Context",
    "Directives",
    "Group",
    "LineageLookup",
    "Missing",
    "Payload",
    "Stage",
    "Value",
    "as_json",
    "check_choice",
    "other_side",
    "same",
]


class Missing(Enum):
    """Marker for an absent value (JSON ``null`` and absent are the same)."""

    MISSING = "missing"

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final = Missing.MISSING

Value = JsonValue | Missing
Payload = dict[str, JsonValue]
SIDES: Final = (Side.OURS, Side.THEIRS)
_ORDER: Final = (Side.BASE, Side.OURS, Side.THEIRS)
_PREFERABLE = frozenset(
    {
        ConflictKind.ATTRIBUTE_VALUE,
        ConflictKind.DELETE_MODIFY,
        ConflictKind.ADD_ADD,
        ConflictKind.SCHEMA_PROPERTY,
        ConflictKind.SCHEMA_DELETE_MODIFY,
    }
)
#: Conflicts on one Attribute: the only ones Lineage-based policies decide.
_ATTRIBUTE_KINDS = frozenset({ConflictKind.ATTRIBUTE_VALUE, ConflictKind.ADD_ADD})

LineageLookup = Callable[[Side, str, str], Mapping[str, ChangeStamp]]
"""``(side, partition, key) -> {attribute id: change stamp}`` of the last change of each
Attribute of an element on ``ours`` or ``theirs``."""


def same(a: Value, b: Value) -> bool:
    """Structural equality that keeps JSON types apart (``1``, ``1.0`` and ``True`` differ)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        assert isinstance(b, dict)
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        assert isinstance(b, list)
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def as_json(value: Value) -> JsonValue:
    return None if value is MISSING else value


class Stage(Enum):
    """When a grouped Conflict is found.

    ``DECISION`` Conflicts are settled by their resolution. ``CHECK`` Conflicts are found by
    re-checking the merged state; they are reported again while a resolution leaves the
    state invalid.
    """

    DECISION = "decision"
    CHECK = "check"


@dataclass
class Directives:
    """Changes to the merge derived from resolutions of cross-cutting and schema Conflicts.

    They apply on the next run of the merge: schema element and property overrides, sides
    whose changes are ignored for a partition or an Attribute, and data state overrides.
    """

    elements: dict[str, Payload | None] = field(default_factory=dict)
    properties: dict[tuple[str, tuple[str | int, ...]], Value] = field(default_factory=dict)
    trait_removals: set[tuple[str, str]] = field(default_factory=set)
    trait_mappings: dict[tuple[str, str], Payload] = field(default_factory=dict)
    ignored_partitions: set[tuple[Side, str]] = field(default_factory=set)
    ignored_attributes: set[tuple[Side, str, str]] = field(default_factory=set)
    states: dict[tuple[str, str], Payload | None] = field(default_factory=dict)
    values: dict[tuple[str, str, str], Value] = field(default_factory=dict)

    def _parts(self) -> list[Any]:
        return [getattr(self, f.name) for f in fields(self)]

    def covers(self, other: Directives) -> bool:
        """Whether every directive of ``other`` is already part of this set."""
        for mine, theirs in zip(self._parts(), other._parts(), strict=True):
            if isinstance(mine, set):
                assert isinstance(theirs, set)
                if not theirs <= mine:
                    return False
                continue
            assert isinstance(mine, dict)
            assert isinstance(theirs, dict)
            for key, value in theirs.items():
                if key not in mine or not _same_directive(mine[key], value):
                    return False
        return True

    def absorb(self, other: Directives) -> bool:
        """Add the directives of ``other``; return whether anything changed."""
        if self.covers(other):
            return False
        for mine, theirs in zip(self._parts(), other._parts(), strict=True):
            if isinstance(mine, set):
                assert isinstance(theirs, set)
                mine |= theirs
            else:
                assert isinstance(mine, dict)
                assert isinstance(theirs, dict)
                mine.update(theirs)
        return True

    def copy(self) -> Directives:
        return Directives(**{f.name: copy.copy(getattr(self, f.name)) for f in fields(self)})


def _same_directive(a: object, b: object) -> bool:
    if isinstance(a, Missing) or isinstance(b, Missing):
        return a is b
    return same(a, b)  # type: ignore[arg-type]


@dataclass
class Group:
    """A Conflict being collected: grouped Conflicts gather members during the run."""

    kind: ConflictKind
    partition: str
    key: str | None
    attribute: str | None
    stage: Stage
    #: Schema element whose three versions identify the Conflict (hashes and values).
    element: str | None
    values: tuple[JsonValue, JsonValue, JsonValue] = (None, None, None)
    message: str = ""
    details: dict[str, JsonValue] = field(default_factory=dict)
    members: dict[str, JsonValue] = field(default_factory=dict)
    #: The side whose data conflicts with the other side's schema change, if any.
    data_side: Side | None = None


@dataclass
class Context:
    """Inputs and outputs of one run of the merge pipeline."""

    store: NodeStore
    objects: ObjectStore
    policy: MergePolicy
    params: TreeParams
    schema_loader: SchemaLoader
    roots: dict[Side, bytes | None]
    schemas: dict[Side, Schema | None]
    resolutions: Mapping[str, Resolution]
    directives: Directives
    virtual: bool = False
    #: Options of the static checks of the merged schema.
    rules: SchemaRules = field(default_factory=SchemaRules)
    #: Lineage of the sides, read by the ``authority`` and ``newest_wins`` policies.
    lineage: LineageLookup | None = None
    conflicts: list[Conflict] = field(default_factory=list)
    auto: list[AutoResolution] = field(default_factory=list)
    out: Directives = field(default_factory=Directives)
    groups: dict[tuple[object, ...], Group] = field(default_factory=dict)
    #: Partitions whose element has an open schema Conflict: no cross-cutting Conflicts.
    blocked: set[str] = field(default_factory=set)
    #: Partitions whose element has an open delete/modify Conflict: no data Conflicts either.
    frozen: set[str] = field(default_factory=set)
    #: Elements with an open data Conflict: left out of the consistency checks.
    open_keys: set[tuple[str, str]] = field(default_factory=set)
    #: Schema elements with an open schema Conflict: no static checks on them.
    open_elements: set[str] = field(default_factory=set)
    #: Full member keys of the grouped Conflicts reported, by fingerprint.
    members: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Static schema problems already present on ours or theirs: ``(code, path)``.
    known_issues: set[tuple[object, tuple[object, ...]]] = field(default_factory=set)
    schema_hashes: dict[Side, dict[str, bytes]] = field(default_factory=dict)
    _documents: dict[bytes, Payload] = field(default_factory=dict)

    # Objects --------------------------------------------------------------------

    def document(self, digest: bytes | None) -> Payload | None:
        """The payload of an object, cached for the run."""
        if digest is None:
            return None
        payload = self._documents.get(digest)
        if payload is None:
            payload = self._documents[digest] = load_object(self.objects, digest)
        return payload

    def put(self, object_type: ObjectType, payload: Payload) -> bytes:
        digest = self.objects.put(object_type, payload)
        self._documents.setdefault(digest, payload)
        return digest

    def element_hashes(self, key: str | None) -> tuple[bytes | None, bytes | None, bytes | None]:
        if key is None:
            return (None, None, None)
        return (
            self.schema_hashes[Side.BASE].get(key),
            self.schema_hashes[Side.OURS].get(key),
            self.schema_hashes[Side.THEIRS].get(key),
        )

    def element_document(self, side: Side, key: str) -> Payload | None:
        return self.document(self.schema_hashes[side].get(key))

    # Conflicts ------------------------------------------------------------------

    def conflict(
        self,
        kind: ConflictKind,
        partition: str,
        key: str | None,
        attribute: str | None,
        values: tuple[JsonValue, JsonValue, JsonValue],
        hashes: tuple[bytes | None, bytes | None, bytes | None],
        *,
        members: list[str] | None = None,
        message: str = "",
        details: dict[str, JsonValue] | None = None,
    ) -> Conflict:
        ordered = sorted(members or ())
        return Conflict(
            kind=kind,
            fingerprint=conflict_fingerprint(kind, partition, key, attribute, hashes, ordered),
            partition=partition,
            key=key,
            attribute=attribute,
            base=values[0],
            ours=values[1],
            theirs=values[2],
            base_hash=None if hashes[0] is None else hashes[0].hex(),
            ours_hash=None if hashes[1] is None else hashes[1].hex(),
            theirs_hash=None if hashes[2] is None else hashes[2].hex(),
            count=max(1, len(ordered)),
            sample=tuple(ordered[: self.policy.sample_size]),
            message=message,
            details=details or {},
            choices=CHOICES[kind],
            machine_resolvable=kind in MACHINE_RESOLVABLE,
        )

    def decide(
        self,
        conflict: Conflict,
        element: str | None,
        attribute: str | None,
        *,
        attribute_authority: str | None = None,
    ) -> Resolution | None:
        """The resolution of ``conflict``: given by a resolver, else by policy, else none.

        Policy rules are tried in the order of ``MergePolicy.rules``; the first that decides
        resolves the Conflict and is recorded as an automatic resolution with its reasons.
        """
        resolution = self.resolutions.get(conflict.fingerprint)
        if resolution is not None:
            check_choice(conflict, resolution)
            return resolution
        if self.virtual or conflict.kind not in _PREFERABLE:
            return None
        rules = self.policy.rules(element, attribute, attribute_authority=attribute_authority)
        for rule in rules:
            decision = self._apply(rule, conflict)
            if decision is None:
                continue
            choice = ResolutionChoice(decision.side.value)
            resolver = decision.name.resolver
            self.record_auto(conflict, choice, resolver, details=decision.details)
            return Resolution(fingerprint=conflict.fingerprint, choice=choice, resolver=resolver)
        return None

    def _apply(self, rule: PolicyRule, conflict: Conflict) -> PolicyDecision | None:
        if rule.name is PolicyName.PREFER:
            assert rule.side is not None
            return PolicyDecision(PolicyName.PREFER, rule.side)
        if conflict.kind not in _ATTRIBUTE_KINDS:
            return None
        ours = self.lineage_entry(Side.OURS, conflict)
        theirs = self.lineage_entry(Side.THEIRS, conflict)
        if rule.name is PolicyName.AUTHORITY:
            return decide_authority(rule.ranking, ours, theirs)
        return decide_newest(ours, theirs)

    def lineage_entry(self, side: Side, conflict: Conflict) -> ChangeStamp | None:
        """The stamp of ``side``'s last change of the Conflict's Attribute."""
        if self.lineage is None or conflict.key is None or conflict.attribute is None:
            return None
        return self.lineage(side, conflict.partition, conflict.key).get(conflict.attribute)

    def record_auto(
        self,
        conflict: Conflict,
        choice: ResolutionChoice,
        resolver: str,
        *,
        details: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if self.virtual:
            return
        self.auto.append(
            AutoResolution(
                kind=conflict.kind,
                fingerprint=conflict.fingerprint,
                partition=conflict.partition,
                key=conflict.key,
                attribute=conflict.attribute,
                choice=choice,
                resolver=resolver,
                count=conflict.count,
                sample=conflict.sample,
                details=dict(details or {}),
            )
        )

    def report(self, conflict: Conflict) -> None:
        if not self.virtual:
            self.conflicts.append(conflict)

    def group(
        self,
        kind: ConflictKind,
        partition: str,
        key: str | None,
        attribute: str | None,
        *,
        stage: Stage,
        element: str | None,
        message: str,
        data_side: Side | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> Group:
        """The Conflict group for this location, created on first use."""
        group_key = (kind, partition, key, attribute, stage)
        found = self.groups.get(group_key)
        if found is None:
            found = self.groups[group_key] = Group(
                kind=kind,
                partition=partition,
                key=key,
                attribute=attribute,
                stage=stage,
                element=element,
                values=self.element_values(element, attribute),
                message=message,
                details=dict(details or {}),
                data_side=data_side,
            )
        return found

    def property_values(self, element: str, name: str) -> tuple[JsonValue, JsonValue, JsonValue]:
        """The three versions of one top-level property of a schema element."""
        values = [
            None if document is None else document.get(name)
            for document in (self.element_document(side, element) for side in _ORDER)
        ]
        return (values[0], values[1], values[2])

    def element_values(
        self, element: str | None, attribute: str | None
    ) -> tuple[JsonValue, JsonValue, JsonValue]:
        """The three versions of an element's Attribute definition, or of the element."""
        if element is None:
            return (None, None, None)
        values: list[JsonValue] = []
        for side in (Side.BASE, Side.OURS, Side.THEIRS):
            document = self.element_document(side, element)
            if document is None:
                values.append(None)
                continue
            attributes = document.get("attributes")
            if attribute is not None and isinstance(attributes, dict):
                values.append(attributes.get(attribute))
            else:
                values.append(document.get("api_name"))
        return (values[0], values[1], values[2])


def other_side(side: Side) -> Side:
    return Side.THEIRS if side is Side.OURS else Side.OURS


def check_choice(conflict: Conflict, resolution: Resolution) -> None:
    if resolution.choice not in CHOICES[conflict.kind]:
        raise ResolutionError(
            f"{resolution.choice} does not resolve a {conflict.kind} conflict "
            f"({conflict.fingerprint})"
        )
