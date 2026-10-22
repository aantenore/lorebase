"""Records one trace step per traced request (`keeper.trace`, docs/agent-trace.md).

A request is a candidate when it could be traced: it opts in with the opt-in header, or
`trace_all`, `principals` or `api_keys` is set; a malformed session id rules it out.
`trace_all` leaves out the web app's own requests unless `include_web`: those whose client
header names the web app, and those authenticated by a browser session cookie that name no
agent session (Guide forwards the cookie of the user it explores for, with a session id).

A step's session is the one the session header names; without one the step is marked
`inferred` with a client fingerprint (a digest of the principal, its API key or web session,
and the user agent), and the store puts it in the inferred session of that fingerprint with a
step within `inferred_session_idle_s` of it, or a new one. Fingerprints include the principal,
so inferred sessions are never shared across principals. For a candidate the middleware keeps
the request body (up to `max_query_bytes`) and the response body (up to `max_response_bytes`),
streams both through unchanged, and after the response decides from the authenticated
principal whether to record it. Unauthenticated requests are
never recorded. For reads the query is kept (`capture`) and, when they succeed, the counts and
refs of the response; for writes only the route, status and the Proposal they name. With
`capture_filter_values: non_sensitive` each field a read compares is reported with whether
it is sensitive (`notes.note_field`), and the values filters compare on fields that are not
sensitive are kept.

Routes of the trace itself (`/trace/...`) are never traced.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.errors import request_id_of
from lorebase_keeper.tracing import capture
from lorebase_keeper.tracing.config import SESSION_ID, TraceSettings
from lorebase_keeper.tracing.notes import TraceNotes, tracing
from lorebase_keeper.tracing.recorder import TraceRecorder
from lorebase_store.tracing import NewTraceStep

TRACE_ROUTES = "/trace/"
WRITE_BODY_BYTES = 65_536
"""Most of a write's answer kept, to find the Proposal it names."""
OPT_IN_VALUES = frozenset({"on", "1", "true", "debug"})
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_PRINCIPAL_KEY = "principal"

ReadClassifier = Callable[[str, str], bool]


def trace_id_of(headers: Headers) -> str | None:
    """The W3C trace id of the request (`traceparent`), if any."""
    match = _TRACEPARENT.match(headers.get("traceparent", "").strip().lower())
    return match.group(1) if match and match.group(1) != "0" * 32 else None


class _Body:
    """Bytes of a body kept up to a limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[bytes] = []
        self.size = 0
        self.complete = True
        self.kept = True

    def skip(self) -> None:
        """Keep nothing of this body (it will not be inspected)."""
        self.kept = False
        self.parts.clear()

    def add(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if not self.kept or not self.complete:
            return
        if self.size > self.limit:
            self.complete = False
            self.parts.clear()
            return
        self.parts.append(chunk)

    def bytes(self) -> bytes:
        return b"".join(self.parts)


class TraceMiddleware:
    """See the module doc."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        recorder: TraceRecorder,
        api_prefix: str = "",
        is_read: ReadClassifier | None = None,
    ) -> None:
        self.app = app
        self._recorder = recorder
        self._settings: TraceSettings = recorder.settings
        self._api_prefix = api_prefix
        self._is_read = is_read
        self._listed = bool(
            self._settings.trace_all or self._settings.principals or self._settings.api_keys
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = self._settings
        headers = Headers(scope=scope)
        session_id = headers.get(settings.session_header, "").strip()
        opt_in = headers.get(settings.opt_in_header, "").strip().lower()
        opted_in = settings.allow_header_opt_in and opt_in in OPT_IN_VALUES
        malformed = bool(session_id) and not SESSION_ID.match(session_id)
        if malformed or not (opted_in or self._listed):
            await self.app(scope, receive, send)
            return

        at = datetime.now(UTC)
        begin = time.perf_counter()
        request_body = _Body(settings.max_query_bytes)
        response_body = _Body(settings.max_response_bytes)
        status = 500
        media_type = ""

        async def receive_and_keep() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                request_body.add(message.get("body", b""))
            return message

        async def send_and_keep(message: Message) -> None:
            nonlocal status, media_type
            if message["type"] == "http.response.start":
                status = message["status"]
                media_type = Headers(raw=message.get("headers", [])).get("content-type", "")
                self._size_response(scope, headers, response_body, status, media_type, opted_in)
            elif message["type"] == "http.response.body":
                response_body.add(message.get("body", b""))
            await send(message)

        notes = TraceNotes(
            debug=opted_in and opt_in == "debug",
            filter_values=settings.capture_filter_values == "non_sensitive"
            and not settings.capture_literals,
        )
        notes.may_explain = self._explain_check(scope)
        with tracing(notes):
            try:
                await self.app(scope, receive_and_keep, send_and_keep)
            finally:
                latency_ms = (time.perf_counter() - begin) * 1000
                step = self._step(
                    scope,
                    headers,
                    session_id=session_id,
                    opted_in=opted_in,
                    at=at,
                    latency_ms=latency_ms,
                    status=status,
                    json_response=media_type.startswith("application/json"),
                    request_body=request_body,
                    response_body=response_body,
                    notes=notes,
                )
                if step is not None:
                    self._recorder.offer(step)

    def _size_response(
        self,
        scope: Scope,
        headers: Headers,
        body: _Body,
        status: int,
        media_type: str,
        opted_in: bool,
    ) -> None:
        """Decide, once the answer starts, how much of it to keep: nothing unless it is a
        successful JSON answer to a principal who is traced; a read's answer up to
        `max_response_bytes`, a write's up to `WRITE_BODY_BYTES` (only the Proposal it names
        is read)."""
        principal = _principal(scope)
        if (
            principal is None
            or not self._traced(principal, headers, opted_in)
            or not 200 <= status < 300
            or not media_type.startswith("application/json")
        ):
            body.skip()
            return
        if not self._read_route(scope):
            body.limit = min(body.limit, WRITE_BODY_BYTES)

    def _read_route(self, scope: Scope) -> bool:
        route = self._route_template(scope)
        relative = route[len(self._api_prefix) :] if route.startswith(self._api_prefix) else route
        return self._is_read is not None and self._is_read(str(scope["method"]), relative or "/")

    def _explain_check(self, scope: Scope) -> Callable[[str], Any]:
        async def may_explain(domain_id: str) -> bool:
            principal = _principal(scope)
            app = scope.get("app")
            auth = getattr(getattr(app, "state", None), "auth", None)
            if principal is None or auth is None:
                return False
            allowed: bool = await auth.authorizer.allows(
                principal, Permission.TRACE_EXPLAIN, domain_id
            )
            return allowed

        return may_explain

    def _traced(self, principal: Principal, headers: Headers, opted_in: bool) -> bool:
        settings = self._settings
        return (
            opted_in
            or (settings.trace_all and (settings.include_web or not self._web(principal, headers)))
            or principal.id in settings.principals
            or (principal.key_id is not None and principal.key_id in settings.api_keys)
        )

    def _web(self, principal: Principal, headers: Headers) -> bool:
        """Whether the request is the web app's own: its client header names the web app, or
        a browser session cookie authenticated it and it names no agent session."""
        settings = self._settings
        client = headers.get(settings.client_header, "").strip().lower()
        if client == settings.web_client.strip().lower():
            return True
        return principal.method == "session" and not headers.get(settings.session_header, "")

    def _route_template(self, scope: Scope) -> str:
        path = getattr(scope.get("route"), "path", None)
        if not isinstance(path, str):
            return str(scope["path"])
        under_prefix = self._api_prefix and str(scope["path"]).startswith(self._api_prefix)
        return (
            self._api_prefix + path
            if under_prefix and not path.startswith(self._api_prefix)
            else path
        )

    def _step(
        self,
        scope: Scope,
        headers: Headers,
        *,
        session_id: str,
        opted_in: bool,
        at: datetime,
        latency_ms: float,
        status: int,
        json_response: bool,
        request_body: _Body,
        response_body: _Body,
        notes: TraceNotes,
    ) -> NewTraceStep | None:
        principal = _principal(scope)
        if principal is None or not self._traced(principal, headers, opted_in):
            return None
        route = self._route_template(scope)
        relative = route[len(self._api_prefix) :] if route.startswith(self._api_prefix) else route
        if relative.startswith(TRACE_ROUTES):
            return None
        method = str(scope["method"])
        settings = self._settings
        succeeded = 200 <= status < 300
        is_read = self._is_read is not None and self._is_read(method, relative or "/")
        query = result = refs = None
        proposal = None
        if is_read and status == 422:
            # A query Keeper could not read is not redacted reliably: keep only that it failed.
            query = {"rejected": True}
        elif is_read:
            query = capture.query_of(
                settings,
                params=_query_params(scope),
                body=request_body.bytes() if method == "POST" else None,
                body_complete=request_body.complete,
                plain=notes.plain if notes.filter_values else None,
            )
        document = (
            capture.parse_json(response_body.bytes())
            if succeeded and json_response and response_body.kept and response_body.complete
            else None
        )
        if document is not None and is_read:
            result = capture.result_of(document, settings)
            refs = capture.refs_of(document, settings.max_refs)
        elif document is not None and method != "GET":
            proposal = capture.proposal_of(document)
        if succeeded and is_read and response_body.kept and not response_body.complete:
            result = {"response_too_large": True, "bytes": response_body.size}
        tool = headers.get(settings.tool_header, "").strip()[:128] or None
        params = scope.get("path_params") or {}
        domain_id = params.get("domain_id")
        explain = {"plans": notes.plans} if notes.plans else None
        inferred = not session_id
        client = None
        if inferred:
            app = scope.get("app")
            auth = getattr(getattr(app, "state", None), "auth", None)
            cookie = auth.authenticator.session_id(headers) if auth is not None else None
            client = fingerprint(principal, headers, cookie)
        return NewTraceStep(
            session_id=session_id,
            inferred=inferred,
            fingerprint=client,
            actor=principal.id,
            type="call",
            at=at,
            principal_type=principal.type.value,
            domain_id=domain_id if isinstance(domain_id, str) else None,
            tool=tool,
            method=method,
            route=route,
            status=status,
            latency_ms=round(latency_ms, 3),
            cache=notes.cache,
            branch=capture.branch_of(query),
            query=query,
            result=result,
            refs=refs,
            explain=explain,
            proposal_id=proposal,
            request_id=request_id_of(scope.get("state")),
            trace_id=trace_id_of(headers),
        )


def fingerprint(principal: Principal, headers: Headers, session_cookie: str | None) -> str:
    """Digest naming one client of a principal: its API key, web session or subject, and its
    user agent."""
    if principal.key_id is not None:
        credential = "key:" + principal.key_id
    elif session_cookie:
        credential = "session:" + hashlib.sha256(session_cookie.encode()).hexdigest()
    else:
        credential = principal.method
    agent = hashlib.sha256(headers.get("user-agent", "").encode()).hexdigest()
    material = "\x1f".join((principal.id, credential, agent))
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _principal(scope: Scope) -> Principal | None:
    found = (scope.get("state") or {}).get(_PRINCIPAL_KEY)
    return found if isinstance(found, Principal) else None


def _query_params(scope: Scope) -> list[tuple[str, str]]:
    raw = scope.get("query_string", b"")
    return parse_qsl(raw.decode("latin-1"), keep_blank_values=True)
