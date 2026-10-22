"""Which sensitivity tags a value carries, through what computes it (design 10.2, 13.5).

A value computed from sensitive values tells about them. Derivations and the step values of
Operations read Attributes; what they compute must carry every tag of what they may read, and
never read a vault Attribute (whose stored values are references). The reads of an
Operation expression are resolved against the Kind the type checker infers for each Record
read; fields of untyped (`dyn`) values are matched by name, conservatively (a superset,
never fewer).

`effective_tags` gives, per Attribute id, the tags of a schema united with tags from elsewhere
(the default branch's schema) and carried through Derivations: what a reader must be cleared
for, whatever the schema read says. `tag_origins` tells which Kinds each tag comes from
(`effective_origins`, for every Attribute at once). `rebased_tags` stores the tags: it gives a
branch forked before the default branch tagged an Attribute the tags the default branch has.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from lorebase_core.expr.ast import (
    STEP_SYNTAX,
    STEP_VARIABLE,
    Call,
    Ident,
    Index,
    Literal,
    Node,
    Select,
    parse,
)
from lorebase_core.expr.checker import Checker
from lorebase_core.expr.derivations import _nodes, derivation_reads
from lorebase_core.expr.environment import PARAMS, PRINCIPAL, operation_environment
from lorebase_core.expr.errors import ExprError
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.types import ExprType, TypeKind
from lorebase_core.model.attribute import Attribute, Sensitivity
from lorebase_core.model.kind import Kind
from lorebase_core.model.operation import Operation
from lorebase_core.model.schema import Schema

Tags = frozenset[Sensitivity]
NO_TAGS: Tags = frozenset()


def schema_tags(schema: Schema | None) -> dict[str, Tags]:
    """Attribute id -> sensitivity tags, for the Attributes of `schema` that have any."""
    if schema is None:
        return {}
    return {a.id: frozenset(a.sensitivity) for a in all_attributes(schema) if a.sensitivity}


def all_attributes(schema: Schema) -> list[Attribute]:
    """Attributes of every Kind and Relation."""
    found = [a for kind in schema.kinds.values() for a in kind.attributes.values()]
    found += [a for rel in schema.relations.values() for a in rel.attributes.values()]
    return found


def lowered_tags(old: Schema, new: Schema) -> Tags:
    """Tags `new` drops from Attributes of `old`: removed from an Attribute, or with the
    Attribute, its Kind or its Relation."""
    after = schema_tags(new)
    lowered: set[Sensitivity] = set()
    for attribute_id, tags in schema_tags(old).items():
        lowered |= tags - after.get(attribute_id, NO_TAGS)
    return frozenset(lowered)


def raised_tags(old: Schema, new: Schema) -> Tags:
    """Tags `new` gives an Attribute that `old` does not give it, Attributes new to `new`
    included: another schema (a branch) may hold the same Attribute untagged."""
    before = schema_tags(old)
    raised: set[Sensitivity] = set()
    for attribute_id, tags in schema_tags(new).items():
        raised |= tags - before.get(attribute_id, NO_TAGS)
    return frozenset(raised)


def rebased_tags(
    schema: Schema, extra: Mapping[str, Tags], limits: ExprLimits | None = None
) -> Schema:
    """`schema` with the tags of `extra` (the default branch's, by Attribute id) on the
    Attributes it has, and on its derived Attributes the tags of what their Derivations may
    read (`effective_tags`). Tags are only ever added: a schema to store on a branch forked
    before the default branch tagged an Attribute."""
    return with_tags(schema, effective_tags(schema, extra, limits))


def with_tags(schema: Schema, extra: Mapping[str, Tags]) -> Schema:
    """`schema` with each Attribute's tags united with `extra[attribute id]`: for the checks
    that depend on tags (validation), never stored."""
    if not extra:
        return schema

    def raised(attributes: Mapping[str, Attribute]) -> dict[str, Attribute]:
        return {
            k: a
            if not (extra.get(k, NO_TAGS) - set(a.sensitivity))
            else a.model_copy(update={"sensitivity": tuple(sorted({*a.sensitivity, *extra[k]}))})
            for k, a in attributes.items()
        }

    kinds = {
        k: kind.model_copy(update={"attributes": raised(kind.attributes)})
        for k, kind in schema.kinds.items()
    }
    relations = {
        k: rel.model_copy(update={"attributes": raised(rel.attributes)})
        for k, rel in schema.relations.items()
    }
    return schema.model_copy(update={"kinds": kinds, "relations": relations})


def effective_tags(
    schema: Schema, extra: Mapping[str, Tags] | None = None, limits: ExprLimits | None = None
) -> dict[str, Tags]:
    """Attribute id -> the tags a reader must be cleared for: the schema's and `extra`'s, and
    for a derived Attribute those of everything its Derivation may read, transitively."""
    tags: dict[str, set[Sensitivity]] = {
        a.id: {*a.sensitivity, *(extra or {}).get(a.id, NO_TAGS)} for a in all_attributes(schema)
    }
    reads = {
        derivation.target[1]: [a for _, a in derivation_reads(schema, derivation, limits)]
        for derivation in schema.derivations.values()
        if derivation.target is not None
    }
    changed = True
    while changed:
        changed = False
        for target, inputs in reads.items():
            if target not in tags:
                continue
            gained = set().union(*(tags.get(i, set()) for i in inputs)) - tags[target]
            if gained:
                tags[target] |= gained
                changed = True
    return {k: frozenset(v) for k, v in tags.items() if v}


Origins = dict[Sensitivity, frozenset[str]]
"""Tag -> ids of the Kinds holding the Attributes the tag comes from."""


def effective_origins(
    schema: Schema, extra: Mapping[str, Tags] | None = None, limits: ExprLimits | None = None
) -> dict[str, Origins]:
    """Attribute id -> `tag_origins`, for every Attribute of a Kind that has a tag (by the
    schema, `extra` or what its Derivation may read), in one pass over the Derivations."""
    origins: dict[str, dict[Sensitivity, set[str]]] = {
        a.id: {tag: {kind.id} for tag in {*a.sensitivity, *(extra or {}).get(a.id, NO_TAGS)}}
        for kind in schema.kinds.values()
        for a in kind.attributes.values()
    }
    reads = {
        derivation.target[1]: [a for _, a in derivation_reads(schema, derivation, limits)]
        for derivation in schema.derivations.values()
        if derivation.target is not None
    }
    changed = True
    while changed:
        changed = False
        for target, inputs in reads.items():
            held = origins.setdefault(target, {})
            for source in inputs:
                for tag, kinds in origins.get(source, {}).items():
                    current = held.setdefault(tag, set())
                    if not kinds <= current:
                        current |= kinds
                        changed = True
    return {
        attribute_id: {tag: frozenset(kinds) for tag, kinds in found.items()}
        for attribute_id, found in origins.items()
        if found
    }


def tag_origins(
    schema: Schema,
    attribute_id: str,
    extra: Mapping[str, Tags] | None = None,
    limits: ExprLimits | None = None,
) -> Origins:
    """Tag -> ids of the Kinds holding the Attributes the tag comes from: the Attribute's own
    tags (the schema's and `extra`'s) and, transitively, those of everything its Derivation may
    read. A value computed from a sensitive Attribute of another Kind tells about Records of
    that Kind, so a reader must be cleared on it too."""
    return effective_origins(schema, extra, limits).get(attribute_id, {})


def expression_fields(source: str, limits: ExprLimits | None = None) -> frozenset[str]:
    """Names an Operation expression selects from Records (`record(r).name`,
    `$step[0].name`), leaving out parameters and the principal."""
    try:
        root = parse(source, limits or ExprLimits())
    except ExprError:
        return frozenset()  # reported by expression validation
    names: set[str] = set()
    for node in _nodes(root):
        if isinstance(node, Select):
            operand = node.operand
            if isinstance(operand, Ident) and operand.name in (PARAMS, PRINCIPAL):
                continue
            names.add(node.field)
    return frozenset(names)


def operation_reads(
    schema: Schema,
    operation: Operation,
    source: str,
    limits: ExprLimits | None = None,
    *,
    step: int | None = None,
) -> list[Attribute]:
    """Attributes an Operation expression may read from Records. `step` is the index of the
    step the expression belongs to (`$step[n]` is defined there), `None` for preconditions
    and the review policy.

    The expression is type checked and each field is resolved against the Kind of the Record
    it is selected from, wherever the checker infers one: `record(p)`, `links(r, "rel")`,
    comprehension variables over them, conditional results and nested selections. Fields of
    untyped (`dyn`) values are matched by name against every Kind and Relation (a superset).
    An expression that does not type check is resolved from its syntax (`_guessed_reads`)."""
    try:
        root = parse(source, limits or ExprLimits())
    except ExprError:
        return []  # reported by expression validation
    environment = operation_environment(
        schema,
        operation,
        step_index=None if step is None else max(0, min(step, len(operation.steps))),
    )
    checker = Checker(environment)
    try:
        checker.check(root)
    except (ExprError, RecursionError):
        return _guessed_reads(schema, operation, root)
    found: dict[str, Attribute] = {}
    unresolved: set[str] = set()
    for node in _nodes(root):
        if not isinstance(node, Select):
            continue
        operand = checker.types.get(node.operand)
        if operand is None or operand.kind is TypeKind.DYN:
            unresolved.add(node.field)
            continue
        owner = _record_kind(schema, operand)
        if owner is None:
            continue  # parameters, the principal, maps, references, geo points
        attribute = owner.attribute_by_api_name(node.field)
        if attribute is not None:
            found[attribute.id] = attribute
    for attribute in attributes_named(schema, unresolved):
        found.setdefault(attribute.id, attribute)
    return list(found.values())


def _record_kind(schema: Schema, expr_type: ExprType) -> Kind | None:
    """The Kind of a `record(Kind)` type; `None` for any other type."""
    if expr_type.kind is not TypeKind.RECORD or expr_type.name is None:
        return None
    return schema.kinds.get(expr_type.name)


def _guessed_reads(schema: Schema, operation: Operation, root: Node) -> list[Attribute]:
    """`operation_reads` for an expression that does not type check, from its syntax alone:
    fields of `record(p)` for a parameter `p` of type `ref(Kind)`, of `$step[n]` for a step
    that writes a Record of a Kind and of `links(r, "rel")` resolve against that Kind; any
    other field is matched by name against every Kind and Relation (a superset)."""
    kinds_of_parameters = {
        p.name: p.type.kind_ref for p in operation.parameters if p.type.kind_ref is not None
    }

    def parameter(node: Node) -> str | None:
        if isinstance(node, Ident):
            return node.name
        if (
            isinstance(node, Select)
            and isinstance(node.operand, Ident)
            and node.operand.name == PARAMS
        ):
            return node.field
        return None

    def kind_of(node: Node) -> Kind | None:
        if isinstance(node, Call) and node.target is None and node.args:
            if node.function == "record":
                name = parameter(node.args[0])
                kind_id = None if name is None else kinds_of_parameters.get(name)
                return None if kind_id is None else schema.kinds.get(kind_id)
            if node.function == "links" and len(node.args) == 2:
                relation = node.args[1]
                if isinstance(relation, Literal) and isinstance(relation.value, str):
                    found = schema.link_by_api_name(relation.value)
                    if found is not None:
                        rel, direction = found
                        return schema.kinds.get(rel.far_endpoint(direction))
            return None
        if (
            isinstance(node, Index)
            and isinstance(node.operand, Ident)
            and node.operand.name in (STEP_SYNTAX, STEP_VARIABLE)
            and isinstance(node.index, Literal)
            and isinstance(node.index.value, int)
            and 0 <= node.index.value < len(operation.steps)
        ):
            step = operation.steps[node.index.value]
            kind_id = getattr(step, "kind", None)
            return None if kind_id is None else schema.kinds.get(kind_id)
        return None

    found: dict[str, Attribute] = {}
    unresolved: set[str] = set()
    for node in _nodes(root):
        if not isinstance(node, Select):
            continue
        operand = node.operand
        if isinstance(operand, Ident) and operand.name in (PARAMS, PRINCIPAL):
            continue
        owner = kind_of(operand)
        if owner is None:
            unresolved.add(node.field)
            continue
        attribute = owner.attribute_by_api_name(node.field)
        if attribute is not None:
            found[attribute.id] = attribute
    for attribute in attributes_named(schema, unresolved):
        found.setdefault(attribute.id, attribute)
    return list(found.values())


def attributes_named(schema: Schema, names: Iterable[str]) -> list[Attribute]:
    """Attributes of any Kind or Relation with one of these api names."""
    wanted = set(names)
    return [a for a in all_attributes(schema) if a.api_name in wanted]
