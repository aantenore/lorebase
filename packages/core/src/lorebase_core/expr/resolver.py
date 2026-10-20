"""How evaluation reads Records it does not hold: the ``RecordResolver`` protocol.

The engine never touches a store. Callers pass a resolver answering from Records they have
loaded. A resolver that has not loaded what is asked raises ``RecordNotLoaded``; the engine
lets it through untouched, so the caller can load the Record (or the Links) and evaluate
again. Evaluation is deterministic, so a retry computes the same result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue

from lorebase_core.model.relation import LinkDirection


@dataclass(frozen=True, slots=True)
class RecordRequest:
    """One Record by Kind id and key."""

    kind: str
    key: str


@dataclass(frozen=True, slots=True)
class LinksRequest:
    """The Records linked to ``key`` through ``relation``, traversed in ``direction``."""

    relation: str
    direction: LinkDirection
    key: str


@dataclass(frozen=True, slots=True)
class LinkedRecord:
    """A Record at the far end of a Link: its key, its values and the Link's values."""

    key: str
    values: Mapping[str, JsonValue]
    link_values: Mapping[str, JsonValue] = field(default_factory=dict)


class RecordNotLoaded(Exception):  # noqa: N818 - a signal to the caller, not an error
    """The resolver does not hold what evaluation needs; load ``request`` and retry."""

    def __init__(self, request: RecordRequest | LinksRequest) -> None:
        super().__init__(f"not loaded: {request}")
        self.request = request


class RecordResolver(Protocol):
    """Read access to Records for ``record()``, ``links()`` and ``links.<relation>``.

    Values are canonical and keyed by Attribute id (derived values may be included).
    """

    def record(self, kind_id: str, key: str) -> Mapping[str, JsonValue] | None:
        """The values of the Record, ``None`` if it does not exist."""
        ...

    def linked(
        self, relation_id: str, direction: LinkDirection, key: str
    ) -> Sequence[LinkedRecord]:
        """Records linked to ``key``; ``direction`` says which end ``key`` is."""
        ...


@dataclass
class MemoryResolver:
    """A resolver over loaded data. Unknown entries raise ``RecordNotLoaded``.

    ``records`` maps ``(kind_id, key)`` to values, or to ``None`` for a Record known not to
    exist. ``links`` maps ``(relation_id, direction, key)`` to the linked Records.
    """

    records: dict[tuple[str, str], Mapping[str, JsonValue] | None] = field(default_factory=dict)
    links: dict[tuple[str, LinkDirection, str], Sequence[LinkedRecord]] = field(
        default_factory=dict
    )

    def record(self, kind_id: str, key: str) -> Mapping[str, JsonValue] | None:
        try:
            return self.records[(kind_id, key)]
        except KeyError:
            raise RecordNotLoaded(RecordRequest(kind_id, key)) from None

    def linked(
        self, relation_id: str, direction: LinkDirection, key: str
    ) -> Sequence[LinkedRecord]:
        try:
            return self.links[(relation_id, direction, key)]
        except KeyError:
            raise RecordNotLoaded(LinksRequest(relation_id, direction, key)) from None
