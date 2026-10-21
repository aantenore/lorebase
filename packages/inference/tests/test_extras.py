"""Tests of the optional local models, skipped unless the extras are installed.

They run the real libraries and download model weights on first use, so they are slow and need
network access once. Point `LOREBASE_TEST_MODEL_CACHE` at a directory to reuse downloads and
`LOREBASE_TEST_GLINER_MODEL` / `LOREBASE_TEST_GLIREL_MODEL` at smaller checkpoints to speed
them up. Install the extras with `uv sync --all-packages --extra fastembed --extra
local-extractors` (or `pip install 'lorebase-inference[fastembed,local-extractors]'`).
"""

from __future__ import annotations

import importlib.util
import math
import os

import pytest

from lorebase_inference import (
    InferenceSettings,
    Label,
    RelationLabel,
    create_embedding_model,
    create_relation_extractor,
    create_span_extractor,
)

CACHE = os.environ.get("LOREBASE_TEST_MODEL_CACHE")


def installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def settings(**section: object) -> InferenceSettings:
    return InferenceSettings.model_validate(section)


@pytest.mark.skipif(not installed("fastembed"), reason="extra 'fastembed' is not installed")
async def test_fastembed_embeddings_on_cpu() -> None:
    model = create_embedding_model(
        settings(embeddings={"provider": "fastembed", "batch_size": 2, "cache_dir": CACHE})
    )

    vectors = await model.embed(
        ["The cat sat on the mat.", "A kitten rests on a rug.", "Quarterly revenue rose 4%."]
    )

    assert (model.name, model.dims) == ("BAAI/bge-small-en-v1.5", 384)
    assert [len(v) for v in vectors] == [384, 384, 384]
    assert all(math.isclose(math.hypot(*v), 1.0, abs_tol=1e-3) for v in vectors)

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2])
    again = await model.embed(["The cat sat on the mat."])
    assert cosine(vectors[0], again[0]) > 0.999


GLINER_MODEL = os.environ.get("LOREBASE_TEST_GLINER_MODEL", "urchade/gliner_multi-v2.1")
GLIREL_MODEL = os.environ.get("LOREBASE_TEST_GLIREL_MODEL", "jackboyla/glirel-large-v0")
TEXT = "Satya Nadella is the chief executive of Microsoft, which is based in Redmond."


@pytest.mark.skipif(not installed("gliner"), reason="extra 'local-extractors' is not installed")
async def test_gliner_extracts_spans_with_offsets() -> None:
    extractor = create_span_extractor(
        settings(
            local_extractors={
                "provider": "gliner",
                "gliner": {"model": GLINER_MODEL, "threshold": 0.4, "cache_dir": CACHE},
            }
        )
    )

    spans = await extractor.extract(TEXT, [Label("person"), Label("organization"), Label("city")])

    found = {(s.label, s.text) for s in spans}
    assert ("person", "Satya Nadella") in found
    assert ("organization", "Microsoft") in found
    assert ("city", "Redmond") in found
    assert all(TEXT[s.start : s.end] == s.text and 0 < s.score <= 1 for s in spans)
    assert spans == sorted(spans, key=lambda s: s.start)


@pytest.mark.skipif(
    not (installed("gliner") and installed("glirel")),
    reason="extra 'local-extractors' is not installed",
)
async def test_glirel_extracts_relations_between_given_spans() -> None:
    config = {
        "provider": "gliner",
        "relation_provider": "glirel",
        "gliner": {"model": GLINER_MODEL, "threshold": 0.4, "cache_dir": CACHE},
        "glirel": {"model": GLIREL_MODEL, "threshold": 0.1, "cache_dir": CACHE},
    }
    configured = settings(local_extractors=config)
    spans = await create_span_extractor(configured).extract(
        TEXT, [Label("person"), Label("organization"), Label("city")]
    )

    relations = await create_relation_extractor(configured).extract(
        TEXT,
        spans,
        [
            RelationLabel(
                "chief executive of", head_labels=("person",), tail_labels=("organization",)
            ),
            RelationLabel("headquartered in", head_labels=("organization",), tail_labels=("city",)),
        ],
    )

    triples = {(r.head.text, r.label, r.tail.text) for r in relations}
    assert ("Satya Nadella", "chief executive of", "Microsoft") in triples
    assert ("Microsoft", "headquartered in", "Redmond") in triples
    assert all(r.head in spans and r.tail in spans for r in relations)
