from __future__ import annotations

from typing import Any

import pytest
import yaml

from lorebase_core.model import (
    Schema,
    SchemaYamlError,
    dump_schema,
    id_prefix,
    load_schema,
    schema_from_document,
    schema_to_document,
    validate_schema,
    validate_schema_yaml,
    yaml_path,
)
from lorebase_core.validate import IssueCode, SchemaRules

from .supply import (
    LEGAL_NAME,
    SUPPLIER,
    SUPPLIER_COUNT_DRV,
    supply_schema,
)
from .yaml_support import SECTIONS, counting_factory, strip_ids


def full_schema() -> Schema:
    return supply_schema(with_operation=True)


def document_text(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False)


# Round trip -------------------------------------------------------------------------------------


def test_round_trip_is_lossless() -> None:
    schema = full_schema()
    text = dump_schema(schema)
    assert load_schema(text) == schema
    assert dump_schema(load_schema(text)) == text


def test_dump_always_writes_ids() -> None:
    document = schema_to_document(full_schema())
    for section in SECTIONS:
        for element in document[section].values():
            assert "id" in element
            for attribute in element.get("attributes", {}).values():
                assert "id" in attribute


def test_dump_refers_by_api_name_and_omits_defaults() -> None:
    document = schema_to_document(full_schema())
    part = document["kinds"]["Part"]
    assert part["primary_key"] == ["part_number"]
    assert part["attributes"]["supplier_count"]["derivation"] == "supplier_count"
    assert part["attributes"]["preferred_plant"]["type"] == "ref(Plant)"
    assert document["kinds"]["Contract"]["attributes"]["value"]["type"] == "decimal(12,2)"
    assert document["kinds"]["Supplier"]["traits"][0]["trait"] == "Located"
    assert "status" not in part
    assert document["relations"]["made_at"]["foreign_key_attribute"] == "preferred_plant"
    assert not any(key.endswith("_id") for key in part["attributes"]["supplier_count"])


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("decimal(10, 3)", "decimal(10,3)"),
        ("array<string>", "array<string>"),
        ({"enum": ["a", "b"]}, {"enum": ["a", "b"]}),
        ({"array": {"enum": ["a", "b"]}}, {"array": {"enum": ["a", "b"]}}),
        ("array<ref(Plant)>", "array<ref(Plant)>"),
        ("timestamp", "timestamp"),
    ],
)
def test_type_forms_round_trip(spec: object, expected: object) -> None:
    document = schema_to_document(full_schema())
    document["kinds"]["Part"]["attributes"]["extra"] = {"type": spec}
    schema = schema_from_document(document)
    assert schema_to_document(schema)["kinds"]["Part"]["attributes"]["extra"]["type"] == expected


def test_dates_in_yaml_stay_text() -> None:
    document = schema_to_document(full_schema())
    document["kinds"]["Part"]["attributes"]["valid_from"] = {"type": "date"}
    text = dump_schema(schema_from_document(document)).replace(
        "valid_from:", "valid_from:\n        constraints:\n          min: 2024-01-31", 1
    )
    attribute = next(
        a
        for kind in load_schema(text).kinds.values()
        for a in kind.attributes.values()
        if a.api_name == "valid_from"
    )
    assert attribute.constraints.min == "2024-01-31"


def test_multiline_text_is_kept() -> None:
    document = schema_to_document(full_schema())
    document["kinds"]["Part"]["description"] = "First line.\nSecond line."
    text = dump_schema(schema_from_document(document))
    assert "|" in text
    assert load_schema(text).kind_by_api_name("Part").description == "First line.\nSecond line."  # type: ignore[union-attr]


# Ids ----------------------------------------------------------------------------------------------


def test_missing_ids_are_minted() -> None:
    schema = full_schema()
    text = document_text(strip_ids(schema_to_document(schema)))
    loaded = load_schema(text, id_factory=counting_factory())
    assert loaded != schema
    for section in SECTIONS:
        for element_id, element in getattr(loaded, section).items():
            assert id_prefix(element_id) is not None
            assert element.id == element_id
    assert dump_schema(loaded) == dump_schema(load_schema(text, id_factory=counting_factory()))


def test_reload_against_base_produces_no_changes() -> None:
    schema = full_schema()
    text = document_text(strip_ids(schema_to_document(schema)))
    assert load_schema(text, base=schema, id_factory=counting_factory()) == schema


def test_rename_keeps_id() -> None:
    document = schema_to_document(supply_schema())
    document["kinds"] = {
        ("Vendor" if name == "Supplier" else name): body for name, body in document["kinds"].items()
    }
    attributes = document["kinds"]["Vendor"]["attributes"]
    document["kinds"]["Vendor"]["attributes"] = {
        ("trading_name" if name == "legal_name" else name): body
        for name, body in attributes.items()
    }
    document["kinds"]["Vendor"]["title_attribute"] = "trading_name"
    for relation in document["relations"].values():
        for end in ("from_kind", "to_kind"):
            if relation[end] == "Supplier":
                relation[end] = "Vendor"
    document["sources"]["erp.vendors"]["target_kind"] = "Vendor"
    document["sources"]["erp.vendors"]["attribute_mapping"] = {"trading_name": {"column": "name"}}

    renamed = load_schema(document_text(document))

    vendor = renamed.kinds[SUPPLIER]
    assert vendor.api_name == "Vendor"
    assert renamed.kind_by_api_name("Supplier") is None
    assert vendor.attributes[LEGAL_NAME].api_name == "trading_name"
    assert vendor.title_attribute == LEGAL_NAME
    assert validate_schema(renamed) == []


def test_rename_without_id_against_base_is_a_new_element() -> None:
    schema = full_schema()
    document = strip_ids(schema_to_document(schema))
    document["traits"]["Placed"] = document["traits"].pop("Located")
    for kind in document["kinds"].values():
        for implementation in kind.get("traits", []):
            implementation["trait"] = "Placed"
    renamed = load_schema(document_text(document), base=schema, id_factory=counting_factory())
    assert renamed.trait_by_api_name("Placed") is not None
    assert renamed.trait_by_api_name("Placed").id not in schema.traits  # type: ignore[union-attr]


def test_explicit_id_wins_over_base_name_match() -> None:
    schema = full_schema()
    document = strip_ids(schema_to_document(schema))
    document["kinds"]["Plant"]["id"] = schema.kind_by_api_name("Supplier").id  # type: ignore[union-attr]
    document["kinds"]["Supplier"]["id"] = schema.kind_by_api_name("Plant").id  # type: ignore[union-attr]
    swapped = load_schema(document_text(document), base=schema, id_factory=counting_factory())
    assert swapped.kind_by_api_name("Plant").id == schema.kind_by_api_name("Supplier").id  # type: ignore[union-attr]
    assert swapped.kind_by_api_name("Supplier").id == schema.kind_by_api_name("Plant").id  # type: ignore[union-attr]


# Validation issues surface with YAML paths ------------------------------------------------------


def test_validation_issue_paths_point_into_the_yaml() -> None:
    document = schema_to_document(full_schema())
    document["kinds"]["Supplier"]["attributes"]["tier"]["default"] = "9"
    issues = validate_schema_yaml(
        document_text(document), rules=SchemaRules(allow_expressions=True)
    )
    codes = {issue.code for issue in issues}
    assert IssueCode.SCHEMA_DEFAULT_INVALID in codes
    issue = next(i for i in issues if i.code is IssueCode.SCHEMA_DEFAULT_INVALID)
    assert issue.path == ("kinds", "Supplier", "attributes", "tier", "default")
    assert issue.kind == "Supplier"
    assert issue.attribute == "tier"


def test_expression_gate_issue_path_uses_names() -> None:
    text = dump_schema(full_schema())
    issues = validate_schema_yaml(text)
    gated = [i for i in issues if i.code is IssueCode.SCHEMA_EXPRESSIONS_DISABLED]
    assert gated
    assert gated[0].path[:2] == ("operations", "flag_supplier_risk")
    assert not validate_schema_yaml(text, rules=SchemaRules(allow_expressions=True))


def test_yaml_path_translates_every_reference_kind() -> None:
    schema = full_schema()
    assert yaml_path(schema, ("derivations", SUPPLIER_COUNT_DRV, "definition")) == (
        "derivations",
        "supplier_count",
        "definition",
    )
    assert yaml_path(schema, ("kinds", SUPPLIER, "traits", 0, "trait_id")) == (
        "kinds",
        "Supplier",
        "traits",
        0,
        "trait",
    )


# Load errors ------------------------------------------------------------------------------------


def problems_of(document: object) -> dict[tuple[str | int, ...], str]:
    with pytest.raises(SchemaYamlError) as excinfo:
        schema_from_document(document)
    return {problem.path: problem.message for problem in excinfo.value.problems}


def base_document() -> dict[str, Any]:
    return schema_to_document(full_schema())


def test_unknown_key_is_reported_with_its_path() -> None:
    document = base_document()
    document["kinds"]["Part"]["colour"] = "red"
    problems = problems_of(document)
    assert ("kinds", "Part", "colour") in problems


def test_unknown_reference_is_reported() -> None:
    document = base_document()
    document["relations"]["supplied_by"]["to_kind"] = "Vendor"
    document["kinds"]["Part"]["primary_key"] = ["nope"]
    problems = problems_of(document)
    assert "unknown Kind 'Vendor'" in problems[("relations", "supplied_by", "to_kind")]
    assert "unknown Attribute" in problems[("kinds", "Part", "primary_key", 0)]


def test_all_problems_are_collected() -> None:
    document = base_document()
    document["kinds"]["Part"]["attributes"]["name"]["type"] = "strng"
    document["kinds"]["Plant"]["attributes"]["code"]["type"] = "decimal"
    document["unknown_section"] = {}
    problems = problems_of(document)
    assert ("kinds", "Part", "attributes", "name", "type") in problems
    assert ("kinds", "Plant", "attributes", "code", "type") in problems
    assert ("unknown_section",) in problems


def test_missing_domain_and_type_are_reported() -> None:
    document = base_document()
    del document["domain"]
    del document["kinds"]["Part"]["attributes"]["name"]["type"]
    problems = problems_of(document)
    assert ("domain", "id") in problems
    assert ("kinds", "Part", "attributes", "name", "type") in problems


def test_domain_examples_round_trip_and_stay_absent_when_empty() -> None:
    document = base_document()
    document["domain"]["examples"] = [{"label": "Overview", "question": "What is in the graph?"}]
    schema = schema_from_document(document)
    assert schema.domain.examples is not None
    assert [e.label for e in schema.domain.examples] == ["Overview"]
    assert load_schema(dump_schema(schema)) == schema
    assert schema.content()["domain"]["examples"] == [
        {"label": "Overview", "question": "What is in the graph?"}
    ]

    document["domain"]["examples"] = []
    empty = schema_from_document(document)
    assert empty.domain.examples is None
    assert "examples" not in empty.content()["domain"]
    assert "examples" not in schema_to_document(empty)["domain"]


def test_invalid_domain_examples_are_reported() -> None:
    document = base_document()
    document["domain"]["examples"] = [
        {"label": "", "question": "What is in the graph?"},
        {"label": "Same", "question": "One?", "extra": 1},
    ]
    problems = problems_of(document)
    assert ("domain", "examples", 0, "label") in problems
    assert ("domain", "examples", 1, "extra") in problems

    document["domain"]["examples"] = [
        {"label": "Same", "question": "One?"},
        {"label": "Same", "question": "Two?"},
    ]
    assert "duplicate example labels" in problems_of(document)[("domain", "examples")]


def test_ids_must_have_the_right_prefix_and_be_unique() -> None:
    document = base_document()
    document["kinds"]["Part"]["id"] = "rel_01JB8Z00000000000000000001"
    document["kinds"]["Plant"]["id"] = document["kinds"]["Supplier"]["id"]
    problems = problems_of(document)
    assert ("kinds", "Part", "id") in problems
    assert ("kinds", "Plant", "id") in problems


def test_enum_values_must_be_text() -> None:
    document = base_document()
    document["kinds"]["Part"]["attributes"]["grade"] = {"type": {"enum": [1, 2]}}
    assert ("kinds", "Part", "attributes", "grade", "type") in problems_of(document)


def test_invalid_step_and_variant_are_reported() -> None:
    document = base_document()
    document["operations"]["flag_supplier_risk"]["steps"][0]["step"] = "teleport"
    document["derivations"]["supplier_count"]["definition"]["variant"] = "magic"
    problems = problems_of(document)
    assert ("operations", "flag_supplier_risk", "steps", 0, "step") in problems
    assert ("derivations", "supplier_count", "definition", "variant") in problems


def test_syntax_errors_and_duplicate_keys_are_reported() -> None:
    with pytest.raises(SchemaYamlError, match="invalid YAML"):
        load_schema("domain: [unclosed")
    with pytest.raises(SchemaYamlError, match="duplicate key 'Part'"):
        load_schema("domain: {id: x}\nkinds:\n  Part: {}\n  Part: {}\n")


def test_non_text_keys_are_rejected() -> None:
    with pytest.raises(SchemaYamlError, match="quote the key"):
        load_schema(
            "domain: {id: x}\nkinds:\n  Part:\n    attributes:\n      yes: {type: string}\n"
        )


def test_unsupported_format_is_rejected() -> None:
    document = base_document()
    document["format"] = "other/v9"
    assert ("format",) in problems_of(document)


# Source options ---------------------------------------------------------------------------------


def http_json_document() -> dict[str, Any]:
    document = base_document()
    source = document["sources"]["erp.vendors"]
    source["type"] = "http_json"
    source["connection"] = "secret://env/VENDORS_API_URL"
    source["records_path"] = "data.items"
    source["flatten_separator"] = "."
    source["http"] = {
        "headers": {"Authorization": "secret://env/VENDORS_API_TOKEN", "X-Tenant": "acme"},
        "pagination": {"type": "cursor", "cursor_path": "meta.next", "cursor_param": "after"},
        "since_param": "updated_since",
        "max_pages": 50,
    }
    return document


def test_http_json_options_round_trip() -> None:
    schema = schema_from_document(http_json_document())
    source = next(iter(schema.sources.values()))
    assert source.http is not None
    assert source.http.pagination.cursor_path == "meta.next"
    assert source.records_path == "data.items"
    text = dump_schema(schema)
    assert load_schema(text) == schema
    assert "VENDORS_API_TOKEN" in text


def test_literal_credentials_and_incomplete_pagination_are_reported() -> None:
    document = http_json_document()
    document["sources"]["erp.vendors"]["http"]["headers"]["X-Api-Key"] = "plain-key"
    messages = " ".join(problems_of(document).values())
    assert "must be a secret reference" in messages
    assert "plain-key" not in messages
    document = http_json_document()
    document["sources"]["erp.vendors"]["http"]["pagination"] = {"type": "offset"}
    path = ("sources", "erp.vendors", "http", "pagination")
    assert "offset pagination needs page_size" in problems_of(document)[path]
