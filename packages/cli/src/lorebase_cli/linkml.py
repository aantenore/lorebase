"""LinkML interchange for schema documents.

`to_linkml` turns a Lorebase schema document (`lorebase.schema/v1`, parsed YAML) into a LinkML
schema; `from_linkml` turns a LinkML schema into a Lorebase schema document plus warnings about
what it could not keep. Both work on parsed YAML: the part of the LinkML metamodel they use
(classes, slots, attributes, slot_usage, mixins, enums, types, unique keys, annotations) is small,
so no LinkML library is needed. Reading and writing YAML needs PyYAML, an optional dependency
(`pip install "lorebase-cli[linkml]"`).

Mapping, both ways:

| Lorebase | LinkML |
|----------|--------|
| Domain | schema (`name`, `title`, `description`) |
| Kind | class |
| Trait | mixin class (`mixin: true`), Kinds list it in `mixins` |
| Attribute | class attribute (an inline slot) |
| type | `range`; arrays are `multivalued`; enums are enums |
| `required`, constraints, `default` | `required`, `minimum_value` and friends, `ifabsent` |
| primary key | `identifier: true` (one Attribute) or the unique key `primary_key` |
| `unique` | a unique key of one slot |
| Relation | a slot with a class range on each side, linked by `inverse` |
| `cardinality` | `multivalued` of the two slots |
| `id`, sensitivity and every other field | annotations `lorebase.<field>` |
| Operations, Derivations, Sources | schema annotations on export; skipped on import |

Annotation values are scalars; structured values are written as JSON text and read back as
JSON when the text is a JSON object or array.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Value of the `format` key of a Lorebase schema document.
LOREBASE_FORMAT = "lorebase.schema/v1"
#: Prefix of the annotation tags that carry Lorebase fields.
ANNOTATION_PREFIX = "lorebase."
#: Namespace and import of the LinkML built-in types.
LINKML_NAMESPACE = "https://w3id.org/linkml/"
LINKML_TYPES_IMPORT = "linkml:types"

#: Sections of a Lorebase document kept only as annotations in LinkML.
EXPORT_ONLY_SECTIONS = ("operations", "derivations", "sources")
#: Attribute fields that name elements of those sections, dropped on import.
_SECTION_REFERENCES = {"derivation": "derivations", "authority": "sources"}
_LOREBASE_ROOT_KEYS = frozenset(
    {"format", "domain", "kinds", "relations", "traits", *EXPORT_ONLY_SECTIONS}
)

#: Lorebase types with a LinkML range of the same meaning.
_NATIVE_RANGES = {
    "string": "string",
    "uri": "uri",
    "integer": "integer",
    "float": "float",
    "boolean": "boolean",
    "date": "date",
    "timestamp": "datetime",
}
#: LinkML built-in types and their Lorebase types.
_LINKML_TYPES = {
    "string": "string",
    "integer": "integer",
    "boolean": "boolean",
    "float": "float",
    "double": "float",
    "date": "date",
    "datetime": "timestamp",
    "uri": "uri",
    "uriorcurie": "uri",
    "curie": "string",
    "ncname": "string",
    "objectidentifier": "string",
    "nodeidentifier": "string",
    "jsonpointer": "string",
    "jsonpath": "string",
    "sparqlpath": "string",
}
#: LinkML built-in types without an exact Lorebase type: the closest one.
_APPROXIMATE_LINKML_TYPES = {"time": "string", "date_or_datetime": "timestamp"}

_DECIMAL = re.compile(r"decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_REF = re.compile(r"ref\(\s*([^()\s]+)\s*\)")
_ARRAY = re.compile(r"array<(.+)>")
_IFABSENT = re.compile(r"(string|int|float|decimal|date|datetime)\((.*)\)", re.DOTALL)
#: Constraints with a LinkML slot field of the same meaning.
_CONSTRAINT_SLOTS = (
    ("min", "minimum_value"),
    ("max", "maximum_value"),
    ("pattern", "pattern"),
    ("enum_values", "equals_string_in"),
)
#: Length constraints: LinkML cardinalities on arrays, annotations otherwise.
_LENGTH_SLOTS = (("min_length", "minimum_cardinality"), ("max_length", "maximum_cardinality"))
_PRIMARY_KEY = "primary_key"
#: Long annotation values (JSON text) stay on one line.
_LINE_WIDTH = 2**31
_INVERSE_MARK = "inverse"


@dataclass(frozen=True)
class LinkmlOptions:
    """Choices the mapping cannot read from its input.

    `schema_uri` is the `id` of exported schemas (`{domain}` is the Domain id). LinkML
    `decimal` has no precision: imports without a `lorebase.type` annotation use
    `decimal_precision` and `decimal_scale`. `default_range` applies when the LinkML schema
    sets none.
    """

    schema_uri: str = "urn:lorebase:schema:{domain}"
    decimal_precision: int = 38
    decimal_scale: int = 9
    default_range: str = "string"


@dataclass(frozen=True)
class ConversionWarning:
    """Something of the input that the output does not keep; `path` locates it in the input."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class LinkmlError(ValueError):
    """The input cannot be converted at all."""


@dataclass
class Conversion:
    """A converted document and what got lost on the way."""

    document: dict[str, Any]
    warnings: list[ConversionWarning] = field(default_factory=list)


# YAML -----------------------------------------------------------------------------------------


def load_yaml(text: str) -> Any:
    """Parse YAML text (safe loader)."""
    _require_yaml()
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise LinkmlError(f"invalid YAML: {error}") from error


def dump_yaml(document: Mapping[str, Any]) -> str:
    """Block style YAML in document order; empty values (enum permissible values) print blank."""
    _require_yaml()
    import yaml

    class Dumper(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    def blank(dumper: yaml.SafeDumper, _: None) -> yaml.ScalarNode:
        return dumper.represent_scalar("tag:yaml.org,2002:null", "")

    Dumper.add_representer(type(None), blank)
    return str(
        yaml.dump(
            dict(document), Dumper=Dumper, sort_keys=False, allow_unicode=True, width=_LINE_WIDTH
        )
    )


def _require_yaml() -> None:
    try:
        import yaml  # noqa: F401
    except ImportError:  # pragma: no cover - installed in the workspace
        raise LinkmlError("LinkML needs PyYAML: pip install 'lorebase-cli[linkml]'") from None


# Shared helpers -------------------------------------------------------------------------------


def _encode(value: Any) -> Any:
    """Annotation value: scalars as they are, anything else as JSON text."""
    if isinstance(value, str | bool | int | float):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _annotations(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        ANNOTATION_PREFIX + key: _encode(value)
        for key, value in fields.items()
        if value is not None
    }


def _tags(annotations: Any) -> set[str]:
    """Tags of LinkML annotations, written as a mapping or as a list of tag and value."""
    if isinstance(annotations, Mapping):
        return {str(tag) for tag in annotations}
    if isinstance(annotations, list):
        return {str(item.get("tag")) for item in annotations if isinstance(item, Mapping)}
    return set()


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, "", [], {}):
        target[key] = value


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LinkmlError(f"{path}: expected a mapping")
    return dict(value)


def snake_name(name: str) -> str:
    """`hasEmploymentHistory`, `has employment history` -> `has_employment_history`."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name).strip())
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not text or not text[0].isalpha():
        text = f"slot_{text}".rstrip("_")
    return text


def pascal_name(name: str) -> str:
    """`named thing`, `named_thing` -> `NamedThing`; already Pascal names stay as they are."""
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", str(name)) if part]
    text = "".join(part[:1].upper() + part[1:] for part in parts)
    if not text or not text[0].isalpha():
        text = f"C{text}"
    return text


@dataclass(frozen=True)
class _Type:
    """A parsed Lorebase type node."""

    name: str
    enum: tuple[str, ...] = ()
    target: str = ""
    element: _Type | None = None

    @property
    def scalar(self) -> _Type:
        return self.element if self.element is not None else self


def _parse_type(node: Any, path: str) -> _Type:
    if isinstance(node, Mapping) and len(node) == 1:
        ((key, value),) = node.items()
        if key == "enum" and isinstance(value, list):
            return _Type("enum", enum=tuple(str(item) for item in value))
        if key == "array":
            return _Type("array", element=_parse_type(value, path))
    if isinstance(node, str):
        text = node.strip()
        if match := _ARRAY.fullmatch(text):
            return _Type("array", element=_parse_type(match[1], path))
        if match := _REF.fullmatch(text):
            return _Type("ref", target=match[1])
        if _DECIMAL.fullmatch(text):
            return _Type("decimal")
        return _Type(text)
    raise LinkmlError(f"{path}: unrecognised type {node!r}")


def _ifabsent(value: Any) -> str | None:
    """LinkML `ifabsent` of a scalar default, None for other values."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return f"int({value})"
    if isinstance(value, float):
        return f"float({value!r})"
    if isinstance(value, str):
        return f"string({value})"
    return None


# Export ---------------------------------------------------------------------------------------


def to_linkml(document: Any, options: LinkmlOptions | None = None) -> dict[str, Any]:
    """The LinkML schema of a Lorebase schema document. Nothing is lost: what LinkML cannot
    express travels as `lorebase.*` annotations."""
    return _Exporter(document, options or LinkmlOptions()).schema()


class _Exporter:
    def __init__(self, document: Any, options: LinkmlOptions) -> None:
        if not isinstance(document, Mapping):
            raise LinkmlError("a Lorebase schema document is a mapping")
        found = document.get("format", LOREBASE_FORMAT)
        if found != LOREBASE_FORMAT:
            raise LinkmlError(f"format: expected {LOREBASE_FORMAT}, found {found!r}")
        unknown = sorted(str(key) for key in document if key not in _LOREBASE_ROOT_KEYS)
        if unknown:
            raise LinkmlError(f"unknown sections: {', '.join(unknown)}")
        self.document = document
        self.options = options
        self.kinds = _mapping(document.get("kinds"), "kinds")
        self.traits = _mapping(document.get("traits"), "traits")
        self.relations = _mapping(document.get("relations"), "relations")
        self.enums: dict[str, dict[str, Any]] = {}

    def schema(self) -> dict[str, Any]:
        domain = _mapping(self.document.get("domain"), "domain")
        name = domain.pop("id", None)
        if not isinstance(name, str) or not name:
            raise LinkmlError("domain: no id")
        uri = self.options.schema_uri.format(domain=name)
        schema: dict[str, Any] = {"id": uri, "name": name}
        _put(schema, "title", domain.pop("display_name", None))
        _put(schema, "description", domain.pop("description", None))
        namespace = uri if uri.endswith(("/", "#")) else f"{uri}/"
        schema["prefixes"] = {"linkml": LINKML_NAMESPACE, name: namespace}
        schema["default_prefix"] = name
        schema["default_range"] = "string"
        schema["imports"] = [LINKML_TYPES_IMPORT]
        annotations = {ANNOTATION_PREFIX + "format": LOREBASE_FORMAT, **_annotations(domain)}
        for section in EXPORT_ONLY_SECTIONS:
            if self.document.get(section):
                annotations[ANNOTATION_PREFIX + section] = _encode(self.document[section])
        schema["annotations"] = annotations

        classes: dict[str, dict[str, Any]] = {}
        for kind_name, kind in self.kinds.items():
            classes[kind_name] = self.kind_class(kind_name, _mapping(kind, f"kinds/{kind_name}"))
        for trait_name, trait in self.traits.items():
            path = f"traits/{trait_name}"
            classes[trait_name] = self.trait_class(trait_name, _mapping(trait, path))
        for relation_name, relation in self.relations.items():
            path = f"relations/{relation_name}"
            self.relation_slots(relation_name, _mapping(relation, path), classes, path)
        _put(schema, "classes", classes)
        _put(schema, "enums", self.enums)
        return schema

    def kind_class(self, name: str, kind: dict[str, Any]) -> dict[str, Any]:
        path = f"kinds/{name}"
        cls: dict[str, Any] = {}
        annotations: dict[str, Any] = {}
        _put(annotations, ANNOTATION_PREFIX + "id", kind.pop("id", None))
        _put(cls, "description", kind.pop("description", None))
        _put(cls, "title", kind.pop("display_name", None))
        primary_key = list(kind.pop("primary_key", None) or [])
        traits = list(kind.pop("traits", None) or [])
        attributes = _mapping(kind.pop("attributes", None), f"{path}/attributes")
        if traits:
            cls["mixins"] = [str(_mapping(item, f"{path}/traits").get("trait")) for item in traits]
            if not self.identity_traits(traits, attributes):
                annotations[ANNOTATION_PREFIX + "traits"] = _encode(traits)
        slots: dict[str, Any] = {}
        unique_keys: dict[str, Any] = {}
        if len(primary_key) > 1:
            unique_keys[_PRIMARY_KEY] = {"unique_key_slots": primary_key}
        for attribute_name, attribute in attributes.items():
            identifier = primary_key == [attribute_name]
            slot, unique = self.attribute_slot(
                name, attribute_name, attribute, f"{path}/attributes/{attribute_name}", identifier
            )
            slots[attribute_name] = slot
            if unique:
                unique_keys[f"{attribute_name}_unique"] = {"unique_key_slots": [attribute_name]}
        annotations.update(_annotations(kind))
        cls["attributes"] = slots
        _put(cls, "unique_keys", unique_keys)
        cls["annotations"] = annotations
        return cls

    def identity_traits(self, traits: list[Any], attributes: Mapping[str, Any]) -> bool:
        """Whether `mixins` alone say it all: every map pairs equal names present in the Kind."""
        for item in traits:
            if not isinstance(item, Mapping) or set(item) - {"trait", "attribute_map"}:
                return False
            trait = self.traits.get(str(item.get("trait")))
            names = _mapping(trait, "traits").get("attributes") or {}
            expected = {name: name for name in names if name in attributes}
            if dict(item.get("attribute_map") or {}) != expected:
                return False
        return True

    def trait_class(self, name: str, trait: dict[str, Any]) -> dict[str, Any]:
        path = f"traits/{name}"
        cls: dict[str, Any] = {}
        annotations: dict[str, Any] = {}
        _put(annotations, ANNOTATION_PREFIX + "id", trait.pop("id", None))
        _put(cls, "description", trait.pop("description", None))
        _put(cls, "title", trait.pop("display_name", None))
        cls["mixin"] = True
        slots = {}
        attributes = _mapping(trait.pop("attributes", None), f"{path}/attributes")
        for attribute_name, attribute in attributes.items():
            attribute_path = f"{path}/attributes/{attribute_name}"
            slots[attribute_name], _ = self.attribute_slot(
                name, attribute_name, attribute, attribute_path, False
            )
        annotations.update(_annotations(trait))
        cls["attributes"] = slots
        cls["annotations"] = annotations
        return cls

    def attribute_slot(
        self, owner: str, name: str, attribute: Any, path: str, identifier: bool
    ) -> tuple[dict[str, Any], bool]:
        """The slot of an Attribute and whether the Attribute is unique."""
        fields = _mapping(attribute, path)
        slot: dict[str, Any] = {}
        annotations: dict[str, Any] = {}
        _put(annotations, ANNOTATION_PREFIX + "id", fields.pop("id", None))
        _put(slot, "description", fields.pop("description", None))
        _put(slot, "title", fields.pop("display_name", None))
        if "type" not in fields:
            raise LinkmlError(f"{path}: no type")
        node = fields.pop("type")
        parsed = _parse_type(node, path)
        scalar = parsed.scalar
        if scalar.name == "enum":
            slot["range"] = self.enum_name(owner, name, scalar.enum)
        elif scalar.name in _NATIVE_RANGES:
            slot["range"] = _NATIVE_RANGES[scalar.name]
        else:  # the closest range, the exact type in an annotation
            slot["range"] = {"decimal": "decimal", "ref": scalar.target}.get(scalar.name, "string")
            annotations[ANNOTATION_PREFIX + "type"] = _encode(node)
        if parsed.name == "array":
            slot["multivalued"] = True
        required = bool(fields.pop("required", False))
        if required or identifier:
            slot["required"] = required
        if identifier:
            slot["identifier"] = True
        unique = bool(fields.pop("unique", False))
        if "default" in fields:
            default = fields.pop("default")
            ifabsent = _ifabsent(default)
            if ifabsent is not None:
                slot["ifabsent"] = ifabsent
            elif default is not None:
                annotations[ANNOTATION_PREFIX + "default"] = _encode(default)
        constraints = _mapping(fields.pop("constraints", None), f"{path}/constraints")
        for key, target in _CONSTRAINT_SLOTS:
            if constraints.get(key) is not None:
                slot[target] = constraints.pop(key)
        for key, target in _LENGTH_SLOTS:
            if constraints.get(key) is not None:
                value = constraints.pop(key)
                if parsed.name == "array":
                    slot[target] = value
                else:
                    annotations[ANNOTATION_PREFIX + key] = value
        if constraints:
            annotations[ANNOTATION_PREFIX + "constraints"] = _encode(constraints)
        sensitivity = fields.pop("sensitivity", None)
        if sensitivity:
            annotations[ANNOTATION_PREFIX + "sensitivity"] = ",".join(map(str, sensitivity))
        annotations.update(_annotations(fields))
        _put(slot, "annotations", annotations)
        return slot, unique

    def enum_name(self, owner: str, attribute: str, values: tuple[str, ...]) -> str:
        """A LinkML enum per Attribute, `OwnerAttribute`; equal value lists share a name."""
        base = f"{owner}{pascal_name(attribute)}"
        candidate, number = base, 1
        taken = set(self.kinds) | set(self.traits)
        while candidate in taken or (
            candidate in self.enums and tuple(self.enums[candidate]["permissible_values"]) != values
        ):
            number += 1
            candidate = f"{base}{number}"
        self.enums[candidate] = {"permissible_values": dict.fromkeys(values)}
        return candidate

    def relation_slots(
        self, name: str, relation: dict[str, Any], classes: dict[str, dict[str, Any]], path: str
    ) -> None:
        from_kind = str(relation.pop("from_kind", ""))
        to_kind = str(relation.pop("to_kind", ""))
        inverse = str(relation.pop("inverse_api_name", ""))
        if not inverse:
            raise LinkmlError(f"{path}: no inverse_api_name")
        cardinality = str(relation.pop("cardinality", "many_to_many"))
        many_from, _, many_to = cardinality.partition("_to_")
        if {many_from, many_to} - {"one", "many"}:
            raise LinkmlError(f"{path}: unknown cardinality {cardinality!r}")
        forward: dict[str, Any] = {}
        annotations: dict[str, Any] = {}
        _put(annotations, ANNOTATION_PREFIX + "id", relation.pop("id", None))
        _put(forward, "description", relation.pop("description", None))
        _put(forward, "title", relation.pop("display_name", None))
        forward["range"] = to_kind
        if many_to == "many":
            forward["multivalued"] = True
        if relation.pop("required_from", False):
            forward["required"] = True
        forward["inverse"] = inverse
        annotations.update(_annotations(relation))
        _put(forward, "annotations", annotations)
        self.add_slot(classes, from_kind, name, forward, path)
        if inverse == name and from_kind == to_kind:
            return  # symmetric: one slot is its own inverse
        back: dict[str, Any] = {"range": from_kind}
        if many_from == "many":
            back["multivalued"] = True
        back["inverse"] = name
        back["annotations"] = {ANNOTATION_PREFIX + "relation": _INVERSE_MARK}
        self.add_slot(classes, to_kind, inverse, back, path)

    def add_slot(
        self,
        classes: dict[str, dict[str, Any]],
        kind: str,
        name: str,
        slot: dict[str, Any],
        path: str,
    ) -> None:
        if kind not in self.kinds:
            raise LinkmlError(f"{path}: unknown Kind {kind!r}")
        slots = classes[kind].setdefault("attributes", {})
        if name in slots:
            raise LinkmlError(f"{path}: Kind {kind} already has an attribute or relation {name}")
        slots[name] = slot


# Import ---------------------------------------------------------------------------------------


def from_linkml(schema: Any, options: LinkmlOptions | None = None) -> Conversion:
    """The Lorebase schema document of a LinkML schema, with warnings for what it leaves out."""
    return _Importer(schema, options or LinkmlOptions()).run()


#: Keys of a LinkML schema the import reads.
_SCHEMA_KEYS = frozenset(
    {
        "id",
        "name",
        "title",
        "description",
        "prefixes",
        "default_prefix",
        "default_range",
        "imports",
        "annotations",
        "classes",
        "slots",
        "enums",
        "types",
    }
)
_CLASS_KEYS = frozenset(
    {
        "description",
        "title",
        "is_a",
        "mixins",
        "mixin",
        "abstract",
        "tree_root",
        "slots",
        "attributes",
        "slot_usage",
        "unique_keys",
        "annotations",
    }
)
_ATTRIBUTE_KEYS = frozenset(
    {
        "description",
        "title",
        "range",
        "multivalued",
        "required",
        "identifier",
        "key",
        "ifabsent",
        "minimum_value",
        "maximum_value",
        "pattern",
        "equals_string_in",
        "minimum_cardinality",
        "maximum_cardinality",
        "annotations",
    }
)
_RELATION_KEYS = frozenset(
    {
        "description",
        "title",
        "range",
        "multivalued",
        "required",
        "inverse",
        "inlined",
        "inlined_as_list",
        "annotations",
    }
)
_INVERSE_SLOT_KEYS = frozenset({"range", "multivalued", "inverse", "annotations"})
_TRAIT_ATTRIBUTE_KEYS = ("id", "display_name", "description", "type", "required")
_TYPE_KEYS = frozenset({"typeof", "uri", "base", "repr", "description"})
_ENUM_KEYS = frozenset({"description", "title", "permissible_values", "enum_uri"})


@dataclass
class _Candidate:
    """A slot with a class range: one side of a Relation."""

    owner: str
    slot_name: str
    slot: dict[str, Any]
    target: str
    path: str
    annotations: dict[str, Any]


class _Importer:
    def __init__(self, schema: Any, options: LinkmlOptions) -> None:
        if not isinstance(schema, Mapping):
            raise LinkmlError("a LinkML schema is a mapping")
        self.schema = schema
        self.options = options
        self.warnings: list[ConversionWarning] = []
        self.classes = _mapping(schema.get("classes"), "classes")
        self.slots = _mapping(schema.get("slots"), "slots")
        self.enums = _mapping(schema.get("enums"), "enums")
        self.types = _mapping(schema.get("types"), "types")
        self.default_range = str(schema.get("default_range") or options.default_range)
        self.class_names: dict[str, str] = {}
        self.trait_attributes: dict[str, list[str]] = {}
        self._enum_values: dict[str, tuple[str, ...] | None] = {}
        self._type_names: dict[str, str] = {}
        self._inherited: dict[str, dict[str, dict[str, Any]]] = {}

    def warn(self, path: str, message: str) -> None:
        warning = ConversionWarning(path, message)
        if warning not in self.warnings:
            self.warnings.append(warning)

    def leftovers(self, mapping: Mapping[str, Any], handled: Iterable[str], path: str) -> None:
        known = set(handled)
        extra = sorted(str(key) for key in mapping if key not in known)
        if extra:
            self.warn(path, f"not supported, ignored: {', '.join(extra)}")

    def annotations(self, value: Any, path: str) -> dict[str, Any]:
        """The `lorebase.*` annotations without prefix, values decoded; warns about others."""
        pairs: list[tuple[str, Any]] = []
        if isinstance(value, Mapping):
            for tag, item in value.items():
                if isinstance(item, Mapping) and "value" in item:
                    item = item["value"]
                pairs.append((str(tag), item))
        elif isinstance(value, list):
            pairs = [
                (str(item.get("tag")), item.get("value"))
                for item in value
                if isinstance(item, Mapping)
            ]
        lorebase = {}
        foreign = []
        for tag, item in pairs:
            if tag.startswith(ANNOTATION_PREFIX):
                lorebase[tag.removeprefix(ANNOTATION_PREFIX)] = _decode(item)
            else:
                foreign.append(tag)
        if foreign:
            self.warn(f"{path}/annotations", f"not kept: {', '.join(sorted(foreign))}")
        return lorebase

    # Schema -----------------------------------------------------------------------------------

    def run(self) -> Conversion:
        domain = self.domain()
        self.leftovers(self.schema, _SCHEMA_KEYS, "schema")
        imports = [str(item) for item in self.schema.get("imports") or []]
        unresolved = [item for item in imports if item != LINKML_TYPES_IMPORT]
        if unresolved:
            self.warn("imports", f"not resolved, ignored: {', '.join(unresolved)}")

        kinds_source, traits_source = self.class_roles()
        traits: dict[str, Any] = {}
        for name in traits_source:
            traits[self.class_names[name]] = self.trait(name)
        kinds: dict[str, Any] = {}
        candidates: list[_Candidate] = []
        for name in kinds_source:
            kinds[self.class_names[name]] = self.kind(name, candidates)
        relations = self.relations(candidates, set(kinds_source))

        document: dict[str, Any] = {"format": LOREBASE_FORMAT, "domain": domain}
        _put(document, "kinds", kinds)
        _put(document, "relations", relations)
        _put(document, "traits", traits)
        return Conversion(document, self.warnings)

    def domain(self) -> dict[str, Any]:
        name = self.schema.get("name")
        if not isinstance(name, str) or not name.strip():
            raise LinkmlError("schema: no name")
        slug = re.sub(r"[^a-z0-9_-]+", "_", name.strip().lower()).strip("_-")
        if not slug or not slug[0].isalpha():
            slug = f"d_{slug}".rstrip("_")
        if slug != name:
            self.warn("name", f"Domain id {slug!r} made from {name!r}")
        domain: dict[str, Any] = {"id": slug}
        _put(domain, "display_name", self.schema.get("title"))
        _put(domain, "description", self.schema.get("description"))
        lorebase = self.annotations(self.schema.get("annotations"), "schema")
        lorebase.pop("format", None)
        for section in EXPORT_ONLY_SECTIONS:
            if lorebase.pop(section, None) is not None:
                self.warn(
                    f"annotations/{ANNOTATION_PREFIX}{section}",
                    f"{section} are not imported; define them again in Lorebase",
                )
        domain.update(lorebase)
        return domain

    def class_roles(self) -> tuple[list[str], list[str]]:
        """LinkML classes that become Kinds and Traits; others are reported."""
        kinds: list[str] = []
        traits: list[str] = []
        for name, definition in self.classes.items():
            cls = _mapping(definition, f"classes/{name}")
            self.classes[name] = cls
            path = f"classes/{name}"
            if cls.get("mixin"):
                traits.append(name)
            elif cls.get("abstract"):
                self.warn(path, "abstract class is not a Kind; classes inheriting it get its slots")
                continue
            elif cls.get("tree_root"):
                self.warn(path, "tree_root container class skipped with its slots")
                continue
            else:
                kinds.append(name)
            api_name = pascal_name(name)
            if api_name in self.class_names.values():
                raise LinkmlError(f"{path}: name {api_name!r} is already taken")
            if api_name != name:
                self.warn(path, f"renamed to {api_name}")
            self.class_names[name] = api_name
        return kinds, traits

    def effective_slots(self, name: str, seen: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
        """Slots of a class with inheritance flattened: `is_a`, then mixins, then its own
        `slots` and `attributes` (each replacing an inherited slot of the same name), then
        `slot_usage` refining them."""
        if name in self._inherited:
            return self._inherited[name]
        if name in seen:
            raise LinkmlError(f"classes/{name}: inheritance cycle")
        cls = _mapping(self.classes.get(name), f"classes/{name}")
        path = f"classes/{name}"
        slots: dict[str, dict[str, Any]] = {}
        parents = [cls["is_a"]] if cls.get("is_a") else []
        if ANNOTATION_PREFIX + "traits" not in _tags(cls.get("annotations")):
            # With explicit Trait maps (an export), the class already lists every attribute.
            parents.extend(cls.get("mixins") or [])
        for parent in parents:
            if parent not in self.classes:
                self.warn(path, f"unknown parent class {parent}, ignored")
                continue
            for slot_name, slot in self.effective_slots(parent, (*seen, name)).items():
                slots.setdefault(slot_name, dict(slot))
        for slot_name in cls.get("slots") or []:
            if slot_name not in self.slots:
                self.warn(f"{path}/slots", f"unknown slot {slot_name}, typed {self.default_range}")
            slots.pop(slot_name, None)  # own slots keep their declared order
            slots[slot_name] = _mapping(self.slots.get(slot_name), f"slots/{slot_name}")
        for slot_name, slot in _mapping(cls.get("attributes"), f"{path}/attributes").items():
            slots.pop(slot_name, None)
            slots[slot_name] = _mapping(slot, f"{path}/attributes/{slot_name}")
        for slot_name, usage in _mapping(cls.get("slot_usage"), f"{path}/slot_usage").items():
            usage_path = f"{path}/slot_usage/{slot_name}"
            slots[slot_name] = {**slots.get(slot_name, {}), **_mapping(usage, usage_path)}
        self._inherited[name] = slots
        return slots

    def note_inheritance(self, name: str, cls: Mapping[str, Any]) -> None:
        path = f"classes/{name}"
        if cls.get("is_a"):
            self.warn(path, f"is_a {cls['is_a']} flattened: inherited slots are copied")
        for mixin in cls.get("mixins") or []:
            if mixin in self.classes and not self.classes[mixin].get("mixin"):
                self.warn(path, f"{mixin} is not a mixin class: its slots are copied")

    # Classes ----------------------------------------------------------------------------------

    def trait(self, name: str) -> dict[str, Any]:
        cls = self.classes[name]
        path = f"classes/{name}"
        self.leftovers(cls, _CLASS_KEYS - {"unique_keys", "tree_root"}, path)
        self.note_inheritance(name, cls)
        lorebase = self.annotations(cls.get("annotations"), path)
        trait: dict[str, Any] = {}
        _put(trait, "id", lorebase.pop("id", None))
        _put(trait, "display_name", cls.get("title"))
        _put(trait, "description", cls.get("description"))
        attributes: dict[str, Any] = {}
        for slot_name, slot in self.effective_slots(name).items():
            slot_path = f"{path}/attributes/{slot_name}"
            if self.relation_target(slot) is not None:
                self.warn(slot_path, "a slot with a class range is not a Trait attribute, skipped")
                continue
            attribute = self.attribute(slot, slot_path, unique=False, identifier=False)
            dropped = sorted(set(attribute) - set(_TRAIT_ATTRIBUTE_KEYS))
            if dropped:
                self.warn(slot_path, f"not kept on a Trait attribute: {', '.join(dropped)}")
            attributes[self.attribute_name(slot_name, slot_path)] = {
                key: attribute[key] for key in _TRAIT_ATTRIBUTE_KEYS if key in attribute
            }
        self.trait_attributes[name] = list(attributes)
        trait.update(lorebase)
        _put(trait, "attributes", attributes)
        return trait

    def kind(self, name: str, candidates: list[_Candidate]) -> dict[str, Any]:
        cls = self.classes[name]
        path = f"classes/{name}"
        self.leftovers(cls, _CLASS_KEYS, path)
        self.note_inheritance(name, cls)
        lorebase = self.annotations(cls.get("annotations"), path)
        primary_key: list[str] = []
        unique: set[str] = set()
        for key_name, key in _mapping(cls.get("unique_keys"), f"{path}/unique_keys").items():
            members = [str(item) for item in _mapping(key, path).get("unique_key_slots") or []]
            if key_name == _PRIMARY_KEY:
                primary_key = [snake_name(item) for item in members]
            elif len(members) == 1:
                unique.add(members[0])
            else:
                self.warn(f"{path}/unique_keys/{key_name}", "composite unique key not supported")

        attributes: dict[str, Any] = {}
        for slot_name, slot in self.effective_slots(name).items():
            slot_path = f"{path}/attributes/{slot_name}"
            target = self.relation_target(slot)
            if target is not None:
                if target in self.class_names and not self.classes[target].get("mixin"):
                    slot_annotations = self.annotations(slot.get("annotations"), slot_path)
                    candidates.append(
                        _Candidate(name, slot_name, slot, target, slot_path, slot_annotations)
                    )
                else:
                    self.warn(slot_path, f"range {target} is not a Kind, slot skipped")
                continue
            attribute_name = self.attribute_name(slot_name, slot_path)
            identifier = bool(slot.get("identifier") or slot.get("key"))
            if identifier:
                if primary_key and primary_key != [attribute_name]:
                    self.warn(slot_path, "a second identifier is not part of the primary key")
                    identifier = False
                else:
                    primary_key = [attribute_name]
            attributes[attribute_name] = self.attribute(
                slot, slot_path, unique=slot_name in unique, identifier=identifier
            )

        kind: dict[str, Any] = {}
        _put(kind, "id", lorebase.pop("id", None))
        _put(kind, "display_name", cls.get("title"))
        _put(kind, "description", cls.get("description"))
        _put(kind, "primary_key", primary_key)
        traits = lorebase.pop("traits", None)
        kind.update(lorebase)
        _put(kind, "attributes", attributes)
        if traits is None:
            traits = []
            for mixin in cls.get("mixins") or []:
                if mixin not in self.trait_attributes:
                    continue
                names = self.trait_attributes[mixin]
                item: dict[str, Any] = {"trait": self.class_names[mixin]}
                _put(item, "attribute_map", {a: a for a in names if a in attributes})
                traits.append(item)
        _put(kind, "traits", traits)
        self.draft_when_incomplete(kind, path)
        return kind

    def draft_when_incomplete(self, kind: dict[str, Any], path: str) -> None:
        """Active Kinds need a title attribute and described Attributes, which LinkML schemas
        often lack: such a Kind is imported as a draft, to complete before activating it."""
        if "status" in kind:
            return
        missing = [] if kind.get("title_attribute") else ["a title_attribute"]
        undescribed = (
            [name for name, item in kind["attributes"].items() if not item.get("description")]
            if kind.get("attributes")
            else []
        )
        if undescribed:
            missing.append(f"descriptions of {', '.join(undescribed)}")
        if missing:
            kind["status"] = "draft"
            self.warn(path, f"imported as draft: add {' and '.join(missing)}, then activate it")

    def attribute_name(self, slot_name: str, path: str) -> str:
        name = snake_name(slot_name)
        if name != slot_name:
            self.warn(path, f"renamed to {name}")
        return name

    # Slots ------------------------------------------------------------------------------------

    def relation_target(self, slot: Mapping[str, Any]) -> str | None:
        """The class a slot points to, or None for slots with a type or enum range."""
        if ANNOTATION_PREFIX + "type" in _tags(slot.get("annotations")):
            return None
        target = str(slot.get("range") or self.default_range)
        return target if target in self.classes else None

    def attribute(
        self, slot: Mapping[str, Any], path: str, *, unique: bool, identifier: bool
    ) -> dict[str, Any]:
        self.leftovers(slot, _ATTRIBUTE_KEYS, path)
        lorebase = self.annotations(slot.get("annotations"), path)
        multivalued = bool(slot.get("multivalued"))
        attribute: dict[str, Any] = {}
        _put(attribute, "id", lorebase.pop("id", None))
        _put(attribute, "display_name", slot.get("title"))
        _put(attribute, "description", slot.get("description"))
        if "type" in lorebase:
            attribute["type"] = lorebase.pop("type")
        else:
            attribute["type"] = self.slot_type(slot, path, multivalued)
        required = slot.get("required")
        if required if required is not None else identifier:
            attribute["required"] = True
        if unique:
            attribute["unique"] = True
        if "ifabsent" in slot:
            default = self.default(slot["ifabsent"], path)
            if default is not _MISSING:
                attribute["default"] = default
        elif "default" in lorebase:
            attribute["default"] = lorebase.pop("default")
        constraints: dict[str, Any] = {}
        for key, source in _CONSTRAINT_SLOTS:
            if slot.get(source) is not None:
                constraints[key] = slot[source]
        for key, source in _LENGTH_SLOTS:
            if multivalued and slot.get(source) is not None:
                constraints[key] = slot[source]
            elif key in lorebase:
                constraints[key] = lorebase.pop(key)
        extra = lorebase.pop("constraints", None)
        if isinstance(extra, Mapping):
            constraints.update(extra)
        _put(attribute, "constraints", constraints)
        sensitivity = lorebase.pop("sensitivity", None)
        if sensitivity:
            tags = [tag.strip() for tag in str(sensitivity).split(",") if tag.strip()]
            _put(attribute, "sensitivity", tags)
        for key, section in _SECTION_REFERENCES.items():
            value = lorebase.pop(key, None)
            if value is not None:
                self.warn(path, f"{key} {value!r} dropped: {section} are not imported")
        attribute.update(lorebase)
        return attribute

    def slot_type(self, slot: Mapping[str, Any], path: str, multivalued: bool) -> Any:
        scalar = self.scalar_type(str(slot.get("range") or self.default_range), path)
        if not multivalued:
            return scalar
        return f"array<{scalar}>" if isinstance(scalar, str) else {"array": scalar}

    def scalar_type(self, range_name: str, path: str) -> Any:
        if range_name in self.enums:
            values = self.enum_values(range_name)
            return {"enum": list(values)} if values else "string"
        base = self.type_name(range_name, path)
        if base == "decimal":
            precision, scale = self.options.decimal_precision, self.options.decimal_scale
            self.warn(path, f"decimal without precision: decimal({precision},{scale}) assumed")
            return f"decimal({precision},{scale})"
        if base in _LINKML_TYPES:
            return _LINKML_TYPES[base]
        if base in _APPROXIMATE_LINKML_TYPES:
            closest = _APPROXIMATE_LINKML_TYPES[base]
            self.warn(path, f"range {base} has no Lorebase type, imported as {closest}")
            return closest
        self.warn(path, f"unknown range {range_name}, imported as string")
        return "string"

    def type_name(self, name: str, path: str) -> str:
        """Built-in type a range resolves to, following `typeof` of schema types."""
        if name in self._type_names:
            return self._type_names[name]
        current, seen = name.removeprefix("linkml:"), set()
        while current in self.types and current not in seen:
            seen.add(current)
            definition = _mapping(self.types[current], f"types/{current}")
            self.leftovers(definition, _TYPE_KEYS, f"types/{current}")
            parent = definition.get("typeof")
            if not parent:
                self.warn(f"types/{current}", "type without typeof, imported as string")
                current = "string"
                break
            current = str(parent).removeprefix("linkml:")
        self._type_names[name] = current
        return current

    def enum_values(self, name: str) -> tuple[str, ...] | None:
        if name in self._enum_values:
            return self._enum_values[name]
        path = f"enums/{name}"
        definition = _mapping(self.enums[name], path)
        self.leftovers(definition, _ENUM_KEYS, path)
        permissible = definition.get("permissible_values") or {}
        values: list[str] = []
        detail: set[str] = set()
        if isinstance(permissible, Mapping):
            for text, meta in permissible.items():
                values.append(str(text))
                if isinstance(meta, Mapping):
                    detail.update(str(key) for key in meta if key not in ("text", "description"))
        elif isinstance(permissible, list):
            for item in permissible:
                text = item.get("text") if isinstance(item, Mapping) else item
                values.append(str(text))
        if detail:
            self.warn(f"{path}/permissible_values", f"not kept: {', '.join(sorted(detail))}")
        result = tuple(values) or None
        if result is None:
            self.warn(path, "enum without permissible values, imported as string")
        self._enum_values[name] = result
        return result

    def default(self, value: Any, path: str) -> Any:
        if isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.lower() in ("true", "false"):
                return text.lower() == "true"
            if match := _IFABSENT.fullmatch(text):
                function, body = match.groups()
                try:
                    if function == "int":
                        return int(body)
                    if function == "float":
                        return float(body)
                except ValueError:
                    pass
                else:
                    return body
        self.warn(path, f"ifabsent {value!r} not supported, ignored")
        return _MISSING

    # Relations --------------------------------------------------------------------------------

    def relations(self, candidates: list[_Candidate], kinds: set[str]) -> dict[str, Any]:
        """Pair slots linked by `inverse` into Relations; unpaired slots get a made-up inverse."""
        by_slot = {(c.owner, c.slot_name): c for c in candidates}
        done: set[tuple[str, str]] = set()
        used: set[str] = set()
        relations: dict[str, Any] = {}
        for candidate in candidates:
            if (candidate.owner, candidate.slot_name) in done:
                continue
            forward, back = candidate, self.partner(candidate, by_slot, done)
            if (
                back is not None
                and back is not forward
                and forward.annotations.get("relation") == _INVERSE_MARK
                and back.annotations.get("relation") != _INVERSE_MARK
            ):
                forward, back = back, forward
            done.add((forward.owner, forward.slot_name))
            if back is not None:
                done.add((back.owner, back.slot_name))
            name, relation = self.relation(forward, back, used)
            relations[name] = relation
        return relations

    def partner(
        self,
        candidate: _Candidate,
        by_slot: Mapping[tuple[str, str], _Candidate],
        done: set[tuple[str, str]],
    ) -> _Candidate | None:
        inverse = candidate.slot.get("inverse")
        if inverse is None:
            return None
        partner = by_slot.get((candidate.target, str(inverse)))
        if partner is None or partner.target != candidate.owner:
            self.warn(candidate.path, f"inverse {inverse} not found on {candidate.target}")
            return None
        if (partner.owner, partner.slot_name) in done:
            return None
        return partner

    def relation(
        self, forward: _Candidate, back: _Candidate | None, used: set[str]
    ) -> tuple[str, dict[str, Any]]:
        self.leftovers(forward.slot, _RELATION_KEYS, forward.path)
        owner, target = self.class_names[forward.owner], self.class_names[forward.target]
        if forward.slot.get("inlined") or forward.slot.get("inlined_as_list"):
            self.warn(forward.path, f"inlined objects become linked Records of Kind {target}")
        symmetric = back is forward
        lorebase = dict(forward.annotations)
        lorebase.pop("relation", None)
        name = self.relation_name(snake_name(forward.slot_name), owner, used, forward.path)
        if symmetric:
            inverse = name
        else:
            wanted = snake_name(back.slot_name) if back else f"{snake_name(owner)}_{name}"
            inverse = self.relation_name(wanted, target, used, forward.path)
            if back is not None:
                self.leftovers(back.slot, _INVERSE_SLOT_KEYS, back.path)
                extra = sorted(set(back.annotations) - {"relation"})
                if extra:
                    self.warn(f"{back.path}/annotations", f"not kept: {', '.join(extra)}")
        many_to = "many" if forward.slot.get("multivalued") else "one"
        if symmetric:
            many_from = many_to
        elif back is not None:
            many_from = "many" if back.slot.get("multivalued") else "one"
        else:
            many_from = "many"

        relation: dict[str, Any] = {}
        _put(relation, "id", lorebase.pop("id", None))
        relation["inverse_api_name"] = inverse
        _put(relation, "display_name", forward.slot.get("title"))
        _put(relation, "description", forward.slot.get("description"))
        relation["from_kind"] = owner
        relation["to_kind"] = target
        relation["cardinality"] = f"{many_from}_to_{many_to}"
        if forward.slot.get("required"):
            relation["required_from"] = True
        link_attributes = lorebase.pop("attributes", None)
        relation.update(lorebase)
        if isinstance(link_attributes, Mapping):
            relation["attributes"] = {
                key: self.link_attribute(value, f"{forward.path}/attributes/{key}")
                for key, value in link_attributes.items()
            }
        return name, relation

    def link_attribute(self, attribute: Any, path: str) -> Any:
        if not isinstance(attribute, Mapping):
            return attribute
        kept = dict(attribute)
        for key, section in _SECTION_REFERENCES.items():
            value = kept.pop(key, None)
            if value is not None:
                self.warn(path, f"{key} {value!r} dropped: {section} are not imported")
        return kept

    def relation_name(self, wanted: str, owner: str, used: set[str], path: str) -> str:
        """Relation names are unique in a Domain: a taken one gets the owner Kind as prefix."""
        name = wanted
        if name in used:
            name = f"{snake_name(owner)}_{wanted}"
            number = 2
            while name in used:
                name = f"{snake_name(owner)}_{wanted}_{number}"
                number += 1
            self.warn(path, f"relation name {wanted} is taken, renamed to {name}")
        used.add(name)
        return name


class _Missing:
    pass


_MISSING: Any = _Missing()


# Merge ----------------------------------------------------------------------------------------

#: Fields of each element that a LinkML import sets; the others are kept from the current schema
#: when the import does not carry them.
_OWNED_KIND = frozenset(
    {"id", "display_name", "description", "primary_key", "traits", "attributes"}
)
_OWNED_TRAIT = frozenset({"id", "display_name", "description", "attributes"})
_OWNED_ATTRIBUTE = frozenset(
    {
        "id",
        "display_name",
        "description",
        "type",
        "required",
        "unique",
        "default",
        "constraints",
        "sensitivity",
    }
)
_OWNED_RELATION = frozenset(
    {
        "id",
        "display_name",
        "description",
        "from_kind",
        "to_kind",
        "inverse_api_name",
        "cardinality",
        "required_from",
        "attributes",
    }
)
_OWNED_CONSTRAINTS = frozenset(key for pair in (*_CONSTRAINT_SLOTS, *_LENGTH_SLOTS) for key in pair)


def merge_into_current(current: Any, imported: Conversion, *, prune: bool = False) -> Conversion:
    """The imported document laid over the current schema of a Domain.

    The import owns Kinds, Attributes, Relations, Traits and enums. Everything else is kept:
    Operations, Derivations and Sources, and the fields an element has that the import does not
    carry (`derivation`, `authority`, `indexed`, `icon`, ...). Elements match by `id`, else by
    name, and keep the current id. Kinds, Traits, Relations and Attributes the import leaves out
    are kept with a warning, or removed with `prune`.
    """
    if not isinstance(current, Mapping):
        raise LinkmlError("the current schema is not a mapping")
    old = dict(current)
    document = copy.deepcopy(imported.document)
    warnings = list(imported.warnings)
    merger = _Merger(prune, warnings)

    document["domain"] = {**_mapping(old.get("domain"), "domain"), **document["domain"]}
    for section in EXPORT_ONLY_SECTIONS:
        if old.get(section):
            document[section] = copy.deepcopy(old[section])
    pruned: set[str] = set()
    for section, owned in (("traits", _OWNED_TRAIT), ("kinds", _OWNED_KIND)):
        merged, gone = merger.section(section, old, document, owned, set())
        _put(document, section, merged)
        if section == "kinds":
            pruned = gone
    merged, _ = merger.section("relations", old, document, _OWNED_RELATION, pruned)
    _put(document, "relations", merged)
    merger.restore_status(document, old)
    return Conversion(document, merger.warnings)


class _Merger:
    def __init__(self, prune: bool, warnings: list[ConversionWarning]) -> None:
        self.prune = prune
        self.warnings = warnings

    def note(self, path: str, message: str) -> None:
        self.warnings.append(ConversionWarning(path, message))

    def section(
        self,
        section: str,
        old: Mapping[str, Any],
        document: Mapping[str, Any],
        owned: frozenset[str],
        pruned_kinds: set[str],
    ) -> tuple[dict[str, Any], set[str]]:
        """The merged elements of a section and the names of current ones that are removed."""
        before = _mapping(old.get(section), section)
        merged: dict[str, Any] = {}
        matched = _match(before, _mapping(document.get(section), section))
        for name, item in _mapping(document.get(section), section).items():
            previous = before.get(matched.get(name, ""))
            if previous is None:
                merged[name] = item
                continue
            merged[name] = self.element(f"{section}/{name}", previous, item, owned)
        gone: set[str] = set()
        for name, item in before.items():
            if name in matched.values():
                continue
            path = f"{section}/{name}"
            ends = {str(item.get("from_kind")), str(item.get("to_kind"))}
            if self.prune:
                gone.add(name)
                self.note(path, "removed: not in the LinkML schema (--prune)")
            elif ends & pruned_kinds:
                gone.add(name)
                self.note(path, "removed: it refers to a pruned Kind")
            elif name in merged:
                gone.add(name)
                self.note(path, "removed: its name is taken by another element")
            else:
                merged[name] = copy.deepcopy(item)
                self.note(path, "kept: not in the LinkML schema (--prune removes it)")
        return merged, gone

    def element(
        self, path: str, previous: Mapping[str, Any], item: dict[str, Any], owned: frozenset[str]
    ) -> dict[str, Any]:
        """`item` with the id and the fields it does not own taken from `previous`."""
        result = _inherit(previous, item, owned)
        if "attributes" in owned and (item.get("attributes") or previous.get("attributes")):
            attributes = self.attributes(
                f"{path}/attributes",
                _mapping(previous.get("attributes"), path),
                _mapping(item.get("attributes"), path),
            )
            _put(result, "attributes", attributes)
        return result

    def attributes(
        self, path: str, before: Mapping[str, Any], new: Mapping[str, Any]
    ) -> dict[str, Any]:
        matched = _match(before, new)
        merged: dict[str, Any] = {}
        for name, item in new.items():
            previous = before.get(matched.get(name, ""))
            merged[name] = item if previous is None else self.attribute(previous, item)
        for name, item in before.items():
            if name in matched.values():
                continue
            if self.prune:
                self.note(f"{path}/{name}", "removed: not in the LinkML schema (--prune)")
            elif name in merged:
                self.note(f"{path}/{name}", "removed: its name is taken by another attribute")
            else:
                merged[name] = copy.deepcopy(item)
                self.note(f"{path}/{name}", "kept: not in the LinkML schema (--prune removes it)")
        return merged

    @staticmethod
    def attribute(previous: Mapping[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        result = _inherit(previous, item, _OWNED_ATTRIBUTE)
        constraints = {
            key: value
            for key, value in _mapping(previous.get("constraints"), "constraints").items()
            if key not in _OWNED_CONSTRAINTS
        }
        constraints.update(_mapping(item.get("constraints"), "constraints"))
        _put(result, "constraints", constraints)
        return result

    def restore_status(self, document: dict[str, Any], old: Mapping[str, Any]) -> None:
        """A Kind the import made a draft for lacking a title attribute or descriptions goes
        back to its current status when the merge supplied what was missing."""
        before = _mapping(old.get("kinds"), "kinds")
        kinds = _mapping(document.get("kinds"), "kinds")
        matched = _match(before, kinds)
        for name, kind in kinds.items():
            previous = before.get(matched.get(name, ""))
            if previous is None or kind.get("status") != "draft":
                continue
            if previous.get("status") in (None, "draft") or not kind.get("title_attribute"):
                continue
            if all(a.get("description") for a in (kind.get("attributes") or {}).values()):
                kind["status"] = previous["status"]
                self.warnings = [
                    w
                    for w in self.warnings
                    if not (
                        w.message.startswith("imported as draft")
                        and pascal_name(w.path.rsplit("/", 1)[-1]) == name
                    )
                ]


def _match(before: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, str]:
    """New name -> current name of the elements that are the same: same `id`, else same name."""
    by_id = {
        str(item["id"]): name
        for name, item in before.items()
        if isinstance(item, Mapping) and item.get("id")
    }
    matched: dict[str, str] = {}
    for name, item in new.items():
        wanted = item.get("id") if isinstance(item, Mapping) else None
        found = by_id.get(str(wanted)) if wanted else None
        if found is None and not wanted and name in before:
            found = name
        if found is not None and found not in matched.values():
            matched[name] = found
    return matched


def _inherit(
    previous: Mapping[str, Any], item: dict[str, Any], owned: frozenset[str]
) -> dict[str, Any]:
    """`item`, with the id of `previous` and the fields of `previous` it neither has nor owns."""
    result = dict(item)
    if previous.get("id") and not result.get("id"):
        result = {"id": previous["id"], **result}
    for key, value in previous.items():
        if key not in owned and key not in result:
            result[key] = copy.deepcopy(value)
    return result
