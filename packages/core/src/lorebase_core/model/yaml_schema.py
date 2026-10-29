"""Schema as code: load and dump a Domain schema as YAML.

The YAML form is the human-friendly face of a ``Schema``. Elements are keyed by api name and
refer to each other by api name; the stable ``id`` of each element is an optional field.

Document layout (every section is optional except ``domain``)::

    format: lorebase.schema/v1
    domain: {id: supply, display_name: Supply chain}
    kinds:
      Supplier:                       # key = api name
        id: knd_01JB...               # stable id; minted when absent
        primary_key: [supplier_code]  # Attribute api names
        title_attribute: legal_name
        attributes:
          supplier_code:
            id: att_01JB...
            type: string              # see "Types" below
            required: true
        traits:
          - trait: Located
            attribute_map: {country: country}   # trait Attribute -> Kind Attribute
    relations:
      supplied_by:                    # key = forward api name
        inverse_api_name: supplies
        from_kind: Part
        to_kind: Supplier
        cardinality: many_to_many
        attributes: {...}             # link Attributes, same shape as Kind Attributes
    traits: {Located: {attributes: {country: {type: string}}}}
    operations: {...}                 # parameters, preconditions, steps, review_policy, ...
    derivations: {...}                # definition: {variant: expression | aggregate | path, ...}
    sources: {...}                    # key = dotted api name such as erp.vendors
    lenses: {...}                     # read presets: kinds, traits, relations, depth, ...

Fields not mentioned above keep the names of the model (``display_name``, ``description``,
``status``, ``constraints``, ``sensitivity``, ``indexed``, ``searchable`` and so on) and are
omitted from the dump when they hold their default. Two keys differ from the model because they
hold api names instead of ids: ``derivation`` (``derivation_id``) and ``trait``
(``trait_id``). Expressions are CEL source strings.

Types are written ``string``, ``decimal(12,2)``, ``ref(Supplier)``, ``array<string>`` or, for
enums and arrays of complex types, as a one-key mapping: ``{enum: [low, high]}``,
``{array: {enum: [a, b]}}``.

Ids on load: an explicit ``id`` wins (so a rename keeps the element); without one, the element
with the same api name in the optional ``base`` schema is reused; otherwise a new id is minted.
Dumps always write ids.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar, cast

import yaml
from pydantic import BaseModel, ValidationError

from lorebase_core.model.attribute import Attribute
from lorebase_core.model.base import Model, SchemaElement
from lorebase_core.model.derivation import (
    AggregateDerivation,
    Derivation,
    ExpressionDerivation,
    PathDerivation,
    PathStep,
)
from lorebase_core.model.domain import Domain
from lorebase_core.model.ids import IdPrefix, id_prefix, new_id
from lorebase_core.model.kind import Kind, TraitImplementation
from lorebase_core.model.lens import Lens
from lorebase_core.model.operation import (
    CreateLinkStep,
    CreateRecordStep,
    DeleteLinkStep,
    DeleteRecordStep,
    Operation,
    OperationParameter,
    OperationStep,
    UpdateRecordStep,
)
from lorebase_core.model.relation import LinkDirection, Relation
from lorebase_core.model.schema import Schema
from lorebase_core.model.source import RelationMapping, Source
from lorebase_core.model.trait import Trait, TraitAttribute
from lorebase_core.types import AttributeType, TypeSpec

if TYPE_CHECKING:
    from lorebase_core.validate.issues import Issue
    from lorebase_core.validate.schema_rules import SchemaRules

#: Value of the optional ``format`` key; written on every dump.
SCHEMA_FORMAT = "lorebase.schema/v1"

#: Schema sections holding api-name keyed elements, in document order, with their id prefix.
_SECTIONS: tuple[tuple[str, IdPrefix], ...] = (
    ("kinds", IdPrefix.KIND),
    ("relations", IdPrefix.RELATION),
    ("traits", IdPrefix.TRAIT),
    ("operations", IdPrefix.OPERATION),
    ("derivations", IdPrefix.DERIVATION),
    ("sources", IdPrefix.SOURCE),
    ("lenses", IdPrefix.LENS),
)
_ROOT_KEYS = frozenset({"format", "domain", *(name for name, _ in _SECTIONS)})

#: Model field -> YAML key, where the YAML value is an api name instead of an id.
_YAML_KEYS = {"derivation_id": "derivation", "trait_id": "trait"}
_MODEL_KEYS = {yaml_key: key for key, yaml_key in _YAML_KEYS.items()}

_PathPart = str | int
_Path = tuple[_PathPart, ...]
IdFactory = Callable[[IdPrefix], str]


@dataclass(frozen=True)
class SchemaYamlProblem:
    """A problem found while reading a schema document; ``path`` locates it in the YAML."""

    path: tuple[str | int, ...]
    message: str

    def __str__(self) -> str:
        return f"{render_path(self.path) or '<document>'}: {self.message}"


class SchemaYamlError(ValueError):
    """The document is not a loadable schema. ``problems`` lists everything found."""

    def __init__(self, problems: Iterable[SchemaYamlProblem]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(str(problem) for problem in self.problems))


def render_path(path: Iterable[str | int]) -> str:
    """``("kinds", "Supplier", "attributes", "tier")`` -> ``kinds/Supplier/attributes/tier``."""
    return "/".join(str(part) for part in path)


class _LoadError(Exception):
    def __init__(self, problems: list[SchemaYamlProblem]) -> None:
        super().__init__(problems)
        self.problems = problems


def _fail(path: _Path, message: str) -> NoReturn:
    raise _LoadError([SchemaYamlProblem(path, message)])


# YAML parsing -------------------------------------------------------------------------------


class _Loader(yaml.SafeLoader):
    """Safe loader without implicit timestamps (dates stay text) and with duplicate-key errors."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, Mapping | list | set):
                continue
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_Loader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _Dumper(yaml.SafeDumper):
    """Block style, short scalar lists in flow style, no anchors, literal multi-line text."""

    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)

    def represent_list(self, data: Any) -> yaml.SequenceNode:
        node = super().represent_list(data)
        simple = all(isinstance(item, str | int | float | bool) or item is None for item in data)
        node.flow_style = simple
        return node

    def represent_dict(self, data: Any) -> yaml.MappingNode:
        node = super().represent_dict(data)
        node.flow_style = False
        return node


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _represent_str)
_Dumper.add_representer(list, _Dumper.represent_list)
_Dumper.add_representer(dict, _Dumper.represent_dict)


def parse_yaml(text: str) -> Any:
    """Parse YAML text with the schema loader; syntax errors become ``SchemaYamlError``."""
    try:
        return yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise SchemaYamlError([SchemaYamlProblem((), f"invalid YAML{where}: {problem}")]) from exc


# Types ----------------------------------------------------------------------------------------

_PARAMETERIZED = frozenset(
    {AttributeType.DECIMAL, AttributeType.ENUM, AttributeType.ARRAY, AttributeType.REF}
)
_DECIMAL = re.compile(r"decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_REF = re.compile(r"ref\(\s*([^()\s]+)\s*\)")
_ARRAY = re.compile(r"array<(.+)>")


def _type_node(spec: TypeSpec, names: Mapping[str, str]) -> str | dict[str, Any]:
    if spec.name is AttributeType.ENUM:
        return {"enum": list(spec.enum_values or ())}
    if spec.name is AttributeType.DECIMAL:
        return f"decimal({spec.precision},{spec.scale})"
    if spec.name is AttributeType.REF:
        kind_ref = spec.kind_ref or ""
        return f"ref({names.get(kind_ref, kind_ref)})"
    if spec.name is AttributeType.ARRAY and spec.element is not None:
        element = _type_node(spec.element, names)
        return f"array<{element}>" if isinstance(element, str) else {"array": element}
    return str(spec.name)


# Loader ---------------------------------------------------------------------------------------

_M = TypeVar("_M", bound=BaseModel)

_STEP_MODELS: dict[str, type[Model]] = {
    "create_record": CreateRecordStep,
    "update_record": UpdateRecordStep,
    "delete_record": DeleteRecordStep,
    "create_link": CreateLinkStep,
    "delete_link": DeleteLinkStep,
}


def _kind_of(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _mapping(value: object, path: _Path) -> dict[str, Any]:
    """A copy of a mapping with string keys; ``None`` reads as an empty mapping."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(path, f"expected a mapping, got {_kind_of(value)}")
    for key in value:
        if not isinstance(key, str):
            _fail((*path, str(key)), f"keys must be text, got {_kind_of(key)}; quote the key")
    return dict(value)


def _sequence(value: object, path: _Path) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(path, f"expected a list, got {_kind_of(value)}")
    return list(value)


def _names(value: object, path: _Path) -> list[str]:
    items = _sequence(value, path)
    for index, item in enumerate(items):
        if not isinstance(item, str):
            _fail((*path, index), f"expected a name, got {_kind_of(item)}")
    return items


class _Reader:
    """One load run: assigns ids to every element first, then builds the models."""

    def __init__(self, base: Schema | None, id_factory: IdFactory) -> None:
        self.base = base
        self.id_factory = id_factory
        self.problems: list[SchemaYamlProblem] = []
        self.used: set[str] = set()
        self.ids: dict[str, dict[str, str]] = {name: {} for name, _ in _SECTIONS}
        self.attribute_ids: dict[tuple[str, str], dict[str, str]] = {}
        self.ends: dict[str, tuple[object, object]] = {}
        self.names: dict[str, str] = {}

    # Error collection ------------------------------------------------------------------------

    @contextmanager
    def collect(self) -> Any:
        try:
            yield
        except _LoadError as failure:
            self.problems.extend(failure.problems)

    def _yaml_key(self, part: object) -> _PathPart:
        if isinstance(part, int):
            return part
        text = str(part)
        return _YAML_KEYS.get(text, self.names.get(text, text))

    def build(self, model: type[_M], path: _Path, data: dict[str, Any], **fields: Any) -> _M:
        """Validate ``data`` plus the already resolved ``fields`` into a model."""
        for key in fields:
            if key in data:
                _fail((*path, key), f"unknown key {key!r}")
        try:
            return model.model_validate({**data, **fields})
        except ValidationError as exc:
            raise _LoadError(
                [
                    SchemaYamlProblem(
                        (*path, *(self._yaml_key(part) for part in error["loc"])), error["msg"]
                    )
                    for error in exc.errors()
                ]
            ) from exc

    # Pass 1: ids -----------------------------------------------------------------------------

    def _entries(self, value: object, path: _Path) -> dict[str, Any]:
        with self.collect():
            return _mapping(value, path)
        return {}

    def _claim(self, value: object, prefix: IdPrefix, path: _Path) -> str:
        if not isinstance(value, str) or id_prefix(value) is not prefix:
            self.problems.append(
                SchemaYamlProblem(path, f"expected an id starting with '{prefix.value}_'")
            )
        elif value in self.used:
            self.problems.append(SchemaYamlProblem(path, f"id {value} is used twice"))
        else:
            self.used.add(value)
            return value
        return self._mint(prefix)

    def _mint(self, prefix: IdPrefix) -> str:
        fresh = self.id_factory(prefix)
        self.used.add(fresh)
        return fresh

    def _assign(
        self,
        entries: dict[str, Any],
        prefix: IdPrefix,
        known: Mapping[str, str],
        path: _Path,
    ) -> dict[str, str]:
        """Ids by api name: explicit first, then those of ``known`` (the base), then new ones."""
        assigned: dict[str, str] = {}
        for name, entry in entries.items():
            if isinstance(entry, dict) and entry.get("id") is not None:
                assigned[name] = self._claim(entry["id"], prefix, (*path, name, "id"))
        for name in entries:
            if name in assigned:
                continue
            existing = known.get(name)
            if existing is not None and existing not in self.used:
                self.used.add(existing)
                assigned[name] = existing
            else:
                assigned[name] = self._mint(prefix)
        return {name: assigned[name] for name in entries}

    def assign_ids(self, root: dict[str, Any]) -> dict[str, dict[str, Any]]:
        entries = {
            section: self._entries(root.get(section), (section,)) for section, _ in _SECTIONS
        }
        for section, prefix in _SECTIONS:
            base_items = getattr(self.base, section, {}) if self.base is not None else {}
            known = {item.api_name: item.id for item in base_items.values()}
            self.ids[section] = self._assign(entries[section], prefix, known, (section,))
            for name, element_id in self.ids[section].items():
                self.names[element_id] = name
        for section in ("kinds", "relations", "traits"):
            base_items = getattr(self.base, section, {}) if self.base is not None else {}
            for name, entry in entries[section].items():
                owner = base_items.get(self.ids[section][name])
                known = (
                    {a.api_name: a.id for a in owner.attributes.values()}
                    if owner is not None
                    else {}
                )
                body = entry if isinstance(entry, dict) else {}
                attributes = self._entries(body.get("attributes"), (section, name, "attributes"))
                ids = self._assign(
                    attributes, IdPrefix.ATTRIBUTE, known, (section, name, "attributes")
                )
                self.attribute_ids[(section, name)] = ids
                self.names.update({attribute_id: n for n, attribute_id in ids.items()})
        for name, entry in entries["relations"].items():
            if isinstance(entry, dict):
                self.ends[name] = (entry.get("from_kind"), entry.get("to_kind"))
        for name, entry in entries["derivations"].items():
            ends = self._path_ends(entry)
            if ends is not None and name not in self.ends:
                self.ends[name] = ends
        return entries

    def _path_ends(self, entry: object) -> tuple[object, object] | None:
        """First near and last far Kind name of a `path` Derivation entry (an aggregate may
        read a path like a Relation); `None` when it is not one or its steps are unknown."""
        definition = entry.get("definition") if isinstance(entry, dict) else None
        if not isinstance(definition, dict) or definition.get("variant") != "path":
            return None
        steps = definition.get("steps")
        kinds: list[object] = []
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict) or str(step.get("relation")) not in self.ends:
                return None
            ends = self.ends[str(step["relation"])]
            inverse = step.get("direction") == LinkDirection.INVERSE
            kinds.append(ends[1] if inverse else ends[0])
            kinds.append(ends[0] if inverse else ends[1])
        return (kinds[0], kinds[-1]) if kinds else None

    # Reference resolution --------------------------------------------------------------------

    def lookup(self, section: str, name: object, path: _Path) -> str:
        table = self.ids[section]
        if not isinstance(name, str) or name not in table:
            label = {"kinds": "Kind", "relations": "Relation", "traits": "Trait"}.get(
                section, section[:-1].capitalize()
            )
            _fail(path, f"unknown {label} {name!r}")
        return table[name]

    def attribute(self, scope: tuple[str, str], name: object, path: _Path) -> str:
        table = self.attribute_ids.get(scope)
        if table is None:
            _fail(path, f"unknown {scope[0][:-1]} {scope[1]!r}")
        if not isinstance(name, str) or name not in table:
            _fail(path, f"unknown Attribute {name!r} of {scope[0][:-1]} {scope[1]!r}")
        return table[name]

    def rekey(self, value: object, scope: tuple[str, str], path: _Path) -> dict[str, Any]:
        """A mapping keyed by Attribute api name -> the same mapping keyed by Attribute id."""
        return {
            self.attribute(scope, name, (*path, name)): item
            for name, item in _mapping(value, path).items()
        }

    def type_spec(self, node: object, path: _Path) -> TypeSpec:
        try:
            return self._type_spec(node, path)
        except ValidationError as exc:
            _fail(path, "; ".join(error["msg"] for error in exc.errors()))

    def _type_spec(self, node: object, path: _Path) -> TypeSpec:
        if isinstance(node, str):
            text = node.strip()
            if match := _DECIMAL.fullmatch(text):
                return TypeSpec.decimal(int(match.group(1)), int(match.group(2)))
            if match := _REF.fullmatch(text):
                return TypeSpec.ref(self.lookup("kinds", match.group(1), path))
            if match := _ARRAY.fullmatch(text):
                return TypeSpec.array(self._type_spec(match.group(1), path))
            try:
                base = AttributeType(text)
            except ValueError:
                _fail(path, f"unknown type {text!r}")
            if base in _PARAMETERIZED:
                _fail(path, f"type {text!r} needs parameters, see the schema format notes")
            return TypeSpec.of(base)
        if isinstance(node, dict) and len(node) == 1:
            ((key, value),) = node.items()
            if key == "enum":
                values = _sequence(value, path)
                if not all(isinstance(v, str) for v in values):
                    _fail(path, "enum values must be text; quote numbers and booleans")
                return TypeSpec.enum(*values)
            if key == "array":
                return TypeSpec.array(self._type_spec(value, path))
        _fail(path, "a type is a name such as 'string', 'decimal(12,2)' or a {enum: [...]} mapping")

    def required_type(self, data: dict[str, Any], path: _Path) -> TypeSpec:
        if "type" not in data:
            _fail((*path, "type"), "Field required")
        return self.type_spec(data.pop("type"), (*path, "type"))

    # Pass 2: models --------------------------------------------------------------------------

    def attributes(
        self, value: object, scope: tuple[str, str], path: _Path, *, trait: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, entry in _mapping(value, path).items():
            with self.collect():
                item_path = (*path, name)
                data = _mapping(entry, item_path)
                data.pop("id", None)
                element_id = self.attribute_ids[scope][name]
                fields: dict[str, Any] = {
                    "id": element_id,
                    "api_name": name,
                    "type": self.required_type(data, item_path),
                }
                if trait:
                    result[element_id] = self.build(TraitAttribute, item_path, data, **fields)
                    continue
                if "derivation" in data:
                    fields["derivation_id"] = self.lookup(
                        "derivations", data.pop("derivation"), (*item_path, "derivation")
                    )
                if "authority" in data:
                    fields["authority"] = self.lookup(
                        "sources", data.pop("authority"), (*item_path, "authority")
                    )
                result[element_id] = self.build(Attribute, item_path, data, **fields)
        return result

    def kind(self, name: str, entry: object) -> Kind:
        path: _Path = ("kinds", name)
        scope = ("kinds", name)
        data = _mapping(entry, path)
        data.pop("id", None)
        fields: dict[str, Any] = {
            "id": self.ids["kinds"][name],
            "api_name": name,
            "attributes": self.attributes(
                data.pop("attributes", None), scope, (*path, "attributes")
            ),
        }
        if "primary_key" in data:
            key_path = (*path, "primary_key")
            fields["primary_key"] = tuple(
                self.attribute(scope, item, (*key_path, index))
                for index, item in enumerate(_names(data.pop("primary_key"), key_path))
            )
        if data.get("title_attribute") is not None:
            fields["title_attribute"] = self.attribute(
                scope, data.pop("title_attribute"), (*path, "title_attribute")
            )
        else:
            data.pop("title_attribute", None)
        if "traits" in data:
            traits_path = (*path, "traits")
            implementations = []
            for index, raw in enumerate(_sequence(data.pop("traits"), traits_path)):
                with self.collect():
                    implementations.append(self.trait_implementation(raw, scope, index, path))
            fields["traits"] = tuple(implementations)
        return self.build(Kind, path, data, **fields)

    def trait_implementation(
        self, raw: object, scope: tuple[str, str], index: int, kind_path: _Path
    ) -> TraitImplementation:
        path = (*kind_path, "traits", index)
        data = _mapping(raw, path)
        trait_name = data.pop("trait", None)
        trait_id = self.lookup("traits", trait_name, (*path, "trait"))
        mapping_path = (*path, "attribute_map")
        attribute_map = {
            self.attribute(("traits", str(trait_name)), key, (*mapping_path, key)): self.attribute(
                scope, target, (*mapping_path, key)
            )
            for key, target in _mapping(data.pop("attribute_map", None), mapping_path).items()
        }
        return self.build(
            TraitImplementation, path, data, trait_id=trait_id, attribute_map=attribute_map
        )

    def relation(self, name: str, entry: object) -> Relation:
        path: _Path = ("relations", name)
        scope = ("relations", name)
        data = _mapping(entry, path)
        data.pop("id", None)
        fields: dict[str, Any] = {
            "id": self.ids["relations"][name],
            "api_name": name,
            "attributes": self.attributes(
                data.pop("attributes", None), scope, (*path, "attributes")
            ),
        }
        for end in ("from_kind", "to_kind"):
            if end in data:
                fields[end] = self.lookup("kinds", data.pop(end), (*path, end))
        if data.get("foreign_key_attribute") is not None:
            from_kind = self.ends.get(name, (None, None))[0]
            fields["foreign_key_attribute"] = self.attribute(
                ("kinds", str(from_kind)),
                data.pop("foreign_key_attribute"),
                (*path, "foreign_key_attribute"),
            )
        else:
            data.pop("foreign_key_attribute", None)
        return self.build(Relation, path, data, **fields)

    def trait(self, name: str, entry: object) -> Trait:
        path: _Path = ("traits", name)
        data = _mapping(entry, path)
        data.pop("id", None)
        attributes = self.attributes(
            data.pop("attributes", None), ("traits", name), (*path, "attributes"), trait=True
        )
        return self.build(
            Trait, path, data, id=self.ids["traits"][name], api_name=name, attributes=attributes
        )

    def operation(self, name: str, entry: object) -> Operation:
        path: _Path = ("operations", name)
        data = _mapping(entry, path)
        data.pop("id", None)
        fields: dict[str, Any] = {"id": self.ids["operations"][name], "api_name": name}
        if "parameters" in data:
            parameters_path = (*path, "parameters")
            parameters = []
            for index, raw in enumerate(_sequence(data.pop("parameters"), parameters_path)):
                with self.collect():
                    item_path = (*parameters_path, index)
                    item = _mapping(raw, item_path)
                    type_spec = self.required_type(item, item_path)
                    parameters.append(
                        self.build(OperationParameter, item_path, item, type=type_spec)
                    )
            fields["parameters"] = tuple(parameters)
        if "steps" in data:
            steps_path = (*path, "steps")
            steps = []
            for index, raw in enumerate(_sequence(data.pop("steps"), steps_path)):
                with self.collect():
                    steps.append(self.step(raw, (*steps_path, index)))
            fields["steps"] = tuple(steps)
        return self.build(Operation, path, data, **fields)

    def step(self, raw: object, path: _Path) -> OperationStep:
        data = _mapping(raw, path)
        step_name = data.pop("step", None)
        model = _STEP_MODELS.get(step_name) if isinstance(step_name, str) else None
        if model is None:
            _fail((*path, "step"), f"unknown step {step_name!r}; use {', '.join(_STEP_MODELS)}")
        fields: dict[str, Any] = {"step": step_name}
        scope: tuple[str, str] | None = None
        if "kind" in model.model_fields:
            kind_name = data.pop("kind", None)
            fields["kind"] = self.lookup("kinds", kind_name, (*path, "kind"))
            scope = ("kinds", str(kind_name))
        else:
            relation_name = data.pop("relation", None)
            fields["relation"] = self.lookup("relations", relation_name, (*path, "relation"))
            scope = ("relations", str(relation_name))
        if "values" in data:
            fields["values"] = self.rekey(data.pop("values"), scope, (*path, "values"))
        return cast(OperationStep, self.build(model, path, data, **fields))

    def derivation(self, name: str, entry: object) -> Derivation:
        path: _Path = ("derivations", name)
        data = _mapping(entry, path)
        data.pop("id", None)
        definition_path = (*path, "definition")
        definition = self.definition(
            _mapping(data.pop("definition", None), definition_path), definition_path
        )
        return self.build(
            Derivation,
            path,
            data,
            id=self.ids["derivations"][name],
            api_name=name,
            definition=definition,
        )

    def definition(self, data: dict[str, Any], path: _Path) -> Model:
        variant = data.pop("variant", None)
        fields: dict[str, Any] = {"variant": variant}
        if variant in ("expression", "aggregate"):
            kind_name = data.pop("kind", None)
            fields["kind"] = self.lookup("kinds", kind_name, (*path, "kind"))
            fields["attribute"] = self.attribute(
                ("kinds", str(kind_name)), data.pop("attribute", None), (*path, "attribute")
            )
        if variant == "expression":
            return self.build(ExpressionDerivation, path, data, **fields)
        if variant == "aggregate":
            relation_name = data.pop("relation", None)
            fields["relation"] = (
                self.ids["derivations"][relation_name]
                if isinstance(relation_name, str)
                and relation_name not in self.ids["relations"]
                and relation_name in self.ids["derivations"]
                else self.lookup("relations", relation_name, (*path, "relation"))
            )  # a path Derivation is read like a Relation
            if data.get("source_attribute") is not None:
                far_end = 0 if data.get("direction") == LinkDirection.INVERSE else 1
                far = self.ends.get(str(relation_name), (None, None))[far_end]
                fields["source_attribute"] = self.attribute(
                    ("kinds", str(far)), data.pop("source_attribute"), (*path, "source_attribute")
                )
            else:
                data.pop("source_attribute", None)
            return self.build(AggregateDerivation, path, data, **fields)
        if variant == "path":
            steps_path = (*path, "steps")
            steps = []
            for index, raw in enumerate(_sequence(data.pop("steps", None), steps_path)):
                with self.collect():
                    item_path = (*steps_path, index)
                    item = _mapping(raw, item_path)
                    relation = self.lookup(
                        "relations", item.pop("relation", None), (*item_path, "relation")
                    )
                    steps.append(self.build(PathStep, item_path, item, relation=relation))
            return self.build(PathDerivation, path, data, steps=tuple(steps), **fields)
        _fail((*path, "variant"), "variant must be 'expression', 'aggregate' or 'path'")

    def source(self, name: str, entry: object) -> Source:
        path: _Path = ("sources", name)
        data = _mapping(entry, path)
        data.pop("id", None)
        fields: dict[str, Any] = {"id": self.ids["sources"][name], "api_name": name}
        if "target_kind" in data:
            kind_name = data.pop("target_kind")
            fields["target_kind"] = self.lookup("kinds", kind_name, (*path, "target_kind"))
            if "attribute_mapping" in data:
                fields["attribute_mapping"] = self.rekey(
                    data.pop("attribute_mapping"),
                    ("kinds", str(kind_name)),
                    (*path, "attribute_mapping"),
                )
        if "relation_mapping" in data:
            mapping_path = (*path, "relation_mapping")
            mappings = []
            for index, raw in enumerate(_sequence(data.pop("relation_mapping"), mapping_path)):
                with self.collect():
                    item_path = (*mapping_path, index)
                    item = _mapping(raw, item_path)
                    relation = self.lookup(
                        "relations", item.pop("relation", None), (*item_path, "relation")
                    )
                    mappings.append(self.build(RelationMapping, item_path, item, relation=relation))
            fields["relation_mapping"] = tuple(mappings)
        return self.build(Source, path, data, **fields)

    def lens(self, name: str, entry: object) -> Lens:
        path: _Path = ("lenses", name)
        data = _mapping(entry, path)
        data.pop("id", None)
        fields: dict[str, Any] = {"id": self.ids["lenses"][name], "api_name": name}
        for key, section in (("kinds", "kinds"), ("traits", "traits"), ("relations", "relations")):
            if key in data:
                names_path = (*path, key)
                fields[key] = tuple(
                    self.lookup(section, item, (*names_path, index))
                    for index, item in enumerate(_names(data.pop(key), names_path))
                )
        return self.build(Lens, path, data, **fields)

    def read(self, document: object) -> Schema:
        with self.collect():
            root = _mapping(document, ())
            unknown = sorted(set(root) - _ROOT_KEYS)
            for key in unknown:
                self.problems.append(SchemaYamlProblem((key,), "unknown section"))
            declared = root.get("format")
            if declared is not None and declared != SCHEMA_FORMAT:
                self.problems.append(
                    SchemaYamlProblem(("format",), f"unsupported format, expected {SCHEMA_FORMAT}")
                )
            entries = self.assign_ids(root)
            domain: Domain | None = None
            with self.collect():
                domain = self.build(Domain, ("domain",), _mapping(root.get("domain"), ("domain",)))
            builders: dict[str, Callable[[str, object], SchemaElement]] = {
                "kinds": self.kind,
                "relations": self.relation,
                "traits": self.trait,
                "operations": self.operation,
                "derivations": self.derivation,
                "sources": self.source,
                "lenses": self.lens,
            }
            built: dict[str, dict[str, Any]] = {name: {} for name, _ in _SECTIONS}
            for section, _ in _SECTIONS:
                for name, entry in entries[section].items():
                    with self.collect():
                        element = builders[section](name, entry)
                        built[section][element.id] = element
            if not self.problems and domain is not None:
                return Schema(domain=domain, **built)
        raise SchemaYamlError(self.problems)


def schema_from_document(
    document: object, *, base: Schema | None = None, id_factory: IdFactory = new_id
) -> Schema:
    """Build a ``Schema`` from a parsed document; raises ``SchemaYamlError``."""
    return _Reader(base, id_factory).read(document)


def load_schema(text: str, *, base: Schema | None = None, id_factory: IdFactory = new_id) -> Schema:
    """Load a Schema from YAML text.

    ``base`` is the schema the YAML evolves: elements without an ``id`` that share an api name
    with a base element keep the base id. ``id_factory`` mints ids for new elements.
    """
    return schema_from_document(parse_yaml(text), base=base, id_factory=id_factory)


# Dumper ---------------------------------------------------------------------------------------


def id_names(schema: Schema) -> dict[str, str]:
    """Every element and Attribute id of ``schema`` -> its api name (Domain excluded)."""
    names: dict[str, str] = {}
    for element in schema.elements():
        names[element.id] = element.api_name
        if isinstance(element, Kind | Relation | Trait):
            for attribute in element.attributes.values():
                names[attribute.id] = attribute.api_name
    return names


def _plain(model: BaseModel, exclude: set[str]) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude=exclude)


def _compose(
    element_id: str, plain: dict[str, Any], extras: dict[str, Any], order: tuple[str, ...] = ()
) -> dict[str, Any]:
    """``id``, naming fields, then ``order``ed keys, then the remaining fields."""
    pool = {**plain, **{key: value for key, value in extras.items() if value not in (None, {}, [])}}
    out: dict[str, Any] = {"id": element_id}
    for key in ("display_name", "description", *order):
        if key in pool:
            out[key] = pool.pop(key)
    out.update(pool)
    return out


class _Writer:
    def __init__(self, schema: Schema) -> None:
        self.schema = schema
        self.names = id_names(schema)

    def name(self, element_id: str | None) -> str | None:
        return None if element_id is None else self.names.get(element_id, element_id)

    def attributes(self, attributes: Mapping[str, Attribute | TraitAttribute]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attribute in attributes.values():
            extras: dict[str, Any] = {"type": _type_node(attribute.type, self.names)}
            exclude = {"id", "api_name", "type"}
            if isinstance(attribute, Attribute):
                exclude |= {"derivation_id", "authority"}
                extras["derivation"] = self.name(attribute.derivation_id)
                extras["authority"] = self.name(attribute.authority)
            result[attribute.api_name] = _compose(
                attribute.id, _plain(attribute, exclude), extras, ("type",)
            )
        return result

    def kind(self, kind: Kind) -> dict[str, Any]:
        extras = {
            "primary_key": [self.name(a) for a in kind.primary_key],
            "title_attribute": self.name(kind.title_attribute),
            "attributes": self.attributes(kind.attributes),
            "traits": [
                {
                    "trait": self.name(impl.trait_id),
                    "attribute_map": {
                        self.names.get(k, k): self.names.get(v, v)
                        for k, v in impl.attribute_map.items()
                    },
                }
                for impl in kind.traits
            ],
        }
        plain = _plain(
            kind, {"id", "api_name", "primary_key", "title_attribute", "attributes", "traits"}
        )
        return _compose(
            kind.id, plain, extras, ("primary_key", "title_attribute", "attributes", "traits")
        )

    def relation(self, relation: Relation) -> dict[str, Any]:
        exclude = {"id", "api_name", "from_kind", "to_kind", "attributes", "foreign_key_attribute"}
        extras = {
            "from_kind": self.name(relation.from_kind),
            "to_kind": self.name(relation.to_kind),
            "foreign_key_attribute": self.name(relation.foreign_key_attribute),
            "attributes": self.attributes(relation.attributes),
        }
        order = ("inverse_api_name", "from_kind", "to_kind", "cardinality", "required_from")
        return _compose(relation.id, _plain(relation, exclude), extras, order)

    def trait(self, trait: Trait) -> dict[str, Any]:
        extras = {"attributes": self.attributes(trait.attributes)}
        return _compose(trait.id, _plain(trait, {"id", "api_name", "attributes"}), extras)

    def step(self, step: OperationStep) -> dict[str, Any]:
        out: dict[str, Any] = {"step": step.step}
        if isinstance(step, CreateRecordStep | UpdateRecordStep | DeleteRecordStep):
            out["kind"] = self.name(step.kind)
        else:
            out["relation"] = self.name(step.relation)
        if isinstance(step, UpdateRecordStep | DeleteRecordStep):
            out["target"] = step.target
        if isinstance(step, CreateLinkStep | DeleteLinkStep):
            out["from"] = step.from_
            out["to"] = step.to
        if not isinstance(step, DeleteRecordStep | DeleteLinkStep) and step.values:
            out["values"] = {self.names.get(k, k): v for k, v in step.values.items()}
        return out

    def operation(self, operation: Operation) -> dict[str, Any]:
        parameters = [
            _compose_parameter(parameter, _type_node(parameter.type, self.names))
            for parameter in operation.parameters
        ]
        extras = {"parameters": parameters, "steps": [self.step(s) for s in operation.steps]}
        plain = _plain(operation, {"id", "api_name", "parameters", "steps"})
        order = ("parameters", "preconditions", "steps", "review_policy", "permissions")
        return _compose(operation.id, plain, extras, order)

    def derivation(self, derivation: Derivation) -> dict[str, Any]:
        definition = derivation.definition
        out: dict[str, Any] = {"variant": definition.variant}
        if isinstance(definition, ExpressionDerivation | AggregateDerivation):
            out["kind"] = self.name(definition.kind)
            out["attribute"] = self.name(definition.attribute)
        if isinstance(definition, ExpressionDerivation):
            out["expression"] = definition.expression
        elif isinstance(definition, AggregateDerivation):
            out["relation"] = self.name(definition.relation)
            if definition.direction is not LinkDirection.FORWARD:
                out["direction"] = definition.direction.value
            out["function"] = definition.function.value
            if definition.source_attribute is not None:
                out["source_attribute"] = self.name(definition.source_attribute)
            if definition.filter is not None:
                out["filter"] = definition.filter
        else:
            out["steps"] = [
                {"relation": self.name(step.relation)}
                | (
                    {}
                    if step.direction is LinkDirection.FORWARD
                    else {"direction": step.direction.value}
                )
                for step in definition.steps
            ]
            if definition.inverse_api_name is not None:
                out["inverse_api_name"] = definition.inverse_api_name
            if definition.materialized:
                out["materialized"] = True
        plain = _plain(derivation, {"id", "api_name", "definition"})
        return _compose(derivation.id, plain, {"definition": out}, ("definition",))

    def source(self, source: Source) -> dict[str, Any]:
        extras = {
            "target_kind": self.name(source.target_kind),
            "attribute_mapping": {
                self.names.get(k, k): _plain(v, set()) for k, v in source.attribute_mapping.items()
            },
            "relation_mapping": [
                {"relation": self.name(m.relation)} | _plain(m, {"relation"})
                for m in source.relation_mapping
            ],
        }
        exclude = {"id", "api_name", "target_kind", "attribute_mapping", "relation_mapping"}
        order = ("type", "connection", "target_kind", "key_mapping", "attribute_mapping")
        return _compose(source.id, _plain(source, exclude), extras, order)

    def lens(self, lens: Lens) -> dict[str, Any]:
        extras = {
            "kinds": [self.name(k) for k in lens.kinds],
            "traits": [self.name(t) for t in lens.traits],
            "relations": [self.name(r) for r in lens.relations],
        }
        plain = _plain(lens, {"id", "api_name", "kinds", "traits", "relations"})
        return _compose(lens.id, plain, extras, ("kinds", "traits", "relations"))

    def document(self) -> dict[str, Any]:
        schema = self.schema
        document: dict[str, Any] = {
            "format": SCHEMA_FORMAT,
            "domain": _plain(schema.domain, set()),
        }
        sections: dict[str, Iterable[SchemaElement]] = {
            "kinds": schema.kinds.values(),
            "relations": schema.relations.values(),
            "traits": schema.traits.values(),
            "operations": schema.operations.values(),
            "derivations": schema.derivations.values(),
            "sources": schema.sources.values(),
            "lenses": schema.lenses.values(),
        }
        writers: dict[str, Callable[[Any], dict[str, Any]]] = {
            "kinds": self.kind,
            "relations": self.relation,
            "traits": self.trait,
            "operations": self.operation,
            "derivations": self.derivation,
            "sources": self.source,
            "lenses": self.lens,
        }
        for section, elements in sections.items():
            items = {element.api_name: writers[section](element) for element in elements}
            if items:
                document[section] = items
        return document


def _compose_parameter(
    parameter: OperationParameter, type_node: str | dict[str, Any]
) -> dict[str, Any]:
    plain = _plain(parameter, {"name", "type"})
    return {"name": parameter.name, "type": type_node, **plain}


def schema_to_document(schema: Schema) -> dict[str, Any]:
    """The document (plain dicts and lists) that ``dump_schema`` renders as YAML."""
    return _Writer(schema).document()


def dump_schema(schema: Schema) -> str:
    """Render a Schema as YAML text. Ids are always written; defaults are omitted."""
    return yaml.dump(
        schema_to_document(schema),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )


# Validation with YAML paths ----------------------------------------------------------------


def yaml_path(schema: Schema, path: Iterable[str | int]) -> tuple[str | int, ...]:
    """Translate an issue path (ids) into the path of the same spot in the YAML document."""
    return _translate(id_names(schema), path)


def _translate(names: Mapping[str, str], path: Iterable[str | int]) -> tuple[str | int, ...]:
    return tuple(
        part if isinstance(part, int) else _YAML_KEYS.get(part, names.get(part, part))
        for part in path
    )


def locate_issues(schema: Schema, issues: Iterable[Issue]) -> list[Issue]:
    """Copies of ``issues`` whose ``path`` and element references use YAML names."""
    names = id_names(schema)
    located = []
    for issue in issues:
        update: dict[str, Any] = {"path": _translate(names, issue.path)}
        for field in ("kind", "relation", "attribute"):
            value = getattr(issue, field)
            if value is not None:
                update[field] = names.get(value, value)
        located.append(issue.model_copy(update=update))
    return located


def validate_schema_yaml(
    text: str,
    *,
    base: Schema | None = None,
    rules: SchemaRules | None = None,
    id_factory: IdFactory = new_id,
) -> list[Issue]:
    """Load ``text`` and run static validation; issue paths point into the YAML.

    Raises ``SchemaYamlError`` when the document cannot be loaded at all.
    """
    from lorebase_core.validate.schema_rules import validate_schema

    schema = load_schema(text, base=base, id_factory=id_factory)
    return locate_issues(schema, validate_schema(schema, rules))
