"""Agent trace headers: which session and tool each Keeper call belongs to.

Keeper records calls under the session id it reads from a request header. Herald names the
session and the tool on every call it makes for an agent (`herald.trace`), plus a W3C
`traceparent`: the caller's trace when its request carried one, else a trace id derived from
the session id (keyed per process, so it cannot be guessed), so one session is one trace.

Each tool call is a span of that trace and each Keeper HTTP request a child span of it, with a
span id of its own (`stamp_request`, an httpx request hook). When an OpenTelemetry span is
active in Herald, the `traceparent` is that span's context instead (W3C propagator).

The session id of a call comes from, in order:

1. the session header of the caller's MCP HTTP request (validated; the caller's own id);
2. `herald.trace.session_id`, when configured;
3. the MCP session id (the `mcp-session-id` header, stateful Streamable HTTP).

With none of them Herald sends no session header and Keeper infers the session itself; Herald
never invents an id. The `traceparent` then starts a new trace for the call.

`Tracing.begin` stores the headers and the span of the call being served in context variables
that `KeeperClients.open` and `stamp_request` read, so tool implementations need not pass them
along.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.propagate import inject

from lorebase_herald.settings import TraceSettings

MCP_SESSION_HEADER = "mcp-session-id"
SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
TRACE_MODES = frozenset({"on", "debug"})

call_headers: ContextVar[Mapping[str, str] | None] = ContextVar("herald_call_headers", default=None)
"""Trace headers of the Keeper calls made for the tool call being served."""


def request_header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted and value.strip():
            return value.strip()
    return None


_TRACE_KEY = secrets.token_bytes(32)
"""Per process: trace ids cannot be predicted from a session id, nor collide across Herald
processes for a common session id."""
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")


def trace_id_of(session_id: str) -> str:
    """The 32-hex W3C trace id of a session: stable within this process, so every call of a
    session shares it."""
    return hmac.new(_TRACE_KEY, session_id.encode(), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class Span:
    """A W3C span: `parent_id` is None for the root of a trace."""

    trace_id: str
    span_id: str
    parent_id: str | None = None

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    def child(self) -> Span:
        """A span of the same trace with a fresh id and this span as parent."""
        return Span(self.trace_id, secrets.token_hex(8), self.span_id)


call_span: ContextVar[Span | None] = ContextVar("herald_call_span", default=None)
"""The span of the tool call being served; the parent of its Keeper request spans."""


def tool_span(session_id: str | None, incoming: str | None = None) -> Span:
    """The span of a tool call. It is the active OpenTelemetry span of Herald when there is one;
    else a span with a fresh id in the caller's trace when its request carried a valid
    `traceparent`, else in the session's trace, else in a new trace."""
    active = trace.get_current_span().get_span_context()
    if active.is_valid:
        return Span(f"{active.trace_id:032x}", f"{active.span_id:016x}")
    match = TRACEPARENT.fullmatch((incoming or "").strip().lower())
    if match and match.group(1) != "0" * 32:
        return Span(match.group(1), secrets.token_hex(8), match.group(2))
    trace_id = trace_id_of(session_id) if session_id else secrets.token_hex(16)
    return Span(trace_id, secrets.token_hex(8))


def request_traceparent() -> str | None:
    """The `traceparent` of the next Keeper request of the tool call being served: the active
    OpenTelemetry span's context, else a new child span of the tool call's span; None when
    tracing is off."""
    carrier: dict[str, str] = {}
    inject(carrier)
    if "traceparent" in carrier:
        return carrier["traceparent"]
    parent = call_span.get()
    return parent.child().traceparent if parent else None


async def stamp_request(request: httpx.Request) -> None:
    """httpx request hook: gives every Keeper request its own span."""
    value = request_traceparent()
    if value:
        request.headers["traceparent"] = value


class Tracing:
    """Builds the trace headers of each tool call."""

    def __init__(self, settings: TraceSettings) -> None:
        self._settings = settings

    def session_id(self, headers: Mapping[str, str] | None) -> str | None:
        """The session id of a call, or None; see the module docstring."""
        settings = self._settings
        for candidate in (
            request_header(headers, settings.session_header),
            settings.session_id,
            request_header(headers, MCP_SESSION_HEADER),
        ):
            if candidate and SESSION_ID.fullmatch(candidate):
                return candidate
        return None

    def headers(self, tool: str, request_headers: Mapping[str, str] | None) -> dict[str, str]:
        """Headers to send to Keeper for a call of `tool`; empty when tracing is off. The
        `traceparent` is set per request, see `stamp_request`."""
        settings = self._settings
        if not settings.enabled:
            return {}
        session_id = self.session_id(request_headers)
        out = {settings.tool_header: tool}
        if session_id:
            out[settings.session_header] = session_id
        mode = (request_header(request_headers, settings.trace_header) or "").lower()
        if mode in TRACE_MODES:
            out[settings.trace_header] = mode
        elif settings.opt_in:
            out[settings.trace_header] = "on"
        return out

    def begin(self, ctx: Any, tool: str) -> None:
        """Set the trace headers of the Keeper calls `tool` is about to make."""
        try:
            request_headers = ctx.headers if ctx is not None else None
        except ValueError:  # no request is being served
            request_headers = None
        headers = self.headers(tool, request_headers)
        call_headers.set(headers)
        call_span.set(
            tool_span(
                self.session_id(request_headers), request_header(request_headers, "traceparent")
            )
            if headers
            else None
        )
