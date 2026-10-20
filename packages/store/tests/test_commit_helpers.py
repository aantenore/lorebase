"""Pure helpers of the commit pipeline and the materializer (no database)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lorebase_core.model import Link, Record, Schema
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, link_key, relation_partition
from lorebase_core.types import TypeSpec
from lorebase_core.validate import IssueCode, LinkDelete, RecordDelete, RecordUpsert
from lorebase_store.commit import SchemaChangeError, SchemaDelete, SchemaUpsert
from lorebase_store.commit.migrations import plan_migrations
from lorebase_store.commit.schema import (
    apply_schema_changes,
    compile_schema,
    element_from_body,
    load_compiled,
    object_type_of,
    schema_elements,
    schema_from_elements,
)
from lorebase_store.materialize.rows import (
    PartitionId,
    PartitionType,
    changed_attributes,
    next_lineage,
    parse_partition,
    record_title,
    search_weights,
    split_link_key,
)

from .support import (
    COUNTRY,
    DOMAIN,
    LEGAL_NAME,
    PART,
    RATING,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_CODE,
    VAT,
    attribute,
    domain,
    part_kind,
    supplied_by,
    supplier_kind,
)

AT = datetime(2026, 10, 20, tzinfo=UTC)


def supply_schema() -> Schema:
    return Schema(
        domain=domain(),
        kinds={SUPPLIER: supplier_kind(), PART: part_kind()},
        relations={SUPPLIED_BY: supplied_by()},
    )


def test_partition_ids_round_trip() -> None:
    assert parse_partition(PARTITION_SCHEMA) == PartitionId(PartitionType.SCHEMA, "")
    assert parse_partition(kind_partition(SUPPLIER)) == PartitionId(PartitionType.KIND, SUPPLIER)
    assert parse_partition(relation_partition(SUPPLIED_BY)) == PartitionId(
        PartitionType.RELATION, SUPPLIED_BY
    )
    assert split_link_key(link_key("P-1", "S-1")) == ("P-1", "S-1")
    with pytest.raises(ValueError, match="unknown partition"):
        parse_partition("x:1")
    with pytest.raises(ValueError, match="not a Link key"):
        split_link_key("P-1")


def test_lineage_folding() -> None:
    assert changed_attributes(None, {"a": 1}) == ["a"]
    assert changed_attributes({"a": 1, "b": 2}, {"a": 1, "c": 3}) == ["b", "c"]
    first = next_lineage(None, {"a": "p1", "b": "p1"}, {"a": 1, "b": 2})
    second = next_lineage(first, {"b": "p2", "$element": "p2"}, {"a": 1})
    assert second == {"a": "p1"}
    third = next_lineage(second, {"b": "p3"}, {"a": 1, "b": 5})
    assert third == {"a": "p1", "b": "p3"}
    # Folding every provenance map at once gives the same lineage.
    assert next_lineage(None, {"a": "p1", "b": "p3", "$element": "p2"}, {"a": 1, "b": 5}) == third


def test_titles_and_search_weights() -> None:
    kind = supplier_kind()
    assert record_title(kind, {LEGAL_NAME: "Acme"}) == "Acme"
    assert record_title(kind, {}) is None
    assert record_title(None, {LEGAL_NAME: "Acme"}) is None
    assert search_weights(kind) == [(LEGAL_NAME, "B"), (COUNTRY, "C")]
    assert search_weights(None) == []


def test_schema_codec_round_trip() -> None:
    schema = supply_schema()
    elements = schema_elements(schema)
    assert set(elements) == {DOMAIN, SUPPLIER, PART, SUPPLIED_BY}
    parsed = [element_from_body(object_type_of(e), e.content()) for e in elements.values()]
    assert schema_from_elements(parsed) == schema
    assert load_compiled(compile_schema(schema)) == schema


def test_apply_schema_changes() -> None:
    schema = supply_schema()
    changed, upserted, removed = apply_schema_changes(
        schema, [SchemaDelete(SUPPLIED_BY), SchemaUpsert(supplier_kind(description="New."))], DOMAIN
    )
    assert SUPPLIED_BY not in changed.relations
    assert changed.kinds[SUPPLIER].description == "New."
    assert [e.id for e in upserted] == [SUPPLIER]
    assert removed == [SUPPLIED_BY]
    for changes, message in (
        ([SchemaDelete(DOMAIN)], "cannot be removed"),
        ([SchemaDelete("knd_01JB8Z00000000000000000099")], "unknown"),
        ([SchemaDelete(PART), SchemaDelete(PART)], "more than once"),
    ):
        with pytest.raises(SchemaChangeError, match=message):
            apply_schema_changes(schema, changes, DOMAIN)


def test_plan_migrations_casts_defaults_drops_and_removals() -> None:
    old = supply_schema()
    retyped = supplier_kind(rating=TypeSpec.decimal(4, 1))
    attributes = dict(retyped.attributes)
    del attributes[VAT]
    tier = attribute("att_01JB8Z00000000000000000199", "tier", TypeSpec.of("string"), default="3")
    attributes[tier.id] = tier
    new = old.model_copy(
        update={
            "kinds": {SUPPLIER: retyped.model_copy(update={"attributes": attributes})},
            "relations": {},
        }
    )
    records = {
        SUPPLIER: [
            Record(
                kind=SUPPLIER,
                key="S-1",
                values={SUPPLIER_CODE: "S-1", LEGAL_NAME: "A", RATING: "4", VAT: "X"},
            ),
            Record(
                kind=SUPPLIER,
                key="S-2",
                values={SUPPLIER_CODE: "S-2", LEGAL_NAME: "B", RATING: "bad"},
            ),
            Record(kind=SUPPLIER, key="S-3", values={SUPPLIER_CODE: "S-3", LEGAL_NAME: "C"}),
        ],
        PART: [Record(kind=PART, key="P-1", values={})],
    }
    links = {SUPPLIED_BY: [Link(relation=SUPPLIED_BY, from_key="P-1", to_key="S-1")]}
    plan = plan_migrations(
        old,
        new,
        records_of=lambda kind: records[kind],
        links_of=lambda relation: links[relation],
        skip=[Record(kind=SUPPLIER, key="S-3", values={}).identity],
        actor="usr_t",
        at=AT,
    )
    assert plan.removals == [
        RecordDelete(kind=PART, key="P-1"),
        LinkDelete(relation=SUPPLIED_BY, from_key="P-1", to_key="S-1"),
    ]
    [change] = plan.changes
    assert isinstance(change.change, RecordUpsert)
    assert change.change.record.values == {
        SUPPLIER_CODE: "S-1",
        LEGAL_NAME: "A",
        RATING: "4.0",
        tier.id: "3",
    }
    assert isinstance(change.provenance, dict)
    assert set(change.provenance) == {RATING, VAT, tier.id}
    [issue] = plan.issues
    assert (issue.code, issue.key, issue.details["class"]) == (IssueCode.VALUE_TYPE, "S-2", "XS3")
    assert len(plan.provenance) == 3
    assert plan.refresh_kinds == [SUPPLIER]


def test_plan_migrations_reports_new_duplicates() -> None:
    old = supply_schema()
    attributes = dict(old.kinds[SUPPLIER].attributes)
    attributes[COUNTRY] = attributes[COUNTRY].model_copy(update={"unique": True})
    new = old.model_copy(
        update={
            "kinds": {
                **old.kinds,
                SUPPLIER: old.kinds[SUPPLIER].model_copy(update={"attributes": attributes}),
            }
        }
    )
    records = [
        Record(
            kind=SUPPLIER,
            key=f"S-{n}",
            values={SUPPLIER_CODE: f"S-{n}", LEGAL_NAME: "N", COUNTRY: "IT"},
        )
        for n in (1, 2)
    ]
    plan = plan_migrations(
        old,
        new,
        records_of=lambda kind: records if kind == SUPPLIER else [],
        links_of=lambda r: [],
        actor="usr_t",
        at=AT,
    )
    [issue] = plan.issues
    assert issue.code is IssueCode.VALUE_UNIQUE
    assert issue.details["keys"] == ["S-1", "S-2"]
