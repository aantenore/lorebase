from __future__ import annotations

from collections.abc import Callable

import pytest

from lorebase_core.expr import ExprLimits
from lorebase_core.model import (
    AggregateDerivation,
    AggregateFunction,
    Attribute,
    Cardinality,
    CreateLinkStep,
    CreateRecordStep,
    Derivation,
    ElementStatus,
    HttpOptions,
    KindSearch,
    LinkDirection,
    OperationParameter,
    OperationStep,
    PathDerivation,
    PathStep,
    Precondition,
    Relation,
    RelationBacking,
    RelationMapping,
    Schema,
    SourceType,
    SyncMode,
    TraitAttribute,
    TraitImplementation,
    validate_schema,
)
from lorebase_core.types import Constraints, TypeSpec
from lorebase_core.validate import IssueCode, SchemaRules, Severity

from ..model.supply import (
    CONTRACT,
    CONTRACT_VALUE,
    ERP_VENDORS,
    FLAG_SUPPLIER_RISK,
    FLAGS,
    IS_PRIMARY,
    LEGAL_NAME,
    LOCATED,
    LOCATED_COUNTRY,
    MADE_AT,
    PART,
    PART_NAME,
    PLANT,
    PLANT_CODE,
    PLANT_NAME,
    RISK_FLAG,
    RISK_SCORE,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_CITY,
    SUPPLIER_CODE,
    SUPPLIER_COUNT,
    SUPPLIER_COUNT_DRV,
    SUPPLIER_COUNTRY,
    TIER,
    VAT,
    WITH_SUPPLIER,
    add_attribute,
    attribute,
    ident,
    replace_attribute,
    replace_element,
    replace_kind,
    replace_relation,
    supply_schema,
)

C = IssueCode
T = TypeSpec.of
EXPRESSIONS = SchemaRules(allow_expressions=True)
Mutation = Callable[[Schema], Schema]


def error_codes(schema: Schema, rules: SchemaRules | None = None) -> set[IssueCode]:
    return {issue.code for issue in validate_schema(schema, rules) if issue.is_error}


def with_operation(schema: Schema) -> Schema:
    return schema.model_copy(update={"operations": supply_schema(with_operation=True).operations})


def set_step(schema: Schema, index: int, step: OperationStep) -> Schema:
    schema = with_operation(schema)
    operation = schema.operations[FLAG_SUPPLIER_RISK]
    steps = list(operation.steps)
    steps[index] = step
    return replace_element(schema, "operations", FLAG_SUPPLIER_RISK, steps=tuple(steps))


def flag_parameters() -> tuple[OperationParameter, ...]:
    return supply_schema(with_operation=True).operations[FLAG_SUPPLIER_RISK].parameters


def add_relation(schema: Schema, relation: Relation) -> Schema:
    return schema.model_copy(update={"relations": {**schema.relations, relation.id: relation}})


def add_derivation(schema: Schema, derivation: Derivation) -> Schema:
    return schema.model_copy(
        update={"derivations": {**schema.derivations, derivation.id: derivation}}
    )


def set_trait_map(schema: Schema, kind_id: str, mapping: dict[str, str]) -> Schema:
    implementation = TraitImplementation(trait_id=LOCATED, attribute_map=mapping)
    return replace_kind(schema, kind_id, traits=(implementation,))


def set_aggregate(schema: Schema, **changes: object) -> Schema:
    derivation = schema.derivations[SUPPLIER_COUNT_DRV]
    definition = derivation.definition.model_copy(update=changes)
    return replace_element(schema, "derivations", SUPPLIER_COUNT_DRV, definition=definition)


def derivation_cycle(schema: Schema) -> Schema:
    supplier_max = ident("att", 900)
    second = ident("drv", 2)
    schema = add_attribute(
        schema, SUPPLIER, attribute(supplier_max, "max_parts", T("integer"), derivation_id=second)
    )
    schema = set_aggregate(schema, function=AggregateFunction.MAX, source_attribute=supplier_max)
    return add_derivation(
        schema,
        Derivation(
            id=second,
            api_name="max_parts",
            definition=AggregateDerivation(
                kind=SUPPLIER,
                attribute=supplier_max,
                relation=SUPPLIED_BY,
                direction=LinkDirection.INVERSE,
                function=AggregateFunction.MAX,
                source_attribute=SUPPLIER_COUNT,
            ),
        ),
    )


def path_derivation(*steps: PathStep, name: str = "supplier_plants") -> Derivation:
    return Derivation(id=ident("drv", 3), api_name=name, definition=PathDerivation(steps=steps))


def set_source(schema: Schema, **changes: object) -> Schema:
    return replace_element(schema, "sources", ERP_VENDORS, **changes)


# Positive cases -------------------------------------------------------------------


def test_fixture_schema_is_valid() -> None:
    assert validate_schema(supply_schema()) == []


def test_operation_with_expressions_is_valid_when_enabled() -> None:
    assert validate_schema(supply_schema(with_operation=True), EXPRESSIONS) == []


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda s: add_relation(
                s,
                Relation(
                    id=ident("rel", 9),
                    api_name="partners",
                    inverse_api_name="partners",
                    from_kind=SUPPLIER,
                    to_kind=SUPPLIER,
                ),
            ),
            id="symmetric-self-relation",
        ),
        pytest.param(
            lambda s: replace_kind(s, PLANT, status=ElementStatus.DRAFT, title_attribute=None),
            id="draft-kind-without-title",
        ),
        pytest.param(
            lambda s: replace_kind(
                replace_attribute(s, SUPPLIER, LEGAL_NAME, description=""),
                SUPPLIER,
                status=ElementStatus.DRAFT,
            ),
            id="draft-kind-without-descriptions",
        ),
        pytest.param(
            lambda s: add_derivation(
                s,
                path_derivation(
                    PathStep(relation=SUPPLIED_BY),
                    PathStep(relation=WITH_SUPPLIER, direction=LinkDirection.INVERSE),
                ),
            ),
            id="connected-path-derivation",
        ),
        pytest.param(
            lambda s: replace_attribute(s, SUPPLIER, RISK_SCORE, default="50"),
            id="valid-default",
        ),
    ],
)
def test_valid_variations(mutation: Mutation) -> None:
    assert error_codes(mutation(supply_schema())) == set()


def test_unique_attribute_with_default_is_a_warning() -> None:
    schema = replace_attribute(supply_schema(), SUPPLIER, VAT, default="X")
    issues = validate_schema(schema)
    assert [(i.code, i.severity) for i in issues] == [(C.SCHEMA_DEFAULT_CONFLICT, Severity.WARNING)]


# Negative cases: one rule each ----------------------------------------------------

NEGATIVE: list[tuple[str, Mutation, set[IssueCode], SchemaRules | None]] = [
    # Names
    (
        "kind-name-style",
        lambda s: replace_kind(s, PLANT, api_name="plant"),
        {C.SCHEMA_API_NAME_INVALID},
        None,
    ),
    (
        "attribute-name-style",
        lambda s: replace_attribute(s, SUPPLIER, LEGAL_NAME, api_name="LegalName"),
        {C.SCHEMA_API_NAME_INVALID},
        None,
    ),
    (
        "source-name-style",
        lambda s: set_source(s, api_name="ERP"),
        {C.SCHEMA_API_NAME_INVALID},
        None,
    ),
    (
        "side-effect-name-style",
        lambda s: replace_element(
            with_operation(s), "operations", FLAG_SUPPLIER_RISK, side_effects=("Risk.Raised",)
        ),
        {C.SCHEMA_API_NAME_INVALID},
        EXPRESSIONS,
    ),
    (
        "side-effect-name-length",
        lambda s: replace_element(
            with_operation(s), "operations", FLAG_SUPPLIER_RISK, side_effects=("a" * 65,)
        ),
        {C.SCHEMA_API_NAME_INVALID},
        EXPRESSIONS,
    ),
    (
        "side-effect-duplicate",
        lambda s: replace_element(
            with_operation(s), "operations", FLAG_SUPPLIER_RISK, side_effects=("x", "x")
        ),
        {C.SCHEMA_API_NAME_DUPLICATE},
        EXPRESSIONS,
    ),
    (
        "kind-duplicate",
        lambda s: replace_kind(s, PLANT, api_name="Part"),
        {C.SCHEMA_API_NAME_DUPLICATE},
        None,
    ),
    (
        "kind-trait-duplicate",
        lambda s: replace_kind(s, PLANT, api_name="Located"),
        {C.SCHEMA_API_NAME_DUPLICATE},
        None,
    ),
    (
        "attribute-duplicate",
        lambda s: replace_attribute(s, SUPPLIER, SUPPLIER_CITY, api_name="country"),
        {C.SCHEMA_API_NAME_DUPLICATE},
        None,
    ),
    (
        "relation-inverse-duplicate",
        lambda s: replace_relation(s, FLAGS, inverse_api_name="supplies"),
        {C.SCHEMA_API_NAME_DUPLICATE},
        None,
    ),
    (
        "relation-forward-equals-inverse",
        lambda s: replace_relation(s, FLAGS, inverse_api_name="flags"),
        {C.SCHEMA_API_NAME_DUPLICATE},
        None,
    ),
    (
        "path-derivation-name-collides-with-relation",
        lambda s: add_derivation(
            s,
            path_derivation(
                PathStep(relation=SUPPLIED_BY),
                PathStep(relation=WITH_SUPPLIER, direction=LinkDirection.INVERSE),
                name="supplies",
            ),
        ),
        {C.SCHEMA_API_NAME_DUPLICATE},
        None,
    ),
    (
        "reserved-attribute-name",
        lambda s: replace_attribute(s, SUPPLIER, SUPPLIER_CITY, api_name="key"),
        {C.SCHEMA_API_NAME_RESERVED},
        None,
    ),
    (
        "operation-shadows-builtin",
        lambda s: replace_element(
            with_operation(s), "operations", FLAG_SUPPLIER_RISK, api_name="create_supplier"
        ),
        {C.SCHEMA_API_NAME_DUPLICATE},
        EXPRESSIONS,
    ),
    (
        "operation-parameter-duplicate",
        lambda s: replace_element(
            with_operation(s),
            "operations",
            FLAG_SUPPLIER_RISK,
            parameters=(
                *flag_parameters(),
                OperationParameter(name="reason", type=T("text")),
            ),
        ),
        {C.SCHEMA_API_NAME_DUPLICATE},
        EXPRESSIONS,
    ),
    (
        "attribute-id-duplicate",
        lambda s: add_attribute(s, PLANT, attribute(LEGAL_NAME, "legal_name", T("string"))),
        {C.SCHEMA_ID_DUPLICATE},
        None,
    ),
    (
        "description-missing",
        lambda s: replace_attribute(s, SUPPLIER, LEGAL_NAME, description=" "),
        {C.SCHEMA_DESCRIPTION_MISSING},
        None,
    ),
    # Kinds
    (
        "primary-key-unknown",
        lambda s: replace_kind(s, PLANT, primary_key=(ident("att", 999),)),
        {C.SCHEMA_PRIMARY_KEY_UNKNOWN},
        None,
    ),
    (
        "primary-key-not-required",
        lambda s: replace_attribute(s, PLANT, PLANT_CODE, required=False),
        {C.SCHEMA_PRIMARY_KEY_INVALID},
        None,
    ),
    (
        "primary-key-type",
        lambda s: replace_attribute(s, PLANT, PLANT_CODE, type=T("float")),
        {C.SCHEMA_PRIMARY_KEY_INVALID},
        None,
    ),
    (
        "primary-key-default",
        lambda s: replace_attribute(s, PLANT, PLANT_CODE, default="TRN"),
        {C.SCHEMA_PRIMARY_KEY_INVALID},
        None,
    ),
    (
        "primary-key-listed-twice",
        lambda s: replace_kind(s, PLANT, primary_key=(PLANT_CODE, PLANT_CODE)),
        {C.SCHEMA_PRIMARY_KEY_INVALID},
        None,
    ),
    (
        "title-missing",
        lambda s: replace_kind(s, PLANT, title_attribute=None),
        {C.SCHEMA_TITLE_ATTRIBUTE_MISSING},
        None,
    ),
    (
        "embedding-template-unknown-attribute",
        lambda s: replace_kind(
            s, SUPPLIER, search=KindSearch(embedding_template="{legal_name} {no_such}")
        ),
        {C.SCHEMA_EMBEDDING_TEMPLATE_INVALID},
        None,
    ),
    (
        "vault-without-pii",
        lambda s: replace_attribute(s, SUPPLIER, SUPPLIER_CITY, vault=True),
        {C.SCHEMA_VAULT_INVALID},
        None,
    ),
    (
        "title-unknown",
        lambda s: replace_kind(s, PLANT, title_attribute=ident("att", 999)),
        {C.SCHEMA_TITLE_ATTRIBUTE_UNKNOWN},
        None,
    ),
    (
        "ref-to-unknown-kind",
        lambda s: add_attribute(
            s, PLANT, attribute(ident("att", 990), "owner", TypeSpec.ref(ident("knd", 99)))
        ),
        {C.SCHEMA_TYPE_REFERENCE_UNKNOWN},
        None,
    ),
    (
        "constraint-min-above-max",
        lambda s: replace_attribute(
            s, SUPPLIER, RISK_SCORE, constraints=Constraints(min=10, max=5)
        ),
        {C.SCHEMA_CONSTRAINT_INVALID},
        None,
    ),
    (
        "constraint-not-applicable",
        lambda s: replace_attribute(
            s, SUPPLIER, RISK_SCORE, constraints=Constraints(pattern="^[0-9]+$")
        ),
        {C.SCHEMA_CONSTRAINT_INVALID},
        None,
    ),
    (
        "default-wrong-type",
        lambda s: replace_attribute(s, SUPPLIER, TIER, default="9"),
        {C.SCHEMA_DEFAULT_INVALID},
        None,
    ),
    (
        "default-not-canonical",
        lambda s: replace_attribute(s, CONTRACT, CONTRACT_VALUE, default="1.5"),
        {C.SCHEMA_DEFAULT_INVALID},
        None,
    ),
    (
        "default-violates-constraint",
        lambda s: replace_attribute(s, SUPPLIER, RISK_SCORE, default="500"),
        {C.SCHEMA_DEFAULT_INVALID},
        None,
    ),
    (
        "derived-with-default",
        lambda s: replace_attribute(s, PART, SUPPLIER_COUNT, default="0"),
        {C.SCHEMA_DEFAULT_CONFLICT},
        None,
    ),
    (
        "derived-required",
        lambda s: replace_attribute(s, PART, SUPPLIER_COUNT, required=True),
        {C.SCHEMA_DEFAULT_CONFLICT},
        None,
    ),
    (
        "unique-link-attribute",
        lambda s: replace_relation(
            s,
            SUPPLIED_BY,
            attributes={IS_PRIMARY: attribute(IS_PRIMARY, "is_primary", T("boolean"), unique=True)},
        ),
        {C.SCHEMA_ATTRIBUTE_FLAG_INVALID},
        None,
    ),
    (
        "unique-float",
        lambda s: replace_attribute(s, SUPPLIER, RISK_SCORE, type=T("float"), unique=True),
        {C.SCHEMA_ATTRIBUTE_FLAG_INVALID},
        None,
    ),
    (
        "indexed-json",
        lambda s: add_attribute(
            s, PLANT, attribute(ident("att", 991), "extra", T("json"), indexed=True)
        ),
        {C.SCHEMA_ATTRIBUTE_FLAG_INVALID},
        None,
    ),
    (
        "searchable-integer",
        lambda s: replace_attribute(s, SUPPLIER, RISK_SCORE, searchable=True),
        {C.SCHEMA_ATTRIBUTE_FLAG_INVALID},
        None,
    ),
    (
        "authority-unknown",
        lambda s: replace_attribute(s, SUPPLIER, LEGAL_NAME, authority=ident("src", 99)),
        {C.SCHEMA_AUTHORITY_UNKNOWN},
        None,
    ),
    # Relations
    (
        "relation-endpoint-unknown",
        lambda s: replace_relation(s, FLAGS, to_kind=ident("knd", 99)),
        {C.SCHEMA_RELATION_ENDPOINT_UNKNOWN},
        None,
    ),
    (
        "foreign-key-attribute-missing",
        lambda s: replace_relation(s, MADE_AT, foreign_key_attribute=None),
        {C.SCHEMA_FOREIGN_KEY_INVALID},
        None,
    ),
    (
        "foreign-key-on-explicit-relation",
        lambda s: replace_relation(s, MADE_AT, backing=RelationBacking.EXPLICIT),
        {C.SCHEMA_FOREIGN_KEY_INVALID},
        None,
    ),
    (
        "foreign-key-many-to-many",
        lambda s: replace_relation(s, MADE_AT, cardinality=Cardinality.MANY_TO_MANY),
        {C.SCHEMA_FOREIGN_KEY_INVALID},
        None,
    ),
    (
        "foreign-key-wrong-type",
        lambda s: replace_relation(s, MADE_AT, foreign_key_attribute=PART_NAME),
        {C.SCHEMA_FOREIGN_KEY_INVALID},
        None,
    ),
    # Traits
    (
        "trait-unknown",
        lambda s: replace_kind(s, PLANT, traits=(TraitImplementation(trait_id=ident("trt", 99)),)),
        {C.SCHEMA_TRAIT_UNKNOWN},
        None,
    ),
    (
        "trait-implemented-twice",
        lambda s: replace_kind(s, PLANT, traits=s.kinds[PLANT].traits * 2),
        {C.SCHEMA_TRAIT_DUPLICATE},
        None,
    ),
    (
        "trait-attribute-unmapped",
        lambda s: set_trait_map(s, SUPPLIER, {LOCATED_COUNTRY: SUPPLIER_COUNTRY}),
        {C.SCHEMA_TRAIT_MAPPING_MISSING},
        None,
    ),
    (
        "trait-mapping-extra-key",
        lambda s: set_trait_map(
            s,
            SUPPLIER,
            {
                LOCATED_COUNTRY: SUPPLIER_COUNTRY,
                ident("att", 702): SUPPLIER_CITY,
                ident("att", 799): SUPPLIER_CITY,
            },
        ),
        {C.SCHEMA_TRAIT_MAPPING_INVALID},
        None,
    ),
    (
        "trait-mapping-unknown-attribute",
        lambda s: set_trait_map(
            s, SUPPLIER, {LOCATED_COUNTRY: SUPPLIER_COUNTRY, ident("att", 702): ident("att", 999)}
        ),
        {C.SCHEMA_TRAIT_MAPPING_INVALID},
        None,
    ),
    (
        "trait-mapping-type",
        lambda s: replace_element(
            s,
            "traits",
            LOCATED,
            attributes={
                **s.traits[LOCATED].attributes,
                LOCATED_COUNTRY: TraitAttribute(
                    id=LOCATED_COUNTRY, api_name="country", type=T("integer"), required=True
                ),
            },
        ),
        {C.SCHEMA_TRAIT_MAPPING_TYPE},
        None,
    ),
    (
        "trait-mapping-required",
        lambda s: replace_attribute(s, SUPPLIER, SUPPLIER_COUNTRY, required=False),
        {C.SCHEMA_TRAIT_MAPPING_REQUIRED},
        None,
    ),
    # Operations
    ("expressions-disabled", with_operation, {C.SCHEMA_EXPRESSIONS_DISABLED}, None),
    (
        "operation-step-unknown-kind",
        lambda s: set_step(s, 0, CreateRecordStep(kind=ident("knd", 99))),
        {C.SCHEMA_OPERATION_REFERENCE_UNKNOWN},
        EXPRESSIONS,
    ),
    (
        "operation-step-unknown-attribute",
        lambda s: set_step(s, 0, CreateRecordStep(kind=RISK_FLAG, values={LEGAL_NAME: "x"})),
        {C.SCHEMA_OPERATION_REFERENCE_UNKNOWN},
        EXPRESSIONS,
    ),
    (
        "operation-step-derived-attribute",
        lambda s: set_step(s, 1, CreateRecordStep(kind=PART, values={SUPPLIER_COUNT: "1"})),
        {C.SCHEMA_OPERATION_INVALID},
        EXPRESSIONS,
    ),
    (
        "operation-step-unknown-relation",
        lambda s: set_step(
            s,
            1,
            CreateLinkStep.model_validate({"relation": ident("rel", 99), "from": "a", "to": "b"}),
        ),
        {C.SCHEMA_OPERATION_REFERENCE_UNKNOWN},
        EXPRESSIONS,
    ),
    (
        "operation-links-foreign-key-relation",
        lambda s: set_step(
            s, 1, CreateLinkStep.model_validate({"relation": MADE_AT, "from": "a", "to": "b"})
        ),
        {C.SCHEMA_OPERATION_INVALID},
        EXPRESSIONS,
    ),
    (
        "operation-parameter-unknown-kind",
        lambda s: replace_element(
            with_operation(s),
            "operations",
            FLAG_SUPPLIER_RISK,
            parameters=(
                *flag_parameters(),
                OperationParameter(name="target", type=TypeSpec.ref(ident("knd", 99))),
            ),
        ),
        {C.SCHEMA_TYPE_REFERENCE_UNKNOWN},
        EXPRESSIONS,
    ),
    (
        "operation-expression-invalid",
        lambda s: replace_element(
            with_operation(s),
            "operations",
            FLAG_SUPPLIER_RISK,
            preconditions=(Precondition(expression="record(supplier).tierr", message="m"),),
        ),
        {C.SCHEMA_EXPRESSION_INVALID},
        EXPRESSIONS,
    ),
    (
        "derivation-filter-invalid",
        lambda s: set_aggregate(s, filter="tier + 1"),
        {C.SCHEMA_EXPRESSION_INVALID},
        EXPRESSIONS,
    ),
    # Derivations
    (
        "derivation-unknown",
        lambda s: add_attribute(
            s,
            PLANT,
            attribute(ident("att", 992), "score", T("integer"), derivation_id=ident("drv", 99)),
        ),
        {C.SCHEMA_DERIVATION_REFERENCE_UNKNOWN},
        None,
    ),
    (
        "derivation-target-mismatch",
        lambda s: set_aggregate(s, attribute=PART_NAME),
        {C.SCHEMA_DERIVATION_INVALID},
        None,
    ),
    (
        "aggregate-wrong-direction",
        lambda s: set_aggregate(s, direction=LinkDirection.INVERSE),
        {C.SCHEMA_DERIVATION_INVALID},
        None,
    ),
    (
        "aggregate-sum-of-text",
        lambda s: set_aggregate(s, function=AggregateFunction.SUM, source_attribute=LEGAL_NAME),
        {C.SCHEMA_DERIVATION_INVALID},
        None,
    ),
    (
        "aggregate-unknown-source-attribute",
        lambda s: set_aggregate(s, function=AggregateFunction.MAX, source_attribute=PLANT_NAME),
        {C.SCHEMA_DERIVATION_REFERENCE_UNKNOWN},
        None,
    ),
    (
        "aggregate-unknown-relation",
        lambda s: set_aggregate(s, relation=ident("rel", 99)),
        {C.SCHEMA_DERIVATION_REFERENCE_UNKNOWN},
        None,
    ),
    (
        "path-not-connected",
        lambda s: add_derivation(
            s, path_derivation(PathStep(relation=SUPPLIED_BY), PathStep(relation=MADE_AT))
        ),
        {C.SCHEMA_DERIVATION_INVALID},
        None,
    ),
    ("derivation-cycle", derivation_cycle, {C.SCHEMA_DERIVATION_CYCLE}, None),
    (
        "derivation-expression-disabled",
        lambda s: set_aggregate(s, filter="true"),
        {C.SCHEMA_EXPRESSIONS_DISABLED},
        None,
    ),
    # Sources
    (
        "source-literal-connection",
        lambda s: set_source(s, connection="postgresql://user:pw@db/erp"),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-incremental-without-watermark",
        lambda s: set_source(s, sync_mode=SyncMode.INCREMENTAL),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-query-without-query",
        lambda s: set_source(s, type=SourceType.POSTGRES_QUERY),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-mysql-query-without-query",
        lambda s: set_source(s, type=SourceType.MYSQL_QUERY),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-mssql-query-without-query",
        lambda s: set_source(s, type=SourceType.MSSQL_QUERY, query=" "),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-records-path-on-mssql",
        lambda s: set_source(s, type=SourceType.MSSQL_TABLE, records_path="data.items"),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-records-path-on-postgres",
        lambda s: set_source(s, records_path="data.items"),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-http-options-on-csv",
        lambda s: set_source(s, type=SourceType.CSV, http=HttpOptions()),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-key-mapping-length",
        lambda s: set_source(s, key_mapping=("a", "b")),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-kind-without-key",
        lambda s: set_source(s, target_kind=CONTRACT, attribute_mapping={}),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-relation-from-other-kind",
        lambda s: set_source(
            s, relation_mapping=(RelationMapping(relation=SUPPLIED_BY, columns=("p",)),)
        ),
        {C.SCHEMA_SOURCE_INVALID},
        None,
    ),
    (
        "source-unknown-kind",
        lambda s: set_source(s, target_kind=ident("knd", 99)),
        {C.SCHEMA_SOURCE_REFERENCE_UNKNOWN},
        None,
    ),
    (
        "source-unknown-attribute",
        lambda s: set_source(
            s, attribute_mapping={PLANT_NAME: s.sources[ERP_VENDORS].attribute_mapping[LEGAL_NAME]}
        ),
        {C.SCHEMA_SOURCE_REFERENCE_UNKNOWN},
        None,
    ),
]


@pytest.mark.parametrize(
    ("mutation", "expected", "rules"),
    [pytest.param(m, e, r, id=name) for name, m, e, r in NEGATIVE],
)
def test_rule_violations(
    mutation: Mutation, expected: set[IssueCode], rules: SchemaRules | None
) -> None:
    assert error_codes(mutation(supply_schema()), rules) == expected


def test_issues_carry_paths() -> None:
    schema = replace_attribute(supply_schema(), SUPPLIER, TIER, default="9")
    (issue,) = validate_schema(schema)
    assert issue.path == ("kinds", SUPPLIER, "attributes", TIER, "default")
    assert issue.kind == SUPPLIER
    assert issue.attribute == TIER


def test_expression_issues_locate_the_expression() -> None:
    schema = replace_element(
        with_operation(supply_schema()),
        "operations",
        FLAG_SUPPLIER_RISK,
        preconditions=(Precondition(expression="params.severity == 1", message="m"),),
    )
    (issue,) = validate_schema(schema, EXPRESSIONS)
    assert issue.code is C.SCHEMA_EXPRESSION_INVALID
    assert issue.path == ("operations", FLAG_SUPPLIER_RISK, "preconditions", 0, "expression")
    assert issue.message.startswith("expression: 1:1: cannot compare enum(high, low) with int")
    assert issue.details == {
        "error": "expression.type",
        "expression": "params.severity == 1",
        "line": 1,
        "column": 1,
    }
    limited = SchemaRules(allow_expressions=True, expression_limits=ExprLimits(max_steps=1))
    assert validate_schema(with_operation(supply_schema()), limited) == []
    tight = SchemaRules(allow_expressions=True, expression_limits=ExprLimits(max_source_length=5))
    assert {i.details["error"] for i in validate_schema(schema, tight)} == {
        "expression.limit_exceeded"
    }


def test_reserved_names_are_configurable() -> None:
    schema = replace_attribute(supply_schema(), SUPPLIER, SUPPLIER_CITY, api_name="key")
    assert error_codes(schema, SchemaRules(reserved_attribute_names=frozenset())) == set()


def test_every_schema_code_is_exercised() -> None:
    exercised = {code for _, _, codes, _ in NEGATIVE for code in codes} | {
        C.SCHEMA_DEFAULT_CONFLICT,
        C.SCHEMA_SENSITIVE_ATTRIBUTE,  # test_vault_rules
        C.SCHEMA_SENSITIVITY_INHERITED,  # test_vault_rules
    }
    schema_codes = {code for code in IssueCode if code.value.startswith("schema.")}
    assert schema_codes == exercised


def test_attribute_ids_unique_across_relations() -> None:
    schema = replace_relation(
        supply_schema(),
        SUPPLIED_BY,
        attributes={
            SUPPLIER_CODE: Attribute(
                id=SUPPLIER_CODE, api_name="x", type=T("string"), description="x"
            )
        },
    )
    assert error_codes(schema) == {C.SCHEMA_ID_DUPLICATE}
