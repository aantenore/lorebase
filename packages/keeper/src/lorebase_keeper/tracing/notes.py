"""What the code serving a traced request reports to the trace (`TraceMiddleware`).

The notes live in a context variable, so reading code needs no request object: the response
cache reports hits and misses (`note_cache`), and the query and search compilers hand their
statements to `explain` (`tracing.explain`) when the request asked for query plans. Outside a
traced request every function here does nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

MAX_PLANS = 4
"""Query plans kept per request."""


@dataclass
class TraceNotes:
    """Facts about the current traced request."""

    debug: bool = False
    """The request asked for query plans (`X-Lorebase-Trace: debug`)."""
    may_explain: Callable[[str], Awaitable[bool]] | None = None
    """Whether the caller may see query plans in a Domain (trace.explain)."""
    cache: str | None = None
    plans: list[dict[str, Any]] = field(default_factory=list)


_NOTES: ContextVar[TraceNotes | None] = ContextVar("lorebase_trace_notes", default=None)


def current() -> TraceNotes | None:
    """The notes of the current traced request, if any."""
    return _NOTES.get()


def note_cache(result: str) -> None:
    """Report a response cache lookup: `hit`, `miss` or `bypass`."""
    notes = _NOTES.get()
    if notes is not None:
        notes.cache = result


@contextlib.contextmanager
def tracing(notes: TraceNotes) -> Iterator[TraceNotes]:
    """Collect the notes of the code run inside the block."""
    token = _NOTES.set(notes)
    try:
        yield notes
    finally:
        _NOTES.reset(token)
