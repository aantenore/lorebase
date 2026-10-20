"""Static validation of the supply schema's expressions through ``validate_schema``."""

from __future__ import annotations

from lorebase_core.model import validate_schema, validate_schema_yaml
from lorebase_core.validate import IssueCode, SchemaRules

from .support import SUPPLY_SCHEMA, supply

ENABLED = SchemaRules(allow_expressions=True)


def test_supply_schema_has_no_issues_with_expressions_enabled() -> None:
    assert validate_schema(supply(), ENABLED) == []


def test_expressions_stay_gated_by_default() -> None:
    codes = {issue.code for issue in validate_schema(supply())}
    assert codes == {IssueCode.SCHEMA_EXPRESSIONS_DISABLED}
    assert SchemaRules().allow_expressions is False


def test_broken_expressions_are_reported_with_yaml_paths() -> None:
    text = SUPPLY_SCHEMA.read_text(encoding="utf-8")
    broken = text.replace(
        'record(contract).status == "draft"', 'record(contract).status == "drafted"'
    )
    broken = broken.replace("expression: supplier_count == 1", "expression: supplier_count == '1'")
    issues = validate_schema_yaml(broken, rules=ENABLED)
    assert {issue.code for issue in issues} == {IssueCode.SCHEMA_EXPRESSION_INVALID}
    by_path = {issue.path: issue for issue in issues}
    precondition = by_path[("operations", "approve_contract", "preconditions", 0, "expression")]
    assert "'drafted' is not a value of enum" in precondition.message
    assert precondition.details["column"] == 1
    derivation = by_path[("derivations", "single_sourced", "definition", "expression")]
    assert "cannot compare int with string" in derivation.message
