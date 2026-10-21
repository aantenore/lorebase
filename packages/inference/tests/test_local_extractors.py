from __future__ import annotations

import re

from lorebase_inference import (
    Label,
    RelationExtractor,
    RelationLabel,
    Span,
    SpanExtractor,
    SpanRelation,
)
from lorebase_inference.local_relations import (
    CooccurrenceRelationExtractor,
    LocalRelationExtractor,
    sentence_bounds,
)
from lorebase_inference.local_spans import LocalSpanExtractor, split_windows
from lorebase_inference.settings import CooccurrenceSettings, ExtractorSettings

TEXT = "Acme Corp acquired Globex in 2019. Globex is based in Berlin. Nobody else is here."


def settings(**overrides) -> ExtractorSettings:
    return ExtractorSettings(model="m", threshold=0.4, **overrides)


def span(text: str, label: str, source: str = TEXT, nth: int = 0) -> Span:
    starts = [m.start() for m in re.finditer(re.escape(text), source)]
    start = starts[nth]
    return Span(label, text, start, start + len(text))


# ---- windows ---------------------------------------------------------------------------------


def test_short_text_is_one_window() -> None:
    assert list(split_windows("hello world", 100)) == [(0, "hello world")]


def test_windows_cover_the_text_and_cut_at_sentence_ends_or_spaces() -> None:
    text = "Alpha beta gamma delta epsilon. Zeta eta theta iota kappa lambda mu nu xi."
    windows = list(split_windows(text, 40))
    assert "".join(w for _o, w in windows) == text
    assert all(len(w) <= 40 for _o, w in windows)
    assert all(text[o : o + len(w)] == w for o, w in windows)
    assert windows[0][1] == "Alpha beta gamma delta epsilon. "
    assert windows[1][1].endswith(" ")

    unbroken = "x" * 25
    assert [w for _o, w in split_windows(unbroken, 10)] == ["x" * 10, "x" * 10, "x" * 5]


def test_blank_windows_are_skipped() -> None:
    assert list(split_windows("   ", 10)) == []


# ---- span extractor with a stand-in backend ---------------------------------------------------


def entity_backend(calls: list):
    def build():
        def predict(text, labels, threshold):
            calls.append((text, list(labels), threshold))
            found = []
            for label, word in (("organization", "Acme Corp"), ("organization", "Globex")):
                for m in re.finditer(word, text):
                    found.append({"start": m.start(), "end": m.end(), "label": label, "score": 0.9})
            found.append({"start": 0, "end": 4, "label": "unrequested", "score": 0.9})
            return found

        return predict

    return build


async def test_span_extractor_maps_labels_offsets_and_threshold() -> None:
    calls: list = []
    extractor = LocalSpanExtractor(settings(), entity_backend(calls))

    spans = await extractor.extract(TEXT, [Label("Organization"), Label("Place")])

    assert isinstance(extractor, SpanExtractor)
    assert [(s.label, s.text) for s in spans] == [
        ("Organization", "Acme Corp"),
        ("Organization", "Globex"),
        ("Organization", "Globex"),
    ]
    assert all(TEXT[s.start : s.end] == s.text for s in spans)
    assert calls == [(TEXT, ["Organization", "Place"], 0.4)], "labels are passed per call"
    assert spans == sorted(spans, key=lambda s: s.start)


async def test_span_extractor_shifts_offsets_of_later_windows() -> None:
    calls: list = []
    extractor = LocalSpanExtractor(settings(window_chars=100), entity_backend(calls))
    text = "Filler words. " * 20 + "Then Globex appeared."

    spans = await extractor.extract(text, [Label("organization")])

    assert len(calls) > 1
    assert [s.text for s in spans] == ["Globex"]
    assert text[spans[0].start : spans[0].end] == "Globex"


async def test_span_extractor_with_nothing_to_do_never_loads_the_model() -> None:
    loaded = []
    extractor = LocalSpanExtractor(settings(), lambda: loaded.append(1) or (lambda *a: []))
    assert await extractor.extract("", [Label("x")]) == []
    assert await extractor.extract("text", []) == []
    assert loaded == []


# ---- cooccurrence fallback --------------------------------------------------------------------


def test_sentence_bounds() -> None:
    assert [TEXT[s:e] for s, e in sentence_bounds(TEXT)] == [
        "Acme Corp acquired Globex in 2019.",
        "Globex is based in Berlin.",
        "Nobody else is here.",
    ]
    assert sentence_bounds("   ") == []


async def test_cooccurrence_pairs_spans_of_one_sentence_with_allowed_labels() -> None:
    acme = span("Acme Corp", "Organization")
    globex_1 = span("Globex", "Organization")
    globex_2 = span("Globex", "Organization", nth=1)
    berlin = span("Berlin", "Place")
    labels = [
        RelationLabel("acquired", head_labels=("Organization",), tail_labels=("Organization",)),
        RelationLabel("based_in", head_labels=("Organization",), tail_labels=("Place",)),
    ]
    extractor = CooccurrenceRelationExtractor()

    relations = await extractor.extract(TEXT, [berlin, globex_2, globex_1, acme], labels)

    assert isinstance(extractor, RelationExtractor)
    assert [(r.label, r.head.text, r.tail.text) for r in relations] == [
        ("acquired", "Acme Corp", "Globex"),
        ("based_in", "Globex", "Berlin"),
    ]
    assert relations[0].head == acme
    assert relations[0].tail == globex_1
    assert relations[0].score == 1 / 2, "one word ('acquired') between the spans"
    assert relations[1].score == 1 / 4, "'is based in' lies between"
    assert all(isinstance(r, SpanRelation) for r in relations)


async def test_cooccurrence_orients_a_pair_by_the_label_constraints() -> None:
    globex = span("Globex", "Organization", nth=1)
    berlin = span("Berlin", "Place")
    reversed_label = RelationLabel("hosts", head_labels=("Place",), tail_labels=("Organization",))

    relations = await CooccurrenceRelationExtractor().extract(
        TEXT, [globex, berlin], [reversed_label]
    )

    assert [(r.head.text, r.tail.text) for r in relations] == [("Berlin", "Globex")]


async def test_cooccurrence_ignores_spans_in_different_sentences_and_overlaps() -> None:
    acme = span("Acme Corp", "Organization")
    berlin = span("Berlin", "Place")
    inner = Span("Alias", "Acme", acme.start, acme.start + 4)
    extractor = CooccurrenceRelationExtractor()

    assert await extractor.extract(TEXT, [acme, berlin], [RelationLabel("related")]) == []
    assert await extractor.extract(TEXT, [acme, inner], [RelationLabel("related")]) == []


async def test_cooccurrence_is_deterministic_and_bounded_per_sentence() -> None:
    text = "A1 B2 C3 D4 E5."
    spans = [span(t, "T", text) for t in ("A1", "B2", "C3", "D4", "E5")]
    labels = [RelationLabel("r")]

    first = await CooccurrenceRelationExtractor().extract(text, spans, labels)
    again = await CooccurrenceRelationExtractor().extract(text, list(reversed(spans)), labels)
    limited = await CooccurrenceRelationExtractor(
        CooccurrenceSettings(max_pairs_per_sentence=4)
    ).extract(text, spans, labels)

    assert first == again
    assert len(first) == 10
    assert len(limited) == 4
    assert {(r.head.text, r.tail.text) for r in limited} == {
        ("A1", "B2"),
        ("B2", "C3"),
        ("C3", "D4"),
        ("D4", "E5"),
    }, "the nearest pairs are kept"


# ---- model-backed relation extractor with a stand-in backend ----------------------------------


async def test_relation_extractor_maps_token_positions_back_to_spans() -> None:
    seen: dict = {}

    def build():
        def predict(tokens, entities, labels, threshold):
            seen.update(tokens=tokens, entities=entities, labels=labels, threshold=threshold)
            heads = {e[3]: (e[0], e[1] + 1) for e in entities}  # end is exclusive
            return [
                {
                    "head_pos": list(heads["Acme Corp"]),
                    "tail_pos": list(heads["Globex"]),
                    "label": "Acquired",
                    "score": 0.8,
                },
                {  # violates the head label constraint of "based_in": dropped
                    "head_pos": list(heads["Berlin"]),
                    "tail_pos": list(heads["Globex"]),
                    "label": "based_in",
                    "score": 0.9,
                },
                {"head_pos": [90, 91], "tail_pos": [0, 1], "label": "acquired", "score": 1.0},
                {"head_pos": [0, 1], "tail_pos": [0, 1], "label": "acquired", "score": 1.0},
            ]

        return predict

    sentence = "Acme Corp acquired Globex. It sits in Berlin."
    acme = span("Acme Corp", "Organization", sentence)
    globex = span("Globex", "Organization", sentence)
    berlin = span("Berlin", "Place", sentence)
    labels = [
        RelationLabel("acquired", head_labels=("Organization",)),
        RelationLabel("based_in", head_labels=("Organization",)),
    ]
    extractor = LocalRelationExtractor(settings(), build)

    relations = await extractor.extract(sentence, [berlin, globex, acme], labels)

    assert isinstance(extractor, RelationExtractor)
    assert seen["tokens"][:4] == ["Acme", "Corp", "acquired", "Globex"]
    assert [e[:3] for e in seen["entities"]] == [
        [0, 1, "Organization"],
        [3, 3, "Organization"],
        [8, 8, "Place"],
    ]
    assert seen["labels"] == ["acquired", "based_in"]
    assert seen["threshold"] == 0.4
    assert [(r.label, r.head, r.tail, r.score) for r in relations] == [
        ("acquired", acme, globex, 0.8)
    ]


async def test_relation_extractor_needs_two_spans_and_a_label() -> None:
    loaded = []
    extractor = LocalRelationExtractor(settings(), lambda: loaded.append(1) or (lambda *a: []))
    one = span("Globex", "Organization")
    assert await extractor.extract(TEXT, [one], [RelationLabel("r")]) == []
    assert await extractor.extract(TEXT, [one, span("Berlin", "Place")], []) == []
    assert loaded == []
