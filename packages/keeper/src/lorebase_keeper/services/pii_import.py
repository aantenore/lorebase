"""Who may feed values of vault Attributes through bulk paths (design 13.5).

Commits of the origins of `vault.tombstone_origins` (imports, Source syncs, extraction) check
values of vault Attributes against the tombstones of erased values: an erased value is stored
erased or refused. Whoever can choose such a value can therefore learn whether it was erased.
Feeding values of vault Attributes through these paths needs `pii.import` on the whole Domain
(only `admin` holds it among the built-in roles), checked before anything is committed:

- the built-in imports (`import_records`, `import_batch`) when a row holds a value of a vault
  Attribute (`import_rows_vault_attributes`);
- schema changes and merges that add a Source mapping columns to vault Attributes, change such
  a Source, or make an Attribute a Source maps a vault Attribute (`fed_vault_sources`);
- triggering a synchronization of a Source that maps vault Attributes (`source_vault_targets`).

Scheduled synchronizations run as configured: the Source was vetted when it was defined.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from lorebase_core.model import Schema, Source
from lorebase_keeper.auth.deps import missing_permission
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import NO_CONTEXT, Authorizer
from lorebase_keeper.reading.sensitivity import require_lowering
from lorebase_keeper.services.operation_catalog import (
    KIND,
    Action,
    BuiltinOperation,
)


def source_vault_targets(schema: Schema, source: Source) -> frozenset[str]:
    """Ids of the vault Attributes of the Source's target Kind that the Source maps."""
    kind = schema.kinds.get(source.target_kind)
    if kind is None:
        return frozenset()
    return frozenset(
        attribute_id
        for attribute_id in source.attribute_mapping
        if (attribute := kind.attributes.get(attribute_id)) is not None and attribute.in_vault
    )


def fed_vault_sources(old: Schema, new: Schema) -> list[str]:
    """Ids of the Sources of `new` that map vault Attributes and are new, changed, or map
    vault Attributes `old` did not."""
    fed: list[str] = []
    for source_id, source in new.sources.items():
        targets = source_vault_targets(new, source)
        if not targets:
            continue
        before = old.sources.get(source_id)
        if before is None or before != source or not targets <= source_vault_targets(old, before):
            fed.append(source_id)
    return sorted(fed)


def import_rows_vault_attributes(
    schema: Schema, definition: BuiltinOperation, parameters: Mapping[str, JsonValue]
) -> list[str]:
    """Api names of the vault Attributes some row of a built-in import holds a value of.
    Malformed rows are skipped: binding refuses them later."""
    rows: list[tuple[object, object]] = []
    if definition.action is Action.IMPORT_RECORDS:
        found = parameters.get("rows")
        if isinstance(found, list):
            rows = [(parameters.get(KIND), row) for row in found]
    elif definition.action is Action.IMPORT_BATCH:
        found = parameters.get("records")
        if isinstance(found, list):
            rows = [(row.get(KIND) if isinstance(row, dict) else None, row) for row in found]
    names: set[str] = set()
    for kind_name, row in rows:
        if not isinstance(row, dict) or not isinstance(kind_name, str):
            continue
        kind = schema.kind_by_api_name(kind_name)
        if kind is None:
            continue
        names.update(
            a.api_name
            for a in kind.attributes.values()
            if a.in_vault and row.get(a.api_name) is not None
        )
    return sorted(names)


async def require_pii_import(authorizer: Authorizer, principal: Principal, domain_id: str) -> None:
    """403 naming `pii.import` unless the caller holds it through a Domain-wide grant."""
    if not await authorizer.allows(principal, Permission.PII_IMPORT, domain_id, NO_CONTEXT):
        raise missing_permission(Permission.PII_IMPORT, domain_id)


async def require_source_feeding(
    authorizer: Authorizer, principal: Principal, domain_id: str, old: Schema, new: Schema
) -> None:
    """403 naming `pii.import` when `new` adds or changes a Source feeding vault Attributes
    and the caller does not hold it Domain-wide."""
    if fed_vault_sources(old, new):
        await require_pii_import(authorizer, principal, domain_id)


async def require_schema_change(
    authorizer: Authorizer, principal: Principal, domain_id: str, old: Schema, new: Schema
) -> None:
    """The checks of a caller's schema change or merge from `old` to `new`: removed
    sensitivity tags need their clearance (`reading.sensitivity.require_lowering`), Sources
    feeding vault Attributes need `pii.import`."""
    await require_lowering(authorizer, principal, domain_id, old, new)
    await require_source_feeding(authorizer, principal, domain_id, old, new)
