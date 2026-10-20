"""``infer_schema``: table profiles in, a reviewed-to-be ``SchemaProposal`` out.

Steps: analyze columns and keys of every table (``tables``), find references and junction
tables (``references``), then build Kinds, Attributes and Relations with deterministic ids and
api names, record the evidence of each element and validate the result statically.

Tables are processed in normalized name order and columns in source order, and ids are derived
from a hash of the seed, the Domain and the source names: the same input always gives the same
proposal, whatever the order the tables come in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from pydantic import JsonValue

from lorebase_core.infer.columns import TEXT_FAMILY, ColumnAnalysis
from lorebase_core.infer.names import (
    humanize,
    normalize,
    pascal,
    plural,
    rank,
    strip_suffix,
    unique_name,
    unique_pascal,
)
from lorebase_core.infer.options import InferenceOptions
from lorebase_core.infer.profile import TableProfile
from lorebase_core.infer.proposal import (
    Evidence,
    InferenceIssue,
    InferenceIssueCode,
    SchemaProposal,
    TableOutcome,
    TableRole,
)
from lorebase_core.infer.references import Junction, Reference, find_junctions, find_references
from lorebase_core.infer.tables import TableAnalysis, analyze_table
from lorebase_core.model.attribute import Attribute, SearchWeight
from lorebase_core.model.base import snake_case
from lorebase_core.model.domain import Domain
from lorebase_core.model.ids import IdPrefix, new_id
from lorebase_core.model.kind import Kind
from lorebase_core.model.relation import Cardinality, Relation, RelationBacking
from lorebase_core.model.schema import Schema
from lorebase_core.model.yaml_schema import locate_issues
from lorebase_core.types import AttributeType, Constraints, TypeSpec
from lorebase_core.validate.issues import Severity
from lorebase_core.validate.schema_rules import SchemaRules, validate_schema

#: Confidence of a Kind without any key candidate.
KEYLESS_KIND_CONFIDENCE = 0.5


def infer_schema(
    tables: Iterable[TableProfile],
    domain: Domain | str,
    options: InferenceOptions | None = None,
    rules: SchemaRules | None = None,
) -> SchemaProposal:
    """Propose a schema for ``tables``. Pure: no I/O, no model, nothing committed.

    The proposal is validated with ``rules`` (the defaults when unset); their reserved
    Attribute names are replaced by those of ``options``.
    """
    options = options or InferenceOptions()
    domain = Domain(id=domain) if isinstance(domain, str) else domain
    profiles = sorted(tables, key=lambda t: (normalize(t.name), t.name))
    analyses = [analyze_table(profile, options) for profile in profiles]
    issues: list[InferenceIssue] = []
    references = find_references(analyses, options, issues)
    junctions = find_junctions(analyses, references, options)
    return _Builder(domain, options, analyses, references, junctions, issues, rules).build()


@dataclass
class _KindSlot:
    table: TableAnalysis
    kind_id: str
    api_name: str
    snake: str
    attributes: dict[int, Attribute] = field(default_factory=dict)


class _Builder:
    def __init__(
        self,
        domain: Domain,
        options: InferenceOptions,
        tables: list[TableAnalysis],
        references: list[Reference],
        junctions: dict[int, Junction],
        issues: list[InferenceIssue],
        rules: SchemaRules | None = None,
    ) -> None:
        self.rules = rules or SchemaRules()
        self.domain = domain
        self.options = options
        self.tables = tables
        self.references = references
        self.junctions = junctions
        self.issues = issues
        self.evidence: list[Evidence] = []
        self.outcomes: list[TableOutcome] = []
        self.slots: dict[int, _KindSlot] = {}
        self.kinds: dict[str, Kind] = {}
        self.relations: dict[str, Relation] = {}
        self.relation_names: set[str] = set()
        self.refs_by_column = {
            (r.table, r.column): r for r in references if r.table not in junctions
        }

    # Ids -------------------------------------------------------------------------------------

    def mint(self, prefix: IdPrefix, *parts: str) -> str:
        seed = "\x1f".join((self.options.id_seed, self.domain.id, prefix.value, *parts))
        randomness = hashlib.sha256(seed.encode("utf-8")).digest()[:10]
        return new_id(prefix, timestamp_ms=self.options.id_timestamp_ms, randomness=randomness)

    def note(
        self,
        code: InferenceIssueCode,
        message: str,
        *,
        element: str | None = None,
        source: str | None = None,
        severity: Severity = Severity.WARNING,
        **details: JsonValue,
    ) -> None:
        self.issues.append(
            InferenceIssue(
                code=code,
                message=message,
                severity=severity,
                element=element,
                source=source,
                details=details,
            )
        )

    # Build -----------------------------------------------------------------------------------

    def build(self) -> SchemaProposal:
        taken_kinds: set[str] = set()
        for index, table in enumerate(self.tables):
            if index in self.junctions:
                continue
            base = pascal(table.singular)
            api_name = unique_pascal(base, taken_kinds)
            if api_name != base:
                self.note(
                    InferenceIssueCode.NAME_ADJUSTED,
                    f"table {table.name!r} would be Kind {base}, already taken: named {api_name}",
                    element=f"kinds.{api_name}",
                    source=table.name,
                )
            self.slots[index] = _KindSlot(
                table=table,
                kind_id=self.mint(IdPrefix.KIND, table.name),
                api_name=api_name,
                snake=snake_case(api_name),
            )
        for index, slot in self.slots.items():
            self.kinds[slot.kind_id] = self.build_kind(index, slot)
        for index, table in enumerate(self.tables):
            if index in self.junctions:
                self.build_junction(self.junctions[index])
            elif index in self.slots:
                for column in table.columns:
                    reference = self.refs_by_column.get((index, column.index))
                    if reference is not None:
                        self.build_foreign_key(self.slots[index], column, reference)

        schema = Schema(domain=self.domain, kinds=self.kinds, relations=self.relations)
        rules = replace(
            self.rules,
            reserved_attribute_names=frozenset(self.options.reserved_attribute_names),
        )
        for issue in locate_issues(schema, validate_schema(schema, rules)):
            self.note(
                InferenceIssueCode.SCHEMA_INVALID,
                issue.message,
                element=".".join(str(p) for p in issue.path),
                severity=issue.severity,
                validation_code=issue.code.value,
            )
        return SchemaProposal(
            schema=schema,
            evidence=tuple(self.evidence),
            issues=tuple(self.issues),
            tables=tuple(sorted(self.outcomes, key=lambda o: o.table)),
        )

    # Kinds -----------------------------------------------------------------------------------

    def attribute_name(self, slot: _KindSlot, column: ColumnAnalysis, taken: set[str]) -> str:
        name = column.snake
        if name in self.options.reserved_attribute_names:
            name = f"{slot.snake}_{name}"
        result = unique_name(name, taken)
        if result != column.snake:
            self.note(
                InferenceIssueCode.NAME_ADJUSTED,
                f"column {column.name!r} is named {result!r} (reserved or duplicate name)",
                element=f"kinds.{slot.api_name}.attributes.{result}",
                source=f"{slot.table.name}.{column.name}",
            )
        return result

    def build_kind(self, index: int, slot: _KindSlot) -> Kind:
        table = slot.table
        options = self.options
        key = table.key
        element = f"kinds.{slot.api_name}"
        minted_column = key.single if key is not None and key.minted else None
        taken: set[str] = set()
        key_columns = set(key.columns) if key is not None else set()
        for column in table.columns:
            if column.index == minted_column:
                continue
            reference = self.refs_by_column.get((index, column.index))
            name = self.attribute_name(slot, column, taken)
            attribute_id = self.mint(IdPrefix.ATTRIBUTE, table.name, column.name)
            spec = (
                TypeSpec.ref(self.slots[reference.target].kind_id)
                if reference is not None
                else column.type
            )
            in_key = column.index in key_columns
            unique = (
                not in_key
                and reference is None
                and column.unique
                and column.type.name in (AttributeType.STRING, AttributeType.INTEGER)
                and (
                    column.declared_unique
                    or rank(column.snake, options.unique_name_patterns) is not None
                )
            )
            slot.attributes[column.index] = Attribute(
                id=attribute_id,
                api_name=name,
                display_name=humanize(name),
                description=column.comment or "",
                type=spec,
                required=column.required or in_key,
                unique=unique,
                constraints=Constraints() if reference is not None else column.constraints,
            )
            self.column_evidence(
                f"{element}.attributes.{name}", attribute_id, table, column, reference, unique
            )

        title = self.title(slot, key_columns)
        attributes = {}
        for column_index, attribute in slot.attributes.items():
            if options.propose_search and attribute.type.name in TEXT_FAMILY - {AttributeType.ENUM}:
                if attribute.id == title:
                    attribute = attribute.model_copy(
                        update={"searchable": True, "search_weight": SearchWeight.A}
                    )
                elif attribute.type.name is AttributeType.TEXT:
                    attribute = attribute.model_copy(
                        update={"searchable": True, "search_weight": SearchWeight.B}
                    )
                slot.attributes[column_index] = attribute
            attributes[attribute.id] = attribute

        primary_key = (
            () if key is None or key.minted else tuple(slot.attributes[c].id for c in key.columns)
        )
        kind = Kind(
            id=slot.kind_id,
            api_name=slot.api_name,
            display_name=humanize(slot.snake),
            description=table.comment or "",
            primary_key=primary_key,
            title_attribute=title,
            attributes=attributes,
            status=options.element_status,
        )
        self.kind_evidence(element, slot, title)
        self.outcomes.append(
            TableOutcome(table.name, TableRole.KIND, element, table.rows, table.values_read)
        )
        return kind

    def title(self, slot: _KindSlot, key_columns: set[int]) -> str | None:
        columns = slot.table.columns
        textual = [
            (index, attribute)
            for index, attribute in slot.attributes.items()
            if attribute.type.name in (AttributeType.STRING, AttributeType.TEXT)
            and not columns[index].generated_ids
        ]
        ranked = sorted(
            (rank(a.api_name, self.options.title_names), i, a)
            for i, a in textual
            if rank(a.api_name, self.options.title_names) is not None
        )
        if ranked:
            return ranked[0][2].id
        if len(key_columns) == 1:
            (only,) = key_columns
            attribute = slot.attributes.get(only)
            if attribute is not None and any(i == only for i, _ in textual):
                return attribute.id
        return textual[0][1].id if textual else None

    def kind_evidence(self, element: str, slot: _KindSlot, title: str | None) -> None:
        table = slot.table
        key = table.key
        reasons = [f"table {table.name!r}: {table.rows} rows, {table.values_read} read"]
        samples: tuple[str, ...] = ()
        if key is None:
            confidence = KEYLESS_KIND_CONFIDENCE * (1.0 if table.values_read else 0.5)
            reasons.append("no key candidate: Records get minted keys")
            self.note(
                InferenceIssueCode.NO_PRIMARY_KEY,
                f"no unique, never-null column (or key-named pair) in {table.name!r}: "
                "Records get minted keys",
                element=element,
                source=table.name,
            )
        else:
            confidence = key.confidence
            reasons.extend(key.reasons)
            samples = table.columns[key.columns[0]].samples
            if len(key.candidates) > 1:
                names = [table.columns[c].name for c in key.candidates]
                self.note(
                    InferenceIssueCode.AMBIGUOUS_PRIMARY_KEY,
                    f"{len(names)} key candidates in {table.name!r}: {', '.join(names)}; "
                    f"{names[0]!r} was chosen",
                    element=element,
                    source=table.name,
                    candidates=list[JsonValue](names),
                )
        if title is not None:
            name = next(a.api_name for a in slot.attributes.values() if a.id == title)
            reasons.append(f"title attribute: {name}")
        if table.values_read == 0:
            self.note(
                InferenceIssueCode.EMPTY_TABLE,
                f"table {table.name!r} has no rows: every column defaults to optional string",
                element=element,
                source=table.name,
            )
        if table.sampled:
            self.note(
                InferenceIssueCode.SAMPLED,
                f"{table.values_read} of {table.rows} rows of {table.name!r} were read: "
                "uniqueness and references hold for the sample only",
                element=element,
                source=table.name,
            )
        self.evidence.append(
            Evidence(
                element, slot.kind_id, table.name, round(confidence, 4), tuple(reasons), samples
            )
        )

    def column_evidence(
        self,
        element: str,
        element_id: str,
        table: TableAnalysis,
        column: ColumnAnalysis,
        reference: Reference | None,
        unique: bool,
    ) -> None:
        source = f"{table.name}.{column.name}"
        reasons = list(column.reasons)
        confidence = column.confidence
        if reference is not None:
            target = self.slots[reference.target]
            reasons.append(f"references {target.api_name}: type ref({target.api_name})")
            reasons.extend(reference.reasons)
            confidence = min(confidence, reference.confidence)
        if unique:
            reasons.append("named like an identifier: unique")
        if column.mixed is not None:
            mixed = column.mixed
            self.note(
                InferenceIssueCode.MIXED_TYPES,
                f"{mixed.share:.0%} of {source} values are {mixed.dominant}; "
                + (
                    "accepted within type_tolerance"
                    if mixed.accepted
                    else f"kept as {column.type.describe()}"
                ),
                element=element,
                source=source,
                dominant=mixed.dominant.value,
                outliers=list[JsonValue](mixed.outliers),
            )
        if column.count and not column.non_null:
            self.note(
                InferenceIssueCode.EMPTY_COLUMN,
                f"{source} has only nulls: optional string by default",
                element=element,
                source=source,
            )
        self.evidence.append(
            Evidence(
                element, element_id, source, round(confidence, 4), tuple(reasons), column.samples
            )
        )

    # Relations -------------------------------------------------------------------------------

    def inverse_name(self, from_snake: str, name: str, self_reference: bool) -> str:
        if self_reference:
            return unique_name(f"{name}_of", self.relation_names)
        many = plural(from_snake)
        return unique_name(many, self.relation_names, (f"{many}_via_{name}",))

    def relation_name(self, base: str, from_snake: str, source: str) -> str:
        name = unique_name(base, self.relation_names, (f"{from_snake}_{base}",))
        if name != base:
            self.note(
                InferenceIssueCode.NAME_ADJUSTED,
                f"Relation name {base!r} is taken: named {name!r}",
                element=f"relations.{name}",
                source=source,
            )
        return name

    def build_foreign_key(
        self, slot: _KindSlot, column: ColumnAnalysis, reference: Reference
    ) -> None:
        table = slot.table
        attribute = slot.attributes[column.index]
        target = self.slots[reference.target]
        source = f"{table.name}.{column.name}"
        base = strip_suffix(column.snake, self.options.foreign_key_suffixes) or column.snake
        name = self.relation_name(base, slot.snake, source)
        self_reference = target.kind_id == slot.kind_id
        inverse = self.inverse_name(slot.snake, name, self_reference)
        own_key = table.key is not None and table.key.single == column.index
        one = (
            own_key
            or column.declared_unique
            or (not self.options.one_to_one_needs_declared_unique and column.unique)
        )
        cardinality = Cardinality.ONE_TO_ONE if one else Cardinality.MANY_TO_ONE
        relation_id = self.mint(IdPrefix.RELATION, table.name, column.name)
        self.relations[relation_id] = Relation(
            id=relation_id,
            api_name=name,
            inverse_api_name=inverse,
            display_name=humanize(name),
            description=column.comment or "",
            from_kind=slot.kind_id,
            to_kind=target.kind_id,
            cardinality=cardinality,
            required_from=attribute.required,
            backing=RelationBacking.FOREIGN_KEY,
            foreign_key_attribute=attribute.id,
            status=self.options.element_status,
        )
        reasons = (
            f"{slot.api_name}.{attribute.api_name} references {target.api_name}",
            *reference.reasons,
            f"{cardinality.value}: "
            + ("the reference is the key of its table" if one else "one target per row"),
            *(("self-reference",) if self_reference else ()),
        )
        self.evidence.append(
            Evidence(
                f"relations.{name}",
                relation_id,
                source,
                reference.confidence,
                reasons,
                column.samples,
            )
        )

    def build_junction(self, junction: Junction) -> None:
        table = self.tables[junction.table]
        source_slot = self.slots[junction.source.target]
        target_slot = self.slots[junction.destination.target]
        name = self.relation_name(table.snake, source_slot.snake, table.name)
        self_reference = source_slot.kind_id == target_slot.kind_id
        inverse = self.inverse_name(source_slot.snake, name, self_reference)
        relation_id = self.mint(IdPrefix.RELATION, table.name)
        element = f"relations.{name}"
        link_columns = {junction.source.column, junction.destination.column}
        taken: set[str] = set()
        attributes: dict[str, Attribute] = {}
        for column in table.columns:
            if column.index in link_columns:
                continue
            attribute_name = unique_name(column.snake, taken)
            attribute_id = self.mint(IdPrefix.ATTRIBUTE, table.name, column.name)
            attributes[attribute_id] = Attribute(
                id=attribute_id,
                api_name=attribute_name,
                display_name=humanize(attribute_name),
                description=column.comment or "",
                type=column.type,
                required=column.required,
                constraints=column.constraints,
            )
            self.column_evidence(
                f"{element}.attributes.{attribute_name}", attribute_id, table, column, None, False
            )
        self.relations[relation_id] = Relation(
            id=relation_id,
            api_name=name,
            inverse_api_name=inverse,
            display_name=humanize(name),
            description=table.comment or "",
            from_kind=source_slot.kind_id,
            to_kind=target_slot.kind_id,
            cardinality=junction.cardinality,
            attributes=attributes,
            status=self.options.element_status,
        )
        confidence = min(junction.source.confidence, junction.destination.confidence)
        if table.key is not None:
            confidence = min(confidence, table.key.confidence)
        reasons = (
            f"junction table {table.name!r}: {source_slot.api_name} {name} {target_slot.api_name}",
            *junction.reasons,
            *junction.source.reasons,
            *junction.destination.reasons,
        )
        self.evidence.append(
            Evidence(
                element,
                relation_id,
                table.name,
                round(confidence, 4),
                reasons,
                table.columns[junction.source.column].samples,
            )
        )
        self.outcomes.append(
            TableOutcome(table.name, TableRole.RELATION, element, table.rows, table.values_read)
        )
