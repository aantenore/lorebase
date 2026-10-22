from __future__ import annotations

from collections.abc import Sequence

import pytest

from lorebase_core.model import Link, OnDelete, Record, Schema, Sensitivity
from lorebase_core.validate import (
    DataChange,
    IssueCode,
    LinkDelete,
    LinkUpsert,
    RecordDelete,
    RecordUpsert,
    evaluate_changeset,
    validate_changeset,
)

from ..model.supply import (
    CONTRACT,
    FLAGS,
    MADE_AT,
    PLANT,
    RISK_SCORE,
    SUPPLIED_BY,
    SUPPLIER,
    VAT,
    WITH_SUPPLIER,
    replace_attribute,
    replace_relation,
    supply_schema,
)
from .data import (
    CONTRACT_1,
    CONTRACT_2,
    FLAG_1,
    FLAG_2,
    MemoryIndex,
    contract,
    existing_index,
    link,
    part,
    plant,
    risk_flag,
    supplier,
)

C = IssueCode


def upsert(item: Record | Link) -> DataChange:
    if isinstance(item, Record):
        return RecordUpsert(record=item)
    return LinkUpsert(link=item)


def delete_record(kind: str, key: str) -> DataChange:
    return RecordDelete(kind=kind, key=key)


def delete_link(relation: str, from_key: str, to_key: str) -> DataChange:
    return LinkDelete(relation=relation, from_key=from_key, to_key=to_key)


def codes(
    changes: Sequence[DataChange],
    *,
    schema: Schema | None = None,
    index: MemoryIndex | None = None,
) -> list[IssueCode]:
    issues = validate_changeset(schema or supply_schema(), index or existing_index(), changes)
    return [issue.code for issue in issues]


CASES: list[tuple[str, list[DataChange], list[IssueCode]]] = [
    # Valid batches
    (
        "create-supplier-and-linked-part",
        [upsert(supplier("S-3")), upsert(part("P-3")), upsert(link(SUPPLIED_BY, "P-3", "S-3"))],
        [],
    ),
    ("update-existing-record", [upsert(supplier("S-1", vat="IT001", **{RISK_SCORE: "10"}))], []),
    (
        "contract-with-its-supplier",
        [upsert(contract(CONTRACT_2)), upsert(link(WITH_SUPPLIER, CONTRACT_2, "S-2"))],
        [],
    ),
    # Per-record rules still apply inside a batch
    ("record-rules", [upsert(supplier("S-3", **{RISK_SCORE: "500"}))], [C.VALUE_MAX]),
    ("link-rules", [upsert(link(MADE_AT, "P-1", "TRN"))], [C.LINK_FOREIGN_KEY_BACKED]),
    # Batch structure
    (
        "duplicate-change",
        [upsert(supplier("S-3")), delete_record(SUPPLIER, "S-3")],
        [C.CHANGESET_DUPLICATE_CHANGE],
    ),
    ("delete-missing-record", [delete_record(SUPPLIER, "S-9")], [C.RECORD_NOT_FOUND]),
    ("delete-missing-link", [delete_link(SUPPLIED_BY, "P-1", "S-2")], [C.LINK_NOT_FOUND]),
    # Uniqueness
    ("unique-taken", [upsert(supplier("S-3", vat="IT001"))], [C.VALUE_UNIQUE]),
    (
        "unique-twice-in-batch",
        [upsert(supplier("S-3", vat="IT009")), upsert(supplier("S-4", vat="IT009"))],
        [C.VALUE_UNIQUE],
    ),
    (
        "unique-released-by-update",
        [upsert(supplier("S-1", vat="IT100")), upsert(supplier("S-3", vat="IT001"))],
        [],
    ),
    (
        "unique-kept-by-update",
        [upsert(supplier("S-1", vat="IT001")), upsert(supplier("S-3", vat="IT001"))],
        [C.VALUE_UNIQUE],
    ),
    (
        "unique-released-by-delete",
        [
            delete_record(SUPPLIER, "S-2"),
            upsert(supplier("S-3", vat="IT002")),
        ],
        [],
    ),
    # Referential integrity
    (
        "link-to-missing-record",
        [upsert(link(SUPPLIED_BY, "P-1", "S-9"))],
        [C.LINK_ENDPOINT_MISSING],
    ),
    (
        "link-to-record-deleted-in-batch",
        [delete_record(SUPPLIER, "S-2"), upsert(link(SUPPLIED_BY, "P-1", "S-2"))],
        [C.LINK_ENDPOINT_MISSING],
    ),
    ("ref-to-missing-record", [upsert(part("P-3", "MIL"))], [C.VALUE_REFERENCE_MISSING]),
    (
        "ref-to-record-created-in-batch",
        [upsert(plant("MIL")), upsert(part("P-3", "MIL"))],
        [],
    ),
    (
        "delete-referenced-record",
        [delete_record(PLANT, "TRN")],
        [C.RECORD_DELETE_RESTRICTED],
    ),
    (
        "delete-reference-released-in-batch",
        [delete_record(PLANT, "TRN"), upsert(part("P-1"))],
        [],
    ),
    (
        "delete-with-restrict-links",
        [delete_record(SUPPLIER, "S-1")],
        [C.RECORD_DELETE_RESTRICTED],
    ),
    (
        "delete-with-restrict-link-removed-first",
        [
            delete_link(WITH_SUPPLIER, CONTRACT_1, "S-1"),
            delete_record(SUPPLIER, "S-1"),
            delete_record(CONTRACT, CONTRACT_1),
        ],
        [],
    ),
    # Cardinality
    (
        "one-to-one-second-link-from",
        [upsert(link(FLAGS, FLAG_1, "S-2"))],
        [C.LINK_CARDINALITY],
    ),
    (
        "one-to-one-second-link-to",
        [upsert(risk_flag(FLAG_2)), upsert(link(FLAGS, FLAG_2, "S-1"))],
        [C.LINK_CARDINALITY],
    ),
    (
        "one-to-one-replaced",
        [delete_link(FLAGS, FLAG_1, "S-1"), upsert(link(FLAGS, FLAG_1, "S-2"))],
        [],
    ),
    (
        "competing-links-in-batch",
        [
            upsert(risk_flag(FLAG_2)),
            upsert(link(FLAGS, FLAG_2, "S-2")),
            upsert(supplier("S-3")),
            upsert(link(FLAGS, FLAG_2, "S-3")),
        ],
        [C.LINK_CARDINALITY],
    ),
    (
        "many-to-one-second-supplier",
        [upsert(link(WITH_SUPPLIER, CONTRACT_1, "S-2"))],
        [C.LINK_CARDINALITY],
    ),
    ("many-to-many-unbounded", [upsert(link(SUPPLIED_BY, "P-1", "S-2"))], []),
    ("relink-existing-is-not-new", [upsert(link(FLAGS, FLAG_1, "S-1"))], []),
    # Required relation
    ("created-without-required-link", [upsert(contract(CONTRACT_2))], [C.RECORD_REQUIRED_RELATION]),
    (
        "last-required-link-removed",
        [delete_link(WITH_SUPPLIER, CONTRACT_1, "S-1")],
        [C.RECORD_REQUIRED_RELATION],
    ),
    (
        "required-link-replaced",
        [
            delete_link(WITH_SUPPLIER, CONTRACT_1, "S-1"),
            upsert(link(WITH_SUPPLIER, CONTRACT_1, "S-2")),
        ],
        [],
    ),
]


@pytest.mark.parametrize(("changes", "expected"), [pytest.param(c, e, id=n) for n, c, e in CASES])
def test_changeset_rules(changes: list[DataChange], expected: list[IssueCode]) -> None:
    assert codes(changes) == expected


def test_cascade_yields_link_deletions() -> None:
    report = evaluate_changeset(supply_schema(), existing_index(), [delete_record(SUPPLIER, "S-2")])
    assert report.issues == []
    assert report.derived_changes == [delete_link(SUPPLIED_BY, "P-2", "S-2")]


def test_cascade_and_restrict_on_the_same_record() -> None:
    report = evaluate_changeset(supply_schema(), existing_index(), [delete_record(SUPPLIER, "S-1")])
    assert [(i.code, i.relation) for i in report.issues] == [
        (C.RECORD_DELETE_RESTRICTED, WITH_SUPPLIER)
    ]
    assert {change.identity for change in report.derived_changes} == {
        (SUPPLIED_BY, "P-1", "S-1"),
        (FLAGS, FLAG_1, "S-1"),
    }


def test_restrict_issue_lists_links() -> None:
    (issue,) = validate_changeset(
        supply_schema(), existing_index(), [delete_record(SUPPLIER, "S-1")]
    )
    assert issue.details == {"count": 1, "links": [[CONTRACT_1, "S-1"]]}
    assert issue.path == ("changes", 0)


def test_cascaded_deletions_count_for_required_relations() -> None:
    schema = replace_relation(supply_schema(), WITH_SUPPLIER, on_delete=OnDelete.CASCADE)
    issues = validate_changeset(schema, existing_index(), [delete_record(SUPPLIER, "S-1")])
    assert [(i.code, i.key) for i in issues] == [(C.RECORD_REQUIRED_RELATION, CONTRACT_1)]


def test_required_foreign_key_relation() -> None:
    schema = replace_relation(supply_schema(), MADE_AT, required_from=True)
    assert codes([upsert(part("P-3"))], schema=schema) == [C.RECORD_REQUIRED_RELATION]
    assert codes([upsert(part("P-3", "TRN"))], schema=schema) == []


def test_cardinality_issue_lists_competing_links() -> None:
    (issue,) = validate_changeset(
        supply_schema(), existing_index(), [upsert(link(FLAGS, FLAG_1, "S-2"))]
    )
    assert issue.relation == FLAGS
    assert issue.details == {"side": "from", "count": 2, "links": [[FLAG_1, "S-2"]]}


def test_empty_index_and_batch() -> None:
    assert validate_changeset(supply_schema(), MemoryIndex(), []) == []


def test_uniqueness_of_a_sensitive_value_does_not_name_the_owner() -> None:
    schema = replace_attribute(supply_schema(), SUPPLIER, VAT, sensitivity=(Sensitivity.PII,))
    taken = [upsert(supplier("S-3", vat="IT001"))]
    [issue] = evaluate_changeset(schema, existing_index(), taken).issues
    assert issue.code is C.VALUE_UNIQUE
    assert issue.details == {}
    assert "S-1" not in issue.message
    assert "IT001" not in issue.message
    [plain] = evaluate_changeset(supply_schema(), existing_index(), taken).issues
    assert plain.details == {"other_key": "S-1"}
