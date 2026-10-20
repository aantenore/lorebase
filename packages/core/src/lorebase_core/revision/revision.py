"""Revision objects and their content hash (design 6.4)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from lorebase_core.canonical import HASH_SIZE, from_hex, hex, revision_hash

__all__ = [
    "ROOT_GENERATION",
    "Author",
    "Hash",
    "Origin",
    "Revision",
    "RevisionStats",
    "format_timestamp",
    "next_generation",
    "revision_id",
]

ROOT_GENERATION = 1
"""Generation number of a Revision without parents."""


def _parse_hash(value: object) -> object:
    if isinstance(value, str):
        return from_hex(value)
    if isinstance(value, bytes) and len(value) != HASH_SIZE:
        raise ValueError(f"hashes are {HASH_SIZE} bytes")
    return value


Hash = Annotated[bytes, BeforeValidator(_parse_hash)]
"""A 32-byte hash; accepts raw bytes or lowercase hex."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Author(_Frozen):
    principal: str = Field(min_length=1)
    on_behalf_of: str | None = None


class Origin(_Frozen):
    type: str = Field(min_length=1)
    ref: str | None = None


class RevisionStats(_Frozen):
    partitions_changed: int = Field(ge=0)
    changes: int = Field(ge=0)


class Revision(_Frozen):
    """An immutable snapshot: tree root, parents and metadata.

    ``parents[0]`` is the first parent (the target branch for merges).
    """

    parents: tuple[Hash, ...] = ()
    root: Hash
    domain: str = Field(min_length=1)
    author: Author
    message: str
    origin: Origin
    created_at: datetime
    stats: RevisionStats

    @field_validator("parents")
    @classmethod
    def _distinct_parents(cls, parents: tuple[bytes, ...]) -> tuple[bytes, ...]:
        if len(set(parents)) != len(parents):
            raise ValueError("parents must be distinct")
        return parents

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must carry a time zone")
        return value.astimezone(UTC)

    def canonical_payload(self) -> dict[str, object]:
        """The JSON document whose canonical form is hashed into the Revision id."""
        return {
            "parents": [hex(p) for p in self.parents],
            "root": hex(self.root),
            "domain": self.domain,
            "author": self.author.model_dump(),
            "message": self.message,
            "origin": self.origin.model_dump(),
            "created_at": format_timestamp(self.created_at),
            "stats": self.stats.model_dump(),
        }


def revision_id(revision: Revision) -> bytes:
    """Revision id: ``sha256("lb1\\x00revision\\x00" + canonical_json)``."""
    return revision_hash(revision.canonical_payload())


def next_generation(parent_generations: Iterable[int]) -> int:
    """Generation of a Revision: ``1 + max(parent generations)``, or 1 without parents."""
    return 1 + max(parent_generations, default=ROOT_GENERATION - 1)


def format_timestamp(value: datetime) -> str:
    """RFC 3339 UTC with exactly six fractional digits."""
    if value.tzinfo is None:
        raise ValueError("timestamp must carry a time zone")
    utc = value.astimezone(UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )
