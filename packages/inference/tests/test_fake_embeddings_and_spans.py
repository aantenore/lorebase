from __future__ import annotations

import hashlib
import math

import pytest

from lorebase_inference.fake import FakeEmbeddingModel, FakeSpanExtractor, embed_text
from lorebase_inference.interfaces import EmbeddingModel, Label, Span, SpanExtractor
from lorebase_inference.settings import EmbeddingSettings


def embedder(dims: int = 384) -> FakeEmbeddingModel:
    return FakeEmbeddingModel(EmbeddingSettings(provider="fake", dims=dims))


@pytest.mark.parametrize("dims", [1, 7, 8, 384, 1024])
async def test_embeddings_have_configured_dimension_and_unit_norm(dims: int) -> None:
    vectors = await embedder(dims).embed(["alpha", "beta", ""])
    assert [len(v) for v in vectors] == [dims] * 3
    for vector in vectors:
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, abs_tol=1e-9)


async def test_embeddings_are_deterministic_and_order_preserving() -> None:
    model = embedder()
    first = await model.embed(["alpha", "beta"])
    second = await model.embed(["beta", "alpha"])
    assert first[0] == second[1]
    assert first[1] == second[0]
    assert first == await embedder().embed(["alpha", "beta"])


async def test_different_texts_give_different_vectors() -> None:
    vectors = await embedder().embed([f"text {i}" for i in range(50)])
    assert len({tuple(v) for v in vectors}) == 50


def test_vector_is_derived_from_sha256_of_the_text() -> None:
    seed = hashlib.sha256(b"alpha").digest()
    block = hashlib.sha256(seed + (0).to_bytes(4, "big")).digest()
    raw = [int.from_bytes(block[i : i + 4], "big") / 2**31 - 1.0 for i in range(0, 32, 4)]
    norm = math.sqrt(sum(x * x for x in raw))
    expected = [x / norm for x in raw]
    assert embed_text("alpha", 8) == pytest.approx(expected)
    assert embed_text("alpha", 8) != embed_text("alphb", 8)


def test_embedding_model_exposes_name_and_dims() -> None:
    model = embedder(12)
    assert model.dims == 12
    assert model.name == "fake:BAAI/bge-small-en-v1.5"
    assert isinstance(model, EmbeddingModel)


async def test_spans_match_dictionary_terms_with_offsets() -> None:
    text = "Acme Corp ships to Berlin. ACME CORP, and acme corp again."
    extractor = FakeSpanExtractor({"Supplier": ["Acme Corp"], "City": ["Berlin"]})
    spans = await extractor.extract(text, [Label("Supplier"), Label("City")])
    assert [(s.label, s.start) for s in spans] == [
        ("Supplier", 0),
        ("City", 19),
        ("Supplier", 27),
        ("Supplier", 42),
    ]
    for span in spans:
        assert text[span.start : span.end] == span.text
    assert spans[2].text == "ACME CORP"


async def test_spans_use_label_examples_and_respect_word_boundaries() -> None:
    spans = await FakeSpanExtractor().extract(
        "The part bolt-12 and bolts, not boltx.", [Label("Part", examples=("bolt-12", "bolt"))]
    )
    assert [s.text for s in spans] == ["bolt-12"]


async def test_overlaps_prefer_the_earliest_then_longest_match() -> None:
    extractor = FakeSpanExtractor({"Org": ["Acme"], "Company": ["Acme Corp"]})
    spans = await extractor.extract("Acme Corp", [Label("Org"), Label("Company")])
    assert spans == [Span("Company", "Acme Corp", 0, 9)]


async def test_no_dictionary_and_no_examples_means_no_spans() -> None:
    assert await FakeSpanExtractor().extract("anything", [Label("Thing")]) == []


def test_span_extractor_satisfies_the_protocol() -> None:
    assert isinstance(FakeSpanExtractor(), SpanExtractor)


def test_span_rejects_inverted_offsets() -> None:
    with pytest.raises(ValueError, match="offsets"):
        Span("x", "t", 5, 2)
