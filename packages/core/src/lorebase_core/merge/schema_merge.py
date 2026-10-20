"""Schema merge (design 6.8 step 2): the ``schema`` partition is merged first.

Elements changed on one side are taken; elements changed differently on both sides go
through a structural three-way merge of their JSON documents: objects (such as a Kind's
``attributes``, keyed by id) merge entry by entry, scalars and ordered lists conflict when
both sides changed them differently. The merged schema is then checked statically; problems
that neither side had (api name collisions, broken references, Trait conformance) become
Conflicts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import JsonValue, ValidationError

from lorebase_core.merge.conflicts import (
    ConflictKind,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    Side,
)
from lorebase_core.merge.objects import element_model, schema_object_type
from lorebase_core.merge.state import (
    MISSING,
    Context,
    Payload,
    Stage,
    Value,
    as_json,
    same,
)
from lorebase_core.model import IdPrefix, Schema, id_prefix
from lorebase_core.tree import (
    PARTITION_SCHEMA,
    TreeChange,
    kind_partition,
    relation_partition,
    tree_apply,
)
from lorebase_core.validate import IssueCode, validate_schema

__all__ = [
    "SchemaMerge",
    "at_path",
    "element_partition",
    "known_static_issues",
    "merge_document",
    "merge_schema",
    "pick",
]

_CODE_KINDS: dict[IssueCode, ConflictKind] = {
    IssueCode.SCHEMA_API_NAME_DUPLICATE: ConflictKind.NAMING,
    IssueCode.SCHEMA_TRAIT_DUPLICATE: ConflictKind.TRAIT_CONFORMANCE,
    IssueCode.SCHEMA_TRAIT_MAPPING_MISSING: ConflictKind.TRAIT_CONFORMANCE,
    IssueCode.SCHEMA_TRAIT_MAPPING_TYPE: ConflictKind.TRAIT_CONFORMANCE,
    IssueCode.SCHEMA_TRAIT_MAPPING_REQUIRED: ConflictKind.TRAIT_CONFORMANCE,
    IssueCode.SCHEMA_TRAIT_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_TRAIT_MAPPING_INVALID: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_PRIMARY_KEY_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_TITLE_ATTRIBUTE_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_TYPE_REFERENCE_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_RELATION_ENDPOINT_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_FOREIGN_KEY_INVALID: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_AUTHORITY_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_OPERATION_REFERENCE_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_DERIVATION_REFERENCE_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
    IssueCode.SCHEMA_SOURCE_REFERENCE_UNKNOWN: ConflictKind.BROKEN_REFERENCE,
}

ConflictHandler = Callable[[tuple[str, ...], Value, Value, Value], Value]
"""Decides a conflicting property: ``(path, base, ours, theirs) -> merged value``."""


@dataclass
class SchemaMerge:
    """The merged ``schema`` partition."""

    schema: Schema
    #: Merged ``schema`` partition: element key -> object hash.
    hashes: dict[str, bytes]
    #: Ours' tree with the merged ``schema`` partition.
    root: bytes
    #: Elements taken whole from one side, for provenance.
    origins: dict[str, Side] = field(default_factory=dict)


def element_partition(key: str) -> str | None:
    """Data partition owned by a schema element: Kinds and Relations have one."""
    prefix = id_prefix(key)
    if prefix is IdPrefix.KIND:
        return kind_partition(key)
    if prefix is IdPrefix.RELATION:
        return relation_partition(key)
    return None


def merge_document(
    base: Value,
    ours: Value,
    theirs: Value,
    on_conflict: ConflictHandler,
    atomic: frozenset[str],
    path: tuple[str, ...] = (),
) -> Value:
    """Three-way merge of JSON documents. Objects merge entry by entry unless atomic."""
    if same(ours, theirs):
        return ours
    if same(ours, base):
        return theirs
    if same(theirs, base):
        return ours
    mergeable = (
        isinstance(ours, dict)
        and isinstance(theirs, dict)
        and (isinstance(base, dict) or base is MISSING)
        and not (path and path[-1] in atomic)
    )
    if not mergeable:
        return on_conflict(path, base, ours, theirs)
    assert isinstance(ours, dict)
    assert isinstance(theirs, dict)
    base_map = base if isinstance(base, dict) else {}
    merged: dict[str, JsonValue] = {}
    for key in sorted(base_map.keys() | ours.keys() | theirs.keys()):
        value = merge_document(
            base_map.get(key, MISSING),
            ours.get(key, MISSING),
            theirs.get(key, MISSING),
            on_conflict,
            atomic,
            (*path, key),
        )
        if value is not MISSING and value is not None:
            merged[key] = value
    return merged


def at_path(document: Value, path: tuple[str | int, ...]) -> Value:
    """The value at ``path`` inside a JSON document, or ``MISSING``."""
    current = document
    for part in path:
        if isinstance(current, dict) and isinstance(part, str):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list) and isinstance(part, int) and 0 <= part < len(current):
            current = current[part]
        else:
            return MISSING
    return current


def pick(resolution: Resolution, base: Value, ours: Value, theirs: Value) -> Value:
    """The value a resolution picks among three sides; ``custom`` takes its own value."""
    choice = resolution.choice
    if choice is ResolutionChoice.OURS:
        return ours
    if choice is ResolutionChoice.THEIRS:
        return theirs
    if choice is ResolutionChoice.BASE:
        return base
    value = resolution.value
    return MISSING if value is None else value


def merge_schema(ctx: Context) -> SchemaMerge:
    """Merge the ``schema`` partition and load the merged Schema."""
    hashes = ctx.schema_hashes
    directives = ctx.directives
    patched = (
        set(directives.elements)
        | {key for key, _ in directives.properties}
        | {kind for kind, _ in directives.trait_removals}
        | {kind for kind, _ in directives.trait_mappings}
    )
    keys = sorted(set().union(*(h.keys() for h in hashes.values())) | patched)
    merged: dict[str, bytes] = {}
    origins: dict[str, Side] = {}
    for key in keys:
        b, o, t = ctx.element_hashes(key)
        if key not in patched:
            if o == t:
                if o is not None:
                    merged[key] = o
                continue
            if o == b or t == b:
                chosen, side = (t, Side.THEIRS) if o == b else (o, Side.OURS)
                if chosen is not None:
                    merged[key] = chosen
                origins[key] = side
                continue
        document = _merge_element(ctx, key)
        if document is not None:
            merged[key] = ctx.put(schema_object_type(key), document)
    changes = [
        TreeChange(PARTITION_SCHEMA, key, merged.get(key))
        for key in keys
        if merged.get(key) != hashes[Side.OURS].get(key)
    ]
    ours_root = ctx.roots[Side.OURS]
    assert ours_root is not None
    root = tree_apply(ctx.store, ours_root, changes, ctx.params)
    schema = ctx.schema_loader(root)
    result = SchemaMerge(schema=schema, hashes=merged, root=root, origins=origins)
    if not ctx.virtual:
        _static_conflicts(ctx, result)
    return result


def _merge_element(ctx: Context, key: str) -> Payload | None:
    """Merge one element changed on both sides, then apply directives and validate it."""
    b, o, t = ctx.element_hashes(key)
    base_doc, ours_doc, theirs_doc = (ctx.document(h) for h in (b, o, t))
    partition = element_partition(key)
    document: Payload | None
    if key in ctx.directives.elements:
        document = ctx.directives.elements[key]
    elif o in (t, b) or t == b:
        document = ours_doc if t in (o, b) else theirs_doc
    elif base_doc is not None and (ours_doc is None or theirs_doc is None):
        document = _delete_modify(ctx, key, base_doc, ours_doc, theirs_doc)
    else:

        def on_conflict(path: tuple[str, ...], bv: Value, ov: Value, tv: Value) -> Value:
            conflict = ctx.conflict(
                ConflictKind.SCHEMA_PROPERTY,
                PARTITION_SCHEMA,
                key,
                ".".join(path),
                (as_json(bv), as_json(ov), as_json(tv)),
                (b, o, t),
                message=f"{'.'.join(path)} of {key} changed differently on both sides",
            )
            resolution = ctx.decide(conflict, key, None)
            if resolution is not None:
                return pick(resolution, bv, ov, tv)
            ctx.report(conflict)
            _block(ctx, key, partition)
            return bv

        value = merge_document(
            MISSING if base_doc is None else base_doc,
            MISSING if ours_doc is None else ours_doc,
            MISSING if theirs_doc is None else theirs_doc,
            on_conflict,
            ctx.policy.atomic_properties,
        )
        document = value if isinstance(value, dict) else None
    if document is None:
        return None
    document = _apply_directives(ctx, key, document)
    return _validated(ctx, key, document, (base_doc, ours_doc, theirs_doc))


def _delete_modify(
    ctx: Context, key: str, base: Payload, ours: Payload | None, theirs: Payload | None
) -> Payload | None:
    conflict = ctx.conflict(
        ConflictKind.SCHEMA_DELETE_MODIFY,
        PARTITION_SCHEMA,
        key,
        None,
        (base, ours, theirs),
        ctx.element_hashes(key),
        message=f"{key} was deleted on one side and modified on the other",
    )
    resolution = ctx.decide(conflict, key, None)
    if resolution is None:
        ctx.report(conflict)
        partition = _block(ctx, key, element_partition(key))
        if partition is not None:
            ctx.frozen.add(partition)
        return base
    chosen = pick(
        resolution, base, MISSING if ours is None else ours, MISSING if theirs is None else theirs
    )
    if chosen is MISSING:
        return None
    if not isinstance(chosen, dict):
        raise ResolutionError(f"custom value of {conflict.fingerprint} must be an element document")
    return chosen


def _block(ctx: Context, key: str, partition: str | None) -> str | None:
    ctx.open_elements.add(key)
    if partition is not None:
        ctx.blocked.add(partition)
    return partition


def _apply_directives(ctx: Context, key: str, document: Payload) -> Payload:
    directives = ctx.directives
    result: Payload = document
    for (element, path), value in sorted(directives.properties.items(), key=lambda i: i[0]):
        if element == key:
            result = _set_path(result, path, value)
    removals = {trait for kind, trait in directives.trait_removals if kind == key}
    mappings = {
        trait: mapping
        for (kind, trait), mapping in directives.trait_mappings.items()
        if kind == key
    }
    if removals or mappings:
        traits = result.get("traits")
        if isinstance(traits, list):
            updated: list[JsonValue] = []
            for item in traits:
                trait = item.get("trait_id") if isinstance(item, dict) else None
                if isinstance(trait, str) and trait in removals:
                    continue
                if isinstance(item, dict) and isinstance(trait, str) and trait in mappings:
                    item = {**item, "attribute_map": dict(mappings[trait])}
                updated.append(item)
            result = {**result, "traits": updated}
    return result


def _set_path(document: Payload, path: tuple[str | int, ...], value: Value) -> Payload:
    updated = _set_in(document, path, value)
    return updated if isinstance(updated, dict) else document


def _set_in(current: JsonValue, path: tuple[str | int, ...], value: Value) -> JsonValue:
    """A copy of ``current`` with ``path`` set to ``value`` (``MISSING`` removes it)."""
    if not path:
        return as_json(value)
    head, rest = path[0], path[1:]
    if isinstance(head, int):
        items = list(current) if isinstance(current, list) else []
        if not 0 <= head < len(items):
            return current
        items[head] = _set_in(items[head], rest, value)
        return items
    result = dict(current) if isinstance(current, dict) else {}
    if rest:
        result[head] = _set_in(result.get(head), rest, value)
    elif value is MISSING or value is None:
        result.pop(head, None)
    else:
        result[head] = value
    return result


def _validated(
    ctx: Context,
    key: str,
    document: Payload,
    sides: tuple[Payload | None, Payload | None, Payload | None],
) -> Payload | None:
    """The canonical document of a valid element; an invalid merge result is a Conflict."""
    model = element_model(key)
    try:
        return model.model_validate(document).content()
    except ValidationError as exc:
        conflict = ctx.conflict(
            ConflictKind.SCHEMA_PROPERTY,
            PARTITION_SCHEMA,
            key,
            None,
            sides,
            ctx.element_hashes(key),
            message=f"the merged definition of {key} is invalid",
            details={"errors": [str(e["msg"]) for e in exc.errors()]},
        )
        resolution = ctx.decide(conflict, key, None)
        if resolution is None:
            ctx.report(conflict)
            _block(ctx, key, element_partition(key))
            chosen: Value = MISSING if sides[0] is None else sides[0]
        else:
            chosen = pick(resolution, *(MISSING if s is None else s for s in sides))
        if not isinstance(chosen, dict):
            return None
        return model.model_validate(chosen).content()


def known_static_issues(schemas: list[Schema | None]) -> set[tuple[object, tuple[object, ...]]]:
    """``(code, path)`` of the static errors of the given schemas."""
    known: set[tuple[object, tuple[object, ...]]] = set()
    for schema in schemas:
        if schema is not None:
            known.update((i.code, i.path) for i in validate_schema(schema) if i.is_error)
    return known


def _static_conflicts(ctx: Context, merged: SchemaMerge) -> None:
    """Conflicts for static problems of the merged schema that neither side had."""
    known = ctx.known_issues
    for issue in validate_schema(merged.schema):
        if not issue.is_error or (issue.code, issue.path) in known or len(issue.path) < 2:
            continue
        element = str(issue.path[1])
        if element in ctx.open_elements:
            continue
        rest = tuple(issue.path[2:])
        kind = _CODE_KINDS.get(issue.code, ConflictKind.SCHEMA_PROPERTY)
        attribute = ".".join(map(str, rest)) or None
        merged_doc = ctx.document(merged.hashes.get(element))
        details: dict[str, JsonValue] = {
            "code": issue.code.value,
            "path": [str(p) if isinstance(p, str) else p for p in rest],
        }
        reference = at_path(merged_doc, rest)
        if isinstance(reference, str):
            details["reference"] = reference
        if rest[:1] == ("traits",) and len(rest) > 1:
            trait = at_path(merged_doc, (*rest[:2], "trait_id"))
            if isinstance(trait, str):
                details["trait"] = trait
        group = ctx.group(
            kind,
            PARTITION_SCHEMA,
            element,
            attribute,
            stage=Stage.CHECK,
            element=element,
            message=issue.message,
            details=details,
        )
        group.values = tuple(  # type: ignore[assignment]
            as_json(at_path(ctx.element_document(side, element), rest))
            for side in (Side.BASE, Side.OURS, Side.THEIRS)
        )
