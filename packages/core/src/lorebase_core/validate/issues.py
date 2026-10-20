"""Validation issues: structured findings of schema and data validation."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    """Stable issue codes. ``schema.*`` come from static validation, the others from data."""

    # Schema: names and ids
    SCHEMA_API_NAME_INVALID = "schema.api_name_invalid"
    SCHEMA_API_NAME_DUPLICATE = "schema.api_name_duplicate"
    SCHEMA_API_NAME_RESERVED = "schema.api_name_reserved"
    SCHEMA_ID_DUPLICATE = "schema.id_duplicate"
    SCHEMA_DESCRIPTION_MISSING = "schema.description_missing"
    # Schema: Kinds and Attributes
    SCHEMA_PRIMARY_KEY_UNKNOWN = "schema.primary_key_unknown"
    SCHEMA_PRIMARY_KEY_INVALID = "schema.primary_key_invalid"
    SCHEMA_TITLE_ATTRIBUTE_MISSING = "schema.title_attribute_missing"
    SCHEMA_TITLE_ATTRIBUTE_UNKNOWN = "schema.title_attribute_unknown"
    SCHEMA_TYPE_REFERENCE_UNKNOWN = "schema.type_reference_unknown"
    SCHEMA_CONSTRAINT_INVALID = "schema.constraint_invalid"
    SCHEMA_DEFAULT_INVALID = "schema.default_invalid"
    SCHEMA_DEFAULT_CONFLICT = "schema.default_conflict"
    SCHEMA_ATTRIBUTE_FLAG_INVALID = "schema.attribute_flag_invalid"
    SCHEMA_AUTHORITY_UNKNOWN = "schema.authority_unknown"
    # Schema: Relations
    SCHEMA_RELATION_ENDPOINT_UNKNOWN = "schema.relation_endpoint_unknown"
    SCHEMA_FOREIGN_KEY_INVALID = "schema.foreign_key_invalid"
    # Schema: Traits
    SCHEMA_TRAIT_UNKNOWN = "schema.trait_unknown"
    SCHEMA_TRAIT_DUPLICATE = "schema.trait_duplicate"
    SCHEMA_TRAIT_MAPPING_MISSING = "schema.trait_mapping_missing"
    SCHEMA_TRAIT_MAPPING_INVALID = "schema.trait_mapping_invalid"
    SCHEMA_TRAIT_MAPPING_TYPE = "schema.trait_mapping_type"
    SCHEMA_TRAIT_MAPPING_REQUIRED = "schema.trait_mapping_required"
    # Schema: Operations, Derivations, Sources
    SCHEMA_EXPRESSIONS_DISABLED = "schema.expressions_disabled"
    SCHEMA_EXPRESSION_INVALID = "schema.expression_invalid"
    SCHEMA_OPERATION_REFERENCE_UNKNOWN = "schema.operation_reference_unknown"
    SCHEMA_OPERATION_INVALID = "schema.operation_invalid"
    SCHEMA_DERIVATION_REFERENCE_UNKNOWN = "schema.derivation_reference_unknown"
    SCHEMA_DERIVATION_INVALID = "schema.derivation_invalid"
    SCHEMA_DERIVATION_CYCLE = "schema.derivation_cycle"
    SCHEMA_SOURCE_REFERENCE_UNKNOWN = "schema.source_reference_unknown"
    SCHEMA_SOURCE_INVALID = "schema.source_invalid"

    # Data: values (Records and Links)
    VALUE_ATTRIBUTE_UNKNOWN = "value.attribute_unknown"
    VALUE_DERIVED = "value.derived"
    VALUE_TYPE = "value.type"
    VALUE_NOT_CANONICAL = "value.not_canonical"
    VALUE_REQUIRED = "value.required"
    VALUE_MIN = "value.min"
    VALUE_MAX = "value.max"
    VALUE_MIN_LENGTH = "value.min_length"
    VALUE_MAX_LENGTH = "value.max_length"
    VALUE_PATTERN = "value.pattern"
    VALUE_ENUM_VALUES = "value.enum_values"
    VALUE_UNIQUE = "value.unique"
    VALUE_REFERENCE_MISSING = "value.reference_missing"
    # Data: Records
    RECORD_KIND_UNKNOWN = "record.kind_unknown"
    RECORD_KEY_INVALID = "record.key_invalid"
    RECORD_KEY_MISMATCH = "record.key_mismatch"
    RECORD_TRAIT_CONFORMANCE = "record.trait_conformance"
    RECORD_NOT_FOUND = "record.not_found"
    RECORD_DELETE_RESTRICTED = "record.delete_restricted"
    RECORD_REQUIRED_RELATION = "record.required_relation"
    # Data: Links
    LINK_RELATION_UNKNOWN = "link.relation_unknown"
    LINK_FOREIGN_KEY_BACKED = "link.foreign_key_backed"
    LINK_ENDPOINT_MISSING = "link.endpoint_missing"
    LINK_CARDINALITY = "link.cardinality"
    LINK_NOT_FOUND = "link.not_found"
    # Data: change sets
    CHANGESET_DUPLICATE_CHANGE = "changeset.duplicate_change"


PathPart = str | int


class Issue(BaseModel):
    """One finding. ``path`` locates it inside the schema or the change set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: IssueCode
    severity: Severity = Severity.ERROR
    path: tuple[PathPart, ...] = ()
    message: str
    kind: str | None = None
    relation: str | None = None
    key: str | None = None
    attribute: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


def has_errors(issues: Iterable[Issue]) -> bool:
    return any(issue.is_error for issue in issues)
