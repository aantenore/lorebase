"""Three-way merge of long text values (design 6.8 step 3).

A ``text`` value is cut into tokens (paragraphs, or sentences inside paragraphs) that keep
their trailing whitespace, so the tokens of a value concatenate to the value byte for byte.
Both sides are aligned with the base (``difflib``) and the three token streams are merged as
``diff3`` does: a change made on one side takes that side, a change made the same way on both
sides is taken once, and changes that overlap on the base (share a token), insertions at the
same place and an insertion at the edge of the other side's change make a conflicting hunk.
Non-empty changes that merely touch, such as two neighbouring sentences each rewritten on one
side, merge.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Final

from pydantic import JsonValue

__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_COST",
    "MAX_TOKENS",
    "HunkStatus",
    "TextGranularity",
    "TextHunk",
    "TextMerge",
    "merge_text",
    "tokenize",
]

#: Default cap on the characters of the three values together; above it text is not merged.
DEFAULT_MAX_CHARS: Final = 200_000

#: Cap on the tokens of the three values together, which bounds the cost of the alignment.
MAX_TOKENS: Final = 20_000

#: Cap on the base's tokens times the larger side's: the alignment is quadratic on repetitive
#: text, which the token cap alone does not bound.
MAX_COST: Final = 4_000_000

#: A line break followed by one or more blank lines.
_PARAGRAPH_BREAK = re.compile(r"(\n[ \t\r]*\n(?:[ \t\r]*\n)*)")

#: The end of a sentence: ``.``, ``!``, ``?`` or ``…`` (and closing quotes or brackets), then
#: whitespace. A full stop inside a word or a number is not followed by whitespace.
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]»”\u2019]*\s+")


class TextGranularity(StrEnum):
    """The unit text is compared and merged by."""

    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"


class HunkStatus(StrEnum):
    CLEAN = "clean"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class TextHunk:
    """One region of the three values: ``merged`` is the merged text of a clean hunk."""

    status: HunkStatus
    base: str
    ours: str
    theirs: str
    merged: str | None

    @property
    def changed(self) -> bool:
        return not self.base == self.ours == self.theirs

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "status": self.status.value,
            "base": self.base,
            "ours": self.ours,
            "theirs": self.theirs,
            "merged": self.merged,
        }


@dataclass(frozen=True)
class TextMerge:
    """The hunks of a merge; they cover the three values, in order."""

    hunks: tuple[TextHunk, ...]

    @property
    def clean(self) -> bool:
        return all(h.status is HunkStatus.CLEAN for h in self.hunks)

    @property
    def text(self) -> str:
        """The merged value of a clean merge."""
        assert self.clean
        return "".join(h.merged or "" for h in self.hunks)

    @property
    def ours_changes(self) -> int:
        """Hunks where ours differs from the base."""
        return sum(h.ours != h.base for h in self.hunks)

    @property
    def theirs_changes(self) -> int:
        """Hunks where theirs differs from the base."""
        return sum(h.theirs != h.base for h in self.hunks)

    def to_json(self) -> list[JsonValue]:
        return [h.to_json() for h in self.hunks]


def tokenize(text: str, granularity: TextGranularity = TextGranularity.SENTENCE) -> list[str]:
    """Paragraphs of ``text``, or its sentences; every token keeps the whitespace that follows
    it, so ``"".join(tokenize(text)) == text``."""
    parts = _PARAGRAPH_BREAK.split(text)
    paragraphs = [
        parts[i] + (parts[i + 1] if i + 1 < len(parts) else "") for i in range(0, len(parts), 2)
    ]
    paragraphs = [p for p in paragraphs if p]
    if granularity is TextGranularity.PARAGRAPH:
        return paragraphs
    tokens: list[str] = []
    for paragraph in paragraphs:
        start = 0
        for match in _SENTENCE_END.finditer(paragraph):
            tokens.append(paragraph[start : match.end()])
            start = match.end()
        if start < len(paragraph):
            tokens.append(paragraph[start:])
    return tokens


def merge_text(
    base: str,
    ours: str,
    theirs: str,
    *,
    granularity: TextGranularity = TextGranularity.SENTENCE,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> TextMerge | None:
    """Merge ``ours`` and ``theirs`` against ``base``; ``None`` above the size caps."""
    if len(base) + len(ours) + len(theirs) > max_chars:
        return None
    b, o, t = (tokenize(v, granularity) for v in (base, ours, theirs))
    if len(b) + len(o) + len(t) > MAX_TOKENS or len(b) * max(len(o), len(t)) > MAX_COST:
        return None
    mine, other = _changes(b, o, 0), _changes(b, t, 1)
    # Changes that clash join into one component; its region is the span of its changes.
    parent = list(range(len(mine) + len(other)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ends = [c.stop for c in other]
    for i, x in enumerate(mine):
        for j in range(bisect_left(ends, x.start), len(other)):
            if other[j].start > x.stop:
                break
            if _clash(x, other[j]):
                parent[find(i)] = find(len(mine) + j)
    components: dict[int, list[_Change]] = {}
    sides: dict[int, set[int]] = {}
    for i, change in enumerate([*mine, *other]):
        root = find(i)
        components.setdefault(root, []).append(change)
        sides.setdefault(root, set()).add(int(i >= len(mine)))
    hunks: list[TextHunk] = []
    position = 0
    for root, changes in sorted(components.items(), key=lambda item: min(c.start for c in item[1])):
        changes.sort(key=lambda c: (c.start, c.stop, c.side))
        start, stop = changes[0].start, max(c.stop for c in changes)
        if start > position:
            kept = "".join(b[position:start])
            hunks.append(TextHunk(HunkStatus.CLEAN, kept, kept, kept, kept))
        ours_text = _apply(b, start, stop, [c for c in changes if c.side == 0])
        theirs_text = _apply(b, start, stop, [c for c in changes if c.side == 1])
        base_text = "".join(b[start:stop])
        if len(sides[root]) == 1:
            status, merged = HunkStatus.CLEAN, ours_text if changes[0].side == 0 else theirs_text
        elif len(changes) == 2 and ours_text == theirs_text:
            status, merged = HunkStatus.CLEAN, ours_text
        else:
            status, merged = HunkStatus.CONFLICT, None
        hunks.append(TextHunk(status, base_text, ours_text, theirs_text, merged))
        position = stop
    if position < len(b):
        kept = "".join(b[position:])
        hunks.append(TextHunk(HunkStatus.CLEAN, kept, kept, kept, kept))
    return TextMerge(_coalesce(hunks))


@dataclass(frozen=True)
class _Change:
    """Base tokens ``[start, stop)`` replaced by ``text`` on one side (0 ours, 1 theirs)."""

    start: int
    stop: int
    text: str
    side: int


def _changes(base: list[str], side: list[str], index: int) -> list[_Change]:
    """The changes of ``side`` against ``base``, in base order."""
    matcher = SequenceMatcher(None, base, side, autojunk=False)
    return [
        _Change(i1, i2, "".join(side[j1:j2]), index)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _clash(x: _Change, y: _Change) -> bool:
    """Whether an ours and a theirs change cannot be merged apart (identical ones included:
    they are told apart afterwards)."""
    x_empty, y_empty = x.start == x.stop, y.start == y.stop
    if x_empty and y_empty:
        return x.start == y.start
    if x_empty:
        return y.start <= x.start <= y.stop
    if y_empty:
        return x.start <= y.start <= x.stop
    return x.start < y.stop and y.start < x.stop


def _apply(base: list[str], start: int, stop: int, changes: list[_Change]) -> str:
    """``base[start:stop]`` with the changes of one side applied."""
    out: list[str] = []
    cursor = start
    for change in changes:
        out.append("".join(base[cursor : change.start]))
        out.append(change.text)
        cursor = change.stop
    out.append("".join(base[cursor:stop]))
    return "".join(out)


def _coalesce(hunks: list[TextHunk]) -> tuple[TextHunk, ...]:
    """Join neighbouring hunks that no side changed."""
    joined: list[TextHunk] = []
    for hunk in hunks:
        last = joined[-1] if joined else None
        if last is not None and not last.changed and not hunk.changed:
            text = last.base + hunk.base
            joined[-1] = TextHunk(HunkStatus.CLEAN, text, text, text, text)
        else:
            joined.append(hunk)
    return tuple(joined)
