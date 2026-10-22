"""The ``Schema`` aggregate: every schema element of one Revision, with lookup helpers.

Elements are keyed by id. Api name lookups return ``None`` for unknown names; when a schema
is invalid (duplicate api names) they return one of the candidates, and ``validate_schema``
reports the duplicates.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from functools import cached_property
from typing import Any, Self

from pydantic import Field, model_validator

from lorebase_core.model.attribute import Attribute
from lorebase_core.model.base import Model, SchemaElement
from lorebase_core.model.derivation import Derivation
from lorebase_core.model.domain import Domain
from lorebase_core.model.ids import (
    AttributeId,
    DerivationId,
    KindId,
    OperationId,
    RelationId,
    SourceId,
    TraitId,
)
from lorebase_core.model.kind import Kind
from lorebase_core.model.operation import Operation
from lorebase_core.model.paths import PathRelation, path_relations
from lorebase_core.model.record import parse_ref
from lorebase_core.model.relation import LinkDirection, Relation
from lorebase_core.model.source import Source
from lorebase_core.model.trait import Trait

#: Names of the lazily built lookup indexes (cached properties).
_INDEXES = (
    "_kinds_by_name",
    "_traits_by_name",
    "_relations_by_name",
    "_operations_by_name",
    "_derivations_by_name",
    "_sources_by_name",
    "_paths",
    "_paths_by_name",
)


class Schema(Model):
    domain: Domain
    kinds: dict[KindId, Kind] = Field(default_factory=dict)
    relations: dict[RelationId, Relation] = Field(default_factory=dict)
    traits: dict[TraitId, Trait] = Field(default_factory=dict)
    operations: dict[OperationId, Operation] = Field(default_factory=dict)
    derivations: dict[DerivationId, Derivation] = Field(default_factory=dict)
    sources: dict[SourceId, Source] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        collections: tuple[tuple[str, dict[str, SchemaElement]], ...] = (
            ("kinds", dict(self.kinds)),
            ("relations", dict(self.relations)),
            ("traits", dict(self.traits)),
            ("operations", dict(self.operations)),
            ("derivations", dict(self.derivations)),
            ("sources", dict(self.sources)),
        )
        for name, elements in collections:
            for key, element in elements.items():
                if key != element.id:
                    raise ValueError(f"{name}: key {key} does not match element id {element.id}")
        return self

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Copy the Schema; lookup indexes are rebuilt for the copy."""
        copied = super().model_copy(update=update, deep=deep)
        for name in _INDEXES:
            copied.__dict__.pop(name, None)
        return copied

    # Iteration -----------------------------------------------------------------

    def elements(self) -> Iterator[SchemaElement]:
        """Every schema element except the Domain."""
        yield from self.kinds.values()
        yield from self.relations.values()
        yield from self.traits.values()
        yield from self.operations.values()
        yield from self.derivations.values()
        yield from self.sources.values()

    # Api name lookups ------------------------------------------------------------

    @cached_property
    def _kinds_by_name(self) -> dict[str, Kind]:
        return {kind.api_name: kind for kind in self.kinds.values()}

    @cached_property
    def _traits_by_name(self) -> dict[str, Trait]:
        return {trait.api_name: trait for trait in self.traits.values()}

    @cached_property
    def _relations_by_name(self) -> dict[str, tuple[Relation, LinkDirection]]:
        index: dict[str, tuple[Relation, LinkDirection]] = {}
        for relation in self.relations.values():
            index[relation.inverse_api_name] = (relation, LinkDirection.INVERSE)
            index[relation.api_name] = (relation, LinkDirection.FORWARD)
        return index

    @cached_property
    def _operations_by_name(self) -> dict[str, Operation]:
        return {operation.api_name: operation for operation in self.operations.values()}

    @cached_property
    def _derivations_by_name(self) -> dict[str, Derivation]:
        return {derivation.api_name: derivation for derivation in self.derivations.values()}

    @cached_property
    def _sources_by_name(self) -> dict[str, Source]:
        return {source.api_name: source for source in self.sources.values()}

    @cached_property
    def _paths(self) -> dict[str, PathRelation]:
        return path_relations(self.derivations.values(), self.relations)

    @cached_property
    def _paths_by_name(self) -> dict[str, tuple[PathRelation, LinkDirection]]:
        index: dict[str, tuple[PathRelation, LinkDirection]] = {}
        for path in self._paths.values():
            inverse = path.relation.inverse_api_name
            if inverse:
                index[inverse] = (path, LinkDirection.INVERSE)
            index[path.relation.api_name] = (path, LinkDirection.FORWARD)
        return index

    def kind_by_api_name(self, api_name: str) -> Kind | None:
        return self._kinds_by_name.get(api_name)

    def trait_by_api_name(self, api_name: str) -> Trait | None:
        return self._traits_by_name.get(api_name)

    def relation_by_api_name(self, api_name: str) -> tuple[Relation, LinkDirection] | None:
        """Resolve a forward or inverse Relation name to the Relation and the direction."""
        return self._relations_by_name.get(api_name)

    def path_by_api_name(self, api_name: str) -> tuple[PathRelation, LinkDirection] | None:
        """Resolve the forward or inverse name of a path Derivation (a virtual Relation)."""
        return self._paths_by_name.get(api_name)

    def path_relations(self) -> dict[str, PathRelation]:
        """The valid path Derivations as virtual Relations, by Derivation id."""
        return self._paths

    def link_by_api_name(self, api_name: str) -> tuple[Relation, LinkDirection] | None:
        """Resolve a Relation name or a path Derivation name (its virtual Relation, whose id
        is the Derivation id) to the Relation and the direction."""
        found = self._relations_by_name.get(api_name)
        if found is not None:
            return found
        path = self._paths_by_name.get(api_name)
        return None if path is None else (path[0].relation, path[1])

    def link_relation(self, relation_id: str) -> Relation | None:
        """A Relation by id, or the virtual Relation of a valid path Derivation."""
        relation = self.relations.get(relation_id)
        if relation is not None:
            return relation
        path = self._paths.get(relation_id)
        return None if path is None else path.relation

    def operation_by_api_name(self, api_name: str) -> Operation | None:
        return self._operations_by_name.get(api_name)

    def derivation_by_api_name(self, api_name: str) -> Derivation | None:
        return self._derivations_by_name.get(api_name)

    def source_by_api_name(self, api_name: str) -> Source | None:
        return self._sources_by_name.get(api_name)

    def attribute_by_api_name(self, kind_id: str, api_name: str) -> Attribute | None:
        kind = self.kinds.get(kind_id)
        return kind.attribute_by_api_name(api_name) if kind is not None else None

    # Structural lookups ----------------------------------------------------------

    def attribute(self, kind_id: str, attribute_id: str) -> Attribute | None:
        kind = self.kinds.get(kind_id)
        return kind.attributes.get(attribute_id) if kind is not None else None

    def relations_of(self, kind_id: str) -> list[Relation]:
        """Relations with ``kind_id`` at either end."""
        return [r for r in self.relations.values() if kind_id in (r.from_kind, r.to_kind)]

    def kinds_implementing(self, trait_id: str) -> list[Kind]:
        return [k for k in self.kinds.values() if any(t.trait_id == trait_id for t in k.traits)]

    def resolve_ref(self, ref: str) -> tuple[KindId, str]:
        """Resolve ``Supplier/S-1001`` to ``(kind_id, key)``.

        Raises ``KeyError`` for unknown Kinds.
        """
        kind_api_name, key = parse_ref(ref)
        kind = self.kind_by_api_name(kind_api_name)
        if kind is None:
            raise KeyError(f"unknown Kind {kind_api_name!r}")
        return kind.id, key

    def attribute_owner(self, attribute_id: AttributeId) -> Kind | Relation | Trait | None:
        """The Kind, Relation or Trait declaring ``attribute_id``."""
        owners: list[Kind | Relation | Trait] = [
            *self.kinds.values(),
            *self.relations.values(),
            *self.traits.values(),
        ]
        for owner in owners:
            if attribute_id in owner.attributes:
                return owner
        return None
