"""Source (design 5.9): external backing data mapped into a Kind. Model only."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Self

from pydantic import Field, JsonValue, model_validator

from lorebase_core.model.base import ElementStatus, Model, SchemaElement
from lorebase_core.model.ids import AttributeId, KindId, RelationId, SourceId

#: Prefix of secret references; connections are never literals (design 5.9, 13.4).
SECRET_REFERENCE_PREFIX = "secret://"


class SourceType(StrEnum):
    POSTGRES_TABLE = "postgres_table"
    POSTGRES_QUERY = "postgres_query"
    CSV = "csv"
    PARQUET = "parquet"
    HTTP_JSON = "http_json"


class SyncMode(StrEnum):
    SNAPSHOT = "snapshot"
    INCREMENTAL = "incremental"


class MergePolicy(StrEnum):
    AUTO_WHEN_CLEAN = "auto_when_clean"
    PROPOSAL = "proposal"
    AUTO_WITH_AUTHORITY = "auto_with_authority"


class TransformName(StrEnum):
    CAST = "cast"
    TRIM = "trim"
    LOWER = "lower"
    UPPER = "upper"
    PARSE_DATE = "parse_date"
    CONCAT = "concat"
    CONSTANT = "constant"
    MAP = "map"
    NULL_IF = "null_if"


class Transform(Model):
    name: TransformName
    args: dict[str, JsonValue] = Field(default_factory=dict)


class AttributeMapping(Model):
    """Maps one column or one expression, through transforms, to an Attribute."""

    column: str | None = None
    expression: str | None = None
    transforms: tuple[Transform, ...] = ()

    @model_validator(mode="after")
    def _check_input(self) -> Self:
        if (self.column is None) == (self.expression is None):
            raise ValueError("attribute mapping needs exactly one of column or expression")
        return self


class RelationMapping(Model):
    """Foreign key columns of the Source row that identify the ``to`` Record of ``relation``."""

    relation: RelationId
    columns: tuple[str, ...] = Field(min_length=1)


class TargetBranchPolicy(Model):
    branch_template: str = "source/{source}"
    merge_policy: MergePolicy = MergePolicy.PROPOSAL


class Source(SchemaElement):
    object_type: ClassVar[str] = "schema.source"

    id: SourceId
    type: SourceType
    connection: str
    #: Table of a ``postgres_table`` Source, optionally schema-qualified (``erp.orders``);
    #: ``None`` reads the table named by the last segment of the api name.
    table: str | None = None
    #: Read-only ``SELECT`` of a ``postgres_query`` Source.
    query: str | None = None
    target_kind: KindId
    #: Source columns producing the primary key, in primary key order.
    key_mapping: tuple[str, ...] = ()
    attribute_mapping: dict[AttributeId, AttributeMapping] = Field(default_factory=dict)
    relation_mapping: tuple[RelationMapping, ...] = ()
    scope_filter: str | None = None
    sync_mode: SyncMode = SyncMode.SNAPSHOT
    watermark_column: str | None = None
    tombstone_column: str | None = None
    #: Cron expression; ``None`` means manual runs only.
    schedule: str | None = None
    target_branch_policy: TargetBranchPolicy = Field(default_factory=TargetBranchPolicy)
    status: ElementStatus = ElementStatus.ACTIVE
