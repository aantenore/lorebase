"""Conflict taxonomy, fingerprints and resolutions (design 6.9, 6.10).

Every Conflict has a stable code (``DV1`` to ``XS11``) and a fingerprint computed from its
code, location and the hashes of the three sides. Resolutions are matched to Conflicts by
fingerprint, so a merge recomputed against a moved target re-applies the resolutions of the
Conflicts that did not change.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from lorebase_core.canonical import canonical_bytes

__all__ = [
    "CHOICES",
    "GROUPED_KINDS",
    "MACHINE_RESOLVABLE",
    "AutoResolution",
    "Conflict",
    "ConflictKind",
    "Resolution",
    "ResolutionChoice",
    "ResolutionError",
    "Side",
    "UnresolvedConflictsError",
    "conflict_fingerprint",
]


class ConflictKind(StrEnum):
    """Stable Conflict codes (design 6.9). ``DV`` data, ``SC`` schema, ``XS`` cross-cutting."""

    ATTRIBUTE_VALUE = "DV1"
    DELETE_MODIFY = "DV2"
    ADD_ADD = "DV3"
    SCHEMA_PROPERTY = "SC1"
    SCHEMA_DELETE_MODIFY = "SC2"
    NAMING = "SC3"
    ORPHANED_DATA = "XS1"
    ORPHANED_VALUE = "XS2"
    TYPE_COERCION = "XS3"
    MISSING_VALUE = "XS4"
    CONSTRAINT = "XS5"
    CARDINALITY = "XS6"
    REQUIRED_RELATION = "XS7"
    UNIQUENESS = "XS8"
    DANGLING_LINK = "XS9"
    TRAIT_CONFORMANCE = "XS10"
    BROKEN_REFERENCE = "XS11"


class Side(StrEnum):
    BASE = "base"
    OURS = "ours"
    THEIRS = "theirs"


class ResolutionChoice(StrEnum):
    """How a Conflict is resolved; the meaning per Conflict kind is listed in ``CHOICES``.

    - ``ours``, ``theirs``, ``base``: take that side's value, element or state.
    - ``custom``: a value given by the resolver (``Resolution.value`` or, for grouped
      Conflicts, ``Resolution.values`` keyed by member key).
    - ``drop``: drop the data that breaks the merged schema (orphaned data or values, Records
      with uncastable values, Records without a required Link, dangling Links).
    - ``restore``: restore what the other side removed (a Kind, Relation or Attribute, a type
      before its change, a deleted Record, a referenced schema element).
    - ``relax``: relax the schema rule that the data breaks (required, constraint,
      cardinality, ``required_from``, uniqueness, Trait implementation).
    """

    OURS = "ours"
    THEIRS = "theirs"
    BASE = "base"
    CUSTOM = "custom"
    DROP = "drop"
    RESTORE = "restore"
    RELAX = "relax"


_C = ResolutionChoice
_PICK = (_C.OURS, _C.THEIRS, _C.BASE, _C.CUSTOM)

#: Choices accepted per Conflict kind.
CHOICES: dict[ConflictKind, tuple[ResolutionChoice, ...]] = {
    ConflictKind.ATTRIBUTE_VALUE: _PICK,
    ConflictKind.DELETE_MODIFY: _PICK,
    ConflictKind.ADD_ADD: _PICK,
    ConflictKind.SCHEMA_PROPERTY: _PICK,
    ConflictKind.SCHEMA_DELETE_MODIFY: _PICK,
    ConflictKind.NAMING: (_C.CUSTOM,),
    ConflictKind.ORPHANED_DATA: (_C.DROP, _C.RESTORE),
    ConflictKind.ORPHANED_VALUE: (_C.DROP, _C.RESTORE),
    ConflictKind.TYPE_COERCION: (_C.CUSTOM, _C.DROP, _C.RESTORE),
    ConflictKind.MISSING_VALUE: (_C.CUSTOM, _C.RELAX),
    ConflictKind.CONSTRAINT: (_C.CUSTOM, _C.RELAX),
    ConflictKind.CARDINALITY: (_C.CUSTOM, _C.RELAX),
    ConflictKind.REQUIRED_RELATION: (_C.CUSTOM, _C.DROP, _C.RELAX),
    ConflictKind.UNIQUENESS: (_C.CUSTOM, _C.RELAX),
    ConflictKind.DANGLING_LINK: (_C.DROP, _C.RESTORE),
    ConflictKind.TRAIT_CONFORMANCE: (_C.CUSTOM, _C.RELAX),
    ConflictKind.BROKEN_REFERENCE: (_C.CUSTOM, _C.RESTORE),
}

#: Kinds a ``MergePolicy`` can resolve without a person (``prefer`` and ``cascade_dangling``).
MACHINE_RESOLVABLE = frozenset(
    {
        ConflictKind.ATTRIBUTE_VALUE,
        ConflictKind.DELETE_MODIFY,
        ConflictKind.ADD_ADD,
        ConflictKind.SCHEMA_PROPERTY,
        ConflictKind.SCHEMA_DELETE_MODIFY,
        ConflictKind.DANGLING_LINK,
    }
)

#: Kinds reported as one Conflict over many members, with a count and a sample of keys.
GROUPED_KINDS = frozenset(
    {
        ConflictKind.ORPHANED_DATA,
        ConflictKind.ORPHANED_VALUE,
        ConflictKind.TYPE_COERCION,
        ConflictKind.MISSING_VALUE,
        ConflictKind.CONSTRAINT,
        ConflictKind.CARDINALITY,
        ConflictKind.REQUIRED_RELATION,
        ConflictKind.UNIQUENESS,
        ConflictKind.DANGLING_LINK,
    }
)


def conflict_fingerprint(
    kind: ConflictKind,
    partition: str,
    key: str | None,
    attribute: str | None,
    hashes: tuple[bytes | None, bytes | None, bytes | None],
    members: Iterable[str] = (),
) -> str:
    """``sha256(code, partition, key, attribute, base_hash, ours_hash, theirs_hash)``.

    Grouped Conflicts also hash their sorted member keys, so a group that gains or loses
    members is a different Conflict.
    """
    digest = hashlib.sha256()
    head: list[JsonValue] = [
        kind.value,
        partition,
        key,
        attribute,
        *(None if h is None else h.hex() for h in hashes),
    ]
    digest.update(canonical_bytes(head))
    for member in sorted(members):
        digest.update(b"\x00")
        digest.update(member.encode("utf-8"))
    return digest.hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Conflict(_Frozen):
    """One Conflict of a merge attempt.

    ``partition`` and ``key`` locate the element (for schema Conflicts the partition is
    ``schema`` and the key the element id); ``attribute`` is an Attribute id for data
    Conflicts and a dotted property path for schema Conflicts. ``base``, ``ours`` and
    ``theirs`` hold the competing values (``None`` means absent) and the ``*_hash`` fields the
    hashes of the elements they come from. Grouped Conflicts carry ``count`` members and a
    ``sample`` of their keys.
    """

    kind: ConflictKind
    fingerprint: str
    partition: str
    key: str | None = None
    attribute: str | None = None
    base: JsonValue = None
    ours: JsonValue = None
    theirs: JsonValue = None
    base_hash: str | None = None
    ours_hash: str | None = None
    theirs_hash: str | None = None
    count: int = Field(default=1, ge=1)
    sample: tuple[str, ...] = ()
    message: str = ""
    details: dict[str, JsonValue] = Field(default_factory=dict)
    choices: tuple[ResolutionChoice, ...] = ()
    machine_resolvable: bool = False

    @property
    def grouped(self) -> bool:
        return self.kind in GROUPED_KINDS


class Resolution(_Frozen):
    """A resolver's decision for the Conflict with ``fingerprint``.

    ``custom`` takes ``value`` (``None`` means absent), or ``values`` keyed by member key for
    grouped Conflicts.
    """

    fingerprint: str
    choice: ResolutionChoice
    value: JsonValue = None
    values: dict[str, JsonValue] | None = None
    resolver: str = ""

    @model_validator(mode="after")
    def _check_custom(self) -> Self:
        if self.choice is not ResolutionChoice.CUSTOM and (
            self.value is not None or self.values is not None
        ):
            raise ValueError("only custom resolutions carry a value")
        return self

    def value_for(self, member: str | None) -> JsonValue:
        """The custom value for ``member``: its entry in ``values``, else ``value``."""
        if self.values is not None and member is not None and member in self.values:
            return self.values[member]
        return self.value


class AutoResolution(_Frozen):
    """A Conflict resolved by the engine or by a policy, recorded for Lineage and review."""

    kind: ConflictKind
    fingerprint: str
    partition: str
    key: str | None = None
    attribute: str | None = None
    choice: ResolutionChoice
    resolver: str
    count: int = Field(default=1, ge=1)
    sample: tuple[str, ...] = ()


class ResolutionError(ValueError):
    """A resolution that does not fit the merge attempt."""


class UnresolvedConflictsError(ResolutionError):
    """Raised when resolutions leave Conflicts of the attempt open."""

    def __init__(self, fingerprints: Iterable[str]) -> None:
        self.fingerprints = tuple(sorted(fingerprints))
        super().__init__(f"{len(self.fingerprints)} conflict(s) left unresolved")
