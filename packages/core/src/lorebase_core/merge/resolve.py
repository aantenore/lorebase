"""From collected Conflict groups to reported Conflicts or merge directives (design 6.10).

A Conflict whose fingerprint has a resolution turns into directives for the next run of the
merge (schema overrides, ignored side changes, data overrides). Conflicts found by re-checking
the merged state are reported again when the applied resolution did not make them go away.
"""

from __future__ import annotations

from pydantic import JsonValue

from lorebase_core.merge.conflicts import (
    GROUPED_KINDS,
    ConflictKind,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    Side,
)
from lorebase_core.merge.consistency import relaxed_cardinality
from lorebase_core.merge.objects import partition_element
from lorebase_core.merge.schema_merge import SchemaMerge, at_path
from lorebase_core.merge.state import (
    MISSING,
    SIDES,
    Context,
    Directives,
    Group,
    Payload,
    Stage,
    Value,
    check_choice,
    other_side,
)
from lorebase_core.model import Cardinality
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, link_key, tree_get

__all__ = ["emit_groups"]

_C = ResolutionChoice
_K = ConflictKind


def emit_groups(ctx: Context, schema: SchemaMerge) -> None:
    """Report the collected Conflicts, or turn the resolved ones into directives."""
    ordered = sorted(
        ctx.groups.values(),
        key=lambda g: (g.kind.value, g.partition, g.key or "", g.attribute or "", g.stage.value),
    )
    for group in ordered:
        members = sorted(group.members) if group.kind in GROUPED_KINDS else None
        conflict = ctx.conflict(
            group.kind,
            group.partition,
            group.key,
            group.attribute,
            group.values,
            ctx.element_hashes(group.element),
            members=members,
            message=group.message,
            details=group.details,
        )
        resolution = ctx.resolutions.get(conflict.fingerprint)
        directives = None
        if resolution is not None:
            check_choice(conflict, resolution)
            directives = _directives(ctx, schema, group, resolution)
        if directives is None:
            ctx.report(conflict)
            if members is not None:
                ctx.members[conflict.fingerprint] = tuple(members)
            continue
        ctx.out.absorb(directives)
        if group.stage is Stage.CHECK and ctx.directives.covers(directives):
            ctx.report(conflict)
            if members is not None:
                ctx.members[conflict.fingerprint] = tuple(members)


def _directives(
    ctx: Context, schema: SchemaMerge, group: Group, resolution: Resolution
) -> Directives | None:
    """Directives that carry out ``resolution``; ``None`` when it does not apply."""
    kind, choice = group.kind, resolution.choice
    if group.partition == PARTITION_SCHEMA:
        return _schema_directives(ctx, schema, group, resolution)
    element = partition_element(group.partition)
    if element is None:
        return None
    directives = Directives()
    attribute = group.attribute
    members = sorted(group.members)
    if choice is _C.CUSTOM and kind is not _K.CARDINALITY and kind is not _K.REQUIRED_RELATION:
        if attribute is None:
            return None
        for member in members:
            if kind is _K.UNIQUENESS and (
                resolution.values is None or member not in resolution.values
            ):
                continue
            directives.values[(group.partition, member, attribute)] = _value(
                resolution.value_for(member)
            )
        return directives
    if kind in (_K.ORPHANED_DATA, _K.ORPHANED_VALUE, _K.TYPE_COERCION) and choice is _C.RESTORE:
        return _restore_schema(ctx, group, element)
    if choice is _C.DROP:
        return _drop(ctx, group, members)
    if choice is _C.RELAX:
        return _relax(ctx, schema, group, element)
    if kind is _K.CARDINALITY and choice is _C.CUSTOM:
        kept = resolution.value if isinstance(resolution.value, list) else []
        keep = {tuple(pair) for pair in kept if isinstance(pair, list)}
        for member in members:
            detail = group.members[member]
            links = detail.get("links") if isinstance(detail, dict) else None
            for pair in links if isinstance(links, list) else []:
                if isinstance(pair, list) and tuple(pair) not in keep:
                    directives.states[(group.partition, link_key(str(pair[0]), str(pair[1])))] = (
                        None
                    )
        return directives
    if kind is _K.REQUIRED_RELATION and choice is _C.CUSTOM:
        for member in members:
            target = resolution.value_for(member)
            if not isinstance(target, str):
                raise ResolutionError("required relation resolutions name the Record to link to")
            directives.states[(group.partition, link_key(member, target))] = {
                "relation": element,
                "from": member,
                "to": target,
                "values": {},
            }
        return directives
    if kind is _K.DANGLING_LINK and choice is _C.RESTORE:
        for member in members:
            detail = group.members[member]
            missing = detail.get("missing") if isinstance(detail, dict) else None
            for target in missing if isinstance(missing, list) else []:
                if not isinstance(target, list) or len(target) != 2:
                    continue
                location = (kind_partition(str(target[0])), str(target[1]))
                restored = _latest_version(ctx, location)
                if restored is None:
                    return None
                directives.states[location] = restored
        return directives
    return None


def _value(value: JsonValue) -> Value:
    return MISSING if value is None else value


def _drop(ctx: Context, group: Group, members: list[str]) -> Directives | None:
    directives = Directives()
    if group.kind in (_K.ORPHANED_DATA, _K.ORPHANED_VALUE) and group.stage is Stage.DECISION:
        return directives
    if group.kind is _K.ORPHANED_VALUE or (
        group.kind is _K.DANGLING_LINK and group.attribute is not None
    ):
        assert group.attribute is not None
        for member in members:
            directives.values[(group.partition, member, group.attribute)] = MISSING
        return directives
    if group.kind is _K.REQUIRED_RELATION:
        element = partition_element(group.partition)
        relation = ctx.schemas[Side.OURS], ctx.schemas[Side.THEIRS]
        from_kind = next(
            (s.relations[element].from_kind for s in relation if s and element in s.relations),
            None,
        )
        if from_kind is None:
            return None
        for member in members:
            directives.states[(kind_partition(from_kind), member)] = None
        return directives
    for member in members:
        directives.states[(group.partition, member)] = None
    return directives


def _restore_schema(ctx: Context, group: Group, element: str) -> Directives | None:
    """Restore what the schema side removed or retyped, keeping the data side's version."""
    data_side = group.data_side
    if data_side is None:
        return None
    schema_side = other_side(data_side)
    document = ctx.element_document(data_side, element)
    if document is None:
        return None
    directives = Directives()
    if group.kind is _K.ORPHANED_DATA:
        directives.elements[element] = document
        directives.ignored_partitions.add((schema_side, group.partition))
        return directives
    attribute = group.attribute
    definition = at_path(document, ("attributes", attribute or ""))
    if attribute is None or not isinstance(definition, dict):
        return None
    if group.kind is _K.ORPHANED_VALUE:
        directives.properties[(element, ("attributes", attribute))] = definition
    else:
        directives.properties[(element, ("attributes", attribute, "type"))] = definition.get("type")
    directives.ignored_attributes.add((schema_side, group.partition, attribute))
    return directives


def _relax(ctx: Context, schema: SchemaMerge, group: Group, element: str) -> Directives | None:
    directives = Directives()
    attribute = group.attribute
    if group.kind is _K.CARDINALITY:
        relation = schema.schema.relations.get(element)
        if relation is None or attribute not in ("from", "to"):
            return None
        relaxed: Cardinality = relaxed_cardinality(relation.cardinality, attribute)
        directives.properties[(element, ("cardinality",))] = relaxed.value
        return directives
    if group.kind is _K.REQUIRED_RELATION:
        directives.properties[(element, ("required_from",))] = False
        return directives
    if group.kind is _K.TRAIT_CONFORMANCE:
        trait = _trait_of(group)
        if trait is None:
            return None
        directives.trait_removals.add((element, trait))
        return directives
    if attribute is None:
        return None
    path: tuple[str, ...] = ("attributes", attribute)
    if group.kind is _K.MISSING_VALUE:
        directives.properties[(element, (*path, "required"))] = False
    elif group.kind is _K.UNIQUENESS:
        directives.properties[(element, (*path, "unique"))] = False
    elif group.kind is _K.CONSTRAINT:
        base = at_path(ctx.element_document(Side.BASE, element), (*path, "constraints"))
        directives.properties[(element, (*path, "constraints"))] = base
    else:
        return None
    return directives


def _trait_of(group: Group) -> str | None:
    trait = group.details.get("trait")
    if isinstance(trait, str):
        return trait
    for detail in group.members.values():
        if isinstance(detail, dict) and isinstance(detail.get("trait"), str):
            return str(detail["trait"])
    return None


def _schema_directives(
    ctx: Context, schema: SchemaMerge, group: Group, resolution: Resolution
) -> Directives | None:
    directives = Directives()
    key = group.key
    if key is None:
        return None
    raw_path = group.details.get("path")
    path: tuple[str | int, ...] = (
        tuple(p for p in raw_path if isinstance(p, str | int)) if isinstance(raw_path, list) else ()
    )
    choice = resolution.choice
    if group.kind is _K.TRAIT_CONFORMANCE:
        trait = _trait_of(group)
        if trait is None:
            return None
        if choice is _C.RELAX:
            directives.trait_removals.add((key, trait))
        elif isinstance(resolution.value, dict):
            directives.trait_mappings[(key, trait)] = dict(resolution.value)
        else:
            raise ResolutionError("a custom Trait conformance resolution is an attribute map")
        return directives
    if group.kind is _K.BROKEN_REFERENCE:
        if choice is _C.CUSTOM:
            if not isinstance(resolution.value, dict):
                raise ResolutionError("a custom broken reference resolution is an element")
            directives.elements[key] = dict(resolution.value)
            return directives
        reference = group.details.get("reference")
        return _restore_reference(ctx, schema, reference) if isinstance(reference, str) else None
    if not path:
        return None
    if choice is _C.CUSTOM:
        directives.properties[(key, path)] = _value(resolution.value)
        return directives
    index = {_C.BASE: 0, _C.OURS: 1, _C.THEIRS: 2}[choice]
    directives.properties[(key, path)] = _value(group.values[index])
    return directives


def _restore_reference(ctx: Context, schema: SchemaMerge, reference: str) -> Directives | None:
    """Restore a removed schema element, or a removed Attribute of a merged element."""
    directives = Directives()
    for side in (*SIDES, Side.BASE):
        document = ctx.element_document(side, reference)
        if document is not None:
            directives.elements[reference] = document
            return directives
    for side in (*SIDES, Side.BASE):
        for owner, digest in sorted(ctx.schema_hashes[side].items()):
            if owner not in schema.hashes:
                continue
            definition = at_path(ctx.document(digest), ("attributes", reference))
            if isinstance(definition, dict):
                directives.properties[(owner, ("attributes", reference))] = definition
                return directives
    return None


def _latest_version(ctx: Context, location: tuple[str, str]) -> Payload | None:
    """The version of a Record kept by ours, else theirs, else the base."""
    for side in (*SIDES, Side.BASE):
        root = ctx.roots[side]
        digest = tree_get(ctx.store, root, *location)
        document = ctx.document(digest)
        if document is not None:
            return document
    return None
