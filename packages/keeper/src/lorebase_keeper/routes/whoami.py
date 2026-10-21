"""Identify the caller."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from lorebase_keeper.auth.deps import PrincipalDep
from lorebase_keeper.errors import Problem
from lorebase_keeper.ratelimit.enforce import limits_of
from lorebase_keeper.ratelimit.quotas import QuotaSnapshot
from lorebase_store.models.enums import PrincipalType

router = APIRouter(tags=["auth"])


class QuotaUse(BaseModel):
    """Use of one daily quota. Without a limit, `limit` and `remaining` are null."""

    limit: int | None
    used: int
    remaining: int | None


class QuotaReport(BaseModel):
    """Daily quotas of an API key and what it has used since the last reset."""

    period: Literal["day"] = "day"
    resets_at: datetime = Field(description="Start of the next UTC day, when the counters reset.")
    requests: QuotaUse
    context_tokens: QuotaUse = Field(description="Estimated tokens of context packs delivered.")


def _use(used: int, limit: int | None) -> QuotaUse:
    return QuotaUse(
        limit=limit, used=used, remaining=None if limit is None else max(0, limit - used)
    )


def quota_report(snapshot: QuotaSnapshot) -> QuotaReport:
    """The report for a key's use."""
    return QuotaReport(
        resets_at=snapshot.resets_at,
        requests=_use(snapshot.requests_used, snapshot.requests_limit),
        context_tokens=_use(snapshot.tokens_used, snapshot.tokens_limit),
    )


class WhoAmI(BaseModel):
    """The authenticated caller."""

    id: str
    type: PrincipalType
    display_name: str
    method: str
    subjects: list[str]
    key_scopes: list[str]
    installation_admin: bool
    quota: QuotaReport | None = Field(
        default=None, description="Daily quotas of the API key; null for users and without quotas."
    )


@router.get(
    "/auth/whoami",
    summary="Identify the caller",
    responses={401: {"model": Problem, "description": "No valid credential."}},
)
async def whoami(request: Request, principal: PrincipalDep) -> WhoAmI:
    """Who the credential of this request belongs to, with the ids grants may name and, for an
    API key, its daily quotas."""
    limits = limits_of(request)
    quota = None
    if limits is not None and limits.quotas is not None and principal.key_id is not None:
        quota = quota_report(await limits.quotas.snapshot(principal.key_id))
    return WhoAmI(
        id=principal.id,
        type=principal.type,
        display_name=principal.display_name,
        method=principal.method,
        subjects=list(principal.subjects),
        key_scopes=list(principal.key_scopes),
        installation_admin=principal.installation_admin,
        quota=quota,
    )
