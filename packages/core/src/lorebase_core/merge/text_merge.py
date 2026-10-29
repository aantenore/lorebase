"""Three-way merge of long text values (design 6.8 step 3).

A ``text`` value is cut into tokens (paragraphs, or sentences inside paragraphs) and the
whitespace between them, which is a token of its own, so the tokens of a value concatenate to
the value byte for byte and appending after the last sentence is a pure insertion.
Both sides are aligned with the base (``difflib``) and the three token streams are merged as
``diff3`` does: a change made on one side takes that side, a change made the same way on both
sides is taken once, and changes that overlap on the base (share a token), insertions at the
same place and an insertion at the edge of the other side's change make a conflicting hunk.
Non-empty changes that merely touch, such as two neighbouring sentences each rewritten on one
side, merge. A conflicting hunk is cut down to the passages that differ: text the three
values share at its start and end (cut at whitespace) goes to the clean hunks around it.
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
_PARAGRAPH_BREAK = re.compile(r"(\n[ \t\r]*+\n(?:[ \t\r]*+\n)*+)")

#: The end of a sentence: ``.``, ``!``, ``?`` or ``…`` (and closing quotes or brackets), then
#: the whitespace group. A full stop inside a word or a number is not followed by whitespace.
_SENTENCE_END = re.compile(r"(?<![.!?…])[.!?…]++[\"')\]»”\u2019]*+(\s+)")


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
    """Paragraphs of ``text``, or its sentences, with the whitespace between them as tokens of
    their own, so ``"".join(tokenize(text)) == text``."""
    parts = _PARAGRAPH_BREAK.split(text)
    if granularity is TextGranularity.PARAGRAPH:
        return [p for p in parts if p]
    tokens: list[str] = []
    for index, part in enumerate(parts):
        if index % 2:
            tokens.append(part)
            continue
        start = 0
        for match in _SENTENCE_END.finditer(part):
            tokens.append(part[start : match.start(1)])
            tokens.append(match.group(1))
            start = match.end()
        tail = part[start:]
        kept = tail.rstrip()
        if kept:
            tokens.append(kept)
        if tail[len(kept) :]:
            tokens.append(tail[len(kept) :])
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
        if status is HunkStatus.CONFLICT:
            hunks.extend(_narrowed(base_text, ours_text, theirs_text))
        else:
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


def _shared_start(texts: tuple[str, str, str]) -> int:
    """Length of the longest start the three texts share that ends at a word boundary."""
    limit = 0
    shortest = min(len(t) for t in texts)
    while limit < shortest and texts[0][limit] == texts[1][limit] == texts[2][limit]:
        limit += 1
    for k in range(limit, 0, -1):
        if texts[0][k - 1].isspace() or all(len(t) == k or t[k].isspace() for t in texts):
            return k
    return 0


def _narrowed(base: str, ours: str, theirs: str) -> list[TextHunk]:
    """A conflicting hunk reduced to the passages that differ, between clean hunks holding the
    text the three values share at its start and end; words are never split."""
    texts = (base, ours, theirs)
    head = _shared_start(texts)
    rest = tuple(t[head:] for t in texts)
    tail = _shared_start((rest[0][::-1], rest[1][::-1], rest[2][::-1]))
    hunks: list[TextHunk] = []
    if head:
        hunks.append(TextHunk(HunkStatus.CLEAN, base[:head], base[:head], base[:head], base[:head]))
    cut = tuple(r[: len(r) - tail] for r in rest)
    hunks.append(TextHunk(HunkStatus.CONFLICT, cut[0], cut[1], cut[2], None))
    if tail:
        end = rest[0][len(rest[0]) - tail :]
        hunks.append(TextHunk(HunkStatus.CLEAN, end, end, end, end))
    return hunks


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
