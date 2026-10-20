"""Administration: API keys, principals, grants and the audit log (design 11.1, 10.2, 13.2).

Domain administration (`apikey.manage`, `grant.manage`, `audit.read`) is allowed to holders of
the `admin` role in that Domain. Listing principals is installation-level. Every change is
written to the audit log with a precise action name; secrets never are.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_keeper.auth.deps import Access, AuthDep, require, require_installation, require_store
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.ports import AuthUnitPort
from lorebase_keeper.auth.rbac import domain_scope
from lorebase_keeper.errors import (
    BAD_REQUEST,
    NOT_FOUND,
    VALIDATION_FAILED,
    Problem,
    ProblemError,
    ValidationProblem,
)
from lorebase_keeper.pagination import (
    Page,
    PageParams,
    PageParamsDep,
    encode_cursor,
)
from lorebase_store.auth import (
    ApiKeyRecord,
    AuditRecord,
    GrantRecord,
    PrefixTakenError,
    PrincipalRecord,
)
from lorebase_store.models.enums import GrantScope, PrincipalType

router = APIRouter(tags=["admin"])

KEY_CREATION_ATTEMPTS = 5

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission named in the body."},
    404: {"model": Problem, "description": "Not found."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}


class ApiKeyOut(BaseModel):
    """An API key. The secret is never shown again after creation."""

    id: str
    principal_id: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    """A new API key; `key` is the only time the secret is returned."""

    key: str
    role: str | None = Field(
        default=None, description="Role granted to the new service principal in the Domain."
    )


class CreateApiKey(BaseModel):
    """Request to create an API key for a Domain."""

    name: str = Field(min_length=1, max_length=120, description="Name of the service principal.")
    role: str = Field(
        default="agent-reader",
        description="Role granted in this Domain to the new service principal.",
    )
    ttl_days: int | None = Field(
        default=None, ge=1, description="Lifetime in days; the configured default when omitted."
    )
    principal_id: str | None = Field(
        default=None,
        description="Add the key to an existing service principal instead of creating one; "
        "its grants are left as they are.",
    )


class PrincipalOut(BaseModel):
    """A user or service principal."""

    id: str
    type: PrincipalType
    display_name: str
    external_subject: str | None
    created_at: datetime


class GrantOut(BaseModel):
    """A role of a principal or group in a Domain."""

    id: str
    domain_id: str
    subject: str
    role: str
    scope_type: GrantScope
    scope_ref: str | None
    created_by: str
    created_at: datetime


class CreateGrant(BaseModel):
    """Request to grant a role."""

    subject: str = Field(min_length=1, description="Principal or group id.")
    role: str
    scope_type: GrantScope = GrantScope.DOMAIN
    scope_ref: str | None = Field(
        default=None,
        min_length=1,
        description="Kind api name, Operation name or branch pattern; required unless the "
        "scope is the whole Domain.",
    )


class AuditEntryOut(BaseModel):
    """An audit log entry."""

    seq: int
    at: datetime
    domain_id: str | None
    actor: str
    action: str
    target: str | None
    details: dict[str, Any]
    entry_hash: str


def _invalid(pointer: str, message: str) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": "invalid"}]},
    )


def _key_out(key: ApiKeyRecord) -> dict[str, Any]:
    return {
        "id": key.id,
        "principal_id": key.principal_id,
        "prefix": key.prefix,
        "scopes": list(key.scopes),
        "expires_at": key.expires_at,
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
        "created_at": key.created_at,
    }


def _principal_out(principal: PrincipalRecord) -> PrincipalOut:
    return PrincipalOut(
        id=principal.id,
        type=principal.type,
        display_name=principal.display_name,
        external_subject=principal.external_subject,
        created_at=principal.created_at,
    )


def _grant_out(grant: GrantRecord) -> GrantOut:
    return GrantOut(
        id=grant.id,
        domain_id=grant.domain_id,
        subject=grant.subject,
        role=grant.role,
        scope_type=grant.scope_type,
        scope_ref=grant.scope_ref,
        created_by=grant.created_by,
        created_at=grant.created_at,
    )


def _audit_out(entry: AuditRecord) -> AuditEntryOut:
    return AuditEntryOut(
        seq=entry.seq,
        at=entry.at,
        domain_id=entry.domain_id,
        actor=entry.actor,
        action=entry.action,
        target=entry.target,
        details=dict(entry.details),
        entry_hash=entry.entry_hash.hex(),
    )


def _after(params: PageParams) -> tuple[datetime, str] | None:
    """Keyset position `(created_at, id)` carried by a cursor."""
    if params.position is None:
        return None
    try:
        return datetime.fromisoformat(params.position["at"]), str(params.position["id"])
    except (KeyError, ValueError, TypeError):
        raise _invalid("/query/cursor", "is not a valid cursor") from None


def _slice[R, M](
    rows: list[R],
    limit: int,
    convert: Callable[[R], M],
    position: Callable[[R], Mapping[str, Any]],
) -> tuple[list[M], str | None]:
    """The first `limit` of `limit + 1` fetched rows, with a cursor when more remain."""
    shown = rows[:limit]
    next_cursor = encode_cursor(position(shown[-1])) if len(rows) > limit and shown else None
    return [convert(row) for row in shown], next_cursor


def _created_position(row: ApiKeyRecord | PrincipalRecord | GrantRecord) -> dict[str, str]:
    return {"at": row.created_at.isoformat(), "id": row.id}


def _expiry(settings: ApiKeySettings, ttl_days: int | None, now: datetime) -> datetime | None:
    days = ttl_days if ttl_days is not None else settings.default_ttl_days
    if settings.max_ttl_days is not None and (days is None or days > settings.max_ttl_days):
        raise _invalid("/body/ttl_days", f"must not exceed {settings.max_ttl_days}")
    return None if days is None else now + timedelta(days=days)


def _known_role(auth: AuthDep, role: str) -> None:
    if role not in auth.authorizer.role_names:
        raise _invalid("/body/role", f"unknown role {role!r}")


@router.post(
    "/domains/{domain_id}/api-keys",
    status_code=201,
    summary="Create an API key",
    responses=_ERRORS,
)
async def create_api_key(
    domain_id: str,
    body: CreateApiKey,
    request: Request,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.APIKEY_MANAGE))],
) -> ApiKeyCreated:
    """Create a key bound to a service principal and limited to this Domain.

    Unless `principal_id` names an existing service principal, a new one is created and given
    `role` in the Domain. The secret in `key` is shown once.
    """
    require_store(auth)
    assert auth.store is not None
    if auth.codec is None:
        raise ProblemError(BAD_REQUEST, "API keys are not enabled (keeper.auth.mode).")
    _known_role(auth, body.role)
    now = auth.clock()
    expires_at = _expiry(auth.settings.api_keys, body.ttl_days, now)
    actor = access.principal.id
    try:
        async with auth.store.unit() as unit:
            if body.principal_id is not None:
                service = await unit.principals.get(body.principal_id)
                if service is None or service.type is not PrincipalType.SERVICE:
                    raise ProblemError(NOT_FOUND, "Service principal not found.")
                role = None
            else:
                service = await unit.principals.add(PrincipalType.SERVICE, body.name)
                await unit.grants.add(
                    domain_id=domain_id, subject=service.id, role=body.role, created_by=actor
                )
                role = body.role
            token, key = await _add_key(
                auth, unit, service.id, [domain_scope(domain_id)], expires_at
            )
    except IntegrityError:
        raise ProblemError(NOT_FOUND, "Domain not found.") from None
    auth.authorizer.invalidate()
    annotate(
        request,
        action="apikey.create",
        target=key.id,
        principal_id=service.id,
        prefix=key.prefix,
        role=role,
        expires_at=expires_at.isoformat() if expires_at else None,
    )
    return ApiKeyCreated(**_key_out(key), key=token, role=role)


async def _add_key(
    auth: AuthDep,
    unit: AuthUnitPort,
    principal_id: str,
    scopes: list[str],
    expires_at: datetime | None,
) -> tuple[str, ApiKeyRecord]:
    assert auth.codec is not None
    for _ in range(KEY_CREATION_ATTEMPTS):
        generated = auth.codec.generate()
        try:
            key = await unit.api_keys.add(
                principal_id=principal_id,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=scopes,
                expires_at=expires_at,
            )
        except PrefixTakenError:
            continue
        return generated.token, key
    raise RuntimeError("could not find an unused API key prefix")


@router.get(
    "/domains/{domain_id}/api-keys",
    summary="List the API keys of a Domain",
    responses=_ERRORS,
)
async def list_api_keys(
    domain_id: str,
    params: PageParamsDep,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.APIKEY_MANAGE))],
) -> Page[ApiKeyOut]:
    """Keys limited to this Domain, oldest first, including revoked and expired ones."""
    require_store(auth)
    assert auth.store is not None
    async with auth.store.unit() as unit:
        rows = await unit.api_keys.list_for_scope(
            domain_scope(domain_id), limit=params.limit + 1, after=_after(params)
        )
    items, next_cursor = _slice(
        rows, params.limit, lambda key: ApiKeyOut(**_key_out(key)), _created_position
    )
    return Page[ApiKeyOut](items=items, next_cursor=next_cursor)


@router.delete(
    "/domains/{domain_id}/api-keys/{key_id}",
    status_code=204,
    summary="Revoke an API key",
    responses=_ERRORS,
)
async def revoke_api_key(
    domain_id: str,
    key_id: str,
    request: Request,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.APIKEY_MANAGE))],
) -> Response:
    """Revoke a key of this Domain. Revoking a revoked key succeeds."""
    require_store(auth)
    assert auth.store is not None
    async with auth.store.unit() as unit:
        key = await unit.api_keys.get(key_id)
        if key is None or domain_scope(domain_id) not in key.scopes:
            raise ProblemError(NOT_FOUND, "API key not found.")
        await unit.api_keys.revoke(key_id, auth.clock())
    annotate(request, action="apikey.revoke", target=key_id, principal_id=key.principal_id)
    return Response(status_code=204)


@router.get("/principals", summary="List principals", responses=_ERRORS)
async def list_principals(
    params: PageParamsDep,
    auth: AuthDep,
    access: Annotated[Access, Depends(require_installation(Permission.PRINCIPAL_READ))],
    type_: Annotated[PrincipalType | None, Query(alias="type")] = None,
) -> Page[PrincipalOut]:
    """Users and service principals of the installation, oldest first."""
    require_store(auth)
    assert auth.store is not None
    async with auth.store.unit() as unit:
        rows = await unit.principals.list(type_=type_, limit=params.limit + 1, after=_after(params))
    items, next_cursor = _slice(rows, params.limit, _principal_out, _created_position)
    return Page[PrincipalOut](items=items, next_cursor=next_cursor)


@router.post(
    "/domains/{domain_id}/grants", status_code=201, summary="Grant a role", responses=_ERRORS
)
async def create_grant(
    domain_id: str,
    body: CreateGrant,
    request: Request,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.GRANT_MANAGE))],
) -> GrantOut:
    """Give a principal or group a role in the Domain, optionally narrowed to a scope."""
    require_store(auth)
    assert auth.store is not None
    _known_role(auth, body.role)
    if (body.scope_type is GrantScope.DOMAIN) != (body.scope_ref is None):
        raise _invalid("/body/scope_ref", "is required for kind, operation and branch scopes only")
    try:
        async with auth.store.unit() as unit:
            if not await unit.groups.subject_exists(body.subject):
                raise ProblemError(NOT_FOUND, "Subject not found.")
            grant = await unit.grants.add(
                domain_id=domain_id,
                subject=body.subject,
                role=body.role,
                created_by=access.principal.id,
                scope_type=body.scope_type,
                scope_ref=body.scope_ref,
            )
    except IntegrityError:
        raise ProblemError(NOT_FOUND, "Domain not found.") from None
    auth.authorizer.invalidate()
    annotate(
        request,
        action="grant.create",
        target=grant.id,
        subject=grant.subject,
        role=grant.role,
        scope_type=grant.scope_type.value,
        scope_ref=grant.scope_ref,
    )
    return _grant_out(grant)


@router.get("/domains/{domain_id}/grants", summary="List grants", responses=_ERRORS)
async def list_grants(
    domain_id: str,
    params: PageParamsDep,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.GRANT_MANAGE))],
) -> Page[GrantOut]:
    """Grants of the Domain, oldest first."""
    require_store(auth)
    assert auth.store is not None
    async with auth.store.unit() as unit:
        rows = await unit.grants.list_domain(
            domain_id, limit=params.limit + 1, after=_after(params)
        )
    items, next_cursor = _slice(rows, params.limit, _grant_out, _created_position)
    return Page[GrantOut](items=items, next_cursor=next_cursor)


@router.delete(
    "/domains/{domain_id}/grants/{grant_id}",
    status_code=204,
    summary="Revoke a grant",
    responses=_ERRORS,
)
async def revoke_grant(
    domain_id: str,
    grant_id: str,
    request: Request,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.GRANT_MANAGE))],
) -> Response:
    """Remove a grant from the Domain."""
    require_store(auth)
    assert auth.store is not None
    async with auth.store.unit() as unit:
        grant = await unit.grants.get(domain_id, grant_id)
        if grant is None:
            raise ProblemError(NOT_FOUND, "Grant not found.")
        await unit.grants.delete(domain_id, grant_id)
    auth.authorizer.invalidate()
    annotate(
        request, action="grant.revoke", target=grant_id, subject=grant.subject, role=grant.role
    )
    return Response(status_code=204)


@router.get("/domains/{domain_id}/audit", summary="Read the audit log", responses=_ERRORS)
async def list_audit(
    domain_id: str,
    params: PageParamsDep,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.AUDIT_READ))],
) -> Page[AuditEntryOut]:
    """Audit entries of the Domain, newest first."""
    require_store(auth)
    assert auth.store is not None
    before: int | None = None
    if params.position is not None:
        try:
            before = int(params.position["before"])
        except (KeyError, ValueError, TypeError):
            raise _invalid("/query/cursor", "is not a valid cursor") from None
    async with auth.store.unit() as unit:
        rows = await unit.audit.list(domain_id=domain_id, limit=params.limit + 1, before_seq=before)
    items, next_cursor = _slice(rows, params.limit, _audit_out, lambda row: {"before": row.seq})
    return Page[AuditEntryOut](items=items, next_cursor=next_cursor)
