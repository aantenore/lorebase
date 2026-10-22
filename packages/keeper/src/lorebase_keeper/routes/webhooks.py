"""Webhooks of a Domain (design 10.5): create, list, update clearances, delete.

Managing webhooks needs `domain.settings` (the `admin` role). A new webhook's secret is
returned once; only its digest is stored (`lorebase_store.webhooks.signing`), sealed under
`webhooks.encryption_key` when that is set (`lorebase_store.webhooks.keys`). Every change is
written to the audit log (`webhook.create`, `webhook.update`, `webhook.delete`) without the
secret or the full URL. Deliveries are made by `lorebase_store.webhooks.deliver_pending`, run by
a worker.

A webhook subscribes to `revision.committed`, to `operation.<side effect>` events or to every
Operation event (`operation.*`). Sensitive values in Operation events reach it masked unless it
holds `clearances` for their tags; granting one needs the matching clearance permission
(`pii.read`, `confidential.read`, `restricted.read`) on the whole Domain, so an administrator
cannot route values to a webhook that they could not read themselves. `PATCH` replaces the
clearances: the permission is needed only for those it adds, so anyone managing webhooks can
narrow one.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from lorebase_core.model import Sensitivity
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, AuthDep, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.errors import (
    CONFLICT,
    FORBIDDEN,
    NOT_FOUND,
    Problem,
    ProblemError,
    ValidationProblem,
)
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.sensitivity import CLEARANCES, Clearance, clearance_of
from lorebase_platform.settings import Settings
from lorebase_store.outbox_events import EVENT_TYPES, event_type_problem
from lorebase_store.webhooks import (
    WebhookRepository,
    WebhookRow,
    WebhookSettings,
    new_secret,
    signing_key,
    webhook_settings,
)
from lorebase_store.webhooks.delivery import scheme_problem
from lorebase_store.webhooks.keys import SigningKeys

router = APIRouter(tags=["webhooks"])

BASE = "/domains/{domain_id}/webhooks"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks domain.settings."},
    404: {"model": Problem, "description": "Domain or webhook not found."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}
_CREATE_ERRORS: dict[int | str, dict[str, Any]] = {
    **_ERRORS,
    403: {
        "model": Problem,
        "description": "The caller lacks domain.settings, or a clearance it gives the webhook.",
    },
}

Manage = Annotated[Access, Depends(require(Permission.DOMAIN_SETTINGS))]


def get_webhook_settings(request: Request) -> WebhookSettings:
    return webhook_settings(cast(Settings, request.app.state.settings))


WebhookSettingsDep = Annotated[WebhookSettings, Depends(get_webhook_settings)]


class CreateWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048, description="Target of the POST requests.")
    events: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Event types delivered: revision.committed, operation.<side effect>, or "
        "operation.* for every Operation event; default: revision.committed and operation.*.",
    )
    branches: list[str] | None = Field(
        default=None, min_length=1, description="Only events of these branches; default: all."
    )
    description: str | None = Field(default=None, max_length=500)
    clearances: list[Literal["pii", "confidential", "restricted"]] = Field(
        default_factory=list,
        description="Sensitivity tags whose values the webhook receives in clear; others are "
        "masked. Each needs the matching clearance permission on the Domain.",
    )


class UpdateWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clearances: list[Literal["pii", "confidential", "restricted"]] = Field(
        description="The new clearances, replacing the current ones. Each one added needs the "
        "matching clearance permission on the Domain; removing needs only domain.settings."
    )


class WebhookOut(BaseModel):
    id: str
    url: str
    description: str | None = None
    events: list[str]
    branches: list[str] | None = None
    clearances: list[str] = Field(default_factory=list)
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
        "clearances": list(row.clearances),
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


def _require_clearances(held: Clearance, tags: Iterable[str]) -> None:
    """403 unless `held` (the caller's clearance) covers, Domain-wide, every tag."""
    wanted = set(tags)
    missing = sorted(
        CLEARANCES[Sensitivity(tag)].value
        for tag in wanted
        if Sensitivity(tag) not in held.everywhere
    )
    if missing:
        raise ProblemError(
            FORBIDDEN,
            f"Clearing a webhook for sensitive values needs {' and '.join(missing)} on the Domain.",
            extensions={"code": "clearance_required", "permission": missing[0]},
        )


@router.post(BASE, status_code=201, summary="Create a webhook", responses=_CREATE_ERRORS)
async def create_webhook(
    domain_id: str,
    body: CreateWebhook,
    access: Manage,
    store: StoreDep,
    settings: WebhookSettingsDep,
    request: Request,
    auth: AuthDep,
) -> WebhookCreated:
    """Register an endpoint for the Domain's events; the secret is returned once."""
    problem = scheme_problem(body.url, settings)
    if problem is not None:
        raise invalid("/body/url", problem, "invalid_target")
    events = body.events or list(EVENT_TYPES)
    for index, event in enumerate(events):
        unknown = event_type_problem(event)
        if unknown is not None:
            raise invalid(f"/body/events/{index}", unknown, "unknown")
    if body.clearances:
        held = await clearance_of(auth.authorizer, access.principal, domain_id, None)
        _require_clearances(held, body.clearances)
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
            signing_key=SigningKeys.from_settings(settings).store(signing_key(secret)),
            created_by=access.principal.id,
            clearances=body.clearances,
        )
    annotate(
        request,
        action="webhook.create",
        target=row.id,
        host=urlsplit(row.url).hostname,
        events=list(row.event_types),
        branches=None if row.branches is None else list(row.branches),
        clearances=list(row.clearances),
    )
    return WebhookCreated(**_out(row), secret=secret)


@router.get(BASE, summary="List webhooks", response_model=WebhookList, responses=_ERRORS)
async def list_webhooks(domain_id: str, access: Manage, store: StoreDep) -> WebhookList:
    """Webhooks of the Domain, oldest first (secrets are never shown again)."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        rows = await WebhookRepository(session, domain_id).all()
    return WebhookList(items=[WebhookOut(**_out(row)) for row in rows])


@router.patch(
    f"{BASE}/{{webhook_id}}",
    summary="Change the clearances of a webhook",
    response_model=WebhookOut,
    responses=_CREATE_ERRORS,
)
async def update_webhook(
    domain_id: str,
    webhook_id: str,
    body: UpdateWebhook,
    access: Manage,
    store: StoreDep,
    request: Request,
    auth: AuthDep,
) -> WebhookOut:
    """Replace the sensitivity clearances of a webhook; only added ones need their permission."""
    # Read before the row lock: the authorizer may be slow, the lock is held only to compare.
    held = await clearance_of(auth.authorizer, access.principal, domain_id, None)
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        webhooks = WebhookRepository(session, domain_id)
        current = await webhooks.get(webhook_id, for_update=True)
        if current is None:
            raise ProblemError(NOT_FOUND, f"Webhook {webhook_id!r} not found.")
        _require_clearances(held, set(body.clearances) - set(current.clearances))
        row = await webhooks.set_clearances(webhook_id, body.clearances)
    if row is None:
        raise ProblemError(NOT_FOUND, f"Webhook {webhook_id!r} not found.")
    annotate(
        request,
        action="webhook.update",
        target=row.id,
        clearances_before=list(current.clearances),
        clearances=list(row.clearances),
    )
    return WebhookOut(**_out(row))


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
