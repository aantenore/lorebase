"""In-memory ``IndexView`` and sample data for the ``supply`` fixture schema."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import JsonValue

from lorebase_core.model import Link, Record, new_ulid
from lorebase_core.types import CanonicalValue

from ..model.supply import (
    CONTRACT,
    CONTRACT_TITLE,
    FLAGS,
    LEGAL_NAME,
    PART,
    PART_NAME,
    PART_NUMBER,
    PLANT,
    PLANT_CODE,
    PLANT_COUNTRY,
    PLANT_NAME,
    PREFERRED_PLANT,
    REASON,
    RISK_FLAG,
    SEVERITY,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_CODE,
    SUPPLIER_COUNTRY,
    VAT,
    WITH_SUPPLIER,
)

CONTRACT_1 = new_ulid(timestamp_ms=1, randomness=bytes(10))
CONTRACT_2 = new_ulid(timestamp_ms=2, randomness=bytes(10))
FLAG_1 = new_ulid(timestamp_ms=3, randomness=bytes(10))
FLAG_2 = new_ulid(timestamp_ms=4, randomness=bytes(10))


class MemoryIndex:
    """``IndexView`` over Python collections."""

    def __init__(self, records: Iterable[Record] = (), links: Iterable[Link] = ()) -> None:
        self.records = {record.identity: record for record in records}
        self.links = {link.identity: link for link in links}

    def record_exists(self, kind_id: str, key: str) -> bool:
        return (kind_id, key) in self.records

    def find_unique(self, kind_id: str, attribute_id: str, value: CanonicalValue) -> str | None:
        for (kind, key), record in self.records.items():
            if kind == kind_id and record.values.get(attribute_id) == value:
                return key
        return None

    def keys_referencing(self, kind_id: str, attribute_id: str, key: str) -> Iterable[str]:
        for (kind, record_key), record in self.records.items():
            value = record.values.get(attribute_id)
            if kind == kind_id and (value == key or (isinstance(value, list) and key in value)):
                yield record_key

    def link_exists(self, relation_id: str, from_key: str, to_key: str) -> bool:
        return (relation_id, from_key, to_key) in self.links

    def count_links(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> int:
        return len(list(self.links_of(relation_id, from_key=from_key, to_key=to_key)))

    def links_of(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> Iterable[tuple[str, str]]:
        for relation, start, end in self.links:
            if relation != relation_id:
                continue
            if (from_key is None or start == from_key) and (to_key is None or end == to_key):
                yield (start, end)


def supplier(code: str, *, vat: str | None = None, **values: JsonValue) -> Record:
    base: dict[str, JsonValue] = {
        SUPPLIER_CODE: code,
        LEGAL_NAME: f"Supplier {code}",
        SUPPLIER_COUNTRY: "IT",
        VAT: vat,
    }
    return Record(kind=SUPPLIER, key=code, values={**base, **values})


def part(number: str, plant: str | None = None, /, **values: Any) -> Record:
    base: dict[str, JsonValue] = {
        PART_NUMBER: number,
        PART_NAME: f"Part {number}",
        PREFERRED_PLANT: plant,
    }
    return Record(kind=PART, key=number, values={**base, **values})


def plant(code: str) -> Record:
    return Record(
        kind=PLANT,
        key=code,
        values={PLANT_CODE: code, PLANT_NAME: f"Plant {code}", PLANT_COUNTRY: "IT"},
    )


def contract(key: str) -> Record:
    return Record(kind=CONTRACT, key=key, values={CONTRACT_TITLE: f"Contract {key}"})


def risk_flag(key: str) -> Record:
    return Record(kind=RISK_FLAG, key=key, values={SEVERITY: "low", REASON: "Late deliveries."})


def link(relation: str, from_key: str, to_key: str, **values: JsonValue) -> Link:
    return Link(relation=relation, from_key=from_key, to_key=to_key, values=values)


def existing_index() -> MemoryIndex:
    """S-1 and S-2 supply P-1 and P-2; contract C1 and flag F1 point at S-1."""
    return MemoryIndex(
        records=[
            supplier("S-1", vat="IT001"),
            supplier("S-2", vat="IT002"),
            part("P-1", "TRN"),
            part("P-2"),
            plant("TRN"),
            contract(CONTRACT_1),
            risk_flag(FLAG_1),
        ],
        links=[
            link(SUPPLIED_BY, "P-1", "S-1"),
            link(SUPPLIED_BY, "P-2", "S-2"),
            link(WITH_SUPPLIER, CONTRACT_1, "S-1"),
            link(FLAGS, FLAG_1, "S-1"),
        ],
    )
