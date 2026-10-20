"""Data changes validated as one batch: Record and Link upserts and deletes.

An upsert carries the complete new content of the Record or Link.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from lorebase_core.model.base import Model
from lorebase_core.model.ids import KindId, RelationId
from lorebase_core.model.record import Link, LinkIdentity, Record, RecordIdentity
from lorebase_core.types import normalize_key


class RecordUpsert(Model):
    op: Literal["upsert_record"] = "upsert_record"
    record: Record

    @property
    def identity(self) -> RecordIdentity:
        return self.record.identity


class RecordDelete(Model):
    op: Literal["delete_record"] = "delete_record"
    kind: KindId
    key: str

    @field_validator("key")
    @classmethod
    def _normalize_key(cls, key: str) -> str:
        return normalize_key(key)

    @property
    def identity(self) -> RecordIdentity:
        return RecordIdentity(self.kind, self.key)


class LinkUpsert(Model):
    op: Literal["upsert_link"] = "upsert_link"
    link: Link

    @property
    def identity(self) -> LinkIdentity:
        return self.link.identity


class LinkDelete(Model):
    op: Literal["delete_link"] = "delete_link"
    relation: RelationId
    from_key: str = Field(alias="from")
    to_key: str = Field(alias="to")

    @field_validator("from_key", "to_key")
    @classmethod
    def _normalize_key(cls, key: str) -> str:
        return normalize_key(key)

    @property
    def identity(self) -> LinkIdentity:
        return LinkIdentity(self.relation, self.from_key, self.to_key)


DataChange = Annotated[
    RecordUpsert | RecordDelete | LinkUpsert | LinkDelete,
    Field(discriminator="op"),
]
