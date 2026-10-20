"""Webhooks of a Domain (design 10.5): create, list, delete.

Managing webhooks needs `domain.settings` (the `admin` role). A new webhook's secret is
returned once; only its digest is stored (`lorebase_store.webhooks.signing`). Every change is
written to the audit log (`webhook.create`, `webhook.delete`) without the secret or the full
URL. Deliveries are made by `lorebase_store.webhooks.deliver_pending`, run by a worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.errors import CONFLICT, NOT_FOUND, Problem, ProblemError, ValidationProblem
from lorebase_keeper.reading.problems import invalid
from lorebase_platform.settings import Settings
from lorebase_store.changefeed import EVENT_TYPES
from lorebase_store.webhooks import (
    WebhookRepository,
    WebhookRow,
    WebhookSettings,
    new_secret,
    signing_key,
    webhook_settings,
)
from lorebase_store.webhooks.delivery import scheme_problem

router = APIRouter(tags=["webhooks"])

BASE = "/domains/{domain_id}/webhooks"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks domain.settings."},
    404: {"model": Problem, "description": "Domain or webhook not found."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}

Manage = Annotated[Access, Depends(require(Permission.DOMAIN_SETTINGS))]


def get_webhook_settings(request: Request) -> WebhookSettings:
    return webhook_settings(cast(Settings, request.app.state.settings))


WebhookSettingsDep = Annotated[WebhookSettings, Depends(get_webhook_settings)]


class CreateWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048, description="Target of the POST requests.")
    events: list[str] | None = Field(
        default=None, min_length=1, description="Event types delivered; default: every type."
    )
    branches: list[str] | None = Field(
        default=None, min_length=1, description="Only events of these branches; default: all."
    )
    description: str | None = Field(default=None, max_length=500)


class WebhookOut(BaseModel):
    id: str
    url: str
    description: str | None = None
    events: list[str]
    branches: list[str] | None = None
    created_by: str
    created_at: datetime


class WebhookCreated(WebhookOut):
    """A new webhook; `secret` is shown this once."""

    secret: str = Field(
        description="Signing secret. Requests carry `t=<unix>,v1=<hex HMAC-SHA256>` computed "
        "over `<t>.<body>` with the key SHA-256(secret)."
    )


class WebhookList(BaseModel):
    items: list[WebhookOut]


def _out(row: WebhookRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "url": row.url,
        "description": row.description,
        "events": list(row.event_types),
        "branches": None if row.branches is None else list(row.branches),
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


@router.post(BASE, status_code=201, summary="Create a webhook", responses=_ERRORS)
async def create_webhook(
    domain_id: str,
    body: CreateWebhook,
    access: Manage,
    store: StoreDep,
    settings: WebhookSettingsDep,
    request: Request,
) -> WebhookCreated:
    """Register an endpoint for the Domain's events; the secret is returned once."""
    problem = scheme_problem(body.url, settings)
    if problem is not None:
        raise invalid("/body/url", problem, "invalid_target")
    events = body.events or list(EVENT_TYPES)
    for index, event in enumerate(events):
        if event not in EVENT_TYPES:
            raise invalid(f"/body/events/{index}", f"unknown event type {event!r}", "unknown")
    secret = new_secret(settings.secret_bytes)
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        webhooks = WebhookRepository(session, domain_id)
        if await webhooks.count() >= settings.max_per_domain:
            raise ProblemError(
                CONFLICT, f"The Domain already has {settings.max_per_domain} webhooks."
            )
        row = await webhooks.add(
            url=body.url,
            description=body.description,
            event_types=sorted(set(events)),
            branches=None if body.branches is None else sorted(set(body.branches)),
            secret_hash=signing_key(secret),
            created_by=access.principal.id,
        )
    annotate(
        request,
        action="webhook.create",
        target=row.id,
        host=urlsplit(row.url).hostname,
        events=list(row.event_types),
        branches=None if row.branches is None else list(row.branches),
    )
    return WebhookCreated(**_out(row), secret=secret)


@router.get(BASE, summary="List webhooks", response_model=WebhookList, responses=_ERRORS)
async def list_webhooks(domain_id: str, access: Manage, store: StoreDep) -> WebhookList:
    """Webhooks of the Domain, oldest first (secrets are never shown again)."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        rows = await WebhookRepository(session, domain_id).all()
    return WebhookList(items=[WebhookOut(**_out(row)) for row in rows])


@router.delete(
    f"{BASE}/{{webhook_id}}", status_code=204, summary="Delete a webhook", responses=_ERRORS
)
async def delete_webhook(
    domain_id: str, webhook_id: str, access: Manage, store: StoreDep, request: Request
) -> Response:
    """Remove a webhook and its pending deliveries."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        deleted = await WebhookRepository(session, domain_id).delete(webhook_id)
    if not deleted:
        raise ProblemError(NOT_FOUND, f"Webhook {webhook_id!r} not found.")
    annotate(request, action="webhook.delete", target=webhook_id)
    return Response(status_code=204)
