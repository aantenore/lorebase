"""Tier-1 zero-shot span extraction with a local model, behind a backend callable.

The registry supplies the backend (the only code that touches the model library); this module
handles what is independent of it: windowing of long texts, mapping labels, translating
offsets back to the input text, and running the model in a worker thread.

The models read a few hundred tokens at a time. Texts longer than `window_chars` are cut at
sentence ends or whitespace into consecutive windows, so an entity that straddles a cut is
missed; callers are expected to pass chunks (design 9.2, step 3) and the cut is a safety net.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from lorebase_inference.interfaces import Label, Span
from lorebase_inference.settings import ExtractorSettings

SpanBackend = Callable[[str, list[str], float], list[Mapping[str, Any]]]
"""`(text, label names, threshold)` to entities with `start`, `end`, `label` and `score`."""

_SENTENCE_END = re.compile(r"[.!?]\s|\n")


def split_windows(text: str, size: int) -> Iterator[tuple[int, str]]:
    """Consecutive `(offset, window)` pieces of at most `size` characters covering `text`.

    A cut prefers the last sentence end in the second half of the window, then the last
    whitespace, then falls back to a hard cut. Windows that are only whitespace are skipped.
    """
    position, length = 0, len(text)
    while position < length:
        end = min(length, position + size)
        cut = end - position
        if end < length:
            piece = text[position:end]
            ends = [m.end() for m in _SENTENCE_END.finditer(piece) if m.end() > size // 2]
            if ends:
                cut = ends[-1]
            else:
                space = max(piece.rfind(" "), piece.rfind("\n"), piece.rfind("\t"))
                cut = space + 1 if space > 0 else cut
        if text[position : position + cut].strip():
            yield position, text[position : position + cut]
        position += cut


class LocalSpanExtractor:
    """`SpanExtractor` running a local zero-shot model through a backend."""

    def __init__(
        self, settings: ExtractorSettings, backend_factory: Callable[[], SpanBackend]
    ) -> None:
        self._settings = settings
        self._factory = backend_factory
        self._backend: SpanBackend | None = None
        self._lock = threading.Lock()

    async def extract(self, text: str, labels: Sequence[Label]) -> list[Span]:
        """Spans of the given labels in `text`, ordered by start offset."""
        if not labels or not text.strip():
            return []
        return await asyncio.to_thread(self._extract, text, list(labels))

    def _extract(self, text: str, labels: list[Label]) -> list[Span]:
        names = list(dict.fromkeys(label.name for label in labels))
        by_folded = {name.casefold(): name for name in names}
        spans: list[Span] = []
        with self._lock:
            if self._backend is None:
                self._backend = self._factory()
            backend = self._backend
            for offset, window in split_windows(text, self._settings.window_chars):
                for entity in backend(window, names, self._settings.threshold):
                    name = by_folded.get(str(entity.get("label", "")).casefold())
                    start, end = int(entity["start"]) + offset, int(entity["end"]) + offset
                    if name is None or not 0 <= start < end <= len(text):
                        continue
                    spans.append(
                        Span(
                            label=name,
                            text=text[start:end],
                            start=start,
                            end=end,
                            score=float(entity.get("score", 1.0)),
                        )
                    )
        spans.sort(key=lambda s: (s.start, s.end, s.label))
        return spans
