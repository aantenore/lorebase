"""Content-addressed objects in Postgres (`objects`), scoped to one Domain (design 6.1)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import JsonValue
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

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


class SessionObjectStore:
    """Synchronous core `ObjectStore` of one Domain over a synchronous `Session`.

    Like `PgNodeStore` it runs inside `AsyncSession.run_sync`: reads query `objects` (and are
    cached for the life of the store), writes are buffered and handed to
    `PgObjectStore.put_many` with `take_pending`.
    """

    def __init__(self, session: Session, domain_id: str, *, batch: int = 1000) -> None:
        self._session = session
        self._domain = domain_id
        self._batch = batch
        self._known: dict[bytes, dict[str, Any]] = {}
        self._pending: dict[bytes, EncodedObject] = {}

    def get(self, object_hash: bytes) -> JsonValue | None:
        pending = self._pending.get(object_hash)
        if pending is not None:
            return pending.body
        if object_hash not in self._known:
            self.prefetch([object_hash])
        return self._known.get(object_hash)

    def put(self, object_type: str, payload: JsonValue) -> bytes:
        if not isinstance(payload, dict):
            raise TypeError("object content must be a JSON object")
        encoded = encode_object(object_type, payload)
        self._pending.setdefault(encoded.hash, encoded)
        return encoded.hash

    def prefetch(self, hashes: Iterable[bytes]) -> None:
        """Load the given objects in batched queries; known ones are skipped."""
        missing = [
            h for h in dict.fromkeys(hashes) if h not in self._known and h not in self._pending
        ]
        for chunk in chunks(missing, self._batch):
            rows = self._session.execute(
                select(OBJECTS.c.hash, OBJECTS.c.body).where(
                    OBJECTS.c.domain_id == self._domain, OBJECTS.c.hash.in_(chunk)
                )
            )
            for digest, body in rows:
                self._known[digest] = body

    def take_pending(self) -> list[EncodedObject]:
        """Return and forget the buffered writes."""
        pending, self._pending = self._pending, {}
        self._known.update({digest: item.body for digest, item in pending.items()})
        return list(pending.values())
