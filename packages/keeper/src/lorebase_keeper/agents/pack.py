"""Context packs: request, response shape and the JSON rendering of their sections.

A pack has six sections, rendered in this order: `seeds` (the Records asked for, with every
value), `neighbors` (Records around them up to the requested depth), `links` (grouped by the
forward api name of their Relation), `schema` (only the Kinds and Relations present),
`recent_changes` and `lineage`. The `budget` block accounts for them (see `agents.budget`).
Values stay in canonical form (design 6.1).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from lorebase_core.model import Attribute, Kind, LinkDirection, Relation, Schema
from lorebase_core.query import Query
from lorebase_core.types import AttributeType, TypeSpec
from lorebase_keeper.agents.budget import (
    BudgetResult,
    ChangeItem,
    Draft,
    LineageItem,
    LinkItem,
    NeighborItem,
    SeedItem,
    estimate_tokens,
)

SECTIONS = ("seeds", "neighbors", "links", "schema", "recent_changes", "lineage")
PackFormat = Literal["json", "markdown"]


class ContextRequest(BaseModel):
    """What an agent needs to reason about: seed Records and how much around them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str | None = Field(default=None, description="Default: the Domain's default branch.")
    at: str | None = Field(
        default=None,
        description="Revision id or tag to read at (time travel, served from a snapshot).",
    )
    refs: list[str] | None = Field(
        default=None, min_length=1, description="Seed Records as `Kind/key` references."
    )
    query: Query | None = Field(default=None, description="DSL query selecting the seeds.")
    search: str | None = Field(
        default=None, min_length=1, description="Lexical search text selecting the seeds."
    )
    budget_tokens: int | None = Field(
        default=None, ge=1, description="Estimated token budget; default from settings."
    )
    depth: int | None = Field(default=None, ge=0, description="Neighborhood depth (0: none).")
    relations: list[str] | None = Field(
        default=None,
        description="Relation api names to follow (forward or inverse); default: all.",
    )
    include_schema: bool = True
    include_recent_changes: bool = True
    include_lineage: bool = True
    since: str | None = Field(
        default=None,
        description="Revision id or tag: recent changes after it; default: the latest changes.",
    )
    format: PackFormat = Field(
        default="json", description="`markdown` answers text/markdown to paste into a prompt."
    )

    @model_validator(mode="after")
    def _one_selector(self) -> Self:
        given = [name for name in ("refs", "query", "search") if getattr(self, name) is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of refs, query or search")
        return self


# Response shape (documentation and tests; bodies are built as plain JSON) -------------------


class _Out(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class PackRecordOut(_Out):
    ref: str
    title: str | None = None
    values: dict[str, JsonValue]
    updated_revision: str | None = None


class PackNeighborOut(_Out):
    ref: str
    title: str | None = None
    depth: int
    values: dict[str, JsonValue] | None = Field(default=None, description="Absent when trimmed.")


class PackLinkOut(_Out):
    source: str = Field(alias="from")
    to: str
    values: dict[str, JsonValue] | None = None


class SchemaAttributeOut(_Out):
    name: str
    type: str
    description: str | None = None
    required: bool | None = None
    derived: bool | None = None


class SchemaKindOut(_Out):
    name: str
    description: str | None = None
    key: list[str]
    title: str | None = None
    attributes: list[SchemaAttributeOut]


class SchemaRelationOut(_Out):
    name: str
    inverse: str
    source: str = Field(alias="from")
    to: str
    cardinality: str
    description: str | None = None
    attributes: list[SchemaAttributeOut] | None = None


class SchemaFragmentOut(_Out):
    kinds: list[SchemaKindOut]
    relations: list[SchemaRelationOut]


class PackChangeOut(_Out):
    revision: str
    at: str
    ref: str
    change: Literal["added", "modified", "removed"]
    attributes: list[str] | None = None
    actor: str
    origin: str
    message: str


class PackLineageOut(_Out):
    ref: str
    attributes: list[str]
    origin: str | None = None
    actor: str | None = None
    at: str | None = None


class TrimOut(_Out):
    step: str
    removed: int


class BudgetOut(_Out):
    limit_tokens: int
    used_tokens: int = Field(description="Estimated tokens of the sections.")
    chars_per_token: float
    sections: dict[str, int] = Field(description="Estimated tokens per section.")
    trimmed: list[TrimOut]
    omitted: dict[str, int] = Field(description="Entries removed to fit, per section.")
    over_budget: bool = Field(description="True when even the seed Records exceed the budget.")
    capped: bool = Field(description="True when graph caps cut the neighborhood.")


class ContextPackOut(_Out):
    """A context pack in JSON (`format: json`)."""

    domain: str
    branch: str
    revision: str
    etag: str
    seeds: list[PackRecordOut]
    neighbors: list[PackNeighborOut]
    links: dict[str, list[PackLinkOut]]
    schema_: SchemaFragmentOut | None = Field(default=None, alias="schema")
    recent_changes: list[PackChangeOut] | None = None
    lineage: list[PackLineageOut] | None = None
    since: str | None = None
    budget: BudgetOut


# Rendering ----------------------------------------------------------------------------------


def dumps(value: Any) -> str:
    """Compact JSON, as sent and as measured."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def type_label(spec: TypeSpec, schema: Schema) -> str:
    """Short type name: `decimal(10,2)`, `enum(a|b)`, `array<string>`, `ref(Kind)`."""
    if spec.name is AttributeType.ENUM:
        return "enum(" + "|".join(spec.enum_values or ()) + ")"
    if spec.name is AttributeType.DECIMAL:
        return f"decimal({spec.precision},{spec.scale})"
    if spec.name is AttributeType.REF:
        kind = schema.kinds.get(spec.kind_ref or "")
        return f"ref({kind.api_name if kind is not None else spec.kind_ref})"
    if spec.name is AttributeType.ARRAY and spec.element is not None:
        return f"array<{type_label(spec.element, schema)}>"
    return str(spec.name)


def ordered_attributes(owner: Kind | Relation) -> list[Attribute]:
    """Attributes in a stable reading order: key Attributes, the title Attribute, then the rest
    by api name (compiled schemas do not keep the authoring order)."""
    attributes = owner.attributes
    first: list[str] = []
    if isinstance(owner, Kind):
        first = [*owner.primary_key, *([owner.title_attribute] if owner.title_attribute else [])]
    leading = [attributes[a] for a in dict.fromkeys(first) if a in attributes]
    rest = sorted((a for a in attributes.values() if a.id not in first), key=lambda a: a.api_name)
    return [*leading, *rest]


def ordered_values(owner: Kind | Relation | None, values: Mapping[str, Any]) -> dict[str, Any]:
    """`values` (keyed by api name) in `ordered_attributes` order."""
    if owner is None:
        return dict(values)
    return {
        a.api_name: values[a.api_name] for a in ordered_attributes(owner) if a.api_name in values
    }


def _attribute(attribute: Attribute, schema: Schema, described: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"name": attribute.api_name, "type": type_label(attribute.type, schema)}
    if described and attribute.description:
        out["description"] = attribute.description
    if attribute.required:
        out["required"] = True
    if attribute.is_derived:
        out["derived"] = True
    return out


def kind_fragment(kind: Kind, schema: Schema, described: bool = True) -> dict[str, Any]:
    names = {a.id: a.api_name for a in kind.attributes.values()}
    out: dict[str, Any] = {"name": kind.api_name}
    if described and kind.description:
        out["description"] = kind.description
    out["key"] = [names.get(a, a) for a in kind.primary_key] or ["key"]
    if kind.title_attribute is not None:
        out["title"] = names.get(kind.title_attribute, kind.title_attribute)
    out["attributes"] = [_attribute(a, schema, described) for a in ordered_attributes(kind)]
    return out


def relation_fragment(relation: Relation, schema: Schema, described: bool = True) -> dict[str, Any]:
    def name(kind_id: str) -> str:
        kind = schema.kinds.get(kind_id)
        return kind.api_name if kind is not None else kind_id

    out: dict[str, Any] = {
        "name": relation.api_name,
        "inverse": relation.inverse_api_name,
        "from": name(relation.from_kind),
        "to": name(relation.to_kind),
        "cardinality": str(relation.cardinality),
    }
    if described and relation.description:
        out["description"] = relation.description
    if relation.attributes:
        out["attributes"] = [_attribute(a, schema, described) for a in ordered_attributes(relation)]
    return out


def present_kinds(draft: Draft) -> list[str]:
    """Api names of the Kinds of the Records in the draft, sorted."""
    return sorted({s.kind for s in draft.seeds} | {n.kind for n in draft.neighbors})


def present_relations(draft: Draft) -> list[str]:
    """Forward api names of the Relations of the Links in the draft, sorted."""
    return sorted({link.relation for link in draft.links})


def schema_fragment(draft: Draft, schema: Schema) -> dict[str, Any]:
    kinds = [schema.kind_by_api_name(name) for name in present_kinds(draft)]
    relations = [schema.relation_by_api_name(name) for name in present_relations(draft)]
    return {
        "kinds": [kind_fragment(k, schema, draft.schema_descriptions) for k in kinds if k],
        "relations": [
            relation_fragment(found[0], schema, draft.schema_descriptions)
            for found in relations
            if found is not None and found[1] is LinkDirection.FORWARD
        ],
    }


def seed_json(item: SeedItem) -> dict[str, Any]:
    out: dict[str, Any] = {"ref": item.ref}
    if item.title is not None:
        out["title"] = item.title
    out["values"] = dict(item.values)
    if item.updated_revision is not None:
        out["updated_revision"] = item.updated_revision
    return out


def neighbor_json(item: NeighborItem) -> dict[str, Any]:
    out: dict[str, Any] = {"ref": item.ref}
    if item.title is not None:
        out["title"] = item.title
    out["depth"] = item.depth
    if item.values is not None:
        out["values"] = dict(item.values)
    return out


def links_json(links: Iterable[LinkItem]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        entry: dict[str, Any] = {"from": link.source, "to": link.target}
        if link.values:
            entry["values"] = dict(link.values)
        grouped.setdefault(link.relation, []).append(entry)
    return grouped


def change_json(item: ChangeItem) -> dict[str, Any]:
    out: dict[str, Any] = {
        "revision": item.revision,
        "at": item.at,
        "ref": item.ref,
        "change": item.change,
    }
    if item.attributes:
        out["attributes"] = list(item.attributes)
    out.update(actor=item.actor, origin=item.origin, message=item.message)
    return out


def lineage_json(item: LineageItem) -> dict[str, Any]:
    out: dict[str, Any] = {"ref": item.ref, "attributes": list(item.attributes)}
    for name in ("origin", "actor", "at"):
        value = getattr(item, name)
        if value is not None:
            out[name] = value
    return out


def json_sections(draft: Draft, schema: Schema) -> dict[str, Any]:
    """Value of each present section, in output order."""
    out: dict[str, Any] = {
        "seeds": [seed_json(s) for s in draft.seeds],
        "neighbors": [neighbor_json(n) for n in draft.neighbors],
        "links": links_json(draft.links),
    }
    if draft.schema:
        out["schema"] = schema_fragment(draft, schema)
    if draft.changes is not None:
        out["recent_changes"] = [change_json(c) for c in draft.changes]
    if draft.lineage is not None:
        out["lineage"] = [lineage_json(item) for item in draft.lineage]
    return out


def json_measure(schema: Schema) -> Any:
    """Characters of each section in compact JSON, a `budget.Measure`."""

    def measure(draft: Draft) -> Mapping[str, int]:
        return {name: len(dumps(value)) for name, value in json_sections(draft, schema).items()}

    return measure


def budget_json(
    result: BudgetResult,
    original: Draft,
    *,
    budget_tokens: int,
    chars_per_token: float,
    capped: bool,
) -> dict[str, Any]:
    final = result.draft
    omitted = {
        "neighbors": len(original.neighbors) - len(final.neighbors),
        "neighbor_values": sum(1 for n in final.neighbors if n.values is None)
        - sum(1 for n in original.neighbors if n.values is None),
        "links": len(original.links) - len(final.links),
        "recent_changes": len(original.changes or ()) - len(final.changes or ()),
        "lineage": len(original.lineage or ()) - len(final.lineage or ()),
    }
    return {
        "limit_tokens": budget_tokens,
        "used_tokens": estimate_tokens(result.chars, chars_per_token),
        "chars_per_token": chars_per_token,
        "sections": {
            name: estimate_tokens(chars, chars_per_token) for name, chars in result.sections.items()
        },
        "trimmed": [{"step": str(t.step), "removed": t.removed} for t in result.trims],
        "omitted": {name: count for name, count in omitted.items() if count},
        "over_budget": result.over_budget,
        "capped": capped,
    }
