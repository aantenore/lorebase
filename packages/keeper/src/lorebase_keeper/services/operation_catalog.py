"""Built-in Operations (design 5.7): definitions, parameter binding and the edits they make.

For every Kind with the built-in enabled: `create_{kind}`, `update_{kind}`, `delete_{kind}`;
for every explicit Relation: `link_{relation}`, `unlink_{relation}`; the bulk upserts
`import_records`, `import_links` and `import_batch`; and the bulk delete `delete_records`.
Their parameters are derived from the
schema, so a definition always matches the Revision it is read from:

| Operation | Parameters |
|-----------|------------|
| `create_{kind}` | one per stored Attribute; `key` for Kinds without a natural key |
| `update_{kind}` | `key`, then every stored Attribute as optional; `null` removes a value |
| `delete_{kind}` | `key` |
| `link_{relation}` | `from`, `to` (Record keys), then the link Attributes |
| `unlink_{relation}` | `from`, `to` |
| `import_records` | `kind` (api name), `rows` (objects keyed by Attribute api name; `links`) |
| `import_links` | `relation` (forward api name), `rows` (`from`, `to` and link Attributes) |
| `import_batch` | `records` (Record rows with `kind`), `links` (Link rows with `relation`) |
| `delete_records` | `kind` (api name), `keys` (Record keys), `skip_missing` (optional boolean) |

A Record row's `links` maps a Relation name (forward from the Record's Kind, or inverse when the
Kind is its `to` end) to the key at the other end, an object with `key` and link Attributes, or
a list of these. A Record and its Links are one unit: bulk imports commit in batches of whole
units, so a Relation that is `required_from` is satisfied in the Revision that creates the
Record. Units of Records without Links come first, then Records with Links, then Links of
Records not in the import, so Links usually point to Records committed in an earlier or the
same batch.

`delete_records` deletes Records of one Kind whose `delete` built-in is enabled, all in one
Revision (it is not an import: no batching). Keys that do not exist fail the call, or with
`skip_missing` are left out and reported as warnings. Links of the deleted Records follow each
Relation's `on_delete` policy as for `delete_{kind}`. A `delete_{kind}` of a Kind whose snake
case name is `records` takes precedence over the bulk delete.

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
    LinkDirection,
    Record,
    Relation,
    RelationBacking,
    Schema,
    builtin_link_operation_names,
    builtin_operation_names,
    new_ulid,
    record_key,
    snake_case,
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
from lorebase_keeper.reading.sensitivity import UNCLEARED, Masking
from lorebase_keeper.services.errors import Finding

IMPORT_RECORDS = "import_records"
IMPORT_LINKS = "import_links"
IMPORT_BATCH = "import_batch"
DELETE_RECORDS = "delete_records"
KEY = "key"
KEYS = "keys"
SKIP_MISSING = "skip_missing"
FROM = "from"
TO = "to"
LINKS = "links"
KIND = "kind"
RELATION = "relation"

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
    IMPORT_BATCH = "import_batch"
    DELETE_RECORDS = "delete_records"


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
        return self.action in (Action.IMPORT_RECORDS, Action.IMPORT_LINKS, Action.IMPORT_BATCH)

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
_BOOLEAN = TypeSpec.of(AttributeType.BOOLEAN)


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
            "api name; a row's `links` maps Relation names to the keys at the other end, "
            "committed with the Record. Large imports are committed in batches.",
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
    if name == IMPORT_BATCH:
        return BuiltinOperation(
            name,
            Action.IMPORT_BATCH,
            "Create or replace Records of several Kinds and Links of several Relations in one "
            "call. `records` rows name their `kind` and may hold `links`; `links` rows name "
            "their `relation`. A Record is committed in the same Revision as its Links.",
            (
                Parameter("records", None, False, "Records to upsert, each with `kind`."),
                Parameter("links", None, False, "Links to upsert, each with `relation`."),
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
    if name == DELETE_RECORDS:
        return BuiltinOperation(
            name,
            Action.DELETE_RECORDS,
            "Delete many Records of one Kind in one Revision and, per Relation policy, their "
            "Links. Unknown keys fail the call unless `skip_missing` is true.",
            (
                Parameter(KIND, _STRING, True, "Kind api name."),
                Parameter(KEYS, None, True, "Keys of the Records to delete."),
                Parameter(
                    SKIP_MISSING,
                    _BOOLEAN,
                    False,
                    "Leave out keys that do not exist (reported as warnings) instead of failing.",
                ),
            ),
        )
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
    """Data changes computed from bound parameters, or the findings preventing them.

    `units` lists the sizes of consecutive runs of `changes` that must be committed in the
    same Revision (a Record and its Links); empty means every change stands alone.
    """

    changes: list[DataChange] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    units: list[int] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)


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


# Bulk delete ----------------------------------------------------------------------------------


async def delete_keys(
    schema: Schema, bound: Mapping[str, JsonValue], read: RecordReader, *, max_keys: int
) -> Plan:
    """The deletes of `delete_records`, one per distinct key, or the findings of bad keys."""
    plan = Plan()
    kind = schema.kind_by_api_name(str(bound.get(KIND)))
    if kind is None:
        plan.findings.append(Finding(PARAMETER_INVALID, "unknown Kind", ("parameters", KIND)))
        return plan
    if f"{Action.DELETE.value}_{snake_case(kind.api_name)}" not in builtin_operation_names(kind):
        plan.findings.append(
            Finding(
                PARAMETER_INVALID,
                f"the delete built-in is not enabled for {kind.api_name}",
                ("parameters", KIND),
                kind=kind.id,
            )
        )
        return plan
    raw = bound.get(KEYS)
    if not isinstance(raw, list) or not raw:
        plan.findings.append(
            Finding(PARAMETER_INVALID, "must be a non-empty list of keys", ("parameters", KEYS))
        )
        return plan
    if len(raw) > max_keys:
        plan.findings.append(
            Finding(LIMIT_EXCEEDED, f"at most {max_keys} keys per call", ("parameters", KEYS))
        )
        return plan
    keys: dict[str, int] = {}
    for index, text in enumerate(raw):
        key = _key(text, ("parameters", KEYS, index), plan)
        if key is not None:
            keys.setdefault(key, index)
    if plan.findings:
        return plan
    found = await read(kind.id, list(keys))
    skip = bound.get(SKIP_MISSING) is True
    for key, index in keys.items():
        if key in found:
            plan.changes.append(RecordDelete(kind=kind.id, key=key))
            continue
        missing = Finding(
            IssueCode.RECORD_NOT_FOUND.value,
            f"no {kind.api_name} Record {key!r}" + (" (skipped)" if skip else ""),
            ("parameters", KEYS, index),
            severity="warning" if skip else "error",
            kind=kind.id,
            key=key,
        )
        (plan.warnings if skip else plan.findings).append(missing)
    if plan.findings:
        plan.changes = []
    return plan


# Bulk imports ---------------------------------------------------------------------------------

Path = tuple[str | int, ...]


def _rows(value: JsonValue, path: Path, plan: Plan) -> list[dict[str, JsonValue]] | None:
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        plan.findings.append(Finding(PARAMETER_INVALID, "must be a list of objects", path))
        return None
    return [r for r in value if isinstance(r, dict)]


def import_rows(
    schema: Schema, definition: BuiltinOperation, bound: Mapping[str, JsonValue], *, max_rows: int
) -> Plan:
    """Upserts of `import_records`, `import_links` or `import_batch` rows grouped in units (a
    Record with its Links), or the findings of bad rows."""
    plan = Plan()
    importer = _Importer(schema, plan)
    if definition.action is Action.IMPORT_BATCH:
        records = bound.get("records") or []
        links = bound.get("links") or []
        record_rows = _rows(records, ("parameters", "records"), plan)
        link_rows = _rows(links, ("parameters", "links"), plan)
        if record_rows is None or link_rows is None:
            return plan
        if not record_rows and not link_rows:
            plan.findings.append(
                Finding(PARAMETER_REQUIRED, "records or links are required", ("parameters",))
            )
            return plan
        if len(record_rows) + len(link_rows) > max_rows:
            plan.findings.append(
                Finding(LIMIT_EXCEEDED, f"at most {max_rows} rows per call", ("parameters",))
            )
            return plan
        for index, row in enumerate(record_rows):
            path: Path = ("parameters", "records", index)
            kind = schema.kind_by_api_name(str(row.get(KIND)))
            if kind is None:
                plan.findings.append(Finding(PARAMETER_INVALID, "unknown Kind", (*path, KIND)))
                continue
            importer.record(kind, {k: v for k, v in row.items() if k != KIND}, path)
        for index, row in enumerate(link_rows):
            path = ("parameters", "links", index)
            relation = _stored_relation(schema, str(row.get(RELATION)))
            if relation is None:
                plan.findings.append(
                    Finding(PARAMETER_INVALID, "unknown or derived Relation", (*path, RELATION))
                )
                continue
            importer.link(relation, {k: v for k, v in row.items() if k != RELATION}, path)
        return importer.finish(max_rows)
    rows = _rows(bound.get("rows"), ("parameters", "rows"), plan)
    if rows is None:
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
        for index, row in enumerate(rows):
            importer.record(kind, row, ("parameters", "rows", index))
        return importer.finish(max_rows)
    relation = _stored_relation(schema, str(bound.get("relation")))
    if relation is None:
        plan.findings.append(
            Finding(PARAMETER_INVALID, "unknown or derived Relation", ("parameters", "relation"))
        )
        return plan
    for index, row in enumerate(rows):
        importer.link(relation, row, ("parameters", "rows", index))
    return importer.finish(max_rows)


def _stored_relation(schema: Schema, name: str) -> Relation | None:
    """The explicit Relation with that forward api name."""
    found = schema.relation_by_api_name(name)
    if found is None or found[1] is not LinkDirection.FORWARD:
        return None
    relation = found[0]
    return relation if relation.backing is RelationBacking.EXPLICIT else None


def _relocate(findings: Sequence[Finding], path: Path, plan: Plan) -> bool:
    """Move parameter findings of a row under `path`; true when there were any."""
    for finding in findings:
        plan.findings.append(Finding(finding.code, finding.message, (*path, *finding.path[1:])))
    return bool(findings)


def _link_definition(relation: Relation) -> BuiltinOperation:
    return BuiltinOperation(
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


class _Importer:
    """Collects the units of a bulk import: Records with the Links of their rows, Links."""

    def __init__(self, schema: Schema, plan: Plan) -> None:
        self.schema = schema
        self.plan = plan
        self.records: dict[tuple[str, str], list[DataChange]] = {}
        self.loose: list[DataChange] = []
        self._creates: dict[str, BuiltinOperation] = {}
        self._links: dict[str, BuiltinOperation] = {}

    def create(self, kind: Kind) -> BuiltinOperation:
        definition = self._creates.get(kind.id)
        if definition is None:
            parameters = _attribute_parameters(kind.attributes, required=True)
            if not kind.has_natural_key:
                parameters.insert(0, Parameter(KEY, _STRING, False))
            definition = BuiltinOperation("", Action.CREATE, "", tuple(parameters), kind=kind)
            self._creates[kind.id] = definition
        return definition

    def link_definition(self, relation: Relation) -> BuiltinOperation:
        definition = self._links.get(relation.id)
        if definition is None:
            definition = self._links[relation.id] = _link_definition(relation)
        return definition

    def record(self, kind: Kind, row: Mapping[str, JsonValue], path: Path) -> None:
        links = None
        if LINKS in row and kind.attribute_by_api_name(LINKS) is None:
            links = row[LINKS]
            row = {k: v for k, v in row.items() if k != LINKS}
        row_bound = bind(self.create(kind), row)
        if _relocate(row_bound.findings, path, self.plan):
            return
        values = _values(kind.attributes, row_bound.values)
        key = record_key(kind, values) if kind.has_natural_key else None
        if key is None:
            given = row_bound.values.get(KEY)
            key = _key(given, (*path, KEY), self.plan) if given else new_ulid()
        if key is None:
            return
        unit: list[DataChange] = [RecordUpsert(record=Record(kind=kind.id, key=key, values=values))]
        if links is not None:
            unit.extend(self.row_links(kind, key, links, (*path, LINKS)))
        self.records[(kind.id, key)] = unit

    def row_links(self, kind: Kind, key: str, links: JsonValue, path: Path) -> list[DataChange]:
        if not isinstance(links, dict):
            self.plan.findings.append(
                Finding(PARAMETER_INVALID, "must be an object keyed by Relation name", path)
            )
            return []
        changes: list[DataChange] = []
        for name, targets in links.items():
            found = self.schema.relation_by_api_name(name)
            if (
                found is None
                or found[0].backing is not RelationBacking.EXPLICIT
                or found[0].endpoint(found[1]) != kind.id
            ):
                self.plan.findings.append(
                    Finding(
                        PARAMETER_INVALID,
                        f"{name!r} is not a stored Relation of {kind.api_name}",
                        (*path, name),
                    )
                )
                continue
            relation, direction = found
            many = isinstance(targets, list)
            items = targets if isinstance(targets, list) else [targets]
            for position, target in enumerate(items):
                where: Path = (*path, name, position) if many else (*path, name)
                attributes: dict[str, JsonValue] = {}
                other: JsonValue = target
                if isinstance(target, dict):
                    other = target.get(KEY)
                    attributes = {k: v for k, v in target.items() if k != KEY}
                ends = (
                    {FROM: key, TO: other}
                    if direction is LinkDirection.FORWARD
                    else {FROM: other, TO: key}
                )
                link = self.build_link(relation, {**attributes, **ends}, where)
                if link is not None:
                    changes.append(link)
        return changes

    def build_link(
        self, relation: Relation, row: Mapping[str, JsonValue], path: Path
    ) -> DataChange | None:
        definition = self.link_definition(relation)
        row_bound = bind(definition, row)
        if _relocate(row_bound.findings, path, self.plan):
            return None
        plan = Plan()
        _link_edit(definition, relation, row_bound.values, plan)
        self.plan.findings.extend(
            Finding(f.code, f.message, (*path, *f.path[1:])) for f in plan.findings
        )
        return plan.changes[0] if plan.changes else None

    def link(self, relation: Relation, row: Mapping[str, JsonValue], path: Path) -> None:
        link = self.build_link(relation, row, path)
        if link is None:
            return
        assert isinstance(link, LinkUpsert)
        owner = self.records.get((relation.from_kind, link.link.from_key)) or self.records.get(
            (relation.to_kind, link.link.to_key)
        )
        if owner is None:
            self.loose.append(link)
        else:
            owner.append(link)

    def finish(self, max_rows: int) -> Plan:
        """Units in commit order: lone Records, Records with Links, then lone Links."""
        units = [u for u in self.records.values() if len(u) == 1]
        units += [u for u in self.records.values() if len(u) > 1]
        units += [[link] for link in self.loose]
        total = sum(len(u) for u in units)
        if total > max_rows:
            self.plan.findings.append(
                Finding(
                    LIMIT_EXCEEDED,
                    f"at most {max_rows} Records and Links per call",
                    ("parameters",),
                )
            )
            return self.plan
        for unit in units:
            self.plan.changes.extend(unit)
            self.plan.units.append(len(unit))
        return self.plan


def batches(
    changes: Sequence[DataChange], units: Sequence[int], size: int
) -> list[list[DataChange]]:
    """`changes` in batches of about `size`, never splitting a unit (a unit larger than `size`
    is a batch of its own). Without `units` every change is its own unit."""
    sizes = list(units) or [1] * len(changes)
    out: list[list[DataChange]] = []
    current: list[DataChange] = []
    position = 0
    for count in sizes:
        unit = list(changes[position : position + count])
        position += count
        if current and len(current) + len(unit) > size:
            out.append(current)
            current = []
        current.extend(unit)
    if current:
        out.append(current)
    return out


# Responses ------------------------------------------------------------------------------------


def edit_document(
    schema: Schema, change: DataChange, masking: Masking = UNCLEARED
) -> dict[str, Any]:
    """An edit with api names instead of ids, and the values the caller may not read masked
    (an update carries the Record's current values too)."""
    if isinstance(change, RecordUpsert | RecordDelete):
        kind_id = change.record.kind if isinstance(change, RecordUpsert) else change.kind
        kind = schema.kinds.get(kind_id)
        body: dict[str, Any] = {
            "op": change.op,
            "kind": kind.api_name if kind else kind_id,
            "key": change.identity.key,
        }
        if isinstance(change, RecordUpsert):
            body["values"] = (
                masking.kind_values(kind, change.record.values, omit=False)
                if kind
                else masking.value_map({}, (kind_id,), dict(change.record.values))
            )
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
        body["values"] = (
            masking.link_values(schema, relation, change.link.values)
            if relation
            else masking.value_map({}, (), dict(change.link.values))
        )
    return body
