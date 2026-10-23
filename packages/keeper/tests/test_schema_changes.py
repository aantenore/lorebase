"""Schema change sets without a database: documents, patches and the element diff."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from lorebase_core.model import Schema, load_schema
from lorebase_keeper.services.schema import (
    ChangeSetInput,
    ChangeSetInvalidError,
    ElementRemoval,
    diff_schemas,
    static_issues,
    target_schema,
)
from lorebase_store.commit import SchemaDelete, SchemaUpsert
from lorebase_store.settings import SchemaSettings

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"


RULES = SchemaSettings().rules()


def supply_document() -> dict[str, Any]:
    """The example schema, CEL Operations and Derivations included."""
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    return document


@pytest.fixture
def supply() -> Schema:
    return load_schema(yaml.safe_dump(supply_document()))


@pytest.fixture
def empty() -> Schema:
    return load_schema(yaml.safe_dump({"domain": supply_document()["domain"]}))


def test_full_document_creates_every_element(empty: Schema, supply: Schema) -> None:
    target = target_schema(empty, ChangeSetInput(yaml=yaml.safe_dump(supply_document())))
    assert static_issues(target, RULES) == []
    changes, described = diff_schemas(empty, target)
    created = {(c.section, c.api_name) for c in described if c.op == "create"}
    assert {("kinds", "Supplier"), ("relations", "supplied_by"), ("traits", "Located")} <= created
    assert all(isinstance(c, SchemaUpsert) for c in changes)
    assert target.kind_by_api_name("Supplier").id == supply.kind_by_api_name("Supplier").id  # type: ignore[union-attr]


def test_same_document_changes_nothing(supply: Schema) -> None:
    target = target_schema(supply, ChangeSetInput(yaml=yaml.safe_dump(supply_document())))
    assert diff_schemas(supply, target) == ([], [])


def test_patch_keeps_ids_and_updates_one_element(supply: Schema) -> None:
    plant = supply_document()["kinds"]["Plant"]
    plant.pop("id")
    plant["description"] = "A site that receives parts."
    target = target_schema(supply, ChangeSetInput(patch={"kinds": {"Plant": plant}}))
    changes, described = diff_schemas(supply, target)
    assert [(c.op, c.section, c.api_name) for c in described] == [("update", "kinds", "Plant")]
    assert target.kind_by_api_name("Plant").id == supply.kind_by_api_name("Plant").id  # type: ignore[union-attr]
    assert len(changes) == 1


def test_removal_by_api_name_or_id(supply: Schema) -> None:
    relation = supply.relation_by_api_name("with_supplier")
    assert relation is not None
    by_name = target_schema(
        supply, ChangeSetInput(remove=[ElementRemoval("relations", "with_supplier")])
    )
    by_id = target_schema(
        supply, ChangeSetInput(remove=[ElementRemoval("relations", relation[0].id)])
    )
    for target in (by_name, by_id):
        changes, described = diff_schemas(supply, target)
        assert changes == [SchemaDelete(relation[0].id)]
        assert described[0].op == "delete"


def test_unknown_removal_is_located(supply: Schema) -> None:
    with pytest.raises(ChangeSetInvalidError) as raised:
        target_schema(supply, ChangeSetInput(remove=[ElementRemoval("kinds", "Nope")]))
    assert raised.value.issues[0].path == ("kinds", "Nope")


def test_unreadable_document_is_located(supply: Schema) -> None:
    document = supply_document()
    document["kinds"]["Supplier"]["attributes"]["tier"]["type"] = "no_such_type"
    with pytest.raises(ChangeSetInvalidError) as raised:
        target_schema(supply, ChangeSetInput(yaml=yaml.safe_dump(document)))
    paths = [finding.path for finding in raised.value.issues]
    assert any(path[:4] == ("kinds", "Supplier", "attributes", "tier") for path in paths)


def test_static_issues_use_api_names(supply: Schema) -> None:
    document = supply_document()
    document["kinds"]["Supplier"]["attributes"]["risk_score"]["constraints"] = {"min": 9, "max": 1}
    target = target_schema(supply, ChangeSetInput(yaml=yaml.safe_dump(document)))
    issues = static_issues(target, RULES)
    assert issues
    assert issues[0].path[:4] == ("kinds", "Supplier", "attributes", "risk_score")


def test_expressions_follow_the_schema_settings(supply: Schema) -> None:
    target = target_schema(supply, ChangeSetInput(yaml=yaml.safe_dump(supply_document())))
    assert static_issues(target, RULES) == []
    disabled = SchemaSettings(expressions_enabled=False).rules()
    codes = {finding.code for finding in static_issues(target, disabled)}
    assert codes == {"schema.expressions_disabled"}
    document = supply_document()
    document["derivations"]["single_sourced"]["definition"]["expression"] = "supplier_count + 1"
    broken = target_schema(supply, ChangeSetInput(yaml=yaml.safe_dump(document)))
    (issue,) = static_issues(broken, RULES)
    assert issue.code == "schema.expression_invalid"


def test_domain_id_cannot_change(supply: Schema) -> None:
    document = supply_document()
    document["domain"]["id"] = "other"
    with pytest.raises(ChangeSetInvalidError):
        target_schema(supply, ChangeSetInput(yaml=yaml.safe_dump(document)))


def test_domain_examples_are_a_domain_change(supply: Schema) -> None:
    document = supply_document()
    assert document["domain"]["examples"][0]["label"] == "Single-source risk"
    document["domain"]["examples"] = [{"label": "Overview", "question": "What changed today?"}]
    target = target_schema(supply, ChangeSetInput(yaml=yaml.safe_dump(document)))
    changes, described = diff_schemas(supply, target)
    assert [(c.op, c.section) for c in described] == [("update", "domain")]
    assert len(changes) == 1
    assert target.content()["domain"]["examples"] == [
        {"label": "Overview", "question": "What changed today?"}
    ]
