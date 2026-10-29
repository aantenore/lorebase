"""The semantic reconciliation stage without a database (design 10.6, stage d): similarity,
equivalence, the judge with its cache and budget, failures, the detector's hand-off and the
options a Domain may override."""

from __future__ import annotations

import functools
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from lorebase_core.model import (
    Attribute,
    IdPrefix,
    OperationOrigin,
    ProvenanceEntry,
    Schema,
    new_id,
)
from lorebase_core.reconcile import (
    CandidateDraft,
    CandidateType,
    Claim,
    ReconcileOptions,
    Stage,
)
from lorebase_core.types import AttributeType, TypeSpec
from lorebase_inference import ChatResult, InferenceError, InferenceSettings, Usage
from lorebase_scribe.embeddings import PROBE_TEXT, EmbeddingModels
from lorebase_scribe.reconcile import detector as detector_module
from lorebase_scribe.reconcile.detector import Detector, Raised, effective_options
from lorebase_scribe.reconcile.semantic import (
    EmbeddingSemanticStage,
    build_semantic_stage,
    cosine,
)

NOW = datetime(2026, 10, 29, tzinfo=UTC)
ON = ReconcileOptions(semantic={"enabled": True})


class Vectors:
    """An embedding model answering fixed vectors per text ([1, 0] for others)."""

    name = "fake:semantic"
    dims = 2

    def __init__(self, vectors: dict[str, list[float]], *, fail: bool = False) -> None:
        self.vectors = vectors
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if list(texts) != [PROBE_TEXT]:
            if self.fail:
                raise InferenceError("down")
            self.calls.append(list(texts))
        return [self.vectors.get(t, [1.0, 0.0]) for t in texts]


class Judge:
    """A chat model answering one verdict, counting its calls."""

    def __init__(self, answer: Any, *, fail: bool = False, tokens: int = 10) -> None:
        self.answer = answer
        self.fail = fail
        self.tokens = tokens
        self.calls = 0

    async def complete_json(self, *_args: Any, **_kwargs: Any) -> ChatResult:
        self.calls += 1
        if self.fail:
            raise InferenceError("down")
        return ChatResult(data=self.answer, usage=Usage(self.tokens, 0), model="fake:judge")


class Cache:
    def __init__(self) -> None:
        self.stored: dict[bytes, tuple[Any, Usage, str]] = {}

    async def get(self, key: bytes) -> tuple[Any, Usage, str] | None:
        return self.stored.get(key)

    async def put(self, key: bytes, data: Any, usage: Usage, model: str) -> None:
        self.stored[key] = (data, usage, model)


def _same(judge: Judge) -> Judge:
    return judge


def _models(model: Any) -> EmbeddingModels:
    return EmbeddingModels(InferenceSettings(), dims=2, factory=lambda: model)


def _claim(value: str, principal: str) -> Claim:
    entry = ProvenanceEntry(
        id=new_id(IdPrefix.PROVENANCE),
        origin=OperationOrigin(invocation_id="inv_1", operation="update_x", principal=principal),
        actor=principal,
        at="2026-01-01T00:00:00Z",
    )
    return Claim.of_provenance(entry, value)


def _draft(first: str, second: str, attribute_id: str = "att_text") -> CandidateDraft:
    return CandidateDraft(
        type=CandidateType.CONTRADICTION,
        branch="main",
        kind_id="knd_x",
        keys=("K-1",),
        attribute_id=attribute_id,
        claims=(_claim(first, "usr_a"), _claim(second, "usr_b")),
    )


SAME = {"blue door": [1.0, 0.0], "a door painted blue": [0.99, 0.141], "red door": [0.0, 1.0]}


def test_cosine() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 0]) == 0.0


async def test_similarity_and_equivalence_are_signals_and_null_raises_all() -> None:
    model = Vectors(SAME)
    stage = EmbeddingSemanticStage(_models(model))
    close, apart = _draft("blue door", "a door painted blue"), _draft("blue door", "red door")
    kept = await stage.review([close, apart], ON)
    assert [d.fingerprint for d in kept] == [close.fingerprint, apart.fingerprint]
    assert kept[0].signals["similarity"] == pytest.approx(0.99, abs=0.01)
    assert kept[0].signals["equivalent"] is True
    assert kept[1].signals == {"similarity": 0.0}
    assert len(model.calls) == 1  # one call for every text of the batch


async def test_above_the_equivalence_threshold_a_draft_is_dropped() -> None:
    options = ReconcileOptions(semantic={"enabled": True, "equivalence_threshold": 0.95})
    stage = EmbeddingSemanticStage(_models(Vectors(SAME)))
    close, apart = _draft("blue door", "a door painted blue"), _draft("blue door", "red door")
    kept = await stage.review([close, apart], options)
    assert [d.fingerprint for d in kept] == [apart.fingerprint]


async def test_model_failures_give_no_signal() -> None:
    draft = _draft("blue door", "red door")
    failing = EmbeddingSemanticStage(_models(Vectors(SAME, fail=True)))
    assert await failing.review([draft], ON) == [draft]

    def broken() -> Any:
        raise InferenceError("no provider")

    unavailable = EmbeddingSemanticStage(
        EmbeddingModels(InferenceSettings(), dims=2, factory=broken)
    )
    assert await unavailable.review([draft], ON) == [draft]


async def test_the_judge_adds_a_label_through_the_cache_and_its_budget() -> None:
    options = ReconcileOptions(semantic={"enabled": True, "judge": True})
    judge, cache = Judge({"contradicts": True, "score": 0.8}), Cache()
    stage = EmbeddingSemanticStage(
        _models(Vectors(SAME)), chat=lambda: judge, chat_model="fake:judge", cache=cache
    )
    (draft,) = await stage.review([_draft("blue door", "red door")], options)
    assert draft.signals["judge"] == {"label": "contradiction", "score": 0.8, "model": "fake:judge"}
    # The same question again (texts either way round) is answered by the cache.
    (again,) = await stage.review([_draft("red door", "blue door", "att_other")], options)
    assert again.signals["judge"] == draft.signals["judge"]
    assert judge.calls == 1
    assert stage.tokens == 10

    # Past the budget nothing more is asked; a failing or invalid judge gives no label.
    spent = EmbeddingSemanticStage(_models(Vectors(SAME)), chat=lambda: judge, max_tokens=10)
    spent.tokens = 10
    (plain,) = await spent.review([_draft("blue door", "red door")], options)
    assert "judge" not in plain.signals
    for bad in (Judge(None, fail=True), Judge({"contradicts": "yes", "score": 2})):
        stage = EmbeddingSemanticStage(_models(Vectors(SAME)), chat=functools.partial(_same, bad))
        (draft,) = await stage.review([_draft("blue door", "red door")], options)
        assert "judge" not in draft.signals
        assert draft.signals["similarity"] == 0.0


def test_the_stage_is_built_only_when_the_operator_enables_it() -> None:
    models = _models(Vectors({}))
    assert build_semantic_stage(ReconcileOptions(), models) is None
    assert build_semantic_stage(ON, None) is None
    assert isinstance(build_semantic_stage(ON, models), EmbeddingSemanticStage)


# The detector's hand-off -------------------------------------------------------------------------


class Repository:
    """Stands in for `ReconciliationCandidateRepository`, recording the upserts."""

    upserts: ClassVar[list[tuple[CandidateDraft, dict[str, Any]]]] = []

    def __init__(self, _session: Any, _domain: str) -> None:
        pass

    async def upsert(self, draft: CandidateDraft, **kwargs: Any) -> Any:
        Repository.upserts.append((draft, kwargs))
        return type("Outcome", (), {"created": True, "claims_added": 0})()


def _attribute(attribute_id: str, type_: AttributeType, **extra: Any) -> Attribute:
    return Attribute(
        id=new_id(IdPrefix.ATTRIBUTE), api_name=attribute_id[4:], type=TypeSpec.of(type_), **extra
    )


async def test_the_detector_reviews_text_values_and_counts_what_is_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detector_module, "ReconciliationCandidateRepository", Repository)
    Repository.upserts = []
    options = ReconcileOptions(semantic={"enabled": True, "equivalence_threshold": 0.95})
    stage = EmbeddingSemanticStage(_models(Vectors(SAME)))
    detector = Detector(options, principal="svc", clock=lambda: NOW, semantic=stage)
    text = _attribute("att_text", AttributeType.TEXT)
    plain = _attribute("att_name", AttributeType.STRING)
    raised = [
        Raised(_draft("blue door", "a door painted blue"), text),
        Raised(_draft("blue door", "red door"), text),
        Raised(_draft("blue door", "a door painted blue", "att_name"), plain),
    ]
    report = await detector.store(object(), "dom_1", raised)  # type: ignore[arg-type]
    assert report.as_dict() == {"filtered": 1, "raised": 2}
    stored = [d for d, _ in Repository.upserts]
    assert [d.attribute_id for d in stored] == ["att_text", "att_name"]
    assert "similarity" in stored[0].signals
    assert stored[1].signals == {}  # a string Attribute is not reviewed

    class Broken:
        async def review(self, *_args: Any) -> list[CandidateDraft]:
            raise RuntimeError("bug")

    Repository.upserts = []
    detector = Detector(options, principal="svc", clock=lambda: NOW, semantic=Broken())
    report = await detector.store(object(), "dom_1", raised[:1])  # type: ignore[arg-type]
    assert report.as_dict() == {"raised": 1}

    # Off for the Domain: the stage is not asked.
    off = ReconcileOptions(stages=[Stage.IDENTITY, Stage.VALUES, Stage.NORMALIZE])
    detector = Detector(off, principal="svc", clock=lambda: NOW, semantic=Broken())
    assert (await detector.store(object(), "dom_1", raised[:1])).as_dict() == {"raised": 1}  # type: ignore[arg-type]


async def test_vault_attributes_are_stored_without_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detector_module, "ReconciliationCandidateRepository", Repository)
    Repository.upserts = []
    detector = Detector(ReconcileOptions(), principal="svc", clock=lambda: NOW)
    secret = _attribute("att_secret", AttributeType.STRING, vault=True, sensitivity=["pii"])
    plain = _attribute("att_name", AttributeType.STRING)
    raised = [
        Raised(_draft("a", "b", "att_secret"), secret),
        Raised(_draft("a", "b", "att_name"), plain),
        Raised(_draft("a", "b", "att_none")),
    ]
    await detector.store(object(), "dom_1", raised)  # type: ignore[arg-type]
    assert [kw["vault"] for _, kw in Repository.upserts] == [True, False, False]


# Domain overrides --------------------------------------------------------------------------------


def _override(base: ReconcileOptions, **members: Any) -> ReconcileOptions:
    return effective_options(base, {"reconciliation": members})


def test_a_domain_overrides_only_what_it_may() -> None:
    base = ReconcileOptions(stages=[Stage.IDENTITY, Stage.VALUES, Stage.NORMALIZE])
    found = _override(
        base,
        hold_auto_accept=False,
        normalize={"units_file": "/etc/passwd", "numeric_tolerance": 0.1},
        sweep_max_changes_per_job=10**9,
        max_claims_per_candidate=10**6,
        duplicates={"max_pairs_per_kind": 10**9, "trigram_threshold": 0.5},
        stages=["identity", "values", "semantic"],
        semantic={"enabled": True, "judge": True},
        stale={"max_age": "P30D"},
    )
    assert found.hold_auto_accept is False
    assert found.normalize.units_file is None
    assert found.normalize.numeric_tolerance == 0.1
    assert found.sweep_max_changes_per_job == base.sweep_max_changes_per_job
    assert found.max_claims_per_candidate == base.max_claims_per_candidate
    assert found.duplicates.max_pairs_per_kind == base.duplicates.max_pairs_per_kind
    assert found.duplicates.trigram_threshold == 0.5
    assert found.stages == (Stage.IDENTITY, Stage.VALUES)  # may disable, never add
    assert not found.semantic.enabled
    assert not found.semantic.judge
    assert found.stale.max_age == "P30D"


def test_semantic_members_follow_the_operator_and_bad_members_are_dropped_alone() -> None:
    found = _override(ON, semantic={"judge": True, "equivalence_threshold": 0.97})
    assert found.semantic.judge
    assert found.semantic.equivalence_threshold == 0.97
    assert not _override(ON, semantic={"enabled": False}).runs(Stage.SEMANTIC)
    mixed = _override(ON, hold_auto_accept=False, stale={"max_age": "soon"}, merge_check="off")
    assert mixed.hold_auto_accept is False
    assert mixed.stale.max_age is None
    assert _override(ON) is ON
    assert effective_options(ON, {"reconciliation": ["semantic"]}) is ON


# The hold of extraction items ---------------------------------------------------------------------


def test_superseded_values_are_held_too(supply_schema: Schema) -> None:
    from lorebase_scribe.extraction.candidates import (
        Evidence,
        Mention,
        Resolution,
        Resolved,
        ValueCandidate,
    )
    from lorebase_scribe.reconcile.extraction import ExtractionCheck

    kind = supply_schema.kind_by_api_name("Supplier")
    assert kind is not None
    city = kind.attribute_by_api_name("city")
    assert city is not None
    timed = city.model_copy(update={"valid_time": True})
    kind = kind.model_copy(update={"attributes": {**kind.attributes, city.id: timed}})
    schema = supply_schema.model_copy(update={"kinds": {**supply_schema.kinds, kind.id: kind}})
    entry = ProvenanceEntry(
        id=new_id(IdPrefix.PROVENANCE),
        origin=OperationOrigin(invocation_id="inv_1", operation="update_supplier", principal="u"),
        actor="u",
        at="2024-01-01T00:00:00Z",
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2025-01-01T00:00:00Z",
    )
    detector = Detector(ReconcileOptions(), principal="svc", clock=lambda: NOW)
    check = ExtractionCheck(
        detector,
        schema,
        "main",
        {(kind.id, "S-1013", city.id): Claim.of_provenance(entry, "Vienna")},
        now=NOW,
    )
    mention = Mention(
        kind_id=kind.id,
        text="S-1013",
        start=0,
        end=6,
        chunk=0,
        score=1.0,
        tier=0,
        source="key",
        extractor="test",
        document_id="doc_1",
    )
    known = Resolved(mention, Resolution(kind.id, "matched", "S-1013", 1.0, "exact"))
    evidence = Evidence(document_id="doc_1", chunk=0, start=0, end=20, quote="Graz since 2025")
    moved = ValueCandidate(known, city.id, "Graz", evidence, 1.0, 2, "m", "2025-01-01T00:00:00Z")
    current = Claim.of_provenance(entry, "Vienna")
    proposed = Claim.of_proposal_item(
        "prp_1", "itm_1", "Graz", source="document:doc_1", at=NOW, valid_from="2025-01-01T00:00:00Z"
    )
    verdict = detector.verdict(
        [current, proposed], branch="main", kind=kind, key="S-1013", attribute=timed
    )
    assert verdict.draft is not None
    assert verdict.draft.type is CandidateType.SUPERSEDED
    assert check.contests(moved)  # superseded: periods apart, still held
