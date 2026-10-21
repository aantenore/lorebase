"""Rendering helpers shared by the tools: compact values, list caps and size caps.

Text output is written for a model's context window: one fact per line, `Kind/key` refs,
no decoration. Every cut is visible: lists end with `... N more` and over-long outputs end
with a `[truncated ...]` marker, never a silent cut.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter

from lorebase_herald.settings import OutputSettings

ELLIPSIS = "…"
_TIMESTAMP = TypeAdapter(datetime)


def ref(kind: str, key: str) -> str:
    """The `Kind/key` reference of a Record."""
    return f"{kind}/{key}"


def as_json(value: Any) -> str:
    """Compact JSON (no spaces, UTF-8 kept)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def timestamp(value: datetime) -> str:
    """A timestamp as the API writes it: RFC 3339, `Z` for UTC."""
    return str(_TIMESTAMP.dump_python(value, mode="json"))


def value_text(value: Any, max_chars: int) -> str:
    """A value on one line: strings as they are, other values as compact JSON."""
    text = value if isinstance(value, str) else as_json(value)
    text = text.replace("\n", " ")
    return text if len(text) <= max_chars else text[: max_chars - 1] + ELLIPSIS


def values_text(values: Mapping[str, Any], max_chars: int, separator: str = "; ") -> str:
    """`name=value` pairs on one line."""
    return separator.join(f"{k}={value_text(v, max_chars)}" for k, v in values.items())


def titled(reference: str, title: str | None) -> str:
    return f'{reference} "{title}"' if title else reference


def capped[T](items: Sequence[T], limit: int) -> tuple[list[T], int]:
    """The first `limit` items and how many were left out."""
    return list(items[:limit]), max(0, len(items) - limit)


def more_line(omitted: int, hint: str = "") -> list[str]:
    """`... N more` when items were left out."""
    if omitted <= 0:
        return []
    return [f"... {omitted} more{f' ({hint})' if hint else ''}"]


def finish(text: str, output: OutputSettings) -> str:
    """Cut an output longer than `max_chars` at a line break, with a marker saying so."""
    if len(text) <= output.max_chars:
        return text
    budget = output.max_chars - 120
    cut = text.rfind("\n", 0, budget)
    cut = budget if cut <= 0 else cut
    omitted = len(text) - cut
    return (
        text[:cut] + f"\n[truncated: {omitted} more chars; narrow the request, lower `limit` "
        "or follow `next_cursor`]"
    )


def render(view: Mapping[str, Any], text: str, fmt: str, output: OutputSettings) -> str:
    """The final tool output: compact JSON of `view` or `text`, within the size cap."""
    return finish(as_json(view) if fmt == "json" else text, output)
