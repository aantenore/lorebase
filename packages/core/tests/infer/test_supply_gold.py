"""Gold test: inference on examples/supply/data recovers examples/supply/schema.yaml.

The recall table is printed with the test output (``pytest -s`` or ``-rP``).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lorebase_core.infer import SchemaProposal, infer_schema
from lorebase_core.model import Cardinality, Kind, Schema, load_schema
from lorebase_core.types import TypeSpec

from .support import SUPPLY_DIR, supply_tables

#: Relations must be proposed with at least this confidence.
MIN_RELATION_CONFIDENCE = 0.7
#: Share of gold Attribute types (base type, and scale for decimals) recovered.
MIN_TYPE_RECALL = 0.85


@dataclass
class Row:
    element: str
    found: bool
    detail: str
    confidence: float | None = None


def _type_matches(gold: TypeSpec, ours: TypeSpec, gold_schema: Schema, schema: Schema) -> bool:
    if gold.name != ours.name:
        return False
    if gold.kind_ref is not None and ours.kind_ref is not None:
        return gold_schema.kinds[gold.kind_ref].api_name == schema.kinds[ours.kind_ref].api_name
    return gold.scale == ours.scale


def _kind_rows(gold_schema: Schema, proposal: SchemaProposal) -> tuple[list[Row], int, int, int]:
    schema = proposal.schema
    rows: list[Row] = []
    names = types = total = 0
    for gold in sorted(gold_schema.kinds.values(), key=lambda k: k.api_name):
        ours: Kind | None = schema.kind_by_api_name(gold.api_name)
        evidence = proposal.evidence_for(f"kinds.{gold.api_name}")
        if ours is None:
            rows.append(Row(f"Kind {gold.api_name}", False, "missing"))
            continue
        gold_key = [gold.attributes[a].api_name for a in gold.primary_key]
        our_key = [ours.attributes[a].api_name for a in ours.primary_key]
        key_text = ",".join(our_key) or "minted"
        rows.append(
            Row(
                f"Kind {gold.api_name}",
                True,
                f"key {key_text} " + ("ok" if gold_key == our_key else f"(gold {gold_key})"),
                evidence.confidence if evidence else None,
            )
        )
        for attribute in gold.attributes.values():
            if attribute.derivation_id is not None:
                continue  # derived Attributes have no column
            total += 1
            mine = ours.attribute_by_api_name(attribute.api_name)
            if mine is None:
                rows.append(Row(f"  {attribute.api_name}", False, "missing"))
                continue
            names += 1
            match = _type_matches(attribute.type, mine.type, gold_schema, schema)
            types += match
            if not match:
                rows.append(
                    Row(
                        f"  {attribute.api_name}",
                        True,
                        f"type {mine.type.describe()} (gold {attribute.type.describe()})",
                    )
                )
    return rows, names, types, total


def _relation_rows(gold_schema: Schema, proposal: SchemaProposal) -> list[Row]:
    schema = proposal.schema
    rows = []
    for gold in sorted(gold_schema.relations.values(), key=lambda r: r.api_name):
        ends = (
            gold_schema.kinds[gold.from_kind].api_name,
            gold_schema.kinds[gold.to_kind].api_name,
        )
        candidates = [
            r
            for r in schema.relations.values()
            if (schema.kinds[r.from_kind].api_name, schema.kinds[r.to_kind].api_name) == ends
            and r.cardinality is gold.cardinality
        ]
        label = f"Relation {gold.api_name} {ends[0]}->{ends[1]} {gold.cardinality.value}"
        if not candidates:
            rows.append(Row(label, False, "missing"))
            continue
        best = next((r for r in candidates if r.api_name == gold.api_name), candidates[0])
        evidence = proposal.evidence_for(f"relations.{best.api_name}")
        rows.append(
            Row(label, True, f"as {best.api_name}", evidence.confidence if evidence else None)
        )
    return rows


def _print(rows: list[Row], summary: list[str]) -> None:
    width = max(len(r.element) for r in rows)
    lines = ["", "Recall on examples/supply (gold: schema.yaml)", ""]
    lines.append(f"{'Gold element':<{width}}  Found  {'Confidence':>10}  Detail")
    for row in rows:
        confidence = "" if row.confidence is None else f"{row.confidence:.2f}"
        found = "yes" if row.found else "NO"
        lines.append(f"{row.element:<{width}}  {found:<5}  {confidence:>10}  {row.detail}")
    lines.extend(["", *summary, ""])
    print("\n".join(lines))


def test_inference_recovers_the_supply_schema(capsys: pytest.CaptureFixture[str]) -> None:
    gold_schema = load_schema((SUPPLY_DIR / "schema.yaml").read_text())
    proposal = infer_schema(supply_tables(), "supply")

    kind_rows, names, types, total = _kind_rows(gold_schema, proposal)
    relation_rows = _relation_rows(gold_schema, proposal)
    kinds = [r for r in kind_rows if r.element.startswith("Kind")]
    keys_ok = sum(r.detail.endswith("ok") for r in kinds)
    many_to_one = [r for r in relation_rows if Cardinality.MANY_TO_ONE.value in r.element]
    summary = [
        f"Kinds: {sum(r.found for r in kinds)}/{len(gold_schema.kinds)}",
        f"Primary keys: {keys_ok}/{len(gold_schema.kinds)}",
        f"Attribute names: {names}/{total}",
        f"Attribute types: {types}/{total} ({types / total:.0%})",
        f"Relations: {sum(r.found for r in relation_rows)}/{len(relation_rows)} "
        f"(many_to_one {sum(r.found for r in many_to_one)}/{len(many_to_one)})",
    ]
    with capsys.disabled():
        _print(kind_rows + relation_rows, summary)

    assert all(r.found for r in kinds)
    assert keys_ok == len(gold_schema.kinds)
    assert names == total
    assert types / total >= MIN_TYPE_RECALL
    for row in many_to_one:
        assert row.found, row.element
        assert row.confidence is not None
        assert row.confidence >= MIN_RELATION_CONFIDENCE
    assert all(r.found for r in relation_rows)
    assert {t.table: t.role.value for t in proposal.tables} == {
        "contract": "kind",
        "part": "kind",
        "plant": "kind",
        "purchase_order": "kind",
        "risk_flag": "kind",
        "supplier": "kind",
        "delivered_to": "relation",
        "flags": "relation",
        "for_part": "relation",
        "stocked_at": "relation",
        "supplied_by": "relation",
        "with_supplier": "relation",
    }
