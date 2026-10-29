"""The `schema` partition: schema elements keyed by id, the Domain element keyed by Domain id.

The materialized head keeps the compiled schema (`Schema.content()`) next to the schema
partition hash, so commits read the schema with one row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lorebase_core.canonical import ObjectType
from lorebase_core.model import (
    Derivation,
    Domain,
    Kind,
    Lens,
    Operation,
    Relation,
    Schema,
    Source,
    Trait,
)
from lorebase_store.commit.changes import SchemaChange, SchemaDelete, SchemaElementModel
from lorebase_store.commit.errors import SchemaChangeError

ELEMENT_MODELS: Mapping[ObjectType, type[SchemaElementModel]] = {
    ObjectType.SCHEMA_DOMAIN: Domain,
    ObjectType.SCHEMA_KIND: Kind,
    ObjectType.SCHEMA_RELATION: Relation,
    ObjectType.SCHEMA_TRAIT: Trait,
    ObjectType.SCHEMA_OPERATION: Operation,
    ObjectType.SCHEMA_DERIVATION: Derivation,
    ObjectType.SCHEMA_SOURCE: Source,
    ObjectType.SCHEMA_LENS: Lens,
}

_COLLECTIONS: Mapping[ObjectType, str] = {
    ObjectType.SCHEMA_KIND: "kinds",
    ObjectType.SCHEMA_RELATION: "relations",
    ObjectType.SCHEMA_TRAIT: "traits",
    ObjectType.SCHEMA_OPERATION: "operations",
    ObjectType.SCHEMA_DERIVATION: "derivations",
    ObjectType.SCHEMA_SOURCE: "sources",
    ObjectType.SCHEMA_LENS: "lenses",
}


def object_type_of(element: SchemaElementModel) -> ObjectType:
    return ObjectType(element.object_type)


def element_key(element: SchemaElementModel) -> str:
    """Key of an element in the `schema` partition."""
    return element.id


def schema_elements(schema: Schema) -> dict[str, SchemaElementModel]:
    """Every element of a Schema by partition key, the Domain included."""
    elements: dict[str, SchemaElementModel] = {schema.domain.id: schema.domain}
    for element in schema.elements():
        elements[element.id] = element  # type: ignore[assignment]
    return elements


def schema_from_elements(elements: Iterable[SchemaElementModel]) -> Schema:
    """Assemble a Schema; exactly one Domain element is required."""
    domain: Domain | None = None
    collections: dict[str, dict[str, Any]] = {name: {} for name in _COLLECTIONS.values()}
    for element in elements:
        if isinstance(element, Domain):
            if domain is not None:
                raise SchemaChangeError("a schema holds exactly one Domain element")
            domain = element
            continue
        collections[_COLLECTIONS[object_type_of(element)]][element.id] = element
    if domain is None:
        raise SchemaChangeError("a schema needs its Domain element")
    return Schema(domain=domain, **collections)


def element_from_body(object_type: ObjectType, body: Mapping[str, Any]) -> SchemaElementModel:
    """Parse a stored schema object."""
    return ELEMENT_MODELS[object_type].model_validate(body)


def compile_schema(schema: Schema) -> dict[str, Any]:
    """Document stored in `mat_schema.compiled`."""
    return schema.content()


def load_compiled(compiled: Mapping[str, Any]) -> Schema:
    """Inverse of `compile_schema`."""
    return Schema.model_validate(compiled)


def apply_schema_changes(
    schema: Schema, changes: Sequence[SchemaChange], domain_id: str
) -> tuple[Schema, list[SchemaElementModel], list[str]]:
    """New schema plus the upserted elements and the removed element ids."""
    elements = schema_elements(schema)
    upserted: list[SchemaElementModel] = []
    removed: list[str] = []
    seen: set[str] = set()
    for change in changes:
        key = change.element_id if isinstance(change, SchemaDelete) else change.element.id
        if key in seen:
            raise SchemaChangeError(f"schema element {key} is changed more than once")
        seen.add(key)
        if isinstance(change, SchemaDelete):
            if key == domain_id:
                raise SchemaChangeError("the Domain element cannot be removed")
            if key not in elements:
                raise SchemaChangeError(f"unknown schema element {key}")
            del elements[key]
            removed.append(key)
            continue
        element = change.element
        if isinstance(element, Domain) and element.id != domain_id:
            raise SchemaChangeError(f"Domain element {element.id} in Domain {domain_id}")
        current = elements.get(key)
        if current is not None and object_type_of(current) is not object_type_of(element):
            raise SchemaChangeError(f"schema element {key} cannot change its type")
        elements[key] = element
        upserted.append(element)
    return schema_from_elements(elements.values()), upserted, removed
