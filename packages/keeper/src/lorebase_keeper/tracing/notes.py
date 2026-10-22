"""What the code serving a traced request reports to the trace (`TraceMiddleware`).

The notes live in a context variable, so reading code needs no request object: the response
cache reports hits and misses (`note_cache`), the query and search compilers hand their
statements to `explain` (`tracing.explain`) when the request asked for query plans, and the
check of every field a read filters, orders or groups by reports whether that field is
sensitive (`note_field`), so the values filters compare on the others can be kept. Outside a
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
    filter_values: bool = False
    """Filter values on fields that are not sensitive are kept
    (`keeper.trace.capture_filter_values`): the read reports each field it resolves."""
    plain_fields: set[str] = field(default_factory=set)
    """Names of fields the read resolved and found not sensitive."""
    sensitive_fields: set[str] = field(default_factory=set)
    """Names of fields the read resolved and found sensitive."""

    def plain(self, name: str) -> bool:
        """Whether every field the read resolved under `name` is not sensitive: a filter on it
        compares a value whose literal may be kept. Names the read never resolved are not
        plain, and a name resolved to a sensitive field anywhere in the read (another Kind
        of a Trait, the far side of a Relation) is not plain anywhere."""
        return name in self.plain_fields and name not in self.sensitive_fields


_NOTES: ContextVar[TraceNotes | None] = ContextVar("lorebase_trace_notes", default=None)


def current() -> TraceNotes | None:
    """The notes of the current traced request, if any."""
    return _NOTES.get()


def note_cache(result: str) -> None:
    """Report a response cache lookup: `hit`, `miss` or `bypass`."""
    notes = _NOTES.get()
    if notes is not None:
        notes.cache = result


def note_field(name: str, sensitive: bool) -> None:
    """Report a field the read resolved (an Attribute, derived Attribute, Trait Attribute,
    `key` or `title`) and whether it is sensitive for the Records it is read from."""
    notes = _NOTES.get()
    if notes is None or not notes.filter_values:
        return
    (notes.sensitive_fields if sensitive else notes.plain_fields).add(name)


@contextlib.contextmanager
def tracing(notes: TraceNotes) -> Iterator[TraceNotes]:
    """Collect the notes of the code run inside the block."""
    token = _NOTES.set(notes)
    try:
        yield notes
    finally:
        _NOTES.reset(token)
