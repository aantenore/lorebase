"""What the schema tells the extractors: labels, key patterns, value cues and the tier 2 JSON
schema of an ontology fragment.

Only active Kinds and Relations take part. Attributes that are derived, sensitive (`pii`,
`confidential`, `restricted`) or of a type extraction does not handle (`json`, `geo_point`,
`array`, `duration`) are never proposed. Sensitive means by the effective tags, as reads see
them: the schema's own, those the default branch gives the same Attribute, and those carried
through Derivations (`lorebase_core.expr.sensitivity.effective_tags`), so a schema forked
before the default branch tagged an Attribute does not make it extractable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lorebase_core.expr.sensitivity import Tags, effective_tags
from lorebase_core.model import Attribute, ElementStatus, Kind, Relation, Schema
from lorebase_core.types import AttributeType

EXTRACTABLE_TYPES = frozenset(
    {
        AttributeType.STRING,
        AttributeType.TEXT,
        AttributeType.URI,
        AttributeType.REF,
        AttributeType.ENUM,
        AttributeType.INTEGER,
        AttributeType.DECIMAL,
        AttributeType.FLOAT,
        AttributeType.BOOLEAN,
        AttributeType.DATE,
        AttributeType.TIMESTAMP,
    }
)
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "when",
        "with",
        "which",
        "who",
        "whose",
        "was",
        "were",
        "will",
        "can",
        "may",
        "per",
        "than",
        "then",
        "there",
        "their",
        "them",
        "they",
        "has",
        "have",
        "had",
        "not",
        "no",
        "yes",
        "true",
        "false",
        "example",
        "code",
        "number",
        "value",
        "values",
        "one",
        "two",
        "three",
    ]
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def humanize(api_name: str) -> str:
    """`PurchaseOrder` and `purchase_order` both give `purchase order`."""
    return _CAMEL.sub(" ", api_name).replace("_", " ").strip().lower()


def cue_tokens(attribute: Attribute) -> tuple[str, ...]:
    """Words that announce a value of the Attribute in text (from its names and description)."""
    words = [*humanize(attribute.api_name).split(), *attribute.display_name.lower().split()]
    words += [w.lower() for w in _WORD.findall(attribute.description)[:8]]
    return tuple(dict.fromkeys(w for w in words if len(w) >= 3 and w not in _STOPWORDS))


def key_regex(pattern: str) -> re.Pattern[str] | None:
    """A search pattern for keys from an anchored `pattern` constraint; None when unusable."""
    core = pattern.removeprefix("^").removesuffix("$")
    if not core or core in (".*", ".+"):
        return None
    try:
        return re.compile(rf"(?<![\w-])(?:{core})(?![\w-])")
    except re.error:
        return None


def extractable(attribute: Attribute, tags: Mapping[str, Tags] | None = None) -> bool:
    """Whether extraction may propose values of `attribute`; `tags` are the effective tags
    by Attribute id (the Attribute's own when not given)."""
    sensitive = attribute.sensitivity if tags is None else tags.get(attribute.id)
    return not attribute.is_derived and not sensitive and attribute.type.name in EXTRACTABLE_TYPES


@dataclass(frozen=True)
class KindView:
    """One Kind as the extractors see it."""

    kind: Kind
    label: str
    key_attribute: Attribute | None
    key_pattern: re.Pattern[str] | None
    title_attribute: Attribute | None
    attributes: tuple[Attribute, ...]
    cues: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.kind.id

    @property
    def api_name(self) -> str:
        return self.kind.api_name

    def describe(self) -> str:
        """Label description for span extractors: the Kind and what names its Records."""
        named = (
            f" Named by its {humanize(self.title_attribute.api_name)}."
            if self.title_attribute
            else ""
        )
        return f"{self.kind.description}{named}".strip()


@dataclass(frozen=True)
class Ontology:
    """The extraction view of a schema."""

    schema: Schema
    kinds: Mapping[str, KindView]
    relations: Mapping[str, Relation]
    new_record_kinds: frozenset[str]
    tags: Mapping[str, Tags]
    """Effective sensitivity tags by Attribute id (module doc)."""

    def kind_by_label(self, label: str) -> KindView | None:
        folded = label.casefold()
        for view in self.kinds.values():
            if folded in (view.label.casefold(), view.api_name.casefold()):
                return view
        return None

    def allows_new(self, kind_id: str) -> bool:
        return kind_id in self.new_record_kinds

    def relations_between(self, kind_ids: Iterable[str]) -> list[Relation]:
        present = set(kind_ids)
        return [
            r for r in self.relations.values() if r.from_kind in present and r.to_kind in present
        ]


def build_ontology(
    schema: Schema,
    new_record_kinds: Sequence[str] | None,
    reference: Mapping[str, Tags] | None = None,
) -> Ontology:
    """The extraction view of `schema`; `new_record_kinds` as in `scribe.extraction`;
    `reference` the tags of the default branch's schema by Attribute id, added to the
    schema's own (module doc)."""
    tags = effective_tags(schema, reference)
    kinds: dict[str, KindView] = {}
    for kind in schema.kinds.values():
        if kind.status is not ElementStatus.ACTIVE:
            continue
        key_attribute = None
        pattern = None
        if len(kind.primary_key) == 1:
            key_attribute = kind.attributes.get(kind.primary_key[0])
            if key_attribute is not None and key_attribute.constraints.pattern:
                pattern = key_regex(key_attribute.constraints.pattern)
        title = kind.attributes.get(kind.title_attribute or "")
        attributes = tuple(a for a in kind.attributes.values() if extractable(a, tags))
        kinds[kind.id] = KindView(
            kind=kind,
            label=kind.display_name or humanize(kind.api_name),
            key_attribute=key_attribute,
            key_pattern=pattern,
            title_attribute=title,
            attributes=attributes,
            cues={a.id: cue_tokens(a) for a in attributes},
        )
    relations = {
        r.id: r
        for r in schema.relations.values()
        if r.status is ElementStatus.ACTIVE and r.from_kind in kinds and r.to_kind in kinds
    }
    if new_record_kinds is None:
        allowed = {k for k, view in kinds.items() if view.kind.has_natural_key}
    else:
        names = set(new_record_kinds)
        allowed = {k for k, view in kinds.items() if view.api_name in names}
    return Ontology(schema, kinds, relations, frozenset(allowed), tags)


# Tier 2 JSON schema -------------------------------------------------------------------------


def value_schema(attribute: Attribute) -> dict[str, Any]:
    """JSON schema of one Attribute value as the model writes it."""
    spec = attribute.type
    name = spec.name
    schema: dict[str, Any]
    if name is AttributeType.ENUM:
        schema = {"enum": list(spec.enum_values or ())}
    elif name is AttributeType.INTEGER:
        schema = {"type": "integer"}
    elif name is AttributeType.FLOAT:
        schema = {"type": "number"}
    elif name is AttributeType.BOOLEAN:
        schema = {"type": "boolean"}
    elif name is AttributeType.DATE:
        schema = {"type": "string", "format": "date"}
    elif name is AttributeType.TIMESTAMP:
        schema = {"type": "string", "format": "date-time"}
    elif name is AttributeType.DECIMAL:
        schema = {"type": "string", "description": "decimal number, digits and one dot"}
    else:
        schema = {"type": "string"}
    if attribute.description:
        schema["description"] = attribute.description
    return schema


def _record_schema(view: KindView) -> dict[str, Any]:
    values = {
        a.api_name: {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "value": value_schema(a),
                "evidence": {"type": "string", "description": "Verbatim quote holding the value"},
            },
            "required": ["value", "evidence"],
        }
        for a in view.attributes
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"const": view.api_name},
            "ref": {
                "type": "string",
                "description": "Key of the candidate Record this is, or an empty string for "
                "a Record that is not among the candidates",
            },
            "evidence": {"type": "string", "description": "Verbatim quote naming the Record"},
            "values": {"type": "object", "additionalProperties": False, "properties": values},
        },
        "required": ["kind", "ref", "evidence", "values"],
    }


UNLISTED_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Facts the text states that the schema above has no place for",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {"enum": ["kind", "attribute", "relation"]},
            "name": {
                "type": "string",
                "description": "Name of the missing kind of thing, field or relationship",
            },
            "subject": {
                "type": "string",
                "description": "Key or name of the record the field or relationship belongs "
                "to; empty for a kind",
            },
            "value": {
                "type": "string",
                "description": "The field value, the key or name of the related record, or "
                "the name of the thing of the missing kind",
            },
            "evidence": {"type": "string", "description": "Verbatim quote of the fact"},
        },
        "required": ["type", "name", "subject", "value", "evidence"],
    },
}
"""Tier 2 `unlisted` answers: what the model saw outside the schema (schema discovery)."""


def link_attributes(relation: Relation, tags: Mapping[str, Tags] | None = None) -> list[Attribute]:
    """Link Attributes of `relation` that extraction can propose (`tags` as in
    `extractable`)."""
    return [a for a in relation.attributes.values() if extractable(a, tags)]


def _link_schema(
    relations: Sequence[Relation],
    tags: Mapping[str, Tags],
    with_values: Relation | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "relation": {"const": with_values.api_name}
        if with_values
        else {"enum": [r.api_name for r in relations]},
        "from": {"type": "string", "description": "Key or name of the from Record"},
        "to": {"type": "string", "description": "Key or name of the to Record"},
        "evidence": {"type": "string", "description": "Verbatim quote of the fact"},
    }
    required = ["relation", "from", "to", "evidence"]
    if with_values is not None:
        attributes = link_attributes(with_values, tags)
        properties["values"] = {
            "type": "object",
            "additionalProperties": False,
            "description": "Link Attributes stated by the text; give every required one",
            "properties": {
                a.api_name: {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "value": value_schema(a),
                        "evidence": {
                            "type": "string",
                            "description": "Verbatim quote holding the value",
                        },
                    },
                    "required": ["value", "evidence"],
                }
                for a in attributes
            },
            "required": [a.api_name for a in attributes if a.required],
        }
        required.append("values")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _links_schema(relations: Sequence[Relation], tags: Mapping[str, Tags]) -> dict[str, Any]:
    """Schema of one link answer: Relations with link Attributes get a branch of their own
    whose `values` lists those Attributes."""
    plain = [r for r in relations if not link_attributes(r, tags)]
    typed = [r for r in relations if link_attributes(r, tags)]
    if not typed:
        return _link_schema(plain, tags)
    branches = [_link_schema(plain, tags)] if plain else []
    branches.extend(_link_schema(relations, tags, r) for r in typed)
    return {"anyOf": branches}


def fragment_schema(
    ontology: Ontology, kind_ids: Sequence[str], *, unlisted: bool = False
) -> dict[str, Any]:
    """JSON schema of a tier 2 answer restricted to `kind_ids` and the Relations between them;
    with `unlisted`, the answer also lists facts outside the schema."""
    views = [ontology.kinds[k] for k in kind_ids if k in ontology.kinds]
    properties: dict[str, Any] = {
        "records": {"type": "array", "items": {"anyOf": [_record_schema(v) for v in views]}}
    }
    required = ["records"]
    relations = ontology.relations_between(kind_ids)
    if relations:
        properties["links"] = {"type": "array", "items": _links_schema(relations, ontology.tags)}
        required.append("links")
    if unlisted:
        properties["unlisted"] = UNLISTED_SCHEMA
        required.append("unlisted")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def fragment_description(ontology: Ontology, kind_ids: Sequence[str]) -> str:
    """The ontology fragment as prompt text: Kinds with Attributes, then Relations."""
    lines: list[str] = []
    for kind_id in kind_ids:
        view = ontology.kinds.get(kind_id)
        if view is None:
            continue
        key = f" (key: {view.key_attribute.api_name})" if view.key_attribute else ""
        lines.append(f"Kind {view.api_name}{key}: {view.kind.description}".rstrip())
        for attribute in view.attributes:
            lines.append(f"  - {attribute.api_name}: {attribute.description}".rstrip())
    for relation in ontology.relations_between(kind_ids):
        source = ontology.kinds[relation.from_kind].api_name
        target = ontology.kinds[relation.to_kind].api_name
        lines.append(
            f"Relation {relation.api_name}: {source} -> {target}. {relation.description}".rstrip()
        )
        for attribute in link_attributes(relation, ontology.tags):
            need = " (required: give it with evidence)" if attribute.required else ""
            lines.append(
                f"  - link attribute {attribute.api_name}{need}: {attribute.description}".rstrip()
            )
    return "\n".join(lines)
