"""Folding observations into a stored schema suggestion."""

from __future__ import annotations

from typing import Any

from lorebase_core.infer.suggestions import Observation, SuggestionOptions, SuggestionType
from lorebase_store.repositories.suggestions import (
    SuggestionRow,
    combine,
    consolidate,
    fold,
    is_vocabulary_share,
)

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


def test_a_kind_named_like_a_field_stays_when_it_shows_an_entity_of_its_own() -> None:
    kind, attribute = SuggestionType.KIND, SuggestionType.ATTRIBUTE
    codes = [sighted(kind, "Buyer", "B-1", kind=None), sighted(kind, "Buyer", "B-2", kind=None)]
    fields = [
        sighted(kind, "Carrier", kind=None),
        sighted(attribute, "carrier name", "Acme", kind="+Carrier"),
        sighted(attribute, "carrier code", "C-9", kind="+Carrier"),
    ]
    one_code = [sighted(kind, "Buyer", "B-1", kind=None), sighted(kind, "Buyer", None, kind=None)]
    seen = [sighted(attribute, "buyer", "Corvane"), sighted(attribute, "carrier", "Acme")]
    _, names = consolidate([*seen, *codes], [], OPTIONS)
    assert "Buyer" in names.values()
    _, names = consolidate([*seen, *fields], [], OPTIONS)
    assert "Carrier" in names.values()
    _, names = consolidate([*seen, *one_code], [], OPTIONS)
    assert "Buyer" not in names.values()
    stored_kind = {
        "fingerprint": "kind:buyer",
        "suggestion_type": "kind",
        "name": "Buyer",
        "kind_ref": None,
        "samples": ["B-1"],
        "support": 1,
    }
    _, names = consolidate(
        [*seen, sighted(kind, "Buyer", "B-2", kind=None)], [stored_kind], OPTIONS
    )
    assert "Buyer" in names.values()  # one code stored, one now: two in all


def test_stored_variants_combine_into_the_kept_suggestion() -> None:
    def row(name: str, documents: dict[str, int], samples: list[str], **extra: object) -> dict:
        return {
            "name": name,
            "observations": {d: {"n": n, "c": 0.5 * n} for d, n in documents.items()},
            "examples": [{"document_id": d, "start": 1, "end": 2} for d in documents],
            "samples": samples,
            "labels": [name],
            "sources": ["rules"],
            **extra,
        }

    kept = row("renewal_notice_days", {"doc_a": 2, "doc_b": 1}, ["90"])
    other = row(
        "renewal_proposal_notice_days", {"doc_b": 3, "doc_c": 1}, ["90", "60"], sources=["m"]
    )
    values = combine(kept, [other], OPTIONS)
    assert values["support"] == 7
    assert {d: v["n"] for d, v in values["observations"].items()} == {
        "doc_a": 2,
        "doc_b": 4,
        "doc_c": 1,
    }
    assert values["confidence"] == 0.5
    assert values["samples"] == ["90", "60"]
    assert values["labels"] == ["renewal_notice_days", "renewal_proposal_notice_days"]
    assert values["sources"] == ["rules", "m"]
    assert len(values["examples"]) == OPTIONS.max_examples


def test_combine_inherits_the_target_and_orders_examples_by_document() -> None:
    def row(name: str, document: str, target: str | None) -> dict:
        return {
            "name": name,
            "target_ref": target,
            "observations": {document: {"n": 1}},  # a legacy share without confidence
            "examples": [{"document_id": document, "start": s, "end": s + 1} for s in (1, 2, 3)],
            "samples": [],
            "labels": [],
            "sources": [],
        }

    kept = row("a", "doc_a", None)
    values = combine(kept, [row("b", "doc_b", "knd_x"), row("c", "doc_c", "knd_y")], OPTIONS)
    assert values["target_ref"] == "knd_x"
    assert values["support"] == 3
    assert values["confidence"] == 0.0
    roomy = combine(kept, [row("b", "doc_b", None)], SuggestionOptions(max_examples=5))
    assert [e["document_id"] for e in roomy["examples"]] == ["doc_a"] * 3 + ["doc_b"] * 2


def test_documents_count_only_document_shares() -> None:
    assert is_vocabulary_share("miss:main:paused")
    assert is_vocabulary_share("llm:main:atr_1")
    assert not is_vocabulary_share("doc_a")
    shares = {
        "doc_a": {"n": 1, "c": 0.6},
        "doc_b": {"n": 2, "c": 1.2},
        "miss:main:paused": {"n": 4, "c": 2.4},
        "llm:main:atr_1": {"n": 1, "c": 0.6},
    }

    def row(observations: dict[str, Any]) -> SuggestionRow:
        return SuggestionRow(
            id="sug_1",
            fingerprint="f",
            suggestion_type="value_alias",
            name="paused",
            kind_ref=None,
            target_ref=None,
            labels=[],
            support=0,
            confidence=0.6,
            observations=observations,
            examples=[],
            samples=[],
            sources=[],
            payload={},
            status="pending",
            decided_by=None,
            decided_at=None,
            proposal_id=None,
            branch=None,
            last_job_id=None,
            created_at=None,
            updated_at=None,
        )

    assert row(shares).documents == 2
    assert row({k: v for k, v in shares.items() if ":" in k}).documents == 0
