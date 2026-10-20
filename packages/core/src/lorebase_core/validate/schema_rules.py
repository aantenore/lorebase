"""Static schema validation (design 5.1 to 5.9, invariant I1: the schema is internally consistent).

``validate_schema`` returns every problem found instead of stopping at the first one, so
that editors can show them all. Issues of severity ``error`` make a schema invalid.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from lorebase_core.model.attribute import Attribute
from lorebase_core.model.base import ElementStatus, NameStyle, is_valid_api_name
from lorebase_core.model.derivation import (
    AggregateDerivation,
    AggregateFunction,
    Derivation,
    ExpressionDerivation,
)
from lorebase_core.model.kind import Kind
from lorebase_core.model.operation import (
    CreateLinkStep,
    CreateRecordStep,
    DeleteLinkStep,
    Operation,
    UpdateRecordStep,
    builtin_link_operation_names,
    builtin_operation_names,
)
from lorebase_core.model.relation import Relation, RelationBacking
from lorebase_core.model.schema import Schema
from lorebase_core.model.source import SECRET_REFERENCE_PREFIX, Source, SyncMode
from lorebase_core.types import (
    TEXTUAL_TYPES,
    AttributeType,
    CastRule,
    EncodingError,
    TypeSpec,
    cast_rule,
    check_constraints,
    constraint_problems,
    encode_value,
)
from lorebase_core.validate.issues import Issue, IssueCode, PathPart, Severity

#: Attribute api names reserved for Record envelopes in the API and the query DSL.
DEFAULT_RESERVED_ATTRIBUTE_NAMES = frozenset({"key", "kind", "ref", "revision"})

#: Types usable as primary key components (rendered as stable key text).
KEYABLE_TYPES = frozenset(
    {
        AttributeType.STRING,
        AttributeType.INTEGER,
        AttributeType.DECIMAL,
        AttributeType.DATE,
        AttributeType.TIMESTAMP,
        AttributeType.ENUM,
        AttributeType.URI,
        AttributeType.REF,
    }
)

#: Types that cannot be ``unique``: no reliable equality (float) or not scalar.
NON_UNIQUE_TYPES = frozenset(
    {AttributeType.FLOAT, AttributeType.JSON, AttributeType.GEO_POINT, AttributeType.ARRAY}
)

#: Types that cannot be ``indexed``: opaque values (design 5.4).
NON_INDEXED_TYPES = frozenset({AttributeType.JSON})

#: Types that full-text search can read.
SEARCHABLE_TYPES = TEXTUAL_TYPES | {AttributeType.ENUM}

#: Aggregates that need a numeric source Attribute.
_NUMERIC_AGGREGATES = frozenset({AggregateFunction.SUM, AggregateFunction.AVG})
_NUMERIC_TYPES = frozenset({AttributeType.INTEGER, AttributeType.DECIMAL, AttributeType.FLOAT})


def _where(path: tuple[PathPart, ...]) -> str:
    return "/".join(map(str, path))


@dataclass(frozen=True)
class SchemaRules:
    """Options of static validation.

    ``allow_expressions`` gates Operations and Derivations that carry CEL expressions, which
    are only accepted once expression support is enabled.
    """

    reserved_attribute_names: frozenset[str] = DEFAULT_RESERVED_ATTRIBUTE_NAMES
    allow_expressions: bool = False


class Refs(NamedTuple):
    kind: str | None
    relation: str | None
    attribute: str | None


@dataclass
class _Checker:
    schema: Schema
    rules: SchemaRules
    issues: list[Issue] = field(default_factory=list)

    def add(
        self,
        code: IssueCode,
        path: tuple[PathPart, ...],
        message: str,
        *,
        severity: Severity = Severity.ERROR,
        kind: str | None = None,
        relation: str | None = None,
        attribute: str | None = None,
        refs: Refs | None = None,
    ) -> None:
        if refs is not None:
            kind, relation, attribute = refs
        self.issues.append(
            Issue(
                code=code,
                severity=severity,
                path=path,
                message=message,
                kind=kind,
                relation=relation,
                attribute=attribute,
            )
        )

    # Names -------------------------------------------------------------------

    def check_name(self, name: str, style: NameStyle, path: tuple[PathPart, ...]) -> None:
        if not is_valid_api_name(name, style):
            self.add(
                IssueCode.SCHEMA_API_NAME_INVALID, path, f"api name {name!r} is not {style.value}"
            )

    def check_unique(self, entries: Iterable[tuple[str, tuple[PathPart, ...]]], scope: str) -> None:
        seen: dict[str, tuple[PathPart, ...]] = {}
        for name, path in entries:
            if name in seen:
                self.add(
                    IssueCode.SCHEMA_API_NAME_DUPLICATE,
                    path,
                    f"api name {name!r} is already used in {scope} at {_where(seen[name])}",
                )
            else:
                seen[name] = path

    def check_names(self) -> None:
        schema = self.schema
        type_names: list[tuple[str, tuple[PathPart, ...]]] = []
        for kind in schema.kinds.values():
            path: tuple[PathPart, ...] = ("kinds", kind.id, "api_name")
            self.check_name(kind.api_name, NameStyle.PASCAL, path)
            type_names.append((kind.api_name, path))
            self.check_attribute_names(kind.attributes, ("kinds", kind.id), reserved=True)
        for trait in schema.traits.values():
            path = ("traits", trait.id, "api_name")
            self.check_name(trait.api_name, NameStyle.PASCAL, path)
            type_names.append((trait.api_name, path))
            trait_attribute_names = []
            for attribute in trait.attributes.values():
                attribute_path = ("traits", trait.id, "attributes", attribute.id, "api_name")
                self.check_name(attribute.api_name, NameStyle.SNAKE, attribute_path)
                trait_attribute_names.append((attribute.api_name, attribute_path))
            self.check_unique(trait_attribute_names, f"Trait {trait.api_name}")
        self.check_unique(type_names, "Kinds and Traits")

        relation_names: list[tuple[str, tuple[PathPart, ...]]] = []
        for relation in schema.relations.values():
            forward = ("relations", relation.id, "api_name")
            inverse = ("relations", relation.id, "inverse_api_name")
            self.check_name(relation.api_name, NameStyle.SNAKE, forward)
            self.check_name(relation.inverse_api_name, NameStyle.SNAKE, inverse)
            relation_names.append((relation.api_name, forward))
            symmetric = (
                relation.inverse_api_name == relation.api_name
                and relation.from_kind == relation.to_kind
            )
            if not symmetric:
                relation_names.append((relation.inverse_api_name, inverse))
            self.check_attribute_names(
                relation.attributes, ("relations", relation.id), reserved=False
            )
        for derivation in schema.derivations.values():
            definition = derivation.definition
            if definition.variant == "path":
                relation_names.append(
                    (derivation.api_name, ("derivations", derivation.id, "api_name"))
                )
                if definition.inverse_api_name is not None:
                    path = ("derivations", derivation.id, "definition", "inverse_api_name")
                    self.check_name(definition.inverse_api_name, NameStyle.SNAKE, path)
                    relation_names.append((definition.inverse_api_name, path))
        self.check_unique(relation_names, "Relations")

        builtin = [
            (name, ("kinds", kind.id, "operations_builtin"))
            for kind in schema.kinds.values()
            for name in builtin_operation_names(kind)
        ] + [
            (name, ("relations", relation.id))
            for relation in schema.relations.values()
            if relation.backing is RelationBacking.EXPLICIT
            for name in builtin_link_operation_names(relation)
        ]
        operation_names: list[tuple[str, tuple[PathPart, ...]]] = []
        for operation in schema.operations.values():
            path = ("operations", operation.id, "api_name")
            self.check_name(operation.api_name, NameStyle.SNAKE, path)
            operation_names.append((operation.api_name, path))
            parameter_names = []
            for index, parameter in enumerate(operation.parameters):
                parameter_path = ("operations", operation.id, "parameters", index, "name")
                self.check_name(parameter.name, NameStyle.SNAKE, parameter_path)
                parameter_names.append((parameter.name, parameter_path))
            self.check_unique(parameter_names, f"Operation {operation.api_name}")
        builtin_names = {name for name, _ in builtin}
        for name, path in operation_names:
            if name in builtin_names:
                self.add(
                    IssueCode.SCHEMA_API_NAME_DUPLICATE,
                    path,
                    f"{name!r} is the name of a built-in Operation",
                )
        self.check_unique(operation_names, "Operations")

        derivation_names = []
        for derivation in schema.derivations.values():
            path = ("derivations", derivation.id, "api_name")
            self.check_name(derivation.api_name, NameStyle.SNAKE, path)
            derivation_names.append((derivation.api_name, path))
        self.check_unique(derivation_names, "Derivations")

        source_names = []
        for source in schema.sources.values():
            path = ("sources", source.id, "api_name")
            self.check_name(source.api_name, NameStyle.DOTTED_SNAKE, path)
            source_names.append((source.api_name, path))
        self.check_unique(source_names, "Sources")

    def check_attribute_names(
        self, attributes: dict[str, Attribute], owner: tuple[PathPart, ...], *, reserved: bool
    ) -> None:
        names = []
        for attribute in attributes.values():
            path = (*owner, "attributes", attribute.id, "api_name")
            self.check_name(attribute.api_name, NameStyle.SNAKE, path)
            if reserved and attribute.api_name in self.rules.reserved_attribute_names:
                self.add(
                    IssueCode.SCHEMA_API_NAME_RESERVED,
                    path,
                    f"api name {attribute.api_name!r} is reserved",
                )
            names.append((attribute.api_name, path))
        self.check_unique(names, "/".join(map(str, owner)))

    def check_attribute_ids(self) -> None:
        owners: dict[str, tuple[PathPart, ...]] = {}
        for collection, elements in (
            ("kinds", self.schema.kinds),
            ("relations", self.schema.relations),
            ("traits", self.schema.traits),
        ):
            for element in elements.values():
                for attribute_id in element.attributes:
                    path = (collection, element.id, "attributes", attribute_id)
                    if attribute_id in owners:
                        self.add(
                            IssueCode.SCHEMA_ID_DUPLICATE,
                            path,
                            f"attribute id {attribute_id} is also declared at "
                            f"{_where(owners[attribute_id])}",
                            attribute=attribute_id,
                        )
                    else:
                        owners[attribute_id] = path

    # Types and Attributes --------------------------------------------------------

    def check_type_references(self, spec: TypeSpec, path: tuple[PathPart, ...]) -> None:
        scalar = spec.scalar
        if scalar.kind_ref is not None and scalar.kind_ref not in self.schema.kinds:
            self.add(
                IssueCode.SCHEMA_TYPE_REFERENCE_UNKNOWN,
                path,
                f"ref type names unknown Kind {scalar.kind_ref} "
                "(Kinds of other Domains cannot be referenced)",
            )

    def check_attribute(
        self,
        attribute: Attribute,
        owner: tuple[PathPart, ...],
        *,
        kind_id: str | None,
        relation_id: str | None,
        active: bool,
    ) -> None:
        path = (*owner, "attributes", attribute.id)
        refs = Refs(kind_id, relation_id, attribute.id)
        spec = attribute.type
        if active and not attribute.description.strip():
            self.add(
                IssueCode.SCHEMA_DESCRIPTION_MISSING,
                (*path, "description"),
                f"attribute {attribute.api_name!r} of an active element needs a description",
                refs=refs,
            )
        self.check_type_references(spec, (*path, "type"))
        for name, message in constraint_problems(spec, attribute.constraints):
            self.add(
                IssueCode.SCHEMA_CONSTRAINT_INVALID,
                (*path, "constraints", name.value),
                message,
                refs=refs,
            )

        if attribute.default is not None:
            try:
                canonical = encode_value(spec, attribute.default)
            except EncodingError as exc:
                self.add(
                    IssueCode.SCHEMA_DEFAULT_INVALID,
                    (*path, "default"),
                    f"default: {exc.message}",
                    refs=refs,
                )
            else:
                if canonical != attribute.default:
                    self.add(
                        IssueCode.SCHEMA_DEFAULT_INVALID,
                        (*path, "default"),
                        f"default must be in canonical form: {canonical!r}",
                        refs=refs,
                    )
                elif not constraint_problems(spec, attribute.constraints):
                    for violation in check_constraints(spec, attribute.constraints, canonical):
                        self.add(
                            IssueCode.SCHEMA_DEFAULT_INVALID,
                            (*path, "default"),
                            f"default violates {violation.constraint.value}: {violation.message}",
                            refs=refs,
                        )
            if attribute.is_derived:
                self.add(
                    IssueCode.SCHEMA_DEFAULT_CONFLICT,
                    (*path, "default"),
                    "derived attributes are computed and cannot have a default",
                    refs=refs,
                )
            if attribute.unique:
                self.add(
                    IssueCode.SCHEMA_DEFAULT_CONFLICT,
                    (*path, "default"),
                    "a default on a unique attribute is shared by every Record omitting the value",
                    severity=Severity.WARNING,
                    refs=refs,
                )
        if attribute.is_derived and attribute.required:
            self.add(
                IssueCode.SCHEMA_DEFAULT_CONFLICT,
                (*path, "required"),
                "derived attributes are computed and cannot be required",
                refs=refs,
            )

        if attribute.unique and relation_id is not None:
            self.add(
                IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID,
                (*path, "unique"),
                "link attributes cannot be unique",
                refs=refs,
            )
        elif attribute.unique and spec.name in NON_UNIQUE_TYPES:
            self.add(
                IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID,
                (*path, "unique"),
                f"{spec.describe()} attributes cannot be unique",
                refs=refs,
            )
        if attribute.indexed and spec.scalar.name in NON_INDEXED_TYPES:
            self.add(
                IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID,
                (*path, "indexed"),
                f"{spec.describe()} attributes cannot be indexed",
                refs=refs,
            )
        if attribute.searchable and spec.scalar.name not in SEARCHABLE_TYPES:
            self.add(
                IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID,
                (*path, "searchable"),
                f"{spec.describe()} attributes cannot be full-text searchable",
                refs=refs,
            )
        if attribute.derivation_id is not None:
            if relation_id is not None:
                self.add(
                    IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID,
                    (*path, "derivation_id"),
                    "link attributes cannot be derived",
                    refs=refs,
                )
            else:
                derivation = self.schema.derivations.get(attribute.derivation_id)
                if derivation is None:
                    self.add(
                        IssueCode.SCHEMA_DERIVATION_REFERENCE_UNKNOWN,
                        (*path, "derivation_id"),
                        f"unknown Derivation {attribute.derivation_id}",
                        refs=refs,
                    )
                elif derivation.target != (kind_id, attribute.id):
                    self.add(
                        IssueCode.SCHEMA_DERIVATION_INVALID,
                        (*path, "derivation_id"),
                        f"Derivation {derivation.api_name!r} does not compute this attribute",
                        refs=refs,
                    )
        if attribute.authority is not None and attribute.authority not in self.schema.sources:
            self.add(
                IssueCode.SCHEMA_AUTHORITY_UNKNOWN,
                (*path, "authority"),
                f"unknown Source {attribute.authority}",
                refs=refs,
            )

    # Kinds -----------------------------------------------------------------------

    def check_kind(self, kind: Kind) -> None:
        base: tuple[PathPart, ...] = ("kinds", kind.id)
        active = kind.status is ElementStatus.ACTIVE
        for kind_attribute in kind.attributes.values():
            self.check_attribute(
                kind_attribute, base, kind_id=kind.id, relation_id=None, active=active
            )

        seen: set[str] = set()
        for index, attribute_id in enumerate(kind.primary_key):
            path = (*base, "primary_key", index)
            attribute = kind.attributes.get(attribute_id)
            if attribute is None:
                self.add(
                    IssueCode.SCHEMA_PRIMARY_KEY_UNKNOWN,
                    path,
                    f"primary key attribute {attribute_id} is not an attribute of the Kind",
                    kind=kind.id,
                    attribute=attribute_id,
                )
                continue
            problems = []
            if attribute_id in seen:
                problems.append("is listed twice")
            if not attribute.required:
                problems.append("must be required")
            if attribute.type.name not in KEYABLE_TYPES:
                problems.append(f"cannot be of type {attribute.type.describe()}")
            if attribute.is_derived:
                problems.append("cannot be derived")
            if attribute.default is not None:
                problems.append("cannot have a default")
            seen.add(attribute_id)
            for problem in problems:
                self.add(
                    IssueCode.SCHEMA_PRIMARY_KEY_INVALID,
                    path,
                    f"primary key attribute {attribute.api_name!r} {problem}",
                    kind=kind.id,
                    attribute=attribute_id,
                )

        if kind.title_attribute is None:
            if active:
                self.add(
                    IssueCode.SCHEMA_TITLE_ATTRIBUTE_MISSING,
                    (*base, "title_attribute"),
                    f"active Kind {kind.api_name!r} needs a title attribute",
                    kind=kind.id,
                )
        elif kind.title_attribute not in kind.attributes:
            self.add(
                IssueCode.SCHEMA_TITLE_ATTRIBUTE_UNKNOWN,
                (*base, "title_attribute"),
                f"title attribute {kind.title_attribute} is not an attribute of the Kind",
                kind=kind.id,
                attribute=kind.title_attribute,
            )

        self.check_trait_implementations(kind)

    def check_trait_implementations(self, kind: Kind) -> None:
        implemented: set[str] = set()
        for index, implementation in enumerate(kind.traits):
            path: tuple[PathPart, ...] = ("kinds", kind.id, "traits", index)
            trait = self.schema.traits.get(implementation.trait_id)
            if trait is None:
                self.add(
                    IssueCode.SCHEMA_TRAIT_UNKNOWN,
                    (*path, "trait_id"),
                    f"unknown Trait {implementation.trait_id}",
                    kind=kind.id,
                )
                continue
            if trait.id in implemented:
                self.add(
                    IssueCode.SCHEMA_TRAIT_DUPLICATE,
                    path,
                    f"Trait {trait.api_name!r} is implemented twice",
                    kind=kind.id,
                )
                continue
            implemented.add(trait.id)
            mapping = implementation.attribute_map
            for trait_attribute_id in mapping:
                if trait_attribute_id not in trait.attributes:
                    self.add(
                        IssueCode.SCHEMA_TRAIT_MAPPING_INVALID,
                        (*path, "attribute_map", trait_attribute_id),
                        f"{trait_attribute_id} is not an attribute of Trait {trait.api_name!r}",
                        kind=kind.id,
                    )
            for trait_attribute in trait.attributes.values():
                mapping_path = (*path, "attribute_map", trait_attribute.id)
                kind_attribute_id = mapping.get(trait_attribute.id)
                if kind_attribute_id is None:
                    self.add(
                        IssueCode.SCHEMA_TRAIT_MAPPING_MISSING,
                        mapping_path,
                        f"Trait attribute {trait_attribute.api_name!r} of {trait.api_name!r} "
                        "is not mapped",
                        kind=kind.id,
                    )
                    continue
                kind_attribute = kind.attributes.get(kind_attribute_id)
                if kind_attribute is None:
                    self.add(
                        IssueCode.SCHEMA_TRAIT_MAPPING_INVALID,
                        mapping_path,
                        f"{kind_attribute_id} is not an attribute of the Kind",
                        kind=kind.id,
                        attribute=kind_attribute_id,
                    )
                    continue
                if cast_rule(kind_attribute.type, trait_attribute.type) is not CastRule.SAFE:
                    self.add(
                        IssueCode.SCHEMA_TRAIT_MAPPING_TYPE,
                        mapping_path,
                        f"{kind_attribute.api_name!r} ({kind_attribute.type.describe()}) is not "
                        f"compatible with {trait.api_name}.{trait_attribute.api_name} "
                        f"({trait_attribute.type.describe()})",
                        kind=kind.id,
                        attribute=kind_attribute_id,
                    )
                if trait_attribute.required and not kind_attribute.required:
                    self.add(
                        IssueCode.SCHEMA_TRAIT_MAPPING_REQUIRED,
                        mapping_path,
                        f"{trait.api_name}.{trait_attribute.api_name} is required, "
                        f"{kind_attribute.api_name!r} is not",
                        kind=kind.id,
                        attribute=kind_attribute_id,
                    )

    # Relations -------------------------------------------------------------------

    def check_relation(self, relation: Relation) -> None:
        base: tuple[PathPart, ...] = ("relations", relation.id)
        for end, kind_id in (("from_kind", relation.from_kind), ("to_kind", relation.to_kind)):
            if kind_id not in self.schema.kinds:
                self.add(
                    IssueCode.SCHEMA_RELATION_ENDPOINT_UNKNOWN,
                    (*base, end),
                    f"{end} {kind_id} is not a Kind of Domain {self.schema.domain.id!r} "
                    "(cross-Domain Relations are not supported)",
                    relation=relation.id,
                )
        active = relation.status is ElementStatus.ACTIVE
        for link_attribute in relation.attributes.values():
            self.check_attribute(
                link_attribute, base, kind_id=None, relation_id=relation.id, active=active
            )

        fk_path = (*base, "foreign_key_attribute")
        if relation.backing is RelationBacking.EXPLICIT:
            if relation.foreign_key_attribute is not None:
                self.add(
                    IssueCode.SCHEMA_FOREIGN_KEY_INVALID,
                    fk_path,
                    "explicit Relations have no foreign key attribute",
                    relation=relation.id,
                )
            return
        if relation.attributes:
            self.add(
                IssueCode.SCHEMA_FOREIGN_KEY_INVALID,
                (*base, "attributes"),
                "foreign key Relations cannot carry link attributes",
                relation=relation.id,
            )
        if not relation.single_per_from:
            self.add(
                IssueCode.SCHEMA_FOREIGN_KEY_INVALID,
                (*base, "cardinality"),
                "foreign key Relations allow one Link per from Record (many_to_one or one_to_one)",
                relation=relation.id,
            )
        from_kind = self.schema.kinds.get(relation.from_kind)
        if relation.foreign_key_attribute is None:
            self.add(
                IssueCode.SCHEMA_FOREIGN_KEY_INVALID,
                fk_path,
                "foreign key attribute is missing",
                relation=relation.id,
            )
        elif from_kind is not None:
            attribute = from_kind.attributes.get(relation.foreign_key_attribute)
            if attribute is None:
                self.add(
                    IssueCode.SCHEMA_FOREIGN_KEY_INVALID,
                    fk_path,
                    f"{relation.foreign_key_attribute} is not an attribute of the from Kind",
                    relation=relation.id,
                )
            elif attribute.type != TypeSpec.ref(relation.to_kind):
                self.add(
                    IssueCode.SCHEMA_FOREIGN_KEY_INVALID,
                    fk_path,
                    f"foreign key attribute {attribute.api_name!r} must be a ref to the to Kind",
                    relation=relation.id,
                    attribute=attribute.id,
                )

    # Traits ------------------------------------------------------------------------

    def check_traits(self) -> None:
        for trait in self.schema.traits.values():
            for attribute in trait.attributes.values():
                self.check_type_references(
                    attribute.type, ("traits", trait.id, "attributes", attribute.id, "type")
                )

    # Operations ----------------------------------------------------------------------

    def check_operation(self, operation: Operation) -> None:
        base: tuple[PathPart, ...] = ("operations", operation.id)
        if operation.uses_expressions and not self.rules.allow_expressions:
            self.add(
                IssueCode.SCHEMA_EXPRESSIONS_DISABLED,
                base,
                f"Operation {operation.api_name!r} uses expressions, which are not enabled",
            )
        for index, parameter in enumerate(operation.parameters):
            self.check_type_references(parameter.type, (*base, "parameters", index, "type"))
        for index, step in enumerate(operation.steps):
            path = (*base, "steps", index)
            if isinstance(step, CreateLinkStep | DeleteLinkStep):
                relation = self.schema.relations.get(step.relation)
                if relation is None:
                    self.add(
                        IssueCode.SCHEMA_OPERATION_REFERENCE_UNKNOWN,
                        (*path, "relation"),
                        f"unknown Relation {step.relation}",
                    )
                    continue
                if relation.backing is RelationBacking.FOREIGN_KEY:
                    self.add(
                        IssueCode.SCHEMA_OPERATION_INVALID,
                        (*path, "relation"),
                        f"Links of foreign key Relation {relation.api_name!r} are derived "
                        "and cannot be written",
                        relation=relation.id,
                    )
                if isinstance(step, CreateLinkStep):
                    self.check_step_values(
                        step.values, relation.attributes, path, f"Relation {relation.api_name!r}"
                    )
                continue
            kind = self.schema.kinds.get(step.kind)
            if kind is None:
                self.add(
                    IssueCode.SCHEMA_OPERATION_REFERENCE_UNKNOWN,
                    (*path, "kind"),
                    f"unknown Kind {step.kind}",
                )
                continue
            if isinstance(step, CreateRecordStep | UpdateRecordStep):
                self.check_step_values(
                    step.values, kind.attributes, path, f"Kind {kind.api_name!r}"
                )

    def check_step_values(
        self,
        values: dict[str, str],
        attributes: dict[str, Attribute],
        path: tuple[PathPart, ...],
        owner: str,
    ) -> None:
        for attribute_id in values:
            attribute = attributes.get(attribute_id)
            if attribute is None:
                self.add(
                    IssueCode.SCHEMA_OPERATION_REFERENCE_UNKNOWN,
                    (*path, "values", attribute_id),
                    f"{attribute_id} is not an attribute of {owner}",
                    attribute=attribute_id,
                )
            elif attribute.is_derived:
                self.add(
                    IssueCode.SCHEMA_OPERATION_INVALID,
                    (*path, "values", attribute_id),
                    f"derived attribute {attribute.api_name!r} cannot be written",
                    attribute=attribute_id,
                )

    # Derivations ---------------------------------------------------------------------

    def check_derivation(self, derivation: Derivation) -> None:
        base: tuple[PathPart, ...] = ("derivations", derivation.id, "definition")
        definition = derivation.definition
        uses_expression = isinstance(definition, ExpressionDerivation) or (
            isinstance(definition, AggregateDerivation) and definition.filter is not None
        )
        if uses_expression and not self.rules.allow_expressions:
            self.add(
                IssueCode.SCHEMA_EXPRESSIONS_DISABLED,
                base,
                f"Derivation {derivation.api_name!r} uses expressions, which are not enabled",
            )
        if isinstance(definition, ExpressionDerivation | AggregateDerivation):
            kind = self.schema.kinds.get(definition.kind)
            if kind is None:
                self.add(
                    IssueCode.SCHEMA_DERIVATION_REFERENCE_UNKNOWN,
                    (*base, "kind"),
                    f"unknown Kind {definition.kind}",
                )
                return
            target = kind.attributes.get(definition.attribute)
            if target is None:
                self.add(
                    IssueCode.SCHEMA_DERIVATION_REFERENCE_UNKNOWN,
                    (*base, "attribute"),
                    f"{definition.attribute} is not an attribute of Kind {kind.api_name!r}",
                    kind=kind.id,
                )
            elif target.derivation_id != derivation.id:
                self.add(
                    IssueCode.SCHEMA_DERIVATION_INVALID,
                    (*base, "attribute"),
                    f"attribute {target.api_name!r} does not declare this Derivation",
                    kind=kind.id,
                    attribute=target.id,
                )
            if isinstance(definition, AggregateDerivation):
                self.check_aggregate(definition, kind, base)
            return
        previous_kind: str | None = None
        for index, step in enumerate(definition.steps):
            relation = self.schema.relations.get(step.relation)
            if relation is None:
                self.add(
                    IssueCode.SCHEMA_DERIVATION_REFERENCE_UNKNOWN,
                    (*base, "steps", index, "relation"),
                    f"unknown Relation {step.relation}",
                )
                previous_kind = None
                continue
            if previous_kind is not None and relation.endpoint(step.direction) != previous_kind:
                self.add(
                    IssueCode.SCHEMA_DERIVATION_INVALID,
                    (*base, "steps", index),
                    f"Relation {relation.api_name!r} does not continue the path",
                    relation=relation.id,
                )
            previous_kind = relation.far_endpoint(step.direction)

    def check_aggregate(
        self, definition: AggregateDerivation, kind: Kind, base: tuple[PathPart, ...]
    ) -> None:
        relation = self.schema.relations.get(definition.relation)
        if relation is None:
            self.add(
                IssueCode.SCHEMA_DERIVATION_REFERENCE_UNKNOWN,
                (*base, "relation"),
                f"unknown Relation {definition.relation}",
            )
            return
        if relation.endpoint(definition.direction) != kind.id:
            self.add(
                IssueCode.SCHEMA_DERIVATION_INVALID,
                (*base, "relation"),
                f"Relation {relation.api_name!r} does not start at Kind {kind.api_name!r} "
                f"in direction {definition.direction}",
                relation=relation.id,
            )
            return
        if definition.source_attribute is None:
            return
        far_kind = self.schema.kinds.get(relation.far_endpoint(definition.direction))
        source = (
            far_kind.attributes.get(definition.source_attribute) if far_kind is not None else None
        )
        if source is None:
            self.add(
                IssueCode.SCHEMA_DERIVATION_REFERENCE_UNKNOWN,
                (*base, "source_attribute"),
                f"{definition.source_attribute} is not an attribute of the Kind "
                f"across {relation.api_name!r}",
            )
        elif definition.function in _NUMERIC_AGGREGATES and source.type.name not in _NUMERIC_TYPES:
            self.add(
                IssueCode.SCHEMA_DERIVATION_INVALID,
                (*base, "function"),
                f"{definition.function} needs a numeric source attribute, "
                f"got {source.type.describe()}",
                attribute=source.id,
            )

    def check_derivation_cycles(self) -> None:
        """Aggregates over derived attributes must not depend on themselves."""
        edges: dict[str, set[str]] = defaultdict(set)
        owner: dict[str, str] = {}
        for derivation in self.schema.derivations.values():
            definition = derivation.definition
            if (
                not isinstance(definition, AggregateDerivation)
                or definition.source_attribute is None
            ):
                continue
            owner[definition.attribute] = derivation.id
            relation = self.schema.relations.get(definition.relation)
            if relation is None:
                continue
            far_kind = self.schema.kinds.get(relation.far_endpoint(definition.direction))
            source = (
                far_kind.attributes.get(definition.source_attribute)
                if far_kind is not None
                else None
            )
            if source is not None and source.is_derived:
                edges[definition.attribute].add(source.id)
        visiting: set[str] = set()
        done: set[str] = set()
        reported: set[frozenset[str]] = set()

        def visit(node: str, stack: list[str]) -> None:
            visiting.add(node)
            stack.append(node)
            for successor in sorted(edges.get(node, ())):
                if successor in visiting:
                    cycle = stack[stack.index(successor) :]
                    signature = frozenset(cycle)
                    if signature not in reported:
                        reported.add(signature)
                        derivation_id = owner.get(cycle[0], "")
                        self.add(
                            IssueCode.SCHEMA_DERIVATION_CYCLE,
                            ("derivations", derivation_id),
                            "Derivations depend on each other in a cycle: "
                            f"{' -> '.join([*cycle, successor])}",
                            attribute=cycle[0],
                        )
                elif successor not in done:
                    visit(successor, stack)
            stack.pop()
            visiting.discard(node)
            done.add(node)

        for node in sorted(edges):
            if node not in done:
                visit(node, [])

    # Sources -------------------------------------------------------------------------

    def check_source(self, source: Source) -> None:
        base: tuple[PathPart, ...] = ("sources", source.id)
        if not source.connection.startswith(SECRET_REFERENCE_PREFIX):
            self.add(
                IssueCode.SCHEMA_SOURCE_INVALID,
                (*base, "connection"),
                f"connection must be a secret reference ({SECRET_REFERENCE_PREFIX}...), "
                "never a literal",
            )
        if source.sync_mode is SyncMode.INCREMENTAL and source.watermark_column is None:
            self.add(
                IssueCode.SCHEMA_SOURCE_INVALID,
                (*base, "watermark_column"),
                "incremental Sources need a watermark column",
            )
        kind = self.schema.kinds.get(source.target_kind)
        if kind is None:
            self.add(
                IssueCode.SCHEMA_SOURCE_REFERENCE_UNKNOWN,
                (*base, "target_kind"),
                f"unknown Kind {source.target_kind}",
            )
            return
        if not kind.has_natural_key:
            self.add(
                IssueCode.SCHEMA_SOURCE_INVALID,
                (*base, "target_kind"),
                f"Kind {kind.api_name!r} has no primary key for the Source to map",
                kind=kind.id,
            )
        elif len(source.key_mapping) != len(kind.primary_key):
            self.add(
                IssueCode.SCHEMA_SOURCE_INVALID,
                (*base, "key_mapping"),
                f"key mapping has {len(source.key_mapping)} columns, "
                f"the primary key has {len(kind.primary_key)}",
                kind=kind.id,
            )
        for attribute_id in source.attribute_mapping:
            attribute = kind.attributes.get(attribute_id)
            if attribute is None:
                self.add(
                    IssueCode.SCHEMA_SOURCE_REFERENCE_UNKNOWN,
                    (*base, "attribute_mapping", attribute_id),
                    f"{attribute_id} is not an attribute of Kind {kind.api_name!r}",
                    kind=kind.id,
                    attribute=attribute_id,
                )
            elif attribute.is_derived:
                self.add(
                    IssueCode.SCHEMA_SOURCE_INVALID,
                    (*base, "attribute_mapping", attribute_id),
                    f"derived attribute {attribute.api_name!r} cannot be mapped",
                    kind=kind.id,
                    attribute=attribute_id,
                )
        for index, mapping in enumerate(source.relation_mapping):
            relation = self.schema.relations.get(mapping.relation)
            path = (*base, "relation_mapping", index, "relation")
            if relation is None:
                self.add(
                    IssueCode.SCHEMA_SOURCE_REFERENCE_UNKNOWN,
                    path,
                    f"unknown Relation {mapping.relation}",
                )
            elif relation.from_kind != kind.id:
                self.add(
                    IssueCode.SCHEMA_SOURCE_INVALID,
                    path,
                    f"Relation {relation.api_name!r} does not start at Kind {kind.api_name!r}",
                    relation=relation.id,
                )

    # Entry point ---------------------------------------------------------------------

    def run(self) -> list[Issue]:
        self.check_names()
        self.check_attribute_ids()
        for kind in self.schema.kinds.values():
            self.check_kind(kind)
        for relation in self.schema.relations.values():
            self.check_relation(relation)
        self.check_traits()
        for operation in self.schema.operations.values():
            self.check_operation(operation)
        for derivation in self.schema.derivations.values():
            self.check_derivation(derivation)
        self.check_derivation_cycles()
        for source in self.schema.sources.values():
            self.check_source(source)
        return self.issues


def validate_schema(schema: Schema, rules: SchemaRules | None = None) -> list[Issue]:
    """Return every static problem of ``schema``; an empty list means the schema is valid."""
    return _Checker(schema, rules or SchemaRules()).run()
