"""Agent trace headers: which session and tool each Keeper call belongs to.

Keeper records calls under the session id it reads from a request header. Herald names the
session and the tool on every call it makes for an agent (`herald.trace`), plus a W3C
`traceparent`: the caller's trace when its request carried one, else a trace id derived from
the session id (keyed per process, so it cannot be guessed), so one session is one trace.

The session id of a call comes from, in order:

1. the session header of the caller's MCP HTTP request (validated; the caller's own id);
2. `herald.trace.session_id`, when configured;
3. the MCP session id (the `mcp-session-id` header, stateful Streamable HTTP).

With none of them Herald sends no session header and Keeper infers the session itself; Herald
never invents an id. The `traceparent` then starts a new trace for the call.

`Tracing.begin` stores the headers of the call being served in a context variable that
`KeeperClients.open` reads, so tool implementations need not pass them along.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

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
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}")


def trace_id_of(session_id: str) -> str:
    """The 32-hex W3C trace id of a session: stable within this process, so every call of a
    session shares it."""
    return hmac.new(_TRACE_KEY, session_id.encode(), hashlib.sha256).hexdigest()[:32]


def traceparent(session_id: str | None, incoming: str | None = None) -> str:
    """A `traceparent` with a fresh span id: in the caller's trace when its request carried a
    valid `traceparent`, else in the session's trace, else in a new trace."""
    match = TRACEPARENT.fullmatch((incoming or "").strip().lower())
    if match and match.group(1) != "0" * 32:
        trace_id = match.group(1)
    else:
        trace_id = trace_id_of(session_id) if session_id else secrets.token_hex(16)
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


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
        """Headers to send to Keeper for a call of `tool`; empty when tracing is off."""
        settings = self._settings
        if not settings.enabled:
            return {}
        session_id = self.session_id(request_headers)
        out = {
            settings.tool_header: tool,
            "traceparent": traceparent(session_id, request_header(request_headers, "traceparent")),
        }
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
        call_headers.set(self.headers(tool, request_headers))
