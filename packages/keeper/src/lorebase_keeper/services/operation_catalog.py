"""Built-in Operations (design 5.7): definitions, parameter binding and the edits they make.

For every Kind with the built-in enabled: `create_{kind}`, `update_{kind}`, `delete_{kind}`;
for every explicit Relation: `link_{relation}`, `unlink_{relation}`; and the bulk upserts
`import_records` and `import_links`. Their parameters are derived from the schema, so a
definition always matches the Revision it is read from:

| Operation | Parameters |
|-----------|------------|
| `create_{kind}` | one per stored Attribute; `key` for Kinds without a natural key |
| `update_{kind}` | `key`, then every stored Attribute as optional; `null` removes a value |
| `delete_{kind}` | `key` |
| `link_{relation}` | `from`, `to` (Record keys), then the link Attributes |
| `unlink_{relation}` | `from`, `to` |
| `import_records` | `kind` (api name), `rows` (objects keyed by Attribute api name) |
| `import_links` | `relation` (forward api name), `rows` (`from`, `to` and link Attributes) |

Everything here is pure: reads of existing Records go through `RecordReader`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import JsonValue

from lorebase_core.model import (
    Attribute,
    Kind,
    Link,
    Record,
    Relation,
    RelationBacking,
    Schema,
    builtin_link_operation_names,
    builtin_operation_names,
    new_ulid,
    record_key,
)
from lorebase_core.types import AttributeType, TypeSpec, TypeSystemError, encode_value
from lorebase_core.types.encoding import normalize_key
from lorebase_core.validate import (
    DataChange,
    IssueCode,
    LinkDelete,
    LinkUpsert,
    RecordDelete,
    RecordUpsert,
)
from lorebase_keeper.services.errors import Finding

IMPORT_RECORDS = "import_records"
IMPORT_LINKS = "import_links"
KEY = "key"
FROM = "from"
TO = "to"

PARAMETER_UNKNOWN = "parameter.unknown"
PARAMETER_REQUIRED = "parameter.required"
PARAMETER_INVALID = "parameter.invalid"
RECORD_EXISTS = "record.exists"
LIMIT_EXCEEDED = "parameter.limit_exceeded"

RecordReader = Callable[[str, Sequence[str]], Awaitable[Mapping[str, Mapping[str, JsonValue]]]]
"""Current values (Attribute id keyed) of Records of a Kind by key; absent keys do not exist."""


class Action(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LINK = "link"
    UNLINK = "unlink"
    IMPORT_RECORDS = "import_records"
    IMPORT_LINKS = "import_links"


@dataclass(frozen=True)
class Parameter:
    """A parameter: `type` is a Lorebase type, or `None` for structured JSON (`rows`)."""

    name: str
    type: TypeSpec | None
    required: bool
    description: str = ""
    attribute: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": "json"
            if self.type is None
            else self.type.model_dump(mode="json", exclude_none=True),
            "required": self.required,
            "description": self.description,
        }


@dataclass(frozen=True)
class BuiltinOperation:
    """A built-in Operation of one Kind or Relation (or the bulk imports)."""

    name: str
    action: Action
    description: str
    parameters: tuple[Parameter, ...]
    kind: Kind | None = None
    relation: Relation | None = None

    @property
    def is_import(self) -> bool:
        return self.action in (Action.IMPORT_RECORDS, Action.IMPORT_LINKS)

    def document(self) -> dict[str, Any]:
        """Description served by the schema API, shaped like a stored Operation."""
        return {
            "api_name": self.name,
            "builtin": True,
            "description": self.description,
            "parameters": [p.document() for p in self.parameters],
            "review_policy": {"mode": "never"},
            "status": "active",
        }


_STRING = TypeSpec.of(AttributeType.STRING)


def _stored(attributes: Mapping[str, Attribute]) -> list[Attribute]:
    return [a for a in attributes.values() if not a.is_derived]


def _attribute_parameters(
    attributes: Mapping[str, Attribute], *, required: bool
) -> list[Parameter]:
    return [
        Parameter(a.api_name, a.type, required and a.required, a.description, a.id)
        for a in _stored(attributes)
    ]


def builtin_definition(schema: Schema, name: str) -> BuiltinOperation | None:
    """The built-in Operation called `name` in `schema`, if any."""
    if name == IMPORT_RECORDS:
        return BuiltinOperation(
            name,
            Action.IMPORT_RECORDS,
            "Create or replace many Records of one Kind. Rows are objects keyed by Attribute "
            "api name; large imports are committed in batches.",
            (
                Parameter("kind", _STRING, True, "Kind api name."),
                Parameter("rows", None, True, "Records to upsert."),
            ),
        )
    if name == IMPORT_LINKS:
        return BuiltinOperation(
            name,
            Action.IMPORT_LINKS,
            "Create or replace many Links of one Relation. Rows hold `from`, `to` and link "
            "Attributes by api name.",
            (
                Parameter("relation", _STRING, True, "Forward api name of the Relation."),
                Parameter("rows", None, True, "Links to upsert."),
            ),
        )
    for kind in schema.kinds.values():
        if name not in builtin_operation_names(kind):
            continue
        action = Action(name.split("_", 1)[0])
        key = Parameter(KEY, _STRING, True, f"Key of the {kind.api_name} Record.")
        if action is Action.CREATE:
            parameters = _attribute_parameters(kind.attributes, required=True)
            if not kind.has_natural_key:
                parameters.insert(0, Parameter(KEY, _STRING, False, "Key; minted when absent."))
            text = f"Create a {kind.api_name} Record."
        elif action is Action.UPDATE:
            parameters = [key, *_attribute_parameters(kind.attributes, required=False)]
            text = f"Change Attributes of a {kind.api_name} Record; null removes a value."
        else:
            parameters = [key]
            text = f"Delete a {kind.api_name} Record and, per Relation policy, its Links."
        return BuiltinOperation(name, action, text, tuple(parameters), kind=kind)
    for relation in schema.relations.values():
        if relation.backing is not RelationBacking.EXPLICIT:
            continue
        if name not in builtin_link_operation_names(relation):
            continue
        action = Action.LINK if name.startswith("link_") else Action.UNLINK
        ends = [
            Parameter(FROM, _STRING, True, "Key of the `from` Record."),
            Parameter(TO, _STRING, True, "Key of the `to` Record."),
        ]
        if action is Action.LINK:
            ends.extend(_attribute_parameters(relation.attributes, required=True))
            text = f"Link two Records through {relation.api_name}."
        else:
            text = f"Remove a {relation.api_name} Link."
        return BuiltinOperation(name, action, text, tuple(ends), relation=relation)
    return None


# Parameter binding ---------------------------------------------------------------------------


@dataclass
class Bound:
    """Canonical parameter values and the findings of binding them."""

    values: dict[str, JsonValue] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


def _encode(
    spec: TypeSpec | None, raw: JsonValue, path: tuple[str | int, ...], bound: Bound
) -> JsonValue:
    if spec is None:
        return raw
    try:
        return encode_value(spec, raw)
    except (TypeSystemError, ValueError) as error:
        bound.findings.append(Finding(PARAMETER_INVALID, str(error), path))
        return None


def bind(
    definition: BuiltinOperation, parameters: Mapping[str, JsonValue], *, allow_null: bool = False
) -> Bound:
    """Validate parameters against the definition and encode them to canonical values."""
    bound = Bound()
    known = {p.name: p for p in definition.parameters}
    for name in parameters:
        if name not in known:
            bound.findings.append(
                Finding(PARAMETER_UNKNOWN, f"unknown parameter {name!r}", ("parameters", name))
            )
    nullable = allow_null or definition.action is Action.UPDATE
    for parameter in definition.parameters:
        path: tuple[str | int, ...] = ("parameters", parameter.name)
        if parameter.name not in parameters or (
            parameters[parameter.name] is None and not nullable
        ):
            if parameter.required:
                bound.findings.append(
                    Finding(PARAMETER_REQUIRED, f"{parameter.name!r} is required", path)
                )
            continue
        raw = parameters[parameter.name]
        if raw is None:
            if parameter.required:
                bound.findings.append(
                    Finding(PARAMETER_REQUIRED, f"{parameter.name!r} cannot be null", path)
                )
                continue
            bound.values[parameter.name] = None
            continue
        bound.values[parameter.name] = _encode(parameter.type, raw, path, bound)
    return bound


# Edits ----------------------------------------------------------------------------------------


@dataclass
class Plan:
    """Data changes computed from bound parameters, or the findings preventing them."""

    changes: list[DataChange] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _values(
    attributes: Mapping[str, Attribute], bound: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    by_name = {a.api_name: a for a in _stored(attributes)}
    return {by_name[name].id: value for name, value in bound.items() if name in by_name}


def _key(text: JsonValue, path: tuple[str | int, ...], plan: Plan) -> str | None:
    try:
        return normalize_key(text)
    except (TypeSystemError, ValueError) as error:
        plan.findings.append(Finding(PARAMETER_INVALID, str(error), path))
        return None


async def plan_edits(
    definition: BuiltinOperation,
    bound: Mapping[str, JsonValue],
    read: RecordReader,
) -> Plan:
    """The data changes of a single-Record or single-Link built-in."""
    plan = Plan()
    if definition.kind is not None:
        await _record_edit(definition, definition.kind, bound, read, plan)
    elif definition.relation is not None:
        _link_edit(definition, definition.relation, bound, plan)
    return plan


async def _record_edit(
    definition: BuiltinOperation,
    kind: Kind,
    bound: Mapping[str, JsonValue],
    read: RecordReader,
    plan: Plan,
) -> None:
    values = _values(kind.attributes, bound)
    if definition.action is Action.CREATE:
        key = record_key(kind, values) if kind.has_natural_key else None
        if key is None:
            given = bound.get(KEY)
            key = _key(given, ("parameters", KEY), plan) if given is not None else new_ulid()
        if key is None:
            return
        if key in await read(kind.id, [key]):
            plan.findings.append(
                Finding(
                    RECORD_EXISTS,
                    f"{kind.api_name}/{key} exists already",
                    (),
                    kind=kind.id,
                    key=key,
                )
            )
            return
        plan.changes.append(RecordUpsert(record=Record(kind=kind.id, key=key, values=values)))
        return
    key = _key(bound.get(KEY), ("parameters", KEY), plan)
    if key is None:
        return
    if definition.action is Action.DELETE:
        plan.changes.append(RecordDelete(kind=kind.id, key=key))
        return
    current = (await read(kind.id, [key])).get(key)
    if current is None:
        plan.findings.append(
            Finding(
                IssueCode.RECORD_NOT_FOUND.value,
                f"no {kind.api_name} Record {key!r}",
                ("parameters", KEY),
                kind=kind.id,
                key=key,
            )
        )
        return
    merged: dict[str, JsonValue] = dict(current)
    for attribute_id, value in values.items():
        if value is None:
            merged.pop(attribute_id, None)
        else:
            merged[attribute_id] = value
    plan.changes.append(RecordUpsert(record=Record(kind=kind.id, key=key, values=merged)))


def _link_edit(
    definition: BuiltinOperation, relation: Relation, bound: Mapping[str, JsonValue], plan: Plan
) -> None:
    from_key = _key(bound.get(FROM), ("parameters", FROM), plan)
    to_key = _key(bound.get(TO), ("parameters", TO), plan)
    if from_key is None or to_key is None:
        return
    if definition.action is Action.UNLINK:
        plan.changes.append(LinkDelete(relation=relation.id, from_key=from_key, to_key=to_key))
        return
    values = _values(relation.attributes, bound)
    plan.changes.append(
        LinkUpsert(link=Link(relation=relation.id, from_key=from_key, to_key=to_key, values=values))
    )


# Bulk imports ---------------------------------------------------------------------------------


def import_rows(
    schema: Schema, definition: BuiltinOperation, bound: Mapping[str, JsonValue], *, max_rows: int
) -> Plan:
    """Upserts of `import_records` or `import_links` rows, or the findings of bad rows."""
    plan = Plan()
    rows = bound.get("rows")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        plan.findings.append(
            Finding(PARAMETER_INVALID, "must be a list of objects", ("parameters", "rows"))
        )
        return plan
    if len(rows) > max_rows:
        plan.findings.append(
            Finding(LIMIT_EXCEEDED, f"at most {max_rows} rows per call", ("parameters", "rows"))
        )
        return plan
    if definition.action is Action.IMPORT_RECORDS:
        kind = schema.kind_by_api_name(str(bound.get("kind")))
        if kind is None:
            plan.findings.append(Finding(PARAMETER_INVALID, "unknown Kind", ("parameters", "kind")))
            return plan
        parameters = _attribute_parameters(kind.attributes, required=True)
        if not kind.has_natural_key:
            parameters.insert(0, Parameter(KEY, _STRING, False))
        create = BuiltinOperation("", Action.CREATE, "", tuple(parameters), kind=kind)
        for index, row in enumerate(rows):
            assert isinstance(row, dict)
            _import_record(kind, create, row, index, plan)
        return plan
    found = schema.relation_by_api_name(str(bound.get("relation")))
    relation = None if found is None else found[0]
    if relation is None or relation.backing is not RelationBacking.EXPLICIT:
        plan.findings.append(
            Finding(PARAMETER_INVALID, "unknown or derived Relation", ("parameters", "relation"))
        )
        return plan
    link = BuiltinOperation(
        "",
        Action.LINK,
        "",
        (
            Parameter(FROM, _STRING, True),
            Parameter(TO, _STRING, True),
            *_attribute_parameters(relation.attributes, required=True),
        ),
        relation=relation,
    )
    for index, row in enumerate(rows):
        assert isinstance(row, dict)
        row_bound = bind(link, row)
        if _relocate(row_bound.findings, index, plan):
            continue
        _link_edit(link, relation, row_bound.values, plan)
    return plan


def _relocate(findings: Sequence[Finding], index: int, plan: Plan) -> bool:
    """Move row findings under `parameters/rows/{index}`; true when there were any."""
    for finding in findings:
        path = ("parameters", "rows", index, *finding.path[1:])
        plan.findings.append(Finding(finding.code, finding.message, path))
    return bool(findings)


def _import_record(
    kind: Kind, create: BuiltinOperation, row: Mapping[str, JsonValue], index: int, plan: Plan
) -> None:
    row_bound = bind(create, row)
    if _relocate(row_bound.findings, index, plan):
        return
    values = _values(kind.attributes, row_bound.values)
    key = record_key(kind, values) if kind.has_natural_key else None
    if key is None:
        given = row_bound.values.get(KEY)
        key = _key(given, ("parameters", "rows", index, KEY), plan) if given else new_ulid()
    if key is not None:
        plan.changes.append(RecordUpsert(record=Record(kind=kind.id, key=key, values=values)))


# Responses ------------------------------------------------------------------------------------


def edit_document(schema: Schema, change: DataChange) -> dict[str, Any]:
    """An edit with api names instead of ids."""
    if isinstance(change, RecordUpsert | RecordDelete):
        kind_id = change.record.kind if isinstance(change, RecordUpsert) else change.kind
        kind = schema.kinds.get(kind_id)
        body: dict[str, Any] = {
            "op": change.op,
            "kind": kind.api_name if kind else kind_id,
            "key": change.identity.key,
        }
        if isinstance(change, RecordUpsert):
            body["values"] = _named(kind.attributes if kind else {}, change.record.values)
        return body
    relation_id = change.link.relation if isinstance(change, LinkUpsert) else change.relation
    relation = schema.relations.get(relation_id)
    identity = change.identity
    body = {
        "op": change.op,
        "relation": relation.api_name if relation else relation_id,
        "from": identity.from_key,
        "to": identity.to_key,
    }
    if isinstance(change, LinkUpsert):
        body["values"] = _named(relation.attributes if relation else {}, change.link.values)
    return body


def _named(attributes: Mapping[str, Attribute], values: Mapping[str, JsonValue]) -> dict[str, Any]:
    return {
        (attributes[a].api_name if a in attributes else a): value for a, value in values.items()
    }
