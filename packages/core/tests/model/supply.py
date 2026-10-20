"""A small ``supply`` Domain schema used as a fixture by model and validation tests."""

from __future__ import annotations

from typing import Any

from lorebase_core.model import (
    AggregateDerivation,
    AggregateFunction,
    Attribute,
    AttributeMapping,
    Cardinality,
    CreateLinkStep,
    CreateRecordStep,
    Derivation,
    Domain,
    Kind,
    OnDelete,
    Operation,
    OperationParameter,
    Precondition,
    Relation,
    RelationBacking,
    ReviewMode,
    ReviewPolicy,
    Schema,
    Source,
    SourceType,
    Trait,
    TraitAttribute,
    TraitImplementation,
)
from lorebase_core.types import Constraints, TypeSpec


def ident(prefix: str, number: int) -> str:
    """Deterministic, well-formed prefixed id."""
    return f"{prefix}_01JB8Z{number:020d}"


T = TypeSpec.of

# Kinds
SUPPLIER = ident("knd", 1)
PART = ident("knd", 2)
PLANT = ident("knd", 3)
CONTRACT = ident("knd", 4)
RISK_FLAG = ident("knd", 5)
# Supplier attributes
SUPPLIER_CODE = ident("att", 101)
LEGAL_NAME = ident("att", 102)
SUPPLIER_COUNTRY = ident("att", 103)
SUPPLIER_CITY = ident("att", 104)
TIER = ident("att", 105)
RISK_SCORE = ident("att", 106)
VAT = ident("att", 107)
# Part attributes
PART_NUMBER = ident("att", 201)
PART_NAME = ident("att", 202)
SUPPLIER_COUNT = ident("att", 203)
PREFERRED_PLANT = ident("att", 204)
TAGS = ident("att", 205)
# Plant attributes
PLANT_CODE = ident("att", 301)
PLANT_NAME = ident("att", 302)
PLANT_COUNTRY = ident("att", 303)
PLANT_CITY = ident("att", 304)
# Contract attributes (no natural key)
CONTRACT_TITLE = ident("att", 401)
CONTRACT_VALUE = ident("att", 402)
# RiskFlag attributes
SEVERITY = ident("att", 501)
REASON = ident("att", 502)
# Link attributes
IS_PRIMARY = ident("att", 601)
# Trait Located
LOCATED = ident("trt", 1)
LOCATED_COUNTRY = ident("att", 701)
LOCATED_CITY = ident("att", 702)
# Relations
SUPPLIED_BY = ident("rel", 1)
WITH_SUPPLIER = ident("rel", 2)
MADE_AT = ident("rel", 3)
FLAGS = ident("rel", 4)
# Others
FLAG_SUPPLIER_RISK = ident("opr", 1)
SUPPLIER_COUNT_DRV = ident("drv", 1)
ERP_VENDORS = ident("src", 1)


def attribute(attribute_id: str, api_name: str, spec: TypeSpec, **extra: Any) -> Attribute:
    extra.setdefault("description", f"The {api_name.replace('_', ' ')}.")
    return Attribute(id=attribute_id, api_name=api_name, type=spec, **extra)


def attributes(*items: Attribute) -> dict[str, Attribute]:
    return {item.id: item for item in items}


def supplier_kind() -> Kind:
    return Kind(
        id=SUPPLIER,
        api_name="Supplier",
        description="A company that supplies parts.",
        primary_key=(SUPPLIER_CODE,),
        title_attribute=LEGAL_NAME,
        attributes=attributes(
            attribute(
                SUPPLIER_CODE,
                "supplier_code",
                T("string"),
                required=True,
                constraints=Constraints(pattern="^S-[0-9]+$"),
            ),
            attribute(LEGAL_NAME, "legal_name", T("string"), required=True, searchable=True),
            attribute(
                SUPPLIER_COUNTRY,
                "country",
                T("string"),
                required=True,
                constraints=Constraints(min_length=2, max_length=2),
            ),
            attribute(SUPPLIER_CITY, "city", T("string")),
            attribute(TIER, "tier", TypeSpec.enum("1", "2", "3"), default="3"),
            attribute(
                RISK_SCORE, "risk_score", T("integer"), constraints=Constraints(min=0, max=100)
            ),
            attribute(VAT, "vat_number", T("string"), unique=True),
        ),
        traits=(
            TraitImplementation(
                trait_id=LOCATED,
                attribute_map={LOCATED_COUNTRY: SUPPLIER_COUNTRY, LOCATED_CITY: SUPPLIER_CITY},
            ),
        ),
    )


def part_kind() -> Kind:
    return Kind(
        id=PART,
        api_name="Part",
        description="A part bought from suppliers.",
        primary_key=(PART_NUMBER,),
        title_attribute=PART_NAME,
        attributes=attributes(
            attribute(PART_NUMBER, "part_number", T("string"), required=True),
            attribute(PART_NAME, "name", T("string"), required=True),
            attribute(
                SUPPLIER_COUNT, "supplier_count", T("integer"), derivation_id=SUPPLIER_COUNT_DRV
            ),
            attribute(PREFERRED_PLANT, "preferred_plant", TypeSpec.ref(PLANT)),
            attribute(
                TAGS, "tags", TypeSpec.array(T("string")), constraints=Constraints(max_length=3)
            ),
        ),
    )


def plant_kind() -> Kind:
    return Kind(
        id=PLANT,
        api_name="Plant",
        description="A production plant.",
        primary_key=(PLANT_CODE,),
        title_attribute=PLANT_NAME,
        attributes=attributes(
            attribute(PLANT_CODE, "code", T("string"), required=True),
            attribute(PLANT_NAME, "name", T("string"), required=True),
            attribute(PLANT_COUNTRY, "country", T("string"), required=True),
            attribute(PLANT_CITY, "city", T("string")),
        ),
        traits=(
            TraitImplementation(
                trait_id=LOCATED,
                attribute_map={LOCATED_COUNTRY: PLANT_COUNTRY, LOCATED_CITY: PLANT_CITY},
            ),
        ),
    )


def contract_kind() -> Kind:
    return Kind(
        id=CONTRACT,
        api_name="Contract",
        description="A supply contract; keys are minted.",
        title_attribute=CONTRACT_TITLE,
        attributes=attributes(
            attribute(CONTRACT_TITLE, "title", T("string"), required=True),
            attribute(CONTRACT_VALUE, "value", TypeSpec.decimal(12, 2)),
        ),
    )


def risk_flag_kind() -> Kind:
    return Kind(
        id=RISK_FLAG,
        api_name="RiskFlag",
        description="A risk raised on a supplier.",
        title_attribute=REASON,
        attributes=attributes(
            attribute(
                SEVERITY,
                "severity",
                TypeSpec.enum("low", "medium", "high", "critical"),
                required=True,
            ),
            attribute(REASON, "reason", T("text"), required=True),
        ),
    )


def located_trait() -> Trait:
    return Trait(
        id=LOCATED,
        api_name="Located",
        description="Things with a location.",
        attributes={
            LOCATED_COUNTRY: TraitAttribute(
                id=LOCATED_COUNTRY, api_name="country", type=T("string"), required=True
            ),
            LOCATED_CITY: TraitAttribute(id=LOCATED_CITY, api_name="city", type=T("string")),
        },
    )


def relations() -> dict[str, Relation]:
    items = [
        Relation(
            id=SUPPLIED_BY,
            api_name="supplied_by",
            inverse_api_name="supplies",
            from_kind=PART,
            to_kind=SUPPLIER,
            cardinality=Cardinality.MANY_TO_MANY,
            attributes=attributes(attribute(IS_PRIMARY, "is_primary", T("boolean"))),
        ),
        Relation(
            id=WITH_SUPPLIER,
            api_name="with_supplier",
            inverse_api_name="contracts",
            from_kind=CONTRACT,
            to_kind=SUPPLIER,
            cardinality=Cardinality.MANY_TO_ONE,
            required_from=True,
            on_delete=OnDelete.RESTRICT,
        ),
        Relation(
            id=MADE_AT,
            api_name="made_at",
            inverse_api_name="makes",
            from_kind=PART,
            to_kind=PLANT,
            cardinality=Cardinality.MANY_TO_ONE,
            backing=RelationBacking.FOREIGN_KEY,
            foreign_key_attribute=PREFERRED_PLANT,
        ),
        Relation(
            id=FLAGS,
            api_name="flags",
            inverse_api_name="flagged_by",
            from_kind=RISK_FLAG,
            to_kind=SUPPLIER,
            cardinality=Cardinality.ONE_TO_ONE,
        ),
    ]
    return {item.id: item for item in items}


def flag_operation() -> Operation:
    return Operation(
        id=FLAG_SUPPLIER_RISK,
        api_name="flag_supplier_risk",
        description="Flag a supplier as a risk.",
        parameters=(
            OperationParameter(name="supplier", type=TypeSpec.ref(SUPPLIER), required=True),
            OperationParameter(name="severity", type=TypeSpec.enum("low", "high"), required=True),
            OperationParameter(name="reason", type=TypeSpec.of("text"), required=True),
        ),
        preconditions=(Precondition(expression='record(supplier).tier != "3"', message="No."),),
        steps=(
            CreateRecordStep(
                kind=RISK_FLAG, values={SEVERITY: "params.severity", REASON: "params.reason"}
            ),
            CreateLinkStep.model_validate(
                {"relation": FLAGS, "from": "$step[0]", "to": "params.supplier"}
            ),
        ),
        review_policy=ReviewPolicy(
            mode=ReviewMode.EXPRESSION, expression='params.severity == "high"'
        ),
    )


def supplier_count_derivation() -> Derivation:
    return Derivation(
        id=SUPPLIER_COUNT_DRV,
        api_name="supplier_count",
        definition=AggregateDerivation(
            kind=PART,
            attribute=SUPPLIER_COUNT,
            relation=SUPPLIED_BY,
            function=AggregateFunction.COUNT,
        ),
    )


def erp_source() -> Source:
    return Source(
        id=ERP_VENDORS,
        api_name="erp.vendors",
        type=SourceType.POSTGRES_TABLE,
        connection="secret://env/ERP_DSN",
        target_kind=SUPPLIER,
        key_mapping=("vendor_id",),
        attribute_mapping={LEGAL_NAME: AttributeMapping(column="name")},
    )


def supply_schema(*, with_operation: bool = False) -> Schema:
    """The fixture schema. The custom Operation uses expressions, so it is opt-in."""
    kinds = [supplier_kind(), part_kind(), plant_kind(), contract_kind(), risk_flag_kind()]
    operations = [flag_operation()] if with_operation else []
    return Schema(
        domain=Domain(id="supply", display_name="Supply"),
        kinds={kind.id: kind for kind in kinds},
        relations=relations(),
        traits={LOCATED: located_trait()},
        operations={operation.id: operation for operation in operations},
        derivations={SUPPLIER_COUNT_DRV: supplier_count_derivation()},
        sources={ERP_VENDORS: erp_source()},
    )


# Mutation helpers (copies are not re-validated, which lets tests build invalid schemas) --------


def replace_kind(schema: Schema, kind_id: str, **changes: Any) -> Schema:
    kinds = dict(schema.kinds)
    kinds[kind_id] = kinds[kind_id].model_copy(update=changes)
    return schema.model_copy(update={"kinds": kinds})


def replace_attribute(schema: Schema, kind_id: str, attribute_id: str, **changes: Any) -> Schema:
    kind = schema.kinds[kind_id]
    updated = dict(kind.attributes)
    updated[attribute_id] = updated[attribute_id].model_copy(update=changes)
    return replace_kind(schema, kind_id, attributes=updated)


def add_attribute(schema: Schema, kind_id: str, item: Attribute) -> Schema:
    kind = schema.kinds[kind_id]
    return replace_kind(schema, kind_id, attributes={**kind.attributes, item.id: item})


def replace_relation(schema: Schema, relation_id: str, **changes: Any) -> Schema:
    items = dict(schema.relations)
    items[relation_id] = items[relation_id].model_copy(update=changes)
    return schema.model_copy(update={"relations": items})


def replace_element(schema: Schema, collection: str, element_id: str, **changes: Any) -> Schema:
    items = dict(getattr(schema, collection))
    items[element_id] = items[element_id].model_copy(update=changes)
    return schema.model_copy(update={collection: items})
