"""Inputs and outcome of a commit: data changes with provenance, schema changes, result."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from lorebase_core.model import (
    Derivation,
    Domain,
    Kind,
    Lens,
    Operation,
    Relation,
    Source,
    Trait,
)
from lorebase_core.revision import ChangesetEntry
from lorebase_core.validate import DataChange

SchemaElementModel = Domain | Kind | Relation | Trait | Operation | Derivation | Source | Lens
"""Anything stored in the `schema` partition."""


@dataclass(frozen=True, slots=True)
class Change:
    """A data change and the provenance its changed Attributes are attributed to.

    `provenance` is a provenance entry id for every changed Attribute, or a map from
    Attribute id to entry id (Attributes left out fall back to the commit's own entry).
    `None` attributes the change to the commit's own entry, built from its origin and author.
    """

    change: DataChange
    provenance: str | Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SchemaUpsert:
    """Create or replace a schema element (the Domain element included)."""

    element: SchemaElementModel


@dataclass(frozen=True, slots=True)
class SchemaDelete:
    """Remove a schema element by id; the Domain element cannot be removed."""

    element_id: str


SchemaChange = SchemaUpsert | SchemaDelete


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Outcome of a commit. `no_op` commits change nothing and leave the head in place."""

    revision_id: bytes
    parents: tuple[bytes, ...]
    root: bytes
    generation: int
    ref_version: int
    created_at: datetime
    changeset: tuple[ChangesetEntry, ...]
    derived_changes: tuple[DataChange, ...] = ()
    no_op: bool = False
