"""Content-addressed objects in Postgres (`objects`), scoped to one Domain (design 6.1)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import ObjectType, canonical_bytes, normalize, object_hash
from lorebase_store.bulk import bulk_write, chunks
from lorebase_store.models import metadata
from lorebase_store.settings import WriteSettings

OBJECTS: Table = metadata.tables["objects"]

OBJECT_TYPE_CODES: Mapping[ObjectType, int] = MappingProxyType(
    {
        ObjectType.SCHEMA_DOMAIN: 1,
        ObjectType.SCHEMA_KIND: 2,
        ObjectType.SCHEMA_RELATION: 3,
        ObjectType.SCHEMA_TRAIT: 4,
        ObjectType.SCHEMA_OPERATION: 5,
        ObjectType.SCHEMA_DERIVATION: 6,
        ObjectType.SCHEMA_SOURCE: 7,
        ObjectType.RECORD: 8,
        ObjectType.LINK: 9,
    }
)
"""`objects.object_type` codes. Stored data depends on them: never renumber."""

OBJECT_TYPES_BY_CODE: Mapping[int, ObjectType] = MappingProxyType(
    {code: object_type for object_type, code in OBJECT_TYPE_CODES.items()}
)


@dataclass(frozen=True, slots=True)
class EncodedObject:
    """An object ready to store: hash, type, normalized body and canonical size in bytes."""

    hash: bytes
    object_type: ObjectType
    body: dict[str, Any]
    size: int


def encode_object(object_type: ObjectType | str, content: Mapping[str, Any]) -> EncodedObject:
    """Normalize `content` and compute its object hash (design 6.1)."""
    kind = ObjectType(object_type)
    body = normalize(content)
    if not isinstance(body, dict):
        raise TypeError("object content must be a JSON object")
    return EncodedObject(
        hash=object_hash(kind, body),
        object_type=kind,
        body=body,
        size=len(canonical_bytes(body)),
    )


@dataclass(frozen=True, slots=True)
class StoredBody:
    """Type and body of a stored object."""

    object_type: ObjectType
    body: dict[str, Any]


class PgObjectStore:
    """Async batched reads and bulk writes of one Domain's objects."""

    def __init__(
        self, session: AsyncSession, domain_id: str, *, writes: WriteSettings, batch: int = 1000
    ) -> None:
        self._session = session
        self._domain = domain_id
        self._writes = writes
        self._batch = batch

    async def get_many(self, hashes: Iterable[bytes]) -> dict[bytes, StoredBody]:
        """Stored objects among `hashes`; absent hashes are left out."""
        found: dict[bytes, StoredBody] = {}
        for chunk in chunks(list(dict.fromkeys(hashes)), self._batch):
            rows = await self._session.execute(
                select(OBJECTS.c.hash, OBJECTS.c.object_type, OBJECTS.c.body).where(
                    OBJECTS.c.domain_id == self._domain, OBJECTS.c.hash.in_(chunk)
                )
            )
            for digest, code, body in rows:
                found[digest] = StoredBody(OBJECT_TYPES_BY_CODE[code], body)
        return found

    async def put_many(self, objects: Iterable[EncodedObject]) -> None:
        """Persist objects; objects already stored are left unchanged."""
        unique = {item.hash: item for item in objects}
        rows = [
            {
                "domain_id": self._domain,
                "hash": item.hash,
                "object_type": OBJECT_TYPE_CODES[item.object_type],
                "body": item.body,
                "size": item.size,
            }
            for item in unique.values()
        ]
        await bulk_write(self._session, OBJECTS, rows, writes=self._writes)
