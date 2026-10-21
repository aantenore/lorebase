"""Mentions and their resolution against existing Records (design 9.2 steps 4 and 9).

Tier 0 finds mentions without a model: keys by the pattern constraint of a Kind's key
Attribute, and titles of existing Records by dictionary matching (whole words, case folded).
Tier 1 spans and tier 2 references arrive as mentions too.

Resolution, in order: exact key; exact title; title equal after folding case and punctuation;
trigram similarity through the store (`lorebase_store.reads.similar_titles`). A mention is
`matched` when its best candidate scores at least `match_threshold` and no other candidate is
within `ambiguity_margin`, `ambiguous` when several are, `new` when it is a key of a Kind that
allows new Records and no Record has it, `unresolved` otherwise. An ambiguous mention whose
sentence also names one of its candidates by key is resolved to that candidate.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from lorebase_core.types import normalize_key
from lorebase_scribe.extraction.candidates import (
    MatchCandidate,
    MatchStatus,
    Mention,
    Resolution,
    Resolved,
)
from lorebase_scribe.extraction.ontology import Ontology
from lorebase_scribe.extraction.settings import ExtractionSettings

_PUNCTUATION = re.compile(r"[^\w\s]")


def normalize_title(text: str) -> str:
    """Case folded, accents and punctuation removed, whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_PUNCTUATION.sub(" ", stripped.casefold()).split())


class RecordLookup(Protocol):
    """Reads entity resolution needs from the target branch."""

    async def titles_of_keys(self, kind_id: str, keys: Sequence[str]) -> dict[str, str | None]:
        """Title of each key that exists."""
        ...

    async def similar(
        self, text: str, kind_ids: Sequence[str], *, threshold: float, limit: int
    ) -> list[MatchCandidate]:
        """Records whose title is similar to `text` (score is the trigram similarity, or 1 for
        a case-insensitive equal title)."""
        ...


@dataclass(frozen=True)
class TitleEntry:
    kind_id: str
    key: str
    title: str


class Dictionary:
    """Titles of existing Records for tier 0 matching."""

    def __init__(self, entries: Iterable[TitleEntry], *, min_chars: int) -> None:
        self._by_title: dict[str, list[TitleEntry]] = defaultdict(list)
        by_kind: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            if len(entry.title.strip()) < min_chars:
                continue
            self._by_title[normalize_title(entry.title)].append(entry)
            by_kind[entry.kind_id].add(entry.title.strip())
        self._patterns = {
            kind_id: re.compile(
                r"(?<!\w)(?:"
                + "|".join(re.escape(t) for t in sorted(titles, key=len, reverse=True))
                + r")(?!\w)",
                re.IGNORECASE,
            )
            for kind_id, titles in by_kind.items()
        }

    def entries(self, text: str) -> list[TitleEntry]:
        return list(self._by_title.get(normalize_title(text), ()))

    def find(self, text: str) -> list[tuple[str, int, int]]:
        """`(kind id, start, end)` of every title occurrence in `text`."""
        found: list[tuple[str, int, int]] = []
        for kind_id, pattern in self._patterns.items():
            found.extend((kind_id, m.start(), m.end()) for m in pattern.finditer(text))
        return found


def tier0_mentions(
    ontology: Ontology,
    dictionary: Dictionary,
    text: str,
    *,
    offset: int,
    chunk: int,
    document_id: str,
    settings: ExtractionSettings,
) -> list[Mention]:
    """Key and title mentions in `text` (a chunk starting at `offset` in the document)."""
    mentions: list[Mention] = []
    for view in ontology.kinds.values():
        if view.key_pattern is None:
            continue
        for match in view.key_pattern.finditer(text):
            mentions.append(
                Mention(
                    kind_id=view.id,
                    text=match.group(),
                    start=offset + match.start(),
                    end=offset + match.end(),
                    chunk=chunk,
                    score=settings.confidence.key,
                    tier=0,
                    source="key",
                    extractor="key_pattern",
                    document_id=document_id,
                )
            )
    for kind_id, start, end in dictionary.find(text):
        mentions.append(
            Mention(
                kind_id=kind_id,
                text=text[start:end],
                start=offset + start,
                end=offset + end,
                chunk=chunk,
                score=1.0,
                tier=0,
                source="title",
                extractor="dictionary",
                document_id=document_id,
            )
        )
    return mentions


_PRIORITY = {"key": 0, "title": 1, "span": 2, "model": 3}


def non_overlapping(mentions: Iterable[Mention]) -> list[Mention]:
    """Mentions without overlaps: keys first, then titles, then model spans; longer first."""
    chosen: list[Mention] = []
    for mention in sorted(
        mentions, key=lambda m: (_PRIORITY[m.source], -(m.end - m.start), m.start, m.kind_id)
    ):
        if all(mention.end <= c.start or mention.start >= c.end for c in chosen):
            chosen.append(mention)
    return sorted(chosen, key=lambda m: (m.start, m.end))


class Resolver:
    """Resolves mentions with a per-job cache."""

    def __init__(
        self,
        ontology: Ontology,
        settings: ExtractionSettings,
        lookup: RecordLookup,
        dictionary: Dictionary,
    ) -> None:
        self._ontology = ontology
        self._settings = settings
        self._lookup = lookup
        self._dictionary = dictionary
        self._cache: dict[tuple[str, str, str], Resolution] = {}

    async def resolve(self, mention: Mention) -> Resolved:
        cache_key = (mention.kind_id, mention.source, mention.text.strip().casefold())
        resolution = self._cache.get(cache_key)
        if resolution is None:
            resolution = await self._resolve(mention)
            self._cache[cache_key] = resolution
        return Resolved(mention, resolution)

    async def _by_key(self, kind_id: str, text: str) -> Resolution | None:
        try:
            key = normalize_key(text)
        except ValueError:
            return None
        found = await self._lookup.titles_of_keys(kind_id, [key])
        if key in found:
            candidate = MatchCandidate(kind_id, key, found[key], 1.0, "key")
            return Resolution(kind_id, "matched", key, 1.0, "key", (candidate,))
        return None

    async def _resolve(self, mention: Mention) -> Resolution:
        kind_id = mention.kind_id
        confidence = self._settings.confidence
        if mention.source in ("key", "model"):
            found = await self._by_key(kind_id, mention.text)
            if found is not None:
                return found
            if mention.source == "key":
                if self._ontology.allows_new(kind_id):
                    key = normalize_key(mention.text)
                    return Resolution(kind_id, "new", key, 1.0, "key")
                return Resolution(kind_id, "unresolved", None, 0.0, "key")
        candidates: dict[str, MatchCandidate] = {}
        folded = mention.text.strip().casefold()
        for entry in self._dictionary.entries(mention.text):
            if entry.kind_id != kind_id:
                continue
            exact = entry.title.strip().casefold() == folded
            score = confidence.title if exact else confidence.normalized_title
            method = "title" if exact else "normalized_title"
            candidates[entry.key] = MatchCandidate(kind_id, entry.key, entry.title, score, method)
        matching = self._settings.matching
        for hit in await self._lookup.similar(
            mention.text,
            [kind_id],
            threshold=matching.trigram_threshold,
            limit=matching.candidates,
        ):
            title = hit.title or ""
            if title.strip().casefold() == folded:
                score, method = confidence.title, "title"
            elif normalize_title(title) == normalize_title(mention.text):
                score, method = confidence.normalized_title, "normalized_title"
            else:
                score, method = hit.score * confidence.normalized_title, "trigram"
            if hit.key not in candidates or candidates[hit.key].score < score:
                candidates[hit.key] = MatchCandidate(kind_id, hit.key, hit.title, score, method)
        ranked = sorted(candidates.values(), key=lambda c: (-c.score, c.key))[: matching.candidates]
        if not ranked or ranked[0].score < matching.match_threshold:
            return Resolution(kind_id, "unresolved", None, 0.0, "none", tuple(ranked))
        best = ranked[0]
        close = [c for c in ranked if best.score - c.score <= matching.ambiguity_margin]
        status: MatchStatus = "ambiguous" if len(close) > 1 else "matched"
        return Resolution(kind_id, status, best.key, best.score, best.method, tuple(ranked))


def disambiguate(
    resolved: Sequence[Resolved], sentences: Sequence[tuple[int, int]]
) -> list[Resolved]:
    """Resolve ambiguous mentions by keys named in the same sentence, then keep one mention per
    Record and sentence (the most confident)."""
    by_sentence: dict[int, list[Resolved]] = defaultdict(list)
    for item in resolved:
        by_sentence[_sentence_of(item.mention.start, sentences)].append(item)
    result: list[Resolved] = []
    for index in sorted(by_sentence):
        group = by_sentence[index]
        keyed = {(r.kind_id, r.key) for r in group if r.resolution.status in ("matched", "new")}
        fixed: list[Resolved] = []
        for item in group:
            resolution = item.resolution
            if resolution.status == "ambiguous":
                named = [c for c in resolution.candidates if (c.kind_id, c.key) in keyed]
                if len(named) == 1:
                    choice = named[0]
                    resolution = replace(
                        resolution,
                        status="matched",
                        key=choice.key,
                        score=choice.score,
                        method="context",
                    )
                    item = Resolved(item.mention, resolution)
            fixed.append(item)
        best: dict[tuple[str, str | None], Resolved] = {}
        for item in fixed:
            if not item.usable:
                continue
            element = (item.kind_id, item.key)
            if element not in best or best[element].confidence < item.confidence:
                best[element] = item
        result.extend(sorted(best.values(), key=lambda r: r.mention.start))
    return result


def _sentence_of(offset: int, sentences: Sequence[tuple[int, int]]) -> int:
    for index, (start, end) in enumerate(sentences):
        if start <= offset < end:
            return index
    return -1
