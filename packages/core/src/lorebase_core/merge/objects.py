"""Object access for the merge engine: the object store protocol and the schema partition codec.

The tree maps element keys to object hashes; objects themselves (canonical JSON payloads of
schema elements, Records and Links) live in an ``ObjectStore``. The ``schema`` partition is
keyed by element id; the Domain document is the only key that is not an element id.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from lorebase_core.canonical import ObjectType, object_hash
from lorebase_core.model import (
    Derivation,
    Domain,
    IdPrefix,
    Kind,
    Lens,
    Model,
    Operation,
    Relation,
    Schema,
    Source,
    Trait,
    id_prefix,
)
from lorebase_core.tree import (
    PARTITION_SCHEMA,
    NodeStore,
    TreeChange,
    kind_partition,
    relation_partition,
    tree_entries,
)

__all__ = [
    "DOMAIN_KEY",
    "InMemoryObjectStore",
    "MissingObjectError",
    "ObjectStore",
    "SchemaLoader",
    "data_object_type",
    "element_model",
    "load_object",
    "load_schema",
    "partition_element",
    "put_schema",
    "schema_from_documents",
    "schema_object_type",
    "tree_schema_loader",
]

#: Key of the Domain document in the ``schema`` partition.
DOMAIN_KEY = "domain"

_KIND_PREFIX = kind_partition("")
_RELATION_PREFIX = relation_partition("")

_ELEMENT_TYPES: dict[IdPrefix, tuple[ObjectType, type[Model], str]] = {
    IdPrefix.KIND: (ObjectType.SCHEMA_KIND, Kind, "kinds"),
    IdPrefix.RELATION: (ObjectType.SCHEMA_RELATION, Relation, "relations"),
    IdPrefix.TRAIT: (ObjectType.SCHEMA_TRAIT, Trait, "traits"),
    IdPrefix.OPERATION: (ObjectType.SCHEMA_OPERATION, Operation, "operations"),
    IdPrefix.DERIVATION: (ObjectType.SCHEMA_DERIVATION, Derivation, "derivations"),
    IdPrefix.SOURCE: (ObjectType.SCHEMA_SOURCE, Source, "sources"),
    IdPrefix.LENS: (ObjectType.SCHEMA_LENS, Lens, "lenses"),
}

SchemaLoader = Callable[[bytes], Schema]
"""Loads the Schema of a Revision from its tree root."""


class MissingObjectError(KeyError):
    """Raised when a tree references an object the store does not hold."""


@runtime_checkable
class ObjectStore(Protocol):
    """Content-addressed objects: canonical JSON payloads keyed by object hash."""

    def get(self, object_hash: bytes) -> JsonValue | None: ...

    def put(self, object_type: str, payload: JsonValue) -> bytes:
        """Store ``payload`` and return its object hash."""
        ...


class InMemoryObjectStore:
    """Dictionary-backed :class:`ObjectStore`."""

    def __init__(self) -> None:
        self._objects: dict[bytes, JsonValue] = {}

    def get(self, object_hash: bytes) -> JsonValue | None:
        return self._objects.get(object_hash)

    def put(self, object_type: str, payload: JsonValue) -> bytes:
        digest = object_hash(object_type, payload)
        self._objects.setdefault(digest, payload)
        return digest

    def __len__(self) -> int:
        return len(self._objects)


def load_object(objects: ObjectStore, digest: bytes) -> dict[str, JsonValue]:
    """Fetch an object payload, which must be a JSON object."""
    payload = objects.get(digest)
    if payload is None:
        raise MissingObjectError(digest.hex())
    if not isinstance(payload, dict):
        raise TypeError(f"object {digest.hex()} is not a JSON object")
    return payload


def schema_object_type(key: str) -> ObjectType:
    """Object type of the ``schema`` partition entry under ``key``."""
    prefix = id_prefix(key)
    if prefix is None or prefix not in _ELEMENT_TYPES:
        return ObjectType.SCHEMA_DOMAIN
    return _ELEMENT_TYPES[prefix][0]


def element_model(key: str) -> type[Model]:
    """Model class of the ``schema`` partition entry under ``key``."""
    prefix = id_prefix(key)
    if prefix is None or prefix not in _ELEMENT_TYPES:
        return Domain
    return _ELEMENT_TYPES[prefix][1]


def data_object_type(partition: str) -> ObjectType:
    """Object type stored in a data partition: Records for Kinds, Links for Relations."""
    if partition.startswith(_RELATION_PREFIX):
        return ObjectType.LINK
    return ObjectType.RECORD


def partition_element(partition: str) -> str | None:
    """Kind or Relation id owning a data partition; ``None`` for the ``schema`` partition."""
    for prefix in (_KIND_PREFIX, _RELATION_PREFIX):
        if partition.startswith(prefix):
            return partition[len(prefix) :]
    return None


def put_schema(objects: ObjectStore, schema: Schema) -> list[TreeChange]:
    """Store every element of ``schema`` and return the ``schema`` partition entries."""
    changes = [
        TreeChange(
            PARTITION_SCHEMA,
            DOMAIN_KEY,
            objects.put(ObjectType.SCHEMA_DOMAIN, schema.domain.content()),
        )
    ]
    for element in schema.elements():
        changes.append(
            TreeChange(
                PARTITION_SCHEMA,
                element.id,
                objects.put(schema_object_type(element.id), element.content()),
            )
        )
    return changes


def schema_from_documents(documents: Mapping[str, Mapping[str, JsonValue]]) -> Schema:
    """Build a Schema from ``schema`` partition documents keyed as in the tree."""
    domain: JsonValue = None
    collections: dict[str, dict[str, JsonValue]] = {
        name: {} for _, _, name in _ELEMENT_TYPES.values()
    }
    for key, document in documents.items():
        prefix = id_prefix(key)
        if prefix is None or prefix not in _ELEMENT_TYPES:
            domain = dict(document)
            continue
        collections[_ELEMENT_TYPES[prefix][2]][key] = dict(document)
    if domain is None:
        raise ValueError("the schema partition holds no Domain document")
    return Schema.model_validate({"domain": domain, **collections})


def load_schema(store: NodeStore, objects: ObjectStore, root: bytes) -> Schema:
    """Load the Schema of a tree from its ``schema`` partition."""
    documents = {
        key: load_object(objects, digest)
        for key, digest in tree_entries(store, root, PARTITION_SCHEMA)
    }
    return schema_from_documents(documents)


def tree_schema_loader(store: NodeStore, objects: ObjectStore) -> SchemaLoader:
    """A :data:`SchemaLoader` reading the ``schema`` partition, with a per-root cache."""
    cache: dict[bytes, Schema] = {}

    def loader(root: bytes) -> Schema:
        schema = cache.get(root)
        if schema is None:
            schema = cache[root] = load_schema(store, objects, root)
        return schema

    return loader
