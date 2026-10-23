"""What a reader limited to some Kinds sees of a schema (design 13.3, Kind-scoped invisibility).

`visible_schema(schema, kinds)` is the schema as a caller whose grants are narrowed to the
Kinds `kinds` (api names) sees it: the other Kinds are not there at all, and neither is
anything that would tell about them. Reads resolve names against this schema, so a name the
caller may not read fails exactly like a name that does not exist.

Left out, besides the invisible Kinds:

- Relations with an invisible end, and path Derivations with a step left out;
- Derivations computing an Attribute of an invisible Kind, aggregating over a Relation left
  out, or reading (an expression's `links.<name>` fields, an aggregate's source Attribute and
  filter) anything left out, transitively; the derived Attributes they compute go with them;
- Attributes of type `ref` (or arrays of `ref`) to an invisible Kind;
- Traits implemented by invisible Kinds only, or declaring a reference to one;
- Operations whose parameters, steps or expressions name anything left out;
- Sources writing an invisible Kind or a Relation or Attribute left out;
- Kinds keyed by a reference to an invisible Kind, and Kinds whose titles, full-text index
  or embeddings come from an Attribute left out (a derived searchable or title Attribute
  counting Records of an invisible Kind): their keys, stored titles and search vectors would
  tell about it, so they are invisible too.

Elements are conservative supersets: an element that may tell about an invisible Kind is left
out, even when it would not in practice.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from lorebase_core.expr.ast import Ident, Node, Select, parse
from lorebase_core.expr.derivations import _names, _nodes, derivation_reads
from lorebase_core.expr.environment import LINKS, link_fields
from lorebase_core.expr.errors import ExprError
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.model.attribute import Attribute
from lorebase_core.model.derivation import (
    AggregateDerivation,
    Derivation,
    ExpressionDerivation,
    PathDerivation,
)
from lorebase_core.model.kind import Kind
from lorebase_core.model.operation import CreateLinkStep, DeleteLinkStep, Operation
from lorebase_core.model.relation import Relation
from lorebase_core.model.schema import Schema
from lorebase_core.model.source import Source
from lorebase_core.model.trait import Trait
from lorebase_core.types import TypeSpec

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def visible_schema(
    schema: Schema, kinds: Collection[str], limits: ExprLimits | None = None
) -> Schema:
    """`schema` as seen by a reader of the Kinds named `kinds` (api names) only."""
    limits = limits or ExprLimits()
    shown = {k.id for k in schema.kinds.values() if k.api_name in kinds}
    if len(shown) == len(schema.kinds):
        return schema
    while True:
        relations, paths, dropped, derivations = _project(schema, shown, limits)
        # A Kind whose keys hold keys of a Kind left out, or whose titles, full-text index or
        # embeddings are computed from something left out, would tell about it through them:
        # it is left out too.
        tainted = {
            kind_id
            for kind_id in shown
            if _indexes_hidden(schema.kinds[kind_id], dropped)
            or _keys_hidden(schema.kinds[kind_id], shown)
        }
        if not tainted:
            break
        shown -= tainted
    sources = {
        s.id for s in schema.sources.values() if _source_visible(s, shown, relations, dropped)
    }
    kept_kinds = {
        kind_id: _kind(kind, dropped, sources)
        for kind_id, kind in schema.kinds.items()
        if kind_id in shown
    }
    kept_relations = {
        rid: relation.model_copy(
            update={"attributes": _attributes(relation.attributes, dropped, sources)}
        )
        for rid, relation in schema.relations.items()
        if rid in relations
    }
    traits = {
        tid: trait
        for tid, trait in schema.traits.items()
        if _trait_visible(schema, tid, shown)
        and not any(_refs_hidden(a.type, shown) for a in trait.attributes.values())
    }
    kept_kinds = {
        kind_id: kind.model_copy(
            update={"traits": tuple(t for t in kind.traits if t.trait_id in traits)}
        )
        for kind_id, kind in kept_kinds.items()
    }
    hidden_words = _hidden_words(schema, shown, relations | paths, dropped)
    operations = {
        oid: operation
        for oid, operation in schema.operations.items()
        if _operation_visible(operation, shown, relations, dropped, hidden_words)
    }
    return schema.model_copy(
        update={
            "kinds": kept_kinds,
            "relations": kept_relations,
            "traits": traits,
            "operations": operations,
            "derivations": {
                d: schema.derivations[d] for d in schema.derivations if d in derivations
            },
            "sources": {s: schema.sources[s] for s in schema.sources if s in sources},
        }
    )


def _project(
    schema: Schema, shown: Collection[str], limits: ExprLimits
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Relations, path Derivations, left out Attributes and Derivations for the Kinds
    `shown` (ids)."""
    relations = {
        r.id for r in schema.relations.values() if r.from_kind in shown and r.to_kind in shown
    }
    paths = {
        path.id
        for path in schema.path_relations().values()
        if all(step.relation.id in relations for step in path.steps)
    }
    dropped = _hidden_attributes(schema, shown)
    derivations = _visible_derivations(schema, shown, relations | paths, dropped, limits)
    for kind in schema.kinds.values():
        dropped |= {
            a.id
            for a in kind.attributes.values()
            if a.derivation_id is not None and a.derivation_id not in derivations
        }
    return relations, paths, dropped, derivations


def _indexes_hidden(kind: Kind, dropped: Collection[str]) -> bool:
    """Whether a left out Attribute of `kind` feeds its titles, full-text index or
    embeddings."""
    template = kind.search.embedding_template
    words = set(_WORD.findall(template)) if template else set()
    return any(
        a.id in dropped and (a.searchable or a.id == kind.title_attribute or a.api_name in words)
        for a in kind.attributes.values()
    )


def _refs_hidden(spec: TypeSpec, shown: Collection[str]) -> bool:
    """Whether a type holds keys of a Kind not in `shown`."""
    if spec.kind_ref is not None and spec.kind_ref not in shown:
        return True
    return spec.element is not None and _refs_hidden(spec.element, shown)


def _keys_hidden(kind: Kind, shown: Collection[str]) -> bool:
    """Whether a primary key Attribute of `kind` holds keys of a Kind not in `shown`."""
    return any(
        (a := kind.attributes.get(attribute_id)) is not None and _refs_hidden(a.type, shown)
        for attribute_id in kind.primary_key
    )


def _hidden_attributes(schema: Schema, shown: Collection[str]) -> set[str]:
    """Stored Attributes holding keys of Kinds not in `shown`."""
    owners: list[Kind | Relation] = [*schema.kinds.values(), *schema.relations.values()]
    return {
        a.id for owner in owners for a in owner.attributes.values() if _refs_hidden(a.type, shown)
    }


def _expression_links(
    schema: Schema, kind_id: str, source: str, limits: ExprLimits
) -> set[str] | None:
    """Ids of the Relations (or path Derivations) an expression over a Record of `kind_id`
    reads through `links.<name>`; None when it uses `links` otherwise (every one, then)."""
    try:
        root = parse(source, limits)
    except ExprError:
        return set()  # not evaluated: expression validation reports it
    names = _names(root)
    if LINKS in names.idents and _bare_links(root):
        return None
    fields = link_fields(schema, kind_id)
    return {fields[name].relation.id for name in names.links if name in fields}


def _bare_links(root: Node) -> bool:
    """Whether `links` appears other than as `links.<name>`."""
    selected = sum(
        1
        for node in _nodes(root)
        if isinstance(node, Select)
        and isinstance(node.operand, Ident)
        and node.operand.name == LINKS
    )
    named = sum(1 for node in _nodes(root) if isinstance(node, Ident) and node.name == LINKS)
    return named > selected


def _derivation_visible(
    schema: Schema,
    derivation: Derivation,
    shown: Collection[str],
    links: Collection[str],
    dropped: Collection[str],
    limits: ExprLimits,
) -> bool:
    definition = derivation.definition
    if isinstance(definition, PathDerivation):
        return derivation.id in links
    if definition.kind not in shown:
        return False
    if isinstance(definition, AggregateDerivation):
        if definition.relation not in links:
            return False
        relation = schema.link_relation(definition.relation)
        if relation is None:
            return False
        if definition.filter is not None:
            far = relation.far_endpoint(definition.direction)
            used = _expression_links(schema, far, definition.filter, limits)
            if used is None or not used <= set(links):
                return False
    elif isinstance(definition, ExpressionDerivation):
        used = _expression_links(schema, definition.kind, definition.expression, limits)
        if used is None or not used <= set(links):
            return False
    reads = derivation_reads(schema, derivation, limits)
    return all(kind in shown and attribute not in dropped for kind, attribute in reads)


def _visible_derivations(
    schema: Schema,
    shown: Collection[str],
    links: Collection[str],
    dropped: set[str],
    limits: ExprLimits,
) -> set[str]:
    """Ids of the Derivations left in; `dropped` gains the derived Attributes left out."""
    visible = set(schema.derivations)
    changed = True
    while changed:
        changed = False
        for derivation_id in sorted(visible):
            derivation = schema.derivations[derivation_id]
            if _derivation_visible(schema, derivation, shown, links, dropped, limits):
                continue
            visible.discard(derivation_id)
            target = derivation.target
            if target is not None:
                dropped.add(target[1])
            changed = True
    return visible


def _attributes(
    attributes: dict[str, Attribute], dropped: Collection[str], sources: Collection[str]
) -> dict[str, Attribute]:
    found: dict[str, Attribute] = {}
    for attribute_id, attribute in attributes.items():
        if attribute_id in dropped:
            continue
        if attribute.authority is not None and attribute.authority not in sources:
            attribute = attribute.model_copy(update={"authority": None})
        found[attribute_id] = attribute
    return found


def _kind(kind: Kind, dropped: Collection[str], sources: Collection[str]) -> Kind:
    attributes = _attributes(kind.attributes, dropped, sources)
    return kind.model_copy(update={"attributes": attributes})


def _trait_visible(schema: Schema, trait_id: str, shown: Collection[str]) -> bool:
    implementers = schema.kinds_implementing(trait_id)
    return not implementers or any(k.id in shown for k in implementers)


def _source_visible(
    source: Source, shown: Collection[str], relations: Collection[str], dropped: Collection[str]
) -> bool:
    return (
        source.target_kind in shown
        and all(m.relation in relations for m in source.relation_mapping)
        and not any(a in dropped for a in source.attribute_mapping)
    )


def _hidden_words(
    schema: Schema, shown: Collection[str], links: Collection[str], dropped: Collection[str]
) -> frozenset[str]:
    """Names of what is left out (Kinds, Relations both ways, path Derivations, Attributes)."""
    words: set[str] = {k.api_name for k in schema.kinds.values() if k.id not in shown}
    for relation in schema.relations.values():
        if relation.id not in links:
            words |= {relation.api_name, relation.inverse_api_name}
    for path in schema.path_relations().values():
        if path.id not in links:
            words |= {path.relation.api_name, path.relation.inverse_api_name}
    for kind in schema.kinds.values():
        words |= {a.api_name for a in kind.attributes.values() if a.id in dropped}
    return frozenset(w for w in words if w)


def _operation_visible(
    operation: Operation,
    shown: Collection[str],
    relations: Collection[str],
    dropped: Collection[str],
    hidden_words: Collection[str],
) -> bool:
    if any(_refs_hidden(p.type, shown) for p in operation.parameters):
        return False
    expressions: list[str] = [p.expression for p in operation.preconditions]
    if operation.review_policy.expression is not None:
        expressions.append(operation.review_policy.expression)
    for step in operation.steps:
        if isinstance(step, CreateLinkStep | DeleteLinkStep):
            if step.relation not in relations:
                return False
            expressions += [step.from_, step.to]
        else:
            if step.kind not in shown:
                return False
            target = getattr(step, "target", None)
            if target is not None:
                expressions.append(target)
        values: dict[str, str] = getattr(step, "values", {})
        if any(a in dropped for a in values):
            return False
        expressions += list(values.values())
    return not any(set(_WORD.findall(e)) & set(hidden_words) for e in expressions)


def element_ids(schema: Schema) -> set[str]:
    """Ids of every schema element and of every Attribute of a Kind, Relation or Trait."""
    ids = {element.id for element in schema.elements()}
    owners: list[Kind | Relation | Trait] = [
        *schema.kinds.values(),
        *schema.relations.values(),
        *schema.traits.values(),
    ]
    for owner in owners:
        ids |= set(owner.attributes)
    return ids


__all__ = ["element_ids", "visible_schema"]
