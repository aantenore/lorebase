"""Data migrations implied by a schema change (design 6.6 step 2).

For every Kind or Relation whose definition changed, existing values are carried to the new
definition in the same Revision:

- values of removed Attributes are dropped;
- values of retyped Attributes are cast with the core cast rules; a value that cannot be cast
  is an `XS3`-class violation naming the Record;
- Attributes that are new, or became required, receive their default when they have none;
- unchanged Records are re-validated (tightened constraints, new required Attributes) and
  values of Attributes that became unique are checked for duplicates (`XS8` class).

Records and Links of removed Kinds and Relations are removed. Elements the commit itself
changes are left to the commit's own changes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import JsonValue

from lorebase_core.model import (
    Attribute,
    IdPrefix,
    Link,
    LinkIdentity,
    ProvenanceEntry,
    Record,
    RecordIdentity,
    Schema,
    SchemaMigrationOrigin,
    new_id,
)
from lorebase_core.types import CastError, TypeSpec, cast, format_timestamp
from lorebase_core.validate import (
    DataChange,
    Issue,
    IssueCode,
    LinkDelete,
    LinkUpsert,
    RecordDelete,
    RecordUpsert,
    validate_link,
    validate_record,
)
from lorebase_store.commit.changes import Change

TYPE_COERCION_CLASS = "XS3"
UNIQUENESS_CLASS = "XS8"
_SAMPLE_SIZE = 10


@dataclass
class MigrationPlan:
    """Changes implied by a schema change."""

    #: Value migrations; validated with the commit's own changes.
    changes: list[Change] = field(default_factory=list)
    #: Records and Links of removed Kinds and Relations; not validated (their type is gone).
    removals: list[DataChange] = field(default_factory=list)
    provenance: list[ProvenanceEntry] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    #: Kinds whose definition changed: titles and search vectors are recomputed.
    refresh_kinds: list[str] = field(default_factory=list)


@dataclass
class _Attribution:
    """One provenance entry per migrated Attribute and type transition."""

    actor: str
    at: datetime
    entries: dict[tuple[str, str | None, str | None], ProvenanceEntry] = field(default_factory=dict)

    def entry(self, attribute_id: str, from_type: TypeSpec | None, to_type: TypeSpec | None) -> str:
        memo = (
            attribute_id,
            None if from_type is None else from_type.describe(),
            None if to_type is None else to_type.describe(),
        )
        found = self.entries.get(memo)
        if found is None:
            found = ProvenanceEntry(
                id=new_id(IdPrefix.PROVENANCE),
                origin=SchemaMigrationOrigin(
                    attribute_id=attribute_id, from_type=from_type, to_type=to_type
                ),
                actor=self.actor,
                at=format_timestamp(self.at),
            )
            self.entries[memo] = found
        return found.id


def plan_migrations(
    old: Schema,
    new: Schema,
    *,
    records_of: Callable[[str], Iterable[Record]],
    links_of: Callable[[str], Iterable[Link]],
    skip: Iterable[RecordIdentity | LinkIdentity] = (),
    actor: str,
    at: datetime,
    sensitive: Callable[[Attribute], bool] = lambda a: a.is_sensitive,
) -> MigrationPlan:
    """Data changes that carry existing values from `old` to `new`. Issues never quote
    values of `sensitive` Attributes (by default, those with tags in the schemas)."""
    plan = MigrationPlan()
    attribution = _Attribution(actor, at)
    skipped = set(skip)
    for kind_id in sorted(old.kinds.keys() - new.kinds.keys()):
        plan.removals.extend(
            RecordDelete(kind=kind_id, key=record.key) for record in records_of(kind_id)
        )
    for relation_id in sorted(old.relations.keys() - new.relations.keys()):
        plan.removals.extend(
            LinkDelete(relation=relation_id, from_key=link.from_key, to_key=link.to_key)
            for link in links_of(relation_id)
        )
    for kind_id in sorted(old.kinds.keys() & new.kinds.keys()):
        old_kind, new_kind = old.kinds[kind_id], new.kinds[kind_id]
        if old_kind == new_kind:
            continue
        plan.refresh_kinds.append(kind_id)
        plan.issues.extend(_vault_changes(kind_id, old_kind.attributes, new_kind.attributes))
        unique = [
            a
            for a in new_kind.attributes.values()
            if a.unique and not (a.id in old_kind.attributes and old_kind.attributes[a.id].unique)
        ]
        holders: dict[tuple[str, str], list[str]] = defaultdict(list)
        for record in records_of(kind_id):
            if record.identity in skipped:
                continue
            values, provenance, issues = _migrate_values(
                old_kind.attributes, new_kind.attributes, record.values, attribution, sensitive
            )
            for issue in issues:
                plan.issues.append(
                    issue.model_copy(
                        update={
                            "kind": kind_id,
                            "key": record.key,
                            "path": ("migrations", kind_id, record.key, *issue.path),
                        }
                    )
                )
            migrated = Record(kind=kind_id, key=record.key, values=values)
            # A Record with a failed cast is already reported; validating it would repeat it.
            if provenance and not issues:
                plan.changes.append(Change(RecordUpsert(record=migrated), provenance))
            elif not issues:
                plan.issues.extend(
                    validate_record(new, migrated, path=("migrations", kind_id, record.key))
                )
            for attribute in unique:
                value = migrated.values.get(attribute.id)
                if value is not None:
                    holders[(attribute.id, _value_text(value))].append(record.key)
        plan.issues.extend(_duplicates(kind_id, unique, holders, sensitive))
    for relation_id in sorted(old.relations.keys() & new.relations.keys()):
        old_relation, new_relation = old.relations[relation_id], new.relations[relation_id]
        if old_relation.attributes == new_relation.attributes:
            continue
        for link in links_of(relation_id):
            if link.identity in skipped:
                continue
            values, provenance, issues = _migrate_values(
                old_relation.attributes,
                new_relation.attributes,
                link.values,
                attribution,
                sensitive,
            )
            plan.issues.extend(
                issue.model_copy(
                    update={
                        "relation": relation_id,
                        "path": (
                            "migrations",
                            relation_id,
                            link.from_key,
                            link.to_key,
                            *issue.path,
                        ),
                    }
                )
                for issue in issues
            )
            migrated_link = link.model_copy(update={"values": values})
            if issues:
                continue
            if provenance:
                plan.changes.append(Change(LinkUpsert(link=migrated_link), provenance))
            else:
                plan.issues.extend(
                    validate_link(new, migrated_link, path=("migrations", relation_id))
                )
    plan.provenance = list(attribution.entries.values())
    return plan


def _migrate_values(
    old_attributes: Mapping[str, Attribute],
    new_attributes: Mapping[str, Attribute],
    values: Mapping[str, JsonValue],
    attribution: _Attribution,
    is_sensitive: Callable[[Attribute], bool],
) -> tuple[dict[str, JsonValue], dict[str, str], list[Issue]]:
    migrated: dict[str, JsonValue] = {}
    provenance: dict[str, str] = {}
    issues: list[Issue] = []
    for attribute_id, value in values.items():
        attribute = new_attributes.get(attribute_id)
        before = old_attributes.get(attribute_id)
        if attribute is None:
            provenance[attribute_id] = attribution.entry(
                attribute_id, None if before is None else before.type, None
            )
            continue
        if before is None or before.type == attribute.type:
            migrated[attribute_id] = value
            continue
        try:
            converted: Any = cast(before.type, attribute.type, value)
        except CastError as error:
            # Sensitive values are not quoted: the caller may lack the clearance to read them.
            sensitive = is_sensitive(before) or is_sensitive(attribute)
            details: dict[str, JsonValue] = {
                "class": TYPE_COERCION_CLASS,
                "from_type": before.type.describe(),
                "to_type": attribute.type.describe(),
            }
            if not sensitive:
                details["value"] = value
            shown = "a value" if sensitive else f"value {value!r}"
            issues.append(
                Issue(
                    code=IssueCode.VALUE_TYPE,
                    path=("values", attribute_id),
                    message=(
                        f"{attribute.api_name!r} {shown} cannot be cast from "
                        f"{before.type.describe()} to {attribute.type.describe()}"
                        + ("" if sensitive else f": {error.message}")
                    ),
                    attribute=attribute_id,
                    details=details,
                )
            )
            migrated[attribute_id] = value
            continue
        if converted is not None:
            migrated[attribute_id] = converted
        if converted != value:
            provenance[attribute_id] = attribution.entry(attribute_id, before.type, attribute.type)
    for attribute in new_attributes.values():
        if attribute.id in migrated or attribute.default is None:
            continue
        before = old_attributes.get(attribute.id)
        if before is None or (attribute.required and not before.required):
            migrated[attribute.id] = attribute.default
            provenance[attribute.id] = attribution.entry(attribute.id, None, attribute.type)
    return migrated, provenance, issues


def _value_text(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _vault_changes(
    kind_id: str, old: Mapping[str, Attribute], new: Mapping[str, Attribute]
) -> list[Issue]:
    """Vault storage is chosen when an Attribute is created: turning it on or off, or
    retyping a vault Attribute, would leave Revisions holding values the other way."""
    issues: list[Issue] = []
    for attribute_id, attribute in new.items():
        before = old.get(attribute_id)
        if before is None:
            continue
        if before.in_vault != attribute.in_vault:
            message = "vault storage cannot be turned on or off; add a new attribute instead"
        elif attribute.in_vault and before.type != attribute.type:
            message = "the type of a vault attribute cannot change; add a new attribute instead"
        else:
            continue
        issues.append(
            Issue(
                code=IssueCode.SCHEMA_VAULT_INVALID,
                path=("kinds", kind_id, "attributes", attribute_id, "vault"),
                message=f"{attribute.api_name!r}: {message}",
                kind=kind_id,
                attribute=attribute_id,
            )
        )
    return issues


def _duplicates(
    kind_id: str,
    unique: list[Attribute],
    holders: Mapping[tuple[str, str], list[str]],
    is_sensitive: Callable[[Attribute], bool],
) -> list[Issue]:
    names = {attribute.id: attribute.api_name for attribute in unique}
    sensitive = {attribute.id for attribute in unique if is_sensitive(attribute)}
    issues: list[Issue] = []
    for (attribute_id, value), keys in sorted(holders.items()):
        if len(keys) < 2:
            continue
        ordered = sorted(keys)
        issues.append(
            Issue(
                code=IssueCode.VALUE_UNIQUE,
                path=("migrations", kind_id, ordered[0], "values", attribute_id),
                message=(
                    f"{names[attribute_id]!r} became unique but {len(keys)} Records share "
                    + ("a value" if attribute_id in sensitive else f"the value {value}")
                ),
                kind=kind_id,
                key=ordered[0],
                attribute=attribute_id,
                details={
                    "class": UNIQUENESS_CLASS,
                    "count": len(keys),
                    "keys": list(ordered[:_SAMPLE_SIZE]),
                },
            )
        )
    return issues
