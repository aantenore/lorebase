"""Link candidates between resolved mentions.

- Co-occurrence (tier 0): two Records named in one sentence (a table row is a sentence) are
  linked by every Relation whose endpoint Kinds they have; confidence decreases with the words
  between them and never exceeds `confidence.cooccurrence_max`.
- Headings (tier 0): the Record a heading names is linked to the Records named in its section
  (up to the next heading of the same or a higher level), at `confidence.heading`.
- Model (tier 1): a `RelationExtractor` of the inference package (GLiREL), with Relation api
  names as labels constrained to their endpoint Kinds.

Co-occurrence knows nothing about meaning; its candidates are a superset that evidence, review
and the confidence threshold sort out.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

from lorebase_inference.interfaces import RelationExtractor, RelationLabel, Span
from lorebase_scribe.extraction.candidates import Evidence, LinkCandidate, Resolved
from lorebase_scribe.extraction.ontology import Ontology
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.text import heading_sections

_WORD = re.compile(r"\w+")
COOCCURRENCE = "cooccurrence"
HEADING = "heading"


def _pairs(
    ontology: Ontology, first: Resolved, second: Resolved
) -> list[tuple[str, Resolved, Resolved]]:
    """`(relation id, from, to)` for every Relation the two mentions can stand in."""
    if (first.kind_id, first.key) == (second.kind_id, second.key):
        return []
    found: list[tuple[str, Resolved, Resolved]] = []
    for relation in ontology.relations.values():
        if relation.from_kind == first.kind_id and relation.to_kind == second.kind_id:
            found.append((relation.id, first, second))
        elif relation.from_kind == second.kind_id and relation.to_kind == first.kind_id:
            found.append((relation.id, second, first))
    return found


def cooccurrence_links(
    ontology: Ontology,
    resolved: Sequence[Resolved],
    text: str,
    sentences: Sequence[tuple[int, int]],
    *,
    document_id: str,
    settings: ExtractionSettings,
) -> list[LinkCandidate]:
    by_sentence: dict[tuple[int, int], list[Resolved]] = defaultdict(list)
    for item in resolved:
        for bound in sentences:
            if bound[0] <= item.mention.start < bound[1]:
                by_sentence[bound].append(item)
                break
    found: list[LinkCandidate] = []
    ceiling = settings.confidence.cooccurrence_max
    for (start, end), group in sorted(by_sentence.items()):
        ordered = sorted(group, key=lambda r: r.mention.start)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                gap = len(_WORD.findall(text, first.mention.end, max(second.mention.start, 0)))
                proximity = 1 / (1 + gap / 10)
                confidence = ceiling * min(first.confidence, second.confidence) * proximity
                for relation_id, source, target in _pairs(ontology, first, second):
                    found.append(
                        LinkCandidate(
                            relation_id=relation_id,
                            source=source,
                            target=target,
                            evidence=Evidence(
                                document_id, first.mention.chunk, start, end, text[start:end]
                            ),
                            confidence=confidence,
                            tier=0,
                            model=COOCCURRENCE,
                        )
                    )
    return found


def heading_links(
    ontology: Ontology,
    resolved: Sequence[Resolved],
    text: str,
    sentences: Sequence[tuple[int, int]],
    *,
    document_id: str,
    settings: ExtractionSettings,
) -> list[LinkCandidate]:
    found: list[LinkCandidate] = []
    for section in heading_sections(text):
        start, line_end, end = section.start, section.heading_end, section.end
        heads = [r for r in resolved if start <= r.mention.start < line_end]
        body = [r for r in resolved if line_end <= r.mention.start < end]
        for head in heads:
            for other in body:
                sentence = next(
                    ((s, e) for s, e in sentences if s <= other.mention.start < e),
                    (other.mention.start, other.mention.end),
                )
                confidence = settings.confidence.heading * min(head.confidence, other.confidence)
                for relation_id, source, target in _pairs(ontology, head, other):
                    found.append(
                        LinkCandidate(
                            relation_id=relation_id,
                            source=source,
                            target=target,
                            evidence=Evidence(
                                document_id,
                                other.mention.chunk,
                                sentence[0],
                                sentence[1],
                                text[sentence[0] : sentence[1]],
                            ),
                            confidence=confidence,
                            tier=0,
                            model=HEADING,
                        )
                    )
    return found


async def model_links(
    extractor: RelationExtractor,
    ontology: Ontology,
    resolved: Sequence[Resolved],
    chunk_text: str,
    chunk_start: int,
    *,
    document_id: str,
    model: str,
) -> list[LinkCandidate]:
    """Relations a local model finds between the mentions of one chunk."""
    inside = [
        r
        for r in resolved
        if chunk_start <= r.mention.start and r.mention.end <= chunk_start + len(chunk_text)
    ]
    kinds = {r.kind_id for r in inside}
    relations = ontology.relations_between(kinds)
    if len(inside) < 2 or not relations:
        return []
    by_offsets: dict[tuple[int, int], Resolved] = {}
    spans: list[Span] = []
    for item in inside:
        start, end = item.mention.start - chunk_start, item.mention.end - chunk_start
        by_offsets[(start, end)] = item
        spans.append(Span(ontology.kinds[item.kind_id].api_name, chunk_text[start:end], start, end))
    labels = [
        RelationLabel(
            name=r.api_name,
            description=r.description,
            head_labels=(ontology.kinds[r.from_kind].api_name,),
            tail_labels=(ontology.kinds[r.to_kind].api_name,),
        )
        for r in relations
    ]
    by_name = {r.api_name: r for r in relations}
    found: list[LinkCandidate] = []
    for relation in await extractor.extract(chunk_text, spans, labels):
        spec = by_name.get(relation.label)
        source = by_offsets.get((relation.head.start, relation.head.end))
        target = by_offsets.get((relation.tail.start, relation.tail.end))
        if spec is None or source is None or target is None:
            continue
        if (source.kind_id, target.kind_id) != (spec.from_kind, spec.to_kind):
            continue
        lo = min(relation.head.start, relation.tail.start)
        hi = max(relation.head.end, relation.tail.end)
        found.append(
            LinkCandidate(
                relation_id=spec.id,
                source=source,
                target=target,
                evidence=Evidence(
                    document_id,
                    source.mention.chunk,
                    chunk_start + lo,
                    chunk_start + hi,
                    chunk_text[lo:hi],
                ),
                confidence=relation.score * min(source.confidence, target.confidence),
                tier=1,
                model=model,
            )
        )
    return found
