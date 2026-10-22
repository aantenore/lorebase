"""Audit of mutating and denied requests (design 10.4).

`AuditMiddleware` wraps the application. For every request it creates an `AuditDraft` in the
ASGI scope state; dependencies and routes fill it in (`annotate`): who called, which Domain,
a precise action name and target. When the response is complete the middleware writes one
audit entry if the request mutated state (POST, PUT, PATCH, DELETE by an authenticated caller)
or was denied (401, 403 when `audit.record_denied`).

Writing happens after the response has been produced and never changes it: a failed or slow
write is logged, counted in the `lorebase.audit.write_failures` metric, and dropped. Request
bodies, headers and credentials are never recorded.

With a `ReadAuditor` (`audit.reads.enabled`), successful reads (GET and HEAD, and the POST
routes that only read: `keeper.limits.rate.routes`) of an authenticated caller are handed to
it, with the sensitive values the request showed or masked (`read_audit.collecting`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lorebase_keeper.auth.config import AuditSettings
from lorebase_keeper.auth.ports import AuthStore
from lorebase_keeper.auth.read_audit import READ_ACTION, ReadAuditor, ReadNotes, collecting
from lorebase_keeper.errors import request_id_of
from lorebase_store.auth import NewAuditEntry

logger = logging.getLogger("lorebase.keeper.audit")

STATE_KEY = "audit"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DENIED_STATUSES = frozenset({401, 403})
ANONYMOUS_ACTOR = "anonymous"


@dataclass
class AuditDraft:
    """What is known about the request so far."""

    actor: str | None = None
    domain_id: str | None = None
    action: str | None = None
    target: str | None = None
    permission: str | None = None
    principal_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    suppressed: bool = False


def draft_of(connection: HTTPConnection) -> AuditDraft | None:
    """The request's audit draft, if the audit middleware is installed."""
    state: MutableMapping[str, Any] = connection.scope.get("state") or {}
    draft = state.get(STATE_KEY)
    return draft if isinstance(draft, AuditDraft) else None


def annotate(
    connection: HTTPConnection,
    *,
    actor: str | None = None,
    domain_id: str | None = None,
    action: str | None = None,
    target: str | None = None,
    permission: str | None = None,
    principal_type: str | None = None,
    **details: Any,
) -> None:
    """Add facts to the request's audit entry. Later calls overwrite earlier values."""
    draft = draft_of(connection)
    if draft is None:
        return
    for name, value in (
        ("actor", actor),
        ("domain_id", domain_id),
        ("action", action),
        ("target", target),
        ("permission", permission),
        ("principal_type", principal_type),
    ):
        if value is not None:
            setattr(draft, name, value)
    draft.details.update(details)


def suppress(connection: HTTPConnection) -> None:
    """Leave the request out of the audit log (throttled requests are reported in aggregate)."""
    draft = draft_of(connection)
    if draft is not None:
        draft.suppressed = True


def outcome_of(status: int) -> str:
    """Coarse result recorded with each entry."""
    if status in DENIED_STATUSES:
        return "denied"
    if status >= 500:
        return "error"
    return "rejected" if status >= 400 else "success"


class AuditWriter:
    """Writes entries through the store; failures are logged and counted, not raised."""

    def __init__(
        self,
        store: AuthStore,
        settings: AuditSettings,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self._store = store
        self._timeout_s = settings.write_timeout_s
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("lorebase.keeper")
        self._failures = meter.create_counter(
            "lorebase.audit.write_failures",
            unit="{entry}",
            description="Audit entries that could not be written.",
        )

    async def write(self, entry: NewAuditEntry) -> bool:
        """Append an entry; false when it was dropped."""
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._store.unit() as unit:
                    await unit.audit.append(entry)
        except Exception as exc:
            self._failures.add(1, {"reason": type(exc).__name__})
            logger.error(
                "audit entry dropped",
                exc_info=True,
                extra={"action": entry.action, "actor": entry.actor},
            )
            return False
        return True


ReadClassifier = Callable[[str, str], bool]
"""Whether a request (method, route template without the API prefix) only reads."""


class AuditMiddleware:
    """Records one audit entry per mutating or denied request, and hands reads to the read
    auditor when there is one."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        writer: AuditWriter,
        settings: AuditSettings,
        api_prefix: str = "",
        reads: ReadAuditor | None = None,
        is_read: ReadClassifier | None = None,
    ) -> None:
        self.app = app
        self._writer = writer
        self._record_denied = settings.record_denied
        self._api_prefix = api_prefix
        self._reads = reads
        self._is_read = is_read

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        draft = AuditDraft()
        scope.setdefault("state", {})[STATE_KEY] = draft
        status = 500

        async def send_and_note(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with collecting() as notes:
            try:
                await self.app(scope, receive, send_and_note)
            finally:
                entry = self._entry(scope, draft, status)
                if entry is not None:
                    # Shielded: a client disconnect must not cancel the write half way.
                    await asyncio.shield(self._writer.write(entry))
                elif self._reads is not None:
                    self._offer_read(scope, draft, status, notes)

    def _offer_read(self, scope: Scope, draft: AuditDraft, status: int, notes: ReadNotes) -> None:
        """Hand a successful read of an authenticated caller to the read auditor."""
        reads = self._reads
        if reads is None or draft.suppressed or draft.actor is None or not 200 <= status < 300:
            return
        route = self._route_template(scope)
        relative = route[len(self._api_prefix) :] if route.startswith(self._api_prefix) else route
        if self._is_read is None or not self._is_read(scope["method"], relative or "/"):
            return
        kind = _path_param(scope, "kind")
        sensitive = sorted(notes.sensitive)
        if not reads.wants(draft.principal_type, kind, bool(sensitive)):
            return
        details: dict[str, Any] = {
            "outcome": "success",
            "status": status,
            "method": scope["method"],
            "route": route,
        }
        if draft.principal_type:
            details["principal_type"] = draft.principal_type
        if kind:
            details["kind"] = kind
        if sensitive:
            details["sensitive"] = sensitive
        if draft.permission:
            details["permission"] = draft.permission
        if request_id := request_id_of(scope.get("state")):
            details["request_id"] = request_id
        reads.offer(
            NewAuditEntry(
                actor=draft.actor,
                action=READ_ACTION,
                domain_id=draft.domain_id or _path_param(scope, "domain_id"),
                target=draft.target or kind,
                details=details,
                at=datetime.now(UTC),
            )
        )

    def _route_template(self, scope: Scope) -> str:
        """Route path with its parameters unexpanded, including the API prefix."""
        path = getattr(scope.get("route"), "path", None)
        if not isinstance(path, str):
            return str(scope["path"])
        under_prefix = self._api_prefix and str(scope["path"]).startswith(self._api_prefix)
        return (
            self._api_prefix + path
            if under_prefix and not path.startswith(self._api_prefix)
            else path
        )

    def _entry(self, scope: Scope, draft: AuditDraft, status: int) -> NewAuditEntry | None:
        if draft.suppressed:
            return None
        method = scope["method"]
        denied = status in DENIED_STATUSES
        recorded_mutation = method in MUTATING_METHODS and draft.actor is not None
        recorded_denial = denied and self._record_denied
        if not (recorded_mutation or recorded_denial):
            return None
        route = self._route_template(scope)
        domain_id = draft.domain_id or _path_param(scope, "domain_id")
        details: dict[str, Any] = {
            "outcome": outcome_of(status),
            "status": status,
            "method": method,
            "route": route,
            **draft.details,
        }
        if draft.permission:
            details["permission"] = draft.permission
        if request_id := request_id_of(scope.get("state")):
            details["request_id"] = request_id
        return NewAuditEntry(
            actor=draft.actor or ANONYMOUS_ACTOR,
            action=draft.action or f"{method} {route}",
            domain_id=domain_id,
            target=draft.target,
            details=details,
        )


def _path_param(scope: Mapping[str, Any], name: str) -> str | None:
    value = (scope.get("path_params") or {}).get(name)
    return value if isinstance(value, str) else None
