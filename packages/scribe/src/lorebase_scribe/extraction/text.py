"""Chunks and sentences of parsed text, with offsets into that text (design 9.2 step 3).

Chunks follow the structure: paragraphs, headings, list items and table rows are kept whole
when they fit, longer ones are cut at sentence ends and then at whitespace. Consecutive chunks
overlap by at most `overlap_chars`, starting at a whitespace boundary. A chunk's text is always
`text[start:end]`, so evidence offsets found in a chunk map back to the document by adding its
start.

Sentences end at `.`, `!` or `?` followed by whitespace and a character that is not a
lowercase letter (so `Sp. z o.o.` stays whole), at blank lines, and at line breaks before a
block marker (`#`, `|`, `-`, `*`, `>`, `1.`) or after a heading or table row. Lines wrapped
inside a paragraph stay in one sentence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_BLOCK_START = re.compile(r"\s*(#|\||[-*>]\s|\*\*|\d+[.)]\s)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[^\sa-z])")
_HEADING = re.compile(r"\s*#{1,6}\s")


@dataclass(frozen=True, slots=True)
class Chunk:
    seq: int
    start: int
    end: int
    text: str

    @property
    def hash(self) -> bytes:
        return hashlib.sha256(self.text.encode("utf-8")).digest()


def _lines(text: str) -> list[tuple[int, int]]:
    """`(start, end)` of every line, newline excluded."""
    bounds: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char == "\n":
            bounds.append((start, index))
            start = index + 1
    bounds.append((start, len(text)))
    return bounds


def _groups(text: str) -> list[tuple[int, int]]:
    """Blocks of lines: paragraphs, headings, list items and table rows."""
    groups: list[tuple[int, int]] = []
    current: list[int] | None = None
    previous_single = False
    for start, end in _lines(text):
        line = text[start:end]
        if not line.strip():
            if current is not None:
                groups.append((current[0], current[1]))
            current, previous_single = None, False
            continue
        starts_block = bool(_BLOCK_START.match(line))
        if current is None or starts_block or previous_single:
            if current is not None:
                groups.append((current[0], current[1]))
            current = [start, end]
        else:
            current[1] = end
        stripped = line.lstrip()
        previous_single = stripped.startswith(("#", "|"))
    if current is not None:
        groups.append((current[0], current[1]))
    return [_trim(text, start, end) for start, end in groups if text[start:end].strip()]


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def sentence_bounds(text: str) -> list[tuple[int, int]]:
    """`(start, end)` of the sentences of `text`, whitespace trimmed."""
    sentences: list[tuple[int, int]] = []
    for start, end in _groups(text):
        position = start
        for match in _SENTENCE_END.finditer(text, start, end):
            sentences.append(_trim(text, position, match.start()))
            position = match.end()
        sentences.append(_trim(text, position, end))
    return [bound for bound in sentences if bound[1] > bound[0]]


@dataclass(frozen=True, slots=True)
class Section:
    """A Markdown heading and its section, up to the next heading of the same or a higher
    level; `heading_end` ends the heading line."""

    start: int
    heading_end: int
    end: int
    level: int


_HEADING_LINE = re.compile(r"^(#{1,6})\s", re.MULTILINE)


def heading_sections(text: str) -> list[Section]:
    headings = [(m.start(), len(m.group(1))) for m in _HEADING_LINE.finditer(text)]
    sections: list[Section] = []
    for index, (start, level) in enumerate(headings):
        end = next(
            (later for later, later_level in headings[index + 1 :] if later_level <= level),
            len(text),
        )
        line_end = text.find("\n", start)
        sections.append(Section(start, len(text) if line_end == -1 else line_end, end, level))
    return sections


def is_heading(text: str, offset: int) -> bool:
    """Whether `offset` lies on a Markdown heading line."""
    line_start = text.rfind("\n", 0, offset) + 1
    return bool(_HEADING.match(text, line_start))


def _pieces(text: str, start: int, end: int, size: int) -> list[tuple[int, int]]:
    """Cut `[start, end)` into pieces of at most `size` characters at sentence ends, then at
    whitespace, then anywhere."""
    if end - start <= size:
        return [(start, end)]
    units = [(s, e) for s, e in sentence_bounds(text[start:end])]
    units = [(start + s, start + e) for s, e in units] or [(start, end)]
    pieces: list[tuple[int, int]] = []
    for unit_start, unit_end in units:
        position = unit_start
        while unit_end - position > size:
            cut = text.rfind(" ", position + 1, position + size)
            if cut <= position:
                cut = position + size
            pieces.append(_trim(text, position, cut))
            position = cut
        pieces.append(_trim(text, position, unit_end))
    return [piece for piece in pieces if piece[1] > piece[0]]


def chunk_text(text: str, *, max_chars: int, overlap_chars: int) -> list[Chunk]:
    """Chunks of at most `max_chars` characters covering every non-blank part of `text`."""
    if overlap_chars * 2 >= max_chars:
        raise ValueError("overlap_chars must be less than half of max_chars")
    unit_size = max_chars - overlap_chars
    units = [
        piece for start, end in _groups(text) for piece in _pieces(text, start, end, unit_size)
    ]
    chunks: list[Chunk] = []
    index = 0
    previous_end: int | None = None
    while index < len(units):
        start = units[index][0]
        if previous_end is not None and overlap_chars:
            boundary = text.find(" ", max(previous_end - overlap_chars, 0), previous_end)
            if boundary != -1:
                start = min(start, boundary + 1)
        end = units[index][1]
        index += 1
        while index < len(units) and units[index][1] - start <= max_chars:
            end = units[index][1]
            index += 1
        start, end = _trim(text, start, end)
        chunks.append(Chunk(len(chunks), start, end, text[start:end]))
        previous_end = end
    return chunks


def normalize_space(text: str) -> str:
    """Whitespace runs folded to one space, ends stripped (evidence comparison)."""
    return " ".join(text.split())
