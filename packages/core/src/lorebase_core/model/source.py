"""Source (design 5.9): external backing data mapped into a Kind. Model only."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import Field, JsonValue, model_validator

from lorebase_core.model.base import ElementStatus, Model, SchemaElement
from lorebase_core.model.ids import AttributeId, KindId, RelationId, SourceId
from lorebase_core.types import AttributeType, EncodingError, TypeSpec, decode_value, encode_value

_DURATION = TypeSpec.of(AttributeType.DURATION)

#: Prefix of secret references; connections are never literals (design 5.9, 13.4).
SECRET_REFERENCE_PREFIX = "secret://"


class SourceType(StrEnum):
    POSTGRES_TABLE = "postgres_table"
    POSTGRES_QUERY = "postgres_query"
    MYSQL_TABLE = "mysql_table"
    MYSQL_QUERY = "mysql_query"
    MSSQL_TABLE = "mssql_table"
    MSSQL_QUERY = "mssql_query"
    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"
    PARQUET = "parquet"
    HTTP_JSON = "http_json"


class SyncMode(StrEnum):
    SNAPSHOT = "snapshot"
    INCREMENTAL = "incremental"


class MergePolicy(StrEnum):
    AUTO_WHEN_CLEAN = "auto_when_clean"
    PROPOSAL = "proposal"
    AUTO_WITH_AUTHORITY = "auto_with_authority"


class LocalEdits(StrEnum):
    """What an upstream change does to a value someone else set on the target since the
    Source last wrote it (design 9.1 "Corrections that stick")."""

    #: A Conflict of the Source Proposal, decided by a person (the default).
    REVIEW = "review"
    #: The same Conflict, resolved at once in favor of a person's value.
    KEEP = "keep"
    #: The upstream value overwrites the current one.
    SOURCE_WINS = "source_wins"


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


class PeriodMapping(Model):
    """One end of the validity period of a mapped value: a column or an expression (design
    10.6). Read as a timestamp; a date means its first instant (UTC)."""

    column: str | None = None
    expression: str | None = None

    @model_validator(mode="after")
    def _check_input(self) -> Self:
        if (self.column is None) == (self.expression is None):
            raise ValueError("a period mapping needs exactly one of column or expression")
        return self


class AttributeMapping(Model):
    """Maps one column or one expression, through transforms, to an Attribute. A value of a
    ``valid_time`` Attribute may also map its validity period (``valid_from``, ``valid_to``),
    which lands on the provenance entry of the value, not in content."""

    column: str | None = None
    expression: str | None = None
    transforms: tuple[Transform, ...] = ()
    valid_from: PeriodMapping | None = None
    valid_to: PeriodMapping | None = None

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


class PaginationType(StrEnum):
    NONE = "none"
    CURSOR = "cursor"
    PAGE = "page"
    OFFSET = "offset"
    LINK = "link"


class HttpPagination(Model):
    """How an ``http_json`` Source asks for the next page.

    ``cursor`` sends the value found at ``cursor_path`` of a response as ``cursor_param`` of the
    next request; ``page`` counts ``page_param`` up from ``start_page``; ``offset`` moves
    ``offset_param`` by ``page_size``; ``link`` follows the ``rel="next"`` URL of the ``Link``
    header. ``size_param`` (with ``page_size``) asks for the page size.
    """

    type: PaginationType = PaginationType.NONE
    cursor_path: str | None = Field(default=None, min_length=1)
    cursor_param: str = Field(default="cursor", min_length=1)
    page_param: str = Field(default="page", min_length=1)
    start_page: int = Field(default=1, ge=0)
    offset_param: str = Field(default="offset", min_length=1)
    size_param: str | None = Field(default=None, min_length=1)
    page_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_type(self) -> Self:
        if self.type is PaginationType.CURSOR and self.cursor_path is None:
            raise ValueError("cursor pagination needs cursor_path")
        if self.type is PaginationType.OFFSET and self.page_size is None:
            raise ValueError("offset pagination needs page_size")
        if self.size_param is not None and self.page_size is None:
            raise ValueError("size_param needs page_size")
        return self


#: Request headers an ``http_json`` Source may not set: Scribe owns the target and the framing.
RESERVED_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "te", "upgrade"}
)
#: Header names containing one of these carry credentials: their values must be secret
#: references, so no credential is ever stored in a schema.
CREDENTIAL_HEADER_HINTS = (
    "auth",
    "token",
    "secret",
    "key",
    "password",
    "cookie",
    "session",
    "signature",
)


def credential_header(name: str) -> bool:
    """Whether the value of header ``name`` must be a secret reference."""
    lowered = name.lower()
    return any(hint in lowered for hint in CREDENTIAL_HEADER_HINTS)


class HttpOptions(Model):
    """Requests of an ``http_json`` Source (the URL is the value of its ``connection``)."""

    #: Header values; values of credential headers (``credential_header``) must be secret
    #: references, resolved by Scribe like the connection.
    headers: dict[str, str] = Field(default_factory=dict)
    pagination: HttpPagination = Field(default_factory=HttpPagination)
    #: Query parameter carrying the watermark of the previous run on incremental reads, so the
    #: server can filter; rows are filtered by the watermark column either way.
    since_param: str | None = Field(default=None, min_length=1)
    #: Pages read at most by one run, below ``scribe.sources.http_json.max_pages``.
    max_pages: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_headers(self) -> Self:
        for name, value in self.headers.items():
            if not name.strip() or name.lower() in RESERVED_HEADERS:
                raise ValueError(f"header {name!r} cannot be set by a Source")
            if credential_header(name) and not value.startswith(SECRET_REFERENCE_PREFIX):
                raise ValueError(
                    f"header {name!r} carries credentials: its value must be a secret "
                    f"reference ({SECRET_REFERENCE_PREFIX}...), never a literal"
                )
        return self


class Source(SchemaElement):
    object_type: ClassVar[str] = "schema.source"

    id: SourceId
    type: SourceType
    connection: str
    #: Table of a ``postgres_table``, ``mysql_table`` or ``mssql_table`` Source, optionally
    #: schema-qualified (``erp.orders``); ``None`` reads the table named by the last segment
    #: of the api name.
    table: str | None = None
    #: Read-only ``SELECT`` of a ``postgres_query``, ``mysql_query`` or ``mssql_query`` Source.
    query: str | None = None
    #: ``json`` and ``http_json``: dotted path of the array of records in the document
    #: (``data.items``); ``None``: the document is the array.
    records_path: str | None = Field(default=None, min_length=1)
    #: ``json``, ``jsonl``, ``http_json`` and ``parquet``: nested objects become columns named
    #: ``parent<separator>child``; ``None`` keeps them as JSON values.
    flatten_separator: str | None = Field(default=None, min_length=1)
    #: Requests of an ``http_json`` Source.
    http: HttpOptions | None = None
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
    #: ``None`` means ``review``; left out of the content so existing Sources keep their hash.
    local_edits: LocalEdits | None = None
    #: ``incremental`` Sources: ISO 8601 duration (``P30D``) after which a Record the Source
    #: owns and no run has read upstream is raised as a ``stale`` candidate (``source_unseen``,
    #: design 9.1); ``None`` (the default, left out of the content) turns the check off.
    stale_after: str | None = None
    status: ElementStatus = ElementStatus.ACTIVE

    @property
    def local_edit_mode(self) -> LocalEdits:
        """The effective ``local_edits`` setting."""
        return self.local_edits or LocalEdits.REVIEW

    @property
    def stale_after_delta(self) -> timedelta | None:
        """``stale_after`` as a duration; ``None`` when unset or not a valid duration (schema
        validation reports the latter)."""
        if self.stale_after is None:
            return None
        try:
            delta = decode_value(_DURATION, encode_value(_DURATION, self.stale_after))
        except (EncodingError, ValueError):
            return None
        return delta if isinstance(delta, timedelta) else None
