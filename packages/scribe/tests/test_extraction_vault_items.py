"""Extraction items that set a value of a vault Attribute are never accepted at once (13.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lorebase_core.model import Attribute, Schema, Sensitivity
from lorebase_core.types import TypeSpec
from lorebase_scribe.extraction.assemble import CurrentState, assemble_items
from lorebase_scribe.extraction.candidates import (
    Evidence,
    Mention,
    Resolution,
    Resolved,
    ValueCandidate,
)
from lorebase_scribe.extraction.ontology import build_ontology
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.models.enums import ItemDecision

EMAIL = "att_01KEZF7C00VAVTEMA000000001"
NOW = datetime(2026, 10, 21, tzinfo=UTC)
SETTINGS = ExtractionSettings(auto_accept_threshold=0.6)
EVIDENCE = Evidence(document_id="doc_1", chunk=0, start=0, end=4, quote="text")


def _with_email(schema: Schema) -> tuple[Schema, str]:
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    email = Attribute(
        id=EMAIL,
        api_name="email",
        description="Contact email.",
        type=TypeSpec.of("string"),
        sensitivity=(Sensitivity.PII,),
        vault=True,
    )
    kind = supplier.model_copy(update={"attributes": {**supplier.attributes, EMAIL: email}})
    return schema.model_copy(update={"kinds": {**schema.kinds, kind.id: kind}}), kind.id


def _resolved(kind_id: str, key: str, status: Any) -> Resolved:
    mention = Mention(
        kind_id=kind_id,
        text=key,
        start=0,
        end=len(key),
        chunk=0,
        score=1.0,
        tier=0,
        source="key",
        extractor="test",
        document_id="doc_1",
    )
    return Resolved(mention, Resolution(kind_id, status, key, 1.0, "exact"))


def _value(subject: Resolved, attribute_id: str, value: Any) -> ValueCandidate:
    return ValueCandidate(subject, attribute_id, value, EVIDENCE, 1.0, 0, "test")


def _attribute_id(schema: Schema, kind_id: str, name: str) -> str:
    return next(a.id for a in schema.kinds[kind_id].attributes.values() if a.api_name == name)


def test_vault_values_wait_for_a_reviewer(supply_schema: Schema) -> None:
    schema, supplier = _with_email(supply_schema)
    ontology = build_ontology(schema, None)
    city = _attribute_id(schema, supplier, "city")
    known = _resolved(supplier, "S-1013", "matched")
    current = CurrentState(records={(supplier, "S-1013"): {}}, links=frozenset())
    values = [_value(known, city, "Graz"), _value(known, EMAIL, "guess@example.com")]
    items = assemble_items(
        ontology,
        [known],
        values,
        [],
        current,
        job_id="exj_1",
        principal="svc_scribe",
        now=NOW,
        settings=SETTINGS,
    ).items
    decisions = {i.subject["attribute"]: i.decision for i in items}
    assert decisions == {city: ItemDecision.ACCEPTED, EMAIL: ItemDecision.PENDING}

    # A new Record holding a vault value waits too; without it, it is accepted at once.
    new = _resolved(supplier, "S-9001", "new")
    complete = [
        _value(new, _attribute_id(schema, supplier, "legal_name"), "Guessed GmbH"),
        _value(new, _attribute_id(schema, supplier, "country"), "DE"),
    ]
    for extra, expected in (([], ItemDecision.ACCEPTED), ([EMAIL], ItemDecision.PENDING)):
        candidates = complete + [_value(new, a, "guess@example.com") for a in extra]
        (record,) = assemble_items(
            ontology,
            [new],
            candidates,
            [],
            CurrentState(records={}, links=frozenset()),
            job_id="exj_1",
            principal="svc_scribe",
            now=NOW,
            settings=SETTINGS,
        ).items
        assert record.decision is expected, record.match
