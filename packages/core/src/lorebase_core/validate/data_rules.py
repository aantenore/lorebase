"""Data validation of single Records and Links against a Schema (invariant I1).

These rules need no access to other data: types, canonical form, constraints, required
Attributes, keys and Trait conformance. Rules that need existing data (uniqueness,
cardinality, referential integrity) live in ``changeset``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import JsonValue

from lorebase_core.model.attribute import Attribute
from lorebase_core.model.ids import is_ulid
from lorebase_core.model.kind import Kind
from lorebase_core.model.record import Link, Record, record_key
from lorebase_core.model.relation import RelationBacking
from lorebase_core.model.schema import Schema
from lorebase_core.types import (
    CastError,
    ConstraintName,
    EncodingError,
    cast,
    check_constraints,
    encode_value,
    is_canonical,
)
from lorebase_core.validate.issues import Issue, IssueCode, PathPart

CONSTRAINT_CODES: dict[ConstraintName, IssueCode] = {
    ConstraintName.MIN: IssueCode.VALUE_MIN,
    ConstraintName.MAX: IssueCode.VALUE_MAX,
    ConstraintName.MIN_LENGTH: IssueCode.VALUE_MIN_LENGTH,
    ConstraintName.MAX_LENGTH: IssueCode.VALUE_MAX_LENGTH,
    ConstraintName.PATTERN: IssueCode.VALUE_PATTERN,
    ConstraintName.ENUM_VALUES: IssueCode.VALUE_ENUM_VALUES,
}


class RecordExistence(Protocol):
    """The part of ``IndexView`` needed to check Link endpoints."""

    def record_exists(self, kind_id: str, key: str) -> bool: ...


def check_values(
    attributes: Mapping[str, Attribute],
    values: Mapping[str, JsonValue],
    path: tuple[PathPart, ...],
    *,
    kind: str | None = None,
    relation: str | None = None,
    key: str | None = None,
) -> list[Issue]:
    """Type, canonical form, constraint and required checks of a value map."""
    issues: list[Issue] = []

    def report(code: IssueCode, attribute_id: str, message: str, **details: JsonValue) -> None:
        issues.append(
            Issue(
                code=code,
                path=(*path, "values", attribute_id),
                message=message,
                kind=kind,
                relation=relation,
                key=key,
                attribute=attribute_id,
                details=details,
            )
        )

    for attribute_id, value in values.items():
        attribute = attributes.get(attribute_id)
        if attribute is None:
            report(
                IssueCode.VALUE_ATTRIBUTE_UNKNOWN, attribute_id, f"unknown attribute {attribute_id}"
            )
            continue
        name = attribute.api_name
        if attribute.is_derived:
            report(
                IssueCode.VALUE_DERIVED,
                attribute_id,
                f"{name!r} is derived; derived values are never stored",
            )
            continue
        try:
            canonical = encode_value(attribute.type, value)
        except EncodingError as exc:
            report(IssueCode.VALUE_TYPE, attribute_id, f"{name!r}: {exc}", reason=exc.reason.value)
            continue
        if canonical != value:
            report(
                IssueCode.VALUE_NOT_CANONICAL,
                attribute_id,
                f"{name!r} is not in canonical form; expected {canonical!r}",
                canonical=canonical,
            )
            continue
        for violation in check_constraints(attribute.type, attribute.constraints, canonical):
            details: dict[str, JsonValue] = {"constraint": violation.constraint.value}
            if violation.index is not None:
                details["index"] = violation.index
            report(
                CONSTRAINT_CODES[violation.constraint],
                attribute_id,
                f"{name!r}: {violation.message}",
                **details,
            )

    for attribute in attributes.values():
        if attribute.required and not attribute.is_derived and values.get(attribute.id) is None:
            report(IssueCode.VALUE_REQUIRED, attribute.id, f"{attribute.api_name!r} is required")
    return issues


def _check_key(kind: Kind, record: Record, path: tuple[PathPart, ...]) -> list[Issue]:
    if not kind.has_natural_key:
        if is_ulid(record.key):
            return []
        return [
            Issue(
                code=IssueCode.RECORD_KEY_INVALID,
                path=(*path, "key"),
                message=f"Kind {kind.api_name!r} has no primary key; Record keys are minted ULIDs",
                kind=kind.id,
                key=record.key,
            )
        ]
    for attribute_id in kind.primary_key:
        attribute = kind.attributes.get(attribute_id)
        value = record.values.get(attribute_id)
        if attribute is None or value is None or not is_canonical(attribute.type, value):
            return []
    expected = record_key(kind, record.values)
    if expected is None or expected == record.key:
        return []
    return [
        Issue(
            code=IssueCode.RECORD_KEY_MISMATCH,
            path=(*path, "key"),
            message=f"key {record.key!r} does not match the primary key values ({expected!r})",
            kind=kind.id,
            key=record.key,
            details={"expected": expected},
        )
    ]


def _check_traits(
    schema: Schema, kind: Kind, record: Record, path: tuple[PathPart, ...]
) -> list[Issue]:
    issues: list[Issue] = []
    for implementation in kind.traits:
        trait = schema.traits.get(implementation.trait_id)
        if trait is None:
            continue
        for trait_attribute in trait.attributes.values():
            attribute_id = implementation.attribute_map.get(trait_attribute.id)
            attribute = kind.attributes.get(attribute_id) if attribute_id is not None else None
            label = f"{trait.api_name}.{trait_attribute.api_name}"
            if attribute is None:
                continue
            value = record.values.get(attribute.id)
            message: str | None = None
            if value is None:
                if trait_attribute.required:
                    message = f"{label} is required by Trait {trait.api_name!r}"
            elif is_canonical(attribute.type, value):
                try:
                    cast(attribute.type, trait_attribute.type, value)
                except CastError as exc:
                    message = f"{label}: {exc.message}"
            if message is not None:
                issues.append(
                    Issue(
                        code=IssueCode.RECORD_TRAIT_CONFORMANCE,
                        path=(*path, "values", attribute.id),
                        message=message,
                        kind=kind.id,
                        key=record.key,
                        attribute=attribute.id,
                        details={"trait": trait.id, "trait_attribute": trait_attribute.id},
                    )
                )
    return issues


def validate_record(
    schema: Schema, record: Record, *, path: tuple[PathPart, ...] = ()
) -> list[Issue]:
    """Validate one Record against its Kind: key, types, constraints, required, Traits."""
    kind = schema.kinds.get(record.kind)
    if kind is None:
        return [
            Issue(
                code=IssueCode.RECORD_KIND_UNKNOWN,
                path=(*path, "kind"),
                message=f"unknown Kind {record.kind}",
                kind=record.kind,
                key=record.key,
            )
        ]
    issues = _check_key(kind, record, path)
    issues.extend(check_values(kind.attributes, record.values, path, kind=kind.id, key=record.key))
    issues.extend(_check_traits(schema, kind, record, path))
    return issues


def validate_link(
    schema: Schema,
    link: Link,
    index_view: RecordExistence | None = None,
    *,
    path: tuple[PathPart, ...] = (),
) -> list[Issue]:
    """Validate one Link against its Relation; with ``index_view``, also check its endpoints."""
    relation = schema.relations.get(link.relation)
    if relation is None:
        return [
            Issue(
                code=IssueCode.LINK_RELATION_UNKNOWN,
                path=(*path, "relation"),
                message=f"unknown Relation {link.relation}",
                relation=link.relation,
            )
        ]
    if relation.backing is RelationBacking.FOREIGN_KEY:
        return [
            Issue(
                code=IssueCode.LINK_FOREIGN_KEY_BACKED,
                path=(*path, "relation"),
                message=f"Links of {relation.api_name!r} derive from a foreign key attribute",
                relation=relation.id,
            )
        ]
    issues = check_values(relation.attributes, link.values, path, relation=relation.id)
    if index_view is not None:
        for end, kind_id, key in (
            ("from", relation.from_kind, link.from_key),
            ("to", relation.to_kind, link.to_key),
        ):
            if not index_view.record_exists(kind_id, key):
                issues.append(
                    Issue(
                        code=IssueCode.LINK_ENDPOINT_MISSING,
                        path=(*path, end),
                        message=f"{end} Record {key!r} does not exist",
                        kind=kind_id,
                        relation=relation.id,
                        key=key,
                    )
                )
    return issues
