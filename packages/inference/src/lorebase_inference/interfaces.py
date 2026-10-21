"""Vendor-neutral model interfaces and their result types (design 9.4).

Callers depend on these protocols only; the provider behind them is chosen by configuration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

JsonSchema = Mapping[str, Any]
"""A JSON Schema document describing the expected structure of a model answer."""


@dataclass(frozen=True, slots=True)
class Message:
    """One chat message."""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ChatOptions:
    """Per-call options; unset values fall back to the configured defaults."""

    task: str | None = None
    """Task name used for per-task routing (for example `extraction`)."""
    temperature: float | None = None
    max_tokens: int | None = None
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ChatResult:
    """Parsed JSON answer with accounting."""

    data: Any
    """The answer parsed from JSON; conforms to the requested schema."""
    usage: Usage
    model: str
    """Identifier of the model that produced the answer."""


@dataclass(frozen=True, slots=True)
class Label:
    """An entity type a span extractor should look for."""

    name: str
    description: str = ""
    examples: tuple[str, ...] = field(default_factory=tuple)
    """Surface forms known for this label; used by dictionary-based extractors."""


@dataclass(frozen=True, slots=True)
class Span:
    """A labelled text span with character offsets (`end` exclusive) into the input text."""

    label: str
    text: str
    start: int
    end: int
    score: float = 1.0

    def __post_init__(self) -> None:
        if not 0 <= self.start <= self.end:
            raise ValueError(f"invalid span offsets {self.start}..{self.end}")


@dataclass(frozen=True, slots=True)
class RelationLabel:
    """A relation type a relation extractor should look for between spans."""

    name: str
    description: str = ""
    head_labels: tuple[str, ...] = field(default_factory=tuple)
    """Span labels allowed as head; empty allows any."""
    tail_labels: tuple[str, ...] = field(default_factory=tuple)
    """Span labels allowed as tail; empty allows any."""

    def allows(self, head: Span, tail: Span) -> bool:
        """True when the span labels satisfy the head and tail constraints."""
        return (not self.head_labels or head.label in self.head_labels) and (
            not self.tail_labels or tail.label in self.tail_labels
        )


@dataclass(frozen=True, slots=True)
class SpanRelation:
    """A candidate relation `label` between two spans of the same text."""

    label: str
    head: Span
    tail: Span
    score: float = 1.0


@runtime_checkable
class ChatModel(Protocol):
    """Chat completion constrained to a JSON schema."""

    async def complete_json(
        self,
        messages: Sequence[Message],
        schema: JsonSchema,
        options: ChatOptions | None = None,
    ) -> ChatResult:
        """Answer `messages` with JSON that conforms to `schema`."""
        ...


@runtime_checkable
class EmbeddingModel(Protocol):
    """Text embeddings of a fixed dimension."""

    name: str
    """Model name; vectors are stored keyed by it."""
    dims: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector of `dims` floats per input text, in order."""
        ...


@runtime_checkable
class SpanExtractor(Protocol):
    """Tier-1 extraction of labelled spans from text."""

    async def extract(self, text: str, labels: Sequence[Label]) -> list[Span]:
        """Spans found in `text` for the given labels, ordered by start offset."""
        ...


@runtime_checkable
class RelationExtractor(Protocol):
    """Tier-1 extraction of candidate relations between already extracted spans."""

    async def extract(
        self, text: str, spans: Sequence[Span], labels: Sequence[RelationLabel]
    ) -> list[SpanRelation]:
        """Relations between `spans` (offsets into `text`), ordered by head then tail start."""
        ...
