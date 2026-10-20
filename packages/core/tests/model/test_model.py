from __future__ import annotations

import pytest
from pydantic import ValidationError

from lorebase_core.model import (
    AggregateDerivation,
    AggregateFunction,
    Attribute,
    AttributeMapping,
    Domain,
    IdPrefix,
    ImportOrigin,
    Kind,
    Link,
    LinkDirection,
    OperationOrigin,
    OriginType,
    ProvenanceEntry,
    Record,
    ReviewMode,
    ReviewPolicy,
    Schema,
    Sensitivity,
    builtin_operation_names,
    format_ref,
    id_prefix,
    is_ulid,
    new_id,
    new_ulid,
    parse_ref,
    record_key,
    render_key,
    snake_case,
)
from lorebase_core.types import TypeSpec

from .supply import (
    LEGAL_NAME,
    PART,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_CODE,
    SUPPLIER_COUNT,
    ident,
    replace_kind,
    supplier_kind,
    supply_schema,
)

# Ids ----------------------------------------------------------------------------


def test_new_ulid_is_deterministic_with_inputs() -> None:
    first = new_ulid(timestamp_ms=1_700_000_000_000, randomness=bytes(10))
    assert first == new_ulid(timestamp_ms=1_700_000_000_000, randomness=bytes(10))
    assert is_ulid(first)
    assert first.endswith("0" * 16)


def test_new_ulids_sort_by_time() -> None:
    earlier = new_ulid(timestamp_ms=1000, randomness=b"\xff" * 10)
    later = new_ulid(timestamp_ms=1001, randomness=bytes(10))
    assert earlier < later


def test_new_id_has_prefix() -> None:
    kind_id = new_id(IdPrefix.KIND)
    assert kind_id.startswith("knd_")
    assert id_prefix(kind_id) is IdPrefix.KIND
    assert id_prefix("knd_short") is None
    assert id_prefix("xyz_" + new_ulid()) is None


def test_ulid_bounds() -> None:
    with pytest.raises(ValueError, match="48 bits"):
        new_ulid(timestamp_ms=2**48)
    with pytest.raises(ValueError, match="10 bytes"):
        new_ulid(randomness=b"x")


def test_ids_are_validated_by_prefix() -> None:
    with pytest.raises(ValidationError):
        Kind(id=ident("att", 1), api_name="Wrong")
    with pytest.raises(ValidationError):
        Attribute(id="att_lowercase", api_name="x", type=TypeSpec.of("string"))


# Elements -----------------------------------------------------------------------


def test_elements_are_frozen() -> None:
    kind = supplier_kind()
    with pytest.raises(ValidationError):
        kind.api_name = "Vendor"  # type: ignore[misc]


def test_attribute_map_keys_must_match_ids() -> None:
    attribute = Attribute(id=ident("att", 1), api_name="x", type=TypeSpec.of("string"))
    with pytest.raises(ValidationError, match="does not match"):
        Kind(id=ident("knd", 9), api_name="K", attributes={ident("att", 2): attribute})


def test_schema_keys_must_match_ids() -> None:
    kind = supplier_kind()
    with pytest.raises(ValidationError, match="does not match"):
        Schema(domain=Domain(id="supply"), kinds={ident("knd", 99): kind})


def test_sensitivity_is_sorted_and_unique() -> None:
    attribute = Attribute(
        id=ident("att", 1),
        api_name="x",
        type=TypeSpec.of("string"),
        sensitivity=(Sensitivity.RESTRICTED, Sensitivity.PII, Sensitivity.PII),
    )
    assert attribute.sensitivity == (Sensitivity.PII, Sensitivity.RESTRICTED)


def test_review_policy_expression_matches_mode() -> None:
    assert ReviewPolicy().mode is ReviewMode.NEVER
    with pytest.raises(ValidationError):
        ReviewPolicy(mode=ReviewMode.EXPRESSION)
    with pytest.raises(ValidationError):
        ReviewPolicy(mode=ReviewMode.ALWAYS, expression="true")


def test_aggregate_needs_source_attribute_except_count() -> None:
    with pytest.raises(ValidationError, match="source_attribute"):
        AggregateDerivation(
            kind=PART,
            attribute=SUPPLIER_COUNT,
            relation=SUPPLIED_BY,
            function=AggregateFunction.SUM,
        )


def test_attribute_mapping_needs_one_input() -> None:
    with pytest.raises(ValidationError):
        AttributeMapping()
    with pytest.raises(ValidationError):
        AttributeMapping(column="a", expression="b")


def test_domain_created_at_is_canonical() -> None:
    domain = Domain(id="supply", created_at="2026-10-20T11:12:44+02:00")
    assert domain.created_at == "2026-10-20T09:12:44.000000Z"
    with pytest.raises(ValidationError):
        Domain(id="Supply")


def test_content_uses_ids_and_omits_absent_fields() -> None:
    kind = supplier_kind()
    content = kind.content()
    attributes = content["attributes"]
    assert isinstance(attributes, dict)
    assert set(attributes) == set(kind.attributes)
    assert "icon" not in content
    assert content["primary_key"] == [SUPPLIER_CODE]


def test_rename_does_not_change_referencing_elements() -> None:
    schema = supply_schema()
    renamed = replace_kind(schema, SUPPLIER, api_name="Vendor")
    assert renamed.relations[SUPPLIED_BY].content() == schema.relations[SUPPLIED_BY].content()
    assert renamed.kinds[SUPPLIER].id == schema.kinds[SUPPLIER].id


def test_builtin_operation_names() -> None:
    assert snake_case("RiskFlag") == "risk_flag"
    assert builtin_operation_names(supplier_kind()) == [
        "create_supplier",
        "update_supplier",
        "delete_supplier",
    ]


# Records and Links --------------------------------------------------------------


def test_record_drops_absent_values_and_normalizes_key() -> None:
    record = Record(kind=SUPPLIER, key="Café", values={LEGAL_NAME: "X", SUPPLIER_CODE: None})
    assert record.key == "Café"
    assert record.values == {LEGAL_NAME: "X"}
    assert record.identity == (SUPPLIER, "Café")


@pytest.mark.parametrize("key", ["", "a\x1fb", "a\nb"])
def test_record_key_rejects_invalid(key: str) -> None:
    with pytest.raises(ValidationError):
        Record(kind=SUPPLIER, key=key)


def test_link_content_uses_from_and_to() -> None:
    link = Link.model_validate({"relation": SUPPLIED_BY, "from": "P-1", "to": "S-1"})
    assert link.content() == {"relation": SUPPLIED_BY, "from": "P-1", "to": "S-1", "values": {}}
    assert Link(relation=SUPPLIED_BY, from_key="P-1", to_key="S-1") == link
    assert link.identity == (SUPPLIED_BY, "P-1", "S-1")


def test_render_key() -> None:
    assert render_key(["S-1001"]) == "S-1001"
    assert render_key(["a,b", "c/d"]) == "a%2Cb,c%2Fd"
    with pytest.raises(ValueError, match="at least one"):
        render_key([])


def test_record_key_from_values() -> None:
    kind = supplier_kind()
    assert record_key(kind, {SUPPLIER_CODE: "S-1"}) == "S-1"
    assert record_key(kind, {}) is None
    assert record_key(Kind(id=ident("knd", 50), api_name="NoKey"), {}) is None


def test_refs() -> None:
    assert format_ref("Supplier", "S-1001") == "Supplier/S-1001"
    assert parse_ref("Doc/a/b") == ("Doc", "a/b")
    with pytest.raises(ValueError, match="reference"):
        parse_ref("Supplier")


# Lineage ------------------------------------------------------------------------


def test_provenance_entry_origins() -> None:
    entry = ProvenanceEntry.model_validate(
        {
            "id": ident("prv", 1),
            "origin": {
                "type": "operation",
                "invocation_id": "inv_1",
                "operation": "create_supplier",
                "principal": "usr_1",
            },
            "actor": "usr_1",
            "at": "2026-10-20T09:12:44.5Z",
        }
    )
    assert isinstance(entry.origin, OperationOrigin)
    assert entry.origin_type is OriginType.OPERATION
    assert entry.at == "2026-10-20T09:12:44.500000Z"
    imported = entry.model_copy(update={"origin": ImportOrigin(batch_id="b1")})
    assert imported.origin_type is OriginType.IMPORT
    with pytest.raises(ValidationError):
        ProvenanceEntry.model_validate({**entry.content(), "origin": {"type": "unknown"}})


# Schema lookups -----------------------------------------------------------------


def test_schema_lookups() -> None:
    schema = supply_schema()
    assert schema.kind_by_api_name("Supplier") is schema.kinds[SUPPLIER]
    assert schema.kind_by_api_name("Nope") is None
    found = schema.relation_by_api_name("supplies")
    assert found is not None
    assert found[0].id == SUPPLIED_BY
    assert found[1] is LinkDirection.INVERSE
    assert schema.attribute_by_api_name(SUPPLIER, "legal_name") is not None
    assert schema.resolve_ref("Supplier/S-1") == (SUPPLIER, "S-1")
    with pytest.raises(KeyError):
        schema.resolve_ref("Nope/S-1")
    assert {k.api_name for k in schema.kinds_implementing(ident("trt", 1))} == {"Supplier", "Plant"}
    assert {r.api_name for r in schema.relations_of(PART)} == {"supplied_by", "made_at"}
    owner = schema.attribute_owner(LEGAL_NAME)
    assert owner is not None
    assert owner.id == SUPPLIER
    assert len(list(schema.elements())) == 5 + 4 + 1 + 0 + 1 + 1


def test_schema_copy_refreshes_lookups() -> None:
    schema = supply_schema()
    assert schema.kind_by_api_name("Supplier") is not None
    renamed = replace_kind(schema, SUPPLIER, api_name="Vendor")
    assert renamed.kind_by_api_name("Supplier") is None
    assert renamed.kind_by_api_name("Vendor") is not None


def test_schema_round_trips_through_json() -> None:
    schema = supply_schema(with_operation=True)
    assert Schema.model_validate_json(schema.model_dump_json(by_alias=True)) == schema
