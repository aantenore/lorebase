"""Typed environments: the names an expression may use, derived from the Schema.

- Operation expressions (preconditions, review policy, step values): ``params.<name>`` and
  each parameter by its bare name, ``principal.id`` and ``principal.subjects``, the
  functions ``record(ref)`` and ``links(ref, "relation")``; in steps also ``$step[n]``, the
  Record of an earlier step.
- Record expressions (Derivations, aggregate filters): the Record's Attributes by api name,
  ``key``, and ``links.<relation>``: the Records one hop away, by forward or inverse
  Relation name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from lorebase_core.expr.types import (
    DOUBLE,
    DYN,
    GEO_POINT,
    STRING,
    ExprType,
    TypeKind,
    from_type_spec,
    list_of,
    object_of,
    record_of,
    ref_of,
)
from lorebase_core.model.operation import (
    CreateRecordStep,
    DeleteRecordStep,
    Operation,
    UpdateRecordStep,
)
from lorebase_core.model.relation import LinkDirection, Relation
from lorebase_core.model.schema import Schema
from lorebase_core.types import TypeSpec

#: Names an Operation environment defines itself; parameters cannot use them.
PARAMS = "params"
PRINCIPAL = "principal"
#: Names a Record environment defines itself; Attributes cannot use them (``key`` is
#: reserved for Attributes by schema validation already).
KEY = "key"
LINKS = "links"
#: Fields of a ``record(Kind)`` value besides its Attributes.
RECORD_KEY = "key"
RECORD_REF = "ref"

PRINCIPAL_FIELDS: Mapping[str, ExprType] = {"id": STRING, "subjects": list_of(STRING)}
GEO_POINT_FIELDS: Mapping[str, ExprType] = {"lat": DOUBLE, "lon": DOUBLE}


@dataclass(frozen=True, slots=True)
class LinkField:
    """A ``links.<name>`` field: the Relation and the direction it is traversed in."""

    relation: Relation
    direction: LinkDirection


@dataclass(frozen=True, eq=False)
class Environment:
    """Declared names and types. ``subject`` is the Kind id whose Attributes are variables;
    ``parameters`` are the Operation parameter types (to decode parameter values)."""

    schema: Schema
    variables: Mapping[str, ExprType]
    objects: Mapping[str, Mapping[str, ExprType]] = field(default_factory=dict)
    subject: str | None = None
    records: bool = False
    step_types: tuple[ExprType | None, ...] | None = None
    link_fields: Mapping[str, LinkField] = field(default_factory=dict)
    parameters: Mapping[str, TypeSpec] = field(default_factory=dict)
    _record_fields: dict[str, dict[str, ExprType]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def kind_name(self, kind_id: str) -> str:
        kind = self.schema.kinds.get(kind_id)
        return kind.api_name if kind is not None else kind_id

    def describe(self, expr_type: ExprType) -> str:
        return expr_type.describe(self.kind_name)

    def record_fields(self, kind_id: str) -> dict[str, ExprType]:
        """Fields of ``record(kind)`` values: Attributes by api name, ``key`` and ``ref``."""
        cached = self._record_fields.get(kind_id)
        if cached is not None:
            return cached
        fields: dict[str, ExprType] = {}
        kind = self.schema.kinds.get(kind_id)
        if kind is not None:
            fields = {a.api_name: from_type_spec(a.type) for a in kind.attributes.values()}
        fields[RECORD_KEY] = STRING
        fields[RECORD_REF] = ref_of(kind_id)
        self._record_fields[kind_id] = fields
        return fields

    def fields_of(self, expr_type: ExprType) -> Mapping[str, ExprType] | None:
        """Fields of a record or object type; ``None`` for other types."""
        if expr_type.kind is TypeKind.RECORD and expr_type.name is not None:
            return self.record_fields(expr_type.name)
        if expr_type.kind is TypeKind.OBJECT and expr_type.name is not None:
            if expr_type.name == GEO_POINT:
                return GEO_POINT_FIELDS
            return self.objects.get(expr_type.name)
        return None


def operation_environment(
    schema: Schema, operation: Operation, *, step_index: int | None = None
) -> Environment:
    """Environment of an Operation's expressions; with ``step_index``, of that step's."""
    parameters = {p.name: from_type_spec(p.type) for p in operation.parameters}
    variables: dict[str, ExprType] = dict(parameters)
    variables[PARAMS] = object_of(PARAMS)
    variables[PRINCIPAL] = object_of(PRINCIPAL)
    step_types: tuple[ExprType | None, ...] | None = None
    if step_index is not None:
        step_types = tuple(_step_type(schema, step) for step in operation.steps[:step_index])
    return Environment(
        schema=schema,
        variables=variables,
        objects={PARAMS: parameters, PRINCIPAL: PRINCIPAL_FIELDS},
        records=True,
        step_types=step_types,
        parameters={p.name: p.type for p in operation.parameters},
    )


def _step_type(schema: Schema, step: object) -> ExprType | None:
    """Type of ``$step[n]``: the Record reference, ``dyn`` for an unknown Kind (reported by
    schema validation), ``None`` for Link steps."""
    if isinstance(step, CreateRecordStep | UpdateRecordStep | DeleteRecordStep):
        return ref_of(step.kind) if step.kind in schema.kinds else DYN
    return None


def record_environment(schema: Schema, kind_id: str) -> Environment:
    """Environment of expressions over one Record of ``kind_id`` (Derivations, filters)."""
    kind = schema.kinds.get(kind_id)
    variables: dict[str, ExprType] = {}
    if kind is not None:
        variables = {a.api_name: from_type_spec(a.type) for a in kind.attributes.values()}
    links = link_fields(schema, kind_id)
    variables[KEY] = STRING
    variables[LINKS] = object_of(LINKS)
    link_types = {
        name: list_of(record_of(f.relation.far_endpoint(f.direction))) for name, f in links.items()
    }
    return Environment(
        schema=schema,
        variables=variables,
        objects={LINKS: link_types},
        subject=kind_id,
        link_fields=links,
    )


def link_fields(schema: Schema, kind_id: str) -> dict[str, LinkField]:
    """Relation names usable from a Record of ``kind_id``: forward names of Relations that
    start at the Kind, inverse names of Relations that end there."""
    fields: dict[str, LinkField] = {}
    for relation in schema.relations.values():
        if relation.to_kind == kind_id:
            fields[relation.inverse_api_name] = LinkField(relation, LinkDirection.INVERSE)
        if relation.from_kind == kind_id:
            fields[relation.api_name] = LinkField(relation, LinkDirection.FORWARD)
    return fields
