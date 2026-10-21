"""Tier-1 relation candidates between spans: a local model, or a deterministic fallback.

`LocalRelationExtractor` runs a zero-shot relation model behind a backend callable supplied by
the registry; this module tokenizes the text, maps spans to token ranges and maps predictions
back to spans.

`CooccurrenceRelationExtractor` needs no model and is the documented fallback when the
relation model cannot be installed (it is an optional extra, and its dependencies are heavy).
It pairs spans that share a sentence and emits every relation label whose head and tail
constraints the pair satisfies. It knows nothing about meaning: the score is a proximity value
`1 / (1 + words between the spans)`, not a probability, and the output is a superset of what a
model would propose. Downstream validation, evidence checks and review decide. Sentences end at
`.`, `!` or `?` followed by whitespace, and at blank lines; abbreviations therefore split
sentences, which only loses candidates.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from lorebase_inference.interfaces import RelationLabel, Span, SpanRelation
from lorebase_inference.local_spans import split_windows
from lorebase_inference.settings import CooccurrenceSettings, ExtractorSettings

RelationBackend = Callable[[list[str], list[list[Any]], list[str], float], list[Mapping[str, Any]]]
"""`(tokens, entities, label names, threshold)` to relations.

Entities are `[first token, last token (inclusive), span label, text]`. Each relation holds
`head_pos` and `tail_pos` (`[first token, end token (exclusive)]`), `label` and `score`.
"""

_TOKEN = re.compile(r"\w+(?:['\u2019-]\w+)*|[^\w\s]")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n\s*\n")


def sentence_bounds(text: str) -> list[tuple[int, int]]:
    """`(start, end)` offsets of the sentences of `text`, whitespace excluded."""
    bounds: list[tuple[int, int]] = []
    start = 0
    for match in [*_SENTENCE_BREAK.finditer(text), None]:
        end = match.start() if match else len(text)
        if text[start:end].strip():
            bounds.append((start, end))
        start = match.end() if match else len(text)
    return bounds


class CooccurrenceRelationExtractor:
    """`RelationExtractor` pairing spans of the same sentence, deterministically."""

    def __init__(self, settings: CooccurrenceSettings | None = None) -> None:
        self._limit = (settings or CooccurrenceSettings()).max_pairs_per_sentence

    async def extract(
        self, text: str, spans: Sequence[Span], labels: Sequence[RelationLabel]
    ) -> list[SpanRelation]:
        """Candidate relations, ordered by head start, tail start and label."""
        relations: list[SpanRelation] = []
        ordered = sorted(spans, key=lambda s: (s.start, s.end, s.label))
        for start, end in sentence_bounds(text):
            inside = [s for s in ordered if s.start >= start and s.end <= end]
            pairs = [
                (a, b) for i, a in enumerate(inside) for b in inside[i + 1 :] if b.start >= a.end
            ]
            pairs.sort(key=lambda p: (_gap_words(text, *p), p[0].start, p[1].start))
            for head, tail in pairs[: self._limit]:
                score = 1 / (1 + _gap_words(text, head, tail))
                for label in labels:
                    if label.allows(head, tail):
                        relations.append(SpanRelation(label.name, head, tail, score))
                    elif label.allows(tail, head):
                        relations.append(SpanRelation(label.name, tail, head, score))
        relations.sort(key=lambda r: (r.head.start, r.tail.start, r.label))
        return relations


def _gap_words(text: str, head: Span, tail: Span) -> int:
    return len(_TOKEN.findall(text[head.end : tail.start]))


class LocalRelationExtractor:
    """`RelationExtractor` running a local zero-shot relation model through a backend."""

    def __init__(
        self, settings: ExtractorSettings, backend_factory: Callable[[], RelationBackend]
    ) -> None:
        self._settings = settings
        self._factory = backend_factory
        self._backend: RelationBackend | None = None
        self._lock = threading.Lock()

    async def extract(
        self, text: str, spans: Sequence[Span], labels: Sequence[RelationLabel]
    ) -> list[SpanRelation]:
        """Candidate relations between `spans`, ordered by head start, tail start and label."""
        if not labels or len(spans) < 2:
            return []
        return await asyncio.to_thread(self._extract, text, list(spans), list(labels))

    def _extract(
        self, text: str, spans: list[Span], labels: list[RelationLabel]
    ) -> list[SpanRelation]:
        spans = sorted(spans, key=lambda s: (s.start, s.end, s.label))
        by_name = {label.name.casefold(): label for label in labels}
        names = [label.name for label in labels]
        found: dict[tuple[int, int, str], SpanRelation] = {}
        with self._lock:
            if self._backend is None:
                self._backend = self._factory()
            backend = self._backend
            for offset, window in split_windows(text, self._settings.window_chars):
                local = [s for s in spans if s.start >= offset and s.end <= offset + len(window)]
                if len(local) < 2:
                    continue
                tokens = [(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(window)]
                placed = _place(tokens, local, offset)
                if len(placed) < 2:
                    continue
                entities = [[first, last, span.label, span.text] for span, first, last in placed]
                lookup = {(first, last): span for span, first, last in placed}
                raw = backend([t[0] for t in tokens], entities, names, self._settings.threshold)
                for item in raw:
                    label = by_name.get(str(item.get("label", "")).casefold())
                    head = _find(lookup, item.get("head_pos"))
                    tail = _find(lookup, item.get("tail_pos"))
                    if label is None or head is None or tail is None or head == tail:
                        continue
                    if not label.allows(head, tail):
                        continue
                    key = (head.start, tail.start, label.name)
                    score = float(item.get("score", 1.0))
                    if key not in found or found[key].score < score:
                        found[key] = SpanRelation(label.name, head, tail, score)
        return sorted(found.values(), key=lambda r: (r.head.start, r.tail.start, r.label))


def _place(
    tokens: list[tuple[str, int, int]], spans: list[Span], offset: int
) -> list[tuple[Span, int, int]]:
    """Spans of the window with the inclusive `(first, last)` token indices covering them."""
    placed: list[tuple[Span, int, int]] = []
    for span in spans:
        start, end = span.start - offset, span.end - offset
        covered = [i for i, (_t, s, e) in enumerate(tokens) if e > start and s < end]
        if covered:
            placed.append((span, covered[0], covered[-1]))
    return placed


def _find(lookup: dict[tuple[int, int], Span], position: Any) -> Span | None:
    if isinstance(position, Sequence) and len(position) == 2:
        return lookup.get((int(position[0]), int(position[1]) - 1))
    return None
