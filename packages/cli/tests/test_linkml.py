"""LinkML interchange: the mapping both ways, round trips, `schema export` and `schema import`."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from lorebase_cli.linkml import (
    LinkmlError,
    LinkmlOptions,
    dump_yaml,
    from_linkml,
    load_yaml,
    merge_into_current,
    to_linkml,
)
from lorebase_cli.main import app
from lorebase_core.model.yaml_schema import load_schema, schema_to_document, validate_schema_yaml
from lorebase_core.validate.issues import Severity

ROOT = Path(__file__).resolve().parents[3]
SUPPLY = ROOT / "examples" / "supply" / "schema.yaml"
PERSONINFO = Path(__file__).parent / "fixtures" / "personinfo.linkml.yaml"
API = "http://keeper.test/api/v1"
IMPORT = ["schema", "import", str(PERSONINFO), "--from", "linkml"]
runner = CliRunner()


def supply() -> dict[str, Any]:
    document: dict[str, Any] = load_yaml(SUPPLY.read_text(encoding="utf-8"))
    return document


def round_trip(document: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Lorebase -> LinkML YAML text -> Lorebase, with the warnings of the import."""
    text = dump_yaml(to_linkml(document))
    conversion = from_linkml(load_yaml(text))
    return conversion.document, [str(warning) for warning in conversion.warnings]


def canonical(document: dict[str, Any]) -> dict[str, Any]:
    """The document as the schema loader reads it: defaults and order no longer matter."""
    return schema_to_document(load_schema(dump_yaml(document)))


def without_ids(value: Any) -> Any:
    """Minted ids differ from load to load; elements without an explicit id compare without."""
    if isinstance(value, dict):
        return {k: without_ids(v) for k, v in value.items() if k != "id"}
    if isinstance(value, list):
        return [without_ids(item) for item in value]
    return value


def errors(document: dict[str, Any]) -> list[str]:
    issues = validate_schema_yaml(dump_yaml(document))
    return [f"{i.path}: {i.message}" for i in issues if i.severity is Severity.ERROR]


def without_export_only_parts(document: dict[str, Any]) -> dict[str, Any]:
    """What the import keeps: no Operations, Derivations or Sources, nor references to them."""
    kept = copy.deepcopy(document)
    for section in ("operations", "derivations", "sources"):
        kept.pop(section, None)
    for kind in kept.get("kinds", {}).values():
        for attribute in kind.get("attributes", {}).values():
            attribute.pop("derivation", None)
            attribute.pop("authority", None)
    return kept


# Round trips ----------------------------------------------------------------------------------


def test_supply_schema_round_trips_through_linkml() -> None:
    document = supply()
    back, warnings = round_trip(document)

    assert canonical(back) == canonical(without_export_only_parts(document))
    assert errors(back) == []
    # Only what refers to Operations, Derivations and Sources is reported.
    assert warnings, "the supply schema has Derivations and Sources"
    assert all("are not imported" in warning for warning in warnings)
    assert any("derivation 'supplier_count' dropped" in w for w in warnings)
    assert any("authority 'erp.purchase_orders' dropped" in w for w in warnings)
    for section in ("operations", "derivations", "sources"):
        assert any(f"lorebase.{section}" in w for w in warnings)


EDGE_CASES: dict[str, Any] = {
    "format": "lorebase.schema/v1",
    "domain": {"id": "edge", "display_name": "Edge cases", "settings": {"a": [1, 2]}},
    "kinds": {
        "Line": {
            "id": "knd_01KEZF7C00000000000000000A",
            "description": "An order line.",
            "primary_key": ["order_no", "line_no"],
            "title_attribute": "note",
            "search": {"embedding_template": "{note}"},
            "attributes": {
                "order_no": {"description": "Order.", "type": "string", "required": True},
                "line_no": {"description": "Line.", "type": "integer", "required": True},
                "note": {
                    "description": "Free text.",
                    "type": "text",
                    "constraints": {"min_length": 1, "max_length": 500},
                },
                "amounts": {
                    "description": "Amounts.",
                    "type": "array<decimal(10,2)>",
                    "constraints": {"max_length": 5},
                },
                "labels": {
                    "description": "Labels.",
                    "type": {"array": {"enum": ["red", "green"]}},
                    "constraints": {"min_length": 1},
                },
                "extra": {"description": "Extra.", "type": "json", "default": {"k": [1]}},
                "took": {"description": "Duration.", "type": "duration"},
                "where": {"description": "Point.", "type": "geo_point"},
                "ratio": {"description": "Ratio.", "type": "float", "default": 0.5},
                "code": {
                    "description": "Code.",
                    "type": "string",
                    "unique": True,
                    "constraints": {"enum_values": ["A", "B"]},
                    "sensitivity": ["pii", "confidential"],
                },
            },
            "traits": [{"trait": "Noted", "attribute_map": {"text": "note"}}],
        },
        "Part": {
            "description": "A part.",
            "primary_key": ["number"],
            "title_attribute": "number",
            "attributes": {
                "number": {"description": "Number.", "type": "string", "required": True},
                "line": {"description": "Line key.", "type": "ref(Line)"},
            },
        },
    },
    "relations": {
        "twin": {
            "description": "Twin parts.",
            "inverse_api_name": "twin",
            "from_kind": "Part",
            "to_kind": "Part",
            "cardinality": "one_to_one",
        },
        "used_on": {
            "description": "Lines a part is used on.",
            "inverse_api_name": "uses",
            "from_kind": "Part",
            "to_kind": "Line",
            "cardinality": "one_to_many",
            "required_from": True,
            "on_delete": "restrict",
            "attributes": {"quantity": {"description": "Units.", "type": "integer", "default": 1}},
        },
    },
    "traits": {
        "Noted": {
            "description": "Has a note.",
            "attributes": {"text": {"description": "Text.", "type": "text", "required": True}},
        }
    },
}


def test_features_without_a_linkml_equivalent_round_trip_through_annotations() -> None:
    back, warnings = round_trip(EDGE_CASES)

    assert warnings == []
    assert without_ids(canonical(back)) == without_ids(canonical(EDGE_CASES))
    assert errors(back) == errors(EDGE_CASES)


def test_round_trip_keeps_ids_so_renames_stay_renames() -> None:
    back, _ = round_trip(EDGE_CASES)
    assert back["kinds"]["Line"]["id"] == "knd_01KEZF7C00000000000000000A"


# Export ---------------------------------------------------------------------------------------


def test_export_maps_kinds_traits_relations_and_enums() -> None:
    schema = to_linkml(supply())
    classes = schema["classes"]
    supplier = classes["Supplier"]

    assert schema["name"] == "supply"
    assert schema["id"] == "urn:lorebase:schema:supply"
    assert schema["imports"] == ["linkml:types"]
    assert supplier["mixins"] == ["Located"]
    assert classes["Located"]["mixin"] is True
    code = supplier["attributes"]["supplier_code"]
    assert (code["identifier"], code["required"], code["pattern"]) == (True, True, r"^S-[0-9]{4,}$")
    tier = supplier["attributes"]["tier"]
    assert tier["range"] == "SupplierTier"
    assert list(schema["enums"]["SupplierTier"]["permissible_values"]) == ["1", "2", "3"]
    assert tier["ifabsent"] == "string(3)"
    assert supplier["unique_keys"] == {"vat_number_unique": {"unique_key_slots": ["vat_number"]}}
    vat = supplier["attributes"]["vat_number"]["annotations"]
    assert vat["lorebase.sensitivity"] == "confidential"
    risk = supplier["attributes"]["risk_score"]
    assert (risk["minimum_value"], risk["maximum_value"]) == (0, 100)
    cost = classes["Part"]["attributes"]["unit_cost"]
    assert (cost["range"], cost["annotations"]["lorebase.type"]) == ("decimal", "decimal(10,2)")
    assert classes["Part"]["attributes"]["hazardous"]["ifabsent"] == "False"

    supplied_by = classes["Part"]["attributes"]["supplied_by"]
    assert supplied_by["range"] == "Supplier"
    assert supplied_by["multivalued"] is True
    assert supplied_by["inverse"] == "supplies"
    supplies = supplier["attributes"]["supplies"]
    assert (supplies["range"], supplies["inverse"], supplies["multivalued"]) == (
        "Part",
        "supplied_by",
        True,
    )
    issued_to = classes["PurchaseOrder"]["attributes"]["issued_to"]
    assert "multivalued" not in issued_to
    assert issued_to["required"] is True
    assert supplier["attributes"]["purchase_orders"]["multivalued"] is True
    supplier_ref = classes["PurchaseOrder"]["attributes"]["supplier"]
    assert supplier_ref["annotations"]["lorebase.type"] == "ref(Supplier)"

    operations = json.loads(schema["annotations"]["lorebase.operations"])
    assert operations == supply()["operations"]


def test_export_uses_the_configured_schema_uri() -> None:
    schema = to_linkml(supply(), LinkmlOptions(schema_uri="https://example.org/{domain}/"))
    assert schema["id"] == "https://example.org/supply/"
    assert schema["prefixes"]["supply"] == "https://example.org/supply/"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(format="other/v1"), "format"),
        (lambda d: d.update(widgets={}), "unknown sections: widgets"),
        (lambda d: d["relations"]["flags"].update(from_kind="Nope"), "unknown Kind 'Nope'"),
        (lambda d: d["relations"]["flags"].update(cardinality="few_to_one"), "cardinality"),
        (lambda d: d["relations"]["flags"].update(inverse_api_name="status"), "already has"),
    ],
)
def test_export_refuses_documents_it_cannot_map(change: Any, message: str) -> None:
    document = supply()
    change(document)
    with pytest.raises(LinkmlError, match=message):
        to_linkml(document)


# Import ---------------------------------------------------------------------------------------


def personinfo() -> tuple[dict[str, Any], list[str]]:
    conversion = from_linkml(load_yaml(PERSONINFO.read_text(encoding="utf-8")))
    return conversion.document, [str(warning) for warning in conversion.warnings]


def test_personinfo_imports_as_a_valid_lorebase_schema() -> None:
    document, _ = personinfo()
    kinds = document["kinds"]

    assert errors(document) == []
    assert document["domain"] == {
        "id": "personinfo",
        "display_name": "Person information",
        "description": "People, the organizations they work for and where they live.",
    }
    assert list(kinds) == [
        "Person",
        "Organization",
        "Address",
        "EmploymentEvent",
        "FamilialRelationship",
    ]
    person = kinds["Person"]
    assert person["primary_key"] == ["id"]
    assert person["status"] == "draft"
    assert person["traits"] == [{"trait": "HasAliases", "attribute_map": {"aliases": "aliases"}}]
    attributes = person["attributes"]
    assert list(attributes)[:3] == ["id", "name", "description"]  # inherited from NamedThing
    assert attributes["id"] == {"type": "uri", "required": True}
    assert attributes["primary_email"]["required"] is True  # slot_usage
    assert attributes["aliases"]["type"] == "array<string>"
    assert attributes["age_in_years"]["constraints"] == {"min": 0, "max": 999}
    assert attributes["gender"]["type"] == {
        "enum": ["nonbinary_man", "nonbinary_woman", "cisgender_man", "cisgender_woman"]
    }
    assert attributes["phone"]["type"] == "string"  # custom type resolved through typeof
    assert kinds["Organization"]["attributes"]["revenue"]["type"] == "decimal(38,9)"
    assert document["traits"]["HasAliases"]["attributes"] == {"aliases": {"type": "array<string>"}}

    relations = document["relations"]
    assert relations["employed_at"] == {
        "inverse_api_name": "employees",
        "from_kind": "Person",
        "to_kind": "Organization",
        "cardinality": "many_to_one",
    }
    assert relations["has_employment_history"]["cardinality"] == "many_to_many"
    assert relations["has_employment_history"]["inverse_api_name"] == (
        "person_has_employment_history"
    )
    assert relations["related_to"]["required_from"] is True
    assert relations["employment_event_employed_at"]["from_kind"] == "EmploymentEvent"
    assert "persons" not in relations
    assert "organizations" not in relations


def test_personinfo_import_reports_what_it_leaves_out() -> None:
    _, warnings = personinfo()
    expected = [
        "schema: not supported, ignored: license, version",
        "classes/NamedThing: abstract class is not a Kind",
        "classes/Container: tree_root container class skipped",
        "classes/Person: not supported, ignored: class_uri, exact_mappings, rules",
        "classes/Person: is_a NamedThing flattened",
        "classes/Person/attributes/id: not supported, ignored: slot_uri",
        "enums/GenderType/permissible_values: not kept: meaning",
        "types/PhoneNumberType: not supported, ignored: pattern",
        "classes/Person/attributes/wake_up_time: range time has no Lorebase type",
        "classes/Person/attributes/favorite_color: not supported, ignored: any_of",
        "classes/Organization/attributes/revenue: decimal without precision",
        "classes/Person/attributes/current_address: inlined objects become linked Records",
        "relation name employed_at is taken, renamed to employment_event_employed_at",
        "classes/Person: imported as draft: add a title_attribute",
    ]
    for text in expected:
        assert any(text in warning for warning in warnings), text
    assert len(warnings) == len(set(warnings))


def test_import_uses_the_configured_decimal_and_reads_list_annotations() -> None:
    schema = {
        "name": "Shop Data",
        "annotations": [{"tag": "lorebase.default_branch", "value": "trunk"}, {"tag": "x"}],
        "classes": {
            "order line": {
                "description": "A line.",
                "attributes": {
                    "unitPrice": {"range": "decimal", "ifabsent": "float(1.5)"},
                    "count": {"range": "integer", "ifabsent": "int(2)"},
                    "flag": {"range": "boolean", "ifabsent": "true"},
                    "made": {"range": "date", "ifabsent": "bnode"},
                },
            }
        },
    }
    conversion = from_linkml(schema, LinkmlOptions(decimal_precision=12, decimal_scale=2))
    warnings = [str(warning) for warning in conversion.warnings]
    domain = conversion.document["domain"]
    line = conversion.document["kinds"]["OrderLine"]["attributes"]

    assert domain == {"id": "shop_data", "default_branch": "trunk"}
    assert line["unit_price"] == {"type": "decimal(12,2)", "default": 1.5}
    assert line["count"]["default"] == 2
    assert line["flag"]["default"] is True
    assert "default" not in line["made"]
    for text in (
        "name: Domain id 'shop_data' made from 'Shop Data'",
        "schema/annotations: not kept: x",
        "classes/order line: renamed to OrderLine",
        "classes/order line/attributes/unitPrice: renamed to unit_price",
        "ifabsent 'bnode' not supported",
    ):
        assert any(text in warning for warning in warnings), text


def test_import_refuses_what_is_not_a_schema() -> None:
    with pytest.raises(LinkmlError, match="no name"):
        from_linkml({"classes": {}})
    with pytest.raises(LinkmlError, match="mapping"):
        from_linkml(["not", "a", "schema"])
    with pytest.raises(LinkmlError, match="cycle"):
        from_linkml({"name": "x", "classes": {"A": {"is_a": "B"}, "B": {"is_a": "A"}}})


# Commands -------------------------------------------------------------------------------------


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.setenv("LOREBASE_URL", "http://keeper.test")
    monkeypatch.setenv("LOREBASE_API_KEY", "lbk_test")
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


def test_schema_export_converts_a_file_or_the_domain_schema(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    offline = runner.invoke(app, ["schema", "export", "--format", "linkml", "--file", str(SUPPLY)])
    assert offline.exit_code == 0, offline.output
    assert load_yaml(offline.stdout) == to_linkml(supply())

    route = http.get("/domains/supply/schema", params={"format": "yaml"}).respond(
        200, text=SUPPLY.read_text(encoding="utf-8"), headers={"content-type": "text/yaml"}
    )
    target = tmp_path / "supply.linkml.yaml"
    done = runner.invoke(
        app, ["schema", "export", "--format", "linkml", "-d", "supply", "-o", str(target)]
    )
    assert done.exit_code == 0, done.output
    assert route.called
    assert load_yaml(target.read_text(encoding="utf-8"))["name"] == "supply"


def test_schema_export_rejects_a_document_it_cannot_map(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("format: other/v1\ndomain: {id: x}\n", encoding="utf-8")
    result = runner.invoke(app, ["schema", "export", "--format", "linkml", "--file", str(bad)])
    assert result.exit_code == 2
    assert "format" in result.stderr


def test_schema_import_prints_the_document_and_warnings() -> None:
    result = runner.invoke(app, ["schema", "import", str(PERSONINFO), "--from", "linkml"])
    assert result.exit_code == 0, result.output
    assert load_yaml(result.stdout) == personinfo()[0]
    assert "warning: classes/Container: tree_root" in result.stderr

    as_json = runner.invoke(
        app, ["schema", "import", str(PERSONINFO), "--from", "linkml", "--json"]
    )
    payload = json.loads(as_json.stdout)
    assert payload["document"]["domain"]["id"] == "personinfo"
    container = "tree_root container class skipped with its slots"
    assert {"path": "classes/Container", "message": container} in payload["warnings"]


def test_schema_import_dry_run_plans_the_change_on_a_branch(http: respx.MockRouter) -> None:
    http.get("/domains/crm/schema", params={"branch": "work", "format": "yaml"}).respond(
        404, json={"code": "not_found", "message": "no such Domain"}
    )
    route = http.post("/domains/crm/branches/work/schema/changes").respond(
        200,
        json={
            "status": "dry_run",
            "branch": "work",
            "head": "ab" * 32,
            "revision": None,
            "changes": [{"op": "create", "section": "kinds", "id": "k", "api_name": "Person"}],
            "change_count": 1,
            "data_changes": 0,
            "issues": [],
            "issue_count": 0,
        },
    )
    result = runner.invoke(
        app,
        [*IMPORT, "-d", "crm", "-b", "work", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "dry_run" in result.stdout
    sent = json.loads(route.calls[0].request.content)
    assert sent["dry_run"] is True
    document = load_yaml(sent["yaml"])
    assert document["domain"]["id"] == "crm"
    assert set(document["kinds"]) >= {"Person", "Organization"}


# Applying to an existing Domain ---------------------------------------------------------------

#: Annotations a hand-written LinkML schema does not have: ids and what LinkML cannot carry.
_EXPORT_ONLY_TAGS = {
    f"lorebase.{tag}"
    for tag in ("id", "operations", "derivations", "sources", "authority", "derivation")
}


def hand_written(
    document: dict[str, Any], drop_attribute: tuple[str, str] | None = None, drop_kind: str = ""
) -> dict[str, Any]:
    """The LinkML schema of a Lorebase document as someone would write it by hand."""
    schema = to_linkml(document)

    def strip(node: Any) -> None:
        if isinstance(node, dict):
            for tag in _EXPORT_ONLY_TAGS & set(node.get("annotations") or {}):
                del node["annotations"][tag]
            for value in node.values():
                strip(value)

    strip(schema)
    if drop_attribute:
        del schema["classes"][drop_attribute[0]]["attributes"][drop_attribute[1]]
    if drop_kind:
        del schema["classes"][drop_kind]
        for cls in schema["classes"].values():
            for name, slot in list(cls.get("attributes", {}).items()):
                if slot.get("range") == drop_kind:
                    del cls["attributes"][name]
    return schema


def merged(schema: dict[str, Any], *, prune: bool = False) -> tuple[dict[str, Any], list[str]]:
    result = merge_into_current(supply(), from_linkml(schema), prune=prune)
    return result.document, [str(warning) for warning in result.warnings]


def test_merge_keeps_what_linkml_cannot_carry() -> None:
    document, warnings = merged(hand_written(supply()))

    assert canonical(document) == canonical(supply())
    assert set(document["operations"]) == {"flag_supplier_risk", "approve_contract"}
    assert set(document["derivations"]) == {"supplier_count", "single_sourced"}
    assert set(document["sources"]) == {"approved.suppliers", "erp.purchase_orders"}
    legal_name = document["kinds"]["Supplier"]["attributes"]["legal_name"]
    assert legal_name["authority"] == "approved.suppliers"
    assert legal_name["id"] == supply()["kinds"]["Supplier"]["attributes"]["legal_name"]["id"]
    assert not [w for w in warnings if "kept:" in w or "removed:" in w]


def test_merge_applies_what_linkml_changes() -> None:
    schema = hand_written(supply())
    country = schema["classes"]["Supplier"]["attributes"]["country"]
    country["description"] = "Country of the registered office."
    country["required"] = False
    document, _ = merged(schema)

    country_now = document["kinds"]["Supplier"]["attributes"]["country"]
    assert country_now["description"] == "Country of the registered office."
    assert "required" not in country_now
    assert country_now["indexed"] is True  # not in LinkML: kept
    assert country_now["constraints"] == {"pattern": "^[A-Z]{2}$"}


def test_merge_keeps_elements_the_linkml_schema_leaves_out_and_says_so() -> None:
    schema = hand_written(supply(), drop_attribute=("Supplier", "risk_score"), drop_kind="RiskFlag")
    document, warnings = merged(schema)

    assert "risk_score" in document["kinds"]["Supplier"]["attributes"]
    assert "RiskFlag" in document["kinds"]
    assert "flags" in document["relations"]
    assert errors(document) == errors(supply())
    assert (
        "kinds/Supplier/attributes/risk_score: kept: not in the LinkML schema (--prune removes it)"
        in (warnings)
    )
    assert "kinds/RiskFlag: kept: not in the LinkML schema (--prune removes it)" in warnings


def test_merge_with_prune_removes_them_with_their_relations() -> None:
    schema = hand_written(supply(), drop_attribute=("Supplier", "risk_score"), drop_kind="RiskFlag")
    document, warnings = merged(schema, prune=True)

    assert "risk_score" not in document["kinds"]["Supplier"]["attributes"]
    assert "RiskFlag" not in document["kinds"]
    assert "flags" not in document["relations"]
    assert (
        "kinds/Supplier/attributes/risk_score: removed: not in the LinkML schema (--prune)"
        in warnings
    )
    assert set(document["operations"]) == {"flag_supplier_risk", "approve_contract"}


def test_merge_matches_renames_by_id() -> None:
    schema = to_linkml(supply())
    schema["classes"]["Vendor"] = schema["classes"].pop("Supplier")
    for cls in schema["classes"].values():
        for slot in cls.get("attributes", {}).values():
            if slot.get("range") == "Supplier":
                slot["range"] = "Vendor"
    document, _ = merged(schema)

    assert document["kinds"]["Vendor"]["id"] == supply()["kinds"]["Supplier"]["id"]
    assert "Supplier" not in document["kinds"]
    assert document["kinds"]["Vendor"]["icon"] == "factory"


def apply_route(http: respx.MockRouter) -> respx.Route:
    return http.post("/domains/supply/branches/work/schema/changes").respond(
        200,
        json={
            "status": "dry_run",
            "branch": "work",
            "head": "ab" * 32,
            "revision": None,
            "changes": [],
            "change_count": 0,
            "data_changes": 0,
            "issues": [],
            "issue_count": 0,
        },
    )


def test_schema_import_apply_merges_into_the_current_schema(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    http.get("/domains/supply/schema", params={"branch": "work", "format": "yaml"}).respond(
        200, text=SUPPLY.read_text(encoding="utf-8"), headers={"content-type": "text/yaml"}
    )
    route = apply_route(http)
    source = tmp_path / "supply.linkml.yaml"
    source.write_text(
        dump_yaml(hand_written(supply(), drop_attribute=("Supplier", "risk_score"))),
        encoding="utf-8",
    )
    base = ["schema", "import", str(source), "--from", "linkml", "-d", "supply", "-b", "work"]

    kept = runner.invoke(app, [*base, "--dry-run"])
    assert kept.exit_code == 0, kept.output
    assert "warning: kinds/Supplier/attributes/risk_score: kept" in kept.stderr
    sent = json.loads(route.calls[0].request.content)
    assert sent["dry_run"] is True
    document = load_yaml(sent["yaml"])
    assert set(document["operations"]) == {"flag_supplier_risk", "approve_contract"}
    assert "risk_score" in document["kinds"]["Supplier"]["attributes"]

    pruned = runner.invoke(app, [*base, "--apply", "--prune"])
    assert pruned.exit_code == 0, pruned.output
    sent = json.loads(route.calls[1].request.content)
    assert sent["dry_run"] is False
    document = load_yaml(sent["yaml"])
    assert "risk_score" not in document["kinds"]["Supplier"]["attributes"]
    assert "operations" in document


def test_schema_import_print_ignores_the_current_schema(http: respx.MockRouter) -> None:
    result = runner.invoke(app, ["schema", "import", str(PERSONINFO), "--from", "linkml"])
    assert result.exit_code == 0, result.output
    assert not http.calls


# Path Derivations and merge warnings ----------------------------------------------------------

PATH_DERIVATION: dict[str, Any] = {
    "id": "drv_01KEZF7C00PATH00000000000A",
    "description": "Orders issued to the suppliers of a part.",
    "definition": {
        "variant": "path",
        "steps": [
            {"relation": "supplied_by"},
            {"relation": "issued_to", "direction": "inverse"},
        ],
        "inverse_api_name": "parts_ordered",
        "materialized": True,
    },
}


def with_path() -> dict[str, Any]:
    document = supply()
    document["derivations"]["orders_for_part"] = copy.deepcopy(PATH_DERIVATION)
    return document


def test_export_writes_a_path_derivation_as_an_annotated_slot() -> None:
    schema = to_linkml(with_path())

    slot = schema["classes"]["Part"]["attributes"]["orders_for_part"]
    assert slot["range"] == "PurchaseOrder"
    assert slot["multivalued"] is True
    assert slot["description"] == PATH_DERIVATION["description"]
    notes = slot["annotations"]
    assert notes["lorebase.derivation_variant"] == "path"
    assert notes["lorebase.id"] == PATH_DERIVATION["id"]
    assert json.loads(notes["lorebase.steps"]) == PATH_DERIVATION["definition"]["steps"]
    assert notes["lorebase.inverse_api_name"] == "parts_ordered"
    assert notes["lorebase.materialized"] is True
    kept = json.loads(schema["annotations"]["lorebase.derivations"])
    assert set(kept) == {"supplier_count", "single_sourced"}


def test_path_derivation_round_trips_and_is_not_a_relation() -> None:
    back, warnings = round_trip(with_path())

    assert back["derivations"] == {"orders_for_part": PATH_DERIVATION}
    assert "orders_for_part" not in back["relations"]
    assert "orders_for_part" not in back["kinds"]["Part"]["attributes"]
    assert not [w for w in warnings if "orders_for_part" in w]


def test_path_derivation_alone_leaves_no_derivations_annotation() -> None:
    document = supply()
    document["derivations"] = {"orders_for_part": copy.deepcopy(PATH_DERIVATION)}
    schema = to_linkml(document)

    assert "lorebase.derivations" not in schema["annotations"]
    back, warnings = round_trip(document)
    assert back["derivations"] == {"orders_for_part": PATH_DERIVATION}
    assert not [w for w in warnings if w.startswith("annotations/lorebase.derivations")]


def test_export_keeps_a_path_that_cannot_be_placed_in_the_annotation() -> None:
    document = supply()
    broken = copy.deepcopy(PATH_DERIVATION)
    broken["definition"]["steps"][0]["relation"] = "missing"
    document["derivations"]["orders_for_part"] = broken

    schema = to_linkml(document)
    assert "orders_for_part" not in schema["classes"]["Part"]["attributes"]
    assert "orders_for_part" in json.loads(schema["annotations"]["lorebase.derivations"])


def test_merge_applies_a_path_derivation_and_keeps_the_other_derivations() -> None:
    changed = copy.deepcopy(PATH_DERIVATION)
    changed["definition"]["materialized"] = False
    target = with_path()
    target["derivations"]["orders_for_part"] = changed
    result = merge_into_current(with_path(), from_linkml(to_linkml(target)))

    derivations = result.document["derivations"]
    assert set(derivations) == {"supplier_count", "single_sourced", "orders_for_part"}
    assert derivations["orders_for_part"]["definition"]["materialized"] is False
    assert derivations["orders_for_part"]["id"] == PATH_DERIVATION["id"]


def test_merge_removes_a_path_through_a_pruned_relation() -> None:
    schema = hand_written(with_path(), drop_kind="Supplier")
    result = merge_into_current(with_path(), from_linkml(schema), prune=True)

    assert "orders_for_part" not in result.document["derivations"]
    assert "derivations/orders_for_part: removed: it refers to a pruned Relation" in [
        str(w) for w in result.warnings
    ]


def test_merge_does_not_say_preserved_sections_are_not_imported() -> None:
    imported = from_linkml(to_linkml(supply()))
    assert any("not imported" in str(w) for w in imported.warnings)

    kept = merge_into_current(supply(), imported)
    assert not [w for w in kept.warnings if "not imported" in str(w)]

    bare = {k: v for k, v in supply().items() if k not in ("operations", "derivations", "sources")}
    lost = merge_into_current(bare, imported)
    assert any("not imported" in str(w) for w in lost.warnings)


def test_schema_import_prune_needs_apply_or_dry_run(http: respx.MockRouter) -> None:
    result = runner.invoke(app, [*IMPORT, "--prune"])

    assert result.exit_code == 1
    assert "--prune needs --apply or --dry-run" in result.output
    assert not http.calls


def without_path_slot(document: dict[str, Any]) -> dict[str, Any]:
    schema = to_linkml(document)
    del schema["classes"]["Part"]["attributes"]["orders_for_part"]
    return schema


def test_merge_keeps_a_path_the_linkml_schema_leaves_out_and_says_so() -> None:
    result = merge_into_current(with_path(), from_linkml(without_path_slot(with_path())))

    assert result.document["derivations"]["orders_for_part"] == PATH_DERIVATION
    assert "derivations/orders_for_part: kept: not in the LinkML schema (--prune removes it)" in [
        str(w) for w in result.warnings
    ]


def test_merge_with_prune_removes_a_path_the_linkml_schema_leaves_out() -> None:
    result = merge_into_current(
        with_path(), from_linkml(without_path_slot(with_path())), prune=True
    )

    assert "orders_for_part" not in result.document["derivations"]
    assert "derivations/orders_for_part: removed: not in the LinkML schema (--prune)" in [
        str(w) for w in result.warnings
    ]


def test_merge_renames_the_steps_of_a_kept_path_with_the_relation() -> None:
    schema = without_path_slot(with_path())
    part = schema["classes"]["Part"]["attributes"]
    part["sourced_from"] = part.pop("supplied_by")
    schema["classes"]["Supplier"]["attributes"]["supplies"]["inverse"] = "sourced_from"
    result = merge_into_current(with_path(), from_linkml(schema))

    assert "sourced_from" in result.document["relations"]
    steps = result.document["derivations"]["orders_for_part"]["definition"]["steps"]
    assert steps[0] == {"relation": "sourced_from"}


def test_merge_does_not_overwrite_another_kind_of_derivation_by_name() -> None:
    schema = to_linkml(with_path())
    part = schema["classes"]["Part"]["attributes"]
    slot = part.pop("orders_for_part")
    del slot["annotations"]["lorebase.id"]
    part["supplier_count"] = slot
    result = merge_into_current(supply(), from_linkml(schema))

    assert result.document["derivations"]["supplier_count"]["definition"]["variant"] == "aggregate"
    assert any(str(w).startswith("derivations/supplier_count: skipped") for w in result.warnings)


def test_export_refuses_a_path_whose_name_is_taken_on_its_start_class() -> None:
    document = with_path()
    document["derivations"]["supplies"] = document["derivations"].pop("orders_for_part")
    document["kinds"]["Part"]["attributes"]["supplies"] = {"description": "x", "type": "string"}

    with pytest.raises(LinkmlError, match=r"derivations/supplies: .*already has"):
        to_linkml(document)


def test_export_keeps_a_path_with_fields_a_slot_cannot_carry_in_the_annotation() -> None:
    document = with_path()
    document["derivations"]["orders_for_part"]["definition"]["extra"] = 1

    schema = to_linkml(document)
    assert "orders_for_part" not in schema["classes"]["Part"]["attributes"]
    assert "orders_for_part" in json.loads(schema["annotations"]["lorebase.derivations"])
