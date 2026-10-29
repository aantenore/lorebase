"""Wrapping of sensitive claim values, evidence and decided values (design 10.6)."""

from __future__ import annotations

from lorebase_core.model import Sensitivity
from lorebase_store.repositories import wrap_claim, wrap_decision

EVIDENCE = {"document_id": "doc_1", "chunk": 2, "start": 10, "end": 18, "quote": "VAT PL123"}
CLAIM = {"claim_id": "p1", "value": "PL123", "evidence": EVIDENCE}


def wrapped(value: object, *tags: Sensitivity) -> dict[str, object]:
    return {"$sensitive": {"tags": sorted(t.value for t in tags), "value": value}}


def test_sensitive_evidence_quotes_are_wrapped_and_locators_kept() -> None:
    out = wrap_claim(CLAIM, (Sensitivity.CONFIDENTIAL,))
    assert out["value"] == wrapped("PL123", Sensitivity.CONFIDENTIAL)
    assert out["evidence"] == {
        "document_id": "doc_1",
        "chunk": 2,
        "start": 10,
        "end": 18,
        "quote": wrapped("VAT PL123", Sensitivity.CONFIDENTIAL),
    }


def test_vault_evidence_keeps_the_passage_location_only() -> None:
    out = wrap_claim({**CLAIM, "value": {"$vault": "vlt_1"}}, (Sensitivity.PII,), vault=True)
    assert out["evidence"] == {"document_id": "doc_1", "chunk": 2, "start": 10, "end": 18}
    assert out["value"] == wrapped({"$vault": "vlt_1"}, Sensitivity.PII)


def test_untagged_claims_are_left_alone() -> None:
    assert wrap_claim(CLAIM, ()) == CLAIM


def test_decided_values_are_wrapped() -> None:
    decision = {"choice": "custom", "value": "PL9", "values": {"vat_number": "PL9"}}
    out = wrap_decision(decision, (Sensitivity.CONFIDENTIAL,))
    assert out["choice"] == "custom"
    assert out["value"] == wrapped("PL9", Sensitivity.CONFIDENTIAL)
    assert out["values"] == {"vat_number": wrapped("PL9", Sensitivity.CONFIDENTIAL)}
    assert wrap_decision({"choice": "keep_current"}, ()) == {"choice": "keep_current"}
