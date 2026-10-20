"""Audit of mutating and denied requests (design 10.4).

`AuditMiddleware` wraps the application. For every request it creates an `AuditDraft` in the
ASGI scope state; dependencies and routes fill it in (`annotate`): who called, which Domain,
a precise action name and target. When the response is complete the middleware writes one
audit entry if the request mutated state (POST, PUT, PATCH, DELETE by an authenticated caller)
or was denied (401, 403 when `audit.record_denied`).

Writing happens after the response has been produced and never changes it: a failed or slow
write is logged, counted in the `lorebase.audit.write_failures` metric, and dropped. Request
bodies, headers and credentials are never recorded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lorebase_keeper.auth.config import AuditSettings
from lorebase_keeper.auth.ports import AuthStore
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
    details: dict[str, Any] = field(default_factory=dict)


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
    ):
        if value is not None:
            setattr(draft, name, value)
    draft.details.update(details)


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


class AuditMiddleware:
    """Records one audit entry per mutating or denied request."""

    def __init__(
        self, app: ASGIApp, *, writer: AuditWriter, settings: AuditSettings, api_prefix: str = ""
    ) -> None:
        self.app = app
        self._writer = writer
        self._record_denied = settings.record_denied
        self._api_prefix = api_prefix

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

        try:
            await self.app(scope, receive, send_and_note)
        finally:
            entry = self._entry(scope, draft, status)
            if entry is not None:
                # Shielded: a client disconnect must not cancel the write half way.
                await asyncio.shield(self._writer.write(entry))

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
