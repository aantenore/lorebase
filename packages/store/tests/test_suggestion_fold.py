"""Folding observations into a stored schema suggestion."""

from __future__ import annotations

from lorebase_core.infer.suggestions import Observation, SuggestionOptions, SuggestionType
from lorebase_store.repositories.suggestions import consolidate, fold

OPTIONS = SuggestionOptions(max_examples=2, max_samples=3)


def seen(document: str, start: int, value: str, confidence: float = 0.6) -> Observation:
    return Observation(
        SuggestionType.ATTRIBUTE,
        "Ends",
        document,
        confidence,
        "rules",
        kind="knd_1",
        value=value,
        evidence={"document_id": document, "chunk": 0, "start": start, "end": start + 5},
    )


def test_a_document_extracted_again_replaces_its_share() -> None:
    first = fold(None, [seen("doc_a", 0, "2026-02-07"), seen("doc_a", 10, "2026-02-09")], OPTIONS)
    assert first["support"] == 2
    assert first["observations"] == {"doc_a": {"n": 2, "c": 1.2}}
    again = fold(first, [seen("doc_a", 0, "2026-02-07")], OPTIONS)
    assert again["support"] == 1
    assert [e["start"] for e in again["examples"]] == [0]
    other = fold(again, [seen("doc_b", 3, "2026-04-12", 0.9)], OPTIONS)
    assert other["support"] == 2
    assert other["confidence"] == 0.75
    assert other["samples"] == ["2026-02-07", "2026-02-09", "2026-04-12"]
    assert other["labels"] == ["Ends"]
    assert other["sources"] == ["rules"]


def test_examples_and_samples_are_bounded_and_span_documents() -> None:
    stored = fold(None, [seen("doc_a", i, f"v{i}") for i in range(5)], OPTIONS)
    assert len(stored["examples"]) == 2
    assert len(stored["samples"]) == 3
    # A new document's example is kept within the bound, before more of the same document.
    mixed = fold(
        {**stored, "examples": stored["examples"][:1]},
        [seen("doc_b", 7, "w"), seen("doc_b", 9, "x")],
        OPTIONS,
    )
    assert [e["document_id"] for e in mixed["examples"]] == ["doc_a", "doc_b"]


def sighted(
    type_: SuggestionType, label: str, value: str | None = None, kind: str | None = "knd_c"
) -> Observation:
    return Observation(type_, label, "doc_a", 0.6, "tier2", kind=kind, value=value)


def test_variants_and_noise_kinds_consolidate() -> None:
    attribute = SuggestionType.ATTRIBUTE
    observations = [
        sighted(attribute, "renewal notice days", "90"),
        sighted(attribute, "renewal notice days", "90"),
        sighted(attribute, "renewal proposal notice days", "90"),
        sighted(attribute, "termination notice days", "30"),
        sighted(attribute, "termination for cause notice days", "30"),
        sighted(attribute, "buyer", "Corvane"),
        sighted(SuggestionType.KIND, "Buyer", kind=None),
        sighted(SuggestionType.KIND, "Proposal", kind=None),
        sighted(SuggestionType.KIND, "Quality notice", kind=None),
    ]
    groups, names = consolidate(observations, [], OPTIONS)
    assert sorted(names.values()) == [
        "QualityNotice",
        "buyer",
        "renewal_notice_days",
        "termination_notice_days",
    ]
    by_name = {names[k]: v for k, v in groups.items()}
    assert len(by_name["renewal_notice_days"]) == 3
    assert {o.label for o in by_name["termination_notice_days"]} == {
        "termination notice days",
        "termination for cause notice days",
    }


def test_a_variant_joins_the_stored_suggestion() -> None:
    stored = {
        "fingerprint": "attribute:knd_c:renewal_notice_day",
        "name": "renewal_notice_days",
        "kind_ref": "knd_c",
        "samples": ["90"],
        "support": 4,
    }
    seen_again = [sighted(SuggestionType.ATTRIBUTE, "renewal proposal notice days", "90")]
    groups, names = consolidate(seen_again, [stored], OPTIONS)
    assert list(groups) == [stored["fingerprint"]]
    assert names == {stored["fingerprint"]: "renewal_notice_days"}
