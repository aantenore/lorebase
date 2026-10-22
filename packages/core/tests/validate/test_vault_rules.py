"""Vault storage and sensitivity rules (design 13.5): schema checks and vault references."""

from __future__ import annotations

from typing import Any

import pytest

from lorebase_core.expr.sensitivity import effective_tags, lowered_tags, with_tags
from lorebase_core.model import (
    AggregateDerivation,
    AggregateFunction,
    Attribute,
    KindSearch,
    Precondition,
    Record,
    Schema,
    Sensitivity,
    validate_schema,
    vault_reference,
    vault_token,
)
from lorebase_core.types import TypeSpec
from lorebase_core.validate import (
    IssueCode,
    SchemaRules,
    Severity,
    operation_sensitivity_issues,
    validate_record,
)

from ..model.supply import (
    FLAG_SUPPLIER_RISK,
    LEGAL_NAME,
    PART,
    PLANT,
    PLANT_CITY,
    REASON,
    RISK_SCORE,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_CITY,
    SUPPLIER_CODE,
    SUPPLIER_COUNT,
    SUPPLIER_COUNT_DRV,
    TIER,
    VAT,
    add_attribute,
    ident,
    replace_attribute,
    replace_kind,
    supply_schema,
)

C = IssueCode
NOTES = ident("att", 199)
PII = (Sensitivity.PII,)
TOKEN = "vlt_" + "0" * 32


def vault(schema: Schema, attribute_id: str, **changes: Any) -> Schema:
    update: dict[str, Any] = {"sensitivity": PII, "vault": True, **changes}
    return replace_attribute(schema, SUPPLIER, attribute_id, **update)


def issues(schema: Schema) -> list[tuple[IssueCode, Severity, tuple[Any, ...]]]:
    return [(i.code, i.severity, i.path) for i in validate_schema(schema)]


def test_a_pii_attribute_may_use_the_vault() -> None:
    assert issues(vault(supply_schema(), SUPPLIER_CITY)) == []


def test_vault_is_left_out_of_content_until_set() -> None:
    attribute = supply_schema().kinds[SUPPLIER].attributes[SUPPLIER_CITY]
    assert "vault" not in attribute.content()
    assert attribute.model_copy(update={"vault": True}).content()["vault"] is True


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"sensitivity": ()}, "sensitivity"),
        ({"unique": True}, "unique"),
        ({"indexed": True}, "indexed"),
    ],
)
def test_vault_attributes_have_limits(changes: dict[str, Any], field: str) -> None:
    schema = vault(supply_schema(), SUPPLIER_CITY, **changes)
    found = [i for i in validate_schema(schema) if i.code is C.SCHEMA_VAULT_INVALID]
    assert [i.path[-1] for i in found] == [field]


def test_vault_attributes_cannot_key_or_label_records() -> None:
    keyed = vault(supply_schema(), SUPPLIER_CODE)
    assert ("kinds", SUPPLIER, "primary_key", 0) in {
        i.path for i in validate_schema(keyed) if i.code is C.SCHEMA_VAULT_INVALID
    }
    titled = vault(supply_schema(), LEGAL_NAME, searchable=False)
    assert [i.path for i in validate_schema(titled) if i.code is C.SCHEMA_VAULT_INVALID] == [
        ("kinds", SUPPLIER, "title_attribute")
    ]


def test_sensitive_search_and_titles_are_warned_about() -> None:
    schema = replace_attribute(supply_schema(), SUPPLIER, LEGAL_NAME, sensitivity=PII)
    found = [(i.code, i.severity, i.path[-1]) for i in validate_schema(schema)]
    assert (C.SCHEMA_SENSITIVE_ATTRIBUTE, Severity.WARNING, "searchable") in found
    assert (C.SCHEMA_SENSITIVE_ATTRIBUTE, Severity.WARNING, "title_attribute") in found
    assert not [i for i in validate_schema(schema) if i.is_error]


def test_embedding_templates_never_read_vault_values() -> None:
    schema = vault(supply_schema(), SUPPLIER_CITY)
    schema = replace_kind(
        schema,
        SUPPLIER,
        search=KindSearch(embedding_template="{legal_name} {city}", embedding_sensitive=True),
    )
    assert C.SCHEMA_EMBEDDING_TEMPLATE_INVALID in {i.code for i in validate_schema(schema)}


def test_references_are_accepted_for_vault_attributes_only() -> None:
    schema = vault(supply_schema(), SUPPLIER_CITY)
    values = {
        SUPPLIER_CODE: "S-1",
        LEGAL_NAME: "Acme",
        ident_country(schema): "IT",
        SUPPLIER_CITY: vault_reference(TOKEN),
    }
    record = Record(kind=SUPPLIER, key="S-1", values=values)
    assert validate_record(schema, record) == []
    copied = record.model_copy(update={"values": {**values, VAT: vault_reference(TOKEN)}})
    assert [i.code for i in validate_record(schema, copied)] == [C.VALUE_VAULT_REFERENCE]
    forged = record.model_copy(update={"values": {**values, SUPPLIER_CITY: {"$vault": "x"}}})
    assert [i.code for i in validate_record(schema, forged)] == [C.VALUE_VAULT_REFERENCE]


def test_references_nested_inside_other_values_are_refused() -> None:
    schema = vault(supply_schema(), SUPPLIER_CITY)
    notes = Attribute(id=NOTES, api_name="notes", description="Notes.", type=TypeSpec.of("json"))
    schema = add_attribute(schema, SUPPLIER, notes)
    values = {
        SUPPLIER_CODE: "S-1",
        LEGAL_NAME: "Acme",
        ident_country(schema): "IT",
        SUPPLIER_CITY: vault_reference(TOKEN),
    }
    record = Record(kind=SUPPLIER, key="S-1", values=values)
    plain = {"notes": [{"vault": "no"}, {"text": "fine"}]}
    assert validate_record(schema, with_value(record, plain)) == []
    for nested in (
        {"copy": vault_reference(TOKEN)},
        [1, {"deep": [vault_reference(TOKEN)]}],
        {"looks": {"$vault": "anything"}},
    ):
        found = validate_record(schema, with_value(record, nested))
        assert [(i.code, i.attribute) for i in found] == [(C.VALUE_VAULT_REFERENCE, NOTES)]


def with_value(record: Record, value: Any) -> Record:
    return record.model_copy(update={"values": {**record.values, NOTES: value}})


def test_vault_token_recognizes_well_formed_references_only() -> None:
    assert vault_token(vault_reference(TOKEN)) == TOKEN
    assert vault_token({"$vault": TOKEN, "x": 1}) is None
    assert vault_token({"$vault": "vlt_short"}) is None
    assert vault_token(TOKEN) is None


def ident_country(schema: Schema) -> str:
    kind = schema.kinds[SUPPLIER]
    found: Attribute | None = kind.attribute_by_api_name("country")
    assert found is not None
    return found.id


def test_sensitive_keys_are_refused() -> None:
    keyed = replace_attribute(supply_schema(), SUPPLIER, SUPPLIER_CODE, sensitivity=PII)
    found = [(i.code, i.path) for i in validate_schema(keyed) if i.is_error]
    assert (C.SCHEMA_SENSITIVE_ATTRIBUTE, ("kinds", SUPPLIER, "primary_key", 0)) in found


def _max_risk(schema: Schema) -> Schema:
    derivation = schema.derivations[SUPPLIER_COUNT_DRV].model_copy(
        update={
            "definition": AggregateDerivation(
                kind=PART,
                attribute=SUPPLIER_COUNT,
                relation=SUPPLIED_BY,
                function=AggregateFunction.MAX,
                source_attribute=RISK_SCORE,
            )
        }
    )
    return schema.model_copy(update={"derivations": {SUPPLIER_COUNT_DRV: derivation}})


def test_derived_attributes_inherit_the_tags_of_what_they_read() -> None:
    sensitive = replace_attribute(supply_schema(), SUPPLIER, RISK_SCORE, sensitivity=PII)
    assert issues(_max_risk(supply_schema())) == []
    leaking = _max_risk(sensitive)
    assert [(i.code, i.path[-1]) for i in validate_schema(leaking)] == [
        (C.SCHEMA_SENSITIVITY_INHERITED, "sensitivity")
    ]
    tagged = replace_attribute(_max_risk(sensitive), PART, SUPPLIER_COUNT, sensitivity=PII)
    assert issues(tagged) == []
    vaulted = replace_attribute(tagged, SUPPLIER, RISK_SCORE, vault=True)
    assert C.SCHEMA_VAULT_INVALID in {i.code for i in validate_schema(vaulted)}


RULES = SchemaRules(allow_expressions=True)


def _with_operation(schema: Schema, **changes: Any) -> Schema:
    operation = supply_schema(with_operation=True).operations[FLAG_SUPPLIER_RISK]
    return schema.model_copy(
        update={"operations": {FLAG_SUPPLIER_RISK: operation.model_copy(update=changes)}}
    )


def test_operation_step_values_inherit_the_tags_of_what_they_read() -> None:
    operation = supply_schema(with_operation=True).operations[FLAG_SUPPLIER_RISK]
    step = operation.steps[0]
    copying = step.model_copy(
        update={"values": {**step.values, REASON: "record(params.supplier).legal_name"}}
    )
    steps = (copying, *operation.steps[1:])
    sensitive = replace_attribute(supply_schema(), SUPPLIER, LEGAL_NAME, sensitivity=PII)
    plain = list(validate_schema(_with_operation(supply_schema(), steps=steps), RULES))
    assert not [i for i in plain if i.is_error]
    found = validate_schema(_with_operation(sensitive, steps=steps), RULES)
    assert [(i.code, i.path[-1]) for i in found if i.is_error] == [
        (C.SCHEMA_SENSITIVITY_INHERITED, REASON)
    ]
    # Parameters are not Record values: copying one stays allowed.
    assert not [i for i in validate_schema(_with_operation(sensitive), RULES) if i.is_error]


def test_preconditions_read_no_sensitive_attribute() -> None:
    sensitive = replace_attribute(supply_schema(), SUPPLIER, TIER, sensitivity=PII)
    found = validate_schema(_with_operation(sensitive), RULES)
    assert [(i.code, i.path[-1]) for i in found if i.is_error] == [
        (C.SCHEMA_SENSITIVITY_INHERITED, "expression")
    ]


def test_effective_tags_follow_the_reference_and_derivations() -> None:
    reference = replace_attribute(supply_schema(), SUPPLIER, RISK_SCORE, sensitivity=PII)
    branch = _max_risk(supply_schema())  # forked before the tag: untagged, with a derived max
    tags = effective_tags(branch, {RISK_SCORE: frozenset(PII)})
    assert tags[RISK_SCORE] == frozenset(PII)
    assert tags[SUPPLIER_COUNT] == frozenset(PII)  # derived from it on another Kind
    assert lowered_tags(reference, supply_schema()) == frozenset(PII)
    raised = with_tags(branch, {RISK_SCORE: frozenset(PII)})
    assert raised.kinds[SUPPLIER].attributes[RISK_SCORE].sensitivity == PII
    # Validated with the reference's tags, the untagged derived copy is refused.
    assert C.SCHEMA_SENSITIVITY_INHERITED in {i.code for i in validate_schema(raised)}


def test_step_targets_read_no_sensitive_attribute() -> None:
    operation = supply_schema(with_operation=True).operations[FLAG_SUPPLIER_RISK]
    link = operation.steps[1].model_copy(update={"to": "record(params.supplier).legal_name"})
    steps = (operation.steps[0], link)
    sensitive = replace_attribute(supply_schema(), SUPPLIER, LEGAL_NAME, sensitivity=PII)
    found = validate_schema(_with_operation(sensitive, steps=steps), RULES)
    assert [(i.code, i.path[-1]) for i in found if i.is_error] == [
        (C.SCHEMA_SENSITIVITY_INHERITED, "to")
    ]


def test_an_operation_is_checked_against_the_default_branch_tags() -> None:
    # Valid on a branch forked before the default branch tagged tier; not with its tags.
    operation = supply_schema(with_operation=True).operations[FLAG_SUPPLIER_RISK]
    branch = _with_operation(supply_schema())
    assert operation_sensitivity_issues(branch, operation, RULES) == []
    raised = with_tags(branch, {TIER: frozenset(PII)})
    assert [i.code for i in operation_sensitivity_issues(raised, operation, RULES)] == [
        C.SCHEMA_SENSITIVITY_INHERITED
    ]


def test_fields_resolve_against_the_kind_an_expression_addresses() -> None:
    # Plant.city is personal data, Supplier.city is not: a precondition on the Supplier's
    # city reads no sensitive value.
    sensitive = replace_attribute(supply_schema(), PLANT, PLANT_CITY, sensitivity=PII)
    precondition = Precondition(expression='record(supplier).city != ""', message="No city.")
    schema = _with_operation(sensitive, preconditions=(precondition,))
    assert not [i for i in validate_schema(schema, RULES) if i.is_error]
    # An expression whose Kind cannot be told is matched by name, conservatively.
    loose = Precondition(expression='[record(supplier)].exists(r, r.city != "")', message="x")
    found = validate_schema(_with_operation(sensitive, preconditions=(loose,)), RULES)
    assert C.SCHEMA_SENSITIVITY_INHERITED in {i.code for i in found}
