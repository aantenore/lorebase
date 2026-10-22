"""Schema suggestions from document extraction (design 9.2 step 10).

Extraction sees types, fields and relations that the schema does not have: a field label no
Attribute matches, a heading naming a thing of no Kind, a span or relation label of a local
model outside the schema, a fact the chat model reports outside its JSON schema. Each sighting
is an ``Observation``; observations with the same ``fingerprint`` aggregate into one
suggestion (a new Kind, a new Attribute of a Kind, a new Relation) with a support count,
examples and sample values.

This module holds the pure parts:

- names: ``kind_name``, ``field_name`` and the ``fingerprint`` of an observation;
- deduplication against a schema: ``existing_match`` compares names after normalization and
  singularization, through synonym groups and by similarity (``SuggestionOptions``);
- the type of a new Attribute, inferred from sample values by the column analysis of schema
  inference (``infer_attribute``);
- ``suggestion_patch``: the schema change set patch (``kinds`` and ``relations`` sections of the
  schema document) that adds accepted suggestions to a schema. It is never applied here. New
  Kinds and Relations are drafts (as schema inference proposes them): a modeler describes and
  activates them; Attributes added to an existing Kind are active.

A Kind proposed by another suggestion is referred to as ``+<KindName>`` until it exists.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lorebase_core.expr.sensitivity import effective_tags
from lorebase_core.infer.columns import analyze_column, shape_pattern
from lorebase_core.infer.names import humanize, normalize, pascal, plural, singular, unique_name
from lorebase_core.infer.options import InferenceOptions
from lorebase_core.infer.profile import ColumnProfile
from lorebase_core.model.attribute import Attribute, Sensitivity
from lorebase_core.model.base import ElementStatus, snake_case
from lorebase_core.model.ids import IdPrefix, new_id
from lorebase_core.model.kind import Kind
from lorebase_core.model.relation import Cardinality, Relation
from lorebase_core.model.schema import Schema
from lorebase_core.model.yaml_schema import schema_to_document
from lorebase_core.types import AttributeType, Constraints, TypeSpec

PROPOSED = "+"
"""Prefix of a reference to a Kind that a suggestion proposes (``+QualityNotice``)."""


class SuggestionType(StrEnum):
    KIND = "kind"
    ATTRIBUTE = "attribute"
    RELATION = "relation"


def _type_inference() -> InferenceOptions:
    return InferenceOptions(min_support_values=5, propose_max_length=False, enum_min_values=5)


class SuggestionOptions(BaseModel):
    """Thresholds of schema suggestions, shared by the extraction worker and Keeper."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_support: int = Field(
        default=2,
        ge=1,
        description="Observations a suggestion needs before it is listed for review.",
    )
    max_suggestions: int = Field(
        default=50,
        ge=1,
        description="Pending suggestions listed at most, the best supported first.",
    )
    max_examples: int = Field(default=3, ge=0, description="Evidence examples kept per suggestion.")
    max_samples: int = Field(
        default=20, ge=0, description="Distinct sample values kept per suggestion."
    )
    max_labels: int = Field(
        default=5, ge=1, description="Distinct labels (as written) kept per suggestion."
    )
    max_tracked: int = Field(
        default=2000,
        ge=1,
        description="Suggestions kept per Domain; observations of new ones beyond are dropped.",
    )
    name_similarity: float = Field(
        default=0.85,
        gt=0,
        le=1,
        description="Least similarity (0 to 1, after normalization and singular) for a name "
        "to count as an existing element's.",
    )
    synonyms: tuple[tuple[str, ...], ...] = Field(
        default=(
            ("supplier", "vendor"),
            ("contract", "agreement"),
            ("part", "component", "item"),
            ("plant", "site", "facility"),
            ("purchase_order", "order"),
            ("customer", "client"),
            ("quantity", "qty"),
        ),
        description="Groups of names that mean the same element (snake_case, singular).",
    )
    merge_similarity: float = Field(
        default=0.6,
        gt=0,
        le=1,
        description="Least overlap (Jaccard index of the snake_case words, 0 to 1) for two "
        "Attribute suggestions of one Kind with compatible sample values to count as one "
        "(renewal_notice_days and renewal_proposal_notice_days). 1 merges only the same words.",
    )
    kind_stoplist: tuple[str, ...] = Field(
        default=(
            "section",
            "clause",
            "article",
            "paragraph",
            "chapter",
            "annex",
            "appendix",
            "schedule",
            "exhibit",
            "attachment",
            "page",
            "table",
            "figure",
            "document",
            "proposal",
        ),
        description="Generic document-structure and workflow words (snake_case, singular) "
        "that are never suggested as a Kind.",
    )
    kind_min_attributes: int = Field(
        default=2,
        ge=0,
        description="Attribute suggestions (or observations) a proposed Kind needs before it "
        "is listed for review; a Kind seen once without fields is noise more often than not. "
        "0 lists every Kind.",
    )
    kind_independent_instances: int = Field(
        default=2,
        ge=0,
        description="Distinct codes or names of things (samples) a Kind suggestion needs to "
        "count as an independent entity, which is then kept although an Attribute has its name "
        "(a Kind `Buyer` with the codes B-1 and B-2). 0 turns this criterion off.",
    )
    kind_independent_attributes: int = Field(
        default=2,
        ge=0,
        description="Distinct Attribute suggestions owned by a proposed Kind that make it an "
        "independent entity (see `kind_independent_instances`). 0 turns this criterion off.",
    )
    type_inference: InferenceOptions = Field(
        default_factory=_type_inference,
        description="Options of the column analysis that types new Attributes from samples.",
    )


# Names ------------------------------------------------------------------------------------------


def kind_name(label: str) -> str:
    """``quality notices`` -> ``QualityNotice``."""
    return pascal(singular(normalize(label)))


def field_name(label: str) -> str:
    """``Received at`` -> ``received_at``."""
    return normalize(label)


def proposed_kind(label: str) -> str:
    """Reference to the Kind a ``kind`` suggestion of ``label`` proposes."""
    return f"{PROPOSED}{kind_name(label)}"


def _canonical(name: str) -> str:
    return singular(normalize(name))


@dataclass(frozen=True, slots=True)
class Observation:
    """One sighting of something the schema does not have.

    ``kind`` is the owner Kind of an Attribute or the from Kind of a Relation, ``target`` the
    to Kind of a Relation: a Kind id, or ``+<KindName>`` for a proposed Kind. ``value`` is a
    sample value (an Attribute value, the key or name of a thing of a new Kind).
    """

    type: SuggestionType
    label: str
    document_id: str
    confidence: float
    source: str
    kind: str | None = None
    target: str | None = None
    value: str | None = None
    evidence: Mapping[str, Any] | None = None

    @property
    def name(self) -> str:
        return kind_name(self.label) if self.type is SuggestionType.KIND else field_name(self.label)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.type, self.name, self.kind, self.target)


def fingerprint(
    type_: SuggestionType, name: str, kind: str | None = None, target: str | None = None
) -> str:
    """Identity of a suggestion: observations with the same fingerprint aggregate."""
    if type_ is SuggestionType.KIND:
        return f"kind:{_canonical(name)}"
    if type_ is SuggestionType.ATTRIBUTE:
        return f"attribute:{kind}:{_canonical(name)}"
    return f"relation:{kind}:{_canonical(name)}:{target}"


# Deduplication ----------------------------------------------------------------------------------


def name_similarity(first: str, second: str, options: SuggestionOptions) -> float:
    """1 for equal names (after normalization and singular) or synonyms, else the similarity
    ratio of the normalized names."""
    a, b = _canonical(first), _canonical(second)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    for group in options.synonyms:
        names = {_canonical(n) for n in group}
        if a in names and b in names:
            return 1.0
    return SequenceMatcher(None, a, b).ratio()


_WORD = re.compile(r"[a-z0-9]+")
_INTEGER = re.compile(r"[+-]?[0-9]+")
_NUMBER = re.compile(r"[+-]?[0-9]+(?:[.,][0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ].*)?")
_BOOLEAN = frozenset({"true", "false", "yes", "no"})


def name_words(name: str) -> frozenset[str]:
    """The singular words of a name: ``renewal_notice_days`` -> ``renewal``, ``notice``, ``day``."""
    return frozenset(singular(w) for w in _WORD.findall(normalize(name)))


def word_overlap(first: str, second: str) -> float:
    """Jaccard index of the words of two names (0 when either has none)."""
    a, b = name_words(first), name_words(second)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def value_classes(samples: Iterable[str]) -> frozenset[str]:
    """Coarse value types of sample texts: ``number``, ``date``, ``boolean`` or ``text``."""
    found: set[str] = set()
    for sample in samples:
        text = sample.strip()
        if _NUMBER.fullmatch(text) or _INTEGER.fullmatch(text):
            found.add("number")
        elif _DATE.fullmatch(text):
            found.add("date")
        elif text.casefold() in _BOOLEAN:
            found.add("boolean")
        elif text:
            found.add("text")
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class AttributeSighting:
    """An Attribute suggestion (stored or in a job) that may be a variant of another."""

    key: str
    """Identity of the suggestion (its fingerprint)."""
    name: str
    kind: str | None
    samples: tuple[str, ...]
    support: int


def merge_attributes(
    sightings: Sequence[AttributeSighting], options: SuggestionOptions
) -> dict[str, str]:
    """Attribute suggestions that are variants of one another, as ``key -> kept key``.

    Two suggestions are variants when they belong to the same Kind, their names share at least
    ``merge_similarity`` of their words and their samples have a value type in common (both
    need samples). The best supported suggestion is kept, then the shorter name, then the
    alphabetically first, so the result does not depend on the order of ``sightings``. Every
    key is in the result; a kept key maps to itself.
    """
    ordered = sorted(sightings, key=lambda s: (-s.support, len(s.name), s.name, s.key))
    kept: list[tuple[AttributeSighting, frozenset[str]]] = []
    merged: dict[str, str] = {}
    for sighting in ordered:
        classes = value_classes(sighting.samples)
        best: tuple[float, AttributeSighting] | None = None
        for other, other_classes in kept:
            if other.kind != sighting.kind or not (classes & other_classes):
                continue
            score = word_overlap(sighting.name, other.name)
            if score >= options.merge_similarity and (best is None or score > best[0]):
                best = (score, other)
        if best is None:
            kept.append((sighting, classes))
            merged[sighting.key] = sighting.key
        else:
            merged[sighting.key] = best[1].key
    return merged


def kind_is_independent(instances: int, fields: int, options: SuggestionOptions) -> bool:
    """Whether the evidence of a Kind suggestion shows an entity of its own: ``instances``
    distinct codes or names seen for it, or ``fields`` Attribute suggestions it owns, reach
    ``kind_independent_instances`` or ``kind_independent_attributes`` (0 turns one off)."""
    by_instances = options.kind_independent_instances > 0 and (
        instances >= options.kind_independent_instances
    )
    by_fields = options.kind_independent_attributes > 0 and (
        fields >= options.kind_independent_attributes
    )
    return by_instances or by_fields


def kind_is_noise(
    name: str,
    attribute_names: Collection[str],
    options: SuggestionOptions,
    *,
    independent: bool = False,
) -> bool:
    """Whether a Kind suggestion is a generic word (``kind_stoplist``) or only names a field
    (an Attribute called ``attribute_names``: a Kind ``Buyer`` beside the Attribute ``buyer``).
    An ``independent`` Kind (``kind_is_independent``) is not dropped for naming a field."""
    canonical = _canonical(name)
    if canonical in {_canonical(w) for w in options.kind_stoplist}:
        return True
    if independent:
        return False
    return canonical in {_canonical(n) for n in attribute_names}


def kind_is_filtered(
    name: str,
    attribute_names: Collection[str],
    options: SuggestionOptions,
    *,
    instances: int,
    fields: int,
) -> bool:
    """The one rule that drops a Kind suggestion as noise, for the extraction job and the
    store alike: ``kind_is_noise`` with ``independent`` taken from the ``instances`` and
    ``fields`` the evidence shows (``kind_is_independent``)."""
    return kind_is_noise(
        name,
        attribute_names,
        options,
        independent=kind_is_independent(instances, fields, options),
    )


def dangling_reference_names(schema: Schema) -> list[str]:
    """Api names of the Attributes that hold a reference as free text and point to nothing:
    ``string`` or ``text`` Attributes that no Relation of their Kind is named like. A Kind
    suggestion named like one of these only restates the field; a ``ref`` Attribute, a typed
    Attribute (date, number...) or a field a Relation models is not one."""
    names: list[str] = []
    for kind in schema.kinds.values():
        linked = {
            _canonical(n)
            for relation in schema.relations_of(kind.id)
            for n in (relation.api_name, relation.inverse_api_name)
        }
        names.extend(
            a.api_name
            for a in kind.attributes.values()
            if a.type.name in (AttributeType.STRING, AttributeType.TEXT)
            and _canonical(a.api_name) not in linked
        )
    return names


@dataclass(frozen=True, slots=True)
class ExistingMatch:
    """The schema element a suggestion duplicates."""

    section: str
    """``kinds``, ``attributes`` or ``relations``."""
    id: str
    api_name: str
    score: float
    tags: frozenset[Sensitivity] = frozenset()

    @property
    def sensitive(self) -> bool:
        return bool(self.tags)


def resolve_kind(schema: Schema, ref: str | None, options: SuggestionOptions) -> Kind | None:
    """The Kind a reference names: a Kind id, or a proposed Kind that exists by now."""
    if not ref:
        return None
    if not ref.startswith(PROPOSED):
        return schema.kinds.get(ref)
    match = _best_kind(schema, ref[len(PROPOSED) :], options)
    return None if match is None else schema.kinds.get(match.id)


def _names_of(element: Kind | Relation | Attribute) -> list[str]:
    names = [element.api_name]
    if element.display_name:
        names.append(element.display_name)
    if isinstance(element, Relation):
        names.append(element.inverse_api_name)
    return names


def _best(
    name: str,
    candidates: Iterable[tuple[str, Kind | Relation | Attribute]],
    options: SuggestionOptions,
    tags: Mapping[str, frozenset[Sensitivity]],
) -> ExistingMatch | None:
    best: ExistingMatch | None = None
    for section, element in candidates:
        score = max(name_similarity(name, n, options) for n in _names_of(element))
        if score >= options.name_similarity and (best is None or score > best.score):
            best = ExistingMatch(
                section, element.id, element.api_name, score, tags.get(element.id, frozenset())
            )
    return best


def _best_kind(schema: Schema, name: str, options: SuggestionOptions) -> ExistingMatch | None:
    return _best(name, (("kinds", k) for k in schema.kinds.values()), options, {})


def existing_match(
    schema: Schema,
    type_: SuggestionType,
    name: str,
    *,
    kind: str | None = None,
    target: str | None = None,
    options: SuggestionOptions,
    tags: Mapping[str, frozenset[Sensitivity]] | None = None,
) -> ExistingMatch | None:
    """The element of ``schema`` a suggestion duplicates, or None.

    A Kind matches a Kind by name. An Attribute matches an Attribute of its Kind (also
    ``<kind>_<name>``, as ``code`` for ``supplier_code``), a Relation of its Kind, or a Kind
    (a field naming another Kind refers to it). A Relation matches a Relation between its
    Kinds, in either direction, by name or by the name of its far Kind. ``tags`` are the
    sensitivity tags by Attribute id (``effective_tags`` of the schema by default).
    """
    tags = effective_tags(schema) if tags is None else tags
    if type_ is SuggestionType.KIND:
        return _best_kind(schema, name, options)
    owner = resolve_kind(schema, kind, options)
    if type_ is SuggestionType.ATTRIBUTE:
        if owner is None:
            return None
        prefixed = f"{snake_case(owner.api_name)}_{_canonical(name)}"
        for attribute in owner.attributes.values():
            if _canonical(attribute.api_name) == prefixed:
                return ExistingMatch(
                    "attributes",
                    attribute.id,
                    attribute.api_name,
                    1.0,
                    tags.get(attribute.id, frozenset()),
                )
        candidates: list[tuple[str, Kind | Relation | Attribute]] = [
            ("attributes", a) for a in owner.attributes.values()
        ]
        candidates += [("relations", r) for r in schema.relations_of(owner.id)]
        candidates += [("kinds", k) for k in schema.kinds.values()]
        return _best(name, candidates, options, tags)
    far = resolve_kind(schema, target, options)
    if owner is None or far is None:
        return None
    between = [
        r for r in schema.relations.values() if {r.from_kind, r.to_kind} == {owner.id, far.id}
    ]
    found = _best(name, (("relations", r) for r in between), options, tags)
    if found is not None or not between:
        return found
    if name_similarity(name, far.api_name, options) >= options.name_similarity:
        return ExistingMatch("relations", between[0].id, between[0].api_name, 1.0)
    return None


def sensitive_attributes(schema: Schema) -> dict[str, frozenset[Sensitivity]]:
    """Attribute id -> sensitivity tags, carried through Derivations, of tagged Attributes."""
    return {k: v for k, v in effective_tags(schema).items() if v}


# Types ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttributeGuess:
    """The type a new Attribute gets from its sample values."""

    type: TypeSpec
    constraints: Constraints
    confidence: float
    reasons: tuple[str, ...]


def infer_attribute(
    name: str, samples: Sequence[str], options: SuggestionOptions
) -> AttributeGuess:
    """Type and constraints of an Attribute holding ``samples`` (schema inference rules)."""
    analysis = analyze_column(ColumnProfile(name, tuple(samples)), 0, options.type_inference)
    return AttributeGuess(
        analysis.type, analysis.constraints, analysis.confidence, tuple(analysis.reasons)
    )


# Change set -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suggestion:
    """What a schema change needs from an aggregated suggestion."""

    id: str
    type: SuggestionType
    name: str
    kind: str | None = None
    target: str | None = None
    samples: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


class SuggestionError(ValueError):
    """A suggestion cannot be added to the schema (unknown Kind, name taken)."""

    def __init__(self, suggestion: str, message: str) -> None:
        super().__init__(message)
        self.suggestion = suggestion


@dataclass
class _Draft:
    schema: Schema
    options: SuggestionOptions
    kinds: dict[str, Kind] = field(default_factory=dict)
    relations: dict[str, Relation] = field(default_factory=dict)
    touched_kinds: set[str] = field(default_factory=set)

    def kind(self, ref: str | None, suggestion: Suggestion) -> Kind:
        if ref and ref.startswith(PROPOSED):
            proposed = self.kinds.get(ref[len(PROPOSED) :])
            if proposed is not None:
                return proposed
        found = resolve_kind(self.schema, ref, self.options)
        if found is None:
            raise SuggestionError(
                suggestion.id,
                f"Kind {(ref or '').removeprefix(PROPOSED)!r} does not exist; accept its Kind "
                "suggestion with this one",
            )
        return self.kinds.get(found.api_name, found)


def _description(suggestion: Suggestion) -> str:
    labels = ", ".join(f'"{label}"' for label in suggestion.labels[:3])
    found = f" ({labels})" if labels else ""
    return f"Suggested by document extraction{found}."


def _new_kind(suggestion: Suggestion, options: SuggestionOptions) -> Kind:
    snake = snake_case(suggestion.name)
    kind_id = new_id(IdPrefix.KIND)
    samples = [s for s in suggestion.samples if s]
    pattern = shape_pattern(samples) if samples else None
    if pattern is not None:
        key = Attribute(
            id=new_id(IdPrefix.ATTRIBUTE),
            api_name=f"{snake}_code",
            display_name=humanize(f"{snake}_code"),
            description=f"Code of the {humanize(snake).lower()}, for example {samples[0]}.",
            type=TypeSpec.of(AttributeType.STRING),
            required=True,
            constraints=Constraints(pattern=pattern),
        )
        return Kind(
            id=kind_id,
            api_name=suggestion.name,
            display_name=humanize(snake),
            description=_description(suggestion),
            primary_key=(key.id,),
            attributes={key.id: key},
            status=ElementStatus.DRAFT,
        )
    title = Attribute(
        id=new_id(IdPrefix.ATTRIBUTE),
        api_name="name",
        display_name="Name",
        description=f"Name of the {humanize(snake).lower()}.",
        type=TypeSpec.of(AttributeType.STRING),
        required=True,
    )
    return Kind(
        id=kind_id,
        api_name=suggestion.name,
        display_name=humanize(snake),
        description=_description(suggestion),
        title_attribute=title.id,
        attributes={title.id: title},
        status=ElementStatus.DRAFT,
    )


def _relation_names(schema: Schema, extra: Iterable[Relation]) -> set[str]:
    names: set[str] = set()
    for relation in [*schema.relations.values(), *extra]:
        names.update((relation.api_name, relation.inverse_api_name))
    return names


def suggestion_patch(
    schema: Schema, suggestions: Sequence[Suggestion], options: SuggestionOptions
) -> dict[str, Any]:
    """A schema change set patch adding ``suggestions`` to ``schema``: the ``kinds`` and
    ``relations`` sections holding the new and the changed elements (whole, as the patch
    replaces elements by api name). Raises ``SuggestionError``."""
    draft = _Draft(schema, options)
    ordered = sorted(
        suggestions,
        key=lambda s: [
            SuggestionType.KIND,
            SuggestionType.ATTRIBUTE,
            SuggestionType.RELATION,
        ].index(s.type),
    )
    for suggestion in ordered:
        if suggestion.type is SuggestionType.KIND:
            if schema.kind_by_api_name(suggestion.name) or suggestion.name in draft.kinds:
                raise SuggestionError(suggestion.id, f"Kind {suggestion.name!r} exists")
            kind = _new_kind(suggestion, options)
            draft.kinds[kind.api_name] = kind
            draft.touched_kinds.add(kind.api_name)
        elif suggestion.type is SuggestionType.ATTRIBUTE:
            owner = draft.kind(suggestion.kind, suggestion)
            taken = {a.api_name for a in owner.attributes.values()}
            name = unique_name(suggestion.name, taken)
            guess = infer_attribute(name, suggestion.samples, options)
            attribute = Attribute(
                id=new_id(IdPrefix.ATTRIBUTE),
                api_name=name,
                display_name=humanize(name),
                description=_description(suggestion),
                type=guess.type,
                constraints=guess.constraints,
            )
            draft.kinds[owner.api_name] = owner.model_copy(
                update={"attributes": {**owner.attributes, attribute.id: attribute}}
            )
            draft.touched_kinds.add(owner.api_name)
        else:
            source = draft.kind(suggestion.kind, suggestion)
            target = draft.kind(suggestion.target, suggestion)
            taken = _relation_names(schema, draft.relations.values())
            name = unique_name(suggestion.name, taken)
            inverse = unique_name(plural(snake_case(source.api_name)), taken, (f"{name}_of",))
            relation = Relation(
                id=new_id(IdPrefix.RELATION),
                api_name=name,
                inverse_api_name=inverse,
                display_name=humanize(name),
                description=_description(suggestion),
                from_kind=source.id,
                to_kind=target.id,
                cardinality=Cardinality.MANY_TO_MANY,
                status=ElementStatus.DRAFT,
            )
            draft.relations[name] = relation
    kinds = {**schema.kinds, **{k.id: k for k in draft.kinds.values()}}
    relations = {**schema.relations, **{r.id: r for r in draft.relations.values()}}
    target_schema = schema.model_copy(update={"kinds": kinds, "relations": relations})
    document = schema_to_document(target_schema)
    patch: dict[str, Any] = {}
    if draft.touched_kinds:
        patch["kinds"] = {n: document["kinds"][n] for n in sorted(draft.touched_kinds)}
    if draft.relations:
        patch["relations"] = {n: document["relations"][n] for n in sorted(draft.relations)}
    return patch


__all__ = [
    "PROPOSED",
    "AttributeGuess",
    "AttributeSighting",
    "ExistingMatch",
    "Observation",
    "Suggestion",
    "SuggestionError",
    "SuggestionOptions",
    "SuggestionType",
    "dangling_reference_names",
    "existing_match",
    "field_name",
    "fingerprint",
    "infer_attribute",
    "kind_is_filtered",
    "kind_is_independent",
    "kind_is_noise",
    "kind_name",
    "merge_attributes",
    "name_similarity",
    "proposed_kind",
    "resolve_kind",
    "sensitive_attributes",
    "suggestion_patch",
    "value_classes",
    "word_overlap",
]
