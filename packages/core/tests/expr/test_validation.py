"""Static validation of the supply schema's expressions through ``validate_schema``."""

from __future__ import annotations

from lorebase_core.expr import compile_derivation, derivation_inputs, links_read
from lorebase_core.model import load_schema, validate_schema, validate_schema_yaml
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


def test_derivation_inputs_name_the_derived_attributes_read() -> None:
    schema = supply()
    single = schema.derivation_by_api_name("single_sourced")
    count = schema.derivation_by_api_name("supplier_count")
    assert single is not None
    assert count is not None
    part = schema.kind_by_api_name("Part")
    assert part is not None
    supplier_count = part.attribute_by_api_name("supplier_count")
    assert supplier_count is not None
    assert derivation_inputs(schema, single) == {supplier_count.id}
    assert derivation_inputs(schema, count) == frozenset()
    compiled = compile_derivation(schema, single).expression
    assert compiled is not None
    assert links_read(compiled) == []


def _with_expression(expression: str) -> str:
    return SUPPLY_SCHEMA.read_text(encoding="utf-8").replace(
        "expression: supplier_count == 1", f"expression: {expression}"
    )


def test_links_read_by_an_expression() -> None:
    schema = load_schema(_with_expression("size(links.supplied_by) == 1"))
    single = schema.derivation_by_api_name("single_sourced")
    assert single is not None
    compiled = compile_derivation(schema, single)
    assert compiled.expression is not None
    [field] = links_read(compiled.expression)
    assert field.relation.api_name == "supplied_by"


def test_derivation_cycles_through_expressions_are_reported() -> None:
    itself = validate_schema_yaml(_with_expression("single_sourced || false"), rules=ENABLED)
    assert {i.code for i in itself} == {IssueCode.SCHEMA_DERIVATION_CYCLE}
    text = _with_expression("supplier_count == 1").replace(
        "      relation: supplied_by\n      function: count",
        "      relation: supplied_by\n      function: count\n      filter: 'status == \"active\"'",
    )
    assert not validate_schema_yaml(text, rules=ENABLED)
