"""Candidates the tiers produce before they become Proposal items.

Offsets are always offsets into the parsed document text (end exclusive).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MatchStatus = Literal["matched", "new", "ambiguous", "unresolved"]
MentionSource = Literal["key", "title", "span", "model"]


@dataclass(frozen=True, slots=True)
class Evidence:
    document_id: str
    chunk: int
    start: int
    end: int
    quote: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk": self.chunk,
            "start": self.start,
            "end": self.end,
            "quote": self.quote,
        }


@dataclass(frozen=True, slots=True)
class Mention:
    """A piece of text naming a Record of `kind_id` (by key or by title)."""

    kind_id: str
    text: str
    start: int
    end: int
    chunk: int
    score: float
    tier: int
    source: MentionSource
    extractor: str
    document_id: str

    @property
    def evidence(self) -> Evidence:
        return Evidence(self.document_id, self.chunk, self.start, self.end, self.text)


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    kind_id: str
    key: str
    title: str | None
    score: float
    method: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "score": round(self.score, 4),
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    """Entity resolution of a mention: the Record it names, a new one, or several."""

    kind_id: str
    status: MatchStatus
    key: str | None
    score: float
    method: str
    candidates: tuple[MatchCandidate, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "method": self.method,
            "score": round(self.score, 4),
            "candidates": [c.as_dict() for c in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class Resolved:
    """A mention with its resolution; `confidence` combines both."""

    mention: Mention
    resolution: Resolution

    @property
    def key(self) -> str | None:
        return self.resolution.key

    @property
    def kind_id(self) -> str:
        return self.resolution.kind_id

    @property
    def confidence(self) -> float:
        return self.mention.score * self.resolution.score

    @property
    def usable(self) -> bool:
        return self.resolution.status != "unresolved" and self.resolution.key is not None


@dataclass(slots=True)
class ValueCandidate:
    """An Attribute value for the Record `subject` names."""

    subject: Resolved
    attribute_id: str
    value: Any
    evidence: Evidence
    confidence: float
    tier: int
    model: str


@dataclass(slots=True)
class LinkCandidate:
    relation_id: str
    source: Resolved
    target: Resolved
    evidence: Evidence
    confidence: float
    tier: int
    model: str
    values: dict[str, Any] = field(default_factory=dict)
