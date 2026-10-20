"""Foreign keys by inclusion, junction tables and the Relations they become."""

from __future__ import annotations

import pytest

from lorebase_core.infer import (
    ColumnProfile,
    DeclaredForeignKey,
    InferenceIssueCode,
    InferenceOptions,
    SchemaProposal,
    TableProfile,
    infer_schema,
)
from lorebase_core.model import Cardinality, Relation, RelationBacking

from .support import table

CODES = [f"S-{1000 + i}" for i in range(10)]
SUPPLIER = table("supplier", supplier_code=CODES, legal_name=[f"Firm {i}" for i in range(10)])
PARTS = [f"P-{2000 + i}" for i in range(6)]
PART = table("part", part_number=PARTS, name=[f"Part {i}" for i in range(6)])


def relation(proposal: SchemaProposal, name: str) -> Relation:
    found = proposal.schema.relation_by_api_name(name)
    assert found is not None, sorted(r.api_name for r in proposal.schema.relations.values())
    return found[0]


def kind_name(proposal: SchemaProposal, kind_id: str) -> str:
    return proposal.schema.kinds[kind_id].api_name


def test_foreign_key_becomes_many_to_one_relation() -> None:
    orders = table(
        "purchase_orders",
        po_number=["PO-1", "PO-2", "PO-3", "PO-4"],
        supplier_id=[CODES[0], CODES[1], CODES[1], None],
    )
    proposal = infer_schema([SUPPLIER, orders], "supply")
    rel = relation(proposal, "supplier")
    assert (kind_name(proposal, rel.from_kind), kind_name(proposal, rel.to_kind)) == (
        "PurchaseOrder",
        "Supplier",
    )
    assert rel.cardinality is Cardinality.MANY_TO_ONE
    assert rel.backing is RelationBacking.FOREIGN_KEY
    assert rel.inverse_api_name == "purchase_orders"
    assert rel.required_from is False
    kind = proposal.schema.kinds[rel.from_kind]
    attribute = kind.attributes[rel.foreign_key_attribute or ""]
    assert attribute.api_name == "supplier_id"
    assert attribute.type.kind_ref == rel.to_kind
    assert not proposal.issues


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.95, False), (0.75, True)],
)
def test_inclusion_ratio_threshold(ratio: float, expected: bool) -> None:
    orders = table(
        "orders",
        order_no=["O1", "O2", "O3", "O4"],
        supplier_code=[CODES[0], CODES[1], CODES[2], "S-9999"],
    )
    proposal = infer_schema([SUPPLIER, orders], "d", InferenceOptions(min_inclusion_ratio=ratio))
    assert (proposal.schema.relation_by_api_name("supplier") is not None) is expected


def test_integer_references_need_a_name_hint() -> None:
    plant = table("plant", id=["1", "2", "3"], name=["a", "b", "c"])
    stock = table("stock", id=["10", "11", "12"], shelf=["1", "2", "3"], plant_id=["1", "2", "2"])
    proposal = infer_schema([plant, stock], "d")
    names = {r.api_name for r in proposal.schema.relations.values()}
    assert names == {"plant"}
    loose = infer_schema([plant, stock], "d", InferenceOptions(integer_reference_needs_name=False))
    assert len(loose.schema.relations) == 2


def test_self_reference() -> None:
    employees = table(
        "employee",
        employee_id=["E1", "E2", "E3", "E4"],
        name=["Ann", "Bob", "Cid", "Dan"],
        manager_id=[None, "E1", "E1", "E2"],
    )
    proposal = infer_schema([employees], "d")
    rel = relation(proposal, "manager")
    assert rel.from_kind == rel.to_kind
    assert rel.inverse_api_name == "manager_of"
    assert rel.cardinality is Cardinality.MANY_TO_ONE


def test_junction_table_becomes_many_to_many() -> None:
    supplied_by = table(
        "supplied_by",
        from_key=[PARTS[0], PARTS[1], PARTS[1], PARTS[2]],
        to_key=[CODES[0], CODES[1], CODES[2], CODES[0]],
        lead_time_days=["10", "20", "30", "40"],
    )
    proposal = infer_schema([SUPPLIER, PART, supplied_by], "d")
    assert {k.api_name for k in proposal.schema.kinds.values()} == {"Part", "Supplier"}
    rel = relation(proposal, "supplied_by")
    assert rel.cardinality is Cardinality.MANY_TO_MANY
    assert rel.backing is RelationBacking.EXPLICIT
    assert (kind_name(proposal, rel.from_kind), kind_name(proposal, rel.to_kind)) == (
        "Part",
        "Supplier",
    )
    assert [a.api_name for a in rel.attributes.values()] == ["lead_time_days"]
    assert rel.inverse_api_name == "parts"
    roles = {t.table: t.role.value for t in proposal.tables}
    assert roles == {"part": "kind", "supplier": "kind", "supplied_by": "relation"}


def test_link_table_keyed_by_one_side_becomes_many_to_one() -> None:
    orders = table("purchase_order", po_number=["PO-1", "PO-2", "PO-3"], qty=["1", "2", "3"])
    for_part = table(
        "for_part", from_key=["PO-1", "PO-2", "PO-3"], to_key=[PARTS[0], PARTS[0], PARTS[1]]
    )
    proposal = infer_schema([PART, orders, for_part], "d")
    rel = relation(proposal, "for_part")
    assert rel.cardinality is Cardinality.MANY_TO_ONE
    assert (kind_name(proposal, rel.from_kind), kind_name(proposal, rel.to_kind)) == (
        "PurchaseOrder",
        "Part",
    )


def test_junction_direction_from_column_names() -> None:
    link = table(
        "approvals",
        to_supplier_id=[CODES[0], CODES[1], CODES[0]],
        from_part_id=[PARTS[0], PARTS[1], PARTS[1]],
    )
    proposal = infer_schema([SUPPLIER, PART, link], "d")
    rel = relation(proposal, "approvals")
    assert (kind_name(proposal, rel.from_kind), kind_name(proposal, rel.to_kind)) == (
        "Part",
        "Supplier",
    )


def test_junction_detection_can_be_disabled() -> None:
    supplied_by = table(
        "supplied_by",
        from_key=[PARTS[0], PARTS[1], PARTS[1]],
        to_key=[CODES[0], CODES[1], CODES[2]],
    )
    proposal = infer_schema(
        [SUPPLIER, PART, supplied_by], "d", InferenceOptions(detect_junction_tables=False)
    )
    assert "SuppliedBy" in {k.api_name for k in proposal.schema.kinds.values()}
    assert len(proposal.schema.relations) == 2


def test_equal_key_sets_belong_to_the_widest_table() -> None:
    orders = table(
        "purchase_order",
        po_number=["PO-1", "PO-2"],
        status=["open", "done"],
        qty=["1", "2"],
    )
    delivered = table("delivered_to", from_key=["PO-1", "PO-2"], to_key=[PARTS[0], PARTS[1]])
    proposal = infer_schema([PART, orders, delivered], "d")
    rel = relation(proposal, "delivered_to")
    assert kind_name(proposal, rel.from_kind) == "PurchaseOrder"
    assert "DeliveredTo" not in {k.api_name for k in proposal.schema.kinds.values()}


def test_declared_foreign_keys_need_no_evidence() -> None:
    orders = TableProfile(
        "orders",
        (
            ColumnProfile("order_no", ["O1", "O2"]),
            ColumnProfile("vendor", ["X", "Y"]),
        ),
        foreign_keys=(DeclaredForeignKey(("vendor",), "supplier", ("supplier_code",)),),
    )
    proposal = infer_schema([SUPPLIER, orders], "d")
    evidence = proposal.evidence_for("relations.vendor")
    assert evidence is not None
    assert evidence.confidence == 1.0
    composite = TableProfile(
        "orders",
        orders.columns,
        foreign_keys=(DeclaredForeignKey(("vendor", "order_no"), "supplier", ("a", "b")),),
    )
    issues = infer_schema([SUPPLIER, composite], "d").issues
    assert InferenceIssueCode.UNSUPPORTED_REFERENCE in {i.code for i in issues}


def test_relation_name_collisions_are_resolved() -> None:
    contract = table(
        "contract",
        contract_number=["C-1", "C-2"],
        supplier_code=[CODES[0], CODES[1]],
    )
    orders = table("orders", order_no=["O1", "O2"], supplier_code=[CODES[0], CODES[1]])
    proposal = infer_schema([SUPPLIER, contract, orders], "d")
    names = sorted((r.api_name, r.inverse_api_name) for r in proposal.schema.relations.values())
    assert names == [("order_supplier", "orders"), ("supplier", "contracts")]
    assert InferenceIssueCode.NAME_ADJUSTED in {i.code for i in proposal.issues}
