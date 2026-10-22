"""Folding observations into a stored schema suggestion."""

from __future__ import annotations

from lorebase_core.infer.suggestions import Observation, SuggestionOptions, SuggestionType
from lorebase_store.repositories.suggestions import fold

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
